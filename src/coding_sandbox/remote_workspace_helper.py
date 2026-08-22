"""Fixed remote helper executed with data passed only through argv or staged files."""

from __future__ import annotations

REMOTE_WORKSPACE_HELPER = r"""
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import PurePosixPath


class Failure(Exception):
    def __init__(self, code):
        self.code = code


def fail(code):
    raise Failure(code)


def relative(value, allow_root=False):
    if not value or "\x00" in value or "\\" in value:
        fail("unsafe_path")
    if allow_root and value == ".":
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or value == "." or ".." in path.parts or str(path) != value:
        fail("unsafe_path")
    return value


def root_path(value):
    if not value.startswith("/") or ".." in PurePosixPath(value).parts:
        fail("unsafe_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(value, flags)
    os.close(descriptor)
    return value


def resolve_existing(root, value, expect_directory=False):
    relative(value, allow_root=True)
    current = root
    if value != ".":
        for part in PurePosixPath(value).parts:
            candidate = os.path.join(current, part)
            info = os.stat(candidate, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                fail("unsafe_path")
            current = candidate
    info = os.stat(current, follow_symlinks=False)
    if expect_directory and not stat.S_ISDIR(info.st_mode):
        fail("not_file")
    return current


def secure_parent_fd(root, value, create=False):
    relative(value)
    parts = PurePosixPath(value).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def digest_stream(stream):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def secure_directory_fd(root, value):
    relative(value, allow_root=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        if value != ".":
            for part in PurePosixPath(value).parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            fail("not_file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def digest_relative_file(root, value):
    parent, leaf = secure_parent_fd(root, value)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(leaf, flags, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                fail("not_file")
            with os.fdopen(os.dup(descriptor), "rb", buffering=0) as stream:
                size, digest = digest_stream(stream)
            final = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    )
    final_identity = (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    )
    if opened_identity != final_identity or size != final.st_size:
        fail("protocol_error")
    return size, digest


def safe_files(root, selected, max_files):
    descriptor = secure_directory_fd(root, selected)
    os.close(descriptor)
    results = []
    pending = [selected]
    while pending:
        directory = pending.pop()
        directory_descriptor = secure_directory_fd(root, directory)
        try:
            with os.scandir(directory_descriptor) as entries:
                ordered = sorted(entries, key=lambda item: item.name, reverse=True)
                observed = [
                    (entry.name, entry.stat(follow_symlinks=False))
                    for entry in ordered
                ]
        finally:
            os.close(directory_descriptor)
        for entry_name, info in observed:
            rel = entry_name if directory == "." else directory + "/" + entry_name
            relative(rel)
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(rel)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            size, digest = digest_relative_file(root, rel)
            results.append({"path": rel, "size": size, "sha256": digest})
            if len(results) > max_files:
                return sorted(results[:max_files], key=lambda item: item["path"]), True
    return sorted(results, key=lambda item: item["path"]), False


def scan(root, args):
    selected = args[0]
    maximum = int(args[1])
    if maximum < 1 or maximum > 100000:
        fail("resource_limit")
    files, truncated = safe_files(root, selected, maximum)
    return {"root": selected, "files": files, "truncated": truncated}


def fingerprint(root, args):
    max_files = int(args[0])
    max_total_bytes = int(args[1])
    if max_files < 1 or max_total_bytes < 1:
        fail("resource_limit")
    files, truncated = safe_files(root, ".", max_files)
    if truncated:
        fail("resource_limit")
    total_bytes = sum(item["size"] for item in files)
    if total_bytes > max_total_bytes:
        fail("resource_limit")
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def read_file(root, args):
    selected = relative(args[0])
    maximum = int(args[1])
    if maximum < 1:
        fail("resource_limit")
    parent, leaf = secure_parent_fd(root, selected)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(leaf, flags, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                fail("not_file")
            if info.st_size > maximum:
                fail("file_too_large")
            data = b""
            while len(data) <= maximum:
                chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > maximum:
                fail("file_too_large")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        fail("invalid_encoding")
    return {
        "path": selected,
        "content": content,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def search(root, args):
    selected = args[0]
    query = args[1]
    case_sensitive = args[2] == "1"
    max_matches = int(args[3])
    max_file_bytes = int(args[4])
    max_files = int(args[5])
    if not query or len(query.encode("utf-8")) > 4096:
        fail("resource_limit")
    files, files_truncated = safe_files(root, selected, max_files)
    needle = query if case_sensitive else query.casefold()
    matches = []
    for item in files:
        if item["size"] > max_file_bytes:
            continue
        target = resolve_existing(root, item["path"])
        try:
            with open(target, "r", encoding="utf-8", errors="strict") as stream:
                for line_number, line in enumerate(stream, start=1):
                    haystack = line if case_sensitive else line.casefold()
                    column = haystack.find(needle)
                    if column < 0:
                        continue
                    matches.append({
                        "path": item["path"],
                        "line": line_number,
                        "column": column + 1,
                        "text": line.rstrip("\r\n")[:500],
                    })
                    if len(matches) >= max_matches:
                        return {
                            "query": query,
                            "root": selected,
                            "matches": matches,
                            "truncated": True,
                        }
        except UnicodeDecodeError:
            continue
    return {
        "query": query,
        "root": selected,
        "matches": matches,
        "truncated": files_truncated,
    }


def write_file(root, args):
    selected = relative(args[0])
    staging = args[1]
    overwrite = args[2] == "1"
    maximum = int(args[3])
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(staging, flags)
    try:
        source_info = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_info.st_mode):
            fail("not_file")
        if source_info.st_size > maximum:
            fail("file_too_large")
        data = b""
        while len(data) <= maximum:
            chunk = os.read(
                source_descriptor,
                min(1024 * 1024, maximum + 1 - len(data)),
            )
            if not chunk:
                break
            data += chunk
        if len(data) > maximum:
            fail("file_too_large")
    finally:
        os.close(source_descriptor)
    parent, leaf = secure_parent_fd(root, selected, create=True)
    temporary = ".pi-agent-write-" + uuid.uuid4().hex
    created = True
    try:
        try:
            existing = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            created = False
            if not stat.S_ISREG(existing.st_mode):
                fail("unsafe_path")
            if not overwrite:
                fail("patch_conflict")
        destination = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=parent,
        )
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(destination, data[offset:])
            os.fsync(destination)
        finally:
            os.close(destination)
        os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
    return {
        "path": selected,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "created": created,
    }


def delete_file(root, args):
    selected = relative(args[0])
    parent, leaf = secure_parent_fd(root, selected)
    try:
        info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            fail("not_file")
        os.unlink(leaf, dir_fd=parent)
    finally:
        os.close(parent)
    return {"path": selected}


try:
    action = sys.argv[1]
    workspace_root = root_path(sys.argv[2])
    arguments = sys.argv[3:]
    handlers = {
        "scan": scan,
        "fingerprint": fingerprint,
        "read": read_file,
        "search": search,
        "write": write_file,
        "delete": delete_file,
    }
    if action not in handlers:
        fail("protocol_error")
    payload = handlers[action](workspace_root, arguments)
    print(json.dumps({"ok": True, "result": payload}, separators=(",", ":")))
except FileNotFoundError:
    print(json.dumps({"ok": False, "error": "not_found"}, separators=(",", ":")))
    raise SystemExit(2)
except PermissionError:
    print(json.dumps({"ok": False, "error": "unsafe_path"}, separators=(",", ":")))
    raise SystemExit(2)
except Failure as error:
    print(json.dumps({"ok": False, "error": error.code}, separators=(",", ":")))
    raise SystemExit(2)
except Exception:
    print(json.dumps({"ok": False, "error": "protocol_error"}, separators=(",", ":")))
    raise SystemExit(2)
"""

__all__ = ["REMOTE_WORKSPACE_HELPER"]

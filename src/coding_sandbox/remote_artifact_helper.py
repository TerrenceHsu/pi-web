"""Fixed remote exporter used to build a deterministic Sandbox artifact tar."""

from __future__ import annotations

REMOTE_ARTIFACT_HELPER = r"""
import codecs
import fnmatch
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import uuid
from pathlib import PurePosixPath


class Failure(Exception):
    def __init__(self, code):
        self.code = code


def fail(code):
    raise Failure(code)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def relative(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or "\x00" in value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        fail("artifact_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value == "." or ".." in path.parts or str(path) != value:
        fail("artifact_invalid")
    return value


def absolute_temp_path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/tmp/pi-agent-")
        or "\x00" in value
        or "\\" in value
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
    ):
        fail("artifact_invalid")
    return value


def root_path(value):
    if not value.startswith("/") or ".." in PurePosixPath(value).parts:
        fail("artifact_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(value, flags)
    os.close(descriptor)
    return value


def secure_file(root, value):
    relative(value)
    parts = PurePosixPath(value).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=descriptor)
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_descriptor)
            fail("artifact_invalid")
        return file_descriptor, info
    finally:
        os.close(descriptor)


def secure_directory(root, value):
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        if value != ".":
            relative(value)
            for part in PurePosixPath(value).parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            fail("artifact_invalid")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def digest_descriptor(descriptor, maximum):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > maximum:
            fail("resource_limit")
        digest.update(chunk)
    return size, digest.hexdigest()


def scan(root, max_files, max_file_bytes, max_total_bytes):
    results = []
    total = 0
    pending = ["."]
    while pending:
        directory = pending.pop()
        directory_descriptor = secure_directory(root, directory)
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
            relative_path = (
                entry_name
                if directory == "."
                else directory + "/" + entry_name
            )
            relative(relative_path)
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(relative_path)
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            descriptor, opened = secure_file(root, relative_path)
            try:
                size, digest = digest_descriptor(descriptor, max_file_bytes)
                final = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            final_identity = (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            if identity != final_identity or size != final.st_size:
                fail("workspace_changed")
            total += size
            if total > max_total_bytes:
                fail("resource_limit")
            results.append({"path": relative_path, "size": size, "sha256": digest})
            if len(results) > max_files:
                fail("resource_limit")
    return sorted(results, key=lambda item: item["path"])


def workspace_digest(files):
    digest = hashlib.sha256()
    for item in files:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_request(path, maximum, expected_sha256):
    path = absolute_temp_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum:
            fail("resource_limit")
        data = b""
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
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
    if len(data) > maximum:
        fail("resource_limit")
    if (
        len(data) != final.st_size
        or opened_identity != final_identity
        or hashlib.sha256(data).hexdigest() != expected_sha256
    ):
        fail("artifact_invalid")
    try:
        value = json.loads(data)
    except Exception:
        fail("artifact_invalid")
    if not isinstance(value, dict):
        fail("artifact_invalid")
    return value


def hex_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def validate_exclusions(value, name):
    values = value.get(name)
    if not isinstance(values, list) or len(values) > 1024:
        fail("artifact_invalid")
    normalized = []
    folded = set()
    for item in values:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 1024
            or "\x00" in item
            or item.casefold() in folded
        ):
            fail("artifact_invalid")
        folded.add(item.casefold())
        normalized.append(item)
    return normalized


def matches(value, patterns):
    folded = value.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def excluded(path, request):
    parts = PurePosixPath(path).parts
    directory_names = {
        item.casefold() for item in request["excluded_directory_names"]
    }
    file_names = {item.casefold() for item in request["excluded_file_names"]}
    patterns = request["excluded_globs"]
    for index, part in enumerate(parts[:-1], start=1):
        directory_path = PurePosixPath(*parts[:index]).as_posix()
        if part.casefold() in directory_names or matches(directory_path, patterns):
            return True
    return (
        parts[-1].casefold() in file_names
        or matches(path, patterns)
        or matches(parts[-1], patterns)
    )


def validate_request(value):
    expected = {
        "schema_version",
        "artifact_id",
        "operation_id",
        "workspace_revision",
        "created_at_ms",
        "expected_workspace_sha256",
        "baseline_sha256",
        "baseline",
        "validation_evidence",
        "excluded_directory_names",
        "excluded_file_names",
        "excluded_globs",
        "max_file_count",
        "max_file_bytes",
        "max_total_bytes",
        "max_archive_bytes",
    }
    if set(value) != expected or value["schema_version"] != "pi-agent-artifact-export/v1":
        fail("artifact_invalid")
    artifact_id = value["artifact_id"]
    if (
        not isinstance(artifact_id, str)
        or not artifact_id.startswith("artifact-")
        or len(artifact_id) != 41
        or not all(char in "0123456789abcdef" for char in artifact_id[9:])
    ):
        fail("artifact_invalid")
    operation_id = value["operation_id"]
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        fail("artifact_invalid")
    for name in ("workspace_revision", "created_at_ms"):
        if not isinstance(value[name], int) or isinstance(value[name], bool) or value[name] < 0:
            fail("artifact_invalid")
    for name in ("max_file_count", "max_file_bytes", "max_total_bytes", "max_archive_bytes"):
        if not isinstance(value[name], int) or isinstance(value[name], bool) or value[name] < 1:
            fail("artifact_invalid")
    if not hex_digest(value["expected_workspace_sha256"]) or not hex_digest(
        value["baseline_sha256"]
    ):
        fail("artifact_invalid")
    validate_exclusions(value, "excluded_directory_names")
    validate_exclusions(value, "excluded_file_names")
    validate_exclusions(value, "excluded_globs")
    baseline = value["baseline"]
    if not isinstance(baseline, list) or len(baseline) > value["max_file_count"]:
        fail("resource_limit")
    normalized = []
    folded = set()
    for item in baseline:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "binary"}:
            fail("artifact_invalid")
        path = relative(item["path"])
        if path.casefold() in folded:
            fail("artifact_invalid")
        folded.add(path.casefold())
        if (
            not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or item["size"] > value["max_file_bytes"]
            or not hex_digest(item["sha256"])
            or item["binary"] not in (True, False, None)
        ):
            fail("artifact_invalid")
        normalized.append(item)
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        fail("artifact_invalid")
    baseline_core = [
        {"path": item["path"], "size": item["size"], "sha256": item["sha256"]}
        for item in normalized
    ]
    if hashlib.sha256(canonical(baseline_core)).hexdigest() != value["baseline_sha256"]:
        fail("artifact_invalid")
    evidence = value["validation_evidence"]
    if (
        not isinstance(evidence, dict)
        or evidence.get("passed") is not True
        or evidence.get("operation_id") != operation_id
        or evidence.get("workspace_revision") != value["workspace_revision"]
        or evidence.get("workspace_sha256_after") != value["expected_workspace_sha256"]
    ):
        fail("artifact_invalid")
    return normalized


def classify_binary(root, path, maximum):
    descriptor, _ = secure_file(root, path)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    size = 0
    binary = False
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                fail("resource_limit")
            if b"\x00" in chunk:
                binary = True
            try:
                decoder.decode(chunk, final=False)
            except UnicodeDecodeError:
                binary = True
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            binary = True
    finally:
        os.close(descriptor)
    return binary


def binary_diff(changed, deleted):
    values = []
    for item in changed:
        if item["binary"]:
            values.append({
                "path": item["path"],
                "status": item["status"],
                "before_size": item["before_size"],
                "before_sha256": item["before_sha256"],
                "after_size": item["after_size"],
                "after_sha256": item["after_sha256"],
            })
    for item in deleted:
        if item["before_binary"]:
            values.append({
                "path": item["path"],
                "status": "deleted",
                "before_size": item["before_size"],
                "before_sha256": item["before_sha256"],
                "after_size": None,
                "after_sha256": None,
            })
    return sorted(values, key=lambda item: item["path"])


class HashingReader:
    def __init__(self, descriptor, maximum):
        self.descriptor = descriptor
        self.maximum = maximum
        self.size = 0
        self.digest = hashlib.sha256()
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.binary = False

    def read(self, size=-1):
        requested = 1024 * 1024 if size is None or size < 0 else size
        chunk = os.read(self.descriptor, requested)
        self.size += len(chunk)
        if self.size > self.maximum:
            fail("resource_limit")
        self.digest.update(chunk)
        if b"\x00" in chunk:
            self.binary = True
        try:
            self.decoder.decode(chunk, final=not chunk)
        except UnicodeDecodeError:
            self.binary = True
        return chunk


def add_bytes(archive, name, value, mode=0o600):
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(value))


def add_workspace_file(archive, root, item, maximum):
    descriptor, opened = secure_file(root, item["path"])
    reader = HashingReader(descriptor, maximum)
    try:
        info = tarfile.TarInfo(item["member_path"])
        info.size = item["after_size"]
        info.mode = 0o644
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        archive.addfile(info, reader)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    final_identity = (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
    if (
        identity != final_identity
        or reader.size != item["after_size"]
        or reader.digest.hexdigest() != item["after_sha256"]
    ):
        fail("workspace_changed")


def artifact_manifest_digest(core):
    return hashlib.sha256(canonical(core)).hexdigest()


def hash_file(path, maximum):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb", buffering=0) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                fail("resource_limit")
            digest.update(chunk)
    return size, digest.hexdigest()


def export(root, request, output_path):
    output_path = absolute_temp_path(output_path)
    baseline_list = validate_request(request)
    all_current = scan(
        root,
        request["max_file_count"],
        request["max_file_bytes"],
        request["max_total_bytes"],
    )
    current_sha = workspace_digest(all_current)
    if current_sha != request["expected_workspace_sha256"]:
        fail("workspace_changed")
    baseline = {item["path"]: item for item in baseline_list}
    current = {
        item["path"]: item
        for item in all_current
        if not excluded(item["path"], request)
    }
    changed = []
    deleted = []
    for path in sorted(set(baseline) | set(current)):
        before = baseline.get(path)
        after = current.get(path)
        if before is None and after is not None:
            after_binary = classify_binary(root, path, request["max_file_bytes"])
            changed.append({
                "path": path,
                "status": "added",
                "before_size": None,
                "before_sha256": None,
                "before_binary": None,
                "after_size": after["size"],
                "after_sha256": after["sha256"],
                "after_binary": after_binary,
                "binary": after_binary,
                "member_path": "files/" + path,
            })
        elif before is not None and after is None:
            if not isinstance(before["binary"], bool):
                fail("artifact_invalid")
            deleted.append({
                "path": path,
                "before_size": before["size"],
                "before_sha256": before["sha256"],
                "before_binary": before["binary"],
            })
        elif before is not None and after is not None and before["sha256"] != after["sha256"]:
            if not isinstance(before["binary"], bool):
                fail("artifact_invalid")
            after_binary = classify_binary(root, path, request["max_file_bytes"])
            changed.append({
                "path": path,
                "status": "modified",
                "before_size": before["size"],
                "before_sha256": before["sha256"],
                "before_binary": before["binary"],
                "after_size": after["size"],
                "after_sha256": after["sha256"],
                "after_binary": after_binary,
                "binary": before["binary"] or after_binary,
                "member_path": "files/" + path,
            })
    binary = binary_diff(changed, deleted)
    evidence_bytes = canonical(request["validation_evidence"])
    core = {
        "schema_version": "pi-agent-coding-artifact/v1",
        "artifact_id": request["artifact_id"],
        "operation_id": request["operation_id"],
        "workspace_revision": request["workspace_revision"],
        "created_at_ms": request["created_at_ms"],
        "baseline_sha256": request["baseline_sha256"],
        "workspace_sha256": current_sha,
        "validation_evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "changed_files": changed,
        "deleted_files": deleted,
        "binary_diff": binary,
        "changed_file_count": len(changed),
        "deleted_file_count": len(deleted),
        "binary_diff_count": len(binary),
        "payload_bytes": sum(item["after_size"] for item in changed),
    }
    manifest = dict(core)
    manifest["manifest_sha256"] = artifact_manifest_digest(core)
    temporary = output_path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with tarfile.open(temporary, mode="w", format=tarfile.PAX_FORMAT) as archive:
            add_bytes(archive, "metadata/manifest.json", canonical(manifest))
            add_bytes(archive, "metadata/validation-evidence.json", evidence_bytes)
            add_bytes(archive, "metadata/binary-diff.json", canonical(binary))
            add_bytes(archive, "metadata/deleted-files.json", canonical(deleted))
            for item in changed:
                add_workspace_file(archive, root, item, request["max_file_bytes"])
        after = scan(
            root,
            request["max_file_count"],
            request["max_file_bytes"],
            request["max_total_bytes"],
        )
        if workspace_digest(after) != current_sha:
            fail("workspace_changed")
        size, digest = hash_file(temporary, request["max_archive_bytes"])
        os.replace(temporary, output_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        try:
            os.unlink(output_path)
        except FileNotFoundError:
            pass
        raise
    return {
        "remote_path": output_path,
        "size": size,
        "sha256": digest,
        "manifest_sha256": manifest["manifest_sha256"],
        "workspace_sha256": current_sha,
    }


try:
    if len(sys.argv) != 5 or sys.argv[1] != "export":
        fail("artifact_invalid")
    workspace_root = root_path(sys.argv[2])
    request_path = absolute_temp_path(sys.argv[3])
    request_sha256 = sys.argv[4]
    if not hex_digest(request_sha256):
        fail("artifact_invalid")
    request = read_request(
        request_path,
        32 * 1024 * 1024,
        request_sha256,
    )
    output_path = request.get("output_path")
    if output_path is not None:
        fail("artifact_invalid")
    # The output path is derived from the server-generated artifact id, never input
    # from project files or the model.
    artifact_id = request.get("artifact_id")
    if not isinstance(artifact_id, str):
        fail("artifact_invalid")
    output_path = "/tmp/pi-agent-" + artifact_id + ".tar"
    payload = export(workspace_root, request, output_path)
    print(json.dumps({"ok": True, "result": payload}, separators=(",", ":")))
except FileNotFoundError:
    print(json.dumps({"ok": False, "error": "artifact_invalid"}, separators=(",", ":")))
    raise SystemExit(2)
except PermissionError:
    print(json.dumps({"ok": False, "error": "artifact_invalid"}, separators=(",", ":")))
    raise SystemExit(2)
except Failure as error:
    print(json.dumps({"ok": False, "error": error.code}, separators=(",", ":")))
    raise SystemExit(2)
except Exception:
    print(json.dumps({"ok": False, "error": "artifact_invalid"}, separators=(",", ":")))
    raise SystemExit(2)
"""

__all__ = ["REMOTE_ARTIFACT_HELPER"]

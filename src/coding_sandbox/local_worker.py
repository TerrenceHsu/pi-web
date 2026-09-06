"""Image-owned, stdlib-only transport helper. Never import the Web application.

Invoked in the container with Python -I -S -B. The caller first verifies that no
untrusted process survives. The host still validates every exported byte/path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import BinaryIO

MAX_TRANSFER = 100 * 1024 * 1024
MAX_FILES = 5000


def _flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise RuntimeError("linux_runtime_required")
    return value


def safe_path(value: str, *, allow_missing: bool = False) -> Path:
    if (
        not value.startswith(("/workspace/", "/tmp/"))
        and value != "/workspace"
        or "\\" in value
        or "\0" in value
        or ".." in value.split("/")
        or str(Path(value)) != value
    ):
        raise ValueError("unsafe_path")
    path = Path(value)
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if allow_missing and part == path:
                break
            raise
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("unsafe_path")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("unsafe_path")
    return path


def read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError("unsafe_file")
    descriptor = os.open(path, os.O_RDONLY | _flag("O_NOFOLLOW") | _flag("O_NONBLOCK"))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("file_changed")
        value = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(value) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("file_changed")
    return value


def collect_tree(root: Path, output: BinaryIO) -> None:
    files: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            safe_path(str(Path(directory) / name))
        files.extend(Path(directory) / name for name in names)
        if len(files) > MAX_FILES:
            raise ValueError("resource_limit")
    size = 0
    folded: set[str] = set()
    with tarfile.open(fileobj=output, mode="w|") as archive:
        for path in sorted(files):
            safe_path(str(path))
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in folded:
                raise ValueError("unsafe_path")
            folded.add(relative.casefold())
            data = read_regular(path, min(25 * 1024 * 1024, MAX_TRANSFER - size))
            size += len(data)
            info = tarfile.TarInfo(relative)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))


def main() -> None:
    action = sys.argv[1]
    if action == "idle":
        # PID 1 never executes user work or creates children. This expires a
        # RUNNING container. A paused container cannot schedule its exit, so the
        # product must also reconcile TTLs/orphans outside the chat coroutine.
        lifetime = int(sys.argv[2])
        if not 1 <= lifetime <= 1800:
            raise ValueError("resource_limit")
        time.sleep(lifetime)
        return
    if action == "run":
        request = json.loads(sys.stdin.buffer.read(256 * 1024))
        directory = safe_path(request["cwd"])
        argv = request["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and "\0" not in item for item in argv)
        ):
            raise ValueError("unsafe_command")
        os.chdir(directory)
        os.execvpe(argv[0], argv, {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
    elif action == "write":
        header = json.loads(sys.stdin.buffer.readline(8192))
        target = safe_path(header["path"], allow_missing=True)
        limit = min(MAX_TRANSFER, int(header["limit"]))
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit or hashlib.sha256(data).hexdigest() != header["sha256"]:
            raise ValueError("transfer_failed")
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flag("O_NOFOLLOW"), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        print(json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}))
    elif action == "read":
        sys.stdout.buffer.write(read_regular(safe_path(sys.argv[2]), MAX_TRANSFER))
    elif action == "collect":
        collect_tree(safe_path("/workspace"), sys.stdout.buffer)
    elif action == "health":
        # cgroup v2 is required. Docker's State.OOMKilled alone misses OOMs of
        # exec children when the inert supervisor survives. This readonly file
        # belongs to the private container cgroup, never the host cgroup.
        events = dict(
            line.split() for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines()
        )
        print(json.dumps({key: int(events[key]) for key in ("oom", "oom_kill")}))
    elif action == "prepare_bash":
        effective_uid = getattr(os, "geteuid", None)
        if not callable(effective_uid) or effective_uid() != 10000:
            raise ValueError("control_identity_required")
        request = json.loads(sys.stdin.buffer.read(128 * 1024))
        data = request["script"].encode("utf-8", "strict")
        if (
            not data
            or len(data) > 16 * 1024
            or b"\0" in data
            or hashlib.sha256(data).hexdigest() != request["sha256"]
        ):
            raise ValueError("invalid_script")
        root = Path("/run/pi-command")
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 10000 or info.st_mode & 0o022:
            raise ValueError("unsafe_control_directory")
        # Fixed path, outside /workspace, owned by a different UID from task code.
        # The backend serializes prepare + execution under its operation lock.
        target = root / "command.sh"
        target.unlink(missing_ok=True)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flag("O_NOFOLLOW"), 0o444
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        print(json.dumps({"sha256": hashlib.sha256(data).hexdigest()}))
    else:
        raise ValueError("unsupported_action")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never echo input paths, source code or file contents in infrastructure errors.
        sys.stderr.write("local_runtime_operation_failed\n")
        raise SystemExit(70) from None

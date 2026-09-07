"""Offline, no-clobber installation backup and restore. Never exports OS Keyring."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

_LOCK = ".maintenance.lock"
_MANIFEST = "backup-manifest.json"
_SCHEMA = "pi-local-backup/v1"
_MAX_FILES = 100_000
_MAX_BYTES = 20 * 1024**3


class MaintenanceError(RuntimeError):
    """Safe diagnostic code; never a file's contents or database payload."""


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MaintenanceError("unsafe_path")
    return path


@contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    """Same exclusive OS lock used by the gateway; automatically released on crash."""
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = safe_path(root / _LOCK)
    stream: BinaryIO = lock_path.open("a+b")
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                module = importlib.import_module("msvcrt")
                module.locking(stream.fileno(), module.LK_NBLCK, 1)
            else:
                module = importlib.import_module("fcntl")
                module.flock(stream.fileno(), module.LOCK_EX | module.LOCK_NB)
        except OSError:
            raise MaintenanceError("installation_busy_stop_web_first") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                module.locking(stream.fileno(), module.LK_UNLCK, 1)
            else:
                module.flock(stream.fileno(), module.LOCK_UN)
    finally:
        stream.close()


def _digest(path: Path) -> dict[str, Any]:
    safe_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceError("non_regular_or_linked_file")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"size": info.st_size, "sha256": digest}


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    root = safe_path(root)
    result: dict[str, dict[str, Any]] = {}
    total = 0

    def unreadable_directory(error: OSError) -> None:
        # os.walk silently skips unreadable subtrees unless onerror is set.
        # A partial inventory must never be certified as a complete backup.
        raise MaintenanceError("source_tree_unreadable") from error

    for parent, directories, files in os.walk(
        root, followlinks=False, onerror=unreadable_directory,
    ):
        for name in directories:
            safe_path(Path(parent) / name)
        for name in sorted(files):
            if (Path(parent) == root and name == _LOCK) or (
                name.endswith("-shm") and (Path(parent) / name[:-4]).is_file()
                and _is_sqlite(Path(parent) / name[:-4])
            ):
                continue
            path = Path(parent) / name
            item = _digest(path)
            total += item["size"]
            result[path.relative_to(root).as_posix()] = item
            if len(result) > _MAX_FILES or total > _MAX_BYTES:
                raise MaintenanceError("backup_size_limit")
    return result


def _new_destination(source: Path, destination: Path) -> Path:
    source, destination = safe_path(source), safe_path(destination)
    if not source.is_dir():
        raise MaintenanceError("source_directory_required")
    if destination.exists() or source in destination.parents or destination in source.parents:
        raise MaintenanceError("destination_must_be_new_and_outside_source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def sqlite_snapshot(source: Path, destination: Path) -> None:
    safe_path(source)
    # An offline WAL-mode database without a WAL is fully in the main file.
    # immutable avoids SQLite creating an empty WAL/SHM merely by reading it.
    # Existing WALs must use normal read-only mode so committed frames are included.
    wal = safe_path(Path(str(source) + "-wal"))
    query = "?mode=ro" if wal.exists() else "?mode=ro&immutable=1"
    with closing(sqlite3.connect(source.as_uri() + query, uri=True, timeout=1)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target)
            check_database(target)
            target.commit()
            # Portable snapshots have no WAL dependency and read-only verification
            # must not create sidecar files in the frozen backup inventory.
            target.execute("PRAGMA journal_mode=DELETE")


def check_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise MaintenanceError("database_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MaintenanceError("database_foreign_key_failed")


def snapshot_tree(
    source: Path, destination: Path, *, snapshot_databases: bool = True,
) -> dict[str, dict[str, Any]]:
    """Caller holds installation lock and has stopped external Worker writers."""
    source = safe_path(source)
    destination = _new_destination(source, destination)
    before = inventory(source)
    databases = {name for name in before if _is_sqlite(source / name)}
    sidecars = {name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")}
    destination.mkdir()
    for name in before:
        if name in sidecars:
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in databases and snapshot_databases:
            sqlite_snapshot(source / name, target)
        else:
            shutil.copyfile(source / name, target)
            if _digest(target) != before[name]:
                raise MaintenanceError("source_changed_during_backup")
    if before != inventory(source):
        raise MaintenanceError("source_changed_during_backup")
    return inventory(destination)


def backup_data(source: Path, destination: Path, *, offline: bool = False) -> dict[str, Any]:
    if not offline:
        raise MaintenanceError("stop_web_and_worker_then_confirm_offline")
    source = safe_path(source)
    if not (source / "auth.sqlite").is_file():
        raise MaintenanceError("installation_auth_database_missing")
    destination = _new_destination(source, destination)
    with installation_lock(source):
        destination.mkdir()
        files = snapshot_tree(source, destination / "data")
        manifest = {
            "schema": _SCHEMA,
            "files": files,
            "credentials": "OS Keyring and Docker images are not included; reconfigure separately",
            "signatures": "pending artifacts require their original OS Keyring signing key",
        }
        (destination / _MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verify_backup(destination)
        return {"files": len(files), "bytes": sum(item["size"] for item in files.values())}


def verify_backup(root: Path) -> dict[str, Any]:
    root = safe_path(root)
    manifest_path = safe_path(root / _MANIFEST)
    if manifest_path.stat().st_size > 32 * 1024**2:
        raise MaintenanceError("backup_manifest_too_large")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA:
        raise MaintenanceError("invalid_backup_manifest")
    files = manifest.get("files")
    if not isinstance(files, dict) or not 0 < len(files) <= _MAX_FILES:
        raise MaintenanceError("invalid_backup_manifest")
    for name in files:
        if (
            not isinstance(name, str) or "\\" in name or ":" in name
            or PurePosixPath(name).is_absolute()
            or any(part in {".", "..", ""} for part in name.split("/"))
        ):
            raise MaintenanceError("invalid_backup_path")
    actual = inventory(root / "data")
    if actual != files:
        raise MaintenanceError("backup_hash_mismatch")
    for name in files:
        path = root / "data" / name
        if _is_sqlite(path):
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
                check_database(connection)
    return manifest


def restore_data(backup: Path, destination: Path, *, offline: bool = False) -> dict[str, Any]:
    if not offline:
        raise MaintenanceError("stop_web_and_worker_then_confirm_offline")
    backup = safe_path(backup)
    destination = _new_destination(backup, destination)
    manifest = verify_backup(backup)
    files = snapshot_tree(backup / "data", destination, snapshot_databases=False)
    if files != manifest["files"]:
        raise MaintenanceError("restored_inventory_mismatch")
    # Never automatically changes PI_AGENT_DATA_DIR, credentials, or the running service.
    return {"files": len(files), "restored": True, "activation_required": True}

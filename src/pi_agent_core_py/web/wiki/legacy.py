"""Explicit, recoverable retirement of the legacy chunk-RAG filesystem."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from .errors import LegacyRetirementError, WikiPathError
from .files import WikiFileStore, _is_link_or_reparse
from .models import WIKI_LEGACY_MANIFEST_SCHEMA, WikiLegacyBackupReceipt

LEGACY_DB_FILENAME = "knowledge.db"
LEGACY_LIBRARIES_SUBDIR = "libraries"
LEGACY_MANIFEST_FILENAME = "legacy-manifest.json"
_RETIREMENT_LOCK_FILENAME = ".wiki-legacy-retirement.lock"


def retire_legacy_knowledge(
    root: str | Path,
    *,
    clock_ms: Callable[[], int] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> WikiLegacyBackupReceipt | None:
    """Back up then remove the two fixed legacy roots.

    The caller must first stop the legacy ingestion/indexing workers and close
    ``KnowledgeStore``.  Nothing is removed until a complete verified backup
    directory and manifest have been atomically published under ``legacy/``.
    """

    file_store = WikiFileStore(root)
    file_store.ensure_root()
    lock_path = file_store.root / _RETIREMENT_LOCK_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise LegacyRetirementError("legacy_busy") from exc
    try:
        os.write(descriptor, b"llm-wiki-legacy-retirement-v1\n")
        os.fsync(descriptor)
        return _retire_legacy_knowledge_locked(
            file_store,
            clock_ms=clock_ms,
            id_factory=id_factory,
        )
    finally:
        os.close(descriptor)
        if lock_path.exists() and not _is_link_or_reparse(lock_path):
            lock_path.unlink(missing_ok=True)


def _retire_legacy_knowledge_locked(
    file_store: WikiFileStore,
    *,
    clock_ms: Callable[[], int] | None,
    id_factory: Callable[[], str] | None,
) -> WikiLegacyBackupReceipt | None:
    knowledge_db = file_store.root / LEGACY_DB_FILENAME
    libraries = file_store.root / LEGACY_LIBRARIES_SUBDIR
    wal_path = knowledge_db.with_name(f"{knowledge_db.name}-wal")
    shm_path = knowledge_db.with_name(f"{knowledge_db.name}-shm")

    if not knowledge_db.exists() and (wal_path.exists() or shm_path.exists()):
        raise LegacyRetirementError("legacy_invalid")
    existing_names: list[str] = []
    if knowledge_db.exists():
        _require_regular_file(knowledge_db)
        existing_names.append(LEGACY_DB_FILENAME)
    if libraries.exists():
        _require_directory(libraries)
        existing_names.append(LEGACY_LIBRARIES_SUBDIR)
    if not existing_names:
        return None

    now_ms = (clock_ms or (lambda: int(time.time() * 1000)))()
    backup_id = (id_factory or (lambda: f"backup_{uuid4().hex[:24]}"))()
    if not _valid_backup_id(backup_id):
        raise LegacyRetirementError("invalid_configuration")
    backup_name = f"knowledge-{now_ms}-{backup_id}"
    final_dir = file_store.legacy_root / backup_name
    staging = file_store.legacy_root / f".{backup_name}.{uuid4().hex}.tmp"
    if final_dir.exists() or staging.exists():
        raise LegacyRetirementError("legacy_backup_failed")

    final_published = False
    try:
        staging.mkdir(mode=0o700)
        if knowledge_db.exists():
            _snapshot_sqlite(knowledge_db, staging / LEGACY_DB_FILENAME)
        if libraries.exists():
            _copy_tree_strict(libraries, staging / LEGACY_LIBRARIES_SUBDIR)
        entries = _inventory(staging)
        _verify_inventory(staging, entries)
        manifest_payload = {
            "schema": WIKI_LEGACY_MANIFEST_SCHEMA,
            "backup_id": backup_id,
            "created_at_ms": now_ms,
            "retired_names": existing_names,
            "files": entries,
        }
        manifest = _canonical_json(manifest_payload)
        _write_new_file(staging / LEGACY_MANIFEST_FILENAME, manifest)
        os.replace(staging, final_dir)
        final_published = True
        _make_backup_files_read_only(final_dir)
    except LegacyRetirementError:
        if not final_published:
            _remove_private_staging(staging)
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        if not final_published:
            _remove_private_staging(staging)
        raise LegacyRetirementError("legacy_backup_failed") from exc

    manifest_sha256 = hashlib.sha256(
        (final_dir / LEGACY_MANIFEST_FILENAME).read_bytes()
    ).hexdigest()
    file_count = len(entries) + 1
    total_size = 0
    for entry in entries:
        entry_size = entry["size_bytes"]
        if not isinstance(entry_size, int):
            raise LegacyRetirementError("legacy_backup_failed")
        total_size += entry_size
    total_size += len(manifest)
    receipt = WikiLegacyBackupReceipt(
        backup_id=backup_id,
        backup_relpath=f"legacy/{backup_name}",
        created_at_ms=now_ms,
        file_count=file_count,
        total_size_bytes=total_size,
        manifest_sha256=manifest_sha256,
        retired_names=tuple(existing_names),
        cleanup_complete=False,
    )
    try:
        _cleanup_originals(
            knowledge_db=knowledge_db,
            libraries=libraries,
            wal_path=wal_path,
            shm_path=shm_path,
        )
    except OSError as exc:
        raise LegacyRetirementError(
            "legacy_cleanup_failed",
            receipt=receipt,
        ) from exc
    return receipt.model_copy(update={"cleanup_complete": True})


def _valid_backup_id(value: str) -> bool:
    return (
        len(value) == 31
        and value.startswith("backup_")
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _require_regular_file(path: Path) -> None:
    if _is_link_or_reparse(path):
        raise WikiPathError("path_unsafe")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyRetirementError("legacy_invalid") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WikiPathError("path_unsafe")


def _require_directory(path: Path) -> None:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise WikiPathError("path_unsafe")


def _snapshot_sqlite(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=1.0)
    destination = sqlite3.connect(destination_path)
    try:
        source.execute("PRAGMA busy_timeout=1000")
        source.backup(destination)
        row = destination.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise LegacyRetirementError("legacy_invalid")
        destination.commit()
    except sqlite3.Error as exc:
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            raise LegacyRetirementError("legacy_busy") from exc
        raise LegacyRetirementError("legacy_invalid") from exc
    finally:
        destination.close()
        source.close()


def _copy_tree_strict(source: Path, destination: Path) -> None:
    _require_directory(source)
    destination.mkdir(mode=0o700)
    for entry in sorted(os.scandir(source), key=lambda item: item.name.casefold()):
        source_child = Path(entry.path)
        destination_child = destination / entry.name
        if entry.is_symlink() or _is_link_or_reparse(source_child):
            raise WikiPathError("path_unsafe")
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _copy_tree_strict(source_child, destination_child)
        elif stat.S_ISREG(info.st_mode):
            _copy_regular_file(source_child, destination_child)
        else:
            raise WikiPathError("path_unsafe")


def _copy_regular_file(source: Path, destination: Path) -> None:
    before = source.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise WikiPathError("path_unsafe")
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat(follow_symlinks=False)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise LegacyRetirementError("legacy_busy")


def _inventory(root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_dir():
            continue
        _require_regular_file(path)
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return entries


def _verify_inventory(root: Path, entries: list[dict[str, object]]) -> None:
    actual = _inventory(root)
    if actual != entries:
        raise LegacyRetirementError("legacy_backup_failed")


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _make_backup_files_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)


def _cleanup_originals(
    *,
    knowledge_db: Path,
    libraries: Path,
    wal_path: Path,
    shm_path: Path,
) -> None:
    if knowledge_db.exists():
        _checkpoint_legacy_for_cleanup(knowledge_db)
    for path in (knowledge_db, wal_path, shm_path):
        if path.exists():
            _require_regular_file(path)
            _unlink_with_retry(path)
    if libraries.exists():
        _require_directory(libraries)
        shutil.rmtree(libraries)


def _checkpoint_legacy_for_cleanup(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=1.0)
    try:
        connection.execute("PRAGMA busy_timeout=1000")
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is not None and int(row[0]) != 0:
            raise OSError("legacy database remains busy")
    except sqlite3.Error as exc:
        raise OSError("legacy database checkpoint failed") from exc
    finally:
        connection.close()


def _unlink_with_retry(path: Path) -> None:
    last_error: OSError | None = None
    for _ in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.01)
    if last_error is not None:
        raise last_error


def _remove_private_staging(path: Path) -> None:
    if not path.exists() or _is_link_or_reparse(path):
        return
    shutil.rmtree(path)


__all__ = [
    "LEGACY_DB_FILENAME",
    "LEGACY_LIBRARIES_SUBDIR",
    "LEGACY_MANIFEST_FILENAME",
    "retire_legacy_knowledge",
]

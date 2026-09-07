"""Strict file-queue protocol shared by the OCI supervisor and runtime child.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .errors import WorkerRuntimeError

QUEUE_SCHEMA: Final = "llm-wiki-parser-queue/v1"
REQUEST_NAME: Final = "request.json"
SOURCE_NAME: Final = "source.pdf"
STATUS_NAME: Final = "status.json"
ARTIFACT_NAME: Final = "artifact.tar"
RECEIPT_NAME: Final = "receipt.json"
CANCEL_NAME: Final = "cancel.request"
DESTROY_NAME: Final = "destroy.request"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_REPARSE_POINT = 0x400
# A Linux container can still write a Windows-backed Docker Desktop volume.
# Host sharing violations are a mount property, not the process platform.
_ATOMIC_REPLACE_ATTEMPTS = 20
_ATOMIC_REPLACE_RETRY_SECONDS = 0.002


@dataclass(frozen=True, slots=True)
class QueueRequest:
    provider_job_id: str
    created_at_ms: int
    handle: dict[str, object]
    spec: dict[str, object]


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _has_reparse_attribute(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def require_safe_directory(path: Path, *, create: bool = False) -> Path:
    """Resolve one absolute directory whose existing ancestry has no links."""

    if not path.is_absolute():
        raise WorkerRuntimeError("invalid_configuration")
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise WorkerRuntimeError("invalid_configuration") from error
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WorkerRuntimeError("invalid_configuration") from error
    current = resolved
    while True:
        try:
            metadata = current.lstat()
        except OSError as error:
            raise WorkerRuntimeError("invalid_configuration") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _has_reparse_attribute(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise WorkerRuntimeError("invalid_configuration")
        if current.parent == current:
            break
        current = current.parent
    return resolved


def require_job_directory(queue_root: Path, provider_job_id: str) -> Path:
    if _IDENTIFIER.fullmatch(provider_job_id) is None:
        raise WorkerRuntimeError("invalid_source")
    root = require_safe_directory(queue_root)
    candidate = root / "jobs" / provider_job_id
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise WorkerRuntimeError("invalid_source") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _has_reparse_attribute(metadata)
        or resolved.parent != (root / "jobs").resolve(strict=True)
    ):
        raise WorkerRuntimeError("invalid_source")
    return resolved


def read_stable_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _has_reparse_attribute(before)
            or before.st_size > maximum_bytes
        ):
            raise WorkerRuntimeError("invalid_source")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _stable_identity(opened) != _stable_identity(before):
                raise WorkerRuntimeError("source_changed")
            content = stream.read(maximum_bytes + 1)
        after = path.lstat()
    except WorkerRuntimeError:
        raise
    except OSError as error:
        raise WorkerRuntimeError("invalid_source") from error
    if len(content) > maximum_bytes:
        raise WorkerRuntimeError("artifact_limit_exceeded")
    if _stable_identity(before) != _stable_identity(after):
        raise WorkerRuntimeError("source_changed")
    return content


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    """Replace one host-readable queue file without following a destination link.

    The dedicated queue directory is the confidentiality boundary. Group/other read bits are
    required because Docker Desktop maps files created as container UID 65532 onto Windows ACLs;
    mode 0600 otherwise makes status and receipts unreadable to the host application.
    """

    parent = require_safe_directory(path.parent)
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            # Docker Desktop can block indefinitely when fsync is issued against a Windows
            # bind mount. Atomic replacement, strict reader validation, and interrupted-job
            # recovery provide the queue boundary; durability is owned by the host filesystem.
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise WorkerRuntimeError("invalid_source")
        for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # A host reader can briefly hold a non-delete-sharing handle
                # on Windows. Keep the replacement atomic and bound the wait;
                # persistent ACL/permission failures still surface unchanged.
                if attempt + 1 == _ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)
    except WorkerRuntimeError:
        raise
    except OSError as error:
        raise WorkerRuntimeError("invalid_source") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def read_json_object(path: Path, *, maximum_bytes: int = 1_048_576) -> dict[str, object]:
    try:
        value = json.loads(read_stable_bytes(path, maximum_bytes=maximum_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeError("invalid_source") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WorkerRuntimeError("invalid_source")
    return cast(dict[str, object], value)


def _exact_object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkerRuntimeError("invalid_source")
    return cast(dict[str, object], value)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkerRuntimeError("invalid_source")
    return value


def _nonnegative_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkerRuntimeError("invalid_source")
    return value


def load_queue_request(job_dir: Path) -> QueueRequest:
    payload = _exact_object(
        read_json_object(job_dir / REQUEST_NAME),
        {"schema", "provider_job_id", "created_at_ms", "handle", "spec"},
    )
    if payload["schema"] != QUEUE_SCHEMA:
        raise WorkerRuntimeError("invalid_source")
    provider_job_id = _identifier(payload["provider_job_id"])
    if provider_job_id != job_dir.name:
        raise WorkerRuntimeError("invalid_source")
    handle = _exact_object(
        payload["handle"],
        {"contract_version", "provider", "provider_job_id", "job_id", "source_id", "created_at_ms"},
    )
    if (
        handle["contract_version"] != 2
        or handle["provider"] != "mineru"
        or handle["provider_job_id"] != provider_job_id
    ):
        raise WorkerRuntimeError("invalid_source")
    _identifier(handle["job_id"])
    _identifier(handle["source_id"])
    created_at_ms = _nonnegative_integer(payload["created_at_ms"])
    if handle["created_at_ms"] != created_at_ms:
        raise WorkerRuntimeError("invalid_source")
    spec = payload["spec"]
    if not isinstance(spec, dict):
        raise WorkerRuntimeError("invalid_source")
    return QueueRequest(
        provider_job_id=provider_job_id,
        created_at_ms=created_at_ms,
        handle=handle,
        spec=cast(dict[str, object], spec),
    )


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "ARTIFACT_NAME",
    "CANCEL_NAME",
    "DESTROY_NAME",
    "QUEUE_SCHEMA",
    "RECEIPT_NAME",
    "REQUEST_NAME",
    "SOURCE_NAME",
    "STATUS_NAME",
    "QueueRequest",
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical_json_bytes",
    "load_queue_request",
    "read_json_object",
    "read_stable_bytes",
    "require_job_directory",
    "require_safe_directory",
    "sha256_bytes",
]

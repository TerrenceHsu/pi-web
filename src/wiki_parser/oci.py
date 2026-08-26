"""Provider for the network-free persistent OCI parser file queue.

The main application never imports the AGPL Worker or its parser dependencies.
It exchanges immutable Contract v2 JSON and bytes through one configured volume.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import ValidationError

from .contract_v2 import (
    ParserArtifactReceiptV2,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserProbeV2,
    ParserRoutingConfigIdentity,
)
from .errors import ParserError, ParserErrorCode

_QUEUE_SCHEMA: Final = "llm-wiki-parser-queue/v1"
_PROBE_NAME: Final = "probe.json"
_REQUEST_NAME: Final = "request.json"
_SOURCE_NAME: Final = "source.pdf"
_STATUS_NAME: Final = "status.json"
_ARTIFACT_NAME: Final = "artifact.tar"
_RECEIPT_NAME: Final = "receipt.json"
_CANCEL_NAME: Final = "cancel.request"
_DESTROY_NAME: Final = "destroy.request"
_WINDOWS_REPARSE_POINT: Final = 0x400
_MAX_PROTOCOL_BYTES: Final = 8 * 1024 * 1024
_STATUS_READ_ATTEMPTS: Final = 5
_STATUS_RETRY_SECONDS: Final = 0.002


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute():
        raise ParserError("invalid_configuration", provider="dual_pdf")
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ParserError("invalid_configuration", provider="dual_pdf") from error
    current = resolved
    while True:
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ParserError("invalid_configuration", provider="dual_pdf") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise ParserError("invalid_configuration", provider="dual_pdf")
        if current.parent == current:
            break
        current = current.parent
    return resolved


def _read_stable(path: Path, *, maximum_bytes: int, error_code: str) -> bytes:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or before.st_size > maximum_bytes
        ):
            raise ParserError(cast(ParserErrorCode, error_code), provider="dual_pdf")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise ParserError(cast(ParserErrorCode, error_code), provider="dual_pdf")
            content = stream.read(maximum_bytes + 1)
        after = path.lstat()
    except ParserError:
        raise
    except OSError as error:
        raise ParserError(cast(ParserErrorCode, error_code), provider="dual_pdf") from error
    if len(content) > maximum_bytes or _identity(before) != _identity(after):
        raise ParserError(cast(ParserErrorCode, error_code), provider="dual_pdf")
    return content


def _write_new(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ParserError("provider_unavailable", provider="dual_pdf") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_replace(path: Path, content: bytes) -> None:
    parent = _safe_directory(path.parent)
    temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        _write_new(temporary, content)
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ParserError("artifact_unavailable", provider="dual_pdf")
        os.replace(temporary, path)
    except ParserError:
        raise
    except OSError as error:
        raise ParserError("artifact_unavailable", provider="dual_pdf") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class PersistentOciParserProvider:
    """Contract v2 client for one externally managed persistent OCI Worker."""

    def __init__(
        self,
        *,
        exchange_root: Path,
        routing_config: ParserRoutingConfigIdentity,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        poll_interval_seconds: float = 0.05,
        management_timeout_seconds: float = 30.0,
    ) -> None:
        self._root = _safe_directory(exchange_root, create=True)
        self._jobs = _safe_directory(self._root / "jobs", create=True)
        self._routing = routing_config
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._id_factory = id_factory or (lambda: f"oci-{uuid4().hex}")
        self._poll_interval = poll_interval_seconds
        self._management_timeout = management_timeout_seconds

    def provider_name(self) -> Literal["dual_pdf"]:
        return "dual_pdf"

    async def probe(self) -> ParserProbeV2:
        try:
            payload = await asyncio.to_thread(
                _read_stable,
                self._root / _PROBE_NAME,
                maximum_bytes=_MAX_PROTOCOL_BYTES,
                error_code="provider_unavailable",
            )
            probe = ParserProbeV2.model_validate_json(payload)
        except (ParserError, ValidationError):
            return ParserProbeV2(
                provider="dual_pdf",
                available=False,
                worker_version="0.0.28",
                license_mode="agpl_3_0",
                routing_config=self._routing,
                observed_at_ms=self._clock_ms(),
                error_code="provider_unavailable",
            )
        if probe.routing_config != self._routing:
            return probe.model_copy(
                update={
                    "available": False,
                    "error_code": "invalid_configuration",
                    "observed_at_ms": self._clock_ms(),
                }
            )
        return probe

    def _job_dir(self, handle: ParserJobHandleV2) -> Path:
        if handle.provider != "dual_pdf":
            raise ParserError("invalid_configuration", provider=self.provider_name())
        candidate = self._jobs / handle.provider_job_id
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ParserError("job_not_found", provider=self.provider_name()) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
            or resolved.parent != self._jobs
        ):
            raise ParserError("job_not_found", provider=self.provider_name())
        return resolved

    def _read_status(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        job_dir = self._job_dir(handle)
        content: bytes | None = None
        for attempt in range(_STATUS_READ_ATTEMPTS):
            try:
                content = _read_stable(
                    job_dir / _STATUS_NAME,
                    maximum_bytes=_MAX_PROTOCOL_BYTES,
                    error_code="protocol_error",
                )
                break
            except ParserError as error:
                if error.code != "protocol_error" or attempt + 1 == _STATUS_READ_ATTEMPTS:
                    raise
                time.sleep(_STATUS_RETRY_SECONDS)
        if content is None:  # pragma: no cover - loop either returns content or raises
            raise ParserError("protocol_error", provider=self.provider_name())
        try:
            status = ParserJobStatusV2.model_validate_json(content)
        except ValidationError as error:
            raise ParserError("protocol_error", provider=self.provider_name()) from error
        if status.handle != handle:
            raise ParserError("job_not_found", provider=self.provider_name())
        return status

    @staticmethod
    def _read_source(source_path: Path, spec: ParserJobSpecV2) -> bytes:
        content = _read_stable(
            source_path,
            maximum_bytes=spec.limits.max_source_bytes,
            error_code="invalid_source",
        )
        if (
            len(content) != spec.source.size_bytes
            or hashlib.sha256(content).hexdigest() != spec.source.sha256
            or not content.startswith(b"%PDF-")
        ):
            raise ParserError("invalid_source", provider="dual_pdf")
        return content

    def _create_job_sync(
        self,
        spec: ParserJobSpecV2,
        source_path: Path,
    ) -> ParserJobHandleV2:
        source = self._read_source(source_path, spec)
        provider_job_id = self._id_factory()
        handle = ParserJobHandleV2(
            provider="dual_pdf",
            provider_job_id=provider_job_id,
            job_id=spec.job_id,
            source_id=spec.source.source_id,
            created_at_ms=self._clock_ms(),
        )
        staging = self._jobs / f".{provider_job_id}.{uuid4().hex}.tmp"
        final = self._jobs / provider_job_id
        try:
            # The host application and non-root container use different identities. The
            # dedicated exchange root is the confidentiality boundary; one job directory must
            # preserve inherited Windows ACLs and remain traversable by OCI UID 65532.
            staging.mkdir(mode=0o777)
            _write_new(staging / _SOURCE_NAME, source)
            request = {
                "created_at_ms": handle.created_at_ms,
                "handle": handle.model_dump(mode="json"),
                "provider_job_id": provider_job_id,
                "schema": _QUEUE_SCHEMA,
                "spec": spec.model_dump(mode="json"),
            }
            _write_new(staging / _REQUEST_NAME, _canonical_json(request))
            status = ParserJobStatusV2(
                handle=handle,
                state="queued",
                phase="queued",
                observed_at_ms=self._clock_ms(),
            )
            _write_new(
                staging / _STATUS_NAME,
                _canonical_json(status.model_dump(mode="json")),
            )
            os.rename(staging, final)
        except (OSError, ParserError) as error:
            for child in (_SOURCE_NAME, _REQUEST_NAME, _STATUS_NAME):
                try:
                    (staging / child).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                staging.rmdir()
            except OSError:
                pass
            if isinstance(error, ParserError):
                raise
            raise ParserError("provider_unavailable", provider=self.provider_name()) from error
        return handle

    async def create_job(
        self,
        spec: ParserJobSpecV2,
        *,
        source_path: Path,
    ) -> ParserJobHandleV2:
        if spec.routing_config != self._routing:
            raise ParserError("invalid_configuration", provider=self.provider_name())
        return await asyncio.to_thread(self._create_job_sync, spec, source_path)

    async def status(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        return await asyncio.to_thread(self._read_status, handle)

    async def wait(
        self,
        handle: ParserJobHandleV2,
        *,
        signal: asyncio.Event | None = None,
    ) -> ParserJobStatusV2:
        while True:
            status = await self.status(handle)
            if status.state in {"succeeded", "failed", "cancelled", "destroyed"}:
                return status
            if signal is not None and signal.is_set():
                return await self.cancel(handle)
            await asyncio.sleep(self._poll_interval)

    def _download_sync(
        self,
        handle: ParserJobHandleV2,
        local_path: Path,
        expected_sha256: str | None,
    ) -> ParserArtifactReceiptV2:
        status = self._read_status(handle)
        if status.state != "succeeded" or status.artifact_sha256 is None:
            raise ParserError("artifact_unavailable", provider=self.provider_name())
        job_dir = self._job_dir(handle)
        artifact = _read_stable(
            job_dir / _ARTIFACT_NAME,
            maximum_bytes=status.artifact_size_bytes or 0,
            error_code="artifact_invalid",
        )
        digest = hashlib.sha256(artifact).hexdigest()
        if digest != status.artifact_sha256 or (
            expected_sha256 is not None and expected_sha256 != digest
        ):
            raise ParserError("artifact_invalid", provider=self.provider_name())
        try:
            receipt_content = _read_stable(
                job_dir / _RECEIPT_NAME,
                maximum_bytes=_MAX_PROTOCOL_BYTES,
                error_code="artifact_invalid",
            )
            receipt = ParserArtifactReceiptV2.model_validate_json(receipt_content)
        except ValidationError as error:
            raise ParserError("artifact_invalid", provider=self.provider_name()) from error
        if (
            receipt.job_id != handle.job_id
            or receipt.source_id != handle.source_id
            or receipt.size_bytes != len(artifact)
            or receipt.sha256 != digest
        ):
            raise ParserError("artifact_invalid", provider=self.provider_name())
        _atomic_replace(local_path, artifact)
        return receipt

    async def download_artifact(
        self,
        handle: ParserJobHandleV2,
        *,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> ParserArtifactReceiptV2:
        return await asyncio.to_thread(
            self._download_sync,
            handle,
            local_path,
            expected_sha256,
        )

    def _write_control(self, handle: ParserJobHandleV2, name: str) -> None:
        job_dir = self._job_dir(handle)
        path = job_dir / name
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ParserError(
                    "protocol_error", provider=self.provider_name()
                ) from None
            return
        temporary = job_dir / f".{name}.{uuid4().hex}.tmp"
        try:
            _write_new(temporary, b"1\n")
            os.rename(temporary, path)
        except FileExistsError:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ParserError(
                    "protocol_error", provider=self.provider_name()
                ) from None
        except OSError as error:
            raise ParserError("provider_unavailable", provider=self.provider_name()) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def cancel(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        status = await self.status(handle)
        if status.state not in {"queued", "running"}:
            return status
        await asyncio.to_thread(self._write_control, handle, _CANCEL_NAME)
        return await self.wait(handle)

    async def destroy(self, handle: ParserJobHandleV2) -> None:
        try:
            status = await self.status(handle)
        except ParserError as error:
            if error.code == "job_not_found":
                return
            raise
        if status.state == "destroyed":
            return
        if status.state in {"queued", "running"}:
            await self.cancel(handle)
        await asyncio.to_thread(self._write_control, handle, _DESTROY_NAME)
        deadline = time.monotonic() + self._management_timeout
        while time.monotonic() < deadline:
            if (await self.status(handle)).state == "destroyed":
                return
            await asyncio.sleep(self._poll_interval)
        raise ParserError("request_timeout", provider=self.provider_name())


__all__ = ["PersistentOciParserProvider"]

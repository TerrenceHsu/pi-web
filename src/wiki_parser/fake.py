"""Deterministic, offline ParserProvider for contract and orchestration tests."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tarfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .errors import ParserError, ParserErrorCode
from .models import (
    PARSER_ARTIFACT_SCHEMA,
    ParserArtifactFile,
    ParserArtifactManifest,
    ParserArtifactReceipt,
    ParserCapabilities,
    ParserImageMimeType,
    ParserJobHandle,
    ParserJobSpec,
    ParserJobState,
    ParserJobStatus,
    ParserProbe,
    ParserProviderName,
)

_FAKE_PROVIDER_VERSION = "fake-1"
_IMAGE_SUFFIX: dict[ParserImageMimeType, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


@dataclass(frozen=True, slots=True)
class FakeParserImage:
    """One embedded image emitted by a configured fake parse."""

    mime_type: ParserImageMimeType
    content: bytes = field(repr=False)
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class FakeParserOutput:
    """Safe test seam used to build a deterministic valid artifact tar."""

    markdown: str = field(default="# Parsed source\n", repr=False)
    images: tuple[FakeParserImage, ...] = ()
    page_count: int = 1
    warnings: tuple[str, ...] = ()
    failure_code: ParserErrorCode | None = None


@dataclass(slots=True)
class _FakeParserState:
    handle: ParserJobHandle
    spec: ParserJobSpec
    output: FakeParserOutput
    state: ParserJobState = "queued"
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    safe_error_code: ParserErrorCode | None = None
    artifact: bytes | None = field(default=None, repr=False)
    artifact_sha256: str | None = None
    manifest_sha256: str | None = None
    file_count: int | None = None


class FakeParserProvider:
    """In-memory parser lifecycle with real source and artifact hash checks.

    The Fake never opens a socket, launches a process, imports Marker, or reads
    provider environment variables.  An optional completion gate lets tests
    exercise in-flight cancellation deterministically.
    """

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        outputs: Iterable[FakeParserOutput] | None = None,
        completion_gate: asyncio.Event | None = None,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._id_factory = id_factory or (lambda: f"fake-{uuid4().hex}")
        self._outputs = deque(outputs or ())
        self._completion_gate = completion_gate
        self._wait_timeout_seconds = wait_timeout_seconds
        self._states: dict[str, _FakeParserState] = {}
        self.created_specs: list[ParserJobSpec] = []
        self.destroyed_provider_job_ids: list[str] = []

    def provider_name(self) -> ParserProviderName:
        return "fake"

    async def probe(self) -> ParserProbe:
        return ParserProbe(
            provider="fake",
            available=True,
            provider_version=_FAKE_PROVIDER_VERSION,
            license_mode="not_required",
            capabilities=ParserCapabilities(),
            observed_at_ms=self._clock_ms(),
        )

    async def create_job(
        self,
        spec: ParserJobSpec,
        *,
        source_path: Path,
    ) -> ParserJobHandle:
        await asyncio.to_thread(self._verify_source, source_path, spec)
        now = self._clock_ms()
        handle = ParserJobHandle(
            provider="fake",
            provider_job_id=self._id_factory(),
            job_id=spec.job_id,
            source_id=spec.source.source_id,
            created_at_ms=now,
        )
        if handle.provider_job_id in self._states:
            raise ParserError("protocol_error", provider="fake")
        output = self._outputs.popleft() if self._outputs else FakeParserOutput()
        self._states[handle.provider_job_id] = _FakeParserState(
            handle=handle,
            spec=spec,
            output=output,
        )
        self.created_specs.append(spec)
        return handle

    async def status(self, handle: ParserJobHandle) -> ParserJobStatus:
        return self._snapshot(self._state_for_status(handle))

    async def wait(
        self,
        handle: ParserJobHandle,
        *,
        signal: asyncio.Event | None = None,
    ) -> ParserJobStatus:
        state = self._live_state(handle)
        if state.state in {"succeeded", "failed", "cancelled"}:
            return self._snapshot(state)
        if signal is not None and signal.is_set():
            return self._mark_cancelled(state)

        if state.state == "queued":
            state.state = "running"
            state.started_at_ms = self._clock_ms()

        if self._completion_gate is not None and not self._completion_gate.is_set():
            outcome = await self._wait_for_gate_or_cancel(
                signal,
                timeout_seconds=(
                    self._wait_timeout_seconds
                    if self._wait_timeout_seconds is not None
                    else state.spec.limits.timeout_seconds
                ),
            )
            if outcome == "cancelled":
                return self._mark_cancelled(state)
            if outcome == "request_timeout":
                return self._mark_failed(state, "request_timeout")

        if signal is not None and signal.is_set():
            return self._mark_cancelled(state)
        if state.output.failure_code is not None:
            return self._mark_failed(state, state.output.failure_code)

        try:
            artifact, artifact_sha, manifest_sha, file_count = await asyncio.to_thread(
                self._build_artifact,
                state,
            )
        except ParserError as exc:
            return self._mark_failed(state, exc.code)
        except Exception:
            return self._mark_failed(state, "artifact_invalid")
        state.artifact = artifact
        state.artifact_sha256 = artifact_sha
        state.manifest_sha256 = manifest_sha
        state.file_count = file_count
        state.state = "succeeded"
        state.finished_at_ms = self._clock_ms()
        return self._snapshot(state)

    async def download_artifact(
        self,
        handle: ParserJobHandle,
        *,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> ParserArtifactReceipt:
        state = self._live_state(handle)
        if state.state != "succeeded" or state.artifact is None:
            raise ParserError("artifact_unavailable", provider="fake")
        digest = hashlib.sha256(state.artifact).hexdigest()
        if digest != state.artifact_sha256:
            raise ParserError("artifact_invalid", provider="fake")
        if expected_sha256 is not None and expected_sha256 != digest:
            raise ParserError("artifact_invalid", provider="fake")
        try:
            await asyncio.to_thread(self._atomic_write, local_path, state.artifact)
        except OSError as exc:
            raise ParserError("artifact_unavailable", provider="fake") from exc
        if state.manifest_sha256 is None or state.file_count is None:
            raise ParserError("protocol_error", provider="fake")
        return ParserArtifactReceipt(
            job_id=handle.job_id,
            source_id=handle.source_id,
            size_bytes=len(state.artifact),
            sha256=digest,
            manifest_sha256=state.manifest_sha256,
            file_count=state.file_count,
        )

    async def cancel(self, handle: ParserJobHandle) -> ParserJobStatus:
        state = self._live_state(handle)
        if state.state in {"queued", "running"}:
            return self._mark_cancelled(state)
        return self._snapshot(state)

    async def destroy(self, handle: ParserJobHandle) -> None:
        state = self._states.get(handle.provider_job_id)
        if state is None:
            if handle.provider != "fake":
                raise ParserError("invalid_configuration", provider="fake")
            return
        if state.handle != handle:
            raise ParserError("job_not_found", provider="fake")
        if state.state == "destroyed":
            return
        state.state = "destroyed"
        state.safe_error_code = None
        state.artifact = None
        state.artifact_sha256 = None
        state.manifest_sha256 = None
        state.file_count = None
        if state.finished_at_ms is None:
            state.finished_at_ms = self._clock_ms()
        self.destroyed_provider_job_ids.append(handle.provider_job_id)

    async def _wait_for_gate_or_cancel(
        self,
        signal: asyncio.Event | None,
        *,
        timeout_seconds: float,
    ) -> ParserErrorCode | None:
        assert self._completion_gate is not None
        if timeout_seconds <= 0:
            return "request_timeout"
        gate_task = asyncio.create_task(self._completion_gate.wait())
        signal_task = asyncio.create_task(signal.wait()) if signal is not None else None
        tasks = {gate_task}
        if signal_task is not None:
            tasks.add(signal_task)
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return "request_timeout"
        if (
            signal_task is not None
            and signal_task in done
            and signal is not None
            and signal.is_set()
        ):
            return "cancelled"
        return None

    def _state_for_status(self, handle: ParserJobHandle) -> _FakeParserState:
        if handle.provider != "fake":
            raise ParserError("invalid_configuration", provider="fake")
        state = self._states.get(handle.provider_job_id)
        if state is None or state.handle != handle:
            raise ParserError("job_not_found", provider="fake")
        return state

    def _live_state(self, handle: ParserJobHandle) -> _FakeParserState:
        state = self._state_for_status(handle)
        if state.state == "destroyed":
            raise ParserError("job_not_found", provider="fake")
        return state

    def _snapshot(self, state: _FakeParserState) -> ParserJobStatus:
        return ParserJobStatus(
            handle=state.handle,
            state=state.state,
            observed_at_ms=self._clock_ms(),
            started_at_ms=state.started_at_ms,
            finished_at_ms=state.finished_at_ms,
            safe_error_code=state.safe_error_code,
            artifact_size_bytes=(
                len(state.artifact) if state.state == "succeeded" and state.artifact else None
            ),
            artifact_sha256=(
                state.artifact_sha256 if state.state == "succeeded" else None
            ),
        )

    def _mark_cancelled(self, state: _FakeParserState) -> ParserJobStatus:
        state.state = "cancelled"
        state.safe_error_code = "cancelled"
        state.finished_at_ms = self._clock_ms()
        return self._snapshot(state)

    def _mark_failed(
        self,
        state: _FakeParserState,
        code: ParserErrorCode,
    ) -> ParserJobStatus:
        state.state = "failed"
        state.safe_error_code = code
        state.finished_at_ms = self._clock_ms()
        return self._snapshot(state)

    @staticmethod
    def _verify_source(path: Path, spec: ParserJobSpec) -> None:
        if not path.is_absolute():
            raise ParserError("invalid_source", provider="fake")
        try:
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or path.is_symlink():
                raise ParserError("invalid_source", provider="fake")
            if before.st_size > spec.limits.max_source_bytes:
                raise ParserError("source_too_large", provider="fake")
            data = path.read_bytes()
            after = path.stat(follow_symlinks=False)
        except ParserError:
            raise
        except OSError as exc:
            raise ParserError("invalid_source", provider="fake") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise ParserError("invalid_source", provider="fake")
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != spec.source.size_bytes or digest != spec.source.sha256:
            raise ParserError("invalid_source", provider="fake")

    @staticmethod
    def _build_artifact(
        state: _FakeParserState,
    ) -> tuple[bytes, str, str, int]:
        spec = state.spec
        output = state.output
        markdown_bytes = output.markdown.encode("utf-8")
        if not markdown_bytes:
            raise ParserError("artifact_invalid", provider="fake")
        if output.page_count < 0:
            raise ParserError("artifact_invalid", provider="fake")
        if len(output.images) > spec.limits.max_image_count:
            raise ParserError("resource_limit", provider="fake")

        markdown_entry = ParserArtifactFile(
            path="parsed.md",
            kind="markdown",
            mime_type="text/markdown",
            size_bytes=len(markdown_bytes),
            sha256=hashlib.sha256(markdown_bytes).hexdigest(),
        )
        image_payloads: list[tuple[ParserArtifactFile, bytes]] = []
        for index, image in enumerate(output.images):
            if not image.content or len(image.content) > spec.limits.max_image_bytes:
                raise ParserError("resource_limit", provider="fake")
            digest = hashlib.sha256(image.content).hexdigest()
            suffix = _IMAGE_SUFFIX[image.mime_type]
            entry = ParserArtifactFile(
                path=f"images/img_{index:04d}_{digest[:16]}{suffix}",
                kind="embedded_image",
                mime_type=image.mime_type,
                size_bytes=len(image.content),
                sha256=digest,
                page_number=image.page_number,
            )
            image_payloads.append((entry, image.content))

        file_count = 2 + len(image_payloads)
        if file_count > spec.limits.max_artifact_files:
            raise ParserError("resource_limit", provider="fake")
        manifest = ParserArtifactManifest(
            schema_id=PARSER_ARTIFACT_SCHEMA,
            job_id=spec.job_id,
            source_id=spec.source.source_id,
            source_sha256=spec.source.sha256,
            provider="fake",
            provider_version=_FAKE_PROVIDER_VERSION,
            page_count=output.page_count,
            markdown=markdown_entry,
            images=tuple(entry for entry, _ in image_payloads),
            warnings=output.warnings,
        )
        manifest_bytes = (
            json.dumps(
                manifest.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
            FakeParserProvider._add_tar_file(archive, "manifest.json", manifest_bytes)
            FakeParserProvider._add_tar_file(archive, "parsed.md", markdown_bytes)
            for entry, payload in image_payloads:
                FakeParserProvider._add_tar_file(archive, entry.path, payload)
        artifact = buffer.getvalue()
        if len(artifact) > spec.limits.max_artifact_bytes:
            raise ParserError("resource_limit", provider="fake")
        return (
            artifact,
            hashlib.sha256(artifact).hexdigest(),
            hashlib.sha256(manifest_bytes).hexdigest(),
            file_count,
        )

    @staticmethod
    def _add_tar_file(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = 0
        info.mode = 0o600
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        archive.addfile(info, io.BytesIO(payload))

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def __repr__(self) -> str:
        active = sum(state.state != "destroyed" for state in self._states.values())
        return f"FakeParserProvider(active={active}, queued_outputs={len(self._outputs)})"


__all__ = [
    "FakeParserImage",
    "FakeParserOutput",
    "FakeParserProvider",
]

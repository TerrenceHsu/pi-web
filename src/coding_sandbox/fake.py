"""Deterministic offline backend for contract tests and local development."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .errors import SandboxError
from .models import (
    SandboxBackendName,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxOutputCallback,
    SandboxOutputChunk,
    SandboxStatus,
    SandboxTransferReceipt,
    validate_sandbox_path,
)


@dataclass
class _FakeSandboxState:
    handle: SandboxHandle
    spec: SandboxCreateSpec
    files: dict[str, bytes] = field(default_factory=dict)
    destroyed: bool = False


class FakeSandboxBackend:
    """In-memory provider implementation with real bounded local transfers.

    It never reads provider environment variables, opens sockets or launches a
    process. Queued command results let the same orchestration tests exercise
    success, failure, timeout and cancellation without cloud credentials.
    """

    def __init__(
        self,
        *,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        command_results: Iterable[SandboxCommandResult] | None = None,
    ) -> None:
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._id_factory = id_factory or (lambda: f"fake-{uuid4().hex}")
        self._states: dict[str, _FakeSandboxState] = {}
        self._command_results = deque(command_results or ())
        self.created_specs: list[SandboxCreateSpec] = []
        self.executed_commands: list[SandboxCommand] = []
        self.destroyed_sandbox_ids: list[str] = []

    def backend_name(self) -> SandboxBackendName:
        return "fake"

    async def create(self, spec: SandboxCreateSpec) -> SandboxHandle:
        now = self._clock_ms()
        handle = SandboxHandle(
            provider="fake",
            sandbox_id=self._id_factory(),
            operation_id=spec.operation_id,
            runtime_id=spec.runtime_id,
            created_at_ms=now,
            expires_at_ms=now + spec.limits.lifetime_seconds * 1000,
        )
        if handle.sandbox_id in self._states:
            raise SandboxError("protocol_error", provider="fake")
        self._states[handle.sandbox_id] = _FakeSandboxState(handle=handle, spec=spec)
        self.created_specs.append(spec)
        return handle

    async def attach(self, handle: SandboxHandle) -> SandboxHandle:
        return self._live_state(handle).handle

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        if handle.provider != "fake":
            raise SandboxError("invalid_configuration", provider="fake")
        state = self._states.get(handle.sandbox_id)
        if state is None or state.handle != handle:
            raise SandboxError("sandbox_not_found", provider="fake")
        return SandboxStatus(
            handle=handle,
            state="terminated" if state.destroyed else "ready",
            observed_at_ms=self._clock_ms(),
        )

    async def upload_file(
        self,
        handle: SandboxHandle,
        *,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
    ) -> SandboxTransferReceipt:
        state = self._live_state(handle)
        normalized = validate_sandbox_path(remote_path)
        try:
            data = await asyncio.to_thread(local_path.read_bytes)
        except OSError as exc:
            raise SandboxError("transfer_failed", provider="fake") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise SandboxError("transfer_failed", provider="fake")
        if len(data) > state.spec.limits.max_upload_bytes:
            raise SandboxError("resource_limit", provider="fake")
        state.files[normalized] = data
        return SandboxTransferReceipt(
            remote_path=normalized,
            size=len(data),
            sha256=digest,
        )

    async def download_file(
        self,
        handle: SandboxHandle,
        *,
        remote_path: str,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> SandboxTransferReceipt:
        state = self._live_state(handle)
        normalized = validate_sandbox_path(remote_path)
        try:
            data = state.files[normalized]
        except KeyError as exc:
            raise SandboxError("transfer_failed", provider="fake") from exc
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise SandboxError("transfer_failed", provider="fake")
        try:
            await asyncio.to_thread(self._atomic_write, local_path, data)
        except OSError as exc:
            raise SandboxError("transfer_failed", provider="fake") from exc
        return SandboxTransferReceipt(
            remote_path=normalized,
            size=len(data),
            sha256=digest,
        )

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        state = self._live_state(handle)
        if (
            command.timeout_seconds > state.spec.limits.command_timeout_seconds
            or command.max_output_bytes > state.spec.limits.max_output_bytes
        ):
            raise SandboxError("resource_limit", provider="fake")
        self.executed_commands.append(command)
        now = self._clock_ms()
        if signal is not None and signal.is_set():
            return SandboxCommandResult(
                command_id=command.command_id,
                termination_reason="cancelled",
                exit_code=None,
                started_at_ms=now,
                finished_at_ms=now,
            )

        if self._command_results:
            result = self._command_results.popleft().model_copy(
                update={"command_id": command.command_id}
            )
        else:
            result = SandboxCommandResult(
                command_id=command.command_id,
                started_at_ms=now,
                finished_at_ms=now,
            )
        result = self._bound_result(result, command.max_output_bytes)
        if on_output is not None:
            sequence = 0
            for stream, content in (("stdout", result.stdout), ("stderr", result.stderr)):
                if not content:
                    continue
                await on_output(
                    SandboxOutputChunk(
                        command_id=command.command_id,
                        sequence=sequence,
                        stream=stream,
                        text=content,
                    )
                )
                sequence += 1
        return result

    async def destroy(self, handle: SandboxHandle) -> None:
        if handle.provider != "fake":
            raise SandboxError("invalid_configuration", provider="fake")
        state = self._states.get(handle.sandbox_id)
        if state is None or state.destroyed:
            return
        if state.handle != handle:
            raise SandboxError("sandbox_not_found", provider="fake")
        state.destroyed = True
        self.destroyed_sandbox_ids.append(handle.sandbox_id)

    def queue_command_result(self, result: SandboxCommandResult) -> None:
        self._command_results.append(result)

    def _live_state(self, handle: SandboxHandle) -> _FakeSandboxState:
        if handle.provider != "fake":
            raise SandboxError("invalid_configuration", provider="fake")
        state = self._states.get(handle.sandbox_id)
        if state is None or state.destroyed or state.handle != handle:
            raise SandboxError("sandbox_not_found", provider="fake")
        return state

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(data)
            temp_path.replace(path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _bound_result(
        result: SandboxCommandResult,
        max_output_bytes: int,
    ) -> SandboxCommandResult:
        remaining = max_output_bytes
        stdout, stdout_truncated = _truncate_utf8(result.stdout, remaining)
        remaining -= len(stdout.encode("utf-8"))
        stderr, stderr_truncated = _truncate_utf8(result.stderr, remaining)
        truncated = result.output_truncated or stdout_truncated or stderr_truncated
        return result.model_copy(
            update={
                "stdout": stdout,
                "stderr": stderr,
                "output_truncated": truncated,
            }
        )

    def __repr__(self) -> str:
        active = sum(not state.destroyed for state in self._states.values())
        return f"FakeSandboxBackend(active={active}, queued={len(self._command_results)})"


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    truncated = raw[:limit].decode("utf-8", errors="ignore")
    return truncated, True


__all__ = ["FakeSandboxBackend"]

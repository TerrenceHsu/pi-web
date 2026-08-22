"""E2B adapter with a narrow, optional SDK boundary.

The public backend never imports E2B at module import time.  Production uses
``E2BSdkDriver``; tests inject an ``E2BDriver`` implementation and therefore
remain completely offline.  Provider exceptions are always translated to the
fixed, credential-free ``SandboxError`` taxonomy.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import shlex
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import IO, Literal, Protocol, cast
from uuid import uuid4

from .errors import SandboxError, SandboxErrorCode
from .models import (
    SandboxBackendName,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxLimits,
    SandboxNetworkPolicy,
    SandboxOutputCallback,
    SandboxOutputChunk,
    SandboxStatus,
    SandboxTransferReceipt,
    validate_sandbox_path,
)

_PROVIDER = "e2b"
_METADATA_OPERATION_ID = "pi_agent_operation_id"
_METADATA_RUNTIME_ID = "pi_agent_runtime_id"
_TRANSFER_CHUNK_SIZE = 1024 * 1024

E2BProviderState = Literal["running", "paused", "terminated", "failed"]
E2BStreamCallback = Callable[[str], Awaitable[None]]


class E2BDriverCommandResult(Protocol):
    """Minimum command result surface consumed from an E2B driver."""

    @property
    def exit_code(self) -> int: ...


class E2BDriverCommandHandle(Protocol):
    """Killable background command, required for real cancellation."""

    async def wait(self) -> E2BDriverCommandResult: ...

    async def kill(self) -> bool: ...


class E2BDriverSession(Protocol):
    """Narrow session API independent of E2B SDK object shapes."""

    @property
    def sandbox_id(self) -> str: ...

    async def metadata(self, *, request_timeout: float) -> Mapping[str, str]: ...

    async def make_dir(self, path: str, *, request_timeout: float) -> None: ...

    async def upload(
        self,
        remote_path: str,
        stream: IO[bytes],
        *,
        request_timeout: float,
    ) -> None: ...

    async def download(
        self,
        remote_path: str,
        *,
        request_timeout: float,
    ) -> AsyncIterator[bytes]: ...

    async def start_command(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float,
        request_timeout: float,
        on_stdout: E2BStreamCallback,
        on_stderr: E2BStreamCallback,
    ) -> E2BDriverCommandHandle: ...


class E2BDriver(Protocol):
    """Injectable provider seam used by ``E2BSandboxBackend``."""

    async def create(
        self,
        *,
        template_id: str,
        lifetime_seconds: int,
        metadata: Mapping[str, str],
        network: SandboxNetworkPolicy,
        api_key: str,
        request_timeout: float,
    ) -> E2BDriverSession: ...

    async def connect(
        self,
        sandbox_id: str,
        *,
        lifetime_seconds: int,
        api_key: str,
        request_timeout: float,
    ) -> E2BDriverSession: ...

    async def state(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> E2BProviderState: ...

    async def kill(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> bool: ...


class E2BSandboxBackend:
    """Provider-neutral backend backed by E2B ``AsyncSandbox``.

    The API key is an ephemeral constructor value resolved from ``SecretStore``
    by the composition root.  It is never copied into handles, DTOs, metadata,
    logs or ``repr`` output.
    """

    def __init__(
        self,
        *,
        api_key: str,
        driver: E2BDriver | None = None,
        default_limits: SandboxLimits | None = None,
        request_timeout_seconds: float = 60.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not api_key or api_key != api_key.strip() or any(ord(char) < 32 for char in api_key):
            raise SandboxError("invalid_configuration", provider=_PROVIDER)
        if request_timeout_seconds <= 0 or request_timeout_seconds > 3600:
            raise SandboxError("invalid_configuration", provider=_PROVIDER)
        self._api_key = api_key
        self._driver = driver or E2BSdkDriver()
        self._default_limits = default_limits or SandboxLimits()
        self._request_timeout = request_timeout_seconds
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._sessions: dict[str, E2BDriverSession] = {}
        self._handles: dict[str, SandboxHandle] = {}
        self._limits: dict[str, SandboxLimits] = {}

    def backend_name(self) -> SandboxBackendName:
        return "e2b"

    async def create(self, spec: SandboxCreateSpec) -> SandboxHandle:
        metadata = {
            _METADATA_OPERATION_ID: spec.operation_id,
            _METADATA_RUNTIME_ID: spec.runtime_id,
        }
        session: E2BDriverSession | None = None
        try:
            session = await self._driver.create(
                template_id=spec.runtime_id,
                lifetime_seconds=spec.limits.lifetime_seconds,
                metadata=metadata,
                network=spec.network,
                api_key=self._api_key,
                request_timeout=self._request_timeout,
            )
            if not session.sandbox_id:
                raise SandboxError("protocol_error", provider=_PROVIDER)
            await session.make_dir(spec.workdir, request_timeout=self._request_timeout)
        except asyncio.CancelledError:
            if session is not None:
                await self._best_effort_kill(session.sandbox_id)
            raise
        except Exception as exc:
            if session is not None:
                await self._best_effort_kill(session.sandbox_id)
            raise _translate_exception(exc) from exc

        now = self._clock_ms()
        handle = SandboxHandle(
            provider="e2b",
            sandbox_id=session.sandbox_id,
            operation_id=spec.operation_id,
            runtime_id=spec.runtime_id,
            created_at_ms=now,
            expires_at_ms=now + spec.limits.lifetime_seconds * 1000,
        )
        self._sessions[handle.sandbox_id] = session
        self._handles[handle.sandbox_id] = handle
        self._limits[handle.sandbox_id] = spec.limits
        return handle

    async def attach(self, handle: SandboxHandle) -> SandboxHandle:
        self._validate_handle(handle)
        known = self._handles.get(handle.sandbox_id)
        if known is not None and known != handle:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        try:
            session = await self._driver.connect(
                handle.sandbox_id,
                lifetime_seconds=self._remaining_lifetime(handle),
                api_key=self._api_key,
                request_timeout=self._request_timeout,
            )
            if session.sandbox_id != handle.sandbox_id:
                raise SandboxError("protocol_error", provider=_PROVIDER)
            metadata = await session.metadata(request_timeout=self._request_timeout)
            if (
                metadata.get(_METADATA_OPERATION_ID) != handle.operation_id
                or metadata.get(_METADATA_RUNTIME_ID) != handle.runtime_id
            ):
                raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        except Exception as exc:
            raise _translate_exception(exc) from exc
        self._sessions[handle.sandbox_id] = session
        self._handles[handle.sandbox_id] = handle
        return handle

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        self._validate_handle(handle)
        known = self._handles.get(handle.sandbox_id)
        if known is not None and known != handle:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        try:
            provider_state = await self._driver.state(
                handle.sandbox_id,
                api_key=self._api_key,
                request_timeout=self._request_timeout,
            )
        except Exception as exc:
            raise _translate_exception(exc) from exc
        state = {
            "running": "ready",
            "paused": "paused",
            "terminated": "terminated",
            "failed": "failed",
        }[provider_state]
        return SandboxStatus(
            handle=handle,
            state=state,
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
        normalized = validate_sandbox_path(remote_path)
        limits = self._limits_for(handle)
        try:
            size, digest = await asyncio.to_thread(
                _hash_local_file,
                local_path,
                limits.max_upload_bytes,
            )
            if digest != expected_sha256:
                raise SandboxError("transfer_failed", provider=_PROVIDER)
            session = await self._session_for(handle)
            with local_path.open("rb") as stream:
                async with asyncio.timeout(self._request_timeout):
                    await session.upload(
                        normalized,
                        stream,
                        request_timeout=self._request_timeout,
                    )
            remote_size, remote_digest = await self._hash_remote(
                session,
                normalized,
                max_bytes=limits.max_upload_bytes,
            )
            if remote_size != size or remote_digest != digest:
                raise SandboxError("transfer_failed", provider=_PROVIDER)
        except Exception as exc:
            raise _translate_exception(exc, transfer=True) from exc
        return SandboxTransferReceipt(
            remote_path=normalized,
            size=size,
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
        normalized = validate_sandbox_path(remote_path)
        limits = self._limits_for(handle)
        temp_path = local_path.with_name(f".{local_path.name}.{uuid4().hex}.tmp")
        stream: AsyncIterator[bytes] | None = None
        try:
            session = await self._session_for(handle)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            stream = await session.download(
                normalized,
                request_timeout=self._request_timeout,
            )
            with temp_path.open("wb") as destination:
                async with asyncio.timeout(self._request_timeout):
                    async for chunk in stream:
                        size += len(chunk)
                        if size > limits.max_upload_bytes:
                            raise SandboxError("resource_limit", provider=_PROVIDER)
                        digest.update(chunk)
                        destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            actual_digest = digest.hexdigest()
            if expected_sha256 is not None and actual_digest != expected_sha256:
                raise SandboxError("transfer_failed", provider=_PROVIDER)
            temp_path.replace(local_path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise _translate_exception(exc, transfer=True) from exc
        finally:
            if stream is not None:
                await _close_async_iterator(stream)
        return SandboxTransferReceipt(
            remote_path=normalized,
            size=size,
            sha256=actual_digest,
        )

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        limits = self._limits_for(handle)
        if (
            command.timeout_seconds > limits.command_timeout_seconds
            or command.max_output_bytes > limits.max_output_bytes
        ):
            raise SandboxError("resource_limit", provider=_PROVIDER)
        started_at = self._clock_ms()
        if signal is not None and signal.is_set():
            return _cancelled_result(command.command_id, started_at, self._clock_ms())

        collector = _OutputCollector(command, on_output=on_output)
        command_handle: E2BDriverCommandHandle | None = None
        try:
            session = await self._session_for(handle)
            command_handle = await session.start_command(
                shlex.join(command.argv),
                cwd=command.cwd,
                timeout_seconds=float(command.timeout_seconds),
                request_timeout=self._request_timeout,
                on_stdout=collector.stdout,
                on_stderr=collector.stderr,
            )
            async with asyncio.timeout(float(command.timeout_seconds)):
                driver_result, cancelled = await _wait_for_command(
                    command_handle,
                    signal=signal,
                )
            if cancelled:
                return _cancelled_result(
                    command.command_id,
                    started_at,
                    self._clock_ms(),
                    collector=collector,
                )
            if driver_result is None:
                raise SandboxError("protocol_error", provider=_PROVIDER)
            return SandboxCommandResult(
                command_id=command.command_id,
                exit_code=driver_result.exit_code,
                stdout=collector.stdout_text,
                stderr=collector.stderr_text,
                started_at_ms=started_at,
                finished_at_ms=self._clock_ms(),
                output_truncated=collector.truncated,
            )
        except asyncio.CancelledError:
            if command_handle is not None:
                await _best_effort_command_kill(command_handle)
            raise
        except Exception as exc:
            translated = _translate_exception(exc, command=True)
            if command_handle is not None and translated.code == "command_timeout":
                await _best_effort_command_kill(command_handle)
            raise translated from exc

    async def destroy(self, handle: SandboxHandle) -> None:
        self._validate_handle(handle)
        known = self._handles.get(handle.sandbox_id)
        if known is not None and known != handle:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        try:
            await self._driver.kill(
                handle.sandbox_id,
                api_key=self._api_key,
                request_timeout=self._request_timeout,
            )
        except Exception as exc:
            translated = _translate_exception(exc)
            if translated.code != "sandbox_not_found":
                raise translated from exc
        self._sessions.pop(handle.sandbox_id, None)
        self._handles.pop(handle.sandbox_id, None)
        self._limits.pop(handle.sandbox_id, None)

    async def _session_for(self, handle: SandboxHandle) -> E2BDriverSession:
        self._validate_handle(handle)
        known = self._handles.get(handle.sandbox_id)
        if known is not None and known != handle:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        session = self._sessions.get(handle.sandbox_id)
        if session is None:
            await self.attach(handle)
            session = self._sessions[handle.sandbox_id]
        return session

    async def _hash_remote(
        self,
        session: E2BDriverSession,
        remote_path: str,
        *,
        max_bytes: int,
    ) -> tuple[int, str]:
        stream = await session.download(
            remote_path,
            request_timeout=self._request_timeout,
        )
        digest = hashlib.sha256()
        size = 0
        try:
            async with asyncio.timeout(self._request_timeout):
                async for chunk in stream:
                    size += len(chunk)
                    if size > max_bytes:
                        raise SandboxError("resource_limit", provider=_PROVIDER)
                    digest.update(chunk)
        finally:
            await _close_async_iterator(stream)
        return size, digest.hexdigest()

    def _limits_for(self, handle: SandboxHandle) -> SandboxLimits:
        self._validate_handle(handle)
        known = self._handles.get(handle.sandbox_id)
        if known is not None and known != handle:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        return self._limits.get(handle.sandbox_id, self._default_limits)

    def _remaining_lifetime(self, handle: SandboxHandle) -> int:
        if handle.expires_at_ms is None:
            return self._default_limits.lifetime_seconds
        remaining_ms = handle.expires_at_ms - self._clock_ms()
        if remaining_ms <= 0:
            raise SandboxError("sandbox_not_found", provider=_PROVIDER)
        return max(1, (remaining_ms + 999) // 1000)

    @staticmethod
    def _validate_handle(handle: SandboxHandle) -> None:
        if handle.provider != "e2b":
            raise SandboxError("invalid_configuration", provider=_PROVIDER)

    async def _best_effort_kill(self, sandbox_id: str) -> None:
        try:
            await asyncio.shield(
                self._driver.kill(
                    sandbox_id,
                    api_key=self._api_key,
                    request_timeout=self._request_timeout,
                )
            )
        except BaseException:
            pass

    def __repr__(self) -> str:
        return f"E2BSandboxBackend(active={len(self._sessions)})"


class _OutputCollector:
    def __init__(
        self,
        command: SandboxCommand,
        *,
        on_output: SandboxOutputCallback | None,
    ) -> None:
        self._command = command
        self._on_output = on_output
        self._remaining = command.max_output_bytes
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self.truncated = False

    @property
    def stdout_text(self) -> str:
        return "".join(self._stdout)

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr)

    async def stdout(self, text: str) -> None:
        await self._add("stdout", text)

    async def stderr(self, text: str) -> None:
        await self._add("stderr", text)

    async def _add(self, stream: Literal["stdout", "stderr"], text: str) -> None:
        async with self._lock:
            selected, truncated = _truncate_utf8(text, self._remaining)
            self.truncated = self.truncated or truncated
            self._remaining -= len(selected.encode("utf-8"))
            if not selected:
                return
            if stream == "stdout":
                self._stdout.append(selected)
            else:
                self._stderr.append(selected)
            if self._on_output is not None:
                await self._on_output(
                    SandboxOutputChunk(
                        command_id=self._command.command_id,
                        sequence=self._sequence,
                        stream=stream,
                        text=selected,
                    )
                )
            self._sequence += 1


async def _wait_for_command(
    handle: E2BDriverCommandHandle,
    *,
    signal: asyncio.Event | None,
) -> tuple[E2BDriverCommandResult | None, bool]:
    wait_task = asyncio.create_task(handle.wait())
    if signal is None:
        return await wait_task, False
    cancel_task = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            {wait_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            return await wait_task, False
        if cancel_task in done and cancel_task.result():
            await _best_effort_command_kill(handle)
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
            return None, True
        return await wait_task, False
    except BaseException:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        raise
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)


def _cancelled_result(
    command_id: str,
    started_at_ms: int,
    finished_at_ms: int,
    *,
    collector: _OutputCollector | None = None,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id=command_id,
        termination_reason="cancelled",
        exit_code=None,
        stdout="" if collector is None else collector.stdout_text,
        stderr="" if collector is None else collector.stderr_text,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        output_truncated=False if collector is None else collector.truncated,
    )


async def _best_effort_command_kill(handle: E2BDriverCommandHandle) -> None:
    try:
        await asyncio.shield(handle.kill())
    except BaseException:
        pass


def _hash_local_file(path: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        file_stat = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
            raise SandboxError("transfer_failed", provider=_PROVIDER)
        with path.open("rb") as stream:
            while chunk := stream.read(_TRANSFER_CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise SandboxError("resource_limit", provider=_PROVIDER)
                digest.update(chunk)
        final_stat = path.stat(follow_symlinks=False)
    except SandboxError:
        raise
    except OSError as exc:
        raise SandboxError("transfer_failed", provider=_PROVIDER) from exc
    identity = (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)
    final_identity = (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
        final_stat.st_mtime_ns,
    )
    if identity != final_identity or size != final_stat.st_size:
        raise SandboxError("transfer_failed", provider=_PROVIDER)
    return size, digest.hexdigest()


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


async def _close_async_iterator(stream: AsyncIterator[bytes]) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await cast(Awaitable[None], close())
    except Exception:
        pass


def _translate_exception(
    exc: Exception,
    *,
    command: bool = False,
    transfer: bool = False,
) -> SandboxError:
    if isinstance(exc, SandboxError):
        return SandboxError(exc.code, provider=exc.provider or _PROVIDER)
    name = type(exc).__name__
    code: SandboxErrorCode
    if name in {"AuthenticationException", "AuthenticationError"}:
        code = "authentication_failed"
    elif name in {"RateLimitException", "RateLimitError"}:
        code = "rate_limited"
    elif name in {
        "SandboxNotFoundException",
        "NotFoundException",
        "FileNotFoundException",
    }:
        code = "transfer_failed" if transfer else "sandbox_not_found"
    elif name in {"TimeoutException", "TimeoutError"} or isinstance(
        exc,
        (asyncio.TimeoutError, TimeoutError),
    ):
        code = "command_timeout" if command else "request_timeout"
    elif name in {"NotEnoughSpaceException", "ResourceLimitError"}:
        code = "resource_limit"
    elif name in {"PermissionError", "PermissionDeniedException"} or isinstance(
        exc,
        PermissionError,
    ):
        code = "permission_denied"
    elif name in {"InvalidArgumentException", "TemplateException"}:
        code = "invalid_configuration"
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        code = "invalid_configuration"
    elif isinstance(exc, ConnectionError):
        code = "provider_unavailable"
    elif isinstance(exc, OSError):
        code = "transfer_failed" if transfer else "provider_unavailable"
    elif transfer:
        code = "transfer_failed"
    else:
        code = "unknown_error"
    return SandboxError(code, provider=_PROVIDER)


class _RawCommandResult(Protocol):
    exit_code: int


class _RawCommandHandle(Protocol):
    async def wait(self) -> _RawCommandResult: ...

    async def kill(self) -> bool: ...


class _RawCommands(Protocol):
    async def run(self, command: str, **kwargs: object) -> object: ...


class _RawFiles(Protocol):
    async def make_dir(self, path: str, **kwargs: object) -> bool: ...

    async def write(self, path: str, data: IO[bytes], **kwargs: object) -> object: ...

    async def read(self, path: str, **kwargs: object) -> object: ...


class _RawSandbox(Protocol):
    sandbox_id: str
    files: _RawFiles
    commands: _RawCommands

    async def get_info(self, **kwargs: object) -> object: ...


class _RawAsyncSandboxApi(Protocol):
    async def create(self, **kwargs: object) -> object: ...

    async def connect(self, sandbox_id: str, **kwargs: object) -> object: ...

    async def get_info(self, sandbox_id: str, **kwargs: object) -> object: ...

    async def kill(self, sandbox_id: str, **kwargs: object) -> bool: ...


class E2BSdkDriver:
    """Lazy bridge to the optional official E2B Python SDK."""

    def __init__(self) -> None:
        self._api: _RawAsyncSandboxApi | None = None

    async def create(
        self,
        *,
        template_id: str,
        lifetime_seconds: int,
        metadata: Mapping[str, str],
        network: SandboxNetworkPolicy,
        api_key: str,
        request_timeout: float,
    ) -> E2BDriverSession:
        api = self._load_api()
        network_options: dict[str, object] = {"allow_public_traffic": False}
        allow_internet_access = network.mode == "allowlist"
        if allow_internet_access:
            network_options["allow_out"] = [
                *network.allowed_domains,
                *network.allowed_cidrs,
            ]
        raw = await api.create(
            template=template_id,
            timeout=lifetime_seconds,
            metadata=dict(metadata),
            secure=True,
            allow_internet_access=allow_internet_access,
            network=network_options,
            lifecycle={"on_timeout": "kill", "auto_resume": False},
            api_key=api_key,
            request_timeout=request_timeout,
        )
        return _E2BSdkSession(cast(_RawSandbox, raw), initial_metadata=metadata)

    async def connect(
        self,
        sandbox_id: str,
        *,
        lifetime_seconds: int,
        api_key: str,
        request_timeout: float,
    ) -> E2BDriverSession:
        raw = await self._load_api().connect(
            sandbox_id,
            timeout=lifetime_seconds,
            api_key=api_key,
            request_timeout=request_timeout,
        )
        return _E2BSdkSession(cast(_RawSandbox, raw))

    async def state(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> E2BProviderState:
        info = await self._load_api().get_info(
            sandbox_id,
            api_key=api_key,
            request_timeout=request_timeout,
        )
        raw_state = getattr(info, "state", "running")
        value = str(getattr(raw_state, "value", raw_state)).lower()
        if value in {"running", "paused", "failed"}:
            return cast(E2BProviderState, value)
        return "terminated"

    async def kill(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> bool:
        return await self._load_api().kill(
            sandbox_id,
            api_key=api_key,
            request_timeout=request_timeout,
        )

    def _load_api(self) -> _RawAsyncSandboxApi:
        if self._api is not None:
            return self._api
        try:
            module = importlib.import_module("e2b")
            raw_api = vars(module).get("AsyncSandbox")
            if raw_api is None:
                raise ImportError
        except ImportError as exc:
            raise SandboxError("invalid_configuration", provider=_PROVIDER) from exc
        self._api = cast(_RawAsyncSandboxApi, raw_api)
        return self._api


class _E2BSdkSession:
    def __init__(
        self,
        sandbox: _RawSandbox,
        *,
        initial_metadata: Mapping[str, str] | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._initial_metadata = dict(initial_metadata) if initial_metadata else None

    @property
    def sandbox_id(self) -> str:
        return self._sandbox.sandbox_id

    async def metadata(self, *, request_timeout: float) -> Mapping[str, str]:
        if self._initial_metadata is not None:
            return dict(self._initial_metadata)
        info = await self._sandbox.get_info(request_timeout=request_timeout)
        metadata = getattr(info, "metadata", None)
        if not isinstance(metadata, Mapping):
            return {}
        return {
            str(key): str(value)
            for key, value in metadata.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    async def make_dir(self, path: str, *, request_timeout: float) -> None:
        await self._sandbox.files.make_dir(path, request_timeout=request_timeout)

    async def upload(
        self,
        remote_path: str,
        stream: IO[bytes],
        *,
        request_timeout: float,
    ) -> None:
        await self._sandbox.files.write(
            remote_path,
            stream,
            request_timeout=request_timeout,
            use_octet_stream=True,
        )

    async def download(
        self,
        remote_path: str,
        *,
        request_timeout: float,
    ) -> AsyncIterator[bytes]:
        raw = await self._sandbox.files.read(
            remote_path,
            format="stream",
            request_timeout=request_timeout,
            stream_idle_timeout=min(request_timeout, 30.0),
        )
        if not hasattr(raw, "__aiter__"):
            raise SandboxError("protocol_error", provider=_PROVIDER)
        return cast(AsyncIterator[bytes], raw)

    async def start_command(
        self,
        command: str,
        *,
        cwd: str,
        timeout_seconds: float,
        request_timeout: float,
        on_stdout: E2BStreamCallback,
        on_stderr: E2BStreamCallback,
    ) -> E2BDriverCommandHandle:
        raw = await self._sandbox.commands.run(
            command,
            background=True,
            cwd=cwd,
            timeout=timeout_seconds,
            request_timeout=request_timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        if not hasattr(raw, "wait") or not hasattr(raw, "kill"):
            raise SandboxError("protocol_error", provider=_PROVIDER)
        return _E2BSdkCommandHandle(cast(_RawCommandHandle, raw))


class _E2BSdkCommandHandle:
    def __init__(self, raw: _RawCommandHandle) -> None:
        self._raw = raw

    async def wait(self) -> E2BDriverCommandResult:
        try:
            return await self._raw.wait()
        except Exception as exc:
            exit_code = getattr(exc, "exit_code", None)
            if isinstance(exit_code, int):
                return _CapturedExitCode(exit_code)
            raise

    async def kill(self) -> bool:
        return await self._raw.kill()


class _CapturedExitCode:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    @property
    def exit_code(self) -> int:
        return self._exit_code


__all__ = [
    "E2BDriver",
    "E2BDriverCommandHandle",
    "E2BDriverCommandResult",
    "E2BDriverSession",
    "E2BProviderState",
    "E2BSandboxBackend",
    "E2BSdkDriver",
]

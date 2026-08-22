"""Offline E2B backend tests through the injectable driver seam."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import shlex
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import IO

import pytest

from coding_sandbox import (
    E2BSandboxBackend,
    SandboxBackend,
    SandboxCommand,
    SandboxCreateSpec,
    SandboxError,
    SandboxHandle,
    SandboxLimits,
    SandboxNetworkPolicy,
    SandboxOutputChunk,
)
from coding_sandbox.e2b import (
    E2BDriverCommandHandle,
    E2BDriverCommandResult,
    E2BDriverSession,
    E2BProviderState,
    E2BSdkDriver,
    E2BStreamCallback,
)


class AuthenticationException(Exception):
    pass


class TimeoutException(Exception):
    pass


@dataclass(frozen=True)
class _Result:
    exit_code: int


class _CommandHandle:
    def __init__(
        self,
        *,
        result: E2BDriverCommandResult | None = None,
        error: Exception | None = None,
        output: tuple[tuple[str, str], ...] = (),
        on_stdout: E2BStreamCallback,
        on_stderr: E2BStreamCallback,
        blocker: asyncio.Event | None = None,
    ) -> None:
        self._result = result or _Result(0)
        self._error = error
        self._output = output
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._blocker = blocker
        self.killed = False

    async def wait(self) -> E2BDriverCommandResult:
        if self._blocker is not None:
            await self._blocker.wait()
        for stream, text in self._output:
            callback = self._on_stdout if stream == "stdout" else self._on_stderr
            await callback(text)
        if self._error is not None:
            raise self._error
        return self._result

    async def kill(self) -> bool:
        self.killed = True
        return True


class _Session:
    def __init__(
        self,
        sandbox_id: str,
        metadata: Mapping[str, str],
    ) -> None:
        self._sandbox_id = sandbox_id
        self._metadata = dict(metadata)
        self.files: dict[str, bytes] = {}
        self.made_dirs: list[str] = []
        self.commands: list[tuple[str, str, float, float]] = []
        self.make_dir_error: Exception | None = None
        self.upload_transform: Callable[[bytes], bytes] = lambda value: value
        self.queued_commands: list[
            tuple[
                E2BDriverCommandResult | None,
                Exception | None,
                tuple[tuple[str, str], ...],
                asyncio.Event | None,
            ]
        ] = []
        self.handles: list[_CommandHandle] = []

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    async def metadata(self, *, request_timeout: float) -> Mapping[str, str]:
        return dict(self._metadata)

    async def make_dir(self, path: str, *, request_timeout: float) -> None:
        if self.make_dir_error is not None:
            raise self.make_dir_error
        self.made_dirs.append(path)

    async def upload(
        self,
        remote_path: str,
        stream: IO[bytes],
        *,
        request_timeout: float,
    ) -> None:
        self.files[remote_path] = self.upload_transform(stream.read())

    async def download(
        self,
        remote_path: str,
        *,
        request_timeout: float,
    ) -> AsyncIterator[bytes]:
        data = self.files[remote_path]

        async def chunks() -> AsyncIterator[bytes]:
            midpoint = max(1, len(data) // 2)
            yield data[:midpoint]
            if data[midpoint:]:
                yield data[midpoint:]

        return chunks()

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
        self.commands.append((command, cwd, timeout_seconds, request_timeout))
        queued = self.queued_commands.pop(0) if self.queued_commands else (None, None, (), None)
        handle = _CommandHandle(
            result=queued[0],
            error=queued[1],
            output=queued[2],
            blocker=queued[3],
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )
        self.handles.append(handle)
        return handle


class _Driver:
    def __init__(self) -> None:
        self.session = _Session(
            "e2b-sandbox-1",
            {
                "pi_agent_operation_id": "op-e2b",
                "pi_agent_runtime_id": "template-python",
            },
        )
        self.create_calls: list[dict[str, object]] = []
        self.connect_calls: list[str] = []
        self.state_calls: list[str] = []
        self.kill_calls: list[str] = []
        self.provider_state: E2BProviderState = "running"
        self.create_error: Exception | None = None
        self.kill_error: Exception | None = None

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
        self.create_calls.append(
            {
                "template_id": template_id,
                "lifetime_seconds": lifetime_seconds,
                "metadata": dict(metadata),
                "network": network,
                "api_key": api_key,
                "request_timeout": request_timeout,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        self.session._metadata = dict(metadata)
        return self.session

    async def connect(
        self,
        sandbox_id: str,
        *,
        lifetime_seconds: int,
        api_key: str,
        request_timeout: float,
    ) -> E2BDriverSession:
        self.connect_calls.append(sandbox_id)
        return self.session

    async def state(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> E2BProviderState:
        self.state_calls.append(sandbox_id)
        return self.provider_state

    async def kill(
        self,
        sandbox_id: str,
        *,
        api_key: str,
        request_timeout: float,
    ) -> bool:
        self.kill_calls.append(sandbox_id)
        if self.kill_error is not None:
            raise self.kill_error
        return True


def _spec(
    *,
    network: SandboxNetworkPolicy | None = None,
    limits: SandboxLimits | None = None,
) -> SandboxCreateSpec:
    return SandboxCreateSpec(
        operation_id="op-e2b",
        runtime_id="template-python",
        network=network or SandboxNetworkPolicy(),
        limits=limits or SandboxLimits(),
    )


def _backend(
    driver: _Driver,
    *,
    clock_ms: Callable[[], int] | None = None,
) -> E2BSandboxBackend:
    return E2BSandboxBackend(
        api_key="e2b_test-placeholder",
        driver=driver,
        clock_ms=clock_ms,
    )


def test_e2b_backend_is_protocol_compatible_and_never_repr_leaks_key() -> None:
    driver = _Driver()
    backend: SandboxBackend = _backend(driver)

    assert isinstance(backend, SandboxBackend)
    assert backend.backend_name() == "e2b"
    assert "e2b_test-placeholder" not in repr(backend)
    with pytest.raises(SandboxError) as exc_info:
        E2BSandboxBackend(api_key="  ")
    assert exc_info.value.code == "invalid_configuration"


@pytest.mark.asyncio
async def test_create_passes_closed_network_metadata_and_initializes_workdir() -> None:
    driver = _Driver()
    backend = _backend(driver, clock_ms=lambda: 1_000)

    handle = await backend.create(_spec())

    assert handle.sandbox_id == "e2b-sandbox-1"
    assert handle.expires_at_ms == 1_801_000
    assert driver.session.made_dirs == ["/workspace"]
    call = driver.create_calls[0]
    assert call["template_id"] == "template-python"
    assert call["network"] == SandboxNetworkPolicy(mode="none")
    assert call["metadata"] == {
        "pi_agent_operation_id": "op-e2b",
        "pi_agent_runtime_id": "template-python",
    }
    assert "e2b_test-placeholder" not in handle.model_dump_json()


@pytest.mark.asyncio
async def test_create_failure_after_allocation_kills_sandbox() -> None:
    driver = _Driver()
    driver.session.make_dir_error = ConnectionError("provider detail")
    backend = _backend(driver)

    with pytest.raises(SandboxError) as exc_info:
        await backend.create(_spec())

    assert exc_info.value.code == "provider_unavailable"
    assert driver.kill_calls == ["e2b-sandbox-1"]
    assert "provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_authentication_error_is_safe_and_not_retryable() -> None:
    driver = _Driver()
    driver.create_error = AuthenticationException("contains credential")

    with pytest.raises(SandboxError) as exc_info:
        await _backend(driver).create(_spec())

    assert exc_info.value.code == "authentication_failed"
    assert exc_info.value.retryable is False
    assert "credential" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_attach_verifies_provider_metadata_and_rejects_tampered_handle() -> None:
    driver = _Driver()
    original = await _backend(driver).create(_spec())
    restarted = _backend(driver)

    assert await restarted.attach(original) == original
    tampered = original.model_copy(update={"operation_id": "other-operation"})
    with pytest.raises(SandboxError) as exc_info:
        await _backend(driver).attach(tampered)
    assert exc_info.value.code == "sandbox_not_found"


@pytest.mark.asyncio
async def test_status_uses_non_resuming_driver_probe() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.provider_state = "terminated"

    status = await backend.status(handle)

    assert status.state == "terminated"
    assert driver.state_calls == [handle.sandbox_id]
    assert driver.connect_calls == []


@pytest.mark.asyncio
async def test_status_preserves_paused_state_without_resuming() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.provider_state = "paused"

    status = await backend.status(handle)

    assert status.state == "paused"
    assert driver.connect_calls == []


@pytest.mark.asyncio
async def test_upload_rehashes_remote_content_and_download_is_atomic(tmp_path: Path) -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    source = tmp_path / "snapshot.tar.gz"
    source.write_bytes(b"immutable snapshot")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    uploaded = await backend.upload_file(
        handle,
        local_path=source,
        remote_path="/workspace/input.tar.gz",
        expected_sha256=digest,
    )
    destination = tmp_path / "downloads" / "artifact.tar.gz"
    downloaded = await backend.download_file(
        handle,
        remote_path="/workspace/input.tar.gz",
        local_path=destination,
        expected_sha256=digest,
    )

    assert uploaded == downloaded
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_upload_rejects_local_or_remote_digest_mismatch(tmp_path: Path) -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    source = tmp_path / "snapshot.bin"
    source.write_bytes(b"expected")

    with pytest.raises(SandboxError) as local_error:
        await backend.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/input.bin",
            expected_sha256="0" * 64,
        )
    assert local_error.value.code == "transfer_failed"
    assert driver.session.files == {}

    driver.session.upload_transform = lambda _: b"corrupted"
    with pytest.raises(SandboxError) as remote_error:
        await backend.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/input.bin",
            expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        )
    assert remote_error.value.code == "transfer_failed"


@pytest.mark.asyncio
async def test_download_digest_failure_preserves_existing_destination(tmp_path: Path) -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.session.files["/workspace/artifact"] = b"new bytes"
    destination = tmp_path / "artifact"
    destination.write_bytes(b"existing bytes")

    with pytest.raises(SandboxError) as exc_info:
        await backend.download_file(
            handle,
            remote_path="/workspace/artifact",
            local_path=destination,
            expected_sha256="0" * 64,
        )

    assert exc_info.value.code == "transfer_failed"
    assert destination.read_bytes() == b"existing bytes"
    assert await asyncio.to_thread(
        lambda: list(tmp_path.glob(".artifact.*.tmp"))
    ) == []


@pytest.mark.asyncio
async def test_execute_shell_quotes_argv_and_streams_bounded_ordered_output() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.session.queued_commands.append(
        (_Result(2), None, (("stdout", "ab"), ("stderr", "你好")), None)
    )
    chunks: list[SandboxOutputChunk] = []

    async def on_output(chunk: SandboxOutputChunk) -> None:
        chunks.append(chunk)

    command = SandboxCommand(
        command_id="cmd-quoted",
        argv=("python", "-c", "print('$HOME; rm -rf /')", "a b"),
        max_output_bytes=5,
    )
    result = await backend.execute(handle, command, on_output=on_output)

    assert driver.session.commands[0][0] == shlex.join(command.argv)
    assert result.exit_code == 2
    assert result.stdout == "ab"
    assert result.stderr == "你"
    assert result.output_truncated is True
    assert [(item.sequence, item.stream, item.text) for item in chunks] == [
        (0, "stdout", "ab"),
        (1, "stderr", "你"),
    ]


@pytest.mark.asyncio
async def test_execute_cancellation_kills_remote_command() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    blocker = asyncio.Event()
    driver.session.queued_commands.append((None, None, (), blocker))
    signal = asyncio.Event()

    task = asyncio.create_task(
        backend.execute(
            handle,
            SandboxCommand(command_id="cmd-cancel", argv=("sleep", "60")),
            signal=signal,
        )
    )
    await asyncio.sleep(0)
    signal.set()
    result = await task

    assert result.termination_reason == "cancelled"
    assert result.exit_code is None
    assert driver.session.handles[0].killed is True


@pytest.mark.asyncio
async def test_execute_provider_timeout_kills_command_and_uses_fixed_error() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.session.queued_commands.append((None, TimeoutException("raw"), (), None))

    with pytest.raises(SandboxError) as exc_info:
        await backend.execute(
            handle,
            SandboxCommand(command_id="cmd-timeout", argv=("pytest",)),
        )

    assert exc_info.value.code == "command_timeout"
    assert driver.session.handles[0].killed is True


@pytest.mark.asyncio
async def test_destroy_is_idempotent_but_retains_session_after_transient_failure() -> None:
    driver = _Driver()
    backend = _backend(driver)
    handle = await backend.create(_spec())
    driver.kill_error = ConnectionError("temporary")

    with pytest.raises(SandboxError) as exc_info:
        await backend.destroy(handle)
    assert exc_info.value.code == "provider_unavailable"
    assert "active=1" in repr(backend)

    driver.kill_error = None
    await backend.destroy(handle)
    await backend.destroy(handle)
    assert "active=0" in repr(backend)


@pytest.mark.asyncio
async def test_expired_handle_never_reconnects_or_extends_lifetime() -> None:
    driver = _Driver()
    backend = _backend(driver, clock_ms=lambda: 2_000)
    handle = SandboxHandle(
        provider="e2b",
        sandbox_id="e2b-sandbox-1",
        operation_id="op-e2b",
        runtime_id="template-python",
        created_at_ms=500,
        expires_at_ms=1_000,
    )

    with pytest.raises(SandboxError) as exc_info:
        await backend.attach(handle)

    assert exc_info.value.code == "sandbox_not_found"
    assert driver.connect_calls == []


@pytest.mark.asyncio
async def test_sdk_driver_maps_network_and_secure_lifecycle_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    raw_sandbox = SimpleNamespace(
        sandbox_id="sdk-sandbox",
        files=SimpleNamespace(),
        commands=SimpleNamespace(),
    )

    class RawApi:
        @staticmethod
        async def create(**kwargs: object) -> object:
            calls.append(kwargs)
            return raw_sandbox

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _: SimpleNamespace(AsyncSandbox=RawApi),
    )
    driver = E2BSdkDriver()
    network = SandboxNetworkPolicy(
        mode="allowlist",
        allowed_domains=("pypi.org",),
        allowed_cidrs=("1.1.1.0/24",),
    )

    session = await driver.create(
        template_id="template-python",
        lifetime_seconds=600,
        metadata={"operation": "safe"},
        network=network,
        api_key="e2b_test-placeholder",
        request_timeout=30.0,
    )

    assert session.sandbox_id == "sdk-sandbox"
    assert calls[0]["secure"] is True
    assert calls[0]["allow_internet_access"] is True
    assert calls[0]["network"] == {
        "allow_public_traffic": False,
        "allow_out": ["pypi.org", "1.1.1.0/24"],
    }
    assert calls[0]["lifecycle"] == {
        "on_timeout": "kill",
        "auto_resume": False,
    }


@pytest.mark.asyncio
async def test_sdk_driver_fails_closed_when_optional_sdk_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> object:
        raise ModuleNotFoundError("e2b")

    monkeypatch.setattr(importlib, "import_module", missing)

    with pytest.raises(SandboxError) as exc_info:
        await E2BSdkDriver().create(
            template_id="template-python",
            lifetime_seconds=600,
            metadata={},
            network=SandboxNetworkPolicy(),
            api_key="e2b_test-placeholder",
            request_timeout=30.0,
        )

    assert exc_info.value.code == "invalid_configuration"
    assert "e2b" not in str(exc_info.value).lower()

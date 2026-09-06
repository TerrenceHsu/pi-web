"""Fail-closed local Docker backend; not automatically installed or registered.

All file transfers use the image-owned helper while the container is quiescent.
Docker cp does not reliably export tmpfs. A frozen container permits only this
fixed helper, never further user commands, and returns to paused state afterwards.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bash import BASH_ARGV, script_bytes
from .docker_transport import (
    DockerOutput,
    DockerReply,
    DockerStream,
    DockerTransport,
    require_plain_path,
)
from .errors import SandboxError
from .models import (
    SandboxBackendName,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxLimits,
    SandboxOutputCallback,
    SandboxOutputChunk,
    SandboxStatus,
    SandboxTerminationReason,
    SandboxTransferReceipt,
    validate_sandbox_path,
)

_LABEL = "io.pi-agent.bash-runtime"
_NAMESPACE = "io.pi-agent.namespace"
_OPERATION = "io.pi-agent.operation"
_HELPER = ("python3", "-I", "-S", "-B", "/opt/pi-agent-runtime/worker.py")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")


class LocalDockerExecutionConfig(BaseModel):
    """Independent of E2B configuration and credentials. Deployment-owned only."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    image_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    namespace: str = Field(default="pi-local", pattern=r"^[a-z0-9-]{1,32}$")
    limits: SandboxLimits = Field(
        default_factory=lambda: SandboxLimits(
            cpu=1,
            memory_mb=1024,
            lifetime_seconds=1800,
            command_timeout_seconds=300,
            max_upload_bytes=100 * 1024 * 1024,
            max_file_bytes=25 * 1024 * 1024,
            max_file_count=5000,
            max_output_bytes=1024 * 1024,
        )
    )

    @model_validator(mode="after")
    def _validate(self) -> LocalDockerExecutionConfig:
        if self.enabled and self.image_id is None:
            raise ValueError("an enabled local runtime requires an immutable image ID")
        ceiling = SandboxLimits(
            cpu=1,
            memory_mb=1024,
            lifetime_seconds=1800,
            command_timeout_seconds=300,
            max_upload_bytes=100 * 1024 * 1024,
            max_file_bytes=25 * 1024 * 1024,
            max_file_count=5000,
            max_output_bytes=1024 * 1024,
        )
        if any(value > getattr(ceiling, name) for name, value in self.limits.model_dump().items()):
            raise ValueError("local limits exceed the fixed first-release ceiling")
        return self


class LocalDockerCapability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    available: bool
    configured: bool
    environment_ready: bool = False
    provider: str = "local_docker"
    error_code: str | None = None
    # A read-only daemon/image probe is NOT proof that isolation and reuse work.
    execution_verified: bool = False
    reuse_verified: bool = False


class LocalDockerSandboxBackend:
    def __init__(
        self,
        *,
        transport: DockerTransport,
        config: LocalDockerExecutionConfig,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        self._config = config
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._handles: dict[str, SandboxHandle] = {}
        self._limits: dict[str, SandboxLimits] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._frozen: set[str] = set()
        self._used_commands: set[tuple[str, str]] = set()
        self._cleanup_pending: set[str] = set()

    def backend_name(self) -> SandboxBackendName:
        return "local_docker"

    async def probe(self) -> LocalDockerCapability:
        """No container creation, pulls, execution, or persistent config mutation."""
        if not self._config.enabled:
            return LocalDockerCapability(
                available=False,
                configured=self._config.image_id is not None,
                error_code="bash_disabled",
            )
        try:
            reply = await self._checked(("version", "--format", "{{json .Server}}"))
            server = _object(reply.stdout)
            if server.get("Os") != "linux":
                raise SandboxError("invalid_configuration", provider="local_docker")
            await self._image()
        except (SandboxError, TimeoutError):
            return LocalDockerCapability(
                available=False,
                configured=True,
                error_code="docker_or_image_unavailable",
            )
        return LocalDockerCapability(
            available=False,
            configured=True,
            environment_ready=True,
            error_code="isolation_verification_required",
        )

    async def _image(self) -> None:
        image_id = self._config.image_id
        if image_id is None or _IMAGE.fullmatch(image_id) is None:
            raise SandboxError("invalid_configuration", provider="local_docker")
        reply = await self._checked(("image", "inspect", "--format", "{{json .}}", image_id))
        image = _object(reply.stdout)
        config = _mapping(image.get("Config"))
        if (
            image.get("Id") != image_id
            or image.get("Os") != "linux"
            or _mapping(config.get("Labels")).get(_LABEL) != "1"
            or _mapping(config.get("Labels")).get("io.pi-agent.bash-script") != "1"
            or config.get("Volumes")
            or config.get("Healthcheck")
        ):
            raise SandboxError("invalid_configuration", provider="local_docker")

    def _name(self, operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode()).hexdigest()[:24]
        return f"pi-bash-{self._config.namespace}-{digest}"

    async def create(self, spec: SandboxCreateSpec) -> SandboxHandle:
        if (
            not self._config.enabled
            or self._cleanup_pending
            or spec.runtime_id != self._config.image_id
            or spec.workdir != "/workspace"
            or spec.network.mode != "none"
            or any(
                value > getattr(self._config.limits, key)
                for key, value in spec.limits.model_dump().items()
            )
        ):
            raise SandboxError("invalid_configuration", provider="local_docker")
        await self._image()
        name = self._name(spec.operation_id)
        if name in self._handles:
            raise SandboxError("sandbox_not_ready", provider="local_docker")
        limit = spec.limits
        now = self._clock()
        handle = SandboxHandle(
            provider="local_docker",
            sandbox_id=name,
            operation_id=spec.operation_id,
            runtime_id=spec.runtime_id,
            created_at_ms=now,
            expires_at_ms=now + limit.lifetime_seconds * 1000,
        )
        # Explicit endpoint/config in the transport; never use run's implicit pull.
        argv = (
            "container",
            "create",
            "--pull=never",
            "--name",
            name,
            "--label",
            f"{_LABEL}=1",
            "--label",
            f"{_NAMESPACE}={self._config.namespace}",
            "--label",
            f"{_OPERATION}={spec.operation_id}",
            "--network=none",
            "--ipc=none",
            "--cgroupns=private",
            "--runtime=runc",
            "--read-only",
            "--user=10000:10000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            f"--cpus={limit.cpu}",
            f"--memory={limit.memory_mb}m",
            f"--memory-swap={limit.memory_mb}m",
            "--log-driver=none",
            "--restart=no",
            "--ulimit=nofile=256:256",
            "--ulimit=core=0:0",
            "--tmpfs",
            "/workspace:rw,exec,nosuid,nodev,size=268435456,mode=0700,uid=10001,gid=10001",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=0700,uid=10001,gid=10001",
            "--tmpfs",
            "/run/pi-command:rw,noexec,nosuid,nodev,size=1048576,mode=0755,uid=10000,gid=10000",
            "--workdir=/workspace",
            "--env=PATH=/usr/local/bin:/usr/bin:/bin",
            "--env=LANG=C.UTF-8",
            "--entrypoint=python3",
            spec.runtime_id,
            "-I",
            "-S",
            "-B",
            "/opt/pi-agent-runtime/worker.py",
            "idle",
            str(limit.lifetime_seconds),
        )
        # Register the deterministic reference before the provider mutation so a
        # lost response can be reconciled without creating a second instance.
        self._handles[name], self._limits[name], self._locks[name] = handle, limit, asyncio.Lock()
        try:
            await self._checked(argv)
            await self._checked(("container", "start", name))
            await self._quiesce(handle)
        except BaseException:
            await asyncio.shield(self._cleanup(handle))
            raise
        return handle

    async def attach(self, handle: SandboxHandle) -> SandboxHandle:
        # Recovery may inspect/destroy a reference, never resume old user work.
        self._validate(handle, require_known=True)
        await self._inspect(handle)
        return handle

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        info = await self._inspect(handle)
        state = info["State"]
        return SandboxStatus(
            handle=handle,
            observed_at_ms=self._clock(),
            state="paused"
            if state.get("Paused")
            else ("ready" if state.get("Running") else "terminated"),
        )

    def _validate(self, handle: SandboxHandle, *, require_known: bool = False) -> None:
        if (
            handle.provider != "local_docker"
            or handle.runtime_id != self._config.image_id
            or handle.sandbox_id != self._name(handle.operation_id)
            or require_known
            and self._handles.get(handle.sandbox_id) != handle
        ):
            raise SandboxError("sandbox_not_found", provider="local_docker")

    async def _inspect(self, handle: SandboxHandle) -> dict[str, Any]:
        self._validate(handle)
        reply = await self._transport.call(
            ("container", "inspect", "--format", "{{json .}}", handle.sandbox_id),
        )
        if reply.returncode != 0:
            # Do not interpret an arbitrary daemon/permission error as "deleted".
            raise SandboxError("provider_unavailable", provider="local_docker")
        if reply.truncated:
            raise SandboxError("protocol_error", provider="local_docker")
        value = _object(reply.stdout)
        labels = _mapping(_mapping(value.get("Config")).get("Labels"))
        if (
            labels.get(_LABEL) != "1"
            or labels.get(_NAMESPACE) != self._config.namespace
            or labels.get(_OPERATION) != handle.operation_id
            or value.get("Image") != handle.runtime_id
        ):
            raise SandboxError("permission_denied", provider="local_docker")
        return value

    async def _quiesce(self, handle: SandboxHandle) -> None:
        info = await self._inspect(handle)
        _check_isolation(info, self._limits.get(handle.sandbox_id, self._config.limits))
        state = info.get("State", {})
        if not state.get("Running") or state.get("OOMKilled"):
            raise SandboxError("sandbox_not_ready", provider="local_docker")
        if not state.get("Paused"):
            await self._checked(("container", "pause", handle.sandbox_id))
        reply = await self._checked(("container", "top", handle.sandbox_id, "-eo", "pid"))
        try:
            pids = [int(line.strip()) for line in reply.stdout.decode().splitlines()[1:] if line]
        except ValueError:
            raise SandboxError("protocol_error", provider="local_docker") from None
        if pids != [state.get("Pid")]:
            # Only the known image-owned PID 1 may survive a command. Do not
            # trust argv, process names, parent PID, or a process-group kill.
            raise SandboxError("sandbox_not_ready", provider="local_docker")
        # With user processes gone, briefly admit only the fixed health reader.
        # Exec-child OOMs need cgroup evidence, not just Docker State.OOMKilled.
        await self._checked(("container", "unpause", handle.sandbox_id))
        try:
            health = await self._helper(handle, "health", max_bytes=1024)
            if health.returncode or health.truncated:
                raise SandboxError("sandbox_not_ready", provider="local_docker")
            if _object(health.stdout) != {"oom": 0, "oom_kill": 0}:
                raise SandboxError("resource_limit", provider="local_docker")
        finally:
            await self._checked(("container", "pause", handle.sandbox_id))

    async def _resume(self, handle: SandboxHandle) -> None:
        await self._quiesce(handle)
        await self._checked(("container", "unpause", handle.sandbox_id))

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        return await self._execute(handle, command, signal=signal, on_output=on_output)

    async def execute_bash(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        script: str,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        data = script_bytes(script)
        if command.argv != BASH_ARGV:
            raise SandboxError("invalid_configuration", provider="local_docker")
        return await self._execute(handle, command, signal=signal, on_output=on_output, script=data)

    async def _execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
        script: bytes | None = None,
    ) -> SandboxCommandResult:
        self._validate(handle, require_known=True)
        async with self._locks[handle.sandbox_id]:
            limit = self._limits[handle.sandbox_id]
            key = (handle.sandbox_id, command.command_id)
            if (
                handle.sandbox_id in self._frozen
                or key in self._used_commands
                or command.timeout_seconds > limit.command_timeout_seconds
                or command.max_output_bytes > limit.max_output_bytes
                or self._clock() >= (handle.expires_at_ms or 0)
                or not _within(command.cwd, "/workspace")
            ):
                raise SandboxError("sandbox_not_ready", provider="local_docker")
            self._used_commands.add(key)
            started = self._clock()
            decoders = {
                name: codecs.getincrementaldecoder("utf-8")("replace")
                for name in ("stdout", "stderr")
            }
            sequence = 0

            async def emit(stream: DockerStream, data: bytes) -> None:
                nonlocal sequence
                text = decoders[stream].decode(data)
                if on_output is not None and text:
                    await on_output(
                        SandboxOutputChunk(
                            command_id=command.command_id,
                            sequence=sequence,
                            stream=stream,
                            text=text,
                        )
                    )
                    sequence += 1

            job: asyncio.Task[DockerReply] | None = None
            abort: asyncio.Task[bool] | None = None
            reason: SandboxTerminationReason = "exited"
            result = DockerReply(0)
            try:
                if signal is not None and signal.is_set():
                    reason = "cancelled"
                    await self._cleanup(handle)
                else:
                    await self._resume(handle)
                    if script is not None:
                        prepared = await self._helper(
                            handle,
                            "prepare_bash",
                            stdin=json.dumps(
                                {
                                    "script": script.decode("utf-8"),
                                    "sha256": hashlib.sha256(script).hexdigest(),
                                },
                                ensure_ascii=False,
                            ).encode(),
                            max_bytes=1024,
                            timeout_seconds=min(
                                30,
                                command.timeout_seconds,
                                max(0.001, ((handle.expires_at_ms or 0) - self._clock()) / 1000),
                            ),
                        )
                        if (
                            prepared.returncode
                            or prepared.truncated
                            or _object(prepared.stdout)
                            != {"sha256": hashlib.sha256(script).hexdigest()}
                        ):
                            raise SandboxError("transfer_failed", provider="local_docker")
                    # Cancellation may arrive during the fixed preparation calls.
                    # Recheck before creating a task-code exec, not only afterwards.
                    if signal is not None and signal.is_set():
                        await self._cleanup(handle)
                        return SandboxCommandResult(
                            command_id=command.command_id,
                            termination_reason="cancelled",
                            exit_code=None,
                            started_at_ms=started,
                            finished_at_ms=self._clock(),
                        )
                    if self._clock() >= (handle.expires_at_ms or 0):
                        raise TimeoutError
                    job = asyncio.create_task(
                        self._helper(
                            handle,
                            "run",
                            stdin=json.dumps(
                                {
                                    "argv": command.argv,
                                    "cwd": command.cwd,
                                },
                                ensure_ascii=False,
                            ).encode(),
                            timeout_seconds=min(
                                command.timeout_seconds,
                                max(0.001, ((handle.expires_at_ms or 0) - self._clock()) / 1000),
                            ),
                            max_bytes=command.max_output_bytes,
                            on_output=emit,
                        )
                    )
                    abort = asyncio.create_task(signal.wait()) if signal is not None else None
                    if abort is not None:
                        done, _ = await asyncio.wait(
                            (job, abort),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if abort in done:
                            job.cancel()
                            await asyncio.gather(job, return_exceptions=True)
                            reason = "cancelled"
                            await self._cleanup(handle)
                        else:
                            result = await job
                    else:
                        result = await job
                    if reason == "exited":
                        await self._quiesce(handle)
            except TimeoutError:
                reason = "timed_out"
                await asyncio.shield(self._cleanup(handle))
            except BaseException:
                await asyncio.shield(self._cleanup(handle))
                raise
            finally:
                if job is not None and not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                if abort is not None:
                    abort.cancel()
                    await asyncio.gather(abort, return_exceptions=True)
            return SandboxCommandResult(
                command_id=command.command_id,
                termination_reason=reason,
                exit_code=result.returncode if reason == "exited" else None,
                stdout=result.stdout.decode("utf-8", "replace"),
                stderr=result.stderr.decode("utf-8", "replace"),
                output_truncated=result.truncated,
                started_at_ms=started,
                finished_at_ms=self._clock(),
            )

    async def _helper(
        self,
        handle: SandboxHandle,
        action: str,
        *args: str,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_bytes: int = 1024 * 1024,
        on_output: DockerOutput | None = None,
    ) -> DockerReply:
        return await self._transport.call(
            (
                "container",
                "exec",
                "-i",
                "--user=10000:10000" if action == "prepare_bash" else "--user=10001:10001",
                handle.sandbox_id,
                *_HELPER,
                action,
                *args,
            ),
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            on_output=on_output,
        )

    async def upload_file(
        self,
        handle: SandboxHandle,
        *,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
    ) -> SandboxTransferReceipt:
        self._validate(handle, require_known=True)
        _transfer_path(remote_path)
        require_plain_path(local_path)
        data = await asyncio.to_thread(_read_bounded, local_path, self._limits[handle.sandbox_id])
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise SandboxError("transfer_failed", provider="local_docker")
        async with self._locks[handle.sandbox_id]:
            if handle.sandbox_id in self._frozen:
                raise SandboxError("sandbox_not_ready", provider="local_docker")
            try:
                await self._resume(handle)
                header = (
                    json.dumps(
                        {"path": remote_path, "limit": len(data), "sha256": expected_sha256}
                    ).encode()
                    + b"\n"
                )
                result = await self._helper(handle, "write", stdin=header + data)
                if result.returncode or result.truncated:
                    raise SandboxError("transfer_failed", provider="local_docker")
                receipt = _object(result.stdout)
                if receipt != {"size": len(data), "sha256": expected_sha256}:
                    raise SandboxError("transfer_failed", provider="local_docker")
                await self._quiesce(handle)
            except BaseException:
                await asyncio.shield(self._cleanup(handle))
                raise
        return SandboxTransferReceipt(
            remote_path=remote_path,
            size=len(data),
            sha256=expected_sha256,
        )

    async def download_file(
        self,
        handle: SandboxHandle,
        *,
        remote_path: str,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> SandboxTransferReceipt:
        _transfer_path(remote_path)
        data = await self._collect(handle, "read", remote_path)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise SandboxError("transfer_failed", provider="local_docker")
        await asyncio.to_thread(_write_new, local_path, data)
        return SandboxTransferReceipt(remote_path=remote_path, size=len(data), sha256=digest)

    async def freeze_workspace(self, handle: SandboxHandle, destination: Path) -> str:
        """One-way barrier. Return an untrusted tar for host-side artifact validation."""
        self._validate(handle, require_known=True)
        async with self._locks[handle.sandbox_id]:
            self._frozen.add(handle.sandbox_id)
        data = await self._collect(handle, "collect")
        await asyncio.to_thread(_write_new, destination, data)
        return hashlib.sha256(data).hexdigest()

    async def _collect(self, handle: SandboxHandle, action: str, *args: str) -> bytes:
        self._validate(handle, require_known=True)
        async with self._locks[handle.sandbox_id]:
            try:
                # With all user processes gone, only the image-owned reader is
                # admitted. It cannot execute Workspace source or accept argv.
                await self._resume(handle)
                limit = self._limits[handle.sandbox_id]
                reply = await self._helper(
                    handle,
                    action,
                    *args,
                    max_bytes=limit.max_upload_bytes + 4 * 1024 * 1024,
                )
                if reply.returncode or reply.truncated:
                    raise SandboxError("transfer_failed", provider="local_docker")
                await self._quiesce(handle)
                return reply.stdout
            except BaseException:
                await asyncio.shield(self._cleanup(handle))
                raise

    async def destroy(self, handle: SandboxHandle) -> None:
        self._validate(handle)
        await self._remove(handle)

    async def reconcile_operation(self, operation_id: str) -> None:
        """Destroy only this deployment's exact durable operation; never attach/run."""
        now = self._clock()
        handle = SandboxHandle(
            provider="local_docker",
            sandbox_id=self._name(operation_id),
            operation_id=operation_id,
            runtime_id=self._config.image_id or "",
            created_at_ms=now,
            expires_at_ms=now + 1000,
        )
        await self.destroy(handle)

    async def managed_operations(self) -> tuple[tuple[str, str], ...]:
        """Read only this namespace's labelled containers, including old image versions."""
        reply = await self._checked(
            (
                "container",
                "ls",
                "--all",
                "--filter",
                f"label={_LABEL}=1",
                "--filter",
                f"label={_NAMESPACE}={self._config.namespace}",
                "--format",
                "{{.Names}}",
            )
        )
        names = reply.stdout.decode("utf-8", "strict").splitlines()
        if len(names) > 256:
            raise SandboxError("protocol_error", provider="local_docker")
        found = []
        for name in names:
            if not name.startswith(f"pi-bash-{self._config.namespace}-"):
                raise SandboxError("permission_denied", provider="local_docker")
            info = _object(
                (
                    await self._checked(("container", "inspect", "--format", "{{json .}}", name))
                ).stdout
            )
            labels = _mapping(_mapping(info.get("Config")).get("Labels"))
            operation, runtime = labels.get(_OPERATION), info.get("Image")
            if (
                not isinstance(operation, str)
                or not 1 <= len(operation) <= 128
                or name != self._name(operation)
                or not isinstance(runtime, str)
                or _IMAGE.fullmatch(runtime) is None
                or labels.get(_LABEL) != "1"
                or labels.get(_NAMESPACE) != self._config.namespace
            ):
                raise SandboxError("permission_denied", provider="local_docker")
            found.append((operation, runtime))
        return tuple(found)

    async def _cleanup(self, handle: SandboxHandle) -> None:
        try:
            await self._remove(handle)
        except Exception:
            self._cleanup_pending.add(handle.sandbox_id)

    async def _remove(self, handle: SandboxHandle) -> None:
        # Listing exact names distinguishes absence from an unreachable daemon.
        reply = await self._checked(
            (
                "container",
                "ls",
                "--all",
                "--filter",
                f"name=^/{handle.sandbox_id}$",
                "--format",
                "{{.Names}}",
            )
        )
        names = reply.stdout.decode().splitlines()
        if names:
            if names != [handle.sandbox_id]:
                raise SandboxError("permission_denied", provider="local_docker")
            await self._inspect(handle)
            await self._checked(("container", "rm", "--force", handle.sandbox_id))
        self._handles.pop(handle.sandbox_id, None)
        self._cleanup_pending.discard(handle.sandbox_id)

    async def _checked(self, argv: tuple[str, ...]) -> DockerReply:
        reply = await self._transport.call(argv)
        if reply.returncode or reply.truncated:
            raise SandboxError("provider_unavailable", provider="local_docker")
        return reply

    @property
    def cleanup_pending(self) -> tuple[str, ...]:
        return tuple(sorted(self._cleanup_pending))


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeError):
        pass
    raise SandboxError("protocol_error", provider="local_docker")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SandboxError("protocol_error", provider="local_docker")
    return value


def _check_isolation(info: dict[str, Any], limit: SandboxLimits) -> None:
    """Verify observed configuration; requesting secure flags is not enough."""
    config, host = info.get("Config"), info.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict):
        raise SandboxError("protocol_error", provider="local_docker")
    tmpfs = host.get("Tmpfs")
    if (
        config.get("User") != "10000:10000"
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("CapDrop") != ["ALL"]
        or host.get("CapAdd")
        or host.get("Binds")
        or host.get("Mounts")
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("PortBindings")
        or host.get("PidMode")
        or host.get("IpcMode") != "none"
        or host.get("CgroupnsMode") != "private"
        or host.get("Runtime") != "runc"
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or host.get("PidsLimit") != 64
        or host.get("NanoCpus") != int(limit.cpu * 1_000_000_000)
        or host.get("Memory") != limit.memory_mb * 1024 * 1024
        or host.get("MemorySwap") != host.get("Memory")
        or not isinstance(tmpfs, dict)
        or set(tmpfs) != {"/workspace", "/tmp", "/run/pi-command"}
        or _mapping(host.get("LogConfig")).get("Type") != "none"
        or _mapping(host.get("RestartPolicy")).get("Name") != "no"
    ):
        raise SandboxError("invalid_configuration", provider="local_docker")
    for root, maximum in (("/workspace", 268435456), ("/tmp", 67108864)):
        value = tmpfs[root]
        if not isinstance(value, str) or set(value.split(",")) != {
            "rw",
            "exec" if root == "/workspace" else "noexec",
            "nosuid",
            "nodev",
            f"size={maximum}",
            "mode=0700",
            "uid=10001",
            "gid=10001",
        }:
            raise SandboxError("invalid_configuration", provider="local_docker")
    if tmpfs["/run/pi-command"] != (
        "rw,noexec,nosuid,nodev,size=1048576,mode=0755,uid=10000,gid=10000"
    ):
        raise SandboxError("invalid_configuration", provider="local_docker")


def _within(value: str, root: str) -> bool:
    try:
        validate_sandbox_path(value)
        return PurePosixPath(value).is_relative_to(root)
    except ValueError:
        return False


def _transfer_path(value: str) -> None:
    if not _within(value, "/workspace") and not _within(value, "/tmp"):
        raise SandboxError("permission_denied", provider="local_docker")


def _read_bounded(path: Path, limits: SandboxLimits) -> bytes:
    before = path.stat()
    maximum = min(limits.max_upload_bytes, limits.max_file_bytes)
    if before.st_nlink != 1 or before.st_size > maximum:
        raise SandboxError("resource_limit", provider="local_docker")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    after = path.stat()
    if len(data) > maximum or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SandboxError("transfer_failed", provider="local_docker")
    return data


def _write_new(path: Path, value: bytes) -> None:
    require_plain_path(path.parent, directory=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(value)


__all__ = ["LocalDockerExecutionConfig", "LocalDockerCapability", "LocalDockerSandboxBackend"]

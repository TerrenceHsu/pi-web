"""Offline backend contracts. These tests do not prove real Docker isolation."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coding_sandbox.admin.local_store import SQLiteLocalDockerConfigStore
from coding_sandbox.admin.store import SandboxConfigConflictError, SQLiteSandboxConfigStore
from coding_sandbox.backend import SandboxBackend
from coding_sandbox.docker_transport import DockerCLITransport, DockerOutput, DockerReply
from coding_sandbox.errors import SandboxError
from coding_sandbox.local_docker import (
    LocalDockerExecutionConfig,
    LocalDockerSandboxBackend,
)
from coding_sandbox.models import SandboxCommand, SandboxCreateSpec, SandboxLimits

IMAGE = "sha256:" + "a" * 64


class MemoryDocker:
    """Only models Docker protocol; never launches a process or executes a script."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.info: dict[str, Any] = {}
        self.files: dict[str, bytes] = {}
        self.exists = False
        self.extra_process = False
        self.leave_child = False
        self.command_reply = DockerReply(0, b"ok\n")
        self.command_replies: list[DockerReply] = []
        self.run_requests: list[dict[str, Any]] = []
        self.cancel_on_prepare: asyncio.Event | None = None
        self.command_timeout = False
        self.command_wait: asyncio.Event | None = None
        self.command_started = asyncio.Event()
        self.unreachable = False
        self.oom = False
        self.image: dict[str, Any] = {
            "Id": IMAGE,
            "Os": "linux",
            "Config": {"Labels": {"io.pi-agent.bash-runtime": "1", "io.pi-agent.bash-script": "1"}},
        }

    async def call(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes = b"",
        timeout_seconds: float = 30,
        max_bytes: int = 1024 * 1024,
        on_output: DockerOutput | None = None,
    ) -> DockerReply:
        self.calls.append(argv)
        if self.unreachable:
            raise SandboxError("provider_unavailable", provider="local_docker")
        if argv[0] == "version":
            return DockerReply(0, b'{"Os":"linux"}')
        if argv[:2] == ("image", "inspect"):
            return DockerReply(0, json.dumps(self.image).encode())
        action = argv[1]
        if action == "create":
            self.exists = True
            labels = {
                argv[index + 1].split("=", 1)[0]: argv[index + 1].split("=", 1)[1]
                for index, value in enumerate(argv)
                if value == "--label"
            }
            tmpfs = dict(
                argv[index + 1].split(":", 1)
                for index, value in enumerate(argv)
                if value == "--tmpfs"
            )
            self.info = {
                "Name": argv[argv.index("--name") + 1],
                "Image": IMAGE,
                "Config": {"Labels": labels, "User": "10000:10000"},
                "State": {"Running": False, "Paused": False, "Pid": 12345, "OOMKilled": False},
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "CapDrop": ["ALL"],
                    "CapAdd": None,
                    "Binds": None,
                    "Mounts": None,
                    "PidMode": "",
                    "IpcMode": "none",
                    "CgroupnsMode": "private",
                    "Runtime": "runc",
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 64,
                    "NanoCpus": 1_000_000_000,
                    "Memory": 1024 * 1024 * 1024,
                    "MemorySwap": 1024 * 1024 * 1024,
                    "Tmpfs": tmpfs,
                    "LogConfig": {"Type": "none"},
                    "RestartPolicy": {"Name": "no"},
                },
            }
        elif action == "start":
            self.info["State"]["Running"] = True
        elif action == "pause":
            self.info["State"]["Paused"] = True
        elif action == "unpause":
            self.info["State"]["Paused"] = False
        elif action == "inspect":
            return DockerReply(0, json.dumps(self.info).encode())
        elif action == "top":
            return DockerReply(0, b"PID\n12345\n" + (b"67890\n" if self.extra_process else b""))
        elif action == "ls":
            return DockerReply(0, (self.info["Name"] + "\n").encode() if self.exists else b"")
        elif action == "rm":
            self.exists = False
        elif action == "exec":
            assert self.info["State"]["Paused"] is False
            helper = argv.index("/opt/pi-agent-runtime/worker.py")
            operation = argv[helper + 1]
            if operation == "prepare_bash":
                assert "--user=10000:10000" in argv
                if self.cancel_on_prepare is not None:
                    self.cancel_on_prepare.set()
                request = json.loads(stdin)
                self.files["/run/pi-command/command.sh"] = request["script"].encode()
                return DockerReply(0, json.dumps({"sha256": request["sha256"]}).encode())
            if operation == "health":
                return DockerReply(0, json.dumps({"oom": int(self.oom), "oom_kill": 0}).encode())
            if operation == "run":
                self.run_requests.append(json.loads(stdin))
                self.command_started.set()
                if self.command_wait is not None:
                    await self.command_wait.wait()
                if self.command_timeout:
                    raise TimeoutError
                if on_output is not None:
                    await on_output("stdout", self.command_reply.stdout)
                self.extra_process = self.leave_child
                return self.command_replies.pop(0) if self.command_replies else self.command_reply
            if operation == "write":
                header, payload = stdin.split(b"\n", 1)
                request = json.loads(header)
                self.files[request["path"]] = payload
                return DockerReply(
                    0,
                    json.dumps(
                        {
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ).encode(),
                )
            if operation == "read":
                return DockerReply(0, self.files[argv[helper + 2]])
            if operation == "collect":
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w") as archive:
                    for path, value in self.files.items():
                        if path.startswith("/workspace/"):
                            entry = tarfile.TarInfo(path.removeprefix("/workspace/"))
                            entry.size = len(value)
                            archive.addfile(entry, io.BytesIO(value))
                return DockerReply(0, output.getvalue())
            raise AssertionError(operation)
        else:
            raise AssertionError(argv)
        return DockerReply(0)


def backend() -> tuple[MemoryDocker, LocalDockerSandboxBackend, SandboxCreateSpec]:
    driver = MemoryDocker()
    config = LocalDockerExecutionConfig(enabled=True, image_id=IMAGE)
    service = LocalDockerSandboxBackend(transport=driver, config=config)
    spec = SandboxCreateSpec(operation_id="test-operation", runtime_id=IMAGE, limits=config.limits)
    return driver, service, spec


def command(identifier: str = "command-1", **values: Any) -> SandboxCommand:
    return SandboxCommand(
        command_id=identifier,
        argv=("python3", "scripts/check.py"),
        timeout_seconds=60,
        max_output_bytes=1024,
        **values,
    )


def test_disabled_and_immutable_configuration() -> None:
    assert LocalDockerExecutionConfig().enabled is False
    with pytest.raises(ValidationError):
        LocalDockerExecutionConfig(enabled=True)
    with pytest.raises(ValidationError):
        LocalDockerExecutionConfig(image_id="python:latest")
    with pytest.raises(ValidationError):
        LocalDockerExecutionConfig(limits=SandboxLimits())
    with pytest.raises(ValidationError):
        LocalDockerExecutionConfig.model_validate({"network": "host"})


async def test_probe_does_not_execute_and_never_claims_isolation_verified() -> None:
    driver, service, _ = backend()
    capability = await service.probe()
    assert capability.environment_ready
    assert not capability.available and not capability.execution_verified
    assert not capability.reuse_verified
    assert {args[0] for args in driver.calls} == {"version", "image"}


async def test_disabled_probe_makes_no_daemon_calls() -> None:
    driver = MemoryDocker()
    service = LocalDockerSandboxBackend(transport=driver, config=LocalDockerExecutionConfig())
    assert (await service.probe()).error_code == "bash_disabled"
    assert not driver.calls


async def test_backend_contract_create_secure_spec_and_reuse() -> None:
    driver, service, spec = backend()
    assert isinstance(service, SandboxBackend)
    handle = await service.create(spec)
    args = next(args for args in driver.calls if args[:2] == ("container", "create"))
    for flag in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--user=10000:10000",
        "--log-driver=none",
        "--pids-limit=64",
    ):
        assert flag in args
    assert "--privileged" not in args and "--volume" not in args
    assert (await service.status(handle)).state == "paused"
    assert await service.attach(handle) == handle
    assert (await service.execute(handle, command())).succeeded
    assert (await service.execute(handle, command("command-2"))).succeeded
    assert sum(args[:2] == ("container", "create") for args in driver.calls) == 1
    assert (await service.status(handle)).state == "paused"
    await service.destroy(handle)
    await service.destroy(handle)
    assert not driver.exists


async def test_known_nonzero_exit_can_be_followed_by_repair() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.command_reply = DockerReply(2, stderr=b"test failed")
    result = await service.execute(handle, command())
    assert result.exit_code == 2 and not result.succeeded
    assert driver.exists
    driver.command_reply = DockerReply(0)
    assert (await service.execute(handle, command("repair"))).succeeded


async def test_command_replay_never_executes_twice() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    await service.execute(handle, command())
    before = len(driver.calls)
    with pytest.raises(SandboxError):
        await service.execute(handle, command())
    assert len(driver.calls) == before


@pytest.mark.parametrize("root", ["/tmp", "/etc", "/workspace-other"])
async def test_command_cwd_must_be_in_workspace(root: str) -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    with pytest.raises(SandboxError):
        await service.execute(handle, command(cwd=root))
    assert not driver.command_started.is_set()


@pytest.mark.parametrize(
    "field,value",
    [
        ("NetworkMode", "host"),
        ("Privileged", True),
        ("ReadonlyRootfs", False),
        ("Binds", ["/:/host"]),
        ("CapAdd", ["SYS_ADMIN"]),
        ("PidsLimit", -1),
        ("SecurityOpt", ["seccomp=unconfined"]),
        ("PidMode", "host"),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("NanoCpus", 0),
        ("Devices", [{"PathOnHost": "/dev/sda"}]),
        ("PortBindings", {"80/tcp": []}),
        ("CgroupnsMode", "host"),
        ("IpcMode", "private"),
        ("Runtime", "nvidia"),
        ("LogConfig", "malformed"),
        ("RestartPolicy", "malformed"),
    ],
)
async def test_observed_isolation_drift_destroys_container(field: str, value: Any) -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.info["HostConfig"][field] = value
    with pytest.raises(SandboxError):
        await service.execute(handle, command())
    assert not driver.exists and not driver.command_started.is_set()


async def test_surviving_child_prevents_reuse_and_destroys_container() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.leave_child = True
    with pytest.raises(SandboxError):
        await service.execute(handle, command())
    assert not driver.exists


async def test_exec_child_oom_prevents_reuse_even_when_supervisor_survives() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.oom = True
    assert not driver.info["State"]["OOMKilled"]
    with pytest.raises(SandboxError) as error:
        await service.execute(handle, command())
    assert error.value.code == "resource_limit"
    assert not driver.exists and not driver.command_started.is_set()


async def test_timeout_terminates_real_container_not_only_cli() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.command_timeout = True
    result = await service.execute(handle, command())
    assert result.termination_reason == "timed_out" and result.exit_code is None
    assert not driver.exists
    assert any(args[:3] == ("container", "rm", "--force") for args in driver.calls)


async def test_stop_cancels_execution_and_destroys_container() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    signal = asyncio.Event()
    driver.command_wait = asyncio.Event()
    task = asyncio.create_task(service.execute(handle, command(), signal=signal))
    await asyncio.wait_for(driver.command_started.wait(), 1)
    signal.set()
    result = await asyncio.wait_for(task, 1)
    assert result.termination_reason == "cancelled" and not driver.exists


async def test_preexisting_stop_never_starts_command() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    signal = asyncio.Event()
    signal.set()
    assert (
        await service.execute(handle, command(), signal=signal)
    ).termination_reason == "cancelled"
    assert not driver.command_started.is_set() and not driver.exists


async def test_daemon_loss_marks_cleanup_pending_and_blocks_creation() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.unreachable = True
    with pytest.raises(SandboxError):
        await service.execute(handle, command())
    assert service.cleanup_pending == (handle.sandbox_id,)
    driver.unreachable = False
    with pytest.raises(SandboxError):
        await service.create(spec.model_copy(update={"operation_id": "another"}))
    await service.destroy(handle)
    assert service.cleanup_pending == ()


async def test_forged_handle_cannot_destroy_another_container() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    with pytest.raises(SandboxError):
        await service.destroy(handle.model_copy(update={"operation_id": "someone-else"}))
    driver.info["Config"]["Labels"]["io.pi-agent.namespace"] = "different-deployment"
    with pytest.raises(SandboxError):
        await service.destroy(handle)
    assert driver.exists


async def test_transfers_verify_hash_and_freeze_is_one_way(tmp_path: Path) -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    source = tmp_path / "source.txt"
    source.write_bytes(b"workspace data")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = await service.upload_file(
        handle,
        local_path=source,
        remote_path="/workspace/input.txt",
        expected_sha256=digest,
    )
    assert receipt.sha256 == digest
    target = tmp_path / "download.txt"
    await service.download_file(
        handle,
        remote_path="/workspace/input.txt",
        local_path=target,
        expected_sha256=digest,
    )
    assert target.read_bytes() == source.read_bytes()
    archive = tmp_path / "frozen.tar"
    frozen_hash = await service.freeze_workspace(handle, archive)
    assert frozen_hash == hashlib.sha256(archive.read_bytes()).hexdigest()
    with tarfile.open(archive) as frozen:
        assert frozen.getnames() == ["input.txt"]
    with pytest.raises(SandboxError):
        await service.execute(handle, command())
    with pytest.raises(SandboxError):
        await service.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/new.txt",
            expected_sha256=digest,
        )
    assert not any(args[1:2] == ("cp",) for args in driver.calls)


async def test_transfer_rejects_changed_source_and_unsafe_destination(tmp_path: Path) -> None:
    _, service, spec = backend()
    handle = await service.create(spec)
    source = tmp_path / "source.txt"
    source.write_bytes(b"different")
    with pytest.raises(SandboxError):
        await service.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/test.txt",
            expected_sha256="0" * 64,
        )
    with pytest.raises(SandboxError):
        await service.download_file(handle, remote_path="/etc/passwd", local_path=tmp_path / "bad")


async def test_single_file_limit_is_distinct_from_total_upload_limit(tmp_path: Path) -> None:
    driver, service, spec = backend()
    spec = spec.model_copy(update={"limits": spec.limits.model_copy(update={"max_file_bytes": 4})})
    handle = await service.create(spec)
    source = tmp_path / "oversized.txt"
    source.write_bytes(b"12345")
    with pytest.raises(SandboxError) as error:
        await service.upload_file(
            handle,
            local_path=source,
            remote_path="/workspace/oversized.txt",
            expected_sha256=hashlib.sha256(b"12345").hexdigest(),
        )
    assert error.value.code == "resource_limit"
    assert not driver.files


async def test_restart_does_not_reattach_or_resume_old_execution() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    replacement = LocalDockerSandboxBackend(
        transport=driver,
        config=LocalDockerExecutionConfig(enabled=True, image_id=IMAGE),
    )
    with pytest.raises(SandboxError):
        await replacement.attach(handle)
    await replacement.destroy(handle)
    assert not driver.exists


async def test_local_config_is_revisioned_and_does_not_change_e2b(tmp_path: Path) -> None:
    database = tmp_path / "config.sqlite"
    e2b = await SQLiteSandboxConfigStore.open(database)
    local = await SQLiteLocalDockerConfigStore.open(database)
    try:
        old_e2b = await e2b.get()
        assert (await local.get()).revision == 0
        saved = await local.put(
            LocalDockerExecutionConfig(enabled=True, image_id=IMAGE),
            expected_revision=0,
        )
        assert saved.revision == 1
        assert await e2b.get() == old_e2b
        with pytest.raises(SandboxConfigConflictError):
            await local.put(LocalDockerExecutionConfig(), expected_revision=0)
    finally:
        await local.close()
        await e2b.close()
    reopened = await SQLiteLocalDockerConfigStore.open(database)
    try:
        assert (await reopened.get()).config.image_id == IMAGE
    finally:
        await reopened.close()


def test_cli_transport_rejects_remote_daemon_and_populated_config(tmp_path: Path) -> None:
    executable = tmp_path / "docker.exe"
    executable.write_bytes(b"not executed")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SandboxError):
        DockerCLITransport(
            executable=executable, config_directory=empty, endpoint="tcp://remote:2375"
        )
    (empty / "config.json").write_text("{}")
    with pytest.raises(SandboxError):
        DockerCLITransport(
            executable=executable,
            config_directory=empty,
            endpoint="npipe:////./pipe/docker_engine",
        )


async def test_cli_timeout_is_not_misclassified_as_daemon_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "docker.exe"
    executable.write_bytes(b"not executed")
    config = tmp_path / "client"
    config.mkdir()
    transport = DockerCLITransport(
        executable=executable,
        config_directory=config,
        endpoint=(
            "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
        ),
    )

    async def wait_forever(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", wait_forever)
    with pytest.raises(TimeoutError):
        await transport.call(("version",), timeout_seconds=0.01)


@pytest.mark.parametrize(
    "metadata",
    [
        {"Id": IMAGE, "Os": "windows", "Config": {"Labels": {}}},
        {"Id": IMAGE, "Os": "linux", "Config": {"Labels": "malformed"}},
        {"Id": IMAGE, "Os": "linux", "Config": {"Labels": {}, "Volumes": {"/host": {}}}},
    ],
)
async def test_probe_rejects_unapproved_or_malformed_images(metadata: dict[str, Any]) -> None:
    driver, service, _ = backend()
    driver.image = metadata
    assert not (await service.probe()).environment_ready
    assert not driver.exists

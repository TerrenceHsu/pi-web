"""Explicit local Docker readiness/smoke check. Never installs, pulls or enables.

Without --verify this only inspects the daemon and approved image. Verification
creates disposable, labelled containers and writes only to a temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

import aiosqlite

from coding_agent_app.execution.context import bind_execution_context
from coding_agent_app.execution.models import (
    ExecutionContext,
    ExecutionDenied,
    ExecutionIdentity,
    ExecutionScope,
)
from coding_agent_app.execution.operation import GrantedOperationAccess
from coding_agent_app.execution.service import ExecutionService
from coding_agent_app.execution.store import ExecutionStore
from coding_sandbox.bash import BashRequest
from coding_sandbox.docker_transport import DockerCLITransport
from coding_sandbox.errors import SandboxError
from coding_sandbox.local_docker import LocalDockerExecutionConfig, LocalDockerSandboxBackend
from coding_sandbox.models import (
    SandboxCommand,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxOutputChunk,
)
from coding_sandbox.operation import SandboxOperation
from coding_sandbox.validation import parse_sandbox_validation_config


async def granted_operation_checks(
    backend: LocalDockerSandboxBackend,
    transport: DockerCLITransport,
    spec: SandboxCreateSpec,
    root: Path,
) -> list[str]:
    """Test trusted server composition, not a browser approval or product enablement."""
    identity = ExecutionIdentity(
        account_id="probe",
        session_id="probe",
        request_id=uuid4().hex,
        workspace_id="probe",
        task_id=uuid4().hex,
        operation_id=f"probe-grant-{uuid4().hex}",
    )
    digest = hashlib.sha256(b"probe-only-no-user-workspace").hexdigest()
    scope = ExecutionScope(
        identity=identity,
        kind="coding",
        backend="local_docker",
        runtime_id=spec.runtime_id,
        config_revision=0,
        policy_sha256=digest,
        baseline_revision=0,
        baseline_sha256=digest,
        input_sha256=digest,
        publish_policy_sha256=digest,
        request_sha256=digest,
        capabilities=("edit", "execute", "validate"),
    )
    context = ExecutionContext(identity=identity, scope_sha256=scope.sha256, role="executor")
    config = (
        b'version=1\n[[required_checks]]\nid="check"\n'
        b'argv=["python3","-I","-S","-B","scripts/check.py"]\ntimeout_seconds=10\n'
    )
    results: list[str] = []
    async with aiosqlite.connect(root / "probe-grants.db", isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        await store.prepare(scope)
        await store.approve(identity, expected_scope_sha256=scope.sha256)
        await store.activate(context, current_scope=scope)
        handle = await backend.create(
            spec.model_copy(update={"operation_id": identity.operation_id})
        )

        async def check_scope(saved: ExecutionScope) -> bool:
            return saved == scope

        async def cleanup(saved: ExecutionIdentity) -> bool:
            if saved != identity:
                return False
            await backend.destroy(handle)
            await require_absent(transport, handle)
            return True

        service = ExecutionService(store, scope_check=check_scope, cleanup=cleanup)
        operation = SandboxOperation(
            backend=backend,
            handle=handle,
            workdir=spec.workdir,
            limits=spec.limits,
            staging_root=root / "granted-stage",
            validation_plan=parse_sandbox_validation_config(config),
            execution_guard=GrantedOperationAccess(service, scope),
        )
        try:
            await store.runtime_ready(context)
            try:
                await operation.run_bash(BashRequest(script="touch unauthorized"))
            except ExecutionDenied as exc:
                if exc.code != "grant_required":
                    raise
            else:
                raise SandboxError("permission_denied", provider="local_docker")
            results.append("operation_requires_server_execution_context")
            with bind_execution_context(context):
                await operation.write_file("input.txt", "中文 $HOME\n")
                request = BashRequest(
                    script=(
                        "mkdir -p scripts\n"
                        "cat input.txt | sed 's/HOME/USER/' > scripts/output.txt\n"
                        "cat scripts/output.txt\n"
                    ),
                    timeout_seconds=10,
                )
                result = await operation.run_bash(request)
                if not result.succeeded or result.stdout != "中文 $USER\n":
                    raise SandboxError("protocol_error", provider="local_docker")
                read = await operation.read_file("scripts/output.txt")
                if read.content != result.stdout:
                    raise SandboxError("protocol_error", provider="local_docker")
                result = await operation.run(
                    (
                        "python3",
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        "from pathlib import Path;"
                        "assert Path('scripts/output.txt').read_text()=='中文 $USER\\n'",
                    ),
                    timeout_seconds=10,
                )
                if not result.succeeded:
                    raise SandboxError("protocol_error", provider="local_docker")
                results.append("grant_bash_argv_edits_share_task_copy")
                # A task cannot change/unlink the script or poison the next shell.
                protection = """python3 -I -S -B - <<'PY'
import os, pathlib, subprocess
p = pathlib.Path('/run/pi-command/command.sh')
assert p.stat().st_uid == 10000 and p.stat().st_mode & 0o777 == 0o444
for change in (lambda: p.write_text('exit 99'), p.unlink):
    try:
        change()
    except PermissionError:
        pass
    else:
        raise AssertionError('script control writable')
attempt = subprocess.run(
    ['python3','-I','-S','-B','/opt/pi-agent-runtime/worker.py','prepare_bash'],
    input=b'{}', capture_output=True)
assert attempt.returncode != 0
PY
printf 'exit 99\\n' > /tmp/poison.sh
export BASH_ENV=/tmp/poison.sh
"""
                result = await operation.run_bash(
                    BashRequest(script=protection, timeout_seconds=10)
                )
                if not result.succeeded:
                    raise SandboxError("permission_denied", provider="local_docker")
                result = await operation.run_bash(
                    BashRequest(script='test -z "${BASH_ENV+x}"\nprintf clean', timeout_seconds=10)
                )
                if not result.succeeded or result.stdout != "clean":
                    raise SandboxError("permission_denied", provider="local_docker")
                results.append("protected_script_identity_and_clean_shell_environment")
                await operation.write_file(
                    "json.py", "raise RuntimeError('workspace import must not execute')\n"
                )
                await operation.write_file(
                    "sitecustomize.py", "raise RuntimeError('site must not execute')\n"
                )
                if not (await operation.list_files()).files:
                    raise SandboxError("protocol_error", provider="local_docker")
                results.append("fixed_helpers_ignore_workspace_python_modules")
                await operation.write_file(".pi-agent/sandbox.toml", config.decode())
                await operation.write_file(
                    "scripts/check.py",
                    "from pathlib import Path\n"
                    "assert Path('scripts/output.txt').read_text() == '中文 $USER\\n'\n",
                )
                evidence = await operation.validate_required_checks()
                if not evidence.passed:
                    raise SandboxError("protocol_error", provider="local_docker")
                grant = await store.get(identity)
                if grant.commands_used != 10 or grant.copy_revision != 10:
                    raise SandboxError("protocol_error", provider="local_docker")
                results.append("fixed_validation_and_edits_charge_same_grant")
            with bind_execution_context(context.model_copy(update={"role": "verifier"})):
                await operation.list_files()
                try:
                    await operation.run(("true",), timeout_seconds=1)
                except ExecutionDenied as exc:
                    if exc.code != "execution_role_denied":
                        raise
                else:
                    raise SandboxError("permission_denied", provider="local_docker")
            await service.revoke(identity)
            with bind_execution_context(context):
                try:
                    await operation.run_bash(BashRequest(script="true", timeout_seconds=1))
                except ExecutionDenied as exc:
                    if exc.code != "grant_revoked":
                        raise
                else:
                    raise SandboxError("permission_denied", provider="local_docker")
            results.append("readonly_role_and_revocation_block_execution")
        finally:
            await backend.destroy(handle)
        await require_absent(transport, handle)
    return results


def python_command(identifier: str, source: str, *, timeout: int = 10) -> SandboxCommand:
    return SandboxCommand(
        command_id=identifier,
        argv=("python3", "-I", "-S", "-B", "-c", source),
        timeout_seconds=timeout,
        max_output_bytes=1024,
    )


async def require_absent(transport: DockerCLITransport, handle: SandboxHandle) -> None:
    reply = await transport.call(
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
    if reply.returncode or reply.stdout.strip() or reply.truncated:
        raise SandboxError("sandbox_not_ready", provider="local_docker")


async def extended_checks(
    backend: LocalDockerSandboxBackend,
    transport: DockerCLITransport,
    spec: SandboxCreateSpec,
    root: Path,
) -> list[str]:
    results: list[str] = []

    def fresh(name: str) -> SandboxCreateSpec:
        return spec.model_copy(update={"operation_id": f"probe-{name}-{uuid4().hex}"})

    handle = await backend.create(fresh("limits"))
    try:
        result = await backend.execute(
            handle,
            python_command(
                "resource-policy",
                """
import errno, os, pathlib, signal
p = pathlib.Path
status = dict(line.split(':', 1) for line in p('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'].strip(), 16) == 0
assert int(status['NoNewPrivs']) == 1 and int(status['Seccomp']) == 2
assert p('/sys/fs/cgroup/memory.max').read_text().strip() == '1073741824'
assert p('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
assert p('/sys/fs/cgroup/pids.max').read_text().strip() == '64'
quota, period = p('/sys/fs/cgroup/cpu.max').read_text().split()
assert int(quota) == int(period)
assert not p('/dev/shm').exists()
for path in ('/etc/pi-bash-test', '/run/pi-command/test'):
    try:
        p(path).write_text('must not write')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES)
    else:
        raise AssertionError('root writable')
try:
    os.kill(1, signal.SIGTERM)
except PermissionError:
    pass
else:
    raise AssertionError('supervisor signal permitted')
for path, cap in (('/workspace', 268435456), ('/tmp', 67108864)):
    usage = os.statvfs(path)
    assert usage.f_frsize * usage.f_blocks == cap
# Real ENOSPC is bounded to the 64 MiB temporary mount, then reclaimed.
try:
    with open('/tmp/fill', 'wb') as stream:
        for _ in range(65):
            stream.write(b'x' * 1048576)
except OSError as exc:
    assert exc.errno == errno.ENOSPC
else:
    raise AssertionError('tmpfs quota missing')
p('/tmp/fill').unlink()
print('resource-policy-ok')
""",
            ),
        )
        if not result.succeeded:
            raise SandboxError("invalid_configuration", provider="local_docker")
        results.append("kernel_resource_policy_and_tmpfs_enospc")
        result = await backend.execute(
            handle, python_command("known-failure", "raise SystemExit(3)")
        )
        if result.exit_code != 3:
            raise SandboxError("protocol_error", provider="local_docker")
        result = await backend.execute(handle, python_command("repair", "print('repaired')"))
        if not result.succeeded:
            raise SandboxError("protocol_error", provider="local_docker")
        results.append("known_failure_then_new_command_reuse")
        chunks: list[SandboxOutputChunk] = []

        async def output(chunk: SandboxOutputChunk) -> None:
            chunks.append(chunk)

        result = await backend.execute(
            handle,
            python_command("output-cap", "import os;os.write(1,b'x'*65536);os.write(2,b'y'*65536)"),
            on_output=output,
        )
        if (
            not result.succeeded
            or not result.output_truncated
            or len(result.stdout.encode()) + len(result.stderr.encode()) > 1024
            or sum(len(chunk.text.encode()) for chunk in chunks) > 1024
        ):
            raise SandboxError("protocol_error", provider="local_docker")
        results.append("combined_stream_and_result_output_limit")
    finally:
        await backend.destroy(handle)
    await require_absent(transport, handle)

    for mode in ("timeout", "cancel", "detached", "oom", "symlink", "hardlink", "fifo"):
        item = fresh(mode)
        if mode == "oom":
            item = item.model_copy(
                update={"limits": spec.limits.model_copy(update={"memory_mb": 128})}
            )
        handle = await backend.create(item)
        try:
            if mode in {"timeout", "cancel"}:
                signal = asyncio.Event()

                async def cancel_on_output(
                    chunk: SandboxOutputChunk,
                    event: asyncio.Event = signal,
                    should_cancel: bool = mode == "cancel",
                ) -> None:
                    if should_cancel:
                        event.set()

                result = await backend.execute(
                    handle,
                    python_command(
                        mode,
                        "import time;print('started',flush=True);time.sleep(60)",
                        timeout=2 if mode == "timeout" else 10,
                    ),
                    signal=signal,
                    on_output=cancel_on_output,
                )
                if result.termination_reason != ("timed_out" if mode == "timeout" else "cancelled"):
                    raise SandboxError("protocol_error", provider="local_docker")
            elif mode in {"detached", "oom"}:
                source = (
                    "import subprocess;subprocess.Popen(['sleep','60'],start_new_session=True,"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
                    if mode == "detached"
                    else "data=bytearray(256*1024*1024)"
                )
                try:
                    await backend.execute(handle, python_command(mode, source))
                except SandboxError as exc:
                    if exc.code not in {"sandbox_not_ready", "resource_limit"}:
                        raise
                else:
                    raise SandboxError("permission_denied", provider="local_docker")
            else:
                source = {
                    "symlink": "import os;os.symlink('/etc/passwd','escape')",
                    "hardlink": "import os;open('first','w').close();os.link('first','second')",
                    "fifo": "import os;os.mkfifo('pipe')",
                }[mode]
                if not (await backend.execute(handle, python_command(mode, source))).succeeded:
                    raise SandboxError("protocol_error", provider="local_docker")
                try:
                    await backend.freeze_workspace(handle, root / f"{mode}.tar")
                except SandboxError as exc:
                    if exc.code != "transfer_failed":
                        raise
                else:
                    raise SandboxError("permission_denied", provider="local_docker")
            # Check cleanup BEFORE the finally block, so cleanup bugs cannot pass.
            await require_absent(transport, handle)
            if backend.cleanup_pending:
                raise SandboxError("provider_unavailable", provider="local_docker")
            results.append(f"{mode}_rejected_and_destroyed")
        finally:
            await backend.destroy(handle)
    return results


async def check(executable: Path, image_id: str, *, verify: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="pi-bash-check-") as directory:
        root = Path(directory)
        client_config = root / "client"
        client_config.mkdir(mode=0o700)
        transport = DockerCLITransport(
            executable=executable,
            config_directory=client_config,
            endpoint=(
                "npipe:////./pipe/docker_engine"
                if os.name == "nt"
                else "unix:///var/run/docker.sock"
            ),
        )
        config = LocalDockerExecutionConfig(enabled=True, image_id=image_id)
        backend = LocalDockerSandboxBackend(transport=transport, config=config)
        capability = await backend.probe()
        if not capability.environment_ready or not verify:
            return capability.model_dump()
        results: list[str] = []
        spec = SandboxCreateSpec(
            operation_id=f"probe-{uuid4().hex}",
            runtime_id=image_id,
            limits=config.limits,
        )
        handle = await backend.create(spec)
        try:
            result = await backend.execute(
                handle,
                SandboxCommand(
                    command_id="isolation",
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        "import os,socket,pathlib;assert os.geteuid()==10001;"
                        "assert not pathlib.Path('/var/run/docker.sock').exists();"
                        "assert set(os.environ)<= {'PATH','LANG','LC_CTYPE'};"
                        "s=socket.socket();s.settimeout(1);"
                        "assert s.connect_ex(('192.0.2.1',9))!=0;s.close();print('isolated')",
                    ),
                    timeout_seconds=10,
                    max_output_bytes=1024,
                ),
            )
            if not result.succeeded:
                raise SandboxError("invalid_configuration", provider="local_docker")
            results.append("isolated_user_network_environment")
            source = root / "input.txt"
            source.write_bytes(b"pi-bash-roundtrip\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            await backend.upload_file(
                handle,
                local_path=source,
                remote_path="/workspace/input.txt",
                expected_sha256=digest,
            )
            result = await backend.execute(
                handle,
                SandboxCommand(
                    command_id="bash-copy",
                    argv=(
                        "/bin/bash",
                        "--noprofile",
                        "--norc",
                        "-e",
                        "-o",
                        "pipefail",
                        "-c",
                        "mkdir -p scripts; cp input.txt scripts/output.txt; cat scripts/output.txt",
                    ),
                    timeout_seconds=10,
                    max_output_bytes=1024,
                ),
            )
            if not result.succeeded or result.stdout != "pi-bash-roundtrip\n":
                raise SandboxError("protocol_error", provider="local_docker")
            downloaded = root / "download.txt"
            await backend.download_file(
                handle,
                remote_path="/workspace/scripts/output.txt",
                local_path=downloaded,
                expected_sha256=digest,
            )
            results.append("tmpfs_transfer_and_task_reuse")
            frozen = root / "frozen.tar"
            await backend.freeze_workspace(handle, frozen)
            with tarfile.open(frozen) as archive:
                if sorted(archive.getnames()) != ["input.txt", "scripts/output.txt"]:
                    raise SandboxError("protocol_error", provider="local_docker")
            try:
                await backend.execute(
                    handle,
                    SandboxCommand(
                        command_id="must-not-run",
                        argv=("true",),
                        timeout_seconds=1,
                        max_output_bytes=1024,
                    ),
                )
            except SandboxError as exc:
                if exc.code != "sandbox_not_ready":
                    raise
            else:
                raise SandboxError("permission_denied", provider="local_docker")
            results.append("one_way_freeze")
        finally:
            await backend.destroy(handle)
        child_spec = spec.model_copy(update={"operation_id": f"probe-child-{uuid4().hex}"})
        child_handle = await backend.create(child_spec)
        try:
            try:
                await backend.execute(
                    child_handle,
                    SandboxCommand(
                        command_id="background-child",
                        argv=(
                            "/bin/bash",
                            "--noprofile",
                            "--norc",
                            "-c",
                            "sleep 60 </dev/null >/dev/null 2>&1 &",
                        ),
                        timeout_seconds=5,
                        max_output_bytes=1024,
                    ),
                )
            except SandboxError as exc:
                if exc.code != "sandbox_not_ready" or backend.cleanup_pending:
                    raise
            else:
                raise SandboxError("permission_denied", provider="local_docker")
            results.append("background_child_rejected_and_destroyed")
        finally:
            await backend.destroy(child_handle)
        results.extend(await extended_checks(backend, transport, spec, root))
        results.extend(await granted_operation_checks(backend, transport, spec, root))
        return {
            "environment_ready": True,
            "execution_verified": True,
            "reuse_verified": True,
            "checks": results,
            "feature_enabled": False,
            "note": "Backend smoke only; Web grant/publisher gates remain required.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-executable", type=Path)
    parser.add_argument("--image-id")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    located = args.docker_executable or shutil.which("docker")
    if located is None:
        print(json.dumps({"available": False, "error_code": "docker_unavailable"}))
        return 2
    if args.image_id is None:
        print(json.dumps({"available": False, "error_code": "image_id_required"}))
        return 2
    try:
        result = asyncio.run(check(Path(located), args.image_id, verify=args.verify))
    except (SandboxError, ValueError, OSError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, SandboxError) else "runtime_check_failed"
        print(json.dumps({"available": False, "error_code": code}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("execution_verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())

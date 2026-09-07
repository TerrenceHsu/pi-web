"""Stage 4 shared quotas, cleanup, private history and admin security contracts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from coding_agent_app.execution.cache import sweep_cache
from coding_agent_app.execution.control import ExecutionControl, ExecutionQuota
from coding_agent_app.execution.models import ExecutionDenied
from coding_agent_app.execution.service import ExecutionService
from coding_agent_app.execution.store import ExecutionStore
from coding_sandbox.docker_transport import DockerCLITransport
from coding_sandbox.errors import SandboxError
from coding_sandbox.local_docker import LocalDockerExecutionConfig, LocalDockerSandboxBackend
from coding_sandbox.models import SandboxCommand, SandboxCommandResult, SandboxCreateSpec
from pi_agent_core_py.telemetry import InMemoryTelemetryContext
from pi_agent_core_py.web.execution import WebExecutionRuntime
from tests.test_execution_grants import active, intent, scope
from tests.test_local_docker_backend import IMAGE, MemoryDocker
from tests.test_web_bash import make_client, seed, select_bash, start_bash
from tests.test_web_execution import pending, resolve, terminal
from tests.test_web_telemetry import UI_HEADERS, _gateway, _login


def task(index: int, account: str = "account", workspace: str | None = None):
    return scope(
        identity=scope().identity.model_copy(
            update={
                "task_id": f"task-{index}",
                "operation_id": f"op-{index}",
                "account_id": account,
                "workspace_id": workspace or f"workspace-{index}",
            }
        )
    )


CONFIG = LocalDockerExecutionConfig(enabled=True, image_id=IMAGE)


def test_distinct_installations_never_share_default_cleanup_namespace(tmp_path):
    first = ExecutionControl(tmp_path / "one.sqlite")
    second = ExecutionControl(tmp_path / "two.sqlite")
    assert first.namespace("pi-local") != second.namespace("pi-local")
    assert first.namespace("pi-local") == ExecutionControl(tmp_path / "one.sqlite").namespace(
        "pi-local"
    )
    assert len(first.namespace("x" * 32)) <= 32


@pytest.mark.asyncio
async def test_atomic_quotas_across_two_connections_and_accounts(tmp_path):
    a, b = [ExecutionControl(tmp_path / "shared.sqlite") for _ in range(2)]
    await a.init()
    await b.init()
    try:
        results = await asyncio.gather(
            *(
                (a if n % 2 else b).reserve(task(n, f"a{n}"), CONFIG.limits, namespace="pi-local")
                for n in range(12)
            ),
            return_exceptions=True,
        )
        assert sum(r is None for r in results) == 4
        assert all(r is None or isinstance(r, ExecutionDenied) for r in results)
        assert (await a.summary())["active_tasks"] == (await b.summary())["active_tasks"] == 4
    finally:
        await a.close()
        await b.close()


@pytest.mark.asyncio
async def test_same_workspace_lease_account_limit_and_revocation_barrier(tmp_path):
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    try:
        await control.reserve(task(1), CONFIG.limits, namespace="pi-local")
        with pytest.raises(ExecutionDenied, match="workspace_busy"):
            await control.reserve(
                task(2, workspace="workspace-1"), CONFIG.limits, namespace="pi-local"
            )
        await control.reserve(task(2), CONFIG.limits, namespace="pi-local")
        with pytest.raises(ExecutionDenied, match="quota"):
            await control.reserve(task(3), CONFIG.limits, namespace="pi-local")
        await control.revoke("task-1")
        assert not await control.allowed(task(1).identity)
        with pytest.raises(ExecutionDenied, match="cleanup_pending"):
            await control.reserve(task(4, "other"), CONFIG.limits, namespace="pi-local")
        await control.update(task(1).identity, released=True)
        await control.reserve(task(4, "other"), CONFIG.limits, namespace="pi-local")
        with pytest.raises(ExecutionDenied, match="execution_replay_denied"):
            await control.reserve(task(1), CONFIG.limits, namespace="pi-local")
    finally:
        await control.close()


@pytest.mark.asyncio
async def test_resource_quota_and_heartbeat_cannot_revive_expired_lease(tmp_path):
    control = ExecutionControl(tmp_path / "shared.sqlite", quota=ExecutionQuota(cpu=1))
    now = [100_000]
    control.now = lambda: now[0]
    await control.init()
    try:
        await control.reserve(task(1), CONFIG.limits, namespace="pi-local")
        with pytest.raises(ExecutionDenied, match="quota"):
            await control.reserve(task(2, "other"), CONFIG.limits, namespace="pi-local")
        now[0] += 61_000
        await control.update(task(1).identity, heartbeat=True)
        assert not await control.allowed(task(1).identity)
        assert (await control.summary())["cleanup_pending"] == 1
    finally:
        await control.close()


@pytest.mark.asyncio
async def test_lost_create_reply_restart_cleanup_and_daemon_debt(tmp_path):
    driver = MemoryDocker()
    original = LocalDockerSandboxBackend(transport=driver, config=CONFIG)
    value = task(1)
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    await control.reserve(value, CONFIG.limits, namespace=CONFIG.namespace)
    await original.create(
        SandboxCreateSpec(
            operation_id=value.identity.operation_id,
            runtime_id=IMAGE,
            limits=CONFIG.limits,
        )
    )
    await control.revoke(value.identity.task_id)
    await control.close()
    reopened = ExecutionControl(tmp_path / "shared.sqlite")
    await reopened.init()
    reopened.register(
        "account",
        AsyncMock(side_effect=RuntimeError),
        lambda cfg: LocalDockerSandboxBackend(transport=driver, config=cfg),
        CONFIG,
    )
    try:
        driver.unreachable = True
        await reopened.maintain()
        assert (await reopened.summary())["admission_paused"]
        assert driver.exists
        driver.unreachable = False
        await reopened.maintain()
        assert not driver.exists
        assert (await reopened.summary())["active_tasks"] == 0
        assert not (await reopened.summary())["admission_paused"]
        assert sum(c[:2] == ("container", "create") for c in driver.calls) == 1
        assert not any(c[:2] == ("container", "exec") and c[-1] == "run" for c in driver.calls)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_orphan_sweep_preserves_reserved_operation_then_removes_untracked(tmp_path):
    driver = MemoryDocker()
    backend = LocalDockerSandboxBackend(transport=driver, config=CONFIG)
    value = task(1)
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    control.register(
        "account",
        AsyncMock(),
        lambda cfg: LocalDockerSandboxBackend(transport=driver, config=cfg),
        CONFIG,
    )
    try:
        await control.reserve(value, CONFIG.limits, namespace=CONFIG.namespace)
        await backend.create(
            SandboxCreateSpec(
                operation_id=value.identity.operation_id,
                runtime_id=IMAGE,
                limits=CONFIG.limits,
            )
        )
        await control.maintain()
        assert driver.exists
        # Simulate an old orphan with no live execution authority.
        await control.update(value.identity, released=True)
        await control.maintain()
        assert not driver.exists
    finally:
        await control.close()


@pytest.mark.asyncio
async def test_wrong_labels_are_never_deleted():
    driver = MemoryDocker()
    backend = LocalDockerSandboxBackend(transport=driver, config=CONFIG)
    await backend.create(
        SandboxCreateSpec(operation_id="op", runtime_id=IMAGE, limits=CONFIG.limits)
    )
    driver.info["Config"]["Labels"]["io.pi-agent.namespace"] = "another-product"
    with pytest.raises(SandboxError):
        await backend.reconcile_operation("op")
    assert driver.exists
    assert not any(c[:2] == ("container", "rm") for c in driver.calls)


def test_cache_ttl_protects_review_and_never_touches_workspace(tmp_path):
    root = tmp_path / "staging"
    folder = root / "execution" / "files" / "artifacts"
    folder.mkdir(parents=True)
    old, protected, fresh = [folder / name for name in ("old.tar", "review.tar", "new.tar")]
    for path in (old, protected, fresh):
        path.write_bytes(b"artifact")
    for path in (old, protected):
        os.utime(path, (1, 1))
    workspace = root / "workspace"
    workspace.mkdir()
    business = workspace / "old.md"
    business.write_text("KEEP", encoding="utf-8")
    os.utime(business, (1, 1))
    result = sweep_cache(root, {protected.resolve()})
    assert result["removed_files"] == 1
    assert not old.exists() and protected.exists() and fresh.exists()
    assert business.read_text() == "KEEP"


def test_private_commands_budget_and_telemetry_do_not_leak_content(tmp_path):
    test_client, backend = make_client(tmp_path, script="echo PRIVATE_SCRIPT")
    with test_client as client:
        runtime = client.app.state.execution_runtime
        recorder = InMemoryTelemetryContext()
        runtime._telemetry = recorder
        sid = select_bash(client)
        other = client.post("/api/sessions", json={"title": "Other"}).json()["id"]
        rid = start_bash(client, sid)
        card = pending(client, rid)
        prepared = seed(client, rid, backend)
        assert resolve(client, rid, card).status_code == 200
        terminal(client, rid)
        client.portal.call(runtime.maintain)
        path = f"/api/sessions/{sid}/execution-tasks/{prepared.scope.identity.task_id}"
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert "PRIVATE_SCRIPT" in response.text and "中文 output" in response.text
        assert response.json()["commands_used"] == 1
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get(path.replace(sid, other)).status_code == 404
        assert client.post(path.replace(sid, other) + "/revoke").status_code == 404
        assert client.get("/api/admin/local-execution/status").status_code == 403
        spans = recorder.get_spans()
        assert any(s.name == "execution.command" for s in spans)
        assert (
            sum(s.name == "execution.task" and s.attributes["phase"] == "approved" for s in spans)
            == 1
        )
        assert "PRIVATE_SCRIPT" not in repr(spans) and "中文 output" not in repr(spans)
        assert "stdout_bytes" in repr(spans)


def test_admin_auth_origin_and_header_guards(tmp_path):
    with TestClient(_gateway(tmp_path)) as client:
        endpoint = "/api/admin/local-execution/status"
        assert client.get(endpoint, headers=UI_HEADERS).status_code == 401
        _login(client, "alice", "alice-pass")
        assert client.get(endpoint, headers=UI_HEADERS).status_code == 403
        _login(client, "admin", "123456")
        assert client.get(endpoint).status_code == 400
        assert (
            client.post(
                "/api/admin/local-execution/cleanup",
                headers={
                    **UI_HEADERS,
                    "Origin": "https://evil.example",
                },
            ).status_code
            == 403
        )
        response = client.get(endpoint, headers=UI_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["quota"]["global_tasks"] == 4
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_expired_cleanup_scan_does_not_stop_refreshed_lease(tmp_path):
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    try:
        await control.reserve(task(1), CONFIG.limits, namespace="pi-local")
        # The maintenance snapshot was old but a heartbeat won the CAS race.
        assert not await control.update(task(1).identity, stop=True, expired_only=True)
        assert await control.allowed(task(1).identity)
    finally:
        await control.close()


async def test_namespace_change_recovery_uses_durable_receipt(tmp_path):
    driver = MemoryDocker()
    old = CONFIG.model_copy(update={"namespace": "old-deployment"})
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    value = task(1)
    try:
        await control.reserve(value, old.limits, namespace=old.namespace)
        backend = LocalDockerSandboxBackend(config=old, transport=driver)
        await backend.create(
            SandboxCreateSpec(
                operation_id=value.identity.operation_id,
                runtime_id=IMAGE,
                limits=old.limits,
            )
        )
        runtime = SimpleNamespace(
            _local=CONFIG,
            control=control,
            _local_backend=lambda cfg: LocalDockerSandboxBackend(config=cfg, transport=driver),
        )
        assert await WebExecutionRuntime._reconcile(runtime, value, None)
        assert not driver.exists
    finally:
        await control.close()


async def test_private_log_failure_does_not_skip_command_cleanup(tmp_path):
    async with aiosqlite.connect(tmp_path / "session.sqlite") as db:
        store = ExecutionStore(db)
        await store.init()
        call = await active(store)
        cleanup = AsyncMock(return_value=True)
        service = ExecutionService(store, scope_check=AsyncMock(return_value=True), cleanup=cleanup)
        store.private_result = AsyncMock(side_effect=OSError("private failure"))
        result = SandboxCommandResult(command_id="command", started_at_ms=0, finished_at_ms=1)
        with pytest.raises(ExecutionDenied, match="execution_interrupted"):
            await service.execute(call, intent(), run=AsyncMock(return_value=result))
        grant = await store.get(call.identity)
        assert grant.state == "interrupted" and not grant.cleanup_pending
        cleanup.assert_awaited_once()


async def test_two_same_account_workspaces_can_start_while_first_is_seeding(tmp_path):
    async with aiosqlite.connect(tmp_path / "session.sqlite") as db:
        store = ExecutionStore(db, account_limit=2, global_limit=4)
        await store.init()
        from tests.test_execution_grants import context

        for value in (task(1), task(2)):
            await store.prepare(value)
            await store.approve(value.identity, expected_scope_sha256=value.sha256)
            await store.activate(context(value), current_scope=value)
        assert all(g.state == "active" and g.cleanup_pending for g in await store.list_grants())


@pytest.mark.docker
@pytest.mark.skipif(not os.environ.get("PI_TEST_DOCKER_IMAGE"), reason="explicit Docker opt-in")
async def test_real_cleanup_reconciles_reserved_and_orphan_containers(tmp_path):
    config = CONFIG.model_copy(
        update={
            "image_id": os.environ["PI_TEST_DOCKER_IMAGE"],
            "namespace": "stage5-" + uuid4().hex[:8],
        }
    )
    client_root = tmp_path / "docker-client"
    client_root.mkdir()

    def factory(cfg):
        return LocalDockerSandboxBackend(
            config=cfg,
            transport=DockerCLITransport(
                executable=Path(os.environ["PI_TEST_DOCKER_EXE"]),
                config_directory=client_root,
                endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
            ),
        )

    value = task(1).model_copy(update={"runtime_id": config.image_id})
    peer = task(2).model_copy(update={"runtime_id": config.image_id})
    control = ExecutionControl(tmp_path / "shared.sqlite")
    await control.init()
    control.register("account", AsyncMock(), factory, config)
    backend = factory(config)
    try:
        handles = []
        for item in (value, peer):
            await control.reserve(item, config.limits, namespace=config.namespace)
            handles.append(
                await backend.create(
                    SandboxCreateSpec(
                        operation_id=item.identity.operation_id,
                        runtime_id=config.image_id,
                        limits=config.limits,
                    )
                )
            )
        # Both copies are alive together. Neither inherits another Workspace's
        # files, and there is no non-loopback interface to reach host services.
        for index, handle in enumerate(handles):
            source = (
                "from pathlib import Path; import socket; "
                "assert {name for _, name in socket.if_nameindex()} == {'lo'}; "
                "assert not list(Path('/workspace').iterdir()); "
                f"Path('/workspace/private.txt').write_text('workspace-{index}')"
            )
            result = await backend.execute(
                handle,
                SandboxCommand(
                    command_id=f"seed-{index}",
                    argv=("python3", "-I", "-S", "-B", "-c", source),
                    timeout_seconds=10,
                    max_output_bytes=1024,
                ),
            )
            assert result.succeeded, result
        for index, handle in enumerate(handles):
            result = await backend.execute(
                handle,
                SandboxCommand(
                    command_id=f"read-{index}",
                    argv=("cat", "/workspace/private.txt"),
                    timeout_seconds=10,
                    max_output_bytes=1024,
                ),
            )
            assert result.succeeded and result.stdout == f"workspace-{index}", result
        for item in (value, peer):
            await control.update(item.identity, heartbeat=True)
        await control.maintain()
        assert len(await backend.managed_operations()) == 2
        assert (await control.summary())["active_tasks"] == 2
        await control.revoke(value.identity.task_id)
        await control.maintain()
        assert await backend.managed_operations() == (
            (peer.identity.operation_id, config.image_id),
        )
        assert (await control.summary())["active_tasks"] == 1
        await control.revoke(peer.identity.task_id)
        await control.maintain()
        assert not await backend.managed_operations()
        assert (await control.summary())["active_tasks"] == 0
        await backend.create(
            SandboxCreateSpec(
                operation_id="orphan",
                runtime_id=config.image_id,
                limits=config.limits,
            )
        )
        await control.maintain()
        assert not await backend.managed_operations()
    finally:
        await backend.reconcile_operation(value.identity.operation_id)
        await backend.reconcile_operation(peer.identity.operation_id)
        await backend.reconcile_operation("orphan")
        await control.close()

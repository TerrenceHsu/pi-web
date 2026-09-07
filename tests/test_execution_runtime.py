"""Real SQLite/Workspace preparation with an offline Docker protocol double."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from agent_workspace.store import WorkspaceStore, WorkspaceTreeConflictError
from coding_agent_app.execution.models import ExecutionDenied, digest_json
from coding_agent_app.execution.runtime import (
    ExecutionPlan,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionTaskRuntime,
    PreparedExecution,
)
from coding_agent_app.execution.store import ExecutionStore
from coding_agent_app.planning.models import PlanSpec, PlanTaskSpec
from coding_agent_app.planning.store import PlanStore
from coding_agent_app.sandbox.workspace import WorkspaceSandboxBaselineProvider
from coding_sandbox import HMACSHA256ArtifactSigner
from coding_sandbox.bash import BashRequest
from coding_sandbox.docker_transport import DockerReply
from coding_sandbox.models import SandboxHandle
from coding_sandbox.operation import get_current_coding_workspace
from coding_sandbox.workspace_models import SandboxFileEntry
from pi_agent_core_py.tools.bash import RunBashTool
from tests.test_coding_sandbox_lifecycle import _fingerprint
from tests.test_local_docker_backend import MemoryDocker, backend


@dataclass
class Case:
    runtime: ExecutionTaskRuntime
    store: ExecutionStore
    workspace: WorkspaceStore
    driver: MemoryDocker
    profiles: list[ExecutionProfile]
    allowed: list[bool]
    request: ExecutionRequest
    db: aiosqlite.Connection
    plans: PlanStore

    async def prepare(self, **kwargs: Any) -> PreparedExecution:
        return await self.runtime.prepare(self.request, goal="build", **kwargs)

    async def approve(self, prepared: PreparedExecution) -> None:
        await self.runtime.approve(
            prepared.scope.identity, expected_scope_sha256=prepared.scope.sha256
        )

    def seed_replies(self, prepared: PreparedExecution) -> None:
        def reply(payload: object) -> DockerReply:
            return DockerReply(0, json.dumps({"ok": True, "result": payload}).encode())

        entries = tuple(
            SandboxFileEntry(path=e.path, size=e.size, sha256=e.sha256)
            for e in prepared.baseline.snapshot.manifest.entries
        )
        self.driver.command_replies = [reply(_fingerprint(()))]
        self.driver.command_replies.extend(
            reply({"path": e.path, "size": e.size, "sha256": e.sha256, "created": True})
            for e in entries
        )
        self.driver.command_replies.append(reply(_fingerprint(entries)))


@pytest.fixture
async def case(tmp_path: Path) -> AsyncIterator[Case]:
    driver, docker, spec = backend()
    profiles = [
        ExecutionProfile(
            enabled=True,
            backend="local_docker",
            runtime_id=spec.runtime_id,
            config_revision=1,
            selection_sha256="a" * 64,
            publish_policy_sha256="b" * 64,
            limits=spec.limits,
        )
    ]
    allowed = [True]
    request = ExecutionRequest(
        account_id="account", session_id="session", request_id="request", workspace_id="workspace"
    )
    workspace = WorkspaceStore(tmp_path / "workspace")
    await workspace.init()
    await workspace.ensure_session_workspace("session")
    baseline = WorkspaceSandboxBaselineProvider(
        workspace, materialization_root=tmp_path / "materialized"
    )
    async with aiosqlite.connect(tmp_path / "execution.db", isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        await db.execute("INSERT INTO sessions VALUES('session')")
        store, plans = ExecutionStore(db), PlanStore(db)
        await store.init()
        await plans.init()

        async def profile(_request: ExecutionRequest) -> ExecutionProfile:
            return profiles[0]

        async def version(call: ExecutionRequest) -> tuple[int, str]:
            return await baseline.current_version(call.session_id)

        async def resolve(_profile: ExecutionProfile) -> Any:
            return docker

        async def enabled(_scope: object) -> bool:
            return allowed[0]

        async def request_allowed(call: ExecutionRequest) -> bool:
            return call.account_id == "account" and call.session_id == "session" and allowed[0]

        runtime = ExecutionTaskRuntime(
            store=store,
            profile_provider=profile,
            baseline_provider=baseline,
            baseline_version=version,
            backend_resolver=resolve,
            scope_enabled=enabled,
            request_allowed=request_allowed,
            artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"x" * 32),
            staging_root=tmp_path / "staging",
            plan_store=plans,
        )
        yield Case(runtime, store, workspace, driver, profiles, allowed, request, db, plans)
        await runtime.shutdown()


async def test_prepare_and_approval_make_zero_daemon_calls(case: Case) -> None:
    prepared = await case.prepare()
    assert prepared.baseline.snapshot.archive_path.is_file()
    assert not case.driver.calls
    with pytest.raises(ExecutionDenied, match="approval_required"):
        await case.runtime.start(prepared.scope.identity)
    await case.approve(prepared)
    assert (await case.store.get(prepared.scope.identity)).state == "approved"
    assert not case.driver.calls
    await case.runtime.cancel(prepared.scope.identity)
    with pytest.raises(ExecutionDenied):
        await case.runtime.start(prepared.scope.identity)
    assert not case.driver.calls


async def test_exact_baseline_then_one_task_copy_and_finally_close(case: Case) -> None:
    prepared = await case.prepare()
    before = await case.workspace.inspect_workspace_revision("session")
    await case.approve(prepared)
    case.seed_replies(prepared)
    async with case.runtime.task(prepared.scope.identity) as operation:
        assert get_current_coding_workspace() is operation
        assert (await case.store.get(prepared.scope.identity)).commands_used == 0
        case.driver.command_reply = DockerReply(3)
        assert (await operation.run(("false",), timeout_seconds=5)).exit_code == 3
        case.driver.command_reply = DockerReply(0)
        assert not (
            await RunBashTool().execute("call", {"script": "true", "timeout_seconds": 5})
        ).is_error
        with pytest.raises(ExecutionDenied, match="approval_required"):
            await case.runtime.start(prepared.scope.identity)
    grant = await case.store.get(prepared.scope.identity)
    assert grant.state == "closed" and grant.commands_used == 2 and not grant.cleanup_pending
    assert not case.driver.exists
    assert await case.workspace.inspect_workspace_revision("session") == before
    assert sum(call[:2] == ("container", "create") for call in case.driver.calls) == 1


async def test_request_finish_and_maintenance_share_one_cleanup(
    case: Case, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    identity = prepared.scope.identity
    operation = await case.runtime.start(identity)
    resource = case.runtime._resources[identity.task_id]
    assert resource.backend is not None and operation is resource.operation
    destroy, get = resource.backend.destroy, case.store.get
    entered, release, both_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    readers: set[object] = set()

    async def delayed_destroy(handle: SandboxHandle) -> None:
        entered.set()
        await release.wait()
        await destroy(handle)

    async def observed_get(*args: Any, **kwargs: Any) -> Any:
        value = await get(*args, **kwargs)
        readers.add(asyncio.current_task())
        if len(readers) == 2:
            both_read.set()
        return value

    removal = AsyncMock(side_effect=delayed_destroy)
    monkeypatch.setattr(resource.backend, "destroy", removal)
    monkeypatch.setattr(case.store, "get", observed_get)
    # These are the same cleanup callback used by request completion and the
    # maintenance revoker. Hold the first removal until both callers arrive.
    jobs = [asyncio.create_task(case.runtime._cleanup(identity))]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        jobs.append(asyncio.create_task(case.runtime._cleanup(identity)))
        await asyncio.wait_for(both_read.wait(), 5)
    finally:
        release.set()
        results = await asyncio.gather(*jobs)
    assert results == [True, True]
    removal.assert_awaited_once()
    assert not case.driver.exists


async def test_terminal_resources_release_preserves_durable_revocation(case: Case) -> None:
    prepared = await case.prepare()
    identity = prepared.scope.identity
    assert await case.runtime.release_terminal_resources() == 0
    assert len(await case.store.list_grants(needs_maintenance=True)) == 1
    await case.approve(prepared)
    case.seed_replies(prepared)
    await case.runtime.start(identity)
    assert await case.runtime.release_terminal_resources() == 0
    await case.runtime.finish(identity)
    assert await case.runtime.release_terminal_resources() == 1
    assert not case.runtime._resources and not case.runtime._cleanup_locks
    assert await case.store.list_grants(needs_maintenance=True) == []
    assert (await case.store.get(identity)).state == "closed"
    await case.runtime.cancel(identity)  # Idempotent after eviction; no second destroy.
    assert not case.runtime._cleanup_locks
    with pytest.raises(ExecutionDenied):
        await case.runtime.start(identity)
    assert sum(call[:2] == ("container", "create") for call in case.driver.calls) == 1


@pytest.mark.parametrize("when", ["approve", "start"])
@pytest.mark.parametrize("change", ["workspace", "config", "selection", "request"])
async def test_changed_scope_is_rejected_before_creation(
    case: Case, when: str, change: str
) -> None:
    prepared = await case.prepare()
    if when == "start":
        await case.approve(prepared)
    if change == "workspace":
        await case.workspace.write_text("session", "changed.txt", "new")
    elif change == "request":
        case.allowed[0] = False
    else:
        field = "config_revision" if change == "config" else "selection_sha256"
        case.profiles[0] = case.profiles[0].model_copy(
            update={field: 2 if change == "config" else "c" * 64}
        )
    with pytest.raises(ExecutionDenied, match="approval_stale"):
        if when == "approve":
            await case.approve(prepared)
        else:
            await case.runtime.start(prepared.scope.identity)
    assert not case.driver.calls


async def test_active_copy_does_not_silently_refresh_from_real_workspace(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    async with case.runtime.task(prepared.scope.identity) as operation:
        await case.workspace.write_text("session", "external.txt", "not in approved copy")
        case.driver.command_reply = DockerReply(0)
        assert (await operation.run(("true",), timeout_seconds=5)).succeeded
        assert (await case.store.get(prepared.scope.identity)).scope == prepared.scope


async def test_permission_revocation_rejects_active_task_and_destroys_copy(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    operation = await case.runtime.start(prepared.scope.identity)
    async with case.runtime.bind(prepared.scope.identity):
        case.allowed[0] = False
        with pytest.raises(ExecutionDenied, match="grant_revoked"):
            await operation.run(("true",))
    assert not case.driver.exists


async def test_cancel_during_seeding_does_not_expose_ready_operation(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    case.driver.command_wait = asyncio.Event()
    starting = asyncio.create_task(case.runtime.start(prepared.scope.identity))
    await asyncio.wait_for(case.driver.command_started.wait(), timeout=3)
    await asyncio.wait_for(case.runtime.cancel(prepared.scope.identity), timeout=3)
    with pytest.raises(asyncio.CancelledError):
        await starting
    grant = await case.store.get(prepared.scope.identity)
    assert grant.state == "revoked" and not grant.cleanup_pending and not case.driver.exists


async def test_concurrent_start_has_one_creator(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    results = await asyncio.gather(
        case.runtime.start(prepared.scope.identity),
        case.runtime.start(prepared.scope.identity),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ExecutionDenied) for value in results) == 1
    assert sum(call[:2] == ("container", "create") for call in case.driver.calls) == 1


async def test_new_request_cannot_reuse_old_task_or_identity(case: Case) -> None:
    first = await case.prepare()
    await case.runtime.cancel(first.scope.identity)
    second = await case.runtime.prepare(
        case.request.model_copy(update={"request_id": "next"}), goal="build"
    )
    assert first.scope.identity.operation_id != second.scope.identity.operation_id
    forged = second.scope.identity.model_copy(update={"account_id": "other"})
    with pytest.raises(ExecutionDenied, match="grant_required"):
        await case.runtime.cancel(forged)
    assert (await case.store.get(second.scope.identity)).state == "pending"


async def test_standalone_bash_binds_script_cwd_and_approved_time_budget(case: Case) -> None:
    script = BashRequest(script="printf '中文'", timeout_seconds=5)
    prepared = await case.prepare(bash=script)
    assert prepared.scope.script_sha256 == script.sha256 and prepared.scope.max_execution_ms == 5000
    await case.approve(prepared)
    case.seed_replies(prepared)
    async with case.runtime.task(prepared.scope.identity) as operation:
        with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
            await operation.run_bash(BashRequest(script="changed", timeout_seconds=5))
    assert not any(call[-1] == "prepare_bash" for call in case.driver.calls)


async def test_wrong_handle_is_not_used_or_destroyed_as_if_owned(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    docker = await case.runtime._resolve(case.profiles[0])
    monkeypatch.setattr(
        docker,
        "create",
        AsyncMock(
            return_value=SandboxHandle(
                provider="local_docker",
                sandbox_id="someone-else",
                operation_id="other",
                runtime_id=prepared.scope.runtime_id,
                created_at_ms=1,
            )
        ),
    )
    destroy = AsyncMock()
    monkeypatch.setattr(docker, "destroy", destroy)
    with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
        await case.runtime.start(prepared.scope.identity)
    destroy.assert_not_awaited()
    assert (await case.store.get(prepared.scope.identity)).cleanup_pending


async def test_unknown_create_result_holds_admission_barrier(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    docker = await case.runtime._resolve(case.profiles[0])
    monkeypatch.setattr(docker, "create", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        await case.runtime.start(prepared.scope.identity)
    grant = await case.store.get(prepared.scope.identity)
    assert grant.state == "interrupted" and grant.cleanup_pending
    assert await case.runtime.recover_startup() == (prepared.scope.identity,)
    assert not case.driver.calls


async def test_real_plan_approval_is_atomic_and_later_version_change_blocks_start(
    case: Case,
) -> None:
    spec = PlanSpec(
        goal="build",
        summary="build",
        tasks=(
            PlanTaskSpec(
                id="build", title="build", objective="build", acceptance_criteria=("works",)
            ),
        ),
    )
    run = await case.plans.create_run("session", "request", "build")
    await case.plans.save_plan(run.id, spec)
    prepared = await case.prepare(
        plan=ExecutionPlan(
            plan_id=run.id, version=1, sha256=digest_json(spec.model_dump(mode="json"))
        )
    )
    await case.approve(prepared)
    assert (await case.plans.get_run(run.id)).status == "executing"
    assert (await case.store.get(prepared.scope.identity)).state == "approved"
    await case.db.execute("UPDATE plan_runs SET plan_version=2 WHERE id=?", (run.id,))
    with pytest.raises(ExecutionDenied, match="approval_stale"):
        await case.runtime.start(prepared.scope.identity)
    assert not case.driver.calls


async def test_unauthorized_prepare_does_not_materialize_workspace(case: Case) -> None:
    wrong = case.request.model_copy(update={"account_id": "other"})
    with pytest.raises(ExecutionDenied, match="execution_request_denied"):
        await case.runtime.prepare(wrong, goal="build")
    assert not list(case.runtime._root.glob("snapshots/*")) and not case.driver.calls


async def test_workspace_revision_inspection_detects_out_of_band_byte_changes(case: Case) -> None:
    ref = await case.workspace.write_text("session", "sample.txt", "before")
    prepared = await case.prepare()
    await case.approve(prepared)
    await asyncio.to_thread(Path(ref.path).write_text, "after!")
    with pytest.raises(WorkspaceTreeConflictError):
        await case.runtime.start(prepared.scope.identity)
    assert not case.driver.calls


async def test_cancel_after_prepare_commit_does_not_strand_workspace_lease(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = case.store.prepare
    identities = []

    async def commit_then_cancel(scope: Any, **kwargs: Any) -> Any:
        await original(scope, **kwargs)
        identities.append(scope.identity)
        raise asyncio.CancelledError

    monkeypatch.setattr(case.store, "prepare", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await case.prepare()
    grant = await case.store.get(identities[0])
    assert grant.state == "interrupted" and not grant.cleanup_pending
    monkeypatch.setattr(case.store, "prepare", original)
    assert (await case.prepare()).scope.identity != identities[0]
    assert not case.driver.calls


async def test_replaced_snapshot_is_rejected_and_approval_closed(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    await asyncio.to_thread(prepared.baseline.snapshot.archive_path.write_bytes, b"not an archive")
    from coding_sandbox.snapshot import SnapshotError

    with pytest.raises(SnapshotError):
        await case.runtime.start(prepared.scope.identity)
    assert (await case.store.get(prepared.scope.identity)).state == "interrupted"
    assert not case.driver.calls


async def test_disabled_profile_never_materializes_or_creates_runtime(case: Case) -> None:
    case.profiles[0] = case.profiles[0].model_copy(update={"enabled": False})
    with pytest.raises(ExecutionDenied, match="execution_disabled"):
        await case.prepare()
    assert not case.driver.calls and not list(case.runtime._root.glob("snapshots/*"))


async def test_restart_invalidates_approved_and_live_tasks_without_recreation(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    assert await case.runtime.recover_startup() == ()
    assert (await case.store.get(prepared.scope.identity)).state == "interrupted"
    assert not case.driver.calls
    second = await case.prepare()
    await case.approve(second)
    case.seed_replies(second)
    await case.runtime.start(second.scope.identity)
    # Simulate a fresh process: no handles or preparation objects survive.
    case.runtime._resources.clear()
    before = len(case.driver.calls)
    assert await case.runtime.recover_startup() == (second.scope.identity,)
    assert len(case.driver.calls) == before
    with pytest.raises(ExecutionDenied, match="execution_interrupted"):
        await case.runtime.start(second.scope.identity)

    # Only explicit provider reconciliation may acknowledge absence.
    async def reconcile(scope: Any, handle: Any) -> bool:
        assert scope == second.scope and handle is None
        case.driver.exists = False
        return True

    case.runtime._reconcile = reconcile
    assert await case.runtime.recover_startup() == ()


async def test_startup_timeout_closes_grant_and_confirmed_copy(case: Case) -> None:
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    case.driver.command_wait = asyncio.Event()
    case.runtime._startup_timeout = 0.3
    with pytest.raises(TimeoutError):
        await case.runtime.start(prepared.scope.identity)
    grant = await case.store.get(prepared.scope.identity)
    assert grant.state == "interrupted" and not grant.cleanup_pending and not case.driver.exists

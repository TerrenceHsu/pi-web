"""Offline authorization contracts. No Web adapter or backend is implicitly enabled."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from pydantic import ValidationError

from coding_agent_app.execution.models import (
    CommandIntent,
    ExecutionContext,
    ExecutionDenied,
    ExecutionIdentity,
    ExecutionScope,
)
from coding_agent_app.execution.service import ExecutionService
from coding_agent_app.execution.store import ExecutionStore
from coding_sandbox.models import SandboxCommandResult

SHA = "a" * 64


def scope(**values: Any) -> ExecutionScope:
    defaults: dict[str, Any] = dict(
        identity=ExecutionIdentity(
            account_id="account",
            session_id="session",
            request_id="request",
            workspace_id="workspace",
            task_id="task",
            operation_id="operation",
        ),
        kind="coding",
        backend="local_docker",
        runtime_id="sha256:" + SHA,
        config_revision=1,
        policy_sha256=SHA,
        baseline_revision=1,
        baseline_sha256=SHA,
        input_sha256=SHA,
        publish_policy_sha256=SHA,
        capabilities=("edit", "execute", "validate"),
        request_sha256=SHA,
    )
    return ExecutionScope(**(defaults | values))


def context(value: ExecutionScope) -> ExecutionContext:
    return ExecutionContext(identity=value.identity, scope_sha256=value.sha256, role="executor")


def intent(identifier: str = "command", revision: int = 0, **values: Any) -> CommandIntent:
    return CommandIntent(
        **(
            dict(
                command_id=identifier,
                capability="execute",
                kind="argv",
                payload_sha256=SHA,
                copy_revision=revision,
                timeout_ms=10_000,
            )
            | values
        )
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ExecutionStore]:
    async with aiosqlite.connect(tmp_path / "execution.sqlite", isolation_level=None) as db:
        service = ExecutionStore(db)
        await service.init()
        yield service


async def active(store: ExecutionStore, value: ExecutionScope | None = None) -> ExecutionContext:
    value = value or scope()
    await store.prepare(value)
    await store.approve(value.identity, expected_scope_sha256=value.sha256)
    call = context(value)
    await store.activate(call, current_scope=value)
    await store.runtime_ready(call)
    return call


async def test_explicit_approval_and_exact_snapshot_required(store: ExecutionStore) -> None:
    value = scope()
    call = context(value)
    with pytest.raises(ExecutionDenied, match="grant_required"):
        await store.claim(call, intent())
    await store.prepare(value)
    with pytest.raises(ExecutionDenied):
        await store.activate(call, current_scope=value)
    with pytest.raises(ExecutionDenied, match="approval_stale"):
        await store.approve(value.identity, expected_scope_sha256="b" * 64)
    await store.approve(value.identity, expected_scope_sha256=value.sha256)
    for changed in (
        value.model_copy(update={"config_revision": 2}),
        value.model_copy(update={"baseline_revision": 2}),
    ):
        with pytest.raises(ExecutionDenied, match="approval_stale"):
            await store.activate(call, current_scope=changed)
    await store.activate(call, current_scope=value)
    with pytest.raises(ExecutionDenied, match="cleanup_pending"):
        await store.claim(call, intent())


@pytest.mark.parametrize("field", list(ExecutionIdentity.model_fields))
async def test_every_identity_component_is_bound(store: ExecutionStore, field: str) -> None:
    call = await active(store)
    forged = call.model_copy(update={"identity": call.identity.model_copy(update={field: "other"})})
    with pytest.raises(ExecutionDenied, match="grant_required"):
        await store.claim(forged, intent())
    with pytest.raises(ExecutionDenied, match="grant_required"):
        await store.terminate(forged.identity)
    assert (await store.get(call.identity)).state == "active"


@pytest.mark.parametrize("role", ["planner", "verifier", "read_only"])
async def test_readonly_roles_never_receive_execution_authority(
    store: ExecutionStore, role: str
) -> None:
    call = await active(store)
    with pytest.raises(ExecutionDenied, match="execution_role_denied"):
        await store.claim(call.model_copy(update={"role": role}), intent())


async def test_scope_hash_and_capability_cannot_be_forged(store: ExecutionStore) -> None:
    call = await active(store, scope(capabilities=("execute",)))
    with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
        await store.claim(call.model_copy(update={"scope_sha256": "b" * 64}), intent())
    with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
        await store.claim(call, intent(kind="edit", capability="edit"))


async def test_failed_command_repair_keeps_one_grant_and_monotonic_revision(
    store: ExecutionStore,
) -> None:
    call = await active(store)
    for index, code in enumerate((3, 0)):
        cmd = intent(str(index), index)
        await store.claim(call, cmd)
        await store.start(call, cmd)
        done = await store.finish(call, cmd, exit_code=code)
        assert done.state == ("failed" if code else "succeeded")
    grant = await store.get(call.identity)
    assert grant.state == "active" and grant.commands_used == 2 and grant.copy_revision == 2
    with pytest.raises(ExecutionDenied, match="approval_already_decided"):
        await store.approve(call.identity, expected_scope_sha256=call.scope_sha256)


async def test_concurrent_claim_and_start_have_only_one_winner(store: ExecutionStore) -> None:
    call = await active(store)
    outcomes = await asyncio.gather(
        store.claim(call, intent()), store.claim(call, intent()), return_exceptions=True
    )
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    outcomes = await asyncio.gather(
        store.start(call, intent()), store.start(call, intent()), return_exceptions=True
    )
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    await store.finish(call, intent(), exit_code=0)
    with pytest.raises(ExecutionDenied):
        await store.claim(call, intent(revision=1))
    with pytest.raises(ExecutionDenied):
        await store.finish(call, intent(), exit_code=0)


async def test_payload_swap_and_reordered_copy_never_start(store: ExecutionStore) -> None:
    call = await active(store)
    await store.claim(call, intent())
    with pytest.raises(ExecutionDenied, match="command_replay_or_changed"):
        await store.start(call, intent(payload_sha256="b" * 64))
    with pytest.raises(ExecutionDenied, match="copy_revision_stale"):
        await store.claim(call, intent("second", 0))


async def test_single_bash_approval_binds_script_cwd_and_one_execution(
    store: ExecutionStore,
) -> None:
    value = scope(
        kind="bash",
        capabilities=("execute",),
        script_sha256=SHA,
        cwd="scripts",
        max_commands=1,
        lifetime_ms=600_000,
    )
    call = await active(store, value)
    for wrong in (intent(), intent(kind="bash", cwd="scripts", payload_sha256="b" * 64)):
        with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
            await store.claim(call, wrong)
    cmd = intent(kind="bash", cwd="scripts")
    await store.claim(call, cmd)
    await store.start(call, cmd)
    await store.finish(call, cmd, exit_code=0)
    with pytest.raises(ExecutionDenied, match="task_budget_exhausted"):
        await store.claim(call, intent("second", 1, kind="bash", cwd="scripts"))


async def test_time_budget_is_charged_before_execution(store: ExecutionStore) -> None:
    call = await active(store, scope(max_execution_ms=1000))
    with pytest.raises(ExecutionDenied, match="task_budget_exhausted"):
        await store.claim(call, intent())
    assert (await store.get(call.identity)).commands_used == 0
    await store.claim(call, intent(timeout_ms=1000))
    assert (await store.get(call.identity)).charged_ms == 1000


async def test_expiry_does_not_extend_on_repeated_approval(tmp_path: Path) -> None:
    clock = [1000]
    async with aiosqlite.connect(tmp_path / "expiry.sqlite", isolation_level=None) as db:
        store = ExecutionStore(db, clock_ms=lambda: clock[0])
        await store.init()
        value = scope()
        await store.prepare(value)
        clock[0] += 600_000
        with pytest.raises(ExecutionDenied, match="approval_expired"):
            await store.approve(value.identity, expected_scope_sha256=value.sha256)
        await store.terminate(value.identity)
        value = scope(
            identity=value.identity.model_copy(update={"task_id": "new", "operation_id": "new"})
        )
        await store.prepare(value)
        await store.approve(value.identity, expected_scope_sha256=value.sha256)
        clock[0] += 60_000
        with pytest.raises(ExecutionDenied, match="approval_stale"):
            await store.activate(context(value), current_scope=value)


async def test_workspace_account_global_limits_and_cleanup_barrier(store: ExecutionStore) -> None:
    first = await active(store)
    with pytest.raises(ExecutionDenied, match="workspace_busy"):
        await store.prepare(
            scope(
                identity=first.identity.model_copy(update={"task_id": "t2", "operation_id": "o2"})
            )
        )
    second = scope(
        identity=first.identity.model_copy(
            update={
                "task_id": "t2",
                "operation_id": "o2",
                "workspace_id": "w2",
            }
        )
    )
    await store.prepare(second)
    await store.approve(second.identity, expected_scope_sha256=second.sha256)
    with pytest.raises(ExecutionDenied, match="execution_busy"):
        await store.activate(context(second), current_scope=second)
    third = scope(
        identity=first.identity.model_copy(
            update={
                "task_id": "t3",
                "operation_id": "o3",
                "account_id": "a3",
            }
        )
    )
    await active(store, third)
    fourth = scope(
        identity=first.identity.model_copy(
            update={
                "task_id": "t4",
                "operation_id": "o4",
                "account_id": "a4",
            }
        )
    )
    await store.prepare(fourth)
    await store.approve(fourth.identity, expected_scope_sha256=fourth.sha256)
    with pytest.raises(ExecutionDenied, match="execution_busy"):
        await store.activate(context(fourth), current_scope=fourth)
    await store.terminate(first.identity)
    with pytest.raises(ExecutionDenied, match="cleanup_pending"):
        await store.activate(context(second), current_scope=second)
    await store.confirm_cleanup(first.identity)
    await store.activate(context(second), current_scope=second)


async def test_restart_interrupts_without_resuming_commands(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    async with aiosqlite.connect(path, isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        call = await active(store)
        await store.claim(call, intent())
        await store.start(call, intent())
    async with aiosqlite.connect(path, isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        assert await store.recover_interrupted() == [call.identity]
        assert (await store.get(call.identity)).cleanup_pending
        with pytest.raises(ExecutionDenied, match="grant_revoked"):
            await store.start(call, intent())
        await store.confirm_cleanup(call.identity)
        assert await store.recover_interrupted() == []


async def test_plan_approval_requires_atomic_adapter_and_rolls_back(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "plan.sqlite", isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        await db.execute("CREATE TABLE test_plan (approved INTEGER)")
        value = scope(kind="plan", plan_id="plan", plan_version=1, plan_sha256=SHA)
        await store.prepare(value)
        with pytest.raises(ExecutionDenied, match="plan_approval_required"):
            await store.approve(value.identity, expected_scope_sha256=value.sha256)

        async def broken(connection: aiosqlite.Connection, saved: ExecutionScope) -> None:
            assert saved == value and connection is db
            await connection.execute("INSERT INTO test_plan VALUES (1)")
            raise ExecutionDenied("plan_version_stale")

        with pytest.raises(ExecutionDenied, match="plan_version_stale"):
            await store.approve(
                value.identity, expected_scope_sha256=value.sha256, plan_approval=broken
            )
        assert (await store.get(value.identity)).state == "pending"
        async with db.execute("SELECT count(*) FROM test_plan") as cursor:
            assert await cursor.fetchone() == (0,)


@pytest.mark.parametrize(
    "values",
    [
        {"runtime_id": "python:latest"},
        {"kind": "plan"},
        {"kind": "bash"},
        {"capabilities": ()},
        {"max_commands": 51},
        {"grant_id": "model-forged"},
    ],
)
def test_scope_rejects_implicit_or_expanded_authority(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scope(**values)


@pytest.mark.parametrize("cwd", ["../x", "/tmp", "a/../b", "a\\b", "a//b", "C:x", ""])
def test_command_cwd_is_strict(cwd: str) -> None:
    with pytest.raises(ValidationError):
        intent(cwd=cwd)


async def test_service_revocation_stops_running_command_and_cleans_up(
    store: ExecutionStore,
) -> None:
    call = await active(store)
    running = asyncio.Event()
    cleaned: list[str] = []

    async def check(value: ExecutionScope) -> bool:
        return True

    async def cleanup(identity: ExecutionIdentity) -> bool:
        cleaned.append(identity.task_id)
        return True

    async def run(signal: asyncio.Event) -> SandboxCommandResult:
        running.set()
        await signal.wait()
        raise asyncio.CancelledError

    service = ExecutionService(store, scope_check=check, cleanup=cleanup)
    job = asyncio.create_task(service.execute(call, intent(), run=run))
    await asyncio.wait_for(running.wait(), 2)
    await service.revoke(call.identity)
    with pytest.raises((ExecutionDenied, asyncio.CancelledError)):
        await asyncio.wait_for(job, 2)
    assert cleaned and not (await store.get(call.identity)).cleanup_pending
    assert (await store.get(call.identity)).state == "revoked"


async def test_service_unknown_outcome_never_retries_and_preserves_cleanup_debt(
    store: ExecutionStore,
) -> None:
    call = await active(store)
    runs = 0

    async def check(value: ExecutionScope) -> bool:
        return True

    async def cleanup(identity: ExecutionIdentity) -> bool:
        return False

    async def run(signal: asyncio.Event) -> SandboxCommandResult:
        nonlocal runs
        runs += 1
        raise OSError("test transport disconnected")

    service = ExecutionService(store, scope_check=check, cleanup=cleanup)
    with pytest.raises(ExecutionDenied, match="execution_interrupted"):
        await service.execute(call, intent(), run=run)
    with pytest.raises(ExecutionDenied):
        await service.execute(call, intent(), run=run)
    assert runs == 1 and (await store.get(call.identity)).cleanup_pending


async def test_service_runs_known_failures_without_new_approval(store: ExecutionStore) -> None:
    call = await active(store)

    async def check(value: ExecutionScope) -> bool:
        return True

    async def cleanup(identity: ExecutionIdentity) -> bool:
        raise AssertionError("known failure must preserve the task")

    async def run(signal: asyncio.Event) -> SandboxCommandResult:
        return SandboxCommandResult(
            command_id="command", exit_code=3, started_at_ms=1, finished_at_ms=2
        )

    result = await ExecutionService(store, scope_check=check, cleanup=cleanup).execute(
        call, intent(), run=run
    )
    assert result.exit_code == 3 and (await store.get(call.identity)).state == "active"


async def test_two_connections_cannot_approve_or_claim_twice(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    async with (
        aiosqlite.connect(path, isolation_level=None) as first,
        aiosqlite.connect(path, isolation_level=None) as second,
    ):
        left, right = ExecutionStore(first), ExecutionStore(second)
        await left.init()
        await right.init()
        value = scope()
        await left.prepare(value)
        decisions = await asyncio.gather(
            left.approve(value.identity, expected_scope_sha256=value.sha256),
            right.approve(value.identity, expected_scope_sha256=value.sha256),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in decisions) == 1
        call = context(value)
        await left.activate(call, current_scope=value)
        await left.runtime_ready(call)
        claims = await asyncio.gather(
            left.claim(call, intent()), right.claim(call, intent()), return_exceptions=True
        )
        assert sum(not isinstance(item, BaseException) for item in claims) == 1
        assert (await right.get(value.identity)).commands_used == 1


@pytest.mark.parametrize("mode", ["capability_revoked", "caller_cancelled", "timeout"])
async def test_service_terminal_failures_cleanup_without_retry(
    store: ExecutionStore, mode: str
) -> None:
    call = await active(store)
    started = asyncio.Event()
    allow = True
    cleaned = False

    async def check(value: ExecutionScope) -> bool:
        return allow

    async def cleanup(identity: ExecutionIdentity) -> bool:
        nonlocal cleaned
        cleaned = True
        return True

    async def run(signal: asyncio.Event) -> SandboxCommandResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = ExecutionService(store, scope_check=check, cleanup=cleanup)
    job = asyncio.create_task(service.execute(call, intent(timeout_ms=1000), run=run))
    await asyncio.wait_for(started.wait(), 2)
    if mode == "caller_cancelled":
        job.cancel()
    elif mode == "capability_revoked":
        allow = False
    with pytest.raises((ExecutionDenied, asyncio.CancelledError)):
        await asyncio.wait_for(job, 3)
    grant = await store.get(call.identity)
    assert cleaned and grant.state == "interrupted" and not grant.cleanup_pending


async def test_service_expired_grant_is_cleaned_before_any_command(tmp_path: Path) -> None:
    clock = [1000]
    async with aiosqlite.connect(tmp_path / "service-expiry.sqlite", isolation_level=None) as db:
        store = ExecutionStore(db, clock_ms=lambda: clock[0])
        await store.init()
        call = await active(store, scope(lifetime_ms=1000))
        clock[0] += 1000
        cleaned = False

        async def check(value: ExecutionScope) -> bool:
            raise AssertionError("expired grant must not reach policy/run")

        async def cleanup(identity: ExecutionIdentity) -> bool:
            nonlocal cleaned
            cleaned = True
            return True

        async def run(signal: asyncio.Event) -> SandboxCommandResult:
            raise AssertionError("expired grant must not run")

        service = ExecutionService(store, scope_check=check, cleanup=cleanup)
        with pytest.raises(ExecutionDenied, match="grant_expired"):
            await service.execute(call, intent(), run=run)
        assert cleaned and (await store.get(call.identity)).state == "expired"

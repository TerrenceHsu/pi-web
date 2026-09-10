"""Session deletion must settle execution debt before any session rows cascade."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coding_agent_app.execution.control import ExecutionControl
from coding_agent_app.execution.models import ExecutionDenied
from coding_sandbox.lifecycle import ManagedSandboxLifecycle, ManagedSandboxOperationRecord
from pi_agent_core_py.web.execution import WebExecutionRuntime
from tests.test_execution_grants import active, scope
from tests.test_execution_runtime import Case
from tests.test_execution_runtime import case as case


class Operations:
    def __init__(self):
        self.records: dict[str, ManagedSandboxOperationRecord] = {}
        self.discarded: list[str] = []

    async def active_records(self, *, session_id=None):
        return tuple(
            r for r in self.records.values()
            if not r.terminal and (session_id is None or r.session_id == session_id)
        )

    async def get(self, operation_id):
        return self.records[operation_id]

    async def discard(self, operation_id):
        current = self.records[operation_id]
        assert "discard" in current.allowed_actions
        self.discarded.append(operation_id)
        self.records[operation_id] = current.model_copy(update={"status": "discarded"})
        return self.records[operation_id]


@pytest.fixture
async def web(case: Case, tmp_path):
    control = ExecutionControl(tmp_path / "control.sqlite")
    await control.init()
    # No background reconciliation: assertions exercise only the deletion path.
    control._worker.cancel()
    await asyncio.gather(control._worker, return_exceptions=True)
    case.runtime._control = control
    instance = WebExecutionRuntime.__new__(WebExecutionRuntime)
    instance._account = "account"
    instance._maintenance_lock = asyncio.Lock()
    instance.runtime = case.runtime
    instance.control = control
    instance._approvals = SimpleNamespace(cancel_request=AsyncMock())
    instance._lifecycle = Operations()
    instance._tasks = {}
    instance.bash_tasks = {}
    try:
        yield instance
    finally:
        case.driver.unreachable = False
        await case.runtime.shutdown()
        case.runtime._control = None
        await control.close()


def operation(identity, status="awaiting_approval"):
    return ManagedSandboxOperationRecord(
        operation_id=identity.operation_id,
        session_id=identity.session_id,
        status=status,
        config_revision=1,
        created_at_ms=1,
        updated_at_ms=1,
        artifact_id="artifact-" + "1" * 32 if status == "awaiting_approval" else None,
    )


@pytest.mark.parametrize("state", ["pending", "approved", "active"])
async def test_deletion_revokes_grants_without_live_web_task(case: Case, web, state):
    prepared = await case.prepare()
    identity = prepared.scope.identity
    if state != "pending":
        await case.approve(prepared)
    if state == "active":
        case.seed_replies(prepared)
        await case.runtime.start(identity)
        assert case.driver.exists

    await web.prepare_session_deletion("session")

    grant = await case.store.get(identity)
    assert grant.state == "revoked" and not grant.cleanup_pending
    assert not case.driver.exists
    web._approvals.cancel_request.assert_awaited_with(identity.request_id)
    lease = await web.control.recovery_lease(identity)
    assert lease is None or lease.released
    assert await case.store.list_grants(session_id="session")  # Audit is not deleted.
    await web.prepare_session_deletion("session")


async def test_deletion_blocks_on_daemon_failure_and_retries_without_losing_debt(case: Case, web):
    prepared = await case.prepare()
    await case.approve(prepared)
    case.seed_replies(prepared)
    identity = prepared.scope.identity
    await case.runtime.start(identity)
    case.driver.unreachable = True

    with pytest.raises(ExecutionDenied, match="session_cleanup_pending"):
        await web.prepare_session_deletion("session")
    grant = await case.store.get(identity)
    assert grant.state == "revoked" and grant.cleanup_pending
    assert case.driver.exists
    lease = await web.control.recovery_lease(identity)
    assert lease.stop and not lease.released

    case.driver.unreachable = False
    await web.prepare_session_deletion("session")
    assert not (await case.store.get(identity)).cleanup_pending
    assert not case.driver.exists
    assert (await web.control.recovery_lease(identity)).released


@pytest.mark.parametrize("confirmed", [False, True])
async def test_persisted_cleanup_debt_requires_backend_confirmation(case: Case, web, confirmed):
    value = scope()
    await active(case.store, value)
    await case.store.terminate(value.identity, state="interrupted")
    assert not case.runtime._resources
    case.runtime._reconcile = AsyncMock(return_value=confirmed)
    if confirmed:
        await web.prepare_session_deletion("session")
    else:
        with pytest.raises(ExecutionDenied, match="session_cleanup_pending"):
            await web.prepare_session_deletion("session")
    assert (await case.store.get(value.identity)).cleanup_pending is not confirmed
    case.runtime._reconcile.assert_awaited_once_with(value, None)


async def test_only_target_session_and_account_are_revoked(case: Case, web):
    prepared = await case.prepare()
    other = scope(identity=scope().identity.model_copy(update={
        "session_id": "other", "task_id": "other-task", "operation_id": "other-operation",
        "workspace_id": "other-workspace",
    }))
    await case.store.prepare(other)
    foreign = scope(identity=scope().identity.model_copy(update={
        "account_id": "foreign", "task_id": "foreign-task", "operation_id": "foreign-op",
    }))
    await web.control.reserve(foreign, case.profiles[0].limits, namespace="foreign")

    await web.prepare_session_deletion("session")

    assert (await case.store.get(prepared.scope.identity)).state == "revoked"
    assert (await case.store.get(other.identity)).state == "pending"
    lease = await web.control.recovery_lease(foreign.identity)
    assert not lease.stop and not lease.released


async def test_orphan_control_lease_is_stopped_but_not_falsely_released(case: Case, web):
    value = scope()
    await web.control.reserve(value, case.profiles[0].limits, namespace="owned")
    with pytest.raises(ExecutionDenied, match="session_cleanup_pending"):
        await web.prepare_session_deletion("session")
    lease = await web.control.recovery_lease(value.identity)
    assert lease.stop and not lease.released


async def test_pending_publication_is_discarded_without_touching_other_session(case: Case, web):
    prepared = await case.prepare()
    own = operation(prepared.scope.identity)
    other = own.model_copy(update={
        "operation_id": "sandbox-" + "2" * 32, "session_id": "other",
    })
    web._lifecycle.records = {r.operation_id: r for r in (own, other)}
    await web.prepare_session_deletion("session")
    assert web._lifecycle.records[own.operation_id].status == "discarded"
    assert web._lifecycle.records[other.operation_id].status == "awaiting_approval"
    assert web._lifecycle.discarded == [own.operation_id]


async def test_active_publication_blocks_deletion_and_keeps_its_record(case: Case, web):
    prepared = await case.prepare()
    record = operation(prepared.scope.identity, "publishing")
    web._lifecycle.records[record.operation_id] = record
    with pytest.raises(ExecutionDenied, match="session_cleanup_pending"):
        await web.prepare_session_deletion("session")
    assert web._lifecycle.records[record.operation_id].status == "publishing"
    assert not web._lifecycle.discarded


async def test_lifecycle_discard_failure_cannot_become_success(case: Case, web):
    prepared = await case.prepare()
    record = operation(prepared.scope.identity)
    web._lifecycle.records[record.operation_id] = record
    web._lifecycle.discard = AsyncMock(side_effect=RuntimeError("private destroy failure"))
    with pytest.raises(ExecutionDenied, match="session_cleanup_pending") as caught:
        await web.prepare_session_deletion("session")
    assert "private" not in str(caught.value)
    assert web._lifecycle.records[record.operation_id] == record


async def test_unprepared_task_approval_and_wrapper_are_closed_only_for_target(case: Case, web):
    own = SimpleNamespace(
        request=SimpleNamespace(session_id="session", request_id="own-request"),
        prepared=None,
        close=AsyncMock(),
    )
    other = SimpleNamespace(
        request=SimpleNamespace(session_id="other", request_id="other-request"),
        prepared=None,
        close=AsyncMock(),
    )
    web._tasks = {"own-request": own, "other-request": other}
    await web.prepare_session_deletion("session")
    own.close.assert_awaited_once()
    other.close.assert_not_awaited()
    assert web._tasks == {"other-request": other}
    assert all(
        call.args == ("own-request",) for call in web._approvals.cancel_request.await_args_list
    )


async def test_legacy_operation_without_cleanup_ledger_fails_closed(case: Case, web):
    identity = scope().identity.model_copy(update={"operation_id": "sandbox-" + "3" * 32})
    record = operation(identity, "ready")
    web._lifecycle.records[record.operation_id] = record
    with pytest.raises(ExecutionDenied, match="session_cleanup_pending"):
        await web.prepare_session_deletion("session")
    assert not web._lifecycle.discarded
    assert web._lifecycle.records[record.operation_id] == record


async def test_backend_error_is_redacted_and_retained_for_retry(case: Case, web):
    prepared = await case.prepare()
    original = web.runtime.cancel
    web.runtime.cancel = AsyncMock(side_effect=RuntimeError("secret backend stderr"))
    try:
        with pytest.raises(ExecutionDenied, match="session_cleanup_pending") as caught:
            await web.prepare_session_deletion("session")
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert (await case.store.get(prepared.scope.identity)).state == "pending"
    finally:
        web.runtime.cancel = original


async def test_lifecycle_active_records_projection_is_session_scoped():
    own = operation(scope().identity.model_copy(update={"operation_id": "sandbox-" + "4" * 32}))
    other = own.model_copy(update={"session_id": "other"})
    lifecycle = ManagedSandboxLifecycle.__new__(ManagedSandboxLifecycle)
    lifecycle._store = SimpleNamespace(active_records=AsyncMock(return_value=(own, other)))
    assert await lifecycle.active_records(session_id="session") == (own,)
    assert await lifecycle.active_records() == (own, other)

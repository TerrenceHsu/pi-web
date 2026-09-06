"""Stage 2B input, task-bound operation, and real PlanStore transaction contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from pydantic import ValidationError

from coding_agent_app.execution.context import bind_execution_context
from coding_agent_app.execution.models import ExecutionContext, ExecutionDenied, digest_json
from coding_agent_app.execution.operation import GrantedOperationAccess
from coding_agent_app.execution.service import ExecutionService
from coding_agent_app.execution.store import ExecutionStore
from coding_agent_app.planning.models import PlanSpec, PlanTaskSpec
from coding_agent_app.planning.store import PlanConflictError, PlanStore
from coding_sandbox import SandboxOperation, SandboxWorkspaceError
from coding_sandbox.bash import BASH_ARGV, CONTROL_SCRIPT, BashRequest
from coding_sandbox.docker_transport import DockerReply
from coding_sandbox.errors import SandboxError
from coding_sandbox.models import SandboxCommand
from coding_sandbox.validation import parse_sandbox_validation_config
from pi_agent_core_py.tools.bash import RunBashTool
from tests.test_execution_grants import active, scope
from tests.test_local_docker_backend import MemoryDocker, backend


@pytest.mark.parametrize(
    "values",
    [
        {"script": ""},
        {"script": "a\0b"},
        {"script": "\ud800"},
        {"script": "中" * 6000},
        {"script": 42},
        {"cwd": "../outside"},
        {"cwd": "C:/workspace"},
        {"cwd": "/workspace"},
        {"cwd": "a\\b"},
        {"cwd": "a//b"},
        {"cwd": "a\nb"},
        {"cwd": "\ud800"},
        {"timeout_seconds": "60"},
        {"timeout_seconds": True},
        {"timeout_seconds": 301},
        {"env": {"BASH_ENV": "evil"}},
        {"approved": True},
        {"role": "executor"},
        {"task_id": "forged"},
        {"backend": "host"},
        {"mounts": ["D:/"]},
    ],
)
def test_strict_script_input_cannot_carry_authority(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BashRequest.model_validate({"script": "true"} | values)


def test_script_is_hashed_without_rewriting_quotes_or_line_endings() -> None:
    script = "printf '%s\\n' '中文 $HOME' | cat\r\n# unchanged\n"
    request = BashRequest(script=script)
    assert request.script == script
    assert request.sha256 == hashlib.sha256(script.encode()).hexdigest()
    assert BashRequest(script="x" * 16384).script == "x" * 16384


async def test_script_delivery_uses_fixed_control_identity_and_rejects_replay() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    command = SandboxCommand(
        command_id="bash", argv=BASH_ARGV, timeout_seconds=5, max_output_bytes=1024
    )
    script = "printf '%s\\n' '中文 $HOME'\n"
    assert (await service.execute_bash(handle, command, script=script)).succeeded
    assert driver.files[CONTROL_SCRIPT] == script.encode()
    calls = [call for call in driver.calls if call[:2] == ("container", "exec")]
    prepare = next(call for call in calls if call[-1] == "prepare_bash")
    run = next(call for call in calls if call[-1] == "run")
    assert "--user=10000:10000" in prepare and "--user=10001:10001" in run
    assert calls.index(prepare) < calls.index(run)
    before = len(driver.calls)
    with pytest.raises(SandboxError):
        await service.execute_bash(handle, command, script="changed")
    with pytest.raises(SandboxError, match="configuration"):
        await service.execute_bash(
            handle, command.model_copy(update={"argv": ("bash", "-c", "true")}), script="true"
        )
    assert len(driver.calls) == before
    await service.destroy(handle)


async def test_cancel_during_script_preparation_never_starts_task_code() -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    driver.cancel_on_prepare = asyncio.Event()
    result = await service.execute_bash(
        handle,
        SandboxCommand(
            command_id="cancel-prepare", argv=BASH_ARGV, timeout_seconds=5, max_output_bytes=1024
        ),
        script="must-not-execute",
        signal=driver.cancel_on_prepare,
    )
    assert result.termination_reason == "cancelled"
    assert not driver.run_requests and not driver.exists


@pytest.fixture
async def guarded(
    tmp_path: Path,
) -> AsyncIterator[tuple[SandboxOperation, MemoryDocker, ExecutionStore, ExecutionContext]]:
    driver, service, spec = backend()
    handle = await service.create(spec)
    value = scope(identity=scope().identity.model_copy(update={"operation_id": spec.operation_id}))
    async with aiosqlite.connect(tmp_path / "grants.db", isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        call = await active(store, value)

        async def check(_scope: object) -> bool:
            return True

        async def cleanup(_identity: object) -> bool:
            await service.destroy(handle)
            return not driver.exists

        operation = SandboxOperation(
            backend=service,
            handle=handle,
            workdir="/workspace",
            limits=spec.limits,
            staging_root=tmp_path / "stage",
            execution_guard=GrantedOperationAccess(
                ExecutionService(store, scope_check=check, cleanup=cleanup), value
            ),
        )
        yield operation, driver, store, call
        await service.destroy(handle)


@pytest.mark.parametrize("action", ["run", "bash", "write", "delete", "patch", "validate", "read"])
async def test_missing_context_denies_before_any_backend_effect(guarded: Any, action: str) -> None:
    operation, driver, store, call = guarded
    before = len(driver.calls)
    with pytest.raises(ExecutionDenied, match="grant_required"):
        if action == "run":
            await operation.run(("true",))
        elif action == "bash":
            await operation.run_bash(BashRequest(script="true"))
        elif action == "write":
            await operation.write_file("test.txt", "test")
        elif action == "delete":
            await operation.delete_file("test.txt")
        elif action == "patch":
            await operation.apply_patch("--- /dev/null\n+++ b/test.txt\n@@ -0,0 +1 @@\n+test\n")
        elif action == "validate":
            await operation.validate_required_checks()
        else:
            await operation.list_files()
    assert len(driver.calls) == before
    assert (await store.get(call.identity)).commands_used == 0


@pytest.mark.parametrize("role", ["planner", "verifier", "read_only"])
async def test_readonly_role_cannot_execute_or_edit(guarded: Any, role: str) -> None:
    operation, driver, _store, call = guarded
    before = len(driver.calls)
    with bind_execution_context(call.model_copy(update={"role": role})):
        for action in (lambda: operation.run(("true",)), lambda: operation.write_file("x", "x")):
            with pytest.raises(ExecutionDenied, match="execution_role_denied"):
                await action()
    assert len(driver.calls) == before


@pytest.mark.parametrize(
    "field", ["account_id", "session_id", "request_id", "workspace_id", "task_id", "operation_id"]
)
async def test_operation_rejects_other_task_identity(guarded: Any, field: str) -> None:
    operation, driver, _store, call = guarded
    before = len(driver.calls)
    forged = call.model_copy(update={"identity": call.identity.model_copy(update={field: "other"})})
    with (
        bind_execution_context(forged),
        pytest.raises(ExecutionDenied, match="grant_scope_mismatch"),
    ):
        await operation.run(("true",))
    assert len(driver.calls) == before


async def test_known_failure_bash_argv_and_edit_share_one_task_budget(guarded: Any) -> None:
    operation, driver, store, call = guarded
    with bind_execution_context(call):
        driver.command_reply = DockerReply(3, stderr=b"test failed")
        assert (await operation.run(("false",))).exit_code == 3
        driver.command_reply = DockerReply(0)
        assert (await operation.run_bash(BashRequest(script="true"))).succeeded
        payload = {
            "path": "test.txt",
            "size": 4,
            "sha256": hashlib.sha256(b"test").hexdigest(),
            "created": True,
        }
        driver.command_reply = DockerReply(0, json.dumps({"ok": True, "result": payload}).encode())
        assert (await operation.write_file("test.txt", "test")).created
    grant = await store.get(call.identity)
    assert grant.state == "active" and grant.commands_used == grant.copy_revision == 3
    assert sum(args[:2] == ("container", "create") for args in driver.calls) == 1


async def test_verifier_reads_use_isolated_fixed_helper_without_charging_command(
    guarded: Any,
) -> None:
    operation, driver, store, call = guarded
    driver.command_reply = DockerReply(0, b'{"ok":true,"result":{"root":".","files":[]}}')
    with bind_execution_context(call.model_copy(update={"role": "verifier"})):
        assert not (await operation.list_files()).files
    assert (await store.get(call.identity)).commands_used == 0
    assert driver.run_requests[-1]["argv"][:5] == ["python3", "-I", "-S", "-B", "-c"]


async def test_local_operation_without_guard_is_closed_and_tool_reports_stable_error(
    tmp_path: Path,
) -> None:
    driver, service, spec = backend()
    handle = await service.create(spec)
    operation = SandboxOperation(
        backend=service,
        handle=handle,
        workdir="/workspace",
        limits=spec.limits,
        staging_root=tmp_path,
    )
    before = len(driver.calls)
    with pytest.raises(SandboxWorkspaceError) as error:
        await operation.run(("true",))
    assert error.value.code == "execution_approval_required"
    tool = RunBashTool(lambda: operation)
    result = await tool.execute("call", {"script": "true"})
    assert result.is_error and result.details == {"error_code": "execution_approval_required"}
    invalid = await tool.execute("call", {"script": "true", "approved": True})
    assert invalid.details == {"error_code": "invalid_bash_request"}
    assert len(driver.calls) == before
    await service.destroy(handle)


@pytest.fixture
async def plans(
    tmp_path: Path,
) -> AsyncIterator[tuple[aiosqlite.Connection, PlanStore, ExecutionStore, Any]]:
    async with aiosqlite.connect(tmp_path / "plans.db", isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        await db.execute("INSERT INTO sessions VALUES('session')")
        plans, grants = PlanStore(db), ExecutionStore(db)
        await plans.init()
        await grants.init()
        spec = PlanSpec(
            goal="build",
            summary="Implement and verify",
            tasks=(
                PlanTaskSpec(
                    id="build", title="build", objective="build", acceptance_criteria=("works",)
                ),
            ),
        )
        run = await plans.create_run("session", "request", "build")
        await plans.save_plan(run.id, spec)
        value = scope(
            kind="plan",
            plan_id=run.id,
            plan_version=1,
            plan_sha256=digest_json(spec.model_dump(mode="json")),
            request_sha256=hashlib.sha256(b"build").hexdigest(),
        )
        yield db, plans, grants, value


async def test_real_plan_and_grant_approval_commit_once_together(plans: Any) -> None:
    db, plan_store, grants, value = plans
    await grants.prepare(value, plan_binding=plan_store.bind_execution)
    with pytest.raises(PlanConflictError, match="atomic"):
        await plan_store.approve(value.plan_id)
    await grants.approve(
        value.identity,
        expected_scope_sha256=value.sha256,
        plan_approval=plan_store.approve_execution,
    )
    run = await plan_store.get_run(value.plan_id)
    assert run.status == "executing" and run.sandbox_operation_id == value.identity.operation_id
    assert (await grants.get(value.identity)).state == "approved"
    with pytest.raises(PlanConflictError, match="cannot switch"):
        await plan_store.set_sandbox_operation(value.plan_id, "other-operation")
    with pytest.raises(ExecutionDenied, match="approval_already_decided"):
        await grants.approve(
            value.identity,
            expected_scope_sha256=value.sha256,
            plan_approval=plan_store.approve_execution,
        )
    async with db.execute(
        "SELECT payload_json FROM plan_events WHERE event_type='plan_approved'"
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0])["execution_authorized"] is True


async def test_real_plan_approval_event_and_grant_roll_back_on_failure(plans: Any) -> None:
    db, plan_store, grants, value = plans
    await grants.prepare(value, plan_binding=plan_store.bind_execution)

    async def fail(connection: aiosqlite.Connection, saved: Any) -> None:
        await plan_store.approve_execution(connection, saved)
        raise RuntimeError("injected before grant commit")

    with pytest.raises(RuntimeError, match="injected"):
        await grants.approve(value.identity, expected_scope_sha256=value.sha256, plan_approval=fail)
    assert (await plan_store.get_run(value.plan_id)).status == "awaiting_plan_approval"
    assert (await grants.get(value.identity)).state == "pending"
    async with db.execute(
        "SELECT count(*) FROM plan_events WHERE event_type='plan_approved'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.parametrize("change", ["plan_version", "plan_sha256", "request_sha256", "session_id"])
async def test_plan_binding_rejects_stale_scope_and_rolls_back_grant(
    plans: Any, change: str
) -> None:
    db, plan_store, grants, value = plans
    if change == "session_id":
        value = value.model_copy(
            update={"identity": value.identity.model_copy(update={change: "other"})}
        )
    else:
        value = value.model_copy(update={change: 2 if change == "plan_version" else "b" * 64})
    with pytest.raises(ExecutionDenied, match="plan_version_stale"):
        await grants.prepare(value, plan_binding=plan_store.bind_execution)
    with pytest.raises(ExecutionDenied, match="grant_required"):
        await grants.get(value.identity)
    async with db.execute("SELECT count(*) FROM plan_execution_bindings") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_plan_callbacks_require_same_transaction_and_binding(plans: Any) -> None:
    db, plan_store, grants, value = plans
    for callback in (plan_store.bind_execution, plan_store.approve_execution):
        with pytest.raises(ExecutionDenied, match="transaction_required"):
            await callback(db, value)
    await grants.prepare(value)
    with pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
        await grants.approve(
            value.identity,
            expected_scope_sha256=value.sha256,
            plan_approval=plan_store.approve_execution,
        )
    assert (await grants.get(value.identity)).state == "pending"


async def test_validation_uses_same_grant_and_fixed_check_not_helper_budget(guarded: Any) -> None:
    operation, driver, store, call = guarded
    config = b'version=1\n[[required_checks]]\nid="check"\nargv=["true"]\ntimeout_seconds=5\n'
    operation._validation_plan = parse_sandbox_validation_config(config)

    def reply(payload: dict[str, object]) -> DockerReply:
        return DockerReply(0, json.dumps({"ok": True, "result": payload}).encode())

    read = reply(
        {
            "path": ".pi-agent/sandbox.toml",
            "content": config.decode(),
            "size": len(config),
            "sha256": hashlib.sha256(config).hexdigest(),
        }
    )
    fingerprint = reply({"file_count": 1, "total_bytes": len(config), "sha256": "a" * 64})
    driver.command_replies = [read, fingerprint, DockerReply(0), fingerprint, read]
    with bind_execution_context(call):
        evidence = await operation.validate_required_checks()
    assert evidence.passed
    assert (await store.get(call.identity)).commands_used == 1
    assert driver.run_requests[2]["argv"] == ["true"]
    for request in driver.run_requests[:2] + driver.run_requests[3:]:
        assert request["argv"][:5] == ["python3", "-I", "-S", "-B", "-c"]


async def test_plan_changed_after_binding_cannot_be_approved(plans: Any) -> None:
    db, plan_store, grants, value = plans
    await grants.prepare(value, plan_binding=plan_store.bind_execution)
    await db.execute("UPDATE plan_runs SET plan_version=2 WHERE id=?", (value.plan_id,))
    with pytest.raises(ExecutionDenied, match="plan_version_stale"):
        await grants.approve(
            value.identity,
            expected_scope_sha256=value.sha256,
            plan_approval=plan_store.approve_execution,
        )
    assert (await grants.get(value.identity)).state == "pending"


async def test_expired_operation_entry_reconciles_before_allowing_helpers(tmp_path: Path) -> None:
    clock = [1000]
    async with aiosqlite.connect(tmp_path / "expiry.db", isolation_level=None) as db:
        store = ExecutionStore(db, clock_ms=lambda: clock[0])
        await store.init()
        call = await active(store)
        cleaned: list[object] = []

        async def check(_scope: object) -> bool:
            return True

        async def cleanup(identity: object) -> bool:
            cleaned.append(identity)
            return True

        clock[0] += 1_800_000
        service = ExecutionService(store, scope_check=check, cleanup=cleanup)
        with pytest.raises(ExecutionDenied, match="grant_expired"):
            await service.check(call)
        assert cleaned == [call.identity]
        grant = await store.get(call.identity)
        assert grant.state == "expired" and not grant.cleanup_pending


@pytest.mark.parametrize("field", ["runtime_id", "provider"])
async def test_handle_runtime_or_backend_cannot_change_under_grant(
    guarded: Any, field: str
) -> None:
    operation, driver, _store, call = guarded
    operation._handle = operation._handle.model_copy(update={field: "e2b"})
    before = len(driver.calls)
    with bind_execution_context(call), pytest.raises(ExecutionDenied, match="grant_scope_mismatch"):
        await operation.run(("true",))
    assert len(driver.calls) == before


async def test_run_bash_never_falls_back_to_other_backend(guarded: Any) -> None:
    operation, driver, _store, call = guarded
    operation._handle = operation._handle.model_copy(update={"provider": "e2b"})
    before = len(driver.calls)
    with bind_execution_context(call):
        result = await RunBashTool(lambda: operation).execute("call", {"script": "true"})
    assert result.is_error and result.details == {"error_code": "bash_unavailable"}
    assert len(driver.calls) == before


async def test_service_read_check_requires_verified_runtime(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "startup.db", isolation_level=None) as db:
        store = ExecutionStore(db)
        await store.init()
        value = scope()
        await store.prepare(value)
        await store.approve(value.identity, expected_scope_sha256=value.sha256)
        call = ExecutionContext(identity=value.identity, scope_sha256=value.sha256, role="executor")
        await store.activate(call, current_scope=value)

        async def check(_value: object) -> bool:
            return True

        service = ExecutionService(store, scope_check=check, cleanup=check)
        with pytest.raises(ExecutionDenied, match="cleanup_pending"):
            await service.check(call)

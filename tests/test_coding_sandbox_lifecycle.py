"""Offline lifecycle, persistence, recovery and refresh contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coding_agent_app.planning.orchestrator import PlanOrchestrator
from coding_agent_app.planning.store import PlanStore
from coding_sandbox import (
    HMACSHA256ArtifactSigner,
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    SandboxCommandResult,
    SandboxDiffEntry,
    SandboxDiffResult,
    SandboxFileEntry,
    SandboxLifecycleError,
    SandboxOperationStoreConflictError,
    SQLiteSandboxOperationStore,
)
from coding_sandbox.admin import SandboxAdminConfig, SandboxConfigRecord
from coding_sandbox.fake import FakeSandboxBackend
from coding_sandbox.lifecycle import DEFAULT_VALIDATION_CONFIG
from pi_agent_core_py import DoneEvent, FakeClient, ToolCall, ToolCallEvent, ToolDef
from pi_agent_core_py.session_sqlite import SQLiteSessionStore
from pi_agent_core_py.web.coding_sandbox.automation import (
    CodingSandboxAutomation,
    CodingSandboxAutomationError,
    CodingToolBootstrapModelClient,
)
from pi_agent_core_py.web.coding_sandbox.workspace import (
    WorkspaceSandboxArtifactPublisher,
    WorkspaceSandboxBaselineProvider,
)
from pi_agent_core_py.web.files import (
    WorkspacePublishChange,
    WorkspaceStore,
    WorkspaceVersionConflictError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["awaiting_approval", "publishing", "publish_conflict", "published"]
)
async def test_execution_release_preserves_artifact_when_publication_races(status):
    """Request completion may overlap the separately approved publication action."""
    diff = SandboxDiffResult(entries=())
    record = SimpleNamespace(status=status, allowed_actions=("cancel",), diff=diff)
    operation = SimpleNamespace(output_artifact=object(), diff=AsyncMock())
    live = SimpleNamespace(
        operation=operation, execution_released=False, close_runtime=AsyncMock(),
    )
    lifecycle = SimpleNamespace(
        get=AsyncMock(return_value=record), _live={"op": live}, cancel=AsyncMock(),
    )
    await ManagedSandboxLifecycle.release_execution(lifecycle, "op")
    await ManagedSandboxLifecycle.release_execution(lifecycle, "op")
    live.close_runtime.assert_awaited_once()
    lifecycle.cancel.assert_not_awaited()
    assert live.execution_released
    assert await ManagedSandboxLifecycle.diff(lifecycle, "op") is diff
    operation.diff.assert_not_awaited()


def _helper(result: dict[str, object]) -> SandboxCommandResult:
    return SandboxCommandResult(
        command_id="queued",
        stdout=json.dumps({"ok": True, "result": result}),
        started_at_ms=10,
        finished_at_ms=11,
    )


def _fingerprint(entries: tuple[SandboxFileEntry, ...]) -> dict[str, object]:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.path):
        digest.update(entry.path.encode())
        digest.update(b"\0")
        digest.update(str(entry.size).encode())
        digest.update(b"\0")
        digest.update(entry.sha256.encode())
        digest.update(b"\n")
    return {
        "file_count": len(entries),
        "total_bytes": sum(entry.size for entry in entries),
        "sha256": digest.hexdigest(),
    }


def _record(*, operation_id: str, status: str = "creating") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "session_id": "session-one",
        "status": status,
        "config_revision": 1,
        "created_at_ms": 100,
        "updated_at_ms": 100,
    }


def _workspace_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class _AutomationLifecycle:
    def __init__(
        self,
        *,
        validation_passes: bool = True,
        has_changes: bool = True,
    ) -> None:
        self.validation_passes = validation_passes
        self.has_changes = has_changes
        self.record: ManagedSandboxOperationRecord | None = None
        self.calls: list[str] = []

    def _set(self, status: str) -> ManagedSandboxOperationRecord:
        assert status in {
            "creating",
            "ready",
            "validating",
            "validated",
            "validation_failed",
            "freezing",
            "awaiting_approval",
            "cancelled",
            "discarded",
        }
        diff = SandboxDiffResult(
            entries=(
                (
                    SandboxDiffEntry(
                        path="scripts/main.py",
                        status="added",
                        after_sha256="a" * 64,
                    ),
                )
                if self.has_changes
                else ()
            ),
        )
        self.record = ManagedSandboxOperationRecord.model_construct(
            operation_id="sandbox-" + "c" * 32,
            session_id="session-one",
            status=status,
            config_revision=1,
            created_at_ms=100,
            updated_at_ms=101,
            workspace_revision=1,
            validation=(object() if status in {"validated", "validation_failed"} else None),
            diff=(diff if status in {"validated", "validation_failed"} else None),
            artifact_id=("artifact-" + "d" * 32 if status == "awaiting_approval" else None),
            changed_paths=(
                ("scripts/main.py",)
                if status == "awaiting_approval" and self.has_changes
                else ()
            ),
        )
        return self.record

    async def latest_for_session(
        self, _session_id: str
    ) -> ManagedSandboxOperationRecord | None:
        return self.record

    async def start(self, _session_id: str) -> ManagedSandboxOperationRecord:
        self.calls.append("start")
        return self._set("creating")

    async def get(self, _operation_id: str) -> ManagedSandboxOperationRecord:
        assert self.record is not None
        if self.record.status == "creating":
            return self._set("ready")
        if self.record.status == "validating":
            return self._set("validated" if self.validation_passes else "validation_failed")
        if self.record.status == "freezing":
            return self._set("awaiting_approval")
        return self.record

    async def validate(self, _operation_id: str) -> ManagedSandboxOperationRecord:
        self.calls.append("validate")
        return self._set("validating")

    async def prepare_publish(self, _operation_id: str) -> ManagedSandboxOperationRecord:
        self.calls.append("prepare_publish")
        return self._set("freezing")

    async def diff(self, _operation_id: str) -> SandboxDiffResult:
        return SandboxDiffResult(
            entries=(
                (
                    SandboxDiffEntry(
                        path="scripts/main.py",
                        status="added",
                        after_sha256="a" * 64,
                    ),
                )
                if self.has_changes
                else ()
            )
        )

    async def cancel(self, _operation_id: str) -> ManagedSandboxOperationRecord:
        self.calls.append("cancel")
        return self._set("cancelled")

    async def discard(self, _operation_id: str) -> ManagedSandboxOperationRecord:
        self.calls.append("discard")
        return self._set("discarded")


@pytest.mark.asyncio
async def test_automated_coding_creates_validates_and_stops_for_approval() -> None:
    lifecycle = _AutomationLifecycle()
    automation = CodingSandboxAutomation(
        lifecycle,  # type: ignore[arg-type]
        wait_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    ready = await automation.prepare("session-one")
    result = await automation.validate_and_freeze(ready.operation_id)

    assert result.status == "awaiting_approval"
    assert result.public()["approval_required"] is True
    assert lifecycle.calls == ["start", "validate", "prepare_publish"]


@pytest.mark.asyncio
async def test_automated_coding_validation_failure_never_freezes() -> None:
    lifecycle = _AutomationLifecycle(validation_passes=False)
    automation = CodingSandboxAutomation(
        lifecycle,  # type: ignore[arg-type]
        wait_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    ready = await automation.prepare("session-one")
    with pytest.raises(CodingSandboxAutomationError) as raised:
        await automation.validate_and_freeze(ready.operation_id)

    assert raised.value.code == "coding_validation_failed"
    assert lifecycle.record is not None and lifecycle.record.status == "validation_failed"
    assert lifecycle.calls == ["start", "validate"]


@pytest.mark.asyncio
async def test_automated_coding_replaces_legacy_empty_artifact_and_rejects_no_changes() -> None:
    lifecycle = _AutomationLifecycle(has_changes=False)
    lifecycle._set("awaiting_approval")
    automation = CodingSandboxAutomation(
        lifecycle,  # type: ignore[arg-type]
        wait_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    ready = await automation.prepare("session-one")
    with pytest.raises(CodingSandboxAutomationError) as raised:
        await automation.validate_and_freeze(ready.operation_id)

    assert raised.value.code == "coding_no_changes"
    assert lifecycle.record is not None and lifecycle.record.status == "validated"
    assert lifecycle.calls == ["discard", "start", "validate"]


@pytest.mark.asyncio
async def test_automated_coding_retries_once_when_first_attempt_has_no_changes() -> None:
    lifecycle = _AutomationLifecycle(has_changes=False)
    automation = CodingSandboxAutomation(
        lifecycle,  # type: ignore[arg-type]
        wait_timeout_seconds=1,
        poll_interval_seconds=0,
    )

    ready = await automation.prepare("session-one")
    attempts: list[bool] = []

    async def run_attempt(repair: bool) -> str:
        attempts.append(repair)
        if repair:
            lifecycle.has_changes = True
        return "repaired" if repair else "empty"

    result = await automation.run_with_no_change_retry(
        ready.operation_id,
        run_attempt,
    )

    assert result == "repaired"
    assert attempts == [False, True]

    delegate = FakeClient(
        [
            [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="repair-list",
                        name="coding_list_files",
                        arguments={},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="repair-write",
                        name="coding_write_file",
                        arguments={"path": "scripts/main.py", "content": "pass\n"},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [DoneEvent(stop_reason="stop")],
        ]
    )
    repair_client = CodingToolBootstrapModelClient(delegate, list_first=True)
    tool_defs = [
        ToolDef(name=name, label=name, description=name)
        for name in (
            "coding_list_files",
            "coding_write_file",
            "coding_apply_patch",
            "coding_run",
        )
    ]
    _ = [
        event
        async for event in repair_client.stream(
            system_prompt="list",
            messages=[],
            tools=tool_defs,
        )
    ]
    _ = [
        event
        async for event in repair_client.stream(
            system_prompt="repair",
            messages=[],
            tools=tool_defs,
        )
    ]
    _ = [
        event
        async for event in repair_client.stream(
            system_prompt="continue",
            messages=[],
            tools=tool_defs,
        )
    ]

    assert delegate.all_tools_calls[0] is not None
    assert [tool.name for tool in delegate.all_tools_calls[0]] == [
        "coding_list_files",
    ]
    assert delegate.all_tools_calls[1] is not None
    assert [tool.name for tool in delegate.all_tools_calls[1]] == [
        "coding_write_file",
        "coding_apply_patch",
    ]
    assert delegate.all_tools_calls[2] is not None
    assert [tool.name for tool in delegate.all_tools_calls[2]] == [
        tool.name for tool in tool_defs
    ]


def _plan_role_call(call_id: str, name: str, arguments: dict[str, object]) -> list[object]:
    return [
        ToolCallEvent(
            tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
        ),
        DoneEvent(stop_reason="tool_use"),
    ]


@pytest.mark.asyncio
async def test_plan_mode_runs_planner_executor_verifier_then_freezes(
    tmp_path: Path,
) -> None:
    session_store = SQLiteSessionStore(tmp_path / "plan-success.db")
    await session_store.init()
    session = await session_store.create_session(title="Plan success")
    assert session_store.connection is not None
    plan_store = PlanStore(session_store.connection)
    await plan_store.init()
    lifecycle = _AutomationLifecycle()
    client = FakeClient(
        [
            _plan_role_call(
                "planner",
                "plan_submit",
                {
                    "goal": "add greeting",
                    "summary": "Implement and verify a greeting.",
                    "tasks": [
                        {
                            "id": "implement",
                            "title": "Implement greeting",
                            "objective": "Add the requested greeting.",
                            "dependencies": [],
                            "acceptance_criteria": ["Greeting is present."],
                            "allowed_paths": ["scripts/greet.py"],
                        }
                    ],
                },
            ),
            _plan_role_call(
                "executor",
                "plan_task_complete",
                {
                    "summary": "Greeting implemented.",
                    "changed_paths": ["scripts/greet.py"],
                    "validation_summary": "Direct check passed.",
                },
            ),
            _plan_role_call(
                "verifier",
                "plan_verdict",
                {
                    "passed": True,
                    "reason": "The greeting meets the acceptance criterion.",
                    "suggestions": [],
                    "classification": None,
                },
            ),
        ]
    )

    async def approve(run_id: str) -> None:
        await plan_store.approve(run_id)

    observed: list[str] = []

    async def notify(event_type: str, _run: object) -> None:
        observed.append(event_type)

    orchestrator = PlanOrchestrator(
        store=plan_store,
        client=client,
        automation=CodingSandboxAutomation(
            lifecycle,  # type: ignore[arg-type]
            wait_timeout_seconds=1,
            poll_interval_seconds=0,
        ),
        read_tools=[],
        coding_tools=[],
        wait_for_approval=approve,
        notify=notify,
        cancelled=lambda: False,
    )
    try:
        result = await orchestrator.run(
            session_id=session.id,
            request_id="request-success",
            goal="add greeting",
        )
        assert result.run.status == "awaiting_artifact_approval"
        assert result.run.tasks[0].status == "passed"
        assert result.run.tasks[0].verification is not None
        assert result.run.tasks[0].verification.passed is True
        assert result.sandbox is not None and result.sandbox["approval_required"] is True
        assert lifecycle.calls == ["start", "validate", "prepare_publish"]
        assert "plan_task_verified" in observed
    finally:
        await session_store.close()


@pytest.mark.asyncio
async def test_plan_mode_verifier_rejection_exposes_feedback_and_never_freezes(
    tmp_path: Path,
) -> None:
    session_store = SQLiteSessionStore(tmp_path / "plan-rejected.db")
    await session_store.init()
    session = await session_store.create_session(title="Plan rejected")
    assert session_store.connection is not None
    plan_store = PlanStore(session_store.connection)
    await plan_store.init()
    lifecycle = _AutomationLifecycle()
    client = FakeClient(
        [
            _plan_role_call(
                "planner",
                "plan_submit",
                {
                    "goal": "fix output",
                    "summary": "Fix and verify output.",
                    "tasks": [
                        {
                            "id": "fix",
                            "title": "Fix output",
                            "objective": "Produce the required output.",
                            "acceptance_criteria": ["Output equals expected value."],
                        }
                    ],
                },
            ),
            _plan_role_call(
                "executor",
                "plan_task_complete",
                {
                    "summary": "Output changed.",
                    "changed_paths": ["scripts/main.py"],
                    "validation_summary": "No authoritative expected value was available.",
                },
            ),
            _plan_role_call(
                "verifier",
                "plan_verdict",
                {
                    "passed": False,
                    "reason": "The expected value is not specified.",
                    "suggestions": ["Ask the user for the expected value."],
                    "classification": "user_input_required",
                },
            ),
        ]
    )

    async def approve(run_id: str) -> None:
        await plan_store.approve(run_id)

    async def notify(_event_type: str, _run: object) -> None:
        return None

    orchestrator = PlanOrchestrator(
        store=plan_store,
        client=client,
        automation=CodingSandboxAutomation(
            lifecycle,  # type: ignore[arg-type]
            wait_timeout_seconds=1,
            poll_interval_seconds=0,
        ),
        read_tools=[],
        coding_tools=[],
        wait_for_approval=approve,
        notify=notify,
        cancelled=lambda: False,
    )
    try:
        result = await orchestrator.run(
            session_id=session.id,
            request_id="request-rejected",
            goal="fix output",
        )
        task = result.run.tasks[0]
        assert result.run.status == "blocked"
        assert task.status == "failed"
        assert task.verification is not None
        assert task.verification.reason == "The expected value is not specified."
        assert task.verification.suggestions == ("Ask the user for the expected value.",)
        assert lifecycle.calls == ["start"]
    finally:
        await session_store.close()


async def _wait_for_status(
    lifecycle: ManagedSandboxLifecycle,
    operation_id: str,
    expected: str,
) -> ManagedSandboxOperationRecord:
    for _ in range(100):
        record = await lifecycle.get(operation_id)
        if record.status == expected:
            return record
        if record.terminal:
            await asyncio.sleep(0.05)
            page = await lifecycle.events(operation_id)
            detail = page.events[-1].payload if page.events else {}
            pytest.fail(f"operation became {record.status}: {record.error_code}; {detail}")
        await asyncio.sleep(0.01)
    pytest.fail(f"operation did not reach {expected}")


@pytest.mark.asyncio
async def test_operation_store_persists_records_and_bounded_replay(tmp_path: Path) -> None:
    database = tmp_path / "operations.db"
    store = await SQLiteSandboxOperationStore.open(database, now_ms=lambda: 200)
    record = ManagedSandboxOperationRecord.model_validate(
        _record(operation_id="sandbox-" + "a" * 32)
    )
    await store.create(record)
    event = await store.append_event(
        record.operation_id,
        record.session_id,
        "sandbox_operation_creating",
        {"status": "creating"},
    )
    ready = ManagedSandboxOperationRecord.model_validate(
        {**record.model_dump(mode="python"), "status": "ready", "updated_at_ms": 201}
    )
    assert record.allowed_actions == ("cancel", "discard")
    assert "ready" in record.allowed_transitions
    assert await store.compare_and_swap(record, ready) == ready
    with pytest.raises(SandboxOperationStoreConflictError):
        await store.compare_and_swap(record, ready)
    await store.close()

    reopened = await SQLiteSandboxOperationStore.open(database, now_ms=lambda: 300)
    try:
        assert await reopened.latest_for_session(record.session_id) == ready
        assert ready.allowed_actions == ("validate", "cancel", "discard")
        page = await reopened.list_events(record.operation_id)
        assert page.events == (event,)
        assert page.first_available_sequence == 1
        assert page.last_available_sequence == 1
        assert page.gap is False
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_startup_recovery_marks_state_without_replaying_work(tmp_path: Path) -> None:
    store = await SQLiteSandboxOperationStore.open(tmp_path / "recovery.db")
    record = ManagedSandboxOperationRecord.model_validate(
        _record(operation_id="sandbox-" + "b" * 32, status="validating")
    )
    await store.create(record)
    backend_calls = 0

    async def config_provider() -> SandboxConfigRecord:
        raise AssertionError("recovery must not resolve configuration")

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("recovery must not create a Sandbox")

    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=tmp_path / "projects",
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
    )
    try:
        assert await lifecycle.recover_startup() == (record.operation_id,)
        recovered = await lifecycle.get(record.operation_id)
        assert recovered.status == "interrupted"
        assert recovered.error_code == "server_restarted"
        assert backend_calls == 0
        page = await lifecycle.events(record.operation_id)
        assert page.events[-1].event_type == "sandbox_operation_interrupted"
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_lifecycle_creates_seeds_validates_and_cancels(tmp_path: Path) -> None:
    workspace_store = WorkspaceStore(tmp_path / "uploads")
    await workspace_store.init()
    await workspace_store.ensure_session_workspace("session-one")
    source_code = await workspace_store.write_text(
        "session-one",
        "main.py",
        "print('workspace baseline')\n",
    )
    config_data = DEFAULT_VALIDATION_CONFIG
    config_entry = SandboxFileEntry(
        path=".pi-agent/sandbox.toml",
        size=len(config_data),
        sha256=hashlib.sha256(config_data).hexdigest(),
    )
    workspace_entries = tuple(
        SandboxFileEntry(
            path=ref.logical_path,
            size=ref.size,
            sha256=ref.sha256,
        )
        for ref in await workspace_store.list_session("session-one")
    )
    baseline = tuple(sorted((*workspace_entries, config_entry), key=lambda entry: entry.path))
    backend = FakeSandboxBackend(
        id_factory=lambda: "managed-lifecycle",
        command_results=(
            _helper(_fingerprint(())),
            *(
                _helper(
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "sha256": entry.sha256,
                        "created": True,
                    }
                )
                for entry in baseline
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(_fingerprint(baseline)),
            SandboxCommandResult(
                command_id="check",
                stdout="syntax ok\n",
                started_at_ms=20,
                finished_at_ms=25,
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(
                {
                    "root": ".",
                    "files": [entry.model_dump(mode="json") for entry in baseline],
                    "truncated": False,
                }
            ),
        ),
    )
    config = SandboxAdminConfig(enabled=True, credential_id="credential-one")

    async def config_provider() -> SandboxConfigRecord:
        return SandboxConfigRecord(config=config, revision=3, updated_at_ms=1)

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        return backend

    store = await SQLiteSandboxOperationStore.open(tmp_path / "lifecycle.db")
    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=None,
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
        baseline_provider=WorkspaceSandboxBaselineProvider(
            workspace_store,
            materialization_root=(tmp_path / "staging" / "materialized"),
        ),
        artifact_publisher=WorkspaceSandboxArtifactPublisher(
            workspace_store,
            staging_root=(tmp_path / "staging" / "publishes"),
        ),
    )
    try:
        creating = await lifecycle.start("session-one")
        ready = await _wait_for_status(lifecycle, creating.operation_id, "ready")
        assert ready.config_revision == 3
        assert ready.baseline_workspace_revision == 1
        assert ready.baseline_workspace_sha256 is not None
        assert ready.publish_available is True
        assert lifecycle.workspace_for_session("session-one").workspace_revision == 0

        await workspace_store.update_text(
            "session-one",
            source_code.id,
            "print('newer workspace revision')\n",
            expected_sha256=source_code.sha256,
            expected_workspace_revision=1,
        )
        assert (await workspace_store.get_workspace_state("session-one")).revision == 2
        assert (await lifecycle.get(ready.operation_id)).baseline_workspace_revision == 1

        validating = await lifecycle.validate(ready.operation_id)
        assert validating.status == "validating"
        validated = await _wait_for_status(lifecycle, ready.operation_id, "validated")
        assert validated.validation is not None
        assert validated.validation.passed is True
        assert validated.diff is not None
        assert validated.diff.entries == ()
        with pytest.raises(SandboxLifecycleError) as unavailable:
            await lifecycle.publish(ready.operation_id)
        assert unavailable.value.code == "approval_required"

        cancelled = await lifecycle.cancel(ready.operation_id)
        assert cancelled.status == "cancelled"
        assert backend.destroyed_sandbox_ids == ["managed-lifecycle"]
        page = await lifecycle.events(ready.operation_id)
        assert [event.sequence for event in page.events] == list(range(1, len(page.events) + 1))
        assert page.events[-1].event_type == "sandbox_operation_cancelled"
    finally:
        await lifecycle.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_workspace_publish_commits_one_revision_atomically(tmp_path: Path) -> None:
    workspace_store = WorkspaceStore(tmp_path / "uploads")
    await workspace_store.init()
    await workspace_store.ensure_session_workspace("session-one")
    main = await workspace_store.write_text(
        "session-one",
        "main.py",
        "print('before')\n",
    )
    obsolete = await workspace_store.write_text(
        "session-one",
        "obsolete.py",
        "print('remove me')\n",
    )
    baseline = await workspace_store.materialize_workspace_revision(
        "session-one",
        (tmp_path / "baseline").resolve(),
    )
    next_main = tmp_path / "next-main.py"
    next_main.write_text("print('after')\n", encoding="utf-8")
    new_note = tmp_path / "result.md"
    new_note.write_text("# Result\n", encoding="utf-8")

    result = await workspace_store.publish_workspace_changes(
        "session-one",
        transaction_id="publish-" + "a" * 32,
        expected_workspace_revision=baseline.revision,
        expected_workspace_sha256=baseline.tree_sha256,
        changes=(
            WorkspacePublishChange(
                logical_path=main.logical_path,
                source_path=next_main,
                size=next_main.stat().st_size,
                sha256=hashlib.sha256(next_main.read_bytes()).hexdigest(),
            ),
            WorkspacePublishChange(
                logical_path="notes/result.md",
                source_path=new_note,
                size=new_note.stat().st_size,
                sha256=hashlib.sha256(new_note.read_bytes()).hexdigest(),
            ),
        ),
        deleted_paths=(obsolete.logical_path,),
    )

    assert result.previous_revision == baseline.revision
    assert result.revision == baseline.revision + 1
    assert (await workspace_store.get_workspace_state("session-one")).revision == result.revision
    published = {
        ref.logical_path: ref for ref in await workspace_store.list_session("session-one")
    }
    assert published[main.logical_path].id == main.id
    assert await asyncio.to_thread(
        Path(published[main.logical_path].path).read_text,
        encoding="utf-8",
    ) == (
        "print('after')\n"
    )
    assert await asyncio.to_thread(
        Path(published["notes/result.md"].path).read_text,
        encoding="utf-8",
    ) == (
        "# Result\n"
    )
    assert obsolete.logical_path not in published
    assert {"AGENT.md", "Memory.md"}.issubset(published)


@pytest.mark.asyncio
async def test_workspace_publish_rejects_stale_baseline_without_writes(tmp_path: Path) -> None:
    workspace_store = WorkspaceStore(tmp_path / "uploads")
    await workspace_store.init()
    await workspace_store.ensure_session_workspace("session-one")
    main = await workspace_store.write_text(
        "session-one",
        "main.py",
        "print('baseline')\n",
    )
    baseline = await workspace_store.materialize_workspace_revision(
        "session-one",
        (tmp_path / "baseline").resolve(),
    )
    await workspace_store.update_text(
        "session-one",
        main.id,
        "print('user edit')\n",
        expected_sha256=main.sha256,
        expected_workspace_revision=baseline.revision,
    )
    before_state = await workspace_store.get_workspace_state("session-one")
    before_refs = await workspace_store.list_session("session-one")
    before_files = await asyncio.to_thread(
        lambda: {
            ref.logical_path: (ref.sha256, Path(ref.path).read_bytes())
            for ref in before_refs
        }
    )
    sandbox_output = tmp_path / "sandbox-main.py"
    sandbox_output.write_text("print('sandbox edit')\n", encoding="utf-8")

    with pytest.raises(WorkspaceVersionConflictError):
        await workspace_store.publish_workspace_changes(
            "session-one",
            transaction_id="publish-" + "b" * 32,
            expected_workspace_revision=baseline.revision,
            expected_workspace_sha256=baseline.tree_sha256,
            changes=(
                WorkspacePublishChange(
                    logical_path=main.logical_path,
                    source_path=sandbox_output,
                    size=sandbox_output.stat().st_size,
                    sha256=hashlib.sha256(sandbox_output.read_bytes()).hexdigest(),
                ),
            ),
            deleted_paths=(),
        )

    assert await workspace_store.get_workspace_state("session-one") == before_state
    after_refs = await workspace_store.list_session("session-one")
    assert await asyncio.to_thread(
        lambda: {
            ref.logical_path: (ref.sha256, Path(ref.path).read_bytes())
            for ref in after_refs
        }
    ) == before_files


@pytest.mark.asyncio
async def test_failed_validation_cannot_publish_or_mutate_local_project(
    tmp_path: Path,
) -> None:
    config_data = DEFAULT_VALIDATION_CONFIG
    config_entry = SandboxFileEntry(
        path=".pi-agent/sandbox.toml",
        size=len(config_data),
        sha256=hashlib.sha256(config_data).hexdigest(),
    )
    baseline = (config_entry,)
    backend = FakeSandboxBackend(
        id_factory=lambda: "managed-validation-failure",
        command_results=(
            _helper(_fingerprint(())),
            _helper(
                {
                    "path": config_entry.path,
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                    "created": True,
                }
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(_fingerprint(baseline)),
            SandboxCommandResult(
                command_id="failed-check",
                exit_code=1,
                stderr="syntax failure\n",
                started_at_ms=20,
                finished_at_ms=25,
            ),
            _helper(_fingerprint(baseline)),
            _helper(
                {
                    "path": config_entry.path,
                    "content": config_data.decode(),
                    "size": config_entry.size,
                    "sha256": config_entry.sha256,
                }
            ),
            _helper(
                {
                    "root": ".",
                    "files": [config_entry.model_dump(mode="json")],
                    "truncated": False,
                }
            ),
        ),
    )
    config = SandboxAdminConfig(enabled=True, credential_id="credential-one")

    async def config_provider() -> SandboxConfigRecord:
        return SandboxConfigRecord(config=config, revision=4, updated_at_ms=1)

    async def backend_resolver(_config: SandboxAdminConfig) -> FakeSandboxBackend:
        return backend

    projects_root = tmp_path / "projects"
    store = await SQLiteSandboxOperationStore.open(tmp_path / "validation-failure.db")
    lifecycle = ManagedSandboxLifecycle(
        store=store,
        config_provider=config_provider,
        backend_resolver=backend_resolver,
        artifact_signer=HMACSHA256ArtifactSigner(key_id="test", secret=b"k" * 32),
        session_exists=lambda _session_id: asyncio.sleep(0, result=True),
        projects_root=projects_root,
        state_root=tmp_path / "publisher",
        staging_root=tmp_path / "staging",
    )
    try:
        creating = await lifecycle.start("session-one")
        ready = await _wait_for_status(lifecycle, creating.operation_id, "ready")
        project_root = next(path for path in projects_root.iterdir() if path.is_dir())
        before = _workspace_bytes(project_root)

        await lifecycle.validate(ready.operation_id)
        failed = await _wait_for_status(
            lifecycle,
            ready.operation_id,
            "validation_failed",
        )
        assert failed.validation is not None
        assert failed.validation.passed is False
        assert failed.validation.failure_code == "check_failed"
        assert failed.validation.checks[0].stderr == "syntax failure\n"

        with pytest.raises(SandboxLifecycleError) as raised:
            await lifecycle.prepare_publish(ready.operation_id)
        assert raised.value.code == "validation_required"
        assert _workspace_bytes(project_root) == before

        discarded = await lifecycle.discard(ready.operation_id)
        assert discarded.status == "discarded"
        assert _workspace_bytes(project_root) == before
        assert backend.destroyed_sandbox_ids == ["managed-validation-failure"]
    finally:
        await lifecycle.shutdown()
        await store.close()

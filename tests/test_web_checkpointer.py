"""Web /checkpointer slash-command integration tests."""
from __future__ import annotations

import io
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import (
    DoneEvent,
    ErrorEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import create_app, dispose_app


def _script(text: str) -> list[Any]:
    return [TextDeltaEvent(delta=text), DoneEvent(stop_reason="stop")]


def _build_app(
    tmp_path: Path,
    scripts: list[list[Any]],
    *,
    auto_memory: bool = False,
    code_continuity: bool = False,
):
    fake = FakeClient(scripts)
    harness = AgentHarness(Agent(system_prompt="base-system", client=fake))
    app = create_app(
        harness,
        db_path=tmp_path / "workspace.sqlite",
        uploads_dir=tmp_path / "uploads",
        enable_auto_memory=auto_memory,
        enable_code_continuity=code_continuity,
    )
    return app, fake


def _create_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={"title": "checkpoint"})
    assert response.status_code == 200
    return response.json()["id"]


def _wait_for_request(client: TestClient, request_id: str) -> dict[str, Any]:
    for _ in range(200):
        response = client.get(f"/api/requests/{request_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "error", "aborted"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"request {request_id} did not finish")


def _execute(client: TestClient, session_id: str, command: str = "/checkpointer"):
    return client.post(
        f"/api/sessions/{session_id}/slash-commands",
        json={"command": command},
    )


def test_slash_command_catalog_and_validation(tmp_path: Path) -> None:
    app, _fake = _build_app(tmp_path, [])
    with TestClient(app) as client:
        catalog = client.get("/api/slash-commands")
        assert catalog.status_code == 200
        assert catalog.json()["commands"] == [
            {
                "name": "/checkpointer",
                "description": (
                    "Summarize this conversation to Memory.md, then clear it."
                ),
                "requires_provider": True,
                "accepts_arguments": False,
            }
        ]
        sid = _create_session(client)
        empty = _execute(client, sid)
        assert empty.status_code == 409
        assert empty.json()["detail"]["code"] == "nothing_to_checkpoint"
        unknown = _execute(client, sid, "/missing")
        assert unknown.status_code == 400
        assert unknown.json()["detail"]["code"] == "unknown_slash_command"
    dispose_app(app)


def test_checkpointer_saves_memory_then_clears_and_reloads_it(tmp_path: Path) -> None:
    app, fake = _build_app(
        tmp_path,
        [
            _script("first answer"),
            _script(
                "# Memory\n\n## Work completed\n- Preserved the important result."
            ),
            _script("continued with memory"),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        prompt = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "remember this result"},
        )
        assert prompt.status_code == 200
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2

        started = _execute(client, sid)
        assert started.status_code == 202
        terminal = _wait_for_request(client, started.json()["request_id"])
        assert terminal["status"] == "completed"
        assert terminal["operation"] == "checkpointer"
        assert terminal["result_summary"]["source_message_count"] == 2
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 0

        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        memories = [f for f in files if f["purpose"] == "memory"]
        assert len(memories) == 1
        memory = memories[0]
        assert memory["logical_path"] == "Memory.md"
        assert "path" not in memory
        content = client.get(
            f"/api/sessions/{sid}/files/{memory['id']}"
        ).text
        assert "source_message_count: 2" in content
        assert "Preserved the important result" in content

        edited_memory = content + "\n## User notes\n- Keep this user-maintained fact.\n"
        edited = client.put(
            f"/api/sessions/{sid}/files/{memory['id']}/content",
            json={
                "content": edited_memory,
                "expected_sha256": memory["sha256"],
            },
        )
        assert edited.status_code == 200
        edited_ref = edited.json()["file"]
        assert edited_ref["purpose"] == "memory"
        assert edited_ref["origin"] == "user"
        assert edited_ref["sha256"] != memory["sha256"]
        stale = client.put(
            f"/api/sessions/{sid}/files/{memory['id']}/content",
            json={"content": "stale", "expected_sha256": memory["sha256"]},
        )
        assert stale.status_code == 409

        continued = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "continue"},
        )
        assert continued.status_code == 200
        assert "<session_memory_md>" in fake.last_system_prompt
        assert "Preserved the important result" in fake.last_system_prompt
        assert "Keep this user-maintained fact" in fake.last_system_prompt
    dispose_app(app)


def test_checkpointer_provider_failure_keeps_messages_and_files(tmp_path: Path) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer"),
            [ErrorEvent(message="summary provider unavailable")],
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        initial_files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        initial_memory = next(
            item for item in initial_files if item["logical_path"] == "Memory.md"
        )
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "do not lose this"},
        ).status_code == 200

        started = _execute(client, sid)
        assert started.status_code == 202
        terminal = _wait_for_request(client, started.json()["request_id"])
        assert terminal["status"] == "error"
        assert terminal["error_type"] == "checkpoint_provider_error"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2
        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        memories = [item for item in files if item["logical_path"] == "Memory.md"]
        assert len(memories) == 1
        assert memories[0]["id"] == initial_memory["id"]
        assert memories[0]["sha256"] == initial_memory["sha256"]
    dispose_app(app)


def test_checkpointer_clear_failure_is_retried_from_durable_operation(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer"),
            _script("# Memory\n\n## Work completed\n- Durable result."),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "keep this if commit fails"},
        ).status_code == 200

        store = app.state.web.session_store
        original_complete = store.complete_operation_and_reset_lane

        async def fail_clear(_operation_id: str):
            raise RuntimeError("simulated database commit failure")

        store.complete_operation_and_reset_lane = fail_clear  # type: ignore[method-assign]
        started = _execute(client, sid)
        terminal = _wait_for_request(client, started.json()["request_id"])

        assert terminal["status"] == "error"
        assert terminal["error_type"] == "checkpoint_commit_failed"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2
        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        assert [item["logical_path"] for item in files].count("Memory.md") == 1

        store.complete_operation_and_reset_lane = original_complete
        retried = _execute(client, sid)
        recovered = _wait_for_request(client, retried.json()["request_id"])
        assert recovered["status"] == "completed"
        assert recovered["result_summary"]["idempotent_recovery"] is True
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 0
    dispose_app(app)


def test_checkpointer_startup_recovery_finishes_published_memory(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer"),
            _script("# Memory\n\n## Work completed\n- Survives restart."),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "recover after restart"},
        ).status_code == 200
        store = app.state.web.session_store

        async def fail_clear(_operation_id: str):
            raise RuntimeError("simulated crash boundary")

        store.complete_operation_and_reset_lane = fail_clear  # type: ignore[method-assign]
        started = _execute(client, sid)
        terminal = _wait_for_request(client, started.json()["request_id"])
        assert terminal["status"] == "error"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2
    dispose_app(app)

    recovered_app, recovered_fake = _build_app(tmp_path, [])
    with TestClient(recovered_app) as client:
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 0
        state = client.get("/api/state").json()
        assert state["durable_recovery"] == {
            "scanned": 1,
            "completed": 1,
            "aborted": 0,
            "conflicts": 0,
        }
        assert recovered_fake.all_messages_calls == []
    dispose_app(recovered_app)


def test_checkpointer_recovery_preserves_messages_when_source_leaf_changed(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer"),
            _script("# Memory\n\n## Work completed\n- Older checkpoint."),
            _script("newer answer"),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        assert client.post(
            "/api/prompt", json={"session_id": sid, "text": "older"}
        ).status_code == 200
        store = app.state.web.session_store

        async def fail_clear(_operation_id: str):
            raise RuntimeError("simulated crash boundary")

        store.complete_operation_and_reset_lane = fail_clear  # type: ignore[method-assign]
        started = _execute(client, sid)
        assert _wait_for_request(client, started.json()["request_id"])[
            "status"
        ] == "error"
        assert client.post(
            "/api/prompt", json={"session_id": sid, "text": "newer"}
        ).status_code == 200
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 4
    dispose_app(app)

    recovered_app, recovered_fake = _build_app(tmp_path, [])
    with TestClient(recovered_app) as client:
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 4
        assert client.get("/api/state").json()["durable_recovery"] == {
            "scanned": 1,
            "completed": 0,
            "aborted": 0,
            "conflicts": 1,
        }
        assert recovered_fake.all_messages_calls == []
    dispose_app(recovered_app)


def test_checkpointer_updates_one_cumulative_memory_file(tmp_path: Path) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer one"),
            _script("# Memory\n\n## Work completed\n- First checkpoint."),
            _script("answer two"),
            _script(
                "# Memory\n\n## Work completed\n- First checkpoint.\n- Second checkpoint."
            ),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        for prompt_text in ("first", "second"):
            assert client.post(
                "/api/prompt",
                json={"session_id": sid, "text": prompt_text},
            ).status_code == 200
            started = _execute(client, sid)
            terminal = _wait_for_request(client, started.json()["request_id"])
            assert terminal["status"] == "completed"

        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        memories = [f for f in files if f["logical_path"] == "Memory.md"]
        assert len(memories) == 1
        content = client.get(
            f"/api/sessions/{sid}/files/{memories[0]['id']}"
        ).text
        assert "First checkpoint" in content
        assert "Second checkpoint" in content
    dispose_app(app)


def test_prompt_automatically_updates_memory_without_clearing_messages(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("implemented the requested change"),
            _script("# Memory\n\n## Work completed\n- Implemented the requested change."),
        ],
        auto_memory=True,
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        response = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "implement this"},
        )
        assert response.status_code == 200
        continuity = response.json()["continuity"]
        assert continuity["status"] == "updated"
        assert continuity["workspace_revision"] >= 1
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2

        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        memory = next(item for item in files if item["logical_path"] == "Memory.md")
        content = client.get(f"/api/sessions/{sid}/files/{memory['id']}").text
        assert "operation: auto_memory" in content
        assert "Implemented the requested change" in content
    dispose_app(app)


def test_auto_memory_failure_keeps_answer_and_retries_before_next_turn(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("first answer"),
            [ErrorEvent(message="summary provider unavailable")],
            _script("# Memory\n\n## Work completed\n- First turn retained."),
            _script("second answer"),
            _script(
                "# Memory\n\n## Work completed\n- First turn retained.\n- Second turn retained."
            ),
        ],
        auto_memory=True,
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        first = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "first"},
        )
        assert first.status_code == 200
        assert first.json()["continuity"]["status"] == "pending_retry"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2

        second = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "second"},
        )
        assert second.status_code == 200
        assert second.json()["continuity"]["status"] == "updated"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 4

        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        memory = next(item for item in files if item["logical_path"] == "Memory.md")
        content = client.get(f"/api/sessions/{sid}/files/{memory['id']}").text
        assert "First turn retained" in content
        assert "Second turn retained" in content
    dispose_app(app)


def test_workspace_context_restores_coding_facts_without_chat_history_after_restart(
    tmp_path: Path,
) -> None:
    first_app, _first_fake = _build_app(
        tmp_path,
        [],
        code_continuity=True,
    )
    with TestClient(first_app) as client:
        sid = _create_session(client)
        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        agent_md = next(item for item in files if item["logical_path"] == "AGENT.md")
        memory_md = next(item for item in files if item["logical_path"] == "Memory.md")
        assert client.put(
            f"/api/sessions/{sid}/files/{agent_md['id']}/content",
            json={
                "content": "# AGENT.md\n\nAlways preserve RESTART_INSTRUCTION_MARKER.\n",
                "expected_sha256": agent_md["sha256"],
            },
        ).status_code == 200
        assert client.put(
            f"/api/sessions/{sid}/files/{memory_md['id']}/content",
            json={
                "content": "# Memory\n\nRESTART_MEMORY_FACT remains required.\n",
                "expected_sha256": memory_md["sha256"],
            },
        ).status_code == 200
        uploaded = client.post(
            f"/api/sessions/{sid}/files",
            files={
                "files": (
                    "main.py",
                    io.BytesIO(b"def restart_entrypoint():\n    return 'ready'\n"),
                    "text/x-python",
                )
            },
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["workspace"]["code_continuity"]["status"] == "current"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 0
    dispose_app(first_app)

    second_app, second_fake = _build_app(
        tmp_path,
        [_script("continued from Workspace")],
        code_continuity=True,
    )
    with TestClient(second_app) as client:
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 0
        response = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "continue without prior chat"},
        )
        assert response.status_code == 200
        context_audit = response.json()["workspace_context"]
        assert context_audit["schema"] == "pi-agent-workspace-context/v1"
        assert context_audit["code_continuity_status"] == "current"
        assert context_audit["context_sha256"]

        prompt = second_fake.last_system_prompt
        assert "<session_agent_md>" in prompt
        assert "RESTART_INSTRUCTION_MARKER" in prompt
        assert "<session_memory_md>" in prompt
        assert "RESTART_MEMORY_FACT" in prompt
        assert "<workspace_architecture>" in prompt
        assert "<workspace_code_flow>" in prompt
        assert "restart_entrypoint" in prompt
        assert "<workspace_validation>" in prompt
        assert "<workspace_tree>" in prompt
        assert '"status":"current"' in prompt
    dispose_app(second_app)


def test_workspace_context_marks_pending_artifact_memory_and_stale_code(
    tmp_path: Path,
) -> None:
    app, fake = _build_app(
        tmp_path,
        [
            _script("FIRST_PENDING_FACT"),
            [ErrorEvent(message="memory unavailable")],
            [ErrorEvent(message="memory still unavailable")],
            _script("continued safely"),
            [ErrorEvent(message="memory remains unavailable")],
        ],
        auto_memory=True,
        code_continuity=True,
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        first_code = client.post(
            f"/api/sessions/{sid}/files",
            files={
                "files": (
                    "legacy.py",
                    io.BytesIO(b"def obsolete_summary_symbol():\n    return 1\n"),
                    "text/x-python",
                )
            },
        )
        assert first_code.json()["workspace"]["code_continuity"]["status"] == "current"
        app.state.web.code_continuity_service = None
        second_code = client.post(
            f"/api/sessions/{sid}/files",
            files={
                "files": (
                    "fresh.py",
                    io.BytesIO(b"def current_symbol():\n    return 2\n"),
                    "text/x-python",
                )
            },
        )
        assert second_code.json()["workspace"]["code_continuity"]["status"] == "stale"

        first = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "record unfinished work"},
        )
        assert first.status_code == 200
        assert first.json()["continuity"]["status"] == "pending_retry"

        record = SimpleNamespace(
            terminal=False,
            operation_id="sandbox-" + "a" * 32,
            status="awaiting_approval",
            workspace_revision=2,
            baseline_workspace_revision=2,
            artifact_id="artifact-" + "b" * 32,
            artifact_sha256="c" * 64,
            validation=SimpleNamespace(
                evidence_id="validation-test",
                passed=True,
            ),
            changed_paths=("scripts/fresh.py",),
            deleted_paths=(),
            allowed_actions=("publish", "discard"),
            error_code=None,
        )

        class _Lifecycle:
            async def latest_for_session(self, _session_id: str):
                return record

        app.state.coding_sandbox_runtime = SimpleNamespace(lifecycle=_Lifecycle())
        second = client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "what remains to be done?"},
        )
        assert second.status_code == 200
        context_audit = second.json()["workspace_context"]
        assert context_audit["pending_memory"] is True
        assert context_audit["sandbox_status"] == "awaiting_approval"
        assert context_audit["code_continuity_status"] == "stale"

        continuation_prompt = next(
            prompt
            for prompt in fake.all_system_prompt_calls
            if "<pending_turn_evidence>" in prompt
        )
        assert "FIRST_PENDING_FACT" in continuation_prompt
        assert "<sandbox_continuation>" in continuation_prompt
        assert '"status":"awaiting_approval"' in continuation_prompt
        assert "This state is not published" in continuation_prompt
        assert '"status":"stale"' in continuation_prompt
        assert "Do not rely on fixed code-summary bodies" in continuation_prompt
        assert "<workspace_code_flow>" not in continuation_prompt
        assert "obsolete_summary_symbol" not in continuation_prompt
    dispose_app(app)

"""Web /checkpointer slash-command integration tests."""
from __future__ import annotations

import time
from pathlib import Path
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


def _build_app(tmp_path: Path, scripts: list[list[Any]]):
    fake = FakeClient(scripts)
    harness = AgentHarness(Agent(system_prompt="base-system", client=fake))
    app = create_app(
        harness,
        db_path=tmp_path / "workspace.sqlite",
        uploads_dir=tmp_path / "uploads",
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
        assert all(item["logical_path"] != "Memory.md" for item in files)
    dispose_app(app)


def test_checkpointer_clear_failure_rolls_back_memory_and_keeps_messages(
    tmp_path: Path,
) -> None:
    app, _fake = _build_app(
        tmp_path,
        [
            _script("answer"),
            _script("# Memory\n\n## Work completed\n- Must be rolled back."),
        ],
    )
    with TestClient(app) as client:
        sid = _create_session(client)
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "keep this if commit fails"},
        ).status_code == 200

        store = app.state.web.session_store
        original_replace_messages = store.replace_messages

        async def fail_clear(session_id: str, messages: list[Any]) -> None:
            if session_id == sid and not messages:
                raise RuntimeError("simulated database commit failure")
            await original_replace_messages(session_id, messages)

        store.replace_messages = fail_clear  # type: ignore[method-assign]
        started = _execute(client, sid)
        terminal = _wait_for_request(client, started.json()["request_id"])

        assert terminal["status"] == "error"
        assert terminal["error_type"] == "checkpoint_commit_failed"
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2
        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        assert all(item["logical_path"] != "Memory.md" for item in files)
    dispose_app(app)


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

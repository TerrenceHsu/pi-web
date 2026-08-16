from __future__ import annotations

from fastapi.testclient import TestClient

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import create_app


def _app(tmp_path):
    client = FakeClient([
        [TextDeltaEvent(delta="one"), DoneEvent(stop_reason="stop")],
        [TextDeltaEvent(delta="two"), DoneEvent(stop_reason="stop")],
        [TextDeltaEvent(delta="three"), DoneEvent(stop_reason="stop")],
    ])
    return create_app(
        AgentHarness(Agent(system_prompt="system", client=client)),
        db_path=str(tmp_path / "context.sqlite"),
    )


def test_context_budget_unknown_model_and_turn_safe_compaction(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        session_id = client.get("/api/sessions").json()["sessions"][0]["id"]
        base = client.get(f"/api/sessions/{session_id}/context-budget")
        assert base.status_code == 200
        assert base.json()["estimate"]["level"] == "unknown"
        assert base.json()["estimate"]["estimated_input_tokens"] > 0

        for text in ("turn one", "turn two", "turn three"):
            response = client.post(
                "/api/prompt",
                json={"session_id": session_id, "text": text},
            )
            assert response.status_code == 200

        compacted = client.post(
            f"/api/sessions/{session_id}/context/compact",
            json={"keep_last_n_turns": 1},
        )
        assert compacted.status_code == 200
        body = compacted.json()
        assert body["compacted_message_count"] == 4
        assert body["retained_message_count"] == 2

        messages = client.get(
            f"/api/messages?session_id={session_id}"
        ).json()["messages"]
        assert [item["role"] for item in messages] == [
            "summary", "user", "assistant",
        ]
        assert messages[0]["message"]["summary_type"] == "context_compaction"


def test_context_estimate_rejects_invalid_payload_without_mutation(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        session_id = client.get("/api/sessions").json()["sessions"][0]["id"]
        response = client.post(
            f"/api/sessions/{session_id}/context-budget/estimate",
            json={"text": "draft", "file_ids": [123]},
        )
        assert response.status_code == 422
        messages = client.get(
            f"/api/messages?session_id={session_id}"
        ).json()["messages"]
        assert messages == []

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import create_app


class SummaryFakeClient(FakeClient):
    async def stream(self, **kwargs):
        if (kwargs.get("metadata") or {}).get("operation") == "context_compaction":
            source = json.loads(kwargs["messages"][0].content[0].text)
            entry_id = source["source_entries"][0]["entry_id"]
            assert not kwargs.get("tools")
            yield TextDeltaEvent(delta=json.dumps({
                "current_goal": "Continue the task",
                "facts": [{"text": "The user discussed turn one", "source_entry_ids": [entry_id]}],
                "decisions": [], "failed_attempts": [], "open_questions": [],
                "next_steps": [], "artifacts": [], "memory_item_ids": [],
            }))
            yield DoneEvent(stop_reason="stop")
            return
        async for event in super().stream(**kwargs):
            yield event


def _app(tmp_path):
    client = SummaryFakeClient([
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
                json={"session_id": session_id, "text": text + " repeated evidence" * 250},
            )
            assert response.status_code == 200

        original = client.get(f"/api/messages?session_id={session_id}").json()["messages"]

        compacted = client.post(
            f"/api/sessions/{session_id}/context/compact",
            json={"keep_last_n_turns": 1},
        )
        assert compacted.status_code == 200, compacted.text
        body = compacted.json()
        assert body["compacted_message_count"] == 4
        assert body["retained_message_count"] == 2
        assert body["budget_before"]["estimate"]["estimated_input_tokens"] > 0
        assert body["token_stats"]["estimated_input_tokens_before"] == (
            body["budget_before"]["estimate"]["estimated_input_tokens"]
        )
        assert body["token_stats"]["message_tokens_after"] > 0

        messages = client.get(
            f"/api/messages?session_id={session_id}"
        ).json()["messages"]
        assert messages == original
        assert [item["role"] for item in messages] == ["user", "assistant"] * 3
        assert body["summary_message"]["summary_type"] == "context_compaction"
        assert body["budget"]["compaction"]["covered_message_count"] == 4
        details = client.get(f"/api/sessions/{session_id}/context/compaction").json()
        source_id = details["source_entry_ids"][0]
        assert details["summary_text"]
        source = client.get(f"/api/sessions/{session_id}/context/source/{source_id}")
        assert source.status_code == 200
        assert "turn one" in source.json()["text"]
        other = client.post("/api/sessions", json={"title": "Other"}).json()["id"]
        assert client.get(f"/api/sessions/{other}/context/source/{source_id}").status_code == 404
        setting = client.put(f"/api/sessions/{session_id}/context/compaction",
                             json={"auto_compact": False})
        assert setting.status_code == 200
        assert setting.json()["auto_compact"] is False
        assert client.get(f"/api/sessions/{other}/context/compaction").json()["auto_compact"]

    with TestClient(_app(tmp_path)) as restarted:
        details = restarted.get(f"/api/sessions/{session_id}/context/compaction").json()
        assert details["auto_compact"] is False
        assert details["source_entry_ids"][0] == source_id
        restored = restarted.get(f"/api/messages?session_id={session_id}").json()["messages"]
        assert restored == original


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


def test_budget_includes_actual_output_config_without_resolving_credentials(tmp_path):
    from pi_agent_core_py.ai.providers.factory import default_output_tokens

    assert default_output_tokens("glm") == 4096
    assert default_output_tokens("anthropic") == 4096
    assert default_output_tokens("unknown-provider") is None
    fake = FakeClient([])
    fake.adapter.config = SimpleNamespace(max_tokens=3072)
    app = create_app(AgentHarness(Agent(system_prompt="system", client=fake)),
                     db_path=str(tmp_path / "reserve.sqlite"))
    with TestClient(app) as client:
        sid = client.get("/api/sessions").json()["sessions"][0]["id"]
        estimate = client.get(f"/api/sessions/{sid}/context-budget").json()["estimate"]
        assert estimate["reserved_output_tokens"] == 3072
        assert fake.all_messages_calls == []

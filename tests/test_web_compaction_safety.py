"""Independent Web regressions for live context preparation and final admission."""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.agent.loop import AgentLoopTurnUpdate
from pi_agent_core_py.agent.messages import AssistantMessage, TextContent, ToolCall, UserMessage
from pi_agent_core_py.agent.tooling import AgentTool, ToolResult
from pi_agent_core_py.web.app import create_app


class ReadOutput(AgentTool):
    name = "list_files"
    label = "Read output"
    description = "Read deterministic data."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, body="small data"):
        self.body = body

    async def execute(self, tool_call_id, args, *, signal=None, on_update=None):
        return ToolResult(
            tool_call_id=tool_call_id, name=self.name, content=[TextContent(text=self.body)]
        )


def _tool_turn(call_id):
    return [
        ToolCallEvent(tool_call=ToolCall(id=call_id, name="list_files", arguments={})),
        DoneEvent(stop_reason="tool_use"),
    ]


class WaitingClient(FakeClient):
    def __init__(self, *, summary=False):
        super().__init__([[TextDeltaEvent(delta="finished"), DoneEvent(stop_reason="stop")]])
        self.wait_for_summary = summary
        self.entered = threading.Event()
        self.closed = threading.Event()

    async def stream(self, **kwargs):
        is_summary = (kwargs.get("metadata") or {}).get("operation") == "context_compaction"
        if is_summary == self.wait_for_summary:
            self.entered.set()
            try:
                if not is_summary and kwargs.get("signal") is not None:
                    await kwargs["signal"].wait()
                    yield DoneEvent(stop_reason="aborted")
                    return
                await asyncio.Event().wait()
            finally:
                self.closed.set()
        async for event in super().stream(**kwargs):
            yield event


class RejectSummaryClient(FakeClient):
    def __init__(self):
        super().__init__(
            [
                _tool_turn("call-1"),
                _tool_turn("call-2"),
                [TextDeltaEvent(delta="finished"), DoneEvent(stop_reason="stop")],
            ]
        )
        self.summary_calls = []

    async def stream(self, **kwargs):
        if (kwargs.get("metadata") or {}).get("operation") == "context_compaction":
            self.summary_calls.append(kwargs)
            yield TextDeltaEvent(delta="not JSON")
            yield DoneEvent(stop_reason="stop")
            return
        async for event in super().stream(**kwargs):
            yield event


def _setup(app, client, harness, *, seed=False, model="fake-1", window=15000):
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]

    async def initialize():
        await app.state.web.model_capability_store.upsert(
            provider_id="fake",
            model_id=model,
            context_window=window,
            max_output_tokens=256,
        )
        if seed:
            store = app.state.context_management.store.repository
            for index in range(6):
                await store.append_message(
                    sid, UserMessage(content=[TextContent(text=f"request {index} " + "u" * 3500)])
                )
                await store.append_message(
                    sid,
                    AssistantMessage(
                        api="fake",
                        provider="fake",
                        model="fake-1",
                        content=[TextContent(text="a" * 3500)],
                    ),
                )
            session = app.state.coding_agent_runtime.get(sid)
            target = session.harness if session else harness
            target.agent.state.messages = list(await store.list_messages(sid))

    client.portal.call(initialize)
    return sid


def _terminal(client, request_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/requests/{request_id}").json()
        if result["status"] in {"completed", "aborted", "failed", "error"}:
            return result
        time.sleep(0.01)
    raise AssertionError("request failed to settle")


def test_running_request_budget_preview_never_enters_writable_transform(tmp_path, monkeypatch):
    fake = WaitingClient()
    harness = AgentHarness(Agent(system_prompt="system", client=fake))
    app = create_app(harness, db_path=tmp_path / "preview.sqlite")
    with TestClient(app) as client:
        sid = _setup(app, client, harness)
        modes = []
        original = app.state.tool_context_budgeter.project

        async def observed(*args, **kwargs):
            modes.append(kwargs.get("allow_write", True))
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.state.tool_context_budgeter, "project", observed)
        response = client.post("/api/prompt/async", json={"session_id": sid, "text": "Inspect"})
        assert response.status_code == 202
        request_id = response.json()["request_id"]
        assert fake.entered.wait(3)
        try:
            modes.clear()
            response = client.get(f"/api/sessions/{sid}/context-budget")
            assert response.status_code == 409, response.text
            assert modes == []
        finally:
            assert client.post(f"/api/requests/{request_id}/abort").status_code == 200
            assert _terminal(client, request_id)["status"] == "aborted"
        assert fake.closed.wait(2)
        assert harness.agent.transform_context_fn is None


def test_automatic_invalid_summary_is_limited_across_model_calls(tmp_path):
    fake = RejectSummaryClient()
    harness = AgentHarness(Agent(system_prompt="system", client=fake, tools=[ReadOutput()]))
    app = create_app(harness, db_path=tmp_path / "limits.sqlite")
    with TestClient(app) as client:
        sid = _setup(app, client, harness, seed=True)
        response = client.post(
            "/api/prompt", json={"session_id": sid, "text": "Inspect the next detail."}
        )
        assert response.status_code == 200, response.text
        assert len(fake.summary_calls) == 2
        assert len(fake.all_messages_calls) == 3
        status = client.get(f"/api/sessions/{sid}/context/compaction").json()
        assert status["consecutive_failures"] == 2 and status["circuit_open"]
        messages = client.get(f"/api/messages?session_id={sid}").json()["messages"]
        assert all(message["role"] != "summary" for message in messages)
        assert harness.agent.transform_context_fn is None


def test_current_large_tool_result_is_projected_before_second_model_call(tmp_path):
    body = "large result " * 12000
    fake = FakeClient(
        [
            _tool_turn("large-call"),
            [TextDeltaEvent(delta="finished"), DoneEvent(stop_reason="stop")],
        ]
    )
    harness = AgentHarness(Agent(system_prompt="system", client=fake, tools=[ReadOutput(body)]))
    app = create_app(harness, db_path=tmp_path / "tool.sqlite")
    with TestClient(app) as client:
        sid = _setup(app, client, harness, window=8000)
        response = client.post("/api/prompt", json={"session_id": sid, "text": "Inspect data"})
        assert response.status_code == 200, response.text
        assert len(fake.all_messages_calls) == 2
        result = next(
            message for message in fake.all_messages_calls[-1] if message.role == "toolResult"
        )
        assert "read_tool_output" in result.content[0].text
        assert len(result.content[0].text) < len(body) // 2
        persisted = client.get(f"/api/messages?session_id={sid}").json()["messages"]
        original = next(message for message in persisted if message["role"] == "toolResult")
        assert original["content"][0]["text"] == body


def test_prepare_next_turn_model_switch_uses_actual_binding_and_final_guard(tmp_path, monkeypatch):
    first = FakeClient([_tool_turn("switch-call")])
    second = FakeClient([[TextDeltaEvent(delta="must not run"), DoneEvent(stop_reason="stop")]])
    second.model = "tiny-model"

    async def prepare(_context):
        return AgentLoopTurnUpdate(client=second, system_prompt="large system " * 1200, tools=[])

    harness = AgentHarness(
        Agent(system_prompt="system", client=first, tools=[ReadOutput()], prepare_next_turn=prepare)
    )
    app = create_app(harness, db_path=tmp_path / "switch.sqlite")
    with TestClient(app) as client:
        sid = _setup(app, client, harness, window=64000)
        _setup(app, client, harness, model="tiny-model", window=1024)
        bindings = []
        original = app.state.context_management.prepare

        async def observe(*args, **kwargs):
            info = kwargs["model_context"]
            bindings.append(
                (info.client.model, info.system_prompt, kwargs["context_window"], tuple(info.tools))
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.state.context_management, "prepare", observe)
        response = client.post("/api/prompt", json={"session_id": sid, "text": "Inspect"})
        assert response.status_code == 200, response.text
        assert [item[0] for item in bindings] == ["fake-1", "tiny-model"]
        assert bindings[-1][2] == 1024 and bindings[-1][3] == ()
        assert "large system" in bindings[-1][1]
        assert len(first.all_messages_calls) == 1
        assert second.all_messages_calls == []
        assert harness.agent.state.messages[-1].stop_reason == "error"


def test_abort_during_auto_summary_preserves_source_and_settles_journal(tmp_path):
    fake = WaitingClient(summary=True)
    harness = AgentHarness(Agent(system_prompt="system", client=fake))
    app = create_app(harness, db_path=tmp_path / "cancel.sqlite")
    with TestClient(app) as client:
        sid = _setup(app, client, harness, seed=True)
        before = client.portal.call(app.state.context_management.store.snapshot, sid)
        response = client.post(
            "/api/prompt/async", json={"session_id": sid, "text": "Inspect next"}
        )
        assert response.status_code == 202
        request_id = response.json()["request_id"]
        assert fake.entered.wait(3)
        assert client.post(f"/api/requests/{request_id}/abort").status_code == 200
        assert _terminal(client, request_id)["status"] == "aborted"
        assert fake.closed.wait(2)
        records = client.portal.call(app.state.context_management.store.records, sid)
        assert records[0]["status"] == "failed" and records[0]["error_code"] == "summary_cancelled"
        after = client.portal.call(app.state.context_management.store.snapshot, sid)
        assert after.entry_ids[: len(before.entry_ids)] == before.entry_ids
        assert harness.agent.transform_context_fn is None

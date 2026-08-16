"""P0 runtime golden contract tests.

These tests intentionally pin event order. Changing a literal sequence below is a
runtime protocol change and must be reviewed together with Web consumers.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AgentTool,
    DoneEvent,
    FakeClient,
    ModelCallDecision,
    RequestSnapshot,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    ToolRegistry,
    ToolResult,
    ToolResultMessage,
    TurnSnapshot,
    Usage,
    run_event_loop,
)
from pi_agent_core_py.hooks import (
    AfterToolCallContext,
    BeforeToolCallContext,
    BeforeToolCallResult,
)


class UpdatingTool(AgentTool):
    name = "update"
    label = "Update"
    description = "Emit one update and return a result."
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0
        self.signal: asyncio.Event | None = None

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update=None,
    ) -> ToolResult:
        self.calls += 1
        self.signal = signal
        if on_update is not None:
            await on_update(ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(text="half")],
                details={"progress": 0.5},
            ))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="complete")],
        )


def _two_turn_client(*, first_stop: str = "tool_use") -> FakeClient:
    return FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(id="call-1", name="update", arguments={})),
            DoneEvent(stop_reason=first_stop),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop")],
    ])


@pytest.mark.asyncio
async def test_golden_one_llm_call_plus_tool_batch_per_turn() -> None:
    tool = UpdatingTool()
    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=_two_turn_client(),
        tools=ToolRegistry([tool]),
    )]

    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",       # user
        "message_end",
        "message_start",       # first LLM assistant
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "message_start",       # final tool result, after execution_end
        "message_end",
        "turn_end",
        "turn_start",
        "message_start",       # second LLM assistant
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert sum(event.type == "turn_start" for event in events) == 2
    assert sum(event.type == "turn_end" for event in events) == 2


@pytest.mark.asyncio
async def test_request_snapshot_contains_true_turn_snapshots() -> None:
    agent = Agent(
        system_prompt="sys",
        client=_two_turn_client(),
        tools=ToolRegistry([UpdatingTool()]),
    )
    harness = AgentHarness(agent)

    await harness.run_prompt("go")
    snapshot = harness.last_snapshot

    assert isinstance(snapshot, RequestSnapshot)
    assert len(snapshot.turns) == 2
    assert all(isinstance(turn, TurnSnapshot) for turn in snapshot.turns)
    assert [turn.index for turn in snapshot.turns] == [0, 1]
    assert [len(turn.tool_calls) for turn in snapshot.turns] == [1, 0]
    assert [len(turn.tool_results) for turn in snapshot.turns] == [1, 0]
    assert snapshot.turns[0].events[0].type == "turn_start"
    assert snapshot.turns[0].events[-1].type == "turn_end"
    assert snapshot.turns[1].messages_before == snapshot.turns[0].messages_after


@pytest.mark.asyncio
async def test_length_with_tool_calls_returns_safe_results_without_execution() -> None:
    tool = UpdatingTool()
    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=_two_turn_client(first_stop="length"),
        tools=ToolRegistry([tool]),
    )]

    assert tool.calls == 0
    assert not any(event.type.startswith("tool_execution_") for event in events)
    result_messages = [
        event.message
        for event in events
        if event.type == "message_end" and isinstance(event.message, ToolResultMessage)
    ]
    assert len(result_messages) == 1
    assert result_messages[0].is_error is True
    assert result_messages[0].details == {
        "error_type": "IncompleteToolCall",
        "stop_reason": "length",
    }
    assert sum(event.type == "turn_end" for event in events) == 2


@pytest.mark.asyncio
async def test_signal_reaches_before_hook_tool_and_after_hook() -> None:
    signal = asyncio.Event()
    tool = UpdatingTool()
    seen: list[tuple[str, asyncio.Event | None]] = []

    async def before(
        context: BeforeToolCallContext,
        *,
        signal: asyncio.Event | None = None,
    ) -> BeforeToolCallResult:
        seen.append(("before", signal))
        return BeforeToolCallResult()

    async def after(
        context: AfterToolCallContext,
        *,
        signal: asyncio.Event | None = None,
    ) -> ToolResult:
        seen.append(("after", signal))
        return context.result

    _ = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=_two_turn_client(),
        tools=ToolRegistry([tool]),
        before_tool_call=before,
        after_tool_call=after,
        signal=signal,
    )]

    assert seen == [("before", signal), ("after", signal)]
    assert tool.signal is signal


@pytest.mark.asyncio
async def test_prepare_then_should_stop_after_turn() -> None:
    calls: list[str] = []
    client = _two_turn_client()

    def prepare(context):
        calls.append("prepare")
        return list(context.messages)

    async def should_stop(context) -> bool:
        calls.append("stop")
        return True

    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=client,
        tools=ToolRegistry([UpdatingTool()]),
        prepare_next_turn=prepare,
        should_stop_after_turn=should_stop,
    )]

    assert calls == ["prepare", "stop"]
    assert sum(event.type == "turn_start" for event in events) == 1
    assert [event.type for event in events[-2:]] == ["turn_end", "agent_end"]


@pytest.mark.asyncio
async def test_turn_controls_also_run_before_natural_agent_end() -> None:
    calls: list[str] = []

    def prepare(context):
        calls.append("prepare")
        return None

    def should_stop(context) -> bool:
        calls.append("stop")
        return False

    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=FakeClient([[DoneEvent(stop_reason="stop")]]),
        prepare_next_turn=prepare,
        should_stop_after_turn=should_stop,
    )]

    assert calls == ["prepare", "stop"]
    assert [event.type for event in events[-2:]] == ["turn_end", "agent_end"]


class DelayedTool(AgentTool):
    label = "Delayed"
    description = "Finish after a configured delay."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.delay = delay

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update=None,
    ) -> ToolResult:
        await asyncio.sleep(self.delay)
        if on_update is not None:
            await on_update(ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(text=f"{self.name}-update")],
            ))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=f"{self.name}-done")],
        )


@pytest.mark.asyncio
async def test_golden_parallel_event_order() -> None:
    client = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(id="a", name="slow", arguments={})),
            ToolCallEvent(tool_call=ToolCall(id="b", name="fast", arguments={})),
            DoneEvent(stop_reason="tool_use"),
        ],
        [DoneEvent(stop_reason="stop")],
    ])
    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=client,
        tools=ToolRegistry([
            DelayedTool("slow", 0.03),
            DelayedTool("fast", 0.001),
        ]),
    )]

    lifecycle: list[str] = []
    for event in events:
        if event.type.startswith("tool_execution_"):
            lifecycle.append(f"{event.type}:{event.tool_call.id}")
        elif event.type in {"message_start", "message_end"} and isinstance(
            event.message, ToolResultMessage
        ):
            lifecycle.append(f"{event.type}:result:{event.message.tool_call_id}")

    assert lifecycle == [
        "tool_execution_start:a",
        "tool_execution_start:b",
        "tool_execution_update:b",
        "tool_execution_end:b",
        "tool_execution_update:a",
        "tool_execution_end:a",
        "message_start:result:a",
        "message_end:result:a",
        "message_start:result:b",
        "message_end:result:b",
    ]


def test_legacy_request_shaped_snapshot_is_readable() -> None:
    snapshot = RequestSnapshot.model_validate({
        "id": "legacy",
        "request_type": "prompt",
        "user_text": "old",
        "status": "completed",
        "events": [],
        "tool_calls": [],
        "tool_results": [],
    })
    assert snapshot.id == "legacy"
    assert snapshot.turns == []


@pytest.mark.asyncio
async def test_generation_usage_and_provider_latency_are_persisted_on_message() -> None:
    client = FakeClient([[
        TextDeltaEvent(delta="done"),
        DoneEvent(
            stop_reason="stop",
            usage=Usage(input=12, output=3, total_tokens=15),
        ),
    ]])
    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=client,
    )]
    message = next(
        event.message
        for event in events
        if event.type == "message_end"
        and getattr(event.message, "role", None) == "assistant"
    )
    assert message.usage.total_tokens == 15
    assert message.generation_metrics is not None
    assert message.generation_metrics.latency_ms is not None
    assert message.generation_metrics.usage_available is True
    assert message.generation_metrics.time_to_first_token_ms is not None


@pytest.mark.asyncio
async def test_before_model_call_runs_for_first_and_tool_followup_calls() -> None:
    seen: list[tuple[int, int, int]] = []

    async def before_model_call(context):
        seen.append((context.turn_index, len(context.messages), len(context.tools)))
        return ModelCallDecision()

    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=_two_turn_client(),
        tools=ToolRegistry([UpdatingTool()]),
        before_model_call=before_model_call,
    )]
    assert [turn for turn, _, _ in seen] == [1, 2]
    assert all(tool_count == 1 for _, _, tool_count in seen)
    assert [event.type for event in events].count("turn_end") == 2


@pytest.mark.asyncio
async def test_before_model_call_denial_is_a_safe_terminal_assistant() -> None:
    events = [event async for event in run_event_loop(
        system_prompt="sys",
        user_text="go",
        client=FakeClient([[TextDeltaEvent(delta="must not stream")]]),
        before_model_call=lambda context: ModelCallDecision(
            allow=False,
            error_message="context budget exceeded",
        ),
    )]
    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assistant = events[-2].message
    assert assistant.stop_reason == "error"
    assert assistant.error_message == "context budget exceeded"
    assert assistant.generation_metrics.latency_ms == 0

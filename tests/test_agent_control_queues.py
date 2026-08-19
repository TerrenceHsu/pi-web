"""pi-agent-compatible steering and follow-up queue contracts."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AgentTool,
    DoneEvent,
    FakeClient,
    QueueMode,
    TextContent,
    ToolCall,
    ToolCallEvent,
    ToolRegistry,
    ToolResult,
)
from pi_agent_core_py.llm_messages import LLMMessage, LLMUserMessage


def _user_texts(messages: list[LLMMessage]) -> list[str]:
    return [
        block.text
        for message in messages
        if isinstance(message, LLMUserMessage)
        for block in message.content
    ]


class _EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "Return a fixed value."
    parameters = {"type": "object", "properties": {}}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal=None,
        on_update=None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="ok")],
        )


@pytest.mark.asyncio
async def test_steering_one_at_a_time_drains_one_message_per_turn() -> None:
    client = FakeClient(
        [
            [DoneEvent(stop_reason="stop")],
            [DoneEvent(stop_reason="stop")],
        ]
    )
    agent = Agent(system_prompt="sys", client=client)
    agent.steer("steer-1")
    agent.steer("steer-2")

    await agent.prompt("prompt")

    assert agent.steering_mode == "one-at-a-time"
    assert [_user_texts(call) for call in client.all_messages_calls] == [
        ["prompt", "steer-1"],
        ["prompt", "steer-1", "steer-2"],
    ]
    assert agent.has_queued_messages() is False


@pytest.mark.asyncio
async def test_steering_all_drains_the_whole_queue_before_one_model_call() -> None:
    client = FakeClient([[DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=client, steering_mode="all")
    agent.steer("steer-1")
    agent.steer("steer-2")

    await agent.prompt("prompt")

    assert len(client.all_messages_calls) == 1
    assert _user_texts(client.all_messages_calls[0]) == [
        "prompt",
        "steer-1",
        "steer-2",
    ]


@pytest.mark.asyncio
async def test_follow_up_one_at_a_time_runs_only_after_natural_stop() -> None:
    client = FakeClient(
        [
            [DoneEvent(stop_reason="stop")],
            [DoneEvent(stop_reason="stop")],
            [DoneEvent(stop_reason="stop")],
        ]
    )
    agent = Agent(system_prompt="sys", client=client)
    agent.follow_up("follow-1")
    agent.follow_up("follow-2")

    await agent.prompt("prompt")

    assert agent.follow_up_mode == "one-at-a-time"
    assert [_user_texts(call) for call in client.all_messages_calls] == [
        ["prompt"],
        ["prompt", "follow-1"],
        ["prompt", "follow-1", "follow-2"],
    ]


@pytest.mark.asyncio
async def test_follow_up_all_drains_the_whole_queue_into_one_later_turn() -> None:
    client = FakeClient([
        [DoneEvent(stop_reason="stop")],
        [DoneEvent(stop_reason="stop")],
    ])
    agent = Agent(system_prompt="sys", client=client, follow_up_mode="all")
    agent.follow_up("follow-1")
    agent.follow_up("follow-2")

    await agent.prompt("prompt")

    assert [_user_texts(call) for call in client.all_messages_calls] == [
        ["prompt"],
        ["prompt", "follow-1", "follow-2"],
    ]


@pytest.mark.asyncio
async def test_steering_is_consumed_before_follow_up() -> None:
    client = FakeClient([
        [DoneEvent(stop_reason="stop")],
        [DoneEvent(stop_reason="stop")],
    ])
    agent = Agent(system_prompt="sys", client=client)
    agent.steer("steer")
    agent.follow_up("follow")

    await agent.prompt("prompt")

    assert [_user_texts(call) for call in client.all_messages_calls] == [
        ["prompt", "steer"],
        ["prompt", "steer", "follow"],
    ]


@pytest.mark.asyncio
async def test_follow_up_waits_until_tool_continuation_finishes() -> None:
    client = FakeClient(
        [
            [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="call-1",
                        name="echo",
                        arguments={},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [DoneEvent(stop_reason="stop")],
            [DoneEvent(stop_reason="stop")],
        ]
    )
    agent = Agent(
        system_prompt="sys",
        client=client,
        tools=ToolRegistry([_EchoTool()]),
    )
    agent.follow_up("after-tools")

    await agent.prompt("prompt")

    assert _user_texts(client.all_messages_calls[0]) == ["prompt"]
    assert _user_texts(client.all_messages_calls[1]) == ["prompt"]
    assert _user_texts(client.all_messages_calls[2]) == [
        "prompt",
        "after-tools",
    ]


@pytest.mark.asyncio
async def test_active_turn_steering_is_injected_before_next_model_call() -> None:
    client = FakeClient(
        [
            [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="call-1",
                        name="echo",
                        arguments={},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [DoneEvent(stop_reason="stop")],
        ]
    )
    agent = Agent(
        system_prompt="sys",
        client=client,
        tools=ToolRegistry([_EchoTool()]),
    )
    queued = False

    def on_event(event, _state) -> None:
        nonlocal queued
        if event.type == "turn_end" and not queued:
            queued = True
            agent.steer("change-course")

    agent.subscribe(on_event)
    await agent.prompt("prompt")

    assert _user_texts(client.all_messages_calls[1]) == [
        "prompt",
        "change-course",
    ]


def test_queue_modes_and_clear_apis_are_independent() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([]),
        follow_up_mode="all",
    )
    agent.steer("steer")
    agent.follow_up("follow")
    agent.clear_steering_queue()
    assert agent.has_queued_messages() is True

    agent.clear_follow_up_queue()
    assert agent.has_queued_messages() is False

    agent.steering_mode = "all"
    agent.follow_up_mode = "one-at-a-time"
    assert (agent.steering_mode, agent.follow_up_mode) == (
        "all",
        "one-at-a-time",
    )

    with pytest.raises(ValueError, match="queue mode"):
        agent.steering_mode = cast(QueueMode, "invalid")

    agent.steer("steer")
    agent.follow_up("follow")
    agent.clear_all_queues()
    assert agent.has_queued_messages() is False


def test_reset_clears_control_queues() -> None:
    agent = Agent(system_prompt="sys", client=FakeClient([]))
    agent.steer("steer")
    agent.follow_up("follow")

    agent.reset()

    assert agent.has_queued_messages() is False


def _make_blocked_agent() -> tuple[
    Agent,
    FakeClient,
    asyncio.Event,
    asyncio.Event,
]:
    entered = asyncio.Event()
    release = asyncio.Event()
    client = FakeClient([
        [DoneEvent(stop_reason="stop")],
        [DoneEvent(stop_reason="stop")],
    ])

    async def before_model_call(_context) -> None:
        entered.set()
        await release.wait()

    return (
        Agent(
            system_prompt="sys",
            client=client,
            before_model_call=before_model_call,
        ),
        client,
        entered,
        release,
    )


@pytest.mark.asyncio
async def test_active_agent_rejects_prompt_and_continue_before_enqueue() -> None:
    agent, client, entered, release = _make_blocked_agent()
    event_types: list[str] = []
    agent.subscribe(lambda event, _state: event_types.append(event.type))
    first = asyncio.create_task(agent.prompt("first"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    try:
        with pytest.raises(RuntimeError, match=r"steer\(\).*follow_up\(\)"):
            await agent.prompt("second")
        with pytest.raises(RuntimeError, match=r"steer\(\).*follow_up\(\)"):
            await agent.continue_()

        assert agent.state.status == "running"
        assert agent.state.queue_size == 0
        assert event_types.count("request_queued") == 1
    finally:
        release.set()
        await first

    # Rejection is scoped to the active lifecycle; a settled Agent accepts
    # another ordinary request normally.
    await agent.prompt("after-idle")
    assert len(client.all_messages_calls) == 2


@pytest.mark.asyncio
async def test_pending_request_rejects_second_prompt_before_worker_starts() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[DoneEvent(stop_reason="stop")]]),
    )
    queued = asyncio.Event()
    release = asyncio.Event()
    blocked_first_event = False

    async def on_event(event, _state) -> None:
        nonlocal blocked_first_event
        if event.type == "request_queued" and not blocked_first_event:
            blocked_first_event = True
            queued.set()
            await release.wait()

    agent.subscribe(on_event)
    first = asyncio.create_task(agent.prompt("first"))
    await asyncio.wait_for(queued.wait(), timeout=1)

    try:
        assert agent.state.status == "idle"
        assert agent.state.queue_size == 1
        with pytest.raises(RuntimeError, match="already processing"):
            await agent.prompt("second")
        assert agent.state.queue_size == 1
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_harness_busy_error_points_to_control_queue_apis() -> None:
    agent, _client, entered, release = _make_blocked_agent()
    harness = AgentHarness(agent)
    first = asyncio.create_task(harness.run_prompt("first"))
    await asyncio.wait_for(entered.wait(), timeout=1)

    try:
        with pytest.raises(
            RuntimeError,
            match=r"harness\.agent\.steer\(\).*harness\.agent\.follow_up\(\)",
        ):
            await harness.run_prompt("second")
    finally:
        release.set()
        await first

    assert harness.context.phase == "idle"

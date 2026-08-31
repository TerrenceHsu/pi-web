"""Compatibility-first contracts aligned with pi's core Agent runtime."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py import (
    AfterToolCallResult,
    Agent,
    AgentEndEvent,
    AgentLoopTurnUpdate,
    AgentTool,
    AssistantMessage,
    BeforeToolCallResult,
    CustomMessage,
    DoneEvent,
    FakeClient,
    ImageContent,
    LLMUserMessage,
    TextContent,
    ToolCall,
    ToolCallEvent,
    ToolExecutionEndEvent,
    ToolRegistry,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    deserialize_message,
    serialize_message,
)


class _CapturedUpdateTool(AgentTool):
    name = "captured"
    label = "Captured"
    description = "Capture the update callback and finish immediately."
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.update = None

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update=None,
    ) -> ToolResult:
        self.update = on_update
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="done")],
            terminate=True,
        )


class _SlowTool(AgentTool):
    name = "slow"
    label = "Slow"
    description = "Wait until the test releases the tool."
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update=None,
    ) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="done")],
            terminate=True,
        )


@pytest.mark.asyncio
async def test_parallel_tool_ignores_updates_after_that_tool_settles() -> None:
    captured = _CapturedUpdateTool()
    slow = _SlowTool()
    agent = Agent(
        system_prompt="sys",
        client=FakeClient(
            [[
                ToolCallEvent(
                    tool_call=ToolCall(id="captured-1", name="captured", arguments={})
                ),
                ToolCallEvent(
                    tool_call=ToolCall(id="slow-1", name="slow", arguments={})
                ),
                DoneEvent(stop_reason="tool_use"),
            ]]
        ),
        tools=ToolRegistry([captured, slow]),
    )
    event_types: list[str] = []
    captured_ended = asyncio.Event()

    def observe(event, _state) -> None:
        event_types.append(event.type)
        if (
            isinstance(event, ToolExecutionEndEvent)
            and event.tool_call.id == "captured-1"
        ):
            captured_ended.set()

    agent.subscribe(observe)
    task = asyncio.create_task(agent.prompt("run"))
    await asyncio.wait_for(slow.started.wait(), timeout=1)
    await asyncio.wait_for(captured_ended.wait(), timeout=1)
    updates_before = event_types.count("tool_execution_update")

    assert captured.update is not None
    await captured.update(
        ToolResult(
            tool_call_id="captured-1",
            name="captured",
            content=[TextContent(text="late")],
        )
    )
    await asyncio.sleep(0)
    assert event_types.count("tool_execution_update") == updates_before

    slow.release.set()
    await task


@pytest.mark.asyncio
async def test_unexpected_loop_failure_still_emits_terminal_lifecycle() -> None:
    async def fail_transform(messages):
        raise RuntimeError("transform failed")

    agent = Agent(
        system_prompt="sys",
        client=FakeClient([]),
        transform_context_fn=fail_transform,
    )
    event_types: list[str] = []
    agent.subscribe(lambda event, _state: event_types.append(event.type))

    with pytest.raises(RuntimeError, match="transform failed"):
        await agent.prompt("hello")

    assert event_types[-5:] == [
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
        "request_end",
    ]
    assert isinstance(agent.state.messages[-1], AssistantMessage)
    assert agent.state.messages[-1].stop_reason == "error"

    async def fail_after_turn(context):
        raise RuntimeError("stop callback failed")

    after_turn_agent = Agent(
        system_prompt="sys",
        client=FakeClient([[DoneEvent(stop_reason="stop")]]),
        should_stop_after_turn=fail_after_turn,
    )
    after_turn_events: list[str] = []
    after_turn_agent.subscribe(
        lambda event, _state: after_turn_events.append(event.type)
    )

    with pytest.raises(RuntimeError, match="stop callback failed"):
        await after_turn_agent.prompt("hello")

    assert after_turn_events.count("turn_end") == 1
    assert after_turn_events[-2:] == ["agent_end", "request_end"]


@pytest.mark.asyncio
async def test_continue_from_assistant_tail_consumes_queued_steering() -> None:
    client = FakeClient([[DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=client)
    agent.state.messages = [
        UserMessage(content=[TextContent(text="first")]),
        AssistantMessage(
            content=[TextContent(text="done")],
            api="fake",
            provider="fake",
            model="fake-1",
        ),
    ]
    agent.steer("steered")

    await agent.continue_()

    assert isinstance(client.last_messages[-1], LLMUserMessage)
    assert client.last_messages[-1].content[0].text == "steered"
    assert agent.has_queued_messages() is False


@pytest.mark.asyncio
async def test_prompt_accepts_images_and_agent_message_batches() -> None:
    image = ImageContent(data="aGVsbG8=", mime_type="image/png")
    custom = CustomMessage(custom_type="notice", content="internal")
    converted_inputs: list[list[Any]] = []
    transform_signals: list[asyncio.Event | None] = []

    async def transform(messages, abort_signal=None):
        transform_signals.append(abort_signal)
        return messages

    async def convert(messages):
        converted_inputs.append(list(messages))
        return [LLMUserMessage(content=[TextContent(text="converted")])]

    agent = Agent(
        system_prompt="sys",
        client=FakeClient(
            [
                [DoneEvent(stop_reason="stop")],
                [DoneEvent(stop_reason="stop")],
            ]
        ),
        transform_context_fn=transform,
        convert_to_llm_fn=convert,
    )

    await agent.prompt("look", images=[image])
    await agent.prompt([custom])

    first_user = agent.state.messages[0]
    assert isinstance(first_user, UserMessage)
    assert first_user.content == [TextContent(text="look"), image]
    assert any(isinstance(message, CustomMessage) for message in agent.state.messages)
    assert any(isinstance(message, CustomMessage) for message in converted_inputs[-1])
    assert all(isinstance(signal, asyncio.Event) for signal in transform_signals)
    assert deserialize_message(serialize_message(custom)) == custom


@pytest.mark.asyncio
async def test_agent_end_exposes_full_transcript_and_per_run_delta() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient(
            [
                [DoneEvent(stop_reason="stop")],
                [DoneEvent(stop_reason="stop")],
            ]
        ),
    )
    ends: list[AgentEndEvent] = []
    agent.subscribe(
        lambda event, _state: ends.append(event)
        if isinstance(event, AgentEndEvent)
        else None
    )

    await agent.prompt("first")
    await agent.prompt("second")

    assert isinstance(agent.client, FakeClient)
    assert agent.client.last_thinking_level is None
    assert len(ends[-1].messages) == 4
    assert len(ends[-1].new_messages) == 2
    assert isinstance(ends[-1].new_messages[0], UserMessage)
    assert isinstance(ends[-1].new_messages[1], AssistantMessage)


class _PreparedArgumentsTool(AgentTool):
    name = "prepared"
    label = "Prepared"
    description = "Normalize legacy arguments before execution."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self.executed_args: dict[str, Any] | None = None

    def prepare_arguments(self, args: Any) -> dict[str, Any]:
        return {"value": str(args["legacy"])}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update=None,
    ) -> ToolResult:
        self.executed_args = args
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="original")],
            details={"preserved": True},
            added_tool_names=["deferred"],
        )


@pytest.mark.asyncio
async def test_tool_prepare_and_after_hook_field_overrides() -> None:
    tool = _PreparedArgumentsTool()
    before_args: list[dict[str, Any]] = []

    async def before(context, signal=None):
        before_args.append(context.tool_call.arguments)
        return BeforeToolCallResult()

    async def after(context, signal=None):
        return AfterToolCallResult(
            content=[TextContent(text="patched")],
            terminate=True,
        )

    agent = Agent(
        system_prompt="sys",
        client=FakeClient(
            [[
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="prepared-1",
                        name="prepared",
                        arguments={"legacy": 7},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ]]
        ),
        tools=[tool],
        before_tool_call=before,
        after_tool_call=after,
    )

    await agent.prompt("run")

    result = next(
        message
        for message in agent.state.messages
        if isinstance(message, ToolResultMessage)
    )
    assert before_args == [{"value": "7"}]
    assert tool.executed_args == {"value": "7"}
    assert result.content == [TextContent(text="patched")]
    assert result.details == {"preserved": True}
    assert result.added_tool_names == ["deferred"]
    assert result.terminate is True


@pytest.mark.asyncio
async def test_blocked_before_hook_can_terminate_the_tool_chain() -> None:
    async def before(context, signal=None):
        return BeforeToolCallResult(
            allow=False,
            reason="blocked",
            terminate=True,
        )

    client = FakeClient(
        [[
            ToolCallEvent(
                tool_call=ToolCall(
                    id="blocked-1",
                    name="prepared",
                    arguments={"legacy": 1},
                )
            ),
            DoneEvent(stop_reason="tool_use"),
        ]]
    )
    agent = Agent(
        system_prompt="sys",
        client=client,
        tools=[_PreparedArgumentsTool()],
        before_tool_call=before,
    )

    await agent.prompt("run")

    result = next(
        message
        for message in agent.state.messages
        if isinstance(message, ToolResultMessage)
    )
    assert result.is_error is True
    assert result.terminate is True
    assert len(client.all_messages_calls) == 1


@pytest.mark.asyncio
async def test_prepare_next_turn_replaces_runtime_inputs() -> None:
    first_client = FakeClient(
        [[
            ToolCallEvent(
                tool_call=ToolCall(
                    id="prepared-1",
                    name="prepared",
                    arguments={"legacy": 1},
                )
            ),
            DoneEvent(stop_reason="tool_use"),
        ]]
    )
    second_client = FakeClient([[DoneEvent(stop_reason="stop")]])
    prepared_deltas: list[tuple[Any, ...]] = []

    async def prepare(context):
        prepared_deltas.append(context.new_messages)
        return AgentLoopTurnUpdate(
            system_prompt="replacement-system",
            client=second_client,
            tools=[],
            thinking_level="high",
        )

    agent = Agent(
        system_prompt="initial-system",
        client=first_client,
        tools=[_PreparedArgumentsTool()],
        prepare_next_turn=prepare,
        thinking_level="low",
    )

    await agent.prompt("run")

    assert first_client.last_thinking_level == "low"
    assert second_client.last_system_prompt == "replacement-system"
    assert second_client.last_thinking_level == "high"
    assert second_client.last_tools is None
    assert [message.role for message in prepared_deltas[0]] == [
        "user",
        "assistant",
        "toolResult",
    ]


def test_public_configuration_stays_synchronized() -> None:
    registry = ToolRegistry()
    agent = Agent(
        system_prompt="initial",
        client=FakeClient([]),
        tools=registry,
    )

    agent.system_prompt = "replacement"
    registry.register(_PreparedArgumentsTool())
    assert agent.state.system_prompt == "replacement"
    assert agent.state.active_tool_names == ("prepared",)

    registry.unregister("prepared")
    assert agent.state.active_tool_names == ()

    replacement = ToolRegistry([_PreparedArgumentsTool()])
    agent.tools = replacement
    assert agent.state.active_tool_names == ("prepared",)

    replacement.unregister("prepared")
    assert agent.state.active_tool_names == ()

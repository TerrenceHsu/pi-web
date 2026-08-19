"""Agent-level content block lifecycle and legacy stream normalization."""

from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    AssistantMessage,
    DoneEvent,
    FakeClient,
    MessageUpdateEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolExecutionStartEvent,
    run_event_loop,
)
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult


def _update_types(events: list[Any]) -> list[str]:
    return [
        event.assistant_message_event.type
        for event in events
        if isinstance(event, MessageUpdateEvent) and event.assistant_message_event is not None
    ]


@pytest.mark.asyncio
async def test_agent_normalizes_legacy_deltas_to_balanced_lifecycles() -> None:
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        TextDeltaEvent(delta="answer"),
                        ThinkingDeltaEvent(
                            delta="reason",
                            thinking_signature="reasoning_content",
                        ),
                        DoneEvent(stop_reason="stop"),
                    ]
                ]
            ),
        )
    ]

    assert _update_types(events) == [
        "text_start",
        "text_delta",
        "thinking_start",
        "thinking_delta",
        "text_end",
        "thinking_end",
    ]
    updates = [event for event in events if isinstance(event, MessageUpdateEvent)]
    assert [
        update.assistant_message_event.content_index  # type: ignore[union-attr]
        for update in updates
    ] == [0, 0, 1, 1, 0, 1]


@pytest.mark.asyncio
async def test_agent_preserves_explicit_content_indices_and_block_order() -> None:
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ThinkingStartEvent(
                            content_index=0,
                            thinking_signature="signed",
                        ),
                        ThinkingDeltaEvent(content_index=0, delta="reason"),
                        ThinkingEndEvent(
                            content_index=0,
                            content="reason",
                            thinking_signature="signed",
                        ),
                        TextStartEvent(content_index=2),
                        TextDeltaEvent(content_index=2, delta="answer"),
                        TextEndEvent(content_index=2, content="answer"),
                        DoneEvent(stop_reason="stop"),
                    ]
                ]
            ),
        )
    ]

    assert _update_types(events) == [
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_start",
        "text_delta",
        "text_end",
    ]
    end = next(event for event in events if isinstance(event, AgentEndEvent))
    assistant = next(message for message in end.messages if isinstance(message, AssistantMessage))
    assert [block.type for block in assistant.content] == ["thinking", "text"]
    assert isinstance(assistant.content[0], ThinkingContent)
    assert assistant.content[0].thinking_signature == "signed"
    assert isinstance(assistant.content[1], TextContent)
    assert assistant.content[1].text == "answer"


class _RecordingEchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "Echo text"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: Any = None,
        on_update: Any = None,
    ) -> ToolResult:
        self.calls.append((tool_call_id, args))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=str(args.get("text", "")))],
        )


@pytest.mark.asyncio
async def test_tool_call_becomes_executable_only_after_toolcall_end() -> None:
    tool = _RecordingEchoTool()
    call = ToolCall(id="call-1", name="echo", arguments={"text": "hello"})
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ToolCallStartEvent(
                            content_index=0,
                            tool_call_id=call.id,
                            name=call.name,
                        ),
                        ToolCallDeltaEvent(
                            content_index=0,
                            delta='{"text":"hello"}',
                            tool_call_id=call.id,
                            name=call.name,
                        ),
                        ToolCallEndEvent(content_index=0, tool_call=call),
                        DoneEvent(stop_reason="tool_use"),
                    ],
                    [
                        TextDeltaEvent(delta="done"),
                        DoneEvent(stop_reason="stop"),
                    ],
                ]
            ),
            tools=ToolRegistry([tool]),
        )
    ]

    tool_updates = [
        event
        for event in events
        if isinstance(event, MessageUpdateEvent)
        and event.assistant_message_event is not None
        and event.assistant_message_event.type.startswith("toolcall_")
    ]
    assert [event.assistant_message_event.type for event in tool_updates] == [
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
    ]
    execution_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolExecutionStartEvent)
    )
    end_index = events.index(tool_updates[-1])
    assert end_index < execution_index
    assert tool.calls == [("call-1", {"text": "hello"})]


def test_nested_message_update_serializes_tool_delta_fields() -> None:
    event = MessageUpdateEvent(
        message=AssistantMessage(
            content=[],
            api="test-api",
            provider="test-provider",
            model="test-model",
        ),
        assistant_message_event=ToolCallDeltaEvent(
            content_index=3,
            delta='{"x":',
            tool_call_id="call-3",
            name="lookup",
        ),
    )

    assert event.model_dump(mode="json")["assistant_message_event"] == {
        "type": "toolcall_delta",
        "content_index": 3,
        "delta": '{"x":',
        "tool_call_id": "call-3",
        "name": "lookup",
    }

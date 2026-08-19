"""Thinking/reasoning content model, loop, context, and persistence contracts."""

from __future__ import annotations

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    AssistantMessage,
    DoneEvent,
    FakeClient,
    MessageUpdateEvent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ToolCall,
    convert_to_llm,
    estimate_context,
    run_event_loop,
)
from pi_agent_core_py.llm_messages import LLMAssistantMessage
from pi_agent_core_py.session import deserialize_message, serialize_message


def _assistant_with_thinking() -> AssistantMessage:
    return AssistantMessage(
        content=[
            ThinkingContent(
                thinking="inspect the constraints",
                thinking_signature="reasoning_content",
            ),
            TextContent(text="final answer"),
        ],
        api="openai-completions",
        provider="test",
        model="reasoning-model",
    )


def test_thinking_content_serialization_round_trip() -> None:
    restored = deserialize_message(serialize_message(_assistant_with_thinking()))

    assert isinstance(restored, AssistantMessage)
    thinking = restored.content[0]
    assert isinstance(thinking, ThinkingContent)
    assert thinking.thinking == "inspect the constraints"
    assert thinking.thinking_signature == "reasoning_content"
    assert thinking.redacted is False


def test_convert_to_llm_preserves_thinking_content_and_signature() -> None:
    converted = convert_to_llm([_assistant_with_thinking()])

    assert len(converted) == 1
    assert isinstance(converted[0], LLMAssistantMessage)
    assert isinstance(converted[0].content[0], ThinkingContent)
    assert converted[0].content[0].thinking_signature == "reasoning_content"


@pytest.mark.asyncio
async def test_loop_accumulates_thinking_deltas_in_source_block_order() -> None:
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        TextDeltaEvent(delta="answer first"),
                        ThinkingDeltaEvent(
                            delta="reason later",
                            thinking_signature="reasoning_content",
                        ),
                        DoneEvent(stop_reason="stop"),
                    ]
                ]
            ),
        )
    ]

    updates = [event for event in events if isinstance(event, MessageUpdateEvent)]
    thinking_update = next(
        update
        for update in updates
        if isinstance(update.assistant_message_event, ThinkingDeltaEvent)
    )
    serialized_update = thinking_update.model_dump(mode="json")
    assert serialized_update["assistant_message_event"] == {
        "type": "thinking_delta",
        "delta": "reason later",
        "content_index": 1,
        "thinking_signature": "reasoning_content",
        "redacted": False,
    }
    end = next(event for event in events if isinstance(event, AgentEndEvent))
    assistant = next(message for message in end.messages if isinstance(message, AssistantMessage))
    assert [block.type for block in assistant.content] == ["text", "thinking"]
    thinking = assistant.content[1]
    assert isinstance(thinking, ThinkingContent)
    assert thinking.thinking == "reason later"
    assert thinking.thinking_signature == "reasoning_content"
    assert assistant.generation_metrics is not None
    assert assistant.generation_metrics.time_to_first_token_ms is not None


@pytest.mark.asyncio
async def test_loop_preserves_redacted_thinking_payload() -> None:
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ThinkingDeltaEvent(
                            delta="",
                            thinking_signature="opaque-encrypted-payload",
                            redacted=True,
                        ),
                        DoneEvent(stop_reason="stop"),
                    ]
                ]
            ),
        )
    ]

    end = next(event for event in events if isinstance(event, AgentEndEvent))
    assistant = next(message for message in end.messages if isinstance(message, AssistantMessage))
    thinking = assistant.content[0]
    assert isinstance(thinking, ThinkingContent)
    assert thinking.thinking == ""
    assert thinking.thinking_signature == "opaque-encrypted-payload"
    assert thinking.redacted is True


@pytest.mark.asyncio
async def test_aborted_done_discards_partial_thinking() -> None:
    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ThinkingDeltaEvent(delta="partial private reasoning"),
                        DoneEvent(stop_reason="aborted"),
                    ]
                ]
            ),
        )
    ]

    end = next(event for event in events if isinstance(event, AgentEndEvent))
    assistant = next(message for message in end.messages if isinstance(message, AssistantMessage))
    assert assistant.stop_reason == "aborted"
    assert assistant.content == []


def test_context_separates_thinking_from_tool_calls() -> None:
    message = _assistant_with_thinking().model_copy(
        update={
            "content": [
                ThinkingContent(thinking="reason"),
                ToolCall(id="call-1", name="echo", arguments={}),
            ]
        }
    )

    converted = convert_to_llm([message])[0]

    assert isinstance(converted, LLMAssistantMessage)
    assert isinstance(converted.content[0], ThinkingContent)
    assert converted.tool_calls[0].id == "call-1"


def test_context_budget_counts_thinking_content() -> None:
    converted = convert_to_llm([_assistant_with_thinking()])

    estimate = estimate_context(
        system_prompt="sys",
        messages=converted,
        context_window=None,
    )

    assert estimate.message_tokens > 0

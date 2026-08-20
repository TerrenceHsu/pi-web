from __future__ import annotations

import pytest

from pi_agent_core_py.compaction import (
    CompactionConfig,
    CompactionRetryEvent,
    CompactionRetryPolicy,
    compact_messages,
)
from pi_agent_core_py.context import SUMMARY_CONTEXT_PREFIX, convert_to_llm
from pi_agent_core_py.context_budget import (
    classify_context_budget,
    estimate_context,
    estimate_text_tokens,
)
from pi_agent_core_py.messages import (
    AssistantMessage,
    FileBlock,
    SummaryMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from pi_agent_core_py.providers.errors import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderStreamError,
)
from pi_agent_core_py.tools import ToolDef


def test_mixed_language_estimator_is_deterministic_and_conservative() -> None:
    assert estimate_text_tokens("abcdefgh") == 2
    assert estimate_text_tokens("上下文") == 3
    assert estimate_text_tokens("abcd上下") == 3


def test_context_estimate_includes_system_messages_tools_and_output_reserve() -> None:
    messages = convert_to_llm([
        UserMessage(content=[TextContent(text="hello")]),
    ])
    estimate = estimate_context(
        system_prompt="system",
        messages=messages,
        tools=[ToolDef(
            name="lookup",
            label="Lookup",
            description="Look up a value",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        )],
        context_window=10_000,
        reserved_output_tokens=1_000,
    )
    assert estimate.system_prompt_tokens > 0
    assert estimate.message_tokens > 0
    assert estimate.tool_definition_tokens > 0
    assert estimate.estimated_input_tokens == (
        estimate.system_prompt_tokens
        + estimate.message_tokens
        + estimate.tool_definition_tokens
    )
    assert estimate.projected_tokens == estimate.estimated_input_tokens + 1_000
    assert estimate.approximate is True
    assert estimate.estimator_version == "mixed-char-v1"


def test_attachment_metadata_is_counted_once_inside_converted_message() -> None:
    plain = convert_to_llm([
        UserMessage(content=[TextContent(text="read")]),
    ])
    attached = convert_to_llm([
        UserMessage(content=[
            TextContent(text="read"),
            FileBlock(
                file_id="file-1",
                name="notes.md",
                mime="text/markdown",
                size=42,
                sha256="abc",
                format="markdown",
            ),
        ]),
    ])
    base = estimate_context(
        system_prompt="s",
        messages=plain,
        context_window=None,
    )
    with_file = estimate_context(
        system_prompt="s",
        messages=attached,
        context_window=None,
    )
    assert with_file.message_tokens > base.message_tokens
    assert with_file.level == "unknown"


def test_context_threshold_contract() -> None:
    assert classify_context_budget(None, None) == "unknown"
    assert classify_context_budget(0.6999, 0.8) == "normal"
    assert classify_context_budget(0.70, 0.8) == "warning"
    assert classify_context_budget(0.85, 0.9) == "compact"
    assert classify_context_budget(0.95, 0.99) == "blocked"
    assert classify_context_budget(0.80, 1.001) == "blocked"


@pytest.mark.asyncio
async def test_turn_safe_compaction_never_splits_tool_call_results() -> None:
    messages = [
        UserMessage(content=[TextContent(text="turn one")]),
        AssistantMessage(
            content=[
                ToolCall(id="a", name="one"),
                ToolCall(id="b", name="two"),
            ],
            api="fake", provider="fake", model="fake",
            stop_reason="tool_use",
        ),
        ToolResultMessage(tool_call_id="a", name="one", content=[TextContent(text="A")]),
        ToolResultMessage(tool_call_id="b", name="two", content=[TextContent(text="B")]),
        AssistantMessage(
            content=[TextContent(text="first complete")],
            api="fake", provider="fake", model="fake",
        ),
        UserMessage(content=[TextContent(text="turn two")]),
        AssistantMessage(
            content=[TextContent(text="second complete")],
            api="fake", provider="fake", model="fake",
        ),
        UserMessage(content=[TextContent(text="turn three")]),
        AssistantMessage(
            content=[TextContent(text="third complete")],
            api="fake", provider="fake", model="fake",
        ),
    ]
    result = await compact_messages(
        messages,
        config=CompactionConfig(
            min_messages_to_compact=2,
            keep_last_n_turns=1,
        ),
    )
    assert result.applied is True
    assert result.source.compacted_message_count == 7
    assert result.retained_messages[0]["role"] == "user"
    assert [item["role"] for item in result.retained_messages] == [
        "user", "assistant",
    ]
    converted = convert_to_llm([
        result.summary_message,
        messages[-2],
        messages[-1],
    ])
    assert converted[0].role == "user"
    assert converted[0].content[0].text.startswith(SUMMARY_CONTEXT_PREFIX)
    assert converted[0].content[0].text.endswith("</summary>")


@pytest.mark.asyncio
async def test_token_target_keeps_latest_oversized_turn_complete() -> None:
    messages = [
        UserMessage(content=[TextContent(text="old")]),
        AssistantMessage(
            content=[TextContent(text="old answer")],
            api="fake", provider="fake", model="fake",
        ),
        UserMessage(content=[TextContent(text="latest request " * 200)]),
        AssistantMessage(
            content=[ToolCall(id="call-1", name="lookup")],
            api="fake", provider="fake", model="fake", stop_reason="tool_use",
        ),
        ToolResultMessage(
            tool_call_id="call-1",
            name="lookup",
            content=[TextContent(text="result " * 200)],
        ),
        AssistantMessage(
            content=[TextContent(text="latest complete")],
            api="fake", provider="fake", model="fake",
        ),
    ]
    result = await compact_messages(
        messages,
        config=CompactionConfig(
            min_messages_to_compact=2,
            keep_recent_tokens=1,
        ),
    )
    assert result.applied is True
    assert [item["role"] for item in result.retained_messages] == [
        "user", "assistant", "toolResult", "assistant",
    ]
    assert result.source is not None
    assert result.source.retained_turn_count == 1
    assert result.source.compacted_turn_count == 1


@pytest.mark.asyncio
async def test_compaction_records_canonical_token_window_before_and_after() -> None:
    messages = [
        item
        for index in range(8)
        for item in (
            UserMessage(content=[TextContent(text=f"request {index} " * 80)]),
            AssistantMessage(
                content=[TextContent(text=f"answer {index} " * 80)],
                api="fake", provider="fake", model="fake",
            ),
        )
    ]
    estimate = estimate_context(
        system_prompt="system prompt",
        messages=convert_to_llm(messages),
        context_window=16_000,
        reserved_output_tokens=1_024,
    )
    result = await compact_messages(
        messages,
        config=CompactionConfig(min_messages_to_compact=2, keep_last_n_turns=1),
        context_estimate=estimate,
    )
    assert result.token_stats is not None
    assert result.token_stats.estimated_input_tokens_before == estimate.estimated_input_tokens
    assert result.token_stats.projected_tokens_before == estimate.projected_tokens
    assert result.token_stats.context_window == 16_000
    assert result.token_stats.reserved_output_tokens == 1_024
    assert (
        result.token_stats.estimated_input_tokens_after
        < result.token_stats.estimated_input_tokens_before
    )


@pytest.mark.asyncio
async def test_previous_summary_is_folded_once_into_next_compaction() -> None:
    messages = [
        SummaryMessage(content=[TextContent(text="prior durable facts")]),
        UserMessage(content=[TextContent(text="older request")]),
        AssistantMessage(
            content=[TextContent(text="older answer")],
            api="fake", provider="fake", model="fake",
        ),
        UserMessage(content=[TextContent(text="latest request")]),
        AssistantMessage(
            content=[TextContent(text="latest answer")],
            api="fake", provider="fake", model="fake",
        ),
    ]
    result = await compact_messages(
        messages,
        config=CompactionConfig(min_messages_to_compact=2, keep_last_n_turns=1),
    )
    assert result.summary_message is not None
    text = result.summary_message.content[0].text
    assert "## Prior Summary" in text
    assert text.count("prior durable facts") == 1
    assert result.summary_message.metadata["previous_summary_included"] is True


@pytest.mark.asyncio
async def test_retry_reuses_immutable_input_and_emits_lifecycle() -> None:
    calls: list[list[dict[str, object]]] = []
    events: list[CompactionRetryEvent] = []

    async def generator(value):
        calls.append(value.model_dump(mode="python")["messages_to_compact"])
        if len(calls) == 1:
            value.messages_to_compact.clear()
            raise ProviderRateLimitError("temporary")
        return "recovered summary"

    result = await compact_messages(
        [
            UserMessage(content=[TextContent(text=f"turn {index}")])
            for index in range(6)
        ],
        config=CompactionConfig(min_messages_to_compact=2, keep_last_n_turns=1),
        summary_generator=generator,
        retry_policy=CompactionRetryPolicy(max_attempts=2, initial_delay_ms=0),
        retry_callback=events.append,
    )
    assert result.attempts == 2
    assert len(calls[0]) == len(calls[1]) == 5
    assert [event.phase for event in events] == [
        "retry_scheduled", "retry_attempt_start", "retry_finished",
    ]
    assert events[-1].succeeded is True


@pytest.mark.asyncio
async def test_non_transient_summary_error_is_not_retried() -> None:
    calls = 0

    def generator(_value):
        nonlocal calls
        calls += 1
        raise ProviderAuthenticationError("invalid credential")

    with pytest.raises(ProviderAuthenticationError):
        await compact_messages(
            [UserMessage(content=[TextContent(text=str(index))]) for index in range(6)],
            config=CompactionConfig(min_messages_to_compact=2, keep_last_n_turns=1),
            summary_generator=generator,
            retry_policy=CompactionRetryPolicy(max_attempts=3, initial_delay_ms=0),
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_exhaustion_keeps_source_unchanged() -> None:
    messages = [
        UserMessage(content=[TextContent(text=f"turn {index}")])
        for index in range(6)
    ]
    source_before = [message.model_dump(mode="json") for message in messages]
    events: list[CompactionRetryEvent] = []

    def generator(value):
        value.messages_to_compact.clear()
        raise ProviderStreamError("connection reset")

    with pytest.raises(ProviderStreamError):
        await compact_messages(
            messages,
            config=CompactionConfig(min_messages_to_compact=2, keep_last_n_turns=1),
            summary_generator=generator,
            retry_policy=CompactionRetryPolicy(max_attempts=2, initial_delay_ms=0),
            retry_callback=events.append,
        )
    assert [message.model_dump(mode="json") for message in messages] == source_before
    assert [event.phase for event in events] == [
        "retry_scheduled", "retry_attempt_start", "retry_finished",
    ]
    assert events[-1].succeeded is False

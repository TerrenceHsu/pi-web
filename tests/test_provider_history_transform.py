from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pi_agent_core_py import (
    DoneEvent,
    ErrorEvent,
    ImageContent,
    LLMAssistantMessage,
    LLMToolResultMessage,
    LLMUserMessage,
    ModelClient,
    ProviderAdapter,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderRetryPolicy,
    StreamEvent,
    TextContent,
    TextDeltaEvent,
    ThinkingContent,
    ToolCall,
    estimate_message_tokens,
)
from pi_agent_core_py.providers.anthropic_compat import (
    _extract_anthropic_usage,
    to_anthropic_messages,
)
from pi_agent_core_py.providers.openai_compat import _extract_usage, to_openai_messages
from pi_agent_core_py.providers.transform import (
    IMAGE_OMITTED_TEXT,
    MISSING_TOOL_RESULT_TEXT,
    transform_messages_for_provider,
)


def _image(value: str = "aGVsbG8=") -> ImageContent:
    return ImageContent(data=value, mime_type="image/png")


def test_cross_model_thinking_is_demoted_without_mutating_source() -> None:
    source = LLMAssistantMessage(
        provider="anthropic",
        api="anthropic-messages",
        model="claude-source",
        content=[
            ThinkingContent(thinking="reason", thinking_signature="signed"),
            ThinkingContent(
                thinking="",
                thinking_signature="encrypted",
                redacted=True,
            ),
            TextContent(text="answer"),
        ],
    )

    transformed = transform_messages_for_provider(
        [source],
        target_provider="qwen",
        target_api="openai-completions",
        target_model="qwen-target",
        supports_images=False,
    )

    assert [item.model_dump() for item in transformed[0].content] == [
        {"type": "text", "text": "reason"},
        {"type": "text", "text": "answer"},
    ]
    assert isinstance(source.content[0], ThinkingContent)
    assert source.content[0].thinking_signature == "signed"
    assert source.content[1].redacted is True


def test_exact_model_preserves_signed_and_redacted_thinking() -> None:
    source = LLMAssistantMessage(
        provider="anthropic",
        api="anthropic-messages",
        model="claude-same",
        content=[
            ThinkingContent(thinking="reason", thinking_signature="signed"),
            ThinkingContent(
                thinking="", thinking_signature="encrypted", redacted=True,
            ),
        ],
    )
    transformed = transform_messages_for_provider(
        [source],
        target_provider="anthropic",
        target_api="anthropic-messages",
        target_model="claude-same",
        supports_images=True,
    )
    assert transformed == [source]
    assert transformed[0] is not source


def test_unsupported_images_become_one_explicit_placeholder() -> None:
    source = LLMUserMessage(content=[_image("YQ=="), _image("Yg=="), TextContent(text="q")])
    transformed = transform_messages_for_provider(
        [source],
        target_provider="text-provider",
        target_api="text-api",
        target_model="text-model",
        supports_images=False,
    )
    assert transformed[0].content == [
        TextContent(text=IMAGE_OMITTED_TEXT),
        TextContent(text="q"),
    ]
    assert isinstance(source.content[0], ImageContent)


def test_context_estimator_accounts_for_image_payload_size() -> None:
    small = estimate_message_tokens([LLMUserMessage(content=[_image("YQ==")])])
    large = estimate_message_tokens([
        LLMUserMessage(content=[_image("YQ==" * 4_000)])
    ])
    assert large > small + 1_000


def test_tool_ids_are_normalized_and_missing_results_repaired() -> None:
    source = [
        LLMAssistantMessage(
            provider="old",
            api="old-api",
            model="old-model",
            tool_calls=[
                ToolCall(id="call:1", name="first"),
                ToolCall(id="call:2", name="second"),
            ],
        ),
        LLMToolResultMessage(
            tool_call_id="call:1",
            name="first",
            content=[TextContent(text="ok")],
        ),
        LLMUserMessage(content=[TextContent(text="continue")]),
        # No surviving assistant call points at this result, so it is omitted.
        LLMToolResultMessage(
            tool_call_id="orphan",
            name="orphan",
            content=[TextContent(text="unsafe")],
        ),
    ]
    transformed = transform_messages_for_provider(
        source,
        target_provider="new",
        target_api="new-api",
        target_model="new-model",
        supports_images=True,
        normalize_tool_call_id=lambda value: value.replace(":", "_"),
    )

    assert transformed[0].tool_calls[0].id == "call_1"
    assert transformed[1].tool_call_id == "call_1"
    assert transformed[2] == LLMToolResultMessage(
        tool_call_id="call_2",
        name="second",
        content=[TextContent(text=MISSING_TOOL_RESULT_TEXT)],
        is_error=True,
    )
    assert isinstance(transformed[3], LLMUserMessage)
    assert len(transformed) == 4


def test_aborted_assistant_and_its_result_are_removed() -> None:
    transformed = transform_messages_for_provider(
        [
            LLMAssistantMessage(
                provider="p",
                api="a",
                model="m",
                stop_reason="aborted",
                tool_calls=[ToolCall(id="gone", name="unsafe")],
            ),
            LLMToolResultMessage(
                tool_call_id="gone",
                name="unsafe",
                content=[TextContent(text="result")],
            ),
        ],
        target_provider="p",
        target_api="a",
        target_model="m",
        supports_images=True,
    )
    assert transformed == []


def test_anthropic_and_openai_render_inline_images() -> None:
    user = LLMUserMessage(content=[TextContent(text="look"), _image()])
    anthropic = to_anthropic_messages([user])
    assert anthropic[0]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "aGVsbG8=",
        },
    }
    openai = to_openai_messages(system_prompt="", messages=[user])
    assert openai[0]["content"][1]["image_url"]["url"] == (
        "data:image/png;base64,aGVsbG8="
    )


def test_usage_breakdown_preserves_cache_and_reasoning_tokens() -> None:
    anthropic_usage = _extract_anthropic_usage(SimpleNamespace(
        input_tokens=10,
        output_tokens=4,
        cache_read_input_tokens=6,
        cache_creation_input_tokens=2,
        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=1),
    ))
    assert anthropic_usage.model_dump() == {
        "input": 10,
        "output": 4,
        "cache_read": 6,
        "cache_write": 2,
        "cache_write_1h": 1,
        "total_tokens": 22,
    }

    chunk = cast(Any, SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=20,
        completion_tokens=8,
        total_tokens=28,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=5,
            cache_write_tokens=3,
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )))
    openai_usage = _extract_usage(chunk)
    assert openai_usage is not None
    assert openai_usage.model_dump() == {
        "input": 12,
        "output": 8,
        "cache_read": 5,
        "cache_write": 3,
        "reasoning": 2,
        "total_tokens": 28,
    }


class _RetryAdapter(ProviderAdapter):
    provider_id = "retry"
    api_id = "retry-api"
    model = "retry-model"

    def __init__(self, failures: int, *, partial: bool = False) -> None:
        self.failures = failures
        self.partial = partial
        self.calls: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        self.calls.append(request)
        if len(self.calls) <= self.failures:
            if self.partial:
                yield TextDeltaEvent(delta="partial")
            raise ProviderRateLimitError("temporary")
        yield TextDeltaEvent(delta="ok")
        yield DoneEvent(stop_reason="stop")


@pytest.mark.asyncio
async def test_model_client_retries_only_before_first_stream_event() -> None:
    adapter = _RetryAdapter(2)
    client = ModelClient(
        adapter,
        retry_policy=ProviderRetryPolicy(max_retries=2, initial_delay_s=0),
    )
    events = [
        event
        async for event in client.stream(system_prompt="sys", messages=[])
    ]
    assert len(adapter.calls) == 3
    assert [call.metadata.get("pi_agent_retry_attempt", 0) for call in adapter.calls] == [
        0, 1, 2,
    ]
    assert [type(event) for event in events] == [TextDeltaEvent, DoneEvent]


@pytest.mark.asyncio
async def test_model_client_never_retries_after_partial_output() -> None:
    adapter = _RetryAdapter(1, partial=True)
    client = ModelClient(
        adapter,
        retry_policy=ProviderRetryPolicy(max_retries=2, initial_delay_s=0),
    )
    events = [
        event
        async for event in client.stream(system_prompt="sys", messages=[])
    ]
    assert len(adapter.calls) == 1
    assert isinstance(events[0], TextDeltaEvent)
    assert isinstance(events[1], ErrorEvent)


def test_model_client_exposes_adapter_api_identity() -> None:
    client = ModelClient(_RetryAdapter(0))
    assert (client.provider_id, client.api_id, client.model) == (
        "retry", "retry-api", "retry-model",
    )

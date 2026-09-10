"""Custom endpoints cannot establish thinking-signature provenance from model labels."""

from __future__ import annotations

import pytest

from pi_agent_core_py import (
    Agent,
    DoneEvent,
    LLMAssistantMessage,
    ModelClient,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
)
from pi_agent_core_py.agent.context import convert_to_llm
from pi_agent_core_py.ai.messages import TextContent, ThinkingContent
from pi_agent_core_py.ai.providers.endpoints import capability_provider_id
from pi_agent_core_py.ai.providers.fake import FakeProviderAdapter
from pi_agent_core_py.ai.providers.transform import transform_messages_for_provider


def _source(provider_id: str) -> LLMAssistantMessage:
    return LLMAssistantMessage(
        provider=provider_id, api="anthropic-messages", model="same-model",
        content=[
            ThinkingContent(thinking="Visible reasoning", thinking_signature="source-signature"),
            ThinkingContent(thinking="", thinking_signature="encrypted-opaque", redacted=True),
            TextContent(text="Answer"),
        ],
    )


def test_transform_can_reject_signatures_even_when_model_labels_match() -> None:
    source = _source("anthropic_compatible")
    original = source.model_dump_json()
    transformed = transform_messages_for_provider(
        [source], target_provider=source.provider, target_api=source.api,
        target_model=source.model, supports_images=True, allow_reasoning_signatures=False,
    )
    assert transformed[0].content == [
        TextContent(text="Visible reasoning"), TextContent(text="Answer"),
    ]
    assert source.model_dump_json() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["qwen", "openai_compatible", "anthropic_compatible"])
async def test_custom_model_client_demotes_history_using_frozen_scope(provider_id: str) -> None:
    source = _source(provider_id)
    original = source.model_dump_json()
    first_endpoint = "https://first.example.test/api"
    second_endpoint = "https://second.example.test/api"
    first_scope = capability_provider_id(provider_id, "anthropic_compatible", first_endpoint)
    second_scope = capability_provider_id(provider_id, "anthropic_compatible", second_endpoint)
    assert first_scope != second_scope
    adapter = FakeProviderAdapter([[DoneEvent(stop_reason="stop")]], model=source.model)
    adapter.provider_id = provider_id
    adapter.api_id = source.api
    adapter.capability_provider_id = second_scope
    model = ModelClient(adapter)
    # Selection is frozen even if some caller subsequently changes adapter metadata.
    adapter.capability_provider_id = provider_id
    async for _ in model.stream(system_prompt="system", messages=[source]):
        pass
    replayed = adapter.last_messages[0]
    assert replayed.content == [TextContent(text="Visible reasoning"), TextContent(text="Answer")]
    assert replayed.provider == provider_id
    assert source.model_dump_json() == original
    assert "source-signature" not in replayed.model_dump_json()
    assert "encrypted-opaque" not in replayed.model_dump_json()
    assert first_endpoint not in replayed.model_dump_json()
    assert second_endpoint not in replayed.model_dump_json()


@pytest.mark.asyncio
async def test_official_model_client_keeps_compatible_signed_history() -> None:
    source = _source("anthropic")
    adapter = FakeProviderAdapter([[DoneEvent(stop_reason="stop")]], model=source.model)
    adapter.provider_id = source.provider
    adapter.api_id = source.api
    adapter.capability_provider_id = capability_provider_id(
        "anthropic", "anthropic_compatible", "https://api.anthropic.com",
    )
    assert adapter.capability_provider_id == "anthropic"
    model = ModelClient(adapter)
    async for _ in model.stream(system_prompt="system", messages=[source]):
        pass
    assert adapter.last_messages == [source]
    assert adapter.last_messages[0] is not source


def _thinking_script():
    return [
        ThinkingStartEvent(content_index=0, thinking_signature="source-signature"),
        ThinkingDeltaEvent(
            content_index=0, delta="Visible reasoning", thinking_signature="source-signature",
        ),
        ThinkingEndEvent(
            content_index=0, content="Visible reasoning", thinking_signature="source-signature",
        ),
        ThinkingStartEvent(content_index=1, thinking_signature="encrypted-opaque", redacted=True),
        # Retain the block's redacted state even if a subsequent event omits the flag.
        ThinkingDeltaEvent(content_index=1, delta="encrypted-opaque"),
        ThinkingEndEvent(content_index=1, content="encrypted-opaque", redacted=True),
        TextDeltaEvent(content_index=2, delta="Answer"),
        DoneEvent(stop_reason="stop"),
    ]


def _response_client(script, *, custom):
    adapter = FakeProviderAdapter([script], model="same-model")
    adapter.provider_id = "anthropic"
    adapter.api_id = "anthropic-messages"
    adapter.capability_provider_id = capability_provider_id(
        "anthropic", "anthropic_compatible",
        "https://gateway.example.test" if custom else "https://api.anthropic.com",
    )
    return ModelClient(adapter), adapter


@pytest.mark.asyncio
async def test_custom_response_keeps_visible_lifecycle_without_replay_metadata():
    script = _thinking_script()
    original = [event.model_dump_json() for event in script]
    model, _ = _response_client(script, custom=True)
    events = [event async for event in model.stream(system_prompt="system", messages=[])]
    assert [event.type for event in events] == [
        "thinking_start", "thinking_delta", "thinking_end", "text_delta", "done",
    ]
    assert events[1].delta == "Visible reasoning"
    assert events[2].content == "Visible reasoning"
    serialized = " ".join(event.model_dump_json() for event in events)
    assert "source-signature" not in serialized
    assert "encrypted-opaque" not in serialized
    assert [event.model_dump_json() for event in script] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_generated_history_replayed_to_official_endpoint_keeps_only_official_signatures(
    custom,
):
    model, _ = _response_client(_thinking_script(), custom=custom)
    agent = Agent(system_prompt="system", client=model)
    try:
        await agent.prompt("Explain")
        source = next(message for message in agent.state.messages if message.role == "assistant")
        assert source.provider == "anthropic"
        assert source.api == "anthropic-messages"
        assert source.model == "same-model"
        original = source.model_dump_json()
        assert ("source-signature" in original) is (not custom)
        assert ("encrypted-opaque" in original) is (not custom)

        official, adapter = _response_client([DoneEvent(stop_reason="stop")], custom=False)
        async for _ in official.stream(system_prompt="system", messages=convert_to_llm([source])):
            pass
        replayed = adapter.last_messages[0].model_dump_json()
        assert ("source-signature" in replayed) is (not custom)
        assert ("encrypted-opaque" in replayed) is (not custom)
        assert "Visible reasoning" in replayed
        assert source.model_dump_json() == original
    finally:
        await model.close()

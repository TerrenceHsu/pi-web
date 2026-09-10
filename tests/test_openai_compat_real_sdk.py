"""Real SDK protocol regression tests; HTTP is exclusively a local MockTransport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

import pi_agent_core_py.ai.providers.openai_compat as provider_module
from pi_agent_core_py.ai.llm_messages import LLMUserMessage
from pi_agent_core_py.ai.messages import TextContent
from pi_agent_core_py.ai.model_client import ModelClient
from pi_agent_core_py.ai.providers.base import ProviderRequest
from pi_agent_core_py.ai.providers.errors import (
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderStreamError,
)
from pi_agent_core_py.ai.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from pi_agent_core_py.ai.providers.retry import ProviderRetryPolicy
from pi_agent_core_py.ai.stream_events import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from pi_agent_core_py.ai.tooling import ToolDef

pytestmark = pytest.mark.asyncio

SECRET_MARKER = "sk-offline-sdk-security-marker"


def _request(*, signal: asyncio.Event | None = None) -> ProviderRequest:
    return ProviderRequest(
        system_prompt="Offline fixture",
        messages=[LLMUserMessage(content=[TextContent(text="Inspect fixture")])],
        tools=[ToolDef(
            name="fixture_reader", label="Fixture reader", description="Read fixture metadata",
            parameters={
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )],
        signal=signal,
    )


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": 1,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


class _Body(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes], *, stall_after_first: bool = False) -> None:
        self.parts = parts
        self.closed = False
        self.stall_after_first = stall_after_first
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, part in enumerate(self.parts):
            yield part
            if index == 0 and self.stall_after_first:
                self.waiting.set()
                await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class _Exchange:
    def __init__(
        self, chunks: list[dict[str, Any]], *, status: int = 200, stall: bool = False,
    ) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.status = status
        parts = [b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks]
        parts.append(b"data: [DONE]\n\n")
        if status != 200:
            parts = [json.dumps({"error": {"message": SECRET_MARKER}}).encode()]
        self.body = _Body(parts, stall_after_first=stall)

    def handle(self, request: httpx.Request) -> httpx.Response:
        # Never inspect/store Authorization. All prompts and schemas are synthetic.
        self.payloads.append(json.loads(request.content))
        return httpx.Response(
            self.status,
            headers={
                "content-type": "text/event-stream" if self.status == 200 else "application/json",
            },
            stream=self.body,
        )


class _FailingBody(_Body):
    def __init__(self, parts: list[bytes], error: Exception) -> None:
        super().__init__(parts)
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.parts:
            yield self.parts[0]
        raise self.error


class _SequenceExchange(_Exchange):
    def __init__(self, bodies: list[_Body]) -> None:
        super().__init__([])
        self.bodies = bodies

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.body = self.bodies[len(self.payloads)]
        return super().handle(request)


@asynccontextmanager
async def _adapter(
    exchange: _Exchange,
) -> AsyncIterator[tuple[OpenAICompatibleProvider, AsyncOpenAI]]:
    sdk = AsyncOpenAI(
        api_key=SECRET_MARKER, base_url="https://offline.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(exchange.handle)),
    )
    adapter = OpenAICompatibleProvider(
        OpenAICompatConfig(
            api_key=SECRET_MARKER, base_url="https://offline.invalid/v1", model="fixture-model",
        ),
        provider_id="openai_compatible", client=sdk,
    )
    try:
        yield adapter, sdk
    finally:
        await adapter.aclose()
        await sdk.close()


async def test_non_strict_tool_reaches_real_sdk_transport() -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "OK"}, "stop")])
    async with _adapter(exchange) as (adapter, _):
        events = [event async for event in adapter.stream(_request())]
        assert len(exchange.payloads) == 1
        function = exchange.payloads[0]["tools"][0]["function"]
        assert "strict" not in function
        assert "additionalProperties" not in function["parameters"]
        assert exchange.payloads[0]["stream"] is True
        assert exchange.payloads[0]["stream_options"] == {"include_usage": True}
        assert [event.content for event in events if isinstance(event, TextEndEvent)] == ["OK"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stop_reason == "stop"
        assert exchange.body.closed


async def test_fragmented_tool_text_reasoning_and_usage_only_with_real_sdk() -> None:
    usage_chunk = {
        "id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": 1,
        "model": "fixture-model", "choices": [],
        "usage": {
            "prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }
    exchange = _Exchange([
        _chunk({"role": "assistant", "reasoning_content": "Plan "}),
        _chunk({"reasoning_content": "then inspect", "content": "Reading "}),
        _chunk({"content": "fixture", "tool_calls": [{
            "index": 0, "id": "call_fixture", "type": "function",
            "function": {"name": "fixture_reader", "arguments": '{"path":'},
        }]}),
        _chunk({"tool_calls": [{
            "index": 0, "function": {"arguments": '"fixture.txt"}'},
        }]}, "tool_calls"),
        usage_chunk,
    ])
    async with _adapter(exchange) as (adapter, _):
        events = [event async for event in adapter.stream(_request())]
        assert len(exchange.payloads) == 1
        assert [event.content for event in events if isinstance(event, TextEndEvent)] == [
            "Reading fixture",
        ]
        assert [event.content for event in events if isinstance(event, ThinkingEndEvent)] == [
            "Plan then inspect",
        ]
        assert len([event for event in events if isinstance(event, ToolCallStartEvent)]) == 1
        fragments = [event.delta for event in events if isinstance(event, ToolCallDeltaEvent)]
        assert fragments == ['{"path":', '"fixture.txt"}']
        complete = [event for event in events if isinstance(event, ToolCallEndEvent)]
        assert len(complete) == 1
        assert complete[0].tool_call.id == "call_fixture"
        assert complete[0].tool_call.name == "fixture_reader"
        assert complete[0].tool_call.arguments == {"path": "fixture.txt"}
        assert events.index(complete[0]) > max(
            index for index, event in enumerate(events) if isinstance(event, ToolCallDeltaEvent)
        )
        done = [event for event in events if isinstance(event, DoneEvent)]
        assert len(done) == 1
        assert done[0].stop_reason == "tool_use"
        assert done[0].usage.input == 15
        assert done[0].usage.output == 7
        assert done[0].usage.cache_read == 5
        assert done[0].usage.reasoning == 3
        assert done[0].usage.total_tokens == 27
        assert exchange.body.closed


@pytest.mark.parametrize("reasoning_field", ["reasoning_content", "reasoning", "reasoning_text"])
async def test_unknown_fields_and_missing_usage_keep_compatible_chunks(
    reasoning_field: str,
) -> None:
    first = _chunk({
        "role": "assistant", reasoning_field: "Reason", "future_delta_field": {"enabled": True},
    })
    first["vendor_metadata"] = {"future": 123}
    exchange = _Exchange([first, _chunk({"content": "Answer"}, "stop")])
    async with _adapter(exchange) as (adapter, _):
        events = [event async for event in adapter.stream(_request())]
        assert [event.delta for event in events if isinstance(event, ThinkingDeltaEvent)] == [
            "Reason",
        ]
        assert [event.delta for event in events if isinstance(event, TextDeltaEvent)] == ["Answer"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].usage.input == 0
        assert events[-1].usage.output == 0
        assert events[-1].usage.total_tokens == 0
        assert exchange.body.closed


async def test_length_finish_reason_remains_a_normal_terminal_event() -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "Partial"}, "length")])
    async with _adapter(exchange) as (adapter, _):
        events = [event async for event in adapter.stream(_request())]
        assert [event.content for event in events if isinstance(event, TextEndEvent)] == ["Partial"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stop_reason == "length"
        assert exchange.body.closed


async def test_signal_abort_closes_response_without_closing_injected_client() -> None:
    signal = asyncio.Event()
    exchange = _Exchange([
        _chunk({"role": "assistant", "content": "First"}),
        _chunk({"content": "Must not be emitted"}, "stop"),
    ])
    async with _adapter(exchange) as (adapter, sdk):
        events = []
        async for event in adapter.stream(_request(signal=signal)):
            events.append(event)
            if isinstance(event, TextDeltaEvent):
                signal.set()
        assert [event.delta for event in events if isinstance(event, TextDeltaEvent)] == ["First"]
        assert isinstance(events[-1], DoneEvent)
        assert events[-1].stop_reason == "aborted"
        assert exchange.body.closed
        assert not sdk.is_closed()


async def test_pre_cancelled_signal_does_not_create_http_request() -> None:
    signal = asyncio.Event()
    signal.set()
    exchange = _Exchange([_chunk({"role": "assistant", "content": "Unused"}, "stop")])
    async with _adapter(exchange) as (adapter, _):
        events = [event async for event in adapter.stream(_request(signal=signal))]
        assert len(events) == 1
        assert isinstance(events[0], DoneEvent)
        assert events[0].stop_reason == "aborted"
        assert exchange.payloads == []


async def test_task_cancellation_closes_real_sdk_stream() -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "First"})], stall=True)
    async with _adapter(exchange) as (adapter, sdk):
        async def consume() -> None:
            async for _ in adapter.stream(_request()):
                pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(exchange.body.waiting.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exchange.body.closed
            assert not sdk.is_closed()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_consumer_aclose_closes_response_without_closing_injected_client() -> None:
    exchange = _Exchange([
        _chunk({"role": "assistant", "content": "First"}),
        _chunk({"content": "Unused"}, "stop"),
    ])
    async with _adapter(exchange) as (adapter, sdk):
        stream = adapter.stream(_request())
        try:
            async for event in stream:
                if isinstance(event, TextDeltaEvent):
                    break
        finally:
            await stream.aclose()
        assert exchange.body.closed
        assert not sdk.is_closed()


@pytest.mark.parametrize("status,error_type,safe_message", [
    (401, ProviderAuthenticationError, "provider authentication failed"),
    (403, ProviderAuthenticationError, "provider authentication failed"),
    (429, ProviderRateLimitError, "provider rate limit exceeded"),
    (400, ProviderProtocolError, "provider rejected request"),
    (408, ProviderStreamError, "provider connection failed"),
    (500, ProviderStreamError, "provider connection failed"),
])
async def test_http_errors_keep_safe_taxonomy_without_response_secrets(
    status: int, error_type: type[Exception], safe_message: str, caplog: pytest.LogCaptureFixture,
) -> None:
    exchange = _Exchange([], status=status)
    async with _adapter(exchange) as (adapter, sdk):
        with pytest.raises(error_type) as caught:
            _ = [event async for event in adapter.stream(_request())]
        assert len(exchange.payloads) == 1  # SDK retries disabled, not a local strict-tool error.
        assert str(caught.value) == safe_message
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
        assert SECRET_MARKER not in repr(caught.value)
        assert SECRET_MARKER not in str(caught.value)
        assert SECRET_MARKER not in caplog.text
        assert exchange.body.closed
        assert not sdk.is_closed()


@pytest.mark.parametrize("transport_error", [httpx.ReadError, httpx.ReadTimeout])
async def test_real_stream_transport_errors_are_safe_and_close_response(
    transport_error: type[Exception], caplog: pytest.LogCaptureFixture,
) -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "Partial"})])
    exchange.body = _FailingBody(exchange.body.parts, transport_error(SECRET_MARKER))
    async with _adapter(exchange) as (adapter, sdk):
        with pytest.raises(ProviderStreamError) as caught:
            _ = [event async for event in adapter.stream(_request())]
        assert len(exchange.payloads) == 1
        assert str(caught.value) == "provider connection failed"
        assert SECRET_MARKER not in repr(caught.value)
        assert SECRET_MARKER not in caplog.text
        assert exchange.body.closed
        assert not sdk.is_closed()


async def test_invalid_sse_json_is_protocol_failure_not_connection_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exchange = _Exchange([])
    exchange.body = _Body([b"data: {invalid-" + SECRET_MARKER.encode() + b"}\n\n"])
    async with _adapter(exchange) as (adapter, sdk):
        with pytest.raises(ProviderProtocolError) as caught:
            _ = [event async for event in adapter.stream(_request())]
        assert len(exchange.payloads) == 1
        assert SECRET_MARKER not in str(caught.value)
        assert SECRET_MARKER not in repr(caught.value)
        assert SECRET_MARKER not in caplog.text
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
        assert exchange.body.closed
        assert not sdk.is_closed()


async def test_sse_error_payload_is_safe_protocol_failure() -> None:
    exchange = _Exchange([{"error": {"message": SECRET_MARKER}}])
    async with _adapter(exchange) as (adapter, sdk):
        with pytest.raises(ProviderProtocolError) as caught:
            _ = [event async for event in adapter.stream(_request())]
        assert str(caught.value) == "provider returned invalid response"
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
        assert SECRET_MARKER not in repr(caught.value)
        assert exchange.body.closed
        assert not sdk.is_closed()


@pytest.mark.parametrize("failure_kind", ["invalid_sse", "value_error"])
async def test_model_client_does_not_retry_local_protocol_failures(
    failure_kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    bodies: list[_Body] = []
    for _ in range(3):
        if failure_kind == "invalid_sse":
            bodies.append(_Body([b"data: {invalid-" + SECRET_MARKER.encode() + b"}\n\n"]))
        else:
            bodies.append(_FailingBody([], ValueError(SECRET_MARKER)))
    exchange = _SequenceExchange(bodies)
    async with _adapter(exchange) as (adapter, sdk):
        client = ModelClient(adapter, retry_policy=ProviderRetryPolicy(
            max_retries=2, initial_delay_s=0, max_delay_s=0,
        ))
        request = _request()
        events = [event async for event in client.stream(
            system_prompt=request.system_prompt, messages=request.messages, tools=request.tools,
        )]
        assert len(exchange.payloads) == 1
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert events[0].message == "ProviderProtocolError: provider returned invalid response"
        assert SECRET_MARKER not in repr(events)
        assert SECRET_MARKER not in caplog.text
        assert bodies[0].closed
        assert not bodies[1].closed  # Neither retry body was requested.
        assert not sdk.is_closed()


@pytest.mark.parametrize("recover", [True, False])
async def test_model_client_retries_pre_output_read_errors_with_bounded_attempts(
    recover: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    bodies: list[_Body] = [_FailingBody([], httpx.ReadError(SECRET_MARKER))]
    if recover:
        bodies.append(_Exchange([_chunk({"role": "assistant", "content": "OK"}, "stop")]).body)
    else:
        bodies.extend(_FailingBody([], httpx.ReadError(SECRET_MARKER)) for _ in range(2))
    exchange = _SequenceExchange(bodies)
    async with _adapter(exchange) as (adapter, sdk):
        client = ModelClient(adapter, retry_policy=ProviderRetryPolicy(
            max_retries=2, initial_delay_s=0, max_delay_s=0,
        ))
        request = _request()
        events = [event async for event in client.stream(
            system_prompt=request.system_prompt, messages=request.messages, tools=request.tools,
        )]
        assert len(exchange.payloads) == (2 if recover else 3)
        if recover:
            assert [event.content for event in events if isinstance(event, TextEndEvent)] == ["OK"]
            assert isinstance(events[-1], DoneEvent)
            assert events[-1].stop_reason == "stop"
            assert not any(isinstance(event, ErrorEvent) for event in events)
        else:
            assert len(events) == 1
            assert isinstance(events[0], ErrorEvent)
            assert events[0].message == "ProviderStreamError: provider connection failed"
        assert all(body.closed for body in bodies)
        assert SECRET_MARKER not in repr(events)
        assert SECRET_MARKER not in caplog.text
        assert not sdk.is_closed()


async def test_model_client_does_not_retry_read_errors_after_visible_output() -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "Partial"})])
    exchange.body = _FailingBody(exchange.body.parts, httpx.ReadError(SECRET_MARKER))
    async with _adapter(exchange) as (adapter, _):
        client = ModelClient(adapter, retry_policy=ProviderRetryPolicy(
            max_retries=2, initial_delay_s=0, max_delay_s=0,
        ))
        request = _request()
        events = [event async for event in client.stream(
            system_prompt=request.system_prompt, messages=request.messages, tools=request.tools,
        )]
        assert len(exchange.payloads) == 1
        assert [event.delta for event in events if isinstance(event, TextDeltaEvent)] == ["Partial"]
        assert isinstance(events[-1], ErrorEvent)
        assert events[-1].message == "ProviderStreamError: provider connection failed"
        assert exchange.body.closed


async def test_injected_client_stays_open_until_owner_closes_it() -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "OK"}, "stop")])
    async with _adapter(exchange) as (adapter, sdk):
        _ = [event async for event in adapter.stream(_request())]
        await adapter.aclose()
        await adapter.aclose()
        assert exchange.body.closed
        assert not sdk.is_closed()
    assert sdk.is_closed()


async def test_adapter_owned_real_client_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = _Exchange([_chunk({"role": "assistant", "content": "OK"}, "stop")])

    def local_client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(exchange.handle), **kwargs)

    monkeypatch.setattr(provider_module, "DefaultAsyncHttpxClient", local_client)
    adapter = OpenAICompatibleProvider(OpenAICompatConfig(
        api_key=SECRET_MARKER, base_url="https://offline.invalid/v1", model="fixture-model",
    ), provider_id="openai_compatible")
    try:
        _ = [event async for event in adapter.stream(_request())]
        assert exchange.body.closed
        assert not adapter._client.is_closed()
        await adapter.aclose()
        await adapter.aclose()
        assert adapter._client.is_closed()
    finally:
        await adapter.aclose()

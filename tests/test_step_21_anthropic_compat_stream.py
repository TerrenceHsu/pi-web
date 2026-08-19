"""Step 21 — AnthropicCompatAdapter stream 解析。

不调用真实网络——用 fake raw stream 对象模拟 Anthropic SDK 的 event 序列，
验证 adapter 把 raw event 翻译成正确的内部 StreamEvent。

覆盖：
- text_delta → TextDeltaEvent
- tool_use (content_block_start + input_json_delta + content_block_stop)
  → ToolCallEvent
- message_delta(stop_reason) → DoneEvent(stop_reason=...)
- usage 字段映射
- 非法 tool input JSON → ProviderProtocolError（被 ModelClient 转 ErrorEvent）
- provider 端抛异常 → ErrorEvent
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pi_agent_core_py import (
    AnthropicCompatAdapter,
    AnthropicCompatConfig,
    DoneEvent,
    ErrorEvent,
    LLMUserMessage,
    ModelClient,
    ProviderRequest,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallEvent,
    ToolCallStartEvent,
)
from pi_agent_core_py.messages import TextContent

# ============================================================================
# Fake Anthropic SDK objects
# ============================================================================


class _Delta:
    """模拟 Anthropic SDK 的 event.delta。"""

    def __init__(self, *, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _ContentBlock:
    def __init__(self, *, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for k, v in kwargs.items():
            setattr(self, k, v)


class _RawEvent:
    """模拟 Anthropic SDK 的 stream event。"""

    def __init__(
        self,
        *,
        type_: str,
        delta: Any = None,
        content_block: Any = None,
        index: int = 0,
    ) -> None:
        self.type = type_
        self.delta = delta
        self.content_block = content_block
        self.index = index


class _FakeRawStream:
    """模拟 Anthropic SDK 的 async stream 上下文。"""

    def __init__(self, events: list[_RawEvent], final_usage: dict[str, int] | None = None):
        self._events = events
        self._final_usage = final_usage

    def __aiter__(self) -> AsyncIterator[_RawEvent]:
        async def gen() -> AsyncIterator[_RawEvent]:
            for e in self._events:
                yield e

        return gen()

    async def __aenter__(self) -> _FakeRawStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        """返回带 usage 字段的 mock。"""

        class _Usage:
            def __init__(self, u: dict[str, int]) -> None:
                self.input_tokens = u.get("input", 0)
                self.output_tokens = u.get("output", 0)

        class _Msg:
            def __init__(self, u: dict[str, int]) -> None:
                self.usage = _Usage(u)

        return _Msg(self._final_usage or {})


class _FakeMessagesNamespace:
    """模拟 client.messages——stream() 返回我们的 fake raw stream。"""

    def __init__(self, raw_stream: _FakeRawStream) -> None:
        self._raw = raw_stream
        self.last_kwargs: dict[str, Any] | None = None

    def stream(self, **kwargs: Any) -> _FakeRawStream:
        self.last_kwargs = kwargs
        return self._raw


class _FakeAnthropicClient:
    """模拟 AsyncAnthropic——messages 属性返回 fake namespace。"""

    def __init__(self, raw_stream: _FakeRawStream) -> None:
        self._ns = _FakeMessagesNamespace(raw_stream)
        self.messages = self._ns

    @property
    def last_kwargs(self) -> dict[str, Any] | None:
        return self._ns.last_kwargs


# ============================================================================
# Helpers
# ============================================================================


def _config() -> AnthropicCompatConfig:
    return AnthropicCompatConfig(
        api_key="test-key",
        base_url="https://example.test",
        model="test-model",
        max_tokens=128,
    )


def _req() -> ProviderRequest:
    return ProviderRequest(
        system_prompt="x",
        messages=[LLMUserMessage(content=[TextContent(text="hi")])],
    )


async def _collect(adapter: AnthropicCompatAdapter, req: ProviderRequest) -> list[Any]:
    return [ev async for ev in adapter.stream(req)]


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.asyncio
async def test_text_delta_yields_text_delta_event() -> None:
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="hello "),
            ),
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="world"),
            ),
        ],
        final_usage={"input": 10, "output": 2},
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    events = await _collect(adapter, _req())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert "".join(e.delta for e in text_events) == "hello world"
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].usage.input == 10
    assert events[-1].usage.output == 2
    assert events[-1].usage.total_tokens == 12


@pytest.mark.asyncio
async def test_anthropic_content_blocks_map_to_balanced_lifecycles() -> None:
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                index=0,
                content_block=_ContentBlock(type_="thinking", thinking="", signature=""),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=0,
                delta=_Delta(type_="thinking_delta", thinking="reason"),
            ),
            _RawEvent(type_="content_block_stop", index=0),
            _RawEvent(
                type_="content_block_start",
                index=1,
                content_block=_ContentBlock(type_="text", text=""),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=1,
                delta=_Delta(type_="text_delta", text="answer"),
            ),
            _RawEvent(type_="content_block_stop", index=1),
            _RawEvent(
                type_="content_block_start",
                index=2,
                content_block=_ContentBlock(type_="tool_use", id="t1", name="echo"),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=2,
                delta=_Delta(type_="input_json_delta", partial_json='{"text":"hi"}'),
            ),
            _RawEvent(type_="content_block_stop", index=2),
        ]
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    events = await _collect(adapter, _req())

    assert [event.type for event in events] == [
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_start",
        "text_delta",
        "text_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    assert isinstance(events[0], ThinkingStartEvent)
    assert isinstance(events[2], ThinkingEndEvent)
    assert isinstance(events[3], TextStartEvent)
    assert isinstance(events[5], TextEndEvent)
    assert isinstance(events[6], ToolCallStartEvent)
    assert isinstance(events[7], ToolCallDeltaEvent)
    assert isinstance(events[8], ToolCallEndEvent)
    assert [event.content_index for event in events[:-1]] == [
        0,
        0,
        0,
        1,
        1,
        1,
        2,
        2,
        2,
    ]
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_thinking_and_signature_deltas_are_preserved() -> None:
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                content_block=_ContentBlock(type_="thinking", thinking="", signature=""),
            ),
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="thinking_delta", thinking="reason"),
            ),
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="signature_delta", signature="signed-payload"),
            ),
        ]
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    events = await _collect(adapter, _req())
    thinking_events = [event for event in events if isinstance(event, ThinkingDeltaEvent)]

    assert [event.delta for event in thinking_events] == ["reason", ""]
    assert thinking_events[-1].thinking_signature == "signed-payload"


@pytest.mark.asyncio
async def test_redacted_thinking_block_preserves_opaque_payload() -> None:
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                content_block=_ContentBlock(
                    type_="redacted_thinking",
                    data="opaque-encrypted-payload",
                ),
            ),
        ]
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    events = await _collect(adapter, _req())
    thinking = next(event for event in events if isinstance(event, ThinkingDeltaEvent))

    assert thinking.delta == ""
    assert thinking.thinking_signature == "opaque-encrypted-payload"
    assert thinking.redacted is True


@pytest.mark.asyncio
async def test_tool_use_block_emits_tool_call_event() -> None:
    """content_block_start(tool_use) + input_json_delta + content_block_stop → ToolCallEvent。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                index=0,
                content_block=_ContentBlock(type_="tool_use", id="t1", name="echo"),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=0,
                delta=_Delta(type_="input_json_delta", partial_json='{"text":'),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=0,
                delta=_Delta(type_="input_json_delta", partial_text=None, partial_json='"hi"}'),
            ),
            _RawEvent(
                type_="content_block_stop",
                index=0,
            ),
            _RawEvent(
                type_="message_delta",
                delta=_Delta(type_="message_delta", stop_reason="tool_use"),
            ),
        ],
        final_usage={"input": 5, "output": 5},
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    events = await _collect(adapter, _req())
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    tc = tool_events[0].tool_call
    assert tc.id == "t1"
    assert tc.name == "echo"
    assert tc.arguments == {"text": "hi"}
    assert events[-1].stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_invalid_tool_input_json_raises_protocol_error() -> None:
    """tool_use input 不是合法 JSON → ProviderProtocolError。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                index=0,
                content_block=_ContentBlock(type_="tool_use", id="t1", name="echo"),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=0,
                delta=_Delta(type_="input_json_delta", partial_json="not json"),
            ),
            _RawEvent(type_="content_block_stop", index=0),
        ],
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))
    client = ModelClient(adapter)

    events = []
    async for ev in client.stream(system_prompt="x", messages=[]):
        events.append(ev)

    # ProviderProtocolError 被 ModelClient 捕获 → ErrorEvent
    assert isinstance(events[-1], ErrorEvent)
    assert "ProviderProtocolError" in events[-1].message


@pytest.mark.asyncio
async def test_tool_input_non_object_raises_protocol_error() -> None:
    """tool input 是合法 JSON 但不是 object → ProviderProtocolError。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                index=0,
                content_block=_ContentBlock(type_="tool_use", id="t1", name="echo"),
            ),
            _RawEvent(
                type_="content_block_delta",
                index=0,
                delta=_Delta(type_="input_json_delta", partial_json="[1, 2, 3]"),
            ),
            _RawEvent(type_="content_block_stop", index=0),
        ],
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))
    client = ModelClient(adapter)

    events = []
    async for ev in client.stream(system_prompt="x", messages=[]):
        events.append(ev)
    assert isinstance(events[-1], ErrorEvent)
    assert "ProviderProtocolError" in events[-1].message


@pytest.mark.asyncio
async def test_empty_tool_input_becomes_empty_dict() -> None:
    """tool_use 没有 input_json_delta（input 为空）→ arguments={}。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_start",
                index=0,
                content_block=_ContentBlock(type_="tool_use", id="t1", name="ping"),
            ),
            _RawEvent(type_="content_block_stop", index=0),
        ],
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))
    events = await _collect(adapter, _req())
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert tool_events[0].tool_call.arguments == {}


@pytest.mark.asyncio
async def test_message_delta_max_tokens_sets_length_stop_reason() -> None:
    """stop_reason=max_tokens → DoneEvent.stop_reason='length'。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="x"),
            ),
            _RawEvent(
                type_="message_delta",
                delta=_Delta(type_="message_delta", stop_reason="max_tokens"),
            ),
        ],
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))
    events = await _collect(adapter, _req())
    assert events[-1].stop_reason == "length"


@pytest.mark.asyncio
async def test_missing_usage_defaults_to_zero() -> None:
    """provider 没返回 usage 时，DoneEvent.usage 默认全 0。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="x"),
            ),
        ],
        final_usage=None,
    )

    # get_final_message 抛异常的分支：用 raise 版本
    class _BrokenRawStream(_FakeRawStream):
        async def get_final_message(self) -> Any:
            raise RuntimeError("no final")

    raw2 = _BrokenRawStream(events=raw._events, final_usage=None)
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw2))
    events = await _collect(adapter, _req())
    assert events[-1].usage.input == 0
    assert events[-1].usage.output == 0


@pytest.mark.asyncio
async def test_signal_set_yields_done_event_with_aborted_stop_reason() -> None:
    """signal set 后第一个 event 入口 → 立即 yield DoneEvent(stop_reason='aborted')。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="hello"),
            ),
        ],
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))

    signal = asyncio.Event()
    signal.set()
    req = ProviderRequest(
        system_prompt="x",
        messages=[],
        signal=signal,
    )
    events = await _collect(adapter, req)
    # 信号 set 后立即 yield DoneEvent(stop_reason="aborted")，不再消费 text_delta
    assert isinstance(events[0], DoneEvent)
    assert events[0].stop_reason == "aborted"


@pytest.mark.asyncio
async def test_total_tokens_computed_from_input_plus_output_when_missing() -> None:
    """provider 没给 total_tokens（实际也不会给），由 input+output 算出来。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(
                type_="content_block_delta",
                delta=_Delta(type_="text_delta", text="x"),
            ),
        ],
        final_usage={"input": 7, "output": 3},
    )
    adapter = AnthropicCompatAdapter(_config(), client=_FakeAnthropicClient(raw))
    events = await _collect(adapter, _req())
    assert events[-1].usage.total_tokens == 10


# ============================================================================
# temperature + api_key error type
# ============================================================================


@pytest.mark.asyncio
async def test_temperature_zero_is_passed_to_sdk() -> None:
    """显式 temperature=0.0 应该传给 SDK（不让 SDK 默认 1.0 偷偷生效）。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(type_="content_block_delta", delta=_Delta(type_="text_delta", text="x")),
        ]
    )
    fake_client = _FakeAnthropicClient(raw)
    cfg = _config()  # default temperature=0.0
    adapter = AnthropicCompatAdapter(cfg, client=fake_client)

    async for _ in adapter.stream(_req()):
        pass

    assert fake_client.last_kwargs is not None
    assert fake_client.last_kwargs["temperature"] == 0.0


@pytest.mark.asyncio
async def test_temperature_none_skips_kwarg() -> None:
    """temperature=None 时不传——让 SDK 用自己的默认。"""
    raw = _FakeRawStream(
        events=[
            _RawEvent(type_="content_block_delta", delta=_Delta(type_="text_delta", text="x")),
        ]
    )
    fake_client = _FakeAnthropicClient(raw)
    cfg = AnthropicCompatConfig(
        api_key="test-key",
        base_url="https://example.test",
        model="m",
        max_tokens=128,
        temperature=None,
    )
    adapter = AnthropicCompatAdapter(cfg, client=fake_client)

    async for _ in adapter.stream(_req()):
        pass

    assert fake_client.last_kwargs is not None
    assert "temperature" not in fake_client.last_kwargs


@pytest.mark.asyncio
async def test_temperature_custom_value_passed_through() -> None:
    raw = _FakeRawStream(
        events=[
            _RawEvent(type_="content_block_delta", delta=_Delta(type_="text_delta", text="x")),
        ]
    )
    fake_client = _FakeAnthropicClient(raw)
    cfg = AnthropicCompatConfig(
        api_key="test-key",
        base_url="https://example.test",
        model="m",
        max_tokens=128,
        temperature=0.7,
    )
    adapter = AnthropicCompatAdapter(cfg, client=fake_client)

    async for _ in adapter.stream(_req()):
        pass

    assert fake_client.last_kwargs["temperature"] == 0.7


def test_missing_api_key_raises_provider_config_error() -> None:
    """缺 api_key 应抛 ProviderConfigError（不是 ProviderProtocolError）。"""
    from pi_agent_core_py import ProviderConfigError

    cfg = AnthropicCompatConfig(
        api_key="",
        base_url="https://example.test",
        model="m",
    )
    with pytest.raises(ProviderConfigError):
        AnthropicCompatAdapter(cfg)

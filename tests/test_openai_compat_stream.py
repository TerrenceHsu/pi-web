"""OpenAICompatibleProvider.stream 解析测试（M1-1 §十五.Streaming）.

覆盖：
- 单个 text delta
- 多个 text delta
- content=None
- 空 choices usage chunk
- usage 缺失（默认 Usage()）
- stop / length / tool_calls finish_reason
- reasoning_content 忽略
- 只产生一个 DoneEvent
- tool calling（单 chunk / 多 chunk / 交错 / 按 index 排序）
- finish_reason=tool_calls 触发 flush
- 正常结束 buffer 非空也 flush
- 非 chunk event 跳过
- usage + choices 同 chunk
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py.llm_messages import LLMUserMessage
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.providers.base import ProviderRequest
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from pi_agent_core_py.stream_events import (
    DoneEvent,
    TextDeltaEvent,
    ToolCallEvent,
)
from tests._openai_compat_fakes import (
    _FakeAsyncStream,
    _FakeClient,
    empty_delta_chunk,
    make_event,
    make_non_chunk_event,
    make_usage,
    mixed_delta_chunk,
    reasoning_chunk,
    text_delta_chunk,
    tool_call_delta_chunk,
    usage_only_chunk,
)

SECRET_MARKER = "sk-M1-OPENAI-COMPAT-SECRET-MARKER"


def _config(**overrides: Any) -> OpenAICompatConfig:
    base: dict[str, Any] = {
        "api_key": SECRET_MARKER,
        "base_url": "https://example.test",
        "model": "qwen-plus",
    }
    base.update(overrides)
    return OpenAICompatConfig(**base)


def _req() -> ProviderRequest:
    return ProviderRequest(
        system_prompt="x",
        messages=[LLMUserMessage(content=[TextContent(text="hi")])],
    )


async def _collect(adapter: OpenAICompatibleProvider, req: ProviderRequest) -> list[Any]:
    return [ev async for ev in adapter.stream(req)]


def _adapter(
    stream: _FakeAsyncStream, **overrides: Any,
) -> tuple[OpenAICompatibleProvider, _FakeClient]:
    cfg = _config(**overrides)
    client = _FakeClient(stream)
    adapter = OpenAICompatibleProvider(cfg, provider_id="qwen", client=client)
    return adapter, client


# ============================================================================
# text delta
# ============================================================================


@pytest.mark.asyncio
async def test_single_text_delta() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hello", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert len(text_events) == 1
    assert text_events[0].delta == "hello"
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].stop_reason == "stop"


@pytest.mark.asyncio
async def test_multiple_text_deltas_concat() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hello ")),
        make_event(text_delta_chunk("world", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert "".join(e.delta for e in text_events) == "hello world"


@pytest.mark.asyncio
async def test_content_none_does_not_emit_text_event() -> None:
    """delta.content=None → 不 yield TextDeltaEvent."""
    stream = _FakeAsyncStream([
        make_event(empty_delta_chunk(finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert not any(isinstance(e, TextDeltaEvent) for e in events)
    assert isinstance(events[-1], DoneEvent)


# ============================================================================
# usage handling
# ============================================================================


@pytest.mark.asyncio
async def test_usage_only_chunk_no_choices() -> None:
    """末尾 usage-only chunk（choices=[]）→ continue；不抛错."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
        make_event(usage_only_chunk(prompt_tokens=10, completion_tokens=2)),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.usage.input == 10
    assert done.usage.output == 2
    assert done.usage.total_tokens == 12


@pytest.mark.asyncio
async def test_usage_missing_returns_default_usage() -> None:
    """usage 缺失 → 默认 Usage()（全 0）."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done = events[-1]
    assert done.usage.input == 0
    assert done.usage.output == 0
    assert done.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_usage_with_choices_in_same_chunk() -> None:
    """一个 chunk 同时含 choices 和 usage."""
    stream = _FakeAsyncStream([
        make_event(mixed_delta_chunk(
            content="hi",
            finish_reason="stop",
            usage=make_usage(prompt_tokens=5, completion_tokens=1),
        )),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done = events[-1]
    assert done.usage.input == 5
    assert done.usage.output == 1


@pytest.mark.asyncio
async def test_multiple_usage_chunks_take_last_non_empty() -> None:
    """多个 usage chunk——使用最新非空值."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
        make_event(usage_only_chunk(prompt_tokens=5, completion_tokens=1)),
        make_event(usage_only_chunk(prompt_tokens=10, completion_tokens=2)),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done = events[-1]
    assert done.usage.input == 10
    assert done.usage.output == 2


# ============================================================================
# finish_reason 映射
# ============================================================================


@pytest.mark.asyncio
async def test_finish_reason_stop() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("done", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert events[-1].stop_reason == "stop"


@pytest.mark.asyncio
async def test_finish_reason_length() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("..." * 100, finish_reason="length")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert events[-1].stop_reason == "length"


@pytest.mark.asyncio
async def test_finish_reason_tool_calls_maps_to_tool_use() -> None:
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments='{"x":1}', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert events[-1].stop_reason == "tool_use"


# ============================================================================
# reasoning_content 忽略（修订 I）
# ============================================================================


@pytest.mark.asyncio
async def test_reasoning_content_ignored() -> None:
    """reasoning_content chunk → 不生成 TextDeltaEvent."""
    stream = _FakeAsyncStream([
        make_event(reasoning_chunk("internal reasoning")),
        make_event(text_delta_chunk("visible answer", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert [e.delta for e in text_events] == ["visible answer"]


@pytest.mark.asyncio
async def test_reasoning_only_stream_terminates_with_done() -> None:
    """全部 reasoning_content + finish_reason=stop → DoneEvent 无 text."""
    stream = _FakeAsyncStream([
        make_event(reasoning_chunk("thinking", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert not any(isinstance(e, TextDeltaEvent) for e in events)
    assert isinstance(events[-1], DoneEvent)


# ============================================================================
# 单个 DoneEvent
# ============================================================================


@pytest.mark.asyncio
async def test_only_one_done_event_emitted() -> None:
    """整个正常流只能产生一个最终 DoneEvent."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("a")),
        make_event(text_delta_chunk("b")),
        make_event(text_delta_chunk("c", finish_reason="stop")),
        make_event(usage_only_chunk(prompt_tokens=1, completion_tokens=1)),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done_events = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done_events) == 1


# ============================================================================
# 非 chunk event 跳过
# ============================================================================


@pytest.mark.asyncio
async def test_non_chunk_events_skipped() -> None:
    """SDK 可能产生 content_part / tool_choice 等非 chunk 事件——continue."""
    stream = _FakeAsyncStream([
        make_non_chunk_event("content_part"),
        make_event(text_delta_chunk("hi", finish_reason="stop")),
        make_non_chunk_event("tool_choice"),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    assert any(isinstance(e, TextDeltaEvent) for e in events)


# ============================================================================
# tool calling
# ============================================================================


@pytest.mark.asyncio
async def test_single_tool_call_single_chunk() -> None:
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments='{"x":1}', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tc_events) == 1
    tc = tc_events[0].tool_call
    assert tc.id == "t1"
    assert tc.name == "echo"
    assert tc.arguments == {"x": 1}


@pytest.mark.asyncio
async def test_single_tool_call_split_across_chunks() -> None:
    """arguments 增量 JSON 字符串分片返回."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(0, id="t1", name="echo", arguments='{"text":')),
        make_event(tool_call_delta_chunk(0, arguments='"hi"}')),
        make_event(empty_delta_chunk(finish_reason="tool_calls")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tc_events) == 1
    assert tc_events[0].tool_call.arguments == {"text": "hi"}


@pytest.mark.asyncio
async def test_multiple_tool_calls_interleaved() -> None:
    """两个 tool calls 交错到达（index 0 / 1 交替）."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(0, id="t1", name="alpha", arguments='{"a":')),
        make_event(tool_call_delta_chunk(1, id="t2", name="beta", arguments='{"b":')),
        make_event(tool_call_delta_chunk(0, arguments='1}')),
        make_event(tool_call_delta_chunk(1, arguments='2}')),
        make_event(empty_delta_chunk(finish_reason="tool_calls")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tc_events) == 2
    # 按 index 升序
    assert tc_events[0].tool_call.id == "t1"
    assert tc_events[0].tool_call.arguments == {"a": 1}
    assert tc_events[1].tool_call.id == "t2"
    assert tc_events[1].tool_call.arguments == {"b": 2}


@pytest.mark.asyncio
async def test_tool_calls_emitted_in_index_order() -> None:
    """即使 chunk 中 index 顺序乱，最终 emit 按 index 升序."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(2, id="t3", name="c")),
        make_event(tool_call_delta_chunk(0, id="t1", name="a")),
        make_event(tool_call_delta_chunk(1, id="t2", name="b")),
        make_event(empty_delta_chunk(finish_reason="tool_calls")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    ids = [e.tool_call.id for e in tc_events]
    assert ids == ["t1", "t2", "t3"]


@pytest.mark.asyncio
async def test_tool_call_flushed_on_normal_stream_end_without_finish_reason() -> None:
    """正常 stream 结束 + buffer 非空 → 也 flush tool calls."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(0, id="t1", name="echo", arguments='{"x":1}')),
        # 没有 finish_reason=tool_calls 的 chunk
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tc_events) == 1


@pytest.mark.asyncio
async def test_tool_call_event_emitted_before_done_event() -> None:
    """ToolCallEvent 必须在 DoneEvent 之前."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments='{}', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_idx = next(i for i, e in enumerate(events) if isinstance(e, ToolCallEvent))
    done_idx = next(i for i, e in enumerate(events) if isinstance(e, DoneEvent))
    assert tc_idx < done_idx


@pytest.mark.asyncio
async def test_tool_call_missing_id_raises_protocol_error() -> None:
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, name="echo", arguments='{}', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError) as exc_info:
        await _collect(adapter, _req())
    # 错误信息固定短文本——不含正文
    assert "echo" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_call_missing_name_raises_protocol_error() -> None:
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(0, id="t1", arguments='{}', finish_reason="tool_calls")),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError):
        await _collect(adapter, _req())


@pytest.mark.asyncio
async def test_tool_call_invalid_json_raises_protocol_error() -> None:
    """arguments 拼接后非法 JSON → ProviderProtocolError."""
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments="{not-json", finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError) as exc_info:
        await _collect(adapter, _req())
    # 不得把原始 arguments 放进错误
    assert "not-json" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_call_non_object_json_raises_protocol_error() -> None:
    """arguments 是合法 JSON array → 仍拒绝（必须是 object）."""
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments="[1,2,3]", finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError):
        await _collect(adapter, _req())


@pytest.mark.asyncio
async def test_tool_call_scalar_json_raises_protocol_error() -> None:
    """arguments 是 JSON scalar（如字符串）→ 拒绝."""
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments='"hello"', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError):
        await _collect(adapter, _req())

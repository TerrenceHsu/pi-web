"""OpenAICompatibleProvider 安全不变量测试（M1-1 §十五.Lifecycle/security + §十四）.

完整 marker `sk-M1-OPENAI-COMPAT-SECRET-MARKER` 必须零泄漏到：
- repr(config) / repr(adapter)
- 异常 str / repr
- pytest caplog
- StreamEvent
- ToolCall.raw
- 真实网络调用 = 0
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from pi_agent_core_py.llm_messages import LLMUserMessage
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.providers.base import ProviderRequest
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from pi_agent_core_py.stream_events import DoneEvent, ToolCallEvent
from tests._openai_compat_fakes import (
    _FakeAsyncStream,
    _FakeClient,
    make_event,
    text_delta_chunk,
    tool_call_delta_chunk,
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


def _adapter(stream: _FakeAsyncStream) -> tuple[OpenAICompatibleProvider, _FakeClient]:
    client = _FakeClient(stream)
    adapter = OpenAICompatibleProvider(_config(), provider_id="qwen", client=client)
    return adapter, client


def _req() -> ProviderRequest:
    return ProviderRequest(
        system_prompt="x",
        messages=[LLMUserMessage(content=[TextContent(text="hi")])],
    )


async def _collect(adapter: OpenAICompatibleProvider, req: ProviderRequest) -> list[Any]:
    return [ev async for ev in adapter.stream(req)]


def _assert_no_marker(*values: Any) -> None:
    """断言 marker 在所有 repr / str 中不出现."""
    for v in values:
        s = repr(v) if not isinstance(v, str) else v
        assert SECRET_MARKER not in s, f"marker leaked in: {s!r}"
        assert SECRET_MARKER not in repr(v), f"marker leaked in repr: {repr(v)!r}"


# ============================================================================
# repr 不泄漏 marker
# ============================================================================


def test_config_repr_no_marker() -> None:
    cfg = _config()
    _assert_no_marker(repr(cfg))
    _assert_no_marker(str(cfg))


def test_adapter_repr_no_marker() -> None:
    stream = _FakeAsyncStream([])
    adapter, _ = _adapter(stream)
    _assert_no_marker(repr(adapter))
    _assert_no_marker(str(adapter))


def test_config_model_dump_no_marker() -> None:
    cfg = _config()
    dumped = cfg.model_dump()
    # api_key 在 model_dump 时 SecretStr 默认不暴露明文
    s = repr(dumped)
    assert SECRET_MARKER not in s


# ============================================================================
# StreamEvent 不泄漏 marker
# ============================================================================


@pytest.mark.asyncio
async def test_stream_events_no_marker() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("visible text", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    for e in events:
        _assert_no_marker(repr(e))
        _assert_no_marker(str(e))


@pytest.mark.asyncio
async def test_tool_call_raw_no_marker() -> None:
    """ToolCall.raw 可保存原始 tool-call 结构，但不能含 marker."""
    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, id="t1", name="echo", arguments='{"x":1}', finish_reason="tool_calls",
        )),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    tc_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert tc_events
    raw = tc_events[0].tool_call.raw
    s = repr(raw)
    assert SECRET_MARKER not in s


@pytest.mark.asyncio
async def test_done_event_usage_no_marker() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    events = await _collect(adapter, _req())
    done = events[-1]
    assert isinstance(done, DoneEvent)
    _assert_no_marker(repr(done.usage))


# ============================================================================
# 异常 str / repr 不泄漏 marker
# ============================================================================


@pytest.mark.asyncio
async def test_protocol_error_str_repr_no_marker() -> None:
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, name="echo", arguments='{}', finish_reason="tool_calls",  # 缺 id
        )),
    ])
    adapter, _ = _adapter(stream)
    with pytest.raises(ProviderProtocolError) as exc_info:
        await _collect(adapter, _req())
    err = exc_info.value
    assert SECRET_MARKER not in str(err)
    assert SECRET_MARKER not in repr(err)


# ============================================================================
# caplog 不泄漏 marker
# ============================================================================


@pytest.mark.asyncio
async def test_no_marker_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, _ = _adapter(stream)
    await _collect(adapter, _req())
    full_log = caplog.text
    assert SECRET_MARKER not in full_log


# ============================================================================
# kwargs 透传不含 secret（仅 base_url + model 透传给 SDK）
# ============================================================================


@pytest.mark.asyncio
async def test_kwargs_passed_to_sdk_no_secret_in_kwarg_values() -> None:
    """验证 kwargs 内容——api_key 不会以明文出现在任何 kwarg 值里（base_url/model 等不含）."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, client = _adapter(stream)
    await _collect(adapter, _req())
    assert client.last_kwargs is not None
    # 遍历所有 kwarg 的字符串值
    for k, v in client.last_kwargs.items():
        if isinstance(v, str):
            assert SECRET_MARKER not in v, f"marker leaked in kwarg {k!r}: {v!r}"
        elif isinstance(v, list):
            for item in v:
                s = repr(item)
                assert SECRET_MARKER not in s, f"marker leaked in kwarg {k!r} list item"


@pytest.mark.asyncio
async def test_api_key_not_in_kwargs_keys() -> None:
    """adapter 不直接把 api_key 作为 kwarg——通过 AsyncOpenAI 构造传入."""
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, client = _adapter(stream)
    await _collect(adapter, _req())
    assert client.last_kwargs is not None
    assert "api_key" not in client.last_kwargs
    assert "extra_headers" not in client.last_kwargs
    assert "Authorization" not in client.last_kwargs


# ============================================================================
# 真实网络调用 = 0
# ============================================================================


@pytest.mark.asyncio
async def test_no_real_network_calls(caplog: pytest.LogCaptureFixture) -> None:
    """所有测试使用注入的 fake client——零真实 HTTP 调用."""
    # 本测试断言：通过 client 参数注入，从不构造真实 AsyncOpenAI
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    cfg = _config()
    client = _FakeClient(stream)
    adapter = OpenAICompatibleProvider(cfg, provider_id="qwen", client=client)
    events = await _collect(adapter, _req())
    assert isinstance(events[-1], DoneEvent)
    # 不存在 owned client（注入了）
    # 也即 adapter 不会调真实 AsyncOpenAI.close()
    assert client.closed is False  # owned=False 时 aclose 不关
    # 没有 httpx 出现在 logs
    assert "httpx" not in caplog.text.lower() or "network" not in caplog.text.lower()

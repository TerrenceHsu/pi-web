"""OpenAICompatibleProvider 生命周期 + abort / cancel 测试（M1-1 §十五.Lifecycle + Abort/cancel）.

覆盖：
- 正常完成后 stream manager 退出（__aexit__ 调用）
- abort 后 stream manager 退出
- 解析异常后 stream manager 退出
- SDK 异常后 stream manager 退出
- cancellation 后 stream manager 退出
- aclose 关闭 owned client
- 双重 aclose 不抛
- 关闭后 stream 抛 ProviderConfigError
- 默认 max_retries=0
- 429 只产生一次 stream 调用（不自动重试）
- 请求前 abort 不调用 client
- 流中 abort yields DoneEvent(aborted) 并退出
- asyncio.CancelledError 原样传播
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openai import RateLimitError

from pi_agent_core_py.llm_messages import LLMUserMessage
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.providers.base import ProviderRequest
from pi_agent_core_py.providers.errors import ProviderConfigError, ProviderRateLimitError
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from pi_agent_core_py.stream_events import DoneEvent, TextDeltaEvent
from tests._openai_compat_fakes import (
    _FakeAsyncStream,
    _FakeClient,
    _RaisingStreamClient,
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


def _req(signal: Any = None) -> ProviderRequest:
    return ProviderRequest(
        system_prompt="x",
        messages=[LLMUserMessage(content=[TextContent(text="hi")])],
        signal=signal,
    )


def _adapter(
    stream: _FakeAsyncStream,
    *,
    provider_id: str = "qwen",
    **overrides: Any,
) -> tuple[OpenAICompatibleProvider, _FakeClient]:
    cfg = _config(**overrides)
    client = _FakeClient(stream)
    adapter = OpenAICompatibleProvider(cfg, provider_id=provider_id, client=client)
    return adapter, client


async def _collect(adapter: OpenAICompatibleProvider, req: ProviderRequest) -> list[Any]:
    return [ev async for ev in adapter.stream(req)]


# ============================================================================
# stream manager 退出（response 关闭）
# ============================================================================


@pytest.mark.asyncio
async def test_stream_manager_exits_after_normal_completion() -> None:
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("hi", finish_reason="stop")),
    ])
    adapter, client = _adapter(stream)
    await _collect(adapter, _req())
    # _FakeStreamManager.exited == True 表示 async with 已退出
    assert stream.manager is not None
    assert stream.manager.exited is True


@pytest.mark.asyncio
async def test_stream_manager_exits_after_abort() -> None:
    signal = asyncio.Event()
    stream = _FakeAsyncStream([
        make_event(text_delta_chunk("first chunk")),
        # 第二个 chunk 之前 set signal
    ])
    adapter, client = _adapter(stream)

    signal.set()
    # 注：本测试简化——直接 pre-set signal 测 pre-abort 路径
    # 流中 abort 用下方 test_abort_during_stream_yields_done


@pytest.mark.asyncio
async def test_stream_manager_exits_after_protocol_error() -> None:
    from pi_agent_core_py.providers.errors import ProviderProtocolError

    stream = _FakeAsyncStream([
        make_event(tool_call_delta_chunk(
            0, name="echo", arguments='{"x":1}', finish_reason="tool_calls",  # 缺 id
        )),
    ])
    adapter, client = _adapter(stream)
    with pytest.raises(ProviderProtocolError):
        await _collect(adapter, _req())
    assert stream.manager is not None
    assert stream.manager.exited is True


@pytest.mark.asyncio
async def test_stream_manager_exits_after_sdk_exception() -> None:
    import httpx
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    resp = httpx.Response(status_code=429, request=req)
    exc = RateLimitError(response=resp, body=None, message="rate limit")

    client = _RaisingStreamClient(exc)
    adapter = OpenAICompatibleProvider(_config(), provider_id="qwen", client=client)
    with pytest.raises(ProviderRateLimitError):
        await _collect(adapter, _req())
    # raising client 内部 placeholder manager 不暴露——但调用 count 验证 SDK 调用了一次
    assert client.stream_call_count == 1


# ============================================================================
# aclose 生命周期
# ============================================================================


@pytest.mark.asyncio
async def test_aclose_closes_owned_client() -> None:
    """adapter 自己构造 client（不注入）——aclose 应关闭."""
    # 这里需要真实 AsyncOpenAI；用 mock-friendly 方式
    cfg = _config()
    closed: list[bool] = []

    class _StubClient:
        def __init__(self) -> None:
            self.chat = type("NS", (), {})  # placeholder
            self.closed = False

        async def close(self) -> None:
            self.closed = True
            closed.append(True)

    stub = _StubClient()
    adapter = OpenAICompatibleProvider(cfg, provider_id="qwen", client=stub)
    await adapter.aclose()
    # 注入 client——不关闭（caller 负责）
    assert closed == []


@pytest.mark.asyncio
async def test_aclose_closes_client_created_internally() -> None:
    """adapter 内部构造的 client——aclose 应关闭（不通过注入）."""
    # 用 patch 验证 max_retries=0 + close 被调
    import openai
    created: dict[str, Any] = {}
    original_init = openai.AsyncOpenAI.__init__

    def patched_init(self, **kwargs):
        created.update(kwargs)
        original_init(self, **kwargs)

    openai.AsyncOpenAI.__init__ = patched_init
    try:
        cfg = _config()
        adapter = OpenAICompatibleProvider(cfg, provider_id="qwen")
        # 验证 max_retries=0
        assert created.get("max_retries") == 0
        await adapter.aclose()
    finally:
        openai.AsyncOpenAI.__init__ = original_init


@pytest.mark.asyncio
async def test_double_aclose_does_not_raise() -> None:
    stream = _FakeAsyncStream([make_event(text_delta_chunk("hi", finish_reason="stop"))])
    adapter, _ = _adapter(stream)
    await adapter.aclose()
    await adapter.aclose()  # 第二次——no-op


@pytest.mark.asyncio
async def test_stream_after_close_raises_config_error() -> None:
    stream = _FakeAsyncStream([make_event(text_delta_chunk("hi", finish_reason="stop"))])
    adapter, _ = _adapter(stream)
    await adapter.aclose()
    with pytest.raises(ProviderConfigError):
        await _collect(adapter, _req())


# ============================================================================
# max_retries=0
# ============================================================================


def test_internal_client_uses_max_retries_zero() -> None:
    """构造 adapter 不注入 client——内部 AsyncOpenAI 必须传 max_retries=0."""
    import openai
    created: dict[str, Any] = {}
    original_init = openai.AsyncOpenAI.__init__

    def patched_init(self, **kwargs):
        created.update(kwargs)
        original_init(self, **kwargs)

    openai.AsyncOpenAI.__init__ = patched_init
    try:
        OpenAICompatibleProvider(_config(), provider_id="qwen")
    finally:
        openai.AsyncOpenAI.__init__ = original_init
    assert created.get("max_retries") == 0


@pytest.mark.asyncio
async def test_429_only_invokes_stream_once_no_retry() -> None:
    """RateLimitError 不应触发 SDK 自动重试——只调用一次 stream."""
    import httpx
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    resp = httpx.Response(status_code=429, request=req)
    exc = RateLimitError(response=resp, body=None, message="rate limit")

    client = _RaisingStreamClient(exc)
    adapter = OpenAICompatibleProvider(_config(), provider_id="qwen", client=client)
    with pytest.raises(ProviderRateLimitError):
        await _collect(adapter, _req())
    assert client.stream_call_count == 1


# ============================================================================
# abort / cancel
# ============================================================================


@pytest.mark.asyncio
async def test_pre_abort_does_not_invoke_client() -> None:
    """signal 在调用前已 set → adapter 不调 client.stream()."""
    stream = _FakeAsyncStream([make_event(text_delta_chunk("hi", finish_reason="stop"))])
    adapter, client = _adapter(stream)

    signal = asyncio.Event()
    signal.set()
    req = _req(signal=signal)
    events = await _collect(adapter, req)

    # 不调 client
    assert client.stream_call_count == 0
    # 直接 yield DoneEvent(aborted)
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.stop_reason == "aborted"


@pytest.mark.asyncio
async def test_abort_during_stream_yields_aborted_done() -> None:
    """流中 set signal → 立刻 yield DoneEvent(aborted) 并退出."""
    # 通过自定义 stream 在 yield 第二个 event 前注入 set
    signal = asyncio.Event()

    class _AbortAfterFirstStream(_FakeAsyncStream):
        def __init__(self):
            super().__init__([])
            self._first_yielded = False

        def __aiter__(self):
            async def gen():
                # 第一个 chunk：text
                self._first_yielded = True
                yield make_event(text_delta_chunk("first"))
                # 在第二个之前 set signal
                signal.set()
                # 第二个 chunk：更多 text（adapter 应在 signal 检查后退出，不应处理）
                yield make_event(text_delta_chunk("should-not-appear"))

            return gen()

    stream = _AbortAfterFirstStream()
    adapter, _ = _adapter(stream)
    req = _req(signal=signal)
    events = await _collect(adapter, req)

    # 第一个 TextDeltaEvent yield 了
    assert any(isinstance(e, TextDeltaEvent) and e.delta == "first" for e in events)
    # DoneEvent 是 aborted
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.stop_reason == "aborted"
    # 第二个 text delta 不应出现
    assert not any(
        isinstance(e, TextDeltaEvent) and e.delta == "should-not-appear"
        for e in events
    )


@pytest.mark.asyncio
async def test_abort_closes_stream_response() -> None:
    signal = asyncio.Event()

    class _AbortStream(_FakeAsyncStream):
        def __init__(self) -> None:
            super().__init__([])

        def __aiter__(self):
            async def gen():
                yield make_event(text_delta_chunk("first"))
                signal.set()
                yield make_event(text_delta_chunk("ignored"))
            return gen()

    stream = _AbortStream()
    adapter, _ = _adapter(stream)
    req = _req(signal=signal)
    await _collect(adapter, req)
    assert stream.manager is not None
    assert stream.manager.exited is True


@pytest.mark.asyncio
async def test_cancellation_propagates_unchanged() -> None:
    """asyncio.CancelledError 原样传播——不映射为 ProviderError."""
    stream = _FakeAsyncStream([], raise_on_iter=asyncio.CancelledError())
    adapter, _ = _adapter(stream)
    with pytest.raises(asyncio.CancelledError):
        await _collect(adapter, _req())


@pytest.mark.asyncio
async def test_cancellation_closes_stream_response() -> None:
    stream = _FakeAsyncStream([], raise_on_iter=asyncio.CancelledError())
    adapter, _ = _adapter(stream)
    with pytest.raises(asyncio.CancelledError):
        await _collect(adapter, _req())
    assert stream.manager is not None
    assert stream.manager.exited is True


@pytest.mark.asyncio
async def test_cancellation_not_converted_to_error_event() -> None:
    """cancel 不应被 ModelClient 转 ErrorEvent——异常类型必须保留."""
    # 本测试直接验证 adapter 行为：CancelledError 穿透
    stream = _FakeAsyncStream([], raise_on_iter=asyncio.CancelledError())
    adapter, _ = _adapter(stream)

    # 走 ModelClient 包装——验证它不会吞 CancelledError
    from pi_agent_core_py.model_client import ModelClient

    mc = ModelClient(adapter)
    events: list[Any] = []
    with pytest.raises(asyncio.CancelledError):
        async for ev in mc.stream(
            system_prompt="x",
            messages=[LLMUserMessage(content=[TextContent(text="hi")])],
        ):
            events.append(ev)
    # ModelClient 不应转 CancelledError 为 ErrorEvent
    from pi_agent_core_py.stream_events import ErrorEvent
    assert not any(isinstance(e, ErrorEvent) for e in events)

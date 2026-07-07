"""Step 21 — ModelClient thin wrapper 行为测试。

覆盖：
- ModelClient.stream 把请求委托给 adapter
- ProviderError → ErrorEvent
- 未知 Exception → ErrorEvent
- tools=None 时转 [] 给 adapter
- metadata 可附加在 ProviderRequest 上
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from pi_agent_core_py import (
    DoneEvent,
    ErrorEvent,
    FakeProviderAdapter,
    ModelClient,
    ProviderAdapter,
    ProviderProtocolError,
    ProviderRequest,
    StreamEvent,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.messages import ToolCall, Usage

# ============================================================================
# 测试用 adapter
# ============================================================================


class _RecordingAdapter(ProviderAdapter):
    """记录每次 stream 请求；返回固定事件序列。"""

    provider_id = "test"
    model = "test-1"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        self.calls.append(request)
        yield TextDeltaEvent(delta="hi")
        yield DoneEvent(stop_reason="stop", usage=Usage())


class _ExplodingAdapter(ProviderAdapter):
    """抛 ProviderError 子类。"""

    provider_id = "explode"
    model = "x"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        yield TextDeltaEvent(delta="before-error")
        raise ProviderProtocolError("bad json")
        yield  # type: ignore[unreachable]  # 让 mypy 把这个 fn 当 generator


class _WildcardAdapter(ProviderAdapter):
    """抛未知 Exception。"""

    provider_id = "wild"
    model = "x"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("unexpected")
        yield  # type: ignore[unreachable]


class _ToolCallAdapter(ProviderAdapter):
    """yields 一个 ToolCallEvent。"""

    provider_id = "tools"
    model = "x"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        yield ToolCallEvent(
            tool_call=ToolCall(id="t1", name="echo", arguments={"text": "hi"}),
        )
        yield DoneEvent(stop_reason="tool_use", usage=Usage())


# ============================================================================
# Tests
# ============================================================================


@pytest.mark.asyncio
async def test_model_client_delegates_to_adapter() -> None:
    """ModelClient.stream 构造 ProviderRequest 并委托给 adapter。"""
    adapter = _RecordingAdapter()
    client = ModelClient(adapter)

    events = []
    async for ev in client.stream(
        system_prompt="sys",
        messages=[],
        tools=None,
    ):
        events.append(ev)

    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call.system_prompt == "sys"
    assert call.tools == []  # tools=None → []
    assert isinstance(events[0], TextDeltaEvent)
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_model_client_tools_none_becomes_empty_list() -> None:
    """tools=None 时 adapter 收到的是 []（不是 None），避免 None check 散落。"""
    adapter = _RecordingAdapter()
    client = ModelClient(adapter)
    async for _ in client.stream(system_prompt="x", messages=[], tools=None):
        pass
    assert adapter.calls[0].tools == []


@pytest.mark.asyncio
async def test_model_client_signal_passed_through() -> None:
    """signal 透传给 ProviderRequest.signal。"""
    adapter = _RecordingAdapter()
    client = ModelClient(adapter)
    sig = asyncio.Event()
    async for _ in client.stream(
        system_prompt="x", messages=[], signal=sig,
    ):
        pass
    assert adapter.calls[0].signal is sig


@pytest.mark.asyncio
async def test_model_client_provider_error_to_error_event() -> None:
    """ProviderError 子类被捕获 → ErrorEvent，type/message 保留。"""
    client = ModelClient(_ExplodingAdapter())

    events = []
    async for ev in client.stream(system_prompt="x", messages=[]):
        events.append(ev)

    # 第一条是 normal text delta，第二条是 ErrorEvent
    assert isinstance(events[0], TextDeltaEvent)
    assert isinstance(events[-1], ErrorEvent)
    assert "ProviderProtocolError" in events[-1].message
    assert "bad json" in events[-1].message


@pytest.mark.asyncio
async def test_model_client_unknown_exception_to_error_event() -> None:
    """未知 Exception 也转 ErrorEvent（type 信息保留）。"""
    client = ModelClient(_WildcardAdapter())
    events = []
    async for ev in client.stream(system_prompt="x", messages=[]):
        events.append(ev)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert "RuntimeError" in events[0].message
    assert "unexpected" in events[0].message


@pytest.mark.asyncio
async def test_model_client_exposes_provider_id_and_model() -> None:
    """ModelClient 从 adapter 抄出 provider_id / model 字段。"""
    adapter = _ToolCallAdapter()
    client = ModelClient(adapter)
    assert client.provider_id == "tools"
    assert client.model == "x"


@pytest.mark.asyncio
async def test_model_client_preserves_tool_call_event() -> None:
    """ToolCallEvent 透传，不被 ModelClient 改写。"""
    client = ModelClient(_ToolCallAdapter())
    events = []
    async for ev in client.stream(system_prompt="x", messages=[]):
        events.append(ev)

    assert isinstance(events[0], ToolCallEvent)
    assert events[0].tool_call.name == "echo"
    assert events[0].tool_call.arguments == {"text": "hi"}


@pytest.mark.asyncio
async def test_model_client_with_fake_adapter_via_fake_client() -> None:
    """FakeClient + ModelClient + FakeProviderAdapter 三层组合仍可用。"""
    fake = FakeProviderAdapter([
        [TextDeltaEvent(delta="hi"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])
    # 直接用 ModelClient(adapter)——证明 ModelClient 不挑剔 adapter 子类
    client = ModelClient(fake)
    events = []
    async for ev in client.stream(
        system_prompt="x",
        messages=[],
        tools=None,
    ):
        events.append(ev)
    assert events[0].delta == "hi"


@pytest.mark.asyncio
async def test_model_client_metadata_passed_to_request() -> None:
    """metadata kwarg 透传到 ProviderRequest.metadata。"""
    adapter = _RecordingAdapter()
    client = ModelClient(adapter)
    async for _ in client.stream(
        system_prompt="x",
        messages=[],
        metadata={"request_id": "abc", "trace": "xyz"},
    ):
        pass
    assert adapter.calls[0].metadata == {"request_id": "abc", "trace": "xyz"}


@pytest.mark.asyncio
async def test_model_client_metadata_default_empty_dict() -> None:
    """不传 metadata → ProviderRequest.metadata = {}（不是 None）。"""
    adapter = _RecordingAdapter()
    client = ModelClient(adapter)
    async for _ in client.stream(system_prompt="x", messages=[]):
        pass
    assert adapter.calls[0].metadata == {}

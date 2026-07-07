"""Step 21 — FakeProviderAdapter / FakeClient 兼容性测试。

覆盖：
- FakeClient 旧构造方式仍可用（多 turn 消耗顺序）
- FakeProviderAdapter 多 turn 消耗顺序正确
- last_system_prompt / last_messages / last_tools 记录正确
- abort signal set 后立即收敛
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py import (
    DoneEvent,
    ErrorEvent,
    FakeClient,
    FakeProviderAdapter,
    LLMUserMessage,
    ProviderRequest,
    StreamEvent,
    TextDeltaEvent,
    ToolDef,
)
from pi_agent_core_py.messages import TextContent, Usage


def _user_msg(text: str) -> LLMUserMessage:
    return LLMUserMessage(content=[TextContent(text=text)])


def _text_turn(text: str) -> list[StreamEvent]:
    return [TextDeltaEvent(delta=text), DoneEvent(stop_reason="stop", usage=Usage())]


# ============================================================================
# FakeProviderAdapter
# ============================================================================


@pytest.mark.asyncio
async def test_fake_adapter_consumes_turns_in_order() -> None:
    """每次 stream() 取一个 turn，按列表顺序；耗尽后给 no more scripts。"""
    adapter = FakeProviderAdapter([
        _text_turn("hello"),
        _text_turn("world"),
    ])

    req = ProviderRequest(system_prompt="x", messages=[])

    async def collect() -> list[StreamEvent]:
        return [ev async for ev in adapter.stream(req)]

    first = await collect()
    second = await collect()
    third = await collect()

    assert isinstance(first[-1], DoneEvent)
    assert first[0].delta == "hello"
    assert second[0].delta == "world"
    # 第三次：脚本耗尽 → ErrorEvent
    assert isinstance(third[0], ErrorEvent)
    assert "no more scripts" in third[0].message


@pytest.mark.asyncio
async def test_fake_adapter_records_system_prompt_messages_tools() -> None:
    """last_system_prompt / last_messages / last_tools 字段记录最新一次调用。"""
    adapter = FakeProviderAdapter([_text_turn("ok")])
    req = ProviderRequest(
        system_prompt="you are friendly",
        messages=[_user_msg("hi")],
        tools=[ToolDef(
            name="echo",
            label="echo",
            description="d",
            parameters={"type": "object"},
        )],
    )
    async for _ in adapter.stream(req):
        pass

    assert adapter.last_system_prompt == "you are friendly"
    assert len(adapter.last_messages) == 1
    assert adapter.last_tools is not None
    assert adapter.last_tools[0].name == "echo"


@pytest.mark.asyncio
async def test_fake_adapter_records_all_calls_history() -> None:
    """all_messages_calls / all_tools_calls / all_system_prompt_calls 保留每次调用。"""
    adapter = FakeProviderAdapter([_text_turn("a"), _text_turn("b")])
    req1 = ProviderRequest(
        system_prompt="p1",
        messages=[_user_msg("m1")],
    )
    req2 = ProviderRequest(
        system_prompt="p2",
        messages=[_user_msg("m2")],
    )
    async for _ in adapter.stream(req1):
        pass
    async for _ in adapter.stream(req2):
        pass

    assert len(adapter.all_messages_calls) == 2
    assert len(adapter.all_system_prompt_calls) == 2
    assert adapter.all_system_prompt_calls == ["p1", "p2"]


@pytest.mark.asyncio
async def test_fake_adapter_aborts_on_signal_set() -> None:
    """signal.is_set() → 立即 yield DoneEvent(stop_reason="aborted") 并返回。

    Step 21 统一 abort 语义：aborted 走 DoneEvent，不走 ErrorEvent。
    """
    adapter = FakeProviderAdapter([_text_turn("hello")])

    signal = asyncio.Event()
    signal.set()

    req = ProviderRequest(system_prompt="x", messages=[], signal=signal)
    events = [ev async for ev in adapter.stream(req)]

    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].stop_reason == "aborted"


@pytest.mark.asyncio
async def test_fake_adapter_tools_empty_recorded_as_none() -> None:
    """空 tools（[]）按旧 FakeClient 语义记录为 None（list(tools) if tools else None）。"""
    adapter = FakeProviderAdapter([_text_turn("ok")])
    req = ProviderRequest(system_prompt="x", messages=[], tools=[])
    async for _ in adapter.stream(req):
        pass
    # list([]) if [] else None → None（[] 是 falsy）
    assert adapter.last_tools is None


# ============================================================================
# FakeClient 向后兼容
# ============================================================================


@pytest.mark.asyncio
async def test_fake_client_legacy_construction_works() -> None:
    """FakeClient(scripts) 旧 API 不破坏。"""
    fake = FakeClient([_text_turn("hi")])

    events = []
    async for ev in fake.stream(
        system_prompt="x",
        messages=[_user_msg("hello")],
        tools=None,
    ):
        events.append(ev)

    assert isinstance(events[0], TextDeltaEvent)
    assert events[0].delta == "hi"
    assert isinstance(events[-1], DoneEvent)
    # 兼容字段：FakeClient 转发给 adapter
    assert fake.last_system_prompt == "x"
    assert len(fake.last_messages) == 1
    assert fake.last_tools is None  # tools=None 走旧语义


@pytest.mark.asyncio
async def test_fake_client_legacy_multi_turn_consumption() -> None:
    """FakeClient 多轮脚本按顺序消费。"""
    fake = FakeClient([
        _text_turn("turn1"),
        _text_turn("turn2"),
    ])

    async def call() -> list[StreamEvent]:
        out: list[StreamEvent] = []
        async for ev in fake.stream(system_prompt="x", messages=[]):
            out.append(ev)
        return out

    a = await call()
    b = await call()
    c = await call()

    assert a[0].delta == "turn1"
    assert b[0].delta == "turn2"
    assert isinstance(c[0], ErrorEvent)


@pytest.mark.asyncio
async def test_fake_client_records_all_calls_history() -> None:
    """FakeClient.all_messages_calls 保留每次调用历史（与旧版一致）。"""
    fake = FakeClient([_text_turn("a")])
    async for _ in fake.stream(system_prompt="s1", messages=[_user_msg("m1")]):
        pass
    async for _ in fake.stream(system_prompt="s2", messages=[_user_msg("m2")]):
        pass

    assert len(fake.all_messages_calls) == 2
    assert len(fake.all_system_prompt_calls) == 2
    assert fake.all_system_prompt_calls == ["s1", "s2"]


@pytest.mark.asyncio
async def test_fake_client_aborts_on_signal_set() -> None:
    """FakeClient + signal → DoneEvent(stop_reason="aborted")（Step 21 新约定）。"""
    fake = FakeClient([_text_turn("hello")])
    signal = asyncio.Event()
    signal.set()

    events = []
    async for ev in fake.stream(
        system_prompt="x", messages=[], signal=signal,
    ):
        events.append(ev)

    assert len(events) == 1
    assert isinstance(events[0], DoneEvent)
    assert events[0].stop_reason == "aborted"

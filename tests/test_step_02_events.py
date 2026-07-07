"""Step 2 — Event Stream 单元测试。

测试 src/pi_agent_core_py/ 演进版（用 FakeClient，离线）。

5 个场景：
1. 正常流式输出事件顺序
2. assistant 文本拼接正确
3. agent_end 包含 user + assistant
4. ErrorEvent 会产生 stop_reason="error"
5. ErrorEvent 情况下仍然有 turn_end 和 agent_end
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    AgentStartEvent,
    DoneEvent,
    ErrorEvent,
    FakeClient,
    MessageEndEvent,
    MessageUpdateEvent,
    TextDeltaEvent,
    TurnEndEvent,
    TurnStartEvent,
    Usage,
    run_event_loop,
)

# ============================================================================
# 1. 正常流式输出事件顺序
# ============================================================================


@pytest.mark.asyncio
async def test_event_order_normal_stream() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="Hello"),
        TextDeltaEvent(delta=", world"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])

    events = []
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        events.append(ev)

    types = [e.type for e in events]
    assert types == [
        "agent_start",
        "turn_start",
        "message_start",   # user
        "message_end",     # user
        "message_start",   # assistant (empty partial)
        "message_update",  # "Hello"
        "message_update",  # "Hello, world"
        "message_end",     # assistant final
        "turn_end",
        "agent_end",
    ], f"实际序列: {types}"

    # 同时验证关键事件的实例类型
    assert isinstance(events[0], AgentStartEvent)
    assert isinstance(events[1], TurnStartEvent)
    assert isinstance(events[-1], AgentEndEvent)


# ============================================================================
# 2. assistant 文本拼接正确
# ============================================================================


@pytest.mark.asyncio
async def test_assistant_text_concat() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="Hello"),
        TextDeltaEvent(delta=", "),
        TextDeltaEvent(delta="world!"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])

    final_assistant = None
    last_update_text: str | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        if isinstance(ev, MessageUpdateEvent):
            last_update_text = ev.message.content[0].text
        elif isinstance(ev, MessageEndEvent) and ev.message.role == "assistant":
            final_assistant = ev.message

    assert final_assistant is not None
    assert final_assistant.content[0].text == "Hello, world!"
    # 最后一次 partial 应当与 final 一致（partial 不丢字符）
    assert last_update_text == "Hello, world!"


# ============================================================================
# 3. agent_end 包含 user + assistant
# ============================================================================


@pytest.mark.asyncio
async def test_agent_end_messages() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])

    agent_end = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert len(agent_end.messages) == 2
    assert agent_end.messages[0].role == "user"
    assert agent_end.messages[1].role == "assistant"
    # user 文本就是输入
    assert agent_end.messages[0].content[0].text == "hi"
    # assistant 文本由 stream 拼装
    assert agent_end.messages[1].content[0].text == "ok"


# ============================================================================
# 4. ErrorEvent 会产生 stop_reason="error"
# ============================================================================


@pytest.mark.asyncio
async def test_error_event_sets_stop_reason() -> None:
    fake = FakeClient([[ErrorEvent(message="network down")]])

    final_assistant = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        if isinstance(ev, MessageEndEvent) and ev.message.role == "assistant":
            final_assistant = ev.message

    assert final_assistant is not None
    assert final_assistant.stop_reason == "error"
    assert final_assistant.error_message == "network down"
    # 错误时 content 通常是空
    assert final_assistant.content == []


# ============================================================================
# 5. ErrorEvent 情况下仍然有 turn_end 和 agent_end
# ============================================================================


@pytest.mark.asyncio
async def test_error_event_keeps_full_sequence() -> None:
    fake = FakeClient([[ErrorEvent(message="boom")]])

    types: list[str] = []
    agent_end = None
    turn_end = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        types.append(ev.type)
        if isinstance(ev, AgentEndEvent):
            agent_end = ev
        elif isinstance(ev, TurnEndEvent):
            turn_end = ev

    # 关键事件都在
    assert "message_end" in types, "assistant message_end 必须发（即使错误）"
    assert "turn_end" in types
    assert types[-1] == "agent_end", "agent_end 必须是最后一个事件"

    # 错误情况下 agent_end 仍然有 2 条消息（user + error assistant）
    assert agent_end is not None
    assert len(agent_end.messages) == 2
    assert agent_end.messages[1].stop_reason == "error"
    assert agent_end.messages[1].error_message == "boom"

    # turn_end 的 message 也是 error assistant
    assert turn_end is not None
    assert turn_end.message.stop_reason == "error"
    assert turn_end.tool_results == []   # Step 2 永远为空

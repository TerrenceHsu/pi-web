"""Step 3 — Context 转换 单元测试。

覆盖：
1. convert_to_llm 基础转换（UserMessage/AssistantMessage）
2. CustomMessage 默认过滤
3. transform_context 默认原样返回
4. run_event_loop 接入转换后事件顺序不变
5. ModelClient 收到的是 LLMMessage（不含 CustomMessage）
6. 错误流仍然正常收敛
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    AssistantMessage,
    CustomMessage,
    DoneEvent,
    ErrorEvent,
    FakeClient,
    LLMAssistantMessage,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
    Usage,
    UserMessage,
    convert_to_llm,
    run_event_loop,
    transform_context,
)

# ============================================================================
# 1. convert_to_llm 基础转换
# ============================================================================


@pytest.mark.asyncio
async def test_convert_to_llm_basic() -> None:
    user = UserMessage(content=[TextContent(text="hi")])
    assistant = AssistantMessage(
        content=[TextContent(text="hello")],
        api="anthropic-messages", provider="glm", model="glm-4.5-flash",
    )
    out = convert_to_llm([user, assistant])

    assert len(out) == 2
    assert isinstance(out[0], LLMUserMessage)
    assert isinstance(out[1], LLMAssistantMessage)
    # 内容保持一致
    assert out[0].content[0].text == "hi"
    assert out[1].content[0].text == "hello"
    # assistant 元数据保留
    assert out[1].api == "anthropic-messages"
    assert out[1].provider == "glm"
    assert out[1].model == "glm-4.5-flash"


# ============================================================================
# 2. CustomMessage 默认过滤
# ============================================================================


@pytest.mark.asyncio
async def test_convert_to_llm_filters_custom() -> None:
    user = UserMessage(content=[TextContent(text="hi")])
    note = CustomMessage(custom_type="notification", content="invisible")
    assistant = AssistantMessage(
        content=[TextContent(text="ok")],
        api="x", provider="y", model="z",
    )
    out = convert_to_llm([user, note, assistant])

    assert len(out) == 2  # CustomMessage 被过滤
    assert all(not isinstance(m, CustomMessage) for m in out)
    # 顺序保留
    assert out[0].role == "user"
    assert out[1].role == "assistant"


# ============================================================================
# 3. transform_context 默认原样返回
# ============================================================================


@pytest.mark.asyncio
async def test_transform_context_default_passthrough() -> None:
    msgs = [
        UserMessage(content=[TextContent(text="a")]),
        CustomMessage(custom_type="x", content="y"),
        AssistantMessage(content=[TextContent(text="b")], api="", provider="", model=""),
    ]
    out = await transform_context(msgs)

    assert len(out) == 3
    # 同一对象（默认实现不复制）
    assert out is msgs or [id(a) for a in out] == [id(b) for b in msgs]
    # 内容不变
    assert out[0].content[0].text == "a"
    assert out[2].content[0].text == "b"
    assert isinstance(out[1], CustomMessage)


# ============================================================================
# 4. run_event_loop 接入转换后事件顺序不变
# ============================================================================


@pytest.mark.asyncio
async def test_event_sequence_unchanged_after_context_hook() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="hello"),
        TextDeltaEvent(delta=" world"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    types = []
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        types.append(ev.type)

    assert types == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ], f"实际序列: {types}"


# ============================================================================
# 5. ModelClient 收到的是 LLMMessage（不含 CustomMessage）
# ============================================================================


@pytest.mark.asyncio
async def test_fake_client_receives_llm_messages_only() -> None:
    """用 transform_context_fn 注入 CustomMessage，验证 FakeClient 收到的 messages
    里不含 CustomMessage（被 convert_to_llm 过滤）。"""
    fake = FakeClient([[DoneEvent(stop_reason="stop", usage=Usage())]])

    async def inject_custom(messages):
        return list(messages) + [
            CustomMessage(custom_type="note", content="should not reach LLM"),
        ]

    async for _ in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
        transform_context_fn=inject_custom,
    ):
        pass

    # FakeClient.last_messages 是转换后的 LLMMessage 列表
    assert hasattr(fake, "last_messages")
    assert len(fake.last_messages) == 1  # 只有 user，CustomMessage 被过滤
    assert isinstance(fake.last_messages[0], LLMUserMessage)
    # 类型层确保：last_messages 只含 LLMMessage
    for m in fake.last_messages:
        assert isinstance(m, (LLMUserMessage, LLMAssistantMessage))


# ============================================================================
# 6. 错误流仍然正常收敛
# ============================================================================


@pytest.mark.asyncio
async def test_error_flow_still_converges() -> None:
    fake = FakeClient([[ErrorEvent(message="boom")]])
    types = []
    agent_end = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi", client=fake,
    ):
        types.append(ev.type)
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    # 错误时也走完整序列
    assert "turn_end" in types
    assert types[-1] == "agent_end"
    # assistant.stop_reason == "error"
    assert agent_end is not None
    assert len(agent_end.messages) == 2
    assert agent_end.messages[1].stop_reason == "error"
    assert agent_end.messages[1].error_message == "boom"

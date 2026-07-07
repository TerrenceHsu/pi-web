"""Integration test 1: 真实 GLM 文本 smoke。

UserMessage → GLMClient → TextDeltaEvent → DoneEvent → AssistantMessage。

slow 标记——只在 -m slow 时跑。
"""
from __future__ import annotations

import os

import pytest

from pi_agent_core_py import (
    DoneEvent,
    GLMClient,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
)
from pi_agent_core_py.loop import run_min_loop

_HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.environ.get("ANTHROPIC_API_KEY")
)
_GLM_SKIP_REASON = (
    "需要至少设置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 之一"
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _HAS_GLM_CREDS, reason=_GLM_SKIP_REASON),
]


@pytest.mark.asyncio
async def test_real_glm_text_round_trip_minimal_assertions() -> None:
    """真实 GLM 文本调用；只断言事件类型与最终 assistant，不锁死回答内容。"""
    client = GLMClient()
    try:
        messages = await run_min_loop(
            system_prompt="You are a concise test bot. Reply in one short sentence.",
            user_text="Say hello in English.",
            client=client,
        )
    finally:
        await client.close()

    # 至少有 user + assistant
    assert len(messages) >= 2
    # user 是第一条
    user_msg = messages[0]
    assert isinstance(user_msg, LLMUserMessage) or user_msg.role == "user"

    # 找到最后一条 assistant
    from pi_agent_core_py.messages import AssistantMessage
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]
    assert assistants, "缺 assistant"
    last = assistants[-1]
    # stop_reason 合理
    assert last.stop_reason in ("stop", "length", "tool_use", "aborted", "error")
    # content 非空（至少有一个 TextContent）
    text_parts = [c for c in last.content if isinstance(c, TextContent)]
    assert text_parts, "assistant.content 没有 TextContent"
    assert any(p.text.strip() for p in text_parts), "assistant 文本为空"


@pytest.mark.asyncio
async def test_real_glm_stream_yields_text_or_done_event() -> None:
    """直接调 client.stream，验证至少返回 TextDeltaEvent 或 DoneEvent。"""
    client = GLMClient()
    try:
        events: list = []
        async for ev in client.stream(
            system_prompt="You are a test bot. Reply briefly.",
            messages=[LLMUserMessage(content=[TextContent(text="Reply with the word: OK.")])],
        ):
            events.append(ev)
            if isinstance(ev, DoneEvent):
                break
    finally:
        await client.close()

    has_text = any(isinstance(e, TextDeltaEvent) for e in events)
    has_done = any(isinstance(e, DoneEvent) for e in events)
    assert has_text or has_done, (
        f"stream 既无 TextDeltaEvent 也无 DoneEvent: {events!r}"
    )

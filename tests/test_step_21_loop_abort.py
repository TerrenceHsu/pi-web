"""Step 21 — loop 处理 DoneEvent(stop_reason="aborted") 的回归测试。

覆盖：
- adapter 流出 partial text + DoneEvent(stop_reason="aborted") →
  AssistantMessage.content 为空 / stop_reason="aborted" / error_message="aborted"
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pi_agent_core_py import (
    AssistantMessage,
    DoneEvent,
    ModelClient,
    ProviderAdapter,
    ProviderRequest,
    StreamEvent,
    TextContent,
    TextDeltaEvent,
    run_event_loop,
)
from pi_agent_core_py.messages import Usage


class _PartialThenAbortAdapter(ProviderAdapter):
    """模拟：流一些 text 后，adapter 自身决定 abort（如检测到 signal set），发 aborted DoneEvent。

    不依赖外部 signal timing——adapter 总是先流 2 个 chunk，再发 aborted DoneEvent。
    这样 loop 必须根据 DoneEvent.stop_reason 判定 abort（不是根据 signal 后检）。
    """

    provider_id = "test"
    model = "test-1"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        yield TextDeltaEvent(delta="partial-0-")
        yield TextDeltaEvent(delta="partial-1-")
        yield DoneEvent(stop_reason="aborted", usage=Usage(input=1, output=2, total_tokens=3))


class _NormalStopAdapter(ProviderAdapter):
    """正常 stop——流完 3 chunk 后 DoneEvent(stop_reason="stop")。"""

    provider_id = "test"
    model = "test-1"

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        for i in range(3):
            yield TextDeltaEvent(delta=f"chunk-{i}-")
        yield DoneEvent(stop_reason="stop", usage=Usage(input=1, output=2, total_tokens=3))


@pytest.mark.asyncio
async def test_done_event_aborted_clears_partial_content() -> None:
    """adapter 发 DoneEvent(stop_reason="aborted") → assistant 内容为空。

    回归测试：Step 21 之前 loop 在 DoneEvent 分支不清 text_buf，
    导致 partial text 被保留进 aborted assistant——破坏 Step 9 abort 语义。
    """
    client = ModelClient(_PartialThenAbortAdapter())

    assistant: AssistantMessage | None = None
    async for ev in run_event_loop(
        system_prompt="x",
        user_text="hi",
        client=client,
    ):
        if hasattr(ev, "message") and isinstance(getattr(ev, "message", None), AssistantMessage):
            assistant = ev.message

    assert assistant is not None
    assert assistant.stop_reason == "aborted", (
        f"期望 aborted，实际：{assistant.stop_reason!r}"
    )
    assert assistant.error_message == "aborted"
    # content 必须为空（即使 adapter 已经流过 partial-0- / partial-1-）
    has_text = any(isinstance(c, TextContent) for c in assistant.content)
    assert not has_text, (
        f"aborted assistant 不应携带 partial text，但拿到了：{assistant.content!r}"
    )


@pytest.mark.asyncio
async def test_done_event_normal_stop_keeps_content() -> None:
    """无 abort 路径——DoneEvent(stop_reason="stop") 正常保留 text。"""
    client = ModelClient(_NormalStopAdapter())

    assistant: AssistantMessage | None = None
    async for ev in run_event_loop(
        system_prompt="x",
        user_text="hi",
        client=client,
    ):
        msg = getattr(ev, "message", None)
        if isinstance(msg, AssistantMessage):
            assistant = msg

    assert assistant is not None
    assert assistant.stop_reason == "stop"
    assert assistant.error_message is None
    text = "".join(c.text for c in assistant.content if isinstance(c, TextContent))
    assert "chunk-0-" in text
    assert "chunk-2-" in text

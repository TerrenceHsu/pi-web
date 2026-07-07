"""Smoke test 4: 用 FakeClient 跑无工具 loop。"""
from __future__ import annotations

import pytest

from pi_agent_core_py.loop import run_min_loop
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent


@pytest.mark.asyncio
async def test_fake_loop_basic_round_trip() -> None:
    """UserMessage → FakeClient → TextDelta + Done → AssistantMessage。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="hello"),
        TextDeltaEvent(delta=" world"),
        DoneEvent(stop_reason="stop"),
    ]])

    messages = await run_min_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
    )

    # 应有 user + assistant
    assert len(messages) == 2
    assert isinstance(messages[0], UserMessage)
    assert isinstance(messages[1], AssistantMessage)
    # assistant 文本是 FakeClient 拼接结果
    text = "".join(
        c.text for c in messages[1].content if isinstance(c, TextContent)
    )
    assert text == "hello world"
    assert messages[1].stop_reason == "stop"


@pytest.mark.asyncio
async def test_fake_loop_event_order() -> None:
    """事件顺序：agent_start → turn_start → user msg → assistant msg → turn_end → agent_end。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="x"),
        DoneEvent(stop_reason="stop"),
    ]])

    seen_types: list[str] = []
    async for ev in run_min_loop_events(
        system_prompt="s", user_text="hi", client=fake
    ):
        seen_types.append(ev.type)

    # 关键顺序检查
    assert seen_types[0] == "agent_start"
    assert seen_types[1] == "turn_start"
    # user msg 应在 turn_start 后
    user_msg_start_idx = seen_types.index("message_start")
    assert user_msg_start_idx > 1
    # turn_end 必须在 agent_end 之前
    assert seen_types.index("turn_end") < seen_types.index("agent_end")
    # message_end 必须在 message_start 之后
    assert seen_types.index("message_end") > seen_types.index("message_start")


@pytest.mark.asyncio
async def test_fake_loop_no_pending_tasks() -> None:
    """跑完 loop 后 async generator 能被 close——不应遗留非当前 task 的 pending task。"""
    import asyncio
    import gc

    fake = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop"),
    ]])

    await run_min_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
    )

    # 触发 async generator finalizer
    gc.collect()
    await asyncio.sleep(0.05)
    # 当前 test task 还在运行；不应有其它未完成 task
    current = asyncio.current_task()
    pending = [
        t for t in asyncio.all_tasks()
        if not t.done() and t is not current
    ]
    assert pending == [], f"遗留 pending tasks: {pending!r}"


# ---- helpers ----

from collections.abc import AsyncIterator  # noqa: E402

from pi_agent_core_py.events import AgentEvent  # noqa: E402
from pi_agent_core_py.loop import run_event_loop  # noqa: E402


async def run_min_loop_events(
    *,
    system_prompt: str,
    user_text: str,
    client: FakeClient,
) -> AsyncIterator[AgentEvent]:
    """把 run_event_loop 包装成 async generator 用于事件顺序断言。

    注意：这是 helper，不是测试。pytest 不会把它当 test 收集（不test_ 前缀）。
    """
    async for ev in run_event_loop(
        system_prompt=system_prompt,
        user_text=user_text,
        client=client,
    ):
        yield ev


# 让 pytest 不把 helper 当 test 收集
run_min_loop_events.__test__ = False  # type: ignore[attr-defined]

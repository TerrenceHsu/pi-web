"""Smoke test 5: 关键事件顺序约束。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from pi_agent_core_py.loop import run_event_loop
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent


@pytest.mark.asyncio
async def test_event_temporal_order_invariants() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="hello"),
        DoneEvent(stop_reason="stop"),
    ]])

    types: list[str] = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
    ):
        types.append(ev.type)

    # 不变量
    assert types[0] == "agent_start"
    assert types[-1] == "agent_end"
    assert types.index("turn_start") < types.index("turn_end")
    assert types.index("message_start") < types.index("message_end")


@pytest.mark.asyncio
async def test_event_metadata_json_serializable() -> None:
    """事件 model_dump(mode='json') 必须可 JSON 序列化。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="x"),
        DoneEvent(stop_reason="stop"),
    ]])

    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
    ):
        # Pydantic model_dump(mode="json")
        dumped = ev.model_dump(mode="json")
        # 必须能 JSON 序列化
        json.dumps(dumped)


@pytest.mark.asyncio
async def test_error_event_path_produces_assistant_with_error() -> None:
    """FakeClient 脚本耗尽 → ErrorEvent → loop 收敛为 error assistant。"""
    # 第一轮就耗尽脚本（空 turns 列表）
    fake = FakeClient([])

    events = []
    async for ev in run_event_loop(
        system_prompt="sys",
        user_text="hi",
        client=fake,
    ):
        events.append(ev)

    # 应该有 turn_end 和 agent_end
    assert any(e.type == "agent_end" for e in events)
    last_assistant = None
    for e in events:
        if e.type == "turn_end":
            last_assistant = e.message
    assert last_assistant is not None
    assert last_assistant.stop_reason == "error"


@pytest.mark.asyncio
async def test_run_with_events_within_single_run() -> None:
    """run_event_loop 在单次调用里完成；不应该 hang。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop"),
    ]])

    async def runner() -> list[str]:
        out: list[str] = []
        async for ev in run_event_loop(
            system_prompt="sys", user_text="hi", client=fake,
        ):
            out.append(ev.type)
        return out

    result = await asyncio.wait_for(runner(), timeout=5.0)
    assert "agent_end" in result


def _to_jsonable(obj: Any) -> Any:
    """fallback jsonable——保证测试用例不依赖 pydantic 内部 API。"""
    try:
        return json.loads(json.dumps(obj, default=lambda o: str(o)))
    except Exception:
        return str(obj)

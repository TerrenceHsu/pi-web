"""Smoke test 7: Agent + AgentHarness 主链路。"""
from __future__ import annotations

import pytest

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent


@pytest.mark.asyncio
async def test_agent_initial_state_idle() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[
            TextDeltaEvent(delta="x"),
            DoneEvent(stop_reason="stop"),
        ]]),
    )
    assert agent.state.status == "idle"
    assert agent.state.messages == []


@pytest.mark.asyncio
async def test_agent_prompt_returns_to_idle() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[
            TextDeltaEvent(delta="hello"),
            DoneEvent(stop_reason="stop"),
        ]]),
    )
    msgs = await agent.prompt("hi")
    assert len(msgs) >= 1
    assert agent.state.status == "idle"


@pytest.mark.asyncio
async def test_harness_run_prompt_basic() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="via harness"),
        DoneEvent(stop_reason="stop"),
    ]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    msgs = await harness.run_prompt("hi")
    assert len(msgs) >= 1
    # harness 产生了 snapshot
    assert harness.last_snapshot is not None
    assert harness.last_snapshot.status == "completed"


@pytest.mark.asyncio
async def test_harness_close_idempotent() -> None:
    """harness.close() 调用两次不应抛错。"""
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[
            TextDeltaEvent(delta="x"),
            DoneEvent(stop_reason="stop"),
        ]]),
    )
    harness = AgentHarness(agent)
    await harness.close()
    # 第二次不应抛错
    await harness.close()


@pytest.mark.asyncio
async def test_harness_close_after_run() -> None:
    """跑完 prompt 再 close 不应泄漏 / 报错。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="x"),
        DoneEvent(stop_reason="stop"),
    ]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    await harness.run_prompt("hi")
    await harness.close()


@pytest.mark.asyncio
async def test_agent_state_restored_to_idle_after_error() -> None:
    """FakeClient 脚本耗尽 → error path；Agent 应回到 idle，不卡在 running。"""
    fake = FakeClient([])
    agent = Agent(system_prompt="sys", client=fake)
    # 不应在 prompt 里抛——error 应该被包进 stop_reason="error"
    msgs = await agent.prompt("hi")
    assert agent.state.status == "idle"
    # 至少应该有 user + error assistant
    assert len(msgs) >= 1


@pytest.mark.asyncio
async def test_subscriber_unsubscribe_does_not_crash() -> None:
    """unsubscribe 后再 emit 不应崩。"""
    fake = FakeClient([[
        TextDeltaEvent(delta="x"),
        DoneEvent(stop_reason="stop"),
    ]])
    agent = Agent(system_prompt="sys", client=fake)
    seen: list = []
    unsub = agent.subscribe(lambda ev, state: seen.append(ev.type))
    unsub()
    await agent.prompt("hi")
    # 取消订阅后不应收到事件
    assert seen == []


@pytest.mark.asyncio
async def test_harness_phase_runs_through_lifecycle() -> None:
    fake = FakeClient([[
        TextDeltaEvent(delta="x"),
        DoneEvent(stop_reason="stop"),
    ]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    assert harness.context.phase == "idle"
    await harness.run_prompt("hi")
    assert harness.context.phase == "idle"

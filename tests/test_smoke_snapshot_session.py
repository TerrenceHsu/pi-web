"""Smoke test 8: Snapshot + Session 主链路。"""
from __future__ import annotations

import json

import pytest

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.session import (
    InMemorySessionStore,
    JsonFileSessionStore,
    SessionMemory,
    deserialize_snapshot,
)


@pytest.mark.asyncio
async def test_each_prompt_produces_snapshot(tmp_path) -> None:
    fake = FakeClient([
        [TextDeltaEvent(delta="a"), DoneEvent(stop_reason="stop")],
        [TextDeltaEvent(delta="b"), DoneEvent(stop_reason="stop")],
    ])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)

    await harness.run_prompt("first")
    snap1 = harness.last_snapshot
    assert snap1 is not None
    assert snap1.status == "completed"
    assert len(snap1.messages_before) == 0
    assert len(snap1.messages_after) >= 1

    await harness.run_prompt("second")
    snap2 = harness.last_snapshot
    assert snap2 is not None
    assert len(snap2.messages_before) >= 1


@pytest.mark.asyncio
async def test_snapshot_messages_after_is_copy_not_reference() -> None:
    """snapshot.messages_after 不应是 agent.state.messages 的可变引用。

    后续 turn 修改 agent.state.messages 不应污染旧 snapshot。
    """
    fake = FakeClient([
        [TextDeltaEvent(delta="first"), DoneEvent(stop_reason="stop")],
        [TextDeltaEvent(delta="second"), DoneEvent(stop_reason="stop")],
    ])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)

    await harness.run_prompt("first")
    snap1 = harness.last_snapshot
    assert snap1 is not None
    snap1_count = len(snap1.messages_after)

    await harness.run_prompt("second")

    # 旧 snapshot 不应被影响
    assert len(snap1.messages_after) == snap1_count


@pytest.mark.asyncio
async def test_snapshot_metadata_json_serializable() -> None:
    fake = FakeClient([[TextDeltaEvent(delta="x"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    await harness.run_prompt("hi")

    snap = harness.last_snapshot
    assert snap is not None
    # to_dict 必须可 JSON 序列化
    data = snap.to_dict()
    json.dumps(data)


@pytest.mark.asyncio
async def test_session_append_and_get_messages() -> None:
    """session.append_snapshot 后 get_messages 应返回反序列化的 Message。"""
    fake = FakeClient([[TextDeltaEvent(delta="hi"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="t1", title="T")
    harness.attach_session(session)
    await harness.run_prompt("hello")

    msgs = session.get_messages()
    # 类型完整：不应退化成 dict
    for m in msgs:
        assert isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))


@pytest.mark.asyncio
async def test_in_memory_store_save_load_round_trip() -> None:
    store = InMemorySessionStore()
    s = SessionMemory(session_id="x", title="X", metadata={"k": "v"})
    await store.save(s)
    loaded = await store.load("x")
    assert loaded.id == "x"
    assert loaded.state.title == "X"
    assert loaded.state.metadata["k"] == "v"


@pytest.mark.asyncio
async def test_json_file_store_load_corrupt_json_raises_or_recovers(tmp_path) -> None:
    """JSONL 半损坏时应该明确报错（FileNotFoundError / ValueError / JSONDecodeError）。"""
    store = JsonFileSessionStore(tmp_path)
    s = SessionMemory(session_id="ok", title="ok")
    await store.save(s)
    # 破坏文件
    (tmp_path / "ok.json").write_text("{not valid json", encoding="utf-8")
    # 任何具体的 Exception 子类都可接受——只验证不静默失败
    with pytest.raises((ValueError, json.JSONDecodeError, OSError)):
        await store.load("ok")


@pytest.mark.asyncio
async def test_session_serialize_deserialize_preserves_types(tmp_path) -> None:
    """session JSON 往返后 message 类型不退化成 dict。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    harness.attach_session(SessionMemory(session_id="x"))
    await harness.run_prompt("hi")

    session = harness.session
    assert session is not None
    text = session.to_json()
    restored = SessionMemory.from_json(text)
    msgs = restored.get_messages()
    for m in msgs:
        assert isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))


@pytest.mark.asyncio
async def test_deserialize_snapshot_returns_turnsnapshot() -> None:
    fake = FakeClient([[TextDeltaEvent(delta="x"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=fake)
    harness = AgentHarness(agent)
    await harness.run_prompt("hi")

    snap = harness.last_snapshot
    assert snap is not None
    data = snap.to_dict()
    restored = deserialize_snapshot(data)
    assert restored.id == snap.id
    assert restored.status == snap.status


@pytest.mark.asyncio
async def test_in_memory_store_save_is_snapshot(tmp_path) -> None:
    """InMemorySessionStore.save 应深拷贝——后续修改 session 不污染 store。"""
    store = InMemorySessionStore()
    s = SessionMemory(session_id="x")
    await store.save(s)
    s.set_title("changed")
    loaded = await store.load("x")
    # store 中的快照标题不应被改
    assert loaded.state.title is None


@pytest.mark.asyncio
async def test_json_file_store_rejects_path_traversal(tmp_path) -> None:
    store = JsonFileSessionStore(tmp_path)
    with pytest.raises(ValueError):
        await store.save(SessionMemory(session_id="../../etc/passwd"))
    with pytest.raises(ValueError):
        await store.load("../hidden")
    with pytest.raises(ValueError):
        await store.exists("../../secret")

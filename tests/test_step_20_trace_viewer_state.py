"""Step 20 — TraceEventBuffer / WebAppState 单元测试。

覆盖：
- TraceEventBuffer.append / list / clear
- max_size 上限，超过自动丢最旧
- 非 dict 输入静默跳过
- max_size <= 0 抛 ValueError
- WebAppState 默认字段（harness / event_buffer / event_queue / running）
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py import Agent, AgentHarness, FakeClient
from pi_agent_core_py.web.state import TraceEventBuffer, WebAppState

# ============================================================================
# TraceEventBuffer
# ============================================================================


def test_buffer_append_and_list() -> None:
    buf = TraceEventBuffer(max_size=10)
    buf.append({"type": "agent_start"})
    buf.append({"type": "turn_start"})
    out = buf.list()
    assert len(out) == 2
    assert out[0]["type"] == "agent_start"
    assert out[1]["type"] == "turn_start"


def test_buffer_clear() -> None:
    buf = TraceEventBuffer()
    buf.append({"type": "x"})
    assert len(buf) == 1
    buf.clear()
    assert len(buf) == 0
    assert buf.list() == []


def test_buffer_drops_oldest_when_full() -> None:
    buf = TraceEventBuffer(max_size=3)
    for i in range(5):
        buf.append({"i": i})
    out = buf.list()
    # deque(maxlen=3) 保留最后 3 个
    assert len(out) == 3
    assert [e["i"] for e in out] == [2, 3, 4]


def test_buffer_ignores_non_dict() -> None:
    buf = TraceEventBuffer()
    buf.append("not-a-dict")  # type: ignore
    buf.append(None)  # type: ignore
    buf.append(["list", "not", "dict"])  # type: ignore
    assert len(buf) == 0


def test_buffer_list_returns_shallow_copy() -> None:
    """list() 返回 list 浅拷贝——增删元素不影响 buffer。

    注意：list() 不深拷贝 dict（性能考虑）—— mutate 单个 dict 字段会反映
    到 buffer。这是有意的；调用方需要隔离请自行 deepcopy。
    """
    buf = TraceEventBuffer()
    buf.append({"type": "x"})
    out = buf.list()
    # mutate list（增删元素）不影响 buffer
    out.append({"type": "y"})
    out.clear()
    assert len(buf) == 1
    assert buf.list()[0]["type"] == "x"


def test_buffer_invalid_max_size_raises() -> None:
    with pytest.raises(ValueError):
        TraceEventBuffer(max_size=0)
    with pytest.raises(ValueError):
        TraceEventBuffer(max_size=-1)


# ============================================================================
# WebAppState
# ============================================================================


def _make_harness() -> AgentHarness:
    agent = Agent(system_prompt="x", client=FakeClient([]), tools=None)
    return AgentHarness(agent)


def test_state_defaults() -> None:
    h = _make_harness()
    state = WebAppState(harness=h)
    assert state.harness is h
    assert isinstance(state.event_buffer, TraceEventBuffer)
    assert state.running is False
    assert state.last_error is None
    # event_queue 是 asyncio.Queue
    assert isinstance(state.event_queue, asyncio.Queue)


def test_state_running_and_last_error_mutable() -> None:
    h = _make_harness()
    state = WebAppState(harness=h)
    state.running = True
    state.last_error = "boom"
    assert state.running is True
    assert state.last_error == "boom"

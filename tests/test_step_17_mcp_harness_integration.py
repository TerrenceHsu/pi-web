"""Step 17 — Harness run_prompt + MCP 端到端集成测试。

覆盖：
- attach MCP 后 run_prompt 能执行 MCP tool
- MCP tool result 能进入 ToolResultMessage
- LLM 第二轮看到 ToolResultMessage
- Snapshot metadata 含 MCP servers/tools 状态
- Session snapshot 能看到 MCP metadata
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    InMemorySessionStore,
    MCPClient,
    MCPRegistry,
    MCPServerConfig,
    SessionMemory,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from pi_agent_core_py.mcp import FakeMCPTransport


def _echo_handler():
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": [{
                    "name": "echo",
                    "description": "Echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }]},
            }
        if method == "tools/call":
            params = msg.get("params", {})
            text = (params.get("arguments") or {}).get("text", "")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": f"echoed: {text}"}]},
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    return handler


def _attach_echo(harness: AgentHarness) -> None:
    """attach 一个 fake echo MCP server 到 harness。"""
    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))

    # async helper——通过 attach_mcp_servers 直接 await
    async def _do():
        await harness.attach_mcp_servers([cfg], registry=registry)
    import asyncio
    asyncio.get_event_loop().run_until_complete(_do()) if False else None
    # 上一行不能跑——这里改用 pytest 期望 caller 在 async test 中 await
    harness.__pending_attach = (cfg, registry)  # type: ignore[attr-defined]


async def _await_attach(harness: AgentHarness) -> None:
    cfg, registry = harness.__pending_attach  # type: ignore[attr-defined]
    await harness.attach_mcp_servers([cfg], registry=registry)


# ============================================================================
# run_prompt 端到端
# ============================================================================


@pytest.mark.asyncio
async def test_run_prompt_executes_mcp_tool_end_to_end() -> None:
    """完整流程：attach → run_prompt → loop 调 MCP echo → 回喂 → 最终回答。"""
    fake_llm = FakeClient([
        [   # turn 1: LLM 决定调 mcp__srv__echo
            ToolCallEvent(tool_call=ToolCall(
                id="c1", name="mcp__srv__echo", arguments={"text": "hello"},
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [   # turn 2: 基于工具结果给最终文本
            TextDeltaEvent(delta="final answer"),
            DoneEvent(stop_reason="stop", usage=Usage()),
        ],
    ])
    agent = Agent(system_prompt="x", client=fake_llm)
    harness = AgentHarness(agent)

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)
    assert harness.agent.tools.has("mcp__srv__echo")

    msgs = await harness.run_prompt("echo hello")

    # 至少含 user + assistant(toolcall) + toolResult + assistant(final)
    roles = [m.role for m in msgs]
    assert "user" in roles
    assert "toolResult" in roles
    assert roles.count("assistant") >= 2

    # 找到 ToolResultMessage，验证 MCP 调用结果
    from pi_agent_core_py import ToolResultMessage
    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.name == "mcp__srv__echo"
    assert tr.content[0].text == "echoed: hello"
    assert tr.details["source"] == "mcp"
    assert tr.details["server"] == "srv"
    assert tr.details["mcp_tool"] == "echo"

    # 第二轮 LLM 收到了 ToolResultMessage
    assert len(fake_llm.all_messages_calls) == 2
    from pi_agent_core_py import LLMToolResultMessage
    second_call = fake_llm.all_messages_calls[1]
    tool_msgs = [m for m in second_call if isinstance(m, LLMToolResultMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].name == "mcp__srv__echo"

    await harness.close()


@pytest.mark.asyncio
async def test_snapshot_metadata_contains_mcp_servers_and_tools() -> None:
    """Snapshot.metadata 含 MCP servers / tools 快照。"""
    fake_llm = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    agent = Agent(system_prompt="x", client=fake_llm)
    harness = AgentHarness(agent)

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)

    await harness.run_prompt("hi")

    snapshot = harness.last_snapshot
    assert snapshot is not None
    snap_meta = snapshot.metadata.get("mcp")
    assert snap_meta is not None
    assert snap_meta["attached"] is True
    assert len(snap_meta["servers"]) == 1
    assert snap_meta["servers"][0]["name"] == "srv"
    assert snap_meta["servers"][0]["connected"] is True
    assert len(snap_meta["tools"]) == 1
    assert snap_meta["tools"][0]["name"] == "mcp__srv__echo"

    await harness.close()


@pytest.mark.asyncio
async def test_session_snapshot_records_mcp_metadata() -> None:
    """Session snapshots 中能看到 MCP metadata。"""
    fake_llm = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    agent = Agent(system_prompt="x", client=fake_llm)
    harness = AgentHarness(agent)

    # attach session（让 snapshot 自动写入）
    session = SessionMemory()
    harness.attach_session(session)

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)

    await harness.run_prompt("hi")

    # session 中至少有 1 个 snapshot
    snapshots = session.get_snapshots()
    assert len(snapshots) >= 1
    last_snap = snapshots[-1]
    snap_meta = last_snap.metadata.get("mcp")
    assert snap_meta is not None
    assert snap_meta["attached"] is True
    assert len(snap_meta["servers"]) == 1

    # session.metadata["harness"] 也含 MCP 状态快照（通过 sync_session_metadata）
    harness_meta = session.state.metadata.get("harness", {})
    ctx_meta = harness_meta.get("context_metadata", {})
    assert "mcp" in ctx_meta

    await harness.close()


@pytest.mark.asyncio
async def test_session_save_load_preserves_mcp_metadata_via_snapshot() -> None:
    """session 存盘 → 加载后 snapshot 中的 MCP metadata 仍在。"""
    fake_llm = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    agent = Agent(system_prompt="x", client=fake_llm)
    harness = AgentHarness(agent)
    harness.configure_session_sync(store=InMemorySessionStore(), auto_save_policy="after_snapshot")
    session = SessionMemory(session_id="test-mcp-1")
    harness.attach_session(session)

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)

    await harness.run_prompt("hi")

    # 从 store 加载——load 返回新 SessionMemory，snapshots 从 state.snapshots 反序列化
    loaded = await harness.session_store.load("test-mcp-1")  # type: ignore[union-attr]
    # state.snapshots 是 dict 列表（序列化存储）；从 state 取
    loaded_snaps = loaded.state.snapshots
    assert len(loaded_snaps) >= 1
    last_snap_dict = loaded_snaps[-1]
    snap_meta = last_snap_dict.get("metadata", {}).get("mcp")
    assert snap_meta is not None
    assert snap_meta["attached"] is True

    await harness.close()


@pytest.mark.asyncio
async def test_mcp_tool_definition_passed_to_llm() -> None:
    """attach 后 FakeClient.last_tools 能看到 MCP tool definition。"""
    fake_llm = FakeClient([[
        TextDeltaEvent(delta="ok"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    agent = Agent(system_prompt="x", client=fake_llm)
    harness = AgentHarness(agent)

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler()))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)

    await harness.run_prompt("hi")

    assert fake_llm.last_tools is not None
    names = [t.name for t in fake_llm.last_tools]
    assert "mcp__srv__echo" in names

    await harness.close()

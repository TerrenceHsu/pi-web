"""Step 17 — Harness MCP lifecycle 边界测试。

覆盖：
- harness.close() 是 async + 自动 detach
- close 幂等
- close 后 MCP transports closed
- 单 server refresh 失败时 state.connected=False
- refresh 后 stale tools 不残留
- MCP metadata 在生成异常时不破坏主请求
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    FakeClient,
    MCPClient,
    MCPRegistry,
    MCPServerConfig,
)
from pi_agent_core_py.mcp import FakeMCPTransport


def _echo_handler(server_name: str = "srv"):
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
                    "description": f"echo from {server_name}",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                }]},
            }
        if method == "tools/call":
            params = msg.get("params", {})
            text = (params.get("arguments") or {}).get("text", "")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": f"{server_name}: {text}"}]},
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    return handler


async def _do_attach(harness: AgentHarness, server_names: list[str]) -> dict[str, FakeMCPTransport]:
    """实际 await attach 并返回 transports。"""
    transports: dict[str, FakeMCPTransport] = {}

    def factory(cfg: MCPServerConfig) -> MCPClient:
        t = FakeMCPTransport(handler=_echo_handler(cfg.name))
        transports[cfg.name] = t
        return MCPClient(cfg, transport=t)

    cfgs = [MCPServerConfig(name=n, transport="stdio", command="x") for n in server_names]
    registry = MCPRegistry(cfgs, client_factory=factory)
    await harness.attach_mcp_servers(cfgs, registry=registry)
    return transports


# ============================================================================
# close
# ============================================================================


@pytest.mark.asyncio
async def test_harness_close_is_async() -> None:
    """close() 是 async——MCP transports 必须 await close。"""
    import inspect
    assert inspect.iscoroutinefunction(AgentHarness.close)


@pytest.mark.asyncio
async def test_close_detaches_mcp_servers() -> None:
    """close() 自动 detach——MCP tools 从 ToolRegistry 移除。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    await _do_attach(harness, ["srv1"])
    assert harness.agent.tools.has("mcp__srv1__echo")

    await harness.close()
    assert not harness.agent.tools.has("mcp__srv1__echo")


@pytest.mark.asyncio
async def test_close_full_lifecycle() -> None:
    """完整生命周期：attach → close → MCP tools 移除 + transports closed。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    transports = await _do_attach(harness, ["srv1", "srv2"])

    assert harness.agent.tools.has("mcp__srv1__echo")
    assert harness.agent.tools.has("mcp__srv2__echo")

    await harness.close()

    # MCP tools 全部移除
    assert not harness.agent.tools.has("mcp__srv1__echo")
    assert not harness.agent.tools.has("mcp__srv2__echo")
    # registry 字段为空
    assert harness.mcp_registry is None
    # transports 全部 closed
    for t in transports.values():
        assert t._closed is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """close() 多次调用不抛。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    await _do_attach(harness, ["srv1"])
    await harness.close()
    await harness.close()
    await harness.close()


@pytest.mark.asyncio
async def test_close_without_mcp_attached() -> None:
    """未 attach MCP 时 close 也工作（unsubscribe Agent）。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    await harness.close()
    await harness.close()


# ============================================================================
# refresh 失败隔离
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_failure_marks_server_disconnected() -> None:
    """refresh 中某个 server list_tools 失败 → state.connected=False。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    await _do_attach(harness, ["srv1", "srv2"])

    # 破坏 srv1：让 client.list_tools 抛
    srv1_client = harness.mcp_registry._clients["srv1"]  # noqa: SLF001
    async def boom(): raise RuntimeError("srv1 list_tools boom")
    srv1_client.list_tools = boom  # type: ignore[method-assign]

    await harness.refresh_mcp_tools()

    servers = {s.name: s for s in harness.list_mcp_servers()}
    assert servers["srv1"].connected is False
    assert "RuntimeError" in (servers["srv1"].last_error or "")
    # srv2 仍然健康
    assert servers["srv2"].connected is True
    # srv1 工具被移除（_mcp_tool_names 还记得，refresh 时 unregister）
    assert not harness.agent.tools.has("mcp__srv1__echo")
    # srv2 工具仍在
    assert harness.agent.tools.has("mcp__srv2__echo")

    await harness.close()


@pytest.mark.asyncio
async def test_refresh_no_stale_tools_left() -> None:
    """refresh 后 ToolRegistry 中没有 stale 工具。"""
    agent = Agent(system_prompt="x", client=FakeClient([]))
    harness = AgentHarness(agent)
    await _do_attach(harness, ["srv1"])

    # 第一次 refresh
    await harness.refresh_mcp_tools()
    tools_after = [t.name for t in harness.agent.tools.list() if t.name.startswith("mcp__")]
    assert tools_after == ["mcp__srv1__echo"]

    # 第二次 refresh——不应该重复注册
    await harness.refresh_mcp_tools()
    tools_after2 = [t.name for t in harness.agent.tools.list() if t.name.startswith("mcp__")]
    assert tools_after2 == ["mcp__srv1__echo"]

    await harness.close()


# ============================================================================
# metadata 错误隔离
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_metadata_failure_does_not_crash_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_mcp_metadata 抛错时，写入 metadata_error 而不是破坏主请求。"""
    from pi_agent_core_py import (
        DoneEvent,
        TextDeltaEvent,
        Usage,
    )

    agent = Agent(
        system_prompt="x",
        client=FakeClient([[
            TextDeltaEvent(delta="hello"),
            DoneEvent(stop_reason="stop", usage=Usage()),
        ]]),
    )
    harness = AgentHarness(agent)

    # attach 一个 server
    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    registry = MCPRegistry([cfg], client_factory=(
        lambda c: MCPClient(c, transport=FakeMCPTransport(handler=_echo_handler("srv1")))
    ))
    await harness.attach_mcp_servers([cfg], registry=registry)

    # monkey-patch _build_mcp_metadata 让它抛
    def boom():
        raise RuntimeError("metadata build boom")
    monkeypatch.setattr(harness, "_build_mcp_metadata", boom)

    # run_prompt 不应崩
    msgs = await harness.run_prompt("hi")
    assert len(msgs) >= 1

    # metadata_error 进了 context
    mcp_meta = harness.context.metadata.get("mcp")
    assert mcp_meta is not None
    assert "metadata_error" in mcp_meta
    assert "metadata build boom" in mcp_meta["metadata_error"]

    await harness.close()

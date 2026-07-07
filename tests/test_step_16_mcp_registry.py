"""Step 16 — MCPRegistry 单元测试。

覆盖：
- 多 server connect_all
- 单 server 失败不影响其它 server
- to_agent_tools 返回所有可用工具
- 同名 MCP tool 在不同 server 下 namespaced 后不冲突
- close_all 幂等
- list_servers / list_tools
- refresh_tools 更新 tool_count
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    MCPRegistry,
    MCPServerConfig,
    MCPToolInfo,
)

# ============================================================================
# Handler / config 工厂
# ============================================================================


def _make_handler(
    *,
    tools: list[dict[str, Any]] | None = None,
    raise_on_method: str | None = None,
) -> Any:
    """构造简单 FakeMCPTransport handler。"""
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        if raise_on_method == method:
            # 模拟 receive 抛异常（更直接的方式是 FakeMCPTransport 的 raise_on_receive，
            # 但这里我们让 handler 抛异常也能验证 send 的 stash 机制）
            raise RuntimeError(f"simulated failure for {method}")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools or []}}
        if method == "tools/call":
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    return handler


def _inject_fake_transports(registry: MCPRegistry, handlers: dict[str, Any]) -> None:
    """把 registry 里的 stdio client 替换成 FakeMCPTransport。

    在 connect_all() 之前调用：替换每个 client 的 transport，再 reset client。
    """
    from pi_agent_core_py import MCPClient
    from pi_agent_core_py.mcp import FakeMCPTransport

    # 直接为每个 config 创建一个带 FakeMCPTransport 的 client，注入 registry
    new_clients: dict[str, MCPClient] = {}
    for cfg in registry._configs:  # noqa: SLF001
        if cfg.name in handlers:
            transport = FakeMCPTransport(handler=handlers[cfg.name])
            new_clients[cfg.name] = MCPClient(cfg, transport=transport)

    # connect_all 会重新构造 client——我们绕过它，手动注入
    # 这里改为：先 _configs 替换 transport 工厂不现实，所以测试时
    # 用 monkeypatch _build_default_transport
    # —— 简化：测试时直接用 MCPClient 单测，registry 集成测用下面的方案


# ============================================================================
# 用 monkeypatch 注入 fake transport
# ============================================================================


@pytest.fixture
def patched_registry_transport(monkeypatch: pytest.MonkeyPatch):
    """提供 patch_mcp_default_transport(registry, handler_map) 工厂。

    用法：
        patched_registry_transport(
            registry,
            {"srv1": handler1, "srv2": handler2},
        )
    """
    from pi_agent_core_py.mcp import FakeMCPTransport
    from pi_agent_core_py.mcp import client as client_mod

    def patch(registry: MCPRegistry, handlers: dict[str, Any]) -> None:
        # 替换 _build_default_transport，让它按 config.name 返回 fake
        original = client_mod._build_default_transport

        def fake_builder(cfg):
            if cfg.name in handlers:
                return FakeMCPTransport(handler=handlers[cfg.name])
            return original(cfg)

        monkeypatch.setattr(client_mod, "_build_default_transport", fake_builder)
        # MCPClient import 自 client_mod，但 _build_default_transport 在
        # MCPClient.__init__ 里按名查找——直接 patch client_mod 命名空间即可
        import pi_agent_core_py.mcp as mcp_pkg
        monkeypatch.setattr(
            mcp_pkg, "_build_default_transport", fake_builder, raising=False,
        )

    return patch


# ============================================================================
# 1. connect_all
# ============================================================================


@pytest.mark.asyncio
async def test_connect_all_succeeds(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
        MCPServerConfig(name="srv2", transport="stdio", command="y"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "a", "description": "d"}]),
        "srv2": _make_handler(tools=[{"name": "b", "description": "d"}]),
    })

    await registry.connect_all()
    states = {s.name: s for s in registry.list_servers()}
    assert states["srv1"].connected is True
    assert states["srv2"].connected is True
    assert states["srv1"].last_error is None
    await registry.close_all()


@pytest.mark.asyncio
async def test_connect_all_one_failure_does_not_affect_others(
    patched_registry_transport,
) -> None:
    """srv1 连接失败（raise_on_connect），srv2 应该照常连接。"""
    from pi_agent_core_py.mcp import FakeMCPTransport

    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
        MCPServerConfig(name="srv2", transport="stdio", command="y"),
    ])

    # 直接构造 fake transport——srv1 在 connect 阶段抛
    fake1 = FakeMCPTransport(raise_on_connect=ConnectionError("simulated connect fail"))
    fake2 = FakeMCPTransport(handler=_make_handler(tools=[{"name": "b", "description": "d"}]))

    # patch _build_default_transport：让 MCPClient(cfg) 内部按 cfg.name 返回 fake
    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        if cfg.name == "srv1":
            return fake1
        return fake2

    cm._build_default_transport = fake_builder
    try:
        await registry.connect_all()
    finally:
        cm._build_default_transport = original

    states = {s.name: s for s in registry.list_servers()}
    assert states["srv1"].connected is False
    assert "simulated connect fail" in (states["srv1"].last_error or "")
    assert states["srv2"].connected is True
    await registry.close_all()


# ============================================================================
# 2. tools
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_tools_returns_all_tools(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
        MCPServerConfig(name="srv2", transport="stdio", command="y"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "a", "description": "d"}]),
        "srv2": _make_handler(tools=[
            {"name": "b", "description": "d"},
            {"name": "c", "description": "d"},
        ]),
    })

    await registry.connect_all()
    tools = await registry.refresh_tools()

    names = sorted(t.name for t in tools)
    assert names == ["mcp__srv1__a", "mcp__srv2__b", "mcp__srv2__c"]

    states = {s.name: s for s in registry.list_servers()}
    assert states["srv1"].tool_count == 1
    assert states["srv2"].tool_count == 2
    await registry.close_all()


@pytest.mark.asyncio
async def test_to_agent_tools_triggers_refresh_on_first_call(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "a", "description": "d"}]),
    })
    await registry.connect_all()

    tools = await registry.to_agent_tools()
    assert len(tools) == 1
    assert tools[0].name == "mcp__srv1__a"

    # 第二次调用走缓存
    tools2 = await registry.to_agent_tools()
    assert len(tools2) == 1
    await registry.close_all()


@pytest.mark.asyncio
async def test_same_tool_name_different_servers_no_conflict(
    patched_registry_transport,
) -> None:
    """srv1 和 srv2 都有 'echo' → namespace 后两个工具都注册。"""
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
        MCPServerConfig(name="srv2", transport="stdio", command="y"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "echo", "description": "d"}]),
        "srv2": _make_handler(tools=[{"name": "echo", "description": "d"}]),
    })
    await registry.connect_all()
    tools = await registry.refresh_tools()
    names = sorted(t.name for t in tools)
    assert names == ["mcp__srv1__echo", "mcp__srv2__echo"]
    await registry.close_all()


@pytest.mark.asyncio
async def test_refresh_tools_one_failure_does_not_affect_others(
    patched_registry_transport,
) -> None:
    """srv1 的 tools/list 失败，srv2 仍然能拿到 tools。"""
    from pi_agent_core_py.mcp import FakeMCPTransport

    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
        MCPServerConfig(name="srv2", transport="stdio", command="y"),
    ])

    fake1 = FakeMCPTransport(
        raise_on_receive=lambda: RuntimeError("simulated list fail"),
    )
    fake2 = FakeMCPTransport(handler=_make_handler(tools=[{"name": "b", "description": "d"}]))

    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        if cfg.name == "srv1":
            return fake1
        return fake2

    cm._build_default_transport = fake_builder
    try:
        await registry.connect_all()
        tools = await registry.refresh_tools()
    finally:
        cm._build_default_transport = original

    # srv2 的工具仍然拿到了
    assert any(t.name == "mcp__srv2__b" for t in tools)
    states = {s.name: s for s in registry.list_servers()}
    assert states["srv1"].tool_count == 0
    assert states["srv1"].last_error is not None
    assert states["srv2"].tool_count == 1
    await registry.close_all()


# ============================================================================
# 3. close / state
# ============================================================================


@pytest.mark.asyncio
async def test_close_all_idempotent(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(),
    })
    await registry.connect_all()
    await registry.close_all()
    await registry.close_all()  # 不抛
    await registry.close_all()  # 不抛


@pytest.mark.asyncio
async def test_close_all_resets_connected_state(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "a", "description": "d"}]),
    })
    await registry.connect_all()
    await registry.refresh_tools()
    await registry.close_all()
    state = registry.list_servers()[0]
    assert state.connected is False
    assert state.tool_count == 0


@pytest.mark.asyncio
async def test_list_tools_returns_cached_infos(
    patched_registry_transport,
) -> None:
    registry = MCPRegistry([
        MCPServerConfig(name="srv1", transport="stdio", command="x"),
    ])
    patched_registry_transport(registry, {
        "srv1": _make_handler(tools=[{"name": "a", "description": "d"}]),
    })
    await registry.connect_all()
    await registry.refresh_tools()

    infos = registry.list_tools()
    assert len(infos) == 1
    assert isinstance(infos[0], MCPToolInfo)
    assert infos[0].name == "a"
    await registry.close_all()

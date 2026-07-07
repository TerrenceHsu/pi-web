"""Step 16 — MCPAgentTool 单元测试。

覆盖：
- MCPToolInfo 能包装成 MCPAgentTool
- namespaced name 正确（mcp__{server}__{tool}）
- label 使用原 MCP tool name
- parameters 等于 input_schema
- description fallback（缺时用 "MCP tool X from Y"）
- execute 成功返回 ToolResult
- execute 失败返回 ToolResult(is_error=True)
- is_error=true 的 MCPCallResult 也走 is_error=True 路径
- details 包含 source/server/mcp_tool
- details 含 error_type / error（错误时）
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    MCPAgentTool,
    MCPClient,
    MCPServerConfig,
    MCPToolInfo,
    ToolResult,
)
from pi_agent_core_py.mcp import FakeMCPTransport


def _make_client_and_tool_info(
    *,
    tool_name: str = "echo",
    description: str = "Echo tool",
    schema: dict[str, Any] | None = None,
) -> tuple[MCPClient, MCPToolInfo]:
    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    fake = FakeMCPTransport(handler=lambda m: {"jsonrpc": "2.0", "id": m["id"], "result": {}})
    client = MCPClient(cfg, transport=fake)
    info = MCPToolInfo(
        name=tool_name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}},
    )
    return client, info


# ============================================================================
# 1. 包装 / 字段映射
# ============================================================================


def test_namespace_name_format() -> None:
    client, info = _make_client_and_tool_info(tool_name="echo")
    tool = MCPAgentTool(
        server_name="srv1", client=client, tool_info=info,
    )
    assert tool.name == "mcp__srv1__echo"


def test_label_uses_original_tool_name() -> None:
    client, info = _make_client_and_tool_info(tool_name="my_tool")
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)
    assert tool.label == "my_tool"


def test_parameters_equal_input_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    client, info = _make_client_and_tool_info(schema=schema)
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)
    assert tool.parameters == schema


def test_description_fallback_when_empty() -> None:
    client, info = _make_client_and_tool_info(description="")
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)
    assert tool.description == "MCP tool echo from srv1"


def test_description_keeps_nonempty() -> None:
    client, info = _make_client_and_tool_info(description="Echo input text")
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)
    assert tool.description == "Echo input text"


def test_definition_passes_registry_validation() -> None:
    """AgentTool.definition 在 ToolRegistry.register 时被调用，必须通过校验。"""
    from pi_agent_core_py import ToolRegistry
    client, info = _make_client_and_tool_info()
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)
    reg = ToolRegistry([tool])
    assert reg.has("mcp__srv1__echo")
    defn = reg.definitions()[0]
    assert defn.name == "mcp__srv1__echo"
    assert defn.label == "echo"


def test_same_tool_name_different_servers_no_conflict() -> None:
    """两个 server 都有 'echo' 工具 → namespace 后不冲突。"""
    from pi_agent_core_py import ToolRegistry
    cfg1 = MCPServerConfig(name="srv1", transport="stdio", command="x")
    cfg2 = MCPServerConfig(name="srv2", transport="stdio", command="x")
    fake1 = FakeMCPTransport()
    fake2 = FakeMCPTransport()
    c1 = MCPClient(cfg1, transport=fake1)
    c2 = MCPClient(cfg2, transport=fake2)
    info = MCPToolInfo(name="echo", description="d")
    t1 = MCPAgentTool(server_name="srv1", client=c1, tool_info=info)
    t2 = MCPAgentTool(server_name="srv2", client=c2, tool_info=info)
    reg = ToolRegistry([t1, t2])
    assert reg.has("mcp__srv1__echo")
    assert reg.has("mcp__srv2__echo")


# ============================================================================
# 2. execute 成功路径
# ============================================================================


@pytest.mark.asyncio
async def test_execute_success_returns_tool_result() -> None:
    """handler 返回正常 text content → ToolResult(is_error=False)。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "echoed: hi"}],
                    "isError": False,
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    info = MCPToolInfo(name="echo", description="d")
    tool = MCPAgentTool(server_name="srv1", client=client, tool_info=info)

    await client.connect()
    result = await tool.execute("call_1", {"text": "hi"})

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.content[0].text == "echoed: hi"
    assert result.details["source"] == "mcp"
    assert result.details["server"] == "srv1"
    assert result.details["mcp_tool"] == "echo"
    # 调用时用的是原始 tool name（不是 namespaced）
    sent_req = client.transport.sent[-1]
    assert sent_req["params"]["name"] == "echo"
    assert sent_req["params"]["arguments"] == {"text": "hi"}
    await client.close()


@pytest.mark.asyncio
async def test_execute_server_iserror_returns_error_tool_result() -> None:
    """server 返回 isError=true → ToolResult(is_error=True)。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv1",
        client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("call_1", {})
    assert result.is_error is True
    assert result.details["source"] == "mcp"
    assert result.details["server"] == "srv1"
    assert result.details["error_type"] == "MCPToolCallError"
    await client.close()


# ============================================================================
# 3. execute 失败路径（异常被捕获）
# ============================================================================


@pytest.mark.asyncio
async def test_execute_protocol_error_returns_error_tool_result() -> None:
    """tools/call 抛 MCPProtocolError → adapter 捕获并转 is_error=True ToolResult。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": "internal"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv1",
        client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("call_1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "MCPProtocolError"
    assert "internal" in result.details["error"]
    assert "MCPProtocolError" in result.content[0].text
    await client.close()


@pytest.mark.asyncio
async def test_execute_tool_not_found_returns_error_tool_result() -> None:
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32602, "message": "tool not found: t"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv1",
        client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("call_1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "MCPToolNotFoundError"
    await client.close()


@pytest.mark.asyncio
async def test_execute_transport_closed_returns_error_tool_result() -> None:
    """connect 成功但 receive 时 transport 异常 → adapter 兜底转 is_error=True。"""
    cfg = MCPServerConfig(name="srv1", transport="stdio", command="x")
    fake = FakeMCPTransport(
        raise_on_receive=lambda: ConnectionError("transport closed mid-call"),
    )
    client = MCPClient(cfg, transport=fake)
    tool = MCPAgentTool(
        server_name="srv1",
        client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("call_1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "ConnectionError"
    await client.close()

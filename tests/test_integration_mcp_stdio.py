"""Integration test 4: 真实 MCP stdio smoke。

启动一个 fake MCP server 子进程（tests/fixtures/fake_mcp_stdio_server.py），
验证：
- MCPClient.initialize() 成功
- list_tools() 返回至少一个工具
- 注册到 ToolRegistry 后调用 call_tool 返回 ToolResult
- transport close 后没有残留进程
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from pi_agent_core_py.mcp import (
    MCPClient,
    MCPServerConfig,
    MCPToolCallError,
    MCPToolNotFoundError,
)
from pi_agent_core_py.mcp.adapter import MCPAgentTool
from pi_agent_core_py.mcp.errors import MCPProtocolError
from pi_agent_core_py.mcp.transport import StdioMCPTransport
from pi_agent_core_py.tools import ToolRegistry

pytestmark = [pytest.mark.slow]

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"


def _server_command() -> list[str]:
    """构造启动 fake MCP server 的命令。"""
    return [sys.executable, str(_FIXTURE)]


@pytest.mark.asyncio
async def test_mcp_stdio_initialize_and_list_tools():
    """stdio transport + MCPClient.initialize + list_tools 全链路。"""
    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    client = MCPClient(config)
    try:
        await client.connect()
        result = await client.initialize()
        assert isinstance(result, dict)
        # serverInfo 至少存在
        assert "serverInfo" in result or "capabilities" in result

        tools = await client.list_tools()
        assert tools, "list_tools 返回空"
        # 至少有 echo
        names = [t.name for t in tools]
        assert "echo" in names
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_stdio_call_tool_returns_text():
    """call_tool 通过 MCP 协议返回 echo 文本。"""
    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    client = MCPClient(config)
    try:
        await client.connect()
        await client.initialize()
        result = await client.call_tool("echo", {"text": "stdio-ping"})
        assert not result.is_error
        # content 中至少一个 TextContent
        joined = "".join(c.text for c in result.content)
        assert "stdio-ping" in joined
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_stdio_unknown_tool_raises():
    """call_tool 调未注册工具 → MCPToolNotFoundError 或 MCPToolCallError。"""
    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    client = MCPClient(config)
    try:
        await client.connect()
        await client.initialize()
        with pytest.raises((MCPToolNotFoundError, MCPToolCallError, MCPProtocolError)):
            await client.call_tool("does_not_exist", {})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_stdio_agent_tool_register_and_execute():
    """MCPAgentTool 注册到 ToolRegistry 后能被执行。"""
    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    client = MCPClient(config)
    try:
        await client.connect()
        await client.initialize()
        tools = await client.list_tools()
        echo_info = next(t for t in tools if t.name == "echo")
        agent_tool = MCPAgentTool(
            server_name="fake",
            tool_info=echo_info,
            client=client,
        )
        # 名字符合 mcp__{server}__{tool} 命名空间
        assert agent_tool.name == "mcp__fake__echo"

        registry = ToolRegistry([agent_tool])
        assert registry.has("mcp__fake__echo")

        result = await agent_tool.execute("call-1", {"text": "via-agent-tool"})
        assert not result.is_error
        joined = "".join(c.text for c in result.content)
        assert "via-agent-tool" in joined
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_stdio_transport_close_kills_subprocess():
    """transport.close() 后子进程不应残留。"""
    config = MCPServerConfig(
        name="fake",
        transport="stdio",
        command=sys.executable,
        args=[str(_FIXTURE)],
        timeout_s=10.0,
    )
    transport = StdioMCPTransport(
        config.command, config.args, cwd=config.cwd, env=config.env,
    )
    await transport.connect()
    proc = transport._proc  # type: ignore[attr-defined]
    assert proc is not None
    pid = proc.pid
    await transport.close()
    # 给 OS 一点时间清理
    await asyncio.sleep(0.1)
    # 子进程应已退出
    assert proc.returncode is not None, (
        f"subprocess pid={pid} 在 transport.close 后仍未退出"
    )

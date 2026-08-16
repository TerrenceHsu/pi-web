"""Opt-in real-network integration test for the built-in DDGS MCP process."""

from __future__ import annotations

import json
import sys

import pytest

from pi_agent_core_py.mcp import MCPClient, MCPServerConfig
from pi_agent_core_py.mcp.ddgs_server import DDGSSearchSettings, build_ddgs_server_args


@pytest.mark.integration
async def test_real_ddgs_search_over_project_mcp_client() -> None:
    settings = DDGSSearchSettings(max_results=3, timeout_seconds=15, backend="auto")
    client = MCPClient(
        MCPServerConfig(
            name="ddgs",
            command=sys.executable,
            args=build_ddgs_server_args(settings),
            timeout_s=20,
        )
    )
    try:
        await client.connect()
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool(
            "search_text",
            {"query": "OpenAI official website"},
        )
    finally:
        await client.close()

    assert {tool.name for tool in tools} == {"search_text", "search_news"}
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert len(payload) <= 3
    assert payload

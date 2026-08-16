"""Built-in DDGS MCP protocol and server-enforced settings tests."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from pi_agent_core_py.mcp.ddgs_server import (
    DDGSMCPServer,
    DDGSSearchSettings,
    build_ddgs_server_args,
    settings_from_ddgs_server_args,
)


def test_settings_round_trip_through_command_args() -> None:
    settings = DDGSSearchSettings(
        max_results=12,
        region="cn-zh",
        safesearch="off",
        timelimit="w",
        timeout_seconds=17,
    )

    restored = settings_from_ddgs_server_args(build_ddgs_server_args(settings))

    assert restored == settings


def test_initialize_and_tools_list_are_valid_mcp_responses() -> None:
    server = DDGSMCPServer(DDGSSearchSettings(max_results=7, region="us-en"))

    initialized = server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    listed = server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    assert initialized is not None
    assert initialized["result"]["serverInfo"]["name"] == "pi-agent-ddgs"
    assert listed is not None
    tools = listed["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search_text", "search_news"]
    assert "7 results" in tools[0]["description"]
    assert set(tools[0]["inputSchema"]["properties"]) == {"query", "page"}


def test_search_uses_server_settings_not_agent_arguments(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeDDGS:
        def __init__(self, *, timeout: int) -> None:
            captured["timeout"] = timeout

        def text(self, query: str, **kwargs):
            captured["query"] = query
            captured["kwargs"] = kwargs
            return [{"title": "result", "href": "https://example.test"}]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    settings = DDGSSearchSettings(
        max_results=9,
        region="cn-zh",
        safesearch="on",
        timelimit="m",
        timeout_seconds=13,
        backend="duckduckgo",
    )
    server = DDGSMCPServer(settings)

    response = server.call_tool("search_text", {"query": "pi agent", "page": 2})

    assert response["isError"] is False
    assert json.loads(response["content"][0]["text"])[0]["title"] == "result"
    assert captured["timeout"] == 13
    assert captured["query"] == "pi agent"
    assert captured["kwargs"] == {
        "region": "cn-zh",
        "safesearch": "on",
        "timelimit": "m",
        "max_results": 9,
        "page": 2,
        "backend": "duckduckgo",
    }


def test_agent_cannot_override_locked_search_settings() -> None:
    server = DDGSMCPServer(DDGSSearchSettings())

    response = server.call_tool(
        "search_text",
        {"query": "test", "max_results": 20},
    )

    assert response["isError"] is True
    assert "unsupported arguments: max_results" in response["content"][0]["text"]

"""Built-in DDGS web-search MCP server.

The upstream ``ddgs mcp`` command exposes per-call search parameters, but the
web application needs administrator-controlled defaults that the agent cannot
override.  This small stdio server keeps those settings in its process command
line (which the existing MCP persistence layer already stores) and exposes a
focused pair of search tools.

The module intentionally imports :mod:`ddgs` lazily.  A disabled built-in
server can therefore be listed and configured even when the optional web
dependency has not been installed yet; connection testing reports the missing
dependency as a normal MCP error.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DDGS_SERVER_NAME = "ddgs"
DDGS_SERVER_MODULE = "pi_agent_core_py.mcp.ddgs_server"


class DDGSSearchSettings(BaseModel):
    """User-editable, server-enforced defaults for DuckDuckGo searches."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    max_results: int = Field(default=5, ge=1, le=20)
    region: str = Field(default="wt-wt", min_length=2, max_length=32)
    safesearch: Literal["on", "moderate", "off"] = "moderate"
    timelimit: Literal["d", "w", "m", "y"] | None = None
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    backend: Literal["auto", "duckduckgo"] = "auto"

    @field_validator("region")
    @classmethod
    def _validate_region(cls, value: str) -> str:
        normalized = value.lower()
        if not all(char.isascii() and (char.isalnum() or char in "-_") for char in normalized):
            raise ValueError("region may contain only ASCII letters, numbers, '-' and '_'")
        return normalized


def build_ddgs_server_args(settings: DDGSSearchSettings) -> list[str]:
    """Encode settings into the persisted stdio command arguments."""

    args = [
        "-m",
        DDGS_SERVER_MODULE,
        "--max-results",
        str(settings.max_results),
        "--region",
        settings.region,
        "--safesearch",
        settings.safesearch,
        "--timeout-seconds",
        str(settings.timeout_seconds),
        "--backend",
        settings.backend,
    ]
    if settings.timelimit is not None:
        args.extend(["--timelimit", settings.timelimit])
    return args


def settings_from_ddgs_server_args(args: list[str]) -> DDGSSearchSettings:
    """Recover persisted settings, falling back safely for legacy/bad args."""

    flag_to_field = {
        "--max-results": "max_results",
        "--region": "region",
        "--safesearch": "safesearch",
        "--timelimit": "timelimit",
        "--timeout-seconds": "timeout_seconds",
        "--backend": "backend",
    }
    raw: dict[str, Any] = {}
    index = 0
    while index < len(args):
        field = flag_to_field.get(args[index])
        if field is not None and index + 1 < len(args):
            raw[field] = args[index + 1]
            index += 2
        else:
            index += 1

    for numeric_field in ("max_results", "timeout_seconds"):
        if numeric_field in raw:
            try:
                raw[numeric_field] = int(raw[numeric_field])
            except (TypeError, ValueError):
                return DDGSSearchSettings()
    try:
        return DDGSSearchSettings.model_validate(raw)
    except ValueError:
        return DDGSSearchSettings()


class DDGSMCPServer:
    """Minimal JSON-RPC MCP server backed by :class:`ddgs.DDGS`."""

    def __init__(self, settings: DDGSSearchSettings) -> None:
        self.settings = settings

    def _tool_description(self, source: str) -> str:
        window = self.settings.timelimit or "any time"
        return (
            f"Search DDGS {source} using the {self.settings.backend} backend. "
            f"The server returns at most "
            f"{self.settings.max_results} results for region {self.settings.region}, "
            f"safe search {self.settings.safesearch}, time range {window}."
        )

    def tools(self) -> list[dict[str, Any]]:
        schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "Search query; DuckDuckGo operators such as site: are allowed.",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 1,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        return [
            {
                "name": "search_text",
                "description": self._tool_description("web results"),
                "inputSchema": schema,
            },
            {
                "name": "search_news",
                "description": self._tool_description("news results"),
                "inputSchema": schema,
            },
        ]

    @staticmethod
    def _validate_search_arguments(arguments: Any) -> tuple[str, int]:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        unknown = set(arguments) - {"query", "page"}
        if unknown:
            raise ValueError(f"unsupported arguments: {', '.join(sorted(unknown))}")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if len(query) > 1000:
            raise ValueError("query must be at most 1000 characters")
        page = arguments.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10:
            raise ValueError("page must be an integer between 1 and 10")
        return query.strip(), page

    def _search(self, tool_name: str, arguments: Any) -> list[dict[str, Any]]:
        query, page = self._validate_search_arguments(arguments)
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError(
                "DDGS dependency is unavailable; install the project's web dependencies"
            ) from exc

        client = DDGS(timeout=self.settings.timeout_seconds)
        common: dict[str, Any] = {
            "region": self.settings.region,
            "safesearch": self.settings.safesearch,
            "timelimit": self.settings.timelimit,
            "max_results": self.settings.max_results,
            "page": page,
            "backend": self.settings.backend,
        }
        if tool_name == "search_text":
            result = client.text(query, **common)
        elif tool_name == "search_news":
            result = client.news(query, **common)
        else:
            raise ValueError(f"unknown tool: {tool_name}")
        return list(result or [])

    def call_tool(self, tool_name: str, arguments: Any) -> dict[str, Any]:
        try:
            results = self._search(tool_name, arguments)
            text = json.dumps(results, ensure_ascii=False, indent=2)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            return {"content": [{"type": "text", "text": message[:1000]}], "isError": True}

    def handle_request(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")

        # MCP notifications do not receive a JSON-RPC response.
        if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return None
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")

        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pi-agent-ddgs", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return self._error(request_id, -32602, "Invalid tools/call params")
            result = self.call_tool(params["name"], params.get("arguments", {}))
        else:
            return self._error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Built-in DDGS MCP stdio server")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--region", default="wt-wt")
    parser.add_argument("--safesearch", choices=("on", "moderate", "off"), default="moderate")
    parser.add_argument("--timelimit", choices=("d", "w", "m", "y"), default=None)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument("--backend", choices=("auto", "duckduckgo"), default="auto")
    return parser


def main() -> None:
    namespace = _argument_parser().parse_args()
    settings = DDGSSearchSettings.model_validate(vars(namespace))
    server = DDGSMCPServer(settings)

    for line in sys.stdin:
        response: dict[str, Any] | None
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = server._error(None, -32700, "Parse error")
        else:
            response = server.handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

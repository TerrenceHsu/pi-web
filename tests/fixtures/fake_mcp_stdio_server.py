"""Minimal fake MCP stdio server for integration testing.

JSON-RPC 2.0 over stdio（每行一条 message）。支持：
- initialize
- tools/list  → 返回一个 echo 工具
- tools/call  → 执行 echo，原样返回 arguments["text"]

只在 integration test 中作为子进程启动；不是项目运行时依赖。
"""
from __future__ import annotations

import json
import sys


def _result(req_id: int, result: dict) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result,
    })


def _error(req_id: int, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    })


def handle(line: str) -> str | None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-stdio-mcp", "version": "0.0.1"},
        })
    if method == "notifications/initialized":
        # notification 无 id，无需回应
        return None
    if method == "tools/list":
        return _result(req_id, {
            "tools": [
                {
                    "name": "echo",
                    "description": "echo back the text argument",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            ],
        })
    if method == "tools/call":
        name = params.get("name")
        if name != "echo":
            return _error(req_id, -32601, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        text = args.get("text", "")
        return _result(req_id, {
            "content": [{"type": "text", "text": str(text)}],
            "isError": False,
        })
    return _error(req_id, -32601, f"unknown method: {method}")


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        resp = handle(line)
        if resp is not None:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

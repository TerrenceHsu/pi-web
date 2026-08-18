"""Regression tests for the MCP stdio UTF-8 protocol boundary."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from pi_agent_core_py import MCPProtocolError
from pi_agent_core_py.mcp.transport import StdioMCPTransport


class _Reader:
    def __init__(self, value: bytes) -> None:
        self._value = value

    async def readline(self) -> bytes:
        return self._value


class _Process:
    def __init__(self, stdout: Any | None = None) -> None:
        self.stdout = stdout
        self.stdin = None


@pytest.mark.asyncio
async def test_stdio_spawn_forces_utf8_even_when_config_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_spawn(*args: Any, **kwargs: Any) -> _Process:
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    transport = StdioMCPTransport(
        "python",
        env={
            "CUSTOM_MCP_VALUE": "kept",
            "PYTHONIOENCODING": "cp936",
            "PYTHONUTF8": "0",
        },
    )

    await transport.connect()

    assert captured["env"]["CUSTOM_MCP_VALUE"] == "kept"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["env"]["PYTHONUTF8"] == "1"


@pytest.mark.asyncio
async def test_stdio_receive_preserves_non_ascii_json() -> None:
    transport = StdioMCPTransport("unused")
    transport._proc = _Process(  # noqa: SLF001 - protocol-boundary fixture
        _Reader('{"jsonrpc":"2.0","result":"中文标题"}\n'.encode())
    )

    message = await transport.receive()

    assert message["result"] == "中文标题"


@pytest.mark.asyncio
async def test_stdio_receive_rejects_invalid_utf8_instead_of_persisting_replacements() -> None:
    transport = StdioMCPTransport("unused")
    transport._proc = _Process(  # noqa: SLF001 - protocol-boundary fixture
        _Reader(b'{"jsonrpc":"2.0","result":"\xff"}\n')
    )

    with pytest.raises(MCPProtocolError, match="not valid UTF-8"):
        await transport.receive()


@pytest.mark.asyncio
async def test_python_stdio_child_round_trips_non_ascii_result() -> None:
    script = (
        "import json,sys; "
        "request=json.loads(sys.stdin.readline()); "
        "response={'jsonrpc':'2.0','id':request['id'],'result':'中文标题'}; "
        "sys.stdout.write(json.dumps(response,ensure_ascii=False)+'\\n'); "
        "sys.stdout.flush()"
    )
    transport = StdioMCPTransport(sys.executable, ["-c", script])
    await transport.connect()
    try:
        await transport.send({"jsonrpc": "2.0", "id": 1, "method": "probe"})
        response = await transport.receive()
    finally:
        await transport.close()

    assert response["result"] == "中文标题"

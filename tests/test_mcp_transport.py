"""MCP Transport 单元测试——不依赖真实 MCP server。

覆盖：
- StdioMCPTransport：connect/send/receive/close 成功路径 + 错误路径
- FakeMCPTransport：已由其它测试覆盖，这里只做冒烟

StdioMCPTransport 用 `sys.executable -c` 启动最小 echo JSON Lines 子进程，
不需要 tests/fixtures/fake_mcp_stdio_server.py。
"""
from __future__ import annotations

import sys

import httpx
import pytest

from pi_agent_core_py.mcp.client import MCPClient
from pi_agent_core_py.mcp.config import MCPServerConfig
from pi_agent_core_py.mcp.errors import (
    MCPConnectionError,
    MCPProtocolError,
    MCPTransportClosedError,
)
from pi_agent_core_py.mcp.transport import (
    FakeMCPTransport,
    StdioMCPTransport,
    StreamableHttpMCPTransport,
)

# ============================================================================
# Echo JSON Lines 子进程脚本——读一行 JSON，回一行 JSON
# ============================================================================

# 用 inline Python 脚本作为子进程，避免依赖 fixture 文件路径。
# 行为：每读一行 stdin，解析 JSON，回 {"echo": <msg>}；EOF 退出。
_ECHO_SCRIPT = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    try:\n"
    "        msg = json.loads(line)\n"
    "    except Exception:\n"
    "        sys.stdout.write('not-json-error\\n')\n"
    "        sys.stdout.flush()\n"
    "        continue\n"
    "    sys.stdout.write(json.dumps({'echo': msg}) + '\\n')\n"
    "    sys.stdout.flush()\n"
)


def test_remote_http_transport_config_is_supported() -> None:
    config = MCPServerConfig.model_validate(
        {
            "name": "remote",
            "transport": "http",
            "url": "https://example.com/mcp",
        }
    )
    assert config.url == "https://example.com/mcp"


@pytest.mark.asyncio
async def test_streamable_http_json_session_and_close() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        payload = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "Mcp-Session-Id": "session-1",
            },
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = StreamableHttpMCPTransport(
        "https://example.com/mcp",
        headers={"Authorization": "Bearer test"},
        client=http,
    )
    await transport.connect()
    await transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert (await transport.receive())["id"] == 1
    await transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert (await transport.receive())["id"] == 2
    await transport.close()
    await http.aclose()

    assert requests[0].headers["accept"] == "application/json, text/event-stream"
    assert "mcp-protocol-version" not in requests[0].headers
    assert requests[1].headers["mcp-session-id"] == "session-1"
    assert requests[1].headers["mcp-protocol-version"] == "2025-06-18"
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_streamable_http_parses_sse_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            text=(
                'event: message\ndata: {"jsonrpc":"2.0","method":"progress"}\n\n'
                'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n'
            ),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = StreamableHttpMCPTransport("https://example.com/mcp", client=http)
    await transport.connect()
    await transport.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert await transport.receive() == {"jsonrpc": "2.0", "id": 7, "result": {}}
    await transport.close()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_mcp_client_sends_initialized_notification() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        methods.append(payload["method"])
        if "id" not in payload:
            return httpx.Response(202)
        result = (
            {"protocolVersion": "2025-06-18", "capabilities": {}}
            if payload["method"] == "initialize"
            else {"tools": []}
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://example.com/mcp",
    )
    transport = StreamableHttpMCPTransport(config.url or "", client=http)
    client = MCPClient(config, transport)
    await client.connect()
    await client.initialize()
    assert await client.list_tools() == []
    await client.close()
    await http.aclose()
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


# ============================================================================
# StdioMCPTransport：成功路径
# ============================================================================


@pytest.fixture
def echo_transport():
    """启动 echo 子进程的 transport——测试结束自动 close。"""
    t = StdioMCPTransport(sys.executable, ["-c", _ECHO_SCRIPT])
    return t


@pytest.mark.asyncio
async def test_stdio_connect_creates_process(echo_transport):
    await echo_transport.connect()
    assert echo_transport._proc is not None
    await echo_transport.close()


@pytest.mark.asyncio
async def test_stdio_connect_idempotent(echo_transport):
    """重复 connect 不重启子进程——返回同一个 proc。"""
    await echo_transport.connect()
    proc1 = echo_transport._proc
    await echo_transport.connect()
    assert echo_transport._proc is proc1
    await echo_transport.close()


@pytest.mark.asyncio
async def test_stdio_send_receive_roundtrip(echo_transport):
    """send 一条 JSON → receive 回声 JSON。"""
    await echo_transport.connect()
    await echo_transport.send({"jsonrpc": "2.0", "method": "ping", "id": 1})
    resp = await echo_transport.receive()
    assert resp == {"echo": {"jsonrpc": "2.0", "method": "ping", "id": 1}}
    await echo_transport.close()


@pytest.mark.asyncio
async def test_stdio_multiple_roundtrips(echo_transport):
    """连续多次 send/receive——验证 stdin/stdout 缓冲正常。"""
    await echo_transport.connect()
    for i in range(5):
        await echo_transport.send({"id": i, "msg": f"hello-{i}"})
        resp = await echo_transport.receive()
        assert resp == {"echo": {"id": i, "msg": f"hello-{i}"}}
    await echo_transport.close()


@pytest.mark.asyncio
async def test_stdio_close_idempotent(echo_transport):
    """close 多次不抛错。"""
    await echo_transport.connect()
    await echo_transport.close()
    await echo_transport.close()
    await echo_transport.close()
    assert echo_transport._closed is True


@pytest.mark.asyncio
async def test_stdio_close_without_connect():
    """没 connect 直接 close——不抛错（proc=None 路径）。"""
    t = StdioMCPTransport(sys.executable, ["-c", _ECHO_SCRIPT])
    await t.close()
    assert t._closed is True


# ============================================================================
# StdioMCPTransport：错误路径
# ============================================================================


@pytest.mark.asyncio
async def test_stdio_connect_command_not_found():
    """command 不存在 → MCPConnectionError。"""
    t = StdioMCPTransport("this_command_does_not_exist_xyz_12345", [])
    with pytest.raises(MCPConnectionError, match="command not found"):
        await t.connect()


@pytest.mark.asyncio
async def test_stdio_send_before_connect_raises():
    """没 connect 就 send → MCPTransportClosedError。"""
    t = StdioMCPTransport(sys.executable, ["-c", _ECHO_SCRIPT])
    with pytest.raises(MCPTransportClosedError, match="not connected"):
        await t.send({"x": 1})


@pytest.mark.asyncio
async def test_stdio_receive_before_connect_raises():
    """没 connect 就 receive → MCPTransportClosedError。"""
    t = StdioMCPTransport(sys.executable, ["-c", _ECHO_SCRIPT])
    with pytest.raises(MCPTransportClosedError, match="not connected"):
        await t.receive()


@pytest.mark.asyncio
async def test_stdio_send_after_close_raises(echo_transport):
    """close 后 send → MCPTransportClosedError。"""
    await echo_transport.connect()
    await echo_transport.close()
    with pytest.raises(MCPTransportClosedError):
        await echo_transport.send({"x": 1})


@pytest.mark.asyncio
async def test_stdio_receive_after_close_raises(echo_transport):
    """close 后 receive → MCPTransportClosedError。"""
    await echo_transport.connect()
    await echo_transport.close()
    with pytest.raises(MCPTransportClosedError):
        await echo_transport.receive()


@pytest.mark.asyncio
async def test_stdio_connect_after_close_silent_return():
    """close 后再 connect → 静默 return（_proc 已存在），后续 send/receive 才抛 closed。

    这是 transport.py 的设计——close 幂等；connect 时 _proc 非 None 就 return。
    真正的 closed 检查在 send/receive 路径（_closed flag）。
    """
    t = StdioMCPTransport(sys.executable, ["-c", _ECHO_SCRIPT])
    await t.connect()
    await t.close()
    # connect 不抛错（_proc 仍非 None）
    await t.connect()
    # 但 send/receive 会抛 closed 错
    with pytest.raises(MCPTransportClosedError):
        await t.send({"x": 1})
    with pytest.raises(MCPTransportClosedError):
        await t.receive()


@pytest.mark.asyncio
async def test_stdio_receive_eof_when_subprocess_exits():
    """子进程退出后 receive → MCPTransportClosedError（stdout EOF）。"""
    # 子进程：读完一行立刻 exit 0
    script = "import sys; sys.stdin.readline(); sys.exit(0)"
    t = StdioMCPTransport(sys.executable, ["-c", script])
    await t.connect()
    await t.send({"x": 1})
    # 子进程 exit 后 stdout EOF
    with pytest.raises(MCPTransportClosedError, match="EOF"):
        await t.receive()
    await t.close()


@pytest.mark.asyncio
async def test_stdio_receive_invalid_json_raises():
    """子进程返回非 JSON → MCPProtocolError。"""
    script = "import sys; sys.stdin.readline(); sys.stdout.write('not-json\\n'); sys.stdout.flush()"
    t = StdioMCPTransport(sys.executable, ["-c", script])
    await t.connect()
    await t.send({"x": 1})
    with pytest.raises(MCPProtocolError, match="invalid JSON"):
        await t.receive()
    await t.close()


@pytest.mark.asyncio
async def test_stdio_receive_json_array_not_object_raises():
    """子进程返回 JSON array（非 dict）→ MCPProtocolError。"""
    script = (
        "import sys; sys.stdin.readline(); "
        "sys.stdout.write('[1,2,3]\\n'); sys.stdout.flush()"
    )
    t = StdioMCPTransport(sys.executable, ["-c", script])
    await t.connect()
    await t.send({"x": 1})
    with pytest.raises(MCPProtocolError, match="not an object"):
        await t.receive()
    await t.close()


@pytest.mark.asyncio
async def test_stdio_env_passed_to_subprocess():
    """传 env 给 transport → 子进程能读到。"""
    # 子进程：读到 ENV_TEST_VAR 后 echo 回来
    script = (
        "import os, sys, json\n"
        "var = os.environ.get('E2E_TRANSPORT_TEST_VAR', 'MISSING')\n"
        "sys.stdout.write(json.dumps({'var': var}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    t = StdioMCPTransport(
        sys.executable,
        ["-c", script],
        env={"E2E_TRANSPORT_TEST_VAR": "present"},
    )
    await t.connect()
    # 不 send 任何东西——子进程启动即输出
    resp = await t.receive()
    assert resp == {"var": "present"}
    await t.close()


# ============================================================================
# FakeMCPTransport：最小离线行为覆盖
# ============================================================================


@pytest.mark.asyncio
async def test_fake_transport_roundtrip():
    """FakeMCPTransport 用 handler 自动响应——冒烟测试。"""

    def handler(msg: dict) -> dict:
        return {"ok": True, "received": msg}

    t = FakeMCPTransport(handler)
    await t.connect()
    await t.send({"ping": 1})
    resp = await t.receive()
    assert resp == {"ok": True, "received": {"ping": 1}}
    await t.close()

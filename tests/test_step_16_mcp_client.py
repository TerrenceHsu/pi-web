"""Step 16 — MCPClient 单元测试（FakeMCPTransport，无 subprocess）。

覆盖：
- initialize 成功
- tools/list 成功
- tools/list 解析 inputSchema
- tools/call 成功
- tools/call 解析 text content
- tools/call 非 text content 走占位
- JSON-RPC error 含 "not found" → MCPToolNotFoundError
- JSON-RPC error 其它 → MCPProtocolError
- response 缺 result → MCPProtocolError
- close 幂等
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    MCPClient,
    MCPProtocolError,
    MCPServerConfig,
    MCPToolNotFoundError,
)
from pi_agent_core_py.mcp import FakeMCPTransport

# ============================================================================
# Handler 工厂：按 method 返回 JSON-RPC response
# ============================================================================


def make_handler(
    *,
    initialize_result: dict[str, Any] | None = None,
    tools_list: list[dict[str, Any]] | None = None,
    call_result: dict[str, Any] | None = None,
    error_response: dict[str, Any] | None = None,
    error_for_method: str | None = None,
) -> Any:
    """构造 FakeMCPTransport handler。

    所有参数都是可选；未提供的 method 返回 default empty result。
    """
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        base = {"jsonrpc": "2.0", "id": req_id}

        if error_for_method and method == error_for_method and error_response:
            base["error"] = error_response
            return base

        if method == "initialize":
            base["result"] = initialize_result or {"serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            base["result"] = {"tools": tools_list or []}
        elif method == "tools/call":
            base["result"] = call_result or {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        else:
            base["error"] = {"code": -32601, "message": f"method not found: {method}"}
        return base

    return handler


def make_stdio_config(name: str = "srv") -> MCPServerConfig:
    """构造合法的 stdio config（command 是占位，不真起进程）。"""
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command="placeholder-binary",
        args=[],
    )


# ============================================================================
# 1. initialize
# ============================================================================


@pytest.mark.asyncio
async def test_initialize_succeeds() -> None:
    fake = FakeMCPTransport(
        handler=make_handler(initialize_result={"protocolVersion": "2024-11-05"}),
    )
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    result = await client.initialize()
    assert client.initialized
    assert result.get("protocolVersion") == "2024-11-05"
    await client.close()


@pytest.mark.asyncio
async def test_initialize_sends_correct_jsonrpc() -> None:
    """initialize 应该发 jsonrpc=2.0 + method=initialize + params 含 clientInfo。"""
    fake = FakeMCPTransport(handler=make_handler())
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()

    init_req = fake.sent[0]
    assert init_req["jsonrpc"] == "2.0"
    assert init_req["method"] == "initialize"
    assert "id" in init_req
    params = init_req.get("params", {})
    assert "protocolVersion" in params
    assert "clientInfo" in params
    await client.close()


# ============================================================================
# 2. tools/list
# ============================================================================


@pytest.mark.asyncio
async def test_list_tools_parses_basic_info() -> None:
    tools_list = [
        {
            "name": "echo",
            "description": "Echo input text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    ]
    fake = FakeMCPTransport(handler=make_handler(tools_list=tools_list))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    tools = await client.list_tools()

    assert len(tools) == 1
    t = tools[0]
    assert t.name == "echo"
    assert t.description == "Echo input text"
    assert t.input_schema["properties"]["text"]["type"] == "string"
    assert t.input_schema["required"] == ["text"]
    assert t.raw is not None
    await client.close()


@pytest.mark.asyncio
async def test_list_tools_compatible_with_input_schema_field() -> None:
    """input_schema 也兼容 snake_case 'input_schema' 字段（防御性）。"""
    tools_list = [
        {"name": "t1", "description": "", "input_schema": {"type": "object"}},
    ]
    fake = FakeMCPTransport(handler=make_handler(tools_list=tools_list))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    tools = await client.list_tools()
    assert tools[0].input_schema == {"type": "object"}
    await client.close()


@pytest.mark.asyncio
async def test_list_tools_empty() -> None:
    fake = FakeMCPTransport(handler=make_handler(tools_list=[]))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    tools = await client.list_tools()
    assert tools == []
    await client.close()


# ============================================================================
# 3. tools/call 成功路径
# ============================================================================


@pytest.mark.asyncio
async def test_call_tool_succeeds_text_content() -> None:
    fake = FakeMCPTransport(handler=make_handler(call_result={
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ],
        "isError": False,
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.call_tool("echo", {"text": "hi"})

    assert result.is_error is False
    assert len(result.content) == 2
    assert result.content[0].text == "hello"
    assert result.content[1].text == "world"

    # 检查发出的请求
    call_req = fake.sent[-1]
    assert call_req["method"] == "tools/call"
    assert call_req["params"]["name"] == "echo"
    assert call_req["params"]["arguments"] == {"text": "hi"}
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_non_text_content_uses_placeholder() -> None:
    """非 text 类型的 content 不崩，转占位说明。"""
    fake = FakeMCPTransport(handler=make_handler(call_result={
        "content": [
            {"type": "image", "data": "..."},
            {"type": "text", "text": "real"},
        ],
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.call_tool("t", {})

    assert len(result.content) == 2
    assert "unsupported" in result.content[0].text
    assert result.content[1].text == "real"
    await client.close()


@pytest.mark.asyncio
async def test_call_tool_iserror_returns_error_result() -> None:
    """server 返回 isError=true → MCPCallResult(is_error=True)，不抛异常。"""
    fake = FakeMCPTransport(handler=make_handler(call_result={
        "content": [{"type": "text", "text": "tool internal error"}],
        "isError": True,
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.call_tool("t", {})
    assert result.is_error is True
    assert result.content[0].text == "tool internal error"
    await client.close()


# ============================================================================
# 4. JSON-RPC error
# ============================================================================


@pytest.mark.asyncio
async def test_call_tool_not_found_raises_specific_error() -> None:
    """tools/call 返回 JSON-RPC error 含 'not found' → MCPToolNotFoundError。"""
    fake = FakeMCPTransport(handler=make_handler(
        error_for_method="tools/call",
        error_response={"code": -32602, "message": "tool not found: ghost"},
    ))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPToolNotFoundError):
        await client.call_tool("ghost", {})
    await client.close()


@pytest.mark.asyncio
async def test_generic_jsonrpc_error_raises_protocol_error() -> None:
    fake = FakeMCPTransport(handler=make_handler(
        error_for_method="tools/list",
        error_response={"code": -32603, "message": "internal server error"},
    ))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "internal server error" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_response_missing_result_raises_protocol_error() -> None:
    """response 既无 result 也无 error → MCPProtocolError。"""
    async def bad_handler(msg):
        return {"jsonrpc": "2.0", "id": msg["id"]}  # 缺 result/error

    fake = FakeMCPTransport(handler=bad_handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError):
        await client.initialize()
    await client.close()


# ============================================================================
# 5. close 幂等
# ============================================================================


@pytest.mark.asyncio
async def test_close_idempotent() -> None:
    fake = FakeMCPTransport(handler=make_handler())
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.close()
    await client.close()  # 不抛
    await client.close()  # 不抛


@pytest.mark.asyncio
async def test_close_after_failed_connect_still_safe() -> None:
    """connect 失败后调 close 也安全。"""
    fake = FakeMCPTransport(
        handler=make_handler(),
        raise_on_connect=ConnectionError("simulated"),
    )
    client = MCPClient(make_stdio_config(), transport=fake)
    with pytest.raises(ConnectionError):
        await client.connect()
    await client.close()  # 不抛

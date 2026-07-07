"""Step 16 — 评审反馈修复点测试。

覆盖：
- MCPClient 并发安全（asyncio.Lock）：parallel 多工具调同一 server 不错位
- response jsonrpc / id 校验：错位 / 畸形 response 抛 MCPProtocolError
- request timeout：transport.receive 卡住时 _request 抛 MCPProtocolError
- MCP tool name 字符集校验：非法字符抛 MCPProtocolError
- tools/list 严格模式：缺 tools / tools 非 list 抛 MCPProtocolError
- initialize 幂等返回缓存 result
- MCPAgentTool details 含 raw / content_count / content_types
- MCPRegistry 重复 connect_all 不泄露旧 client
- MCPRegistry.list_agent_tools() 返回 namespaced name + server 来源
- _connect_one initialize 失败后 client.close() 被调用（避免子进程残留）
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py import (
    MCPAgentTool,
    MCPClient,
    MCPProtocolError,
    MCPRegistry,
    MCPServerConfig,
    MCPToolInfo,
)
from pi_agent_core_py.mcp import FakeMCPTransport

# ============================================================================
# 辅助
# ============================================================================


def make_stdio_config(name: str = "srv", *, timeout_s: float = 30.0) -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="stdio", command="x", timeout_s=timeout_s,
    )


def echo_handler(msg: dict[str, Any]) -> dict[str, Any]:
    """简单 echo handler。"""
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"serverInfo": {"name": "fake"}}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": [{
                "name": "echo",
                "description": "echo",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }]},
        }
    if method == "tools/call":
        params = msg.get("params", {})
        text = (params.get("arguments") or {}).get("text", "")
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": f"echoed: {text}"}]},
        }
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}


# ============================================================================
# 1. 并发安全：asyncio.Lock
# ============================================================================


@pytest.mark.asyncio
async def test_concurrent_requests_serialized_by_lock() -> None:
    """同一 MCPClient 上并发 list_tools + call_tool：response 不错位。

    没有 lock 时 FakeMCPTransport 单 _next_response 槽会被后到请求覆盖，
    导致第一个 awaiter 拿到第二个的 response。MCPClient._request_lock 保证
    send/receive 配对完整。
    """
    fake = FakeMCPTransport(handler=echo_handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()

    # 并发：list_tools 和 多个 call_tool 同时跑
    results = await asyncio.gather(
        client.list_tools(),
        client.call_tool("echo", {"text": "a"}),
        client.call_tool("echo", {"text": "b"}),
        client.call_tool("echo", {"text": "c"}),
    )

    tools, call_a, call_b, call_c = results
    # tools/list 拿到的是工具列表，不是 call_tool 的 text
    assert len(tools) == 1
    assert tools[0].name == "echo"
    # 三个 call_tool 各自拿到自己的 echo 结果
    assert call_a.content[0].text == "echoed: a"
    assert call_b.content[0].text == "echoed: b"
    assert call_c.content[0].text == "echoed: c"
    await client.close()


@pytest.mark.asyncio
async def test_request_id_matched_in_response() -> None:
    """每个 response 的 id 必须与 request 的 id 配对。"""
    fake = FakeMCPTransport(handler=echo_handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    # 跑几次，_next_id 递增，handler 用 msg["id"] 回写
    await client.initialize()
    tools = await client.list_tools()
    assert len(tools) == 1
    await client.close()


# ============================================================================
# 2. JSON-RPC 协议校验
# ============================================================================


@pytest.mark.asyncio
async def test_response_with_wrong_id_raises() -> None:
    """response id != req_id → MCPProtocolError。"""
    async def bad_handler(msg):
        # 故意返回错误的 id（req_id + 100）
        return {"jsonrpc": "2.0", "id": msg["id"] + 100, "result": {}}

    fake = FakeMCPTransport(handler=bad_handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "id mismatch" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_response_with_wrong_jsonrpc_version_raises() -> None:
    """response jsonrpc != "2.0" → MCPProtocolError。"""
    async def bad_handler(msg):
        return {"jsonrpc": "1.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=bad_handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "jsonrpc" in str(exc_info.value).lower()
    await client.close()


# ============================================================================
# 3. timeout
# ============================================================================


@pytest.mark.asyncio
async def test_request_receive_timeout_raises_protocol_error() -> None:
    """transport.receive 永久卡住时，_request 应该在 timeout_s 后抛 MCPProtocolError。"""
    # 让 receive 永远不返回（receive 调用时不抛、不返回任何东西）
    frozen_receive_future: asyncio.Future = asyncio.Future()

    class HangingTransport(FakeMCPTransport):
        async def receive(self) -> dict[str, Any]:
            return await frozen_receive_future  # 永远挂住

    fake = HangingTransport(handler=echo_handler)
    cfg = make_stdio_config(timeout_s=0.1)  # 100ms 超时
    client = MCPClient(cfg, transport=fake)
    await client.connect()

    start = asyncio.get_event_loop().time()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    elapsed = asyncio.get_event_loop().time() - start
    # 应该在 ~0.1s 内抛，而不是永久挂住
    assert elapsed < 1.5, f"timeout too slow: {elapsed}s"
    assert "timeout" in str(exc_info.value).lower()
    # 解挂 future，避免 pytest警告
    frozen_receive_future.cancel()
    await client.close()


# ============================================================================
# 4. tool name 字符集校验
# ============================================================================


@pytest.mark.asyncio
async def test_tools_list_rejects_empty_tool_name() -> None:
    async def handler(msg):
        if msg["method"] == "tools/list":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"tools": [{"name": "", "description": "x"}]},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "missing 'name'" in str(exc_info.value) or "invalid" in str(exc_info.value).lower()
    await client.close()


@pytest.mark.asyncio
async def test_tools_list_rejects_invalid_tool_name_chars() -> None:
    """tool name 含空格 / 斜杠 → MCPProtocolError（避免拼 namespaced name 出问题）。"""
    async def handler(msg):
        if msg["method"] == "tools/list":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"tools": [
                    {"name": "read file", "description": "x"},  # 空格
                ]},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "invalid tool name" in str(exc_info.value).lower()
    await client.close()


# ============================================================================
# 5. list_tools 严格模式
# ============================================================================


@pytest.mark.asyncio
async def test_tools_list_missing_tools_field_raises() -> None:
    async def handler(msg):
        if msg["method"] == "tools/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}  # 缺 tools
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "missing 'tools'" in str(exc_info.value)
    await client.close()


@pytest.mark.asyncio
async def test_tools_list_non_list_tools_raises() -> None:
    async def handler(msg):
        if msg["method"] == "tools/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": "not a list"}}

        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "must be a list" in str(exc_info.value)
    await client.close()


# ============================================================================
# 6. initialize 缓存 result
# ============================================================================


@pytest.mark.asyncio
async def test_initialize_idempotent_returns_cached_result() -> None:
    async def handler(msg):
        if msg["method"] == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"serverInfo": {"name": "cached-srv"}, "protocolVersion": "X"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()

    r1 = await client.initialize()
    r2 = await client.initialize()  # 第二次走缓存，不再 send

    assert r1 == r2
    assert r1["serverInfo"]["name"] == "cached-srv"
    # 第二次 initialize 不应该再发请求（_initialized=True 短路）
    init_requests = [m for m in fake.sent if m["method"] == "initialize"]
    assert len(init_requests) == 1
    await client.close()


# ============================================================================
# 7. MCPAgentTool details 含 raw / content_count / content_types
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_agent_tool_details_contains_raw_metadata() -> None:
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                    "isError": False,
                    "_meta": {"timestamp": "2026-06-30"},
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d", input_schema={"type": "object"}),
    )
    await client.connect()
    result = await tool.execute("c1", {})

    assert result.details["source"] == "mcp"
    assert result.details["server"] == "srv"
    assert result.details["mcp_tool"] == "t"
    assert result.details["content_count"] == 2
    assert result.details["content_types"] == ["text", "text"]
    # raw 截断成字符串后保留 server 原始 result 信息
    assert result.details["raw"] is not None
    assert isinstance(result.details["raw"], str)
    assert "isError" in result.details["raw"] or "False" in result.details["raw"]
    assert result.details["raw_truncated"] is False  # 小 raw 未触发截断
    await client.close()


# ============================================================================
# 8. MCPRegistry 重复 connect_all 不泄露旧 client
# ============================================================================


@pytest.mark.asyncio
async def test_repeated_connect_all_does_not_leak_old_client() -> None:
    """重复 connect_all：旧 client 应该被 close（计数 close 次数验证）。"""
    close_count = {"n": 0}

    class CountingFakeTransport(FakeMCPTransport):
        async def close(self) -> None:
            close_count["n"] += 1
            await super().close()

    # 让 _build_default_transport 每次返回新 transport（模拟真实场景）
    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        return CountingFakeTransport(handler=echo_handler)

    cm._build_default_transport = fake_builder
    try:
        registry = MCPRegistry([MCPServerConfig(name="srv", transport="stdio", command="x")])
        await registry.connect_all()
        assert close_count["n"] == 0  # 第一次 connect，没 close

        await registry.connect_all()  # 第二次：旧 client 应该被 close
        # 旧 client 关闭时 close_count +1；新 client 还活着
        assert close_count["n"] == 1

        await registry.close_all()
        # 最后 close_all 关掉新 client
        assert close_count["n"] == 2
    finally:
        cm._build_default_transport = original


# ============================================================================
# 9. MCPRegistry.list_agent_tools 返回 namespaced name + server 来源
# ============================================================================


@pytest.mark.asyncio
async def test_list_agent_tools_returns_namespaced_and_server() -> None:
    """list_agent_tools() 返回 MCPAgentTool，含 namespaced name + server 来源。"""
    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        return FakeMCPTransport(handler=echo_handler)

    cm._build_default_transport = fake_builder
    try:
        registry = MCPRegistry([
            MCPServerConfig(name="srv1", transport="stdio", command="x"),
            MCPServerConfig(name="srv2", transport="stdio", command="y"),
        ])
        await registry.connect_all()
        await registry.refresh_tools()

        # _make_handler for srv2 doesn't exist——用 echo_handler 但需要 patch
        # per-server。简化：srv1 和 srv2 都用同一个 echo handler，工具名都是 echo。
        agent_tools = registry.list_agent_tools()
        # 两个 server 都有 echo，namespace 后不冲突
        assert len(agent_tools) == 2
        names = sorted(t.name for t in agent_tools)
        assert names == ["mcp__srv1__echo", "mcp__srv2__echo"]
        # server 来源可查
        for t in agent_tools:
            assert t.server_name in {"srv1", "srv2"}
            assert t.mcp_tool_name == "echo"
        await registry.close_all()
    finally:
        cm._build_default_transport = original


# ============================================================================
# 10. _connect_one initialize 失败后 client.close() 被调用
# ============================================================================


@pytest.mark.asyncio
async def test_connect_one_initialization_failure_closes_client() -> None:
    """connect 成功但 initialize 失败：client 应该被 close（避免 stdio 子进程残留）。"""
    close_count = {"n": 0}

    class CountingFakeTransport(FakeMCPTransport):
        async def close(self) -> None:
            close_count["n"] += 1
            await super().close()

    # initialize 时返回 JSON-RPC error（触发 MCPProtocolError）
    async def handler(msg):
        if msg["method"] == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": "init failed"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        return CountingFakeTransport(handler=handler)

    cm._build_default_transport = fake_builder
    try:
        registry = MCPRegistry([MCPServerConfig(name="srv1", transport="stdio", command="x")])
        await registry.connect_all()

        # initialize 失败 → client.close() 应该被调用
        assert close_count["n"] == 1
        state = registry.list_servers()[0]
        assert state.connected is False
        assert "MCPProtocolError" in (state.last_error or "")
        await registry.close_all()
    finally:
        cm._build_default_transport = original

"""Step 16 — 第二轮评审硬化点测试。

覆盖：
- MCPRegistry.close_all 即使 _clients 空也清 _tools/states
- _connect_one 失败后 _tools 缓存同步清空（stale tools 不残留）
- MCPToolInfo field_validator 拒绝非法 name
- MCPAgentTool __init__ 兜底校验 server_name + tool name
- MCPClient timeout 后自动 close；后续调用报 client closed
- MCPClient close 后再 connect 抛 MCPProtocolError
- list_tools 遇到非 dict item 抛 MCPProtocolError（不静默跳过）
- tool_validation 对非法 schema 包成 ToolArgumentValidationError
- MCPAgentTool details.raw 体积超限时截断并标 raw_truncated=True
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from pi_agent_core_py import (
    MCPAgentTool,
    MCPClient,
    MCPProtocolError,
    MCPRegistry,
    MCPServerConfig,
    MCPToolInfo,
    ToolArgumentValidationError,
    validate_tool_arguments,
)
from pi_agent_core_py.mcp import FakeMCPTransport


def _stdio_cfg(name: str = "srv", *, timeout_s: float = 30.0) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="x", timeout_s=timeout_s)


def _echo_handler(msg: dict[str, Any]) -> dict[str, Any]:
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": [{
                "name": "echo", "description": "d",
                "inputSchema": {"type": "object"},
            }]},
        }
    if method == "tools/call":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}


# ============================================================================
# 1. close_all 即使 _clients 空也清 _tools/states
# ============================================================================


@pytest.mark.asyncio
async def test_close_all_clears_stale_tools_when_clients_already_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_clients 已空但 _tools 还有缓存时，close_all 必须也清掉 _tools。"""
    import pi_agent_core_py.mcp.client as cm
    monkeypatch.setattr(
        cm, "_build_default_transport",
        lambda cfg: FakeMCPTransport(handler=_echo_handler),
    )

    registry = MCPRegistry([_stdio_cfg()])
    await registry.connect_all()
    await registry.refresh_tools()
    assert len(registry.list_tools()) == 1

    # 手工把 _clients 清空（模拟 close 后再 close / 异常路径）
    registry._clients.clear()  # noqa: SLF001
    # _tools 还在——close_all 必须清它
    await registry.close_all()
    assert len(registry.list_tools()) == 0
    state = registry.list_servers()[0]
    assert state.tool_count == 0
    assert state.connected is False


# ============================================================================
# 2. _connect_one 失败后 _tools 缓存同步清空
# ============================================================================


@pytest.mark.asyncio
async def test_repeated_connect_clears_stale_tools() -> None:
    """重复 connect_all：第一次成功（有 tools），第二次失败——_tools 必须清空。"""
    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    # 第一轮：成功；第二轮：raise_on_connect
    class StatefulBuilder:
        def __init__(self) -> None:
            self.round = 0

        def __call__(self, cfg):
            self.round += 1
            if self.round == 1:
                return FakeMCPTransport(handler=_echo_handler)
            return FakeMCPTransport(raise_on_connect=ConnectionError("round 2 fail"))

    cm._build_default_transport = StatefulBuilder()
    try:
        registry = MCPRegistry([_stdio_cfg()])
        await registry.connect_all()
        await registry.refresh_tools()
        assert len(registry.list_tools()) == 1

        # 第二次 connect_all：旧 client 被 close + 旧 tools 必须清掉
        await registry.connect_all()
        assert len(registry.list_tools()) == 0
        state = registry.list_servers()[0]
        assert state.connected is False
        assert state.tool_count == 0
        await registry.close_all()
    finally:
        cm._build_default_transport = original


# ============================================================================
# 3. MCPToolInfo field_validator 拒绝非法 name
# ============================================================================


def test_mcp_tool_info_rejects_invalid_name_via_validator() -> None:
    """Pydantic field_validator 在 MCPToolInfo 构造时就拒绝非法 name。"""
    with pytest.raises(Exception) as exc_info:
        MCPToolInfo(name="bad/name")
    assert "invalid" in str(exc_info.value).lower() or "name" in str(exc_info.value).lower()


def test_mcp_tool_info_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        MCPToolInfo(name="")


def test_mcp_tool_info_accepts_legal_name() -> None:
    info = MCPToolInfo(name="echo_tool-1")
    assert info.name == "echo_tool-1"


# ============================================================================
# 4. MCPAgentTool __init__ 兜底校验
# ============================================================================


def test_mcp_agent_tool_rejects_invalid_server_name() -> None:
    """即使 MCPToolInfo 合法，server_name 含非法字符也拒绝。"""
    cfg = _stdio_cfg()
    fake = FakeMCPTransport()
    client = MCPClient(cfg, transport=fake)
    info = MCPToolInfo(name="echo")  # 合法
    with pytest.raises(ValueError) as exc_info:
        MCPAgentTool(server_name="bad/server", client=client, tool_info=info)
    assert "server_name" in str(exc_info.value).lower() or "server" in str(exc_info.value).lower()


def test_mcp_agent_tool_rejects_invalid_tool_name_after_manual_construction() -> None:
    """即使 MCPToolInfo 的 field_validator 没拦（绕过 Pydantic 构造），MCPAgentTool 也兜底。"""
    cfg = _stdio_cfg()
    fake = FakeMCPTransport()
    client = MCPClient(cfg, transport=fake)
    # model_construct 跳过 validator，模拟"外部手动构造绕过校验"场景
    info = MCPToolInfo.model_construct(name="bad name")
    with pytest.raises(ValueError) as exc_info:
        MCPAgentTool(server_name="srv", client=client, tool_info=info)
    assert "tool name" in str(exc_info.value).lower() or "name" in str(exc_info.value).lower()


# ============================================================================
# 5. timeout 后 MCPClient 自动 close；后续调用报 closed
# ============================================================================


@pytest.mark.asyncio
async def test_timeout_closes_client_and_subsequent_calls_fail() -> None:
    """transport.receive 永久挂住时，timeout 后 client 应该 close；
    后续 _request 立即报 client closed（而非再次 timeout）。"""
    frozen: asyncio.Future = asyncio.Future()

    class HangingTransport(FakeMCPTransport):
        async def receive(self) -> dict[str, Any]:
            return await frozen  # 永远挂住

    cfg = _stdio_cfg(timeout_s=0.1)
    client = MCPClient(cfg, transport=HangingTransport(handler=_echo_handler))
    await client.connect()

    # 第一次：timeout
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "timeout" in str(exc_info.value).lower()

    # 第二次：client 已 close，应该立即报 closed（不再尝试 transport）
    with pytest.raises(MCPProtocolError) as exc2:
        await client.initialize()
    assert "closed" in str(exc2.value).lower()

    frozen.cancel()


# ============================================================================
# 6. MCPClient close 后禁止再 connect
# ============================================================================


@pytest.mark.asyncio
async def test_connect_after_close_raises() -> None:
    fake = FakeMCPTransport(handler=_echo_handler)
    client = MCPClient(_stdio_cfg(), transport=fake)
    await client.connect()
    await client.close()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.connect()
    assert "after close" in str(exc_info.value).lower()


# ============================================================================
# 7. list_tools 遇到非 dict item 严格报错
# ============================================================================


@pytest.mark.asyncio
async def test_list_tools_non_dict_item_raises() -> None:
    async def handler(msg):
        if msg["method"] == "tools/list":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"tools": ["bad string entry", {"name": "ok"}]},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(_stdio_cfg(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.list_tools()
    assert "must be an object" in str(exc_info.value).lower() or "must be" in str(exc_info.value)
    await client.close()


# ============================================================================
# 8. tool_validation 对非法 schema 包成 ToolArgumentValidationError
# ============================================================================


def test_validate_invalid_json_schema_wrapped() -> None:
    """schema 本身非法（type 不是合法类型）→ ToolArgumentValidationError，
    不让 jsonschema.SchemaError 穿透到 loop 兜底。"""
    bad_schema = {
        "type": "not-a-real-type",
        "properties": {"x": {"type": "string"}},
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t", schema=bad_schema, arguments={"x": "y"},
        )
    assert "invalid JSON Schema" in str(exc_info.value)


def test_validate_invalid_schema_format() -> None:
    """schema properties 不是合法 schema → ToolArgumentValidationError。"""
    bad_schema = {
        "type": "object",
        "properties": {"x": {"type": 123}},  # type 必须是 string
    }
    with pytest.raises(ToolArgumentValidationError) as exc_info:
        validate_tool_arguments(
            tool_name="t", schema=bad_schema, arguments={"x": "y"},
        )
    assert "invalid JSON Schema" in str(exc_info.value)


# ============================================================================
# 9. MCPAgentTool details.raw 体积超限时截断
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_agent_tool_truncates_large_raw() -> None:
    """raw 字符串超过 max_raw_chars 时截断，raw_truncated=True。"""
    big_text = "X" * 5000  # > DEFAULT_MAX_RAW_CHARS=4000

    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": big_text}],
                    "_meta": {"full": big_text},
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
        max_raw_chars=100,  # 故意调小，便于测试
    )
    await client.connect()
    result = await tool.execute("c1", {})
    assert result.details["raw_truncated"] is True
    # raw 长度被限制在 ~100 + 截断标记
    assert len(result.details["raw"]) < 200
    assert "[truncated]" in result.details["raw"]
    await client.close()


@pytest.mark.asyncio
async def test_mcp_agent_tool_small_raw_not_truncated() -> None:
    """raw 没超限时 raw_truncated=False。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"content": [{"type": "text", "text": "small"}]},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("c1", {})
    assert result.details["raw_truncated"] is False
    await client.close()


# ============================================================================
# 10. R3 硬化：send timeout / framing error / timeout_s 校验 / raw=None
# ============================================================================


@pytest.mark.asyncio
async def test_send_timeout_also_closes_client() -> None:
    """send timeout（不止 receive timeout）也应该 close client。

    send 卡住说明 transport 不可靠；后续请求需要立即看到 client closed。
    """
    class HangingSendTransport(FakeMCPTransport):
        async def send(self, message: dict[str, Any]) -> None:
            await asyncio.Future()  # 永远挂住

    cfg = _stdio_cfg(timeout_s=0.1)
    client = MCPClient(cfg, transport=HangingSendTransport(handler=_echo_handler))
    await client.connect()

    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "send timeout" in str(exc_info.value).lower()
    assert "closed" in str(exc_info.value).lower()

    # 后续请求立即报 closed（client 已 close）
    with pytest.raises(MCPProtocolError) as exc2:
        await client.initialize()
    assert "closed" in str(exc2.value).lower()


@pytest.mark.asyncio
async def test_response_id_mismatch_closes_client() -> None:
    """response id mismatch 表示 stream 已错位——close client。

    后续请求不应继续在错位的 stream 上跑。
    """
    async def bad_handler(msg):
        return {"jsonrpc": "2.0", "id": msg["id"] + 999, "result": {}}

    fake = FakeMCPTransport(handler=bad_handler)
    client = MCPClient(_stdio_cfg(), transport=fake)
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "id mismatch" in str(exc_info.value).lower()
    assert "closed" in str(exc_info.value).lower()

    # 后续请求立即报 closed
    with pytest.raises(MCPProtocolError) as exc2:
        await client.list_tools()
    assert "closed" in str(exc2.value).lower()


@pytest.mark.asyncio
async def test_jsonrpc_error_does_not_close_client() -> None:
    """server 返回 JSON-RPC error（如 tools/call tool not found）不 close client。

    这种情况下 transport 仍然健康（一问一答配对完整）——client 可继续用于其它请求。
    """
    call_count = {"n": 0}

    async def handler(msg):
        if msg["method"] == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32602, "message": "tool not found: ghost"},
            }
        # tools/list 返回空——证明 client 仍可用
        call_count["n"] += 1
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}}

    fake = FakeMCPTransport(handler=handler)
    client = MCPClient(_stdio_cfg(), transport=fake)
    await client.connect()
    await client.initialize()

    # 第一次：tools/call tool not found（不 close）
    with pytest.raises(Exception):  # noqa: B017 — MCPToolNotFoundError 也行
        await client.call_tool("ghost", {})

    # client 仍可用——能继续调 list_tools
    tools = await client.list_tools()
    assert tools == []
    await client.close()


def test_timeout_s_must_be_positive() -> None:
    """timeout_s <= 0 → MCPServerConfig 校验失败。"""
    with pytest.raises(ValueError) as exc_info:
        MCPServerConfig(name="srv", transport="stdio", command="x", timeout_s=0)
    assert "timeout_s" in str(exc_info.value)

    with pytest.raises(ValueError):
        MCPServerConfig(name="srv", transport="stdio", command="x", timeout_s=-1)


@pytest.mark.asyncio
async def test_mcp_agent_tool_raw_serialized_as_json() -> None:
    """raw 用 json.dumps 序列化（稳定 JSON，前端友好）。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "_meta": {"a": 1, "b": "two"},
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("c1", {})
    # raw 是 JSON 字符串（含双引号、不带 unicode 转义）
    raw = result.details["raw"]
    assert isinstance(raw, str)
    assert raw.startswith("{") and raw.endswith("}")
    assert '"_meta"' in raw
    await client.close()


@pytest.mark.asyncio
async def test_mcp_agent_tool_include_raw_false_omits_raw_field() -> None:
    """include_raw=False 时 details 不写 raw / raw_truncated 字段。

    生产场景若不想把 server 原始 result 持久化进 session/snapshot，
    可设 include_raw=False；其它 metadata 仍然保留。
    """
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "_meta": {"secret": "should-not-leak"},
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
        include_raw=False,
    )
    await client.connect()
    result = await tool.execute("c1", {})

    # raw 字段不写入
    assert "raw" not in result.details
    assert "raw_truncated" not in result.details
    # 其它 metadata 仍在
    assert result.details["source"] == "mcp"
    assert result.details["content_count"] == 1
    # raw 内容不会出现在 details 里
    assert "should-not-leak" not in str(result.details)
    await client.close()


# ============================================================================
# 11. R4 收紧：字符集 / 长度 / refresh_tools 状态同步 / 非 dict result / is_error raw
# ============================================================================


def test_dot_in_tool_name_rejected() -> None:
    """评审 R4：tool name 含 `.` 不允许（provider tool name 限制）。"""
    with pytest.raises(ValidationError):
        MCPToolInfo(name="read.file")


def test_dot_in_server_name_rejected() -> None:
    """server name 含 `.` 不允许（保持工具名 provider-safe）。"""
    with pytest.raises(ValueError):
        MCPServerConfig(name="my.server", transport="stdio", command="x")


def test_make_namespaced_tool_name_enforces_length() -> None:
    """namespaced name 超过 64 字符 → ValueError。"""
    from pi_agent_core_py.mcp import make_namespaced_tool_name
    long_server = "s" * 30
    long_tool = "t" * 30
    with pytest.raises(ValueError) as exc_info:
        make_namespaced_tool_name(long_server, long_tool)
    assert "too long" in str(exc_info.value).lower()


def test_make_namespaced_tool_name_legal() -> None:
    from pi_agent_core_py.mcp import make_namespaced_tool_name
    name = make_namespaced_tool_name("srv_1", "echo-tool")
    assert name == "mcp__srv_1__echo-tool"


@pytest.mark.asyncio
async def test_refresh_tools_failure_syncs_state_and_removes_client() -> None:
    """refresh_tools 失败：state.connected=False + client 从 _clients 移除。"""
    import pi_agent_core_py.mcp.client as cm
    original = cm._build_default_transport

    def fake_builder(cfg):
        return FakeMCPTransport(handler=_echo_handler)

    cm._build_default_transport = fake_builder
    try:
        registry = MCPRegistry([_stdio_cfg()])
        await registry.connect_all()

        # 破坏：让 client.list_tools 抛错——通过 monkey-patch 单个 client
        client = registry._clients["srv"]  # noqa: SLF001
        async def boom():
            raise RuntimeError("list_tools boom")
        client.list_tools = boom  # type: ignore[method-assign]

        await registry.refresh_tools()
        state = registry.list_servers()[0]
        assert state.connected is False
        assert state.tool_count == 0
        assert "RuntimeError" in (state.last_error or "")
        # client 被从 _clients 移除
        assert "srv" not in registry._clients  # noqa: SLF001
        await registry.close_all()
    finally:
        cm._build_default_transport = original


@pytest.mark.asyncio
async def test_non_dict_result_raises_protocol_error_and_closes() -> None:
    """result 非 dict（如 list/string）→ MCPProtocolError + close client。

    不再包成 {"_raw": result} 掩盖错误。
    """
    async def handler(msg):
        if msg["method"] == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": ["not", "an", "object"],  # ← 非 dict
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    await client.connect()
    with pytest.raises(MCPProtocolError) as exc_info:
        await client.initialize()
    assert "must be object" in str(exc_info.value).lower()
    # client 已 close
    assert client.closed is True


@pytest.mark.asyncio
async def test_is_error_branch_also_attaches_raw() -> None:
    """server 返回 isError=true 时，details 仍保留 raw（debug 需要）。"""
    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                    "_meta": {"error_code": "E1"},
                },
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="t", description="d"),
    )
    await client.connect()
    result = await tool.execute("c1", {})
    assert result.is_error is True
    assert result.details["error_type"] == "MCPToolCallError"
    # is_error 分支也写了 raw（_meta 字段）
    assert "raw" in result.details
    assert "error_code" in result.details["raw"]
    await client.close()


def test_mcp_client_closed_property() -> None:
    """MCPClient.closed 属性供 registry / harness 判断连接状态。"""
    cfg = _stdio_cfg()
    fake = FakeMCPTransport(handler=_echo_handler)
    client = MCPClient(cfg, transport=fake)
    assert client.closed is False


# ============================================================================
# 12. R5：transport 层 MCPError 后 client 必须 close
# ============================================================================


@pytest.mark.asyncio
async def test_transport_send_mcp_error_closes_client() -> None:
    """transport.send 抛 MCPError（如 MCPTransportClosedError）→ client 必须 close。

    场景：stdio transport stdin 已关，send 时抛 MCPTransportClosedError。
    若 client 不 close，registry 会看到 client.closed=False 但 transport 不可用。
    """
    from pi_agent_core_py import MCPTransportClosedError

    class BrokenSendTransport(FakeMCPTransport):
        async def send(self, message: dict[str, Any]) -> None:
            raise MCPTransportClosedError("stdin pipe closed")

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=BrokenSendTransport(handler=_echo_handler))
    await client.connect()

    with pytest.raises(MCPTransportClosedError):
        await client.initialize()

    # 关键：client 也被 close，registry 看到状态一致
    assert client.closed is True

    # 后续请求立即报 client closed（不再次触发 transport）
    with pytest.raises(MCPProtocolError) as exc2:
        await client.initialize()
    assert "client closed" in str(exc2.value).lower()


@pytest.mark.asyncio
async def test_transport_receive_mcp_error_closes_client() -> None:
    """transport.receive 抛 MCPProtocolError（invalid JSON / empty line）→ close client。

    场景：StdioMCPTransport.receive 解析空行/非法 JSON 时抛 MCPProtocolError。
    client 必须在边界捕获并 close。
    """
    from pi_agent_core_py import MCPProtocolError as _MCPProtocolError

    class BrokenReceiveTransport(FakeMCPTransport):
        async def receive(self) -> dict[str, Any]:
            raise _MCPProtocolError("invalid JSON line from server")

    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=BrokenReceiveTransport(handler=_echo_handler))
    await client.connect()

    with pytest.raises(_MCPProtocolError):
        await client.initialize()

    # client 已 close
    assert client.closed is True

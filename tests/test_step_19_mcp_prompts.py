"""Step 19 — MCP prompts/list / prompts/get + MCPRegistry + Adapter 测试。

覆盖：
- MCPClient.list_prompts：成功 / result 格式错误 / prompt 名非法
- MCPClient.get_prompt：成功 / text + 非 text content / include_raw
- MCPRegistry.refresh_prompts：单 server 成功 / 单 server 失败不影响其它
- MCPRegistry.list_prompts：返回 [(server, info)]
- MCPRegistry.close_all：清 prompts cache
- MCPPromptSkillAdapter.load_prompt_as_skill：基础 / metadata / 不存在的 server
- MCPPromptSkillAdapter.load_all_prompts_as_skills：批量 / 单失败被吞
- make_mcp_prompt_skill_name：长度 + 字符集校验
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    MCPClient,
    MCPPromptArgument,
    MCPPromptInfo,
    MCPPromptMessage,
    MCPPromptResult,
    MCPPromptSkillAdapter,
    MCPProtocolError,
    MCPRegistry,
    MCPServerConfig,
    make_mcp_prompt_skill_name,
)
from pi_agent_core_py.mcp import FakeMCPTransport

# ============================================================================
# Fake handler 工厂
# ============================================================================


def make_handler(
    *,
    prompts_list: list[dict[str, Any]] | None = None,
    prompts_get: dict[str, Any] | None = None,
    error_for_method: str | None = None,
    error_response: dict[str, Any] | None = None,
):
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        base: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}

        if error_for_method and method == error_for_method and error_response:
            base["error"] = error_response
            return base

        if method == "initialize":
            base["result"] = {"serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            base["result"] = {"tools": []}
        elif method == "prompts/list":
            base["result"] = {"prompts": prompts_list or []}
        elif method == "prompts/get":
            base["result"] = prompts_get or {
                "description": "default",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "hi"}},
                ],
            }
        else:
            base["error"] = {
                "code": -32601,
                "message": f"method not found: {method}",
            }
        return base

    return handler


def make_stdio_config(name: str = "srv") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="stdio", command="placeholder-binary", args=[],
    )


# ============================================================================
# MCPClient.list_prompts
# ============================================================================


@pytest.mark.asyncio
async def test_list_prompts_returns_prompt_infos() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_list=[
        {
            "name": "summarize",
            "description": "Summarize text",
            "arguments": [
                {"name": "topic", "description": "what to summarize", "required": True},
            ],
        },
        {"name": "translate", "description": "Translate"},
    ]))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    prompts = await client.list_prompts()
    assert len(prompts) == 2
    assert isinstance(prompts[0], MCPPromptInfo)
    assert prompts[0].name == "summarize"
    assert prompts[0].description == "Summarize text"
    assert len(prompts[0].arguments) == 1
    assert isinstance(prompts[0].arguments[0], MCPPromptArgument)
    assert prompts[0].arguments[0].required is True
    await client.close()


@pytest.mark.asyncio
async def test_list_prompts_missing_prompts_field_raises() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_list=None))
    # Hack: prompts/list 返回 {} ——通过 handler 重写 result
    async def h(msg):
        if msg.get("method") == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"foo": "bar"}}
        if msg.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    fake = FakeMCPTransport(handler=h)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPProtocolError):
        await client.list_prompts()
    await client.close()


@pytest.mark.asyncio
async def test_list_prompts_invalid_name_raises() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_list=[
        {"name": "bad.name.with.dots", "description": ""},
    ]))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPProtocolError):
        await client.list_prompts()
    await client.close()


@pytest.mark.asyncio
async def test_list_prompts_non_list_raises() -> None:
    async def h(msg):
        if msg.get("method") == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"prompts": "not-a-list"}}
        if msg.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    fake = FakeMCPTransport(handler=h)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPProtocolError):
        await client.list_prompts()
    await client.close()


# ============================================================================
# MCPClient.get_prompt
# ============================================================================


@pytest.mark.asyncio
async def test_get_prompt_parses_text_content() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_get={
        "description": "summarize something",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "Please summarize X"}},
            {"role": "assistant", "content": "Sure"},
        ],
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.get_prompt("summarize", arguments={"topic": "X"})
    assert isinstance(result, MCPPromptResult)
    assert result.description == "summarize something"
    assert len(result.messages) == 2
    assert isinstance(result.messages[0], MCPPromptMessage)
    assert result.messages[0].role == "user"
    assert result.messages[0].content == "Please summarize X"
    assert result.messages[1].content == "Sure"
    await client.close()


@pytest.mark.asyncio
async def test_get_prompt_string_content_works() -> None:
    """MCP spec 允许 content 直接是 str。"""
    fake = FakeMCPTransport(handler=make_handler(prompts_get={
        "messages": [
            {"role": "user", "content": "plain string content"},
        ],
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.get_prompt("x")
    assert result.messages[0].content == "plain string content"
    await client.close()


@pytest.mark.asyncio
async def test_get_prompt_non_text_content_placeholder() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_get={
        "messages": [
            {"role": "user", "content": {"type": "image", "data": "bin"}},
        ],
    }))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.get_prompt("x")
    assert "unsupported content type: 'image'" in result.messages[0].content
    await client.close()


@pytest.mark.asyncio
async def test_get_prompt_include_raw() -> None:
    raw_result = {
        "description": "d",
        "messages": [{"role": "user", "content": "x"}],
        "extra_field": "value",
    }
    fake = FakeMCPTransport(handler=make_handler(prompts_get=raw_result))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result_default = await client.get_prompt("x")
    assert result_default.raw is None
    # 新 client 重新读
    fake2 = FakeMCPTransport(handler=make_handler(prompts_get=raw_result))
    client2 = MCPClient(make_stdio_config(), transport=fake2)
    await client2.connect()
    await client2.initialize()
    result_with_raw = await client2.get_prompt("x", include_raw=True)
    assert result_with_raw.raw is not None
    assert result_with_raw.raw.get("extra_field") == "value"
    await client.close()
    await client2.close()


@pytest.mark.asyncio
async def test_get_prompt_returns_empty_when_no_messages() -> None:
    fake = FakeMCPTransport(handler=make_handler(prompts_get={"description": "d"}))
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    result = await client.get_prompt("x")
    assert result.messages == []
    await client.close()


@pytest.mark.asyncio
async def test_get_prompt_non_list_messages_raises_protocol_error() -> None:
    """P1 修订：messages 非 list 必须抛 MCPProtocolError，不再静默返回空。"""
    async def h(msg):
        if msg.get("method") == "prompts/get":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {
                "messages": "not-a-list",
            }}
        if msg.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    fake = FakeMCPTransport(handler=h)
    client = MCPClient(make_stdio_config(), transport=fake)
    await client.connect()
    await client.initialize()
    with pytest.raises(MCPProtocolError):
        await client.get_prompt("x")
    await client.close()


# ============================================================================
# MCPRegistry.refresh_prompts / list_prompts / close_all
# ============================================================================


def _make_registry_with_handlers(handler_map: dict[str, Any]) -> MCPRegistry:
    configs = [make_stdio_config(name) for name in handler_map]
    def factory(cfg: MCPServerConfig) -> MCPClient:
        return MCPClient(cfg, transport=FakeMCPTransport(handler=handler_map[cfg.name]))
    return MCPRegistry(configs, client_factory=factory)


@pytest.mark.asyncio
async def test_registry_refresh_prompts_single_server() -> None:
    reg = _make_registry_with_handlers({
        "srv": make_handler(prompts_list=[
            {"name": "summarize", "description": "d"},
        ]),
    })
    await reg.connect_all()
    infos = await reg.refresh_prompts()
    assert len(infos) == 1
    assert infos[0][0] == "srv"
    assert infos[0][1].name == "summarize"
    # list_prompts 也返回
    cached = reg.list_prompts()
    assert len(cached) == 1
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_refresh_prompts_multi_server_partial_failure() -> None:
    """一个 server prompts/list 失败不应影响其它。

    P0 修订：JSON-RPC method-level error（如 -32603 "boom"）不会让 client.closed=True
    ——MCPClient 只对 framing / timeout / transport closed 错误 close client。
    因此 refresh_prompts 现在保留 connected=True 和 tools cache，只清 prompts cache。

    P1 修订（本轮）：软失败时**只**写 state.metadata["prompts_last_error"]，
    **不动** state.last_error——避免 UI 把"可选 prompts capability 失败"误读成
    "server 整体失败"。state.last_error 是 tools/transport 级状态，由
    refresh_tools / connect_all 维护。
    """
    ok_handler = make_handler(prompts_list=[{"name": "a", "description": ""}])
    err_handler = make_handler(error_for_method="prompts/list", error_response={
        "code": -32603, "message": "boom",
    })
    reg = _make_registry_with_handlers({"ok": ok_handler, "err": err_handler})
    await reg.connect_all()
    infos = await reg.refresh_prompts()
    assert len(infos) == 1
    assert infos[0][0] == "ok"
    err_state = reg.get_state("err")
    assert err_state is not None
    # P1 修订核心断言：软失败不动 state.last_error / state.connected
    assert err_state.last_error is None
    assert err_state.connected is True
    # prompts_last_error 仍记录在 metadata 中
    assert "prompts_last_error" in err_state.metadata
    assert "boom" in err_state.metadata["prompts_last_error"]
    # client 仍可用——后续可继续调 tools/list 等
    assert reg.get_client("err") is not None
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_refresh_prompts_framing_error_disconnects() -> None:
    """Framing / transport 错误时（client.closed=True），refresh_prompts
    应该 disconnect 并清理 client。"""
    async def framing_err_handler(msg):
        # 返回非 dict response，触发 MCPProtocolError + client.close
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        # prompts/list 返回缺 jsonrpc / id 的破 dict（triggers framing close）
        return {"result": {"prompts": []}}  # 故意不写 jsonrpc / id
    reg = _make_registry_with_handlers({"srv": framing_err_handler})
    await reg.connect_all()
    await reg.refresh_prompts()
    state = reg.get_state("srv")
    assert state is not None
    # framing error 后 client.closed=True → registry 应已 disconnect
    assert state.connected is False
    assert reg.get_client("srv") is None
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_close_all_clears_prompts_cache() -> None:
    reg = _make_registry_with_handlers({
        "srv": make_handler(prompts_list=[{"name": "p", "description": ""}]),
    })
    await reg.connect_all()
    await reg.refresh_prompts()
    assert len(reg.list_prompts()) == 1
    await reg.close_all()
    assert reg.list_prompts() == []


@pytest.mark.asyncio
async def test_registry_refresh_prompts_does_not_affect_tools() -> None:
    """refresh_prompts 不应清掉 tools 缓存。"""
    async def handler(msg):
        if msg.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        if msg.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "t", "description": "d", "inputSchema": {"type": "object"}},
            ]}}
        if msg.get("method") == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"prompts": [
                {"name": "p", "description": ""},
            ]}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
    reg = _make_registry_with_handlers({"srv": handler})
    await reg.connect_all()
    await reg.refresh_tools()
    assert len(reg.list_tools()) == 1
    await reg.refresh_prompts()
    assert len(reg.list_tools()) == 1  # tools cache 仍在
    assert len(reg.list_prompts()) == 1
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_get_prompt_unknown_server_raises() -> None:
    reg = _make_registry_with_handlers({"srv": make_handler()})
    await reg.connect_all()
    with pytest.raises(ValueError):
        await reg.get_prompt("unknown", "p")
    await reg.close_all()


@pytest.mark.asyncio
async def test_registry_get_client_returns_live_client() -> None:
    reg = _make_registry_with_handlers({"srv": make_handler()})
    await reg.connect_all()
    assert reg.get_client("srv") is not None
    assert reg.get_client("missing") is None
    await reg.close_all()


# ============================================================================
# MCPPromptSkillAdapter
# ============================================================================


@pytest.mark.asyncio
async def test_adapter_load_prompt_as_skill_basic() -> None:
    reg = _make_registry_with_handlers({
        "srv": make_handler(
            prompts_list=[{
                "name": "summarize",
                "description": "Summarize text",
                "arguments": [{"name": "topic", "required": True}],
            }],
            prompts_get={
                "description": "summarize",
                "messages": [
                    {"role": "system", "content": "be concise"},
                    {"role": "user", "content": "summarize this"},
                ],
            },
        ),
    })
    await reg.connect_all()
    await reg.refresh_prompts()
    adapter = MCPPromptSkillAdapter(reg)
    skill = await adapter.load_prompt_as_skill(
        server_name="srv", prompt_name="summarize",
    )
    assert skill.name == "mcp_prompt__srv__summarize"
    assert "summarize" in skill.description.lower()
    assert skill.metadata["source"] == "mcp_prompt"
    assert skill.metadata["server"] == "srv"
    assert skill.metadata["prompt"] == "summarize"
    assert skill.metadata["loader"] == "MCPPromptSkillAdapter"
    # system message 优先
    assert "be concise" in skill.prompt.template
    assert "summarize this" in skill.prompt.template
    assert skill.tags == ["mcp", "prompt", "srv"]
    await reg.close_all()


@pytest.mark.asyncio
async def test_adapter_load_prompt_unknown_server_raises() -> None:
    reg = _make_registry_with_handlers({"srv": make_handler()})
    await reg.connect_all()
    adapter = MCPPromptSkillAdapter(reg)
    with pytest.raises(ValueError):
        await adapter.load_prompt_as_skill(server_name="missing", prompt_name="x")
    await reg.close_all()


@pytest.mark.asyncio
async def test_adapter_load_all_prompts_skips_failures() -> None:
    """load_all_prompts_as_skills 中单个失败不应影响其它。"""
    ok_handler = make_handler(
        prompts_list=[{"name": "a", "description": "da"}],
        prompts_get={"messages": [{"role": "user", "content": "a"}]},
    )
    # bad_handler 不再使用——改用 failing_get_handler 显式模拟 prompts/get 失败。
    # 保留注释说明历史上的 bad_handler 设计为什么不行。
    # （见下 failing_get_handler：它直接返回 error，制造可控的失败路径。）

    async def failing_get_handler(msg):
        if msg.get("method") == "prompts/get":
            return {"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32603, "message": "boom",
            }}
        if msg.get("method") == "prompts/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"prompts": [
                {"name": "b", "description": "db"},
            ]}}
        if msg.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    reg = _make_registry_with_handlers({
        "ok": ok_handler,
        "bad": failing_get_handler,
    })
    await reg.connect_all()
    await reg.refresh_prompts()
    adapter = MCPPromptSkillAdapter(reg)
    skills = await adapter.load_all_prompts_as_skills()
    # ok server 应该成功；bad server 失败被吞掉
    names = [s.name for s in skills]
    assert "mcp_prompt__ok__a" in names
    # bad server 的 prompt 不应该出现
    assert all("mcp_prompt__bad__" not in n for n in names)
    # P1 修订：失败被记录到 adapter.last_errors
    assert len(adapter.last_errors) == 1
    err = adapter.last_errors[0]
    assert err["server"] == "bad"
    assert err["prompt"] == "b"
    assert "boom" in err["error"] or "MCPProtocolError" in err["error"]
    await reg.close_all()


@pytest.mark.asyncio
async def test_adapter_load_prompt_with_prompt_info_arg_avoids_extra_list() -> None:
    """P2 修订：传入 prompt_info 参数时应跳过内部 list_prompts 查询。"""
    call_log: list[str] = []

    async def handler(msg):
        method = msg.get("method")
        req_id = msg.get("id")
        call_log.append(method)
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": "x", "description": "X"},
            ]}}
        if method == "prompts/get":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "messages": [{"role": "user", "content": "body"}],
            }}
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    reg = _make_registry_with_handlers({"srv": handler})
    await reg.connect_all()
    adapter = MCPPromptSkillAdapter(reg)

    # 直接传 prompt_info——不应触发 prompts/list
    info = MCPPromptInfo(name="x", description="X")
    skill = await adapter.load_prompt_as_skill(
        server_name="srv", prompt_name="x", prompt_info=info,
    )
    assert skill.name == "mcp_prompt__srv__x"
    assert "prompts/list" not in call_log  # P2 优化生效
    assert "prompts/get" in call_log
    await reg.close_all()


@pytest.mark.asyncio
async def test_adapter_load_all_prompts_passes_cached_info() -> None:
    """load_all_prompts_as_skills 应该传 prompt_info，避免 N 次 prompts/list。"""
    list_calls = {"count": 0}

    async def handler(msg):
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "prompts/list":
            list_calls["count"] += 1
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": "a", "description": "A"},
                {"name": "b", "description": "B"},
                {"name": "c", "description": "C"},
            ]}}
        if method == "prompts/get":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "messages": [{"role": "user", "content": "x"}],
            }}
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    reg = _make_registry_with_handlers({"srv": handler})
    await reg.connect_all()
    await reg.refresh_prompts()
    list_calls["count"] = 0  # reset 后只数 load_all 内部的 list

    adapter = MCPPromptSkillAdapter(reg)
    skills = await adapter.load_all_prompts_as_skills()
    assert len(skills) == 3
    # P2 修订：3 个 prompt 加载，不应再触发任何 prompts/list
    assert list_calls["count"] == 0
    await reg.close_all()


# ============================================================================
# make_mcp_prompt_skill_name
# ============================================================================


def test_make_mcp_prompt_skill_name_basic() -> None:
    assert make_mcp_prompt_skill_name("srv", "prompt") == "mcp_prompt__srv__prompt"


def test_make_mcp_prompt_skill_name_invalid_chars() -> None:
    with pytest.raises(ValueError):
        make_mcp_prompt_skill_name("sr.v", "prompt")
    with pytest.raises(ValueError):
        make_mcp_prompt_skill_name("srv", "pr.ompt")


def test_make_mcp_prompt_skill_name_too_long() -> None:
    long_name = "a" * 200
    with pytest.raises(ValueError):
        make_mcp_prompt_skill_name("srv", long_name)

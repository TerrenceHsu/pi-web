"""Step 16 — Loop + MCP 集成测试（端到端，无真实网络）。

覆盖：
- MCPAgentTool 注册进 ToolRegistry 后，FakeClient 发 ToolCallEvent 能执行该 MCP tool
- ToolResultMessage 能进入下一轮 LLM
- FakeClient.last_tools 能看到 MCP tool definition
- ToolExecutionStartEvent / ToolExecutionEndEvent 顺序保持
- 参数校验失败时 loop 正常收敛（不调 execute、不调 after_tool_call）
- before_tool_call 改参后按新参数校验
- 多工具中一个校验失败不影响另一个
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    DoneEvent,
    FakeClient,
    MCPAgentTool,
    MCPClient,
    MCPServerConfig,
    MCPToolInfo,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    ToolRegistry,
    ToolResultMessage,
    Usage,
    run_event_loop,
)
from pi_agent_core_py.hooks import (
    AfterToolCallContext,
    BeforeToolCallContext,
    BeforeToolCallResult,
)

# ============================================================================
# 测试 fixture
# ============================================================================


ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def _make_echo_handler(*, echo_prefix: str = "echoed: ") -> Any:
    """构造 handler：tools/call 时把 args.text 加前缀返回。"""
    async def handler(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": [{
                    "name": "echo",
                    "description": "Echo input text",
                    "inputSchema": ECHO_SCHEMA,
                }]},
            }
        if method == "tools/call":
            params = msg.get("params", {})
            text = (params.get("arguments") or {}).get("text", "")
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"{echo_prefix}{text}"}],
                    "isError": False,
                },
            }
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    return handler


def _make_echo_mcp_tool() -> MCPAgentTool:
    """构造一个带 FakeMCPTransport 的 echo MCPAgentTool（已 connect）。"""
    from pi_agent_core_py.mcp import FakeMCPTransport

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    fake = FakeMCPTransport(handler=_make_echo_handler())
    client = MCPClient(cfg, transport=fake)
    info = MCPToolInfo(name="echo", description="Echo input text", input_schema=ECHO_SCHEMA)
    return MCPAgentTool(server_name="srv", client=client, tool_info=info)


# ============================================================================
# 1. MCPAgentTool 端到端集成
# ============================================================================


@pytest.mark.asyncio
async def test_loop_executes_mcp_tool_and_feeds_back() -> None:
    """FakeClient 第一轮发 ToolCallEvent 调 mcp tool → 第二轮最终文本。"""
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    fake_llm = FakeClient([
        [   # turn 1: LLM 决定调 mcp__srv__echo
            ToolCallEvent(tool_call=ToolCall(
                id="call_1", name="mcp__srv__echo",
                arguments={"text": "hi"},
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [   # turn 2: 基于工具结果给最终文本
            TextDeltaEvent(delta="最终回答"),
            DoneEvent(stop_reason="stop", usage=Usage()),
        ],
    ])

    types_seen: list[str] = []
    agent_end: AgentEndEvent | None = None

    async for ev in run_event_loop(
        system_prompt="x", user_text="echo hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
    ):
        types_seen.append(ev.type)
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert types_seen[0] == "agent_start"
    assert types_seen[-1] == "agent_end"
    assert "tool_execution_start" in types_seen
    assert "tool_execution_end" in types_seen

    # messages: [user, assistant(toolcall), toolResult, assistant(text)]
    roles = [m.role for m in agent_end.messages]
    assert roles == ["user", "assistant", "toolResult", "assistant"]

    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.name == "mcp__srv__echo"
    assert tr.content[0].text == "echoed: hi"
    assert tr.details["source"] == "mcp"
    assert tr.details["server"] == "srv"
    assert tr.details["mcp_tool"] == "echo"

    await mcp_tool.client.close()


@pytest.mark.asyncio
async def test_loop_passes_mcp_tool_definition_to_llm() -> None:
    """FakeClient.last_tools 应该看到 MCP tool definition。"""
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    fake_llm = FakeClient([[
        TextDeltaEvent(delta="no tool call"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]])
    async for _ in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
    ):
        pass

    assert fake_llm.last_tools is not None
    names = [t.name for t in fake_llm.last_tools]
    assert "mcp__srv__echo" in names
    # schema 正确传递
    defn = next(t for t in fake_llm.last_tools if t.name == "mcp__srv__echo")
    assert defn.parameters["required"] == ["text"]
    await mcp_tool.client.close()


@pytest.mark.asyncio
async def test_loop_mcp_tool_execution_order() -> None:
    """工具生命周期先 end，随后才发布最终 toolResult message。"""
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    fake_llm = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id="c1", name="mcp__srv__echo", arguments={"text": "x"},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    types_seen: list[str] = []
    async for ev in run_event_loop(
        system_prompt="x", user_text="x",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
    ):
        types_seen.append(ev.type)

    tool_start = types_seen.index("tool_execution_start")
    msg_start_after = types_seen.index("message_start", tool_start + 1)
    msg_end_after = types_seen.index("message_end", tool_start + 1)
    tool_end = types_seen.index("tool_execution_end")

    assert tool_start < tool_end < msg_start_after < msg_end_after
    await mcp_tool.client.close()


# ============================================================================
# 2. 参数校验失败路径
# ============================================================================


@pytest.mark.asyncio
async def test_loop_validation_failure_does_not_call_execute() -> None:
    """参数校验失败时，tool.execute 不应该被调用。

    实现：让 mcp_tool.client.transport 记录 sent，校验失败时 sent 不应有 tools/call。
    """
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    fake_llm = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id="c1", name="mcp__srv__echo",
            # 缺 required text 字段
            arguments={},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="recovered"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert tr.details["error_type"] == "ToolArgumentValidationError"
    assert tr.details["validation"]["tool_name"] == "mcp__srv__echo"
    assert "text" in tr.details["validation"]["message"]  # required 字段名

    # MCP transport 不应被调用（execute 没跑）
    sent_methods = [m.get("method") for m in mcp_tool.client.transport.sent]
    assert "tools/call" not in sent_methods
    await mcp_tool.client.close()


@pytest.mark.asyncio
async def test_loop_validation_failure_does_not_call_after_hook() -> None:
    """校验失败时不调 after_tool_call。

    实现：注入计数 after_tool_call，校验失败后计数应为 0。"""
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    after_call_count = {"n": 0}

    async def counting_after(ctx: AfterToolCallContext):
        after_call_count["n"] += 1
        return ctx.result

    fake_llm = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id="c1", name="mcp__srv__echo", arguments={},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    async for _ in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
        after_tool_call=counting_after,
    ):
        pass

    assert after_call_count["n"] == 0
    await mcp_tool.client.close()


@pytest.mark.asyncio
async def test_loop_before_hook_changes_args_validated_against_new_args() -> None:
    """before_tool_call 改 args 后，按新 args 校验。

    场景：LLM 发空 args，before 补上 text="from-before"。新 args 通过校验。
    """
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    async def patching_before(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
        return BeforeToolCallResult(
            allow=True,
            tool_call=ToolCall(
                id=ctx.tool_call.id,
                name=ctx.tool_call.name,
                arguments={"text": "from-before"},
            ),
        )

    fake_llm = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id="c1", name="mcp__srv__echo", arguments={},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
        before_tool_call=patching_before,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is False
    assert tr.content[0].text == "echoed: from-before"
    await mcp_tool.client.close()


@pytest.mark.asyncio
async def test_loop_validation_failure_one_tool_does_not_affect_others() -> None:
    """多工具 batch：一个校验失败不影响其它工具执行。

    场景：FakeClient 一轮发 2 个 ToolCall：
      - mcp__srv__echo 合法（text="ok"）
      - mcp__srv__echo 不合法（缺 text）
    合法的应该执行成功；不合法的应该返回 is_error=True。
    """
    mcp_tool = _make_echo_mcp_tool()
    await mcp_tool.client.connect()

    fake_llm = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="c1", name="mcp__srv__echo", arguments={"text": "ok"},
            )),
            ToolCallEvent(tool_call=ToolCall(
                id="c2", name="mcp__srv__echo", arguments={},  # 缺 required
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([mcp_tool]),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tool_msgs = [m for m in agent_end.messages if isinstance(m, ToolResultMessage)]
    assert len(tool_msgs) == 2

    by_id = {m.tool_call_id: m for m in tool_msgs}
    assert by_id["c1"].is_error is False
    assert by_id["c1"].content[0].text == "echoed: ok"
    assert by_id["c2"].is_error is True
    assert by_id["c2"].details["error_type"] == "ToolArgumentValidationError"
    await mcp_tool.client.close()


# ============================================================================
# 3. MCPAgentTool 异常在 loop 中被正确兜底
# ============================================================================


@pytest.mark.asyncio
async def test_loop_mcp_protocol_error_translated_to_tool_result() -> None:
    """MCP server 返回 JSON-RPC error → MCPAgentTool 转 is_error=True ToolResult → loop 继续。"""
    from pi_agent_core_py.mcp import FakeMCPTransport

    async def handler(msg):
        if msg["method"] == "tools/call":
            return {
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": "internal error"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    client = MCPClient(cfg, transport=FakeMCPTransport(handler=handler))
    tool = MCPAgentTool(
        server_name="srv", client=client,
        tool_info=MCPToolInfo(name="echo", description="d", input_schema=ECHO_SCHEMA),
    )
    await client.connect()

    fake_llm = FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id="c1", name="mcp__srv__echo", arguments={"text": "x"},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="recovered"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])

    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake_llm, tools=ToolRegistry([tool]),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert tr.details["error_type"] == "MCPProtocolError"
    # loop 仍然进入第二轮
    assert len(fake_llm.all_messages_calls) == 2
    await client.close()

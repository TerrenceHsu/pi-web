"""Step 4 — Tool 基础模型 单元测试（对照用户清单 5.1–5.8）。

覆盖：
5.1 ToolCall 创建测试
5.2 ToolResult 创建测试
5.3 AgentTool 创建测试（含 execution_mode）
5.4 ToolRegistry 注册测试（register / get / has / names / list）
5.5 重复注册测试（抛 ToolRegistrationError）
5.6 不存在工具测试（抛 ToolNotFoundError）
5.7 unregister 测试
5.8 Step 3 不被破坏（事件顺序 / convert_to_llm 过滤 CustomMessage
    / ModelClient.stream 只接 LLMMessage）
"""

from __future__ import annotations

import pytest

from pi_agent_core_py import (
    AgentTool,
    AssistantMessage,
    CustomMessage,
    DoneEvent,
    FakeClient,
    LLMUserMessage,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolDef,
    ToolExecutionMode,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolResult,
    Usage,
    UserMessage,
    convert_to_llm,
    run_event_loop,
)

# ============================================================================
# 5.1 ToolCall 创建测试
# ============================================================================


def test_tool_call_with_arguments() -> None:
    tc = ToolCall(id="call_1", name="echo", arguments={"text": "hi"})
    assert tc.type == "toolCall"
    assert tc.id == "call_1"
    assert tc.name == "echo"
    assert tc.arguments == {"text": "hi"}


def test_tool_call_raw_can_be_empty() -> None:
    """arguments 默认 {}。"""
    tc = ToolCall(id="c1", name="ping")
    assert tc.arguments == {}


def test_tool_call_raw_field() -> None:
    """raw 字段保留 provider 原始 tool_use 数据（可选）。"""
    # 默认 None
    tc = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    assert tc.raw is None

    # 显式传 raw
    raw = {"provider": "anthropic", "index": 0, "input": {"text": "hi"}}
    tc2 = ToolCall(id="c2", name="echo", arguments={"text": "hi"}, raw=raw)
    assert tc2.raw == raw


# ============================================================================
# 5.2 ToolResult 创建测试
# ============================================================================


def test_tool_result_full_fields() -> None:
    r = ToolResult(
        tool_call_id="c1",
        name="echo",
        content=[TextContent(text="hi")],
    )
    assert r.tool_call_id == "c1"
    assert r.name == "echo"
    assert r.content[0].text == "hi"
    assert r.is_error is False  # 默认
    assert r.terminate is False  # 默认
    assert r.details == {}  # 默认


def test_tool_result_error_and_terminate() -> None:
    r = ToolResult(
        tool_call_id="c1",
        name="flaky",
        content=[TextContent(text="timeout")],
        is_error=True,
        terminate=True,
        details={"reason": "timeout", "duration_ms": 5000},
    )
    assert r.is_error is True
    assert r.terminate is True
    assert r.details["duration_ms"] == 5000


# ============================================================================
# 5.3 AgentTool 创建测试（含 execution_mode）
# ============================================================================


class _Echo(AgentTool):
    name = "echo"
    label = "Echo"
    description = "echo back"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, tool_call_id, args):
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=args.get("text", ""))],
        )


class _SequentialEcho(AgentTool):
    name = "seq_echo"
    label = "Seq Echo"
    description = "echo but sequential"
    parameters = {"type": "object", "properties": {}}
    execution_mode: ToolExecutionMode = "sequential"  # 覆盖默认

    async def execute(self, tool_call_id, args):
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[],
        )


def test_agent_tool_class_attrs() -> None:
    t = _Echo()
    assert t.name == "echo"
    assert t.description == "echo back"
    assert "text" in t.parameters["properties"]


def test_agent_tool_execution_mode_default_parallel() -> None:
    assert _Echo().execution_mode == "parallel"


def test_agent_tool_execution_mode_can_be_sequential() -> None:
    assert _SequentialEcho().execution_mode == "sequential"


def test_agent_tool_definition() -> None:
    d = _Echo().definition
    assert isinstance(d, ToolDef)
    assert d.name == "echo"
    assert d.label == "Echo"


def test_agent_tool_definition_requires_name() -> None:
    class _NoName(AgentTool):
        async def execute(self, tool_call_id, args):
            return ToolResult(tool_call_id=tool_call_id, name="x", content=[])

    with pytest.raises(ToolRegistrationError):
        _ = _NoName().definition


@pytest.mark.asyncio
async def test_agent_tool_execute() -> None:
    r = await _Echo().execute("c1", {"text": "hi"})
    assert r.tool_call_id == "c1"
    assert r.name == "echo"
    assert r.content[0].text == "hi"


# ============================================================================
# 5.4 ToolRegistry 注册测试
# ============================================================================


def test_registry_register_then_get() -> None:
    reg = ToolRegistry()
    echo = _Echo()
    reg.register(echo)

    # register 后可以 get
    got = reg.get("echo")
    assert got is echo


def test_registry_has() -> None:
    reg = ToolRegistry([_Echo()])
    assert reg.has("echo") is True
    assert reg.has("nope") is False


def test_registry_names() -> None:
    reg = ToolRegistry([_Echo(), _SequentialEcho()])
    assert reg.names() == ["echo", "seq_echo"]


def test_registry_list() -> None:
    reg = ToolRegistry([_Echo()])
    tools = reg.list()
    assert len(tools) == 1
    assert tools[0].name == "echo"


def test_registry_definitions() -> None:
    reg = ToolRegistry([_Echo()])
    defs = reg.definitions()
    assert len(defs) == 1
    assert defs[0].name == "echo"


# ============================================================================
# 5.5 重复注册测试
# ============================================================================


def test_registry_duplicate_raises() -> None:
    reg = ToolRegistry([_Echo()])
    with pytest.raises(ToolRegistrationError):
        reg.register(_Echo())


def test_registry_empty_name_raises() -> None:
    class _NoName(AgentTool):
        async def execute(self, tool_call_id, args):
            return ToolResult(tool_call_id=tool_call_id, name="x", content=[])

    reg = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        reg.register(_NoName())


def test_registry_empty_description_raises() -> None:
    """description 也必须非空（LLM 需要描述来选工具）。"""

    class _NoDesc(AgentTool):
        name = "no_desc"
        description = ""  # 空

        async def execute(self, tool_call_id, args):
            return ToolResult(tool_call_id=tool_call_id, name="no_desc", content=[])

    reg = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        reg.register(_NoDesc())


# ============================================================================
# 5.6 不存在工具测试
# ============================================================================


def test_registry_get_missing_raises() -> None:
    reg = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        reg.get("ghost")


def test_registry_get_missing_after_some_registered() -> None:
    reg = ToolRegistry([_Echo()])
    assert reg.get("echo").name == "echo"
    with pytest.raises(ToolNotFoundError):
        reg.get("ghost")


# ============================================================================
# 5.7 unregister 测试
# ============================================================================


def test_registry_unregister() -> None:
    reg = ToolRegistry([_Echo()])
    assert reg.has("echo")

    reg.unregister("echo")
    assert reg.has("echo") is False
    with pytest.raises(ToolNotFoundError):
        reg.get("echo")


def test_registry_unregister_missing_is_silent() -> None:
    """unregister 不存在的 name 应静默（不抛错）。"""
    reg = ToolRegistry()
    reg.unregister("ghost")  # 不抛


# ============================================================================
# 5.8 Step 3 不被破坏
# ============================================================================


@pytest.mark.asyncio
async def test_step3_event_sequence_unchanged() -> None:
    """Step 4 不改 run_event_loop 的事件顺序。"""
    fake = FakeClient(
        [
            [
                TextDeltaEvent(delta="hi"),
                DoneEvent(stop_reason="stop", usage=Usage()),
            ]
        ]
    )
    types = []
    async for ev in run_event_loop(
        system_prompt="x",
        user_text="hi",
        client=fake,
    ):
        types.append(ev.type)

    assert types == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",  # user
        "message_start",  # assistant (empty partial)
        "message_update",  # text_start
        "message_update",  # text_delta
        "message_update",  # text_end
        "message_end",  # assistant final
        "turn_end",
        "agent_end",
    ]


def test_step3_convert_to_llm_filters_custom() -> None:
    """CustomMessage 默认过滤；ToolResultMessage 还未引入，convert_to_llm 不应抛错。"""
    msgs = [
        UserMessage(content=[TextContent(text="hi")]),
        CustomMessage(custom_type="note", content="invisible"),
    ]
    out = convert_to_llm(msgs)
    assert len(out) == 1
    assert isinstance(out[0], LLMUserMessage)


@pytest.mark.asyncio
async def test_step3_model_client_receives_llm_messages_only() -> None:
    """ModelClient.stream 仍然只接收 LLMMessage；CustomMessage 不会渗透。"""
    fake = FakeClient([[DoneEvent(stop_reason="stop", usage=Usage())]])

    async def inject_custom(messages):
        return list(messages) + [CustomMessage(custom_type="x", content="y")]

    async for _ in run_event_loop(
        system_prompt="x",
        user_text="hi",
        client=fake,
        transform_context_fn=inject_custom,
    ):
        pass

    for m in fake.last_messages:
        assert not isinstance(m, CustomMessage)


def test_convert_to_llm_filters_tool_call_in_assistant_content() -> None:
    """AssistantMessage.content 含 ToolCall 时，convert_to_llm 只保留 TextContent。

    防止 ToolCall 通过 LLMMessage 边界泄漏给 ModelClient。
    """
    from pi_agent_core_py import LLMAssistantMessage

    am = AssistantMessage(
        content=[
            TextContent(text="calling echo"),
            ToolCall(id="c1", name="echo", arguments={"text": "hi"}),
            TextContent(text="done"),
        ],
        api="x",
        provider="y",
        model="z",
    )
    out = convert_to_llm([am])
    assert len(out) == 1
    assert isinstance(out[0], LLMAssistantMessage)
    # 只保留 TextContent，ToolCall 被过滤
    assert len(out[0].content) == 2
    assert all(c.text in ("calling echo", "done") for c in out[0].content)

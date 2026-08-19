"""Step 21 — Anthropic-compatible message / tool 转换。

覆盖：
- LLMUserMessage → user role + text block
- LLMAssistantMessage text → assistant text block
- LLMToolResultMessage → user/tool_result block
- 多个 tool_result 合并到同一 user message
- ToolDef → Anthropic tools schema
- 空 tools 返回 []
"""
from __future__ import annotations

from pi_agent_core_py import (
    LLMAssistantMessage,
    LLMToolResultMessage,
    LLMUserMessage,
    TextContent,
    ThinkingContent,
    ToolDef,
    to_anthropic_messages,
    to_anthropic_tools,
)


def _u(text: str) -> LLMUserMessage:
    return LLMUserMessage(content=[TextContent(text=text)])


def _a(text: str) -> LLMAssistantMessage:
    return LLMAssistantMessage(content=[TextContent(text=text)])


def _tr(tool_call_id: str, name: str, text: str, *, is_error: bool = False) -> LLMToolResultMessage:
    return LLMToolResultMessage(
        tool_call_id=tool_call_id,
        name=name,
        content=[TextContent(text=text)],
        is_error=is_error,
    )


# ============================================================================
# to_anthropic_messages
# ============================================================================


def test_user_message_becomes_user_text_block() -> None:
    out = to_anthropic_messages([_u("hello")])
    assert out == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]


def test_assistant_message_becomes_assistant_text_block() -> None:
    out = to_anthropic_messages([_a("sure")])
    assert out == [
        {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
    ]


def test_signed_thinking_replays_as_anthropic_thinking_block() -> None:
    message = LLMAssistantMessage(
        content=[
            ThinkingContent(
                thinking="reason",
                thinking_signature="signed-payload",
            ),
            TextContent(text="answer"),
        ]
    )

    out = to_anthropic_messages([message])

    assert out[0]["content"] == [
        {
            "type": "thinking",
            "thinking": "reason",
            "signature": "signed-payload",
        },
        {"type": "text", "text": "answer"},
    ]


def test_unsigned_thinking_falls_back_to_assistant_text() -> None:
    message = LLMAssistantMessage(content=[ThinkingContent(thinking="reason")])

    out = to_anthropic_messages([message])

    assert out[0]["content"] == [{"type": "text", "text": "reason"}]


def test_redacted_thinking_replays_as_opaque_anthropic_block() -> None:
    message = LLMAssistantMessage(
        content=[
            ThinkingContent(
                thinking="",
                thinking_signature="opaque-encrypted-payload",
                redacted=True,
            )
        ]
    )

    out = to_anthropic_messages([message])

    assert out[0]["content"] == [
        {"type": "redacted_thinking", "data": "opaque-encrypted-payload"}
    ]


def test_tool_result_appended_to_previous_user_message() -> None:
    """LLMToolResultMessage 应合并到上一个 user message 的 content list。"""
    out = to_anthropic_messages([
        _u("please echo"),
        _tr("t1", "echo", "echo: please echo"),
    ])
    assert len(out) == 1
    assert out[0]["role"] == "user"
    content = out[0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "tool_result"
    assert content[1]["tool_use_id"] == "t1"
    assert content[1]["content"] == "echo: please echo"
    assert content[1]["is_error"] is False


def test_tool_result_creates_user_message_if_no_prior_user() -> None:
    """没有前置 user message 时，tool_result 自己开一个 user。"""
    out = to_anthropic_messages([_tr("t1", "echo", "ok")])
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"][0]["type"] == "tool_result"


def test_multiple_tool_results_merge_into_one_user() -> None:
    """连续多个 tool_result 都合并到同一个 user message。"""
    out = to_anthropic_messages([
        _u("run them"),
        _tr("t1", "a", "ra"),
        _tr("t2", "b", "rb"),
    ])
    assert len(out) == 1
    content = out[0]["content"]
    # text + 2 tool_result
    assert content[0]["type"] == "text"
    tool_results = [c for c in content if c["type"] == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[0]["tool_use_id"] == "t1"
    assert tool_results[1]["tool_use_id"] == "t2"


def test_tool_result_is_error_propagated() -> None:
    out = to_anthropic_messages([
        _u("go"),
        _tr("t1", "fail", "boom", is_error=True),
    ])
    block = out[0]["content"][1]
    assert block["is_error"] is True


def test_full_conversation_order_preserved() -> None:
    out = to_anthropic_messages([
        _u("q1"),
        _a("a1"),
        _u("q2"),
        _a("a2"),
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]


# ============================================================================
# to_anthropic_tools
# ============================================================================


def test_tools_empty_returns_empty_list() -> None:
    assert to_anthropic_tools([]) == []


def test_tool_def_maps_to_anthropic_schema() -> None:
    """ToolDef → {name, description, input_schema}。"""
    tools = [
        ToolDef(
            name="echo",
            label="echo",
            description="Echo back the provided text.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
    ]
    out = to_anthropic_tools(tools)
    assert out == [
        {
            "name": "echo",
            "description": "Echo back the provided text.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    ]


def test_tool_def_empty_parameters_filled_with_default_object_schema() -> None:
    """parameters={} 时填默认 {"type":"object","properties":{}}。"""
    t1 = ToolDef(name="x", label="x", description="d", parameters={})
    out = to_anthropic_tools([t1])
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}


def test_multiple_tools_preserve_order() -> None:
    tools = [
        ToolDef(name="a", label="a", description="da", parameters={}),
        ToolDef(name="b", label="b", description="db", parameters={}),
        ToolDef(name="c", label="c", description="dc", parameters={}),
    ]
    out = to_anthropic_tools(tools)
    assert [t["name"] for t in out] == ["a", "b", "c"]

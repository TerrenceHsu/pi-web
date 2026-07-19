"""to_openai_messages 转换测试（M1-1 §十五.Messages）.

覆盖：
- system / user / assistant text / assistant tool_call / tool result
- tool-call ID / tool name 保留
- tool arguments 序列化为紧凑 JSON
- tool result 关联原 tool_call_id
- 空 system prompt 不插入
- 不支持消息类型抛 ProviderProtocolError（不含正文）
- 转换不修改输入
"""
from __future__ import annotations

import copy
import json as _json

import pytest

from pi_agent_core_py.llm_messages import (
    LLMAssistantMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from pi_agent_core_py.messages import TextContent, ToolCall
from pi_agent_core_py.providers.errors import ProviderProtocolError
from pi_agent_core_py.providers.openai_compat import to_openai_messages


def _user(text: str) -> LLMUserMessage:
    return LLMUserMessage(content=[TextContent(text=text)])


def _assistant(text: str = "", *, tool_calls: list[ToolCall] | None = None) -> LLMAssistantMessage:
    return LLMAssistantMessage(
        content=[TextContent(text=text)] if text else [],
        tool_calls=tool_calls or [],
        provider="openai_compat",
        model="qwen-plus",
    )


def _tool_call(call_id: str, name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args or {})


def _tool_result(
    call_id: str, name: str, text: str, *, is_error: bool = False,
) -> LLMToolResultMessage:
    return LLMToolResultMessage(
        tool_call_id=call_id,
        name=name,
        content=[TextContent(text=text)],
        is_error=is_error,
    )


# ============================================================================
# system prompt
# ============================================================================


def test_system_prompt_inserted_as_first_message() -> None:
    out = to_openai_messages(system_prompt="be helpful", messages=[_user("hi")])
    assert out[0] == {"role": "system", "content": "be helpful"}


def test_empty_system_prompt_omitted() -> None:
    out = to_openai_messages(system_prompt="", messages=[_user("hi")])
    assert all(m["role"] != "system" for m in out)


def test_whitespace_system_prompt_omitted() -> None:
    out = to_openai_messages(system_prompt="   ", messages=[_user("hi")])
    assert all(m["role"] != "system" for m in out)


# ============================================================================
# user text
# ============================================================================


def test_user_text_becomes_user_message() -> None:
    out = to_openai_messages(system_prompt="", messages=[_user("hello")])
    assert out[0] == {"role": "user", "content": "hello"}


def test_user_multiple_text_blocks_joined() -> None:
    msg = LLMUserMessage(content=[TextContent(text="foo"), TextContent(text="bar")])
    out = to_openai_messages(system_prompt="", messages=[msg])
    assert out[0] == {"role": "user", "content": "foobar"}


# ============================================================================
# assistant text
# ============================================================================


def test_assistant_text_becomes_assistant_message() -> None:
    out = to_openai_messages(system_prompt="", messages=[_assistant("ok")])
    assert out[0] == {"role": "assistant", "content": "ok"}


# ============================================================================
# assistant tool_calls
# ============================================================================


def test_assistant_with_tool_calls_emits_tool_calls_array() -> None:
    tc = _tool_call("t1", "echo", {"text": "hi"})
    msg = _assistant("calling tool", tool_calls=[tc])
    out = to_openai_messages(system_prompt="", messages=[msg])
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "calling tool"
    assert len(out[0]["tool_calls"]) == 1
    payload = out[0]["tool_calls"][0]
    assert payload["id"] == "t1"
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "echo"
    # arguments 是紧凑 JSON string
    args_str = payload["function"]["arguments"]
    assert isinstance(args_str, str)
    assert _json.loads(args_str) == {"text": "hi"}


def test_assistant_tool_calls_without_text_content_is_none() -> None:
    """OpenAI 协议：assistant w/ tool_calls 时 content 可为 None."""
    tc = _tool_call("t1", "echo")
    msg = _assistant("", tool_calls=[tc])
    out = to_openai_messages(system_prompt="", messages=[msg])
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["id"] == "t1"


def test_assistant_tool_call_arguments_compact_json() -> None:
    """紧凑 JSON：无多余空格."""
    tc = _tool_call("t1", "echo", {"a": 1, "b": "x"})
    msg = _assistant(tool_calls=[tc])
    out = to_openai_messages(system_prompt="", messages=[msg])
    args_str = out[0]["tool_calls"][0]["function"]["arguments"]
    # {"a":1,"b":"x"} 不含空格
    assert " " not in args_str


def test_assistant_tool_call_missing_id_raises_protocol_error() -> None:
    tc = ToolCall(id="", name="echo", arguments={})
    msg = _assistant(tool_calls=[tc])
    with pytest.raises(ProviderProtocolError):
        to_openai_messages(system_prompt="", messages=[msg])


def test_assistant_tool_call_missing_name_raises_protocol_error() -> None:
    tc = ToolCall(id="t1", name="", arguments={})
    msg = _assistant(tool_calls=[tc])
    with pytest.raises(ProviderProtocolError):
        to_openai_messages(system_prompt="", messages=[msg])


# ============================================================================
# tool result
# ============================================================================


def test_tool_result_becomes_tool_message_with_tool_call_id() -> None:
    out = to_openai_messages(
        system_prompt="",
        messages=[_tool_result("t1", "echo", "result-text")],
    )
    assert out[0] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "result-text",
    }


def test_tool_result_missing_tool_call_id_raises_protocol_error() -> None:
    msg = LLMToolResultMessage(tool_call_id="", name="echo", content=[TextContent(text="x")])
    with pytest.raises(ProviderProtocolError):
        to_openai_messages(system_prompt="", messages=[msg])


def test_tool_result_associates_with_prior_assistant_tool_call_id() -> None:
    """完整 multi-turn：assistant tool_call → tool_result 配对."""
    tc = _tool_call("t1", "echo", {"x": 1})
    msgs = [
        _user("do echo"),
        _assistant("", tool_calls=[tc]),
        _tool_result("t1", "echo", "ok"),
    ]
    out = to_openai_messages(system_prompt="", messages=msgs)
    # assistant tool_call 的 id 与 tool_result.tool_call_id 一致
    assistant_msg = next(m for m in out if m["role"] == "assistant")
    tool_msg = next(m for m in out if m["role"] == "tool")
    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]


# ============================================================================
# 转换不修改输入
# ============================================================================


def test_to_openai_messages_does_not_mutate_input() -> None:
    msgs = [_user("hi"), _assistant("ok")]
    msgs_before = copy.deepcopy(msgs)
    to_openai_messages(system_prompt="x", messages=msgs)
    assert msgs == msgs_before


def test_to_openai_messages_does_not_mutate_tool_calls() -> None:
    tc = _tool_call("t1", "echo", {"a": 1})
    tc_before = copy.deepcopy(tc)
    msg = _assistant(tool_calls=[tc])
    to_openai_messages(system_prompt="", messages=[msg])
    assert tc == tc_before


# ============================================================================
# 不支持的 message 类型
# ============================================================================


def test_unsupported_message_type_raises_protocol_error_without_payload() -> None:
    """unknown message type → ProviderProtocolError with fixed text (no payload)."""

    class _Foreign:
        role = "summary"

    with pytest.raises(ProviderProtocolError) as exc_info:
        to_openai_messages(system_prompt="", messages=[_Foreign()])  # type: ignore[arg-type]
    msg = str(exc_info.value)
    # 不含原文 / 正文
    assert "summary" not in msg

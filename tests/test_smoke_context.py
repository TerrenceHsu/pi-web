"""Smoke test 3: Context 转换 (AgentMessage → LLMMessage)。

重点：
- user / assistant / tool result 的 role 映射
- system prompt 由 run_event_loop 单独传，不在 messages 里
- tool_call 在 LLMAssistantMessage 中被过滤
- 空列表不崩
- P0-3：FileBlock 在 LLM 边界转成文本说明（不发 image block / 不发 path）
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.context import convert_to_llm
from pi_agent_core_py.llm_messages import (
    LLMAssistantMessage,
    LLMToolResultMessage,
    LLMUserMessage,
)
from pi_agent_core_py.messages import (
    AssistantMessage,
    CustomMessage,
    FileBlock,
    SummaryMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def test_user_message_maps_to_llm_user() -> None:
    msgs = [UserMessage(content=[TextContent(text="hi")])]
    out = convert_to_llm(msgs)
    assert len(out) == 1
    assert isinstance(out[0], LLMUserMessage)
    assert out[0].content[0].text == "hi"


def test_assistant_message_text_only_kept() -> None:
    """AssistantMessage 的 TextContent 应保留。"""
    a = AssistantMessage(
        content=[TextContent(text="hello")],
        api="x", provider="x", model="x",
    )
    out = convert_to_llm([a])
    assert isinstance(out[0], LLMAssistantMessage)
    assert out[0].content[0].text == "hello"


def test_assistant_message_tool_call_filtered() -> None:
    """AssistantMessage.content 中的 ToolCall 块在 LLM 边界被过滤。

    历史调用信息在多轮对话中靠 ToolResultMessage 重建（provider 适配器
    在更底层把它转成 tool_use / tool_result 配对）。
    """
    tc = ToolCall(id="t1", name="echo", arguments={"q": "hi"})
    a = AssistantMessage(
        content=[TextContent(text="ok"), tc],
        api="x", provider="x", model="x",
    )
    out = convert_to_llm([a])
    assert isinstance(out[0], LLMAssistantMessage)
    # 仅保留 TextContent
    assert len(out[0].content) == 1
    assert isinstance(out[0].content[0], TextContent)


def test_tool_result_message_maps_to_llm_tool_result() -> None:
    tr = ToolResultMessage(
        tool_call_id="t1",
        name="echo",
        content=[TextContent(text="ok")],
    )
    out = convert_to_llm([tr])
    assert isinstance(out[0], LLMToolResultMessage)
    assert out[0].tool_call_id == "t1"


def test_tool_result_terminate_details_dropped_at_llm_boundary() -> None:
    """LLMToolResultMessage 不应携带 terminate / details（agent 内部字段）。"""
    tr = ToolResultMessage(
        tool_call_id="t1",
        name="echo",
        content=[TextContent(text="ok")],
        terminate=True,
        details={"k": "v"},
    )
    out = convert_to_llm([tr])
    assert not hasattr(out[0], "terminate")
    assert not hasattr(out[0], "details")


def test_custom_message_filtered() -> None:
    """CustomMessage 不发给 LLM。"""
    msgs: list = [
        UserMessage(content=[TextContent(text="hi")]),
        CustomMessage(custom_type="note", content="secret"),
    ]
    out = convert_to_llm(msgs)
    assert len(out) == 1
    assert isinstance(out[0], LLMUserMessage)


def test_summary_message_becomes_user_with_prefix() -> None:
    """SummaryMessage → LLMUserMessage（带 [Conversation Summary] 前缀）。"""
    s = SummaryMessage(content=[TextContent(text="旧对话内容...")])
    out = convert_to_llm([s])
    assert isinstance(out[0], LLMUserMessage)
    assert "[Conversation Summary]" in out[0].content[0].text


def test_empty_messages_returns_empty_list() -> None:
    assert convert_to_llm([]) == []


def test_round_trip_preserves_role_order() -> None:
    """多消息顺序应保留。"""
    msgs = [
        UserMessage(content=[TextContent(text="q")]),
        AssistantMessage(
            content=[TextContent(text="a")],
            api="x", provider="x", model="x",
        ),
        ToolResultMessage(tool_call_id="t1", name="echo"),
        AssistantMessage(
            content=[TextContent(text="final")],
            api="x", provider="x", model="x",
        ),
    ]
    out = convert_to_llm(msgs)
    assert len(out) == 4
    assert [type(m).__name__ for m in out] == [
        "LLMUserMessage",
        "LLMAssistantMessage",
        "LLMToolResultMessage",
        "LLMAssistantMessage",
    ]


def test_run_event_loop_requires_user_text_or_initial_messages() -> None:
    """run_event_loop 必须传 user_text 或 initial_messages，否则 ValueError。"""
    import asyncio

    from pi_agent_core_py.loop import run_event_loop
    from pi_agent_core_py.model_client import FakeClient

    async def run() -> None:
        with pytest.raises(ValueError):
            async for _ in run_event_loop(
                system_prompt="x",
                client=FakeClient([]),
            ):
                pass

    asyncio.run(run())


# ============================================================================
# P0-3：FileBlock 转换
# ============================================================================


def test_fileblock_converted_to_text_in_llm_user_message() -> None:
    """UserMessage 含 FileBlock → LLMUserMessage 多条 TextContent。"""
    msg = UserMessage(content=[
        TextContent(text="look"),
        FileBlock(
            file_id="f1", name="a.csv", mime="text/csv",
            size=10, sha256="x", format="csv",
        ),
    ])
    out = convert_to_llm([msg])
    assert isinstance(out[0], LLMUserMessage)
    # 两段：原 text + fileblock 描述
    assert len(out[0].content) == 2
    assert out[0].content[0].text == "look"
    desc = out[0].content[1].text
    assert "a.csv" in desc
    assert "view_file" in desc


def test_fileblock_image_unsupported_converted_to_unsupported_text() -> None:
    """图片 FileBlock → LLM 文本说明明确不支持图片。"""
    msg = UserMessage(content=[
        FileBlock(
            file_id="f1", name="img.png", mime="image/png",
            size=1, sha256="x", format="image_unsupported",
        ),
    ])
    out = convert_to_llm([msg])
    text = out[0].content[0].text
    assert "不支持图片" in text
    # 不应包含 image block（只发 text）
    for c in out[0].content:
        assert isinstance(c, TextContent)


def test_fileblock_text_does_not_expose_path() -> None:
    """FileBlock 描述文本不应暴露 path。"""
    msg = UserMessage(content=[
        FileBlock(
            file_id="f1", name="a.csv", mime="text/csv",
            size=1, sha256="x", format="csv",
        ),
    ])
    out = convert_to_llm([msg])
    text = out[0].content[0].text
    assert "path" not in text.lower()


def test_old_text_only_user_message_does_not_regress() -> None:
    """回归：纯 TextContent UserMessage 转换仍正常。"""
    msg = UserMessage(content=[TextContent(text="hi")])
    out = convert_to_llm([msg])
    assert isinstance(out[0], LLMUserMessage)
    assert len(out[0].content) == 1
    assert out[0].content[0].text == "hi"

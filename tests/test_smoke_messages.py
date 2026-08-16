"""Smoke test 2: Message 模型基础契约。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from pi_agent_core_py.messages import (
    AssistantMessage,
    CustomMessage,
    FileBlock,
    GenerationMetrics,
    Message,
    SummaryMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def test_user_message_role_and_default_factory() -> None:
    """UserMessage.role 固定 user，content 默认空 list，timestamp 自动填。"""
    msg = UserMessage(content=[TextContent(text="hi")])
    assert msg.role == "user"
    assert len(msg.content) == 1
    assert msg.content[0].text == "hi"
    assert msg.timestamp > 0


def test_assistant_message_minimal_required_fields() -> None:
    """AssistantMessage 必须显式提供 api / provider / model（避免 None 漂移）。"""
    msg = AssistantMessage(
        content=[TextContent(text="hello")],
        api="anthropic-messages",
        provider="glm",
        model="glm-4.5-flash",
    )
    assert msg.role == "assistant"
    assert msg.stop_reason == "stop"
    assert msg.error_message is None
    assert msg.generation_metrics is None


def test_assistant_generation_metrics_round_trip() -> None:
    msg = AssistantMessage(
        content=[TextContent(text="hello")],
        api="anthropic-messages",
        provider="glm",
        model="glm-4.5-flash",
        generation_metrics=GenerationMetrics(
            latency_ms=125,
            time_to_first_token_ms=42,
            usage_available=True,
        ),
    )
    restored = AssistantMessage.model_validate(msg.model_dump(mode="json"))
    assert restored.generation_metrics is not None
    assert restored.generation_metrics.latency_ms == 125
    assert restored.generation_metrics.time_to_first_token_ms == 42
    assert restored.generation_metrics.usage_available is True


def test_assistant_message_missing_provider_raises() -> None:
    """api / provider / model 是必填——缺失应抛 ValidationError。"""
    with pytest.raises(ValidationError):
        AssistantMessage(content=[TextContent(text="x")])


def test_tool_call_default_arguments_empty_dict() -> None:
    """ToolCall.arguments 默认 {}（不是 None）。"""
    tc = ToolCall(id="t1", name="echo")
    assert tc.arguments == {}


def test_tool_result_message_round_trip() -> None:
    """ToolResultMessage model_dump → model_validate 类型保留。"""
    msg = ToolResultMessage(
        tool_call_id="t1",
        name="echo",
        content=[TextContent(text="ok")],
        is_error=False,
        terminate=False,
    )
    data = msg.model_dump()
    restored = ToolResultMessage.model_validate(data)
    assert isinstance(restored, ToolResultMessage)
    assert restored.tool_call_id == "t1"
    assert restored.role == "toolResult"


def test_message_discriminator_round_trip() -> None:
    """Message union 通过 role 判别能正确恢复子类。"""
    from pydantic import TypeAdapter

    adapter = TypeAdapter(Message)
    msgs = [
        UserMessage(content=[TextContent(text="hi")]),
        AssistantMessage(
            content=[TextContent(text="hello")],
            api="anthropic-messages",
            provider="glm",
            model="glm-4.5-flash",
        ),
        ToolResultMessage(tool_call_id="t1", name="echo"),
    ]
    for m in msgs:
        data = m.model_dump()
        restored = adapter.validate_python(data)
        assert type(restored) is type(m)


def test_summary_message_default_type() -> None:
    """SummaryMessage.summary_type 默认 context_compaction。"""
    msg = SummaryMessage(content=[TextContent(text="summary")])
    assert msg.role == "summary"
    assert msg.summary_type == "context_compaction"


def test_custom_message_role() -> None:
    """CustomMessage 默认不发给 LLM，role=custom。"""
    cm = CustomMessage(custom_type="ui_note", content="drawn in ui")
    assert cm.role == "custom"
    assert cm.display is False


# ============================================================================
# P0-3：FileBlock round trip
# ============================================================================


def test_fileblock_in_user_message_round_trip() -> None:
    """UserMessage 含 FileBlock → model_dump → model_validate 类型保留。"""
    msg = UserMessage(content=[
        TextContent(text="hi"),
        FileBlock(
            file_id="f1", name="a.csv", mime="text/csv",
            size=10, sha256="abc", format="csv",
        ),
    ])
    data = msg.model_dump()
    restored = UserMessage.model_validate(data)
    assert isinstance(restored.content[1], FileBlock)
    assert restored.content[1].file_id == "f1"
    assert restored.content[1].format == "csv"


def test_fileblock_default_format_unsupported() -> None:
    """未指定 format 默认 unsupported。"""
    fb = FileBlock(
        file_id="f1", name="x", mime="application/x-strange",
        size=1, sha256="x",
    )
    assert fb.format == "unsupported"


def test_fileblock_image_unsupported_format() -> None:
    """图片文件以 image_unsupported 标记。"""
    fb = FileBlock(
        file_id="f1", name="a.png", mime="image/png",
        size=1, sha256="x", format="image_unsupported",
    )
    assert fb.format == "image_unsupported"


def test_user_message_with_only_text_still_works() -> None:
    """回归：旧 [TextContent] 入参不受 FileBlock 引入影响。"""
    msg = UserMessage(content=[TextContent(text="hello")])
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], TextContent)

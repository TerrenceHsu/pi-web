"""LLMMessage 类型——传给 ModelClient 的边界消息（Step 5 扩展）。

Step 5 新增 `LLMToolResultMessage`：让工具结果能进入下一轮 LLM 调用。

LLMMessage union:
    LLMUserMessage | LLMAssistantMessage | LLMToolResultMessage

注意：LLMToolResultMessage 没有 `terminate` 和 `details` 字段——它们是 agent
内部语义，不发给 LLM。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .messages import TextContent, ToolCall, Usage


class LLMUserMessage(BaseModel):
    """发给 LLM 的 user 消息。"""
    role: Literal["user"] = "user"
    content: list[TextContent] = Field(default_factory=list)
    timestamp: int = 0


class LLMAssistantMessage(BaseModel):
    """发给 LLM 的 assistant 消息（多轮对话的历史回放）。

    content 仅含 TextContent；Step 21 修复：新增 `tool_calls` 字段保留
    AssistantMessage 的 ToolCall 列表，让 provider 适配器能重建 tool_use
    block（Anthropic 协议要求 tool_result 必须配对前一条 assistant 的
    tool_use，否则 multi-turn tool 调用会失败）。
    """
    role: Literal["assistant"] = "assistant"
    content: list[TextContent] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    api: str = ""
    provider: str = ""
    model: str = ""
    stop_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    timestamp: int = 0


class LLMToolResultMessage(BaseModel):
    """发给 LLM 的工具结果消息（Step 5 新增）。

    对应 LLM 协议里的 tool_result block（Anthropic） / tool role（OpenAI）。
    字段比 ToolResultMessage 少 terminate / details——这两者是 agent 内部语义。
    """
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    name: str
    content: list[TextContent] = Field(default_factory=list)
    is_error: bool = False
    timestamp: int = 0


LLMMessage = Annotated[
    LLMUserMessage | LLMAssistantMessage | LLMToolResultMessage,
    Field(discriminator="role"),
]


__all__ = [
    "LLMUserMessage", "LLMAssistantMessage", "LLMToolResultMessage", "LLMMessage",
]

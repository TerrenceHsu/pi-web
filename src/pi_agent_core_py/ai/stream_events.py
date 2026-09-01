"""Provider-neutral stream event models.

把 stream event 定义从 `model_client.py` 拆出来的目的：避免
`model_client` ↔ `providers/*` 之间的循环 import——
`providers/base.py` / `providers/anthropic_compat.py` / `providers/fake.py`
都需要引用 StreamEvent 类型。

`model_client.py` / `__init__.py` 重新导出全部类型。文本、思考和工具调用均有
start/delta/end 生命周期；旧 delta-only 与整块 ToolCall 事件继续兼容。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .messages import ToolCall, Usage


class StreamEvent(BaseModel):
    """所有 stream 事件的基类。"""


class TextStartEvent(StreamEvent):
    """A text content block started at ``content_index``."""

    type: Literal["text_start"] = "text_start"
    content_index: int


class TextDeltaEvent(StreamEvent):
    """文本增量。"""

    type: Literal["text_delta"] = "text_delta"
    delta: str
    # ``None`` keeps delta-only Fake/custom providers source-compatible.  The
    # agent loop assigns an index and synthesizes start/end around such streams.
    content_index: int | None = None


class TextEndEvent(StreamEvent):
    """A text content block completed with its canonical full content."""

    type: Literal["text_end"] = "text_end"
    content_index: int
    content: str


class ThinkingStartEvent(StreamEvent):
    """A thinking/reasoning content block started."""

    type: Literal["thinking_start"] = "thinking_start"
    content_index: int
    thinking_signature: str | None = None
    redacted: bool = False


class ThinkingDeltaEvent(StreamEvent):
    """Reasoning increment plus optional provider replay metadata."""

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str
    content_index: int | None = None
    thinking_signature: str | None = None
    redacted: bool = False


class ThinkingEndEvent(StreamEvent):
    """A thinking/reasoning block completed with its canonical content."""

    type: Literal["thinking_end"] = "thinking_end"
    content_index: int
    content: str
    thinking_signature: str | None = None
    redacted: bool = False


class DoneEvent(StreamEvent):
    """响应正常结束。

    支持四种 stop_reason：
    - `stop` —— 模型自然结束（默认）
    - `length` —— 触达 max_tokens
    - `tool_use` —— 模型请求调用工具
    - `aborted` —— 外部 abort signal 触发，由 ProviderAdapter 在 signal set 时主动发出

    `usage` 缺省为 `Usage()`（全 0）—— 兼容旧调用 `DoneEvent(stop_reason="stop")`。
    """

    type: Literal["done"] = "done"
    stop_reason: Literal["stop", "length", "tool_use", "aborted"] = "stop"
    usage: Usage = Field(default_factory=Usage)


class ErrorEvent(StreamEvent):
    """错误或中断。

    注意：从 Step 21 起，外部 abort signal 不再用 ErrorEvent 表达——改用
    `DoneEvent(stop_reason="aborted")`。ErrorEvent 仅用于真正的错误路径
    （网络异常 / 协议错 / 业务错）。
    """

    type: Literal["error"] = "error"
    message: str


class ToolCallEvent(StreamEvent):
    """Legacy whole-tool-call event retained for custom/Fake providers.

    The agent normalizes this into ``toolcall_start`` + ``toolcall_end``.
    Real adapters emit the fine-grained lifecycle directly.
    """

    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


class ToolCallStartEvent(StreamEvent):
    """A streamed tool call started; id/name may arrive incrementally."""

    type: Literal["toolcall_start"] = "toolcall_start"
    content_index: int
    tool_call_id: str | None = None
    name: str | None = None


class ToolCallDeltaEvent(StreamEvent):
    """A raw JSON-argument fragment for an in-progress tool call."""

    type: Literal["toolcall_delta"] = "toolcall_delta"
    content_index: int
    delta: str
    tool_call_id: str | None = None
    name: str | None = None


class ToolCallEndEvent(ToolCallEvent):
    """A tool call completed and is now safe for the agent to execute.

    Subclassing the former whole-call event deliberately preserves
    ``isinstance(event, ToolCallEvent)`` for existing consumers while the
    distinct discriminator exposes the new lifecycle.
    """

    type: Literal["toolcall_end"] = "toolcall_end"  # type: ignore[assignment]
    content_index: int


__all__ = [
    "StreamEvent",
    "TextStartEvent",
    "TextDeltaEvent",
    "TextEndEvent",
    "ThinkingStartEvent",
    "ThinkingDeltaEvent",
    "ThinkingEndEvent",
    "DoneEvent",
    "ErrorEvent",
    "ToolCallEvent",
    "ToolCallStartEvent",
    "ToolCallDeltaEvent",
    "ToolCallEndEvent",
]

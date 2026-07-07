"""StreamEvent 类型（Step 21 抽出）。

把 stream event 定义从 `model_client.py` 拆出来的目的：避免
`model_client` ↔ `providers/*` 之间的循环 import——
`providers/base.py` / `providers/anthropic_compat.py` / `providers/fake.py`
都需要引用 StreamEvent 类型。

类型本身没变；`model_client.py` / `__init__.py` 都重新导出，外部 API 兼容。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .messages import ToolCall, Usage


class StreamEvent(BaseModel):
    """所有 stream 事件的基类。"""


class TextDeltaEvent(StreamEvent):
    """文本增量。"""

    type: Literal["text_delta"] = "text_delta"
    delta: str


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
    """模型请求调用工具。

    FakeClient 可以直接 yield ToolCallEvent 模拟模型决定调工具；
    真实 provider 适配器会把 Anthropic tool_use content block / OpenAI tool_calls
    翻译成此事件。

    携带完整 ToolCall（含 id / name / arguments / raw）。
    """

    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


__all__ = [
    "StreamEvent",
    "TextDeltaEvent",
    "DoneEvent",
    "ErrorEvent",
    "ToolCallEvent",
]

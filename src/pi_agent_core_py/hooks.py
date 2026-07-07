"""Tool Hooks（Step 6）。

两个钩子：
- `before_tool_call`：工具执行前拦截 / 修改 ToolCall
- `after_tool_call`：工具执行后修改 ToolResult

设计要点：
- Hook **不新增 AgentEvent**——结果体现在 `ToolResultMessage.details` 里
- Hook 抛异常不让 loop 崩，转成 is_error=True 的 ToolResult（details.hook / details.error_type）
- `before` 返回 `{allow=False, reason=...}` 阻断；返回修改后的 `tool_call` 改参
- `after` 直接返回新的 ToolResult（覆盖 content / details / is_error / terminate）

默认实现：
- `default_before_tool_call` 总是 allow=True
- `default_after_tool_call` 原样返回 ctx.result
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .messages import AgentMessage, ToolCall
from .tools import AgentTool, ToolResult

# ============================================================================
# Hook 函数类型
# ============================================================================


BeforeToolCallFn = Callable[
    ["BeforeToolCallContext"],
    Awaitable["BeforeToolCallResult"],
]
AfterToolCallFn = Callable[
    ["AfterToolCallContext"],
    Awaitable[ToolResult],
]


# ============================================================================
# before_tool_call：输入 / 输出
# ============================================================================


class BeforeToolCallContext(BaseModel):
    """`before_tool_call` 的输入。

    `tool` 是从 registry 查找到的工具；若 `ToolNotFoundError` 则为 None，
    hook 仍会被调用（便于在工具不存在时做特殊处理，如建议替代工具）。
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_call: ToolCall
    tool: AgentTool | None
    messages: list[AgentMessage] = Field(default_factory=list)


class BeforeToolCallResult(BaseModel):
    """`before_tool_call` 的输出。

    - `allow=True`：放行（默认）
    - `allow=False`：阻断，`reason` 写入 ToolResultMessage.content
    - `tool_call` 不为 None：使用修改后的 ToolCall 继续执行（替换原 tool_call）
    - `details`：合并到阻断时的 ToolResult.details
    """
    allow: bool = True
    tool_call: ToolCall | None = None
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# after_tool_call：输入
# ============================================================================


class AfterToolCallContext(BaseModel):
    """`after_tool_call` 的输入。

    `result` 是工具执行后的 ToolResult；hook 可以基于此返回新的 ToolResult
    （覆盖 content / details / is_error / terminate）。
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_call: ToolCall
    tool: AgentTool | None
    result: ToolResult
    messages: list[AgentMessage] = Field(default_factory=list)


# ============================================================================
# 默认实现
# ============================================================================


async def default_before_tool_call(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
    """默认放行，不修改 ToolCall。"""
    return BeforeToolCallResult(allow=True)


async def default_after_tool_call(ctx: AfterToolCallContext) -> ToolResult:
    """默认原样返回 result。"""
    return ctx.result


__all__ = [
    "BeforeToolCallFn", "AfterToolCallFn",
    "BeforeToolCallContext", "BeforeToolCallResult",
    "AfterToolCallContext",
    "default_before_tool_call", "default_after_tool_call",
]

"""Agent runtime event contracts.

9 种事件：
    agent_start / agent_end           — run 级
    turn_start / turn_end             — turn 级
    message_start / message_update / message_end — 消息级
    tool_execution_start / tool_execution_update / tool_execution_end
                                           — 工具执行级

时序（Step 5 含工具的典型一次 run）：
    agent_start
    turn_start
    message_start (user)
    message_end   (user)
    message_start (assistant)
    [message_update (assistant) × N]    ← 若有 text_delta
    message_end   (assistant with ToolCall)
    tool_execution_start
    [tool_execution_update × N]
    tool_execution_end
    message_start (toolResult)
    message_end   (toolResult)
    turn_end
    turn_start
    message_start (assistant)            ← 第二轮 LLM
    message_update (assistant) × N
    message_end   (assistant)
    turn_end
    agent_end
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, SerializeAsAny

from ..ai.model_client import StreamEvent
from .messages import AgentMessage, AssistantMessage, ToolCall
from .tooling import ToolResult

# ============================================================================
# run 级事件
# ============================================================================


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(BaseModel):
    type: Literal["agent_end"] = "agent_end"
    # Compatibility: ``messages`` remains the complete transcript used by the
    # current Web/session stack. ``new_messages`` makes the per-run delta
    # explicit without silently changing the established wire contract.
    messages: list[AgentMessage] = Field(default_factory=list)
    new_messages: list[AgentMessage] = Field(default_factory=list)


# ============================================================================
# turn 级事件
# ============================================================================


class TurnStartEvent(BaseModel):
    type: Literal["turn_start"] = "turn_start"


class TurnEndEvent(BaseModel):
    """一个 turn 结束。Step 5 起 tool_results 可能含 ToolResultMessage。"""
    type: Literal["turn_end"] = "turn_end"
    message: AssistantMessage
    tool_results: list[Any] = Field(default_factory=list)


# ============================================================================
# 消息级事件
# ============================================================================


class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: AgentMessage


class MessageUpdateEvent(BaseModel):
    """流式更新。只对 assistant 消息。"""
    type: Literal["message_update"] = "message_update"
    message: AssistantMessage
    assistant_message_event: SerializeAsAny[StreamEvent] | None = None


class MessageEndEvent(BaseModel):
    type: Literal["message_end"] = "message_end"
    message: AgentMessage


# ============================================================================
# 工具执行级事件（Step 5 新增）
# ============================================================================


class ToolExecutionStartEvent(BaseModel):
    """单个工具开始执行。后续可以有零到多个 update，最后必须有 end。"""
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call: ToolCall


class ToolExecutionUpdateEvent(BaseModel):
    """工具执行中的增量结果。

    ``partial_result`` 只用于 UI / observability，不会写入对话上下文；最终
    ``ToolResultMessage`` 仍由 :class:`ToolExecutionEndEvent` 的 result 生成。
    """
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call: ToolCall
    partial_result: ToolResult


class ToolExecutionEndEvent(BaseModel):
    """单个工具结束执行。携带 ToolResult（agent 内部结果对象）。"""
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call: ToolCall
    result: ToolResult


# ============================================================================
# Queue / Abort 级事件（Step 9 新增）
# ============================================================================


#: 请求类型（Step 9）
AgentRequestType = Literal["prompt", "continue"]

#: 请求结束状态（Step 9）
RequestEndStatus = Literal["completed", "aborted", "error"]


class RequestQueuedEvent(BaseModel):
    """prompt / continue 进入队列时发出。"""
    type: Literal["request_queued"] = "request_queued"
    request_id: str
    request_type: AgentRequestType
    queue_size: int


class RequestStartEvent(BaseModel):
    """队列中某个 request 真正开始执行时发出。"""
    type: Literal["request_start"] = "request_start"
    request_id: str
    request_type: AgentRequestType


class RequestEndEvent(BaseModel):
    """request 完成 / 中止 / 异常时发出。"""
    type: Literal["request_end"] = "request_end"
    request_id: str
    request_type: AgentRequestType
    status: RequestEndStatus


class AgentAbortEvent(BaseModel):
    """调用 abort() 后发出。"""
    type: Literal["agent_abort"] = "agent_abort"
    request_id: str | None
    reason: str | None = None


# ============================================================================
# Union
# ============================================================================


AgentEvent = Annotated[
    Union[  # noqa: UP007
        AgentStartEvent,
        AgentEndEvent,
        TurnStartEvent,
        TurnEndEvent,
        MessageStartEvent,
        MessageUpdateEvent,
        MessageEndEvent,
        ToolExecutionStartEvent,
        ToolExecutionUpdateEvent,
        ToolExecutionEndEvent,
        # Step 9 新增
        RequestQueuedEvent,
        RequestStartEvent,
        RequestEndEvent,
        AgentAbortEvent,
    ],
    Field(discriminator="type"),
]


__all__ = [
    "AgentStartEvent", "AgentEndEvent",
    "TurnStartEvent", "TurnEndEvent",
    "MessageStartEvent", "MessageUpdateEvent", "MessageEndEvent",
    "ToolExecutionStartEvent", "ToolExecutionUpdateEvent", "ToolExecutionEndEvent",
    # Step 9
    "AgentRequestType", "RequestEndStatus",
    "RequestQueuedEvent", "RequestStartEvent", "RequestEndEvent", "AgentAbortEvent",
    "AgentEvent",
]

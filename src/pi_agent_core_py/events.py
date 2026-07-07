"""AgentEvent 类型（Step 5）。

9 种事件：
    agent_start / agent_end           — run 级
    turn_start / turn_end             — turn 级
    message_start / message_update / message_end — 消息级
    tool_execution_start / tool_execution_end   — 工具执行级（Step 5 新增）

时序（Step 5 含工具的典型一次 run）：
    agent_start
    turn_start
    message_start (user)
    message_end   (user)
    message_start (assistant)
    [message_update (assistant) × N]    ← 若有 text_delta
    message_end   (assistant with ToolCall)
    tool_execution_start
    message_start (toolResult)
    message_end   (toolResult)
    tool_execution_end
    message_start (assistant)            ← 第二轮 LLM
    message_update (assistant) × N
    message_end   (assistant)
    turn_end
    agent_end
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

from .messages import AssistantMessage, Message, ToolCall
from .model_client import StreamEvent
from .tools import ToolResult

# ============================================================================
# run 级事件
# ============================================================================


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(BaseModel):
    type: Literal["agent_end"] = "agent_end"
    messages: list[Message] = Field(default_factory=list)


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
    message: Message


class MessageUpdateEvent(BaseModel):
    """流式更新。只对 assistant 消息。"""
    type: Literal["message_update"] = "message_update"
    message: AssistantMessage
    assistant_message_event: StreamEvent | None = None


class MessageEndEvent(BaseModel):
    type: Literal["message_end"] = "message_end"
    message: Message


# ============================================================================
# 工具执行级事件（Step 5 新增）
# ============================================================================


class ToolExecutionStartEvent(BaseModel):
    """单个工具开始执行。

    Step 5 不实现 update 事件（流式工具输出在 Step 7+ 加）。
    Step 5 不实现 batch（多个工具同时执行在 Step 7 加）。
    """
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call: ToolCall


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
    "ToolExecutionStartEvent", "ToolExecutionEndEvent",
    # Step 9
    "AgentRequestType", "RequestEndStatus",
    "RequestQueuedEvent", "RequestStartEvent", "RequestEndEvent", "AgentAbortEvent",
    "AgentEvent",
]

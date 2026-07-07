"""Turn Snapshot —— 不可变请求快照（Step 11）。

Snapshot 是**记录层**，不改变 Agent 执行语义；依附 AgentHarness，不塞进 Agent 内核；
只存在内存中，不写文件、不写数据库。

```text
Agent        ：状态机 / queue / abort / event stream / messages
AgentHarness ：请求生命周期编排 / 外部 hook / 事件观察 / 共享 context
TurnSnapshot ：一次请求的完整快照（request metadata + messages before/after
               + event timeline + tool calls/results + status + metadata）
```

6 个 Pydantic 模型 + 1 个 Builder：

- `SnapshotStatus`     —— Literal["running","completed","aborted","error"]
- `EventSnapshot`      ——单个 AgentEvent 的不可变记录
- `ToolCallSnapshot`   ——工具调用快照（来自 ToolExecutionStartEvent）
- `ToolResultSnapshot` ——工具结果快照（来自 ToolExecutionEndEvent）
- `TurnSnapshot`       ——一次请求的完整快照，支持 to_dict / to_json
- `SnapshotBuilder`    ——在一次请求生命周期内累积事件，finish() 后定型

设计要点：
- Snapshot 是**记录层**，不改变 Agent 执行语义
- Snapshot **依附 AgentHarness**，不塞进 Agent 内核
- Snapshot **只存在内存**，不写文件、不写数据库
- on_event 错误**不能破坏** snapshot 收集
- ToolResultMessage.is_error **不等于** SnapshotStatus.error
- Snapshot 不持有 Agent 引用（循环引用 + 可变性）

Step 11 **不做**：Session / Memory / Durable Storage（Step 12）/ Skills / CLI。
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from .events import (
    AgentEvent,
    AgentRequestType,
    RequestEndEvent,
    RequestStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from .messages import Message

# ============================================================================
# SnapshotStatus
# ============================================================================


#: TurnSnapshot 的终态。
#: - "running"   请求仍在执行中（仅 SnapshotBuilder.start 后、finish 前）
#: - "completed" 请求正常完成
#: - "aborted"   请求被 abort（观察到 RequestEndEvent(status="aborted")）
#: - "error"     Harness / Agent 自身或 before/after_request hook 抛异常
#:
#: 注意：
#: - 工具结果 is_error=True **不一定**让 status 变成 error
#: - LLM ErrorEvent 也**不一定**让 status 变成 error
#: - 只有请求生命周期本身失败（hook 抛 / Agent 抛），才是 error
SnapshotStatus = Literal["running", "completed", "aborted", "error"]


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# Snapshot ID 生成（进程级计数器 + 时间戳）
# ============================================================================


_snapshot_counter: int = 0


def _gen_snapshot_id() -> str:
    """生成 snapshot id：`snap-{毫秒时间戳}-{自增计数}`。

    用 global 计数器保证同一毫秒内 id 不冲突。
    """
    global _snapshot_counter
    _snapshot_counter += 1
    return f"snap-{_now_ms()}-{_snapshot_counter}"


# ============================================================================
# EventSnapshot
# ============================================================================


class EventSnapshot(BaseModel):
    """单个 AgentEvent 的不可变记录。

    字段：
      index      事件在本次 turn 中的顺序编号（从 0 开始）
      type       AgentEvent.type 字面量（如 "agent_start" / "tool_execution_end"）
      timestamp  事件被 Harness 观察到的时间（毫秒）
      event      AgentEvent 的可序列化 dict（Pydantic model_dump(mode="json")）
    """
    index: int
    type: str
    timestamp: int
    event: dict[str, Any]


# ============================================================================
# ToolCallSnapshot / ToolResultSnapshot
# ============================================================================


class ToolCallSnapshot(BaseModel):
    """工具调用快照（来自 ToolExecutionStartEvent.tool_call）。

    字段对应 ToolCall，但 raw 被深拷贝为独立 dict（避免与 Agent 内部共享引用）。
    """
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] | None = None
    timestamp: int


class ToolResultSnapshot(BaseModel):
    """工具结果快照（来自 ToolExecutionEndEvent.result）。

    字段对应 ToolResult；content 是 list[dict]（每个 TextContent 序列化成 dict）。
    """
    tool_call_id: str
    name: str
    content: list[dict[str, Any]]
    is_error: bool
    terminate: bool
    details: dict[str, Any]
    timestamp: int


# ============================================================================
# TurnSnapshot
# ============================================================================


class TurnSnapshot(BaseModel):
    """一次 Harness 请求的完整快照。

    字段：
      id               snapshot 唯一 id（`snap-{ts}-{counter}`）
      request_id       来自 RequestStartEvent / RequestEndEvent
      request_type     "prompt" | "continue"
      user_text        prompt 文本；continue 时为 None
      status           running / completed / aborted / error
      error            lifecycle 失败时的错误描述
      started_at       请求开始时间（毫秒）
      ended_at         请求结束时间（毫秒）
      duration_ms      ended_at - started_at
      messages_before  请求前 messages 的可序列化 dict 列表
      messages_after   请求后 messages 的可序列化 dict 列表
      events           本次请求观察到的 AgentEvent 时间线
      tool_calls       本次请求中的工具调用（按 start 事件顺序）
      tool_results     本次请求中的工具结果（按 end 事件观察顺序）
      metadata         HarnessContext.metadata 的拷贝

    约定：
    - 构造期（SnapshotBuilder.start → finish）可变
    - finish() 后视为**不可变**（Pydantic 不强制 frozen；语义上不可变）
    - 不要持有 Agent 引用（循环引用 + 可变性）
    """
    id: str
    request_id: str | None = None
    request_type: AgentRequestType | None = None
    user_text: str | None = None

    status: SnapshotStatus = "running"
    error: str | None = None

    started_at: int | None = None
    ended_at: int | None = None
    duration_ms: int | None = None

    messages_before: list[dict[str, Any]] = Field(default_factory=list)
    messages_after: list[dict[str, Any]] = Field(default_factory=list)

    events: list[EventSnapshot] = Field(default_factory=list)
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    tool_results: list[ToolResultSnapshot] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。

        内部用 `model_dump(mode="json")`——所有嵌套 Pydantic 模型 / datetime / UUID
        等都转成 JSON 友好类型。
        """
        return self.model_dump(mode="json")

    def to_json(self, indent: int | None = None) -> str:
        """导出为 JSON 字符串。

        `ensure_ascii=False` 保留中文等非 ASCII 字符，便于人眼读。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ============================================================================
# 序列化辅助
# ============================================================================


def _serialize_message(msg: Message) -> dict[str, Any]:
    """Message → 可 JSON 序列化的 dict。"""
    return msg.model_dump(mode="json")


def _serialize_event(event: AgentEvent) -> dict[str, Any]:
    """AgentEvent → 可 JSON 序列化的 dict。

    所有当前 AgentEvent 都是 Pydantic BaseModel——走 model_dump；
    若未来出现非 Pydantic 事件，fallback 到 {"repr": repr(event)}。
    """
    if isinstance(event, BaseModel):
        return event.model_dump(mode="json")
    return {"repr": repr(event)}


# ============================================================================
# SnapshotBuilder
# ============================================================================


class SnapshotBuilder:
    """在一次 Harness 请求生命周期内收集事件，最终生成 TurnSnapshot。

    用法（harness._run_one 中）：

    ```text
    builder = SnapshotBuilder()
    snapshot = builder.start(
        request_type="prompt",
        user_text="hi",
        messages_before=list(agent.state.messages),
        metadata=dict(context.metadata),
    )
    context.snapshot = snapshot
    self._snapshot_builder = builder

    # ... agent_call 期间每个 AgentEvent 都会调 observe_event ...

    snapshot = builder.finish(
        status="completed" | "aborted" | "error",
        messages_after=list(agent.state.messages),
        error=None | "RuntimeError: ...",
        metadata=dict(context.metadata),
    )
    self.last_snapshot = snapshot
    self.snapshots.append(snapshot)
    self._snapshot_builder = None
    ```

    设计要点：
    - 单实例贯穿一次请求；finish() 后实例不再使用
    - `seen_aborted` 标志在 observe_event 中维护——观察到 RequestEndEvent(aborted)
      时置 True；finish() 时由 caller 决定是否传入 status="aborted"
    - 所有 collections / dict 都做深拷贝，避免与 Agent / Harness 内部共享引用
    """

    def __init__(self, snapshot_id: str | None = None):
        self.snapshot: TurnSnapshot = TurnSnapshot(
            id=snapshot_id or _gen_snapshot_id(),
        )
        self._event_index: int = 0
        # 观察到 RequestEndEvent(status="aborted") 时置 True
        self.seen_aborted: bool = False

    # ------------------------------------------------------------------
    # start：开启快照
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        request_type: AgentRequestType,
        user_text: str | None,
        messages_before: list[Message],
        metadata: dict[str, Any],
    ) -> TurnSnapshot:
        """开始快照——设置 running 状态、初始字段。

        - status 设为 "running"
        - started_at 设为当前时间
        - request_type / user_text / messages_before / metadata 从入参拷贝
        - 其余字段保持 TurnSnapshot 默认值（events / tool_calls / tool_results 等）
        """
        self.snapshot.status = "running"
        self.snapshot.started_at = _now_ms()
        self.snapshot.request_type = request_type
        self.snapshot.user_text = user_text
        self.snapshot.messages_before = [
            _serialize_message(m) for m in messages_before
        ]
        self.snapshot.metadata = dict(metadata)
        return self.snapshot

    # ------------------------------------------------------------------
    # observe_event：累积事件
    # ------------------------------------------------------------------

    def observe_event(self, event: AgentEvent) -> None:
        """记录一个 AgentEvent；按 type 提取子信息。

        顺序：
        1. append EventSnapshot（index 递增）
        2. 如果是 RequestStartEvent → 写 snapshot.request_id
        3. 如果是 ToolExecutionStartEvent → append ToolCallSnapshot
        4. 如果是 ToolExecutionEndEvent → append ToolResultSnapshot
        5. 如果是 RequestEndEvent(status="aborted") → seen_aborted = True

        注意：
        - `RequestEndEvent(status="completed")` 不立即 finish——因为 after_request
          hook 还没跑。最终状态由 finish() 决定。
        - ToolCallSnapshot 按 start 事件顺序；ToolResultSnapshot 按 end 事件观察顺序。
        """
        index = self._event_index
        self._event_index += 1

        self.snapshot.events.append(EventSnapshot(
            index=index,
            type=event.type,
            timestamp=_now_ms(),
            event=_serialize_event(event),
        ))

        if isinstance(event, RequestStartEvent):
            self.snapshot.request_id = event.request_id
        elif isinstance(event, ToolExecutionStartEvent):
            tc = event.tool_call
            self.snapshot.tool_calls.append(ToolCallSnapshot(
                id=tc.id,
                name=tc.name,
                arguments=dict(tc.arguments),
                raw=dict(tc.raw) if tc.raw else None,
                timestamp=_now_ms(),
            ))
        elif isinstance(event, ToolExecutionEndEvent):
            r = event.result
            self.snapshot.tool_results.append(ToolResultSnapshot(
                tool_call_id=r.tool_call_id,
                name=r.name,
                content=[c.model_dump(mode="json") for c in r.content],
                is_error=r.is_error,
                terminate=r.terminate,
                details=dict(r.details),
                timestamp=_now_ms(),
            ))
        elif isinstance(event, RequestEndEvent):
            if event.status == "aborted":
                self.seen_aborted = True

    # ------------------------------------------------------------------
    # finish：定型快照
    # ------------------------------------------------------------------

    def finish(
        self,
        *,
        status: SnapshotStatus,
        messages_after: list[Message],
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TurnSnapshot:
        """结束快照——固定 status / ended_at / duration_ms / messages_after。

        - status 由 caller 决定（"completed" | "aborted" | "error"）
        - messages_after 从入参序列化
        - error 仅在 status="error" 时有意义
        - metadata 若提供则覆盖（caller 应传 dict(context.metadata)）
        """
        self.snapshot.status = status
        self.snapshot.ended_at = _now_ms()
        if self.snapshot.started_at is not None:
            self.snapshot.duration_ms = (
                self.snapshot.ended_at - self.snapshot.started_at
            )
        self.snapshot.messages_after = [
            _serialize_message(m) for m in messages_after
        ]
        self.snapshot.error = error
        if metadata is not None:
            self.snapshot.metadata = dict(metadata)
        return self.snapshot


__all__ = [
    "SnapshotStatus",
    "EventSnapshot",
    "ToolCallSnapshot",
    "ToolResultSnapshot",
    "TurnSnapshot",
    "SnapshotBuilder",
]

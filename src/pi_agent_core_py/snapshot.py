"""请求级与 Turn 级不可变快照。

``RequestSnapshot`` 覆盖一次 Harness request；``TurnSnapshot`` 严格覆盖一次
LLM 调用及该调用产生的当批工具。持久化层继续保存 RequestSnapshot，因此旧版
“一个 TurnSnapshot 等于一次 request”的 JSON 可无损兼容读取。
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from .events import (
    AgentEvent,
    AgentRequestType,
    MessageEndEvent,
    RequestEndEvent,
    RequestStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from .messages import Message

SnapshotStatus = Literal["running", "completed", "aborted", "error"]


def _now_ms() -> int:
    return int(time.time() * 1000)


_snapshot_counter = 0


def _gen_snapshot_id() -> str:
    global _snapshot_counter
    _snapshot_counter += 1
    return f"snap-{_now_ms()}-{_snapshot_counter}"


class EventSnapshot(BaseModel):
    index: int
    type: str
    timestamp: int
    event: dict[str, Any]


class ToolCallSnapshot(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] | None = None
    timestamp: int


class ToolResultSnapshot(BaseModel):
    tool_call_id: str
    name: str
    content: list[dict[str, Any]]
    is_error: bool
    terminate: bool
    details: dict[str, Any]
    timestamp: int


class _SnapshotBase(BaseModel):
    """公共 JSON 序列化接口。"""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class TurnSnapshot(_SnapshotBase):
    """一次真实 LLM 调用 + 该调用当批工具的快照。"""

    id: str
    request_snapshot_id: str
    request_id: str | None = None
    index: int
    status: SnapshotStatus = "running"
    error: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    duration_ms: int | None = None
    messages_before: list[dict[str, Any]] = Field(default_factory=list)
    messages_after: list[dict[str, Any]] = Field(default_factory=list)
    message: dict[str, Any] | None = None
    events: list[EventSnapshot] = Field(default_factory=list)
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    tool_results: list[ToolResultSnapshot] = Field(default_factory=list)


class RequestSnapshot(_SnapshotBase):
    """一次 prompt / continue 请求的完整生命周期快照。

    除新增 ``turns`` 外，字段保持旧 TurnSnapshot 的 wire format，确保 Web API、
    SQLite 以及历史 JSON 的兼容性。
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
    turns: list[TurnSnapshot] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _serialize_message(msg: Message) -> dict[str, Any]:
    return msg.model_dump(mode="json")


def _serialize_event(event: AgentEvent) -> dict[str, Any]:
    if isinstance(event, BaseModel):
        return event.model_dump(mode="json")
    return {"repr": repr(event)}


def _tool_call_snapshot(event: ToolExecutionStartEvent, timestamp: int) -> ToolCallSnapshot:
    tc = event.tool_call
    return ToolCallSnapshot(
        id=tc.id,
        name=tc.name,
        arguments=dict(tc.arguments),
        raw=dict(tc.raw) if tc.raw else None,
        timestamp=timestamp,
    )


def _tool_result_snapshot(event: ToolExecutionEndEvent, timestamp: int) -> ToolResultSnapshot:
    result = event.result
    return ToolResultSnapshot(
        tool_call_id=result.tool_call_id,
        name=result.name,
        content=[item.model_dump(mode="json") for item in result.content],
        is_error=result.is_error,
        terminate=result.terminate,
        details=dict(result.details),
        timestamp=timestamp,
    )


class SnapshotBuilder:
    """在一次 Harness request 内同时构建 RequestSnapshot 与 TurnSnapshot。"""

    def __init__(self, snapshot_id: str | None = None):
        self.snapshot = RequestSnapshot(id=snapshot_id or _gen_snapshot_id())
        self._event_index = 0
        self._turn_event_index = 0
        self._active_turn: TurnSnapshot | None = None
        self._current_messages: list[dict[str, Any]] = []
        self.seen_aborted = False

    def start(
        self,
        *,
        request_type: AgentRequestType,
        user_text: str | None,
        messages_before: list[Message],
        metadata: dict[str, Any],
    ) -> RequestSnapshot:
        self.snapshot.status = "running"
        self.snapshot.started_at = _now_ms()
        self.snapshot.request_type = request_type
        self.snapshot.user_text = user_text
        self.snapshot.messages_before = [_serialize_message(m) for m in messages_before]
        self.snapshot.metadata = dict(metadata)
        self._current_messages = [dict(item) for item in self.snapshot.messages_before]
        return self.snapshot

    def _begin_turn(self, timestamp: int) -> None:
        if self._active_turn is not None:
            self._finish_active_turn(
                status="error",
                timestamp=timestamp,
                error="turn_start observed before previous turn_end",
            )
        index = len(self.snapshot.turns)
        self._turn_event_index = 0
        self._active_turn = TurnSnapshot(
            id=f"{self.snapshot.id}-turn-{index + 1}",
            request_snapshot_id=self.snapshot.id,
            request_id=self.snapshot.request_id,
            index=index,
            started_at=timestamp,
            messages_before=[dict(item) for item in self._current_messages],
        )

    def _finish_active_turn(
        self,
        *,
        status: SnapshotStatus,
        timestamp: int,
        error: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> None:
        turn = self._active_turn
        if turn is None:
            return
        turn.status = status
        turn.error = error
        turn.message = message
        turn.ended_at = timestamp
        if turn.started_at is not None:
            turn.duration_ms = timestamp - turn.started_at
        turn.messages_after = [dict(item) for item in self._current_messages]
        self.snapshot.turns.append(turn)
        self._active_turn = None

    def observe_event(self, event: AgentEvent) -> None:
        timestamp = _now_ms()
        payload = _serialize_event(event)
        self.snapshot.events.append(EventSnapshot(
            index=self._event_index,
            type=event.type,
            timestamp=timestamp,
            event=payload,
        ))
        self._event_index += 1

        if isinstance(event, RequestStartEvent):
            self.snapshot.request_id = event.request_id
        if isinstance(event, TurnStartEvent):
            self._begin_turn(timestamp)

        turn = self._active_turn
        if turn is not None:
            turn.request_id = self.snapshot.request_id
            turn.events.append(EventSnapshot(
                index=self._turn_event_index,
                type=event.type,
                timestamp=timestamp,
                event=payload,
            ))
            self._turn_event_index += 1

        if isinstance(event, MessageEndEvent):
            self._current_messages.append(_serialize_message(event.message))

        if isinstance(event, ToolExecutionStartEvent):
            tool_call_item = _tool_call_snapshot(event, timestamp)
            self.snapshot.tool_calls.append(tool_call_item)
            if turn is not None:
                turn.tool_calls.append(tool_call_item.model_copy(deep=True))
        elif isinstance(event, ToolExecutionEndEvent):
            tool_result_item = _tool_result_snapshot(event, timestamp)
            self.snapshot.tool_results.append(tool_result_item)
            if turn is not None:
                turn.tool_results.append(tool_result_item.model_copy(deep=True))
        elif isinstance(event, TurnEndEvent):
            stop_reason = event.message.stop_reason
            status: SnapshotStatus = (
                "aborted" if stop_reason == "aborted"
                else "error" if stop_reason == "error"
                else "completed"
            )
            self._finish_active_turn(
                status=status,
                timestamp=timestamp,
                error=event.message.error_message,
                message=_serialize_message(event.message),
            )
        elif isinstance(event, RequestEndEvent) and event.status == "aborted":
            self.seen_aborted = True

    def finish(
        self,
        *,
        status: SnapshotStatus,
        messages_after: list[Message],
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RequestSnapshot:
        ended_at = _now_ms()
        serialized_after = [_serialize_message(m) for m in messages_after]
        if self._active_turn is not None:
            self._current_messages = [dict(item) for item in serialized_after]
            self._finish_active_turn(
                status=status,
                timestamp=ended_at,
                error=error or "request ended before turn_end",
            )
        self.snapshot.status = status
        self.snapshot.ended_at = ended_at
        if self.snapshot.started_at is not None:
            self.snapshot.duration_ms = ended_at - self.snapshot.started_at
        self.snapshot.messages_after = serialized_after
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
    "RequestSnapshot",
    "SnapshotBuilder",
]

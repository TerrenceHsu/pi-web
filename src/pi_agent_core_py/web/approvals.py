"""In-memory Human Approval coordination for active Web requests.

The original ToolCall and arguments stay on the suspended Agent task.  Only a
bounded, redacted JSON view is exposed to the browser; the browser can resolve
the exact approval id but can never replace the ToolCall being executed.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from ..policy import ToolApprovalContext

ApprovalStatus = Literal["pending", "approved", "denied", "cancelled"]
ApprovalDecision = Literal["approve", "deny"]
ApprovalEventSink = Callable[[dict[str, Any], str, str | None], Awaitable[None]]

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_json(value: Any, *, depth: int = 0) -> Any:
    if depth >= 5:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "…"
    if isinstance(value, dict):
        dict_result: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, item in items[:50]:
            key = str(raw_key)[:128]
            dict_result[key] = (
                "[REDACTED]"
                if _is_sensitive_key(key)
                else _redact_json(item, depth=depth + 1)
            )
        if len(items) > 50:
            dict_result["_truncated"] = True
        return dict_result
    if isinstance(value, (list, tuple)):
        values = list(value)
        list_result = [_redact_json(item, depth=depth + 1) for item in values[:50]]
        if len(values) > 50:
            list_result.append("[TRUNCATED]")
        return list_result
    return f"<{type(value).__name__}>"


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    safe = _redact_json(arguments)
    if not isinstance(safe, dict):
        return {"value": safe}
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 8192:
        return safe
    return {"_truncated": True, "preview": encoded[:8000] + "…"}


@dataclass
class ToolApprovalRecord:
    id: str
    request_id: str
    session_id: str | None
    tool_call_id: str
    tool_name: str
    tool_label: str
    arguments: dict[str, Any]
    reason: str | None
    policy_name: str
    policy_metadata: dict[str, Any]
    status: ApprovalStatus
    created_at: str
    resolved_at: str | None = None
    future: asyncio.Future[ApprovalStatus] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_label": self.tool_label,
            "arguments": dict(self.arguments),
            "reason": self.reason,
            "policy_name": self.policy_name,
            "policy_metadata": dict(self.policy_metadata),
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


class ToolApprovalManager:
    """Coordinates pending approvals without trusting browser-supplied ToolCalls."""

    def __init__(self, event_sink: ApprovalEventSink, *, max_records: int = 1000):
        self._event_sink = event_sink
        self._records: dict[str, ToolApprovalRecord] = {}
        self._order: deque[str] = deque()
        self._max_records = max(10, max_records)
        self._lock = asyncio.Lock()

    async def request_approval(
        self,
        *,
        request_id: str,
        session_id: str | None,
        context: ToolApprovalContext,
    ) -> bool:
        if context.signal is not None and context.signal.is_set():
            return False

        loop = asyncio.get_running_loop()
        resolution_future: asyncio.Future[ApprovalStatus] = loop.create_future()
        record = ToolApprovalRecord(
            id=f"approval_{uuid4().hex[:16]}",
            request_id=request_id,
            session_id=session_id,
            tool_call_id=context.tool_call.id,
            tool_name=context.tool_call.name,
            tool_label=(
                getattr(context.tool, "label", None) or context.tool_call.name
            ),
            arguments=_safe_arguments(dict(context.tool_call.arguments)),
            reason=context.decision.reason,
            policy_name=context.decision.policy_name,
            policy_metadata=_redact_json(dict(context.decision.metadata)),
            status="pending",
            created_at=_now_iso(),
            future=resolution_future,
        )
        async with self._lock:
            self._records[record.id] = record
            self._order.append(record.id)
            self._prune_unlocked()

        await self._emit("tool_approval_requested", record)
        try:
            resolution = await resolution_future
        except asyncio.CancelledError:
            await self._cancel_record(record.id)
            raise
        return resolution == "approved"

    def list_for_request(
        self,
        request_id: str,
        *,
        status: ApprovalStatus | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for approval_id in self._order:
            record = self._records.get(approval_id)
            if record is None or record.request_id != request_id:
                continue
            if status is not None and record.status != status:
                continue
            result.append(record.public())
        return result

    def pending_count(self, request_id: str) -> int:
        return sum(
            1
            for record in self._records.values()
            if record.request_id == request_id and record.status == "pending"
        )

    async def resolve(
        self,
        *,
        request_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> tuple[dict[str, Any], bool]:
        desired: ApprovalStatus = "approved" if decision == "approve" else "denied"
        future: asyncio.Future[ApprovalStatus] | None = None
        async with self._lock:
            record = self._records.get(approval_id)
            if record is None or record.request_id != request_id:
                raise KeyError(approval_id)
            if record.status == desired:
                return record.public(), True
            if record.status != "pending":
                raise ValueError(f"approval already resolved as {record.status}")
            record.status = desired
            record.resolved_at = _now_iso()
            future = record.future
            public = record.public()

        if future is not None and not future.done():
            future.set_result(desired)
        await self._emit("tool_approval_resolved", record)
        return public, False

    async def cancel_request(self, request_id: str) -> None:
        pending = [
            record.id
            for record in self._records.values()
            if record.request_id == request_id and record.status == "pending"
        ]
        for approval_id in pending:
            await self._cancel_record(approval_id)

    async def cancel_all(self) -> None:
        pending_request_ids = {
            record.request_id
            for record in self._records.values()
            if record.status == "pending"
        }
        for request_id in pending_request_ids:
            await self.cancel_request(request_id)

    async def _cancel_record(self, approval_id: str) -> None:
        future: asyncio.Future[ApprovalStatus] | None = None
        async with self._lock:
            record = self._records.get(approval_id)
            if record is None or record.status != "pending":
                return
            record.status = "cancelled"
            record.resolved_at = _now_iso()
            future = record.future
        if future is not None and not future.done():
            future.set_result("cancelled")
        await self._emit("tool_approval_resolved", record)

    async def _emit(self, event_type: str, record: ToolApprovalRecord) -> None:
        try:
            await self._event_sink(
                {"type": event_type, "approval": record.public()},
                record.request_id,
                record.session_id,
            )
        except Exception:
            # Event delivery must never decide whether a ToolCall executes.
            return

    def _prune_unlocked(self) -> None:
        while len(self._order) > self._max_records:
            approval_id = self._order[0]
            record = self._records.get(approval_id)
            if record is not None and record.status == "pending":
                break
            self._order.popleft()
            self._records.pop(approval_id, None)


__all__ = [
    "ApprovalDecision",
    "ApprovalStatus",
    "ToolApprovalManager",
    "ToolApprovalRecord",
]

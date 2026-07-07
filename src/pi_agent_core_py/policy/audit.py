"""Audit log——Step 18。

每次 policy 检查都写一条 `ToolPermissionAuditRecord`。

接口：

- `InMemoryToolPermissionAuditLog.append(record)`
- `InMemoryToolPermissionAuditLog.list_records()`
- `InMemoryToolPermissionAuditLog.clear()`

故意不实现持久化（文件 / DB）——Step 18 只做内存审计；Step 20 Web UI
按需读 `list_records()` 展示。
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now_ms() -> int:
    return int(time.time() * 1000)


class ToolPermissionAuditRecord(BaseModel):
    """单次权限决策的审计记录。"""

    timestamp: int = Field(default_factory=_now_ms)
    tool_call_id: str
    tool_name: str
    decision: Literal["allow", "deny", "require_approval"]
    policy_name: str
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InMemoryToolPermissionAuditLog:
    """进程内审计日志（无锁；单 event loop 内追加 / 读取）。

    Step 18 不做持久化 / 不做大小限制——长跑进程应自行 wrap 一个会
    截断 / 落盘的实现。
    """

    def __init__(self) -> None:
        self.records: list[ToolPermissionAuditRecord] = []

    def append(self, record: ToolPermissionAuditRecord) -> None:
        self.records.append(record)

    def list_records(self) -> list[ToolPermissionAuditRecord]:
        """返回内部记录的浅拷贝列表（防止外部 append / sort 污染）。"""
        return list(self.records)

    def clear(self) -> None:
        self.records.clear()

    def counts(self) -> dict[str, int]:
        """便利方法：按 decision 计数。

        供 Harness 写 metadata summary 用。
        """
        out = {"allow": 0, "deny": 0, "require_approval": 0}
        for r in self.records:
            out[r.decision] = out.get(r.decision, 0) + 1
        return out


__all__ = [
    "ToolPermissionAuditRecord",
    "InMemoryToolPermissionAuditLog",
]

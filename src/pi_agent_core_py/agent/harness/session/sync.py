"""Harness/session synchronization and consistency contracts.

Step 12 把 snapshot 自动 append 到 session；Step 13 补工程化能力：

- `SessionAutoSavePolicy`
  ——自动保存策略（never / after_snapshot / after_success / after_any_request）
- `SessionSyncConfig`
  ——同步配置（auto_save_policy / sync_harness_metadata / sync_agent_messages / strict_consistency）
- `SessionConsistencyIssue`  ——单个一致性问题（code + message + severity + details）
- `SessionConsistencyReport` ——一致性检查报告（ok + issues + checked_at）

AgentHarness 用法：

```text
harness.configure_session_sync(
    store=JsonFileSessionStore("./sessions"),
    auto_save_policy="after_any_request",
    sync_harness_metadata=True,
    sync_agent_messages=True,
    strict_consistency=False,
)
harness.attach_session(session)
await harness.run_prompt("hi")

report = harness.check_session_consistency()
if not report.ok:
    for issue in report.issues:
        print(issue.severity, issue.code, issue.message)
```

Step 13 **不做**：Skills / Compaction / Vector Memory / 自动摘要。
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# SessionAutoSavePolicy
# ============================================================================


#: 控制 _post_finish_snapshot 是否自动保存 session 到 store。
#:
#: - "never"             默认；不自动保存（调方需要显式 save_session）
#: - "after_snapshot"    每次 _finish_snapshot 后保存（任意 status）
#: - "after_success"     仅 snapshot.status == "completed" 时保存
#: - "after_any_request" completed / aborted / error 都保存
#:
#: 注意："after_snapshot" 和 "after_any_request" 当前等价——保留两个名字是为
#: 后续区分更细粒度时机（如 on_message_end / on_turn_end 等）
SessionAutoSavePolicy = Literal[
    "never",
    "after_snapshot",
    "after_success",
    "after_any_request",
]


# ============================================================================
# SessionSyncConfig
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


class SessionSyncConfig(BaseModel):
    """Harness ↔ Session 同步行为配置。

    字段：
      auto_save_policy       控制 _maybe_auto_save_session 行为
      sync_harness_metadata  是否把 context.metadata 同步到
                             session.state.metadata["harness"]
      sync_agent_messages    每次 snapshot 完成后是否调
                             sync_agent_messages_to_session() 兜底
      strict_consistency     check_session_consistency 发现 error 时是否抛 RuntimeError
    """
    auto_save_policy: SessionAutoSavePolicy = "never"
    sync_harness_metadata: bool = True
    sync_agent_messages: bool = True
    strict_consistency: bool = False


# ============================================================================
# SessionConsistencyIssue
# ============================================================================


#: 常见 issue code——避免硬编码字符串
ISSUE_NO_SESSION = "no_session"
ISSUE_TURN_COUNT_MISMATCH = "turn_count_mismatch"
ISSUE_MESSAGES_MISMATCH = "messages_mismatch"
ISSUE_SNAPSHOT_COUNT_MISMATCH = "snapshot_count_mismatch"
ISSUE_LAST_SNAPSHOT_MISMATCH = "last_snapshot_mismatch"
ISSUE_AGENT_SESSION_MESSAGES_MISMATCH = "agent_session_messages_mismatch"
ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH = "harness_session_snapshot_mismatch"


IssueSeverity = Literal["info", "warning", "error"]


class SessionConsistencyIssue(BaseModel):
    """单个一致性问题。

    字段：
      code      机器可读的错误代码（见 ISSUE_* 常量）
      message   人类可读的错误描述
      severity  "info" / "warning" / "error"——只有 "error" 在 strict 模式下抛
      details   任意结构化详情（如 actual vs expected）
    """
    code: str
    message: str
    severity: IssueSeverity = "warning"
    details: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# SessionConsistencyReport
# ============================================================================


class SessionConsistencyReport(BaseModel):
    """一致性检查报告。

    字段：
      ok         没有 severity="error" 的 issue（warning / info 不影响 ok）
      issues     全部 issue 列表
      checked_at 检查发生的时间（毫秒）
    """
    ok: bool
    issues: list[SessionConsistencyIssue] = Field(default_factory=list)
    checked_at: int = Field(default_factory=_now_ms)

    def has_errors(self) -> bool:
        """是否有 severity='error' 的 issue。"""
        return any(issue.severity == "error" for issue in self.issues)

    def has_warnings(self) -> bool:
        """是否有 severity='warning' 的 issue。"""
        return any(issue.severity == "warning" for issue in self.issues)


__all__ = [
    "SessionAutoSavePolicy",
    "SessionSyncConfig",
    "IssueSeverity",
    "SessionConsistencyIssue",
    "SessionConsistencyReport",
    # issue code 常量
    "ISSUE_NO_SESSION",
    "ISSUE_TURN_COUNT_MISMATCH",
    "ISSUE_MESSAGES_MISMATCH",
    "ISSUE_SNAPSHOT_COUNT_MISMATCH",
    "ISSUE_LAST_SNAPSHOT_MISMATCH",
    "ISSUE_AGENT_SESSION_MESSAGES_MISMATCH",
    "ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH",
]

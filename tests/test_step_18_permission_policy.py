"""Step 18 — Permission Policy 基础测试。

覆盖：
- AllowAll / DenyAll / Default 三种策略的 check_tool_call 返回
- Default policy 规则：
  - read-only 工具默认允许
  - write/delete/shell/network/database 默认拒绝（require_approval）
  - 显式 allow / deny 命中
  - workspace_roots 控制 write 类
- parse_mcp_namespaced_tool 解析
- is_path_within_roots / extract_candidate_paths 行为
- audit log append / list / clear / counts
- policy 抛异常 → loop 不崩
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    AllowAllToolPermissionPolicy,
    DefaultToolPermissionPolicy,
    DenyAllToolPermissionPolicy,
    InMemoryToolPermissionAuditLog,
    ToolCall,
    ToolPermissionAuditRecord,
    ToolPermissionDecision,
    extract_candidate_paths,
    is_path_within_roots,
    parse_mcp_namespaced_tool,
)


def _tc(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments=args or {})


# ============================================================================
# 1. AllowAll / DenyAll
# ============================================================================


@pytest.mark.asyncio
async def test_allow_all_returns_allow() -> None:
    policy = AllowAllToolPermissionPolicy()
    decision = await policy.check_tool_call(
        tool_call=_tc("anything"), tool=None, messages=[],
    )
    assert decision.allow
    assert decision.policy_name == "allow_all"


@pytest.mark.asyncio
async def test_deny_all_returns_deny() -> None:
    policy = DenyAllToolPermissionPolicy()
    decision = await policy.check_tool_call(
        tool_call=_tc("anything"), tool=None, messages=[],
    )
    assert decision.denied
    assert decision.policy_name == "deny_all"
    assert decision.reason == "deny_all policy"


# ============================================================================
# 2. Default policy：read-only / write / shell 默认行为
# ============================================================================


@pytest.mark.asyncio
async def test_default_policy_allows_readonly_local() -> None:
    policy = DefaultToolPermissionPolicy()
    for name in ("read_file", "list_files", "search", "web_search"):
        decision = await policy.check_tool_call(
            tool_call=_tc(name), tool=None, messages=[],
        )
        assert decision.allow, f"{name} 应该被默认允许"


@pytest.mark.asyncio
async def test_default_policy_denies_write_without_workspace() -> None:
    policy = DefaultToolPermissionPolicy()
    decision = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/tmp/x"}), tool=None, messages=[],
    )
    # write_file 命中高风险 + 没有总开关 → require_approval
    assert decision.require_approval


@pytest.mark.asyncio
async def test_default_policy_denies_shell_by_default() -> None:
    policy = DefaultToolPermissionPolicy()
    decision = await policy.check_tool_call(
        tool_call=_tc("shell"), tool=None, messages=[],
    )
    assert decision.require_approval
    assert decision.metadata.get("category") == "shell"


@pytest.mark.asyncio
async def test_default_policy_allow_shell_when_switch_on() -> None:
    policy = DefaultToolPermissionPolicy(allow_shell=True)
    decision = await policy.check_tool_call(
        tool_call=_tc("shell"), tool=None, messages=[],
    )
    assert decision.allow


@pytest.mark.asyncio
async def test_default_policy_unknown_tool_requires_approval() -> None:
    policy = DefaultToolPermissionPolicy()
    decision = await policy.check_tool_call(
        tool_call=_tc("totally_unknown_xyz"), tool=None, messages=[],
    )
    assert decision.require_approval
    assert decision.metadata.get("reason") == "unknown_tool"


# ============================================================================
# 3. 显式 allow / deny
# ============================================================================


@pytest.mark.asyncio
async def test_default_explicit_deny_overrides_readonly() -> None:
    policy = DefaultToolPermissionPolicy(denied_tools={"read_file"})
    decision = await policy.check_tool_call(
        tool_call=_tc("read_file"), tool=None, messages=[],
    )
    assert decision.denied
    assert decision.metadata.get("matched_deny") == "denied_tools"


@pytest.mark.asyncio
async def test_default_explicit_allow_lets_write_through_with_workspace() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        workspace_roots=["/workspace"],
        allow_write=True,
    )
    decision = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/workspace/x.txt"}),
        tool=None, messages=[],
    )
    assert decision.allow


@pytest.mark.asyncio
async def test_default_write_outside_workspace_denied() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        workspace_roots=["/workspace"],
        allow_write=True,
    )
    decision = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/etc/passwd"}),
        tool=None, messages=[],
    )
    assert decision.denied
    assert decision.metadata.get("matched_deny") == "path_outside_workspace"


@pytest.mark.asyncio
async def test_default_write_without_workspace_denied() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        allow_write=True,
    )
    decision = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/tmp/x"}),
        tool=None, messages=[],
    )
    assert decision.denied
    assert decision.metadata.get("path_unchecked") is True


# ============================================================================
# 4. parse_mcp_namespaced_tool
# ============================================================================


def test_parse_mcp_namespaced_tool_roundtrip() -> None:
    assert parse_mcp_namespaced_tool("mcp__fs__read_file") == ("fs", "read_file")
    assert parse_mcp_namespaced_tool("mcp__github__create_issue") == ("github", "create_issue")


def test_parse_mcp_namespaced_tool_non_mcp_returns_none() -> None:
    assert parse_mcp_namespaced_tool("read_file") is None
    assert parse_mcp_namespaced_tool("mcp__only_one_part") is None
    assert parse_mcp_namespaced_tool("mcp___leading__") is None


# ============================================================================
# 5. Sandbox
# ============================================================================


def test_is_path_within_roots_inside() -> None:
    assert is_path_within_roots("/workspace/x.txt", ["/workspace"]) is True


def test_is_path_within_roots_escape() -> None:
    assert is_path_within_roots("/workspace/../etc/passwd", ["/workspace"]) is False


def test_is_path_within_roots_no_roots_returns_false() -> None:
    assert is_path_within_roots("/workspace/x", []) is False


def test_is_path_within_roots_outside() -> None:
    assert is_path_within_roots("/etc/passwd", ["/workspace"]) is False


def test_is_path_within_roots_relative_resolved(tmp_path) -> None:
    root = str(tmp_path)
    rel = str(tmp_path / "sub" / "x.txt")
    assert is_path_within_roots(rel, [root]) is True


def test_extract_candidate_paths_picks_known_fields() -> None:
    args = {
        "path": "/a/b",
        "file_path": "/c/d",
        "ignored": "x",
        "nested": {"target_path": "/e/f"},
    }
    paths = extract_candidate_paths(args)
    assert "/a/b" in paths
    assert "/c/d" in paths
    assert "/e/f" in paths
    assert "x" not in paths


def test_extract_candidate_paths_dedup() -> None:
    paths = extract_candidate_paths({"path": "/a", "file_path": "/a"})
    assert paths == ["/a"]


# ============================================================================
# 6. Audit log
# ============================================================================


def test_audit_append_list_clear() -> None:
    log = InMemoryToolPermissionAuditLog()
    log.append(ToolPermissionAuditRecord(
        tool_call_id="c1", tool_name="read_file",
        decision="allow", policy_name="default",
    ))
    log.append(ToolPermissionAuditRecord(
        tool_call_id="c2", tool_name="write_file",
        decision="deny", policy_name="default",
    ))
    records = log.list_records()
    assert len(records) == 2
    assert records[0].tool_call_id == "c1"
    assert records[1].decision == "deny"

    counts = log.counts()
    assert counts == {"allow": 1, "deny": 1, "require_approval": 0}

    log.clear()
    assert log.list_records() == []


def test_audit_list_records_is_copy() -> None:
    log = InMemoryToolPermissionAuditLog()
    log.append(ToolPermissionAuditRecord(
        tool_call_id="c1", tool_name="read_file",
        decision="allow", policy_name="default",
    ))
    snapshot = log.list_records()
    snapshot.clear()
    assert len(log.list_records()) == 1


# ============================================================================
# 7. ToolPermissionDecision 属性
# ============================================================================


def test_decision_properties() -> None:
    assert ToolPermissionDecision(decision="allow").allow is True
    assert ToolPermissionDecision(decision="deny").denied is True
    assert ToolPermissionDecision(decision="require_approval").require_approval is True

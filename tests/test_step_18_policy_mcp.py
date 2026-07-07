"""Step 18 — MCP × Permission Policy 集成测试。

覆盖：
- mcp__fs__read_file 默认允许（read-only MCP 前缀）
- mcp__fs__write_file 默认 require_approval
- mcp__shell__exec 默认 require_approval
- allowed_mcp_servers 放行
- denied_mcp_servers 拒绝
- allowed_mcp_tools = {"fs:read_file"} 放行特定 tool
- denied_mcp_tools = {"fs:write_file"} 拒绝特定 tool
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    DefaultToolPermissionPolicy,
    ToolCall,
)


def _tc(name: str) -> ToolCall:
    return ToolCall(id=f"call_{name}", name=name, arguments={})


# ============================================================================
# 1. 默认 read-only / write / shell MCP 行为
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_readonly_default_allow() -> None:
    policy = DefaultToolPermissionPolicy()
    for name in (
        "mcp__fs__read_file",
        "mcp__fs__list_files",
        "mcp__fs__get_info",
        "mcp__fs__search",
    ):
        d = await policy.check_tool_call(
            tool_call=_tc(name), tool=None, messages=[],
        )
        assert d.allow, f"{name} 默认应允许"


@pytest.mark.asyncio
async def test_mcp_write_default_require_approval() -> None:
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__fs__write_file"), tool=None, messages=[],
    )
    assert d.require_approval
    assert d.metadata.get("category") == "write"


@pytest.mark.asyncio
async def test_mcp_delete_default_require_approval() -> None:
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__fs__delete_file"), tool=None, messages=[],
    )
    assert d.require_approval
    assert d.metadata.get("category") == "delete"


@pytest.mark.asyncio
async def test_mcp_shell_default_require_approval() -> None:
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__shell__exec"), tool=None, messages=[],
    )
    assert d.require_approval


# ============================================================================
# 2. allowed_mcp_servers / denied_mcp_servers
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_mcp_server_lets_write_pass_with_switch() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_mcp_servers={"fs"},
        allow_write=True,
        workspace_roots=["/ws"],
    )
    d = await policy.check_tool_call(
        tool_call=ToolCall(
            id="c", name="mcp__fs__write_file",
            arguments={"path": "/ws/x"},
        ),
        tool=None, messages=[],
    )
    assert d.allow


@pytest.mark.asyncio
async def test_denied_mcp_server_blocks_read() -> None:
    policy = DefaultToolPermissionPolicy(
        denied_mcp_servers={"shell"},
    )
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__shell__read_file"), tool=None, messages=[],
    )
    assert d.denied
    assert d.metadata.get("matched_deny") == "denied_mcp_servers"
    assert d.metadata.get("server") == "shell"


# ============================================================================
# 3. allowed_mcp_tools / denied_mcp_tools（"server:tool" 格式）
# ============================================================================


@pytest.mark.asyncio
async def test_allowed_mcp_tool_specific_pass() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_mcp_tools={"github:list_issues"},
    )
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__github__list_issues"), tool=None, messages=[],
    )
    # list_issues 命中 read-only prefix "list"，本来也 allow；测一遍确认无 false deny
    assert d.allow


@pytest.mark.asyncio
async def test_denied_mcp_tool_specific_block() -> None:
    policy = DefaultToolPermissionPolicy(
        denied_mcp_tools={"github:delete_repo"},
    )
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__github__delete_repo"), tool=None, messages=[],
    )
    assert d.denied
    assert d.metadata.get("matched_deny") == "denied_mcp_tools"


@pytest.mark.asyncio
async def test_mcp_unknown_server_readonly_still_allow() -> None:
    """非 allowlist 配置时，read-only MCP tool 仍按默认规则放行。"""
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__unknowndb__list_tables"), tool=None, messages=[],
    )
    assert d.allow


# ============================================================================
# 4. case-insensitive
# ============================================================================


@pytest.mark.asyncio
async def test_deny_mcp_server_case_insensitive() -> None:
    policy = DefaultToolPermissionPolicy(denied_mcp_servers={"Shell"})
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__shell__exec"), tool=None, messages=[],
    )
    assert d.denied

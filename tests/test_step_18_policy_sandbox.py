"""Step 18 — Sandbox × Policy 集成测试。

覆盖：
- write path inside workspace allowed
- ../ escape denied
- absolute path outside workspace denied
- write path without workspace_roots denied
- read path without workspace_roots allowed but metadata path_unchecked=True
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    DefaultToolPermissionPolicy,
    ToolCall,
)


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(id=f"c_{name}", name=name, arguments=args)


# ============================================================================
# 1. write 类 path sandbox
# ============================================================================


@pytest.mark.asyncio
async def test_write_inside_workspace_allowed() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        workspace_roots=["/workspace"],
        allow_write=True,
    )
    d = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/workspace/x.txt"}),
        tool=None, messages=[],
    )
    assert d.allow


@pytest.mark.asyncio
async def test_write_parent_escape_denied() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        workspace_roots=["/workspace"],
        allow_write=True,
    )
    d = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/workspace/../etc/passwd"}),
        tool=None, messages=[],
    )
    assert d.denied
    assert d.metadata.get("matched_deny") == "path_outside_workspace"


@pytest.mark.asyncio
async def test_write_abs_outside_workspace_denied() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        workspace_roots=["/workspace"],
        allow_write=True,
    )
    d = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/etc/passwd"}),
        tool=None, messages=[],
    )
    assert d.denied


@pytest.mark.asyncio
async def test_write_without_workspace_denied() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"write_file"},
        allow_write=True,
    )
    d = await policy.check_tool_call(
        tool_call=_tc("write_file", {"path": "/tmp/x"}),
        tool=None, messages=[],
    )
    assert d.denied
    assert d.metadata.get("path_unchecked") is True


# ============================================================================
# 2. read 类 path sandbox（宽松）
# ============================================================================


@pytest.mark.asyncio
async def test_read_without_workspace_allowed_but_unchecked() -> None:
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("read_file", {"path": "/anywhere"}),
        tool=None, messages=[],
    )
    assert d.allow
    assert d.metadata.get("path_unchecked") is True


@pytest.mark.asyncio
async def test_read_inside_workspace_clean_allow() -> None:
    policy = DefaultToolPermissionPolicy(workspace_roots=["/workspace"])
    d = await policy.check_tool_call(
        tool_call=_tc("read_file", {"path": "/workspace/x"}),
        tool=None, messages=[],
    )
    assert d.allow
    assert "path_unchecked" not in d.metadata


@pytest.mark.asyncio
async def test_read_outside_workspace_requires_approval() -> None:
    """读类策略：roots 配了但路径不在 roots 内 → require_approval（保守）。"""
    policy = DefaultToolPermissionPolicy(workspace_roots=["/workspace"])
    d = await policy.check_tool_call(
        tool_call=_tc("read_file", {"path": "/outside"}),
        tool=None, messages=[],
    )
    assert d.require_approval
    assert d.metadata.get("matched_deny") == "path_outside_workspace"
    assert d.metadata.get("category") == "read"


@pytest.mark.asyncio
async def test_non_file_tool_with_path_arg_still_engages_sandbox() -> None:
    """非 file/path 工具（如 http_delete）若参数里塞了 path 字段，
    path sandbox 仍启用——避免通过抽象工具名绕过路径逃逸检测。

    这是 R3 修订的语义：path sandbox 在"工具名含 file/path/dir"
    **OR** "参数里有 path 字段"任一满足时启用。
    """
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"http_delete"},  # 显式 allow 跳过 category switch
        workspace_roots=["/workspace"],
    )
    d = await policy.check_tool_call(
        tool_call=_tc("http_delete", {"path": "/totally/outside"}),
        tool=None, messages=[],
    )
    # 名字含 "delete" → _path_category 判为 write → 路径在 roots 外 → deny
    assert d.denied
    assert d.metadata.get("matched_deny") == "path_outside_workspace"


@pytest.mark.asyncio
async def test_search_tool_no_path_arg_skips_sandbox() -> None:
    """search / web_search 等只读工具不含 file/path/dir 关键字——
    即使无 path 参数也不应误进 path sandbox。"""
    policy = DefaultToolPermissionPolicy()
    d = await policy.check_tool_call(
        tool_call=_tc("search", {"query": "hello"}), tool=None, messages=[],
    )
    assert d.allow


@pytest.mark.asyncio
async def test_tool_with_path_arg_engages_sandbox_even_if_name_has_no_file_keyword() -> None:
    """工具名不含 file/path/dir 关键字（如 mcp__fs__read / open），
    但参数里有 path 字段——path sandbox 仍要启用，否则路径逃逸检测失效。
    """
    policy = DefaultToolPermissionPolicy(workspace_roots=["/workspace"])
    # read-like 工具，但名字不含 file/path/dir
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__fs__read", {"path": "/etc/passwd"}),
        tool=None, messages=[],
    )
    assert d.require_approval
    assert d.metadata.get("matched_deny") == "path_outside_workspace"


@pytest.mark.asyncio
async def test_tool_with_path_arg_in_workspace_engages_sandbox_and_allows() -> None:
    """同上，但路径在 workspace 内——应 allow（path sandbox 启用了）。"""
    policy = DefaultToolPermissionPolicy(workspace_roots=["/workspace"])
    d = await policy.check_tool_call(
        tool_call=_tc("mcp__fs__read", {"path": "/workspace/x"}),
        tool=None, messages=[],
    )
    assert d.allow


@pytest.mark.asyncio
async def test_non_file_tool_without_path_arg_skips_sandbox() -> None:
    """工具名不含 file/path/dir，参数也无 path 字段——跳过 path sandbox。"""
    policy = DefaultToolPermissionPolicy(
        allowed_tools={"http_get"},  # 显式 allow 跳过 category switch
    )
    d = await policy.check_tool_call(
        tool_call=_tc("http_get", {"url": "https://example.com"}),
        tool=None, messages=[],
    )
    assert d.allow


# ============================================================================
# 3. MCP read / write 工具的 path sandbox
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_write_path_sandbox_enforced() -> None:
    policy = DefaultToolPermissionPolicy(
        allowed_mcp_servers={"fs"},
        workspace_roots=["/ws"],
        allow_write=True,
    )
    d_ok = await policy.check_tool_call(
        tool_call=ToolCall(
            id="c", name="mcp__fs__write_file",
            arguments={"file_path": "/ws/x"},
        ),
        tool=None, messages=[],
    )
    assert d_ok.allow

    d_bad = await policy.check_tool_call(
        tool_call=ToolCall(
            id="c2", name="mcp__fs__write_file",
            arguments={"file_path": "/etc/x"},
        ),
        tool=None, messages=[],
    )
    assert d_bad.denied

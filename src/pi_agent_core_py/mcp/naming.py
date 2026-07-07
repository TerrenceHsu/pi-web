"""MCP 命名约定（Step 16 新增 + 评审 R4 抽出）。

集中：
- 字符集：server name / tool name 都限制为 `[A-Za-z0-9_-]+`
  （去掉 `.`——很多 provider 的 tool/function name 不允许点号）
- 长度：namespaced tool name `mcp__{server}__{tool}` ≤ 64 字符
  （Anthropic / OpenAI 的 tool name 上限常见为 64）
- 拼接：`make_namespaced_tool_name(server, tool)`
- 校验：`validate_namespace_part(kind, value)`

config.py / client.py / adapter.py 都从这里 import，避免重复实现。
"""
from __future__ import annotations

import re

#: MCP server name / tool name 合法字符集。
#: 收紧到 `[A-Za-z0-9_-]+`（不含点号）——Anthropic / OpenAI 等很多 provider
#: 的 tool/function name 只允许这套字符；含 `.` 会被 provider schema 拒绝。
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


#: namespaced tool name 字符上限。
#: `mcp__{server}__{tool}` 总长度不超过此值，避免触发 provider tool name 长度限制。
#: Anthropic / OpenAI 常见上限是 64 字符。
MAX_NAMESPACED_NAME_LEN = 64


def validate_namespace_part(*, kind: str, value: str) -> None:
    """校验 server name / tool name 字符集。

    kind: "server_name" / "tool name" 等，仅用于错误信息。
    value: 待校验字符串。

    失败抛 ValueError（含 kind + value + 允许的字符集说明）。
    """
    if not value or not MCP_NAME_RE.match(value):
        raise ValueError(
            f"invalid {kind} {value!r}; only [A-Za-z0-9_-] allowed"
        )


def make_namespaced_tool_name(server_name: str, tool_name: str) -> str:
    """拼 `mcp__{server}__{tool}` 并校验字符集 + 长度。

    失败抛 ValueError。成功返回 namespaced name。
    """
    validate_namespace_part(kind="server_name", value=server_name)
    validate_namespace_part(kind="tool name", value=tool_name)
    namespaced = f"mcp__{server_name}__{tool_name}"
    if len(namespaced) > MAX_NAMESPACED_NAME_LEN:
        raise ValueError(
            f"MCP namespaced tool name too long ({len(namespaced)} > {MAX_NAMESPACED_NAME_LEN}): "
            f"{namespaced!r}"
        )
    return namespaced


__all__ = [
    "MCP_NAME_RE",
    "MAX_NAMESPACED_NAME_LEN",
    "validate_namespace_part",
    "make_namespaced_tool_name",
]

"""工具权限策略——Step 18。

在 `_execute_tool_with_hooks` 中接入：

```text
registry.get
before_tool_call
（hook 改 tool_call 后重查 tool）
permission_policy.check_tool_call
  → decision="deny"            → ToolResult(is_error, error_type="ToolPermissionDenied")
  → decision="require_approval" → ToolResult(is_error, error_type="ToolApprovalRequired")
  → decision="allow"           → 继续 validate_tool_arguments / execute / after_tool_call
validate_tool_arguments
tool.execute
after_tool_call
```

设计要点：
- `permission_policy is None` → loop 不做权限检查，保持旧行为（向后兼容）
- `policy` 抛异常 / 返回非法对象 → 不让 loop 崩，
  转成 `error_type="ToolPermissionPolicyError"` ToolResult
- `require_approval` 在 Step 18 中按"不执行工具"处理——没有交互式审批 UI，
  loop 把它转成 `error_type="ToolApprovalRequired"` 的 is_error ToolResult，
  语义上等同于 deny 但 error_type 区分（未来 Step 20 UI 能据此弹审批框）
- 每次检查都写一条 `ToolPermissionAuditRecord`（如果 audit_log 不为 None）

实现策略：
- `AllowAllToolPermissionPolicy`：测试 / 开发
- `DenyAllToolPermissionPolicy`：测试
- `DefaultToolPermissionPolicy`：默认安全策略
  - 显式 deny 优先级最高（denied_tools / denied_mcp_servers / denied_mcp_tools）
  - 显式 allow 名单是最高级放行配置（不受 allow_write=False 等类别开关阻止）
  - read-only 工具名模式默认允许（含 MCP `mcp__*_read* / list* / get* / search*`）
  - 高风险关键字（write / delete / shell / network / sql / post / put / patch）
    默认 `require_approval`；Step 18 没有 UI，loop 按拒绝执行处理
"""
from __future__ import annotations

import abc
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..messages import AgentMessage, ToolCall
from ..tools import AgentTool

# ============================================================================
# Decision 类型
# ============================================================================


#: 权限决策类型。
#: - "allow"            允许执行
#: - "deny"             拒绝；返回 is_error ToolResult
#: - "require_approval" 需要人工审批；Step 18 按 deny 处理（error_type 区分）
PermissionDecisionType = Literal["allow", "deny", "require_approval"]


class ToolPermissionDecision(BaseModel):
    """单次工具调用的权限决策。

    `metadata` 给 audit log / ToolResult.details.policy.metadata 用；可放
    `path_unchecked=True` / `matched_deny="denied_tools"` 等诊断字段。
    """

    decision: PermissionDecisionType
    reason: str | None = None
    policy_name: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def allow(self) -> bool:
        return self.decision == "allow"

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def require_approval(self) -> bool:
        return self.decision == "require_approval"


# ============================================================================
# Policy 抽象
# ============================================================================


class ToolPermissionPolicy(abc.ABC):
    """工具权限策略抽象。

    子类设置 `name`（写入 audit log / ToolResult.details.policy.policy_name）
    并实现 `check_tool_call`。
    """

    name: str = "base"

    @abc.abstractmethod
    async def check_tool_call(
        self,
        *,
        tool_call: ToolCall,
        tool: AgentTool | None,
        messages: list[AgentMessage],
    ) -> ToolPermissionDecision:
        ...


# ============================================================================
# 测试 / 开发用策略
# ============================================================================


class AllowAllToolPermissionPolicy(ToolPermissionPolicy):
    """全部放行——用于测试 / 开发。"""

    name = "allow_all"

    async def check_tool_call(
        self,
        *,
        tool_call: ToolCall,
        tool: AgentTool | None,
        messages: list[AgentMessage],
    ) -> ToolPermissionDecision:
        return ToolPermissionDecision(
            decision="allow",
            reason="allow_all policy",
            policy_name=self.name,
        )


class DenyAllToolPermissionPolicy(ToolPermissionPolicy):
    """全部拒绝——用于测试。"""

    name = "deny_all"

    async def check_tool_call(
        self,
        *,
        tool_call: ToolCall,
        tool: AgentTool | None,
        messages: list[AgentMessage],
    ) -> ToolPermissionDecision:
        return ToolPermissionDecision(
            decision="deny",
            reason="deny_all policy",
            policy_name=self.name,
        )


# ============================================================================
# MCP 工具名解析
# ============================================================================


def parse_mcp_namespaced_tool(name: str) -> tuple[str, str] | None:
    """把 `mcp__{server}__{tool}` 解析成 (server, tool)。

    非 MCP 工具（不以 `mcp__` 开头 / 解析失败）返回 None。
    """
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3:
        return None
    _, server, tool = parts
    if not server or not tool:
        return None
    return server, tool


# ============================================================================
# 默认安全策略
# ============================================================================


#: 默认视为 read-only 的本地工具名（小写比较）。
_DEFAULT_READONLY_LOCAL = {
    "search", "web_search",
    "read_file", "list_files", "get_file_info",
    "fetch_url_readonly",
}

#: 默认视为高风险的本地工具名（小写比较）。
_DEFAULT_HIGH_RISK_LOCAL = {
    "shell", "run_shell", "exec", "execute_command", "terminal",
    "write_file", "delete_file", "remove_file", "move_file",
    "rename_file", "patch_file", "apply_patch",
    "http_post", "http_put", "http_delete",
    "database_write", "sql_execute",
}

#: MCP tool name 中出现这些关键字（小写 contains）视为高风险。
_DEFAULT_HIGH_RISK_MCP_KEYWORDS = {
    "write", "delete", "remove", "patch", "move", "rename",
    "exec", "shell", "terminal",
    "post", "put", "database", "sql",
}

#: MCP tool name 中以这些前缀开头（小写）视为 read-only。
_DEFAULT_READONLY_MCP_PREFIXES = ("read", "list", "get", "search")


class DefaultToolPermissionPolicy(ToolPermissionPolicy):
    """默认安全策略——read-only 默认放行，高风险默认拒绝。

    优先级（自顶向下）：

    1. **显式 deny**：`denied_tools` / `denied_mcp_servers` / `denied_mcp_tools`
       命中即 `decision="deny"`。
    2. **显式 allow 名单**（最高级放行配置）：`allowed_tools` /
       `allowed_mcp_servers` / `allowed_mcp_tools` 命中 → 跳过类别判断，
       直接走 path sandbox；都通过才 `decision="allow"`。
       显式 allowlist **不被** `allow_write / allow_delete / allow_shell /
       allow_network=False` 阻止——后者只是 "类别批量放行" 开关，
       不是 allowlist 之上的额外限制。
    3. **read-only 模式匹配**：默认只读集合（`_DEFAULT_READONLY_LOCAL` /
       MCP 前缀 `read/list/get/search`）→ 进 path sandbox。
    4. **高风险模式匹配**：默认高风险集合 → 若对应 `allow_*` 总开关为 False
       则 `decision="require_approval"`；开关为 True 才进 path sandbox。
    5. 其它未知工具 → `require_approval`（保守拒绝）。

    配置项语义：

    - `allowed_mcp_tools` / `denied_mcp_tools`：`"server:tool"` 格式
      （例如 `"fs:read_file"`）；解析时用 `parse_mcp_namespaced_tool`
      得到的 (server, tool) 拼接比对。
    - `workspace_roots`：path sandbox 的合法根（path sandbox 在以下条件之一满足时
      启用：工具名含 file/path/dir 关键字，**或** 参数里能提取出 path 字段）。
      - 写类 + roots 空 → `deny`
      - 写类 + 路径不在 roots 内 → `deny`
      - 读类 file tool + roots 空 → `allow` + `metadata.path_unchecked=True`
      - 读类 file tool + 路径不在 roots 内 → `require_approval`
      - 工具名不含 file/path/dir **且** 参数无 path 字段（如 http_delete /
        sql_execute / shell）→ 跳过 path sandbox，由 category switch 控制
    - `allow_write / allow_delete / allow_shell / allow_network`：
      **类别批量放行开关**——仅当工具既不在显式 allowlist 也不在显式
      denylist 时生效。True 表示"这一类工具我允许走 path sandbox"，False
      表示"按 require_approval 处理"。
    """

    name = "default"

    def __init__(
        self,
        *,
        allowed_tools: set[str] | None = None,
        denied_tools: set[str] | None = None,
        allowed_mcp_servers: set[str] | None = None,
        denied_mcp_servers: set[str] | None = None,
        allowed_mcp_tools: set[str] | None = None,
        denied_mcp_tools: set[str] | None = None,
        read_only_tool_names: set[str] | None = None,
        workspace_roots: list[str] | None = None,
        allow_network: bool = False,
        allow_shell: bool = False,
        allow_write: bool = False,
        allow_delete: bool = False,
    ) -> None:
        self.allowed_tools = {n.lower() for n in (allowed_tools or set())}
        self.denied_tools = {n.lower() for n in (denied_tools or set())}
        self.allowed_mcp_servers = {s.lower() for s in (allowed_mcp_servers or set())}
        self.denied_mcp_servers = {s.lower() for s in (denied_mcp_servers or set())}
        # MCP tool allowlist 用 "server:tool" 格式（小写）
        self.allowed_mcp_tools = {k.lower() for k in (allowed_mcp_tools or set())}
        self.denied_mcp_tools = {k.lower() for k in (denied_mcp_tools or set())}
        self.read_only_tool_names = {n.lower() for n in (read_only_tool_names or set())}
        self.workspace_roots = list(workspace_roots or [])
        self.allow_network = allow_network
        self.allow_shell = allow_shell
        self.allow_write = allow_write
        self.allow_delete = allow_delete

    async def check_tool_call(
        self,
        *,
        tool_call: ToolCall,
        tool: AgentTool | None,
        messages: list[AgentMessage],
    ) -> ToolPermissionDecision:
        name = tool_call.name
        name_lower = name.lower()
        mcp = parse_mcp_namespaced_tool(name)
        # 局部 import 避免 policy/__init__ 顺序问题
        from .sandbox import extract_candidate_paths, is_path_within_roots

        # --- 1. 显式 deny 优先 ---
        if name_lower in self.denied_tools:
            return ToolPermissionDecision(
                decision="deny",
                reason=f"tool {name!r} in denied_tools",
                policy_name=self.name,
                metadata={"matched_deny": "denied_tools"},
            )
        if mcp is not None:
            server, mcp_tool = mcp
            if server.lower() in self.denied_mcp_servers:
                return ToolPermissionDecision(
                    decision="deny",
                    reason=f"mcp server {server!r} in denied_mcp_servers",
                    policy_name=self.name,
                    metadata={"matched_deny": "denied_mcp_servers", "server": server},
                )
            key = f"{server}:{mcp_tool}".lower()
            if key in self.denied_mcp_tools:
                return ToolPermissionDecision(
                    decision="deny",
                    reason=f"mcp tool {key!r} in denied_mcp_tools",
                    policy_name=self.name,
                    metadata={
                        "matched_deny": "denied_mcp_tools",
                        "server": server,
                        "tool": mcp_tool,
                    },
                )

        # --- 2. 显式 allow 名单 ---
        explicit_allow = False
        if name_lower in self.allowed_tools:
            explicit_allow = True
        elif mcp is not None:
            server, mcp_tool = mcp
            if server.lower() in self.allowed_mcp_servers:
                explicit_allow = True
            elif f"{server}:{mcp_tool}".lower() in self.allowed_mcp_tools:
                explicit_allow = True

        # --- 3/4. 没有显式 allow，按模式匹配判断 ---
        if not explicit_allow:
            if mcp is not None:
                server, mcp_tool = mcp
                mcp_t_lower = mcp_tool.lower()
                is_readonly = any(
                    mcp_t_lower.startswith(p) for p in _DEFAULT_READONLY_MCP_PREFIXES
                )
                is_high_risk = any(
                    k in mcp_t_lower for k in _DEFAULT_HIGH_RISK_MCP_KEYWORDS
                )
            else:
                is_readonly = (
                    name_lower in _DEFAULT_READONLY_LOCAL
                    or name_lower in self.read_only_tool_names
                )
                is_high_risk = name_lower in _DEFAULT_HIGH_RISK_LOCAL

            # 高风险优先于 readonly——保守
            if is_high_risk:
                # 高风险必须显式 allow 名单命中且总开关开启
                category = self._high_risk_category(name_lower, mcp)
                allowed_by_switch = self._category_allowed(category)
                if not allowed_by_switch:
                    return ToolPermissionDecision(
                        decision="require_approval",
                        reason=(
                            f"high-risk tool category {category!r} not "
                            f"explicitly allowed"
                        ),
                        policy_name=self.name,
                        metadata={
                            "category": category,
                            "high_risk": True,
                        },
                    )
                # 总开关开启：继续走 path sandbox
            elif not is_readonly:
                # 既不是 read-only 也不是显式 allow——保守 require_approval
                return ToolPermissionDecision(
                    decision="require_approval",
                    reason=f"tool {name!r} not classified as read-only",
                    policy_name=self.name,
                    metadata={"reason": "unknown_tool"},
                )

        # --- 5. path sandbox（只对 file/path 类工具 OR 参数里有 path 字段生效） ---
        # http_delete / sql_execute / shell 等非文件工具原则上不走 path sandbox；
        # 它们由 category switch / allowlist / denylist 控制。
        #
        # 但若工具名不含 file/path/dir 关键字（如 mcp__fs__read /
        # mcp__workspace__open），而参数里明显有 path/file_path 等字段，
        # 仍要进 path sandbox——否则路径逃逸检测会失效。
        candidate_paths = extract_candidate_paths(tool_call.arguments)
        is_path_sensitive = (
            self._is_file_path_tool(name_lower, mcp) or bool(candidate_paths)
        )
        if not is_path_sensitive:
            return ToolPermissionDecision(
                decision="allow",
                reason="non-file tool; controlled by category switches / allowlist",
                policy_name=self.name,
            )

        path_category = self._path_category(name_lower, mcp)
        roots = self.workspace_roots

        if path_category == "write":
            # 写类：必须有 workspace_roots 且路径在 roots 内
            if not roots:
                return ToolPermissionDecision(
                    decision="deny",
                    reason="write-class tool without workspace_roots configured",
                    policy_name=self.name,
                    metadata={"path_unchecked": True, "category": "write"},
                )
            for p in candidate_paths:
                if not is_path_within_roots(p, roots):
                    return ToolPermissionDecision(
                        decision="deny",
                        reason=f"path {p!r} outside workspace_roots",
                        policy_name=self.name,
                        metadata={
                            "path": p,
                            "category": "write",
                            "matched_deny": "path_outside_workspace",
                        },
                    )
        else:
            # 读类 file tool：
            #   - roots 空 → allow + path_unchecked=True（兼容旧行为）
            #   - roots 非空 + path 在 roots 内 → 干净 allow
            #   - roots 非空 + path 在 roots 外 → require_approval（保守）
            if not roots and candidate_paths:
                return ToolPermissionDecision(
                    decision="allow",
                    reason="read-only tool; path unchecked because workspace_roots is empty",
                    policy_name=self.name,
                    metadata={"path_unchecked": True},
                )
            for p in candidate_paths:
                if not is_path_within_roots(p, roots):
                    return ToolPermissionDecision(
                        decision="require_approval",
                        reason=f"read path {p!r} outside workspace_roots",
                        policy_name=self.name,
                        metadata={
                            "path": p,
                            "category": "read",
                            "matched_deny": "path_outside_workspace",
                        },
                    )

        return ToolPermissionDecision(
            decision="allow",
            reason="default policy allow",
            policy_name=self.name,
        )

    # ------------------------------------------------------------------
    # 内部分类辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _is_file_path_tool(
        name_lower: str,
        mcp: tuple[str, str] | None,
    ) -> bool:
        """工具是否是 file/path 类（按工具名）。

        http_delete / sql_execute / shell 等非文件工具返回 False——它们的
        "高风险性"已经由 category switch / allowlist / denylist 控制，
        不应该让 path sandbox 干扰判断。

        判定依据：工具名（或 MCP tool 名）含 file / path / dir 关键字。

        注意：path sandbox 是否实际启用还看 `extract_candidate_paths(arguments)`
        是否非空——两者 OR 才进 sandbox。这覆盖了名字不含关键字但参数里有
        path 字段的工具（如 mcp__fs__read / mcp__workspace__open）。
        """
        target = mcp[1].lower() if mcp is not None else name_lower
        return any(k in target for k in ("file", "path", "dir"))

    @staticmethod
    def _high_risk_category(
        name_lower: str,
        mcp: tuple[str, str] | None,
    ) -> str:
        if mcp is not None:
            mcp_t = mcp[1].lower()
            if any(k in mcp_t for k in ("delete", "remove")):
                return "delete"
            if any(k in mcp_t for k in ("write", "patch", "move", "rename")):
                return "write"
            if any(k in mcp_t for k in ("shell", "exec", "terminal")):
                return "shell"
            if any(k in mcp_t for k in ("post", "put")):
                return "network"
            if any(k in mcp_t for k in ("sql", "database")):
                return "database"
        else:
            if any(k in name_lower for k in ("delete", "remove")):
                return "delete"
            if any(k in name_lower for k in ("write", "patch", "move", "rename")):
                return "write"
            if any(k in name_lower for k in ("shell", "exec", "terminal")):
                return "shell"
            if any(k in name_lower for k in ("http_post", "http_put", "http_delete")):
                return "network"
            if any(k in name_lower for k in ("sql", "database")):
                return "database"
        return "unknown"

    def _category_allowed(self, category: str) -> bool:
        if category == "write":
            return self.allow_write
        if category == "delete":
            return self.allow_delete
        if category == "shell":
            return self.allow_shell
        if category == "network":
            return self.allow_network
        if category == "database":
            return self.allow_write  # database write 归到 write 总开关
        return False

    @staticmethod
    def _path_category(
        name_lower: str,
        mcp: tuple[str, str] | None,
    ) -> Literal["read", "write"]:
        """判定工具属于 read 还是 write 类（path sandbox 用）。"""
        if mcp is not None:
            mcp_t = mcp[1].lower()
            if any(k in mcp_t for k in (
                "write", "delete", "remove", "patch", "move", "rename",
            )):
                return "write"
            return "read"
        if name_lower in _DEFAULT_HIGH_RISK_LOCAL and any(
            k in name_lower for k in (
                "write", "delete", "remove", "patch", "move", "rename",
            )
        ):
            return "write"
        if name_lower in _DEFAULT_READONLY_LOCAL:
            return "read"
        return "read"


__all__ = [
    "PermissionDecisionType",
    "ToolPermissionDecision",
    "ToolPermissionPolicy",
    "AllowAllToolPermissionPolicy",
    "DenyAllToolPermissionPolicy",
    "DefaultToolPermissionPolicy",
    "parse_mcp_namespaced_tool",
]

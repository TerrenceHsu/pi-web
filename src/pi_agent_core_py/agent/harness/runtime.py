"""Agent harness request-lifecycle coordinator.
+ Session Memory 集成（Step 12）+ Harness ↔ Session 同步增强（Step 13）
+ Skills / Prompt Templates 注入（Step 14）+ Compaction / Branch Summary（Step 15）
+ MCP lifecycle 集成（Step 17）+ Permission / Approval Policy（Step 18）。

AgentHarness **不替代** Agent，是 Agent 外层的执行协调层：

```text
Agent        ：状态机 / queue / abort / event stream / messages（不感知 skill / MCP / policy）
AgentHarness ：请求生命周期编排 / 外部 hook / 事件观察 / 共享上下文 / skill 注入 /
               MCP lifecycle / 工具权限策略与审计
RequestSnapshot：一次请求快照；内部 turns 是逐次 LLM 调用快照
SessionMemory：一个会话累计的 messages + snapshots + metadata（Step 12）
Skill        ：可复用行为提示单元（Step 14）
MCPRegistry  ：多 MCP server 管理（Step 16/17）
ToolPermissionPolicy：工具调用决策（allow / deny / require_approval）（Step 18）
```

四个 hook：
- `before_request(ctx)`  请求前：审计、metadata 写入、策略检查（抛异常则阻止请求）
- `after_request(ctx)`   请求后：统计、结果检查（抛异常则 harness 报错，但 Agent 已完成）
- `on_event(ev, ctx)`    事件观察（抛异常不影响 Agent 主流程）
- `on_error(exc, ctx)`   错误观察（抛异常追加到 metadata["on_error_errors"]，不递归）

HarnessPhase（与 AgentStatus 独立）：
- `idle` / `before_request` / `running` / `after_request` / `error`

Step 11 新增：snapshot 字段 + _finish_snapshot（异常路径先 _fail 后 finish）
Step 12 新增：session 字段 + attach/detach_session + save/load_session
              + _finish_snapshot 自动 append
Step 13 新增：session_store / session_sync_config / configure_session_sync /
              sync_session_metadata / check_session_consistency / export_import_session /
              _post_finish_snapshot（sync + consistency + auto-save）

Step 14 新增（Skills / Prompt Templates）：skill_registry / skill_injection_config /
              attach_skills / detach_skills / enable_skill / disable_skill /
              render_system_prompt / run_prompt+run_continue 加 skill_selection /
              _run_one 临时替换 + finally 还原 agent.system_prompt /
              渲染失败作为 before-agent 错误路径 / FakeClient 加 system_prompt 记录

Step 15 新增（Compaction / Branch Summary）：
- `AgentHarness.last_compaction_result` / `last_branch_summary` 字段
- `AgentHarness.compact_context(config=, summary_generator=)`：基于 agent.state.messages 压缩
- `AgentHarness.compact_session(config=, summary_generator=)`：基于 session.messages 压缩
- `AgentHarness.create_branch_summary(config=, summary_generator=)`
- `AgentHarness.get_branch_summary(index=-1)` / `clear_branch_summaries()`
- compaction 写入 session.compactions；applied 时同步 agent + session messages
- branch summary 写入 session.branch_summaries；**不修改** messages
- compaction **不删除** snapshots——历史记录永久保留

Step 17 新增（MCP Harness Integration）：
- `AgentHarness.attach_mcp_servers(configs, *, auto_register_tools=True, registry=None)`
- `AgentHarness.detach_mcp_servers()` / `refresh_mcp_tools()`
- `AgentHarness.list_mcp_servers()` / `list_mcp_tools()` / `mcp_registry` property
- Harness 持有 `MCPRegistry`；MCP tools 自动注册进 Agent ToolRegistry
- refresh 时移除 stale MCP tools；detach 时清理所有 MCP tools
- MCP server/tool 状态写入 `context.metadata["mcp"]` → snapshot / session
- `AgentHarness.close()` 改为 `async`，自动调 `detach_mcp_servers()`

Step 18 新增（Permission / Approval Policy）：
- `AgentHarness(permission_policy=, permission_audit_log=)` 构造入参
- `permission_policy`：`ToolPermissionPolicy | None`——None 关闭权限检查
- `permission_audit_log`：默认创建 `InMemoryToolPermissionAuditLog`
- `set_permission_policy(policy)`：运行期替换（含 None）+ 同步到 agent
- `list_permission_audit_records()` / `clear_permission_audit_records()`
- policy summary 写入 `context.metadata["policy"]`：
  `{policy_name, audit_count, allowed_count, denied_count, approval_required_count}`
- _run_one 开头 + 每个 finish_snapshot 前刷新 metadata
- snapshot / session.metadata["harness"]["context_metadata"]["policy"] 自动捕获
- audit records **不**全量进 snapshot metadata——通过 list_permission_audit_records() 读

P2-B：可选 ``tool_approval_handler`` 把 require_approval 交给 Web UI；未配置时
仍按安全错误处理，保持 Step 18 向后兼容。
              Web UI（Step 20 PolicyPage 才展示 audit）/ 多用户权限 / OAuth / RBAC /
              企业 secret vault / RAG / Long-term Memory / MCP resources / MCP prompts（Step 19）。
"""
from __future__ import annotations

import inspect
import time
import typing
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...mcp import (
    MCPAgentTool,
    MCPRegistry,
    MCPServerConfig,
    MCPServerState,
)
from ...mcp.prompts import (
    MCPPromptInfo,
    MCPPromptSkillAdapter,
)
from ...policy import (
    InMemoryToolPermissionAuditLog,
    ToolApprovalHandler,
    ToolPermissionAuditRecord,
    ToolPermissionPolicy,
)
from ..events import AgentEvent, AgentRequestType
from ..messages import AgentMessage
from ..runtime import Agent, AgentState
from .compaction import (
    BranchSummary,
    BranchSummaryConfig,
    CompactionConfig,
    CompactionResult,
    CompactionRetryCallback,
    CompactionRetryPolicy,
    CompactionRetryPredicate,
    SummaryGenerator,
    compact_messages,
)
from .compaction import (
    create_branch_summary as _create_branch_summary_fn,
)
from .compaction.budget import ContextEstimate
from .session import (
    SessionMemory,
    SessionStore,
    serialize_messages,
)
from .session.sync import (
    ISSUE_AGENT_SESSION_MESSAGES_MISMATCH,
    ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH,
    ISSUE_LAST_SNAPSHOT_MISMATCH,
    ISSUE_MESSAGES_MISMATCH,
    ISSUE_NO_SESSION,
    ISSUE_TURN_COUNT_MISMATCH,
    SessionAutoSavePolicy,
    SessionConsistencyIssue,
    SessionConsistencyReport,
    SessionSyncConfig,
)
from .skill_loader import (
    SkillFileLoader,
    SkillLoadConfig,
)
from .skills import (
    PromptTemplateRenderError,
    Skill,
    SkillInjectionConfig,
    SkillNotFoundError,
    SkillRegistrationError,
    SkillRegistry,
    SkillSelection,
    render_skill_block,
)
from .snapshot import RequestSnapshot, SnapshotBuilder, SnapshotStatus, TurnSnapshot

# ============================================================================
# HarnessPhase / HarnessContext
# ============================================================================


#: Harness 层状态——独立于 AgentStatus。
#: - "idle"           无请求在跑
#: - "before_request" 正在跑 before_request hooks
#: - "running"        Agent 正在处理请求
#: - "after_request"  正在跑 after_request hooks
#: - "error"          Harness 自身或 hook 异常
HarnessPhase = Literal[
    "idle", "before_request", "running", "after_request", "error",
]


class HarnessContext(BaseModel):
    """请求生命周期上下文。每次 run_prompt / run_continue 复用同一实例。"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    phase: HarnessPhase = "idle"
    agent: Agent
    request_type: AgentRequestType | None = None
    user_text: str | None = None
    messages_before: list[AgentMessage] = Field(default_factory=list)
    messages_after: list[AgentMessage] = Field(default_factory=list)
    last_event: AgentEvent | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: int | None = None
    ended_at: int | None = None
    # Step 11：当前/最近一次请求的 snapshot（finish 后保持引用；新请求会替换）
    snapshot: RequestSnapshot | None = None
    # Step 14：本次请求 skill 注入信息
    selected_skills: list[str] = Field(default_factory=list)
    skill_metadata: dict[str, Any] = Field(default_factory=dict)
    rendered_system_prompt: str | None = None


# ============================================================================
# Hook 类型
# ============================================================================


#: Hook 返回 None 或 Awaitable[None]；harness 用 inspect.isawaitable 同时支持 sync/async
BeforeRequestHook = Callable[[HarnessContext], "None | Awaitable[None]"]
AfterRequestHook = Callable[[HarnessContext], "None | Awaitable[None]"]
OnEventHook = Callable[[AgentEvent, HarnessContext], "None | Awaitable[None]"]
OnErrorHook = Callable[[Exception, HarnessContext], "None | Awaitable[None]"]


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# AgentHarness
# ============================================================================


class AgentHarness:
    """Agent 外层请求生命周期协调器。

    用法：

    ```text
    agent = Agent(...)
    harness = AgentHarness(
        agent,
        before_request=[audit_hook],
        after_request=[stats_hook],
        on_event=[log_hook],
        on_error=[alert_hook],
    )
    msgs = await harness.run_prompt("hello")
    ```
    """

    def __init__(
        self,
        agent: Agent,
        *,
        before_request: list[BeforeRequestHook] | None = None,
        after_request: list[AfterRequestHook] | None = None,
        on_event: list[OnEventHook] | None = None,
        on_error: list[OnErrorHook] | None = None,
        metadata: dict[str, Any] | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
        permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
        tool_approval_handler: ToolApprovalHandler | None = None,
    ):
        self.agent = agent
        self.before_request_hooks: list[BeforeRequestHook] = (
            list(before_request) if before_request else []
        )
        self.after_request_hooks: list[AfterRequestHook] = (
            list(after_request) if after_request else []
        )
        self.on_event_hooks: list[OnEventHook] = list(on_event) if on_event else []
        self.on_error_hooks: list[OnErrorHook] = list(on_error) if on_error else []

        self.context = HarnessContext(agent=agent)
        if metadata:
            self.context.metadata = dict(metadata)

        # 通过 Agent.subscribe 观察 AgentEvent
        self._unsubscribe: Callable[[], None] | None = agent.subscribe(
            self._handle_agent_event,
        )

        # Step 11：snapshot 状态
        self.last_snapshot: RequestSnapshot | None = None
        self.snapshots: list[RequestSnapshot] = []
        self._snapshot_builder: SnapshotBuilder | None = None

        # Step 12：session 状态
        self.session: SessionMemory | None = None

        # Step 13：同步配置 + store + 一致性报告
        self.session_store: SessionStore | None = None
        self.session_sync_config: SessionSyncConfig = SessionSyncConfig()
        self.last_consistency_report: SessionConsistencyReport | None = None

        # Step 14：Skills / Prompt Templates
        self.skill_registry: SkillRegistry | None = None
        self.skill_injection_config: SkillInjectionConfig = SkillInjectionConfig()

        # Step 15：Compaction / Branch Summary
        self.last_compaction_result: CompactionResult | None = None
        self.last_branch_summary: BranchSummary | None = None

        # Step 17：MCP lifecycle
        # _mcp_registry：当前绑定的 MCPRegistry（None 表示未 attach）
        # _mcp_tool_names：由 Harness 自动注册到 Agent ToolRegistry 的 MCP tool names
        #                  用于 refresh / detach 时清理旧工具（避免 stale）
        self._mcp_registry: MCPRegistry | None = None
        self._mcp_tool_names: set[str] = set()

        # Step 18：工具权限策略 / 审计
        # Harness 持有 policy + audit_log；每次 _run_one 前同步到 agent
        # （agent 只持有引用——它本身不做策略决策，只是透传给 loop）
        self.permission_policy: ToolPermissionPolicy | None = permission_policy
        self.tool_approval_handler: ToolApprovalHandler | None = tool_approval_handler
        self.permission_audit_log: InMemoryToolPermissionAuditLog = (
            permission_audit_log if permission_audit_log is not None
            else InMemoryToolPermissionAuditLog()
        )
        # 把 audit_log / policy 同步到 agent（loop 实际读 agent 的字段）
        self.agent.permission_policy = self.permission_policy
        self.agent.permission_audit_log = self.permission_audit_log
        self.agent.tool_approval_handler = self.tool_approval_handler

        # Step 19：最近一次 attach_mcp_prompts_as_skills 中被吞掉的错误
        # （完整 list[{server, prompt, error}]——区别于 metadata 中只存摘要）
        # UI / 调试时可直接读这个字段；metadata 摘要为了不膨胀只存 count + first_error
        self.last_mcp_prompt_skill_errors: list[dict[str, str]] = []

        # Step 22+ (Sandbox / Slash / Multi-Agent) 未包含在本副本——
        # 见 D:\LLMTutorial\pi\pi-py 主仓库

    # ----------------------------------------------------------------------
    # 入口：run_prompt / run_continue
    # ----------------------------------------------------------------------

    async def run_prompt(
        self,
        user_text: str,
        *,
        skill_selection: SkillSelection | None = None,
        system_prompt_suffix: str | None = None,
    ) -> list[AgentMessage]:
        """包装 agent.prompt(user_text)，前后跑 hooks。

        Step 14：可选传 skill_selection——单次请求级 skill 选择 + 渲染变量。
        """
        return await self._run_one(
            request_type="prompt",
            user_text=user_text,
            skill_selection=skill_selection,
            system_prompt_suffix=system_prompt_suffix,
            agent_call=lambda: self.agent.prompt(user_text),
        )

    async def run_continue(
        self,
        *,
        skill_selection: SkillSelection | None = None,
        system_prompt_suffix: str | None = None,
    ) -> list[AgentMessage]:
        """包装 agent.continue_()，前后跑 hooks。

        Agent 无 messages 时沿用 Agent.continue_ 的 ValueError（before_request hook 仍会跑）。
        """
        return await self._run_one(
            request_type="continue",
            user_text=None,
            skill_selection=skill_selection,
            system_prompt_suffix=system_prompt_suffix,
            agent_call=lambda: self.agent.continue_(),
        )

    async def _run_one(
        self,
        *,
        request_type: AgentRequestType,
        user_text: str | None,
        agent_call: Callable[[], Awaitable[list[AgentMessage]]],
        skill_selection: SkillSelection | None = None,
        system_prompt_suffix: str | None = None,
    ) -> list[AgentMessage]:
        """run_prompt / run_continue 共用主流程。"""
        if self.context.phase != "idle":
            raise RuntimeError(
                f"Harness is already running (phase={self.context.phase}). "
                "Use harness.agent.steer() or harness.agent.follow_up(), "
                "or wait_for_idle()."
            )

        # 准备 context
        self.context.request_type = request_type
        self.context.user_text = user_text
        self.context.messages_before = list(self.agent.state.messages)
        self.context.messages_after = []
        self.context.started_at = _now_ms()
        self.context.ended_at = None
        self.context.last_error = None

        # Step 14：清空上一轮的 skill 状态——避免渲染失败时残留进 error snapshot
        self.context.selected_skills = []
        self.context.skill_metadata = {}
        self.context.rendered_system_prompt = None
        self.context.metadata.pop("skills", None)
        self.context.metadata.pop("system_prompt_suffix", None)
        if system_prompt_suffix and system_prompt_suffix.strip():
            self.context.metadata["system_prompt_suffix"] = {
                "included": True,
                "characters": len(system_prompt_suffix),
            }

        # Step 17：注入当前 MCP 状态——snapshot / session 自动捕获
        self._inject_mcp_metadata()

        # Step 18：注入当前 policy 状态——snapshot / session 自动捕获
        self._inject_policy_metadata()

        # Step 11：开启 snapshot——任何后续异常路径都会走 finish_snapshot_error
        builder = SnapshotBuilder()
        builder.start(
            request_type=request_type,
            user_text=user_text,
            messages_before=list(self.context.messages_before),
            metadata=dict(self.context.metadata),
        )
        self.context.snapshot = builder.snapshot
        self._snapshot_builder = builder

        # Step 14：渲染 system_prompt（含 skill 注入）；可能抛 PromptTemplateRenderError
        # 临时替换 agent.system_prompt——请求结束 finally 还原
        original_system_prompt = self.agent.system_prompt
        try:
            try:
                rendered_prompt, selected_skills = self._prepare_skill_prompt(
                    skill_selection
                )
                if system_prompt_suffix and system_prompt_suffix.strip():
                    rendered_prompt = (
                        f"{rendered_prompt}\n\n{system_prompt_suffix.strip()}"
                    )
                # skill 信息写入 context.metadata["skills"]——snapshot 会自动捕获
                self.context.selected_skills = [s.name for s in selected_skills]
                self.context.skill_metadata = {
                    s.name: dict(s.metadata) for s in selected_skills
                }
                self.context.rendered_system_prompt = rendered_prompt
                self.context.metadata["skills"] = {
                    "selected": self.context.selected_skills,
                    "metadata": self.context.skill_metadata,
                }
            except Exception as e:
                # 渲染失败（如 PromptTemplateRenderError / SkillNotFoundError）
                # —— Agent 不应执行；作为 before-agent 错误路径
                # 记录本轮 skill_selection 信息——便于诊断
                self.context.metadata["skills"] = {
                    "selected": (
                        list(skill_selection.names)
                        if skill_selection and skill_selection.names
                        else []
                    ),
                    "tags": (
                        list(skill_selection.tags)
                        if skill_selection and skill_selection.tags
                        else []
                    ),
                    "metadata": {},
                    "render_error": f"{type(e).__name__}: {e}",
                }
                await self._fail(e)
                snapshot = self._finish_snapshot(
                    status="error", messages_after=[],
                    error=f"{type(e).__name__}: {e}",
                )
                await self._post_finish_snapshot(snapshot)
                raise

            # 临时替换 system_prompt——只影响本次请求
            self.agent.system_prompt = rendered_prompt

            try:
                # Phase 1: before_request
                self.context.phase = "before_request"
                try:
                    await self._run_hooks(self.before_request_hooks)
                except Exception as e:
                    # before_request 失败：先 _fail（让 on_error hook 能写 metadata），
                    # 再 _finish_snapshot（拷贝 metadata 时含 on_error 的写入）
                    await self._fail(e)
                    snapshot = self._finish_snapshot(
                        status="error", messages_after=[],
                        error=f"{type(e).__name__}: {e}",
                    )
                    # Step 13：sync + consistency + auto-save（即使 error 也跑，便于诊断）
                    await self._post_finish_snapshot(snapshot)
                    raise

                # Phase 2: running（调用 Agent）
                self.context.phase = "running"
                try:
                    messages = await agent_call()
                except Exception as e:
                    # Agent 抛异常：同上——先 _fail，后 snapshot
                    await self._fail(e)
                    snapshot = self._finish_snapshot(
                        status="error",
                        messages_after=list(self.agent.state.messages),
                        error=f"{type(e).__name__}: {e}",
                    )
                    await self._post_finish_snapshot(snapshot)
                    raise

                # Phase 3: after_request
                self.context.phase = "after_request"
                self.context.messages_after = list(self.agent.state.messages)
                self.context.ended_at = _now_ms()
                try:
                    await self._run_hooks(self.after_request_hooks)
                except Exception as e:
                    # after_request 失败：同上——先 _fail，后 snapshot
                    await self._fail(e)
                    # Step 17：finish 前刷新 MCP metadata——本轮可能有 MCP
                    # tool call 失败导致 server connected=False
                    self._inject_mcp_metadata()
                    # Step 18：刷新 policy summary——本轮可能有 deny / approval
                    self._inject_policy_metadata()
                    snapshot = self._finish_snapshot(
                        status="error",
                        messages_after=list(self.agent.state.messages),
                        error=f"{type(e).__name__}: {e}",
                    )
                    await self._post_finish_snapshot(snapshot)
                    raise

                # 正常返回也可能携带 Provider/max_turns error assistant；按 turn
                # 终态决定 request snapshot，而不是一律标 completed。
                # Step 17：finish 前刷新 MCP metadata——本轮可能有 MCP
                # tool call 失败导致 server connected=False
                self._inject_mcp_metadata()
                # Step 18：刷新 policy summary（含本轮 audit counts）
                self._inject_policy_metadata()
                normal_status: SnapshotStatus = (
                    "aborted" if builder.seen_aborted
                    else "error" if builder.seen_error
                    else "completed"
                )
                snapshot = self._finish_snapshot(
                    status=normal_status,
                    messages_after=list(self.agent.state.messages),
                    error=builder.last_error if builder.seen_error else None,
                )
                await self._post_finish_snapshot(snapshot)

                self.context.phase = "idle"
                return messages
            finally:
                # 还原 agent.system_prompt——本请求结束就还原（无论成功失败）
                self.agent.system_prompt = original_system_prompt
        finally:
            # 无论成功/失败，请求结束都清空 _snapshot_builder——
            # 之后若用户直接 agent.prompt 触发的事件不应进入任何 snapshot
            self._snapshot_builder = None
            # error 是 on_error hooks / snapshot 构建期间的瞬态；请求 coroutine
            # 已经结束后 Harness 必须可再次使用，同时保留 last_error 供诊断。
            self.context.phase = "idle"

    # ----------------------------------------------------------------------
    # abort / wait_for_idle / close
    # ----------------------------------------------------------------------

    async def abort(self, reason: str | None = None) -> None:
        """转发给 Agent.abort；不维护自己的 abort 信号。"""
        try:
            await self.agent.abort(reason)
        except Exception as e:
            await self._fail(e)
            raise

    async def wait_for_idle(self) -> None:
        """转发给 Agent.wait_for_idle——Harness 不维护自己的 queue。"""
        await self.agent.wait_for_idle()

    async def close(self) -> None:
        """关闭 Harness 持有的所有资源。幂等。

        Step 17：先 detach MCP servers（关 transport），再 unsubscribe Agent 事件。
        detach 失败不阻塞 unsubscribe——保证资源最终释放。

        Step 21 修复：detach MCP 之后还要关闭 ModelClient（释放 httpx
        连接池——GLMClient / AnthropicCompatAdapter 内部持有 AsyncAnthropic）。
        """
        # Step 17：先关 MCP（async）
        try:
            await self.detach_mcp_servers()
        except Exception:
            # detach 内部已经吞了所有异常；这里再兜底防御
            pass
        # Step 21：关 ModelClient（释放 provider httpx 连接）
        try:
            close_fn = getattr(self.agent.client, "close", None)
            if close_fn is not None:
                await close_fn()
        except Exception:
            pass
        # 再 unsubscribe（sync）
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # ----------------------------------------------------------------------
    # Step 17：MCP lifecycle
    # ----------------------------------------------------------------------

    @property
    def mcp_registry(self) -> MCPRegistry | None:
        """当前绑定的 MCPRegistry（None 表示未 attach）。"""
        return self._mcp_registry

    async def attach_mcp_servers(
        self,
        configs: list[MCPServerConfig],
        *,
        auto_register_tools: bool = True,
        registry: MCPRegistry | None = None,
    ) -> None:
        """连接一个或多个 MCP server，可选把 MCP tools 自动注册进 Agent ToolRegistry。

        流程：
            1. 若已有 _mcp_registry：先 detach_mcp_servers（清理旧工具 + close 旧 transport）
            2. 创建 MCPRegistry(configs) 或使用传入的 registry
            3. registry.connect_all()——单 server 失败不影响其它
            4. 保存 _mcp_registry
            5. auto_register_tools=True：to_agent_tools → register 到 Agent.tools
               记录 _mcp_tool_names（用于 refresh / detach 清理）
            6. MCP 状态写入 context.metadata["mcp"]

        参数：
            configs: MCP server 配置列表（即使为空也会创建 registry）
            auto_register_tools: 默认 True；False 时只连接不注册（调用方自己处理）
            registry: 可选——传入已构造的 MCPRegistry（如带 client_factory 的测试场景）；
                传入时忽略 configs

        单 server 失败语义：状态进入 MCPServerState.last_error，整体不抛。
        """
        # 1. 清理旧 MCP
        if self._mcp_registry is not None:
            await self.detach_mcp_servers()

        # 2/3. 创建 + 连接
        if registry is not None:
            self._mcp_registry = registry
        else:
            self._mcp_registry = MCPRegistry(configs)
        # connect_all 内部对单 server 失败已兜底（写入 state.last_error）
        await self._mcp_registry.connect_all()

        # 4. 总是 refresh（让 registry._tools 填充，list_mcp_tools 可见）
        #    auto_register_tools 只控制是否注入到 Agent ToolRegistry
        await self._mcp_registry.refresh_tools()

        # 5. 可选：自动注册到 Agent ToolRegistry
        #    _register_mcp_tools_into_agent 内部会调 _inject_mcp_metadata
        #    （含可能的 registration_errors）；不要在末尾再调 inject 覆盖它
        if auto_register_tools:
            await self._register_mcp_tools_into_agent()
        else:
            self._inject_mcp_metadata()

    async def detach_mcp_servers(self) -> None:
        """关闭所有 MCP server 并清理已注册的 MCP tools。幂等。

        流程：
            1. 从 Agent ToolRegistry 中 unregister 所有 _mcp_tool_names
            2. 清空 _mcp_tool_names
            3. 若 _mcp_registry 不为 None：close_all()
            4. _mcp_registry = None
            5. 更新 metadata

        单个 tool unregister 失败 / close_all 失败都不阻塞——继续清理剩余资源。
        """
        # 1. 清理 ToolRegistry
        for name in list(self._mcp_tool_names):
            try:
                self.agent.tools.unregister(name)
            except Exception:
                # unregister 文档承诺不存在静默；这里再兜底防御
                pass
        self._mcp_tool_names.clear()

        # 2. 关闭 MCPRegistry
        if self._mcp_registry is not None:
            try:
                await self._mcp_registry.close_all()
            except Exception:
                pass
            self._mcp_registry = None

        # 3. 更新 metadata
        self._inject_mcp_metadata()

    async def refresh_mcp_tools(self) -> list[MCPAgentTool]:
        """重新拉 tools/list 并替换 Agent ToolRegistry 中的 MCP tools。

        流程：
            1. _mcp_registry is None → 返回 []
            2. await registry.refresh_tools()
            3. unregister 旧 _mcp_tool_names
            4. 注册新 MCP tools（单个失败不影响其它，记录到 metadata）
            5. 更新 _mcp_tool_names + metadata
            6. 返回新工具列表

        单 server refresh 失败：state.last_error 记录，不影响其它 server；
        失败 server 的旧工具会被 unregister（因为 _mcp_tool_names 还记着）。
        """
        if self._mcp_registry is None:
            return []
        return await self._refresh_mcp_tools_internal()

    def list_mcp_servers(self) -> list[MCPServerState]:
        """当前 MCP server 状态列表（按配置顺序）。未 attach 返回 []。"""
        if self._mcp_registry is None:
            return []
        return self._mcp_registry.list_servers()

    def list_mcp_tools(self) -> list[MCPAgentTool]:
        """当前 MCPRegistry 缓存的 MCP tools（含 namespaced name + server 来源）。

        注意：这里返回的是 registry 已知的所有工具，**不一定都注册到了 Agent
        ToolRegistry**——`attach_mcp_servers(auto_register_tools=False)` 时
        registry 有工具但 ToolRegistry 中没有。要查"实际注册到 ToolRegistry
        的工具名"，看 `_mcp_tool_names`（或新增的 `list_registered_mcp_tool_names`）。

        未 attach 返回 []。
        """
        if self._mcp_registry is None:
            return []
        return self._mcp_registry.list_agent_tools()

    def list_registered_mcp_tool_names(self) -> list[str]:
        """已注册到 Agent ToolRegistry 的 MCP tool names（sorted）。

        与 list_mcp_tools() 互补：后者是 registry 视角，本方法是 ToolRegistry
        视角。refresh / detach 时 Harness 只清理这个集合里的工具。
        """
        return sorted(self._mcp_tool_names)

    # ---- Step 17 内部 ----

    async def _refresh_mcp_tools_internal(self) -> list[MCPAgentTool]:
        """refresh_mcp_tools 实现：refresh registry + 重注册工具。

        - 调 registry.refresh_tools() 拉新工具
        - unregister 旧 _mcp_tool_names
        - 注册新工具（单个失败记录到 metadata["mcp"]["registration_errors"]）
        - 更新 _mcp_tool_names
        - 更新 metadata
        """
        assert self._mcp_registry is not None

        # 1. 拉新工具
        new_tools = await self._mcp_registry.refresh_tools()

        # 2/3. 重注册（unregister 旧 + 注册新）
        await self._register_mcp_tools_into_agent()

        return new_tools

    async def _register_mcp_tools_into_agent(self) -> None:
        """unregister 旧 _mcp_tool_names + 注册 registry 中所有当前工具。

        单个注册失败不影响其它；失败信息写入 metadata["mcp"]["registration_errors"]。
        """
        assert self._mcp_registry is not None

        # 1. unregister 旧工具
        for name in list(self._mcp_tool_names):
            try:
                self.agent.tools.unregister(name)
            except Exception:
                pass
        self._mcp_tool_names.clear()

        # 2. 注册 registry 中所有当前工具
        registration_errors: list[dict[str, Any]] = []
        for tool in self._mcp_registry.list_agent_tools():
            try:
                self.agent.tools.register(tool)
                self._mcp_tool_names.add(tool.name)
            except Exception as e:
                registration_errors.append({
                    "tool": tool.name,
                    "error": f"{type(e).__name__}: {e}",
                })

        # 3. 更新 metadata
        self._inject_mcp_metadata(extra={
            "registration_errors": registration_errors,
        } if registration_errors else None)

    def _build_mcp_metadata(self) -> dict[str, Any]:
        """构造 MCP server/tool 的 JSON-safe 状态快照。

        不保存 live object（MCPClient / MCPAgentTool），避免序列化进 session 时崩溃。
        工具 schema 不写入——避免每次 snapshot metadata 变大；schema 通过
        ToolRegistry.definitions() 已经传给 LLM。
        """
        if self._mcp_registry is None:
            return {"attached": False}

        servers: list[dict[str, Any]] = []
        for state in self._mcp_registry.list_servers():
            servers.append({
                "name": state.name,
                "connected": state.connected,
                "tool_count": state.tool_count,
                "last_error": state.last_error,
            })

        tools: list[dict[str, Any]] = []
        for tool in self._mcp_registry.list_agent_tools():
            tools.append({
                "name": tool.name,
                "server": tool.server_name,
                "mcp_tool": tool.mcp_tool_name,
                "description": tool.description,
            })

        return {
            "attached": True,
            "servers": servers,
            "tools": tools,
            "registered_count": len(self._mcp_tool_names),
        }

    def _inject_mcp_metadata(
        self, *, extra: dict[str, Any] | None = None,
    ) -> None:
        """把 MCP 状态写入 context.metadata["mcp"]。

        - 生成失败不能让主请求失败：捕获异常，写 metadata_error
        - extra 用于附加本轮诊断信息（如 registration_errors）
        """
        try:
            metadata = self._build_mcp_metadata()
        except Exception as e:
            metadata = {"metadata_error": f"{type(e).__name__}: {e}"}
        if extra:
            metadata.update(extra)
        self.context.metadata["mcp"] = metadata

    # ----------------------------------------------------------------------
    # Step 18：Permission / Approval Policy
    # ----------------------------------------------------------------------

    def set_permission_policy(
        self,
        policy: ToolPermissionPolicy | None,
    ) -> None:
        """替换当前权限策略；同步到 agent。

        传 None 关闭权限检查（向后兼容旧行为）。
        """
        self.permission_policy = policy
        self.agent.permission_policy = policy

    def set_tool_approval_handler(
        self,
        handler: ToolApprovalHandler | None,
    ) -> None:
        """替换一次性工具审批处理器；``None`` 恢复安全错误行为。"""
        self.tool_approval_handler = handler
        self.agent.tool_approval_handler = handler

    def list_permission_audit_records(self) -> list[ToolPermissionAuditRecord]:
        """读取权限审计记录（按写入顺序）。"""
        return self.permission_audit_log.list_records()

    def clear_permission_audit_records(self) -> None:
        """清空权限审计记录。"""
        self.permission_audit_log.clear()

    def _inject_policy_metadata(self) -> None:
        """把 policy summary 写入 context.metadata["policy"]。

        写入 summary（policy_name / audit_count / denied_count /
        approval_required_count），**不写完整 records**——避免每个
        snapshot metadata 都带全量 audit。
        """
        records = self.permission_audit_log.list_records()
        counts = self.permission_audit_log.counts()
        self.context.metadata["policy"] = {
            "policy_name": (
                self.permission_policy.name
                if self.permission_policy is not None
                else None
            ),
            "audit_count": len(records),
            "allowed_count": counts.get("allow", 0),
            "denied_count": counts.get("deny", 0),
            "approval_required_count": counts.get("require_approval", 0),
        }

    # ----------------------------------------------------------------------
    # Step 11：Snapshot 接口
    # ----------------------------------------------------------------------

    def get_snapshot(self, index: int = -1) -> RequestSnapshot | None:
        """取第 index 个 snapshot（默认 -1 = 最近一次）。

        snapshots 为空时返回 None。索引越界抛 IndexError。
        """
        if not self.snapshots:
            return None
        return self.snapshots[index]

    def clear_snapshots(self) -> None:
        """清空 Harness 内存中的所有 snapshot。

        注意：不影响已 attach 的 session——session 里的 snapshots 是独立副本。
        """
        self.snapshots.clear()
        self.last_snapshot = None
        if self.context.snapshot is not None:
            self.context.snapshot = None

    # ----------------------------------------------------------------------
    # Step 12 + Step 13：Session 接口
    # ----------------------------------------------------------------------

    def configure_session_sync(
        self,
        *,
        store: SessionStore | None = None,
        auto_save_policy: SessionAutoSavePolicy | None = None,
        sync_harness_metadata: bool | None = None,
        sync_agent_messages: bool | None = None,
        strict_consistency: bool | None = None,
    ) -> None:
        """统一配置 Harness ↔ Session 同步行为。

        只更新传入的字段；None 字段保持原值。
        """
        if store is not None:
            self.session_store = store
        if auto_save_policy is not None:
            self.session_sync_config.auto_save_policy = auto_save_policy
        if sync_harness_metadata is not None:
            self.session_sync_config.sync_harness_metadata = sync_harness_metadata
        if sync_agent_messages is not None:
            self.session_sync_config.sync_agent_messages = sync_agent_messages
        if strict_consistency is not None:
            self.session_sync_config.strict_consistency = strict_consistency

    def attach_session(
        self,
        session: SessionMemory,
        *,
        restore_messages: bool = True,
        store: SessionStore | None = None,
        auto_save_policy: SessionAutoSavePolicy | None = None,
    ) -> None:
        """附加 SessionMemory；可选恢复 Agent messages / turn_count。

        - 默认 `restore_messages=True`：把 session.get_messages() 写入
          agent.state.messages，agent.state.turn_count 写为 session.state.turn_count
        - restore_messages=False：只附加，不动 Agent 状态——调方自己同步
        - Agent 处于 running / aborting 时抛 RuntimeError——不能在运行中换 session
        - Step 13：可选传 store / auto_save_policy——等价于先调 configure_session_sync
        """
        if self.agent.state.status in ("running", "aborting"):
            raise RuntimeError(
                f"Cannot attach session while agent is {self.agent.state.status!r}"
            )
        self.session = session
        if store is not None:
            self.session_store = store
        if auto_save_policy is not None:
            self.session_sync_config.auto_save_policy = auto_save_policy
        if restore_messages:
            self.agent.state.messages = session.get_messages()
            self.agent.state.turn_count = session.state.turn_count

    def detach_session(self) -> SessionMemory | None:
        """解附加 session；返回旧 session（未附加时返回 None）。

        不清空 Agent 状态——已恢复到 Agent 的 messages 保留。
        也不清空 session_store / session_sync_config——便于下次 attach 复用。
        """
        old = self.session
        self.session = None
        return old

    async def save_session(self, store: SessionStore | None = None) -> None:
        """便利方法：把当前 session 保存到 store。

        - store=None 时用 self.session_store（若 configure 过）
        - 未 attach session 时抛 RuntimeError
        - store 与 self.session_store 都为 None 时抛 RuntimeError
        """
        if self.session is None:
            raise RuntimeError("AgentHarness.save_session: 未 attach session")
        target = store if store is not None else self.session_store
        if target is None:
            raise RuntimeError(
                "AgentHarness.save_session: 未提供 store（构造/attach/configure 时未设）"
            )
        await target.save(self.session)

    async def load_session(
        self,
        store: SessionStore,
        session_id: str,
        *,
        restore_messages: bool = True,
    ) -> SessionMemory:
        """便利方法：从 store 加载 session 并 attach。

        返回加载的 session。如果当前已 attach 旧 session，会被覆盖（不抛错）。
        也会把传入的 store 设为 self.session_store（便于后续 auto-save）。
        """
        session = await store.load(session_id)
        self.attach_session(
            session,
            restore_messages=restore_messages,
            store=store,
        )
        return session

    # ------------------------------------------------------------------
    # Step 13：sync / consistency / export / import
    # ------------------------------------------------------------------

    def sync_session_metadata(self) -> None:
        """把 harness 状态快照放入 session.metadata["harness"]。

        不覆盖 session 顶层 metadata——只写 "harness" 这个 key。
        顶层 project / tags / notes 等保留。
        """
        if self.session is None:
            return
        self.session.state.metadata["harness"] = {
            "last_request_type": self.context.request_type,
            "last_user_text": self.context.user_text,
            "last_snapshot_id": self.last_snapshot.id if self.last_snapshot else None,
            "context_metadata": dict(self.context.metadata),
            "synced_at": _now_ms(),
            "updated_by": "AgentHarness",
        }
        self.session.state.updated_at = _now_ms()

    def sync_agent_messages_to_session(self) -> None:
        """兜底：把 agent.state.messages 同步进 session.messages。

        通常 append_snapshot() 已经用 snapshot.messages_after 覆盖；本方法用于：
        - messages_after 为空的 error 路径（如 before_request 抛）
        - 调方手动改了 agent.state.messages 想立即同步
        """
        if self.session is None:
            return
        self.session.set_messages(list(self.agent.state.messages))

    def check_session_consistency(self) -> SessionConsistencyReport:
        """检查 Harness / Agent / Session 三方状态一致性。

        6 项检查（详见 SessionConsistencyIssue 的 ISSUE_* 常量）：
          - no_session                        info（不算错）
          - turn_count_mismatch               error
          - messages_mismatch                 error
          - last_snapshot_mismatch            warning
          - agent_session_messages_mismatch   error
          - harness_session_snapshot_mismatch warning

        返回 SessionConsistencyReport；存入 self.last_consistency_report。
        若 config.strict_consistency=True 且 report.has_errors()，抛 RuntimeError。
        """
        issues: list[SessionConsistencyIssue] = []

        if self.session is None:
            issues.append(SessionConsistencyIssue(
                code=ISSUE_NO_SESSION,
                message="未 attach session",
                severity="info",
            ))
            report = SessionConsistencyReport(ok=True, issues=issues)
            self.last_consistency_report = report
            return report

        session = self.session

        # 1. turn_count 与 len(snapshots)
        if session.state.turn_count != len(session.state.snapshots):
            issues.append(SessionConsistencyIssue(
                code=ISSUE_TURN_COUNT_MISMATCH,
                message=f"turn_count={session.state.turn_count} 与 "
                        f"len(snapshots)={len(session.state.snapshots)} 不一致",
                severity="error",
                details={
                    "turn_count": session.state.turn_count,
                    "snapshots_len": len(session.state.snapshots),
                },
            ))

        # 2. session.messages 与最后一个 snapshot.messages_after（若非空）
        if session.state.snapshots:
            last_snap_dict = session.state.snapshots[-1]
            last_messages_after = last_snap_dict.get("messages_after") or []
            if last_messages_after and session.state.messages != last_messages_after:
                issues.append(SessionConsistencyIssue(
                    code=ISSUE_MESSAGES_MISMATCH,
                    message="session.messages 与最后 snapshot.messages_after 不一致",
                    severity="error",
                    details={
                        "session_messages_len": len(session.state.messages),
                        "last_messages_after_len": len(last_messages_after),
                    },
                ))

            # 3. harness.last_snapshot 与 session 末尾 snapshot
            if self.last_snapshot is not None:
                if self.last_snapshot.to_dict() != last_snap_dict:
                    issues.append(SessionConsistencyIssue(
                        code=ISSUE_LAST_SNAPSHOT_MISMATCH,
                        message="harness.last_snapshot 与 session 末尾 snapshot 不一致",
                        severity="warning",
                    ))

        # 4. agent.state.messages 与 session.messages
        agent_messages_serialized = serialize_messages(
            list(self.agent.state.messages)
        )
        if agent_messages_serialized != session.state.messages:
            issues.append(SessionConsistencyIssue(
                code=ISSUE_AGENT_SESSION_MESSAGES_MISMATCH,
                message="agent.state.messages 与 session.messages 不一致",
                severity="error",
                details={
                    "agent_messages_len": len(agent_messages_serialized),
                    "session_messages_len": len(session.state.messages),
                },
            ))

        # 5. harness.snapshots 与 session.snapshots 数量
        # harness.snapshots 可能比 session 少（session 是从 store 加载的旧 + 新），
        # 但 harness 不应该比 session 多（每条 harness snapshot 应该都进 session）
        if len(self.snapshots) > len(session.state.snapshots):
            issues.append(SessionConsistencyIssue(
                code=ISSUE_HARNESS_SESSION_SNAPSHOT_MISMATCH,
                message=f"harness.snapshots({len(self.snapshots)}) > "
                        f"session.snapshots({len(session.state.snapshots)})",
                severity="warning",
            ))

        ok = not any(i.severity == "error" for i in issues)
        report = SessionConsistencyReport(ok=ok, issues=issues)
        self.last_consistency_report = report

        if self.session_sync_config.strict_consistency and report.has_errors():
            raise RuntimeError(
                f"Session consistency check failed (strict mode): "
                f"{[i.code for i in issues if i.severity == 'error']}"
            )

        return report

    def export_session(self, indent: int | None = 2) -> str:
        """导出当前 session 为 JSON 文本。

        未 attach session 时抛 RuntimeError。
        """
        if self.session is None:
            raise RuntimeError("AgentHarness.export_session: 未 attach session")
        return self.session.to_json(indent=indent)

    def import_session(
        self,
        text: str,
        *,
        restore_messages: bool = True,
    ) -> SessionMemory:
        """从 JSON 文本反序列化为 SessionMemory 并 attach。

        不依赖 SessionStore——纯文本接口。
        """
        session = SessionMemory.from_json(text)
        self.attach_session(session, restore_messages=restore_messages)
        return session

    # ------------------------------------------------------------------
    # Step 14：Skills / Prompt Templates
    # ------------------------------------------------------------------

    def attach_skills(
        self,
        skills: SkillRegistry | list[Skill],
        *,
        injection_config: SkillInjectionConfig | None = None,
    ) -> None:
        """附加 SkillRegistry（或从 list[Skill] 构造一个）。

        - 传 SkillRegistry → 直接用
        - 传 list[Skill]  → SkillRegistry(skills)
        - 传 injection_config → 替换 self.skill_injection_config
        """
        if isinstance(skills, SkillRegistry):
            self.skill_registry = skills
        else:
            self.skill_registry = SkillRegistry(list(skills))
        if injection_config is not None:
            self.skill_injection_config = injection_config

    def detach_skills(self) -> SkillRegistry | None:
        """解附加 skill registry；返回旧实例。"""
        old = self.skill_registry
        self.skill_registry = None
        return old

    def add_on_event_hook(self, hook: OnEventHook) -> None:
        """追加一个 on_event hook（Step 20 引入）。

        与直接 `harness.on_event_hooks.append(hook)` 等价——提供这个方法是为了：

        - 在 IDE / type checker 中暴露出 OnEventHook 类型签名
        - 让外部模块（如 web.app）订阅事件时不依赖内部 attribute 命名
        - 后续若 hook 注册加观察 / 校验逻辑，集中在一个入口

        hook 签名：`(event: AgentEvent, ctx: HarnessContext) -> None | Awaitable[None]`
        hook 抛异常不影响 Agent 主流程（_handle_agent_event 已有 try/except 兜底）。
        """
        self.on_event_hooks.append(hook)

    def remove_on_event_hook(self, hook: OnEventHook) -> bool:
        """移除一个此前通过 add_on_event_hook 注册的 hook。

        返回 True 表示找到并移除；False 表示未注册过（不抛错，幂等）。

        用途：web.app 的 create_app 注册 hook 后，需要在 app 关闭 / 重建时
        清理，避免多次 create_app 同一个 harness 导致 hook 累积、事件被
        重复广播。
        """
        try:
            self.on_event_hooks.remove(hook)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # Step 19：Skill File Loader + MCP Prompts
    # ------------------------------------------------------------------

    def attach_skill_files(
        self,
        paths: list[str | Path],
        *,
        loader_config: SkillLoadConfig | None = None,
        replace_existing_registry: bool = False,
    ) -> list[Skill]:
        """从一组 SKILL.md 文件路径加载 Skill 并注册。

        - 用 SkillFileLoader.load_many 加载
        - replace_existing_registry=True 或当前没有 attach 过 registry：
            用 attach_skills(loaded) 替换为全新 SkillRegistry
        - replace_existing_registry=False 且已有 registry：逐个 register 进现有
            registry；单个重名抛 SkillRegistrationError 立即停止（fail-fast）
        - metadata["skill_loader"]["file_skills"] 记录 count + names

        参数：
            paths                      SKILL.md 文件路径列表
            loader_config              SkillFileLoader 配置（None 用默认）
            replace_existing_registry  True = 替换 SkillRegistry；False = 追加

        返回加载得到的 Skill 列表（无论替换 / 追加）。
        """
        loader = SkillFileLoader(loader_config)
        loaded = loader.load_many(paths)
        self._apply_loaded_skills(
            loaded,
            replace_existing_registry=replace_existing_registry,
        )
        self._inject_skill_loader_metadata(
            file_skills=loaded,
            mcp_prompt_skills=None,
        )
        return loaded

    def attach_skill_dir(
        self,
        root: str,
        *,
        recursive: bool = True,
        loader_config: SkillLoadConfig | None = None,
        replace_existing_registry: bool = False,
    ) -> list[Skill]:
        """从目录扫描 SKILL.md 并注册。

        - 用 SkillFileLoader.load_dir(root, recursive=recursive) 加载
        - replace / 追加语义同 attach_skill_files
        - metadata["skill_loader"]["file_skills"] 记录 count + names

        返回加载得到的 Skill 列表。
        """
        loader = SkillFileLoader(loader_config)
        loaded = loader.load_dir(root, recursive=recursive)
        self._apply_loaded_skills(
            loaded,
            replace_existing_registry=replace_existing_registry,
        )
        self._inject_skill_loader_metadata(
            file_skills=loaded,
            mcp_prompt_skills=None,
        )
        return loaded

    async def refresh_mcp_prompts(self) -> list[tuple[str, MCPPromptInfo]]:
        """刷新当前 MCPRegistry 的 prompts 缓存。

        - 未 attach MCPRegistry → 返回 []
        - 调 self._mcp_registry.refresh_prompts()
        - 不影响 MCP tools / permission policy
        """
        if self._mcp_registry is None:
            return []
        return await self._mcp_registry.refresh_prompts()

    async def attach_mcp_prompts_as_skills(
        self,
        *,
        arguments_by_prompt: dict[str, dict[str, Any]] | None = None,
        replace_existing_registry: bool = False,
    ) -> list[Skill]:
        """把当前 MCPRegistry 的所有 prompts 转 Skill 并注册。

        - 未 attach MCPRegistry / 无 prompts → 返回 []
        - 先自动调 refresh_mcp_prompts()（保证 list_prompts 缓存新鲜）
        - 再调 MCPPromptSkillAdapter(registry).load_all_prompts_as_skills()
        - replace / 追加语义同 attach_skill_files
        - metadata["skill_loader"]["mcp_prompt_skills"] 记录 count + names

        返回加载得到的 Skill 列表。
        """
        if self._mcp_registry is None:
            return []

        # 保证 prompts 缓存新鲜——若调用方已显式 refresh 过，再 refresh 一次
        # 只是多一次 round-trip，无副作用
        await self._mcp_registry.refresh_prompts()

        adapter = MCPPromptSkillAdapter(self._mcp_registry)
        loaded = await adapter.load_all_prompts_as_skills(
            arguments_by_prompt=arguments_by_prompt,
        )
        # P1 修订：保留完整错误列表（adapter 是局部变量，否则调用方拿不到）
        self.last_mcp_prompt_skill_errors = list(adapter.last_errors)
        self._apply_loaded_skills(
            loaded,
            replace_existing_registry=replace_existing_registry,
        )
        self._inject_skill_loader_metadata(
            file_skills=None,
            mcp_prompt_skills=loaded,
            mcp_prompt_errors=adapter.last_errors,
        )
        return loaded

    # ---- Step 19 内部 ----

    def _apply_loaded_skills(
        self,
        skills: list[Skill],
        *,
        replace_existing_registry: bool,
    ) -> None:
        """把加载得到的 Skill 列表注册到 self.skill_registry。

        - skills 为空：仍走 attach_skills([])（保证 replace=True 时
          会清空旧 registry）
        - replace=True 或 self.skill_registry is None：替换为新 SkillRegistry
        - replace=False：逐个 register；重名立即抛 SkillRegistrationError
        """
        if replace_existing_registry or self.skill_registry is None:
            self.attach_skills(list(skills))
            return
        # 追加到现有 registry
        for skill in skills:
            self.skill_registry.register(skill)

    def _inject_skill_loader_metadata(
        self,
        *,
        file_skills: list[Skill] | None,
        mcp_prompt_skills: list[Skill] | None,
        mcp_prompt_errors: list[dict[str, str]] | None = None,
    ) -> None:
        """把 file / mcp_prompt skill 加载信息写入 context.metadata["skill_loader"]。

        只记 count + names——不写完整 SKILL.md 内容 / MCP prompt raw，
        避免 metadata 膨胀。

        mcp_prompt_errors（P1 修订）：list[{server, prompt, error}]——只记
        count + prompt names + 第一条 error message 摘要，不写完整 stack。

        每次调用 merge 进现有 dict（保留之前的 file_skills / mcp_prompt_skills
        字段，除非显式覆盖）。
        """
        bucket: dict[str, Any] = dict(
            self.context.metadata.get("skill_loader") or {}
        )

        if file_skills is not None:
            bucket["file_skills"] = {
                "count": len(file_skills),
                "names": [s.name for s in file_skills],
            }

        if mcp_prompt_skills is not None:
            bucket["mcp_prompt_skills"] = {
                "count": len(mcp_prompt_skills),
                "names": [s.name for s in mcp_prompt_skills],
            }

        if mcp_prompt_errors is not None:
            bucket["mcp_prompt_errors"] = {
                "count": len(mcp_prompt_errors),
                "prompts": [
                    f"{e.get('server', '?')}/{e.get('prompt', '?')}"
                    for e in mcp_prompt_errors
                ],
                # 写第一条 error 摘要便于 UI 展示；完整列表通过
                # adapter.last_errors / harness 自行访问 registry 拿
                "first_error": (
                    mcp_prompt_errors[0].get("error")
                    if mcp_prompt_errors else None
                ),
            }

        self.context.metadata["skill_loader"] = bucket

    def enable_skill(self, name: str) -> None:
        """启用 skill；未 attach registry 抛 RuntimeError；name 不存在抛 SkillNotFoundError。"""
        if self.skill_registry is None:
            raise RuntimeError("AgentHarness.enable_skill: 未 attach skill_registry")
        self.skill_registry.enable(name)

    def disable_skill(self, name: str) -> None:
        """禁用 skill；未 attach registry 抛 RuntimeError；name 不存在抛 SkillNotFoundError。"""
        if self.skill_registry is None:
            raise RuntimeError("AgentHarness.disable_skill: 未 attach skill_registry")
        self.skill_registry.disable(name)

    def render_system_prompt(
        self,
        *,
        skill_selection: SkillSelection | None = None,
    ) -> str:
        """渲染当前 system_prompt：base + skill block。

        - 未 attach skill_registry → 返回 base（agent.system_prompt）
        - skill_injection_config.enabled=False → 返回 base
        - skill_selection=None → 选 enabled skills，values={}
        - skill_selection.names/tags → 按 select 规则过滤
        - skill_selection.values → 传给 PromptTemplate.render

        不会修改 agent.system_prompt（只读）。
        可能抛 PromptTemplateRenderError / SkillNotFoundError（select 时）。
        """
        base = self.agent.system_prompt
        if self.skill_registry is None or not self.skill_injection_config.enabled:
            return base

        selection = skill_selection or SkillSelection()
        skills = self.skill_registry.select(
            names=selection.names,
            tags=selection.tags,
            enabled_only=True,
        )
        block = render_skill_block(
            skills,
            values=selection.values,
            config=self.skill_injection_config,
        )
        if not block:
            return base
        return f"{base}\n\n{block}"

    def _prepare_skill_prompt(
        self,
        skill_selection: SkillSelection | None,
    ) -> tuple[str, list[Skill]]:
        """Step 14 内部辅助：渲染 system_prompt 并返回选中的 skills。

        抛 PromptTemplateRenderError / SkillNotFoundError 等。

        P0-5：当 `agent.system_prompt` 为空（或仅 whitespace）时，调用
        `system_prompt.build_default_system_prompt(...)` 构造默认对话向 prompt，
        并在 `context.metadata["system_prompt_source"]` 写 "default" / "explicit"。
        本方法不会破坏显式 system_prompt 行为——显式 prompt 不被覆盖。
        """
        base = self.agent.system_prompt

        # P0-5：base 空 → 用 default system prompt
        if not base or not base.strip():
            from .system_prompt import build_default_system_prompt

            # 收集 enabled skills 给 default prompt
            default_skills: list[Skill] = []
            if (
                self.skill_registry is not None
                and self.skill_injection_config.enabled
            ):
                selection = skill_selection or SkillSelection()
                default_skills = self.skill_registry.select(
                    names=selection.names,
                    tags=selection.tags,
                    enabled_only=True,
                )

            # 收集 enabled MCP tools 给 default prompt
            default_mcp_tools: list[Any] = []
            if self._mcp_registry is not None:
                for tool in self._mcp_registry.list_agent_tools():
                    if tool.name in self._mcp_tool_names:
                        default_mcp_tools.append(tool)

            base = build_default_system_prompt(
                skills=default_skills,
                mcp_tools=default_mcp_tools,
                file_tools_enabled=True,
                knowledge_enabled=self.agent.tools.has("search_knowledge"),
            )
            self.context.metadata["system_prompt_source"] = "default"
            self.context.metadata["enabled_skill_names"] = [s.name for s in default_skills]
            self.context.metadata["enabled_mcp_tool_names"] = [
                t.name for t in default_mcp_tools
            ]
            # default prompt 已经把 skills 和 mcp tools 文本拼进去了；
            # 跳过下方 skill block 二次拼接（避免重复）
            return base, default_skills

        # explicit 路径：原行为
        self.context.metadata["system_prompt_source"] = "explicit"
        if self.skill_registry is not None and self.skill_injection_config.enabled:
            selection = skill_selection or SkillSelection()
            selected = typing.cast(
                list[Skill],
                self.skill_registry.select(
                    names=selection.names,
                    tags=selection.tags,
                    enabled_only=True,
                ),
            )
            self.context.metadata["enabled_skill_names"] = [s.name for s in selected]
        else:
            self.context.metadata["enabled_skill_names"] = []
        # MCP tool names metadata
        if self._mcp_registry is not None:
            self.context.metadata["enabled_mcp_tool_names"] = sorted(self._mcp_tool_names)
        else:
            self.context.metadata["enabled_mcp_tool_names"] = []

        if self.skill_registry is None or not self.skill_injection_config.enabled:
            return base, []

        selection = skill_selection or SkillSelection()
        skills = self.skill_registry.select(
            names=selection.names,
            tags=selection.tags,
            enabled_only=True,
        )
        block = render_skill_block(
            skills,
            values=selection.values,
            config=self.skill_injection_config,
        )
        if not block:
            return base, []
        return f"{base}\n\n{block}", skills

    # ------------------------------------------------------------------
    # Step 15：Compaction / Branch Summary
    # ------------------------------------------------------------------

    async def compact_context(
        self,
        *,
        config: CompactionConfig | None = None,
        summary_generator: SummaryGenerator | None = None,
        context_estimate: ContextEstimate | None = None,
        retry_policy: CompactionRetryPolicy | None = None,
        retry_predicate: CompactionRetryPredicate | None = None,
        retry_callback: CompactionRetryCallback | None = None,
    ) -> CompactionResult:
        """基于 agent.state.messages 压缩当前上下文。

        - Agent 处于 running / aborting 时抛 RuntimeError
        - 调 compact_messages(agent.state.messages, snapshots=self.snapshots, ...)
        - 若 result.applied：agent.state.messages = deserialize(result.new_messages)
        - 设置 self.last_compaction_result = result
        - 若 attach session：session.append_compaction(result) + sync_agent_messages +
          check_session_consistency + 按 auto-save policy 落盘
        - 返回 result
        """
        if self.agent.state.status in ("running", "aborting"):
            raise RuntimeError(
                f"Cannot compact context while agent is {self.agent.state.status!r}"
            )

        result = await compact_messages(
            list(self.agent.state.messages),
            snapshots=list(self.snapshots),
            config=config,
            summary_generator=summary_generator,
            context_estimate=context_estimate,
            retry_policy=retry_policy,
            retry_predicate=retry_predicate,
            retry_callback=retry_callback,
        )
        self.last_compaction_result = result

        if result.applied:
            # 用 session.deserialize_messages 把 dict 还原为 AgentMessage 对象
            from .session import deserialize_messages  # 局部 import 避免循环
            self.agent.state.messages = deserialize_messages(result.new_messages)

        if self.session is not None:
            self.session.append_compaction(result)
            # applied 时 session.messages 已被 append_compaction 覆盖
            self.sync_agent_messages_to_session()
            self.check_session_consistency()
            await self._maybe_save_after_compaction()

        return result

    async def compact_session(
        self,
        *,
        config: CompactionConfig | None = None,
        summary_generator: SummaryGenerator | None = None,
        context_estimate: ContextEstimate | None = None,
        retry_policy: CompactionRetryPolicy | None = None,
        retry_predicate: CompactionRetryPredicate | None = None,
        retry_callback: CompactionRetryCallback | None = None,
    ) -> CompactionResult:
        """基于 session.messages 压缩——以 session 为权威源。

        - 未 attach session 抛 RuntimeError
        - 调 compact_messages(session.get_messages(), snapshots=session.get_snapshots(), ...)
        - session.append_compaction(result)——applied 时 session.messages 被覆盖
        - 若 result.applied：agent.state.messages = session.get_messages()（同步）
        - 设置 last_compaction_result；check_session_consistency；按 policy 落盘
        """
        if self.session is None:
            raise RuntimeError("AgentHarness.compact_session: 未 attach session")

        result = await compact_messages(
            self.session.get_messages(),
            snapshots=self.session.get_snapshots(),
            config=config,
            summary_generator=summary_generator,
            context_estimate=context_estimate,
            retry_policy=retry_policy,
            retry_predicate=retry_predicate,
            retry_callback=retry_callback,
        )
        self.last_compaction_result = result

        # session.append_compaction 内部会用 result.new_messages 覆盖 session.messages
        self.session.append_compaction(result)
        if result.applied:
            # 同步到 agent
            self.agent.state.messages = self.session.get_messages()

        self.check_session_consistency()
        await self._maybe_save_after_compaction()
        return result

    async def create_branch_summary(
        self,
        *,
        config: BranchSummaryConfig | None = None,
        summary_generator: SummaryGenerator | None = None,
    ) -> BranchSummary:
        """基于 session 或 agent 上下文生成分支级旁路摘要。

        - attach session：用 session 作为来源
        - 未 attach：用 agent.state.messages + harness.snapshots
        - 调 compaction.create_branch_summary（不修改任何 messages）
        - 设置 last_branch_summary
        - attach session 时 append_branch_summary + 按 policy 落盘
        """
        summary = await _create_branch_summary_fn(
            session=self.session,
            messages=(
                None if self.session is not None else list(self.agent.state.messages)
            ),
            snapshots=(
                None if self.session is not None else list(self.snapshots)
            ),
            config=config,
            summary_generator=summary_generator,
        )
        self.last_branch_summary = summary

        if self.session is not None:
            self.session.append_branch_summary(summary)
            await self._maybe_save_after_compaction()

        return summary

    def get_branch_summary(self, index: int = -1) -> BranchSummary | None:
        """取第 index 个 branch summary；默认 -1 = 最近一次。

        - attach session：从 session.branch_summaries 取
        - 未 attach：仅 index=-1 时返回 self.last_branch_summary；其它返回 None
        """
        if self.session is not None:
            summaries = self.session.get_branch_summaries()
            if not summaries:
                return None
            return typing.cast("BranchSummary | None", summaries[index])
        if index == -1:
            return self.last_branch_summary
        return None

    def clear_branch_summaries(self) -> None:
        """清空 branch_summaries；不动 messages / snapshots / compactions。"""
        if self.session is not None:
            self.session.clear_branch_summaries()
        self.last_branch_summary = None

    async def _maybe_save_after_compaction(self) -> None:
        """compaction / branch summary 后按 policy 落盘——复用 Step 13 逻辑。

        若 session_store 存在且 auto_save_policy != "never"——保存。
        失败时静默吞掉（compaction 是显式调用，错误应可见但不阻塞返回）；
        若需要严格可见，调方应在 try 中检查 session_store。
        """
        if self.session is None or self.session_store is None:
            return
        if self.session_sync_config.auto_save_policy == "never":
            return
        try:
            await self.session_store.save(self.session)
        except Exception as e:
            # 不覆盖 compact_* 的返回值；写到 context.metadata
            self.context.metadata.setdefault("compaction_save_errors", []).append(
                f"{type(e).__name__}: {e}"
            )

    # ------------------------------------------------------------------
    # Step 13：_finish_snapshot 现在返回 snapshot；_post_finish_snapshot 做 sync/save
    # ------------------------------------------------------------------

    def _finish_snapshot(
        self,
        *,
        status: SnapshotStatus,
        messages_after: list[AgentMessage],
        error: str | None = None,
    ) -> RequestSnapshot | None:
        """ finalize 当前 builder 的 snapshot，存入 last_snapshot / snapshots。

        Step 12：若 session 已 attach，自动调 session.append_snapshot。
        Step 13：返回 snapshot（可能为 None，若 builder 未启用）。

        被 _run_one 的所有完成路径调用（正常 completed/aborted + 三类 error）。
        completed / aborted / error 三类 snapshot 都进 session——有调试价值。
        """
        if self._snapshot_builder is None:
            return None
        snapshot = self._snapshot_builder.finish(
            status=status,
            messages_after=messages_after,
            error=error,
            metadata=dict(self.context.metadata),
        )
        self.context.snapshot = snapshot
        self.last_snapshot = snapshot
        self.snapshots.append(snapshot)

        # Step 12：自动同步到 session（若已 attach）
        if self.session is not None:
            self.session.append_snapshot(snapshot)

        return snapshot

    async def _post_finish_snapshot(self, snapshot: RequestSnapshot | None) -> None:
        """Step 13：_finish_snapshot 之后的 sync + consistency + auto-save。

        顺序：
        1. 若有 session + config.sync_harness_metadata：sync_session_metadata
        2. 若有 session + config.sync_agent_messages：sync_agent_messages_to_session
        3. 若有 session：check_session_consistency（strict 时抛 RuntimeError）
        4. 若有 session + session_store + policy 允许：_maybe_auto_save_session
        5. 若 auto-save 失败：写 context.metadata["post_finish_errors"] +
           若 sync_harness_metadata 开启，**立即再 sync 一次**——让错误进入
           session.metadata["harness"]["context_metadata"]

        注意：本方法在 error 路径中也会被调用——便于诊断错误状态。
        auto-save 失败不覆盖原始异常；追加到 metadata["post_finish_errors"]。
        """
        if snapshot is None or self.session is None:
            return

        if self.session_sync_config.sync_harness_metadata:
            self.sync_session_metadata()

        if self.session_sync_config.sync_agent_messages:
            self.sync_agent_messages_to_session()

        # consistency check（strict 时会抛 RuntimeError）
        self.check_session_consistency()

        # auto-save（policy 判断在方法内部）
        try:
            await self._maybe_auto_save_session(snapshot)
        except Exception as e:
            # auto-save 失败不应覆盖原始路径的返回值/异常；写入 metadata 供诊断
            self.context.metadata.setdefault("post_finish_errors", []).append(
                f"auto_save: {type(e).__name__}: {e}"
            )
            # 关键：sync_session_metadata 已经在前面跑过——若不再 sync 一次，
            # 这条 post_finish_errors 不会进入 session.metadata["harness"]。
            # 立即 re-sync 让错误可观测。
            if self.session_sync_config.sync_harness_metadata:
                self.sync_session_metadata()

    async def _maybe_auto_save_session(self, snapshot: RequestSnapshot) -> None:
        """根据 policy 决定是否自动保存 session 到 session_store。

        - 没有 session 或 session_store：直接 return
        - "never"：return
        - "after_success"：snapshot.status != "completed" → return
        - "after_snapshot" / "after_any_request"：保存（任意 status）
        """
        if self.session is None or self.session_store is None:
            return
        policy = self.session_sync_config.auto_save_policy
        if policy == "never":
            return
        if policy == "after_success" and snapshot.status != "completed":
            return
        # after_snapshot / after_any_request / after_success（completed）→ 保存
        await self.session_store.save(self.session)

    # ----------------------------------------------------------------------
    # 内部：Agent 事件 → on_event
    # ----------------------------------------------------------------------

    async def _handle_agent_event(self, event: AgentEvent, _agent_state: AgentState) -> None:
        """Agent.subscribe 的回调。

        - 先 observe_event 进 snapshot（即使 on_event hook 抛异常，事件也已记录）
        - 更新 context.last_event
        - 跑 on_event hooks（抛异常不破坏 Agent 主流程；记录 + on_error）
        """
        # Step 11：先把事件交给 snapshot builder——保证 hook 抛异常也不丢事件
        if self._snapshot_builder is not None:
            self._snapshot_builder.observe_event(event)

        self.context.last_event = event
        for hook in list(self.on_event_hooks):
            try:
                result = hook(event, self.context)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                # on_event 异常不影响 Agent；记录 + on_error hooks（不重抛）
                self.context.last_error = (
                    f"on_event: {type(e).__name__}: {e}"
                )
                # Step 11：写入 metadata["event_errors"]，最终进入 snapshot
                self.context.metadata.setdefault("event_errors", []).append(
                    f"{type(e).__name__}: {e}"
                )
                await self._run_on_error(e)

    # ----------------------------------------------------------------------
    # 内部：hook 执行器
    # ----------------------------------------------------------------------

    async def _run_hooks(self, hooks: list[Any]) -> None:
        """顺序执行 hooks；任一抛异常立即停止并向上抛。"""
        for hook in list(hooks):
            result = hook(self.context)
            if inspect.isawaitable(result):
                await result

    async def _run_on_error(self, exc: Exception) -> None:
        """执行 on_error hooks；它们抛异常追加到 metadata，不递归。"""
        for hook in list(self.on_error_hooks):
            try:
                result = hook(exc, self.context)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                errs = self.context.metadata.setdefault("on_error_errors", [])
                errs.append(f"{type(e).__name__}: {e}")

    async def _fail(self, exc: Exception) -> None:
        """进入 error 态、记录 last_error、跑 on_error hooks。"""
        self.context.phase = "error"
        self.context.last_error = f"{type(exc).__name__}: {exc}"
        await self._run_on_error(exc)


__all__ = [
    "HarnessPhase", "HarnessContext",
    "BeforeRequestHook", "AfterRequestHook",
    "OnEventHook", "OnErrorHook",
    "AgentHarness",
    # Step 11 重导出（方便 from .harness import ...）
    "SnapshotBuilder", "SnapshotStatus", "RequestSnapshot", "TurnSnapshot",
    # Step 12 重导出
    "SessionMemory", "SessionStore",
    # Step 13 重导出
    "SessionAutoSavePolicy", "SessionSyncConfig",
    "SessionConsistencyIssue", "SessionConsistencyReport",
    # Step 14 重导出
    "Skill", "SkillRegistry", "SkillSelection", "SkillInjectionConfig",
    "PromptTemplateRenderError",
    "SkillNotFoundError", "SkillRegistrationError",
    # Step 15 重导出
    "CompactionConfig", "CompactionResult",
    "BranchSummary", "BranchSummaryConfig",
    "SummaryGenerator",
    # Step 18 重导出
    "ToolPermissionPolicy", "ToolPermissionAuditRecord",
    "InMemoryToolPermissionAuditLog",
    # Step 19 重导出
    "SkillLoadConfig", "SkillFileLoader",
    "MCPPromptInfo",
]

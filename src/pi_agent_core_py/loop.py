"""Agent Loop——Step 7 多工具 batch 执行。

入口：
- `run_event_loop`：async generator，按顺序 yield AgentEvent（主入口）
- `run_min_loop`：Step 1 便捷封装

Step 7 的核心变化（相对 Step 6）：
- 移除"多 ToolCall 显式拒绝"分支
- 单工具执行链路升级为 `_execute_tool_batch`（sequential / parallel）
- `_execute_tool_with_hooks`（Step 6）保留，作为 batch 内部的"单工具执行单元"
- 新增 `ExecutedToolResult`（index + tool_call + result + message）
- terminate 早停规则：**本批所有工具 terminate=True 才早停**

事件顺序契约：
- 工具生命周期：tool_execution_start → update* → tool_execution_end
- 最终 ToolResultMessage 的 message_start/end 在 execution_end 之后
- parallel 模式 start 按源序，update/end 按实际完成序，result message 按源序
- 每次 LLM 调用及其当批工具构成一个 turn，下一次调用前重新发 turn_start

Hook 契约（继承 Step 6）：
- before_tool_call / after_tool_call 对每个工具独立生效
- 任一 hook 抛异常 → 当前工具转 error ToolResult；batch 不崩
- before 改 tool_call.name 时重新查 registry（Step 6 修复保留）

Step 9 新增（Queue / Abort）：
- run_event_loop 加 `signal: asyncio.Event | None` 参数
- 多个 abort 检查点：开始 while iter / 流式 ErrorEvent / 工具 batch 前 / sequential 模式 per-tool
- abort 触发时生成 `AssistantMessage(stop_reason="aborted", error_message="aborted", content=[])`
- `_execute_tool_batch` 也接 signal：sequential 模式下逐个工具前检查
- 信号传给 `client.stream(signal=...)`——FakeClient / GLMClient 已支持
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from .context import (
    ConvertToLLMFn,
    TransformContextFn,
    apply_transform_context,
    convert_to_llm,
)
from .context import (
    transform_context as default_transform_context,
)
from .events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from .hooks import (
    AfterToolCallContext,
    AfterToolCallFn,
    AfterToolCallResult,
    BeforeToolCallContext,
    BeforeToolCallFn,
    BeforeToolCallResult,
    default_after_tool_call,
    default_before_tool_call,
)
from .llm_messages import LLMMessage
from .messages import (
    AgentMessage,
    AssistantMessage,
    GenerationMetrics,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from .model_client import (
    DoneEvent,
    ErrorEvent,
    ModelClient,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallEvent,
    ToolCallStartEvent,
)
from .policy import (
    InMemoryToolPermissionAuditLog,
    ToolApprovalContext,
    ToolApprovalHandler,
    ToolPermissionAuditRecord,
    ToolPermissionDecision,
    ToolPermissionPolicy,
)
from .tool_validation import (
    ToolArgumentValidationError,
    validate_tool_arguments,
)
from .tools import (
    AgentTool,
    ToolExecutionMode,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolUpdateCallback,
    _validate_tool_execution_mode,
)

# ============================================================================
# 内部数据结构
# ============================================================================


@dataclass
class ExecutedToolResult:
    """batch 内单个工具的执行产物。

    `index` 保留 ToolCall 在原 assistant 消息里的源序；
    parallel 模式下完成后用它把 results 重排回源序，保证 new_messages 稳定。
    """

    index: int
    tool_call: ToolCall
    result: ToolResult
    message: ToolResultMessage


def _tool_result_message(result: ToolResult) -> ToolResultMessage:
    """Build the transcript message without dropping ToolResult metadata."""
    return ToolResultMessage(
        tool_call_id=result.tool_call_id,
        name=result.name,
        content=result.content,
        is_error=result.is_error,
        terminate=result.terminate,
        details=result.details,
        usage=result.usage,
        added_tool_names=list(result.added_tool_names),
    )


@dataclass
class _PreparedToolCall:
    """已串行完成 preflight、可以进入实际执行阶段的工具调用。"""

    tool_call: ToolCall
    tool: AgentTool
    messages: list[AgentMessage]
    after_tool_call: AfterToolCallFn
    signal: asyncio.Event | None


@dataclass
class _BatchDone:
    """`_execute_tool_batch` 的结束哨兵——携带按源序排好的结果。

    非公开 AgentEvent；只在 loop.py 内部用作 async generator 的"返回值"。
    """

    ordered: list[ExecutedToolResult]


@dataclass(frozen=True)
class TurnControlContext:
    """传给 turn 控制回调的只读运行时视图。"""

    turn_index: int
    messages: tuple[AgentMessage, ...]
    new_messages: tuple[AgentMessage, ...]
    message: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    signal: asyncio.Event | None
    system_prompt: str = ""
    client: ModelClient | None = None
    tools: tuple[AgentTool, ...] = ()
    thinking_level: str = "off"


@dataclass(frozen=True)
class AgentLoopTurnUpdate:
    """Runtime replacements applied immediately before an actual next turn."""

    messages: Sequence[AgentMessage] | None = None
    system_prompt: str | None = None
    client: ModelClient | None = None
    tools: ToolRegistry | Iterable[AgentTool] | None = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class ModelCallContext:
    """Exact, read-only input immediately before one Provider call."""

    turn_index: int
    system_prompt: str
    messages: tuple[LLMMessage, ...]
    tools: tuple[Any, ...]
    client: ModelClient
    signal: asyncio.Event | None


@dataclass(frozen=True)
class ModelCallDecision:
    allow: bool = True
    error_message: str | None = None


ShouldStopAfterTurnFn = Callable[[TurnControlContext], bool | Awaitable[bool]]
PrepareNextTurnFn = Callable[
    [TurnControlContext],
    AgentLoopTurnUpdate
    | list[AgentMessage]
    | None
    | Awaitable[AgentLoopTurnUpdate | list[AgentMessage] | None],
]
BeforeModelCallFn = Callable[
    [ModelCallContext],
    ModelCallDecision | bool | None | Awaitable[ModelCallDecision | bool | None],
]
PendingMessagesFn = Callable[
    [],
    Sequence[AgentMessage] | None | Awaitable[Sequence[AgentMessage] | None],
]


@dataclass
class _ToolUpdateItem:
    index: int
    tool_call: ToolCall
    result: ToolResult


@dataclass
class _ToolDoneItem:
    index: int
    executed: ExecutedToolResult | None = None
    error: BaseException | None = None


async def _await_maybe(value: Any) -> Any:
    """同时支持 sync / async 的控制回调。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def _poll_pending_messages(
    callback: PendingMessagesFn | None,
) -> list[AgentMessage]:
    """Drain one queue poll into an isolated list.

    Queue ownership stays with the caller (normally :class:`Agent`).  The loop
    only asks for messages at the upstream-defined delivery boundaries.
    """
    if callback is None:
        return []
    pending = await _await_maybe(callback())
    return list(pending) if pending else []


def _accepts_parameter(fn: Callable[..., Any], name: str) -> bool:
    """签名探测失败时采用新版契约；可探测时兼容旧版实现。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _call_tool_hook(
    hook: Callable[..., Any],
    context: BeforeToolCallContext | AfterToolCallContext,
    signal: asyncio.Event | None,
) -> Any:
    """调用新版 ``hook(context, signal=...)``，并兼容旧的一参数 hook。"""
    if _accepts_parameter(hook, "signal"):
        return await _await_maybe(hook(context, signal=signal))
    return await _await_maybe(hook(context))


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


def _validate_thinking_level(level: str) -> ThinkingLevel:
    if level not in _THINKING_LEVELS:
        raise ValueError(f"unsupported thinking level: {level}")
    return cast(ThinkingLevel, level)


async def _call_tool_execute(
    tool: AgentTool,
    tool_call: ToolCall,
    *,
    signal: asyncio.Event | None,
    on_update: ToolUpdateCallback | None,
) -> ToolResult:
    """调用完整工具契约，并让旧版二参数工具平滑迁移。"""
    kwargs: dict[str, Any] = {}
    if _accepts_parameter(tool.execute, "signal"):
        kwargs["signal"] = signal
    if _accepts_parameter(tool.execute, "on_update"):
        kwargs["on_update"] = on_update
    return await tool.execute(tool_call.id, tool_call.arguments, **kwargs)


# ============================================================================
# Registry 适配
# ============================================================================


def _to_registry(
    tools: ToolRegistry | Iterable[AgentTool] | None,
) -> ToolRegistry:
    """统一转 ToolRegistry。None 时返回空 registry。"""
    if tools is None:
        return ToolRegistry()
    if isinstance(tools, ToolRegistry):
        return tools
    return ToolRegistry(list(tools))


def _should_run_sequential(
    registry: ToolRegistry,
    tool_calls: list[ToolCall],
    tool_execution: ToolExecutionMode = "parallel",
) -> bool:
    """全局串行或任一**已注册**工具声明串行 → 整批串行。

    逐工具 ``parallel`` 不能放宽全局 ``sequential``。ToolNotFoundError
    的工具不参与逐工具判断（错误结果不影响模式选择）。
    """
    if tool_execution == "sequential":
        return True
    for tc in tool_calls:
        if not registry.has(tc.name):
            continue
        tool = registry.get(tc.name)
        if tool.execution_mode == "sequential":
            return True
    return False


# ============================================================================
# 单工具执行（含 hooks）——Step 6 保留，作为 batch 的执行单元
# ============================================================================


async def _execute_tool_with_hooks(
    *,
    registry: ToolRegistry,
    tool_call: ToolCall,
    messages: Sequence[AgentMessage],
    before_tool_call: BeforeToolCallFn,
    after_tool_call: AfterToolCallFn,
    signal: asyncio.Event | None = None,
    on_update: ToolUpdateCallback | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    tool_approval_handler: ToolApprovalHandler | None = None,
    prepare_only: bool = False,
) -> ToolResult | _PreparedToolCall:
    """执行单个工具，含 before / after hooks；所有失败路径都包成 ToolResult。

    顺序：
      1. registry.get(name) → tool 或 None（ToolNotFoundError）
      2. before_tool_call(BeforeToolCallContext{tool_call, tool, messages})
         - 抛异常    → error ToolResult（details.hook="before_tool_call"）
         - allow=False → error ToolResult（details.blocked_by="before_tool_call"）
         - tool_call 不为 None → 用新 tool_call 继续，但 id 必须保持不变
      3. 若 hook 修改了 tool_call.name → 重查 registry
      4. 若 tool is None → error ToolResult（ToolNotFoundError）
      5. **Step 18 permission_policy.check_tool_call**（在 validation 前）
         - policy 抛异常 → error ToolResult（error_type="ToolPermissionPolicyError"）+ audit deny
         - decision="deny" → error ToolResult（error_type="ToolPermissionDenied"）+ audit deny
         - decision="require_approval"
           → error ToolResult（error_type="ToolApprovalRequired"）+ audit
         - decision="allow" → 继续；audit allow
      6. validate_tool_arguments（Step 16）—— 失败 → error ToolResult（不调 execute / after）
      7. tool.execute(...) → ToolResult；抛异常 → error ToolResult
      8. after_tool_call(AfterToolCallContext{tool_call, tool, result, messages})
         - 抛异常    → 丢弃原 result，error ToolResult（details.hook="after_tool_call"）
         - 返回新 result → 透传

    Step 18 policy=None 时跳过 5——保持向后兼容。

    Step 9 signal 检查点（4 处，任一命中即返回 aborted ToolResult）：
      - before_tool_call 之前
      - tool.execute 之前
      - tool.execute 之后
      - after_tool_call 之前
    """
    # 1. registry 查找
    try:
        tool = registry.get(tool_call.name)
    except ToolNotFoundError:
        tool = None

    # Tool-owned compatibility normalization happens before hooks and schema
    # validation. Unlike hook mutations, this has one narrow, reusable owner.
    if tool is not None:
        try:
            prepared_arguments = tool.prepare_arguments(tool_call.arguments)
            tool_call = tool_call.model_copy(
                update={"arguments": prepared_arguments},
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=[
                    TextContent(
                        text=f"Argument preparation failed: {type(e).__name__}: {e}",
                    )
                ],
                is_error=True,
                details={"error_type": "ToolArgumentPreparationError"},
            )

    # Step 9 检查点 1：before_tool_call 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True,
            details={"error_type": "AbortSignal"},
        )

    # 2. before_tool_call
    try:
        before_ctx = BeforeToolCallContext(
            tool_call=tool_call,
            tool=tool,
            messages=list(messages),
        )
        before_result = BeforeToolCallResult.model_validate(
            await _call_tool_hook(before_tool_call, before_ctx, signal)
        )
    except Exception as e:
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[
                TextContent(
                    text=f"before_tool_call failed: {type(e).__name__}: {e}",
                )
            ],
            is_error=True,
            details={
                "hook": "before_tool_call",
                "error_type": type(e).__name__,
            },
        )

    # 3. 阻断
    if not before_result.allow:
        merged_details: dict[str, Any] = {"blocked_by": "before_tool_call"}
        merged_details.update(before_result.details)
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text=before_result.reason or "Tool call blocked")],
            is_error=True,
            terminate=before_result.terminate,
            details=merged_details,
        )

    # 4. 使用修改后的 tool_call（若有）
    effective_tool_call = before_result.tool_call or tool_call

    # ToolCall ID 是 Provider 协议中 assistant tool call 与 tool result 的关联键。
    # hook 可以兼容性地修正 name / arguments，但绝不能创建新的调用身份。
    if effective_tool_call.id != tool_call.id:
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[
                TextContent(
                    text=(
                        "before_tool_call cannot change tool_call.id "
                        f"({tool_call.id!r} -> {effective_tool_call.id!r})"
                    )
                )
            ],
            is_error=True,
            details={
                "hook": "before_tool_call",
                "error_type": "ToolCallIdentityMutationError",
            },
        )

    # 4.1 如果 hook 修改了 tool_call.name，需要重新查 registry
    if before_result.tool_call is not None and effective_tool_call.name != tool_call.name:
        try:
            tool = registry.get(effective_tool_call.name)
        except ToolNotFoundError:
            tool = None

    # 4.5 Step 18：权限策略检查（在 validation / execute / after_tool_call 之前）
    # - policy=None → 跳过，保持向后兼容
    # - policy 抛异常 / 返回非法对象 → 不让 loop 崩；按 deny 处理
    #   （error_type=ToolPermissionPolicyError）
    # - decision="deny" → error_type=ToolPermissionDenied
    # - decision="require_approval" → error_type=ToolApprovalRequired（Step 18 不执行）
    # - decision="allow" → 继续
    #
    # 注意：policy 在 "tool is None" 检查之前——LLM 调了未注册的工具名，policy 仍会
    # 先记一条 audit（可能是 allow / deny / require_approval）。这给 policy 一个机会
    # 记录或拦截未知工具名；不影响安全（未注册工具不会 execute）。
    #
    # 关键：policy 调用 + isinstance 校验 + audit append + deny/require_approval 分支
    # 全部包在同一个 try 里——任何一条出错都进 except，转 ToolPermissionPolicyError。
    if permission_policy is not None:
        try:
            decision = await permission_policy.check_tool_call(
                tool_call=effective_tool_call,
                tool=tool,
                messages=list(messages),
            )
            if not isinstance(decision, ToolPermissionDecision):
                raise TypeError(
                    f"permission policy must return ToolPermissionDecision, "
                    f"got {type(decision).__name__}"
                )

            # audit log（allow / deny / require_approval 都记）
            if permission_audit_log is not None:
                permission_audit_log.append(
                    ToolPermissionAuditRecord(
                        tool_call_id=effective_tool_call.id,
                        tool_name=effective_tool_call.name,
                        decision=decision.decision,
                        policy_name=decision.policy_name,
                        reason=decision.reason,
                        metadata=dict(decision.metadata),
                    )
                )

            if decision.denied:
                return ToolResult(
                    tool_call_id=effective_tool_call.id,
                    name=effective_tool_call.name,
                    content=[
                        TextContent(
                            text=f"Tool call denied by policy: {decision.reason or 'no reason'}",
                        )
                    ],
                    is_error=True,
                    details={
                        "error_type": "ToolPermissionDenied",
                        "policy": {
                            "decision": "deny",
                            "policy_name": decision.policy_name,
                            "reason": decision.reason,
                            "metadata": dict(decision.metadata),
                        },
                    },
                )
            if decision.require_approval:
                policy_details = {
                    "decision": "require_approval",
                    "policy_name": decision.policy_name,
                    "reason": decision.reason,
                    "metadata": dict(decision.metadata),
                }
                if tool_approval_handler is None:
                    # 保持 Step 18 兼容语义：没有交互层时绝不执行。
                    return ToolResult(
                        tool_call_id=effective_tool_call.id,
                        name=effective_tool_call.name,
                        content=[
                            TextContent(
                                text=(
                                    "Tool call requires approval but no approval "
                                    f"handler is available: {decision.reason or 'no reason'}"
                                ),
                            )
                        ],
                        is_error=True,
                        details={
                            "error_type": "ToolApprovalRequired",
                            "policy": policy_details,
                        },
                    )
                try:
                    approved = await _await_maybe(
                        tool_approval_handler(
                            ToolApprovalContext(
                                tool_call=effective_tool_call,
                                tool=tool,
                                decision=decision,
                                signal=signal,
                            )
                        )
                    )
                    if not isinstance(approved, bool):
                        raise TypeError(
                            f"tool approval handler must return bool, got {type(approved).__name__}"
                        )
                except Exception as approval_exc:
                    return ToolResult(
                        tool_call_id=effective_tool_call.id,
                        name=effective_tool_call.name,
                        content=[
                            TextContent(
                                text=(
                                    "Tool approval handler failed: "
                                    f"{type(approval_exc).__name__}: {approval_exc}"
                                )
                            )
                        ],
                        is_error=True,
                        details={
                            "error_type": "ToolApprovalHandlerError",
                            "policy": policy_details,
                        },
                    )
                if not approved:
                    return ToolResult(
                        tool_call_id=effective_tool_call.id,
                        name=effective_tool_call.name,
                        content=[TextContent(text="Tool call denied by user.")],
                        is_error=True,
                        details={
                            "error_type": "ToolApprovalDenied",
                            "policy": policy_details,
                        },
                    )
                # Approve once：只放行当前精确 ToolCall，随后继续 validation/execute。
            # decision="allow" 继续
        except Exception as policy_exc:
            audit_record = ToolPermissionAuditRecord(
                tool_call_id=effective_tool_call.id,
                tool_name=effective_tool_call.name,
                decision="deny",
                policy_name=getattr(permission_policy, "name", "unknown"),
                reason=f"policy raised exception: {type(policy_exc).__name__}: {policy_exc}",
                metadata={"policy_exception": type(policy_exc).__name__},
            )
            if permission_audit_log is not None:
                permission_audit_log.append(audit_record)
            return ToolResult(
                tool_call_id=effective_tool_call.id,
                name=effective_tool_call.name,
                content=[
                    TextContent(
                        text=(
                            f"Permission policy error: {type(policy_exc).__name__}: {policy_exc}"
                        ),
                    )
                ],
                is_error=True,
                details={
                    "error_type": "ToolPermissionPolicyError",
                    "policy": {
                        "decision": "deny",
                        "policy_name": audit_record.policy_name,
                        "reason": audit_record.reason,
                        "metadata": dict(audit_record.metadata),
                    },
                },
            )

    # 5. 工具不存在（after hook 不会跑——还没执行）
    if tool is None:
        return ToolResult(
            tool_call_id=effective_tool_call.id,
            name=effective_tool_call.name,
            content=[TextContent(text=f"Tool not found: {effective_tool_call.name}")],
            is_error=True,
            details={"error_type": "ToolNotFoundError"},
        )

    # 5.5 参数校验（Step 16 新增）
    # 校验 effective_tool_call.arguments（before_tool_call 改过的也走这里）
    # 失败：返回 is_error ToolResult，不调 execute、不调 after_tool_call
    try:
        validate_tool_arguments(
            tool_name=effective_tool_call.name,
            schema=tool.parameters,
            arguments=effective_tool_call.arguments,
        )
    except ToolArgumentValidationError as e:
        return ToolResult(
            tool_call_id=effective_tool_call.id,
            name=effective_tool_call.name,
            content=[TextContent(text=f"Argument validation failed: {e}")],
            is_error=True,
            details={
                "error_type": "ToolArgumentValidationError",
                "validation": {
                    "tool_name": effective_tool_call.name,
                    "message": str(e),
                },
            },
        )

    prepared = _PreparedToolCall(
        tool_call=effective_tool_call,
        tool=tool,
        messages=list(messages),
        after_tool_call=after_tool_call,
        signal=signal,
    )
    if prepare_only:
        return prepared
    return await _execute_prepared_tool_call(
        prepared=prepared,
        on_update=on_update,
    )


async def _execute_prepared_tool_call(
    *,
    prepared: _PreparedToolCall,
    on_update: ToolUpdateCallback | None = None,
) -> ToolResult:
    """执行已完成 preflight 的工具，并运行 after hook。"""
    tool_call = prepared.tool_call
    tool = prepared.tool
    signal = prepared.signal
    accepting_updates = True
    update_loop = asyncio.get_running_loop()

    def guarded_update(partial_result: ToolResult) -> Awaitable[None]:
        """Ignore progress reported after ``execute`` has settled.

        Tools sometimes retain the callback and invoke it from detached work.
        Such updates no longer belong to this execution and must not leak into
        another parallel tool's still-active event queue.
        """
        if not accepting_updates or on_update is None:
            completed: asyncio.Future[None] = update_loop.create_future()
            completed.set_result(None)
            return completed
        return on_update(partial_result)

    # Step 9 检查点 2：tool.execute 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True,
            details={"error_type": "AbortSignal"},
        )

    # 6. 执行
    try:
        try:
            raw_result = await _call_tool_execute(
                tool,
                tool_call,
                signal=signal,
                on_update=guarded_update if on_update is not None else None,
            )
        finally:
            accepting_updates = False
        result = ToolResult.model_validate(raw_result)
        # 工具实现不拥有调用身份；即使返回了错误 ID，也要绑定回原 ToolCall。
        if result.tool_call_id != tool_call.id:
            result = result.model_copy(update={"tool_call_id": tool_call.id})
    except Exception as e:
        result = ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text=f"{type(e).__name__}: {e}")],
            is_error=True,
            details={"error_type": type(e).__name__},
        )

    # Step 9 检查点 3/4：tool.execute 之后、after_tool_call 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True,
            details={"error_type": "AbortSignal"},
        )

    # 7. after_tool_call
    try:
        after_ctx = AfterToolCallContext(
            tool_call=tool_call,
            tool=tool,
            result=result,
            messages=list(prepared.messages),
        )
        hook_result = await _call_tool_hook(
            prepared.after_tool_call,
            after_ctx,
            signal,
        )
        if hook_result is None:
            final_result = result
        elif isinstance(hook_result, AfterToolCallResult):
            updates: dict[str, Any] = {}
            if hook_result.content is not None:
                updates["content"] = hook_result.content
            if hook_result.details is not None:
                updates["details"] = hook_result.details
            if hook_result.is_error is not None:
                updates["is_error"] = hook_result.is_error
            if hook_result.usage is not None:
                updates["usage"] = hook_result.usage
            if hook_result.terminate is not None:
                updates["terminate"] = hook_result.terminate
            final_result = ToolResult.model_validate(
                result.model_copy(update=updates)
            )
        else:
            final_result = ToolResult.model_validate(hook_result)
    except Exception as e:
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[
                TextContent(
                    text=f"after_tool_call failed: {type(e).__name__}: {e}",
                )
            ],
            is_error=True,
            details={
                "hook": "after_tool_call",
                "error_type": type(e).__name__,
            },
        )

    if final_result.tool_call_id != tool_call.id:
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[
                TextContent(
                    text=(
                        "after_tool_call cannot change tool_call_id "
                        f"({tool_call.id!r} -> {final_result.tool_call_id!r})"
                    )
                )
            ],
            is_error=True,
            details={
                "hook": "after_tool_call",
                "error_type": "ToolCallIdentityMutationError",
            },
        )

    # added_tool_names describes the load point produced by tool execution.
    # Upstream afterToolCall overrides do not expose this field, so a hook must
    # not be able to invent or remove deferred-tool availability metadata.
    return final_result.model_copy(
        update={"added_tool_names": list(result.added_tool_names)}
    )


# ============================================================================
# 多工具 batch 执行——Step 7 核心
# ============================================================================


async def _exec_one_tool(
    *,
    index: int,
    tool_call: ToolCall,
    registry: ToolRegistry,
    messages: Sequence[AgentMessage],
    before_fn: BeforeToolCallFn,
    after_fn: AfterToolCallFn,
    signal: asyncio.Event | None = None,
    on_update: ToolUpdateCallback | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    tool_approval_handler: ToolApprovalHandler | None = None,
) -> ExecutedToolResult:
    """执行单个工具并包成 ExecutedToolResult（含 message）。

    纯计算 / IO，不发事件。事件由 `_execute_tool_batch` 的调用方发。

    Step 9：signal 在工具执行前检查——set 时立即返回 aborted ToolResult，
    不调 before/after hook、不执行 tool.execute。

    Step 18：透传 permission_policy / permission_audit_log 给
    `_execute_tool_with_hooks`。
    """
    if signal is not None and signal.is_set():
        result = ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True,
            details={"error_type": "AbortSignal"},
        )
    else:
        prepared_or_result = await _execute_tool_with_hooks(
            registry=registry,
            tool_call=tool_call,
            messages=messages,
            before_tool_call=before_fn,
            after_tool_call=after_fn,
            signal=signal,
            on_update=on_update,
            permission_policy=permission_policy,
            permission_audit_log=permission_audit_log,
            tool_approval_handler=tool_approval_handler,
        )
        if isinstance(prepared_or_result, _PreparedToolCall):
            raise RuntimeError("tool preflight unexpectedly remained deferred")
        result = prepared_or_result
    msg = _tool_result_message(result)
    return ExecutedToolResult(
        index=index,
        tool_call=tool_call,
        result=result,
        message=msg,
    )


async def _execute_tool_batch(
    *,
    registry: ToolRegistry,
    tool_calls: list[ToolCall],
    messages: Sequence[AgentMessage],
    before_fn: BeforeToolCallFn,
    after_fn: AfterToolCallFn,
    tool_execution: ToolExecutionMode = "parallel",
    signal: asyncio.Event | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    tool_approval_handler: ToolApprovalHandler | None = None,
) -> AsyncIterator[Any]:
    """执行一批工具，并遵守上游事件顺序契约。

    ``tool_execution_start → update* → tool_execution_end`` 描述工具生命周期；
    最终 ToolResultMessage 一律在 end 之后发出。并行模式的 start 保持源序、
    update/end 保持实际到达/完成序、result message 保持源序。
    """
    is_sequential = _should_run_sequential(registry, tool_calls, tool_execution)

    async def produce(
        index: int,
        tc: ToolCall,
        queue: asyncio.Queue[_ToolUpdateItem | _ToolDoneItem],
        prepared: _PreparedToolCall | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()

        def on_update(partial_result: ToolResult) -> Awaitable[None]:
            future: asyncio.Future[None] = loop.create_future()
            try:
                partial = ToolResult.model_validate(partial_result)
                queue.put_nowait(
                    _ToolUpdateItem(
                        index=index,
                        tool_call=tc,
                        result=partial,
                    )
                )
                future.set_result(None)
            except Exception as exc:
                future.set_exception(exc)
            return future

        try:
            if prepared is None:
                executed = await _exec_one_tool(
                    index=index,
                    tool_call=tc,
                    registry=registry,
                    messages=list(messages),
                    before_fn=before_fn,
                    after_fn=after_fn,
                    signal=signal,
                    on_update=on_update,
                    permission_policy=permission_policy,
                    permission_audit_log=permission_audit_log,
                    tool_approval_handler=tool_approval_handler,
                )
            else:
                result = await _execute_prepared_tool_call(
                    prepared=prepared,
                    on_update=on_update,
                )
                executed = executed_result(index, tc, result)
            queue.put_nowait(_ToolDoneItem(index=index, executed=executed))
        except Exception as exc:  # 最后一层安全网：单工具不能炸掉整个 batch
            queue.put_nowait(_ToolDoneItem(index=index, error=exc))

    def failed_execution(index: int, tc: ToolCall, exc: BaseException) -> ExecutedToolResult:
        result = ToolResult(
            tool_call_id=tc.id,
            name=tc.name,
            content=[TextContent(text=f"{type(exc).__name__}: {exc}")],
            is_error=True,
            details={"error_type": type(exc).__name__},
        )
        return ExecutedToolResult(
            index=index,
            tool_call=tc,
            result=result,
            message=_tool_result_message(result),
        )

    def executed_result(
        index: int,
        tc: ToolCall,
        result: ToolResult,
    ) -> ExecutedToolResult:
        return ExecutedToolResult(
            index=index,
            tool_call=tc,
            result=result,
            message=_tool_result_message(result),
        )

    if is_sequential:
        ordered: list[ExecutedToolResult] = []
        for i, tc in enumerate(tool_calls):
            # Step 9：sequential 模式下逐工具前检查 signal
            if signal is not None and signal.is_set():
                break
            yield ToolExecutionStartEvent(tool_call=tc)
            queue: asyncio.Queue[_ToolUpdateItem | _ToolDoneItem] = asyncio.Queue()
            task = asyncio.create_task(produce(i, tc, queue))
            executed: ExecutedToolResult | None = None
            while executed is None:
                item = await queue.get()
                if isinstance(item, _ToolUpdateItem):
                    yield ToolExecutionUpdateEvent(
                        tool_call=item.tool_call,
                        partial_result=item.result,
                    )
                    continue
                executed = (
                    item.executed
                    if item.executed is not None
                    else failed_execution(i, tc, item.error or RuntimeError("tool failed"))
                )
            await task
            ordered.append(executed)
            yield ToolExecutionEndEvent(tool_call=tc, result=executed.result)
            yield MessageStartEvent(message=executed.message)
            yield MessageEndEvent(message=executed.message)
    else:
        # Step 9：parallel 模式 batch 开始前检查 signal——已 abort 就不发 start
        if signal is not None and signal.is_set():
            yield _BatchDone(ordered=[])
            return

        # pi-agent 语义：并行仅作用于实际 tool.execute。所有 preflight
        # （before hook、policy、approval、validation）必须按源序串行完成，
        # 避免审批重叠和安全决策竞态。
        prepared_calls: list[tuple[int, ToolCall, _PreparedToolCall]] = []
        results_by_index: dict[int, ExecutedToolResult] = {}
        for i, tc in enumerate(tool_calls):
            yield ToolExecutionStartEvent(tool_call=tc)
            try:
                prepared_or_result = await _execute_tool_with_hooks(
                    registry=registry,
                    tool_call=tc,
                    messages=list(messages),
                    before_tool_call=before_fn,
                    after_tool_call=after_fn,
                    signal=signal,
                    permission_policy=permission_policy,
                    permission_audit_log=permission_audit_log,
                    tool_approval_handler=tool_approval_handler,
                    prepare_only=True,
                )
            except Exception as exc:
                executed = failed_execution(i, tc, exc)
                results_by_index[i] = executed
                yield ToolExecutionEndEvent(tool_call=tc, result=executed.result)
                continue

            if isinstance(prepared_or_result, _PreparedToolCall):
                prepared_calls.append((i, tc, prepared_or_result))
            else:
                executed = executed_result(i, tc, prepared_or_result)
                results_by_index[i] = executed
                yield ToolExecutionEndEvent(tool_call=tc, result=executed.result)

            if signal is not None and signal.is_set():
                break

        parallel_queue: asyncio.Queue[_ToolUpdateItem | _ToolDoneItem] = asyncio.Queue()
        tasks = [
            asyncio.create_task(produce(i, tc, parallel_queue, prepared))
            for i, tc, prepared in prepared_calls
        ]
        pending_count = len(prepared_calls)
        completed_count = 0
        while completed_count < pending_count:
            item = await parallel_queue.get()
            if isinstance(item, _ToolUpdateItem):
                yield ToolExecutionUpdateEvent(
                    tool_call=item.tool_call,
                    partial_result=item.result,
                )
                continue
            completed_count += 1
            if item.executed is not None:
                executed = item.executed
            else:
                executed = failed_execution(
                    item.index,
                    tool_calls[item.index],
                    item.error or RuntimeError("tool failed"),
                )
            results_by_index[executed.index] = executed
            yield ToolExecutionEndEvent(
                tool_call=executed.tool_call,
                result=executed.result,
            )
        await asyncio.gather(*tasks)
        # 按原 index 排序——保证 new_messages 顺序稳定
        ordered = [results_by_index[i] for i in sorted(results_by_index)]
        for executed in ordered:
            yield MessageStartEvent(message=executed.message)
            yield MessageEndEvent(message=executed.message)

    yield _BatchDone(ordered=ordered)


# ============================================================================
# 主入口：run_event_loop
# ============================================================================


async def run_event_loop(
    *,
    system_prompt: str,
    user_text: str | None = None,
    prompt_messages: Sequence[AgentMessage] | None = None,
    initial_messages: list[AgentMessage] | None = None,
    client: ModelClient,
    tools: ToolRegistry | Iterable[AgentTool] | None = None,
    transform_context_fn: TransformContextFn | None = None,
    convert_to_llm_fn: ConvertToLLMFn | None = None,
    before_tool_call: BeforeToolCallFn | None = None,
    after_tool_call: AfterToolCallFn | None = None,
    tool_execution: ToolExecutionMode = "parallel",
    signal: asyncio.Event | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    tool_approval_handler: ToolApprovalHandler | None = None,
    should_stop_after_turn: ShouldStopAfterTurnFn | None = None,
    prepare_next_turn: PrepareNextTurnFn | None = None,
    before_model_call: BeforeModelCallFn | None = None,
    get_steering_messages: PendingMessagesFn | None = None,
    get_follow_up_messages: PendingMessagesFn | None = None,
    thinking_level: str = "off",
    max_turns: int = 50,
) -> AsyncIterator[AgentEvent]:
    """事件驱动的 agent loop（Step 9：abort signal；Step 8：initial_messages；Step 7：batch）。

    Step 9 新增参数：
      signal —— asyncio.Event | None；外部 set 后，loop 在最近的检查点生成
                aborted AssistantMessage 并正常收敛（turn_end + agent_end）

    Step 18 新增参数：
      permission_policy      —— ToolPermissionPolicy | None；None 时跳过权限检查，
                                 保持旧行为（向后兼容）
      permission_audit_log   —— InMemoryToolPermissionAuditLog | None；每次 check
                                 都会 append 一条记录（policy 为 None 时不写）
      tool_approval_handler  —— 可选一次性审批回调；仅在 require_approval 时调用，
                                 None 时保持 ToolApprovalRequired 安全错误

    Bug-fix 新增参数：
      max_turns              —— 单次 run_event_loop 的最大 LLM 调用轮数（默认 50）；
                                 达到上限时生成 stop_reason="error" AssistantMessage
                                 收敛（error_message="max_turns exceeded"），
                                 防止模型持续 yield tool_call 时死循环。

    工具批次调度：
      tool_execution         —— 全局模式（默认 parallel）；全局 sequential
                                 或批次内任一工具 execution_mode=sequential 时，
                                 整批按 sequential 执行。

    Agent 控制队列：
      get_steering_messages  —— 每个 assistant turn 后优先拉取；首次模型调用前
                                 也拉取一次。
      get_follow_up_messages —— 仅在没有后续 tool turn / steering 时拉取。

    Step 9 abort 检查点：
      1. while iter 开始（before transform/stream）—— 立即发 aborted assistant + 收敛
      2. client.stream 迭代中收到 ErrorEvent 且 signal set —— 标 aborted
      3. tool batch 之前（signal set 时跳过 batch，发 aborted assistant + 收敛）
      4. sequential 模式下每个工具执行前（已由 _execute_tool_batch 处理）

    Step 8 / 7 / 6 / 5 / 4 / 3 / 2 / 1 行为全部保留。
    """
    tool_execution = _validate_tool_execution_mode(tool_execution)
    current_thinking_level = _validate_thinking_level(thinking_level)
    if user_text is not None and prompt_messages:
        raise ValueError("run_event_loop: user_text and prompt_messages are mutually exclusive")
    if user_text is None and not prompt_messages and not initial_messages:
        raise ValueError(
            "run_event_loop: 必须提供 user_text、prompt_messages 或 initial_messages 至少一项"
        )
    if max_turns < 1:
        raise ValueError(f"run_event_loop: max_turns 必须 >= 1，实际 {max_turns}")

    current_system_prompt = system_prompt
    current_client = client
    registry = _to_registry(tools)
    tool_defs = registry.definitions()
    before_fn = before_tool_call or default_before_tool_call
    after_fn = after_tool_call or default_after_tool_call

    new_messages: list[AgentMessage] = (
        list(initial_messages) if initial_messages else []
    )
    run_messages: list[AgentMessage] = []

    prompts = list(prompt_messages) if prompt_messages else []
    if user_text is not None:
        prompts.append(UserMessage(content=[TextContent(text=user_text)]))

    yield AgentStartEvent()
    yield TurnStartEvent()

    for prompt_message in prompts:
        yield MessageStartEvent(message=prompt_message)
        yield MessageEndEvent(message=prompt_message)
        new_messages.append(prompt_message)
        run_messages.append(prompt_message)

    # 与 pi-agent 一致：在第一次模型调用前先检查 steering。这样在请求真正
    # 开始流式响应前到达的 steer 不会被无故延迟一个 turn。
    pending_messages = await _poll_pending_messages(get_steering_messages)

    # P0：一个 turn 严格对应一次 LLM 调用及该调用产生的整批工具。
    turn_count = 1

    def make_turn_control(
        message: AssistantMessage,
        tool_results: list[ToolResultMessage],
    ) -> TurnControlContext:
        """Build the immutable snapshot seen by turn-boundary callbacks."""
        return TurnControlContext(
            turn_index=turn_count,
            messages=tuple(new_messages),
            new_messages=tuple(run_messages),
            message=message,
            tool_results=tuple(tool_results),
            signal=signal,
            system_prompt=current_system_prompt,
            client=current_client,
            tools=tuple(registry),
            thinking_level=current_thinking_level,
        )

    def make_agent_end() -> AgentEndEvent:
        return AgentEndEvent(
            messages=list(new_messages),
            new_messages=list(run_messages),
        )

    async def should_stop_turn(control: TurnControlContext) -> bool:
        """Ask for graceful termination before doing next-turn preparation."""
        return bool(
            should_stop_after_turn is not None
            and await _await_maybe(should_stop_after_turn(control))
        )

    async def prepare_for_next_turn(control: TurnControlContext) -> None:
        """Apply compatible runtime replacements before an actual next turn."""
        nonlocal new_messages, current_system_prompt, current_client
        nonlocal registry, tool_defs, current_thinking_level
        if prepare_next_turn is not None:
            prepared = await _await_maybe(prepare_next_turn(control))
            if isinstance(prepared, AgentLoopTurnUpdate):
                if prepared.messages is not None:
                    new_messages = list(prepared.messages)
                if prepared.system_prompt is not None:
                    current_system_prompt = prepared.system_prompt
                if prepared.client is not None:
                    current_client = prepared.client
                if prepared.tools is not None:
                    registry = _to_registry(prepared.tools)
                    tool_defs = registry.definitions()
                if prepared.thinking_level is not None:
                    current_thinking_level = _validate_thinking_level(
                        prepared.thinking_level
                    )
            elif prepared is not None:
                new_messages = list(prepared)

    while True:
        current_tool_results: list[ToolResultMessage] = []
        # —— Step 9 检查点 1：while iter 开始 ——
        if signal is not None and signal.is_set():
            async for ev in _finalize_abort(
                client=current_client,
                new_messages=new_messages,
                run_messages=run_messages,
                tool_results=current_tool_results,
            ):
                yield ev
            return

        # steering / follow-up 都作为独立消息在下一次模型调用前注入，并产生
        # 完整 message_start/end 事件，保证 transcript、snapshot 与 UI 一致。
        for pending_message in pending_messages:
            yield MessageStartEvent(message=pending_message)
            yield MessageEndEvent(message=pending_message)
            new_messages.append(pending_message)
            run_messages.append(pending_message)
        pending_messages = []

        # —— Context 转换 ——
        raw_context: list[AgentMessage] = list(new_messages)
        transform = transform_context_fn or default_transform_context
        transformed = await apply_transform_context(transform, raw_context, signal)
        converter = convert_to_llm_fn or convert_to_llm
        llm_messages = list(await _await_maybe(converter(transformed)))

        if before_model_call is not None:
            admission_error: str | None = None
            try:
                raw_decision = await _await_maybe(
                    before_model_call(
                        ModelCallContext(
                            turn_index=turn_count,
                            system_prompt=current_system_prompt,
                            messages=tuple(llm_messages),
                            tools=tuple(tool_defs),
                            client=current_client,
                            signal=signal,
                        )
                    )
                )
                if isinstance(raw_decision, ModelCallDecision):
                    if not raw_decision.allow:
                        admission_error = (
                            raw_decision.error_message
                            or "model call was blocked by the admission policy"
                        )
                elif raw_decision is False:
                    admission_error = "model call was blocked by the admission policy"
            except Exception as exc:
                admission_error = f"before_model_call failed: {type(exc).__name__}"

            if admission_error is not None:
                blocked = AssistantMessage(
                    content=[],
                    api=current_client.api_id,
                    provider=current_client.provider_id,
                    model=getattr(current_client, "model", "unknown"),
                    stop_reason="error",
                    error_message=admission_error,
                    generation_metrics=GenerationMetrics(
                        latency_ms=0,
                        usage_available=False,
                    ),
                )
                yield MessageStartEvent(message=blocked)
                yield MessageEndEvent(message=blocked)
                new_messages.append(blocked)
                run_messages.append(blocked)
                yield TurnEndEvent(message=blocked, tool_results=[])
                yield make_agent_end()
                return

        # —— 调 LLM ——
        content_by_index: dict[int, TextContent | ThinkingContent | ToolCall] = {}
        claimed_indices: set[int] = set()
        open_text_indices: set[int] = set()
        open_thinking_indices: set[int] = set()
        pending_tool_calls: dict[int, dict[str, Any]] = {}
        legacy_text_index: int | None = None
        legacy_thinking_index: int | None = None
        next_content_index = 0
        tool_calls: list[ToolCall] = []
        stop_reason: str = "stop"
        error_message: str | None = None
        final_usage = Usage()
        generation_started = time.perf_counter()
        first_response_at: float | None = None
        generation_metrics: GenerationMetrics | None = None

        def claim_content_index(  # noqa: B023
            index: int | None = None,
            claimed: set[int] = claimed_indices,
        ) -> int:
            nonlocal next_content_index
            if index is None:
                while next_content_index in claimed:
                    next_content_index += 1
                index = next_content_index
            claimed.add(index)
            next_content_index = max(next_content_index, index + 1)
            return index

        def make_assistant() -> AssistantMessage:  # noqa: B023
            content = [
                content_by_index[index]  # noqa: B023
                for index in sorted(content_by_index)  # noqa: B023
            ]
            return AssistantMessage(
                content=content,
                api=current_client.api_id,
                provider=current_client.provider_id,
                model=getattr(current_client, "model", "unknown"),
                stop_reason=stop_reason,  # noqa: B023
                error_message=error_message,  # noqa: B023
                usage=final_usage,  # noqa: B023
                generation_metrics=generation_metrics,  # noqa: B023
            )

        yield MessageStartEvent(message=make_assistant())

        async for s_ev in current_client.stream(
            system_prompt=current_system_prompt,
            messages=llm_messages,
            tools=tool_defs if tool_defs else None,
            signal=signal,
            # Match pi-agent: "off" means no reasoning option at the Agent
            # boundary. Provider adapters may still apply an explicit off
            # value when their own capability metadata requires one.
            thinking_level=(
                None if current_thinking_level == "off" else current_thinking_level
            ),
        ):
            if isinstance(s_ev, TextStartEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                content_by_index[index] = TextContent(text="")
                open_text_indices.add(index)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, TextDeltaEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                text_index = s_ev.content_index
                if text_index is None:
                    if legacy_text_index is None:
                        legacy_text_index = claim_content_index()
                    text_index = legacy_text_index
                    s_ev = s_ev.model_copy(update={"content_index": text_index})
                else:
                    claim_content_index(text_index)
                if text_index not in open_text_indices:
                    content_by_index[text_index] = TextContent(text="")
                    open_text_indices.add(text_index)
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=TextStartEvent(content_index=text_index),
                    )
                current_text = content_by_index.get(text_index)
                prefix = current_text.text if isinstance(current_text, TextContent) else ""
                content_by_index[text_index] = TextContent(text=prefix + s_ev.delta)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, TextEndEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                if index not in open_text_indices:
                    content_by_index[index] = TextContent(text="")
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=TextStartEvent(content_index=index),
                    )
                content_by_index[index] = TextContent(text=s_ev.content)
                open_text_indices.discard(index)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ThinkingStartEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                content_by_index[index] = ThinkingContent(
                    thinking="",
                    thinking_signature=s_ev.thinking_signature,
                    redacted=s_ev.redacted,
                )
                open_thinking_indices.add(index)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ThinkingDeltaEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                thinking_index = s_ev.content_index
                if thinking_index is None:
                    if legacy_thinking_index is None:
                        legacy_thinking_index = claim_content_index()
                    thinking_index = legacy_thinking_index
                    s_ev = s_ev.model_copy(update={"content_index": thinking_index})
                else:
                    claim_content_index(thinking_index)
                if thinking_index not in open_thinking_indices:
                    content_by_index[thinking_index] = ThinkingContent(
                        thinking="",
                        thinking_signature=s_ev.thinking_signature,
                        redacted=s_ev.redacted,
                    )
                    open_thinking_indices.add(thinking_index)
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=ThinkingStartEvent(
                            content_index=thinking_index,
                            thinking_signature=s_ev.thinking_signature,
                            redacted=s_ev.redacted,
                        ),
                    )
                current_thinking = content_by_index.get(thinking_index)
                if isinstance(current_thinking, ThinkingContent):
                    thinking = current_thinking.thinking + s_ev.delta
                    signature = (
                        s_ev.thinking_signature
                        if s_ev.thinking_signature is not None
                        else current_thinking.thinking_signature
                    )
                    redacted = current_thinking.redacted or s_ev.redacted
                else:
                    thinking = s_ev.delta
                    signature = s_ev.thinking_signature
                    redacted = s_ev.redacted
                content_by_index[thinking_index] = ThinkingContent(
                    thinking=thinking,
                    thinking_signature=signature,
                    redacted=redacted,
                )
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ThinkingEndEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                current_thinking = content_by_index.get(index)
                if index not in open_thinking_indices:
                    content_by_index[index] = ThinkingContent(thinking="")
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=ThinkingStartEvent(
                            content_index=index,
                            thinking_signature=s_ev.thinking_signature,
                            redacted=s_ev.redacted,
                        ),
                    )
                content_by_index[index] = ThinkingContent(
                    thinking=s_ev.content,
                    thinking_signature=(
                        s_ev.thinking_signature
                        if s_ev.thinking_signature is not None
                        else (
                            current_thinking.thinking_signature
                            if isinstance(current_thinking, ThinkingContent)
                            else None
                        )
                    ),
                    redacted=s_ev.redacted
                    or (
                        current_thinking.redacted
                        if isinstance(current_thinking, ThinkingContent)
                        else False
                    ),
                )
                open_thinking_indices.discard(index)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ToolCallStartEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                pending_tool_calls[index] = {
                    "id": s_ev.tool_call_id,
                    "name": s_ev.name,
                    "argument_parts": [],
                }
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ToolCallDeltaEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                if index not in pending_tool_calls:
                    pending_tool_calls[index] = {
                        "id": s_ev.tool_call_id,
                        "name": s_ev.name,
                        "argument_parts": [],
                    }
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=ToolCallStartEvent(
                            content_index=index,
                            tool_call_id=s_ev.tool_call_id,
                            name=s_ev.name,
                        ),
                    )
                pending = pending_tool_calls[index]
                if s_ev.tool_call_id is not None:
                    pending["id"] = s_ev.tool_call_id
                if s_ev.name is not None:
                    pending["name"] = s_ev.name
                pending["argument_parts"].append(s_ev.delta)
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ToolCallEndEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index(s_ev.content_index)
                if index not in pending_tool_calls:
                    yield MessageUpdateEvent(
                        message=make_assistant(),
                        assistant_message_event=ToolCallStartEvent(
                            content_index=index,
                            tool_call_id=s_ev.tool_call.id,
                            name=s_ev.tool_call.name,
                        ),
                    )
                pending_tool_calls.pop(index, None)
                content_by_index[index] = s_ev.tool_call
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ToolCallEvent):
                if first_response_at is None:
                    first_response_at = time.perf_counter()
                index = claim_content_index()
                start_event = ToolCallStartEvent(
                    content_index=index,
                    tool_call_id=s_ev.tool_call.id,
                    name=s_ev.tool_call.name,
                )
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=start_event,
                )
                content_by_index[index] = s_ev.tool_call
                yield MessageUpdateEvent(
                    message=make_assistant(),
                    assistant_message_event=ToolCallEndEvent(
                        content_index=index,
                        tool_call=s_ev.tool_call,
                    ),
                )
            elif isinstance(s_ev, DoneEvent):
                stop_reason = s_ev.stop_reason
                final_usage = s_ev.usage
                # Step 21：adapter 主动发 DoneEvent(stop_reason="aborted") 时
                # 也走 abort 路径——抛弃 partial text / tool_calls，记 error_message
                if stop_reason == "aborted":
                    error_message = "aborted"
                    content_by_index.clear()
                    pending_tool_calls.clear()
                    open_text_indices.clear()
                    open_thinking_indices.clear()
                else:
                    for index in sorted(open_text_indices):
                        block = content_by_index.get(index)
                        content = block.text if isinstance(block, TextContent) else ""
                        yield MessageUpdateEvent(
                            message=make_assistant(),
                            assistant_message_event=TextEndEvent(
                                content_index=index,
                                content=content,
                            ),
                        )
                    open_text_indices.clear()
                    for index in sorted(open_thinking_indices):
                        block = content_by_index.get(index)
                        if isinstance(block, ThinkingContent):
                            content = block.thinking
                            signature = block.thinking_signature
                            redacted = block.redacted
                        else:
                            content = ""
                            signature = None
                            redacted = False
                        yield MessageUpdateEvent(
                            message=make_assistant(),
                            assistant_message_event=ThinkingEndEvent(
                                content_index=index,
                                content=content,
                                thinking_signature=signature,
                                redacted=redacted,
                            ),
                        )
                    open_thinking_indices.clear()
                    if pending_tool_calls:
                        stop_reason = "error"
                        error_message = "provider ended with incomplete tool call"
                break
            elif isinstance(s_ev, ErrorEvent):
                # Step 9：若 signal set，标记为 aborted；否则 error
                if signal is not None and signal.is_set():
                    stop_reason = "aborted"
                    error_message = "aborted"
                    # 抛弃已累积的 partial text / tool_calls——aborted assistant 内容为空
                    content_by_index.clear()
                    pending_tool_calls.clear()
                    open_text_indices.clear()
                    open_thinking_indices.clear()
                else:
                    stop_reason = "error"
                    error_message = s_ev.message
                break

        generation_ended = time.perf_counter()
        generation_metrics = GenerationMetrics(
            latency_ms=max(0, round((generation_ended - generation_started) * 1000)),
            time_to_first_token_ms=(
                max(0, round((first_response_at - generation_started) * 1000))
                if first_response_at is not None
                else None
            ),
            usage_available=bool(
                final_usage.input or final_usage.output or final_usage.total_tokens
            ),
        )

        # Step 9：stream 结束后再检一次 signal（覆盖 signal 在 stream 末尾被 set 的情况）
        if signal is not None and signal.is_set() and stop_reason == "stop":
            stop_reason = "aborted"
            error_message = "aborted"
            content_by_index.clear()
            pending_tool_calls.clear()
            open_text_indices.clear()
            open_thinking_indices.clear()
            tool_calls.clear()

        tool_calls = [
            content
            for _, content in sorted(content_by_index.items())
            if isinstance(content, ToolCall)
        ]
        assistant = make_assistant()
        yield MessageEndEvent(message=assistant)
        assistant_message_index = len(new_messages)
        assistant_run_index = len(run_messages)
        new_messages.append(assistant)
        run_messages.append(assistant)

        # 错误或被中断：直接结束
        if assistant.stop_reason in ("error", "aborted"):
            yield TurnEndEvent(message=assistant, tool_results=current_tool_results)
            yield make_agent_end()
            return

        # 没 ToolCall：本轮 turn 自然结束
        if not tool_calls:
            yield TurnEndEvent(message=assistant, tool_results=current_tool_results)
            turn_control = make_turn_control(
                assistant,
                current_tool_results,
            )
            stop_requested = await should_stop_turn(turn_control)
            if stop_requested or turn_count >= max_turns:
                yield make_agent_end()
                return

            # steering 始终优先；只有 Agent 原本将结束时才消费 follow-up。
            pending_messages = await _poll_pending_messages(
                get_steering_messages,
            )
            if not pending_messages:
                pending_messages = await _poll_pending_messages(
                    get_follow_up_messages,
                )
            if not pending_messages:
                yield make_agent_end()
                return

            await prepare_for_next_turn(turn_control)
            turn_count += 1
            yield TurnStartEvent()
            continue

        # Provider 因长度限制截断时，tool call 可能是不完整 JSON。绝不执行这些
        # 调用；为每个 call 生成可回喂 LLM 的安全错误结果。
        if assistant.stop_reason == "length":
            ordered = []
            for index, tc in enumerate(tool_calls):
                yield ToolExecutionStartEvent(tool_call=tc)
                result = ToolResult(
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=[
                        TextContent(
                            text=(
                                "Tool call was not executed because the model response "
                                "ended at the length limit. Retry with complete arguments."
                            )
                        )
                    ],
                    is_error=True,
                    details={
                        "error_type": "IncompleteToolCall",
                        "stop_reason": "length",
                    },
                )
                message = _tool_result_message(result)
                ordered.append(
                    ExecutedToolResult(
                        index=index,
                        tool_call=tc,
                        result=result,
                        message=message,
                    )
                )
                yield ToolExecutionEndEvent(tool_call=tc, result=result)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
        else:
            # —— Step 9 检查点 3：tool batch 之前 ——
            # abort 来了：跳过 batch，发 aborted assistant + 收敛
            if signal is not None and signal.is_set():
                async for ev in _finalize_abort(
                    client=current_client,
                    new_messages=new_messages,
                    run_messages=run_messages,
                    tool_results=current_tool_results,
                ):
                    yield ev
                return

            # —— Step 7：多工具 batch 执行 ——
            batch_messages_snapshot = list(new_messages)
            ordered = []
            async for ev in _execute_tool_batch(
                registry=registry,
                tool_calls=tool_calls,
                messages=batch_messages_snapshot,
                before_fn=before_fn,
                after_fn=after_fn,
                tool_execution=tool_execution,
                signal=signal,
                permission_policy=permission_policy,
                permission_audit_log=permission_audit_log,
                tool_approval_handler=tool_approval_handler,
            ):
                if isinstance(ev, _BatchDone):
                    ordered = ev.ordered
                    break
                yield ev

        # 按原序追加 ToolResultMessage
        for executed in ordered:
            new_messages.append(executed.message)
            run_messages.append(executed.message)
            current_tool_results.append(executed.message)

        terminate = bool(ordered) and all(ex.result.terminate for ex in ordered)
        maxed = turn_count >= max_turns
        turn_message = assistant
        if maxed and not terminate:
            # 不创建无 LLM 调用的“伪 turn”；把本次真实调用标为安全上限终止。
            turn_message = assistant.model_copy(
                update={
                    "stop_reason": "error",
                    "error_message": f"max_turns exceeded ({max_turns})",
                }
            )
            # TurnEndEvent、AgentEndEvent 与持久化 transcript 必须引用同一个
            # 终态 assistant；不能只修改临时的 turn_message。
            new_messages[assistant_message_index] = turn_message
            run_messages[assistant_run_index] = turn_message

        yield TurnEndEvent(message=turn_message, tool_results=current_tool_results)

        turn_control = make_turn_control(
            turn_message,
            current_tool_results,
        )
        stop_requested = await should_stop_turn(turn_control)

        if stop_requested or maxed or (signal is not None and signal.is_set()):
            yield make_agent_end()
            return

        pending_messages = await _poll_pending_messages(
            get_steering_messages,
        )
        if terminate and not pending_messages:
            # terminate 表示工具链原本将结束，因此此时才轮到 follow-up。
            pending_messages = await _poll_pending_messages(
                get_follow_up_messages,
            )
            if not pending_messages:
                yield make_agent_end()
                return

        await prepare_for_next_turn(turn_control)
        if not pending_messages:
            # Preparation may be long-running. Pick up steering that arrived
            # while it ran without double-draining one-at-a-time queues.
            pending_messages = await _poll_pending_messages(
                get_steering_messages,
            )
        turn_count += 1
        yield TurnStartEvent()
        # 继续下一次 LLM 调用。


async def _finalize_abort(
    *,
    client: ModelClient,
    new_messages: list[AgentMessage],
    run_messages: list[AgentMessage],
    tool_results: list[ToolResultMessage],
) -> AsyncIterator[AgentEvent]:
    """Step 9 abort 终化——生成空的 aborted assistant 并正常收敛。

    用于检查点 1（while iter 开始）与检查点 3（tool batch 之前）：
    这两个位置还没有进行中的 assistant message，需要现造一个。
    """
    aborted = AssistantMessage(
        content=[],
        api=client.api_id,
        provider=client.provider_id,
        model=getattr(client, "model", "unknown"),
        stop_reason="aborted",
        error_message="aborted",
    )
    yield MessageStartEvent(message=aborted)
    yield MessageEndEvent(message=aborted)
    new_messages.append(aborted)
    run_messages.append(aborted)
    yield TurnEndEvent(message=aborted, tool_results=tool_results)
    yield AgentEndEvent(
        messages=list(new_messages),
        new_messages=list(run_messages),
    )


async def run_min_loop(
    *,
    system_prompt: str,
    user_text: str | None = None,
    prompt_messages: Sequence[AgentMessage] | None = None,
    initial_messages: list[AgentMessage] | None = None,
    client: ModelClient,
    tools: ToolRegistry | Iterable[AgentTool] | None = None,
    transform_context_fn: TransformContextFn | None = None,
    convert_to_llm_fn: ConvertToLLMFn | None = None,
    before_tool_call: BeforeToolCallFn | None = None,
    after_tool_call: AfterToolCallFn | None = None,
    tool_execution: ToolExecutionMode = "parallel",
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    tool_approval_handler: ToolApprovalHandler | None = None,
    should_stop_after_turn: ShouldStopAfterTurnFn | None = None,
    prepare_next_turn: PrepareNextTurnFn | None = None,
    before_model_call: BeforeModelCallFn | None = None,
    get_steering_messages: PendingMessagesFn | None = None,
    get_follow_up_messages: PendingMessagesFn | None = None,
    thinking_level: str = "off",
    max_turns: int = 50,
) -> list[AgentMessage]:
    """Step 1+ 便捷封装：跑 event loop，返回所有新 messages。

    Step 8 起支持 continue_ 用法（user_text=None + initial_messages=[...]）。
    Step 18 起支持 permission_policy / permission_audit_log 透传。
    """
    async for ev in run_event_loop(
        system_prompt=system_prompt,
        user_text=user_text,
        prompt_messages=prompt_messages,
        initial_messages=initial_messages,
        client=client,
        tools=tools,
        transform_context_fn=transform_context_fn,
        convert_to_llm_fn=convert_to_llm_fn,
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
        tool_execution=tool_execution,
        permission_policy=permission_policy,
        permission_audit_log=permission_audit_log,
        tool_approval_handler=tool_approval_handler,
        should_stop_after_turn=should_stop_after_turn,
        prepare_next_turn=prepare_next_turn,
        before_model_call=before_model_call,
        get_steering_messages=get_steering_messages,
        get_follow_up_messages=get_follow_up_messages,
        thinking_level=thinking_level,
        max_turns=max_turns,
    ):
        if isinstance(ev, AgentEndEvent):
            return ev.messages
    return []


__all__ = [
    "run_event_loop",
    "run_min_loop",
    "ExecutedToolResult",
    "TurnControlContext",
    "AgentLoopTurnUpdate",
    "ShouldStopAfterTurnFn",
    "PrepareNextTurnFn",
    "ModelCallContext",
    "ModelCallDecision",
    "BeforeModelCallFn",
    "PendingMessagesFn",
]

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

事件顺序契约（Step 7）：
- sequential 模式：tool_execution_start → message_start/end → tool_execution_end
  按 ToolCall 原始顺序逐个发出
- parallel 模式：
  - tool_execution_start：按原始 ToolCall 顺序批量发出
  - 工具执行：asyncio.gather 并发
  - message_start/end + tool_execution_end：按**实际完成顺序**发出
  - ToolResultMessage 追加到 new_messages：**按原始 ToolCall 顺序**（index 排序）

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
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .context import (
    TransformContextFn,
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
    TurnEndEvent,
    TurnStartEvent,
)
from .hooks import (
    AfterToolCallContext,
    AfterToolCallFn,
    BeforeToolCallContext,
    BeforeToolCallFn,
    default_after_tool_call,
    default_before_tool_call,
)
from .messages import (
    AgentMessage,
    AssistantMessage,
    Message,
    TextContent,
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
    ToolCallEvent,
)
from .policy import (
    InMemoryToolPermissionAuditLog,
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
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
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


@dataclass
class _BatchDone:
    """`_execute_tool_batch` 的结束哨兵——携带按源序排好的结果。

    非公开 AgentEvent；只在 loop.py 内部用作 async generator 的"返回值"。
    """
    ordered: list[ExecutedToolResult]


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
) -> bool:
    """batch 中任一**已注册**工具声明 sequential → 整批 sequential。

    ToolNotFoundError 的工具不参与判断（错误结果不影响模式选择）。
    """
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
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
) -> ToolResult:
    """执行单个工具，含 before / after hooks；所有失败路径都包成 ToolResult。

    顺序：
      1. registry.get(name) → tool 或 None（ToolNotFoundError）
      2. before_tool_call(BeforeToolCallContext{tool_call, tool, messages})
         - 抛异常    → error ToolResult（details.hook="before_tool_call"）
         - allow=False → error ToolResult（details.blocked_by="before_tool_call"）
         - tool_call 不为 None → 用新 tool_call 继续
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

    # Step 9 检查点 1：before_tool_call 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True, details={"error_type": "AbortSignal"},
        )

    # 2. before_tool_call
    try:
        before_ctx = BeforeToolCallContext(
            tool_call=tool_call, tool=tool, messages=list(messages),
        )
        before_result = await before_tool_call(before_ctx)
    except Exception as e:
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=[TextContent(
                text=f"before_tool_call failed: {type(e).__name__}: {e}",
            )],
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
            details=merged_details,
        )

    # 4. 使用修改后的 tool_call（若有）
    effective_tool_call = before_result.tool_call or tool_call

    # 4.1 如果 hook 修改了 tool_call.name，需要重新查 registry
    if (
        before_result.tool_call is not None
        and effective_tool_call.name != tool_call.name
    ):
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
                permission_audit_log.append(ToolPermissionAuditRecord(
                    tool_call_id=effective_tool_call.id,
                    tool_name=effective_tool_call.name,
                    decision=decision.decision,
                    policy_name=decision.policy_name,
                    reason=decision.reason,
                    metadata=dict(decision.metadata),
                ))

            if decision.denied:
                return ToolResult(
                    tool_call_id=effective_tool_call.id,
                    name=effective_tool_call.name,
                    content=[TextContent(
                        text=f"Tool call denied by policy: {decision.reason or 'no reason'}",
                    )],
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
                # Step 18 没有人工审批 UI——按 deny 行为处理（error_type 区分）
                return ToolResult(
                    tool_call_id=effective_tool_call.id,
                    name=effective_tool_call.name,
                    content=[TextContent(
                        text=(
                            "Tool call requires approval (Step 18 has no approval UI): "
                            f"{decision.reason or 'no reason'}"
                        ),
                    )],
                    is_error=True,
                    details={
                        "error_type": "ToolApprovalRequired",
                        "policy": {
                            "decision": "require_approval",
                            "policy_name": decision.policy_name,
                            "reason": decision.reason,
                            "metadata": dict(decision.metadata),
                        },
                    },
                )
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
                content=[TextContent(
                    text=(
                        f"Permission policy error: "
                        f"{type(policy_exc).__name__}: {policy_exc}"
                    ),
                )],
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

    # Step 9 检查点 2：tool.execute 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=effective_tool_call.id, name=effective_tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True, details={"error_type": "AbortSignal"},
        )

    # 6. 执行
    try:
        result = await tool.execute(
            effective_tool_call.id, effective_tool_call.arguments,
        )
    except Exception as e:
        result = ToolResult(
            tool_call_id=effective_tool_call.id,
            name=effective_tool_call.name,
            content=[TextContent(text=f"{type(e).__name__}: {e}")],
            is_error=True,
            details={"error_type": type(e).__name__},
        )

    # Step 9 检查点 3：tool.execute 之后
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=effective_tool_call.id, name=effective_tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True, details={"error_type": "AbortSignal"},
        )

    # Step 9 检查点 4：after_tool_call 之前
    if signal is not None and signal.is_set():
        return ToolResult(
            tool_call_id=effective_tool_call.id, name=effective_tool_call.name,
            content=[TextContent(text="aborted")],
            is_error=True, details={"error_type": "AbortSignal"},
        )

    # 7. after_tool_call
    try:
        after_ctx = AfterToolCallContext(
            tool_call=effective_tool_call,
            tool=tool,
            result=result,
            messages=list(messages),
        )
        final_result = await after_tool_call(after_ctx)
    except Exception as e:
        return ToolResult(
            tool_call_id=effective_tool_call.id,
            name=effective_tool_call.name,
            content=[TextContent(
                text=f"after_tool_call failed: {type(e).__name__}: {e}",
            )],
            is_error=True,
            details={
                "hook": "after_tool_call",
                "error_type": type(e).__name__,
            },
        )

    return final_result


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
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
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
        result = await _execute_tool_with_hooks(
            registry=registry,
            tool_call=tool_call,
            messages=messages,
            before_tool_call=before_fn,
            after_tool_call=after_fn,
            signal=signal,
            permission_policy=permission_policy,
            permission_audit_log=permission_audit_log,
        )
    msg = ToolResultMessage(
        tool_call_id=result.tool_call_id,
        name=result.name,
        content=result.content,
        is_error=result.is_error,
        terminate=result.terminate,
        details=result.details,
    )
    return ExecutedToolResult(
        index=index, tool_call=tool_call, result=result, message=msg,
    )


async def _execute_tool_batch(
    *,
    registry: ToolRegistry,
    tool_calls: list[ToolCall],
    messages: Sequence[AgentMessage],
    before_fn: BeforeToolCallFn,
    after_fn: AfterToolCallFn,
    signal: asyncio.Event | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
) -> AsyncIterator[Any]:
    """执行一批工具，按模式 yield 事件；末尾 yield `_BatchDone`。

    事件契约（两种模式的事件**形状不同**）：

      sequential 模式——每个工具形成完整闭环再动下一个：
        for each tc:
          if signal set: break       ← Step 9 加
          tool_execution_start(tc)
          _exec_one_tool
          message_start(tr_msg) / message_end(tr_msg)
          tool_execution_end(tc, result)

      parallel 模式——先批量声明意图，再按完成序上报进度：
        1. if signal set: 立即返回（不发任何事件） ← Step 9 加
        2. 按原序批量 yield tool_execution_start
        3. asyncio.ensure_future 创建并发 task
        4. asyncio.as_completed 按完成序迭代
        5. 每完成一个：message_start/end + tool_execution_end

    末尾必 yield `_BatchDone(ordered=[源序 ExecutedToolResult])`。

    signal 行为：
      - sequential：在每个工具开始前检查；set 后跳出循环，**不再执行后续工具**
      - parallel：在批量 start 前检查一次；set 后直接返回空 ordered（一旦 task 已发出，
        无法收回——它们会跑完；但 run_event_loop 上层会在 batch 后检测 signal 并终止）
    """
    is_sequential = _should_run_sequential(registry, tool_calls)

    if is_sequential:
        ordered: list[ExecutedToolResult] = []
        for i, tc in enumerate(tool_calls):
            # Step 9：sequential 模式下逐工具前检查 signal
            if signal is not None and signal.is_set():
                break
            yield ToolExecutionStartEvent(tool_call=tc)
            executed = await _exec_one_tool(
                index=i, tool_call=tc,
                registry=registry, messages=list(messages),
                before_fn=before_fn, after_fn=after_fn,
                signal=signal,
                permission_policy=permission_policy,
                permission_audit_log=permission_audit_log,
            )
            ordered.append(executed)
            yield MessageStartEvent(message=executed.message)
            yield MessageEndEvent(message=executed.message)
            yield ToolExecutionEndEvent(tool_call=tc, result=executed.result)
    else:
        # Step 9：parallel 模式 batch 开始前检查 signal——已 abort 就不发 start
        if signal is not None and signal.is_set():
            yield _BatchDone(ordered=[])
            return

        for tc in tool_calls:
            yield ToolExecutionStartEvent(tool_call=tc)

        tasks = [
            asyncio.ensure_future(_exec_one_tool(
                index=i, tool_call=tc,
                registry=registry, messages=list(messages),
                before_fn=before_fn, after_fn=after_fn,
                signal=signal,
                permission_policy=permission_policy,
                permission_audit_log=permission_audit_log,
            ))
            for i, tc in enumerate(tool_calls)
        ]
        results_by_index: dict[int, ExecutedToolResult] = {}
        for done_coro in asyncio.as_completed(tasks):
            executed = await done_coro
            results_by_index[executed.index] = executed
            yield MessageStartEvent(message=executed.message)
            yield MessageEndEvent(message=executed.message)
            yield ToolExecutionEndEvent(
                tool_call=executed.tool_call, result=executed.result,
            )
        # 按原 index 排序——保证 new_messages 顺序稳定
        ordered = [results_by_index[i] for i in range(len(tool_calls))]

    yield _BatchDone(ordered=ordered)


# ============================================================================
# 主入口：run_event_loop
# ============================================================================


async def run_event_loop(
    *,
    system_prompt: str,
    user_text: str | None = None,
    initial_messages: list[Message] | None = None,
    client: ModelClient,
    tools: ToolRegistry | Iterable[AgentTool] | None = None,
    transform_context_fn: TransformContextFn | None = None,
    before_tool_call: BeforeToolCallFn | None = None,
    after_tool_call: AfterToolCallFn | None = None,
    signal: asyncio.Event | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
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

    Bug-fix 新增参数：
      max_turns              —— 单次 run_event_loop 的最大 LLM 调用轮数（默认 50）；
                                 达到上限时生成 stop_reason="error" AssistantMessage
                                 收敛（error_message="max_turns exceeded"），
                                 防止模型持续 yield tool_call 时死循环。

    Step 9 abort 检查点：
      1. while iter 开始（before transform/stream）—— 立即发 aborted assistant + 收敛
      2. client.stream 迭代中收到 ErrorEvent 且 signal set —— 标 aborted
      3. tool batch 之前（signal set 时跳过 batch，发 aborted assistant + 收敛）
      4. sequential 模式下每个工具执行前（已由 _execute_tool_batch 处理）

    Step 8 / 7 / 6 / 5 / 4 / 3 / 2 / 1 行为全部保留。
    """
    if user_text is None and not initial_messages:
        raise ValueError(
            "run_event_loop: 必须提供 user_text 或 initial_messages 至少一项"
        )
    if max_turns < 1:
        raise ValueError(
            f"run_event_loop: max_turns 必须 >= 1，实际 {max_turns}"
        )

    registry = _to_registry(tools)
    tool_defs = registry.definitions()
    before_fn = before_tool_call or default_before_tool_call
    after_fn = after_tool_call or default_after_tool_call

    new_messages: list[Message] = list(initial_messages) if initial_messages else []

    yield AgentStartEvent()
    yield TurnStartEvent()

    if user_text is not None:
        user_msg = UserMessage(content=[TextContent(text=user_text)])
        yield MessageStartEvent(message=user_msg)
        yield MessageEndEvent(message=user_msg)
        new_messages.append(user_msg)

    last_tool_results: list[ToolResultMessage] = []
    turn_count = 0

    while True:
        # —— max_turns 安全网：超出即收敛为 error assistant ——
        if turn_count >= max_turns:
            over_assistant = AssistantMessage(
                content=[TextContent(text="")],
                api=client.api_id, provider=client.provider_id,
                model=getattr(client, "model", "unknown"),
                stop_reason="error",
                error_message=f"max_turns exceeded ({max_turns})",
            )
            yield MessageStartEvent(message=over_assistant)
            yield MessageEndEvent(message=over_assistant)
            new_messages.append(over_assistant)
            yield TurnEndEvent(message=over_assistant, tool_results=last_tool_results)
            yield AgentEndEvent(messages=new_messages)
            return
        turn_count += 1
        # —— Step 9 检查点 1：while iter 开始 ——
        if signal is not None and signal.is_set():
            async for ev in _finalize_abort(
                client=client,
                new_messages=new_messages,
                last_tool_results=last_tool_results,
            ):
                yield ev
            return

        # —— Context 转换 ——
        raw_context: list[AgentMessage] = list(new_messages)
        transform = transform_context_fn or default_transform_context
        transformed = await transform(raw_context)
        llm_messages = convert_to_llm(transformed)

        # —— 调 LLM ——
        text_buf: list[str] = []
        tool_calls: list[ToolCall] = []
        stop_reason: str = "stop"
        error_message: str | None = None
        final_usage = Usage()

        def make_assistant() -> AssistantMessage:  # noqa: B023
            content: list[Any] = []
            if text_buf:  # noqa: B023
                content.append(TextContent(text="".join(text_buf)))  # noqa: B023
            content.extend(tool_calls)  # noqa: B023
            return AssistantMessage(
                content=content,
                api=client.api_id, provider=client.provider_id,
                model=getattr(client, "model", "unknown"),
                stop_reason=stop_reason, error_message=error_message,  # noqa: B023
                usage=final_usage,  # noqa: B023
            )

        yield MessageStartEvent(message=make_assistant())

        async for s_ev in client.stream(
            system_prompt=system_prompt,
            messages=llm_messages,
            tools=tool_defs if tool_defs else None,
            signal=signal,
        ):
            if isinstance(s_ev, TextDeltaEvent):
                text_buf.append(s_ev.delta)
                yield MessageUpdateEvent(
                    message=make_assistant(), assistant_message_event=s_ev,
                )
            elif isinstance(s_ev, ToolCallEvent):
                tool_calls.append(s_ev.tool_call)
            elif isinstance(s_ev, DoneEvent):
                stop_reason = s_ev.stop_reason
                final_usage = s_ev.usage
                # Step 21：adapter 主动发 DoneEvent(stop_reason="aborted") 时
                # 也走 abort 路径——抛弃 partial text / tool_calls，记 error_message
                if stop_reason == "aborted":
                    error_message = "aborted"
                    text_buf.clear()
                    tool_calls.clear()
                break
            elif isinstance(s_ev, ErrorEvent):
                # Step 9：若 signal set，标记为 aborted；否则 error
                if signal is not None and signal.is_set():
                    stop_reason = "aborted"
                    error_message = "aborted"
                    # 抛弃已累积的 partial text / tool_calls——aborted assistant 内容为空
                    text_buf.clear()
                    tool_calls.clear()
                else:
                    stop_reason = "error"
                    error_message = s_ev.message
                break

        # Step 9：stream 结束后再检一次 signal（覆盖 signal 在 stream 末尾被 set 的情况）
        if signal is not None and signal.is_set() and stop_reason == "stop":
            stop_reason = "aborted"
            error_message = "aborted"
            text_buf.clear()
            tool_calls.clear()

        assistant = make_assistant()
        yield MessageEndEvent(message=assistant)
        new_messages.append(assistant)

        # 错误或被中断：直接结束
        if assistant.stop_reason in ("error", "aborted"):
            yield TurnEndEvent(message=assistant, tool_results=last_tool_results)
            yield AgentEndEvent(messages=new_messages)
            return

        # 没 ToolCall：本轮 turn 自然结束
        if not tool_calls:
            yield TurnEndEvent(message=assistant, tool_results=last_tool_results)
            yield AgentEndEvent(messages=new_messages)
            return

        # —— Step 9 检查点 3：tool batch 之前 ——
        # abort 来了：跳过 batch，发 aborted assistant + 收敛
        if signal is not None and signal.is_set():
            async for ev in _finalize_abort(
                client=client,
                new_messages=new_messages,
                last_tool_results=last_tool_results,
            ):
                yield ev
            return

        # —— Step 7：多工具 batch 执行 ——
        batch_messages_snapshot = list(new_messages)
        ordered: list[ExecutedToolResult] = []
        async for ev in _execute_tool_batch(
            registry=registry,
            tool_calls=tool_calls,
            messages=batch_messages_snapshot,
            before_fn=before_fn,
            after_fn=after_fn,
            signal=signal,
            permission_policy=permission_policy,
            permission_audit_log=permission_audit_log,
        ):
            if isinstance(ev, _BatchDone):
                ordered = ev.ordered
                break
            yield ev

        # 按原序追加 ToolResultMessage
        for executed in ordered:
            new_messages.append(executed.message)
            last_tool_results.append(executed.message)

        # terminate 早停：本批所有工具 terminate=True 才生效
        if ordered and all(ex.result.terminate for ex in ordered):
            yield TurnEndEvent(message=assistant, tool_results=last_tool_results)
            yield AgentEndEvent(messages=new_messages)
            return

        # 否则继续下一轮 LLM 调用（while 循环）


async def _finalize_abort(
    *,
    client: ModelClient,
    new_messages: list[Message],
    last_tool_results: list[ToolResultMessage],
) -> AsyncIterator[AgentEvent]:
    """Step 9 abort 终化——生成空的 aborted assistant 并正常收敛。

    用于检查点 1（while iter 开始）与检查点 3（tool batch 之前）：
    这两个位置还没有进行中的 assistant message，需要现造一个。
    """
    aborted = AssistantMessage(
        content=[],
        api=client.api_id, provider=client.provider_id,
        model=getattr(client, "model", "unknown"),
        stop_reason="aborted", error_message="aborted",
    )
    yield MessageStartEvent(message=aborted)
    yield MessageEndEvent(message=aborted)
    new_messages.append(aborted)
    yield TurnEndEvent(message=aborted, tool_results=last_tool_results)
    yield AgentEndEvent(messages=new_messages)


async def run_min_loop(
    *,
    system_prompt: str,
    user_text: str | None = None,
    initial_messages: list[Message] | None = None,
    client: ModelClient,
    tools: ToolRegistry | Iterable[AgentTool] | None = None,
    before_tool_call: BeforeToolCallFn | None = None,
    after_tool_call: AfterToolCallFn | None = None,
    permission_policy: ToolPermissionPolicy | None = None,
    permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
    max_turns: int = 50,
) -> list[Message]:
    """Step 1+ 便捷封装：跑 event loop，返回所有新 messages。

    Step 8 起支持 continue_ 用法（user_text=None + initial_messages=[...]）。
    Step 18 起支持 permission_policy / permission_audit_log 透传。
    """
    async for ev in run_event_loop(
        system_prompt=system_prompt, user_text=user_text,
        initial_messages=initial_messages,
        client=client, tools=tools,
        before_tool_call=before_tool_call,
        after_tool_call=after_tool_call,
        permission_policy=permission_policy,
        permission_audit_log=permission_audit_log,
        max_turns=max_turns,
    ):
        if isinstance(ev, AgentEndEvent):
            return ev.messages
    return []


__all__ = [
    "run_event_loop", "run_min_loop",
    "ExecutedToolResult",
]

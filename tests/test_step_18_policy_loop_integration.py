"""Step 18 — Permission Policy × Loop 集成测试。

覆盖：
- permission 在 validation 前执行
- policy deny 不调 validate_tool_arguments / tool.execute / after_tool_call
- policy allow 后正常 validation + execute + after hook
- before_tool_call 改 tool_call 后按新 tool 做 permission
- policy 抛异常不让 loop 崩
- parallel batch 中一个 deny 不影响另一个 allow
- audit log 写入 allow / deny / require_approval / exception 四类
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    AgentEndEvent,
    AllowAllToolPermissionPolicy,
    DefaultToolPermissionPolicy,
    DenyAllToolPermissionPolicy,
    DoneEvent,
    FakeClient,
    InMemoryToolPermissionAuditLog,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    ToolRegistry,
    ToolResultMessage,
    Usage,
    run_event_loop,
)
from pi_agent_core_py.hooks import (
    AfterToolCallContext,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from pi_agent_core_py.tools import AgentTool, ToolResult

# ============================================================================
# Fixture
# ============================================================================


class _StubTool(AgentTool):
    """记录每次 execute 调用；返回固定文本。"""

    def __init__(self, name: str, execution_mode: str = "parallel") -> None:
        self.name = name
        self.label = name
        self.description = f"stub {name}"
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self.execution_mode = execution_mode  # type: ignore[assignment]
        self.execute_calls: list[tuple[str, dict]] = []
        self.raise_on_execute: Exception | None = None

    async def execute(self, tool_call_id: str, args: dict[str, Any]) -> ToolResult:
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.execute_calls.append((tool_call_id, dict(args)))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=f"executed:{self.name}")],
        )


def _make_fake_llm_single_tool_call(tool_name: str, args: dict | None = None) -> FakeClient:
    return FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id=f"call_{tool_name}", name=tool_name, arguments=args or {},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])


# ============================================================================
# 1. deny 不调 validate / execute / after
# ============================================================================


@pytest.mark.asyncio
async def test_deny_policy_skips_execute_and_after_hook() -> None:
    tool = _StubTool("read_file")
    audit = InMemoryToolPermissionAuditLog()
    after_calls: list[str] = []

    async def after_hook(ctx: AfterToolCallContext) -> ToolResult:
        after_calls.append(ctx.tool_call.name)
        return ctx.result

    fake = _make_fake_llm_single_tool_call("read_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        after_tool_call=after_hook,
        permission_policy=DenyAllToolPermissionPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert tool.execute_calls == [], "deny 不应调 execute"
    assert after_calls == [], "deny 不应调 after_tool_call"

    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert tr.details["error_type"] == "ToolPermissionDenied"
    assert tr.details["policy"]["policy_name"] == "deny_all"

    # audit log 记录了 deny
    records = audit.list_records()
    assert len(records) == 1
    assert records[0].decision == "deny"


# ============================================================================
# 2. allow 后正常 validation + execute + after
# ============================================================================


@pytest.mark.asyncio
async def test_allow_policy_runs_full_chain() -> None:
    tool = _StubTool("read_file")
    audit = InMemoryToolPermissionAuditLog()
    after_calls: list[str] = []

    async def after_hook(ctx: AfterToolCallContext) -> ToolResult:
        after_calls.append(ctx.tool_call.name)
        return ctx.result

    fake = _make_fake_llm_single_tool_call("read_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        after_tool_call=after_hook,
        permission_policy=AllowAllToolPermissionPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert len(tool.execute_calls) == 1
    assert after_calls == ["read_file"]

    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is False
    assert tr.content[0].text == "executed:read_file"

    assert [r.decision for r in audit.list_records()] == ["allow"]


# ============================================================================
# 3. before_tool_call 改名后按新 tool 做 permission
# ============================================================================


@pytest.mark.asyncio
async def test_permission_uses_effective_tool_call_after_hook_rename() -> None:
    """before_tool_call 把 write_file 改成 read_file。
    AllowAll policy 也按新 name 检查；execute 走 read_file。
    """
    write_tool = _StubTool("write_file")
    read_tool = _StubTool("read_file")

    async def before_hook(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
        return BeforeToolCallResult(
            allow=True,
            tool_call=ToolCall(
                id=ctx.tool_call.id, name="read_file", arguments={},
            ),
        )

    fake = _make_fake_llm_single_tool_call("write_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([write_tool, read_tool]),
        before_tool_call=before_hook,
        permission_policy=DefaultToolPermissionPolicy(),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    # execute 跑了 read_tool（被改名后的），write_tool 没跑
    assert len(read_tool.execute_calls) == 1
    assert write_tool.execute_calls == []
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.name == "read_file"
    assert tr.is_error is False


# ============================================================================
# 4. policy 抛异常不让 loop 崩
# ============================================================================


class _ExplodingPolicy(AllowAllToolPermissionPolicy):
    name = "exploding"

    async def check_tool_call(self, **kwargs):  # type: ignore[override]
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_policy_exception_does_not_crash_loop() -> None:
    tool = _StubTool("read_file")
    audit = InMemoryToolPermissionAuditLog()
    fake = _make_fake_llm_single_tool_call("read_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=_ExplodingPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert tool.execute_calls == []
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert tr.details["error_type"] == "ToolPermissionPolicyError"

    records = audit.list_records()
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].metadata.get("policy_exception") == "RuntimeError"


# ============================================================================
# 5. parallel batch：deny 不影响 allow
# ============================================================================


@pytest.mark.asyncio
async def test_parallel_batch_one_deny_one_allow_independent() -> None:
    read_tool = _StubTool("read_file")
    write_tool = _StubTool("write_file")
    audit = InMemoryToolPermissionAuditLog()

    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="c_read", name="read_file", arguments={},
            )),
            ToolCallEvent(tool_call=ToolCall(
                id="c_write", name="write_file", arguments={},
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([read_tool, write_tool]),
        permission_policy=DefaultToolPermissionPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    # read_file 执行了，write_file 没有
    assert len(read_tool.execute_calls) == 1
    assert write_tool.execute_calls == []

    # audit 两条记录
    decisions = sorted(r.decision for r in audit.list_records())
    assert decisions == ["allow", "require_approval"]


# ============================================================================
# 6. permission_policy=None 保持向后兼容
# ============================================================================


@pytest.mark.asyncio
async def test_no_policy_keeps_old_behavior() -> None:
    tool = _StubTool("anything")
    fake = _make_fake_llm_single_tool_call("anything")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=None,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert len(tool.execute_calls) == 1
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.is_error is False


# ============================================================================
# 7. require_approval 写 audit
# ============================================================================


@pytest.mark.asyncio
async def test_require_approval_decision_writes_audit() -> None:
    tool = _StubTool("shell")
    audit = InMemoryToolPermissionAuditLog()
    fake = _make_fake_llm_single_tool_call("shell")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=DefaultToolPermissionPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.details["error_type"] == "ToolApprovalRequired"

    records = audit.list_records()
    assert len(records) == 1
    assert records[0].decision == "require_approval"


# ============================================================================
# 8. require_approval 不调 execute
# ============================================================================


@pytest.mark.asyncio
async def test_require_approval_does_not_execute() -> None:
    tool = _StubTool("shell")
    fake = _make_fake_llm_single_tool_call("shell")
    async for _ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=DefaultToolPermissionPolicy(),
    ):
        # 仅消费；断言在末尾
        pass
    assert tool.execute_calls == []


# ============================================================================
# 9. policy 返回非法对象（不是 ToolPermissionDecision）也不让 loop 崩
# ============================================================================


class _InvalidReturnPolicy(AllowAllToolPermissionPolicy):
    """返回 dict 而非 ToolPermissionDecision——应被 loop 当作 PolicyError。"""

    name = "invalid_return"

    async def check_tool_call(self, **kwargs):  # type: ignore[override]
        return {"decision": "allow"}  # type: ignore[return-value]


class _NoneReturnPolicy(AllowAllToolPermissionPolicy):
    """返回 None——loop 同样应包成 PolicyError。"""

    name = "none_return"

    async def check_tool_call(self, **kwargs):  # type: ignore[override]
        return None  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_policy_returning_dict_does_not_crash_loop() -> None:
    tool = _StubTool("read_file")
    audit = InMemoryToolPermissionAuditLog()
    fake = _make_fake_llm_single_tool_call("read_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=_InvalidReturnPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    assert tool.execute_calls == []
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.details["error_type"] == "ToolPermissionPolicyError"
    # audit 当 deny 处理
    records = audit.list_records()
    assert len(records) == 1
    assert records[0].decision == "deny"
    assert records[0].metadata.get("policy_exception") == "TypeError"


@pytest.mark.asyncio
async def test_policy_returning_none_does_not_crash_loop() -> None:
    tool = _StubTool("read_file")
    fake = _make_fake_llm_single_tool_call("read_file")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=_NoneReturnPolicy(),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.details["error_type"] == "ToolPermissionPolicyError"


# ============================================================================
# 10. policy deny 不调用 validate_tool_arguments
# ============================================================================


@pytest.mark.asyncio
async def test_deny_policy_skips_validate_tool_arguments() -> None:
    """DenyAll 拒绝一个 schema 不对的工具——validation 不应被调用。

    若 validation 真被调用，它会抛 ToolArgumentValidationError，loop 应转为
    error_type=ToolArgumentValidationError；本测试断言 result 是
    ToolPermissionDenied 而非 ArgumentValidation，证明 validation 没跑。
    """
    # 故意构造一个 schema 要求 required field，args 不传——validation 失败
    class _StrictTool(_StubTool):
        def __init__(self) -> None:
            super().__init__("read_file")
            self.parameters = {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }

    tool = _StrictTool()
    fake = _make_fake_llm_single_tool_call("read_file")  # args={}
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry([tool]),
        permission_policy=DenyAllToolPermissionPolicy(),
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    # 是 PermissionDenied 而非 ArgumentValidationError——证明 validation 没跑
    assert tr.details["error_type"] == "ToolPermissionDenied"


# ============================================================================
# 11. policy 对 ToolNotFound 工具仍记 audit
# ============================================================================


@pytest.mark.asyncio
async def test_policy_audits_unknown_tool_before_tool_not_found() -> None:
    """LLM 调了一个不存在的 read-like 工具——policy 先 allow 记 audit，
    然后才返回 ToolNotFoundError。

    这是 Step 18 设计契约：policy 在 ToolNotFound 检查之前，给 policy 机会
    记录或拦截未知工具名。安全不受影响（未注册工具不会 execute）。
    """
    audit = InMemoryToolPermissionAuditLog()
    fake = _make_fake_llm_single_tool_call("totally_unknown_readlike")
    agent_end: AgentEndEvent | None = None
    async for ev in run_event_loop(
        system_prompt="x", user_text="hi",
        client=fake, tools=ToolRegistry(),  # 空 registry
        permission_policy=AllowAllToolPermissionPolicy(),
        permission_audit_log=audit,
    ):
        if isinstance(ev, AgentEndEvent):
            agent_end = ev

    assert agent_end is not None
    tr = next(m for m in agent_end.messages if isinstance(m, ToolResultMessage))
    assert tr.details["error_type"] == "ToolNotFoundError"

    # policy 先记了一条 allow
    records = audit.list_records()
    assert len(records) == 1
    assert records[0].decision == "allow"
    assert records[0].tool_name == "totally_unknown_readlike"

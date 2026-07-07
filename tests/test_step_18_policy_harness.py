"""Step 18 — AgentHarness × Permission Policy 集成测试。

覆盖：
- Harness 构造时传入 policy / audit_log
- set_permission_policy 替换（含 None）
- list_permission_audit_records / clear_permission_audit_records
- snapshot metadata 含 policy summary
- session metadata 含 policy summary（via context_metadata）
- audit log 不全量塞 snapshot metadata
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AllowAllToolPermissionPolicy,
    DefaultToolPermissionPolicy,
    DenyAllToolPermissionPolicy,
    DoneEvent,
    FakeClient,
    InMemoryToolPermissionAuditLog,
    SessionMemory,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from pi_agent_core_py.tools import AgentTool, ToolResult

# ============================================================================
# Fixture
# ============================================================================


class _Stub(AgentTool):
    def __init__(self, name: str) -> None:
        self.name = name
        self.label = name
        self.description = f"stub {name}"
        self.parameters: dict = {"type": "object", "properties": {}}
        self.execution_mode = "parallel"

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id, name=self.name,
            content=[TextContent(text=f"ok:{self.name}")],
        )


def _fake_llm(tool_name: str) -> FakeClient:
    return FakeClient([
        [ToolCallEvent(tool_call=ToolCall(
            id=f"c_{tool_name}", name=tool_name, arguments={},
        )), DoneEvent(stop_reason="tool_use", usage=Usage())],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])


# ============================================================================
# 1. 构造时传入 policy / audit_log
# ============================================================================


@pytest.mark.asyncio
async def test_harness_constructor_accepts_policy_and_audit() -> None:
    audit = InMemoryToolPermissionAuditLog()
    agent = Agent(system_prompt="x", client=FakeClient([]), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=DefaultToolPermissionPolicy(),
        permission_audit_log=audit,
    )
    assert harness.agent is agent
    # audit 同步到 agent
    assert agent.permission_audit_log is audit
    assert agent.permission_policy is not None
    assert agent.permission_policy.name == "default"


@pytest.mark.asyncio
async def test_harness_default_audit_log_created_if_not_provided() -> None:
    agent = Agent(system_prompt="x", client=FakeClient([]), tools=[_Stub("read_file")])
    harness = AgentHarness(agent)
    assert harness.permission_audit_log is not None
    assert agent.permission_audit_log is harness.permission_audit_log


# ============================================================================
# 2. set_permission_policy
# ============================================================================


@pytest.mark.asyncio
async def test_set_permission_policy_replaces_and_syncs_to_agent() -> None:
    agent = Agent(system_prompt="x", client=FakeClient([]), tools=[_Stub("read_file")])
    harness = AgentHarness(agent)
    assert agent.permission_policy is None

    harness.set_permission_policy(DefaultToolPermissionPolicy())
    assert agent.permission_policy is not None
    assert agent.permission_policy.name == "default"

    harness.set_permission_policy(None)
    assert agent.permission_policy is None


# ============================================================================
# 3. list / clear audit records
# ============================================================================


@pytest.mark.asyncio
async def test_list_and_clear_audit_records_via_harness() -> None:
    agent = Agent(system_prompt="x", client=_fake_llm("read_file"), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=DenyAllToolPermissionPolicy(),
    )

    await harness.run_prompt("hi")
    records = harness.list_permission_audit_records()
    assert len(records) == 1
    assert records[0].decision == "deny"

    harness.clear_permission_audit_records()
    assert harness.list_permission_audit_records() == []


# ============================================================================
# 4. snapshot metadata 含 policy summary
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_metadata_contains_policy_summary() -> None:
    agent = Agent(system_prompt="x", client=_fake_llm("read_file"), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=AllowAllToolPermissionPolicy(),
    )

    await harness.run_prompt("hi")
    snap = harness.last_snapshot
    assert snap is not None
    policy_meta = snap.metadata.get("policy")
    assert policy_meta is not None
    assert policy_meta["policy_name"] == "allow_all"
    assert policy_meta["audit_count"] == 1
    assert policy_meta["allowed_count"] == 1
    assert policy_meta["denied_count"] == 0


@pytest.mark.asyncio
async def test_snapshot_metadata_records_denied_count() -> None:
    agent = Agent(system_prompt="x", client=_fake_llm("read_file"), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=DenyAllToolPermissionPolicy(),
    )
    await harness.run_prompt("hi")
    snap = harness.last_snapshot
    assert snap is not None
    policy_meta = snap.metadata["policy"]
    assert policy_meta["denied_count"] == 1
    assert policy_meta["allowed_count"] == 0


# ============================================================================
# 5. 不全量塞 audit records 进 snapshot
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_does_not_embed_full_audit_records() -> None:
    agent = Agent(system_prompt="x", client=_fake_llm("read_file"), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=AllowAllToolPermissionPolicy(),
    )
    await harness.run_prompt("hi")
    snap = harness.last_snapshot
    assert snap is not None
    # policy summary 是个轻量 dict，不应包含 records 列表
    assert "records" not in snap.metadata["policy"]


# ============================================================================
# 6. session metadata 含 policy summary（via context_metadata）
# ============================================================================


@pytest.mark.asyncio
async def test_session_metadata_contains_policy_summary() -> None:
    agent = Agent(system_prompt="x", client=_fake_llm("read_file"), tools=[_Stub("read_file")])
    harness = AgentHarness(
        agent,
        permission_policy=AllowAllToolPermissionPolicy(),
    )
    session = SessionMemory(session_id="sess-1")
    harness.attach_session(session)

    await harness.run_prompt("hi")

    harness.sync_session_metadata()
    harness_meta = session.state.metadata.get("harness") or {}
    ctx_meta = harness_meta.get("context_metadata") or {}
    policy_meta = ctx_meta.get("policy")
    assert policy_meta is not None
    assert policy_meta["policy_name"] == "allow_all"
    assert policy_meta["audit_count"] == 1


# ============================================================================
# 7. agent.permission_policy 替换后下个请求生效
# ============================================================================


@pytest.mark.asyncio
async def test_policy_change_takes_effect_on_next_request() -> None:
    agent = Agent(
        system_prompt="x",
        client=FakeClient([
            [
                ToolCallEvent(tool_call=ToolCall(
                    id="c1", name="read_file", arguments={},
                )),
                DoneEvent(stop_reason="tool_use", usage=Usage()),
            ],
            [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
        ]),
        tools=[_Stub("read_file")],
    )
    harness = AgentHarness(agent)
    harness.set_permission_policy(AllowAllToolPermissionPolicy())
    await harness.run_prompt("hi")
    assert harness.list_permission_audit_records()[0].decision == "allow"

    # 第二个请求改 DenyAll
    agent.client = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="c2", name="read_file", arguments={},
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])
    harness.set_permission_policy(DenyAllToolPermissionPolicy())
    await harness.run_prompt("hi2")
    last = harness.list_permission_audit_records()[-1]
    assert last.decision == "deny"

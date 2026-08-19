"""Regression coverage for the pi-agent semantic-alignment fixes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py import (
    AfterToolCallContext,
    Agent,
    AgentEndEvent,
    AgentHarness,
    AgentTool,
    AssistantMessage,
    BeforeToolCallContext,
    BeforeToolCallResult,
    DoneEvent,
    FakeClient,
    SessionMemory,
    TextContent,
    ToolCall,
    ToolCallEvent,
    ToolRegistry,
    ToolResult,
    ToolResultMessage,
    UserMessage,
    run_event_loop,
)
from pi_agent_core_py.policy import (
    ToolApprovalContext,
    ToolPermissionDecision,
    ToolPermissionPolicy,
)


class _RecordingTool(AgentTool):
    description = "Record execution order."
    parameters = {"type": "object", "properties": {}}
    execution_mode = "parallel"

    def __init__(self, name: str, order: list[str] | None = None) -> None:
        self.name = name
        self.label = name
        self.order = order if order is not None else []
        self.calls = 0

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        self.calls += 1
        self.order.append(f"execute:{tool_call_id}")
        await asyncio.sleep(0)
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="ok")],
        )


def _parallel_client() -> FakeClient:
    return FakeClient(
        [
            [
                ToolCallEvent(tool_call=ToolCall(id="a", name="first", arguments={})),
                ToolCallEvent(tool_call=ToolCall(id="b", name="second", arguments={})),
                DoneEvent(stop_reason="tool_use"),
            ],
            [DoneEvent(stop_reason="stop")],
        ]
    )


class _ApprovalPolicy(ToolPermissionPolicy):
    name = "approval_order"

    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def check_tool_call(self, *, tool_call, tool, messages):
        self.order.append(f"policy:{tool_call.id}")
        return ToolPermissionDecision(
            decision="require_approval",
            reason="test",
            policy_name=self.name,
        )


@pytest.mark.asyncio
async def test_parallel_batch_serializes_all_preflight_before_execution() -> None:
    order: list[str] = []
    active_approvals = 0
    max_active_approvals = 0

    async def before(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
        order.append(f"before:{ctx.tool_call.id}")
        await asyncio.sleep(0)
        return BeforeToolCallResult()

    async def approve(ctx: ToolApprovalContext) -> bool:
        nonlocal active_approvals, max_active_approvals
        order.append(f"approval-start:{ctx.tool_call.id}")
        active_approvals += 1
        max_active_approvals = max(max_active_approvals, active_approvals)
        await asyncio.sleep(0.001)
        active_approvals -= 1
        order.append(f"approval-end:{ctx.tool_call.id}")
        return True

    _ = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=_parallel_client(),
            tools=ToolRegistry(
                [
                    _RecordingTool("first", order),
                    _RecordingTool("second", order),
                ]
            ),
            before_tool_call=before,
            permission_policy=_ApprovalPolicy(order),
            tool_approval_handler=approve,
        )
    ]

    expected_preflight = [
        "before:a",
        "policy:a",
        "approval-start:a",
        "approval-end:a",
        "before:b",
        "policy:b",
        "approval-start:b",
        "approval-end:b",
    ]
    assert order[:8] == expected_preflight
    assert all(item.startswith("execute:") for item in order[8:])
    assert max_active_approvals == 1


@pytest.mark.asyncio
async def test_max_turns_error_is_canonical_and_persisted() -> None:
    tool = _RecordingTool("first")
    client = FakeClient(
        [
            [
                ToolCallEvent(tool_call=ToolCall(id="a", name="first", arguments={})),
                DoneEvent(stop_reason="tool_use"),
            ]
        ]
    )
    agent = Agent(
        system_prompt="sys",
        client=client,
        tools=ToolRegistry([tool]),
        max_turns=1,
    )
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="semantic-max-turns")
    harness.attach_session(session)

    messages = await harness.run_prompt("go")
    assistants = [m for m in messages if isinstance(m, AssistantMessage)]

    assert assistants[-1].stop_reason == "error"
    assert assistants[-1].error_message == "max_turns exceeded (1)"
    assert agent.state.messages == messages
    assert harness.last_snapshot is not None
    assert harness.last_snapshot.status == "error"
    persisted = [m for m in session.get_messages() if isinstance(m, AssistantMessage)]
    assert persisted[-1].stop_reason == "error"


@pytest.mark.asyncio
async def test_continue_rejects_assistant_tail_before_enqueue() -> None:
    client = FakeClient([[DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="sys", client=client)
    agent.state.messages = [
        UserMessage(content=[TextContent(text="hello")]),
        AssistantMessage(
            content=[TextContent(text="done")],
            api="fake",
            provider="fake",
            model="fake-1",
        ),
    ]

    with pytest.raises(ValueError, match="assistant"):
        await agent.continue_()

    assert agent.state.queue_size == 0
    assert client.all_messages_calls == []


@pytest.mark.asyncio
async def test_before_hook_cannot_change_tool_call_id() -> None:
    tool = _RecordingTool("first")

    async def before(ctx: BeforeToolCallContext) -> BeforeToolCallResult:
        return BeforeToolCallResult(
            tool_call=ctx.tool_call.model_copy(
                update={"id": "replacement"},
            )
        )

    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ToolCallEvent(
                            tool_call=ToolCall(
                                id="original",
                                name="first",
                                arguments={},
                            )
                        ),
                        DoneEvent(stop_reason="tool_use"),
                    ],
                    [DoneEvent(stop_reason="stop")],
                ]
            ),
            tools=ToolRegistry([tool]),
            before_tool_call=before,
        )
    ]
    agent_end = next(event for event in events if isinstance(event, AgentEndEvent))
    result = next(
        message for message in agent_end.messages if isinstance(message, ToolResultMessage)
    )

    assert tool.calls == 0
    assert result.tool_call_id == "original"
    assert result.details["error_type"] == "ToolCallIdentityMutationError"


@pytest.mark.asyncio
async def test_after_hook_cannot_change_tool_call_id() -> None:
    tool = _RecordingTool("first")

    async def after(ctx: AfterToolCallContext) -> ToolResult:
        return ctx.result.model_copy(update={"tool_call_id": "replacement"})

    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=FakeClient(
                [
                    [
                        ToolCallEvent(
                            tool_call=ToolCall(
                                id="original",
                                name="first",
                                arguments={},
                            )
                        ),
                        DoneEvent(stop_reason="tool_use"),
                    ],
                    [DoneEvent(stop_reason="stop")],
                ]
            ),
            tools=ToolRegistry([tool]),
            after_tool_call=after,
        )
    ]
    agent_end = next(event for event in events if isinstance(event, AgentEndEvent))
    result = next(
        message for message in agent_end.messages if isinstance(message, ToolResultMessage)
    )

    assert tool.calls == 1
    assert result.tool_call_id == "original"
    assert result.details["error_type"] == "ToolCallIdentityMutationError"


@pytest.mark.asyncio
async def test_harness_returns_to_idle_after_hook_error_and_can_retry() -> None:
    calls = 0

    async def fail_once(context) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("before hook failed")

    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[DoneEvent(stop_reason="stop")]]),
    )
    harness = AgentHarness(agent, before_request=[fail_once])

    with pytest.raises(RuntimeError, match="before hook failed"):
        await harness.run_prompt("first")

    assert harness.context.phase == "idle"
    assert harness.context.last_error == "RuntimeError: before hook failed"
    assert harness.last_snapshot is not None
    assert harness.last_snapshot.status == "error"

    await harness.run_prompt("second")
    assert harness.context.phase == "idle"
    assert harness.last_snapshot is not None
    assert harness.last_snapshot.status == "completed"

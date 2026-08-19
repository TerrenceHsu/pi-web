"""ToolResult usage and deferred-tool load-point metadata contracts."""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AgentTool,
    DoneEvent,
    FakeClient,
    LLMToolResultMessage,
    SessionMemory,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    ToolExecutionEndEvent,
    ToolRegistry,
    ToolResult,
    ToolResultMessage,
    Usage,
    run_event_loop,
)
from pi_agent_core_py.hooks import AfterToolCallContext
from pi_agent_core_py.web.serializers import serialize_event, serialize_message


class _LoaderTool(AgentTool):
    name = "loader"
    label = "Loader"
    description = "Mark an existing deferred tool as available."
    parameters = {"type": "object", "properties": {}}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        **_: Any,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="loaded")],
            usage=Usage(input=7, output=3, total_tokens=10),
            added_tool_names=["deferred_search"],
        )


class _DeferredSearchTool(AgentTool):
    name = "deferred_search"
    label = "Deferred search"
    description = "A tool already present in Context.tools but loaded later."
    parameters = {"type": "object", "properties": {}}

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        **_: Any,
    ) -> ToolResult:
        return ToolResult(tool_call_id=tool_call_id, name=self.name)


def test_tool_result_metadata_defaults_are_backward_compatible() -> None:
    result = ToolResult.model_validate({"tool_call_id": "t1", "name": "legacy"})
    message = ToolResultMessage.model_validate(
        {"role": "toolResult", "tool_call_id": "t1", "name": "legacy"}
    )

    assert result.usage is None
    assert result.added_tool_names == []
    assert message.usage is None
    assert message.added_tool_names == []


@pytest.mark.asyncio
async def test_after_hook_can_replace_usage_but_not_added_tool_names() -> None:
    async def after(context: AfterToolCallContext, _signal=None) -> ToolResult:
        return context.result.model_copy(
            update={
                "usage": Usage(input=11, output=4, total_tokens=15),
                "added_tool_names": ["hook_injected"],
            }
        )

    events = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="load it",
            client=FakeClient(
                [
                    [
                        ToolCallEvent(
                            tool_call=ToolCall(
                                id="load-1", name="loader", arguments={}
                            )
                        ),
                        DoneEvent(stop_reason="tool_use"),
                    ],
                    [DoneEvent(stop_reason="stop")],
                ]
            ),
            tools=ToolRegistry([_LoaderTool(), _DeferredSearchTool()]),
            after_tool_call=after,
        )
    ]
    result_message = next(
        event.message
        for event in events
        if event.type == "message_end"
        and isinstance(event.message, ToolResultMessage)
    )

    assert result_message.usage == Usage(input=11, output=4, total_tokens=15)
    assert result_message.added_tool_names == ["deferred_search"]


@pytest.mark.asyncio
async def test_tool_result_metadata_survives_runtime_and_persistence_boundaries() -> None:
    fake = FakeClient(
        [
            [
                ToolCallEvent(
                    tool_call=ToolCall(id="load-1", name="loader", arguments={})
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop")],
        ]
    )
    agent = Agent(
        system_prompt="sys",
        client=fake,
        tools=ToolRegistry([_LoaderTool(), _DeferredSearchTool()]),
    )
    harness = AgentHarness(agent)
    session = SessionMemory(session_id="tool-result-metadata")
    harness.attach_session(session)
    events = []
    unsubscribe = agent.subscribe(lambda event, _state: events.append(event))

    try:
        messages = await harness.run_prompt("load it")
    finally:
        unsubscribe()

    result_message = next(
        message for message in messages if isinstance(message, ToolResultMessage)
    )
    assert result_message.usage == Usage(input=7, output=3, total_tokens=10)
    assert result_message.added_tool_names == ["deferred_search"]

    execution_end = next(
        event for event in events if isinstance(event, ToolExecutionEndEvent)
    )
    assert execution_end.result.usage == result_message.usage
    assert execution_end.result.added_tool_names == result_message.added_tool_names

    message_payload = serialize_message(result_message)
    assert message_payload["usage"] == {"input": 7, "output": 3, "total_tokens": 10}
    assert message_payload["added_tool_names"] == ["deferred_search"]
    event_payload = serialize_event(execution_end)
    assert event_payload["result"]["usage"] == message_payload["usage"]
    assert event_payload["result"]["added_tool_names"] == ["deferred_search"]

    second_call_result = next(
        message
        for message in fake.all_messages_calls[1]
        if isinstance(message, LLMToolResultMessage)
    )
    assert second_call_result.usage == result_message.usage
    assert second_call_result.added_tool_names == ["deferred_search"]

    # added_tool_names is a provider load point, not a registry mutation request.
    assert [tool.name for tool in fake.all_tools_calls[1] or []] == [
        "loader",
        "deferred_search",
    ]

    snapshot = harness.last_snapshot
    assert snapshot is not None
    snap_result = snapshot.tool_results[0]
    assert snap_result.usage == result_message.usage
    assert snap_result.added_tool_names == ["deferred_search"]
    assert snapshot.turns[0].tool_results[0] == snap_result

    restored_result = next(
        message
        for message in session.get_messages()
        if isinstance(message, ToolResultMessage)
    )
    assert restored_result.usage == result_message.usage
    assert restored_result.added_tool_names == ["deferred_search"]

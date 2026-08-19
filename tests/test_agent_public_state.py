"""Public AgentState compatibility and lifecycle contracts."""

from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentModelState,
    AgentState,
    AssistantMessage,
    DoneEvent,
    FakeClient,
    ModelClient,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
)
from pi_agent_core_py.providers.fake import FakeProviderAdapter
from pi_agent_core_py.tools import AgentTool, ToolResult


class _EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "Echo a supplied value"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: Any = None,
        on_update: Any = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=str(args["value"]))],
        )


def test_public_state_exposes_model_and_thinking_configuration() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([]),
        thinking_level="high",
    )

    assert agent.state.model == AgentModelState(
        id="fake-1",
        provider="fake",
        api="fake",
    )
    assert agent.state.thinking_level == "high"
    assert agent.state.is_streaming is False
    assert agent.state.streaming_message is None
    assert agent.state.pending_tool_calls == frozenset()
    assert agent.state.error_message is None

    replacement = ModelClient(FakeProviderAdapter([], model="replacement-model"))
    replacement.api_id = "replacement-api"
    agent.client = replacement

    assert agent.state.model == AgentModelState(
        id="replacement-model",
        provider="fake",
        api="replacement-api",
    )


def test_agent_state_rejects_unknown_thinking_level() -> None:
    with pytest.raises(ValueError, match="thinking_level"):
        AgentState(thinking_level="turbo")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_state_follows_message_and_tool_lifecycles() -> None:
    tool_call = ToolCall(
        id="call-1",
        name="echo",
        arguments={"value": "hello"},
    )
    agent = Agent(
        system_prompt="sys",
        client=FakeClient(
            [
                [
                    ToolCallEvent(tool_call=tool_call),
                    DoneEvent(stop_reason="tool_use"),
                ],
                [
                    TextDeltaEvent(delta="done"),
                    DoneEvent(stop_reason="stop"),
                ],
            ]
        ),
        tools=[_EchoTool()],
    )
    observations: list[
        tuple[str, bool, AssistantMessage | None, frozenset[str]]
    ] = []

    def observe(event: Any, state: AgentState) -> None:
        streaming_message = (
            state.streaming_message.model_copy(deep=True)
            if isinstance(state.streaming_message, AssistantMessage)
            else None
        )
        observations.append(
            (
                event.type,
                state.is_streaming,
                streaming_message,
                state.pending_tool_calls,
            )
        )

    agent.subscribe(observe)
    await agent.prompt("go")

    assistant_updates = [
        item for item in observations if item[0] == "message_update"
    ]
    assert assistant_updates
    assert all(item[1] is True for item in assistant_updates)
    assert any(item[2] is not None for item in assistant_updates)

    tool_start = next(
        item for item in observations if item[0] == "tool_execution_start"
    )
    tool_end = next(
        item for item in observations if item[0] == "tool_execution_end"
    )
    assert tool_start[3] == frozenset({"call-1"})
    assert tool_end[3] == frozenset()

    agent_end = next(item for item in reversed(observations) if item[0] == "agent_end")
    assert agent_end[1] is True
    assert agent_end[2] is None
    assert agent.state.is_streaming is False
    assert agent.state.streaming_message is None
    assert agent.state.pending_tool_calls == frozenset()


@pytest.mark.asyncio
async def test_error_state_and_reset_clear_runtime_owned_fields() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([]),
        thinking_level="medium",
    )

    await agent.prompt("go")

    assert agent.state.error_message == "FakeClient: no more scripts"
    assert agent.state.is_streaming is False

    model_before = agent.state.model
    agent.reset()

    assert agent.state.model == model_before
    assert agent.state.thinking_level == "medium"
    assert agent.state.error_message is None
    assert agent.state.streaming_message is None
    assert agent.state.pending_tool_calls == frozenset()

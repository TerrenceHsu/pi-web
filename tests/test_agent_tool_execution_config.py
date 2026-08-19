"""Global tool execution configuration and per-tool override contracts."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from pi_agent_core_py import (
    Agent,
    AgentTool,
    DoneEvent,
    FakeClient,
    TextContent,
    ToolCall,
    ToolCallEvent,
    ToolExecutionMode,
    ToolRegistry,
    ToolResult,
    run_event_loop,
    run_min_loop,
)


class _ConcurrencyProbe:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.release = asyncio.Event()


class _ProbeTool(AgentTool):
    description = "Probe tool execution concurrency."
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        probe: _ConcurrencyProbe,
        execution_mode: ToolExecutionMode = "parallel",
    ) -> None:
        self.name = name
        self.label = name
        self.probe = probe
        self.execution_mode = execution_mode

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        self.probe.active += 1
        self.probe.max_active = max(self.probe.max_active, self.probe.active)
        if self.probe.active == 2:
            self.probe.release.set()
        try:
            try:
                await asyncio.wait_for(self.probe.release.wait(), timeout=0.05)
            except TimeoutError:
                pass
        finally:
            self.probe.active -= 1
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="ok")],
        )


def _client_for_two_tools() -> FakeClient:
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


def _tools_with_modes(
    first_mode: ToolExecutionMode,
    second_mode: ToolExecutionMode,
) -> tuple[_ConcurrencyProbe, ToolRegistry]:
    probe = _ConcurrencyProbe()
    return probe, ToolRegistry(
        [
            _ProbeTool("first", probe, first_mode),
            _ProbeTool("second", probe, second_mode),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_execution", "first_mode", "second_mode", "expected_max_active"),
    [
        ("parallel", "parallel", "parallel", 2),
        ("parallel", "sequential", "parallel", 1),
        ("sequential", "parallel", "parallel", 1),
        ("sequential", "parallel", "sequential", 1),
    ],
)
async def test_global_mode_and_per_tool_override_select_batch_execution(
    tool_execution: ToolExecutionMode,
    first_mode: ToolExecutionMode,
    second_mode: ToolExecutionMode,
    expected_max_active: int,
) -> None:
    probe, tools = _tools_with_modes(first_mode, second_mode)

    _ = [
        event
        async for event in run_event_loop(
            system_prompt="sys",
            user_text="go",
            client=_client_for_two_tools(),
            tools=tools,
            tool_execution=tool_execution,
        )
    ]

    assert probe.max_active == expected_max_active


@pytest.mark.asyncio
async def test_agent_passes_tool_execution_to_loop() -> None:
    probe, tools = _tools_with_modes("parallel", "parallel")
    agent = Agent(
        system_prompt="sys",
        client=_client_for_two_tools(),
        tools=tools,
        tool_execution="sequential",
    )

    await agent.prompt("go")

    assert probe.max_active == 1
    assert agent.tool_execution == "sequential"


@pytest.mark.asyncio
async def test_run_min_loop_passes_tool_execution_to_event_loop() -> None:
    probe, tools = _tools_with_modes("parallel", "parallel")

    await run_min_loop(
        system_prompt="sys",
        user_text="go",
        client=_client_for_two_tools(),
        tools=tools,
        tool_execution="sequential",
    )

    assert probe.max_active == 1


def test_agent_tool_execution_is_mutable_and_runtime_validated() -> None:
    agent = Agent(
        system_prompt="sys",
        client=FakeClient([[DoneEvent(stop_reason="stop")]]),
    )

    agent.tool_execution = "sequential"
    assert agent.tool_execution == "sequential"

    with pytest.raises(ValueError, match="tool_execution"):
        agent.tool_execution = cast(ToolExecutionMode, "invalid")

    with pytest.raises(ValueError, match="tool_execution"):
        Agent(
            system_prompt="sys",
            client=FakeClient([[DoneEvent(stop_reason="stop")]]),
            tool_execution=cast(ToolExecutionMode, "invalid"),
        )


@pytest.mark.asyncio
async def test_invalid_loop_tool_execution_fails_before_model_call() -> None:
    client = FakeClient([[DoneEvent(stop_reason="stop")]])

    with pytest.raises(ValueError, match="tool_execution"):
        _ = [
            event
            async for event in run_event_loop(
                system_prompt="sys",
                user_text="go",
                client=client,
                tool_execution=cast(ToolExecutionMode, "invalid"),
            )
        ]

    assert client.all_messages_calls == []

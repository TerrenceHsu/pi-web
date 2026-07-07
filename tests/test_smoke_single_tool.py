"""Smoke test 6: 单工具调用全链路。

UserMessage → FakeClient 返回 ToolCallEvent → registry 找 echo
→ 执行 → ToolResultMessage 回喂 → FakeClient 第二轮 final answer。
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.loop import run_min_loop
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = "echo back the input"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name="echo",
            content=[TextContent(text=args.get("text", ""))],
        )


class FailingTool(AgentTool):
    name = "boom"
    label = "Boom"
    description = "always raises"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        raise RuntimeError("boom!")


class TerminateTool(AgentTool):
    name = "stop_now"
    label = "Stop"
    description = "stops the loop"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name="stop_now",
            content=[TextContent(text="done")],
            terminate=True,
        )


@pytest.mark.asyncio
async def test_single_tool_call_round_trip() -> None:
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="echo", arguments={"text": "hi"},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [
            TextDeltaEvent(delta="final answer"),
            DoneEvent(stop_reason="stop"),
        ],
    ])
    registry = ToolRegistry([EchoTool()])
    msgs = await run_min_loop(
        system_prompt="sys",
        user_text="call echo",
        client=fake,
        tools=registry,
    )

    # 期望：user + assistant(tool_call) + tool_result + final_assistant
    types = [type(m).__name__ for m in msgs]
    assert "UserMessage" in types
    assert "ToolResultMessage" in types

    # ToolCall.id == ToolResult.tool_call_id
    tool_call_msg = next(m for m in msgs if isinstance(m, AssistantMessage) and any(
        isinstance(c, ToolCall) for c in m.content
    ))
    tc = next(c for c in tool_call_msg.content if isinstance(c, ToolCall))
    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.tool_call_id == tc.id
    assert tr.name == "echo"

    # 最终 assistant 应该是 stop
    last_a = [m for m in msgs if isinstance(m, AssistantMessage)][-1]
    assert last_a.stop_reason == "stop"


@pytest.mark.asyncio
async def test_tool_exception_becomes_is_error_result_not_loop_crash() -> None:
    """工具抛异常 → ToolResult(is_error=True)，loop 不崩，能继续到 final answer。"""
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="boom", arguments={},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [
            TextDeltaEvent(delta="after error"),
            DoneEvent(stop_reason="stop"),
        ],
    ])
    registry = ToolRegistry([FailingTool()])
    msgs = await run_min_loop(
        system_prompt="sys",
        user_text="call boom",
        client=fake,
        tools=registry,
    )

    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert tr.tool_call_id == "t1"


@pytest.mark.asyncio
async def test_terminate_tool_short_circuits_loop() -> None:
    """terminate=True 的工具执行后 loop 应该结束，不再调 LLM。"""
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="stop_now", arguments={},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        # 即便有第二轮脚本，也不应该走到
        [
            TextDeltaEvent(delta="should not happen"),
            DoneEvent(stop_reason="stop"),
        ],
    ])
    registry = ToolRegistry([TerminateTool()])
    msgs = await run_min_loop(
        system_prompt="sys",
        user_text="stop",
        client=fake,
        tools=registry,
    )

    # 没有第二个 assistant
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    assert len(assistants) == 1


@pytest.mark.asyncio
async def test_unknown_tool_becomes_error_result() -> None:
    """LLM 调用未注册工具 → ToolResult(is_error)，loop 不崩。"""
    fake = FakeClient([
        [
            ToolCallEvent(tool_call=ToolCall(
                id="t1", name="ghost", arguments={},
            )),
            DoneEvent(stop_reason="tool_use"),
        ],
        [
            TextDeltaEvent(delta="recovered"),
            DoneEvent(stop_reason="stop"),
        ],
    ])
    # registry 里没有 ghost
    registry = ToolRegistry([EchoTool()])
    msgs = await run_min_loop(
        system_prompt="sys",
        user_text="call ghost",
        client=fake,
        tools=registry,
    )

    tr = next(m for m in msgs if isinstance(m, ToolResultMessage))
    assert tr.is_error is True
    assert (
        "ToolNotFound" in tr.details.get("error_type", "")
        or "not found" in tr.content[0].text.lower()
    )

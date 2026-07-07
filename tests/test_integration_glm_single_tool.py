"""Integration test 2: 真实 GLM 单工具调用 smoke。

UserMessage → GLM tool schema → 模型 tool_use → echo tool 执行
→ ToolResultMessage 回喂 → 最终 AssistantMessage。
"""
from __future__ import annotations

import os

import pytest

from pi_agent_core_py import GLMClient
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult

_HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.environ.get("ANTHROPIC_API_KEY")
)
_GLM_SKIP_REASON = (
    "需要至少设置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 之一"
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _HAS_GLM_CREDS, reason=_GLM_SKIP_REASON),
]


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = (
        "Echoes back the text you provide in the 'text' argument. "
        "Use this tool whenever the user asks you to echo something."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "the text to echo back"},
        },
        "required": ["text"],
    }

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name="echo",
            content=[TextContent(text=args.get("text", ""))],
        )


@pytest.mark.asyncio
async def test_real_glm_single_tool_call_round_trip() -> None:
    """模型应该调 echo 工具，结果回喂后给最终 assistant。"""
    client = GLMClient(max_tokens=512)
    agent = Agent(
        system_prompt=(
            "You are a tool-using test bot. "
            "When the user asks you to echo something, you MUST call the 'echo' tool "
            "with that text, then reply briefly confirming what you did."
        ),
        client=client,
        tools=ToolRegistry([EchoTool()]),
    )
    harness = AgentHarness(agent)
    try:
        msgs = await harness.run_prompt("Please echo the text: integration-test-ping")
    finally:
        await harness.close()

    # 至少产生一次 ToolCall
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    tool_calls: list[ToolCall] = []
    for a in assistants:
        tool_calls.extend(c for c in a.content if isinstance(c, ToolCall))
    assert tool_calls, (
        f"模型未触发任何 ToolCall；messages={msgs!r}"
    )

    # ToolResultMessage 被追加，且 tool_call_id 与某个 ToolCall.id 匹配
    results = [m for m in msgs if isinstance(m, ToolResultMessage)]
    assert results, "缺 ToolResultMessage"
    call_ids = {tc.id for tc in tool_calls}
    result_ids = {r.tool_call_id for r in results}
    assert result_ids & call_ids, (
        f"ToolResult.tool_call_id 与 ToolCall.id 不匹配: calls={call_ids} results={result_ids}"
    )

    # 最终 assistant 存在且非异常
    last = assistants[-1]
    assert last.stop_reason in ("stop", "length", "tool_use")
    assert last.stop_reason != "error", f"模型最终 stop_reason=error: {last.error_message}"

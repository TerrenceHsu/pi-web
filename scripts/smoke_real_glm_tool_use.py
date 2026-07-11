"""P1-A3 — 独立 smoke 脚本：真实 GLM e2e_probe 多轮 tool_use。

便于本地排查（不依赖 pytest），输出简洁事件摘要。

用法：
    PYTHONPATH=src /d/miniconda/envs/pipy/python.exe scripts/smoke_real_glm_tool_use.py

退出码：
    0 — PASS（模型调用了 e2e_probe + 最终答案含 token）
    1 — FAIL
    2 — SKIP（无凭证）
"""
from __future__ import annotations

import asyncio
import os
import sys

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

PROBE_TOKEN = "PROBE_OK_ALPHA_7F3A"


class E2EProbeTool(AgentTool):
    name = "e2e_probe"
    label = "E2E Probe"
    description = (
        "Probe tool that returns a fixed token. "
        "You MUST call this tool to obtain the probe token; do not invent it."
    )
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name="e2e_probe",
            content=[TextContent(text=PROBE_TOKEN)],
        )


def _has_creds() -> bool:
    return bool(
        os.environ.get("GLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )


async def main() -> int:
    if not _has_creds():
        print("[smoke] SKIP: no GLM credentials in env")
        return 2

    print("[smoke] starting real GLM e2e_probe round-trip...")
    client = GLMClient(max_tokens=512)
    agent = Agent(
        system_prompt=(
            "You are running a tool-use integration test. "
            "For any request asking for a probe token, you MUST call the e2e_probe tool. "
            "Do not invent the token. "
            "After receiving the tool result, return the exact token in your final answer."
        ),
        client=client,
        tools=ToolRegistry([E2EProbeTool()]),
    )
    harness = AgentHarness(agent)

    try:
        msgs = await harness.run_prompt(
            'Use the e2e_probe tool with value "alpha". '
            "After the tool result is returned, "
            "answer with the exact returned token."
        )
    finally:
        await harness.close()

    print(f"[smoke] total messages: {len(msgs)}")
    for i, m in enumerate(msgs):
        kind = type(m).__name__
        if isinstance(m, AssistantMessage):
            text_parts = [
                c.text for c in m.content if isinstance(c, TextContent)
            ]
            calls = [c for c in m.content if isinstance(c, ToolCall)]
            preview = text_parts[0][:80] if text_parts else ""
            print(
                f"  [{i}] {kind}: text={preview!r} "
                f"calls=[{','.join(c.name for c in calls)}] "
                f"stop={m.stop_reason}"
            )
        elif isinstance(m, ToolResultMessage):
            content_text = "".join(
                c.text for c in m.content if isinstance(c, TextContent)
            )
            print(
                f"  [{i}] {kind}: tool={m.name} "
                f"call_id={m.tool_call_id[:8]}... "
                f"content={content_text[:50]!r}"
            )
        else:
            print(f"  [{i}] {kind}")

    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    tool_calls = [
        c for a in assistants for c in a.content if isinstance(c, ToolCall)
    ]
    e2e_calls = [c for c in tool_calls if c.name == "e2e_probe"]

    if not e2e_calls:
        print(
            f"[smoke] FAIL: model did not call e2e_probe "
            f"(tool_calls={[c.name for c in tool_calls]})"
        )
        return 1

    last = assistants[-1]
    final_text = "".join(
        c.text for c in last.content if isinstance(c, TextContent)
    )
    if PROBE_TOKEN not in final_text:
        print(
            f"[smoke] FAIL: final answer missing token. "
            f"got: {final_text[:200]!r}"
        )
        return 1

    print(f"[smoke] PASS: model called e2e_probe and returned {PROBE_TOKEN}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

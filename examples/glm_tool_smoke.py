"""examples/glm_tool_smoke.py

最小可运行示例：真实 GLM 单工具调用。

如何运行
--------
1. .env 同 glm_text_smoke.py 配置。

2. 运行：

   /d/miniconda/envs/pipy/python.exe examples/glm_tool_smoke.py

预期输出
--------
程序 prompt 让模型调用 echo 工具；打印：
- 每个 ToolCall（id / name / arguments）
- 工具执行结果（ToolResultMessage）
- 最终 assistant 文本

如何判断失败
------------
- 模型不调工具：system_prompt 不够明确——加大 max_tokens / 改 prompt 措辞
- ProviderError：检查凭证 / 网络
- ToolArgumentValidationError：模型生成的 arguments 不符合 schema

finally 中 await harness.close() 关闭 httpx 连接 + 清理 hook。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

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


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = (
        "Echoes back the 'text' argument. "
        "Use this tool whenever the user asks to echo something."
    )
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, tool_call_id: str, args: dict) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id, name="echo",
            content=[TextContent(text=args.get("text", ""))],
        )


async def main() -> None:
    client = GLMClient(max_tokens=512)
    agent = Agent(
        system_prompt=(
            "You are a tool-using demo bot. "
            "When the user asks to echo something, you MUST call the 'echo' tool."
        ),
        client=client,
        tools=ToolRegistry([EchoTool()]),
    )
    harness = AgentHarness(agent)
    try:
        print("[glm_tool_smoke] prompt: echo 'integration-demo'")
        msgs = await harness.run_prompt("Please echo the text: integration-demo")

        for m in msgs:
            if isinstance(m, AssistantMessage):
                for c in m.content:
                    if isinstance(c, ToolCall):
                        print(f"  ToolCall: id={c.id} name={c.name} args={c.arguments}")
                    elif isinstance(c, TextContent):
                        print(f"  AssistantText: {c.text!r}")
            elif isinstance(m, ToolResultMessage):
                text = "".join(c.text for c in m.content)
                print(f"  ToolResult: tool_call_id={m.tool_call_id} text={text!r}")

        print(f"[glm_tool_smoke] {len(msgs)} messages total")
    finally:
        await harness.close()


if __name__ == "__main__":
    if not (
        os.environ.get("GLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    ):
        raise SystemExit(
            "缺凭证：请先在 .env 配置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY"
        )
    asyncio.run(main())

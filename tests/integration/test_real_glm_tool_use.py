"""P1-A3 — 真实 GLM 多轮 tool_use smoke（强制 tool 调用版）。

使用 ``E2EProbeTool`` 返回**固定 token** ``PROBE_OK_ALPHA_7F3A``，模型必须
真实调用工具才能拿到 token，避免“猜答案”造成假阳性。

链路：
    UserMessage
    → GLM stream 输出 ToolCall(name=e2e_probe, arguments={"value": "alpha"})
    → Agent 执行 E2EProbeTool
    → ToolResultMessage(content=[PROBE_OK_ALPHA_7F3A])
    → 回喂 GLM 第二轮
    → 最终 AssistantMessage 文本含 PROBE_OK_ALPHA_7F3A

标记：``slow + integration``（默认 offline suite 自动 skip）。

显式运行：

    $env:PI_RUN_INTEGRATION = "1"
    D:\\miniconda\\envs\\pipy\\python.exe -m pytest \\
        tests/integration/test_real_glm_tool_use.py -v -m "slow and integration"
"""

from __future__ import annotations

import pytest

from pi_agent_core_py import GLMClient
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.context import convert_to_llm
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from pi_agent_core_py.providers import to_anthropic_messages
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult

PROBE_TOKEN = "PROBE_OK_ALPHA_7F3A"


class E2EProbeTool(AgentTool):
    """Probe tool——返回固定 token，模型必须调用才能拿到。"""

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
        # 忽略 args.value 内容——固定 token 防止模型猜测
        return ToolResult(
            tool_call_id=tool_call_id,
            name="e2e_probe",
            content=[TextContent(text=PROBE_TOKEN)],
        )


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_glm_e2e_probe_round_trip(glm_live_config):
    """真实 GLM 必须调用 e2e_probe 才能得到 PROBE_OK_ALPHA_7F3A。

    模型偶发不调工具时允许最多 2 次重试（每次新建 client/harness）。
    两次都不调 → 报告 FAIL，不允许为了让测试通过改 runtime。
    """
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            msgs = await _run_probe_conversation(glm_live_config)
            _assert_probe_round_trip(msgs)
            return
        except AssertionError as e:
            last_err = e
            # 重试前打印摘要便于排查（不打印 key）
            print(f"[attempt {attempt}] assertion failed: {e}")

    assert last_err is None, f"两次尝试模型都未通过 e2e_probe 断言: {last_err}"


async def _run_probe_conversation(config):
    client = GLMClient(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        max_tokens=512,
    )
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
    return msgs


def _assert_probe_round_trip(msgs: list) -> None:
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    assert assistants, "缺 AssistantMessage"

    tool_calls = [c for a in assistants for c in a.content if isinstance(c, ToolCall)]
    e2e_calls = [c for c in tool_calls if c.name == "e2e_probe"]
    assert e2e_calls, f"模型未调用 e2e_probe（tool_calls={[c.name for c in tool_calls]}）"

    for c in e2e_calls:
        assert c.arguments.get("value") == "alpha", (
            f"arguments.value={c.arguments.get('value')!r}，预期 'alpha'"
        )

    results = [m for m in msgs if isinstance(m, ToolResultMessage)]
    assert results, "缺 ToolResultMessage"

    call_ids = {c.id for c in e2e_calls}
    for r in results:
        assert r.tool_call_id in call_ids, (
            f"ToolResult.tool_call_id={r.tool_call_id} 找不到配对 ToolCall; calls={call_ids}"
        )
        assert r.is_error is False, f"ToolResult 报错: {r.content}"

    last = assistants[-1]
    assert last.stop_reason != "error", f"最终 assistant stop_reason=error: {last.error_message}"
    final_text = "".join(c.text for c in last.content if isinstance(c, TextContent))
    assert PROBE_TOKEN in final_text, f"最终 assistant 未包含 {PROBE_TOKEN}: {final_text[:200]}"

    # 验证 messages 序列转 Anthropic 后 tool_use/tool_result 配对合法
    llm_msgs = convert_to_llm(msgs)
    anthropic_msgs = to_anthropic_messages(llm_msgs)
    produced = {
        b["id"]
        for m in anthropic_msgs
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("type") == "tool_use"
    }
    referenced = {
        b["tool_use_id"]
        for m in anthropic_msgs
        if m["role"] == "user"
        for b in m["content"]
        if b.get("type") == "tool_result"
    }
    assert referenced.issubset(produced), (
        f"tool_result 引用了不存在的 tool_use: referenced={referenced} produced={produced}"
    )

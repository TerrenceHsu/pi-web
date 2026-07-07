"""Integration test 3: 真实 GLM 多轮 tool_use smoke。

验证之前修复的核心问题（Step 21 fix）：
    AssistantMessage.content 中的 ToolCall 在 convert_to_llm 后保留
    → LLMAssistantMessage.tool_calls
    → to_anthropic_messages 渲染为 tool_use block
    → 后续 user(tool_result) 能找到配对 tool_use_id

由于真实模型不一定每次都触发多轮 tool_use，本测试分两部分：

1. **deterministic 部分（不依赖模型行为）**：构造已知 messages，跑
   convert_to_llm + to_anthropic_messages，断言输出序列合法。
2. **real-GLM 部分（依赖模型）**：让模型连续调用工具；slow + skip 守卫。
"""
from __future__ import annotations

import os

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
    UserMessage,
)
from pi_agent_core_py.providers import to_anthropic_messages
from pi_agent_core_py.tools import AgentTool, ToolRegistry, ToolResult

_HAS_GLM_CREDS = bool(
    os.environ.get("GLM_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.environ.get("ANTHROPIC_API_KEY")
)
_GLM_SKIP_REASON = (
    "需要至少设置 GLM_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY 之一"
)


# ============================================================================
# Deterministic 部分（无网络 / 无凭证，但 slow 标记便于和 real 一起跑）
# ============================================================================


def _build_multi_turn_agent_messages() -> list:
    """构造多轮 tool_use 序列：assistant(tool_use) → tool_result × 2 → assistant。"""
    return [
        UserMessage(content=[TextContent(text="echo alpha then echo beta")]),
        AssistantMessage(
            content=[
                TextContent(text="I will echo alpha first."),
                ToolCall(id="t1", name="echo", arguments={"text": "alpha"}),
            ],
            api="anthropic-messages", provider="glm", model="glm-4.5-flash",
        ),
        ToolResultMessage(
            tool_call_id="t1", name="echo",
            content=[TextContent(text="alpha")],
        ),
        AssistantMessage(
            content=[
                TextContent(text="Now I will echo beta."),
                ToolCall(id="t2", name="echo", arguments={"text": "beta"}),
            ],
            api="anthropic-messages", provider="glm", model="glm-4.5-flash",
        ),
        ToolResultMessage(
            tool_call_id="t2", name="echo",
            content=[TextContent(text="beta")],
        ),
        AssistantMessage(
            content=[TextContent(text="Done.")],
            api="anthropic-messages", provider="glm", model="glm-4.5-flash",
        ),
    ]


@pytest.mark.slow
def test_anthropic_message_pairs_are_legal_multi_turn():
    """断言：multi-turn tool_use 序列里每个 tool_result 都能找到配对的 tool_use。"""
    msgs = _build_multi_turn_agent_messages()
    llm_msgs = convert_to_llm(msgs)
    anthropic_msgs = to_anthropic_messages(llm_msgs)

    # 收集所有 assistant 消息中的 tool_use_id
    produced_tool_use_ids: set[str] = set()
    for m in anthropic_msgs:
        if m["role"] != "assistant":
            continue
        for block in m["content"]:
            if block.get("type") == "tool_use":
                produced_tool_use_ids.add(block["id"])

    # 收集所有 tool_result block 引用的 tool_use_id
    referenced_tool_use_ids: set[str] = set()
    for m in anthropic_msgs:
        if m["role"] != "user":
            continue
        for block in m["content"]:
            if block.get("type") == "tool_result":
                referenced_tool_use_ids.add(block["tool_use_id"])

    assert produced_tool_use_ids, "assistant 中无 tool_use block —— 修复回归"
    assert referenced_tool_use_ids == produced_tool_use_ids, (
        f"tool_use / tool_result 配对失败: "
        f"produced={produced_tool_use_ids} referenced={referenced_tool_use_ids}"
    )


@pytest.mark.slow
def test_anthropic_assistant_with_tool_use_block_present():
    """断言：含 ToolCall 的 AssistantMessage 转 Anthropic 后，content 含 tool_use block。"""
    msgs = [
        UserMessage(content=[TextContent(text="call echo")]),
        AssistantMessage(
            content=[
                TextContent(text="calling"),
                ToolCall(id="t1", name="echo", arguments={"text": "hi"}),
            ],
            api="x", provider="x", model="x",
        ),
        ToolResultMessage(
            tool_call_id="t1", name="echo",
            content=[TextContent(text="hi")],
        ),
    ]
    llm_msgs = convert_to_llm(msgs)
    anthropic_msgs = to_anthropic_messages(llm_msgs)

    asst = next(m for m in anthropic_msgs if m["role"] == "assistant")
    block_types = [b["type"] for b in asst["content"]]
    assert "tool_use" in block_types


# ============================================================================
# Real-GLM 部分（依赖凭证 + 网络）
# ============================================================================


class EchoTool(AgentTool):
    name = "echo"
    label = "Echo"
    description = (
        "Echoes back the 'text' argument. "
        "Use this tool when the user asks to echo something."
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


real_glm = pytest.mark.skipif(not _HAS_GLM_CREDS, reason=_GLM_SKIP_REASON)


@real_glm
@pytest.mark.slow
@pytest.mark.asyncio
async def test_real_glm_two_echo_turns_preserves_tool_use_pairing():
    """真实 GLM 连续调两次 echo；断言配对不丢。

    注意：模型可能选择一次性把两次 tool_call 放在同一条 assistant message
    （Anthropic 协议允许），也可能是两轮。本测试接受两种路径，
    只断言最终所有 tool_result 都能找到 tool_use 配对（由 convert_to_llm 保证）。
    """
    client = GLMClient(max_tokens=512)
    agent = Agent(
        system_prompt=(
            "You are a tool-using test bot. When the user asks to echo two things "
            "in sequence, call the 'echo' tool twice—once for each. "
            "After both calls return, briefly confirm completion."
        ),
        client=client,
        tools=ToolRegistry([EchoTool()]),
    )
    harness = AgentHarness(agent)
    try:
        msgs = await harness.run_prompt(
            "Please echo 'first-ping' and then echo 'second-ping'."
        )
    finally:
        await harness.close()

    # 至少 2 次 ToolCall + 2 个 ToolResultMessage
    assistants = [m for m in msgs if isinstance(m, AssistantMessage)]
    call_ids_from_msgs: list[str] = []
    for a in assistants:
        call_ids_from_msgs.extend(c.id for c in a.content if isinstance(c, ToolCall))
    results = [m for m in msgs if isinstance(m, ToolResultMessage)]

    # 至少 1 次（模型可能合并）—— 这里宽松：至少 1 次成功 tool round-trip
    assert call_ids_from_msgs, "模型未调用 echo"
    assert results, "缺 ToolResultMessage"

    # 每个 ToolResultMessage.tool_call_id 必须能在 call_ids 里找到
    for r in results:
        assert r.tool_call_id in call_ids_from_msgs, (
            f"ToolResult.tool_call_id={r.tool_call_id} 找不到配对 ToolCall; "
            f"calls={call_ids_from_msgs}"
        )

    # 最终 assistant 非 error
    last = assistants[-1]
    assert last.stop_reason != "error", f"最终 error: {last.error_message}"

    # 验证 messages 序列转换成 Anthropic 后配对合法（修复回归保护）
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
        f"tool_result 引用了不存在的 tool_use: "
        f"referenced={referenced} produced={produced}"
    )

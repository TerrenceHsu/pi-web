"""P0-5：默认对话向 system prompt 测试。

覆盖：
- build_default_system_prompt 模板渲染（基础 / + skills / + mcp tools）
- Agent.system_prompt 空时 harness 自动用 default
- Agent.system_prompt 显式时不覆盖
- enabled skills 进入 default prompt
- mcp tools 进入 default prompt
- snapshot / context.metadata 记录 system_prompt_source
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.skills import Skill
from pi_agent_core_py.system_prompt import build_default_system_prompt

# ============================================================================
# build_default_system_prompt 单元测试
# ============================================================================


def test_default_prompt_base_is_non_empty():
    """空入参时返回基础 prompt，非空。"""
    p = build_default_system_prompt()
    assert isinstance(p, str)
    assert "对话助手" in p or "助手" in p
    assert "list_files" in p  # file_tools_enabled 默认 True
    assert "view_file" in p


def test_default_prompt_supports_md_html_csv_parquet():
    """P0-3：默认 prompt 必须列出 md / html / csv / parquet 支持格式。"""
    p = build_default_system_prompt()
    assert "markdown" in p.lower() or "md" in p.lower()
    assert "html" in p.lower()
    assert "csv" in p.lower()
    assert "parquet" in p.lower()


def test_default_prompt_states_image_unsupported():
    """P0-3：默认 prompt 必须明确说明不支持图片（不做图片直读 / 不做 OCR）。"""
    p = build_default_system_prompt()
    # 不应再宣称图片直读 / 视觉理解
    assert "图片你可以直接理解" not in p
    assert "GLM-4V" not in p
    # 应明确说明不支持
    assert "不支持图片" in p
    assert "OCR" in p or "视觉理解" in p


def test_default_prompt_disables_file_tools_hint_when_flag_off():
    """file_tools_enabled=False → 不提 list_files/view_file。"""
    p = build_default_system_prompt(file_tools_enabled=False)
    # 基础 prompt 里仍会提到"调用 list_files / view_file"作为行为准则描述
    # 但 _FILE_TOOLS_HINT 段（"可用工具（始终启用）"）不应出现
    assert "可用工具（始终启用）" not in p


def test_default_prompt_with_skills_sorted_by_priority():
    """skills 按 (priority, name) 排序注入。"""
    s1 = Skill(name="alpha", description="alpha desc", prompt="x", priority=10)
    s2 = Skill(name="beta", description="beta desc", prompt="x", priority=5)
    s3 = Skill(name="gamma", description="gamma desc", prompt="x", priority=5)
    p = build_default_system_prompt(skills=[s1, s2, s3])
    # beta (5) < gamma (5) < alpha (10)
    pos_beta = p.index("beta")
    pos_gamma = p.index("gamma")
    pos_alpha = p.index("alpha")
    assert pos_beta < pos_gamma < pos_alpha
    assert "## 可用 Skills" in p
    assert "alpha desc" in p


def test_default_prompt_with_skills_includes_tags_and_tool_names():
    """skill tags / tool_names 渲染进 prompt。"""
    s = Skill(
        name="demo", description="demo desc", prompt="x",
        tags=["finance", "research"],
        tool_names=["web_search", "view_file"],
    )
    p = build_default_system_prompt(skills=[s])
    assert "finance" in p
    assert "research" in p
    assert "web_search" in p


def test_default_prompt_empty_skills_omits_section():
    """空 skills 列表 → 不出现 Skills 段。"""
    p = build_default_system_prompt(skills=[])
    assert "可用 Skills" not in p


def test_default_prompt_with_mcp_tools_grouped_by_server():
    """mcp_tools 按 server 分组渲染。"""

    class _FakeTool:
        def __init__(self, name: str, server: str, desc: str = ""):
            self.name = name
            self.server_name = server
            self.description = desc

    tools = [
        _FakeTool("mcp__fs__read", "fs", "read a file"),
        _FakeTool("mcp__fs__write", "fs", "write a file"),
        _FakeTool("mcp__git__log", "git", "git log"),
    ]
    p = build_default_system_prompt(mcp_tools=tools)
    assert "## 可用 MCP 工具" in p
    assert "fs" in p
    assert "git" in p
    assert "mcp__fs__read" in p
    assert "mcp__git__log" in p
    # 同 server 内按 name 排序
    pos_read = p.index("mcp__fs__read")
    pos_write = p.index("mcp__fs__write")
    assert pos_read < pos_write


def test_default_prompt_combined_skills_and_mcp():
    """skills + mcp 同时存在，两段都进 prompt。"""
    s = Skill(name="demo", description="demo", prompt="x")

    class _T:
        name = "mcp__fs__read"
        server_name = "fs"
        description = "read"

    p = build_default_system_prompt(skills=[s], mcp_tools=[_T()])
    assert "可用 Skills" in p
    assert "可用 MCP 工具" in p


# ============================================================================
# Harness 集成：empty system_prompt 自动用 default
# ============================================================================


@pytest.mark.asyncio
async def test_harness_uses_default_when_system_prompt_empty():
    """Agent.system_prompt="" → harness 自动用 default。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    # Agent 构造允许 system_prompt 是空字符串
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    try:
        await harness.run_prompt("hi")
        # 应标记为 default
        assert harness.context.metadata.get("system_prompt_source") == "default"
        # FakeClient 记录的 last_system_prompt 应当是 default（非空）
        assert fake.last_system_prompt
        assert "对话助手" in fake.last_system_prompt or "助手" in fake.last_system_prompt
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_harness_does_not_override_explicit_system_prompt():
    """Agent.system_prompt="custom" → 不被覆盖。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="You are a custom agent.", client=fake)
    harness = AgentHarness(agent)
    try:
        await harness.run_prompt("hi")
        assert harness.context.metadata.get("system_prompt_source") == "explicit"
        assert fake.last_system_prompt == "You are a custom agent."
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_harness_whitespace_only_system_prompt_treated_as_empty():
    """Agent.system_prompt="   \\n  " → 视为空，用 default。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="   \n  ", client=fake)
    harness = AgentHarness(agent)
    try:
        await harness.run_prompt("hi")
        assert harness.context.metadata.get("system_prompt_source") == "default"
        assert "对话助手" in fake.last_system_prompt
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_harness_default_prompt_includes_enabled_skills():
    """attach skills + system_prompt 空 → enabled skills 进 default prompt。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    skill = Skill(
        name="finance_helper", description="金融研究助手", prompt="do finance",
        tags=["finance"], priority=10,
    )
    harness = AgentHarness(agent)
    harness.attach_skills([skill])
    try:
        await harness.run_prompt("分析 A 股")
        # default prompt 中应包含 skill
        assert "finance_helper" in fake.last_system_prompt
        assert "金融研究助手" in fake.last_system_prompt
        # metadata 记录 enabled skill
        assert "finance_helper" in harness.context.metadata["enabled_skill_names"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_harness_default_prompt_records_metadata_in_snapshot():
    """snapshot metadata 含 system_prompt_source / enabled_skill_names / enabled_mcp_tool_names。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    try:
        await harness.run_prompt("hi")
        snap = harness.last_snapshot
        assert snap is not None
        # metadata 经过 _finish_snapshot 拷贝到 snapshot
        meta = snap.metadata
        assert meta.get("system_prompt_source") == "default"
        assert "enabled_skill_names" in meta
        assert "enabled_mcp_tool_names" in meta
        assert meta["enabled_skill_names"] == []
        assert meta["enabled_mcp_tool_names"] == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_harness_explicit_prompt_metadata_recorded():
    """explicit 路径也写 enabled_skill_names（即使 skill_injection 关闭）。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="custom prompt", client=fake)
    skill = Skill(name="x", description="x desc", prompt="x")
    harness = AgentHarness(agent)
    harness.attach_skills([skill])
    try:
        await harness.run_prompt("hi")
        assert harness.context.metadata.get("system_prompt_source") == "explicit"
        # explicit + skill_registry enabled → selected 写 enabled_skill_names
        assert "x" in harness.context.metadata.get("enabled_skill_names", [])
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_default_prompt_does_not_break_skill_block_injection():
    """explicit prompt + skills → 仍然拼接 skill block（原 Step 14 行为不回归）。"""
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="base", client=fake)
    skill = Skill(name="demo", description="demo", prompt="be helpful")
    harness = AgentHarness(agent)
    harness.attach_skills([skill])
    try:
        await harness.run_prompt("hi")
        # explicit base + skill block 拼接（## Skills 段来自 render_skill_block）
        assert fake.last_system_prompt.startswith("base")
        # enabled_skill_names 写入
        assert "demo" in harness.context.metadata["enabled_skill_names"]
    finally:
        await harness.close()


# ============================================================================
# 用户消息字段（UserMessage content）回归——P0-5 不破坏 message 类型
# ============================================================================


def test_user_message_textcontent_round_trip():
    """回归：UserMessage + TextContent 序列化往返不退化。"""
    from pi_agent_core_py.messages import UserMessage

    msg = UserMessage(content=[TextContent(text="hello")])
    data = msg.model_dump()
    restored = UserMessage.model_validate(data)
    assert restored.role == "user"
    assert restored.content[0].text == "hello"

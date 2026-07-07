"""Step 19 — AgentHarness × Skill File Loader × MCP Prompts 集成测试。

覆盖：
- attach_skill_files：注册到 SkillRegistry / metadata["skill_loader"]
- attach_skill_dir：批量目录扫描注册
- replace_existing_registry=True 替换 / False 追加 / 默认 registry 不存在时建新
- attach_mcp_prompts_as_skills：MCP prompts → Skill 注册
- refresh_mcp_prompts：未 attach MCPRegistry 返回 []
- render_system_prompt：能看到加载的 file skill / MCP prompt skill
- session.metadata 通过 context_metadata 看到 skill_loader summary
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    FakeClient,
    MCPClient,
    MCPPromptInfo,
    MCPRegistry,
    MCPServerConfig,
    SessionMemory,
    Skill,
    SkillLoadConfig,
    SkillRegistry,
)
from pi_agent_core_py.mcp import FakeMCPTransport

# ============================================================================
# Fixtures
# ============================================================================


def _make_harness() -> AgentHarness:
    """构造最小 harness（FakeClient agent，无 MCP）。"""
    agent = Agent(
        system_prompt="base-prompt",
        client=FakeClient([]),
        tools=None,
    )
    return AgentHarness(agent)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _make_fake_mcp_registry(handler) -> MCPRegistry:
    """构造一个使用 FakeMCPTransport 的 MCPRegistry（含 prompts）。"""
    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    def factory(c: MCPServerConfig) -> MCPClient:
        return MCPClient(c, transport=FakeMCPTransport(handler=handler))
    return MCPRegistry([cfg], client_factory=factory)


def _prompts_handler():
    async def h(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": "summarize", "description": "Summarize text"},
            ]}}
        if method == "prompts/get":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "description": "summarize",
                "messages": [
                    {"role": "system", "content": "be concise"},
                    {"role": "user", "content": "summarize this"},
                ],
            }}
        return {"jsonrpc": "2.0", "id": req_id, "error": {
            "code": -32601, "message": "nf",
        }}
    return h


# ============================================================================
# attach_skill_files
# ============================================================================


@pytest.mark.asyncio
async def test_attach_skill_files_creates_new_registry(tmp_path: Path) -> None:
    p1 = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    p2 = _write(tmp_path / "b" / "SKILL.md", "# B\n## Description\ndb\n## Instructions\nib\n")
    harness = _make_harness()
    loaded = harness.attach_skill_files([str(p1), str(p2)])
    assert {s.name for s in loaded} == {"A", "B"}
    assert isinstance(harness.skill_registry, SkillRegistry)
    assert set(harness.skill_registry.names()) == {"A", "B"}


@pytest.mark.asyncio
async def test_attach_skill_files_metadata_records_names(tmp_path: Path) -> None:
    p = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    harness.attach_skill_files([str(p)])
    bucket = harness.context.metadata["skill_loader"]
    assert bucket["file_skills"]["count"] == 1
    assert bucket["file_skills"]["names"] == ["A"]
    # mcp_prompt_skills 不应该被这次调用写入
    assert "mcp_prompt_skills" not in bucket


@pytest.mark.asyncio
async def test_attach_skill_files_append_to_existing_registry(tmp_path: Path) -> None:
    p1 = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    # 手动 attach 一个初始 skill
    harness.attach_skills([Skill(
        name="seed", description="seed", prompt="seed-prompt",
    )])
    harness.attach_skill_files([str(p1)], replace_existing_registry=False)
    assert set(harness.skill_registry.names()) == {"seed", "A"}


@pytest.mark.asyncio
async def test_attach_skill_files_replace_existing_registry(tmp_path: Path) -> None:
    p1 = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    harness.attach_skills([Skill(name="seed", description="seed", prompt="seed-prompt")])
    harness.attach_skill_files([str(p1)], replace_existing_registry=True)
    assert set(harness.skill_registry.names()) == {"A"}


# ============================================================================
# attach_skill_dir
# ============================================================================


@pytest.mark.asyncio
async def test_attach_skill_dir_recursive(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    _write(tmp_path / "b" / "sub" / "SKILL.md", "# B\n## Description\ndb\n## Instructions\nib\n")
    harness = _make_harness()
    loaded = harness.attach_skill_dir(str(tmp_path), recursive=True)
    assert {s.name for s in loaded} == {"A", "B"}
    assert set(harness.skill_registry.names()) == {"A", "B"}


@pytest.mark.asyncio
async def test_attach_skill_dir_passes_loader_config(tmp_path: Path) -> None:
    """loader_config 中 default_priority 应该生效到加载的 Skill 上。"""
    _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    harness.attach_skill_dir(
        str(tmp_path),
        loader_config=SkillLoadConfig(default_priority=99),
    )
    assert harness.skill_registry.get("A").priority == 99


# ============================================================================
# render_system_prompt with loaded skills
# ============================================================================


@pytest.mark.asyncio
async def test_render_system_prompt_includes_file_skill(tmp_path: Path) -> None:
    p = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    harness.attach_skill_files([str(p)])
    rendered = harness.render_system_prompt()
    assert "Skills" in rendered
    assert "### A" in rendered
    assert "ia" in rendered


# ============================================================================
# MCP prompts integration
# ============================================================================


@pytest.mark.asyncio
async def test_refresh_mcp_prompts_no_registry_returns_empty() -> None:
    harness = _make_harness()
    result = await harness.refresh_mcp_prompts()
    assert result == []


@pytest.mark.asyncio
async def test_refresh_mcp_prompts_with_registry() -> None:
    reg = _make_fake_mcp_registry(_prompts_handler())
    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    infos = await harness.refresh_mcp_prompts()
    assert len(infos) == 1
    assert infos[0][0] == "srv"
    assert isinstance(infos[0][1], MCPPromptInfo)
    await harness.close()


@pytest.mark.asyncio
async def test_attach_mcp_prompts_as_skills_no_registry_returns_empty() -> None:
    harness = _make_harness()
    loaded = await harness.attach_mcp_prompts_as_skills()
    assert loaded == []


@pytest.mark.asyncio
async def test_attach_mcp_prompts_as_skills_basic() -> None:
    reg = _make_fake_mcp_registry(_prompts_handler())
    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    loaded = await harness.attach_mcp_prompts_as_skills()
    assert len(loaded) == 1
    assert loaded[0].name == "mcp_prompt__srv__summarize"
    bucket = harness.context.metadata["skill_loader"]
    assert bucket["mcp_prompt_skills"]["count"] == 1
    assert bucket["mcp_prompt_skills"]["names"] == ["mcp_prompt__srv__summarize"]
    await harness.close()


@pytest.mark.asyncio
async def test_attach_mcp_prompts_does_not_unregister_mcp_tools() -> None:
    """attach MCP prompts 不应该影响 MCP tools。"""
    # 用一个同时暴露 tools 和 prompts 的 handler
    async def handler(msg):
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "echo", "description": "d", "inputSchema": {"type": "object"}},
            ]}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": "summarize", "description": ""},
            ]}}
        if method == "prompts/get":
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "messages": [{"role": "user", "content": "x"}],
            }}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    def factory(c):
        return MCPClient(c, transport=FakeMCPTransport(handler=handler))
    reg = MCPRegistry([cfg], client_factory=factory)

    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    tools_before = harness.list_registered_mcp_tool_names()
    assert "mcp__srv__echo" in tools_before
    # 现在加载 prompts
    await harness.attach_mcp_prompts_as_skills()
    tools_after = harness.list_registered_mcp_tool_names()
    assert tools_before == tools_after
    await harness.close()


@pytest.mark.asyncio
async def test_attach_mcp_prompts_records_errors_in_metadata() -> None:
    """P1 修订：单个 prompt 加载失败应进入 metadata["skill_loader"]["mcp_prompt_errors"]。"""
    async def handler(msg):
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": [
                {"name": "ok", "description": "ok"},
                {"name": "bad", "description": "bad"},
            ]}}
        if method == "prompts/get":
            params = msg.get("params", {})
            name = params.get("name")
            if name == "bad":
                return {"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32603, "message": "boom",
                }}
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "messages": [{"role": "user", "content": "ok"}],
            }}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    def factory(c):
        return MCPClient(c, transport=FakeMCPTransport(handler=handler))
    reg = MCPRegistry([cfg], client_factory=factory)

    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    loaded = await harness.attach_mcp_prompts_as_skills()
    # ok 加载成功，bad 失败
    assert len(loaded) == 1
    assert loaded[0].name == "mcp_prompt__srv__ok"

    bucket = harness.context.metadata["skill_loader"]
    assert bucket["mcp_prompt_skills"]["count"] == 1
    # P1 修订：失败信息进入 metadata
    errs = bucket["mcp_prompt_errors"]
    assert errs["count"] == 1
    assert "srv/bad" in errs["prompts"]
    assert errs["first_error"] is not None
    # P1 修订（本轮）：完整错误列表挂在 harness 上
    assert len(harness.last_mcp_prompt_skill_errors) == 1
    full_err = harness.last_mcp_prompt_skill_errors[0]
    assert full_err["server"] == "srv"
    assert full_err["prompt"] == "bad"
    await harness.close()


@pytest.mark.asyncio
async def test_attach_mcp_prompts_preserves_connected_on_method_not_found() -> None:
    """P0 修订：server 不支持 prompts（method not found）时不应被 disconnect。

    验证：attach_mcp_prompts_as_skills 调用后，server 仍 connected，
    且 tools cache 仍可用（虽然 prompts 是空）。
    """
    async def handler(msg):
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
                {"name": "echo", "description": "d", "inputSchema": {"type": "object"}},
            ]}}
        if method == "prompts/list":
            # server 不支持 prompts capability
            return {"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32601, "message": "method not found: prompts/list",
            }}
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "nf"}}

    cfg = MCPServerConfig(name="srv", transport="stdio", command="x")
    def factory(c):
        return MCPClient(c, transport=FakeMCPTransport(handler=handler))
    reg = MCPRegistry([cfg], client_factory=factory)

    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    # attach MCP prompts——server 不支持，应该返回空 + 不抛
    loaded = await harness.attach_mcp_prompts_as_skills()
    assert loaded == []

    # server 仍 connected；tools 仍可用
    state = harness.list_mcp_servers()[0]
    assert state.connected is True
    assert harness.list_registered_mcp_tool_names() == ["mcp__srv__echo"]
    await harness.close()


@pytest.mark.asyncio
async def test_render_system_prompt_includes_mcp_prompt_skill() -> None:
    reg = _make_fake_mcp_registry(_prompts_handler())
    harness = _make_harness()
    await harness.attach_mcp_servers([], registry=reg)
    await harness.attach_mcp_prompts_as_skills()
    rendered = harness.render_system_prompt()
    assert "### mcp_prompt__srv__summarize" in rendered
    assert "summarize this" in rendered
    await harness.close()


# ============================================================================
# Skill loader metadata 不含完整 prompt body（信息脱敏）
# ============================================================================


@pytest.mark.asyncio
async def test_skill_loader_metadata_only_records_summary(tmp_path: Path) -> None:
    """metadata["skill_loader"] 不应该含完整 SKILL.md 内容。"""
    p = _write(
        tmp_path / "a" / "SKILL.md",
        "# A\n## Description\nda\n## Instructions\nSECRET-MARKER-12345\n",
    )
    harness = _make_harness()
    harness.attach_skill_files([str(p)])
    bucket = harness.context.metadata["skill_loader"]
    blob = repr(bucket)
    assert "SECRET-MARKER-12345" not in blob
    assert bucket["file_skills"]["names"] == ["A"]


# ============================================================================
# Session metadata 自动捕获 skill_loader summary
# ============================================================================


@pytest.mark.asyncio
async def test_session_metadata_captures_skill_loader(tmp_path: Path) -> None:
    """session.metadata["harness"]["context_metadata"]["skill_loader"] 应该可见。"""
    p = _write(tmp_path / "a" / "SKILL.md", "# A\n## Description\nda\n## Instructions\nia\n")
    harness = _make_harness()
    harness.attach_session(SessionMemory())
    harness.attach_skill_files([str(p)])
    # sync_session_metadata 把 context.metadata 复制到 session
    harness.sync_session_metadata()
    harness_md = harness.session.state.metadata["harness"]
    ctx_md = harness_md["context_metadata"]
    assert ctx_md["skill_loader"]["file_skills"]["count"] == 1

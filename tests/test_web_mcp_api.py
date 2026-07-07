"""P0-4 Step 2: Web MCP management API.

覆盖：
- GET /api/mcp/servers 初始为空
- POST /api/skills/servers 成功；duplicate name → 409
- POST invalid name / empty command / bad args / bad env → 400
- env value **绝对不能**出现在任何 response（强校验）
- POST /api/mcp/servers/{name}/test 成功（用 fake stdio MCP server）
- test connection 不污染 harness 当前已启用 server
- test connection 失败 → 502
- POST /api/mcp/servers/{name}/enable / disable
- enable 后 GET /api/mcp/tools 能看到 tools
- disable 后 GET /api/mcp/tools 为空
- DELETE /api/mcp/servers/{name} 成功 + 清理 disabled_mcp_tools 孤儿
- DELETE missing → 404
- POST /api/mcp/tools/{tool_name}/enable / disable
- disabled tool 后 GET /api/mcp/tools enabled=false
- unknown tool enable/disable → 404
- tool disable 真实生效（不在 agent.tools 中）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app

# 不标 slow——33 个测试用 FakeClient + tests/fixtures/fake_mcp_stdio_server.py
# 子进程，单跑仅 2.5s（~75ms/test），完全可以默认运行。覆盖 web/app.py 的
# MCP server CRUD / test / enable-disable / tools enable-disable 大段代码。


_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"


def _fake_server_command() -> list[str]:
    """启动 fake MCP stdio server——跨平台用 sys.executable。"""
    return [sys.executable, str(_FIXTURE)]


def _make_harness() -> AgentHarness:
    scripts = [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    return AgentHarness(agent)


@pytest.fixture
def web_client():
    """独立 harness / app / TestClient，触发 lifespan。"""
    harness = _make_harness()
    app = create_app(harness)
    with TestClient(app) as client:
        try:
            yield client, harness, app
        finally:
            pass
    dispose_app(app)


# ============================================================================
# helpers
# ============================================================================


def _add_server(
    client: TestClient,
    *,
    name: str = "fake",
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    enabled: bool = False,
):
    """POST /api/mcp/servers。默认用 fake stdio server 命令。

    注意：command=None 时才用默认；显式传 "" / falsy 时尊重调用方意图
    （用于测试空 command → 400）。
    """
    if command is None:
        command = sys.executable
    payload: dict = {
        "name": name,
        "command": command,
        "args": args if args is not None else [str(_FIXTURE)],
        "env": env or {},
        "enabled": enabled,
    }
    return client.post("/api/mcp/servers", json=payload)


# ============================================================================
# 1-3: GET / POST /api/mcp/servers
# ============================================================================


def test_get_servers_initial_empty(web_client):
    """GET /api/mcp/servers 初始为空。"""
    client, _, _ = web_client
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["servers"] == []


def test_post_server_success(web_client):
    """POST /api/mcp/servers 成功；enabled=False 时不 attach。"""
    client, harness, _ = web_client
    r = _add_server(client, env={"API_KEY": "secret_value_xyz"})
    assert r.status_code == 200, r.text
    server = r.json()
    assert server["name"] == "fake"
    assert server["enabled"] is False
    assert server["tool_count"] == 0
    # env_keys 不含 value
    assert server["env_keys"] == ["API_KEY"]
    # harness 未 attach——mcp_registry 为 None
    assert harness.mcp_registry is None


def test_post_server_duplicate_returns_409(web_client):
    """重名 → 409。"""
    client, _, _ = web_client
    r1 = _add_server(client, name="fake")
    assert r1.status_code == 200
    r2 = _add_server(client, name="fake")
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_post_server_invalid_name_returns_400(web_client):
    """name 含非法字符 → 400。"""
    client, _, _ = web_client
    r = _add_server(client, name="fake server")  # 空格非法
    assert r.status_code == 400


def test_post_server_empty_command_returns_400(web_client):
    """command 空 → 400。"""
    client, _, _ = web_client
    r = _add_server(client, command="")
    assert r.status_code == 400


def test_post_server_bad_args_returns_400(web_client):
    """args 不是 list[str] → 400。"""
    client, _, _ = web_client
    r = _add_server(client, args="not-a-list")
    assert r.status_code == 400


def test_post_server_bad_env_value_type_returns_400(web_client):
    """env value 不是 str → 400。"""
    client, _, _ = web_client
    r = _add_server(client, env={"KEY": 123})
    assert r.status_code == 400


def test_post_server_env_value_never_in_response(web_client):
    """env value 绝不进入任何 response text。"""
    client, _, _ = web_client
    secret = "SUPER_SECRET_VALUE_4242_xyz"
    r = _add_server(client, env={"API_TOKEN": secret})
    assert r.status_code == 200
    # 强校验：response text 不含 secret
    assert secret not in r.text
    # GET list 也不应泄露
    r2 = client.get("/api/mcp/servers")
    assert secret not in r2.text
    # GET /api/mcp 也不应泄露
    r3 = client.get("/api/mcp")
    assert secret not in r3.text


def test_get_servers_returns_added_server(web_client):
    """GET /api/mcp/servers 返回已添加 server。"""
    client, _, _ = web_client
    _add_server(client, name="srv1")
    _add_server(client, name="srv2")
    r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["servers"]]
    assert set(names) == {"srv1", "srv2"}


# ============================================================================
# 4: POST /api/mcp/servers/{name}/test
# ============================================================================


def test_test_connection_success(web_client):
    """test connection 用 fake stdio server 成功——返回 echo tool。"""
    client, _, _ = web_client
    _add_server(client, name="fake")
    r = client.post("/api/mcp/servers/fake/test")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["server"] == "fake"
    tool_names = [t["name"] for t in data["tools"]]
    assert "echo" in tool_names


def test_test_connection_does_not_pollute_harness(web_client):
    """test connection 不污染 harness 当前已启用 server。

    场景：先 enable server A；再 test connection 同一 server——不应让 harness
    中的 client 数量增加。
    """
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    # 此时 harness.mcp_registry 应该有 1 个 server
    assert harness.mcp_registry is not None
    servers_before = harness.mcp_registry.list_servers()
    assert len(servers_before) == 1

    # test connection——不应增加 client
    r = client.post("/api/mcp/servers/fake/test")
    assert r.status_code == 200
    # 再次确认 harness.mcp_registry 没有被新增 client
    servers_after = harness.mcp_registry.list_servers()
    assert len(servers_after) == 1


def test_test_connection_failure_returns_502(web_client):
    """test connection 失败 → 502 + 清晰 error。"""
    client, _, _ = web_client
    # 启动一个不存在的命令——一定失败
    _add_server(
        client,
        name="bad",
        command="this_command_does_not_exist_xyz",
        args=[],
    )
    r = client.post("/api/mcp/servers/bad/test")
    assert r.status_code == 502
    data = r.json()
    assert data["ok"] is False
    assert "error" in data
    # 错误信息不应泄露 env value（这里没设，但仍校验）
    assert "server" in data


def test_test_connection_missing_server_returns_404(web_client):
    """test 不存在的 server → 404。"""
    client, _, _ = web_client
    r = client.post("/api/mcp/servers/no_such/test")
    assert r.status_code == 404


def test_test_connection_does_not_update_state(web_client):
    """test connection 不写 state（last_error / tool_count 不变）。"""
    client, _, _ = web_client
    _add_server(client, name="fake")
    # 初始 last_error=None tool_count=0
    before = client.get("/api/mcp/servers").json()["servers"][0]
    assert before["last_error"] is None
    assert before["tool_count"] == 0
    # test connection 成功
    r = client.post("/api/mcp/servers/fake/test")
    assert r.status_code == 200
    # state 不变
    after = client.get("/api/mcp/servers").json()["servers"][0]
    assert after["last_error"] is None
    assert after["tool_count"] == 0  # 没写到 state


# ============================================================================
# 5: enable / disable
# ============================================================================


def test_enable_server_success(web_client):
    """enable server 成功；harness.mcp_registry 有 client。"""
    client, harness, _ = web_client
    _add_server(client, name="fake")
    r = client.post("/api/mcp/servers/fake/enable")
    assert r.status_code == 200, r.text
    server = r.json()
    assert server["enabled"] is True
    assert server["tool_count"] >= 1  # echo tool
    # harness 已 attach
    assert harness.mcp_registry is not None
    assert any(s.name == "fake" for s in harness.mcp_registry.list_servers())


def test_enable_server_tools_visible(web_client):
    """enable 后 GET /api/mcp/tools 看到 tools + enabled=True。"""
    client, _, _ = web_client
    _add_server(client, name="fake")
    client.post("/api/mcp/servers/fake/enable")
    r = client.get("/api/mcp/tools")
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert any(t["name"] == "mcp__fake__echo" for t in tools)
    echo = next(t for t in tools if t["name"] == "mcp__fake__echo")
    assert echo["enabled"] is True


def test_disable_server_success(web_client):
    """disable server 后 harness detach；tools 列表为空。"""
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    # 确认已 attach
    assert harness.mcp_registry is not None
    r = client.post("/api/mcp/servers/fake/disable")
    assert r.status_code == 200, r.text
    server = r.json()
    assert server["enabled"] is False
    assert server["tool_count"] == 0
    # GET /api/mcp/tools 应为空
    r2 = client.get("/api/mcp/tools")
    assert r2.json()["count"] == 0


def test_enable_missing_server_returns_404(web_client):
    client, _, _ = web_client
    r = client.post("/api/mcp/servers/no_such/enable")
    assert r.status_code == 404


def test_disable_missing_server_returns_404(web_client):
    client, _, _ = web_client
    r = client.post("/api/mcp/servers/no_such/disable")
    assert r.status_code == 404


# ============================================================================
# 6: DELETE /api/mcp/servers/{name}
# ============================================================================


def test_delete_server_success(web_client):
    """DELETE 已存在的 server 成功。"""
    client, _, _ = web_client
    _add_server(client, name="fake")
    r = client.delete("/api/mcp/servers/fake")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    # GET 应不再返回
    r2 = client.get("/api/mcp/servers")
    assert all(s["name"] != "fake" for s in r2.json()["servers"])


def test_delete_enabled_server_first_disables(web_client):
    """DELETE enabled server 时先 disable 释放 transport。"""
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    assert harness.mcp_registry is not None
    r = client.delete("/api/mcp/servers/fake")
    assert r.status_code == 200
    # detach 后 mcp_registry 应为 None
    assert harness.mcp_registry is None


def test_delete_missing_returns_404(web_client):
    client, _, _ = web_client
    r = client.delete("/api/mcp/servers/no_such")
    assert r.status_code == 404


def test_delete_server_cleans_disabled_tools(web_client):
    """DELETE 时清理 disabled_mcp_tools 中该 server 的孤儿。"""
    client, _, _ = web_client
    _add_server(client, name="fake", enabled=True)
    # disable 一个 tool——加入 disabled_mcp_tools
    r = client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert r.status_code == 200
    # 删除 server
    r2 = client.delete("/api/mcp/servers/fake")
    assert r2.status_code == 200
    cleaned = r2.json().get("cleaned_disabled_tools", [])
    assert "mcp__fake__echo" in cleaned
    # 重新添加同名 server——之前 disabled 设置不应残留
    _add_server(client, name="fake", enabled=True)
    # /api/mcp/tools 应该全部 enabled=True
    r3 = client.get("/api/mcp/tools")
    for t in r3.json()["tools"]:
        if t["name"] == "mcp__fake__echo":
            assert t["enabled"] is True


# ============================================================================
# 7: POST /api/mcp/tools/{tool_name}/enable | disable
# ============================================================================


def test_disable_tool_marks_and_removes_from_registry(web_client):
    """disable tool：标记 + 真实从 agent.tools 移除。"""
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    # 确认工具在 agent.tools 中
    assert harness.agent.tools.has("mcp__fake__echo")
    r = client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # 真实从 agent.tools 移除
    assert not harness.agent.tools.has("mcp__fake__echo")


def test_disable_then_enable_tool_restores(web_client):
    """disable 后 enable：工具重新注册到 agent.tools。"""
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert not harness.agent.tools.has("mcp__fake__echo")
    r = client.post("/api/mcp/tools/mcp__fake__echo/enable")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    # 重新注册到 agent.tools
    assert harness.agent.tools.has("mcp__fake__echo")


def test_disabled_tool_shows_enabled_false_in_list(web_client):
    """disable 后 GET /api/mcp/tools 中 enabled=False。"""
    client, _, _ = web_client
    _add_server(client, name="fake", enabled=True)
    client.post("/api/mcp/tools/mcp__fake__echo/disable")
    r = client.get("/api/mcp/tools")
    tools = r.json()["tools"]
    echo = next(t for t in tools if t["name"] == "mcp__fake__echo")
    assert echo["enabled"] is False


def test_disable_tool_invalid_name_returns_400(web_client):
    """tool_name 不是 mcp__ 全名 → 400。"""
    client, _, _ = web_client
    r = client.post("/api/mcp/tools/just_a_name/disable")
    assert r.status_code == 400


def test_enable_tool_unknown_returns_404(web_client):
    """enable 不存在的 tool → 404。"""
    client, _, _ = web_client
    _add_server(client, name="fake", enabled=True)
    r = client.post("/api/mcp/tools/mcp__fake__no_such_tool/enable")
    assert r.status_code == 404


def test_enable_tool_server_disabled_returns_409(web_client):
    """enable tool 但 server disabled → 409。"""
    client, _, _ = web_client
    _add_server(client, name="fake", enabled=False)
    r = client.post("/api/mcp/tools/mcp__fake__echo/enable")
    assert r.status_code == 409


def test_disable_tool_idempotent(web_client):
    """disable tool 幂等——重复 disable 不报错。"""
    client, _, _ = web_client
    _add_server(client, name="fake", enabled=True)
    r1 = client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert r1.status_code == 200
    r2 = client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert r2.status_code == 200


def test_disable_filter_survives_server_refresh(web_client):
    """disable 后重新 enable server → 过滤仍生效（关键回归点）。"""
    client, harness, _ = web_client
    _add_server(client, name="fake", enabled=True)
    client.post("/api/mcp/tools/mcp__fake__echo/disable")
    # disable server（全量 refresh）
    client.post("/api/mcp/servers/fake/disable")
    # 重新 enable server——attach 后应再次过滤 disabled tool
    client.post("/api/mcp/servers/fake/enable")
    # 工具应仍处于 disabled 状态
    assert not harness.agent.tools.has("mcp__fake__echo")
    r = client.get("/api/mcp/tools")
    echo = next(t for t in r.json()["tools"] if t["name"] == "mcp__fake__echo")
    assert echo["enabled"] is False


# ============================================================================
# 8: 旧 /api/mcp 不破坏
# ============================================================================


def test_legacy_get_mcp_still_works(web_client):
    """旧 GET /api/mcp 在 server 未 attach 时返回 attached=False。"""
    client, _, _ = web_client
    r = client.get("/api/mcp")
    assert r.status_code == 200
    data = r.json()
    assert "attached" in data
    assert "servers" in data
    assert "tools" in data


def test_legacy_get_mcp_tools_still_works(web_client):
    """旧 GET /api/mcp/tools 兼容。"""
    client, _, _ = web_client
    r = client.get("/api/mcp/tools")
    assert r.status_code == 200
    assert "tools" in r.json()

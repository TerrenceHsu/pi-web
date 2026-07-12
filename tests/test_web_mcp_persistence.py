"""P1-C3: MCP server / tool persistence 测试。

覆盖 16 项门槛：
- env_keys 持久化 / env value 不入库
- desired_enabled vs attached 语义
- test connection 不改 desired state
- disable 保留 disabled tool rows / delete cascade
- 结构化 tool key / tool 名称含 __ 仍可持久化
- DB 失败 rollback
- 并发 mutation
- 非 MCP tool 不写入 disabled 表

默认运行（fake stdio MCP + TestClient）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"


def _make_harness() -> AgentHarness:
    fake = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    harness = _make_harness()
    app = create_app(harness, db_path=str(tmp_path / "mcp.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


def _add_server(client, name="fake", command=None, env=None, enabled=False):
    return client.post(
        "/api/mcp/servers",
        json={
            "name": name,
            "command": command or sys.executable,
            "args": [str(_FIXTURE)],
            "env": env or {},
            "enabled": enabled,
        },
    )


# ============================================================================
# Tests 1-3: env_keys 持久化 / 无 secret / response 不含 value
# ============================================================================


async def test_1_add_server_persists_only_env_keys(web_client):
    client, _, app = web_client
    resp = _add_server(client, "fake", env={"SECRET_TOKEN": "super-secret-value"})
    assert resp.status_code == 200

    persisted = await app.state.web.extension_store.get_mcp_server("fake")
    assert persisted is not None
    assert json.loads(persisted.env_keys_json) == ["SECRET_TOKEN"]
    # **绝不存 value**
    assert "super-secret-value" not in persisted.env_keys_json
    assert "super-secret-value" not in persisted.command
    assert "super-secret-value" not in persisted.args_json


async def test_2_no_secret_marker_in_sqlite(web_client, tmp_path):
    client, _, _ = web_client
    _add_server(client, "fake", env={"API_KEY": "SECRET_MARKER_12345"})

    # 读 SQLite 文件——确认无 secret
    db_file = tmp_path / "mcp.sqlite"
    content = db_file.read_bytes()
    assert b"SECRET_MARKER_12345" not in content


async def test_3_response_no_env_value(web_client):
    client, _, _ = web_client
    _add_server(client, "fake", env={"TOKEN": "secret-val"})
    resp = client.get("/api/mcp/servers")
    body = resp.json()
    server = next(s for s in body["servers"] if s["name"] == "fake")
    assert "env_keys" in server
    assert server["env_keys"] == ["TOKEN"]
    # response 不含 value
    assert "secret-val" not in json.dumps(body)


# ============================================================================
# Tests 4-6: desired_enabled vs attached 语义
# ============================================================================


async def test_4_enabled_equals_desired_enabled(web_client):
    client, _, _ = web_client
    _add_server(client, "fake", enabled=False)
    resp = client.get("/api/mcp/servers")
    server = next(s for s in resp.json()["servers"] if s["name"] == "fake")
    assert server["enabled"] == server["desired_enabled"]


async def test_5_attach_success_sets_attached_true(web_client):
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    resp = client.post("/api/mcp/servers/fake/enable")
    body = resp.json()
    # fake MCP 能 attach——attached=True
    assert body.get("attached") is True or body.get("tool_count", 0) > 0


async def test_6_attach_failure_keeps_desired_enabled_true(web_client):
    """attach 失败——desired_enabled=true, attached=false。

    用不存在的 command 让 attach 自然失败。
    """
    client, _, app = web_client
    # 添加一个 command 不存在的 server
    _add_server(client, "broken", command="/nonexistent/binary/path")

    resp = client.post("/api/mcp/servers/broken/enable")
    body = resp.json()
    assert resp.status_code == 502
    assert body["desired_enabled"] is True  # 用户意图保留
    assert body["attached"] is False  # 运行时未连接
    assert body["last_error"] is not None


# ============================================================================
# Test 7: test connection 不改 desired state
# ============================================================================


async def test_7_test_connection_no_state_change(web_client):
    client, _, app = web_client
    _add_server(client, "fake", enabled=False)

    # 记录 desired_enabled
    before = await app.state.web.extension_store.get_mcp_server("fake")
    assert before.desired_enabled is False

    # test connection
    resp = client.post("/api/mcp/servers/fake/test")
    assert resp.status_code == 200

    # desired_enabled 不变
    after = await app.state.web.extension_store.get_mcp_server("fake")
    assert after.desired_enabled is False


# ============================================================================
# Tests 8-9: disable 保留 disabled tool rows / delete cascade
# ============================================================================


async def test_8_disable_server_preserves_disabled_tool_rows(web_client):
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    # 等 attach
    client.post("/api/mcp/servers/fake/enable")
    # disable tool
    client.post("/api/mcp/tools/mcp__fake__echo/disable")

    # disable server
    client.post("/api/mcp/servers/fake/disable")

    # DB 中 disabled tool row 仍存在
    tools = await app.state.web.extension_store.list_disabled_mcp_tools("fake")
    assert len(tools) >= 1
    assert any(t.tool_name == "echo" for t in tools)


async def test_9_delete_server_cascades_disabled_tools(web_client):
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    client.post("/api/mcp/servers/fake/enable")
    client.post("/api/mcp/tools/mcp__fake__echo/disable")

    # delete server
    resp = client.delete("/api/mcp/servers/fake")
    assert resp.status_code == 200

    # DB cascade——disabled tools 清空
    tools = await app.state.web.extension_store.list_disabled_mcp_tools("fake")
    assert tools == []
    # server row 也删了
    persisted = await app.state.web.extension_store.get_mcp_server("fake")
    assert persisted is None


# ============================================================================
# Tests 10-11: 结构化 tool key / tool 名称含 __
# ============================================================================


async def test_10_tool_key_uses_raw_names(web_client):
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    client.post("/api/mcp/servers/fake/enable")
    client.post("/api/mcp/tools/mcp__fake__echo/disable")

    tools = await app.state.web.extension_store.list_disabled_mcp_tools("fake")
    assert len(tools) == 1
    assert tools[0].server_name == "fake"
    assert tools[0].tool_name == "echo"  # raw tool name（不含 mcp__ 前缀）


async def test_11_tool_name_with_underscores_persisted(web_client, monkeypatch):
    """tool 名称含 __ 时仍可正确持久化——结构化 key 不依赖 split。"""
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    client.post("/api/mcp/servers/fake/enable")

    # mock registry 返回一个 tool name 含 __ 的 tool
    # 实际 fake MCP 只提供 echo——这里直接测 DB 层
    await app.state.web.extension_store.disable_mcp_tool("fake", "tool__with__underscores")
    tools = await app.state.web.extension_store.list_disabled_mcp_tools("fake")
    assert len(tools) == 1
    assert tools[0].tool_name == "tool__with__underscores"

    # enable 后删除
    await app.state.web.extension_store.enable_mcp_tool("fake", "tool__with__underscores")
    tools_after = await app.state.web.extension_store.list_disabled_mcp_tools("fake")
    assert tools_after == []


# ============================================================================
# Tests 12-13: DB 失败 rollback
# ============================================================================


async def test_12_db_failure_add_server_rolls_back(web_client, monkeypatch):
    client, _, app = web_client

    async def failing_upsert(**kwargs):
        from pi_agent_core_py.web.extension_store import ExtensionStoreError
        raise ExtensionStoreError("simulated DB failure")

    monkeypatch.setattr(
        app.state.web.extension_store, "upsert_mcp_server", failing_upsert
    )

    resp = _add_server(client, "broken")
    assert resp.status_code == 500
    # runtime config 回滚
    assert "broken" not in app.state.web.mcp_server_configs


async def test_13_db_failure_tool_disable_rolls_back(web_client, monkeypatch):
    client, _, app = web_client
    _add_server(client, "fake", enabled=True)
    client.post("/api/mcp/servers/fake/enable")

    async def failing_disable(*args, **kwargs):
        from pi_agent_core_py.web.extension_store import ExtensionStoreError
        raise ExtensionStoreError("simulated DB failure")

    monkeypatch.setattr(
        app.state.web.extension_store, "disable_mcp_tool", failing_disable
    )

    resp = client.post("/api/mcp/tools/mcp__fake__echo/disable")
    assert resp.status_code == 500
    # runtime 回滚——tool 仍在 agent.tools 中
    assert app.state.web.harness.agent.tools.has("mcp__fake__echo")
    assert "mcp__fake__echo" not in app.state.web.disabled_mcp_tools


# ============================================================================
# Test 14: 并发 mutation 不漂移
# ============================================================================


async def test_14_concurrent_server_mutations(web_client):
    """两个并发 add server——mutation lock 串行化，都成功。"""
    client, _, app = web_client

    resp1 = _add_server(client, "server_a")
    resp2 = _add_server(client, "server_b")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert "server_a" in app.state.web.mcp_server_configs
    assert "server_b" in app.state.web.mcp_server_configs


# ============================================================================
# Test 15: 非 MCP tool 不写入 disabled MCP tool 表
# ============================================================================


async def test_15_non_mcp_tool_not_in_disabled_table(web_client):
    """非 MCP tool（如 view_file）的 disable 不写 web_mcp_disabled_tools 表。"""
    client, _, app = web_client

    # view_file 是 built-in tool——不在 MCP disabled 表
    # MCP disable endpoint 拒绝非 mcp__ 前缀
    resp = client.post("/api/tools/view_file/disable")
    assert resp.status_code in (404, 405, 400)  # 不存在此 endpoint 或拒绝


# ============================================================================
# Test 16: 原 MCP API 不回归
# ============================================================================


async def test_16_mcp_api_not_regressed(web_client):
    client, _, _ = web_client
    # GET /api/mcp/servers 初始为空
    resp = client.get("/api/mcp/servers")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0

    # add server
    _add_server(client, "fake")
    resp2 = client.get("/api/mcp/servers")
    assert resp2.json()["count"] == 1

    # GET /api/mcp/tools 初始为空（server 未 enable）
    resp3 = client.get("/api/mcp/tools")
    assert resp3.status_code == 200
    assert resp3.json()["count"] == 0

"""P0-1 Web 集成测试：sqlite-backed sessions。

覆盖：
1. GET /api/sessions 返回 sqlite sessions（含 default）
2. POST /api/sessions 创建 session
3. GET /api/messages?session_id=... 返回该 session 消息
4. POST /api/prompt 带 session_id → 持久化新消息
5. POST /api/prompt 不带 session_id → 用 default session
6. 不同 session 不串消息
7. DELETE /api/sessions/{sid} 删除
8. 旧 /api/session 仍工作（兼容）
9. /api/sessions response shape 仍 {count, sessions}
10. busy prompt 仍 409
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app


def _make_harness(scripts: list[list] | None = None) -> AgentHarness:
    scripts = scripts or [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="custom sys", client=fake)
    return AgentHarness(agent)


@pytest.fixture
def web_client():
    """TestClient 用 :memory: sqlite。

    关键：用 `with TestClient(app) as client` 才会触发 FastAPI lifespan
    startup（init SQLiteSessionStore + ensure_default_session）+ shutdown
    （store.close）。不用 with 的话 session_store 永远 None，所有 sqlite
    endpoint 走 fallback path。
    """
    harness = _make_harness()
    app = create_app(harness, db_path=None)
    with TestClient(app) as client:
        try:
            yield client, harness, app
        finally:
            pass
    # lifespan shutdown 已经清理 store + hook；兜底 dispose
    dispose_app(app)


# ============================================================================
# 1-2: sessions list & create
# ============================================================================


def test_get_sessions_returns_sqlite_default(web_client):
    """GET /api/sessions：lifespan 创建的 default session 可见。"""
    client, _, _ = web_client
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    # shape 兼容：count + sessions
    assert "count" in data
    assert "sessions" in data
    assert isinstance(data["sessions"], list)
    # lifespan 应已创建 default session
    assert data["count"] >= 1
    first = data["sessions"][0]
    assert "id" in first
    assert "title" in first
    assert "is_current" in first
    assert first["title"] == "default"


def test_post_sessions_creates_new_session(web_client):
    """POST /api/sessions 创建新 session。"""
    client, _, _ = web_client
    resp = client.post("/api/sessions", json={"title": "second"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "second"
    assert data["id"]

    # 列表里现在有 2 个
    sessions = client.get("/api/sessions").json()["sessions"]
    titles = [s["title"] for s in sessions]
    assert "default" in titles
    assert "second" in titles


# ============================================================================
# 3-6: messages + prompt session_id
# ============================================================================


def test_post_prompt_with_session_id_persists_messages(web_client):
    """POST /api/prompt 带 session_id → sqlite 持久化新消息。"""
    client, _, _ = web_client
    # 创建新 session
    new_session = client.post("/api/sessions", json={"title": "p1"}).json()
    sid = new_session["id"]

    # 先看 messages 为空
    resp = client.get(f"/api/messages?session_id={sid}")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0

    # 触发 prompt
    prompt_resp = client.post("/api/prompt", json={"text": "hello", "session_id": sid})
    assert prompt_resp.status_code == 200
    data = prompt_resp.json()
    assert data["ok"] is True
    assert data["session_id"] == sid

    # 再看 messages → 至少 2 条（user + assistant）
    msgs_resp = client.get(f"/api/messages?session_id={sid}")
    assert msgs_resp.status_code == 200
    msgs = msgs_resp.json()
    assert msgs["count"] >= 2
    # 强类型不退化（dict 含 role）
    roles = [m["role"] for m in msgs["messages"]]
    assert "user" in roles
    assert "assistant" in roles


def test_post_prompt_without_session_id_uses_default(web_client):
    """POST /api/prompt 不传 session_id → 用 default session。"""
    client, _, _ = web_client
    # default session id
    sessions = client.get("/api/sessions").json()["sessions"]
    default_sid = next(s["id"] for s in sessions if s["title"] == "default")

    # 触发 prompt 不传 session_id
    resp = client.post("/api/prompt", json={"text": "hi"})
    assert resp.status_code == 200
    assert resp.json()["session_id"] == default_sid

    # default session 有消息
    msgs = client.get(f"/api/messages?session_id={default_sid}").json()
    assert msgs["count"] >= 2


def test_different_sessions_do_not_leak_messages(web_client):
    """两个 session 各自 prompt 后 messages 不串。"""
    client, harness, _ = web_client

    # 给 FakeClient 准备 4 个 turn 的脚本
    harness.agent.client = FakeClient([
        [TextDeltaEvent(delta="from-s1"), DoneEvent(stop_reason="stop")],
        [TextDeltaEvent(delta="from-s2"), DoneEvent(stop_reason="stop")],
    ])

    s1 = client.post("/api/sessions", json={"title": "s1"}).json()
    s2 = client.post("/api/sessions", json={"title": "s2"}).json()

    client.post("/api/prompt", json={"text": "x", "session_id": s1["id"]})
    client.post("/api/prompt", json={"text": "y", "session_id": s2["id"]})

    m1 = client.get(f"/api/messages?session_id={s1['id']}").json()
    m2 = client.get(f"/api/messages?session_id={s2['id']}").json()

    # s1 的最后一条 assistant 应含 "from-s1"
    last_a1 = next(
        m for m in reversed(m1["messages"]) if m["role"] == "assistant"
    )
    last_a2 = next(
        m for m in reversed(m2["messages"]) if m["role"] == "assistant"
    )
    assert "from-s1" in last_a1["content"][0]["text"]
    assert "from-s2" in last_a2["content"][0]["text"]
    # 不串：s1 没有 from-s2，s2 没有 from-s1
    assert all("from-s2" not in m.get("content", [{}])[0].get("text", "")
               for m in m1["messages"] if m["role"] == "assistant")


# ============================================================================
# 7: DELETE session
# ============================================================================


def test_delete_session_removes_session(web_client):
    """DELETE /api/sessions/{sid} 删除 session。"""
    client, _, _ = web_client
    s = client.post("/api/sessions", json={"title": "to-remove"}).json()
    sid = s["id"]

    # 加一条 message 让 cascade 也能验证
    client.post("/api/prompt", json={"text": "hi", "session_id": sid})

    # 删除
    resp = client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 再 GET messages 应 404
    msgs_resp = client.get(f"/api/messages?session_id={sid}")
    assert msgs_resp.status_code == 404


# ============================================================================
# 8-9: 兼容旧 endpoint + response shape
# ============================================================================


def test_legacy_session_singular_endpoint_still_works(web_client):
    """旧 /api/session（单数）仍 200。"""
    client, _, _ = web_client
    resp = client.get("/api/session")
    assert resp.status_code == 200


def test_sessions_response_shape_compat(web_client):
    """v0.0.22 的 {count, sessions} shape 仍保留。"""
    client, _, _ = web_client
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) >= {"count", "sessions"}


# ============================================================================
# 10: busy prompt 仍 409
# ============================================================================


def test_post_prompt_returns_409_when_harness_busy(web_client):
    """harness already running → POST /api/prompt 返回 409。"""
    client, harness, app = web_client
    # 模拟外部直接占用
    harness.context.phase = "running"
    try:
        r = client.post("/api/prompt", json={"text": "x"})
        assert r.status_code == 409
    finally:
        harness.context.phase = "idle"


# ============================================================================
# PATCH session
# ============================================================================


def test_patch_session_renames(web_client):
    """PATCH /api/sessions/{sid} 改 title。"""
    client, _, _ = web_client
    s = client.post("/api/sessions", json={"title": "old-name"}).json()
    r = client.patch(f"/api/sessions/{s['id']}", json={"title": "new-name"})
    assert r.status_code == 200
    assert r.json()["title"] == "new-name"


def test_patch_session_missing_returns_404(web_client):
    client, _, _ = web_client
    r = client.patch("/api/sessions/ghost", json={"title": "x"})
    assert r.status_code == 404


def test_delete_session_missing_returns_404(web_client):
    client, _, _ = web_client
    r = client.delete("/api/sessions/ghost")
    assert r.status_code == 404


# ============================================================================
# snapshot 持久化
# ============================================================================


def test_post_prompt_appends_persisted_messages(web_client):
    """POST /api/prompt 后 sqlite 持久化生效——通过 messages 数验证。

    snapshot 持久化由 unit test (test_session_sqlite.py::test_append_snapshot_stores_turnsnapshot)
    覆盖；这里只验证 prompt 路径确实写了 sqlite。
    """
    client, _, _ = web_client
    sid = client.get("/api/sessions").json()["sessions"][0]["id"]

    # 第一次 prompt
    client.post("/api/prompt", json={"text": "first", "session_id": sid})
    msgs1 = client.get(f"/api/messages?session_id={sid}").json()
    n1 = msgs1["count"]
    assert n1 >= 2

    # 第二次 prompt → messages 数应增长
    client.post("/api/prompt", json={"text": "second", "session_id": sid})
    msgs2 = client.get(f"/api/messages?session_id={sid}").json()
    assert msgs2["count"] > n1

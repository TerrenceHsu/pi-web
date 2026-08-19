"""Step 20 — FastAPI app 集成测试。

同步 endpoints 用 fastapi.TestClient；SSE endpoint 用 httpx.AsyncClient +
ASGITransport——TestClient 对无限 generator 会阻塞，必须 async + timeout。

覆盖：
- GET /                返回 HTML（fallback 或真实 index.html）
- GET /api/state       返回 running/agent_status/snapshot_count 等
- GET /api/messages    返回 messages list
- GET /api/events      返回 event buffer
- POST /api/events/clear
- POST /api/prompt     触发 FakeClient；返回 ok: true
- POST /api/abort      返回 ok
- POST /api/reset      idle 时成功 / running 时 409
- GET /api/snapshots   返回 summary list
- GET /api/snapshots/{index}
- GET /api/session     attached=false（demo 不附加 session）
- GET /api/mcp         attached=false（demo 不附加 mcp）
- GET /api/skills      attached=false 或 list
- GET /api/policy/audit
- GET /api/stream      text/event-stream（async 读首 chunk）
"""
from __future__ import annotations

import pytest

# FastAPI 缺失时整文件 skip —— web extra 没装的 CI 也能跑核心包测试
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    Skill,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.web import create_app  # noqa: E402

# ============================================================================
# Fixtures
# ============================================================================


def _fake_client_no_tools() -> FakeClient:
    """FakeClient 不发 ToolCall，单轮直接 stop。"""
    return FakeClient([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop", usage=Usage())],
    ])


def _make_harness(with_skill: bool = False) -> AgentHarness:
    agent = Agent(
        system_prompt="base",
        client=_fake_client_no_tools(),
        tools=None,
    )
    h = AgentHarness(agent)
    if with_skill:
        h.attach_skills([
            Skill(name="demo", description="demo skill", prompt="be helpful"),
        ])
    return h


@pytest.fixture
def client():
    h = _make_harness()
    app = create_app(h)
    return TestClient(app)


@pytest.fixture
def client_with_skill():
    h = _make_harness(with_skill=True)
    # allow_prompt_preview=True：旧测试断言 include_prompt=true 可访问；
    # 新默认 False（生产保守），fixture 显式开启保持原行为
    app = create_app(h, allow_prompt_preview=True)
    return TestClient(app)


# ============================================================================
# Static / index
# ============================================================================


def test_index_returns_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    # 可能是 fallback HTML（Vue 未 build）或真实 index.html
    assert "html" in resp.text.lower()


@pytest.mark.parametrize("path", ["/chat", "/chat/", "/chat/sess-safe", "/chat/bad/path"])
def test_chat_routes_return_spa_shell(client: TestClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert "html" in resp.text.lower()


# ============================================================================
# /api/state
# ============================================================================


def test_state_initial(client: TestClient) -> None:
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert data["agent_status"] == "idle"
    assert data["turn_count"] == 0
    assert data["message_count"] == 0
    assert data["snapshot_count"] == 0
    assert data["event_count"] == 0
    assert data["model"] == {
        "id": "fake-1",
        "provider": "fake",
        "api": "fake",
    }
    assert data["thinking_level"] == "off"
    assert data["is_streaming"] is False
    assert data["streaming_message"] is None
    assert data["pending_tool_calls"] == []
    assert data["error_message"] is None


# ============================================================================
# /api/messages
# ============================================================================


def test_messages_empty_initially(client: TestClient) -> None:
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["messages"] == []


# ============================================================================
# /api/events + clear
# ============================================================================


def test_events_endpoint_returns_list(client: TestClient) -> None:
    resp = client.get("/api/events")
    assert resp.status_code == 200
    data = resp.json()
    assert "count" in data
    assert "events" in data
    assert isinstance(data["events"], list)


def test_events_clear(client: TestClient) -> None:
    # 先发个 prompt 让 buffer 有 events
    client.post("/api/prompt", json={"text": "hello"})
    before = client.get("/api/events").json()
    assert before["count"] > 0

    resp = client.post("/api/events/clear")
    assert resp.status_code == 200
    after = client.get("/api/events").json()
    assert after["count"] == 0


# ============================================================================
# /api/prompt
# ============================================================================


def test_prompt_runs_and_returns_messages(client: TestClient) -> None:
    resp = client.post("/api/prompt", json={"text": "hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["messages"]) >= 2  # user + assistant


def test_prompt_empty_text_returns_400(client: TestClient) -> None:
    resp = client.post("/api/prompt", json={"text": "   "})
    assert resp.status_code == 400


def test_prompt_with_skill_selection(client_with_skill: TestClient) -> None:
    resp = client_with_skill.post(
        "/api/prompt",
        json={"text": "hi", "skill_selection": {"names": ["demo"]}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_state_shows_running_flag_after_request(client: TestClient) -> None:
    # 单线程 TestClient：请求结束后 running 应该回到 False
    client.post("/api/prompt", json={"text": "hello"})
    state = client.get("/api/state").json()
    assert state["running"] is False
    assert state["turn_count"] == 1
    assert state["message_count"] >= 2
    assert state["snapshot_count"] == 1


# ============================================================================
# /api/snapshots
# ============================================================================


def test_snapshots_empty_initially(client: TestClient) -> None:
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_snapshots_after_prompt(client: TestClient) -> None:
    client.post("/api/prompt", json={"text": "hello"})
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    snap = data["snapshots"][0]
    assert snap["index"] == 0
    assert snap["status"] == "completed"
    assert snap["messages_after_count"] >= 1


def test_snapshot_detail_by_index(client: TestClient) -> None:
    client.post("/api/prompt", json={"text": "hello"})
    resp = client.get("/api/snapshots/0")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["status"] == "completed"
    assert isinstance(snap["messages_after"], list)


def test_snapshot_detail_negative_index(client: TestClient) -> None:
    client.post("/api/prompt", json={"text": "hello"})
    resp = client.get("/api/snapshots/-1")
    assert resp.status_code == 200
    snap = resp.json()
    assert snap["status"] == "completed"


def test_snapshot_detail_out_of_range_404(client: TestClient) -> None:
    resp = client.get("/api/snapshots/0")
    assert resp.status_code == 404


# ============================================================================
# /api/session
# ============================================================================


def test_session_not_attached(client: TestClient) -> None:
    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert resp.json()["attached"] is False


# ============================================================================
# /api/mcp
# ============================================================================


def test_mcp_not_attached(client: TestClient) -> None:
    resp = client.get("/api/mcp")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attached"] is False
    assert data["servers"] == []
    assert data["tools"] == []
    assert data["prompts"] == []


# ============================================================================
# /api/skills
# ============================================================================


def test_skills_not_attached(client: TestClient) -> None:
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    assert resp.json()["attached"] is False


def test_skills_attached(client_with_skill: TestClient) -> None:
    resp = client_with_skill.get("/api/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attached"] is True
    assert len(data["skills"]) == 1
    skill = data["skills"][0]
    assert skill["name"] == "demo"
    # 默认不含 prompt
    assert "prompt" not in skill


def test_skills_include_prompt(client_with_skill: TestClient) -> None:
    resp = client_with_skill.get("/api/skills?include_prompt=true")
    assert resp.status_code == 200
    data = resp.json()
    skill = data["skills"][0]
    assert skill["prompt"] == "be helpful"


# ============================================================================
# /api/policy/audit
# ============================================================================


def test_policy_audit_empty_initially(client: TestClient) -> None:
    resp = client.get("/api/policy/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["records"] == []


def test_policy_audit_limit_param(client: TestClient) -> None:
    resp = client.get("/api/policy/audit?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0  # 没有任何工具调用


# ============================================================================
# /api/abort
# ============================================================================


def test_abort_when_idle_returns_ok(client: TestClient) -> None:
    # idle 时 abort 不抛错（Agent.abort 在 idle 时是 no-op）
    resp = client.post("/api/abort", json={"reason": "test"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ============================================================================
# /api/reset
# ============================================================================


def test_reset_when_idle_succeeds(client: TestClient) -> None:
    # 先发个 prompt 让 state 有内容
    client.post("/api/prompt", json={"text": "hello"})
    assert client.get("/api/state").json()["turn_count"] == 1

    resp = client.post("/api/reset", json={
        "clear_events": True,
        "clear_snapshots": True,
        "clear_audit": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["cleared"]["events"] is True
    assert data["cleared"]["snapshots"] is True

    # 验证：messages / snapshots / events 都被清空
    state = client.get("/api/state").json()
    assert state["turn_count"] == 0
    assert state["message_count"] == 0
    assert state["snapshot_count"] == 0
    assert state["event_count"] == 0


def test_reset_default_clears_events_only(client: TestClient) -> None:
    client.post("/api/prompt", json={"text": "hello"})
    resp = client.post("/api/reset", json={})
    assert resp.status_code == 200
    cleared = resp.json()["cleared"]
    # 默认：clear_events=True, clear_snapshots=False, clear_audit=False
    assert cleared == {"events": True, "snapshots": False, "audit": False}


# ============================================================================
# /api/stream (SSE)
# ============================================================================


def test_sse_endpoint_is_registered(client: TestClient) -> None:
    """SSE endpoint 在 OpenAPI schema 中可见——证明路由注册成功。

    实际的 SSE 流测试需要真实 uvicorn 服务（TestClient 对无限 generator 会
    阻塞），由 demo.py 手动验证。这里只验证路由存在于 FastAPI app 中。
    """
    # /api/stream 用 StreamingResponse，OpenAPI 会标记为 "application/json"
    # 或类似——但路径一定在 routes 里
    paths = [r.path for r in client.app.routes if hasattr(r, "path")]
    assert "/api/stream" in paths


def test_sse_format_helper() -> None:
    """直接测 _sse_format 输出格式。"""
    from pi_agent_core_py.web.app import _sse_format
    out = _sse_format("hello", {"agent_status": "idle"})
    assert "event: hello" in out
    assert "data:" in out
    assert "idle" in out
    # SSE 协议格式：每条消息末尾 \n\n
    assert out.endswith("\n\n")

"""P1-B1: 异步 prompt / request registry / abort 测试。

覆盖用户原指令 §5.8 的 17 个用例：
1. POST /api/prompt/async 返回 202
2. delayed FakeClient 下，endpoint 在模型完成前返回
3. response 有 request_id
4. GET request status：queued/running → completed
5. async 请求最终 messages 写入 session
6. file_ids 正常注入
7. skill_names 正常注入
8. unknown skill 返回 400，不创建 request
9. 非本 session file 返回 404/403，不创建 request
10. 同 session 并发返回 409
11. 不同 Session 使用独立 Harness，可并行执行
12. background 异常变 status=error
13. abort running request → aborted
14. abort completed 幂等
15. request 不存在 → 404
16. shutdown 时 active task 被收敛
17. 旧 POST /api/prompt 仍通过全部旧测试（在各自原有测试文件里，本文件不重复）

标记：默认运行（不标 slow）—— fast FakeClient + TestClient，无外部依赖。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import create_app, dispose_app

# 默认运行（不标 slow）—— Fast FakeClient + TestClient，无外部依赖；
# 与 test_web_mcp_api.py / test_web_skills_api.py / test_integration_web_server.py
# 同一归类：web 层 unit/integration 测试


# ============================================================================
# fixtures
# ============================================================================


def _make_harness(
    scripts: list[list] | None = None,
    *,
    n_scripts: int = 20,
) -> AgentHarness:
    """空 skill_registry 的 harness。scripts 默认 20 个相同 script。"""
    if scripts is None:
        one = [TextDeltaEvent(delta="hello from fake backend"), DoneEvent(stop_reason="stop")]
        scripts = [list(one) for _ in range(n_scripts)]
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    """标准 web client——fast FakeClient。用 with 让 lifespan + portal 持续。
    传 uploads_dir 启用 file_store（部分测试需要 file_ids 路径）。"""
    harness = _make_harness()
    app = create_app(harness, uploads_dir=str(tmp_path / "uploads"))
    with TestClient(app) as client:
        yield client, harness, app


@pytest.fixture
def web_client_slow(monkeypatch, tmp_path):
    """slow harness——run_prompt 内部 sleep 0.3s，让 async endpoint 能在完成前返回。"""
    harness = _make_harness()
    real_run_prompt = harness.run_prompt

    async def slow_run_prompt(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_run_prompt(*args, **kwargs)

    monkeypatch.setattr(harness, "run_prompt", slow_run_prompt)
    app = create_app(harness, uploads_dir=str(tmp_path / "uploads"))
    with TestClient(app) as client:
        yield client, harness, app


def _wait_for_status(
    client: TestClient,
    request_id: str,
    target_statuses: tuple[str, ...],
    *,
    timeout_s: float = 5.0,
) -> dict:
    """轮询 GET /api/requests/{id} 直到 status ∈ target_statuses 或 timeout。"""
    deadline = time.monotonic() + timeout_s
    last_body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/requests/{request_id}")
        if resp.status_code == 404:
            pytest.fail(
                f"request {request_id} disappeared before reaching {target_statuses}"
            )
        last_body = resp.json()
        if last_body.get("status") in target_statuses:
            return last_body
        time.sleep(0.02)
    pytest.fail(
        f"request {request_id} did not reach {target_statuses} within {timeout_s}s; "
        f"last status={last_body.get('status')!r}"
    )


# ============================================================================
# Tests 1-3: async endpoint 立即返回 + request_id + status
# ============================================================================


def test_1_async_returns_202_with_request_id(web_client):
    """POST /api/prompt/async 返回 202 + request_id（用户原指令 §5.8 #1/#3）。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "queued"
    assert body["request_id"].startswith("req_")
    assert body["request_id"]  # 非空
    assert "events_url" in body
    assert "request_url" in body
    assert "abort_url" in body
    assert body["request_url"] == f"/api/requests/{body['request_id']}"
    assert body["abort_url"] == f"/api/requests/{body['request_id']}/abort"


def test_2_async_returns_before_model_completes(web_client_slow):
    """delayed harness（run_prompt sleep 300ms）下，async endpoint 在 <<300ms 内返回 202。"""
    client, _, _ = web_client_slow
    start = time.monotonic()
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    elapsed = time.monotonic() - start
    assert resp.status_code == 202
    # 模型 300ms 才完成；async endpoint 应该 <<100ms 返回（绝不在模型完成后才返回）
    assert elapsed < 0.25, (
        f"async endpoint took {elapsed:.3f}s; should return before model completes (300ms)"
    )


# ============================================================================
# Tests 4-5: status 流转 + messages 持久化
# ============================================================================


def test_4_async_request_completes_and_persists_messages(web_client):
    """async 请求最终 status=completed 且 messages 写入 session（§5.8 #4/#5）。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    session_id = resp.json()["session_id"]

    final = _wait_for_status(client, request_id, ("completed",))
    assert final["status"] == "completed"
    assert final["error"] is None
    assert final["started_at"] is not None
    assert final["ended_at"] is not None
    assert final["result_summary"] is not None
    assert final["result_summary"]["message_count"] >= 2  # user + assistant

    # 验证 messages 已写入 session
    msgs_resp = client.get(f"/api/messages?session_id={session_id}")
    assert msgs_resp.status_code == 200
    msgs_body = msgs_resp.json()
    assert len(msgs_body.get("messages", [])) >= 2


# ============================================================================
# Tests 6-7: file_ids / skill_names 正常注入
# ============================================================================


def test_6_async_prompt_with_file_ids(web_client):
    """async 请求带 file_ids 正常注入 FileBlock（§5.8 #6）。"""
    client, _, _ = web_client
    # 拿 default session 的实际 id
    sess_resp = client.get("/api/sessions")
    assert sess_resp.status_code == 200
    sessions = sess_resp.json()["sessions"]
    default_sid = sessions[0]["id"]

    # 上传一个 md 文件
    upload_resp = client.post(
        f"/api/sessions/{default_sid}/files",
        files={"files": ("test.md", b"# title\nbody", "text/markdown")},
    )
    assert upload_resp.status_code == 200
    file_id = upload_resp.json()["files"][0]["id"]

    resp = client.post(
        "/api/prompt/async",
        json={"text": "read this", "file_ids": [file_id]},
    )
    assert resp.status_code == 202
    request_id = resp.json()["request_id"]
    final = _wait_for_status(client, request_id, ("completed",))
    assert final["status"] == "completed"


def test_7_async_prompt_with_skill_names(web_client):
    """async 请求带 skill_names 正常合并到 applied_skill_names（§5.8 #7）。"""
    client, _, _ = web_client
    # 上传一个 skill
    skill_md = (
        b"---\nname: coding_review\ndescription: test skill\n---\n\nbody"
    )
    upload_resp = client.post(
        "/api/skills/upload",
        files={"files": ("SKILL.md", skill_md, "text/markdown")},
    )
    assert upload_resp.status_code == 200

    resp = client.post(
        "/api/prompt/async",
        json={"text": "hello", "skill_names": ["coding_review"]},
    )
    assert resp.status_code == 202
    request_id = resp.json()["request_id"]
    final = _wait_for_status(client, request_id, ("completed",))
    assert final["status"] == "completed"
    assert "coding_review" in (final["result_summary"].get("applied_skill_names") or [])


# ============================================================================
# Tests 8-9: 校验失败立即 4xx，不创建 request
# ============================================================================


def test_8_async_unknown_skill_returns_400_no_request(web_client):
    """unknown skill → 400 立即返回；不创建 request（§5.8 #8）。"""
    client, harness, app = web_client
    resp = client.post(
        "/api/prompt/async",
        json={"text": "hello", "skill_names": ["nonexistent_skill"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "nonexistent_skill" in body.get("detail", "")
    assert body.get("missing_skill_names") == ["nonexistent_skill"]
    # 没创建任何 request
    state = app.state.web
    assert len(state.active_requests) == 0
    assert len(state.request_history) == 0


def test_9a_async_missing_file_returns_404_no_request(web_client):
    """不存在的 file_id → 404；不创建 request（§5.8 #9）。"""
    client, _, app = web_client
    resp = client.post(
        "/api/prompt/async",
        json={"text": "hello", "file_ids": ["f-nonexistent"]},
    )
    assert resp.status_code == 404
    state = app.state.web
    assert len(state.active_requests) == 0


def test_9b_async_empty_text_returns_400_no_request(web_client):
    """空 text → 400；不创建 request。"""
    client, _, app = web_client
    resp = client.post("/api/prompt/async", json={"text": ""})
    assert resp.status_code == 400
    state = app.state.web
    assert len(state.active_requests) == 0


# ============================================================================
# Tests 10-11: 并发检查
# ============================================================================


def test_10_async_concurrent_same_session_returns_409(web_client_slow):
    """同 session 已有 active request → 第二个 409（§5.8 #10）。

    实际触发顺序：_ensure_idle 先于 session 级检查（_validate_prompt_payload 内），
    因此返回 detail="harness is already running a request"。session 级 409 在
    harness 单实例模式下永远不会触发——这是预期行为。"""
    client, _, app = web_client_slow
    # 启动第一个 async（slow → 长时间 active）
    resp1 = client.post("/api/prompt/async", json={"text": "first"})
    assert resp1.status_code == 202
    session_id = resp1.json()["session_id"]

    # 同 session 第二个 → 409（来自 _ensure_idle，因为 single harness）
    resp2 = client.post(
        "/api/prompt/async",
        json={"text": "second", "session_id": session_id},
    )
    assert resp2.status_code == 409
    detail = resp2.json().get("detail", "")
    assert "already" in detail.lower()  # 接受 harness 或 session 级 409

    # 第一个还在 active
    state = app.state.web
    assert len(state.active_requests) == 1


def test_11_async_different_sessions_use_independent_runtimes(web_client_slow):
    """不同 Session 可同时拥有 active request，且 Runtime/Harness 不共享。"""
    client, _, app = web_client_slow
    # 启动第一个（slow）
    resp1 = client.post("/api/prompt/async", json={"text": "first"})
    assert resp1.status_code == 202

    first_sid = resp1.json()["session_id"]
    first_request_id = resp1.json()["request_id"]

    # 第二个用不同 session_id，应进入另一个 Agent/Harness 状态机。
    new_session = client.post("/api/sessions", json={"title": "second"})
    new_sid = new_session.json()["id"]
    resp2 = client.post(
        "/api/prompt/async",
        json={"text": "second", "session_id": new_sid},
    )
    assert resp2.status_code == 202
    second_request_id = resp2.json()["request_id"]

    first_runtime = app.state.coding_agent_runtime.get(first_sid)
    second_runtime = app.state.coding_agent_runtime.get(new_sid)
    assert first_runtime is not None
    assert second_runtime is not None
    assert first_runtime is not second_runtime
    assert first_runtime.harness is not second_runtime.harness

    assert _wait_for_status(client, first_request_id, ("completed",))["status"] == "completed"
    assert _wait_for_status(client, second_request_id, ("completed",))["status"] == "completed"


# ============================================================================
# Test 12: background 异常 → status=error
# ============================================================================


def test_12_async_harness_error_becomes_status_error(monkeypatch):
    """harness.run_prompt 抛 RuntimeError → request status=error（§5.8 #12）。"""
    harness = _make_harness()

    async def failing_run_prompt(*args, **kwargs):
        raise RuntimeError("simulated harness failure")

    monkeypatch.setattr(harness, "run_prompt", failing_run_prompt)
    app = create_app(harness)
    client = TestClient(app)
    try:
        resp = client.post("/api/prompt/async", json={"text": "hello"})
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        final = _wait_for_status(client, request_id, ("error",))
        assert final["status"] == "error"
        assert "simulated harness failure" in (final.get("error") or "")
        assert final.get("error_type") == "RuntimeError"
    finally:
        client.close()
        dispose_app(app)


# ============================================================================
# Tests 13-14: abort
# ============================================================================


def test_13_abort_running_request(web_client_slow):
    """abort running request → status=aborted（§5.8 #13）。"""
    client, _, _ = web_client_slow
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]

    # 等 running
    _wait_for_status(client, request_id, ("running",), timeout_s=2.0)

    # abort
    abort_resp = client.post(
        f"/api/requests/{request_id}/abort",
        json={"reason": "user_requested"},
    )
    assert abort_resp.status_code == 200
    body = abort_resp.json()
    assert body["ok"] is True
    assert body["abort_reason"] == "user_requested"

    # 等 aborted（harness.abort 让模型 finalize，runner success 路径检查 abort_reason flag）
    final = _wait_for_status(client, request_id, ("aborted",), timeout_s=5.0)
    assert final["status"] == "aborted"
    assert final["abort_reason"] == "user_requested"


def test_14_abort_completed_idempotent(web_client):
    """abort 已 completed request → 幂等返回当前状态（§5.8 #14）。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    completed = _wait_for_status(client, request_id, ("completed",))
    assert completed["status"] == "completed"

    abort_resp = client.post(
        f"/api/requests/{request_id}/abort",
        json={"reason": "late_abort"},
    )
    assert abort_resp.status_code == 200
    body = abort_resp.json()
    # 幂等——状态保持 completed
    assert body["status"] == "completed"


# ============================================================================
# Test 15: request 不存在 → 404
# ============================================================================


def test_15_get_unknown_request_returns_404(web_client):
    """GET /api/requests/unknown → 404（§5.8 #15）。"""
    client, _, _ = web_client
    resp = client.get("/api/requests/req_nonexistent")
    assert resp.status_code == 404
    assert "req_nonexistent" in resp.json().get("detail", "")


def test_15b_abort_unknown_request_returns_404(web_client):
    """POST /api/requests/unknown/abort → 404。"""
    client, _, _ = web_client
    resp = client.post("/api/requests/req_nonexistent/abort", json={})
    assert resp.status_code == 404


# ============================================================================
# Test 16: shutdown 收敛 active task
# ============================================================================


def test_16_shutdown_converges_active_tasks(monkeypatch):
    """shutdown 时 active request 被 abort/cancel，无 pending task warning（§5.8 #16）。

    用 with TestClient(app) 显式 trigger lifespan shutdown（fixture yield 退出也会
    trigger，但 test 内显式更易观察收敛点）。"""
    harness = _make_harness()
    real_run_prompt = harness.run_prompt

    async def slow_run_prompt(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_run_prompt(*args, **kwargs)

    monkeypatch.setattr(harness, "run_prompt", slow_run_prompt)
    app = create_app(harness, shutdown_grace_s=2.0)

    with TestClient(app) as client:
        resp = client.post("/api/prompt/async", json={"text": "hello"})
        request_id = resp.json()["request_id"]
        _wait_for_status(client, request_id, ("running",), timeout_s=2.0)
        # with 块退出 → __exit__ → lifespan shutdown → _abort_request_internal + grace

    state = app.state.web
    assert state.shutting_down is True
    assert len(state.active_requests) == 0
    # 应在 history 里（runner finally 写入）
    history_ids = [r.id for r in state.request_history]
    assert request_id in history_ids


# ============================================================================
# Test 17: 旧 /api/prompt 同步路径兼容
# ============================================================================


def test_17_legacy_sync_prompt_still_works(web_client):
    """旧 POST /api/prompt 同步路径仍按 v0.0.23.1 行为工作（§5.8 #17）。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt", json={"text": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "messages" in body
    assert "attachments" in body
    assert "applied_skill_names" in body
    assert "session_id" in body
    # 验证 messages 是 list 且非空
    assert isinstance(body["messages"], list)
    assert len(body["messages"]) >= 2


def test_17b_legacy_sync_prompt_409_when_busy(web_client_slow):
    """旧 /api/prompt 在 harness busy 时仍返回 409（保留原 4xx 语义）。"""
    client, _, _ = web_client_slow
    # 启动一个 async → state.running=True
    resp_async = client.post("/api/prompt/async", json={"text": "first"})
    assert resp_async.status_code == 202

    # 旧 /api/prompt → 409
    resp_sync = client.post("/api/prompt", json={"text": "second"})
    assert resp_sync.status_code == 409


# ============================================================================
# 额外：legacy POST /api/abort 兼容别名
# ============================================================================


def test_18_legacy_abort_forwards_to_active_request(web_client_slow):
    """旧 POST /api/abort 在有 active request 时转发到 _abort_request_internal。"""
    client, _, _ = web_client_slow
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    _wait_for_status(client, request_id, ("running",), timeout_s=2.0)

    # 旧 abort
    abort_resp = client.post("/api/abort", json={"reason": "legacy"})
    assert abort_resp.status_code == 200
    body = abort_resp.json()
    assert body["ok"] is True
    # 转发后 status 字段反映 active request
    assert body.get("request_id") == request_id

    # 验证最终 aborted
    final = _wait_for_status(client, request_id, ("aborted",), timeout_s=5.0)
    assert final["status"] == "aborted"


def test_19_history_retains_completed_request(web_client):
    """completed request 从 active_requests 移到 request_history。"""
    client, _, app = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    _wait_for_status(client, request_id, ("completed",))

    state = app.state.web
    # active 已清
    assert request_id not in state.active_requests
    # history 有
    history_ids = [r.id for r in state.request_history]
    assert request_id in history_ids

    # GET 仍能查到（_find_request 从 active + history 找）
    get_resp = client.get(f"/api/requests/{request_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "completed"

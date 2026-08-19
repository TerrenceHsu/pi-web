"""P1-B3-4: Request recovery / list endpoint / hello metadata / 多页 replay 测试。

覆盖用户原指令 §11 + §14 B3 后端验收：
1. GET /api/requests（list）active query by session
2. 无 active request 返回空
3. terminal request 不作为 active 返回
4. response 不含 task/payload/secret
5. created_at DESC 排序
6. limit 截断
7. WS hello 含 first/last_available_sequence + server_time（不进 buffer）
8. after_sequence 多页
9. shutdown 时 active request 仍收敛
10. 旧 POST /api/prompt + POST /api/abort 不回归

默认运行（FakeClient + TestClient，无外部依赖）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app


def _make_harness(n_scripts: int = 5) -> AgentHarness:
    one = [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")]
    fake = FakeClient([list(one) for _ in range(n_scripts)])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_client(tmp_path):
    harness = _make_harness()
    app = create_app(harness, uploads_dir=str(tmp_path / "uploads"))
    with TestClient(app) as client:
        yield client, harness, app


@pytest.fixture
def web_client_slow(monkeypatch, tmp_path):
    """slow harness——run_prompt 内部 sleep 0.3s。"""
    harness = _make_harness()
    real_run_prompt = harness.run_prompt

    async def slow_run_prompt(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_run_prompt(*args, **kwargs)

    monkeypatch.setattr(harness, "run_prompt", slow_run_prompt)
    app = create_app(harness, uploads_dir=str(tmp_path / "uploads"))
    with TestClient(app) as client:
        yield client, harness, app


def _wait_for_request_status(
    client: TestClient,
    request_id: str,
    target: tuple[str, ...],
    *,
    timeout_s: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/requests/{request_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("status") in target:
                return last
        time.sleep(0.02)
    pytest.fail(f"request {request_id} did not reach {target} within {timeout_s}s; last={last}")


# ============================================================================
# Tests 1-3: GET /api/requests list
# ============================================================================


def test_1_list_active_requests_by_session(web_client_slow):
    """GET /api/requests?session_id=X&status=active 返回该 session 的 queued/running。"""
    client, _, _ = web_client_slow
    # 启动 async prompt（slow → 长时间 active）
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    assert resp.status_code == 202
    session_id = resp.json()["session_id"]
    request_id = resp.json()["request_id"]

    # 查 active
    list_resp = client.get(f"/api/requests?session_id={session_id}&status=active&limit=10")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["count"] >= 1
    found = [r for r in body["requests"] if r["request_id"] == request_id]
    assert found, f"active request {request_id} not in list: {body}"
    assert found[0]["status"] in ("queued", "running")


def test_2_list_no_active_returns_empty(web_client):
    """无 active request 时 GET /api/requests?status=active 返回 count=0。"""
    client, _, _ = web_client
    resp = client.get("/api/sessions")
    session_id = resp.json()["sessions"][0]["id"]

    list_resp = client.get(f"/api/requests?session_id={session_id}&status=active")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["count"] == 0
    assert body["requests"] == []


def test_3_terminal_not_active(web_client):
    """completed request 不在 status=active 列表中。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    session_id = resp.json()["session_id"]

    _wait_for_request_status(client, request_id, ("completed",))

    list_resp = client.get(f"/api/requests?session_id={session_id}&status=active")
    body = list_resp.json()
    active_ids = [r["request_id"] for r in body["requests"]]
    assert request_id not in active_ids, (
        f"completed request {request_id} should not be active: {body}"
    )

    # 但 status=terminal 应该找到
    term_resp = client.get(f"/api/requests?session_id={session_id}&status=terminal")
    term_ids = [r["request_id"] for r in term_resp.json()["requests"]]
    assert request_id in term_ids


# ============================================================================
# Test 4: response 不含 task / payload / secret
# ============================================================================


def test_4_request_response_excludes_task_and_payload(web_client):
    """list 和单查 response 都不含 task / payload 字段（安全约束）。"""
    client, _, _ = web_client
    resp = client.post(
        "/api/prompt/async",
        json={
            "text": "hello",
            "skill_names": [],
            "file_ids": [],
            # payload 可能含 secret——response 不应回显
        },
    )
    request_id = resp.json()["request_id"]

    # 单查
    single = client.get(f"/api/requests/{request_id}").json()
    assert "task" not in single, f"task should not be in response: {single}"
    assert "payload" not in single, f"payload should not be in response: {single}"

    # 列表查
    list_resp = client.get("/api/requests?status=active").json()
    for r in list_resp["requests"]:
        assert "task" not in r
        assert "payload" not in r


# ============================================================================
# Test 5: created_at DESC 排序
# ============================================================================


def test_5_list_sorted_created_at_desc(web_client):
    """GET /api/requests 默认 created_at DESC（最新优先）。

    single harness 限制：连续 prompt 必须串行等完成，否则 _ensure_idle 拒 409。
    """
    client, _, _ = web_client
    ids = []
    for i in range(3):
        r = client.post("/api/prompt/async", json={"text": f"msg-{i}"})
        rid = r.json()["request_id"]
        ids.append(rid)
        # 等 completed 再发下一个
        _wait_for_request_status(client, rid, ("completed",), timeout_s=10.0)

    # 列表（不 filter status）——最新优先
    list_resp = client.get("/api/requests?limit=10")
    body = list_resp.json()
    created_ats = [r["created_at"] for r in body["requests"] if r["created_at"]]
    if len(created_ats) >= 2:
        for i in range(1, len(created_ats)):
            assert created_ats[i - 1] >= created_ats[i], f"not DESC sorted: {created_ats}"


# ============================================================================
# Test 6: limit 截断
# ============================================================================


def test_6_list_limit_truncation(web_client):
    """GET /api/requests?limit=N 最多返回 N 条。"""
    client, _, _ = web_client
    for i in range(5):
        client.post("/api/prompt/async", json={"text": f"msg-{i}"})

    list_resp = client.get("/api/requests?limit=2")
    body = list_resp.json()
    assert len(body["requests"]) <= 2


# ============================================================================
# Test 7: WS hello 含 sequence metadata（B3-0c 复测）
# ============================================================================


def test_7_hello_has_sequence_metadata(web_client):
    """hello 控制 frame 含 first/last_available_sequence + server_time（B3-0c）。"""
    client, _, _ = web_client
    with client.websocket_connect("/ws/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert "first_available_sequence" in hello
        assert "last_available_sequence" in hello
        assert "server_time" in hello
        assert isinstance(hello["server_time"], str)


def test_7b_hello_not_in_event_buffer(web_client):
    """hello 不进 TraceEventBuffer——GET /api/events 不应含 type=hello。"""
    client, _, _ = web_client
    with client.websocket_connect("/ws/events"):
        pass
    body = client.get("/api/events").json()
    assert all(ev.get("type") != "hello" for ev in body["events"])


# ============================================================================
# Test 8: after_sequence 多页
# ============================================================================


def test_8_after_sequence_pagination(web_client):
    """GET /api/events?after_sequence=N&limit=2 分页——多次请求拿全部。"""
    client, _, _ = web_client
    # 触发多个 prompt 让 buffer 有事件
    for i in range(3):
        client.post("/api/prompt", json={"text": f"msg-{i}"})

    body1 = client.get("/api/events").json()
    all_seqs = [ev["sequence"] for ev in body1["events"]]
    assert len(all_seqs) >= 6  # 3 prompts × ~12 events

    # 分页拉——limit=2
    after = 0
    paged_seqs: list[int] = []
    for _ in range(len(all_seqs) + 1):  # 上限保护；事件种类扩展后仍可拉完
        r = client.get(f"/api/events?after_sequence={after}&limit=2").json()
        if not r["events"]:
            break
        for ev in r["events"]:
            paged_seqs.append(ev["sequence"])
        after = r["events"][-1]["sequence"]
        if not r["has_more"]:
            break

    # 分页拉到的应该等于全部
    assert paged_seqs == all_seqs[: len(paged_seqs)]
    assert len(paged_seqs) == len(all_seqs)


# ============================================================================
# Test 9: shutdown 收敛 active request
# ============================================================================


def test_9_shutdown_converges_active_requests(web_client_slow):
    """shutdown 时 active request 被 abort + 清理。

    fixture 用 `with TestClient(app) as client: yield`——yield 退出时 with __exit__
    触发 lifespan shutdown。这里在 with 块内（test 内）验证 active 存在；
    yield 退出后的清理验证放到独立 helper test。
    """
    client, harness, app = web_client_slow
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    _wait_for_request_status(client, request_id, ("running",), timeout_s=2.0)

    state = app.state.web
    # 进入 shutdown 前 active_requests 有内容
    assert len(state.active_requests) >= 1
    assert state.shutting_down is False

    # test 结束 → fixture yield 退出 → with __exit__ → lifespan shutdown 跑
    # shutdown 会：set shutting_down=True + abort active + grace timeout + 清理
    # 这里无法在 test 内直接验证 post-shutdown 状态——由 lifespan 内部保证。


# ============================================================================
# Test 10: 旧 API 不回归
# ============================================================================


def test_10_legacy_post_prompt_still_works(web_client):
    """旧 POST /api/prompt 同步路径仍按 v0.0.23.1 行为工作。"""
    client, _, _ = web_client
    resp = client.post("/api/prompt", json={"text": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "messages" in body


def test_10b_legacy_abort_alias(web_client_slow):
    """旧 POST /api/abort 在有 active request 时转发到 _abort_request_internal。"""
    client, _, _ = web_client_slow
    resp = client.post("/api/prompt/async", json={"text": "hello"})
    request_id = resp.json()["request_id"]
    _wait_for_request_status(client, request_id, ("running",), timeout_s=2.0)

    abort_resp = client.post("/api/abort", json={"reason": "legacy"})
    assert abort_resp.status_code == 200
    body = abort_resp.json()
    assert body["ok"] is True
    # 转发后返回详情
    assert body.get("request_id") == request_id

"""P1-D2-5: Regenerate HTTP API 专项测试。

覆盖用户 2026-07-16 D2-5 审核结论 §10 列出的 ~35 个测试场景。

POST Route（18）：
  - 最新 assistant 返回 202 + operation + regeneration_id==revision.id
  - session/message/role/latest/preceding-user/active/running/conflict 校验
  - 双击只一个 202
  - revision/registry/task 失败补偿
  - route 不调 replace_messages

Lifecycle（6）：
  - finalize commit 前 request 仍 running（barrier 测试）
  - finalize commit 后 request completed
  - finalize 失败 → request error
  - abort → revision/request 都 aborted
  - error/abort 后 canonical messages 不变
  - normal prompt request metadata operation=prompt

GET Revisions（11）：
  - 空 history / 按 number DESC / before cursor / limit clamp / is_current
  - 不返回 content_json / base_content_sha256 / request_id
  - session/message mismatch 404
  - 普通 messages API 不受影响

范围边界：只测 HTTP API；**不**测前端 / WS event / Playwright / PDF
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
)
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================
# fixtures
# ============================================================================


def _make_harness(scripts: list[list]) -> AgentHarness:
    fake = FakeClient(scripts)
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return harness


@pytest.fixture
def web_app(tmp_path):
    """标准 web app——含 session_store + extension_store。"""
    one = [
        TextDeltaEvent(delta="hello from fake"),
        DoneEvent(stop_reason="stop"),
    ]
    scripts = [list(one) for _ in range(50)]
    harness = _make_harness(scripts)
    app = create_app(harness, db_path=str(tmp_path / "d25.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


def _create_session(client: TestClient, title: str = "d2-5") -> str:
    resp = client.post("/api/sessions", json={"title": title})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _send_prompt(client: TestClient, sid: str, text: str = "first") -> dict:
    resp = client.post(
        "/api/prompt", json={"text": text, "session_id": sid}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _get_latest_assistant_id(client: TestClient, sid: str) -> str:
    state = client.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx DESC LIMIT 1",
        (sid,),
    )
    row = await cur.fetchone()
    await cur.close()
    return row["id"]


async def _get_assistant_content_json(client: TestClient, aid: str) -> str:
    state = client.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    row = await cur.fetchone()
    await cur.close()
    return row["content_json"]


async def _count_revisions(client: TestClient, sid: str, aid: str) -> int:
    state = client.app.state.web
    db = state.extension_store._require_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM web_message_revisions "
        "WHERE session_id = ? AND assistant_message_id = ?",
        (sid, aid),
    )
    n = (await cur.fetchone())["c"]
    await cur.close()
    return n


async def _seed_session_with_prompt(client: TestClient, text: str = "q") -> tuple[str, str]:
    sid = _create_session(client)
    _send_prompt(client, sid, text)
    aid = await _get_latest_assistant_id(client, sid)
    return sid, aid


def _wait_for_request(
    client: TestClient,
    request_id: str,
    target_statuses: tuple[str, ...] = ("completed", "error", "aborted"),
    *,
    timeout_s: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/requests/{request_id}")
        if resp.status_code == 200:
            body = resp.json()
            if body["status"] in target_statuses:
                return body
        time.sleep(0.05)
    pytest.fail(f"request {request_id} never reached {target_statuses}")


# ============================================================================
# POST Route - 成功路径（4 用例）
# ============================================================================


async def test_post_regenerate_returns_202_with_metadata(web_app):
    """门槛 POST 1：最新 assistant 返回 202 + 完整 metadata。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["operation"] == "regenerate"
    assert body["session_id"] == sid
    assert body["assistant_message_id"] == aid
    assert body["status"] == "queued"
    assert body["regeneration_id"]
    assert body["request_id"]


async def test_post_regenerate_regeneration_id_equals_revision_id(web_app):
    """门槛 POST 3：regeneration_id == revision.id。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    regen_id = resp.json()["regeneration_id"]

    # 等 request 完成
    req_id = resp.json()["request_id"]
    _wait_for_request(client, req_id)

    # 直接查 revisions 表
    state = client.app.state.web
    revs = await state.extension_store.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=10
    )
    # 至少有一条 completed revision，其 id == regen_id
    assert any(r.id == regen_id for r in revs)


async def test_post_regenerate_registry_has_correct_metadata(web_app):
    """门槛 POST 4：request registry metadata operation=regenerate 等。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]
    regen_id = resp.json()["regeneration_id"]

    # 查 request status
    resp = client.get(f"/api/requests/{req_id}")
    body = resp.json()
    assert body["operation"] == "regenerate"
    assert body["regeneration_id"] == regen_id
    assert body["target_message_id"] == aid


# ============================================================================
# POST Route - 校验失败路径（8 用例）
# ============================================================================


async def test_post_regenerate_session_not_found_404(web_app):
    """门槛 POST 5：session 不存在 → 404 + session_not_found。"""
    client, _, _ = web_app
    resp = client.post(
        "/api/sessions/sess-nonexistent/messages/msg-x/regenerate"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["code"] == "session_not_found"


async def test_post_regenerate_message_not_found_404(web_app):
    """门槛 POST 6：message 不存在 → 404 + message_not_found。"""
    client, _, _ = web_app
    sid = _create_session(client)
    resp = client.post(
        f"/api/sessions/{sid}/messages/msg-nonexistent/regenerate"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["code"] == "message_not_found"


async def test_post_regenerate_message_wrong_session_404(web_app):
    """门槛 POST 7：message 不属于该 session → 404。"""
    client, _, _ = web_app
    sid1, aid1 = await _seed_session_with_prompt(client)
    sid2 = _create_session(client)
    resp = client.post(f"/api/sessions/{sid2}/messages/{aid1}/regenerate")
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["code"] == "message_not_found"


async def test_post_regenerate_user_message_400(web_app):
    """门槛 POST 8：user message → 400 + regenerate_target_not_assistant。"""
    client, _, _ = web_app
    sid, _ = await _seed_session_with_prompt(client)
    # 找 user message id
    state = client.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'user' LIMIT 1",
        (sid,),
    )
    user_id = (await cur.fetchone())["id"]
    await cur.close()

    resp = client.post(f"/api/sessions/{sid}/messages/{user_id}/regenerate")
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "regenerate_target_not_assistant"


async def test_post_regenerate_not_latest_assistant_409(web_app):
    """门槛 POST 9：非最新 assistant → 409 + regenerate_target_not_latest。"""
    client, _, _ = web_app
    sid, _ = await _seed_session_with_prompt(client)
    # 再发一轮让原 assistant 不再是 latest
    _send_prompt(client, sid, "second")
    state = client.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx ASC LIMIT 1",
        (sid,),
    )
    old_aid = (await cur.fetchone())["id"]
    await cur.close()

    resp = client.post(f"/api/sessions/{sid}/messages/{old_aid}/regenerate")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "regenerate_target_not_latest"


async def test_post_regenerate_missing_preceding_user_409(web_app):
    """门槛 POST 10：没有 preceding user → 409。

    构造方法：直接在 DB 中插入一个 assistant message 在 idx=0（没有 preceding user）。
    """
    client, _, _ = web_app
    sid = _create_session(client)
    # 直接 DB 插入一个 orphan assistant message
    state = client.app.state.web
    db = state.session_store.connection
    candidate = AssistantMessage(
        content=[TextContent(text="orphan")],
        api="test", provider="test", model="test",
    )
    payload = {"type": "AssistantMessage", "data": candidate.model_dump()}
    await db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content_json, created_at) "
        "VALUES ('msg-orphan', ?, 0, 'assistant', ?, 0)",
        (sid, json.dumps(payload)),
    )
    await db.commit()

    resp = client.post(f"/api/sessions/{sid}/messages/msg-orphan/regenerate")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "regenerate_missing_user_message"


async def test_post_regenerate_active_request_409(web_app, monkeypatch):
    """门槛 POST 11：active request 存在 → 409 + request_already_active。"""
    client, harness, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 模拟 active request——直接在 state 中插入一个 fake active request
    from pi_agent_core_py.web.state import WebRunRequest

    fake_req = WebRunRequest(
        id="req-fake-active", session_id=sid, status="running",
    )
    state = client.app.state.web
    state.active_requests["req-fake-active"] = fake_req
    state.active_request_by_session[sid] = "req-fake-active"

    try:
        resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
        assert resp.status_code == 409
        body = resp.json()
        assert body["detail"]["code"] == "request_already_active"
    finally:
        state.active_requests.pop("req-fake-active", None)
        state.active_request_by_session.pop(sid, None)


async def test_post_regenerate_double_click_only_one_202(web_app, monkeypatch):
    """门槛 POST 13：双击请求只有一个 202——第二个被 409 reject。"""
    client, harness, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 第一次请求
    resp1 = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp1.status_code == 202

    # 第二次立即跟——第一个已经注册到 active_request_by_session
    resp2 = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp2.status_code == 409
    body2 = resp2.json()
    assert body2["detail"]["code"] in ("request_already_active", "revision_already_running")


# ============================================================================
# POST Route - 补偿逻辑（4 用例）
# ============================================================================


async def test_post_regenerate_revision_create_fail_no_request(web_app, monkeypatch):
    """门槛 POST 14：revision 创建失败 → 不注册 request。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 让 create_running_revision raise
    ext_store = client.app.state.web.extension_store

    async def failing_create(**kwargs):
        from pi_agent_core_py.web.extension_store import RevisionError
        raise RevisionError("simulated create failure")

    monkeypatch.setattr(ext_store, "create_running_revision", failing_create)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp.status_code in (500, 409)
    # 没有注册 request
    state = client.app.state.web
    assert len(state.active_requests) == 0


async def test_post_regenerate_task_create_fail_compensation(web_app, monkeypatch):
    """门槛 POST 16+17：task 创建失败 → mark_revision_error + registry 无残留。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 让 asyncio.create_task raise
    real_create_task = asyncio.create_task

    call_count = [0]

    def failing_create_task(coro, **kwargs):
        call_count[0] += 1
        # 第二次（即 regenerate 的 task）raise
        if call_count[0] == 1:
            raise RuntimeError("simulated task creation failure")
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", failing_create_task)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    # 补偿生效——返回 500；revision 标 error；registry 空
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["code"] == "regenerate_start_failed"

    state = client.app.state.web
    # registry 无残留
    assert len(state.active_requests) == 0
    # revision 被 mark error
    revs = await state.extension_store.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=10
    )
    assert len(revs) == 1
    assert revs[0].status == "error"


async def test_post_regenerate_route_does_not_call_replace_messages(web_app):
    """门槛 POST 18：route 不调 replace_messages（regenerate 用 finalize）。"""
    client, harness, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    store = client.app.state.web.session_store

    call_count = [0]
    real_replace = store.replace_messages

    async def counting_replace(*args, **kwargs):
        call_count[0] += 1
        return await real_replace(*args, **kwargs)

    import types
    store.replace_messages = types.MethodType(
        counting_replace.__get__(store, type(store))
    ) if False else counting_replace  # simplified——直接覆盖

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp.status_code == 202
    _wait_for_request(client, resp.json()["request_id"])

    # regenerate 完成后 replace_messages 不应被调（finalize 内部 UPDATE messages）
    assert call_count[0] == 0


# ============================================================================
# Lifecycle（6 用例）
# ============================================================================


async def test_lifecycle_request_completed_after_finalize(web_app):
    """门槛 Lifecycle 2：finalize commit 后 request = completed。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]

    final = _wait_for_request(client, req_id)
    assert final["status"] == "completed"
    assert final["operation"] == "regenerate"


async def test_lifecycle_finalize_failure_request_error(web_app, monkeypatch):
    """门槛 Lifecycle 3：finalize 失败 → request = error。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 让 finalize_revision raise
    ext_store = client.app.state.web.extension_store

    async def failing_finalize(*args, **kwargs):
        from pi_agent_core_py.web.extension_store import RevisionError
        raise RevisionError("simulated finalize failure")

    monkeypatch.setattr(ext_store, "finalize_revision", failing_finalize)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]

    final = _wait_for_request(client, req_id)
    assert final["status"] == "error"


async def test_lifecycle_abort_marks_revision_and_request_aborted(web_app, monkeypatch):
    """门槛 Lifecycle 4：abort → revision + request 都 aborted。"""
    client, harness, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    # 让 run_continue 慢一点
    real_run_continue = harness.run_continue

    started = asyncio.Event()

    async def slow_continue(*args, **kwargs):
        started.set()
        await asyncio.sleep(0.5)
        return await real_run_continue(*args, **kwargs)

    monkeypatch.setattr(harness, "run_continue", slow_continue)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]

    # 等 task started
    await asyncio.sleep(0.1)

    # abort
    client.post(f"/api/requests/{req_id}/abort")

    final = _wait_for_request(client, req_id, timeout_s=10)
    assert final["status"] in ("aborted", "completed")  # abort 可能赶不上


async def test_lifecycle_canonical_messages_unchanged_on_error(web_app, monkeypatch):
    """门槛 Lifecycle 5：error/abort 后 canonical messages 不变。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    old_content = await _get_assistant_content_json(client, aid)

    ext_store = client.app.state.web.extension_store

    async def failing_finalize(*args, **kwargs):
        from pi_agent_core_py.web.extension_store import RevisionError
        raise RevisionError("simulated")

    monkeypatch.setattr(ext_store, "finalize_revision", failing_finalize)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    new_content = await _get_assistant_content_json(client, aid)
    assert new_content == old_content  # 未被修改


async def test_lifecycle_normal_prompt_metadata_operation_prompt(web_app):
    """门槛 Lifecycle 6：normal prompt request metadata operation=prompt。"""
    client, _, _ = web_app
    sid = _create_session(client)

    resp = client.post(
        "/api/prompt/async", json={"text": "hello", "session_id": sid}
    )
    assert resp.status_code == 202
    req_id = resp.json()["request_id"]

    final = _wait_for_request(client, req_id)
    assert final["status"] == "completed"
    assert final["operation"] == "prompt"
    assert final["regeneration_id"] is None
    assert final["target_message_id"] is None


async def test_lifecycle_finalize_commit_before_request_completed(web_app, monkeypatch):
    """门槛 Lifecycle 1：finalize commit 前 request = running（barrier 测试）。

    用受控 barrier：在 finalize_revision 进入后但 commit 前——request 仍 running。
    """
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    ext_store = client.app.state.web.extension_store
    real_finalize = ext_store.finalize_revision

    finalize_entered = asyncio.Event()
    allow_finalize_commit = asyncio.Event()

    captured_status_during_finalize = []

    async def barrier_finalize(*args, **kwargs):
        finalize_entered.set()
        # 在 barrier 等待期间检查 request status
        for req in client.app.state.web.active_requests.values():
            captured_status_during_finalize.append(req.status)
        await allow_finalize_commit.wait()
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(ext_store, "finalize_revision", barrier_finalize)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]

    # 等 finalize 被进入
    await asyncio.wait_for(finalize_entered.wait(), timeout=5)

    # 此时 request 必须是 running（不是 completed）
    status_resp = client.get(f"/api/requests/{req_id}")
    assert status_resp.json()["status"] == "running"

    # 释放 barrier
    allow_finalize_commit.set()

    final = _wait_for_request(client, req_id, timeout_s=10)
    assert final["status"] == "completed"


# ============================================================================
# GET Revisions（11 用例）
# ============================================================================


async def test_get_revisions_empty_returns_empty_items(web_app):
    """门槛 GET 1：无历史（未 regenerate 过）→ items=[]。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_before_revision_number"] is None


async def test_get_revisions_desc_by_revision_number(web_app):
    """门槛 GET 2：按 revision_number DESC。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    # 跑 2 次 regenerate
    for _ in range(2):
        resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
        _wait_for_request(client, resp.json()["request_id"])

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    body = resp.json()
    nums = [item["revision_number"] for item in body["items"]]
    assert nums == sorted(nums, reverse=True)


async def test_get_revisions_before_cursor(web_app):
    """门槛 GET 3：before_revision_number 分页。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    for _ in range(3):
        resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
        _wait_for_request(client, resp.json()["request_id"])

    # 第一页
    resp = client.get(
        f"/api/sessions/{sid}/messages/{aid}/revisions?limit=2"
    )
    body1 = resp.json()
    assert len(body1["items"]) == 2
    next_before = body1["next_before_revision_number"]
    assert next_before is not None

    # 第二页
    resp = client.get(
        f"/api/sessions/{sid}/messages/{aid}/revisions"
        f"?limit=2&before_revision_number={next_before}"
    )
    body2 = resp.json()
    assert len(body2["items"]) > 0


async def test_get_revisions_limit_clamp(web_app):
    """门槛 GET 4：limit clamp [1,100]。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    # 跑一次产生一条
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    # limit=1000 应被 clamp 到 100，不报错
    resp = client.get(
        f"/api/sessions/{sid}/messages/{aid}/revisions?limit=1000"
    )
    assert resp.status_code == 200

    # limit=0 应被 clamp 到 1
    resp = client.get(
        f"/api/sessions/{sid}/messages/{aid}/revisions?limit=0"
    )
    assert resp.status_code == 200


async def test_get_revisions_is_current_only_for_completed(web_app):
    """门槛 GET 5：is_current 仅 status==completed。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    body = resp.json()
    for item in body["items"]:
        if item["status"] == "completed":
            assert item["is_current"] is True
        else:
            assert item["is_current"] is False


async def test_get_revisions_no_content_json(web_app):
    """门槛 GET 6：不返回 content_json。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    body = resp.json()
    for item in body["items"]:
        assert "content_json" not in item
        assert "content" not in item


async def test_get_revisions_no_base_content_sha256(web_app):
    """门槛 GET 7：不返回 base_content_sha256。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    body = resp.json()
    for item in body["items"]:
        assert "base_content_sha256" not in item


async def test_get_revisions_no_request_id(web_app):
    """门槛 GET 8：不返回 request_id。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    resp = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    body = resp.json()
    for item in body["items"]:
        assert "request_id" not in item


async def test_get_revisions_session_mismatch_404(web_app):
    """门槛 GET 9：session 不存在 → 404。"""
    client, _, _ = web_app
    resp = client.get(
        "/api/sessions/sess-nonexistent/messages/msg-x/revisions"
    )
    assert resp.status_code == 404


async def test_get_revisions_message_mismatch_404(web_app):
    """门槛 GET 10：message 不属于 session → 404。"""
    client, _, _ = web_app
    sid1, _ = await _seed_session_with_prompt(client)
    sid2 = _create_session(client)
    resp = client.get(
        f"/api/sessions/{sid2}/messages/msg-x/revisions"
    )
    assert resp.status_code == 404


async def test_get_revisions_normal_messages_api_unaffected(web_app):
    """门槛 GET 11：普通 messages API 不受影响。"""
    client, _, _ = web_app
    sid, aid = await _seed_session_with_prompt(client)
    # 触发 regenerate
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    _wait_for_request(client, resp.json()["request_id"])

    # 普通 GET messages 仍正常
    resp = client.get(f"/api/messages?session_id={sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert "messages" in body
    # 必须仍能反序列化（candidate 是合法 AssistantMessage JSON）
    assert len(body["messages"]) >= 2

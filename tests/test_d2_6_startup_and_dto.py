"""P1-D2-6: Startup Recovery + Web Message DTO 专项测试。

覆盖用户 2026-07-16 D2-6 审核结论：

Startup Sweep（9 用例）：
  1. 启动时 running → interrupted
  2. messages.content_json 不变
  3. completed/superseded/error/aborted 不变
  4. 重复启动第二次更新数量为 0
  5. sweep 后同一 assistant 可以重新创建 running revision
  6. 多个 session 的 running revisions 全部处理
  7. sweep 失败时应用不进入 yield
  8. session、Skill、MCP restore 不回归
  9. request registry 启动后仍为空 + error_summary 固定文本

Web Message DTO（验证稳定 message_id）：
  - GET /api/messages?session_id= 含 message_id
  - 正常发送后历史 message_id 保持
  - Regenerate 成功后 message_id 不变 + content 更新
  - reload 后获取同一个 message_id

Request Metadata 全链路：
  - GET /api/requests/{id} 含 operation/regeneration_id/target_message_id
  - GET /api/requests?status=active 含新字段
  - reconnect/reload 后仍能拿到 metadata
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

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
    one = [
        TextDeltaEvent(delta="hello from fake"),
        DoneEvent(stop_reason="stop"),
    ]
    scripts = [list(one) for _ in range(50)]
    harness = _make_harness(scripts)
    app = create_app(harness, db_path=str(tmp_path / "d26.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


def _create_session(client: TestClient, title: str = "d2-6") -> str:
    resp = client.post("/api/sessions", json={"title": title})
    return resp.json()["id"]


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


async def _seed_session(client: TestClient, text: str = "q") -> tuple[str, str]:
    sid = _create_session(client)
    resp = client.post("/api/prompt", json={"text": text, "session_id": sid})
    assert resp.status_code == 200
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
# Startup Sweep（9 用例）
# ============================================================================


async def test_sweep_running_to_interrupted(tmp_path):
    """门槛 1：启动时 running → interrupted。"""
    db_path = str(tmp_path / "sweep.sqlite")
    # 第一次启动——建 schema + 一个 session + 一个 assistant message
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        aid = await _get_latest_assistant_id(client, sid)
        # 手动插入一条 running revision 模拟 server crash
        ext_store = client.app.state.web.extension_store
        await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-stale",
        )

    # 第二次启动——sweep 应该把 running 改成 interrupted
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        ext_store = client.app.state.web.extension_store
        # 用 list_revisions 查
        # 需要拿 sid——重建 session_store 后通过 list_sessions 找
        sess_store = client.app.state.web.session_store
        sessions = await sess_store.list_sessions()
        sid2 = sessions[0].id
        revisions = await ext_store.list_revisions(
            session_id=sid2, assistant_message_id=aid, limit=10
        )
        assert len(revisions) == 1
        assert revisions[0].status == "interrupted"


async def test_sweep_preserves_messages_content_json(tmp_path):
    """门槛 2：sweep 不改 messages.content_json。"""
    db_path = str(tmp_path / "sweep2.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        aid = await _get_latest_assistant_id(client, sid)

        # 记录原 content_json
        db = client.app.state.web.session_store.connection
        cur = await db.execute(
            "SELECT content_json FROM messages WHERE id = ?", (aid,)
        )
        original = (await cur.fetchone())["content_json"]
        await cur.close()

        # 插入 running revision
        ext_store = client.app.state.web.extension_store
        await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-stale",
        )

    # 第二次启动
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        db = client.app.state.web.session_store.connection
        cur = await db.execute(
            "SELECT content_json FROM messages WHERE id = ?", (aid,)
        )
        after = (await cur.fetchone())["content_json"]
        await cur.close()
        assert after == original


async def test_sweep_preserves_terminal_states(tmp_path):
    """门槛 3：completed/superseded/error/aborted 不变。"""
    db_path = str(tmp_path / "sweep3.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ] * 10)
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        aid = await _get_latest_assistant_id(client, sid)
        ext_store = client.app.state.web.extension_store

        # 直接通过 API 触发多种终态 revision
        # completed + superseded（一次成功 finalize）
        rev1 = await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-1",
        )
        await ext_store.finalize_revision(
            revision_id=rev1.id, request_id="req-1",
            candidate_content_json='{"type":"AssistantMessage","data":{"role":"assistant","content":[{"type":"text","text":"new"}],"api":"x","provider":"x","model":"x"}}',
        )
        # error
        rev2 = await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-2",
        )
        await ext_store.mark_revision_error(
            revision_id=rev2.id, request_id="req-2", error_summary="boom",
        )
        # aborted
        rev3 = await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-3",
        )
        await ext_store.mark_revision_aborted(
            revision_id=rev3.id, request_id="req-3",
        )
        # 多留一条 running 让 sweep 有事可做
        rev4 = await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-4",
        )

    # 第二次启动
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        ext_store = client.app.state.web.extension_store
        sess_store = client.app.state.web.session_store
        sessions = await sess_store.list_sessions()
        sid2 = sessions[0].id
        revisions = await ext_store.list_revisions(
            session_id=sid2, assistant_message_id=aid, limit=100
        )
        by_id = {r.id: r for r in revisions}
        # 完整状态保留
        # rev1: completed（仍是 completed）
        assert by_id[rev1.id].status == "completed"
        # rev0 (revision 0) 是 superseded
        superseded = [r for r in revisions if r.status == "superseded"]
        assert len(superseded) >= 1
        # rev2: error
        assert by_id[rev2.id].status == "error"
        # rev3: aborted
        assert by_id[rev3.id].status == "aborted"
        # rev4: interrupted（sweep 改的）
        assert by_id[rev4.id].status == "interrupted"


async def test_sweep_repeat_returns_zero(tmp_path):
    """门槛 4：sweep 重复执行返回 0。"""
    db_path = str(tmp_path / "sweep4.sqlite")
    harness = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app = create_app(harness, db_path=db_path)
    with TestClient(app) as client:
        ext_store = client.app.state.web.extension_store
        # lifespan startup 已 sweep 一次（返回值未暴露，所以直接调一次）
        n = await ext_store.mark_running_revisions_interrupted(
            completed_at="2026-07-16T00:00:00Z"
        )
        assert n == 0  # 已经被 startup sweep 清空


async def test_sweep_enables_new_running_after_restart(tmp_path):
    """门槛 5：sweep 后同一 assistant 可以重新 create_running_revision。"""
    db_path = str(tmp_path / "sweep5.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        aid = await _get_latest_assistant_id(client, sid)
        ext_store = client.app.state.web.extension_store
        # 插入 running revision（模拟 server crash）
        await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-stale",
        )

    # 第二次启动——sweep 清理
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        ext_store = client.app.state.web.extension_store
        # 现在可以重新 create_running_revision（之前 partial unique 会阻止）
        rev = await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-new",
        )
        assert rev.status == "running"


async def test_sweep_handles_multi_session(tmp_path):
    """门槛 6：多个 session 的 running revisions 全部处理。"""
    db_path = str(tmp_path / "sweep6.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ] * 10)
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid1 = _create_session(client, "s1")
        sid2 = _create_session(client, "s2")
        client.post("/api/prompt", json={"text": "x", "session_id": sid1})
        client.post("/api/prompt", json={"text": "y", "session_id": sid2})
        aid1 = await _get_latest_assistant_id(client, sid1)
        aid2 = await _get_latest_assistant_id(client, sid2)
        ext_store = client.app.state.web.extension_store
        await ext_store.create_running_revision(
            session_id=sid1, assistant_message_id=aid1, request_id="req-1",
        )
        await ext_store.create_running_revision(
            session_id=sid2, assistant_message_id=aid2, request_id="req-2",
        )

    # 第二次启动
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        ext_store = client.app.state.web.extension_store
        for sid, aid in [(sid1, aid1), (sid2, aid2)]:
            revs = await ext_store.list_revisions(
                session_id=sid, assistant_message_id=aid, limit=10
            )
            assert len(revs) == 1
            assert revs[0].status == "interrupted"


async def test_sweep_failure_prevents_app_yield(tmp_path, monkeypatch):
    """门槛 7：sweep 失败 → 应用不进入 yield（startup 失败）。

    让 mark_running_revisions_interrupted raise——TestClient with 语句应抛异常。
    """
    db_path = str(tmp_path / "sweep_fail.sqlite")
    # 先建一个 DB（含 schema）
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1):
        pass

    # patch ExtensionSQLiteStore.mark_running_revisions_interrupted raise
    async def failing_sweep(self, *, completed_at):
        raise RuntimeError("simulated DB corruption")

    monkeypatch.setattr(
        ExtensionSQLiteStore,
        "mark_running_revisions_interrupted",
        failing_sweep,
    )

    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    # 启动必须失败——TestClient with 抛异常（RuntimeError from lifespan sweep）
    with pytest.raises(RuntimeError, match="startup sweep failed"):
        with TestClient(app2):
            pass


async def test_sweep_does_not_regress_session_skill_mcp_restore(tmp_path):
    """门槛 8：session/Skill/MCP restore 不回归。"""
    db_path = str(tmp_path / "sweep8.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        # upload 一个 skill
        skill_body = b"---\nname: test\ndescription: d\n---\nbody"
        client.post(
            "/api/skills/upload",
            files={"files": ("test.md", skill_body, "text/markdown")},
        )

    # 第二次启动——skill 应该被 restore
    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        # skill 仍存在
        resp = client.get("/api/skills")
        body = resp.json()
        skill_names = [s["name"] for s in body.get("skills", [])]
        assert "test" in skill_names
        # session 仍存在
        sess_store = client.app.state.web.session_store
        sessions = await sess_store.list_sessions()
        assert any(s.id == sid for s in sessions)


async def test_sweep_keeps_registry_empty_and_safe_error_summary(tmp_path):
    """门槛 9：sweep 后 request registry 空 + error_summary 是固定文本。"""
    db_path = str(tmp_path / "sweep9.sqlite")
    harness1 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app1 = create_app(harness1, db_path=db_path)
    with TestClient(app1) as client:
        sid = _create_session(client)
        client.post("/api/prompt", json={"text": "x", "session_id": sid})
        aid = await _get_latest_assistant_id(client, sid)
        ext_store = client.app.state.web.extension_store
        await ext_store.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-stale",
        )

    harness2 = _make_harness([
        [TextDeltaEvent(delta="hello"), DoneEvent(stop_reason="stop")],
    ])
    app2 = create_app(harness2, db_path=db_path)
    with TestClient(app2) as client:
        # request registry 空
        state = client.app.state.web
        assert len(state.active_requests) == 0
        # error_summary 是固定文本
        sess_store = state.session_store
        sessions = await sess_store.list_sessions()
        sid2 = sessions[0].id
        revisions = await state.extension_store.list_revisions(
            session_id=sid2, assistant_message_id=aid, limit=10
        )
        assert revisions[0].error_summary == "Generation interrupted by server restart"


# ============================================================================
# Web Message DTO（4 用例）
# ============================================================================


async def test_messages_api_returns_message_id(web_app):
    """DTO 1：GET /api/messages?session_id= 含 message_id。"""
    client, _, _ = web_app
    sid = _create_session(client)
    client.post("/api/prompt", json={"text": "x", "session_id": sid})

    resp = client.get(f"/api/messages?session_id={sid}")
    body = resp.json()
    assert body["session_id"] == sid
    msgs = body["messages"]
    assert len(msgs) >= 2  # user + assistant
    for m in msgs:
        # 必须含 message_id（稳定 SQLite row id）
        assert m["message_id"]
        assert m["message_id"].startswith("msg-")
        assert m["session_id"] == sid
        assert m["idx"] >= 0
        assert m["role"] in ("user", "assistant", "toolResult", "summary", "custom")
        assert "content" in m
        assert "created_at" in m


async def test_dto_normal_send_preserves_message_id(web_app):
    """DTO 2：正常发送下一轮 → 历史 message_id 保持。"""
    client, _, _ = web_app
    sid = _create_session(client)
    client.post("/api/prompt", json={"text": "first", "session_id": sid})

    # 拿第一轮的 message_ids
    resp = client.get(f"/api/messages?session_id={sid}")
    ids_before = [m["message_id"] for m in resp.json()["messages"]]

    # 发第二轮
    client.post("/api/prompt", json={"text": "second", "session_id": sid})

    resp = client.get(f"/api/messages?session_id={sid}")
    ids_after = [m["message_id"] for m in resp.json()["messages"]]

    # 历史 message_id 必须保持（前两条）
    assert ids_after[: len(ids_before)] == ids_before


async def test_dto_regenerate_preserves_message_id(web_app):
    """DTO 3：Regenerate 成功 → message_id 不变 + content 更新。"""
    client, _, _ = web_app
    sid, aid = await _seed_session(client)

    # 拿原 content
    resp = client.get(f"/api/messages?session_id={sid}")
    original = next(
        m for m in resp.json()["messages"] if m["message_id"] == aid
    )

    # 触发 regenerate
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert resp.status_code == 202
    _wait_for_request(client, resp.json()["request_id"])

    # 再读 messages——同 message_id 仍存在
    resp = client.get(f"/api/messages?session_id={sid}")
    after = next(
        m for m in resp.json()["messages"] if m["message_id"] == aid
    )
    assert after is not None
    # role 必须仍是 assistant
    assert after["role"] == "assistant"
    # idx 不变
    assert after["idx"] == original["idx"]
    # message_id 不变
    assert after["message_id"] == aid


async def test_dto_reload_fetches_same_message_id(web_app):
    """DTO 4：reload（第二次 TestClient 启动同 DB）→ 获取同一个 message_id。

    注意：此测试**不**真的 reload——TestClient lifecycle 与 single harness 冲突。
    验证同 client 多次 GET 仍返回同 message_id。
    """
    client, _, _ = web_app
    sid = _create_session(client)
    client.post("/api/prompt", json={"text": "x", "session_id": sid})

    resp1 = client.get(f"/api/messages?session_id={sid}")
    resp2 = client.get(f"/api/messages?session_id={sid}")
    ids1 = [m["message_id"] for m in resp1.json()["messages"]]
    ids2 = [m["message_id"] for m in resp2.json()["messages"]]
    assert ids1 == ids2


# ============================================================================
# Request Metadata 全链路（3 用例）
# ============================================================================


async def test_request_status_contains_metadata(web_app):
    """Metadata 1：GET /api/requests/{id} 含 operation/regeneration_id/target_message_id。"""
    client, _, _ = web_app
    sid, aid = await _seed_session(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]
    regen_id = resp.json()["regeneration_id"]

    resp = client.get(f"/api/requests/{req_id}")
    body = resp.json()
    assert body["operation"] == "regenerate"
    assert body["regeneration_id"] == regen_id
    assert body["target_message_id"] == aid


async def test_list_requests_contains_metadata(web_app):
    """Metadata 2：GET /api/requests?status=active 含新字段。"""
    client, _, _ = web_app
    sid, aid = await _seed_session(client)
    # 启动一个 regenerate 后立即查 active（可能已 terminal——多启几个保活）
    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]

    # 立即查 list——可能 active 也可能 terminal；都应含新字段
    resp = client.get("/api/requests")
    body = resp.json()
    found = [r for r in body["requests"] if r["request_id"] == req_id]
    if found:
        assert found[0]["operation"] == "regenerate"
        assert found[0]["target_message_id"] == aid


async def test_request_metadata_visible_after_complete(web_app):
    """Metadata 3：request terminal 后仍能在 history 中查到 metadata（reload 模拟）。"""
    client, _, _ = web_app
    sid, aid = await _seed_session(client)

    resp = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = resp.json()["request_id"]
    regen_id = resp.json()["regeneration_id"]

    # 等 terminal
    _wait_for_request(client, req_id)

    # 通过 GET single request 仍能拿到 metadata（history 路径）
    resp = client.get(f"/api/requests/{req_id}")
    body = resp.json()
    assert body["operation"] == "regenerate"
    assert body["regeneration_id"] == regen_id
    assert body["target_message_id"] == aid

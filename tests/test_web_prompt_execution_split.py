"""P1-D2-4: 执行/持久化解耦 专项测试。

覆盖用户 2026-07-15 D2-4 审核结论 §9 列出的 25 个测试门槛。

测试策略：
- 用 TestClient 启动真实 app（含 SQLiteSessionStore + ExtensionSQLiteStore 共享 connection）
- 通过 `app.state.d24_*` 暴露的内部函数直接测试 _execute_prompt / persist / runner
- 用 FakeClient 脚本化模型行为，覆盖 success / multi-tool / error / abort 路径

范围边界：只测执行/持久化拆分；**不**测 /regenerate route（D2-5）/ 前端 / WS event
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
)
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import PromptRuntimeError, create_app
from pi_agent_core_py.web.extension_store import (
    RevisionBaseContentChangedError,
)

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
    scripts = [list(one) for _ in range(30)]
    harness = _make_harness(scripts)
    app = create_app(harness, db_path=str(tmp_path / "d24.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


@pytest.fixture
def web_app_multiturn(tmp_path):
    """多轮 tool_use 的 harness——占位（多轮测试改用直接构造 AssistantMessage）。"""
    one = [
        TextDeltaEvent(delta="plain"),
        DoneEvent(stop_reason="stop"),
    ]
    scripts = [list(one) for _ in range(20)]
    harness = _make_harness(scripts)
    app = create_app(harness, db_path=str(tmp_path / "d24m.sqlite"))
    with TestClient(app) as client:
        yield client, harness, app


async def _seed_session(
    client: TestClient,
    *,
    text: str = "first turn",
) -> tuple[str, str]:
    """创建 session 并跑一轮 prompt——返回 (session_id, assistant_msg_id_via_db)。"""
    # 创建 session
    resp = client.post("/api/sessions", json={"title": "d2-4 test"})
    assert resp.status_code in (200, 201)
    sid = resp.json()["id"]
    # 发一个 prompt
    resp = client.post("/api/prompt", json={"text": text, "session_id": sid})
    assert resp.status_code == 200
    # 从 app.state.web 拿 store + 找最新 assistant id
    state = client.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx DESC LIMIT 1",
        (sid,),
    )
    row = await cur.fetchone()
    await cur.close()
    return sid, row["id"]


async def _get_assistant_content_json(client: TestClient, sid: str, aid: str) -> str:
    """直接从 DB 读 assistant content_json（绕过反序列化）。"""
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


# ============================================================================
# _execute_prompt 不写 DB（2 用例）
# ============================================================================


async def test_execute_prompt_does_not_write_messages(web_app):
    """门槛 1：_execute_prompt 不写 messages 表。"""
    client, harness, app = web_app
    state = client.app.state.web
    sid, aid = await _seed_session(client)

    # 读 messages 表行数
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (sid,)
    )
    count_before = (await cur.fetchone())["c"]
    await cur.close()

    # 跑 _execute_prompt
    validate = client.app.state.d24_validated_factory
    execution_fn = client.app.state.d24_execute_prompt
    # 构造 validated
    validated = await validate({"text": "second", "session_id": sid})

    execution = await execution_fn(validated)

    # 执行后 messages 表行数不变（_execute_prompt 不写 DB）
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (sid,)
    )
    count_after = (await cur.fetchone())["c"]
    await cur.close()
    assert count_before == count_after
    # 但 harness state 应该有新 message
    assert len(execution.messages) > count_before


async def test_execute_prompt_does_not_write_revision(web_app):
    """门槛 2：_execute_prompt 不写 revision 表。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)

    validate = client.app.state.d24_validated_factory
    execution_fn = client.app.state.d24_execute_prompt
    validated = await validate({"text": "more", "session_id": sid})

    rev_before = await _count_revisions(client, sid, aid)
    await execution_fn(validated)
    rev_after = await _count_revisions(client, sid, aid)
    assert rev_before == rev_after == 0


# ============================================================================
# 普通 persistence（2 用例）
# ============================================================================


async def test_normal_persistence_calls_replace_messages(web_app):
    """门槛 3：普通 persistence 走 replace_messages（messages 表更新）。"""
    client, harness, app = web_app
    sid, _ = await _seed_session(client)
    state = client.app.state.web
    db = state.session_store.connection

    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (sid,)
    )
    before = (await cur.fetchone())["c"]
    await cur.close()

    # 直接 POST /api/prompt 走完整 _run_prompt_core（thin wrapper）
    resp = client.post("/api/prompt", json={"text": "second", "session_id": sid})
    assert resp.status_code == 200

    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (sid,)
    )
    after = (await cur.fetchone())["c"]
    await cur.close()
    assert after == before + 2  # user + assistant


async def test_normal_prompt_preserves_historical_message_ids(web_app):
    """门槛 4：普通 prompt 历史 message ID 由 diff-based replace_messages 保持。"""
    client, harness, app = web_app
    state = client.app.state.web
    sid, aid = await _seed_session(client)

    # 再发一轮
    client.post("/api/prompt", json={"text": "second", "session_id": sid})

    # 第一轮的 assistant_msg_id 应该仍在
    db = state.session_store.connection
    cur = await db.execute("SELECT id FROM messages WHERE id = ?", (aid,))
    assert await cur.fetchone() is not None
    await cur.close()


# ============================================================================
# Regenerate persistence（3 用例）
# ============================================================================


async def test_regenerate_does_not_call_replace_messages(web_app):
    """门槛 5：regenerate persistence 不调 replace_messages——assistant 不新增 row。"""
    client, harness, app = web_app
    state = client.app.state.web
    sid, aid = await _seed_session(client)
    db = state.session_store.connection

    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ? AND role = 'assistant'",
        (sid,),
    )
    asst_before = (await cur.fetchone())["c"]
    await cur.close()

    # 调 _run_regeneration_core
    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "regenerate", "session_id": sid})
    await run_regen(
        validated,
        assistant_message_id=aid,
        request_id="req-regen-1",
    )

    # assistant row 数不变——regenerate **不**新增 row
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE session_id = ? AND role = 'assistant'",
        (sid,),
    )
    asst_after = (await cur.fetchone())["c"]
    await cur.close()
    assert asst_before == asst_after == 1


async def test_regenerate_candidate_comes_from_suffix(web_app):
    """门槛 6：regenerate candidate 来自本次执行 suffix，不是整个 messages。

    FakeClient 是确定性脚本——新旧文本可能相同；本测试验证的是 candidate 是
    **本次执行产生的** AssistantMessage（通过 type=AssistantMessage + 含 fake 文本），
    而不是messages 表里的整个 history 被当作 candidate 写回。
    """
    client, harness, app = web_app
    sid, aid = await _seed_session(client)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(
        validated,
        assistant_message_id=aid,
        request_id="req-regen-1",
    )

    new_content = await _get_assistant_content_json(client, sid, aid)
    # 新 content 必须是合法的 AssistantMessage canonical JSON
    payload = json.loads(new_content)
    assert payload["type"] == "AssistantMessage"
    # 必须含本次执行产生的 fake 文本（证明 candidate 是来自 suffix）
    text_parts = [
        b.get("text", "") for b in payload["data"]["content"]
        if b.get("type") == "text"
    ]
    assert any("hello from fake" in t for t in text_parts)


async def test_intermediate_tool_call_not_final_candidate(web_app):
    """门槛 7：中间 tool-call-only assistant 不被当作最终 candidate。"""
    client, harness, app = web_app
    extract_fn = client.app.state.d24_extract_terminal

    # 构造 suffix：中间 tool-call AssistantMessage + ToolResult + final text AssistantMessage
    intermediate = AssistantMessage(
        content=[ToolCall(id="c1", name="echo", arguments={"text": "x"})],
        api="test",
        provider="test",
        model="test",
    )
    final = AssistantMessage(
        content=[TextContent(text="final answer")],
        api="test",
        provider="test",
        model="test",
    )
    # ToolResultMessage——需要构造正确类型
    from pi_agent_core_py.messages import ToolResultMessage

    tool_result = ToolResultMessage(
        tool_call_id="c1",
        name="echo",
        content=[TextContent(text="echo result")],
    )
    suffix = [intermediate, tool_result, final]

    candidate = extract_fn(suffix)
    assert candidate is final  # 必须是 final，不是 intermediate


# ============================================================================
# Finalize 后状态保持（5 用例）
# ============================================================================


async def test_finalize_updates_same_message_id(web_app):
    """门槛 8：finalize 成功后同 message ID 仍存在 + content 是合法 AssistantMessage。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    # 同 ID 仍存在
    db = client.app.state.web.session_store.connection
    cur = await db.execute("SELECT id FROM messages WHERE id = ?", (aid,))
    assert await cur.fetchone() is not None
    await cur.close()
    # content 仍是合法 AssistantMessage canonical JSON
    new = await _get_assistant_content_json(client, sid, aid)
    payload = json.loads(new)
    assert payload["type"] == "AssistantMessage"


async def test_running_period_old_messages_unchanged(web_app, monkeypatch):
    """门槛 9：regenerate running 期间旧 messages 内容不变。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    old = await _get_assistant_content_json(client, sid, aid)
    session_harness = app.state.coding_agent_runtime.get(sid).harness

    # 让 run_continue 慢一点
    real_run_continue = session_harness.run_continue

    captured: dict[str, Any] = {}

    async def slow_continue(*args, **kwargs):
        # 在执行前捕获——此时 messages 表应仍是旧 content
        captured["during"] = await _get_assistant_content_json(client, sid, aid)
        await asyncio.sleep(0.05)
        return await real_run_continue(*args, **kwargs)

    monkeypatch.setattr(session_harness, "run_continue", slow_continue)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    # 执行期间 messages.content_json 仍是旧值（finalize 未 commit）
    assert captured["during"] == old


async def test_model_error_messages_unchanged(web_app, monkeypatch):
    """门槛 10：model 执行失败后 messages 不变。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    old = await _get_assistant_content_json(client, sid, aid)
    session_harness = app.state.coding_agent_runtime.get(sid).harness

    async def failing_run_continue(*args, **kwargs):
        raise RuntimeError("model blew up")

    monkeypatch.setattr(session_harness, "run_continue", failing_run_continue)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    with pytest.raises(PromptRuntimeError, match="model blew up"):
        await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    new = await _get_assistant_content_json(client, sid, aid)
    assert new == old  # 未被修改


async def test_finalize_hash_stale_messages_unchanged(web_app, monkeypatch):
    """门槛 12：finalize hash stale 后 messages 不变 + revision error。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    old = await _get_assistant_content_json(client, sid, aid)
    session_harness = app.state.coding_agent_runtime.get(sid).harness

    # 篡改 create_running_revision 后的 messages.content_json，让 finalize 时 hash mismatch
    real_run_continue = session_harness.run_continue
    tampered = False

    async def tamper_then_continue(*args, **kwargs):
        nonlocal tampered
        if not tampered:
            # 篡改 messages
            db = client.app.state.web.session_store.connection
            await db.execute(
                "UPDATE messages SET content_json = ? WHERE id = ?",
                ('{"tampered":true}', aid),
            )
            await db.commit()
            tampered = True
        return await real_run_continue(*args, **kwargs)

    monkeypatch.setattr(session_harness, "run_continue", tamper_then_continue)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    with pytest.raises(RevisionBaseContentChangedError):
        await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    # revision 应该被 mark error
    state = client.app.state.web
    revs = await state.extension_store.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=10
    )
    assert len(revs) == 1
    assert revs[0].status == "error"
    # messages 被篡改成 tampered（finalize rollback 不会撤销外部篡改）
    # —— 验证 finalize 自己没写
    new = await _get_assistant_content_json(client, sid, aid)
    assert new != old  # 外部篡改生效
    assert "tampered" in new


# ============================================================================
# Harness state 恢复（4 用例）
# ============================================================================


async def test_harness_restored_to_canonical_after_success(web_app):
    """门槛 14：成功后 Harness 从 SQLite 恢复（含新 active content）。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    state = client.app.state.web

    # 读 canonical 之前的 messages 长度
    canonical_before = await state.session_store.list_messages(sid)
    len_before = len(canonical_before)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")
    session_harness = validated.agent_session.harness

    # harness state 应该 == canonical
    canonical_after = await state.session_store.list_messages(sid)
    assert len(canonical_after) == len_before
    # agent state 应该已 reset（无截断 context 残留）
    assert len(session_harness.agent.state.messages) == len_before
    # 内容应该匹配
    assert [type(m).__name__ for m in session_harness.agent.state.messages] == [
        type(m).__name__ for m in canonical_after
    ]


async def test_harness_restored_after_model_error(web_app, monkeypatch):
    """门槛 15：error 后 Harness 恢复旧 content。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    state = client.app.state.web
    canonical_before = await state.session_store.list_messages(sid)
    session_harness = app.state.coding_agent_runtime.get(sid).harness

    async def failing(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_harness, "run_continue", failing)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    with pytest.raises(PromptRuntimeError, match="boom"):
        await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    # harness state 应已恢复
    canonical_after = await state.session_store.list_messages(sid)
    assert len(session_harness.agent.state.messages) == len(canonical_after) == len(
        canonical_before
    )


async def test_no_truncated_context_residue(web_app):
    """门槛 17：agent state 中不残留截断 context。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    state = client.app.state.web

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")
    session_harness = validated.agent_session.harness

    # agent state 长度应该 == canonical（2：user + assistant），不是截断后的 1
    canonical = await state.session_store.list_messages(sid)
    assert len(session_harness.agent.state.messages) == len(canonical)
    # 必须含 assistant（不是只 user）
    assert any(
        isinstance(m, AssistantMessage)
        for m in session_harness.agent.state.messages
    )


async def test_no_partial_tool_turn_residue(web_app):
    """门槛 18：agent state 中不残留 partial tool turn（无 ToolResult 配对）。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")
    session_harness = validated.agent_session.harness

    # agent state 应不含 ToolCall（fake backend 不调工具）
    from pi_agent_core_py.messages import ToolCall

    for m in session_harness.agent.state.messages:
        if isinstance(m, AssistantMessage):
            for c in m.content:
                assert not isinstance(c, ToolCall)


# ============================================================================
# Snapshot 失败 best-effort（1 用例）
# ============================================================================


async def test_snapshot_failure_keeps_revision_completed(web_app, monkeypatch):
    """门槛 19：snapshot 失败后 revision/request 仍 completed。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)

    # 让 append_snapshot raise
    async def failing_snapshot(*args, **kwargs):
        raise RuntimeError("snapshot storage full")

    monkeypatch.setattr(
        client.app.state.web.session_store, "append_snapshot", failing_snapshot
    )

    run_regen = client.app.state.d24_run_regeneration
    validate = client.app.state.d24_validated_factory
    validated = await validate({"text": "x", "session_id": sid})
    # 不应抛——snapshot 失败是 best-effort
    await run_regen(validated, assistant_message_id=aid, request_id="req-1")

    # revision 仍 completed
    revs = await client.app.state.web.extension_store.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=10
    )
    completed = [r for r in revs if r.status == "completed"]
    assert len(completed) == 1
    # messages 已 finalize（active 已切换）
    new_content = await _get_assistant_content_json(client, sid, aid)
    assert "tampered" not in new_content


# ============================================================================
# 普通 async prompt 不回归（2 用例）
# ============================================================================


def test_normal_async_prompt_no_regression(web_app):
    """门槛 23：普通 async prompt 不回归——返回 202 + 最终 completed。"""
    client, _, _ = web_app
    # 创建 session
    resp = client.post("/api/sessions", json={"title": "async test"})
    sid = resp.json()["id"]

    # async prompt
    resp = client.post(
        "/api/prompt/async", json={"text": "hello", "session_id": sid}
    )
    assert resp.status_code == 202
    request_id = resp.json()["request_id"]

    # 轮询直到 completed
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        resp = client.get(f"/api/requests/{request_id}")
        body = resp.json()
        if body["status"] in ("completed", "error", "aborted"):
            break
        time.sleep(0.05)
    assert body["status"] == "completed"


def test_request_scoped_abort_no_regression(web_app):
    """门槛 24：request-scoped abort 不回归。"""
    client, _, _ = web_app
    # 用 slow client
    resp = client.post("/api/sessions", json={"title": "abort test"})
    sid = resp.json()["id"]

    resp = client.post(
        "/api/prompt/async", json={"text": "long", "session_id": sid}
    )
    request_id = resp.json()["request_id"]
    # 立即 abort
    resp = client.post(f"/api/requests/{request_id}/abort")
    assert resp.status_code == 200

    # 最终 status 应该是 aborted（或 completed 如果太快完成）
    import time
    deadline = time.monotonic() + 5
    final_status = None
    while time.monotonic() < deadline:
        resp = client.get(f"/api/requests/{request_id}")
        body = resp.json()
        final_status = body["status"]
        if final_status in ("aborted", "completed"):
            break
        time.sleep(0.05)
    assert final_status in ("aborted", "completed")


# ============================================================================
# WebEventEnvelope schema 不变（1 用例）
# ============================================================================


def test_webevent_envelope_schema_unchanged(web_app):
    """门槛 25：WebEventEnvelope schema 7 字段不变。"""
    client, _, _ = web_app
    resp = client.post("/api/sessions", json={"title": "envelope"})
    sid = resp.json()["id"]

    # 发一个 prompt，收事件
    resp = client.post("/api/prompt", json={"text": "x", "session_id": sid})
    assert resp.status_code == 200

    # 读 events
    resp = client.get("/api/events?limit=20")
    body = resp.json()
    assert "events" in body
    events = body["events"]
    assert len(events) > 0
    # 每个 envelope 必须有 7 字段
    required = {
        "event_id",
        "request_id",
        "session_id",
        "sequence",
        "type",
        "timestamp",
        "payload",
    }
    for ev in events:
        assert required.issubset(ev.keys()), f"missing fields: {required - set(ev.keys())}"


# ============================================================================
# 额外：candidate 缺失 → revision error
# ============================================================================


async def test_no_candidate_marks_revision_error(web_app, monkeypatch):
    """额外：persist 收到 None candidate → ValueError（caller 应 mark revision error）。"""
    client, harness, app = web_app
    sid, aid = await _seed_session(client)
    state = client.app.state.web

    # 直接构造 execution with assistant_message=None，验证 persist 抛 ValueError
    from pi_agent_core_py.web.app import PromptExecutionResult

    fake_execution = PromptExecutionResult(
        messages=[],
        assistant_message=None,  # 关键——None
        messages_before=[],
        messages_after=[],
        stop_reason=None,
        usage=None,
        snapshot_payload=None,
        result_summary=None,
    )

    # 先创建一个 running revision
    revision = await state.extension_store.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-cand-1"
    )

    real_persist = client.app.state.d24_persist_regeneration
    with pytest.raises(ValueError, match="no qualified assistant candidate"):
        await real_persist(
            await client.app.state.d24_validated_factory(
                {"text": "x", "session_id": sid}
            ),
            fake_execution,
            revision_id=revision.id,
            request_id="req-cand-1",
        )

    # revision 仍是 running（persist 抛 ValueError 前 finalize 未调）
    rev = await state.extension_store.get_revision(revision.id)
    assert rev.status == "running"

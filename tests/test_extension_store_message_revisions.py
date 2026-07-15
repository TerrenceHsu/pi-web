"""P1-D2-3: Revision Repository 专项测试。

审核要求的测试门槛（按用户 2026-07-15 D2-3 审核结论 §"D2-3 测试要求"）：

Create（9）
1.  创建 running revision
2.  revision number 从 1 开始
3.  后续 number 单调增长
4.  base hash 基于原始 content_json
5.  非 assistant 拒绝
6.  session 不匹配拒绝
7.  非最新 assistant 拒绝
8.  同 assistant 第二个 running 被拒绝
9.  request_id 重复被拒绝

Finalize（12）
1.  首次成功创建 revision 0
2.  revision 0 保存旧回答
3.  candidate 成为 completed
4.  messages 同一 ID 内容更新
5.  message idx 和 created_at 不变
6.  第二次成功把旧 completed 改成 superseded
7.  每个 assistant 最多一个 completed
8.  base hash 变化时拒绝
9.  request_id 不匹配拒绝
10. 重复 finalize 幂等
11. aborted/error/interrupted 不可 finalize
12. finalize SQL 失败完整回滚

Terminal state（7）
1. running→error
2. running→aborted
3. running→interrupted（startup sweep）
4. 终态不能互相转换
5. startup sweep 只处理 running
6. sweep 重复执行返回 0
7. error_summary 截断到 500

Query/Cleanup（8）
1. history 按 revision number DESC
2. before_revision_number 分页正确
3. limit 最大值限制
4. session delete cascade
5. replace_messages 删除 message 时清理 revision
6. revision 表操作不影响 Skill/MCP
7. 普通 message API 不回归
8. limit 最小值 clamp

范围边界：只测 repository；**不**测 API / Harness / 前端（留 D2-4+）
"""
from __future__ import annotations

import hashlib

import pytest

from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from pi_agent_core_py.session_sqlite import SQLiteSessionStore
from pi_agent_core_py.web.extension_store import (
    ExtensionSQLiteStore,
    RevisionAlreadyRunningError,
    RevisionBaseContentChangedError,
    RevisionNotFoundError,
    RevisionRequestConflictError,
    RevisionStateTransitionError,
    RevisionTargetNotAssistantError,
    RevisionTargetNotFoundError,
    RevisionTargetNotLatestError,
)

# 默认运行（不标 slow）——纯 SQLite 单元测试，无外部依赖

# ============================================================================
# fixtures
# ============================================================================


def _assistant_msg(text: str = "hello") -> AssistantMessage:
    """构造一个 valid AssistantMessage。"""
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="test",
        provider="test",
        model="test",
    )


@pytest.fixture
async def shared(tmp_path):
    """session_store + ext_store 共享 connection + 一条 user/assistant 对话。

    返回 (session_store, ext_store, session_id, assistant_msg_id)。
    """
    db_path = str(tmp_path / "d23.sqlite")
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    sid_obj = await session_store.create_session(title="d2-3 test")
    sid = sid_obj.id
    await session_store.append_message(
        sid, UserMessage(content=[TextContent(text="hi")])
    )
    await session_store.append_message(sid, _assistant_msg("hello"))

    # AgentMessage 没有 id 字段——直接查 DB 取最新 assistant message id
    db_raw = session_store.connection
    cur = await db_raw.execute(
        "SELECT id FROM messages "
        "WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx DESC LIMIT 1",
        (sid,),
    )
    row = await cur.fetchone()
    await cur.close()
    assistant_msg_id = row["id"]

    ext_store = ExtensionSQLiteStore(db_path, connection=session_store.connection)
    await ext_store.init()
    try:
        yield session_store, ext_store, sid, assistant_msg_id
    finally:
        await ext_store.close()
        await session_store.close()


# ============================================================================
# Create（9）
# ============================================================================


async def test_create_running_revision_returns_object(shared):
    """门槛 Create 1：创建 running revision 返回完整对象。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid,
        assistant_message_id=aid,
        request_id="req-1",
    )
    assert rev.status == "running"
    assert rev.session_id == sid
    assert rev.assistant_message_id == aid
    assert rev.request_id == "req-1"
    assert rev.content_json is None
    assert rev.completed_at is None
    assert rev.error_summary is None
    assert rev.base_content_sha256  # 非空


async def test_revision_number_starts_from_1(shared):
    """门槛 Create 2：revision_number 从 1 开始。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    assert rev.revision_number == 1


async def test_revision_number_monotonic(shared):
    """门槛 Create 3：后续 number 单调增长。"""
    _, ext, sid, aid = shared
    # 第一次：rev 1（running）→ 标 aborted
    rev1 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    assert rev1.revision_number == 1
    await ext.mark_revision_aborted(
        revision_id=rev1.id, request_id="req-1"
    )
    # 第二次：rev 2
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )
    assert rev2.revision_number == 2


async def test_base_hash_from_raw_content_json(shared):
    """门槛 Create 4：base hash 直接基于 DB 中原始 content_json 字符串。"""
    _, ext, sid, aid = shared
    # 直接从 DB 读 content_json（绕过 Pydantic 重序列化）
    db = ext._require_db()
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    row = await cur.fetchone()
    await cur.close()
    expected_sha = hashlib.sha256(row["content_json"].encode("utf-8")).hexdigest()

    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    assert rev.base_content_sha256 == expected_sha


async def test_create_rejects_non_assistant_target(shared):
    """门槛 Create 5：目标 role 非 assistant → 拒绝。"""
    _, ext, sid, _ = shared
    # 找出 user message id
    db = ext._require_db()
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'user' LIMIT 1",
        (sid,),
    )
    user_id = (await cur.fetchone())["id"]
    await cur.close()

    with pytest.raises(RevisionTargetNotAssistantError):
        await ext.create_running_revision(
            session_id=sid, assistant_message_id=user_id, request_id="req-x"
        )


async def test_create_rejects_session_mismatch(shared):
    """门槛 Create 6：assistant_message_id 存在但 session 不匹配 → 拒绝。"""
    session_store, ext, sid, aid = shared
    # 在同一 session_store 上创建另一个 session
    other_sid_obj = await session_store.create_session(title="other")
    other_sid = other_sid_obj.id

    with pytest.raises(RevisionTargetNotFoundError):
        await ext.create_running_revision(
            session_id=other_sid,
            assistant_message_id=aid,  # 这个 aid 属于 sid，不属于 other_sid
            request_id="req-x",
        )


async def test_create_rejects_non_latest_assistant(shared):
    """门槛 Create 7：目标不是最新 assistant → 拒绝。"""
    session_store, ext, sid, old_aid = shared
    # 在已有 assistant 后再加一轮对话，让原 assistant 不再是最新
    await session_store.append_message(sid, UserMessage(content=[TextContent(text="q2")]))
    await session_store.append_message(sid, _assistant_msg("a2"))

    with pytest.raises(RevisionTargetNotLatestError):
        await ext.create_running_revision(
            session_id=sid,
            assistant_message_id=old_aid,  # 第一轮的 assistant
            request_id="req-x",
        )


async def test_create_rejects_second_running(shared):
    """门槛 Create 8：同 assistant 已有 running revision → 拒绝第二条。"""
    _, ext, sid, aid = shared
    await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    with pytest.raises(RevisionAlreadyRunningError):
        await ext.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id="req-2"
        )


async def test_create_rejects_duplicate_request_id(shared):
    """门槛 Create 9：request_id 已被占用 → 拒绝。"""
    session_store, ext, sid, aid = shared
    await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-dup"
    )
    # 加一个新 assistant（让原 assistant 不再是 latest，但 request_id 仍占）
    await session_store.append_message(sid, UserMessage(content=[TextContent(text="more")]))
    await session_store.append_message(sid, _assistant_msg("a2"))
    # 取最新 assistant id
    db = ext._require_db()
    cur = await db.execute(
        "SELECT id FROM messages "
        "WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx DESC LIMIT 1",
        (sid,),
    )
    new_aid = (await cur.fetchone())["id"]
    await cur.close()

    with pytest.raises(RevisionRequestConflictError):
        await ext.create_running_revision(
            session_id=sid, assistant_message_id=new_aid, request_id="req-dup"
        )


# ============================================================================
# Finalize（12）
# ============================================================================


async def test_finalize_first_success_creates_revision_zero(shared):
    """门槛 Finalize 1：第一次成功创建 revision 0。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    finalized = await ext.finalize_revision(
        revision_id=rev.id,
        request_id="req-1",
        candidate_content_json='{"role":"assistant","content":[{"type":"text","text":"new!"}],"api":"test","provider":"test","model":"test"}',
    )
    assert finalized.status == "completed"

    # revision 0 必须存在
    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=100)
    assert len(revs) == 2
    rev0 = next(r for r in revs if r.revision_number == 0)
    assert rev0.status == "superseded"
    assert rev0.request_id is None


async def test_revision_zero_saves_old_answer(shared):
    """门槛 Finalize 2：revision 0 保存 finalize 前的 messages.content_json。"""
    _, ext, sid, aid = shared
    # 直接从 DB 读原始 content_json（绕过 Pydantic 重序列化）
    db = ext._require_db()
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    original_content_json = (await cur.fetchone())["content_json"]
    await cur.close()

    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev.id,
        request_id="req-1",
        candidate_content_json='{"role":"assistant","new":true}',
    )

    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=100)
    rev0 = next(r for r in revs if r.revision_number == 0)
    assert rev0.content_json == original_content_json
    assert rev0.base_content_sha256 == hashlib.sha256(
        original_content_json.encode("utf-8")
    ).hexdigest()


async def test_finalize_candidate_becomes_completed(shared):
    """门槛 Finalize 3：candidate 写入 revision 的 content_json + status=completed。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    candidate = '{"role":"assistant","content":[{"type":"text","text":"final!"}]}'
    finalized = await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json=candidate,
    )
    assert finalized.status == "completed"
    assert finalized.content_json == candidate


async def test_finalize_updates_messages_same_id(shared):
    """门槛 Finalize 4：messages 同一 ID 的 content_json 被更新。"""
    session_store, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    candidate = '{"role":"assistant","content":[{"type":"text","text":"replaced!"}]}'
    await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json=candidate,
    )
    # 读 DB 验证 content_json 是新值
    db = ext._require_db()
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    new_content = (await cur.fetchone())["content_json"]
    await cur.close()
    assert new_content == candidate


async def test_finalize_preserves_idx_and_created_at(shared):
    """门槛 Finalize 5：message 的 idx 和 created_at 不变。"""
    _, ext, sid, aid = shared
    db = ext._require_db()
    cur = await db.execute(
        "SELECT idx, created_at FROM messages WHERE id = ?", (aid,)
    )
    before = await cur.fetchone()
    await cur.close()
    original_idx = before["idx"]
    original_created_at = before["created_at"]

    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json='{"new":true}',
    )

    cur = await db.execute(
        "SELECT idx, created_at FROM messages WHERE id = ?", (aid,)
    )
    after = await cur.fetchone()
    await cur.close()
    assert after["idx"] == original_idx
    assert after["created_at"] == original_created_at


async def test_finalize_second_success_marks_old_completed_superseded(shared):
    """门槛 Finalize 6：第二次成功 finalize 把上一个 completed 改成 superseded。"""
    _, ext, sid, aid = shared
    # 第一次 finalize
    rev1 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev1.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    # 第二次 finalize（基于第一次完成后的 active）
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )
    await ext.finalize_revision(
        revision_id=rev2.id, request_id="req-2",
        candidate_content_json='{"v":2}',
    )

    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=100)
    by_num = {r.revision_number: r for r in revs}
    # rev 1 应该变成 superseded
    assert by_num[1].status == "superseded"
    # rev 2 是 completed
    assert by_num[2].status == "completed"
    # rev 0 仍是 superseded（第一次创建后保持）
    assert by_num[0].status == "superseded"


async def test_at_most_one_completed_per_assistant(shared):
    """门槛 Finalize 7：每个 assistant 最多一个 completed。"""
    _, ext, sid, aid = shared
    rev1 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev1.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )
    await ext.finalize_revision(
        revision_id=rev2.id, request_id="req-2",
        candidate_content_json='{"v":2}',
    )
    # 查所有 completed——必须只有 1 条（最新的 rev2）
    db = ext._require_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM web_message_revisions "
        "WHERE assistant_message_id = ? AND status = 'completed'",
        (aid,),
    )
    assert (await cur.fetchone())["c"] == 1


async def test_finalize_rejects_on_base_hash_mismatch(shared):
    """门槛 Finalize 8：base_content_sha256 与当前 messages 不符 → 拒绝 + rollback。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    # 篡改 messages.content_json 让 sha 不匹配
    db = ext._require_db()
    await db.execute(
        "UPDATE messages SET content_json = ? WHERE id = ?",
        ('{"tampered":true}', aid),
    )
    await db.commit()

    with pytest.raises(RevisionBaseContentChangedError):
        await ext.finalize_revision(
            revision_id=rev.id, request_id="req-1",
            candidate_content_json='{"new":true}',
        )

    # revision 应仍是 running（rollback 了）
    rev_after = await ext.get_revision(rev.id)
    assert rev_after.status == "running"
    assert rev_after.content_json is None


async def test_finalize_rejects_request_id_mismatch(shared):
    """门槛 Finalize 9：request_id 与 revision 不符 → 拒绝（无 SQL 改动）。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    with pytest.raises(RevisionRequestConflictError):
        await ext.finalize_revision(
            revision_id=rev.id, request_id="wrong-req",
            candidate_content_json='{"x":1}',
        )
    # 没动状态
    rev_after = await ext.get_revision(rev.id)
    assert rev_after.status == "running"


async def test_finalize_idempotent_when_already_completed(shared):
    """门槛 Finalize 10：已 completed + request_id 匹配 → 幂等返回，不改 completed_at。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    finalized1 = await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    finalized2 = await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    assert finalized1.id == finalized2.id
    assert finalized1.status == finalized2.status == "completed"
    assert finalized1.completed_at == finalized2.completed_at
    # 不应该多创建 revision 0
    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=100)
    assert len(revs) == 2  # rev 0 + rev 1


async def test_finalize_rejects_terminal_states(shared):
    """门槛 Finalize 11：aborted / error / interrupted 不可 finalize。"""
    _, ext, sid, aid = shared
    # aborted
    rev1 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.mark_revision_aborted(revision_id=rev1.id, request_id="req-1")
    with pytest.raises(RevisionStateTransitionError):
        await ext.finalize_revision(
            revision_id=rev1.id, request_id="req-1",
            candidate_content_json='{"x":1}',
        )

    # error
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )
    await ext.mark_revision_error(
        revision_id=rev2.id, request_id="req-2",
        error_summary="something failed",
    )
    with pytest.raises(RevisionStateTransitionError):
        await ext.finalize_revision(
            revision_id=rev2.id, request_id="req-2",
            candidate_content_json='{"x":1}',
        )

    # interrupted
    affected = await ext.mark_running_revisions_interrupted(
        completed_at="2026-07-15T00:00:00Z"
    )
    assert affected == 0  # 没有 running
    # 把 rev2（error）当作 interrupted 模拟——直接改 status
    db = ext._require_db()
    await db.execute(
        "UPDATE web_message_revisions SET status = 'interrupted' WHERE id = ?",
        (rev2.id,),
    )
    await db.commit()
    with pytest.raises(RevisionStateTransitionError):
        await ext.finalize_revision(
            revision_id=rev2.id, request_id="req-2",
            candidate_content_json='{"x":1}',
        )


async def test_finalize_rollback_on_internal_failure(shared, monkeypatch):
    """门槛 Finalize 12：finalize 内部 SQL 失败 → 完整回滚（messages + revision 状态）。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    # 拿到 finalize 前的 messages 状态
    db = ext._require_db()
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    original_msg_content = (await cur.fetchone())["content_json"]
    await cur.close()

    # 篡改 finalize 流程：让 step 9（UPDATE messages）失败
    # 通过 monkeypatch _verify_target_assistant_latest 后再让 UPDATE 失败
    # 简化：直接用一段不合法 candidate 让 messages UPDATE 触发约束（不行——
    # messages.content_json 是 TEXT 无约束）。改用 monkeypatch 让 db.execute 在
    # 看到 "UPDATE messages" 时 raise。
    import aiosqlite

    real_execute = aiosqlite.Connection.execute

    async def patched_execute(self, sql, parameters=()):  # noqa: ANN001
        if "UPDATE messages" in sql and "content_json = ?" in sql:
            raise RuntimeError("simulated SQL failure on messages UPDATE")
        if len(parameters) == 0:
            return await real_execute(self, sql)
        return await real_execute(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", patched_execute)

    with pytest.raises(RuntimeError, match="simulated"):
        await ext.finalize_revision(
            revision_id=rev.id, request_id="req-1",
            candidate_content_json='{"will":"fail"}',
        )

    monkeypatch.undo()

    # 验证 messages 没变（rollback）
    cur = await db.execute(
        "SELECT content_json FROM messages WHERE id = ?", (aid,)
    )
    after = await cur.fetchone()
    await cur.close()
    assert after["content_json"] == original_msg_content
    # revision 仍 running（rollback）
    rev_after = await ext.get_revision(rev.id)
    assert rev_after.status == "running"


# ============================================================================
# Terminal state（7）
# ============================================================================


async def test_mark_error_transitions_running_to_error(shared):
    """门槛 Terminal 1：running → error。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    err = await ext.mark_revision_error(
        revision_id=rev.id, request_id="req-1",
        error_summary="model failed",
    )
    assert err.status == "error"
    assert err.error_summary == "model failed"
    assert err.completed_at is not None


async def test_mark_aborted_transitions_running_to_aborted(shared):
    """门槛 Terminal 2：running → aborted。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    ab = await ext.mark_revision_aborted(
        revision_id=rev.id, request_id="req-1"
    )
    assert ab.status == "aborted"
    assert ab.completed_at is not None


async def test_startup_sweep_marks_running_interrupted(shared):
    """门槛 Terminal 3：mark_running_revisions_interrupted 把 running 改 interrupted。"""
    _, ext, sid, aid = shared
    await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    affected = await ext.mark_running_revisions_interrupted(
        completed_at="2026-07-15T00:00:00Z"
    )
    assert affected == 1
    # 验证状态
    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=10)
    assert len(revs) == 1
    assert revs[0].status == "interrupted"
    assert revs[0].error_summary == "Generation interrupted by server restart"


async def test_terminal_states_cannot_transition(shared):
    """门槛 Terminal 4：终态不能互相转换（completed→error / error→aborted 等）。"""
    _, ext, sid, aid = shared
    # 先 finalize 让一条变成 completed
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    # completed → error 必须失败
    with pytest.raises(RevisionStateTransitionError):
        await ext.mark_revision_error(
            revision_id=rev.id, request_id="req-1",
            error_summary="too late",
        )

    # error → aborted（先把一条改成 error，再尝试 aborted）
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )
    await ext.mark_revision_error(
        revision_id=rev2.id, request_id="req-2",
        error_summary="err",
    )
    with pytest.raises(RevisionStateTransitionError):
        await ext.mark_revision_aborted(
            revision_id=rev2.id, request_id="req-2"
        )


async def test_terminal_mark_methods_idempotent(shared):
    """Terminal 幂等：已是目标状态 → 返回现有（不改 completed_at / error_summary）。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    err1 = await ext.mark_revision_error(
        revision_id=rev.id, request_id="req-1",
        error_summary="first",
    )
    err2 = await ext.mark_revision_error(
        revision_id=rev.id, request_id="req-1",
        error_summary="second",
    )
    assert err1.status == err2.status == "error"
    # 幂等：error_summary 不改（保持 first）
    assert err2.error_summary == "first"


async def test_startup_sweep_only_handles_running(shared):
    """门槛 Terminal 5：sweep 只处理 running，不动其它状态。"""
    _, ext, sid, aid = shared
    # 一条 running + 一条 completed
    rev1 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev1.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )
    # 加一条 running（基于刚刚完成的 active）
    rev2 = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-2"
    )

    affected = await ext.mark_running_revisions_interrupted(
        completed_at="2026-07-15T00:00:00Z"
    )
    assert affected == 1  # 只 rev2 是 running

    # rev1 仍 completed；rev2 变 interrupted
    assert (await ext.get_revision(rev1.id)).status == "completed"
    assert (await ext.get_revision(rev2.id)).status == "interrupted"


async def test_startup_sweep_repeat_returns_zero(shared):
    """门槛 Terminal 6：sweep 重复执行第二次返回 0。"""
    _, ext, sid, aid = shared
    await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    n1 = await ext.mark_running_revisions_interrupted(
        completed_at="2026-07-15T00:00:00Z"
    )
    n2 = await ext.mark_running_revisions_interrupted(
        completed_at="2026-07-15T00:00:00Z"
    )
    assert n1 == 1
    assert n2 == 0


async def test_error_summary_truncated_to_500(shared):
    """Terminal 7：error_summary 截断到 500 字符。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    long_msg = "x" * 2000
    err = await ext.mark_revision_error(
        revision_id=rev.id, request_id="req-1",
        error_summary=long_msg,
    )
    assert len(err.error_summary) == 500
    assert err.error_summary == "x" * 500


# ============================================================================
# Query / Cleanup（8）
# ============================================================================


async def test_list_revisions_desc_by_revision_number(shared):
    """门槛 Query 1：history 按 revision_number DESC 排序。"""
    _, ext, sid, aid = shared
    for i in range(3):
        rev = await ext.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id=f"req-{i}"
        )
        await ext.mark_revision_aborted(
            revision_id=rev.id, request_id=f"req-{i}"
        )

    revs = await ext.list_revisions(session_id=sid, assistant_message_id=aid, limit=100)
    assert [r.revision_number for r in revs] == [3, 2, 1]


async def test_before_revision_number_pagination(shared):
    """门槛 Query 2：before_revision_number 分页。"""
    _, ext, sid, aid = shared
    for i in range(5):
        rev = await ext.create_running_revision(
            session_id=sid, assistant_message_id=aid, request_id=f"req-{i}"
        )
        await ext.mark_revision_aborted(
            revision_id=rev.id, request_id=f"req-{i}"
        )

    page1 = await ext.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=2
    )
    assert [r.revision_number for r in page1] == [5, 4]
    page2 = await ext.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=2,
        before_revision_number=page1[-1].revision_number,
    )
    assert [r.revision_number for r in page2] == [3, 2]


async def test_limit_clamped_to_max_100(shared):
    """门槛 Query 3：limit 最大值限制到 100。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.mark_revision_aborted(revision_id=rev.id, request_id="req-1")

    # 传 1000——必须被 clamp 到 100，不报错
    revs = await ext.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=1000
    )
    assert len(revs) == 1


async def test_limit_clamped_to_min_1(shared):
    """门槛 Query 8：limit 最小值 clamp 到 1。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.mark_revision_aborted(revision_id=rev.id, request_id="req-1")

    # 传 0 或负数——必须被 clamp 到 1
    revs = await ext.list_revisions(
        session_id=sid, assistant_message_id=aid, limit=0
    )
    assert len(revs) == 1


async def test_session_delete_cascades_revisions(shared):
    """门槛 Query 4：session delete CASCADE 删 revisions（FK session_id CASCADE）。"""
    session_store, ext, sid, aid = shared
    await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    db = ext._require_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM web_message_revisions")
    assert (await cur.fetchone())["c"] == 1

    await session_store.delete_session(sid)

    cur = await db.execute("SELECT COUNT(*) AS c FROM web_message_revisions")
    assert (await cur.fetchone())["c"] == 0


async def test_replace_messages_cleans_orphan_revisions(shared):
    """门槛 Query 5：replace_messages 删除 assistant 时清理 revision（orphan 防护）。"""
    session_store, ext, sid, aid = shared
    # 给 assistant 创建一条 finalized revision——candidate 必须是合法
    # AssistantMessage 的 canonical JSON（{type, data} 包装），否则 list_messages
    # 反序列化失败
    candidate = (
        '{"type": "AssistantMessage", "data": '
        + _assistant_msg("regenerated").model_dump_json()
        + "}"
    )
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json=candidate,
    )

    # 现在 replace_messages 用**更短的**列表（删掉 assistant）——必须清理 revision
    msgs = await session_store.list_messages(sid)
    # AgentMessage 没 id——通过 list_messages 拿到对话内容，然后只保留 user 部分
    shorter = [m for m in msgs if m.role == "user"]
    await session_store.replace_messages(sid, shorter)

    db = ext._require_db()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM web_message_revisions "
        "WHERE assistant_message_id = ?",
        (aid,),
    )
    assert (await cur.fetchone())["c"] == 0


async def test_revision_ops_do_not_affect_skill_mcp(shared):
    """门槛 Query 6：revision 操作不影响 Skill/MCP 表。"""
    _, ext, sid, aid = shared
    # 先种 Skill + MCP server 数据
    await ext.upsert_uploaded_skill(
        name="test_skill",
        skill_json='{"name":"test_skill"}',
        raw_markdown="raw",
    )
    await ext.upsert_mcp_server(
        name="test_srv",
        command="echo",
        args=[],
        env_keys=[],
    )

    # 跑一堆 revision 操作
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-1"
    )
    await ext.finalize_revision(
        revision_id=rev.id, request_id="req-1",
        candidate_content_json='{"v":1}',
    )

    # Skill / MCP 仍在
    assert await ext.get_uploaded_skill("test_skill") is not None
    assert await ext.get_mcp_server("test_srv") is not None


async def test_normal_message_api_no_regression(shared):
    """门槛 Query 7：revision 表存在不影响普通 message API（append / list / replace）。"""
    session_store, ext, sid, aid = shared
    # revision 表已被 ext_store init 创建
    # 加一条 message
    await session_store.append_message(sid, _assistant_msg("more"))
    msgs = await session_store.list_messages(sid)
    assert len(msgs) == 3  # user + assistant + new assistant

    # replace_messages（不删 assistant，尾部追加）
    await session_store.replace_messages(
        sid,
        msgs + [UserMessage(content=[TextContent(text="extra")])],
    )
    msgs2 = await session_store.list_messages(sid)
    assert len(msgs2) == 4


# ============================================================================
# Get by request_id
# ============================================================================


async def test_get_revision_by_request_id(shared):
    """额外：get_revision_by_request_id 能反查。"""
    _, ext, sid, aid = shared
    rev = await ext.create_running_revision(
        session_id=sid, assistant_message_id=aid, request_id="req-unique"
    )
    found = await ext.get_revision_by_request_id("req-unique")
    assert found is not None
    assert found.id == rev.id

    assert await ext.get_revision_by_request_id("req-not-exist") is None


async def test_get_revision_returns_none_for_unknown(shared):
    """额外：get_revision 不存在返回 None。"""
    _, ext, _, _ = shared
    assert await ext.get_revision("rev-nonexistent") is None


async def test_mark_terminal_unknown_revision_raises(shared):
    """额外：mark_error 对不存在的 revision 抛 RevisionNotFoundError。"""
    _, ext, _, _ = shared
    with pytest.raises(RevisionNotFoundError):
        await ext.mark_revision_error(
            revision_id="rev-nonexistent",
            request_id="req-x",
            error_summary="x",
        )

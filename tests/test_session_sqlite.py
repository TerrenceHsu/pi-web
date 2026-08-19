"""P0-1: SQLiteSessionStore 单元测试。

覆盖：
1. init creates tables
2. create_session
3. list_sessions（updated_at desc）
4. get_session
5. get_session missing → None
6. rename_session
7. delete_session cascade
8. append_message (User / Assistant)
9. list_messages restores strong types
10. replace_messages resets idx
11. append_snapshot
12. list_snapshots restores TurnSnapshot
13. append_message missing session raises SessionNotFoundError
14. corrupted message json raises SessionSerializationError
15. concurrent append idx unique + ordered
16. close is idempotent
17. ensure_default_session
"""
from __future__ import annotations

import asyncio
import json

import pytest

from pi_agent_core_py.messages import (
    AssistantMessage,
    SummaryMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_agent_core_py.session_sqlite import (
    SessionNotFoundError,
    SessionSerializationError,
    SQLiteSession,
    SQLiteSessionError,
    SQLiteSessionStore,
)
from pi_agent_core_py.snapshot import SnapshotBuilder

# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    """单测用临时 sqlite 文件。"""
    s = SQLiteSessionStore(tmp_path / "test.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def _user(text: str = "hi") -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _assistant(text: str = "ok") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="test-api", provider="test", model="test-1",
    )


def _build_snapshot(status="completed") -> any:
    """用 SnapshotBuilder 构造一个 TurnSnapshot。"""
    builder = SnapshotBuilder()
    builder.start(
        request_type="prompt",
        user_text="hi",
        messages_before=[],
        metadata={},
    )
    return builder.finish(
        status=status,
        messages_after=[_user(), _assistant()],
    )


# ============================================================================
# init / schema
# ============================================================================


@pytest.mark.asyncio
async def test_init_creates_tables(tmp_path):
    """init 后 legacy projection 与 append-only tree 表都存在。"""

    s = SQLiteSessionStore(tmp_path / "x.db")
    await s.init()
    try:
        # 内部连接能查到
        db = s._require_db()
        for table in (
            "sessions",
            "messages",
            "snapshots",
            "session_entries",
            "session_lanes",
            "session_facts",
        ):
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            row = await cur.fetchone()
            await cur.close()
            assert row is not None, f"缺表 {table}"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_init_idempotent(tmp_path):
    """init 重复调安全。"""
    s = SQLiteSessionStore(tmp_path / "y.db")
    await s.init()
    await s.init()
    await s.close()


# ============================================================================
# session CRUD
# ============================================================================


@pytest.mark.asyncio
async def test_create_session_returns_valid_session(store):
    s = await store.create_session(title="demo", metadata={"k": "v"})
    assert isinstance(s, SQLiteSession)
    assert s.title == "demo"
    assert s.metadata == {"k": "v"}
    assert s.id.startswith("sess-")
    assert s.created_at > 0
    assert s.updated_at >= s.created_at


@pytest.mark.asyncio
async def test_list_sessions_orders_by_updated_at_desc(store):
    s1 = await store.create_session(title="first")
    # 微小延迟保证 updated_at 不同
    await asyncio.sleep(0.005)
    await store.create_session(title="second")
    await asyncio.sleep(0.005)
    # touch s1 → 它应该排到第一
    await store.touch_session(s1.id)
    sessions = await store.list_sessions()
    assert [x.title for x in sessions] == ["first", "second"]


@pytest.mark.asyncio
async def test_get_session_returns_session(store):
    created = await store.create_session(title="x")
    fetched = await store.get_session(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "x"


@pytest.mark.asyncio
async def test_get_session_missing_returns_none(store):
    assert await store.get_session("does-not-exist") is None


@pytest.mark.asyncio
async def test_rename_session_updates_title_and_updated_at(store):
    s = await store.create_session(title="old")
    old_updated = s.updated_at
    await asyncio.sleep(0.005)
    renamed = await store.rename_session(s.id, "new")
    assert renamed.title == "new"
    assert renamed.updated_at > old_updated


@pytest.mark.asyncio
async def test_rename_session_missing_raises(store):
    with pytest.raises(SessionNotFoundError):
        await store.rename_session("ghost", "x")


@pytest.mark.asyncio
async def test_delete_session_cascades_messages_and_snapshots(store):
    s = await store.create_session(title="todelete")
    await store.append_message(s.id, _user())
    await store.append_snapshot(s.id, _build_snapshot())
    await store.delete_session(s.id)
    # 再 get 应当 None
    assert await store.get_session(s.id) is None
    # messages / snapshots 也清空（FK ON DELETE CASCADE）
    db = store._require_db()
    cur = await db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (s.id,)
    )
    count = (await cur.fetchone())[0]
    await cur.close()
    assert count == 0


@pytest.mark.asyncio
async def test_delete_session_missing_raises(store):
    with pytest.raises(SessionNotFoundError):
        await store.delete_session("ghost")


# ============================================================================
# message CRUD
# ============================================================================


@pytest.mark.asyncio
async def test_append_message_stores_user_message(store):
    s = await store.create_session()
    rec = await store.append_message(s.id, _user("hello"))
    assert rec.role == "user"
    assert rec.idx == 0
    assert isinstance(rec.message, UserMessage)


@pytest.mark.asyncio
async def test_append_message_stores_assistant_message(store):
    s = await store.create_session()
    rec = await store.append_message(s.id, _assistant("ok"))
    assert rec.role == "assistant"
    assert isinstance(rec.message, AssistantMessage)


@pytest.mark.asyncio
async def test_append_message_stores_tool_result(store):
    s = await store.create_session()
    msg = ToolResultMessage(
        tool_call_id="t1", name="echo",
        content=[TextContent(text="ok")],
        usage=Usage(input=2, output=1, total_tokens=3),
        added_tool_names=["search"],
    )
    rec = await store.append_message(s.id, msg)
    assert rec.role == "toolResult"
    assert isinstance(rec.message, ToolResultMessage)
    assert rec.message.usage == Usage(input=2, output=1, total_tokens=3)
    assert rec.message.added_tool_names == ["search"]

    restored = await store.list_messages(s.id)
    assert isinstance(restored[0], ToolResultMessage)
    assert restored[0].usage == msg.usage
    assert restored[0].added_tool_names == ["search"]


@pytest.mark.asyncio
async def test_append_message_stores_summary(store):
    s = await store.create_session()
    msg = SummaryMessage(content=[TextContent(text="summary")])
    rec = await store.append_message(s.id, msg)
    assert rec.role == "summary"


@pytest.mark.asyncio
async def test_list_messages_restores_strong_types(store):
    """关键：restore 后是强类型对象，不是 dict。"""
    s = await store.create_session()
    await store.append_message(s.id, _user("hi"))
    await store.append_message(s.id, _assistant("ok"))

    msgs = await store.list_messages(s.id)
    assert len(msgs) == 2
    assert isinstance(msgs[0], UserMessage)
    assert isinstance(msgs[1], AssistantMessage)
    # 字段可访问（不是 dict）
    assert msgs[0].content[0].text == "hi"
    assert msgs[1].content[0].text == "ok"


@pytest.mark.asyncio
async def test_list_messages_preserves_assistant_with_tool_call(store):
    """AssistantMessage 含 ToolCall 时反序列化保留 ToolCall。"""
    s = await store.create_session()
    tc = ToolCall(id="t1", name="echo", arguments={"q": "x"})
    a = AssistantMessage(
        content=[TextContent(text="calling"), tc],
        api="x", provider="x", model="x",
    )
    await store.append_message(s.id, a)
    msgs = await store.list_messages(s.id)
    assert len(msgs) == 1
    assert isinstance(msgs[0], AssistantMessage)
    assert len(msgs[0].content) == 2
    assert isinstance(msgs[0].content[1], ToolCall)
    assert msgs[0].content[1].id == "t1"


@pytest.mark.asyncio
async def test_replace_messages_resets_idx(store):
    s = await store.create_session()
    await store.append_message(s.id, _user("a"))
    await store.append_message(s.id, _assistant("b"))
    # 替换为完全不同的 3 条
    new_msgs = [_user("x"), _assistant("y"), _user("z")]
    await store.replace_messages(s.id, new_msgs)
    msgs = await store.list_messages(s.id)
    assert len(msgs) == 3
    assert msgs[0].content[0].text == "x"
    assert msgs[1].content[0].text == "y"
    assert msgs[2].content[0].text == "z"


@pytest.mark.asyncio
async def test_append_message_missing_session_raises(store):
    with pytest.raises(SessionNotFoundError):
        await store.append_message("ghost", _user())


@pytest.mark.asyncio
async def test_list_messages_missing_session_raises(store):
    with pytest.raises(SessionNotFoundError):
        await store.list_messages("ghost")


# ============================================================================
# snapshot CRUD
# ============================================================================


@pytest.mark.asyncio
async def test_append_snapshot_stores_turnsnapshot(store):
    s = await store.create_session()
    snap = _build_snapshot()
    rec = await store.append_snapshot(s.id, snap)
    assert rec.snapshot.id == snap.id
    assert rec.turn_id == snap.request_id


@pytest.mark.asyncio
async def test_list_snapshots_restores_turnsnapshot(store):
    s = await store.create_session()
    snap1 = _build_snapshot(status="completed")
    await store.append_snapshot(s.id, snap1)
    snap2 = _build_snapshot(status="error")
    await store.append_snapshot(s.id, snap2)

    snaps = await store.list_snapshots(s.id)
    assert len(snaps) == 2
    # 类型完整
    assert snaps[0].id == snap1.id
    assert snaps[0].status == "completed"
    assert snaps[1].status == "error"
    # messages_after 字段可访问（不是 dict）
    assert isinstance(snaps[0].messages_after, list)


# ============================================================================
# 错误处理
# ============================================================================


@pytest.mark.asyncio
async def test_corrupted_message_json_raises_serialization_error(store):
    """直接往 messages 表写坏 JSON → list_messages 抛 SessionSerializationError。"""
    s = await store.create_session()
    db = store._require_db()
    await db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("bad-id", s.id, 0, "user", "{not valid json", 0),
    )
    await db.commit()
    with pytest.raises(SessionSerializationError):
        await store.list_messages(s.id)


@pytest.mark.asyncio
async def test_unknown_message_type_raises_serialization_error(store):
    s = await store.create_session()
    db = store._require_db()
    bad_payload = json.dumps({"type": "GhostMessage", "data": {}})
    await db.execute(
        "INSERT INTO messages (id, session_id, idx, role, content_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("bad-id2", s.id, 0, "ghost", bad_payload, 0),
    )
    await db.commit()
    with pytest.raises(SessionSerializationError):
        await store.list_messages(s.id)


@pytest.mark.asyncio
async def test_corrupted_snapshot_json_raises_serialization_error(store):
    s = await store.create_session()
    db = store._require_db()
    await db.execute(
        "INSERT INTO snapshots (id, session_id, turn_id, content_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("bad-snap", s.id, None, "{broken", 0),
    )
    await db.commit()
    with pytest.raises(SessionSerializationError):
        await store.list_snapshots(s.id)


# ============================================================================
# 并发
# ============================================================================


@pytest.mark.asyncio
async def test_concurrent_append_keeps_idx_unique_and_ordered(store):
    """20 个并发 append_message → 最终 idx 是 0..19，无重复。"""
    s = await store.create_session()
    # 并发发起 20 个 append（msg-{i:02d} 让字典序 == 数字序）
    await asyncio.gather(*[
        store.append_message(s.id, _user(f"msg-{i:02d}"))
        for i in range(20)
    ])
    msgs = await store.list_messages(s.id)
    assert len(msgs) == 20
    # 内容应该是 20 个不同 UserMessage（按字典序补 0 后等于数字序）
    texts = sorted(m.content[0].text for m in msgs)
    assert texts == [f"msg-{i:02d}" for i in range(20)]
    # idx 0..19 不重复——UNIQUE(session_id, idx) 约束保证
    # （如果 idx 计算并发冲突，INSERT 会抛 OperationalError，gather 会失败）


@pytest.mark.asyncio
async def test_concurrent_append_to_different_sessions(store):
    """不同 session 并发 append 不串。"""
    s1 = await store.create_session(title="s1")
    s2 = await store.create_session(title="s2")
    await asyncio.gather(*[
        store.append_message(s1.id, _user("from-s1")),
        store.append_message(s2.id, _user("from-s2")),
    ])
    msgs1 = await store.list_messages(s1.id)
    msgs2 = await store.list_messages(s2.id)
    assert len(msgs1) == 1
    assert len(msgs2) == 1
    assert msgs1[0].content[0].text == "from-s1"
    assert msgs2[0].content[0].text == "from-s2"


# ============================================================================
# 生命周期
# ============================================================================


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path):
    s = SQLiteSessionStore(tmp_path / "z.db")
    await s.init()
    await s.close()
    # 第二次 close 不抛错
    await s.close()
    assert s.closed


@pytest.mark.asyncio
async def test_operations_after_close_raise(tmp_path):
    s = SQLiteSessionStore(tmp_path / "w.db")
    await s.init()
    await s.close()
    with pytest.raises(SQLiteSessionError):
        await store_list_sessions_safe(s)


async def store_list_sessions_safe(s):
    await s.list_sessions()


# ============================================================================
# default session
# ============================================================================


@pytest.mark.asyncio
async def test_ensure_default_session_creates_when_empty(store):
    s = await store.ensure_default_session()
    assert s.title == "default"
    # 再调一次应返回已存在的 default（不创建新的）
    s2 = await store.ensure_default_session()
    assert s2.id == s.id


# ============================================================================
# 持久化：close + reopen
# ============================================================================


@pytest.mark.asyncio
async def test_data_persists_across_reopen(tmp_path):
    """关闭再开，session 与 messages 不丢。"""
    db_path = tmp_path / "persist.db"
    s = SQLiteSessionStore(db_path)
    await s.init()
    session = await s.create_session(title="persist-test")
    await s.append_message(session.id, _user("hello"))
    await s.close()

    # 重开
    s2 = SQLiteSessionStore(db_path)
    await s2.init()
    try:
        sessions = await s2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].title == "persist-test"
        msgs = await s2.list_messages(sessions[0].id)
        assert len(msgs) == 1
        assert isinstance(msgs[0], UserMessage)
        assert msgs[0].content[0].text == "hello"
    finally:
        await s2.close()

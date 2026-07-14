"""D2 阻塞检查 #1: Message ID 稳定性 characterization test.

**背景**（审核 D2 批准范围）：
> 在实现 revision schema 前，必须先写 characterization test：
> - 创建第一轮 user msg_u1 + assistant msg_a1
> - 执行第二轮普通 prompt → replace_messages
> - 重新查询：msg_u1 ID 是否仍然相同 / msg_a1 ID 是否仍然相同
>
> 如果当前 replace_messages() 会删除并重新生成全部 ID，
> 那么必须先在 Web persistence 层修正。

**当前实现**（session_sqlite.py:505-536）：
`replace_messages` 是 `DELETE FROM messages WHERE session_id=?` + 循环 INSERT
每条消息用 `_gen_id("msg") = f"msg-{ms_timestamp}-{uuid.uuid4().hex[:8]}"`
所以历史 message ID **每次 replace_messages 都重新生成**。

**预期修复后的行为**：
- 正常追加对话（不截断历史）→ 历史 message ID 永远不变
- 历史 hash 稳定 → revision 表的 `assistant_message_id` 外键可可靠引用
- 只有显式 truncate / branch 才允许 ID 变化

测试目前用 xfail 标记，D2 修复 `replace_messages` 后移除 xfail。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from pi_agent_core_py.messages import AssistantMessage, TextContent, UserMessage
from pi_agent_core_py.session_sqlite import SQLiteSessionStore


@pytest.fixture
async def store():
    with tempfile.TemporaryDirectory() as td:
        s = SQLiteSessionStore(db_path=os.path.join(td, "test.sqlite"))
        await s.init()
        yield s
        await s.close()


async def _fetch_msg_ids(store: SQLiteSessionStore, sid: str) -> list[str]:
    """直接查 messages 表 id 列——绕过 list_messages（只返回 AgentMessage）。"""
    db = store._require_db()
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? ORDER BY idx ASC",
        (sid,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [r["id"] for r in rows]


# ============================================================================
# Characterization: replace_messages 当前会重新生成历史 ID（待修复）
# ============================================================================


@pytest.mark.xfail(
    reason="D2 prerequisite: replace_messages() 当前 DELETE+INSERT 重新生成所有 ID；"
    "D2 修复后此测试应 PASS。修复策略 = 按 idx diff 同步（UPDATE 优先于 DELETE）。",
    strict=True,
)
async def test_replace_messages_preserves_historical_ids(store):
    """replace_messages 不应改变未修改历史消息的 ID。

    场景：
    1. 写入 u1 + a1（first turn）
    2. list_messages → 拿到当前 messages list
    3. 模拟第二轮 prompt：list + append(u2 + a2) 后用 replace_messages 覆盖
    4. 断言：u1 / a1 的 row ID 在 replace_messages 后仍然相同
    """
    session = await store.create_session(title="T")
    sid = session.id

    # First turn
    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, AssistantMessage(content=[TextContent(text="A1")]))

    ids_after_turn1 = await _fetch_msg_ids(store, sid)
    assert len(ids_after_turn1) == 2
    u1_id, a1_id = ids_after_turn1

    # Second turn: harness 当前行为是 list_messages → 加新 → replace_messages 覆盖
    msgs = await store.list_messages(sid)
    msgs.append(UserMessage(content=[TextContent(text="Q2")]))
    msgs.append(AssistantMessage(content=[TextContent(text="A2")]))
    await store.replace_messages(sid, msgs)

    # 断言：u1 / a1 ID 不变；u2 / a2 是新增
    ids_after_turn2 = await _fetch_msg_ids(store, sid)
    assert len(ids_after_turn2) == 4
    assert ids_after_turn2[0] == u1_id, (
        f"u1 ID changed: was {u1_id}, now {ids_after_turn2[0]}"
    )
    assert ids_after_turn2[1] == a1_id, (
        f"a1 ID changed: was {a1_id}, now {ids_after_turn2[1]}"
    )


@pytest.mark.xfail(
    reason="D2 prerequisite: 同上——append_message 的 row id 在后续 replace_messages 后会丢失。",
    strict=True,
)
async def test_append_message_id_survives_subsequent_replace(store):
    """append_message 返回的 id 应该在后续 replace_messages 中存活。

    场景：Web app 当前 prompt 流程：
      1. await store.list_messages(sid)
      2. harness.run_prompt(text, initial_messages=...)
      3. await store.replace_messages(sid, final_messages)

    如果 step 3 把所有 row id 都换了，那么任何引用 row id 的子系统
    （包括未来的 revision 表）的引用都会失效。
    """
    session = await store.create_session(title="T")
    sid = session.id

    # Append 一条 user，记录返回的 row id
    stored = await store.append_message(sid, UserMessage(content=[TextContent(text="hi")]))
    original_id = stored.id

    # 模拟 prompt 流程：list → append assistant → replace_messages
    msgs = await store.list_messages(sid)
    msgs.append(AssistantMessage(content=[TextContent(text="answer")]))
    await store.replace_messages(sid, msgs)

    # user row id 不应变化
    ids = await _fetch_msg_ids(store, sid)
    assert original_id in ids, (
        f"original user row id {original_id} disappeared after replace_messages; "
        f"current ids: {ids}"
    )


# ============================================================================
# Non-regression: append_message 单独调用时 row id 稳定（已经 PASS）
# ============================================================================


async def test_append_message_returns_distinct_stable_ids(store):
    """append_message 多次调用产生不同 id——单独调用时已经稳定。

    这是当前实现已经满足的不变量——`_gen_id` 用 uuid 保证唯一。
    """
    session = await store.create_session(title="T")
    sid = session.id

    r1 = await store.append_message(sid, UserMessage(content=[TextContent(text="a")]))
    r2 = await store.append_message(sid, UserMessage(content=[TextContent(text="b")]))
    r3 = await store.append_message(sid, UserMessage(content=[TextContent(text="c")]))

    ids = {r1.id, r2.id, r3.id}
    assert len(ids) == 3, "append_message 应产生互不相同的 id"
    assert all(r.id.startswith("msg-") for r in [r1, r2, r3])

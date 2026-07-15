"""D2-1: Message ID 稳定性 + diff-based replace_messages.

**背景**（D2 设计 docs/P1_D2_REGENERATE_DESIGN.md）：
> revision 表需要稳定的 `assistant_message_id` 外键。如果 replace_messages
> 每次都 DELETE + INSERT 重新生成所有 row id，revision FK 会失效。

**当前实现（D2-1）**：
`replace_messages` 改为 diff-based：
- 共同前缀同 role 同 content：不动（id + created_at 都保持）
- 共同前缀同 role 不同 content：UPDATE content_json，保留 id + created_at
- role mismatch / 长度差异：从 mismatch 位置 DELETE + INSERT 新尾部
- 任一步失败：transaction ROLLBACK

**不变量**：
1. 正常追加对话（不截断历史）→ 历史 row id + created_at 永远不变
2. 历史 row id 稳定 → revision 表 FK 可可靠引用
3. 同 role 内容修订 → id 不变（regenerate 关键场景）
4. 只有显式 truncate / role mismatch / 新追加 才生成新 id
"""
from __future__ import annotations

import os
import tempfile

import pytest

from pi_agent_core_py.messages import AssistantMessage, TextContent, UserMessage
from pi_agent_core_py.session_sqlite import SQLiteSessionStore


def _assistant(text: str = "ok") -> AssistantMessage:
    """和 test_session_sqlite.py 一致的 AssistantMessage 构造（带必需字段）。"""
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="test-api", provider="test", model="test-1",
    )


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


async def _fetch_rows(store: SQLiteSessionStore, sid: str) -> list[dict]:
    """读 messages 表全字段（id / idx / role / content_json / created_at）。"""
    db = store._require_db()
    cur = await db.execute(
        "SELECT id, idx, role, content_json, created_at "
        "FROM messages WHERE session_id = ? ORDER BY idx ASC",
        (sid,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [
        {
            "id": r["id"],
            "idx": r["idx"],
            "role": r["role"],
            "content_json": r["content_json"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ============================================================================
# #1 / #2 / #3: 初次写入 / 尾部追加 / 历史 ID（user + assistant）保持
# ============================================================================


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

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, _assistant("A1"))

    ids_after_turn1 = await _fetch_msg_ids(store, sid)
    assert len(ids_after_turn1) == 2
    u1_id, a1_id = ids_after_turn1

    msgs = await store.list_messages(sid)
    msgs.append(UserMessage(content=[TextContent(text="Q2")]))
    msgs.append(_assistant("A2"))
    await store.replace_messages(sid, msgs)

    ids_after_turn2 = await _fetch_msg_ids(store, sid)
    assert len(ids_after_turn2) == 4
    assert ids_after_turn2[0] == u1_id, (
        f"u1 ID changed: was {u1_id}, now {ids_after_turn2[0]}"
    )
    assert ids_after_turn2[1] == a1_id, (
        f"a1 ID changed: was {a1_id}, now {ids_after_turn2[1]}"
    )


# ============================================================================
# #13: 普通第二轮 prompt 后第一轮 user row id 保持
# ============================================================================


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

    stored = await store.append_message(sid, UserMessage(content=[TextContent(text="hi")]))
    original_id = stored.id

    msgs = await store.list_messages(sid)
    msgs.append(_assistant("answer"))
    await store.replace_messages(sid, msgs)

    ids = await _fetch_msg_ids(store, sid)
    assert original_id in ids, (
        f"original user row id {original_id} disappeared after replace_messages; "
        f"current ids: {ids}"
    )


# ============================================================================
# #1: 初次写入 ID 唯一（每条 row 拿到独立 id）
# ============================================================================


async def test_first_write_generates_distinct_ids(store):
    """replace_messages 首次写入（DB 为空）应给每条 message 生成唯一 id。"""
    session = await store.create_session(title="T")
    sid = session.id

    msgs = [
        UserMessage(content=[TextContent(text="Q1")]),
        _assistant("A1"),
        UserMessage(content=[TextContent(text="Q2")]),
    ]
    await store.replace_messages(sid, msgs)

    ids = await _fetch_msg_ids(store, sid)
    assert len(ids) == 3
    assert len(set(ids)) == 3, f"ids not unique: {ids}"
    assert all(i.startswith("msg-") for i in ids)


# ============================================================================
# #4: 同 role 内容更新时 ID 不变
# ============================================================================


async def test_same_role_content_update_preserves_id(store):
    """同 idx + 同 role + 不同 content → UPDATE content_json，id 保持。

    D2 regenerate 场景：assistant 回答被重写，但 row id 必须稳定
    让 revision.assistant_message_id FK 可靠引用。
    """
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(
        sid, _assistant("old answer")
    )

    ids_before = await _fetch_msg_ids(store, sid)

    # 同结构、同 role、不同 content——模拟 regenerate
    msgs = [
        UserMessage(content=[TextContent(text="Q1")]),
        _assistant("NEW answer"),
    ]
    await store.replace_messages(sid, msgs)

    ids_after = await _fetch_msg_ids(store, sid)
    assert ids_after == ids_before, (
        f"id changed on pure content update: was {ids_before}, now {ids_after}"
    )


# ============================================================================
# #5: 更新内容后 created_at 不变
# ============================================================================


async def test_content_update_preserves_created_at(store):
    """UPDATE content_json 时 created_at 保持——审计时间戳不变。

    created_at 表示"该 row 首次写入 DB 的时间"，不是"最后修改时间"。
    """
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(
        sid, _assistant("old")
    )

    rows_before = await _fetch_rows(store, sid)
    created_before = [r["created_at"] for r in rows_before]

    msgs = [
        UserMessage(content=[TextContent(text="Q1")]),
        _assistant("new"),
    ]
    await store.replace_messages(sid, msgs)

    rows_after = await _fetch_rows(store, sid)
    created_after = [r["created_at"] for r in rows_after]
    assert created_after == created_before, (
        f"created_at changed on pure content update: "
        f"was {created_before}, now {created_after}"
    )


# ============================================================================
# #6: 新追加的消息获得新 id（!= 任何历史 id）
# ============================================================================


async def test_new_appended_messages_get_fresh_ids(store):
    """追加尾部时新 row 必须拿到新 id，不能复用历史 id。"""
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, _assistant("A1"))

    ids_before = set(await _fetch_msg_ids(store, sid))

    msgs = await store.list_messages(sid)
    msgs.append(UserMessage(content=[TextContent(text="Q2")]))
    msgs.append(_assistant("A2"))
    await store.replace_messages(sid, msgs)

    ids_after = await _fetch_msg_ids(store, sid)
    new_ids = set(ids_after) - ids_before
    assert len(new_ids) == 2, (
        f"expected 2 fresh ids for new rows, got {new_ids}; full list: {ids_after}"
    )


# ============================================================================
# #7: 缩短尾部——超出部分被正确删除
# ============================================================================


async def test_truncate_tail_deletes_excess_rows(store):
    """输入 list 比当前短 → 多余尾部 row 应被删除。

    场景：3 条 → 替换为 1 条（前 1 条 content 相同）。
    """
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, _assistant("A1"))
    await store.append_message(sid, UserMessage(content=[TextContent(text="Q2")]))

    ids_before = await _fetch_msg_ids(store, sid)
    assert len(ids_before) == 3

    # 截断到只保留第一条（content 不变）
    msgs = [UserMessage(content=[TextContent(text="Q1")])]
    await store.replace_messages(sid, msgs)

    ids_after = await _fetch_msg_ids(store, sid)
    assert len(ids_after) == 1
    assert ids_after[0] == ids_before[0], (
        f"first row id should be preserved: was {ids_before[0]}, now {ids_after[0]}"
    )


# ============================================================================
# #8: role mismatch 只重建不匹配位置及其后方（前缀不动）
# ============================================================================


async def test_role_mismatch_rebuilds_from_mismatch_onwards(store):
    """前缀相同（role + content）→ 保留；从 mismatch 起 DELETE + INSERT。

    场景：[u, a, u] → [u, a, a]（最后一条 role 从 user 变 assistant）。
    前 2 条保留 id；第 3 条 role 不同 → DELETE + INSERT。
    """
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, _assistant("A1"))
    await store.append_message(sid, UserMessage(content=[TextContent(text="Q2")]))

    ids_before = await _fetch_msg_ids(store, sid)

    # 前 2 条同 role 同 content；第 3 条 role 改为 assistant
    msgs = [
        UserMessage(content=[TextContent(text="Q1")]),
        _assistant("A1"),
        _assistant("Q2-as-assistant"),
    ]
    await store.replace_messages(sid, msgs)

    ids_after = await _fetch_msg_ids(store, sid)
    assert len(ids_after) == 3
    # 前 2 条 id 不变
    assert ids_after[0] == ids_before[0]
    assert ids_after[1] == ids_before[1]
    # 第 3 条必须新 id（role mismatch 触发重建）
    assert ids_after[2] != ids_before[2], (
        f"row 2 should have new id after role change; "
        f"before={ids_before[2]}, after={ids_after[2]}"
    )


# ============================================================================
# #9: idx 始终从 0 连续
# ============================================================================


async def test_idx_always_continuous_from_zero(store):
    """无论 UPDATE/DELETE/INSERT 路径，最终 idx 必须是 [0, N) 连续。"""
    session = await store.create_session(title="T")
    sid = session.id

    # 场景 1：初次写入
    await store.replace_messages(
        sid,
        [
            UserMessage(content=[TextContent(text="a")]),
            _assistant("b"),
            UserMessage(content=[TextContent(text="c")]),
        ],
    )
    rows = await _fetch_rows(store, sid)
    assert [r["idx"] for r in rows] == [0, 1, 2]

    # 场景 2：尾部追加（前缀不变）
    msgs = await store.list_messages(sid)
    msgs.append(_assistant("d"))
    await store.replace_messages(sid, msgs)
    rows = await _fetch_rows(store, sid)
    assert [r["idx"] for r in rows] == [0, 1, 2, 3]

    # 场景 3：truncate
    await store.replace_messages(
        sid, [UserMessage(content=[TextContent(text="only")])]
    )
    rows = await _fetch_rows(store, sid)
    assert [r["idx"] for r in rows] == [0]

    # 场景 4：role mismatch 触发尾部重建
    await store.append_message(sid, _assistant("x"))
    await store.append_message(sid, UserMessage(content=[TextContent(text="y")]))
    # 现在 [u("only"), a("x"), u("y")]——把第 2 条 role 从 a 变 u
    await store.replace_messages(
        sid,
        [
            UserMessage(content=[TextContent(text="only")]),
            UserMessage(content=[TextContent(text="x-as-user")]),
            UserMessage(content=[TextContent(text="y")]),
        ],
    )
    rows = await _fetch_rows(store, sid)
    assert [r["idx"] for r in rows] == [0, 1, 2]


# ============================================================================
# #10: SQL 失败时完全回滚（transaction atomicity）
# ============================================================================


async def test_sql_failure_rolls_back_transaction(store, monkeypatch):
    """任一步 SQL 失败 → ROLLBACK 整个变更，DB 回到调用前状态。

    用 monkeypatch 让 _gen_id 抛错——模拟 INSERT 失败（触发 ROLLBACK 路径）。
    """
    from pi_agent_core_py import session_sqlite as mod

    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))
    await store.append_message(sid, _assistant("A1"))

    rows_before = await _fetch_rows(store, sid)
    ids_before = [r["id"] for r in rows_before]

    # 让任何 _gen_id 调用抛错——会触发 INSERT 路径时失败
    original_gen_id = mod._gen_id

    def failing_gen_id(prefix):
        raise RuntimeError("simulated SQL failure during replace")

    monkeypatch.setattr(mod, "_gen_id", failing_gen_id)

    # 用 role mismatch 路径触发 INSERT：[u, a] → [u, u]（第 2 条 role 变）
    msgs = [
        UserMessage(content=[TextContent(text="Q1")]),
        UserMessage(content=[TextContent(text="A1-as-user")]),
    ]

    with pytest.raises(RuntimeError, match="simulated SQL failure"):
        await store.replace_messages(sid, msgs)

    monkeypatch.setattr(mod, "_gen_id", original_gen_id)

    # 验证 DB 状态 == 调用前状态（rollback 成功）
    rows_after = await _fetch_rows(store, sid)
    assert len(rows_after) == len(rows_before), (
        f"row count changed after rollback: "
        f"before={len(rows_before)}, after={len(rows_after)}"
    )
    assert [r["id"] for r in rows_after] == ids_before, (
        f"ids changed after rollback: before={ids_before}, "
        f"after={[r['id'] for r in rows_after]}"
    )
    # content 也应保持原样（UPDATE 没发生，因为 role mismatch 在 i=1）
    assert rows_after[0]["content_json"] == rows_before[0]["content_json"]
    assert rows_after[1]["content_json"] == rows_before[1]["content_json"]


# ============================================================================
# #11: session.updated_at 总是更新
# ============================================================================


async def test_session_updated_at_always_advances(store):
    """无论是否修改 messages，session.updated_at 必须更新到当前时间。"""
    session = await store.create_session(title="T")
    sid = session.id

    await store.append_message(sid, UserMessage(content=[TextContent(text="Q1")]))

    # 拿到 updated_at baseline
    s1 = await store.get_session(sid)
    assert s1 is not None
    updated_before = s1.updated_at

    # 等待至少 1ms 确保 timestamp 真的会变
    import asyncio

    await asyncio.sleep(0.005)

    # replace_messages 用相同的 messages（无任何变化）
    msgs = await store.list_messages(sid)
    await store.replace_messages(sid, msgs)

    s2 = await store.get_session(sid)
    assert s2 is not None
    assert s2.updated_at > updated_before, (
        f"session.updated_at should advance: "
        f"before={updated_before}, after={s2.updated_at}"
    )


# ============================================================================
# #12: list_messages 顺序与 idx 升序一致
# ============================================================================


async def test_list_messages_order_matches_idx(store):
    """list_messages 必须按 idx 升序——diff-based 后顺序仍正确。"""
    session = await store.create_session(title="T")
    sid = session.id

    # 写入 3 条
    await store.replace_messages(
        sid,
        [
            UserMessage(content=[TextContent(text="first")]),
            _assistant("second"),
            UserMessage(content=[TextContent(text="third")]),
        ],
    )

    # 截断 + 追加 + 内容修订混合
    await store.replace_messages(
        sid,
        [
            UserMessage(content=[TextContent(text="first-revised")]),  # UPDATE
            _assistant("second"),   # 不动
            UserMessage(content=[TextContent(text="third")]),         # 不动
            _assistant("fourth"),   # 新 INSERT
        ],
    )

    msgs = await store.list_messages(sid)
    assert len(msgs) == 4
    assert msgs[0].role == "user"
    assert msgs[0].content[0].text == "first-revised"
    assert msgs[1].role == "assistant"
    assert msgs[1].content[0].text == "second"
    assert msgs[2].role == "user"
    assert msgs[2].content[0].text == "third"
    assert msgs[3].role == "assistant"
    assert msgs[3].content[0].text == "fourth"


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

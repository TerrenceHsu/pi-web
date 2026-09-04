"""Extension schema migrations, including historical v1→v2 and current v2→v3.

审核要求的 20 个测试门槛（按用户 2026-07-15 审核结论 §"D2-2 测试门槛"）：

1.  全新 DB 直接为当前版本
2.  v1 DB 连续升级到当前版本
3.  migration 后 C1 三张表数据保持
4.  session/messages 数据保持
5.  Skill 数据保持
6.  MCP server 数据保持
7.  disabled tool 数据保持
8.  migration 重复 initialize 幂等
9.  当前版本重开不重复 DDL
10. 未来版本在任何 schema 修改前失败
11. migration 中途异常后 version 仍为 1
12. migration 中途异常后 revision table 不残留
13. status CHECK 拒绝非法状态
14. revision_number CHECK 拒绝负数
15. request_id partial unique 生效
16. running partial unique 生效
17. completed partial unique 生效
18. 同一 assistant 可以有多个 superseded
19. session delete CASCADE 删除 revisions
20. revision 表为空时原 messages API 不受影响

实现说明：
- 测试 INSERT revisions 的场景使用 **shared connection 模式**（session_store + ext_store
  共享同一 aiosqlite.Connection）——这是 production 的真实路径（web/app.py:283），
  也让 FK to sessions(id) 能在 INSERT 时通过校验。
- migration 异常测试通过 monkeypatch 替换 `_migrate_v1_to_v2` 为镜像实现 +
  注入 failure——保留真实 try/except/rollback 模式以验证 transaction 语义。

范围边界：只测 schema + migration；**不**测 revision CRUD / regenerate API / 前端（留 D2-3+）
"""
from __future__ import annotations

import aiosqlite
import pytest

from pi_agent_core_py.session_sqlite import SQLiteSessionStore
from pi_agent_core_py.web import extension_store as ext_module
from pi_agent_core_py.web.extension_store import (
    SCHEMA_VERSION,
    ExtensionSQLiteStore,
    ExtensionStoreError,
)

# 默认运行（不标 slow）——纯 SQLite 单元测试，无外部依赖

# ============================================================================
# v1 schema 常量——用于手工"种"v1 DB（绕过新版 init() 的 v2 逻辑）
# ============================================================================

_V1_EXTENSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS web_extension_schema_meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS web_uploaded_skills (
    name                TEXT PRIMARY KEY,
    skill_json          TEXT NOT NULL,
    raw_markdown        TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    source_kind         TEXT NOT NULL DEFAULT 'upload'
                        CHECK (source_kind IN ('upload')),
    content_sha256      TEXT NOT NULL DEFAULT '',
    last_restore_error  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_mcp_servers (
    name                TEXT PRIMARY KEY,
    transport           TEXT NOT NULL DEFAULT 'stdio'
                        CHECK (transport IN ('stdio')),
    command             TEXT NOT NULL,
    args_json           TEXT NOT NULL DEFAULT '[]',
    desired_enabled     INTEGER NOT NULL DEFAULT 0
                        CHECK (desired_enabled IN (0, 1)),
    env_keys_json       TEXT NOT NULL DEFAULT '[]',
    last_restore_error  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_mcp_disabled_tools (
    server_name         TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    disabled_at         TEXT NOT NULL,
    PRIMARY KEY (server_name, tool_name),
    FOREIGN KEY (server_name)
        REFERENCES web_mcp_servers(name)
        ON DELETE CASCADE
);

INSERT OR IGNORE INTO web_extension_schema_meta (id, version) VALUES (1, 1);
"""


# ============================================================================
# helpers
# ============================================================================


async def _seed_v1_db(
    db_path: str,
    *,
    include_session_data: bool = False,
    include_extension_data: bool = False,
) -> None:
    """手工建一个 v1 schema 的 DB（含可选样例数据）——绕过新版 init() 的 v2 逻辑。

    顺序：先 session_store（建 sessions/messages/snapshots 表），再独立连接加 v1
    extension schema + 可选样例数据。两步都立刻 close 自己的连接。
    """
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    if include_session_data:
        from pi_agent_core_py.messages import (
            AssistantMessage,
            TextContent,
            UserMessage,
        )

        sid_obj = await session_store.create_session(title="seeded")
        sid = sid_obj.id
        await session_store.append_message(
            sid, UserMessage(content=[TextContent(text="hi")])
        )
        await session_store.append_message(
            sid,
            AssistantMessage(
                content=[TextContent(text="hello back")],
                api="test",
                provider="test",
                model="test",
            ),
        )
    await session_store.close()

    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(_V1_EXTENSION_SCHEMA_SQL)
    if include_extension_data:
        await db.execute(
            "INSERT INTO web_uploaded_skills "
            "(name, skill_json, raw_markdown, enabled, source_kind, "
            " content_sha256, last_restore_error, created_at, updated_at) "
            "VALUES ('seeded_skill', '{}', 'seed', 1, 'upload', '', NULL, 't1', 't1')"
        )
        await db.execute(
            "INSERT INTO web_mcp_servers "
            "(name, transport, command, args_json, desired_enabled, "
            " env_keys_json, last_restore_error, created_at, updated_at) "
            "VALUES ('seeded_srv', 'stdio', 'echo', '[]', 0, '[]', NULL, 't1', 't1')"
        )
        await db.execute(
            "INSERT INTO web_mcp_disabled_tools "
            "(server_name, tool_name, disabled_at) "
            "VALUES ('seeded_srv', 'dangerous_tool', 't1')"
        )
    await db.commit()
    await db.close()


async def _seed_v2_db(db_path: str) -> None:
    """Build the exact pre-HTTP v2 layout with representative MCP data."""
    await _seed_v1_db(db_path, include_extension_data=True)
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA foreign_keys=ON")
        for statement in ext_module._REVISIONS_DDL_STATEMENTS:
            await db.execute(statement)
        await db.execute(
            "UPDATE web_extension_schema_meta SET version = 2 WHERE id = 1"
        )
        await db.commit()
    finally:
        await db.close()


async def _read_meta_version(db_path: str) -> int | None:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            "SELECT version FROM web_extension_schema_meta WHERE id = 1"
        )
        row = await cur.fetchone()
        return row["version"] if row is not None else None
    finally:
        await db.close()


async def _table_exists(db_path: str, table: str) -> bool:
    db = await aiosqlite.connect(db_path)
    try:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return await cur.fetchone() is not None
    finally:
        await db.close()


async def _index_exists(db_path: str, index: str) -> bool:
    db = await aiosqlite.connect(db_path)
    try:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index,),
        )
        return await cur.fetchone() is not None
    finally:
        await db.close()


@pytest.fixture
async def shared_store(tmp_path):
    """session_store + ext_store 共享 connection——production 模式。

    返回 (session_store, ext_store, session_id)——session_id 是一个已创建的真实
    session（满足 FK），供所有需要 INSERT revisions 的测试直接复用。
    """
    db_path = str(tmp_path / "shared.sqlite")
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    sid_obj = await session_store.create_session(title="test_session")
    session_id = sid_obj.id
    ext_store = ExtensionSQLiteStore(db_path, connection=session_store.connection)
    await ext_store.init()
    try:
        yield session_store, ext_store, session_id
    finally:
        await ext_store.close()
        await session_store.close()


# ============================================================================
# 1-2. version 流程
# ============================================================================


async def test_fresh_db_initializes_to_current_version(tmp_path):
    """门槛 1：全新 DB 直接为当前版本。"""
    db_path = str(tmp_path / "fresh.sqlite")
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        assert await store.get_schema_version() == SCHEMA_VERSION
        assert SCHEMA_VERSION == 3
        for table in (
            "web_uploaded_skills",
            "web_mcp_servers",
            "web_mcp_disabled_tools",
            "web_message_revisions",
            "web_workspace_extension_selection",
            "web_workspace_mcp_selection",
            "web_workspace_skill_selection",
        ):
            assert await _table_exists(db_path, table), f"missing table {table}"
        for idx in (
            "uq_web_message_revision_request",
            "uq_web_message_revision_running",
            "uq_web_message_revision_active",
            "idx_web_message_revisions_history",
        ):
            assert await _index_exists(db_path, idx), f"missing index {idx}"
    finally:
        await store.close()


async def test_v1_db_upgrades_to_current_version(tmp_path):
    """门槛 2：v1 DB 连续升级到当前版本。"""
    db_path = str(tmp_path / "v1.sqlite")
    await _seed_v1_db(db_path)
    assert await _read_meta_version(db_path) == 1

    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        assert await store.get_schema_version() == SCHEMA_VERSION
        assert await _table_exists(db_path, "web_message_revisions")
    finally:
        await store.close()


async def test_v2_db_upgrades_to_v3_and_preserves_mcp_rows(tmp_path):
    db_path = str(tmp_path / "v2-to-v3.sqlite")
    await _seed_v2_db(db_path)

    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        assert await store.get_schema_version() == 3
        persisted = await store.get_mcp_server("seeded_srv")
        assert persisted is not None
        assert persisted.transport == "stdio"
        assert persisted.command == "echo"
        assert persisted.url is None
        assert persisted.header_env_json == "{}"
        disabled = await store.list_disabled_mcp_tools()
        assert [(item.server_name, item.tool_name) for item in disabled] == [
            ("seeded_srv", "dangerous_tool")
        ]
        assert await _table_exists(db_path, "web_workspace_extension_selection")
    finally:
        await store.close()


# ============================================================================
# 3-7. 数据保持
# ============================================================================


async def test_migration_preserves_c1_tables(tmp_path):
    """门槛 3：migration 后 C1 三张表结构保持（含数据可读写）。"""
    db_path = str(tmp_path / "c1.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        skill_rows = await store.list_uploaded_skill_rows()
        assert len(skill_rows) == 1
        assert skill_rows[0]["name"] == "seeded_skill"
        mcp_rows = await store.list_mcp_server_rows()
        assert len(mcp_rows) == 1
        assert mcp_rows[0]["name"] == "seeded_srv"
        disabled = await store.list_disabled_mcp_tools()
        assert len(disabled) == 1
        assert disabled[0].server_name == "seeded_srv"
        assert disabled[0].tool_name == "dangerous_tool"
    finally:
        await store.close()


async def test_migration_preserves_sessions_and_messages(tmp_path):
    """门槛 4：session/messages 数据保持。

    避免 WAL 多次开连接导致的 Windows hang——seed v1 后用 production 的
    shared-connection 模式跑 migration + 验证。
    """
    db_path = str(tmp_path / "sess.sqlite")
    await _seed_v1_db(db_path, include_session_data=True)
    assert await _read_meta_version(db_path) == 1

    # 用 shared-connection 模式跑 migration——一次开连接
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    store = ExtensionSQLiteStore(db_path, connection=session_store.connection)
    await store.init()
    try:
        # migration 已完成
        assert await store.get_schema_version() == SCHEMA_VERSION
        # 通过 shared connection 直接查 sessions 表
        db = store._require_db()
        cur = await db.execute("SELECT id FROM sessions")
        sessions = await cur.fetchall()
        assert len(sessions) == 1
        sid = sessions[0]["id"]
        cur = await db.execute(
            "SELECT role FROM messages WHERE session_id = ? ORDER BY idx",
            (sid,),
        )
        msgs = await cur.fetchall()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
    finally:
        await store.close()
        await session_store.close()


async def test_skill_data_preserved_after_migration(tmp_path):
    """门槛 5：Skill 数据保持。"""
    db_path = str(tmp_path / "skill.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        persisted = await store.get_uploaded_skill("seeded_skill")
        assert persisted is not None
        assert persisted.name == "seeded_skill"
        assert persisted.source_kind == "upload"
        assert persisted.enabled is True
    finally:
        await store.close()


async def test_mcp_server_data_preserved_after_migration(tmp_path):
    """门槛 6：MCP server 数据保持。"""
    db_path = str(tmp_path / "mcp.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        persisted = await store.get_mcp_server("seeded_srv")
        assert persisted is not None
        assert persisted.name == "seeded_srv"
        assert persisted.command == "echo"
        assert persisted.transport == "stdio"
    finally:
        await store.close()


async def test_disabled_tool_data_preserved_after_migration(tmp_path):
    """门槛 7：disabled tool 数据保持。"""
    db_path = str(tmp_path / "disabled.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        rows = await store.list_disabled_mcp_tools()
        assert len(rows) == 1
        assert rows[0].server_name == "seeded_srv"
        assert rows[0].tool_name == "dangerous_tool"
    finally:
        await store.close()


# ============================================================================
# 8-9. 幂等 / 重开
# ============================================================================


async def test_migration_idempotent_on_reinit(tmp_path):
    """门槛 8：migration 重复 initialize 幂等。"""
    db_path = str(tmp_path / "idem.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)

    store = ExtensionSQLiteStore(db_path)
    await store.init()
    try:
        await store.init()  # 同一 store 再 init——no-op
        assert await store.get_schema_version() == SCHEMA_VERSION
    finally:
        await store.close()

    store2 = ExtensionSQLiteStore(db_path)
    await store2.init()
    try:
        assert await store2.get_schema_version() == SCHEMA_VERSION
    finally:
        await store2.close()


async def test_current_version_reopen_does_not_repeat_ddl(tmp_path):
    """门槛 9：当前版本重开不重复 DDL——validate 路径不修改 schema。

    用 shared_store 模式创建一个真实的 session row + revision row，然后 close →
    重开 → 验证 revision row 仍在（schema 没被 DROP+CREATE）。
    """
    db_path = str(tmp_path / "v2.sqlite")
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    sid_obj = await session_store.create_session(title="seed")
    sid = sid_obj.id
    store = ExtensionSQLiteStore(db_path, connection=session_store.connection)
    await store.init()
    try:
        db = store._require_db()
        await db.execute(
            "INSERT INTO web_message_revisions "
            "(id, session_id, assistant_message_id, revision_number, "
            " status, base_content_sha256, content_json, created_at) "
            "VALUES ('rev_test', ?, 'msg_test', 0, "
            " 'superseded', 'abc', '{}', 't')",
            (sid,),
        )
        await db.commit()
    finally:
        await store.close()
        await session_store.close()

    # 重开——必须走 validate，不能 DROP + CREATE
    session_store2 = SQLiteSessionStore(db_path)
    await session_store2.init()
    store2 = ExtensionSQLiteStore(db_path, connection=session_store2.connection)
    await store2.init()
    try:
        db = store2._require_db()
        cur = await db.execute(
            "SELECT id FROM web_message_revisions WHERE id = 'rev_test'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["id"] == "rev_test"
    finally:
        await store2.close()
        await session_store2.close()


# ============================================================================
# 10. future version 拒绝
# ============================================================================


async def test_future_version_fails_before_any_ddl(tmp_path):
    """门槛 10：未来 version 在任何 schema 修改前失败。

    关键审核要求——避免把未来版本的 DB 当成旧版重建。
    """
    db_path = str(tmp_path / "future.sqlite")
    store = ExtensionSQLiteStore(db_path)
    await store.init()
    await store.close()

    # 把 version 篡改为 99
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "UPDATE web_extension_schema_meta SET version = 99 WHERE id = 1"
    )
    await db.commit()
    await db.close()

    store2 = ExtensionSQLiteStore(db_path)
    with pytest.raises(ExtensionStoreError):
        await store2.init()
    await store2.close()

    assert await _read_meta_version(db_path) == 99


# ============================================================================
# 11-12. migration 中途异常 → rollback
# ============================================================================


async def test_migration_exception_leaves_version_at_1(tmp_path, monkeypatch):
    """门槛 11：migration 中途异常后 version 仍为 1。

    镜像真实 `_migrate_v1_to_v2` 的 try/except/rollback 模式，注入 failure——
    验证 transaction rollback 把 version 留在 1。
    """
    db_path = str(tmp_path / "fail.sqlite")
    await _seed_v1_db(db_path, include_extension_data=True)

    async def failing_migrate(self):
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT version FROM web_extension_schema_meta WHERE id = 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or row["version"] != 1:
                raise ExtensionStoreError(
                    "schema version changed unexpectedly during migration"
                )
            for stmt in ext_module._REVISIONS_DDL_STATEMENTS:
                await db.execute(stmt)
            # 注入失败——在 UPDATE version 前
            raise RuntimeError("simulated mid-migration failure")
        except Exception:
            await db.rollback()
            raise

    monkeypatch.setattr(
        ExtensionSQLiteStore, "_migrate_v1_to_v2", failing_migrate
    )

    store = ExtensionSQLiteStore(db_path)
    with pytest.raises(RuntimeError, match="simulated mid-migration failure"):
        await store.init()
    await store.close()

    assert await _read_meta_version(db_path) == 1


async def test_migration_exception_leaves_no_revision_table(tmp_path, monkeypatch):
    """门槛 12：migration 中途异常后 revision table 不残留。"""
    db_path = str(tmp_path / "fail2.sqlite")
    await _seed_v1_db(db_path)

    async def failing_migrate(self):
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            for stmt in ext_module._REVISIONS_DDL_STATEMENTS:
                await db.execute(stmt)
            raise RuntimeError("simulated mid-migration failure")
        except Exception:
            await db.rollback()
            raise

    monkeypatch.setattr(
        ExtensionSQLiteStore, "_migrate_v1_to_v2", failing_migrate
    )

    store = ExtensionSQLiteStore(db_path)
    with pytest.raises(RuntimeError, match="simulated mid-migration failure"):
        await store.init()
    await store.close()

    assert not await _table_exists(db_path, "web_message_revisions")
    assert await _table_exists(db_path, "web_uploaded_skills")
    assert await _table_exists(db_path, "web_mcp_servers")


# ============================================================================
# 13-14. CHECK 约束
# ============================================================================


async def test_status_check_rejects_invalid_value(shared_store):
    """门槛 13：status CHECK 拒绝非法状态。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO web_message_revisions "
            "(id, session_id, assistant_message_id, revision_number, "
            " status, base_content_sha256, content_json, created_at) "
            "VALUES ('r1', ?, 'm1', 0, "
            " 'pending', 'abc', '{}', 't')",
            (sid,),
        )
        await db.commit()


async def test_revision_number_check_rejects_negative(shared_store):
    """门槛 14：revision_number CHECK 拒绝负数。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO web_message_revisions "
            "(id, session_id, assistant_message_id, revision_number, "
            " status, base_content_sha256, content_json, created_at) "
            "VALUES ('r1', ?, 'm1', -1, "
            " 'completed', 'abc', '{}', 't')",
            (sid,),
        )
        await db.commit()


# ============================================================================
# 15-18. partial unique 索引
# ============================================================================


async def _insert_revision(
    db, rid, status, session_id, request_id=None, assistant_id="m1", revision_number=0,
) -> None:
    await db.execute(
        "INSERT INTO web_message_revisions "
        "(id, session_id, assistant_message_id, revision_number, "
        " request_id, status, base_content_sha256, content_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'abc', '{}', 't')",
        (rid, session_id, assistant_id, revision_number, request_id, status),
    )
    await db.commit()


async def test_request_id_partial_unique(shared_store):
    """门槛 15：request_id partial unique——同一 request_id 不能关联多条 revision。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    await _insert_revision(
        db, "r1", "completed", session_id=sid, request_id="req_a", revision_number=0
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert_revision(
            db, "r2", "superseded", session_id=sid, request_id="req_a", revision_number=1
        )


async def test_running_partial_unique(shared_store):
    """门槛 16：running partial unique——同一 assistant 至多 1 个 running。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    await _insert_revision(db, "r1", "running", session_id=sid, revision_number=0)
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert_revision(db, "r2", "running", session_id=sid, revision_number=1)


async def test_completed_partial_unique(shared_store):
    """门槛 17：completed partial unique——同一 assistant 至多 1 个 completed。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    await _insert_revision(db, "r1", "completed", session_id=sid, revision_number=0)
    with pytest.raises(aiosqlite.IntegrityError):
        await _insert_revision(db, "r2", "completed", session_id=sid, revision_number=1)


async def test_multiple_superseded_allowed(shared_store):
    """门槛 18：同一 assistant 可以有多个 superseded。"""
    _, ext, sid = shared_store
    db = ext._require_db()
    await _insert_revision(db, "r1", "superseded", session_id=sid, revision_number=0)
    await _insert_revision(db, "r2", "superseded", session_id=sid, revision_number=1)
    await _insert_revision(db, "r3", "superseded", session_id=sid, revision_number=2)
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM web_message_revisions "
        "WHERE status = 'superseded'"
    )
    row = await cur.fetchone()
    assert row["c"] == 3


# ============================================================================
# 19. CASCADE
# ============================================================================


async def test_session_delete_cascades_revisions(shared_store):
    """门槛 19：删除 session 级联删除 revisions（FK to sessions ON DELETE CASCADE）。"""
    session_store, ext, _ = shared_store
    new_sid_obj = await session_store.create_session(title="to_delete")
    new_sid = new_sid_obj.id

    db = ext._require_db()
    await db.execute(
        "INSERT INTO web_message_revisions "
        "(id, session_id, assistant_message_id, revision_number, "
        " status, base_content_sha256, content_json, created_at) "
        "VALUES ('r1', ?, 'm1', 0, 'completed', 'abc', '{}', 't')",
        (new_sid,),
    )
    await db.commit()

    cur = await db.execute("SELECT COUNT(*) AS c FROM web_message_revisions")
    assert (await cur.fetchone())["c"] == 1

    # 删除 session——必须级联删 revision
    await db.execute("DELETE FROM sessions WHERE id = ?", (new_sid,))
    await db.commit()

    cur = await db.execute("SELECT COUNT(*) AS c FROM web_message_revisions")
    assert (await cur.fetchone())["c"] == 0


# ============================================================================
# 20. 空 revisions 表不影响 messages API
# ============================================================================


async def test_empty_revisions_table_does_not_affect_messages(shared_store):
    """门槛 20：revision 表为空时原 messages API 不受影响。"""
    session_store, ext, sid = shared_store
    from pi_agent_core_py.messages import (
        AssistantMessage,
        TextContent,
        UserMessage,
    )

    await session_store.append_message(
        sid, UserMessage(content=[TextContent(text="q")])
    )
    await session_store.append_message(
        sid,
        AssistantMessage(
            content=[TextContent(text="a")],
            api="test",
            provider="test",
            model="test",
        ),
    )
    msgs = await session_store.list_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"

    # revision 表为空但表本身存在
    db = ext._require_db()
    cur = await db.execute("SELECT COUNT(*) AS c FROM web_message_revisions")
    assert (await cur.fetchone())["c"] == 0

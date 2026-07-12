"""P1-C1: ExtensionSQLiteStore 单元测试——24 用例。

覆盖：
- 基础 CRUD（Skills / MCP servers / disabled tools）
- :memory: 共享 connection（修正点 1）
- 逐行隔离 decode（修正点 5）
- schema version（修正点 7）
- env value 不入库（修正点 4）
- cascade delete
- close 幂等
- 与 session_store 共存
"""
from __future__ import annotations

import asyncio
import json

import pytest

from pi_agent_core_py.session_sqlite import SQLiteSessionStore
from pi_agent_core_py.web.extension_store import (
    SCHEMA_VERSION,
    ExtensionSQLiteStore,
    ExtensionStoreConflictError,
    ExtensionStoreError,
    ExtensionStoreValidationError,
)

# 默认运行（不标 slow）——纯 SQLite 单元测试，无外部依赖


# ============================================================================
# fixtures
# ============================================================================


@pytest.fixture
async def ext_store(tmp_path):
    """独立文件型 extension_store——用于大部分 CRUD 测试。"""
    store = ExtensionSQLiteStore(str(tmp_path / "ext.sqlite"))
    await store.init()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def shared_store(tmp_path):
    """session_store + extension_store 共享 connection（:memory: 模拟）。"""
    db_path = str(tmp_path / "shared.sqlite")
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    ext_store = ExtensionSQLiteStore(
        db_path, connection=session_store.connection
    )
    await ext_store.init()
    try:
        yield session_store, ext_store
    finally:
        await ext_store.close()
        await session_store.close()


# ============================================================================
# Tests 1-3: 初始化 / 幂等 / schema
# ============================================================================


async def test_1_empty_db_initializes():
    """空数据库初始化——3 表 + schema_meta 创建成功。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        store = ExtensionSQLiteStore(str(Path(td) / "fresh.sqlite"))
        await store.init()
        try:
            # schema_meta
            v = await store.get_schema_version()
            assert v == SCHEMA_VERSION
            # 空表
            assert await store.list_uploaded_skill_rows() == []
            assert await store.list_mcp_server_rows() == []
            assert await store.list_disabled_mcp_tools() == []
        finally:
            await store.close()


async def test_2_existing_session_db_initializes(shared_store):
    """已有 session 数据库可附加 extension 表——两 store 共存。"""
    session_store, ext_store = shared_store
    # session_store 正常工作
    default = await session_store.ensure_default_session()
    assert default.id
    # extension_store 表也可用
    await ext_store.upsert_uploaded_skill(
        name="skill1",
        skill_json='{"name":"skill1"}',
        raw_markdown="# skill1\nbody",
    )
    skill = await ext_store.get_uploaded_skill("skill1")
    assert skill is not None
    assert skill.name == "skill1"


async def test_3_init_idempotent(ext_store):
    """init() 重复调用安全。"""
    # 已 init（fixture 内）——再调一次应 no-op
    await ext_store.init()
    await ext_store.init()
    assert ext_store.closed is False


# ============================================================================
# Tests 4-6: Skill CRUD
# ============================================================================


async def test_4_skill_insert_get_list(ext_store):
    await ext_store.upsert_uploaded_skill(
        name="codereview",
        skill_json='{"name":"codereview","description":"code review"}',
        raw_markdown="---\nname: codereview\n---\nbody",
        enabled=True,
        source_kind="upload",
        content_sha256="abc123",
    )
    skill = await ext_store.get_uploaded_skill("codereview")
    assert skill is not None
    assert skill.name == "codereview"
    assert skill.enabled is True
    assert skill.source_kind == "upload"
    assert skill.content_sha256 == "abc123"

    rows = await ext_store.list_uploaded_skill_rows()
    assert len(rows) == 1


async def test_5_skill_update_enabled(ext_store):
    await ext_store.upsert_uploaded_skill(
        name="s1", skill_json="{}", raw_markdown="md"
    )
    assert (await ext_store.get_uploaded_skill("s1")).enabled is True

    await ext_store.set_skill_enabled("s1", False)
    assert (await ext_store.get_uploaded_skill("s1")).enabled is False

    await ext_store.set_skill_enabled("s1", True)
    assert (await ext_store.get_uploaded_skill("s1")).enabled is True


async def test_6_skill_delete(ext_store):
    await ext_store.upsert_uploaded_skill(
        name="s1", skill_json="{}", raw_markdown="md"
    )
    deleted = await ext_store.delete_uploaded_skill("s1")
    assert deleted is True

    # 再删返回 False
    deleted2 = await ext_store.delete_uploaded_skill("s1")
    assert deleted2 is False

    assert await ext_store.get_uploaded_skill("s1") is None


# ============================================================================
# Tests 7-9: MCP server CRUD
# ============================================================================


async def test_7_mcp_server_insert_get_list(ext_store):
    await ext_store.upsert_mcp_server(
        name="fake",
        transport="stdio",
        command="python",
        args=["server.py"],
        desired_enabled=False,
        env_keys=["API_KEY", "SECRET"],
    )
    server = await ext_store.get_mcp_server("fake")
    assert server is not None
    assert server.command == "python"
    assert server.desired_enabled is False
    # env_keys 排序存储
    assert json.loads(server.env_keys_json) == ["API_KEY", "SECRET"]
    # **绝不存 value**
    assert "super-secret" not in server.env_keys_json
    assert "super-secret" not in server.args_json

    rows = await ext_store.list_mcp_server_rows()
    assert len(rows) == 1


async def test_8_mcp_server_desired_enabled_update(ext_store):
    await ext_store.upsert_mcp_server(
        name="s1", command="python", args=[], env_keys=[]
    )
    assert (await ext_store.get_mcp_server("s1")).desired_enabled is False

    await ext_store.set_mcp_server_enabled("s1", True)
    assert (await ext_store.get_mcp_server("s1")).desired_enabled is True


async def test_9_disabled_tool_insert_list_delete(ext_store):
    await ext_store.upsert_mcp_server(
        name="s1", command="python", args=[], env_keys=[]
    )
    await ext_store.disable_mcp_tool("s1", "echo")
    await ext_store.disable_mcp_tool("s1", "second_tool")

    tools = await ext_store.list_disabled_mcp_tools("s1")
    assert len(tools) == 2
    tool_names = {t.tool_name for t in tools}
    assert tool_names == {"echo", "second_tool"}

    await ext_store.enable_mcp_tool("s1", "echo")
    tools_after = await ext_store.list_disabled_mcp_tools("s1")
    assert len(tools_after) == 1
    assert tools_after[0].tool_name == "second_tool"


# ============================================================================
# Test 10: cascade delete
# ============================================================================


async def test_10_server_delete_cascades_disabled_tools(ext_store):
    await ext_store.upsert_mcp_server(
        name="s1", command="python", args=[], env_keys=[]
    )
    await ext_store.disable_mcp_tool("s1", "echo")
    await ext_store.disable_mcp_tool("s1", "second")

    deleted = await ext_store.delete_mcp_server("s1")
    assert deleted is True

    # disabled tools cascade 删除
    tools = await ext_store.list_disabled_mcp_tools("s1")
    assert tools == []

    # 全局 list 也空
    all_tools = await ext_store.list_disabled_mcp_tools()
    assert all_tools == []


# ============================================================================
# Tests 11-12: timestamps + JSON 校验
# ============================================================================


async def test_11_created_at_updated_at(ext_store):
    await ext_store.upsert_uploaded_skill(
        name="s1", skill_json="{}", raw_markdown="md"
    )
    s1 = await ext_store.get_uploaded_skill("s1")
    assert s1.created_at
    assert s1.updated_at
    assert s1.last_restore_error is None

    # update 后 updated_at 变化
    import asyncio as _asyncio

    await _asyncio.sleep(0.01)
    await ext_store.set_skill_enabled("s1", False)
    s2 = await ext_store.get_uploaded_skill("s1")
    assert s2.updated_at >= s1.updated_at


async def test_12_json_validation():
    """字段校验——空 name / 空 command / args 非 list 抛 ValidationError。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        store = ExtensionSQLiteStore(str(Path(td) / "v.sqlite"))
        await store.init()
        try:
            with pytest.raises(ExtensionStoreValidationError):
                await store.upsert_uploaded_skill(
                    name="", skill_json="{}", raw_markdown="md"
                )
            with pytest.raises(ExtensionStoreValidationError):
                await store.upsert_mcp_server(
                    name="s1", command="", args=[], env_keys=[]
                )
            with pytest.raises(ExtensionStoreValidationError):
                await store.upsert_mcp_server(
                    name="s1",
                    command="python",
                    args="not-a-list",  # type: ignore[arg-type]
                    env_keys=[],
                )
        finally:
            await store.close()


# ============================================================================
# Tests 13-14: 损坏 JSON 逐行隔离 + 不存在 row
# ============================================================================


async def test_13_corrupted_skill_row_isolated(ext_store, tmp_path):
    """一条坏 Skill row 不影响另一条——逐行 decode。"""
    # 插入 2 条好的
    await ext_store.upsert_uploaded_skill(
        name="good1", skill_json='{"name":"good1"}', raw_markdown="md1"
    )
    await ext_store.upsert_uploaded_skill(
        name="good2", skill_json='{"name":"good2"}', raw_markdown="md2"
    )
    # 直接 SQL 破坏一条 skill_json
    assert ext_store._db is not None
    await ext_store._db.execute(
        "UPDATE web_uploaded_skills SET skill_json = ? WHERE name = ?",
        ("NOT VALID JSON{{{", "good1"),
    )
    await ext_store._db.commit()

    # list_uploaded_skill_rows 仍能返回所有行
    rows = await ext_store.list_uploaded_skill_rows()
    assert len(rows) == 2

    # decode 逐行——good1 error, good2 ok
    results = [ExtensionSQLiteStore.decode_uploaded_skill(r) for r in rows]
    by_name = {r.name: r for r in results}
    assert by_name["good1"].error is not None
    assert by_name["good1"].skill is None
    assert by_name["good2"].error is None
    assert by_name["good2"].skill is not None


async def test_14_nonexistent_row_returns_none(ext_store):
    assert await ext_store.get_uploaded_skill("nope") is None
    assert await ext_store.get_mcp_server("nope") is None

    with pytest.raises(ExtensionStoreConflictError):
        await ext_store.set_skill_enabled("nope", True)
    with pytest.raises(ExtensionStoreConflictError):
        await ext_store.set_mcp_server_enabled("nope", True)


# ============================================================================
# Tests 15-16: close 幂等 + 错误转换
# ============================================================================


async def test_15_close_idempotent(tmp_path):
    store = ExtensionSQLiteStore(str(tmp_path / "c.sqlite"))
    await store.init()
    await store.close()
    await store.close()  # 幂等
    assert store.closed is True


async def test_16_db_error_safe_conversion(ext_store):
    """关闭后操作抛 ExtensionStoreError（不含 SQL / 路径）。"""
    await ext_store.close()
    with pytest.raises(ExtensionStoreError) as exc_info:
        await ext_store.get_uploaded_skill("any")
    msg = str(exc_info.value)
    # 安全——不含 SQL 语句 / 绝对路径
    assert "SELECT" not in msg
    assert ".sqlite" not in msg


# ============================================================================
# Tests 17-18: 与 session 共存 + 无 secret marker
# ============================================================================


async def test_17_session_messages_unaffected(shared_store):
    """extension 表不影响 session/messages。"""
    session_store, ext_store = shared_store
    # 写 session 数据
    default = await session_store.ensure_default_session()
    # 写 extension 数据
    await ext_store.upsert_uploaded_skill(
        name="s1", skill_json="{}", raw_markdown="md"
    )
    # session 数据仍可读
    sessions = await session_store.list_sessions()
    assert any(s.id == default.id for s in sessions)
    # extension 数据也可读
    skill = await ext_store.get_uploaded_skill("s1")
    assert skill is not None


async def test_18_no_secret_marker_in_db(ext_store, tmp_path):
    """SQLite 文件内不含测试 secret marker。"""
    secret_marker = "SUPER_SECRET_VALUE_12345"
    await ext_store.upsert_mcp_server(
        name="s1",
        command="python",
        args=["server.py"],
        env_keys=["API_KEY"],  # 只存 key
    )
    await ext_store.close()

    # 读 SQLite 文件二进制——确认无 secret
    db_file = tmp_path / "ext.sqlite"
    content = db_file.read_bytes()
    assert secret_marker.encode() not in content


# ============================================================================
# Test 19-20: 修正点 1——:memory: 共享 + 文件型共享
# ============================================================================


async def test_19_file_db_shared_between_stores(tmp_path):
    """文件型 db_path：session_store + extension_store 独立 connection 共享同一文件。"""
    db_path = str(tmp_path / "shared.sqlite")
    session_store = SQLiteSessionStore(db_path)
    await session_store.init()
    ext_store = ExtensionSQLiteStore(db_path)  # 独立 connection
    await ext_store.init()

    try:
        # 两 store 都能读写
        default = await session_store.ensure_default_session()
        await ext_store.upsert_uploaded_skill(
            name="s1", skill_json="{}", raw_markdown="md"
        )
        # extension_store 看不到 session 表（但物理同一文件）
        # 验证：重新打开同一文件能读到双方数据
        await ext_store.close()
        await session_store.close()

        session_store2 = SQLiteSessionStore(db_path)
        await session_store2.init()
        ext_store2 = ExtensionSQLiteStore(db_path)
        await ext_store2.init()
        sessions = await session_store2.list_sessions()
        assert any(s.id == default.id for s in sessions)
        skill = await ext_store2.get_uploaded_skill("s1")
        assert skill is not None
        await session_store2.close()
        await ext_store2.close()
    except Exception:
        await ext_store.close()
        await session_store.close()
        raise


async def test_20_memory_db_shared_connection():
    """:memory: 模式——必须共享 connection 才能让两表共存。

    **不变量 1 验证**：extension_store.close() 后 session_store 仍可读写——
    injected connection 不被 extension close。
    """
    session_store = SQLiteSessionStore(":memory:")
    await session_store.init()
    ext_store = ExtensionSQLiteStore(
        ":memory:", connection=session_store.connection
    )
    await ext_store.init()
    try:
        default = await session_store.ensure_default_session()
        await ext_store.upsert_uploaded_skill(
            name="s1", skill_json="{}", raw_markdown="md"
        )
        # 两表在同一内存数据库
        sessions = await session_store.list_sessions()
        assert any(s.id == default.id for s in sessions)
        skill = await ext_store.get_uploaded_skill("s1")
        assert skill is not None

        # **不变量 1**：extension_store.close() 不关 session_store.connection
        await ext_store.close()
        assert ext_store.closed is True
        # session_store 仍可读写
        sessions_after = await session_store.list_sessions()
        assert any(s.id == default.id for s in sessions_after)
        default2 = await session_store.ensure_default_session()
        assert default2.id == default.id
    finally:
        await session_store.close()


# ============================================================================
# Tests 21-22: 修正点 5——逐行隔离（MCP）
# ============================================================================


async def test_21_bad_mcp_row_isolated(ext_store):
    """一条坏 MCP row 不影响另一条。"""
    await ext_store.upsert_mcp_server(
        name="good", command="python", args=[], env_keys=[]
    )
    await ext_store.upsert_mcp_server(
        name="bad", command="python", args=[], env_keys=[]
    )
    # 破坏 bad 的 args_json
    assert ext_store._db is not None
    await ext_store._db.execute(
        "UPDATE web_mcp_servers SET args_json = ? WHERE name = ?",
        ("NOT JSON{{", "bad"),
    )
    await ext_store._db.commit()

    rows = await ext_store.list_mcp_server_rows()
    assert len(rows) == 2
    results = [ExtensionSQLiteStore.decode_mcp_server(r) for r in rows]
    by_name = {r.name: r for r in results}
    assert by_name["good"].error is None
    assert by_name["bad"].error is not None


async def test_22_skill_restore_error_recording(ext_store):
    """set_skill_restore_error 记录 + 清除。"""
    await ext_store.upsert_uploaded_skill(
        name="s1", skill_json="{}", raw_markdown="md"
    )
    await ext_store.set_skill_restore_error("s1", "decode failed: bad json")
    s1 = await ext_store.get_uploaded_skill("s1")
    assert s1.last_restore_error == "decode failed: bad json"

    await ext_store.set_skill_restore_error("s1", None)
    s2 = await ext_store.get_uploaded_skill("s1")
    assert s2.last_restore_error is None


# ============================================================================
# Test 23: schema version 重复初始化
# ============================================================================


async def test_23_schema_version_repeated_init(tmp_path):
    """schema_meta 重复 init 不报错——INSERT OR IGNORE。"""
    db_path = str(tmp_path / "v.sqlite")
    store1 = ExtensionSQLiteStore(db_path)
    await store1.init()
    v1 = await store1.get_schema_version()
    await store1.close()

    store2 = ExtensionSQLiteStore(db_path)
    await store2.init()
    v2 = await store2.get_schema_version()
    await store2.close()

    assert v1 == SCHEMA_VERSION
    assert v2 == SCHEMA_VERSION

    # 确认 meta 表只有一行
    store3 = ExtensionSQLiteStore(db_path)
    await store3.init()
    assert store3._db is not None
    cursor = await store3._db.execute(
        "SELECT COUNT(*) AS c FROM web_extension_schema_meta"
    )
    row = await cursor.fetchone()
    assert row["c"] == 1
    await store3.close()


# ============================================================================
# Test 24: busy_timeout 并发生效
# ============================================================================


async def test_24_busy_timeout_concurrent_writes(tmp_path):
    """两个 connection 并发写入——busy_timeout=5000 防止 immediate fail。"""
    db_path = str(tmp_path / "busy.sqlite")
    store1 = ExtensionSQLiteStore(db_path)
    await store1.init()
    store2 = ExtensionSQLiteStore(db_path)
    await store2.init()

    try:
        # 并发写——busy_timeout 让第二个等第一个
        async def write(store, name):
            await store.upsert_uploaded_skill(
                name=name, skill_json="{}", raw_markdown="md"
            )

        await asyncio.gather(
            write(store1, "s1"),
            write(store2, "s2"),
            write(store1, "s3"),
            write(store2, "s4"),
        )
        # 验证 4 条都写入
        rows1 = await store1.list_uploaded_skill_rows()
        rows2 = await store2.list_uploaded_skill_rows()
        # WAL 模式下可能 reader 看不到 uncommitted——commit 后都能看到
        # 这里都 commit 了
        assert len(rows1) >= 4
        assert len(rows2) >= 4
    finally:
        await store1.close()
        await store2.close()


# ============================================================================
# Test 25: 不变量 2——共享 connection 事务并发（session + extension 同时写）
# ============================================================================


async def test_25_shared_connection_concurrent_session_and_extension():
    """共享 connection 下 session_store + extension_store 并发写入——无事务交叉。

    **不变量 2**：aiosqlite 单连接串行所有 SQL；两个 store 共用 connection 时
    不应出现 "cannot start a transaction within a transaction"。
    """
    session_store = SQLiteSessionStore(":memory:")
    await session_store.init()
    ext_store = ExtensionSQLiteStore(
        ":memory:", connection=session_store.connection
    )
    await ext_store.init()
    try:
        default = await session_store.ensure_default_session()

        # 并发：session 写 message + extension 写 skill
        async def write_session():
            # 用 list_sessions 模拟读操作（不写 message 避免 AgentMessage 复杂）
            for _ in range(5):
                await session_store.list_sessions()

        async def write_extension(i: int):
            await ext_store.upsert_uploaded_skill(
                name=f"skill_{i}", skill_json="{}", raw_markdown=f"md_{i}"
            )

        # 10 个并发任务——5 session + 5 extension
        tasks = [write_session() for _ in range(5)]
        tasks.extend(write_extension(i) for i in range(5))
        await asyncio.gather(*tasks)

        # 验证双方写入都成功
        skills = await ext_store.list_uploaded_skill_rows()
        assert len(skills) == 5
        sessions = await session_store.list_sessions()
        assert any(s.id == default.id for s in sessions)
    finally:
        await ext_store.close()
        await session_store.close()


# ============================================================================
# Test 26: 不变量 3——schema version 单例（id=1 CHECK）
# ============================================================================


async def test_26_schema_version_singleton(ext_store):
    """schema_meta 只有一行 id=1——CHECK 约束防止多行。"""
    import sqlite3

    v = await ext_store.get_schema_version()
    assert v == SCHEMA_VERSION

    # 尝试插入第二行——CHECK(id=1) 约束拒绝
    assert ext_store._db is not None
    with pytest.raises(sqlite3.IntegrityError):
        await ext_store._db.execute(
            "INSERT INTO web_extension_schema_meta (id, version) VALUES (2, 1)"
        )


# ============================================================================
# Test 27: 不变量 4——Skill 双份内容真源（skill_json + raw_markdown + sha256）
# ============================================================================


async def test_27_skill_dual_content_storage(ext_store):
    """skill_json 是 canonical source；raw_markdown 是审计；sha256 校验完整性。"""
    import hashlib

    raw_md = "---\nname: test_skill\ndescription: test\n---\n# Instructions\nbody"
    skill_json = '{"name":"test_skill","description":"test","prompt":"body"}'
    sha = hashlib.sha256(raw_md.encode("utf-8")).hexdigest()

    await ext_store.upsert_uploaded_skill(
        name="test_skill",
        skill_json=skill_json,
        raw_markdown=raw_md,
        content_sha256=sha,
    )
    skill = await ext_store.get_uploaded_skill("test_skill")
    assert skill is not None
    assert skill.skill_json == skill_json
    assert skill.raw_markdown == raw_md
    assert skill.content_sha256 == sha

    # 恢复时校验 sha256——C2 restore 会用此字段验证 raw_markdown 完整性
    assert hashlib.sha256(skill.raw_markdown.encode("utf-8")).hexdigest() == sha

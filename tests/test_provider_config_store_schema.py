"""Schema initialization & validation tests for SQLiteProviderConfigStore (E2-1).

覆盖 15 项 schema 检查（spec §16.1）：
1. fresh DB 初始化
2. initialize 幂等
3. 独立 schema meta v1
4. 不影响 Credential schema
5. 不影响 Extension schema
6. 未知高版本拒绝
7. 缺表拒绝
8. 缺列拒绝
9. 缺索引拒绝
10. partial unique index 缺 WHERE 拒绝
11. CHECK enum 损坏拒绝
12. Binding FK 缺失拒绝
13. ON DELETE RESTRICT 缺失拒绝
14. foreign_keys=OFF → 自动开启并验证返回 1
15. 非法 schema 不静默修复
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from pi_agent_core_py.web.provider_config_store import (
    WEB_PROVIDER_CONFIG_SCHEMA_VERSION,
    ProviderConfigSchemaValidationError,
    ProviderConfigSchemaVersionError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Helpers
# ============================================================================


async def _corrupt(db_path: Path, corrupt_fn) -> None:
    """Open DB raw (no validation) and apply corruption_fn."""
    conn = await aiosqlite.connect(str(db_path), isolation_level=None)
    conn.row_factory = aiosqlite.Row
    try:
        await corrupt_fn(conn)
        await conn.commit()
    finally:
        await conn.close()


# ============================================================================
# 1-3. Fresh init / idempotent / independent meta
# ============================================================================


async def test_1_fresh_db_initializes_v1_schema(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        version = await store.get_schema_version()
        assert version == WEB_PROVIDER_CONFIG_SCHEMA_VERSION

        # Tables present
        async with store._require_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('web_provider_profiles', 'web_session_model_bindings', "
            "'web_provider_config_schema_meta')"
        ) as cursor:
            rows = await cursor.fetchall()
        assert {r["name"] for r in rows} == {
            "web_provider_profiles",
            "web_session_model_bindings",
            "web_provider_config_schema_meta",
        }
    finally:
        await store.close()


async def test_2_initialize_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "idempotent.db"
    store1 = await SQLiteProviderConfigStore.open(db)
    try:
        await store1.create_profile(
            profile_id="profile-idem-1",
            name="Idem",
            provider_id="anthropic",
            credential_id="cred-idem",
            default_model="claude-X",
        )
    finally:
        await store1.close()

    # Re-open—no error, data preserved
    store2 = await SQLiteProviderConfigStore.open(db)
    try:
        version = await store2.get_schema_version()
        assert version == WEB_PROVIDER_CONFIG_SCHEMA_VERSION
        profile = await store2.get_profile("profile-idem-1")
        assert profile.name == "Idem"
    finally:
        await store2.close()


async def test_3_independent_schema_meta_table_name(tmp_path: Path) -> None:
    """Provider Config schema meta uses its own table name—no collision with E1."""
    db = tmp_path / "meta.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        async with store._require_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%schema_meta%'"
        ) as cursor:
            rows = await cursor.fetchall()
        names = {r["name"] for r in rows}
        assert "web_provider_config_schema_meta" in names
        # Must NOT alias to credentials' meta table
        assert "web_credentials_schema_meta" not in names
    finally:
        await store.close()


# ============================================================================
# 4-5. Doesn't affect other stores' schema
# ============================================================================


async def test_4_does_not_create_credentials_schema_meta(tmp_path: Path) -> None:
    """Opening provider_config store must not create web_credentials_schema_meta."""
    db = tmp_path / "isolated.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        pass
    finally:
        await store.close()

    conn = await aiosqlite.connect(str(db))
    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='web_credentials_schema_meta'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is None, (
            "provider_config store must NOT create web_credentials_schema_meta"
        )

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='web_credentials'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is None, "provider_config store must NOT create web_credentials"
    finally:
        await conn.close()


async def test_5_does_not_touch_extension_schema(tmp_path: Path) -> None:
    """Opening provider_config store must not affect extension_store schema."""
    db = tmp_path / "ext.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        pass
    finally:
        await store.close()

    conn = await aiosqlite.connect(str(db))
    conn.row_factory = aiosqlite.Row
    try:
        # Provider Config creates exactly 3 tables—nothing else.
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            rows = await cursor.fetchall()
        names = {r["name"] for r in rows}
        assert names == {
            "web_provider_config_schema_meta",
            "web_provider_profiles",
            "web_session_model_bindings",
        }
    finally:
        await conn.close()


# ============================================================================
# 6. Unknown high version rejected
# ============================================================================


async def _bump_version(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "UPDATE web_provider_config_schema_meta SET value = '99' WHERE key = 'version'"
    )


async def test_6_unknown_high_version_rejected(tmp_path: Path) -> None:
    db = tmp_path / "highver.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _bump_version)

    with pytest.raises(ProviderConfigSchemaVersionError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 7. Missing table rejected
# ============================================================================


async def _drop_profiles_table(conn: aiosqlite.Connection) -> None:
    await conn.execute("DROP TABLE web_provider_profiles")


async def test_7a_missing_profiles_table_rejected(tmp_path: Path) -> None:
    db = tmp_path / "no_profiles.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    # Must also drop bindings first because of FK
    async def _corrupt_fn(c: aiosqlite.Connection) -> None:
        await c.execute("PRAGMA foreign_keys=OFF")
        await c.execute("DROP TABLE web_session_model_bindings")
        await c.execute("DROP TABLE web_provider_profiles")

    await _corrupt(db, _corrupt_fn)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


async def test_7b_missing_bindings_table_rejected(tmp_path: Path) -> None:
    db = tmp_path / "no_bindings.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    async def _corrupt_fn(c: aiosqlite.Connection) -> None:
        await c.execute("PRAGMA foreign_keys=OFF")
        await c.execute("DROP TABLE web_session_model_bindings")

    await _corrupt(db, _corrupt_fn)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 8. Missing column rejected
# ============================================================================


async def _drop_credential_id_column(conn: aiosqlite.Connection) -> None:
    # SQLite refuses DROP COLUMN when an index references the column—
    # drop the dependent index first, then the column.
    await conn.execute("DROP INDEX IF EXISTS ix_provider_profiles_credential")
    await conn.execute(
        "ALTER TABLE web_provider_profiles DROP COLUMN credential_id"
    )


async def test_8_missing_column_rejected(tmp_path: Path) -> None:
    db = tmp_path / "nocol.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _drop_credential_id_column)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 9. Missing index rejected
# ============================================================================


async def _drop_updated_at_index(conn: aiosqlite.Connection) -> None:
    await conn.execute("DROP INDEX ix_provider_profiles_updated_at")


async def test_9_missing_index_rejected(tmp_path: Path) -> None:
    db = tmp_path / "noidx.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _drop_updated_at_index)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 10. Partial unique index missing WHERE rejected
# ============================================================================


async def _strip_where_from_default_index(conn: aiosqlite.Connection) -> None:
    await conn.execute("DROP INDEX ux_provider_profiles_default")
    await conn.execute(
        "CREATE UNIQUE INDEX ux_provider_profiles_default "
        "ON web_provider_profiles(is_default)"
    )


async def test_10_partial_unique_index_missing_where_rejected(
    tmp_path: Path,
) -> None:
    db = tmp_path / "nowhere.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _strip_where_from_default_index)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


async def test_10b_partial_unique_index_missing_unique_rejected(
    tmp_path: Path,
) -> None:
    db = tmp_path / "nounique.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    async def _strip_unique(c: aiosqlite.Connection) -> None:
        await c.execute("DROP INDEX ux_provider_profiles_default")
        await c.execute(
            "CREATE INDEX ux_provider_profiles_default "
            "ON web_provider_profiles(is_default) WHERE is_default = 1"
        )

    await _corrupt(db, _strip_unique)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 11. CHECK enum corruption rejected
# ============================================================================


async def _corrupt_source_enum(conn: aiosqlite.Connection) -> None:
    """Recreate bindings table with source IN ('default','explicit','other')."""
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("DROP TABLE web_session_model_bindings")
    await conn.execute(
        """
        CREATE TABLE web_session_model_bindings (
            session_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            model_id TEXT NOT NULL
                CHECK(length(trim(model_id)) BETWEEN 1 AND 256),
            source TEXT NOT NULL
                CHECK(source IN ('default', 'explicit', 'other')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(profile_id)
                REFERENCES web_provider_profiles(id)
                ON DELETE RESTRICT
        )
        """
    )
    await conn.execute(
        "CREATE INDEX ix_session_bindings_profile "
        "ON web_session_model_bindings(profile_id)"
    )


async def test_11_check_enum_corruption_rejected(tmp_path: Path) -> None:
    db = tmp_path / "badenum.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _corrupt_source_enum)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 12. Binding FK missing rejected
# ============================================================================


async def _drop_fk_from_bindings(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("DROP TABLE web_session_model_bindings")
    await conn.execute(
        """
        CREATE TABLE web_session_model_bindings (
            session_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            model_id TEXT NOT NULL
                CHECK(length(trim(model_id)) BETWEEN 1 AND 256),
            source TEXT NOT NULL
                CHECK(source IN ('default', 'explicit')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    await conn.execute(
        "CREATE INDEX ix_session_bindings_profile "
        "ON web_session_model_bindings(profile_id)"
    )


async def test_12_binding_fk_missing_rejected(tmp_path: Path) -> None:
    db = tmp_path / "nofk.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _drop_fk_from_bindings)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 13. ON DELETE RESTRICT missing rejected
# ============================================================================


async def _change_on_delete_to_cascade(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("DROP TABLE web_session_model_bindings")
    await conn.execute(
        """
        CREATE TABLE web_session_model_bindings (
            session_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            model_id TEXT NOT NULL
                CHECK(length(trim(model_id)) BETWEEN 1 AND 256),
            source TEXT NOT NULL
                CHECK(source IN ('default', 'explicit')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(profile_id)
                REFERENCES web_provider_profiles(id)
                ON DELETE CASCADE
        )
        """
    )
    await conn.execute(
        "CREATE INDEX ix_session_bindings_profile "
        "ON web_session_model_bindings(profile_id)"
    )


async def test_13_on_delete_restrict_missing_rejected(tmp_path: Path) -> None:
    db = tmp_path / "cascade.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    await _corrupt(db, _change_on_delete_to_cascade)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)


# ============================================================================
# 14. foreign_keys auto-enabled and verified
# ============================================================================


async def test_14_foreign_keys_pragma_returns_1_after_open(tmp_path: Path) -> None:
    """open() must set PRAGMA foreign_keys=ON and the resulting value is 1."""
    db = tmp_path / "fk.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        async with store._require_db().execute(
            "PRAGMA foreign_keys"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 1

        # Functional: FK fires when inserting binding with non-existent profile
        from pi_agent_core_py.web.provider_config_store import (
            ProviderProfileNotFoundError,
        )
        with pytest.raises(ProviderProfileNotFoundError):
            await store.upsert_binding(
                session_id="sess-1",
                profile_id="profile-nonexistent",
                model_id="model-X",
                source="explicit",
            )
    finally:
        await store.close()


# ============================================================================
# 15. Invalid schema not silently repaired
# ============================================================================


async def test_15_invalid_schema_not_silently_repaired(tmp_path: Path) -> None:
    """If validation fails, schema is NOT recreated—caller must handle error."""
    db = tmp_path / "corrupt.db"
    store = await SQLiteProviderConfigStore.open(db)
    await store.close()

    # Drop required column—validation must reject, NOT auto-recreate
    await _corrupt(db, _drop_credential_id_column)

    with pytest.raises(ProviderConfigSchemaValidationError):
        await SQLiteProviderConfigStore.open(db)

    # Confirm DB still has the broken schema (no auto-repair)
    conn = await aiosqlite.connect(str(db))
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(
            "PRAGMA table_info(web_provider_profiles)"
        ) as cursor:
            rows = await cursor.fetchall()
        columns = {r["name"] for r in rows}
        assert "credential_id" not in columns, (
            "schema validation must not auto-repair"
        )
    finally:
        await conn.close()


# ============================================================================
# Additional: forbidden columns never appear in schema
# ============================================================================


async def test_forbidden_columns_never_appear_in_schema(tmp_path: Path) -> None:
    """Schema must NEVER contain api_key / secret / authorization / etc."""
    db = tmp_path / "no_secret.db"
    store = await SQLiteProviderConfigStore.open(db)
    try:
        async with store._require_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'web_%' AND name != 'web_provider_config_schema_meta'"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            table = row["name"]
            async with store._require_db().execute(
                f"PRAGMA table_info({table})"
            ) as cursor:
                cols = await cursor.fetchall()
            col_names = {c["name"] for c in cols}
            forbidden = {
                "api_key", "secret", "secret_value", "secret_ref",
                "fingerprint", "masked_value", "authorization",
                "headers", "base_url", "validation_endpoint",
            }
            overlap = col_names & forbidden
            assert not overlap, (
                f"table {table} contains forbidden columns: {overlap}"
            )
    finally:
        await store.close()

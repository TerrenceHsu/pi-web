"""Credentials store schema tests（P1-E1-2）.

覆盖：
- Fresh DB 初始化
- 重复 init 幂等
- version=1 正常路径
- 未知高版本拒绝
- version 存在但表缺失
- version 存在但列缺失
- version 存在但索引缺失
- 独立 meta 不修改 extension schema version
- 事务失败回滚
"""
from __future__ import annotations

import aiosqlite
import pytest

from pi_agent_core_py.web.credentials_store import (
    WEB_CREDENTIALS_SCHEMA_VERSION,
    CredentialsSchemaValidationError,
    CredentialsSchemaVersionError,
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.extension_store import SCHEMA_VERSION as EXTENSION_SCHEMA_VERSION

# ============================================================================
# Fresh DB
# ============================================================================


class TestFreshDB:
    async def test_fresh_db_initializes_to_v1(self, tmp_path) -> None:
        store = await SQLiteCredentialStore.open(str(tmp_path / "creds.db"))

        version = await store.get_schema_version()
        assert version == WEB_CREDENTIALS_SCHEMA_VERSION == 1

        await store.close()

    async def test_init_creates_credentials_table(self, tmp_path) -> None:
        db_path = tmp_path / "creds.db"
        store = await SQLiteCredentialStore.open(str(db_path))
        await store.close()

        # 重开 raw connection 检查 schema
        async with aiosqlite.connect(str(db_path)) as raw:
            async with raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='web_credentials'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "web_credentials"

    async def test_init_creates_indexes(self, tmp_path) -> None:
        db_path = tmp_path / "creds.db"
        store = await SQLiteCredentialStore.open(str(db_path))
        await store.close()

        async with aiosqlite.connect(str(db_path)) as raw:
            async with raw.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_web_credentials_%'"
            ) as cur:
                rows = await cur.fetchall()
            names = {r[0] for r in rows}
            assert "idx_web_credentials_updated_at" in names
            assert "idx_web_credentials_fingerprint" in names

    async def test_init_creates_schema_meta(self, tmp_path) -> None:
        db_path = tmp_path / "creds.db"
        store = await SQLiteCredentialStore.open(str(db_path))
        await store.close()

        async with aiosqlite.connect(str(db_path)) as raw:
            async with raw.execute(
                "SELECT key, value FROM web_credentials_schema_meta"
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "version"
            assert rows[0][1] == 1


# ============================================================================
# Idempotency
# ============================================================================


class TestIdempotency:
    async def test_repeated_init_is_idempotent(self, tmp_path) -> None:
        db_path = str(tmp_path / "creds.db")
        store = await SQLiteCredentialStore.open(db_path)
        # open() validates existing schema——calling again via separate instance is OK
        store_2 = await SQLiteCredentialStore.open(db_path)
        version = await store.get_schema_version()
        assert version == 1
        await store.close()
        await store_2.close()

    async def test_reopen_db_preserves_version(self, tmp_path) -> None:
        db_path = str(tmp_path / "creds.db")

        s1 = await SQLiteCredentialStore.open(db_path)
        await s1.close()

        s2 = await SQLiteCredentialStore.open(db_path)
        assert await s2.get_schema_version() == 1
        await s2.close()


# ============================================================================
# Unknown high version
# ============================================================================


class TestUnknownVersion:
    async def test_higher_version_rejected(self, tmp_path) -> None:
        db_path = str(tmp_path / "creds.db")

        # 用第一次 init 建立 schema
        s1 = await SQLiteCredentialStore.open(db_path)
        await s1.close()

        # 手动 bump version 到未来值
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute(
                "UPDATE web_credentials_schema_meta SET value = ? WHERE key = 'version'",
                (WEB_CREDENTIALS_SCHEMA_VERSION + 1,),
            )
            await raw.commit()

        # 重开应抛 CredentialsSchemaVersionError
        with pytest.raises(CredentialsSchemaVersionError):
            await SQLiteCredentialStore.open(db_path)


# ============================================================================
# Validation failures——version=1 but structure broken
# ============================================================================


class TestValidationFailures:
    async def _init_then_break(self, tmp_path, breaker) -> str:
        """helper: init fresh schema, run breaker on raw conn, return db_path."""
        db_path = str(tmp_path / "creds.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.close()

        async with aiosqlite.connect(db_path) as raw:
            await breaker(raw)
            await raw.commit()
        return db_path

    async def test_version_present_but_table_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("DROP TABLE web_credentials")

        db_path = await self._init_then_break(tmp_path, breaker)

        with pytest.raises(CredentialsSchemaValidationError, match="missing"):
            await SQLiteCredentialStore.open(db_path)

    async def test_version_present_but_column_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            # SQLite 不支持 DROP COLUMN 直接——重建表去masked_value 列
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old_creds")
            await raw.execute(
                """
                CREATE TABLE web_credentials (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    storage_mode TEXT NOT NULL,
                    secret_ref TEXT NOT NULL UNIQUE,
                    fingerprint_sha256 TEXT,
                    provider_hint TEXT,
                    provider_hint_confidence TEXT,
                    validation_status TEXT NOT NULL,
                    last_validated_provider_id TEXT,
                    last_validated_at INTEGER,
                    last_error_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            # masked_value 缺失

        db_path = await self._init_then_break(tmp_path, breaker)

        with pytest.raises(CredentialsSchemaValidationError, match="missing columns"):
            await SQLiteCredentialStore.open(db_path)

    async def test_version_present_but_index_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("DROP INDEX idx_web_credentials_updated_at")

        db_path = await self._init_then_break(tmp_path, breaker)

        with pytest.raises(
            CredentialsSchemaValidationError,
            match="idx_web_credentials_updated_at",
        ):
            await SQLiteCredentialStore.open(db_path)


# ============================================================================
# Independence from extension_store schema version
# ============================================================================


class TestExtensionStoreIndependence:
    async def test_credentials_store_does_not_touch_extension_schema_meta(
        self, tmp_path
    ) -> None:
        """credentials_store 用 web_credentials_schema_meta——不污染 web_extension_schema_meta.

        E1-2.1：两个 store 用**独立 connection** 共享同一 DB 文件（生产路径）.
        """
        db_path = str(tmp_path / "shared.db")

        from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

        # ext 用自己的 connection（默认 isolation_level）
        async with aiosqlite.connect(db_path) as ext_conn:
            ext_conn.row_factory = aiosqlite.Row
            ext = ExtensionSQLiteStore(db_path, connection=ext_conn)
            await ext.init()

            # credentials 用独立 connection（isolation_level=None——由 open() 配置）
            creds = await SQLiteCredentialStore.open(db_path)

            # 各自独立 version
            ext_version = await ext.get_schema_version()
            cred_version = await creds.get_schema_version()
            assert ext_version == EXTENSION_SCHEMA_VERSION  # 仍为 2
            assert cred_version == WEB_CREDENTIALS_SCHEMA_VERSION  # 1

            # 各自独立 meta 表（通过任一 connection 可见——同一 DB 文件）
            async with ext_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'web_%_schema_meta'"
            ) as cur:
                rows = await cur.fetchall()
            meta_names = {r[0] for r in rows}
            assert "web_extension_schema_meta" in meta_names
            assert "web_credentials_schema_meta" in meta_names

            await creds.close()

    async def test_extension_store_schema_version_unchanged_after_credential_init(
        self, tmp_path
    ) -> None:
        db_path = str(tmp_path / "shared.db")

        from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

        async with aiosqlite.connect(db_path) as ext_conn:
            ext_conn.row_factory = aiosqlite.Row
            ext = ExtensionSQLiteStore(db_path, connection=ext_conn)
            await ext.init()

            before = await ext.get_schema_version()

            # credentials store 用独立 connection——不影响 ext schema
            creds = await SQLiteCredentialStore.open(db_path)
            await creds.close()
            # 再次 open 也不影响（idempotent validation）
            creds_2 = await SQLiteCredentialStore.open(db_path)
            await creds_2.close()

            after = await ext.get_schema_version()
            assert before == after == EXTENSION_SCHEMA_VERSION


# ============================================================================
# Transaction rollback on failure
# ============================================================================


class TestTransactionRollback:
    async def test_create_failure_rolls_back(self, tmp_path) -> None:
        """create() IntegrityError 时 ROLLBACK——meta 和 table 仍在，下次 init 可重试."""
        db_path = str(tmp_path / "creds.db")
        store = await SQLiteCredentialStore.open(db_path)

        from pi_agent_core_py.web.credentials_store import (
            CredentialAlreadyExistsError,
            CredentialRecord,
        )

        rec1 = CredentialRecord(
            id="cred-1",
            label="Test",
            storage_mode="keyring",
            secret_ref="cred-ref-1",
            masked_value="sk****8A31",
            fingerprint_sha256=None,
            provider_hint=None,
            provider_hint_confidence=None,
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=1000,
            updated_at=1000,
        )
        rec2 = CredentialRecord(
            id="cred-1",           # duplicate id
            label="Test",
            storage_mode="keyring",
            secret_ref="cred-ref-2",  # different secret_ref——只触发 id conflict
            masked_value="sk****8A31",
            fingerprint_sha256=None,
            provider_hint=None,
            provider_hint_confidence=None,
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=1000,
            updated_at=1000,
        )
        await store.create(rec1)

        # Duplicate primary key only
        with pytest.raises(CredentialAlreadyExistsError):
            await store.create(rec2)

        # Schema should still be intact——version readable
        version = await store.get_schema_version()
        assert version == 1
        await store.close()

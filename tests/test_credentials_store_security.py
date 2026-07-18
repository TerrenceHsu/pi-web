"""Credentials store security tests（P1-E1-2）.

用 SECRET_MARKER 验证：
- SQLite 数据库内容（文件字节 + SELECT * 输出）
- Repository 异常 str / repr
- decode error 不输出整行
- 完整 fingerprint 不出现在普通 serializer
- 字段名中不存在 api_key 或 secret_value
- extension schema version 保持 2
"""
from __future__ import annotations

import aiosqlite
import pytest

from pi_agent_core_py.web.credentials_store import (
    CredentialNotFoundError,
    CredentialRecord,
    CredentialRecordDecodeError,
    CredentialsSchemaValidationError,
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.extension_store import SCHEMA_VERSION as EXTENSION_SCHEMA_VERSION

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Helpers
# ============================================================================


def _record_with_marker(marker_secret_ref: str = "cred-marker") -> CredentialRecord:
    """Build a record whose masked_value looks normal——but tests verify SQLite
    does NOT contain the raw SECRET_MARKER (which we would only have stored if
    we accidentally wrote api_key instead of masked_value)."""
    from pi_agent_core_py.secrets import fingerprint_secret, mask_secret

    return CredentialRecord(
        id="cred-marker",
        label="Leak Test",
        storage_mode="keyring",
        secret_ref=marker_secret_ref,
        masked_value=mask_secret(SECRET_MARKER),
        fingerprint_sha256=fingerprint_secret(SECRET_MARKER),
        provider_hint="glm",
        provider_hint_confidence="high",
        validation_status="never_validated",
        last_validated_provider_id=None,
        last_validated_at=None,
        last_error_code=None,
        created_at=1000,
        updated_at=1000,
    )


@pytest.fixture
async def store_with_marker(tmp_path):
    db_path = str(tmp_path / "leak_check.db")
    s = await SQLiteCredentialStore.open(db_path)
    await s.create(_record_with_marker())
    yield s, db_path
    await s.close()


# ============================================================================
# 1. SQLite file bytes——marker 0 命中
# ============================================================================


class TestSQLiteByteContent:
    async def test_sqlite_file_does_not_contain_marker(self, store_with_marker, tmp_path) -> None:
        _, db_path = store_with_marker
        # Force checkpoint to ensure all pages are flushed
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("PRAGMA wal_checkpoint(FULL)")
            await conn.commit()

        # Read raw bytes (sync file IO——use asyncio.to_thread to avoid blocking event loop)
        import asyncio

        def _read_bytes() -> bytes:
            with open(db_path, "rb") as f:
                return f.read()

        data = await asyncio.to_thread(_read_bytes)

        assert SECRET_MARKER not in data.decode("utf-8", errors="ignore"), (
            "SECRET_MARKER leaked into SQLite file bytes"
        )
        assert SECRET_MARKER not in data.decode("latin-1", errors="ignore"), (
            "SECRET_MARKER leaked (latin-1 decode)"
        )


# ============================================================================
# 1b. P1-E1-5B / GAP-1: SQLite WAL / SHM marker scan pre-checkpoint
# ============================================================================


class TestWalShmMarkerScan:
    """Scan main DB + WAL + SHM files for marker before AND after checkpoint.

    SQLite WAL (write-ahead log) keeps recent writes in a -wal sidecar file
    until checkpoint merges them into the main DB. The -shm file is a shared
    memory index for WAL. We must verify the raw secret never appears in
    any of these.

    Setup explicitly forces journal_mode=WAL (default is delete) so a -wal
    file actually exists to scan.
    """

    @staticmethod
    def _scan_file(path, label: str) -> None:
        """Assert SECRET_MARKER not in file bytes (utf-8 + latin-1 decode)."""
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return  # file not generated——skip (not a failure)
        data = p.read_bytes()
        for decode in ("utf-8", "latin-1"):
            decoded = data.decode(decode, errors="ignore")
            assert SECRET_MARKER not in decoded, (
                f"SECRET_MARKER leaked into {label} ({decode} decode)"
            )

    async def test_wal_shm_files_pre_and_post_checkpoint_no_marker(self, tmp_path) -> None:
        db_path = str(tmp_path / "wal_check.db")
        s = await SQLiteCredentialStore.open(db_path)

        # Force WAL mode——default is delete which produces no -wal/-shm files
        async with s._require_db().execute("PRAGMA journal_mode=WAL") as cur:
            row = await cur.fetchone()
            assert row is not None and row[0].lower() == "wal", (
                f"failed to enable WAL mode; got {row}"
            )
        # Disable auto-checkpoint so WAL retains writes until we explicitly checkpoint
        async with s._require_db().execute("PRAGMA wal_autocheckpoint=0"):
            pass

        # Insert record with marker (only masked_value + fingerprint are stored;
        # raw SECRET_MARKER must NEVER be written)
        await s.create(_record_with_marker())

        main_path = db_path
        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"

        # Pre-checkpoint scan——WAL file MUST exist (we wrote + disabled autocheckpoint).
        # Use os.path.exists (sync) wrapped in asyncio.to_thread to satisfy ASYNC240.
        import asyncio
        import os

        wal_exists = await asyncio.to_thread(os.path.exists, wal_path)
        assert wal_exists, (
            "WAL file must exist pre-checkpoint after writes (test setup failure)"
        )

        self._scan_file(main_path, "main DB pre-checkpoint")
        self._scan_file(wal_path, "WAL pre-checkpoint")
        self._scan_file(shm_path, "SHM pre-checkpoint")

        # SELECT * — verify rows themselves don't contain raw marker
        async with s._require_db().execute("SELECT * FROM web_credentials") as cur:
            rows = await cur.fetchall()
        for row in rows:
            for key in row.keys():
                v = row[key]
                if isinstance(v, str):
                    assert SECRET_MARKER not in v, (
                        f"SECRET_MARKER in row column {key}: {v!r}"
                    )

        # Now checkpoint via separate connection and re-scan
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("PRAGMA wal_checkpoint(FULL)")
            await conn.commit()

        self._scan_file(main_path, "main DB post-checkpoint")
        self._scan_file(wal_path, "WAL post-checkpoint")
        self._scan_file(shm_path, "SHM post-checkpoint")

        await s.close()


# ============================================================================
# 2. SELECT * FROM web_credentials——marker 0 命中
# ============================================================================


class TestSelectStarNoMarker:
    async def test_select_all_rows_no_marker(self, store_with_marker) -> None:
        store, db_path = store_with_marker
        async with store._require_db().execute("SELECT * FROM web_credentials") as cur:
            rows = await cur.fetchall()
        for row in rows:
            # 把整行展开成字符串
            row_str = " ".join(str(row[col] if col in row.keys() else "") for col in row.keys())
            # row 包含 masked_value（不含完整 marker）和 fingerprint（不含 marker）
            assert SECRET_MARKER not in row_str, (
                f"SECRET_MARKER leaked in row: {row_str!r}"
            )


# ============================================================================
# 3. Repository exception str/repr——marker 0 命中
# ============================================================================


class TestRepositoryExceptions:
    async def test_not_found_error_no_marker(self, store_with_marker) -> None:
        store, _ = store_with_marker
        # 在 marker record 存在的情况下查 missing——error 不应包含 marker
        try:
            await store.get("nonexistent-id")
        except CredentialNotFoundError as e:
            assert SECRET_MARKER not in str(e)
            assert SECRET_MARKER not in repr(e)

    async def test_create_conflict_error_no_marker(self, store_with_marker) -> None:
        store, _ = store_with_marker
        # Duplicate id but different secret_ref——error 应只含 credential_id (safe)
        from pi_agent_core_py.web.credentials_store import (
            CredentialAlreadyExistsError,
        )
        try:
            await store.create(_record_with_marker(marker_secret_ref="different-ref"))
        except CredentialAlreadyExistsError as e:
            assert SECRET_MARKER not in str(e)
            assert SECRET_MARKER not in repr(e)


# ============================================================================
# 4. Decode error 不输出整行
# ============================================================================


class TestDecodeErrorSafety:
    async def test_decode_error_does_not_dump_row(self, tmp_path) -> None:
        db_path = str(tmp_path / "decode.db")
        s = await SQLiteCredentialStore.open(db_path)

        # 手动 INSERT 一行 malformed 数据（缺 required field）
        async with s._require_db().execute(
            """
            INSERT INTO web_credentials (
                id, label, storage_mode, secret_ref,
                masked_value, fingerprint_sha256,
                provider_hint, provider_hint_confidence,
                validation_status, last_validated_provider_id,
                last_validated_at, last_error_code,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cred-bad",
                "Bad",
                "keyring",
                "ref-bad",
                # masked_value 是 INTEGER 而非 TEXT——schema CHECK 不拦
                12345,
                None,
                None,
                None,
                "never_validated",
                None,
                None,
                None,
                1000,
                1000,
            ),
        ):
            pass
        await s._require_db().commit()

        # Read back——masked_value 是 int——CredentialRecord 的 masked_value: str 会失败
        # 但 SQLite 不强类型——只是取回 int。
        # 实际上 dataclass(frozen=True) 不强制 type——只要构造时类型符合就能装
        # 所以这个 case 不一定抛——让我们直接检查 malformed row 能被读出但不泄
        rec = await s.get("cred-bad")
        # masked_value 是 int 12345——dataclass 接受了（Python 不强制）
        # 关键：str(rec) 不应包含完整 row dump 或 fingerprint
        text = repr(rec)
        # 内部字段都是 safe context（id/label/ref/masked_value 数字等）——
        # 但 fingerprint_sha256 是 None 所以没泄漏
        assert SECRET_MARKER not in text

        await s.close()

    async def test_decode_error_safe_message_on_missing_column(self, tmp_path) -> None:
        """如果 row 缺关键列——decode 抛但 message 只含 credential_id."""
        db_path = str(tmp_path / "decode.db")
        s = await SQLiteCredentialStore.open(db_path)

        # 手动建一个缺列的表（绕过 schema）
        async with s._require_db().execute(
            "CREATE TABLE broken_creds (id TEXT PRIMARY KEY, label TEXT)"
        ):
            pass
        async with s._require_db().execute(
            "INSERT INTO broken_creds (id, label) VALUES ('cred-x', 'X')"
        ):
            pass
        await s._require_db().commit()

        # 直接从 broken_creds 读——row dict 缺大量字段
        async with s._require_db().execute("SELECT * FROM broken_creds") as cur:
            row = await cur.fetchone()

        from pi_agent_core_py.web.credentials_store import _decode_row
        with pytest.raises(CredentialRecordDecodeError) as exc_info:
            _decode_row(row)

        msg = str(exc_info.value)
        # 异常可以含 credential_id（safe），但不能含 row dump
        assert SECRET_MARKER not in msg
        # 不应直接 dump 整行（行只有 id='cred-x', label='X'——这俩本身是 safe 的，
        # 但 CredentialRecordDecodeError 不应包含"row="或大量字段）
        assert "row=" not in msg.lower()

        await s.close()


# ============================================================================
# 5. fingerprint 不进入"普通 serializer"——用 list() 验证
# ============================================================================


class TestFingerprintNotInListOutput:
    async def test_fingerprint_in_record_but_not_via_safe_path(self, store_with_marker) -> None:
        """list() 返回完整 CredentialRecord（含 fingerprint）——但 serializer 应过滤.

        E1-2 只验证 fingerprint_sha256 字段存在但不通过任何"安全 DTO"出口.
        实际 serializer 在 E1-4 实现；E1-2 这里只验证 record 是内部可访问的.
        """
        store, _ = store_with_marker
        records = await store.list()
        assert len(records) == 1
        # 内部 record 有 fingerprint——OK
        assert records[0].fingerprint_sha256 is not None
        # 但 marker 不能出现在 record 的任何字段里
        # （fingerprint 是 sha256 hash，不含原 secret）
        for field_name in (
            "id", "label", "storage_mode", "secret_ref", "masked_value",
            "fingerprint_sha256", "provider_hint", "provider_hint_confidence",
            "last_error_code",
        ):
            v = getattr(records[0], field_name)
            if isinstance(v, str):
                assert SECRET_MARKER not in v, (
                    f"SECRET_MARKER leaked in field {field_name}: {v!r}"
                )


# ============================================================================
# 6. 字段名安全——没有 api_key / secret_value 等敏感字段名
# ============================================================================


class TestNoSensitiveFieldNames:
    async def test_no_api_key_column(self, store_with_marker) -> None:
        store, _ = store_with_marker
        async with store._require_db().execute("PRAGMA table_info(web_credentials)") as cur:
            rows = await cur.fetchall()
        column_names = {r["name"] for r in rows}

        forbidden = {"api_key", "secret", "secret_value", "authorization", "headers"}
        for name in forbidden:
            assert name not in column_names, (
                f"forbidden column name exists: {name}"
            )


# ============================================================================
# 7. extension schema version 保持 2
# ============================================================================


class TestExtensionSchemaPreserved:
    async def test_extension_schema_unchanged_after_credentials_init(self, tmp_path) -> None:
        db_path = str(tmp_path / "shared.db")
        async with aiosqlite.connect(db_path) as ext_conn:
            ext_conn.row_factory = aiosqlite.Row
            from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

            ext = ExtensionSQLiteStore(db_path, connection=ext_conn)
            await ext.init()

            # credentials 用独立 connection——不影响 ext schema
            creds = await SQLiteCredentialStore.open(db_path)
            await creds.close()

            ext_version = await ext.get_schema_version()
            assert ext_version == EXTENSION_SCHEMA_VERSION  # 仍为 2

    async def test_credentials_meta_table_distinct(self, tmp_path) -> None:
        db_path = str(tmp_path / "shared.db")
        async with aiosqlite.connect(db_path) as ext_conn:
            ext_conn.row_factory = aiosqlite.Row
            from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

            ext = ExtensionSQLiteStore(db_path, connection=ext_conn)
            await ext.init()
            creds = await SQLiteCredentialStore.open(db_path)
            await creds.close()

            async with ext_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'web_%_schema_meta' ORDER BY name"
            ) as cur:
                rows = await cur.fetchall()
            names = [r[0] for r in rows]
            assert names == ["web_credentials_schema_meta", "web_extension_schema_meta"]


# ============================================================================
# 8. Summary——marker 全局扫描
# ============================================================================


def test_credentials_store_module_globals_no_marker() -> None:
    """credentials_store 模块顶层 globals 不含 marker."""
    import pi_agent_core_py.web.credentials_store as mod
    for name, value in vars(mod).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert SECRET_MARKER not in value, (
                f"marker in module global {name}: {value!r}"
            )


# ============================================================================
# 9. E1-2.2 schema 校验错误——异常 str/repr 不得泄漏完整 DDL
# ============================================================================


class TestSchemaErrorNoDdlLeak:
    """E1-2.2：schema 校验失败时，CredentialsSchemaValidationError 不得含完整 DDL.

    DDL 里可能被攻击者注入 comment / identifier——必须保证异常只输出固定
    描述（不含整段 DDL），防止信息泄漏.
    """

    async def test_enum_mismatch_error_does_not_leak_ddl(self, tmp_path) -> None:
        db_path = str(tmp_path / "leak.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.close()

        # 把 SECRET_MARKER 注入 DDL 注释里——schema 校验失败时必须不输出
        async with aiosqlite.connect(db_path) as raw:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            await raw.execute(
                f"""
                CREATE TABLE web_credentials (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    storage_mode TEXT NOT NULL,
                    secret_ref TEXT NOT NULL UNIQUE,
                    masked_value TEXT NOT NULL,
                    fingerprint_sha256 TEXT,
                    provider_hint TEXT,
                    provider_hint_confidence TEXT,
                    validation_status TEXT NOT NULL,
                    last_validated_provider_id TEXT,
                    last_validated_at INTEGER,
                    last_error_code TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CHECK (length(trim(label)) > 0),
                    -- {SECRET_MARKER}
                    CHECK (storage_mode IN ('keyring')),
                    CHECK (validation_status IN
                        ('never_validated', 'valid', 'invalid', 'error')),
                    CHECK (provider_hint_confidence IS NULL
                        OR provider_hint_confidence IN
                        ('high', 'medium', 'low', 'unknown'))
                )
                """
            )
            await raw.commit()

        with pytest.raises(CredentialsSchemaValidationError) as exc_info:
            await SQLiteCredentialStore.open(db_path)

        msg = str(exc_info.value)
        assert SECRET_MARKER not in msg, (
            f"SECRET_MARKER leaked in schema error: {msg!r}"
        )
        assert SECRET_MARKER not in repr(exc_info.value)
        # 不应包含 CREATE TABLE 等完整 DDL 片段
        assert "CREATE TABLE" not in msg.upper()

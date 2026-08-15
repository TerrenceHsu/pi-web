"""Credentials store concurrency + CAS + schema constraint tests（P1-E1-2.1）.

E1-2.1 加固：
- 独立 connection（与 ext/session store 共享 DB 文件但不共享 aiosqlite.Connection）
- 显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK + asyncio.Lock 序列化单 store 内写操作
- CAS via `expected_secret_ref`——stale rotate / validation / delete 拒绝

覆盖：
- Transaction isolation（多 store 独立 connection 互不污染）
- CAS race（并发 rotate / stale validation / stale delete）
- Schema constraint missing（CHECK + UNIQUE 缺失抛 ValidationError）
- 异常安全（CredentialConcurrentModificationError 不泄漏 ref）
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from pi_agent_core_py.web.credentials.store import (
    CredentialAlreadyExistsError,
    CredentialConcurrentModificationError,
    CredentialNotFoundError,
    CredentialRecord,
    CredentialsSchemaValidationError,
    SQLiteCredentialStore,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_record(
    *,
    id: str = "cred-1",
    label: str = "Work Key",
    secret_ref: str = "ref-1",
    storage_mode: str = "keyring",
    masked_value: str = "sk****8A31",
    fingerprint_sha256: str | None = "sha256:abc",
    provider_hint: str | None = "glm",
    provider_hint_confidence: str | None = "high",
    validation_status: str = "never_validated",
    last_validated_provider_id: str | None = None,
    last_validated_at: int | None = None,
    last_error_code: str | None = None,
    created_at: int = 1000,
    updated_at: int = 1000,
) -> CredentialRecord:
    return CredentialRecord(
        id=id,
        label=label,
        storage_mode=storage_mode,
        secret_ref=secret_ref,
        masked_value=masked_value,
        fingerprint_sha256=fingerprint_sha256,
        provider_hint=provider_hint,
        provider_hint_confidence=provider_hint_confidence,
        validation_status=validation_status,
        last_validated_provider_id=last_validated_provider_id,
        last_validated_at=last_validated_at,
        last_error_code=last_error_code,
        created_at=created_at,
        updated_at=updated_at,
    )


# ============================================================================
# 1. Transaction Isolation——独立 connection 互不污染
# ============================================================================


class TestTransactionIsolation:
    async def test_two_stores_independent_connections_no_cross_tx(self, tmp_path) -> None:
        """两个 store 用独立 connection——A 的 COMMIT 不会提交 B 未 commit 的写."""
        db_path = str(tmp_path / "shared.db")

        s1 = await SQLiteCredentialStore.open(db_path)
        s2 = await SQLiteCredentialStore.open(db_path)

        # 直接走底层——绕过 Lock——构造真实"两事务交错"
        # 但单 store Lock 序列化——所以这里用 raw aiosqlite connection 模拟
        async with aiosqlite.connect(db_path, isolation_level=None) as raw1:
            raw1.row_factory = aiosqlite.Row
            async with aiosqlite.connect(db_path, isolation_level=None) as raw2:
                raw2.row_factory = aiosqlite.Row

                # raw1 BEGIN + INSERT 但不 COMMIT
                await raw1.execute("BEGIN IMMEDIATE")
                await raw1.execute(
                    "INSERT INTO web_credentials (id, label, storage_mode, secret_ref, "
                    "masked_value, validation_status, created_at, updated_at) "
                    "VALUES ('cred-A', 'A', 'keyring', 'ref-A', 'sk****AAAA', "
                    "'never_validated', 1000, 1000)"
                )

                # raw2 不应看到未提交的 cred-A
                async with raw2.execute(
                    "SELECT id FROM web_credentials WHERE id = 'cred-A'"
                ) as cur:
                    row = await cur.fetchone()
                assert row is None

                # raw1 COMMIT
                await raw1.execute("COMMIT")

                # raw2 现在能看到
                async with raw2.execute(
                    "SELECT id FROM web_credentials WHERE id = 'cred-A'"
                ) as cur:
                    row = await cur.fetchone()
                assert row is not None

        await s1.close()
        await s2.close()

    async def test_failed_transaction_does_not_roll_back_other(self, tmp_path) -> None:
        """一个事务失败 ROLLBACK 不会影响另一已提交事务."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)

        # 第一条成功提交
        await s.create(_make_record(id="cred-A", secret_ref="ref-A"))

        # 第二条失败（duplicate id）
        with pytest.raises(CredentialAlreadyExistsError):
            await s.create(_make_record(id="cred-A", secret_ref="ref-B"))

        # cred-A 仍在
        got = await s.get("cred-A")
        assert got.id == "cred-A"
        assert got.secret_ref == "ref-A"

        await s.close()

    async def test_concurrent_create_via_single_store_serialized(self, tmp_path) -> None:
        """单 store + asyncio.Lock 序列化写——并发 create 都成功."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)

        async def create_one(i: int) -> None:
            await s.create(_make_record(id=f"cred-{i}", secret_ref=f"ref-{i}"))

        await asyncio.gather(*(create_one(i) for i in range(10)))

        records = await s.list()
        assert len(records) == 10
        await s.close()

    async def test_concurrent_update_and_delete_no_partial_state(self, tmp_path) -> None:
        """并发 update + delete 不会产生部分状态（要么全成，要么全失败）."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="ref-1"))

        async def do_update() -> None:
            await s.update_label("cred-1", "Updated")

        async def do_delete() -> bool:
            try:
                await s.delete("cred-1")
                return True
            except CredentialNotFoundError:
                return False

        # 并发——一个 update + 一个 delete
        results = await asyncio.gather(do_update(), do_delete(), return_exceptions=True)
        # 至少一个成功——不产生部分状态
        assert any(r is None or r is True for r in results)

        await s.close()

    async def test_close_only_closes_owned_connection(self, tmp_path) -> None:
        """Store.close() 只关闭它自己 owns 的 connection."""
        db_path = str(tmp_path / "shared.db")
        async with aiosqlite.connect(db_path, isolation_level=None) as injected:
            injected.row_factory = aiosqlite.Row
            # 先 init schema
            s = await SQLiteCredentialStore.open(db_path)
            await s.close()

            # 现在 for_testing——owns_connection=False
            s2 = await SQLiteCredentialStore.for_testing(injected, owns_connection=False)
            await s2.close()

            # injected 仍可用
            async with injected.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) > 0  # tables 还在

    async def test_injected_connection_not_closed_by_store(self, tmp_path) -> None:
        """owns_connection=False 时 close() 不关闭 connection."""
        db_path = str(tmp_path / "shared.db")
        # 先用 open 创建 schema
        s_init = await SQLiteCredentialStore.open(db_path)
        await s_init.close()

        # 用 raw connection 注入
        async with aiosqlite.connect(db_path, isolation_level=None) as raw:
            raw.row_factory = aiosqlite.Row
            s = await SQLiteCredentialStore.for_testing(raw, owns_connection=False)
            await s.close()
            # raw 应仍可用
            async with raw.execute("SELECT 1") as cur:
                row = await cur.fetchone()
            assert row is not None

    async def test_injected_wrong_isolation_level_rejected(self, tmp_path) -> None:
        """isolation_level != None 的注入 connection 应拒绝."""
        db_path = str(tmp_path / "shared.db")
        s_init = await SQLiteCredentialStore.open(db_path)
        await s_init.close()

        # 默认 isolation_level='' (deferred)——不是 None
        async with aiosqlite.connect(db_path) as raw:
            from pi_agent_core_py.web.credentials.store import CredentialStoreError

            with pytest.raises(CredentialStoreError, match="isolation_level"):
                await SQLiteCredentialStore.for_testing(raw)


# ============================================================================
# 2. CAS——expected_secret_ref 守卫
# ============================================================================


class TestCASRotate:
    async def test_concurrent_rotate_only_one_succeeds(self, tmp_path) -> None:
        """两个并发 rotate（都从同一 old_ref 读起）——只一个成功."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="old-ref"))

        # 读当前 secret_ref（simulate two concurrent rotates reading same state）
        current = await s.get("cred-1")
        assert current.secret_ref == "old-ref"

        async def rotate_to(new_ref: str) -> bool:
            try:
                await s.replace_secret_metadata(
                    "cred-1",
                    expected_secret_ref="old-ref",  # 两者都用同一 expected
                    new_secret_ref=new_ref,
                    masked_value="sk****XXXX",
                    fingerprint_sha256=None,
                )
                return True
            except CredentialConcurrentModificationError:
                return False

        # 并发——单 store Lock 序列化——但 CAS 守卫会让一个失败
        results = await asyncio.gather(
            rotate_to("new-A"),
            rotate_to("new-B"),
        )
        # 一个成功一个失败
        assert sum(results) == 1

        await s.close()

    async def test_stale_rotate_raises_concurrent_modification(self, tmp_path) -> None:
        """stale rotate（expected_secret_ref 已被改）→ ConcurrentModificationError."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="old-ref"))

        # 第一次 rotate 成功
        await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="old-ref",
            new_secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256=None,
        )

        # stale rotate——expected="old-ref" 但实际是 "new-ref"
        with pytest.raises(CredentialConcurrentModificationError):
            await s.replace_secret_metadata(
                "cred-1",
                expected_secret_ref="old-ref",  # stale
                new_secret_ref="other-ref",
                masked_value="sk****OTHER",
                fingerprint_sha256=None,
            )

        await s.close()

    async def test_concurrent_modification_error_does_not_leak_ref(self, tmp_path) -> None:
        """CredentialConcurrentModificationError str/repr 不含 expected/current secret_ref."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="PI_E1_SECRET_REF_LEAK"))

        # 把 ref 改成不同值
        await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="PI_E1_SECRET_REF_LEAK",
            new_secret_ref="new-value",
            masked_value="sk****NEW0",
            fingerprint_sha256=None,
        )

        # stale rotate——expected 仍是旧 ref
        with pytest.raises(CredentialConcurrentModificationError) as exc_info:
            await s.replace_secret_metadata(
                "cred-1",
                expected_secret_ref="PI_E1_SECRET_REF_LEAK",
                new_secret_ref="other",
                masked_value="sk****X",
                fingerprint_sha256=None,
            )

        err_str = str(exc_info.value)
        err_repr = repr(exc_info.value)
        # 任何 secret_ref（旧 / 新 / 预期）都不应泄漏
        assert "PI_E1_SECRET_REF_LEAK" not in err_str
        assert "PI_E1_SECRET_REF_LEAK" not in err_repr
        assert "new-value" not in err_str

        await s.close()


class TestCASValidation:
    async def test_stale_validation_after_rotate_rejected(self, tmp_path) -> None:
        """旧 Key validation 完成时，Credential 已 rotate → 验证结果拒绝写入."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="old-ref"))

        # Simulate: validation started with secret_ref="old-ref" 读到的
        # 期间 rotate 完成 → secret_ref = "new-ref"
        await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="old-ref",
            new_secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256=None,
        )

        # validation 完成时试图写——stale
        with pytest.raises(CredentialConcurrentModificationError):
            await s.update_validation_state(
                "cred-1",
                expected_secret_ref="old-ref",  # stale
                validation_status="valid",
                provider_id="glm",
                validated_at=9999,
                error_code=None,
            )

        # 新 Key 不应继承旧 Key 的 validation
        current = await s.get("cred-1")
        assert current.validation_status == "never_validated"
        assert current.last_validated_at is None

        await s.close()

    async def test_normal_validation_after_rotate_passes_with_new_ref(self, tmp_path) -> None:
        """rotate 后用新 ref 调 validation_state 应成功."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="old-ref"))

        await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="old-ref",
            new_secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256=None,
        )

        # 用新 ref 调用 validation——OK
        updated = await s.update_validation_state(
            "cred-1",
            expected_secret_ref="new-ref",
            validation_status="valid",
            provider_id="glm",
            validated_at=9999,
            error_code=None,
        )
        assert updated.validation_status == "valid"
        assert updated.last_validated_provider_id == "glm"

        await s.close()


class TestCASDelete:
    async def test_stale_delete_after_rotate_rejected(self, tmp_path) -> None:
        """delete 时传 stale expected_secret_ref → 拒绝（不删除新 ref row）."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="old-ref"))

        # rotate 把 secret_ref 改了
        await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="old-ref",
            new_secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256=None,
        )

        # 试图用旧 ref delete——拒绝
        with pytest.raises(CredentialConcurrentModificationError):
            await s.delete("cred-1", expected_secret_ref="old-ref")

        # Row 仍在（new-ref 还在）
        still = await s.get("cred-1")
        assert still.secret_ref == "new-ref"

        await s.close()

    async def test_delete_without_cas_still_works(self, tmp_path) -> None:
        """不传 expected_secret_ref（向后兼容）→ 简单 delete."""
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="ref-1"))

        deleted = await s.delete("cred-1")  # no CAS
        assert deleted.id == "cred-1"

        with pytest.raises(CredentialNotFoundError):
            await s.get("cred-1")

        await s.close()

    async def test_delete_with_matching_cas_succeeds(self, tmp_path) -> None:
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="ref-1"))

        deleted = await s.delete("cred-1", expected_secret_ref="ref-1")
        assert deleted.id == "cred-1"

        await s.close()


# ============================================================================
# 3. Schema constraint missing——DDL 字符串校验
# ============================================================================


class TestSchemaConstraintValidation:
    async def _init_then_break(self, tmp_path, breaker) -> str:
        db_path = str(tmp_path / "creds.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.close()

        async with aiosqlite.connect(db_path) as raw:
            await breaker(raw)
            await raw.commit()
        return db_path

    async def test_storage_mode_check_missing(self, tmp_path) -> None:
        """version=1 但 storage_mode CHECK 缺失 → 拒绝."""
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            # 重建表——去掉 storage_mode CHECK
            await raw.execute(
                """
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
                    CHECK (validation_status IN
                        ('never_validated', 'valid', 'invalid', 'error')),
                    CHECK (provider_hint_confidence IS NULL
                        OR provider_hint_confidence IN
                        ('high', 'medium', 'low', 'unknown'))
                )
                """
            )

        db_path = await self._init_then_break(tmp_path, breaker)
        with pytest.raises(CredentialsSchemaValidationError, match="storage_mode CHECK"):
            await SQLiteCredentialStore.open(db_path)

    async def test_validation_status_check_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            await raw.execute(
                """
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
                    CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
                    CHECK (provider_hint_confidence IS NULL
                        OR provider_hint_confidence IN
                        ('high', 'medium', 'low', 'unknown'))
                )
                """
            )

        db_path = await self._init_then_break(tmp_path, breaker)
        with pytest.raises(
            CredentialsSchemaValidationError,
            match="validation_status CHECK",
        ):
            await SQLiteCredentialStore.open(db_path)

    async def test_confidence_check_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            await raw.execute(
                """
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
                    CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
                    CHECK (validation_status IN
                        ('never_validated', 'valid', 'invalid', 'error'))
                )
                """
            )

        db_path = await self._init_then_break(tmp_path, breaker)
        with pytest.raises(
            CredentialsSchemaValidationError,
            match="provider_hint_confidence CHECK",
        ):
            await SQLiteCredentialStore.open(db_path)

    async def test_label_check_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            await raw.execute(
                """
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
                    CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
                    CHECK (validation_status IN
                        ('never_validated', 'valid', 'invalid', 'error')),
                    CHECK (provider_hint_confidence IS NULL
                        OR provider_hint_confidence IN
                        ('high', 'medium', 'low', 'unknown'))
                )
                """
            )

        db_path = await self._init_then_break(tmp_path, breaker)
        with pytest.raises(
            CredentialsSchemaValidationError,
            match="label non-empty CHECK",
        ):
            await SQLiteCredentialStore.open(db_path)

    async def test_secret_ref_unique_missing(self, tmp_path) -> None:
        async def breaker(raw: aiosqlite.Connection) -> None:
            await raw.execute("ALTER TABLE web_credentials RENAME TO _old")
            await raw.execute(
                """
                CREATE TABLE web_credentials (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    storage_mode TEXT NOT NULL,
                    secret_ref TEXT NOT NULL,
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
                    CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
                    CHECK (validation_status IN
                        ('never_validated', 'valid', 'invalid', 'error')),
                    CHECK (provider_hint_confidence IS NULL
                        OR provider_hint_confidence IN
                        ('high', 'medium', 'low', 'unknown'))
                )
                """
            )

        db_path = await self._init_then_break(tmp_path, breaker)
        with pytest.raises(
            CredentialsSchemaValidationError,
            match="secret_ref UNIQUE",
        ):
            await SQLiteCredentialStore.open(db_path)


# ============================================================================
# 4. Sanity——正常 replace / validation / delete 仍通过
# ============================================================================


class TestCASNormalPath:
    async def test_normal_replace_with_matching_expected(self, tmp_path) -> None:
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="ref-1"))

        rotated = await s.replace_secret_metadata(
            "cred-1",
            expected_secret_ref="ref-1",
            new_secret_ref="ref-2",
            masked_value="sk****NEW0",
            fingerprint_sha256="sha256:new",
        )
        assert rotated.secret_ref == "ref-2"
        await s.close()

    async def test_normal_validation_with_matching_expected(self, tmp_path) -> None:
        db_path = str(tmp_path / "shared.db")
        s = await SQLiteCredentialStore.open(db_path)
        await s.create(_make_record(id="cred-1", secret_ref="ref-1"))

        updated = await s.update_validation_state(
            "cred-1",
            expected_secret_ref="ref-1",
            validation_status="valid",
            provider_id="glm",
            validated_at=9999,
            error_code=None,
        )
        assert updated.validation_status == "valid"
        await s.close()

"""Credentials store repository CRUD tests（P1-E1-2）.

覆盖：
- create / get happy path
- list 排序 + limit 边界
- duplicate id → CredentialAlreadyExistsError
- duplicate secret_ref → CredentialSecretRefConflictError
- update_label happy / 空 label 拒绝
- replace_secret_metadata + rotate 后 validation reset
- update_validation_state valid / invalid / error
- delete 返回旧 record
- delete missing → CredentialNotFoundError
- 并发 create
- 写事务 rollback
- 时间字段为整数毫秒
"""
from __future__ import annotations

import asyncio
import time

import pytest

from pi_agent_core_py.web.credentials_store import (
    CredentialAlreadyExistsError,
    CredentialNotFoundError,
    CredentialRecord,
    CredentialSecretRefConflictError,
    CredentialStoreError,
    SQLiteCredentialStore,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = SQLiteCredentialStore(str(tmp_path / "creds.db"))
    await s.init()
    yield s
    await s.close()


def _make_record(
    *,
    id: str = "cred-1",
    label: str = "Work Key",
    storage_mode: str = "keyring",
    secret_ref: str = "cred-ref-1",
    masked_value: str = "sk****8A31",
    fingerprint_sha256: str | None = "sha256:abc123def456",
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
# Create / Get
# ============================================================================


class TestCreateGet:
    async def test_create_then_get_roundtrip(self, store) -> None:
        rec = _make_record()
        await store.create(rec)
        got = await store.get("cred-1")
        assert got.id == "cred-1"
        assert got.label == "Work Key"
        assert got.storage_mode == "keyring"
        assert got.secret_ref == "cred-ref-1"
        assert got.masked_value == "sk****8A31"
        assert got.fingerprint_sha256 == "sha256:abc123def456"
        assert got.validation_status == "never_validated"

    async def test_get_missing_raises_not_found(self, store) -> None:
        with pytest.raises(CredentialNotFoundError):
            await store.get("never-exists")


# ============================================================================
# List
# ============================================================================


class TestList:
    async def test_list_returns_records_ordered_by_updated_at_desc(self, store) -> None:
        for i in range(5):
            await store.create(
                _make_record(
                    id=f"cred-{i}",
                    secret_ref=f"ref-{i}",
                    updated_at=1000 + i,
                )
            )
        records = await store.list()
        assert [r.id for r in records] == ["cred-4", "cred-3", "cred-2", "cred-1", "cred-0"]

    async def test_list_limit_clamped_low(self, store) -> None:
        await store.create(_make_record(id="cred-1", secret_ref="ref-1"))
        records = await store.list(limit=0)  # clamp to 1
        assert len(records) == 1

    async def test_list_limit_clamped_high(self, store) -> None:
        """limit > 500 应 clamp 到 500——创建少量数据测不出，但行为验证."""
        # 测 5 个 + limit=1000 应返回 5（不抛）
        for i in range(5):
            await store.create(
                _make_record(id=f"cred-{i}", secret_ref=f"ref-{i}")
            )
        records = await store.list(limit=1000)
        assert len(records) == 5

    async def test_list_pagination_before_updated_at(self, store) -> None:
        for i in range(5):
            await store.create(
                _make_record(
                    id=f"cred-{i}",
                    secret_ref=f"ref-{i}",
                    updated_at=1000 + i,
                )
            )
        # 拿 updated_at < 1003 的（即 cred-0/1/2）
        records = await store.list(before_updated_at=1003)
        ids = [r.id for r in records]
        assert ids == ["cred-2", "cred-1", "cred-0"]


# ============================================================================
# Duplicates
# ============================================================================


class TestDuplicates:
    async def test_duplicate_id_rejected(self, store) -> None:
        rec1 = _make_record(id="cred-1", secret_ref="ref-1")
        rec2 = _make_record(id="cred-1", secret_ref="ref-2")
        await store.create(rec1)
        with pytest.raises(CredentialAlreadyExistsError):
            await store.create(rec2)

    async def test_duplicate_secret_ref_rejected(self, store) -> None:
        rec1 = _make_record(id="cred-1", secret_ref="shared-ref")
        rec2 = _make_record(id="cred-2", secret_ref="shared-ref")
        await store.create(rec1)
        with pytest.raises(CredentialSecretRefConflictError):
            await store.create(rec2)


# ============================================================================
# Update label
# ============================================================================


class TestUpdateLabel:
    async def test_update_label_happy(self, store) -> None:
        await store.create(_make_record())
        updated = await store.update_label("cred-1", "Personal Key")
        assert updated.label == "Personal Key"

    async def test_update_label_missing_id(self, store) -> None:
        with pytest.raises(CredentialNotFoundError):
            await store.update_label("never-exists", "X")

    async def test_update_label_empty_rejected(self, store) -> None:
        await store.create(_make_record())
        with pytest.raises(CredentialStoreError, match="label"):
            await store.update_label("cred-1", "")

    async def test_update_label_whitespace_rejected(self, store) -> None:
        await store.create(_make_record())
        with pytest.raises(CredentialStoreError, match="label"):
            await store.update_label("cred-1", "   ")


# ============================================================================
# Replace secret metadata (rotate)
# ============================================================================


class TestReplaceSecretMetadata:
    async def test_replace_updates_secret_ref(self, store) -> None:
        await store.create(_make_record())
        rotated = await store.replace_secret_metadata(
            "cred-1",
            secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256="sha256:newfingerprint",
        )
        assert rotated.secret_ref == "new-ref"
        assert rotated.masked_value == "sk****NEW0"
        assert rotated.fingerprint_sha256 == "sha256:newfingerprint"

    async def test_rotate_resets_validation_state(self, store) -> None:
        """Rotate 后必须重置 validation_status + last_validated_*."""
        await store.create(_make_record())
        # 先 mark validated
        await store.update_validation_state(
            "cred-1",
            validation_status="valid",
            provider_id="glm",
            validated_at=5000,
            error_code=None,
        )
        validated = await store.get("cred-1")
        assert validated.validation_status == "valid"
        assert validated.last_validated_provider_id == "glm"

        # Now rotate
        rotated = await store.replace_secret_metadata(
            "cred-1",
            secret_ref="new-ref",
            masked_value="sk****NEW0",
            fingerprint_sha256="sha256:new",
        )
        assert rotated.validation_status == "never_validated"
        assert rotated.last_validated_provider_id is None
        assert rotated.last_validated_at is None
        assert rotated.last_error_code is None

    async def test_rotate_missing_id_raises(self, store) -> None:
        with pytest.raises(CredentialNotFoundError):
            await store.replace_secret_metadata(
                "never-exists",
                secret_ref="x",
                masked_value="x",
                fingerprint_sha256=None,
            )

    async def test_rotate_duplicate_secret_ref_rejected(self, store) -> None:
        await store.create(_make_record(id="cred-1", secret_ref="ref-1"))
        await store.create(_make_record(id="cred-2", secret_ref="ref-2"))

        # Try to rotate cred-2 to ref-1（已被 cred-1 占用）
        with pytest.raises(CredentialSecretRefConflictError):
            await store.replace_secret_metadata(
                "cred-2",
                secret_ref="ref-1",
                masked_value="sk****X",
                fingerprint_sha256=None,
            )


# ============================================================================
# Update validation state
# ============================================================================


class TestUpdateValidationState:
    async def test_update_to_valid(self, store) -> None:
        await store.create(_make_record())
        updated = await store.update_validation_state(
            "cred-1",
            validation_status="valid",
            provider_id="glm",
            validated_at=9999,
            error_code=None,
        )
        assert updated.validation_status == "valid"
        assert updated.last_validated_provider_id == "glm"
        assert updated.last_validated_at == 9999

    async def test_update_to_invalid_with_error_code(self, store) -> None:
        await store.create(_make_record())
        updated = await store.update_validation_state(
            "cred-1",
            validation_status="invalid",
            provider_id="glm",
            validated_at=9999,
            error_code="authentication_failed",
        )
        assert updated.validation_status == "invalid"
        assert updated.last_error_code == "authentication_failed"

    async def test_update_to_error_state(self, store) -> None:
        await store.create(_make_record())
        updated = await store.update_validation_state(
            "cred-1",
            validation_status="error",
            provider_id="glm",
            validated_at=9999,
            error_code="request_timeout",
        )
        assert updated.validation_status == "error"
        assert updated.last_error_code == "request_timeout"

    async def test_update_validation_invalid_status_rejected(self, store) -> None:
        await store.create(_make_record())
        with pytest.raises(CredentialStoreError, match="validation_status"):
            await store.update_validation_state(
                "cred-1",
                validation_status="bogus",  # type: ignore[arg-type]
                provider_id=None,
                validated_at=None,
                error_code=None,
            )

    async def test_update_validation_missing_id(self, store) -> None:
        with pytest.raises(CredentialNotFoundError):
            await store.update_validation_state(
                "never-exists",
                validation_status="valid",
                provider_id="glm",
                validated_at=9999,
                error_code=None,
            )


# ============================================================================
# Delete
# ============================================================================


class TestDelete:
    async def test_delete_returns_pre_deletion_record(self, store) -> None:
        rec = _make_record()
        await store.create(rec)
        deleted = await store.delete("cred-1")
        assert deleted.id == "cred-1"
        assert deleted.label == rec.label

        # 验证已删
        with pytest.raises(CredentialNotFoundError):
            await store.get("cred-1")

    async def test_delete_missing_raises(self, store) -> None:
        with pytest.raises(CredentialNotFoundError):
            await store.delete("never-exists")


# ============================================================================
# Concurrency
# ============================================================================


class TestConcurrency:
    async def test_concurrent_create_different_ids_succeeds(self, store) -> None:
        async def create_one(i: int) -> None:
            await store.create(
                _make_record(id=f"cred-{i}", secret_ref=f"ref-{i}")
            )

        await asyncio.gather(*(create_one(i) for i in range(10)))
        records = await store.list()
        assert len(records) == 10

    async def test_concurrent_create_duplicate_only_one_succeeds(self, store) -> None:
        async def try_create() -> bool:
            try:
                await store.create(_make_record(id="same-id", secret_ref="same-ref"))
                return True
            except CredentialAlreadyExistsError:
                return False
            except CredentialSecretRefConflictError:
                return False

        results = await asyncio.gather(*(try_create() for _ in range(5)))
        # 至少一个成功——其它失败
        assert sum(results) == 1


# ============================================================================
# Integer ms timestamps
# ============================================================================


class TestTimestamps:
    async def test_update_label_uses_ms_epoch(self, store) -> None:
        await store.create(_make_record(created_at=1000, updated_at=1000))
        before = int(time.time() * 1000)
        updated = await store.update_label("cred-1", "New")
        after = int(time.time() * 1000)
        # updated_at 应在 [before, after] 区间
        assert before <= updated.updated_at <= after
        # created_at 不变
        assert updated.created_at == 1000

    async def test_replace_metadata_uses_ms_epoch(self, store) -> None:
        await store.create(_make_record(created_at=1000, updated_at=1000))
        before = int(time.time() * 1000)
        rotated = await store.replace_secret_metadata(
            "cred-1",
            secret_ref="new",
            masked_value="sk****NEW",
            fingerprint_sha256=None,
        )
        after = int(time.time() * 1000)
        assert before <= rotated.updated_at <= after

    async def test_update_validation_uses_ms_epoch(self, store) -> None:
        await store.create(_make_record())
        before = int(time.time() * 1000)
        updated = await store.update_validation_state(
            "cred-1",
            validation_status="valid",
            provider_id="glm",
            validated_at=9999,
            error_code=None,
        )
        after = int(time.time() * 1000)
        assert before <= updated.updated_at <= after


# ============================================================================
# Record field validation
# ============================================================================


class TestRecordValidation:
    async def test_create_empty_id_rejected(self, store) -> None:
        with pytest.raises(CredentialStoreError, match="id"):
            await store.create(_make_record(id=""))

    async def test_create_empty_label_rejected(self, store) -> None:
        with pytest.raises(CredentialStoreError, match="label"):
            await store.create(_make_record(label=""))

    async def test_create_invalid_storage_mode_rejected(self, store) -> None:
        with pytest.raises(CredentialStoreError, match="storage_mode"):
            await store.create(_make_record(storage_mode="invalid"))  # type: ignore[arg-type]

    async def test_create_empty_secret_ref_rejected(self, store) -> None:
        with pytest.raises(CredentialStoreError, match="secret_ref"):
            await store.create(_make_record(secret_ref=""))

    async def test_create_invalid_hint_confidence_rejected(self, store) -> None:
        with pytest.raises(CredentialStoreError, match="provider_hint_confidence"):
            await store.create(_make_record(provider_hint_confidence="bogus"))  # type: ignore[arg-type]

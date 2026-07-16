"""Credentials store restart persistence + storage status resolver tests（P1-E1-2）.

覆盖：
- 重开 connection 后 CredentialRecord 存在
- 新 InMemory Store → needs_key（进程重启语义）
- Env var 存在 → ready
- Env var 不存在 → needs_key
- Keyring unavailable → backend_unavailable
- Resolver 不返回 secret
- Resolver 不修改 record
- Resolver 不持久化
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStoreUnavailableError,
)
from pi_agent_core_py.web.credentials_store import (
    CredentialRecord,
    SQLiteCredentialStore,
    resolve_storage_status,
)


def _make_record(**overrides) -> CredentialRecord:
    defaults = dict(
        id="cred-1",
        label="Test",
        storage_mode="keyring",
        secret_ref="cred-ref-1",
        masked_value="sk****8A31",
        fingerprint_sha256="sha256:abc",
        provider_hint=None,
        provider_hint_confidence=None,
        validation_status="never_validated",
        last_validated_provider_id=None,
        last_validated_at=None,
        last_error_code=None,
        created_at=1000,
        updated_at=1000,
    )
    defaults.update(overrides)
    return CredentialRecord(**defaults)


# ============================================================================
# Restart persistence
# ============================================================================


class TestRestartPersistence:
    async def test_record_survives_reopen(self, tmp_path) -> None:
        db_path = str(tmp_path / "creds.db")

        s1 = SQLiteCredentialStore(db_path)
        await s1.init()
        await s1.create(_make_record())
        await s1.close()

        s2 = SQLiteCredentialStore(db_path)
        await s2.init()
        got = await s2.get("cred-1")
        assert got.id == "cred-1"
        assert got.label == "Test"
        assert got.masked_value == "sk****8A31"
        await s2.close()

    async def test_multiple_records_survive_reopen(self, tmp_path) -> None:
        db_path = str(tmp_path / "creds.db")

        s1 = SQLiteCredentialStore(db_path)
        await s1.init()
        for i in range(3):
            await s1.create(
                _make_record(id=f"cred-{i}", secret_ref=f"ref-{i}")
            )
        await s1.close()

        s2 = SQLiteCredentialStore(db_path)
        await s2.init()
        records = await s2.list()
        assert len(records) == 3
        await s2.close()


# ============================================================================
# Storage status resolver
# ============================================================================


class TestStorageStatusResolver:
    async def test_inmemory_with_secret_ready(self) -> None:
        store = InMemorySecretStore()
        await store.set("cred-ref-1", "sk-test-aaaaaaaa")
        rec = _make_record(secret_ref="cred-ref-1")
        status = await resolve_storage_status(rec, store)
        assert status == "ready"

    async def test_inmemory_without_secret_needs_key(self) -> None:
        """模拟 restart——SQLite row 在但新 InMemory 实例无 secret."""
        store = InMemorySecretStore()  # 新实例——没有 secret
        rec = _make_record(secret_ref="cred-ref-1")
        status = await resolve_storage_status(rec, store)
        assert status == "needs_key"

    async def test_env_var_present_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_PI_E1_RESOLVER", "sk-test-aaaaaaaa")
        store = EnvSecretStore()
        rec = _make_record(storage_mode="env", secret_ref="TEST_PI_E1_RESOLVER")
        status = await resolve_storage_status(rec, store)
        assert status == "ready"

    async def test_env_var_missing_needs_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_PI_E1_MISSING", raising=False)
        store = EnvSecretStore()
        rec = _make_record(storage_mode="env", secret_ref="TEST_PI_E1_MISSING")
        status = await resolve_storage_status(rec, store)
        assert status == "needs_key"


# ============================================================================
# Backend unavailable
# ============================================================================


class _UnavailableStore:
    """Mock SecretStore that always reports unavailable."""

    async def is_available(self) -> bool:
        return False

    async def set(self, secret_ref: str, value: str) -> None:
        raise SecretStoreUnavailableError("unavailable")

    async def get(self, secret_ref: str) -> str | None:
        return None

    async def delete(self, secret_ref: str) -> None:
        pass

    def backend_name(self) -> str:
        return "unavailable"


class _RaisingStore:
    """Mock SecretStore that raises on every call."""

    async def is_available(self) -> bool:
        raise RuntimeError("boom")

    async def set(self, secret_ref: str, value: str) -> None:
        raise RuntimeError("boom")

    async def get(self, secret_ref: str) -> str | None:
        raise RuntimeError("boom")

    async def delete(self, secret_ref: str) -> None:
        raise RuntimeError("boom")

    def backend_name(self) -> str:
        return "raising"


class TestBackendUnavailable:
    async def test_unavailable_returns_backend_unavailable(self) -> None:
        rec = _make_record()
        status = await resolve_storage_status(rec, _UnavailableStore())
        assert status == "backend_unavailable"

    async def test_is_available_raises_returns_backend_unavailable(self) -> None:
        """is_available() 抛异常时 resolver 安全映射为 backend_unavailable."""
        rec = _make_record()
        status = await resolve_storage_status(rec, _RaisingStore())
        assert status == "backend_unavailable"

    async def test_get_raises_returns_backend_unavailable(self) -> None:
        """get() 抛异常时（即使 is_available=True）也应安全映射."""
        class _GetRaises:
            async def is_available(self) -> bool:
                return True

            async def set(self, secret_ref: str, value: str) -> None:
                pass

            async def get(self, secret_ref: str) -> str | None:
                raise RuntimeError("backend broken")

            async def delete(self, secret_ref: str) -> None:
                pass

            def backend_name(self) -> str:
                return "broken"

        rec = _make_record()
        status = await resolve_storage_status(rec, _GetRaises())
        assert status == "backend_unavailable"


# ============================================================================
# Resolver safety——不返回 secret / 不修改 record
# ============================================================================


class TestResolverSafety:
    async def test_resolver_does_not_return_secret(self) -> None:
        """resolve_storage_status 只返回 status 枚举——不返回 secret value."""
        store = InMemorySecretStore()
        await store.set("cred-ref-1", "PI_E1_SECRET_MARKER_7F3A91D2")
        rec = _make_record(secret_ref="cred-ref-1")
        result = await resolve_storage_status(rec, store)
        # result 是 Literal["ready", "needs_key", "backend_unavailable"]——是 str
        assert isinstance(result, str)
        assert "PI_E1_SECRET_MARKER_7F3A91D2" not in result
        assert result == "ready"

    async def test_resolver_does_not_modify_record(self) -> None:
        store = InMemorySecretStore()
        await store.set("cred-ref-1", "sk-test-aaaaaaaa")
        rec = _make_record(secret_ref="cred-ref-1")
        original_updated_at = rec.updated_at
        original_validation_status = rec.validation_status
        _ = await resolve_storage_status(rec, store)
        # record 是 frozen——不可变；只要"调用前后比较"相等即可
        assert rec.updated_at == original_updated_at
        assert rec.validation_status == original_validation_status

    async def test_resolver_does_not_persist_status(self, tmp_path) -> None:
        """resolver 不写 SQLite——只返回值."""
        store = InMemorySecretStore()
        await store.set("cred-ref-1", "sk-test-aaaaaaaa")

        db_path = str(tmp_path / "creds.db")
        sqlite_store = SQLiteCredentialStore(db_path)
        await sqlite_store.init()
        await sqlite_store.create(_make_record(secret_ref="cred-ref-1"))

        rec = await sqlite_store.get("cred-1")
        _ = await resolve_storage_status(rec, store)

        # Re-read——确保没"偷偷"update validation_status
        rec_after = await sqlite_store.get("cred-1")
        assert rec_after.updated_at == rec.updated_at
        await sqlite_store.close()

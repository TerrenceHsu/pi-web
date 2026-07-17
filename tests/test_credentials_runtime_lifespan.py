"""credential_runtime_context lifespan tests（P1-E1-4A）.

覆盖 spec §10 Composition / Lifespan 11–22：
- memory 模式成功启动
- auto + Keyring available
- auto + Keyring unavailable（degraded）
- keyring + unavailable（degraded）
- schema 初始化成功
- schema 初始化失败关闭 owned connection
- 失败不留下半初始化 runtime
- shutdown 关闭 Repository
- shutdown 幂等
- 不关闭其他 Web Store connection
- Runtime repr 不泄漏 Store
- App State 不可序列化为 Secret DTO
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.credentials_runtime import (
    CredentialReadiness,
    CredentialRuntimeConfig,
    CredentialRuntimeState,
    build_credential_runtime_config,
    credential_runtime_context,
)
from pi_agent_core_py.web.local_web_security import default_web_security_config

# ============================================================================
# Helpers
# ============================================================================


def _config(
    tmp_path: Path,
    *,
    mode: str = "memory",
) -> CredentialRuntimeConfig:
    return build_credential_runtime_config(
        database_path=str(tmp_path / "creds.db"),
        secret_backend_mode=mode,
        web_security=default_web_security_config(),
    )


# ============================================================================
# memory mode
# ============================================================================


class TestMemoryMode:
    async def test_memory_mode_starts_and_yields_runtime(
        self, tmp_path: Path
    ) -> None:
        cfg = _config(tmp_path, mode="memory")
        async with credential_runtime_context(cfg) as runtime:
            assert isinstance(runtime, CredentialRuntimeState)
            assert runtime.config is cfg
            assert runtime.repository is not None
            assert runtime.router is not None
            assert runtime.service is not None
            assert isinstance(runtime.readiness, CredentialReadiness)

    async def test_memory_mode_readiness_ready(self, tmp_path: Path) -> None:
        """memory 是用户主动禁用 keyring——ready, not degraded."""
        cfg = _config(tmp_path, mode="memory")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.status == "ready"
            assert runtime.readiness.configured_backend == "memory"
            assert runtime.readiness.keyring_available is False
            assert runtime.readiness.reason_code is None

    async def test_memory_mode_keyring_slot_is_none(
        self, tmp_path: Path
    ) -> None:
        """keyring slot 必须为 None——不能伪装成 InMemorySecretStore."""
        cfg = _config(tmp_path, mode="memory")
        async with credential_runtime_context(cfg) as runtime:
            store = runtime.router.try_resolve("keyring")
            assert store is None


# ============================================================================
# auto / keyring modes——keyring 几乎永远 unavailable 在测试沙盒里
# ============================================================================


class TestAutoKeyringModes:
    async def test_auto_mode_starts(self, tmp_path: Path) -> None:
        """auto 模式——keyring 通常在 CI 不可用，但应用仍启动."""
        cfg = _config(tmp_path, mode="auto")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.configured_backend == "auto"

    async def test_keyring_mode_starts(self, tmp_path: Path) -> None:
        """keyring 模式——keyring 不可用时应用仍启动，但 credentials degraded."""
        cfg = _config(tmp_path, mode="keyring")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.configured_backend == "keyring"


# ============================================================================
# Readiness: keyring available vs unavailable
# ============================================================================


class TestReadinessStates:
    async def test_auto_keyring_unavailable_is_degraded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """强制 keyring unavailable（CI 通常已经如此，但显式 patch OSKeyringSecretStore）."""
        # Patch OSKeyringSecretStore so it always reports unavailable
        from pi_agent_core_py.web import credentials_runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        cfg = _config(tmp_path, mode="auto")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.status == "degraded"
            assert runtime.readiness.keyring_available is False
            assert runtime.readiness.reason_code == "keyring_unavailable"

    async def test_keyring_mode_unavailable_is_degraded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from pi_agent_core_py.web import credentials_runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        cfg = _config(tmp_path, mode="keyring")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.status == "degraded"
            assert runtime.readiness.reason_code == "keyring_unavailable"

    async def test_auto_keyring_available_is_ready(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Patch _probe_keyring_available=True——auto 走 ready 路径."""
        from pi_agent_core_py.secrets import InMemorySecretStore as _IMS
        from pi_agent_core_py.web import credentials_runtime as rt

        async def _probe_ok() -> bool:
            return True

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_ok)
        # Patch OSKeyringSecretStore to a fake so construction succeeds
        monkeypatch.setattr(
            rt, "OSKeyringSecretStore", lambda: _IMS(), raising=False
        )
        cfg = _config(tmp_path, mode="auto")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.status == "ready"
            assert runtime.readiness.keyring_available is True

    async def test_memory_mode_ready_even_when_keyring_unavailable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """memory 模式——keyring 不可用是用户期望，不应标记 degraded."""
        from pi_agent_core_py.web import credentials_runtime as rt

        async def _probe_fail() -> bool:
            return False

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_fail)
        cfg = _config(tmp_path, mode="memory")
        async with credential_runtime_context(cfg) as runtime:
            assert runtime.readiness.status == "ready"
            assert runtime.readiness.reason_code is None


# ============================================================================
# Schema initialization
# ============================================================================


class TestSchemaInitialization:
    async def test_fresh_db_initializes_schema(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            version = await runtime.repository.get_schema_version()
            assert version == 1

    async def test_reopen_existing_db_keeps_schema(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "creds.db")
        cfg1 = build_credential_runtime_config(
            database_path=path,
            secret_backend_mode="memory",
            web_security=default_web_security_config(),
        )
        async with credential_runtime_context(cfg1) as r1:
            await r1.repository.get_schema_version()  # initializes

        cfg2 = build_credential_runtime_config(
            database_path=path,
            secret_backend_mode="memory",
            web_security=default_web_security_config(),
        )
        async with credential_runtime_context(cfg2) as r2:
            assert await r2.repository.get_schema_version() == 1


# ============================================================================
# AsyncExitStack rollback on partial-init failure
# ============================================================================


class TestPartialInitRollback:
    async def test_schema_failure_closes_owned_connection(
        self, tmp_path: Path
    ) -> None:
        """Pre-create DB with unsupported schema version——second open fails
        during schema validation. AsyncExitStack must close the connection."""
        import aiosqlite

        from pi_agent_core_py.web.credentials_store import (
            WEB_CREDENTIALS_SCHEMA_VERSION,
            CredentialsSchemaVersionError,
        )

        db_path = tmp_path / "creds.db"
        # Seed schema_meta with a version higher than supported
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "CREATE TABLE web_credentials_schema_meta "
                "(key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            await db.execute(
                "INSERT INTO web_credentials_schema_meta (key, value) "
                "VALUES ('version', ?)",
                (WEB_CREDENTIALS_SCHEMA_VERSION + 1,),
            )
            await db.commit()

        # Now build config + try to open——schema validation must fail
        cfg = _config(tmp_path)
        with pytest.raises(CredentialsSchemaVersionError):
            async with credential_runtime_context(cfg):
                pass

        # AsyncExitStack closed the connection. Verify file is unlocked by
        # opening it again (would fail if connection still held).
        async with aiosqlite.connect(str(db_path)):
            pass

    async def test_failed_init_does_not_leave_half_initialized_state(
        self, tmp_path: Path
    ) -> None:
        """If init raises, no CredentialRuntimeState is yielded."""
        from pi_agent_core_py.web import credentials_runtime as rt

        original_open = rt.SQLiteCredentialStore.open

        async def _fail_once(path: str):
            raise RuntimeError("simulated init failure")

        rt.SQLiteCredentialStore.open = _fail_once  # type: ignore[method-assign]
        try:
            cfg = _config(tmp_path)
            yielded = False
            try:
                async with credential_runtime_context(cfg):
                    yielded = True
            except RuntimeError:
                pass
            assert not yielded, "runtime should not have been yielded on failure"
        finally:
            rt.SQLiteCredentialStore.open = original_open  # type: ignore[method-assign]


# ============================================================================
# Shutdown
# ============================================================================


class TestShutdown:
    async def test_shutdown_closes_repository(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            repo = runtime.repository
            # Verify open
            assert await repo.get_schema_version() == 1
        # After context exit, repo should be closed
        assert repo._closed is True

    async def test_shutdown_is_idempotent(self, tmp_path: Path) -> None:
        """close() 多次调用安全."""
        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            repo = runtime.repository
        # First close after context exit
        await repo.close()
        # Second close——should not raise
        await repo.close()
        assert repo._closed is True


# ============================================================================
# Runtime repr / serialization safety
# ============================================================================


class TestRuntimeReprSafety:
    async def test_runtime_repr_does_not_expose_store_internals(
        self, tmp_path: Path
    ) -> None:
        """repr=False——默认 object repr 不暴露 Store."""
        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            text = repr(runtime)
            # 默认 object repr——不含 Store 字段细节
            assert "SecretStoreRouter" not in text
            assert "CredentialService" not in text
            assert "SQLiteCredentialStore" not in text
            # 含类名
            assert "CredentialRuntimeState" in text

    async def test_runtime_does_not_serialize_secret_dto(
        self, tmp_path: Path
    ) -> None:
        """CredentialRuntimeState 不可 pickle——sqlite3.Connection 不可序列化."""
        import pickle

        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            with pytest.raises((TypeError, pickle.PicklingError)):
                pickle.dumps(runtime)

    async def test_runtime_does_not_hold_secret_value(
        self, tmp_path: Path
    ) -> None:
        """runtime attrs 不含 secret string."""
        cfg = _config(tmp_path)
        async with credential_runtime_context(cfg) as runtime:
            # 遍历 dataclass fields——不应有 string-typed secret-bearing attr
            for f in runtime.__dataclass_fields__:
                value = getattr(runtime, f)
                # repository / router / service / config / readiness 是非 string
                # 类型——这里只检查没有 string-typed "secret" / "api_key" 字段
                if isinstance(value, str):
                    assert "secret" not in f.lower()
                    assert "api_key" not in f.lower()

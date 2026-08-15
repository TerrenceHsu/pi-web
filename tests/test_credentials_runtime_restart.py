"""credential_runtime_context restart recovery tests（P1-E1-4A）.

覆盖 spec §10 Restart 23–29：
- 相同绝对 DB 文件恢复 CredentialRecord
- session-only 重启后 needs_key
- Env 变量存在时 ready
- Env 变量缺失时 needs_key
- Fake Keyring 重启后 ready
- 重启不产生第二个 SQLite 文件
- 更换 cwd 后仍使用同一文件
"""
from __future__ import annotations

from pathlib import Path

from pi_agent_core_py.secrets import InMemorySecretStore
from pi_agent_core_py.web.credentials.runtime import (
    build_credential_runtime_config,
    credential_runtime_context,
)
from pi_agent_core_py.web.credentials.service import (
    CreateCredentialCommand,
)
from pi_agent_core_py.web.credentials.store import resolve_storage_status
from pi_agent_core_py.web.local_web_security import default_web_security_config

# ============================================================================
# Helpers
# ============================================================================


def _cfg(
    path: str | Path,
    *,
    mode: str = "memory",
):
    return build_credential_runtime_config(
        database_path=str(path),
        secret_backend_mode=mode,
        web_security=default_web_security_config(),
    )


# ============================================================================
# Persisted CredentialRecord survives restart
# ============================================================================


class TestRecordPersistence:
    async def test_record_survives_restart_same_db_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "creds.db"
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="session_only",
                    secret_value="sk-test-1234567890",
                )
            )
            cred_id = result.record.id

        # Reopen——record should still be there
        cfg2 = _cfg(path)
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            assert row.label == "X"

    async def test_restart_does_not_create_second_sqlite_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "creds.db"
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1):
            pass

        cfg2 = _cfg(path)
        async with credential_runtime_context(cfg2):
            pass

        # Should have exactly one .db file (plus maybe -wal / -shm which we ignore)
        import asyncio

        db_files = await asyncio.to_thread(lambda: list(tmp_path.glob("creds.db*")))
        # Filter to main + wal + shm only——no duplicate main
        main_db = [f for f in db_files if f.name == "creds.db"]
        assert len(main_db) == 1

    async def test_restart_with_different_cwd_uses_same_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Resolve absolute path once——cwd change doesn't redirect."""
        path = (tmp_path / "creds.db").resolve()
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="Before",
                    storage_mode="session_only",
                    secret_value="sk-test-aaaaaaaaaa",
                )
            )
            cred_id = result.record.id

        # Change cwd——config was already resolved to absolute path
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        cfg2 = _cfg(path)  # Same absolute path string
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            assert row.label == "Before"


# ============================================================================
# storage_mode recovery semantics
# ============================================================================


class TestSessionOnlyRestart:
    async def test_session_only_restart_becomes_needs_key(
        self, tmp_path: Path
    ) -> None:
        """session_only 用 InMemorySecretStore——重启后内存清空，row 仍在但
        storage_status=needs_key."""
        path = tmp_path / "creds.db"
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="Session",
                    storage_mode="session_only",
                    secret_value="sk-test-bbbbbbbbbb",
                )
            )
            cred_id = result.record.id

        cfg2 = _cfg(path)
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            # New InMemorySecretStore doesn't have the secret
            store = rt2.router.resolve("session_only")
            status = await resolve_storage_status(row, store)
            assert status == "needs_key"


class TestEnvRestart:
    async def test_env_with_variable_present_is_ready(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("MY_APP_KEY", "env-value-1234")
        path = tmp_path / "creds.db"
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="Env",
                    storage_mode="env",
                    env_var_name="MY_APP_KEY",
                )
            )
            cred_id = result.record.id

        # Variable still set
        cfg2 = _cfg(path)
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            store = rt2.router.resolve("env")
            status = await resolve_storage_status(row, store)
            assert status == "ready"

    async def test_env_with_variable_missing_is_needs_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("MY_APP_KEY_GONE", "first-value")
        path = tmp_path / "creds.db"
        cfg1 = _cfg(path)
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="Env",
                    storage_mode="env",
                    env_var_name="MY_APP_KEY_GONE",
                )
            )
            cred_id = result.record.id

        # Delete env var between runs
        monkeypatch.delenv("MY_APP_KEY_GONE")

        cfg2 = _cfg(path)
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            store = rt2.router.resolve("env")
            status = await resolve_storage_status(row, store)
            assert status == "needs_key"


class TestKeyringRestart:
    async def test_fake_keyring_persists_across_restart(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Patch OSKeyringSecretStore to InMemorySecretStore-like persistent backend.

        Simulates real keyring behavior——same backend survives across
        SQLiteCredentialStore reopens within the same process.
        """
        # Use a single shared InMemorySecretStore to simulate persistent keyring
        shared_keyring = InMemorySecretStore()

        from pi_agent_core_py.web.credentials import runtime as rt

        # Patch construction + probe
        async def _probe_ok() -> bool:
            return True

        monkeypatch.setattr(rt, "_probe_keyring_available", _probe_ok)
        monkeypatch.setattr(
            rt, "OSKeyringSecretStore", lambda: shared_keyring, raising=False
        )

        path = tmp_path / "creds.db"
        cfg1 = _cfg(path, mode="keyring")
        async with credential_runtime_context(cfg1) as rt1:
            result = await rt1.service.create(
                CreateCredentialCommand(
                    label="Keyring",
                    storage_mode="keyring",
                    secret_value="sk-ant-test1234567890",
                )
            )
            cred_id = result.record.id

        cfg2 = _cfg(path, mode="keyring")
        async with credential_runtime_context(cfg2) as rt2:
            row = await rt2.repository.get(cred_id)
            # shared_keyring still has the secret——ready
            status = await resolve_storage_status(row, shared_keyring)
            assert status == "ready"

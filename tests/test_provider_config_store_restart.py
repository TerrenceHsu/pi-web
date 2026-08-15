"""Restart persistence tests for SQLiteProviderConfigStore (E2-1).

覆盖 6 项重启行为（spec §16.5）：
52. close + reopen → Profile recovered
53. Binding recovered
54. default Profile recovered
55. FK enforcement still ON after restart
56. uses same absolute DB file
57. does not produce second SQLite file
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from pi_agent_core_py.web.provider_config_store import (
    ProviderProfileNotFoundError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Helpers
# ============================================================================


async def _seed_state(db_path: Path) -> None:
    """Seed: 1 default Profile + 1 non-default Profile + 1 Binding."""
    s = await SQLiteProviderConfigStore.open(db_path)
    try:
        await s.create_profile(
            profile_id="profile-def",
            name="Default",
            provider_id="anthropic",
            credential_id="cred-def",
            default_model="claude-X",
            enabled=True,
            is_default=True,
        )
        await s.create_profile(
            profile_id="profile-alt",
            name="Alt",
            provider_id="glm",
            credential_id="cred-alt",
            default_model="glm-4",
            enabled=True,
            is_default=False,
        )
        await s.upsert_binding(
            session_id="sess-1",
            profile_id="profile-def",
            model_id="claude-X",
            source="default",
        )
    finally:
        await s.close()


# ============================================================================
# 52. close + reopen → Profile recovered
# ============================================================================


async def test_52_profile_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    await _seed_state(db)

    s = await SQLiteProviderConfigStore.open(db)
    try:
        p = await s.get_profile("profile-def")
        assert p.name == "Default"
        assert p.provider_id == "anthropic"
        assert p.is_default is True

        p_alt = await s.get_profile("profile-alt")
        assert p_alt.name == "Alt"
    finally:
        await s.close()


# ============================================================================
# 53. Binding recovered
# ============================================================================


async def test_53_binding_persists_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    await _seed_state(db)

    s = await SQLiteProviderConfigStore.open(db)
    try:
        b = await s.get_binding("sess-1")
        assert b is not None
        assert b.profile_id == "profile-def"
        assert b.model_id == "claude-X"
        assert b.source == "default"
    finally:
        await s.close()


# ============================================================================
# 54. default Profile recovered
# ============================================================================


async def test_54_default_profile_persists_across_restart(
    tmp_path: Path,
) -> None:
    db = tmp_path / "restart.db"
    await _seed_state(db)

    s = await SQLiteProviderConfigStore.open(db)
    try:
        default = await s.get_default_profile()
        assert default is not None
        assert default.id == "profile-def"
    finally:
        await s.close()


# ============================================================================
# 55. FK enforcement still ON after restart
# ============================================================================


async def test_55_fk_enforcement_on_after_restart(tmp_path: Path) -> None:
    db = tmp_path / "fk_restart.db"
    await _seed_state(db)

    s = await SQLiteProviderConfigStore.open(db)
    try:
        # Verify PRAGMA reports ON
        async with s._require_db().execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
        assert int(row[0]) == 1

        # Verify FK fires: binding to non-existent profile fails
        with pytest.raises(ProviderProfileNotFoundError):
            await s.upsert_binding(
                session_id="sess-fk",
                profile_id="profile-nonexistent",
                model_id="model-X",
                source="explicit",
            )
    finally:
        await s.close()


# ============================================================================
# 56. uses same absolute DB file (no second SQLite file)
# ============================================================================


async def test_56_uses_same_absolute_db_file(tmp_path: Path) -> None:
    """Both open() calls hit the same DB file (verified by data persistence)."""
    db = tmp_path / "same.db"
    abs_path = str(db.resolve())

    s1 = await SQLiteProviderConfigStore.open(abs_path)
    try:
        await s1.create_profile(
            profile_id="profile-same",
            name="Same",
            provider_id="anthropic",
            credential_id="cred-same",
            default_model="claude-X",
        )
    finally:
        await s1.close()

    # Different cwd, same absolute path → same data
    import os
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # Verify absolute path still resolves to the same file
        assert os.path.exists(abs_path)  # noqa: ASYNC240
        s2 = await SQLiteProviderConfigStore.open(abs_path)
        try:
            p = await s2.get_profile("profile-same")
            assert p.name == "Same"
        finally:
            await s2.close()
    finally:
        os.chdir(original_cwd)


# ============================================================================
# 57. does not produce second SQLite file
# ============================================================================


async def test_57_no_second_sqlite_file(tmp_path: Path) -> None:
    """Opening store at path X.db must not create X-copy.db or other extras.

    Provider Config Store must reuse the same file as Session / Extension /
    Credentials stores (when pointed at the same path).
    """
    db = tmp_path / "shared.db"

    # Open store 1
    s1 = await SQLiteProviderConfigStore.open(db)
    try:
        await s1.create_profile(
            profile_id="profile-shared",
            name="Shared",
            provider_id="anthropic",
            credential_id="cred-shared",
            default_model="claude-X",
        )
    finally:
        await s1.close()

    # List files in tmp_path—only shared.db (and possibly shared.db-wal/shm)
    import os
    files_before = sorted(os.listdir(tmp_path))

    # Open store 2 at SAME path
    s2 = await SQLiteProviderConfigStore.open(db)
    try:
        p = await s2.get_profile("profile-shared")
        assert p.name == "Shared"
    finally:
        await s2.close()

    files_after = sorted(os.listdir(tmp_path))
    # No extra .db files created
    db_files_before = [f for f in files_before if f.endswith(".db")]
    db_files_after = [f for f in files_after if f.endswith(".db")]
    assert db_files_after == db_files_before == ["shared.db"]


# ============================================================================
# Additional: shared DB file with credentials store (same file, separate schemas)
# ============================================================================


async def test_provider_config_shares_db_file_with_credentials_store(
    tmp_path: Path,
) -> None:
    """Provider Config and Credentials can coexist in same DB file without collision."""
    db = tmp_path / "coexist.db"

    # Open credentials store first
    from pi_agent_core_py.web.credentials.store import (
        SQLiteCredentialStore,
    )
    cred_store = await SQLiteCredentialStore.open(str(db))
    try:
        # Now open provider config store at SAME file
        pc_store = await SQLiteProviderConfigStore.open(db)
        try:
            # Both schemas coexist
            conn = await aiosqlite.connect(str(db))
            conn.row_factory = aiosqlite.Row
            try:
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "ORDER BY name"
                ) as cursor:
                    rows = await cursor.fetchall()
                table_names = {r["name"] for r in rows}
                # Both stores' tables present
                assert "web_credentials" in table_names
                assert "web_credentials_schema_meta" in table_names
                assert "web_provider_profiles" in table_names
                assert "web_session_model_bindings" in table_names
                assert "web_provider_config_schema_meta" in table_names
            finally:
                await conn.close()
        finally:
            await pc_store.close()
    finally:
        await cred_store.close()

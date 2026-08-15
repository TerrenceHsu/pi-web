"""Concurrency tests for SQLiteProviderConfigStore (E2-1).

覆盖 6 项并发行为（spec §16.4）：
46. 两个 connection 同时创建 default Profile，最终只有一个 default
47. 并发切换 default 不违反 unique index
48. Binding insert 与 Profile delete 竞争
49. 事务失败回滚
50. 一个 Store rollback 不影响另一个连接已提交数据
51. 关闭一个 Store 不关闭其他 connection
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi_agent_core_py.web.providers.config_store import (
    ProviderProfileNotFoundError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


async def _open_store(
    db_path: Path,
    *,
    base_clock: int = 10000,
) -> SQLiteProviderConfigStore:
    counter = {"n": base_clock}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    return await SQLiteProviderConfigStore.open(db_path, now_ms=fake_now)


# ============================================================================
# 46. two connections create default Profile simultaneously → only one default
# ============================================================================


async def test_46_concurrent_create_default_yields_single_default(
    tmp_path: Path,
) -> None:
    """Two stores race to create default profiles—BEGIN IMMEDIATE serializes,
    and UPDATE-before-INSERT pattern ensures only one ends up as default.
    """
    db = tmp_path / "race.db"
    s1 = await _open_store(db, base_clock=10000)
    try:
        # Seed one enabled non-default profile so default switching is meaningful
        await s1.create_profile(
            profile_id="profile-pre",
            name="Pre",
            provider_id="anthropic",
            credential_id="cred-pre",
            default_model="claude-X",
        )
    finally:
        await s1.close()

    s1 = await _open_store(db, base_clock=20000)
    s2 = await _open_store(db, base_clock=30000)
    try:
        # Race two create_profile with is_default=True
        await asyncio.gather(
            s1.create_profile(
                profile_id="profile-a",
                name="A",
                provider_id="anthropic",
                credential_id="cred-a",
                default_model="claude-A",
                is_default=True,
            ),
            s2.create_profile(
                profile_id="profile-b",
                name="B",
                provider_id="glm",
                credential_id="cred-b",
                default_model="glm-4",
                is_default=True,
            ),
        )

        listed = await s1.list_profiles()
        defaults = [p for p in listed if p.is_default]
        assert len(defaults) == 1, (
            f"expected exactly 1 default; got {[d.id for d in defaults]}"
        )
    finally:
        await s1.close()
        await s2.close()


# ============================================================================
# 47. concurrent default switch does not violate unique index
# ============================================================================


async def test_47_concurrent_default_switch_safe(tmp_path: Path) -> None:
    """Concurrent is_default=True updates across two profiles never create two defaults."""
    db = tmp_path / "switch.db"
    s1 = await _open_store(db, base_clock=10000)
    try:
        await s1.create_profile(
            profile_id="profile-x",
            name="X",
            provider_id="anthropic",
            credential_id="cred-x",
            default_model="claude-X",
        )
        await s1.create_profile(
            profile_id="profile-y",
            name="Y",
            provider_id="glm",
            credential_id="cred-y",
            default_model="glm-4",
        )
    finally:
        await s1.close()

    s1 = await _open_store(db, base_clock=20000)
    s2 = await _open_store(db, base_clock=30000)
    try:
        # Both stores concurrently try to set their target as default
        await asyncio.gather(
            s1.update_profile("profile-x", is_default=True),
            s2.update_profile("profile-y", is_default=True),
        )

        listed = await s1.list_profiles()
        defaults = [p for p in listed if p.is_default]
        assert len(defaults) == 1
    finally:
        await s1.close()
        await s2.close()


# ============================================================================
# 48. Binding insert vs Profile delete race
# ============================================================================


async def test_48_binding_insert_vs_profile_delete_race(
    tmp_path: Path,
) -> None:
    """Concurrent binding insert + profile delete never produces orphan binding.

    Two possible outcomes (both safe):
        - Profile deletes first → Binding insert fails on FK RESTRICT
        - Binding inserts first → Profile delete raises ProviderProfileInUseError
    """
    db = tmp_path / "race2.db"

    # Seed profile via a short-lived store
    s_seed = await _open_store(db, base_clock=10000)
    try:
        await s_seed.create_profile(
            profile_id="profile-race",
            name="Race",
            provider_id="anthropic",
            credential_id="cred-race",
            default_model="claude-X",
        )
    finally:
        await s_seed.close()

    s1 = await _open_store(db, base_clock=20000)
    s2 = await _open_store(db, base_clock=30000)
    try:
        # Try both operations concurrently
        results = await asyncio.gather(
            s1.delete_profile("profile-race"),
            s2.upsert_binding(
                session_id="sess-race",
                profile_id="profile-race",
                model_id="model-X",
                source="explicit",
            ),
            return_exceptions=True,
        )

        # At least one must have raised—no orphan binding possible
        delete_result, binding_result = results
        exceptions = [
            r for r in results if isinstance(r, BaseException)
        ]
        assert len(exceptions) >= 1, (
            "expected at least one operation to fail under race; "
            "both apparently succeeded—this means an orphan binding may exist"
        )

        # Verify no orphan binding: if profile exists, binding may exist;
        # if profile gone, binding must NOT exist.
        try:
            await s1.get_profile("profile-race")
            profile_exists = True
        except ProviderProfileNotFoundError:
            profile_exists = False

        binding = await s1.get_binding("sess-race")
        if not profile_exists:
            assert binding is None, (
                "orphan binding exists after profile was deleted"
            )
    finally:
        await s1.close()
        await s2.close()


# ============================================================================
# 49. transaction failure rolls back
# ============================================================================


async def test_49_transaction_failure_rolls_back(tmp_path: Path) -> None:
    """If a write transaction fails mid-way, all changes are rolled back."""
    db = tmp_path / "rollback.db"
    s = await _open_store(db, base_clock=10000)
    try:
        await s.create_profile(
            profile_id="profile-keep",
            name="Keep",
            provider_id="anthropic",
            credential_id="cred-keep",
            default_model="claude-X",
        )

        # Force a failure mid-update: invalid name (control char)
        from pi_agent_core_py.web.providers.config_store import (
            ProviderConfigStoreError,
        )
        with pytest.raises(ProviderConfigStoreError):
            await s.update_profile(
                "profile-keep",
                name="bad\nname",
            )

        # Original name preserved
        p = await s.get_profile("profile-keep")
        assert p.name == "Keep"
    finally:
        await s.close()


# ============================================================================
# 50. one Store rollback does not affect another connection's committed data
# ============================================================================


async def test_50_rollback_does_not_affect_other_store(tmp_path: Path) -> None:
    """Rollback in store A does not undo store B's committed data."""
    db = tmp_path / "isolate.db"
    s1 = await _open_store(db, base_clock=10000)
    s2 = await _open_store(db, base_clock=20000)
    try:
        # s2 commits a profile
        await s2.create_profile(
            profile_id="profile-from-s2",
            name="FromS2",
            provider_id="anthropic",
            credential_id="cred-s2",
            default_model="claude-X",
        )

        # s1 attempts an operation that fails
        from pi_agent_core_py.web.providers.config_store import (
            ProviderConfigStoreError,
        )
        with pytest.raises(ProviderConfigStoreError):
            await s1.create_profile(
                profile_id="",  # invalid empty id
                name="Bad",
                provider_id="anthropic",
                credential_id="cred-x",
                default_model="claude-X",
            )

        # s2's data still intact
        p = await s1.get_profile("profile-from-s2")
        assert p.name == "FromS2"
    finally:
        await s1.close()
        await s2.close()


# ============================================================================
# 51. closing one Store does not close other connection
# ============================================================================


async def test_51_closing_one_store_does_not_close_other(
    tmp_path: Path,
) -> None:
    """Closing store A's connection does not invalidate store B."""
    db = tmp_path / "two.db"
    s1 = await _open_store(db, base_clock=10000)
    s2 = await _open_store(db, base_clock=20000)
    try:
        await s1.create_profile(
            profile_id="profile-persist",
            name="Persist",
            provider_id="anthropic",
            credential_id="cred-p",
            default_model="claude-X",
        )
    finally:
        # Close s1 only
        await s1.close()

    # s2 should still work
    try:
        listed = await s2.list_profiles()
        assert any(p.id == "profile-persist" for p in listed)

        # s2 can still write
        await s2.create_profile(
            profile_id="profile-after",
            name="After",
            provider_id="glm",
            credential_id="cred-a",
            default_model="glm-4",
        )
    finally:
        await s2.close()

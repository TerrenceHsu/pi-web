"""ProviderProfile CRUD tests for SQLiteProviderConfigStore (E2-1).

覆盖 17 项 Profile 行为（spec §16.2）：
16. create / get / list
17. duplicate ID rejected
18. provider_id immutable
19. update name
20. update credential_id
21. update default_model
22. enable / disable
23. create default Profile
24. switch default Profile
25. at most one default
26. can have no default
27. disable default auto-clears is_default
28. disabled + default combination rejected
29. delete unused Profile
30. delete non-existent Profile
31. list order stable
32. monotonic time on update
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.providers.config_store import (
    ProviderProfile,
    ProviderProfileAlreadyExistsError,
    ProviderProfileInUseError,
    ProviderProfileNotFoundError,
    ProviderProfileStateError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteProviderConfigStore:
    """Fresh store at tmp_path/profile.db with auto-incrementing fake clock."""
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    s = await SQLiteProviderConfigStore.open(
        tmp_path / "profile.db",
        now_ms=fake_now,
    )
    try:
        yield s
    finally:
        await s.close()


async def _make_profile(
    store: SQLiteProviderConfigStore,
    *,
    pid: str = "profile-A",
    name: str = "Profile A",
    provider_id: str = "anthropic",
    credential_id: str = "cred-A",
    default_model: str = "claude-A",
    enabled: bool = True,
    is_default: bool = False,
) -> ProviderProfile:
    return await store.create_profile(
        profile_id=pid,
        name=name,
        provider_id=provider_id,
        credential_id=credential_id,
        default_model=default_model,
        enabled=enabled,
        is_default=is_default,
    )


# ============================================================================
# 16. create / get / list
# ============================================================================


async def test_16_create_get_list(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-1", name="One")
    await _make_profile(store, pid="profile-2", name="Two")

    # get
    got = await store.get_profile("profile-1")
    assert got.id == "profile-1"
    assert got.name == "One"
    assert got.provider_id == "anthropic"
    assert got.credential_id == "cred-A"
    assert got.default_model == "claude-A"
    assert got.enabled is True
    assert got.is_default is False
    assert got.created_at == got.updated_at

    # list
    listed = await store.list_profiles()
    assert len(listed) == 2
    assert {p.id for p in listed} == {"profile-1", "profile-2"}


# ============================================================================
# 17. duplicate ID
# ============================================================================


async def test_17_duplicate_id_rejected(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-dup")
    with pytest.raises(ProviderProfileAlreadyExistsError):
        await _make_profile(store, pid="profile-dup")


# ============================================================================
# 18. provider_id immutable
# ============================================================================


async def test_18_provider_id_immutable(store: SQLiteProviderConfigStore) -> None:
    """update_profile does not accept provider_id—so it is naturally immutable."""
    await _make_profile(store, pid="profile-imm", provider_id="anthropic")
    # update_profile signature has no provider_id kwarg
    import inspect
    sig = inspect.signature(store.update_profile)
    assert "provider_id" not in sig.parameters

    # Update other field—provider_id preserved
    updated = await store.update_profile("profile-imm", name="Renamed")
    assert updated.provider_id == "anthropic"
    assert updated.name == "Renamed"


# ============================================================================
# 19-21. update mutable fields
# ============================================================================


async def test_19_update_name(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-n", name="Old")
    updated = await store.update_profile("profile-n", name="New Name")
    assert updated.name == "New Name"


async def test_20_update_credential_id(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-c", credential_id="cred-old")
    updated = await store.update_profile("profile-c", credential_id="cred-new")
    assert updated.credential_id == "cred-new"


async def test_21_update_default_model(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-m", default_model="model-old")
    updated = await store.update_profile("profile-m", default_model="model-new")
    assert updated.default_model == "model-new"


# ============================================================================
# 22. enable / disable
# ============================================================================


async def test_22_enable_disable(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-en", enabled=True)
    disabled = await store.update_profile("profile-en", enabled=False)
    assert disabled.enabled is False
    assert disabled.is_default is False

    re_enabled = await store.update_profile("profile-en", enabled=True)
    assert re_enabled.enabled is True


# ============================================================================
# 23. create default Profile
# ============================================================================


async def test_23_create_default_profile(store: SQLiteProviderConfigStore) -> None:
    p = await _make_profile(
        store, pid="profile-d1", is_default=True, enabled=True
    )
    assert p.is_default is True

    default = await store.get_default_profile()
    assert default is not None
    assert default.id == "profile-d1"


# ============================================================================
# 24. switch default Profile
# ============================================================================


async def test_24_switch_default_profile(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(
        store, pid="profile-s1", is_default=True
    )
    await _make_profile(
        store, pid="profile-s2", is_default=False
    )

    # Make p2 the new default
    updated_p2 = await store.update_profile("profile-s2", is_default=True)
    assert updated_p2.is_default is True

    # p1 should no longer be default
    refreshed_p1 = await store.get_profile("profile-s1")
    assert refreshed_p1.is_default is False

    # Only one default
    default = await store.get_default_profile()
    assert default is not None
    assert default.id == "profile-s2"


# ============================================================================
# 25. at most one default
# ============================================================================


async def test_25_at_most_one_default(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-o1", is_default=True)
    # Creating another default must clear the first
    await _make_profile(store, pid="profile-o2", is_default=True)

    listed = await store.list_profiles()
    defaults = [p for p in listed if p.is_default]
    assert len(defaults) == 1
    assert defaults[0].id == "profile-o2"


# ============================================================================
# 26. can have no default
# ============================================================================


async def test_26_can_have_no_default(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-nd1", is_default=False)
    await _make_profile(store, pid="profile-nd2", is_default=False)

    default = await store.get_default_profile()
    assert default is None


# ============================================================================
# 27. disable default auto-clears is_default
# ============================================================================


async def test_27_disable_default_auto_clears(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-ac", is_default=True, enabled=True)
    # Disable without explicitly passing is_default
    disabled = await store.update_profile("profile-ac", enabled=False)
    assert disabled.enabled is False
    assert disabled.is_default is False

    # No default anymore—Store did not auto-pick another
    assert await store.get_default_profile() is None


# ============================================================================
# 28. disabled + default combination rejected
# ============================================================================


async def test_28a_create_disabled_default_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderProfileStateError):
        await store.create_profile(
            profile_id="profile-bad",
            name="Bad",
            provider_id="anthropic",
            credential_id="cred-x",
            default_model="model-x",
            enabled=False,
            is_default=True,
        )


async def test_28b_update_set_default_on_disabled_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    await _make_profile(store, pid="profile-pre", enabled=False)

    with pytest.raises(ProviderProfileStateError):
        await store.update_profile("profile-pre", is_default=True)


async def test_28c_explicit_disabled_default_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    await _make_profile(store, pid="profile-x", enabled=True)
    with pytest.raises(ProviderProfileStateError):
        await store.update_profile(
            "profile-x", enabled=False, is_default=True
        )


# ============================================================================
# 29. delete unused Profile
# ============================================================================


async def test_29_delete_unused_profile(store: SQLiteProviderConfigStore) -> None:
    await _make_profile(store, pid="profile-del")
    pre = await store.delete_profile("profile-del")
    assert pre.id == "profile-del"

    with pytest.raises(ProviderProfileNotFoundError):
        await store.get_profile("profile-del")


# ============================================================================
# 30. delete non-existent Profile
# ============================================================================


async def test_30_delete_nonexistent_profile(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderProfileNotFoundError):
        await store.delete_profile("profile-nonexistent")


# ============================================================================
# 31. list order stable
# ============================================================================


async def test_31_list_order_stable(store: SQLiteProviderConfigStore) -> None:
    """list_profiles orders by updated_at DESC, then id ASC for stability."""
    await _make_profile(store, pid="profile-z", name="Z")
    await _make_profile(store, pid="profile-a", name="A")
    await _make_profile(store, pid="profile-m", name="M")

    listed = await store.list_profiles()
    # All created in order with monotonically increasing timestamps via fixture
    # → newest first: profile-m, profile-a, profile-z
    assert [p.id for p in listed] == ["profile-m", "profile-a", "profile-z"]

    # Touch profile-z to make it newest
    await store.update_profile("profile-z", name="Z2")
    listed2 = await store.list_profiles()
    assert listed2[0].id == "profile-z"


# ============================================================================
# 32. monotonic time on update
# ============================================================================


async def test_32_monotonic_time_on_update(
    tmp_path: Path,
) -> None:
    """Even with a clock that returns the same value, updated_at must increase."""
    fixed = {"value": 5000}

    def stuck_clock() -> int:
        return fixed["value"]

    s = await SQLiteProviderConfigStore.open(
        tmp_path / "mono.db",
        now_ms=stuck_clock,
    )
    try:
        p = await s.create_profile(
            profile_id="profile-mono",
            name="Mono",
            provider_id="anthropic",
            credential_id="cred-mono",
            default_model="model-mono",
        )
        assert p.created_at == 5000
        assert p.updated_at == 5000

        u1 = await s.update_profile("profile-mono", name="M2")
        assert u1.created_at == 5000  # preserved
        assert u1.updated_at == 5001  # max(now, old+1)

        u2 = await s.update_profile("profile-mono", name="M3")
        assert u2.updated_at == 5002
    finally:
        await s.close()


# ============================================================================
# Additional: Profile in-use protection (delete blocked by binding)
# ============================================================================


async def test_delete_profile_with_binding_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    await _make_profile(store, pid="profile-inuse")
    await store.upsert_binding(
        session_id="sess-1",
        profile_id="profile-inuse",
        model_id="model-x",
        source="explicit",
    )
    with pytest.raises(ProviderProfileInUseError):
        await store.delete_profile("profile-inuse")

    # Profile still exists
    still_there = await store.get_profile("profile-inuse")
    assert still_there.id == "profile-inuse"


# ============================================================================
# Additional: input validation rejects control chars
# ============================================================================


async def test_create_profile_rejects_control_chars_in_name(
    store: SQLiteProviderConfigStore,
) -> None:
    from pi_agent_core_py.web.providers.config_store import (
        ProviderConfigStoreError,
    )
    with pytest.raises(ProviderConfigStoreError):
        await store.create_profile(
            profile_id="profile-ctrl",
            name="bad\nname",
            provider_id="anthropic",
            credential_id="cred-x",
            default_model="model-x",
        )


async def test_create_profile_rejects_control_chars_in_model_id(
    store: SQLiteProviderConfigStore,
) -> None:
    from pi_agent_core_py.web.providers.config_store import (
        ProviderConfigStoreError,
    )
    with pytest.raises(ProviderConfigStoreError):
        await store.create_profile(
            profile_id="profile-ctrl2",
            name="OK",
            provider_id="anthropic",
            credential_id="cred-x",
            default_model="model\x00bad",
        )

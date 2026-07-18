"""SessionModelBinding CRUD tests for SQLiteProviderConfigStore (E2-1).

覆盖 13 项 Binding 行为（spec §16.3）：
33. create binding
34. get binding
35. update binding
36. update preserves created_at
37. source=default
38. source=explicit
39. invalid source rejected
40. invalid model_id rejected
41. delete binding
42. delete non-existent binding returns None
43. Profile not exist → FK reject
44. list bindings by profile
45. one Session at most one Binding
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.provider_config_store import (
    ProviderConfigStoreError,
    ProviderProfileNotFoundError,
    SessionModelBinding,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteProviderConfigStore:
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    s = await SQLiteProviderConfigStore.open(
        tmp_path / "bindings.db",
        now_ms=fake_now,
    )
    try:
        # Seed a profile so bindings can reference it
        await s.create_profile(
            profile_id="profile-seed",
            name="Seed",
            provider_id="anthropic",
            credential_id="cred-seed",
            default_model="claude-X",
        )
        yield s
    finally:
        await s.close()


# ============================================================================
# 33. create binding
# ============================================================================


async def test_33_create_binding(store: SQLiteProviderConfigStore) -> None:
    b = await store.upsert_binding(
        session_id="sess-create",
        profile_id="profile-seed",
        model_id="model-A",
        source="explicit",
    )
    assert isinstance(b, SessionModelBinding)
    assert b.session_id == "sess-create"
    assert b.profile_id == "profile-seed"
    assert b.model_id == "model-A"
    assert b.source == "explicit"
    assert b.created_at == b.updated_at


# ============================================================================
# 34. get binding
# ============================================================================


async def test_34_get_binding(store: SQLiteProviderConfigStore) -> None:
    await store.upsert_binding(
        session_id="sess-get",
        profile_id="profile-seed",
        model_id="model-A",
        source="default",
    )
    got = await store.get_binding("sess-get")
    assert got is not None
    assert got.session_id == "sess-get"

    # Non-existent session returns None
    miss = await store.get_binding("sess-nonexistent")
    assert miss is None


# ============================================================================
# 35. update binding
# ============================================================================


async def test_35_update_binding(store: SQLiteProviderConfigStore) -> None:
    await store.upsert_binding(
        session_id="sess-up",
        profile_id="profile-seed",
        model_id="model-old",
        source="default",
    )
    updated = await store.upsert_binding(
        session_id="sess-up",
        profile_id="profile-seed",
        model_id="model-new",
        source="explicit",
    )
    assert updated.model_id == "model-new"
    assert updated.source == "explicit"


# ============================================================================
# 36. update preserves created_at
# ============================================================================


async def test_36_update_preserves_created_at(
    store: SQLiteProviderConfigStore,
) -> None:
    initial = await store.upsert_binding(
        session_id="sess-ca",
        profile_id="profile-seed",
        model_id="model-A",
        source="default",
    )
    updated = await store.upsert_binding(
        session_id="sess-ca",
        profile_id="profile-seed",
        model_id="model-B",
        source="explicit",
    )
    assert updated.created_at == initial.created_at
    assert updated.updated_at >= initial.updated_at


# ============================================================================
# 37-38. source=default / explicit
# ============================================================================


async def test_37_source_default(store: SQLiteProviderConfigStore) -> None:
    b = await store.upsert_binding(
        session_id="sess-d",
        profile_id="profile-seed",
        model_id="model-X",
        source="default",
    )
    assert b.source == "default"


async def test_38_source_explicit(store: SQLiteProviderConfigStore) -> None:
    b = await store.upsert_binding(
        session_id="sess-e",
        profile_id="profile-seed",
        model_id="model-X",
        source="explicit",
    )
    assert b.source == "explicit"


# ============================================================================
# 39. invalid source rejected
# ============================================================================


async def test_39_invalid_source_rejected(store: SQLiteProviderConfigStore) -> None:
    with pytest.raises(ProviderConfigStoreError):
        await store.upsert_binding(
            session_id="sess-bad",
            profile_id="profile-seed",
            model_id="model-X",
            source="auto",  # type: ignore[arg-type]
        )


# ============================================================================
# 40. invalid model_id rejected
# ============================================================================


async def test_40a_invalid_model_id_empty_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderConfigStoreError):
        await store.upsert_binding(
            session_id="sess-m1",
            profile_id="profile-seed",
            model_id="",
            source="explicit",
        )


async def test_40b_invalid_model_id_control_char_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderConfigStoreError):
        await store.upsert_binding(
            session_id="sess-m2",
            profile_id="profile-seed",
            model_id="model\ninject",
            source="explicit",
        )


async def test_40c_invalid_model_id_too_long_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderConfigStoreError):
        await store.upsert_binding(
            session_id="sess-m3",
            profile_id="profile-seed",
            model_id="x" * 257,
            source="explicit",
        )


# ============================================================================
# 41. delete binding
# ============================================================================


async def test_41_delete_binding(store: SQLiteProviderConfigStore) -> None:
    await store.upsert_binding(
        session_id="sess-del",
        profile_id="profile-seed",
        model_id="model-A",
        source="explicit",
    )
    pre = await store.delete_binding("sess-del")
    assert pre is not None
    assert pre.session_id == "sess-del"

    miss = await store.get_binding("sess-del")
    assert miss is None


# ============================================================================
# 42. delete non-existent binding returns None
# ============================================================================


async def test_42_delete_nonexistent_binding_returns_none(
    store: SQLiteProviderConfigStore,
) -> None:
    pre = await store.delete_binding("sess-never")
    assert pre is None


# ============================================================================
# 43. Profile not exist → FK reject
# ============================================================================


async def test_43_binding_to_nonexistent_profile_rejected(
    store: SQLiteProviderConfigStore,
) -> None:
    with pytest.raises(ProviderProfileNotFoundError):
        await store.upsert_binding(
            session_id="sess-no-profile",
            profile_id="profile-nonexistent",
            model_id="model-X",
            source="explicit",
        )


# ============================================================================
# 44. list bindings by profile
# ============================================================================


async def test_44_list_bindings_for_profile(
    store: SQLiteProviderConfigStore,
) -> None:
    # Seed a second profile
    await store.create_profile(
        profile_id="profile-other",
        name="Other",
        provider_id="glm",
        credential_id="cred-other",
        default_model="glm-4",
    )
    await store.upsert_binding(
        session_id="sess-1",
        profile_id="profile-seed",
        model_id="model-A",
        source="default",
    )
    await store.upsert_binding(
        session_id="sess-2",
        profile_id="profile-seed",
        model_id="model-B",
        source="explicit",
    )
    await store.upsert_binding(
        session_id="sess-3",
        profile_id="profile-other",
        model_id="model-C",
        source="explicit",
    )

    seed_bindings = await store.list_bindings_for_profile("profile-seed")
    assert len(seed_bindings) == 2
    assert {b.session_id for b in seed_bindings} == {"sess-1", "sess-2"}

    other_bindings = await store.list_bindings_for_profile("profile-other")
    assert len(other_bindings) == 1
    assert other_bindings[0].session_id == "sess-3"

    # Non-existent profile → empty tuple (no error)
    none_bindings = await store.list_bindings_for_profile("profile-none")
    assert none_bindings == ()


# ============================================================================
# 45. one Session at most one Binding
# ============================================================================


async def test_45_one_session_at_most_one_binding(
    store: SQLiteProviderConfigStore,
) -> None:
    """Upsert on existing session_id replaces; never creates second row."""
    await store.upsert_binding(
        session_id="sess-uniq",
        profile_id="profile-seed",
        model_id="model-A",
        source="default",
    )
    await store.upsert_binding(
        session_id="sess-uniq",
        profile_id="profile-seed",
        model_id="model-B",
        source="explicit",
    )

    # Verify only one row exists in DB
    db = store._require_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM web_session_model_bindings "
        "WHERE session_id = ?",
        ("sess-uniq",),
    ) as cursor:
        row = await cursor.fetchone()
    assert int(row["cnt"]) == 1

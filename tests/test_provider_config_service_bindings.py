"""ProviderConfigService Session binding tests (E2-2).

覆盖 spec §12.4 Binding 矩阵（12 项）.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.providers.model_options import InvalidModelIdError
from pi_agent_core_py.web.providers.config_service import (
    ProviderConfigService,
    ProviderProfileDisabledError,
    SessionNotFoundError,
)
from pi_agent_core_py.web.providers.config_store import (
    ProviderProfileNotFoundError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fixtures (reuse fakes pattern)
# ============================================================================


# Import the fake helpers from the profiles test module
from tests.test_provider_config_service_profiles import (  # noqa: E402
    FakeCredentialService,
    FakeProviderRegistry,
    _make_credential_view,
)


@pytest.fixture
async def service(tmp_path: Path) -> tuple[ProviderConfigService, FakeCredentialService, set[str]]:
    """Build Service + return (svc, fake_cred, existing_sessions) for tests."""
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    store = await SQLiteProviderConfigStore.open(
        tmp_path / "bind.db", now_ms=fake_now
    )
    registry = FakeProviderRegistry()
    cred = FakeCredentialService()
    cred.seed(_make_credential_view(cid="cred-ready", storage_status="ready"))
    cred.seed(_make_credential_view(cid="cred-needs-key", storage_status="needs_key"))
    cred.seed(_make_credential_view(cid="cred-deleted-later", storage_status="ready"))

    existing_sessions: set[str] = {"sess-known-1", "sess-known-2"}

    async def session_exists(session_id: str) -> bool:
        return session_id in existing_sessions

    svc = ProviderConfigService(
        store=store,
        provider_registry=registry,
        credential_service=cred,
        session_exists=session_exists,
    )
    try:
        yield svc, cred, existing_sessions
    finally:
        await store.close()


# ============================================================================
# 1-3. Validation rejections
# ============================================================================


async def test_1_session_not_exist_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    # Create a profile first
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    with pytest.raises(SessionNotFoundError):
        await svc.set_session_binding(
            session_id="sess-does-not-exist",
            profile_id=p.id,
            model_id="claude-X",
        )


async def test_2_profile_not_exist_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    with pytest.raises(ProviderProfileNotFoundError):
        await svc.set_session_binding(
            session_id="sess-known-1",
            profile_id="profile-nonexistent",
            model_id="claude-X",
        )


async def test_3_disabled_profile_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
        enabled=False,
    )
    with pytest.raises(ProviderProfileDisabledError):
        await svc.set_session_binding(
            session_id="sess-known-1",
            profile_id=p.id,
            model_id="claude-X",
        )


# ============================================================================
# 4. Ready Profile can be bound
# ============================================================================


async def test_4_ready_profile_can_be_bound(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    b = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-X",
    )
    assert b.session_id == "sess-known-1"
    assert b.profile_id == p.id
    assert b.source == "explicit"


# ============================================================================
# 5-6. Soft-deleted / needs_key Profile can still save Binding
# ============================================================================


async def test_5_needs_key_profile_can_save_binding(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    """Profile status=needs_key still allows Binding save—E3 will reject execution."""
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-needs-key",
        default_model="claude-X",
    )
    # Verify status is needs_key
    view = await svc.get_profile(p.id)
    assert view.status == "needs_key"

    # Binding save succeeds
    b = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-X",
    )
    assert b.profile_id == p.id


async def test_6_needs_credential_profile_can_save_binding(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    """Profile status=needs_credential (Credential removed) still allows Binding."""
    svc, cred, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-deleted-later",
        default_model="claude-X",
    )
    # Simulate independent deletion
    cred.remove("cred-deleted-later")

    view = await svc.get_profile(p.id)
    assert view.status == "needs_credential"

    # Binding save still succeeds
    b = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-X",
    )
    assert b.profile_id == p.id


# ============================================================================
# 7. Source is fixed to "explicit"
# ============================================================================


async def test_7_source_fixed_to_explicit(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    b = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-X",
    )
    assert b.source == "explicit"


# ============================================================================
# 8. Update preserves created_at
# ============================================================================


async def test_8_update_preserves_created_at(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    initial = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-old",
    )
    updated = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-new",
    )
    assert updated.created_at == initial.created_at
    assert updated.updated_at >= initial.updated_at


# ============================================================================
# 9. Sessions A/B can bind different profiles
# ============================================================================


async def test_9_sessions_can_bind_different_profiles(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p_anthropic = await svc.create_profile(
        name="A",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    p_glm = await svc.create_profile(
        name="G",
        provider_id="glm",
        credential_id="cred-ready",
        default_model="glm-4.5-flash",
    )
    b1 = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p_anthropic.id,
        model_id="claude-X",
    )
    b2 = await svc.set_session_binding(
        session_id="sess-known-2",
        profile_id=p_glm.id,
        model_id="glm-4.5-flash",
    )
    assert b1.profile_id == p_anthropic.id
    assert b2.profile_id == p_glm.id
    assert b1.session_id != b2.session_id


# ============================================================================
# 10-11. model_id validation
# ============================================================================


async def test_10_invalid_model_id_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    with pytest.raises(InvalidModelIdError):
        await svc.set_session_binding(
            session_id="sess-known-1",
            profile_id=p.id,
            model_id="bad\ninject",
        )


async def test_11_manual_model_id_outside_static_list_allowed(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    """model_id not in static catalog is still accepted (just format-validated)."""
    svc, _, _ = service
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    # "claude-opus-4.7-my-private-fork" not in ANTHROPIC_MODEL_OPTIONS (empty)
    b = await svc.set_session_binding(
        session_id="sess-known-1",
        profile_id=p.id,
        model_id="claude-opus-4.7-my-private-fork",
    )
    assert b.model_id == "claude-opus-4.7-my-private-fork"


# ============================================================================
# 12-13. get_session_binding semantics
# ============================================================================


async def test_12_get_binding_returns_none_when_no_binding(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    result = await svc.get_session_binding("sess-known-1")
    assert result is None


async def test_13_get_binding_rejects_unknown_session(
    service: tuple[ProviderConfigService, FakeCredentialService, set[str]],
) -> None:
    svc, _, _ = service
    with pytest.raises(SessionNotFoundError):
        await svc.get_session_binding("sess-does-not-exist")

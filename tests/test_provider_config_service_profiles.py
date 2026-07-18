"""ProviderConfigService Profile CRUD tests (E2-2).

覆盖 spec §12.1 Profile 矩阵（14 项）.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_agent_core_py.providers.registry import ProviderDefinition, ProviderRegistry
from pi_agent_core_py.web.credentials_service import CredentialService, CredentialView
from pi_agent_core_py.web.credentials_store import CredentialNotFoundError
from pi_agent_core_py.web.model_options import InvalidModelIdError
from pi_agent_core_py.web.provider_config_service import (
    CredentialNotFoundForProfileError,
    InvalidProfileNameError,
    ProviderConfigService,
    UnknownProviderError,
)
from pi_agent_core_py.web.provider_config_store import (
    ProviderProfileInUseError,
    ProviderProfileNotFoundError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fakes
# ============================================================================


def _make_provider_def(pid: str, display: str) -> ProviderDefinition:
    """Construct ProviderDefinition with only fields needed by Service."""
    return ProviderDefinition(
        id=pid,
        display_name=display,
        api_style="anthropic",  # not used by Service
        default_base_url=f"https://{pid}.example.com",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
        key_prefix_hints=(),
    )


class FakeProviderRegistry(ProviderRegistry):
    """In-memory ProviderRegistry with anthropic + glm."""

    def __init__(self) -> None:
        self._defs: dict[str, ProviderDefinition] = {
            "anthropic": _make_provider_def("anthropic", "Anthropic"),
            "glm": _make_provider_def("glm", "GLM"),
        }

    def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
        return self._defs.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
        return tuple(self._defs.values())

    def has(self, provider_id: str) -> bool:  # type: ignore[override]
        return provider_id in self._defs


class FakeCredentialService(CredentialService):
    """In-memory CredentialService returning only safe CredentialView.

    Inherits CredentialService so isinstance() checks pass; we override
    every public method we need. ``get`` / ``list`` are SAFE (return views);
    write methods raise NotImplementedError to catch accidental mutation.
    """

    def __init__(self) -> None:
        self._views: dict[str, CredentialView] = {}

    def seed(self, view: CredentialView) -> None:
        self._views[view.id] = view

    def remove(self, credential_id: str) -> None:
        """Simulate independent deletion of a Credential."""
        self._views.pop(credential_id, None)

    async def get(self, credential_id: str) -> CredentialView:  # type: ignore[override]
        if credential_id not in self._views:
            raise CredentialNotFoundError(
                f"credential not found: {credential_id}"
            )
        return self._views[credential_id]

    async def list(  # type: ignore[override]
        self,
        *,
        limit: int = 100,
        before_updated_at: int | None = None,
    ) -> list[CredentialView]:
        views = list(self._views.values())
        if before_updated_at is not None:
            views = [v for v in views if v.updated_at < before_updated_at]
        return sorted(views, key=lambda v: v.updated_at, reverse=True)[:limit]

    # Write methods—E2-2 Service MUST NOT call these.
    async def create(self, command: Any) -> Any:
        raise NotImplementedError("Service must not call create()")

    async def update_label(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Service must not call update_label()")

    async def rotate(self, command: Any) -> Any:
        raise NotImplementedError("Service must not call rotate()")

    async def delete(self, credential_id: str) -> Any:
        raise NotImplementedError("Service must not call delete()")

    async def validate(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Service must not call validate()")


def _make_credential_view(
    *,
    cid: str = "cred-A",
    masked: str = "sk-****8A31",
    storage_status: str = "ready",
    validation_status: str = "never_validated",
    last_validated_provider_id: str | None = None,
) -> CredentialView:
    """Build a CredentialView without touching real CredentialRecord.

    CredentialView is a frozen dataclass; we construct via __init__.
    """
    return CredentialView(
        id=cid,
        label=f"Label-{cid}",
        storage_mode="keyring",
        masked_value=masked,
        provider_hint=None,
        provider_hint_confidence=None,
        validation_status=validation_status,  # type: ignore[arg-type]
        last_validated_provider_id=last_validated_provider_id,
        last_validated_at=None,
        last_error_code=None,
        created_at=1000,
        updated_at=1000,
        storage_status=storage_status,  # type: ignore[arg-type]
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def service(tmp_path: Path) -> tuple[ProviderConfigService, FakeCredentialService]:
    """Build a Service with fakes; return (service, fake_cred) for inspection."""
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    store = await SQLiteProviderConfigStore.open(
        tmp_path / "svc.db", now_ms=fake_now
    )
    registry = FakeProviderRegistry()
    cred = FakeCredentialService()
    # Seed one ready credential by default
    cred.seed(_make_credential_view(cid="cred-A"))

    async def session_exists(session_id: str) -> bool:
        # For profile tests, assume all sessions exist
        return True

    svc = ProviderConfigService(
        store=store,
        provider_registry=registry,
        credential_service=cred,
        session_exists=session_exists,
    )
    try:
        yield svc, cred
    finally:
        await store.close()


# ============================================================================
# 1-2. Create valid profiles
# ============================================================================


async def test_1_create_anthropic_profile(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    view = await svc.create_profile(
        name="Work Anthropic",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    assert view.provider_id == "anthropic"
    assert view.provider_display_name == "Anthropic"
    assert view.status == "ready"
    assert view.credential_masked_value == "sk-****8A31"


async def test_2_create_glm_profile(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    view = await svc.create_profile(
        name="GLM Work",
        provider_id="glm",
        credential_id="cred-A",
        default_model="glm-4.5-flash",
    )
    assert view.provider_id == "glm"
    assert view.provider_display_name == "GLM"


# ============================================================================
# 3. Unknown provider rejected
# ============================================================================


async def test_3_unknown_provider_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    with pytest.raises(UnknownProviderError):
        await svc.create_profile(
            name="Bad",
            provider_id="openai",  # not in fake registry
            credential_id="cred-A",
            default_model="gpt-4",
        )


# ============================================================================
# 4. Credential not found rejected
# ============================================================================


async def test_4_credential_not_found_rejected(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    with pytest.raises(CredentialNotFoundForProfileError):
        await svc.create_profile(
            name="Bad",
            provider_id="anthropic",
            credential_id="cred-nonexistent",
            default_model="claude-X",
        )


# ============================================================================
# 5. Credential never_validated allowed
# ============================================================================


async def test_5_credential_never_validated_allowed(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, cred = service
    cred.seed(
        _make_credential_view(
            cid="cred-nv",
            validation_status="never_validated",
        )
    )
    view = await svc.create_profile(
        name="NV",
        provider_id="anthropic",
        credential_id="cred-nv",
        default_model="claude-X",
    )
    assert view.status == "ready"  # never_validated does NOT block use


# ============================================================================
# 6. Credential provider_hint mismatch allowed
# ============================================================================


async def test_6_credential_provider_hint_mismatch_allowed(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    """provider_hint is just a heuristic—Service does NOT enforce it."""
    svc, cred = service
    # Seed credential with provider_hint=None (simulated mismatch)
    cred.seed(_make_credential_view(cid="cred-hint"))
    # Create a GLM profile pointing at it—allowed even if hint doesn't suggest GLM
    view = await svc.create_profile(
        name="Cross",
        provider_id="glm",
        credential_id="cred-hint",
        default_model="glm-4.5-flash",
    )
    assert view.provider_id == "glm"


# ============================================================================
# 7. One credential can be referenced by multiple profiles
# ============================================================================


async def test_7_one_credential_multiple_profiles(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    p1 = await svc.create_profile(
        name="A",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    p2 = await svc.create_profile(
        name="B",
        provider_id="glm",
        credential_id="cred-A",
        default_model="glm-4.5-flash",
    )
    assert p1.credential_id == p2.credential_id == "cred-A"


# ============================================================================
# 8. provider_id immutable
# ============================================================================


async def test_8_provider_id_immutable(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    import inspect
    sig = inspect.signature(svc.update_profile)
    assert "provider_id" not in sig.parameters


# ============================================================================
# 9. Update credential requires target exists
# ============================================================================


async def test_9_update_credential_must_exist(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    with pytest.raises(CredentialNotFoundForProfileError):
        await svc.update_profile(
            created.id,
            credential_id="cred-nonexistent",
        )


# ============================================================================
# 10. Update default_model
# ============================================================================


async def test_10_update_default_model(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-old",
    )
    updated = await svc.update_profile(created.id, default_model="claude-new")
    assert updated.default_model == "claude-new"


# ============================================================================
# 11. Disable default Profile (auto-clears is_default)
# ============================================================================


async def test_11_disable_default_profile_auto_clears(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
        is_default=True,
    )
    assert created.is_default is True
    disabled = await svc.update_profile(created.id, enabled=False)
    assert disabled.enabled is False
    assert disabled.is_default is False


# ============================================================================
# 12. Delete unused Profile
# ============================================================================


async def test_12_delete_unused_profile(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    await svc.delete_profile(created.id)
    with pytest.raises(ProviderProfileNotFoundError):
        await svc.get_profile(created.id)


# ============================================================================
# 13. Delete in-use Profile propagates error
# ============================================================================


async def test_13_delete_in_use_profile_propagates_error(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-1",
        profile_id=created.id,
        model_id="claude-X",
    )
    with pytest.raises(ProviderProfileInUseError):
        await svc.delete_profile(created.id)


# ============================================================================
# 14. list_profiles reflects needs_credential after Credential removed
# ============================================================================


async def test_14_list_profiles_shows_needs_credential_after_removal(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, cred = service
    created = await svc.create_profile(
        name="X",
        provider_id="anthropic",
        credential_id="cred-A",
        default_model="claude-X",
    )
    # Verify initial state
    listed = await svc.list_profiles()
    assert listed[0].status == "ready"

    # Simulate independent Credential deletion
    cred.remove("cred-A")

    listed2 = await svc.list_profiles()
    assert len(listed2) == 1
    assert listed2[0].id == created.id
    assert listed2[0].status == "needs_credential"
    assert listed2[0].credential_masked_value is None


# ============================================================================
# Additional: name validation
# ============================================================================


async def test_create_profile_rejects_invalid_name(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    with pytest.raises(InvalidProfileNameError):
        await svc.create_profile(
            name="bad\nname",
            provider_id="anthropic",
            credential_id="cred-A",
            default_model="claude-X",
        )


async def test_create_profile_rejects_invalid_model_id(
    service: tuple[ProviderConfigService, FakeCredentialService],
) -> None:
    svc, _ = service
    with pytest.raises(InvalidModelIdError):
        await svc.create_profile(
            name="OK",
            provider_id="anthropic",
            credential_id="cred-A",
            default_model="bad\nmodel",
        )

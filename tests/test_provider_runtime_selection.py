"""RequestProviderRuntime.resolve_selection tests（M1-4 §十四.Selection 14 项）.

覆盖：
- Session 不存在传播领域错误
- 无 Binding → None
- explicit Binding
- default Binding
- model_id 来自 Binding（不是 Profile.default_model）
- disabled Profile 拒绝
- Profile 不存在 → ProviderSelectionNotFoundError
- ProviderDefinition 不存在 → ProviderInitializationError
- credential_id 进入快照但不进 repr
- resolve 阶段 Secret read = 0
- resolve 阶段 Factory call = 0
- Profile 更新后既有 selection 不变（frozen 快照）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
)
from pi_agent_core_py.web.credentials.service import (
    CredentialService,
    CredentialView,
)
from pi_agent_core_py.web.credentials.store import CredentialNotFoundError
from pi_agent_core_py.web.providers.config_service import (
    ProviderConfigService,
    SessionNotFoundError,
)
from pi_agent_core_py.web.providers.config_store import SQLiteProviderConfigStore
from pi_agent_core_py.web.providers.runtime import (
    ProviderInitializationError,
    ProviderSelectionDisabledError,
    ProviderSelectionNotFoundError,
    RequestProviderRuntime,
    RequestProviderSelection,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fakes
# ============================================================================


def _make_provider_def(pid: str, display: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=pid,
        display_name=display,
        api_style="anthropic_compatible",
        default_base_url=f"https://{pid}.example.com",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
        key_prefix_hints=(),
    )


class FakeProviderRegistry(ProviderRegistry):
    def __init__(self) -> None:
        self._defs: dict[str, ProviderDefinition] = {
            "anthropic": _make_provider_def("anthropic", "Anthropic"),
            "glm": _make_provider_def("glm", "GLM"),
            "qwen": _make_provider_def("qwen", "Qwen"),
        }

    def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
        return self._defs.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
        return tuple(self._defs.values())

    def has(self, provider_id: str) -> bool:  # type: ignore[override]
        return provider_id in self._defs

    def remove(self, provider_id: str) -> None:
        """Test helper: simulate provider being removed from registry."""
        self._defs.pop(provider_id, None)


class FakeCredentialService(CredentialService):
    """Tracks resolve_secret_for_request calls——never reads a real backend."""

    def __init__(self) -> None:
        self._views: dict[str, CredentialView] = {}
        self.resolve_calls: list[str] = []
        self.resolve_secret: str = "fake-secret-from-fake-service"

    def seed(self, view: CredentialView) -> None:
        self._views[view.id] = view

    async def get(self, credential_id: str) -> CredentialView:  # type: ignore[override]
        if credential_id not in self._views:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return self._views[credential_id]

    async def list(  # type: ignore[override]
        self,
        *,
        limit: int = 100,
        before_updated_at: int | None = None,
    ) -> list[CredentialView]:
        return list(self._views.values())[:limit]

    async def resolve_secret_for_request(  # type: ignore[override]
        self,
        credential_id: str,
    ) -> str:
        self.resolve_calls.append(credential_id)
        if credential_id not in self._views:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return self.resolve_secret


def _make_credential_view(cid: str) -> CredentialView:
    return CredentialView(
        id=cid,
        label=f"label-{cid}",
        storage_mode="session_only",
        masked_value="sk-***-masked",
        provider_hint=None,
        provider_hint_confidence=None,
        validation_status="never_validated",
        last_validated_provider_id=None,
        last_validated_at=None,
        last_error_code=None,
        storage_status="ready",
        created_at=1000,
        updated_at=1000,
    )


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def env(tmp_path: Path) -> tuple[
    ProviderConfigService,
    FakeCredentialService,
    FakeProviderRegistry,
    RequestProviderRuntime,
    set[str],
]:
    """Build (svc, cred, registry, runtime, existing_sessions) tuple."""
    counter = {"n": 1000}

    def fake_now() -> int:
        counter["n"] += 1
        return counter["n"]

    store = await SQLiteProviderConfigStore.open(
        tmp_path / "runtime.db", now_ms=fake_now
    )
    registry = FakeProviderRegistry()
    cred = FakeCredentialService()
    cred.seed(_make_credential_view("cred-ready"))

    existing: set[str] = {"sess-bound", "sess-empty", "sess-missing"}

    async def session_exists(session_id: str) -> bool:
        return session_id in existing

    svc = ProviderConfigService(
        store=store,
        provider_registry=registry,
        credential_service=cred,
        session_exists=session_exists,
    )

    factory_calls: list[dict[str, Any]] = []

    def fake_factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> Any:
        factory_calls.append(
            {
                "provider_id": provider_definition.id,
                "model_id": model_id,
            }
        )
        raise AssertionError("Factory must not be called during resolve_selection")

    runtime = RequestProviderRuntime(
        provider_config_service=svc,
        credential_service=cred,
        provider_registry=registry,
        provider_factory=fake_factory,
    )

    try:
        yield svc, cred, registry, runtime, existing, factory_calls  # type: ignore[misc]
    finally:
        await store.close()


# ============================================================================
# 14-17: Session / Binding basic
# ============================================================================


async def test_14_session_not_found_propagates(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, _ = env
    with pytest.raises(SessionNotFoundError):
        await runtime.resolve_selection("sess-does-not-exist")


async def test_15_session_without_binding_returns_none(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, _ = env
    # Seed a profile but don't bind anything to sess-empty
    await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    # Make sess-empty the "existing session" without binding——already in existing
    selection = await runtime.resolve_selection("sess-empty")
    assert selection is None


async def test_16_explicit_binding(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="Explicit",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-explicit-model",
    )
    selection = await runtime.resolve_selection("sess-bound")
    assert selection is not None
    assert selection.selection_source == "explicit"
    assert selection.profile_id == p.id
    assert selection.provider_id == "anthropic"
    assert selection.model_id == "claude-explicit-model"


async def test_17_default_binding(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="Default",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-default",
        is_default=True,
    )
    # initialize_new_session_binding seeds a default binding
    await svc.initialize_new_session_binding(session_id="sess-empty")
    selection = await runtime.resolve_selection("sess-empty")
    assert selection is not None
    assert selection.selection_source == "default"
    assert selection.profile_id == p.id
    assert selection.model_id == "claude-default"


# ============================================================================
# 18-19: model_id source
# ============================================================================


async def test_18_model_id_from_binding_not_profile_default(
    env: tuple,
) -> None:
    """Binding.model_id overrides Profile.default_model."""
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="P",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-profile-default",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-binding-override",
    )
    selection = await runtime.resolve_selection("sess-bound")
    assert selection is not None
    assert selection.model_id == "claude-binding-override"
    assert selection.model_id != "claude-profile-default"


# ============================================================================
# 19-22: Profile state rejections
# ============================================================================


async def test_19_disabled_profile_rejected(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="WillDisable",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-X",
    )
    # Now disable the already-bound profile
    await svc.update_profile(p.id, enabled=False)
    with pytest.raises(ProviderSelectionDisabledError) as exc_info:
        await runtime.resolve_selection("sess-bound")
    assert str(exc_info.value) == "session provider profile is disabled"


async def test_20_profile_not_found(
    env: tuple,
) -> None:
    """Binding points to a Profile whose get_profile raises NotFound.

    Store-level FK prevents injecting a dangling binding——so we monkeypatch
    ``ProviderConfigService.get_profile`` to simulate post-binding profile loss.
    """
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="WillVanish",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-X",
    )

    # Simulate "binding points to deleted profile"——raise at the service layer
    from pi_agent_core_py.web.providers.config_store import (
        ProviderProfileNotFoundError,
    )

    async def _raise_not_found(profile_id: str) -> Any:
        raise ProviderProfileNotFoundError("simulated: profile gone")

    svc.get_profile = _raise_not_found  # type: ignore[assignment]

    with pytest.raises(ProviderSelectionNotFoundError) as exc_info:
        await runtime.resolve_selection("sess-bound")
    assert str(exc_info.value) == "session provider profile is unavailable"


async def test_21_provider_definition_missing(
    env: tuple,
) -> None:
    """Provider removed from registry after Profile was created."""
    svc, _, registry, runtime, _, _ = env
    p = await svc.create_profile(
        name="P",
        provider_id="qwen",
        credential_id="cred-ready",
        default_model="qwen-plus",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="qwen-plus",
    )
    # Simulate provider being removed from registry
    registry.remove("qwen")
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.resolve_selection("sess-bound")
    assert str(exc_info.value) == "session provider initialization failed"


# ============================================================================
# 22: credential_id in snapshot but not in repr
# ============================================================================


async def test_22_credential_id_in_snapshot_but_not_in_repr(
    env: tuple,
) -> None:
    svc, cred, _, runtime, _, _ = env
    cred.seed(_make_credential_view("cred-super-secret-id"))
    p = await svc.create_profile(
        name="ReprCheck",
        provider_id="anthropic",
        credential_id="cred-super-secret-id",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-X",
    )
    selection = await runtime.resolve_selection("sess-bound")
    assert selection is not None
    assert selection.credential_id == "cred-super-secret-id"
    # repr must NOT contain credential_id
    r = repr(selection)
    assert "cred-super-secret-id" not in r
    # But should contain profile_id / provider_id / model_id / selection_source
    assert "anthropic" in r
    assert "claude-X" in r
    assert "explicit" in r


# ============================================================================
# 23-24: resolve stage invariants
# ============================================================================


async def test_23_resolve_does_not_read_secret(
    env: tuple,
) -> None:
    svc, cred, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="NoSecret",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-X",
    )
    await runtime.resolve_selection("sess-bound")
    assert cred.resolve_calls == [], (
        "resolve_selection must not trigger resolve_secret_for_request"
    )


async def test_24_resolve_does_not_call_factory(
    env: tuple,
) -> None:
    svc, _, _, runtime, _, factory_calls = env
    p = await svc.create_profile(
        name="NoFactory",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-X",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-X",
    )
    await runtime.resolve_selection("sess-bound")
    assert factory_calls == [], (
        "resolve_selection must not trigger provider_factory"
    )


# ============================================================================
# 14 (bonus): frozen snapshot——Profile update does not affect existing selection
# ============================================================================


async def test_profile_update_does_not_mutate_existing_selection(
    env: tuple,
) -> None:
    """Once selection snapshotted, updating Profile / Binding must not affect it."""
    svc, _, _, runtime, _, _ = env
    p = await svc.create_profile(
        name="Orig",
        provider_id="anthropic",
        credential_id="cred-ready",
        default_model="claude-orig",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p.id,
        model_id="claude-orig",
    )
    selection = await runtime.resolve_selection("sess-bound")
    assert selection is not None
    snap_provider = selection.provider_id
    snap_model = selection.model_id
    snap_profile_id = selection.profile_id

    # Change binding to a different profile + model
    p2 = await svc.create_profile(
        name="New",
        provider_id="glm",
        credential_id="cred-ready",
        default_model="glm-new",
    )
    await svc.set_session_binding(
        session_id="sess-bound",
        profile_id=p2.id,
        model_id="glm-new",
    )
    # Existing selection is frozen——unchanged
    assert selection.provider_id == snap_provider
    assert selection.model_id == snap_model
    assert selection.profile_id == snap_profile_id
    # Dataclass(frozen=True)——assignment raises
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        selection.model_id = "tampered"  # type: ignore[misc]


# ============================================================================
# Bonus: RequestProviderSelection repr exposes non-sensitive fields
# ============================================================================


def test_selection_repr_exposes_safe_fields() -> None:
    s = RequestProviderSelection(
        profile_id="prof-123",
        provider_id="anthropic",
        model_id="claude-X",
        selection_source="explicit",
        credential_id="cred-secret",
    )
    r = repr(s)
    assert "prof-123" in r
    assert "anthropic" in r
    assert "claude-X" in r
    assert "explicit" in r
    assert "cred-secret" not in r

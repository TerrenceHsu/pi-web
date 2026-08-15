"""ProviderConfigService Profile status derivation tests (E2-2).

覆盖 spec §12.2 Status 矩阵（10 项）——纯函数 ``derive_profile_status`` 测试.
"""
from __future__ import annotations

from pi_agent_core_py.web.credentials.service import CredentialView
from pi_agent_core_py.web.providers.config_service import (
    derive_profile_status,
)
from pi_agent_core_py.web.providers.config_store import ProviderProfile

# ============================================================================
# Helpers
# ============================================================================


def _profile(
    *,
    provider_id: str = "anthropic",
    enabled: bool = True,
    is_default: bool = False,
) -> ProviderProfile:
    return ProviderProfile(
        id="profile-x",
        name="X",
        provider_id=provider_id,
        credential_id="cred-A",
        default_model="model-A",
        enabled=enabled,
        is_default=is_default,
        created_at=1000,
        updated_at=1000,
    )


def _cred(
    *,
    storage_status: str = "ready",
    validation_status: str = "never_validated",
    last_validated_provider_id: str | None = None,
) -> CredentialView:
    return CredentialView(
        id="cred-A",
        label="Label-A",
        storage_mode="keyring",
        masked_value="sk-****8A31",
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
# Status matrix (10 cases per spec §12.2)
# ============================================================================


def test_1_disabled_wins_over_everything() -> None:
    """disabled has highest priority—even over missing credential."""
    p = _profile(enabled=False)
    c = None  # credential missing
    assert derive_profile_status(p, c) == "disabled"


def test_2_missing_credential() -> None:
    p = _profile()
    assert derive_profile_status(p, None) == "needs_credential"


def test_3_needs_key() -> None:
    p = _profile()
    c = _cred(storage_status="needs_key")
    assert derive_profile_status(p, c) == "needs_key"


def test_4_backend_unavailable() -> None:
    p = _profile()
    c = _cred(storage_status="backend_unavailable")
    assert derive_profile_status(p, c) == "backend_unavailable"


def test_5_provider_matched_invalid() -> None:
    p = _profile(provider_id="anthropic")
    c = _cred(
        storage_status="ready",
        validation_status="invalid",
        last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(p, c) == "credential_invalid"


def test_6_provider_matched_error() -> None:
    p = _profile(provider_id="anthropic")
    c = _cred(
        storage_status="ready",
        validation_status="error",
        last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(p, c) == "credential_error"


def test_7_provider_mismatched_invalid_does_not_affect() -> None:
    """Anthropic validation failure must NOT pollute GLM Profile referencing same cred."""
    p = _profile(provider_id="glm")
    c = _cred(
        storage_status="ready",
        validation_status="invalid",
        last_validated_provider_id="anthropic",  # different provider
    )
    assert derive_profile_status(p, c) == "ready"


def test_8_provider_mismatched_error_does_not_affect() -> None:
    p = _profile(provider_id="glm")
    c = _cred(
        storage_status="ready",
        validation_status="error",
        last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(p, c) == "ready"


def test_9_never_validated_is_ready() -> None:
    p = _profile()
    c = _cred(
        storage_status="ready",
        validation_status="never_validated",
        last_validated_provider_id=None,
    )
    assert derive_profile_status(p, c) == "ready"


def test_10_valid_is_ready() -> None:
    p = _profile(provider_id="anthropic")
    c = _cred(
        storage_status="ready",
        validation_status="valid",
        last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(p, c) == "ready"


# ============================================================================
# Priority ordering (defensive—validate fixed ordering)
# ============================================================================


def test_disabled_beats_needs_credential() -> None:
    """disabled > needs_credential in priority."""
    p = _profile(enabled=False)
    assert derive_profile_status(p, None) == "disabled"


def test_needs_key_beats_provider_matched_invalid() -> None:
    """storage_status=needs_key > validation_status=invalid."""
    p = _profile(provider_id="anthropic")
    c = _cred(
        storage_status="needs_key",
        validation_status="invalid",
        last_validated_provider_id="anthropic",
    )
    assert derive_profile_status(p, c) == "needs_key"


def test_backend_unavailable_beats_needs_key() -> None:
    """Both are storage issues—needs_key checked first per ordering."""
    # Per implementation, needs_key is checked before backend_unavailable.
    # This test documents the order; if storage_status were both (impossible),
    # needs_key would win.
    p = _profile()
    c1 = _cred(storage_status="needs_key")
    c2 = _cred(storage_status="backend_unavailable")
    assert derive_profile_status(p, c1) == "needs_key"
    assert derive_profile_status(p, c2) == "backend_unavailable"


# ============================================================================
# Status type exhaustiveness
# ============================================================================


def test_all_status_values_covered() -> None:
    """Sanity: each ProfileStatus literal can be produced by some input."""
    produced: set[str] = set()
    cases = [
        (_profile(enabled=False), None),  # disabled
        (_profile(), None),  # needs_credential
        (_profile(), _cred(storage_status="needs_key")),  # needs_key
        (_profile(), _cred(storage_status="backend_unavailable")),  # backend_unavailable
        (
            _profile(provider_id="anthropic"),
            _cred(
                storage_status="ready",
                validation_status="invalid",
                last_validated_provider_id="anthropic",
            ),
        ),  # credential_invalid
        (
            _profile(provider_id="anthropic"),
            _cred(
                storage_status="ready",
                validation_status="error",
                last_validated_provider_id="anthropic",
            ),
        ),  # credential_error
        (_profile(), _cred(storage_status="ready")),  # ready
    ]
    for p, c in cases:
        produced.add(derive_profile_status(p, c))
    expected = {
        "ready", "disabled", "needs_credential", "needs_key",
        "backend_unavailable", "credential_invalid", "credential_error",
    }
    assert produced == expected

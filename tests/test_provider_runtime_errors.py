"""RequestProviderRuntime error mapping tests（M1-4 §十一 + §十四.Errors）.

覆盖：
- 各 CredentialService 错误 → ProviderSelectionUnavailableError
- Factory / Registry 错误 → ProviderInitializationError
- Profile / Disabled / NotFound 错误映射
- 所有 wrap 异常 `from None`
- 固定安全消息（4 条）
- 异常 str/repr 不含敏感字段
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py.providers.base import ProviderAdapter
from pi_agent_core_py.providers.errors import ProviderConfigError
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
)
from pi_agent_core_py.web.credentials.errors import (
    CredentialRequestSecretBackendError,
    CredentialRequestSecretUnavailableError,
)
from pi_agent_core_py.web.credentials.service import CredentialService
from pi_agent_core_py.web.credentials.store import CredentialNotFoundError
from pi_agent_core_py.web.provider_runtime import (
    ProviderInitializationError,
    ProviderSelectionDisabledError,
    ProviderSelectionNotFoundError,
    ProviderSelectionUnavailableError,
    RequestProviderRuntime,
    RequestProviderSelection,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Fakes
# ============================================================================


def _make_def(pid: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=pid,
        display_name=pid,
        api_style="openai_compatible",
        default_base_url=f"https://{pid}.example.com",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


class _Registry(ProviderRegistry):
    def __init__(self) -> None:
        self._defs = {"qwen": _make_def("qwen"), "glm": _make_def("glm")}

    def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
        return self._defs.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
        return tuple(self._defs.values())

    def has(self, provider_id: str) -> bool:  # type: ignore[override]
        return provider_id in self._defs


class _NoOpConfigService:
    pass


class _StubAdapter(ProviderAdapter):
    def __init__(self) -> None:
        self.provider_id = "qwen"
        self.model = "qwen-plus"

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        yield  # type: ignore[unreachable]


def _selection(
    *,
    provider_id: str = "qwen",
    credential_id: str = "cred-X",
) -> RequestProviderSelection:
    return RequestProviderSelection(
        profile_id="prof-X",
        provider_id=provider_id,
        model_id="qwen-plus",
        selection_source="explicit",
        credential_id=credential_id,
    )


def _runtime(
    *,
    secret_outcome: str = "ok",
    factory_outcome: str = "ok",
    registry_missing: set[str] | None = None,
) -> tuple[RequestProviderRuntime, _Registry]:
    registry = _Registry()
    if registry_missing:
        for pid in registry_missing:
            registry._defs.pop(pid, None)

    class _CredSvc(CredentialService):
        def __init__(self) -> None:
            pass

        async def resolve_secret_for_request(  # type: ignore[override]
            self, credential_id: str
        ) -> str:
            if secret_outcome == "not_found":
                raise CredentialNotFoundError(f"not found: {credential_id}")
            if secret_outcome == "unavailable":
                raise CredentialRequestSecretUnavailableError(
                    "provider credential is unavailable"
                )
            if secret_outcome == "backend":
                raise CredentialRequestSecretBackendError(
                    "provider credential backend is unavailable"
                )
            return "stub-secret"

    def _factory(**kwargs: Any) -> ProviderAdapter:
        if factory_outcome == "config_error":
            raise ProviderConfigError("simulated factory error")
        if factory_outcome == "unknown_error":
            raise RuntimeError("unexpected factory failure")
        return _StubAdapter()

    runtime = RequestProviderRuntime(
        provider_config_service=_NoOpConfigService(),  # type: ignore[arg-type]
        credential_service=_CredSvc(),  # type: ignore[arg-type]
        provider_registry=registry,
        provider_factory=_factory,
    )
    return runtime, registry


# ============================================================================
# Credential errors → ProviderSelectionUnavailableError
# ============================================================================


async def test_credential_not_found_maps_to_unavailable() -> None:
    runtime, _ = _runtime(secret_outcome="not_found")
    with pytest.raises(ProviderSelectionUnavailableError) as exc_info:
        await runtime.build_adapter(_selection(credential_id="cred-super-secret"))
    assert str(exc_info.value) == "session provider credential is unavailable"
    assert exc_info.value.__cause__ is None


async def test_credential_unavailable_maps_to_unavailable() -> None:
    runtime, _ = _runtime(secret_outcome="unavailable")
    with pytest.raises(ProviderSelectionUnavailableError) as exc_info:
        await runtime.build_adapter(_selection())
    assert str(exc_info.value) == "session provider credential is unavailable"
    assert exc_info.value.__cause__ is None


async def test_credential_backend_unavailable_maps_to_unavailable() -> None:
    runtime, _ = _runtime(secret_outcome="backend")
    with pytest.raises(ProviderSelectionUnavailableError) as exc_info:
        await runtime.build_adapter(_selection())
    assert str(exc_info.value) == "session provider credential is unavailable"
    assert exc_info.value.__cause__ is None


# ============================================================================
# Factory / Registry errors → ProviderInitializationError
# ============================================================================


async def test_factory_config_error_maps_to_initialization() -> None:
    runtime, _ = _runtime(factory_outcome="config_error")
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.build_adapter(_selection())
    assert str(exc_info.value) == "session provider initialization failed"
    assert exc_info.value.__cause__ is None


async def test_factory_unknown_error_maps_to_initialization() -> None:
    runtime, _ = _runtime(factory_outcome="unknown_error")
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.build_adapter(_selection())
    assert str(exc_info.value) == "session provider initialization failed"
    assert exc_info.value.__cause__ is None


async def test_provider_definition_missing_maps_to_initialization() -> None:
    runtime, _ = _runtime(registry_missing={"qwen"})
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.build_adapter(_selection(provider_id="qwen"))
    assert str(exc_info.value) == "session provider initialization failed"
    assert exc_info.value.__cause__ is None


# ============================================================================
# All errors are RequestProviderRuntimeError subclasses
# ============================================================================


def test_error_hierarchy() -> None:
    from pi_agent_core_py.web.provider_runtime import (
        RequestProviderRuntimeError,
    )

    assert issubclass(ProviderSelectionNotFoundError, RequestProviderRuntimeError)
    assert issubclass(ProviderSelectionDisabledError, RequestProviderRuntimeError)
    assert issubclass(ProviderSelectionUnavailableError, RequestProviderRuntimeError)
    assert issubclass(ProviderInitializationError, RequestProviderRuntimeError)


# ============================================================================
# Error messages are stable + safe
# ============================================================================


SAFE_MESSAGES = {
    "session provider profile is unavailable",
    "session provider profile is disabled",
    "session provider credential is unavailable",
    "session provider initialization failed",
}


def test_safe_messages_set_covers_all_runtime_errors() -> None:
    """All four runtime error classes must use one of the fixed safe messages.
    Since messages are module constants, instantiate + check directly."""
    from pi_agent_core_py.web import provider_runtime as mod

    assert mod._MSG_PROFILE_UNAVAILABLE in SAFE_MESSAGES
    assert mod._MSG_PROFILE_DISABLED in SAFE_MESSAGES
    assert mod._MSG_CREDENTIAL_UNAVAILABLE in SAFE_MESSAGES
    assert mod._MSG_INITIALIZATION_FAILED in SAFE_MESSAGES


def test_safe_messages_no_sensitive_tokens() -> None:
    """The 4 messages must not contain profile/credential/model hints."""
    sensitive = ("profile_id", "credential_id", "model_id", "provider_id", "cred-")
    for msg in SAFE_MESSAGES:
        for token in sensitive:
            assert token not in msg, f"safe message leaks {token!r}: {msg!r}"


# ============================================================================
# Errors don't leak credential_id / profile_id / model_id via str/repr
# ============================================================================


async def test_unavailable_error_does_not_leak_credential_id() -> None:
    runtime, _ = _runtime(secret_outcome="unavailable")
    with pytest.raises(ProviderSelectionUnavailableError) as exc_info:
        await runtime.build_adapter(_selection(credential_id="cred-LEAK-CHECK"))
    s = str(exc_info.value)
    r = repr(exc_info.value)
    assert "cred-LEAK-CHECK" not in s
    assert "cred-LEAK-CHECK" not in r


async def test_initialization_error_does_not_leak_model_id() -> None:
    runtime, _ = _runtime(factory_outcome="config_error")
    sel = _selection()
    sel_with_sensitive_model = RequestProviderSelection(
        profile_id=sel.profile_id,
        provider_id=sel.provider_id,
        model_id="super-secret-model-id",
        selection_source="explicit",
        credential_id=sel.credential_id,
    )
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.build_adapter(sel_with_sensitive_model)
    s = str(exc_info.value)
    r = repr(exc_info.value)
    assert "super-secret-model-id" not in s
    assert "super-secret-model-id" not in r

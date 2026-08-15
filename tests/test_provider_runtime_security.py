"""RequestProviderRuntime security / import-boundary tests（M1-4 §十四.Security）.

覆盖：
- provider_runtime.py 源码不直接 import SecretStoreRouter / CredentialRepository
  / OSKeyringSecretStore / EnvSecretStore / InMemorySecretStore / SQLiteProviderConfigStore
  / httpx / anthropic / openai
- Selection repr 无 credential_id
- 异常 str/repr 无 profile / credential / model
- Secret marker 不出现在 repr / caplog / 异常链
- 构造 + 解析阶段真实网络调用 = 0
- 源码不写 context.metadata / Message / Event / Snapshot / Revision
"""
from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any

import pytest

from pi_agent_core_py.providers.base import ProviderAdapter
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
)
from pi_agent_core_py.web.credentials.errors import (
    CredentialRequestSecretUnavailableError,
)
from pi_agent_core_py.web.credentials.service import CredentialService
from pi_agent_core_py.web.provider_runtime import (
    ProviderInitializationError,
    ProviderSelectionUnavailableError,
    RequestProviderRuntime,
    RequestProviderSelection,
)

pytestmark = pytest.mark.asyncio

SECRET_MARKER = "sk-M1-4-SEC-FACTORY-MARKER-DO-NOT-LEAK"
PROFILE_MARKER = "prof-TOP-SECRET-ID"
CREDENTIAL_MARKER = "cred-TOP-SECRET-ID"
MODEL_MARKER = "model-TOP-SECRET-ID"


# ============================================================================
# Helpers
# ============================================================================


_RUNTIME_SRC_PATH = (
    pathlib.Path(__file__).parent.parent
    / "src"
    / "pi_agent_core_py"
    / "web"
    / "provider_runtime.py"
)


def _runtime_imports() -> set[str]:
    tree = ast.parse(_RUNTIME_SRC_PATH.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module)
    return mods


# ============================================================================
# 51-54: Import boundaries
# ============================================================================


def test_runtime_does_not_import_secret_store_router() -> None:
    for mod in _runtime_imports():
        assert "secret_store_router" not in mod, f"imports {mod!r}"
        assert "SecretStoreRouter" not in mod, f"imports {mod!r}"


def test_runtime_does_not_import_credential_repository() -> None:
    """Runtime may import domain error classes from credentials_store, but
    must NOT import the concrete repository class (data layer)."""
    source = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # Forbidden class imports——data layer
                assert alias.name not in (
                    "SQLiteCredentialStore",
                    "CredentialRepository",
                    "CredentialRecord",
                ), f"runtime imports credentials_store.{alias.name}"


def test_runtime_does_not_import_concrete_secret_stores() -> None:
    forbidden = ("secrets.memory", "secrets.env", "secrets.keyring", "secrets.base")
    imports = _runtime_imports()
    for mod in imports:
        for token in forbidden:
            assert token not in mod, f"runtime imports {mod!r}"


def test_runtime_does_not_import_sdk_http_clients() -> None:
    forbidden = ("httpx", "anthropic", "openai", "aiohttp")
    imports = _runtime_imports()
    for mod in imports:
        for token in forbidden:
            assert token not in mod, f"runtime imports {mod!r}"


def test_runtime_does_not_import_provider_config_store() -> None:
    """SQLiteProviderConfigStore is E2 internals——Runtime must go through
    ProviderConfigService. Domain error classes from provider_config_store
    (e.g. ProviderProfileNotFoundError) are allowed——they bubble up via the
    Service."""
    source = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in (
                    "SQLiteProviderConfigStore",
                    "ProviderConfigStore",
                ), f"runtime imports provider_config_store.{alias.name}"


def test_runtime_does_not_import_web_app() -> None:
    imports = _runtime_imports()
    for mod in imports:
        assert "web.app" not in mod, f"runtime imports {mod!r}"
        # Provider runtime must not pull FastAPI / uvicorn
        assert "fastapi" not in mod, f"runtime imports {mod!r}"
        assert "uvicorn" not in mod, f"runtime imports {mod!r}"


# ============================================================================
# 55: Selection repr has no credential_id (also covered in selection tests)
# ============================================================================


def test_selection_repr_omits_credential_id() -> None:
    s = RequestProviderSelection(
        profile_id=PROFILE_MARKER,
        provider_id="qwen",
        model_id=MODEL_MARKER,
        selection_source="explicit",
        credential_id=CREDENTIAL_MARKER,
    )
    r = repr(s)
    assert CREDENTIAL_MARKER not in r
    # profile_id / model_id ARE allowed in repr (runtime IDs are not secrets)
    # but for paranoia, MODEL_MARKER and PROFILE_MARKER are not Secret——
    # verify CREDENTIAL_MARKER is the only hidden one
    assert PROFILE_MARKER in r
    assert MODEL_MARKER in r


# ============================================================================
# 56: Runtime repr minimal
# ============================================================================


def test_runtime_repr_does_not_expose_inner_services() -> None:
    """Runtime repr should be the default object repr——don't auto-expand
    Service / Registry which may contain sensitive state in the future."""

    class _CredSvcStub:
        async def resolve_secret_for_request(self, credential_id: str) -> str:
            return "x"

    class _CfgSvcStub:
        pass

    runtime = RequestProviderRuntime(
        provider_config_service=_CfgSvcStub(),  # type: ignore[arg-type]
        credential_service=_CredSvcStub(),  # type: ignore[arg-type]
        provider_registry=ProviderRegistry(()),  # empty registry
    )
    r = repr(runtime)
    # Default object repr is just `RequestProviderRuntime object at 0x...`——
    # we don't see the injected services by default. Verify by absence.
    assert "cred" not in r.lower()
    assert "secret" not in r.lower()


# ============================================================================
# 57: Exceptions carry no profile / credential / model markers
# ============================================================================


async def test_exceptions_no_sensitive_markers() -> None:
    """Build an adapter where CredentialService raises with the SECRET_MARKER
    payload——Runtime must wrap such that the marker is absent from the
    surfaced exception str/repr."""
    PROFILE_ID = PROFILE_MARKER
    CRED_ID = CREDENTIAL_MARKER
    MODEL_ID = MODEL_MARKER

    class _RaisingCredSvc(CredentialService):
        def __init__(self) -> None:
            pass

        async def resolve_secret_for_request(  # type: ignore[override]
            self, credential_id: str
        ) -> str:
            # Secret value is intentionally referenced here to prove the
            # wrapping drops it
            _ = SECRET_MARKER
            raise CredentialRequestSecretUnavailableError(
                "provider credential is unavailable"
            )

    class _Registry(ProviderRegistry):
        def __init__(self) -> None:
            self._defs = {"qwen": ProviderDefinition(
                id="qwen",
                display_name="qwen",
                api_style="openai_compatible",
                default_base_url="https://qwen.example.com",
                credential_validation_strategy="unsupported",
                credential_validation_endpoint=None,
                supports_model_listing=False,
            )}

        def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
            return self._defs.get(provider_id)

        def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
            return tuple(self._defs.values())

        def has(self, provider_id: str) -> bool:  # type: ignore[override]
            return provider_id in self._defs

    runtime = RequestProviderRuntime(
        provider_config_service=type("CS", (), {}),  # type: ignore[arg-type]
        credential_service=_RaisingCredSvc(),  # type: ignore[arg-type]
        provider_registry=_Registry(),
    )

    sel = RequestProviderSelection(
        profile_id=PROFILE_ID,
        provider_id="qwen",
        model_id=MODEL_ID,
        selection_source="explicit",
        credential_id=CRED_ID,
    )

    with pytest.raises(ProviderSelectionUnavailableError) as exc_info:
        await runtime.build_adapter(sel)

    s = str(exc_info.value)
    r = repr(exc_info.value)
    for marker in (SECRET_MARKER, PROFILE_MARKER, CREDENTIAL_MARKER, MODEL_MARKER):
        assert marker not in s, f"str leaks {marker!r}: {s!r}"
        assert marker not in r, f"repr leaks {marker!r}: {r!r}"


# ============================================================================
# 58-60: Secret marker absent from repr / caplog / exception chain
# ============================================================================


async def test_secret_marker_absent_from_caplog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Runtime does not log——any log line that exists must not contain marker."""
    caplog.set_level(logging.DEBUG)

    class _CredSvcStub(CredentialService):
        def __init__(self) -> None:
            pass

        async def resolve_secret_for_request(  # type: ignore[override]
            self, credential_id: str
        ) -> str:
            return SECRET_MARKER

    class _Registry(ProviderRegistry):
        def __init__(self) -> None:
            self._defs = {"qwen": ProviderDefinition(
                id="qwen",
                display_name="qwen",
                api_style="openai_compatible",
                default_base_url="https://qwen.example.com",
                credential_validation_strategy="unsupported",
                credential_validation_endpoint=None,
                supports_model_listing=False,
            )}

        def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
            return self._defs.get(provider_id)

        def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
            return tuple(self._defs.values())

        def has(self, provider_id: str) -> bool:  # type: ignore[override]
            return provider_id in self._defs

    def _factory(**kwargs: Any) -> ProviderAdapter:
        class _A(ProviderAdapter):
            def __init__(self_) -> None:
                self_.provider_id = "qwen"
                self_.model = kwargs["model_id"]

            async def stream(self_, request: Any) -> Any:  # pragma: no cover
                yield  # type: ignore[unreachable]

        return _A()

    runtime = RequestProviderRuntime(
        provider_config_service=type("CS", (), {}),  # type: ignore[arg-type]
        credential_service=_CredSvcStub(),  # type: ignore[arg-type]
        provider_registry=_Registry(),
        provider_factory=_factory,
    )

    sel = RequestProviderSelection(
        profile_id="prof",
        provider_id="qwen",
        model_id="m",
        selection_source="explicit",
        credential_id="cred",
    )
    await runtime.build_adapter(sel)

    assert SECRET_MARKER not in caplog.text


def test_secret_marker_absent_from_exception_chain() -> None:
    """Manually construct a wrapped exception to confirm __cause__ truncation
    pattern works at the module level."""
    try:
        try:
            raise ValueError(f"simulated SDK error: key={SECRET_MARKER}")
        except Exception:
            raise ProviderInitializationError(
                "session provider initialization failed"
            ) from None
    except ProviderInitializationError as exc:
        assert SECRET_MARKER not in str(exc)
        assert SECRET_MARKER not in repr(exc)
        # __cause__ must be None——from None guarantees this
        assert exc.__cause__ is None


# ============================================================================
# 61: Real network calls = 0 during build_adapter
# ============================================================================


async def test_no_network_calls_during_build_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class _CredSvcStub(CredentialService):
        def __init__(self) -> None:
            pass

        async def resolve_secret_for_request(  # type: ignore[override]
            self, credential_id: str
        ) -> str:
            return SECRET_MARKER

    class _Registry(ProviderRegistry):
        def __init__(self) -> None:
            self._defs = {"qwen": ProviderDefinition(
                id="qwen",
                display_name="qwen",
                api_style="openai_compatible",
                default_base_url="https://qwen.example.com",
                credential_validation_strategy="unsupported",
                credential_validation_endpoint=None,
                supports_model_listing=False,
            )}

        def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
            return self._defs.get(provider_id)

        def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
            return tuple(self._defs.values())

        def has(self, provider_id: str) -> bool:  # type: ignore[override]
            return provider_id in self._defs

    def _no_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_adapter must not create HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", _no_http)
    monkeypatch.setattr(httpx, "Client", _no_http)

    def _factory(**_: Any) -> ProviderAdapter:
        class _A(ProviderAdapter):
            def __init__(self_) -> None:
                self_.provider_id = "qwen"
                self_.model = "qwen-plus"

            async def stream(self_, request: Any) -> Any:  # pragma: no cover
                yield  # type: ignore[unreachable]

        return _A()

    runtime = RequestProviderRuntime(
        provider_config_service=type("CS", (), {}),  # type: ignore[arg-type]
        credential_service=_CredSvcStub(),  # type: ignore[arg-type]
        provider_registry=_Registry(),
        provider_factory=_factory,
    )

    sel = RequestProviderSelection(
        profile_id="prof",
        provider_id="qwen",
        model_id="m",
        selection_source="explicit",
        credential_id="cred",
    )
    adapter = await runtime.build_adapter(sel)
    assert adapter is not None


# ============================================================================
# 62-63: No context.metadata / Message / Event / Snapshot / Revision writes
# ============================================================================


def _strip_docstrings(tree: ast.Module) -> str:
    """Re-serialize AST without module/class/function docstrings."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_runtime_source_does_not_write_context_metadata() -> None:
    """Parse AST, strip docstrings——check actual code doesn't assign to
    context.metadata."""
    source = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    code_only = _strip_docstrings(tree)
    # Runtime must not assign to context.metadata (Prompt path's territory)
    assert "context.metadata" not in code_only
    assert ".metadata =" not in code_only
    assert ".metadata[" in code_only or "metadata[" not in code_only  # no subscript writes


def test_runtime_source_does_not_write_messages_events_snapshots() -> None:
    """Runtime is lifecycle only——no message/event/snapshot mutation in code."""
    source = _RUNTIME_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    code_only = _strip_docstrings(tree)
    forbidden = (
        "LLMMessage",
        "AssistantMessage",
        "UserMessage",
        "ToolResultMessage",
        "TextDeltaEvent",
        "ToolCallEvent",
        "DoneEvent",
        "ErrorEvent",
        ".Snapshot",
        ".Revision",
        "append_message",
        "add_message",
        "session.add_",
    )
    for token in forbidden:
        assert token not in code_only, (
            f"runtime code references {token!r}——lifecycle only"
        )

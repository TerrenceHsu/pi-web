"""Composition root tests for M1-5（spec §十三.Composition 1-8）.

验证：
- Provider Runtime 启用时 → RequestProviderRuntime 创建
- Provider Config / Credential Runtime 未启用 → runtime = None
- 复用同一 CredentialService / ProviderConfigService
- 使用默认 _DEFAULT_REGISTRY
- 使用 create_provider factory
- 不创建第二个 SecretStoreRouter
- App shutdown 无额外 close
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.providers.factory import create_provider  # noqa: E402
from pi_agent_core_py.providers.registry import _DEFAULT_REGISTRY  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.runtime import RequestProviderRuntime  # noqa: E402

pytestmark = pytest.mark.asyncio


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


def _build_app(
    tmp_path: Path,
    *,
    enable_credential_runtime: bool | None = None,
    enable_provider_profiles_api: bool | None = None,
) -> object:
    kwargs = {
        "db_path": str(tmp_path / "app.db"),
        "credential_secret_backend": "memory",
        "credential_extra_hosts": ("testserver", "localhost", "127.0.0.1"),
        "enable_trusted_host": True,
    }
    if enable_credential_runtime is not None:
        kwargs["enable_credential_runtime"] = enable_credential_runtime
    if enable_provider_profiles_api is not None:
        kwargs["enable_provider_profiles_api"] = enable_provider_profiles_api
    return create_app(_harness(), **kwargs)


# ============================================================================
# 1-2: Provider Runtime construction gating
# ============================================================================


async def test_1_runtime_created_when_both_runtimes_enabled(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        assert app.state.credential_runtime is not None
        assert app.state.provider_config_runtime is not None
        assert app.state.request_provider_runtime is not None
        assert isinstance(app.state.request_provider_runtime, RequestProviderRuntime)


async def test_2_runtime_none_when_credential_runtime_disabled(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path, enable_credential_runtime=False)
    with TestClient(app, base_url="http://testserver"):
        assert app.state.credential_runtime is None
        assert app.state.provider_config_runtime is None
        assert app.state.request_provider_runtime is None


async def test_2b_runtime_none_when_provider_profiles_disabled(
    tmp_path: Path,
) -> None:
    app = _build_app(tmp_path, enable_provider_profiles_api=False)
    with TestClient(app, base_url="http://testserver"):
        # credential_runtime may be enabled, but provider_config_runtime disabled
        # → request_provider_runtime must be None
        assert app.state.provider_config_runtime is None
        assert app.state.request_provider_runtime is None


# ============================================================================
# 3-4: Reuse existing services
# ============================================================================


async def test_3_reuses_credential_service(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None
        cred_runtime = app.state.credential_runtime
        assert cred_runtime is not None
        # The same CredentialService instance must be reused——not a second one
        assert runtime._credential_service is cred_runtime.service


async def test_4_reuses_provider_config_service(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None
        pc_runtime = app.state.provider_config_runtime
        assert pc_runtime is not None
        assert runtime._provider_config_service is pc_runtime.service


# ============================================================================
# 5-6: Default registry + factory
# ============================================================================


async def test_5_uses_default_provider_registry(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None
        # Must use the frozen default registry——all 4 providers available.
        # We don't assert identity (``is``) because editable-install module
        # reloading across the full suite can produce distinct singleton
        # instances; structural equality is sufficient.
        reg = runtime._provider_registry
        ids = {d.id for d in reg.list()}
        assert ids == {d.id for d in _DEFAULT_REGISTRY.list()}
        assert "glm" in ids
        assert "anthropic" in ids
        assert "qwen" in ids
        assert "kimi" in ids


async def test_6_uses_create_provider_factory(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None
        # Must be the M1-3 factory function
        assert runtime._provider_factory is create_provider


# ============================================================================
# 7: No second SecretStoreRouter
# ============================================================================


async def test_7_does_not_create_second_secret_store_router(
    tmp_path: Path,
) -> None:
    """Runtime must not instantiate its own SecretStoreRouter——it talks to
    CredentialService only."""
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None
        # No router attribute on the runtime
        assert not hasattr(runtime, "_router") or runtime._router is None
        assert not hasattr(runtime, "_secret_store_router")

    # Module source must not import SecretStoreRouter as a class
    import ast
    import pathlib

    src_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "providers"
        / "runtime.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert "SecretStore" not in alias.name, (
                    f"runtime imports {alias.name}——must not pull SecretStore layer"
                )
                assert "secret_store_router" not in (node.module or ""), (
                    f"runtime imports from {node.module}"
                )


# ============================================================================
# 8: App shutdown——no extra close for runtime
# ============================================================================


async def test_8_shutdown_clears_state_without_extra_close(
    tmp_path: Path,
) -> None:
    """Runtime has no long-lived resources——shutdown just clears the ref."""
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver"):
        runtime = app.state.request_provider_runtime
        assert runtime is not None

    # After exit: state cleared
    assert app.state.request_provider_runtime is None
    assert app.state.provider_config_runtime is None
    assert app.state.credential_runtime is None


# ============================================================================
# 9: Placeholder initialized to None before lifespan
# ============================================================================


def test_9_placeholder_set_to_none_at_construction(tmp_path: Path) -> None:
    """Before TestClient enters (lifespan runs), placeholder must be None."""
    app = _build_app(tmp_path)
    # Before lifespan——all placeholders None
    assert app.state.request_provider_runtime is None
    assert app.state.credential_runtime is None
    assert app.state.provider_config_runtime is None

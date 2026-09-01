"""Prompt provider runtime security tests for M1-5（spec §十三.Security）.

覆盖：
- Secret marker 不出现在 HTTP / WS / error state / Message / Snapshot /
  Revision / 日志
- 不写 context.metadata["provider_selection"]
- 真实网络调用 = 0
- Runtime 错误不包含 profile / model / credential
- web/app.py 不直接导入 SecretStoreRouter / CredentialRepository（runtime 边界）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

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
from pi_agent_core_py.providers.base import ProviderAdapter, ProviderRequest  # noqa: E402
from pi_agent_core_py.providers.registry import (  # noqa: E402
    _DEFAULT_REGISTRY,
    ProviderDefinition,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.runtime import (  # noqa: E402
    RequestProviderRuntime,
)

_UI = {"X-PI-Agent-UI": "1"}
SECRET_MARKER = "sk-M1-5-SECURITY-SECRET-MARKER-DO-NOT-LEAK-ANYWHERE"


# ============================================================================
# Setup
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="legacy"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


def _factory_called_with_marker(
    *,
    log: list[dict[str, Any]],
) -> Any:
    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        log.append({"api_key": api_key})

        class _A(ProviderAdapter):
            def __init__(self_) -> None:
                self_.provider_id = provider_definition.id
                self_.model = model_id

            async def stream(self_, request: ProviderRequest) -> Any:
                yield TextDeltaEvent(delta="ok")  # type: ignore[unreachable]
                yield DoneEvent(stop_reason="stop", usage=Usage())

        return _A()

    return factory


@pytest.fixture
async def env(tmp_path: Path) -> tuple[TestClient, object, list[dict[str, Any]]]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    factory_log: list[dict[str, Any]] = []
    with TestClient(app, base_url="http://testserver") as client:
        runtime = RequestProviderRuntime(
            provider_config_service=app.state.provider_config_runtime.service,
            credential_service=app.state.credential_runtime.service,
            provider_registry=_DEFAULT_REGISTRY,
            provider_factory=_factory_called_with_marker(log=factory_log),
        )
        app.state.request_provider_runtime = runtime
        yield client, app, factory_log  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": "L", "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(c: TestClient, *, provider_id: str, credential_id: str) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": f"P-{provider_id}",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": "m",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["profile"]["id"]


def _make_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"title": "T"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _bind(c: TestClient, *, session_id: str, profile_id: str) -> None:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": "m"},
    )
    assert r.status_code == 200, r.text


# ============================================================================
# 49: Secret marker not in HTTP response body
# ============================================================================


async def test_secret_marker_not_in_http_response_body(env: tuple) -> None:
    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200
    assert SECRET_MARKER not in r.text

    # History endpoint
    hist = client.get(f"/api/messages?session_id={sess}")
    assert SECRET_MARKER not in hist.text


async def test_secret_marker_not_in_sessions_endpoint(env: tuple) -> None:
    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    r = client.get("/api/sessions")
    assert SECRET_MARKER not in r.text


# ============================================================================
# 51: Secret marker not in Message persistence
# ============================================================================


async def test_secret_marker_not_in_persisted_message(env: tuple) -> None:
    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    body = client.get(f"/api/messages?session_id={sess}").json()
    for msg in body["messages"]:
        # The full message dict
        assert SECRET_MARKER not in repr(msg)
        # The inner AgentMessage dict
        if "message" in msg:
            assert SECRET_MARKER not in repr(msg["message"])


# ============================================================================
# 52: Secret marker not in Snapshot
# ============================================================================


async def test_secret_marker_not_in_snapshot(env: tuple) -> None:
    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    # Snapshots endpoint——if exists
    r = client.get(f"/api/sessions/{sess}/snapshots")
    if r.status_code == 200:
        assert SECRET_MARKER not in r.text


# ============================================================================
# 54: Secret marker not in log output
# ============================================================================


async def test_secret_marker_not_in_logs(
    env: tuple,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, app, _ = env
    caplog.set_level(logging.DEBUG)
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    assert SECRET_MARKER not in caplog.text


# ============================================================================
# 55: context.metadata["provider_selection"] not written
# ============================================================================


async def test_no_provider_selection_in_context_metadata(env: tuple) -> None:
    """M1-5 must not write provider_selection info into harness.context.metadata."""
    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    harness = app.state.web.harness
    metadata = harness.context.metadata
    # Spec forbids writing provider selection into metadata
    forbidden_keys = (
        "provider_selection",
        "provider_profile_id",
        "provider_credential_id",
        "request_selection",
    )
    for key in forbidden_keys:
        assert key not in metadata, (
            f"metadata[{key!r}] must not be written by M1-5"
        )


# ============================================================================
# 57: web/app.py must NOT import SecretStoreRouter / CredentialRepository directly
# ============================================================================


def test_app_py_does_not_import_secret_store_router() -> None:
    """web/app.py may reference CredentialService / CredentialRuntime, but must
    not import SecretStoreRouter directly——that's the runtime's boundary."""
    import ast
    import pathlib

    app_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "app.py"
    )
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # May import for type-only reasons; forbid concrete pulls
                assert alias.name != "SecretStoreRouter", (
                    "app.py imports SecretStoreRouter directly"
                )


def test_app_py_delegates_provider_binding_to_coding_agent_composition() -> None:
    """Web installs Provider runtime but never performs its binding itself."""
    import inspect

    from pi_agent_core_py.web import app as app_module

    src = inspect.getsource(app_module)
    assert "RequestProviderRuntime" in src
    assert "coding_agent_services.provider_runtime" in src
    assert "agent_session.compose_request" in src
    assert ".bind_to_harness(" not in src
    assert ".resolve_selection(" not in src


# ============================================================================
# 56: Real network calls = 0 during prompt execution
# ============================================================================


async def test_no_real_network_calls_during_prompt(
    env: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block httpx clients——prompt path must not make real HTTP calls."""
    import httpx

    client, app, _ = env
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof)

    def _no_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("prompt path must not create real HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", _no_http)
    monkeypatch.setattr(httpx, "Client", _no_http)

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200


# ============================================================================
# 58: Runtime errors carry no profile / model / credential markers
# ============================================================================


async def test_runtime_errors_no_profile_model_credential_markers(
    env: tuple,
) -> None:
    """Factory-failure error response must not contain profile_id / model_id /
    credential_id values."""
    from pi_agent_core_py.providers.errors import ProviderConfigError

    client, app, _ = env

    PROFILE_NAME = "profile-LEAK-CHECK"
    MODEL_ID = "model-LEAK-CHECK"
    CRED_LABEL = "cred-LEAK-CHECK"

    cred_r = client.post(
        "/api/credentials",
        headers=_UI,
        json={"label": CRED_LABEL, "storage_mode": "session_only", "secret_value": SECRET_MARKER},
    )
    cred_id = cred_r.json()["credential"]["credential_id"]

    prof_r = client.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": PROFILE_NAME,
            "provider_id": "qwen",
            "credential_id": cred_id,
            "default_model": MODEL_ID,
        },
    )
    prof_id = prof_r.json()["profile"]["id"]

    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof_id)

    # Swap factory to one that fails——error response must omit sensitive ids
    runtime: RequestProviderRuntime = app.state.request_provider_runtime

    def failing_factory(**_: Any) -> Any:
        raise ProviderConfigError("simulated failure")

    runtime._provider_factory = failing_factory  # type: ignore[assignment]

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    body_text = r.text

    # Profile ID, Model ID, Cred ID values must not appear in error response
    assert prof_id not in body_text
    assert MODEL_ID not in body_text
    assert cred_id not in body_text
    # The actual profile/cred values may share prefix patterns——PROFILE_NAME
    # is a safe marker to grep
    assert PROFILE_NAME not in body_text
    assert CRED_LABEL not in body_text

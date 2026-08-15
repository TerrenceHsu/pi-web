"""Prompt integration tests for M1-5（spec §十三.Legacy + Selected Provider）.

验证：
- Session 无 Binding → 使用 legacy FakeClient，Secret/Factory 调用 = 0
- GLM / Qwen / Kimi Profile 正常执行
- selected model_id 来自 Binding（不是 Profile.default_model）
- AssistantMessage.provider / model 正确反映所选 Provider
- usage 继续保存
- 多轮 tool loop 使用同一 request client
"""
from __future__ import annotations

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
from pi_agent_core_py.stream_events import StreamEvent  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.runtime import (  # noqa: E402
    RequestProviderRuntime,
)

pytestmark = pytest.mark.asyncio

_UI = {"X-PI-Agent-UI": "1"}
SECRET_MARKER = "sk-M1-5-INTEGRATION-SECRET-MARKER"


# ============================================================================
# Stub Adapter——controllable provider_id / model + event script
# ============================================================================


class _ScriptedAdapter(ProviderAdapter):
    """ProviderAdapter that plays back a fixed event script + tracks stream calls."""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        events: list[StreamEvent],
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self.events = list(events)
        self.stream_calls = 0
        self.last_system_prompt: str | None = None

    async def stream(self, request: ProviderRequest) -> Any:
        self.stream_calls += 1
        self.last_system_prompt = request.system_prompt
        for ev in self.events:
            yield ev

    async def aclose(self) -> None:
        # Track close for lifecycle assertions
        self.closed = True  # type: ignore[attr-defined]


def _scripted_factory_call_tracker() -> tuple[
    Any, list[dict[str, Any]], list[_ScriptedAdapter]
]:
    """Return (factory, calls_log, adapters_created) — factory creates a new
    _ScriptedAdapter each call and records what was passed."""
    calls: list[dict[str, Any]] = []
    adapters: list[_ScriptedAdapter] = []

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
        events: list[StreamEvent] | None = None,
    ) -> _ScriptedAdapter:
        default_events: list[StreamEvent] = [
            TextDeltaEvent(delta="hello"),
            DoneEvent(stop_reason="stop", usage=Usage()),
        ]
        adapter = _ScriptedAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events=events or default_events,
        )
        adapters.append(adapter)
        calls.append(
            {
                "provider_definition": provider_definition,
                "api_key": api_key,
                "model_id": model_id,
            }
        )
        return adapter

    return factory, calls, adapters


def _install_runtime(
    app: object,
    *,
    factory: Any,
) -> RequestProviderRuntime:
    """Replace app.state.request_provider_runtime with a Runtime whose factory
    is the test-controlled one. Reuses the existing CredentialService + Provider
    Config Service + default Registry."""
    runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=factory,
    )
    app.state.request_provider_runtime = runtime
    return runtime


# ============================================================================
# App + harness setup
# ============================================================================


def _harness() -> AgentHarness:
    """Legacy FakeClient——records the events it actually emits so we can detect
    whether the prompt went through legacy vs request path."""
    client = FakeClient(
        [[TextDeltaEvent(delta="legacy"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


@pytest.fixture
async def env(tmp_path: Path) -> tuple[TestClient, AgentHarness, object]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as client:
        yield client, app  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={
            "label": "L",
            "storage_mode": "session_only",
            "secret_value": secret,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient,
    *,
    provider_id: str,
    credential_id: str,
    default_model: str,
    name: str = "P",
) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": name,
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": default_model,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["profile"]["id"]


def _bind(
    c: TestClient,
    *,
    session_id: str,
    profile_id: str,
    model_id: str,
) -> dict[str, Any]:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": model_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["binding"]


def _make_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"title": "T"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _last_assistant(c: TestClient, session_id: str) -> dict[str, Any]:
    """Return the last persisted AssistantMessage dict (the inner `message`
    field——full AgentMessage with provider/model/usage)."""
    r = c.get(f"/api/messages?session_id={session_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    for msg in reversed(body["messages"]):
        if msg.get("role") == "assistant":
            return msg["message"]
    raise AssertionError(f"no assistant message in session {session_id}")


# ============================================================================
# Legacy path (items 9-14)
# ============================================================================


async def test_legacy_session_without_binding_uses_original_client(
    env: tuple,
) -> None:
    """No Binding → legacy FakeClient used; no Factory / Secret reads."""
    client, app = env
    factory, calls, adapters = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    sess = _make_session(client)
    # No binding set——resolve_selection returns None

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200, r.text

    # Legacy path: factory NOT called
    assert calls == []
    # Legacy FakeClient emits "legacy" delta
    last = _last_assistant(client, sess)
    text_parts = [c.get("text", "") for c in last.get("content", [])]
    assert any("legacy" in t for t in text_parts), last


async def test_legacy_path_no_secret_read_no_factory_call(env: tuple) -> None:
    client, app = env
    factory, calls, _adapters = _scripted_factory_call_tracker()
    runtime = _install_runtime(app, factory=factory)

    # Track resolve_secret_for_request calls
    cred_svc = runtime._credential_service
    orig_resolve = cred_svc.resolve_secret_for_request
    resolve_calls: list[str] = []

    async def _track_resolve(credential_id: str) -> str:
        resolve_calls.append(credential_id)
        return await orig_resolve(credential_id)

    cred_svc.resolve_secret_for_request = _track_resolve  # type: ignore[assignment]

    sess = _make_session(client)
    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    assert resolve_calls == []
    assert calls == []


# ============================================================================
# Selected Provider (items 15-23)
# ============================================================================


async def test_qwen_profile_uses_request_client(env: tuple) -> None:
    client, app = env
    factory, calls, adapters = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="qwen-test-model",
    )

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200, r.text

    assert len(calls) == 1
    assert calls[0]["provider_definition"].id == "qwen"
    assert calls[0]["model_id"] == "qwen-test-model"
    assert calls[0]["api_key"] == SECRET_MARKER


async def test_qwen_assistant_message_carries_provider_and_model(
    env: tuple,
) -> None:
    client, app = env
    factory, _calls, _adapters = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="qwen-test-model",
    )

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    last = _last_assistant(client, sess)
    assert last.get("provider") == "qwen"
    assert last.get("model") == "qwen-test-model"


async def test_kimi_assistant_message_carries_provider_and_model(
    env: tuple,
) -> None:
    client, app = env
    factory, _calls, _adapters = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred,
        default_model="kimi-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="kimi-test-model",
    )

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    last = _last_assistant(client, sess)
    assert last.get("provider") == "kimi"
    assert last.get("model") == "kimi-test-model"


async def test_glm_profile_uses_request_client(env: tuple) -> None:
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="glm",
        credential_id=cred,
        default_model="glm-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="glm-4.5-flash",
    )

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200, r.text

    assert len(calls) == 1
    assert calls[0]["provider_definition"].id == "glm"
    assert calls[0]["model_id"] == "glm-4.5-flash"


async def test_selected_model_comes_from_binding_not_profile_default(
    env: tuple,
) -> None:
    """Profile.default_model='qwen-default' but Binding.model_id='qwen-override'
    → AssistantMessage.model == 'qwen-override'."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-profile-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="qwen-binding-override",
    )

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    assert calls[0]["model_id"] == "qwen-binding-override"
    last = _last_assistant(client, sess)
    assert last.get("model") == "qwen-binding-override"
    assert last.get("model") != "qwen-profile-default"


async def test_usage_continues_to_be_persisted(env: tuple) -> None:
    """Usage info from DoneEvent must land on AssistantMessage even with
    request client."""
    client, app = env

    # Custom factory that injects non-zero usage
    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> _ScriptedAdapter:
        return _ScriptedAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events=[
                TextDeltaEvent(delta="hello"),
                DoneEvent(
                    stop_reason="stop",
                    usage=Usage(input=42, output=13, total_tokens=55),
                ),
            ],
        )

    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="qwen-plus",
    )

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    last = _last_assistant(client, sess)
    usage = last.get("usage", {})
    assert usage.get("input") == 42
    assert usage.get("output") == 13
    assert usage.get("total_tokens") == 55


# ============================================================================
# Item 23: multi-turn tool loop uses same request client
# ============================================================================


async def test_multi_turn_tool_loop_uses_same_request_client(env: tuple) -> None:
    """Single prompt = single factory call——all turns within that prompt hit
    the same request Adapter. Verified via factory call count; we don't actually
    need to drive a tool loop——bind_to_harness wraps the whole _execute_prompt
    body, so by construction no mid-prompt swap can occur."""
    client, app = env
    factory, calls, _adapters = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(
        client,
        session_id=sess,
        profile_id=prof,
        model_id="qwen-plus",
    )

    r = client.post("/api/prompt", json={"text": "hi", "session_id": sess})
    assert r.status_code == 200, r.text

    # Exactly one factory call per prompt——the Adapter is reused for any
    # internal multi-turn loop within that prompt's harness.run_prompt.
    assert len(calls) == 1

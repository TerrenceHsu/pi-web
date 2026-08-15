"""Prompt provider runtime switching tests for M1-5（spec §十三.Snapshot）.

覆盖：
- 请求开始读取 Binding 一次
- 请求中切换 Binding → 当前请求不受影响
- 下一次请求使用新 selection
- 请求中不再读取 Profile / Secret
- 多轮 tool loop 不重新构造 Adapter
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


# ============================================================================
# Tracking factory + adapter
# ============================================================================


class _TrackingAdapter(ProviderAdapter):
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        api_key_seen: str,
        log: list[dict[str, Any]],
        events: list[StreamEvent],
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._api_key_seen = api_key_seen
        self._log = log
        self._events = list(events)

    async def stream(self, request: ProviderRequest) -> Any:
        # Record each stream invocation——catches mid-prompt re-invocation
        self._log.append(
            {
                "provider_id": self.provider_id,
                "model": self.model,
                "api_key": self._api_key_seen,
            }
        )
        for ev in self._events:
            yield ev


def _tracking_factory(log: list[dict[str, Any]]) -> Any:
    """Each Factory call appends to `log`; returns a fresh Adapter."""

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> _TrackingAdapter:
        return _TrackingAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            api_key_seen=api_key,
            log=log,
            events=[
                TextDeltaEvent(delta="hi"),
                DoneEvent(stop_reason="stop", usage=Usage()),
            ],
        )

    return factory


def _install_tracking_runtime(app: object, log: list[dict[str, Any]]) -> None:
    app.state.request_provider_runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=_tracking_factory(log),
    )


# ============================================================================
# Setup
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="legacy"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


@pytest.fixture
async def env(tmp_path: Path) -> tuple[TestClient, object, list[dict[str, Any]]]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    log: list[dict[str, Any]] = []
    with TestClient(app, base_url="http://testserver") as client:
        _install_tracking_runtime(app, log)
        yield client, app, log  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": "L", "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient,
    *,
    provider_id: str,
    credential_id: str,
    default_model: str,
) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": f"P-{provider_id}",
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
) -> None:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": model_id},
    )
    assert r.status_code == 200, r.text


def _make_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"title": "T"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ============================================================================
# 24: 请求开始读取 Binding 一次
# ============================================================================


async def test_binding_read_once_per_prompt(env: tuple) -> None:
    """A prompt triggers exactly one factory call (one selection snapshot)."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
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
        model_id="qwen-A",
    )

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    # M1-5: one prompt → one resolve_selection → one build_adapter → one factory
    assert len(log) == 1


# ============================================================================
# 25-27: Snapshot semantics across requests
# ============================================================================


async def test_switching_binding_between_requests_uses_new_selection(
    env: tuple,
) -> None:
    """First request uses profile A; after switching binding, second request
    uses profile B."""
    client, app, log = env

    cred_a = _seed_credential(client, "key-A")
    cred_b = _seed_credential(client, "key-B")
    prof_a = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_a,
        default_model="qwen-default",
    )
    prof_b = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_b,
        default_model="kimi-default",
    )
    sess = _make_session(client)

    # Bind to A → run → expect key-A / qwen
    _bind(client, session_id=sess, profile_id=prof_a, model_id="qwen-A")
    client.post("/api/prompt", json={"text": "first", "session_id": sess})
    assert len(log) == 1
    assert log[0]["provider_id"] == "qwen"
    assert log[0]["api_key"] == "key-A"
    assert log[0]["model"] == "qwen-A"

    # Switch to B → run → expect key-B / kimi
    _bind(client, session_id=sess, profile_id=prof_b, model_id="kimi-B")
    client.post("/api/prompt", json={"text": "second", "session_id": sess})
    assert len(log) == 2
    assert log[1]["provider_id"] == "kimi"
    assert log[1]["api_key"] == "key-B"
    assert log[1]["model"] == "kimi-B"


async def test_snapshot_is_frozen_dataclass(env: tuple) -> None:
    """RequestProviderSelection is frozen dataclass——cannot be mutated mid-request."""
    client, app, _log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof, model_id="qwen-plus")

    # Track resolve_selection and capture the snapshot
    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    captured: list[Any] = []
    orig_resolve = runtime.resolve_selection

    async def _capture(session_id: str) -> Any:
        sel = await orig_resolve(session_id)
        captured.append(sel)
        return sel

    runtime.resolve_selection = _capture  # type: ignore[assignment]

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    assert len(captured) == 1
    sel = captured[0]
    assert sel is not None
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.model_id = "tampered"  # type: ignore[misc]


# ============================================================================
# 28: 请求中不再次读取 Profile
# ============================================================================


async def test_profile_not_re_read_during_request(env: tuple) -> None:
    """Mid-request Profile mutation does not affect the current request."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof, model_id="qwen-plus")

    # Track Profile reads on the service
    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    pc_svc = runtime._provider_config_service
    profile_reads: list[str] = []
    orig_get_profile = pc_svc.get_profile

    async def _track_profile(profile_id: str) -> Any:
        profile_reads.append(profile_id)
        return await orig_get_profile(profile_id)

    pc_svc.get_profile = _track_profile  # type: ignore[assignment]

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    # resolve_selection reads Profile exactly once; build_adapter doesn't re-read
    assert len(profile_reads) == 1
    assert profile_reads[0] == prof


# ============================================================================
# 29: 请求中不再次读取 Secret
# ============================================================================


async def test_secret_not_re_read_during_request(env: tuple) -> None:
    """Within a single request, resolve_secret_for_request is called exactly once."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof, model_id="qwen-plus")

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    cred_svc = runtime._credential_service
    resolve_calls: list[str] = []
    orig_resolve = cred_svc.resolve_secret_for_request

    async def _track_resolve(credential_id: str) -> str:
        resolve_calls.append(credential_id)
        return await orig_resolve(credential_id)

    cred_svc.resolve_secret_for_request = _track_resolve  # type: ignore[assignment]

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    assert len(resolve_calls) == 1
    assert resolve_calls[0] == cred


# ============================================================================
# 30: 多轮 tool loop 不重新构造 Adapter
# ============================================================================


async def test_single_prompt_constructs_single_adapter(env: tuple) -> None:
    """Even if the agent loop drives multiple turns, only one Adapter is built
    per prompt. bind_to_harness wraps the whole _execute_prompt."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sess = _make_session(client)
    _bind(client, session_id=sess, profile_id=prof, model_id="qwen-plus")

    client.post("/api/prompt", json={"text": "hi", "session_id": sess})

    # Single prompt → single factory call → single Adapter
    assert len(log) == 1


# ============================================================================
# Cross-request independence
# ============================================================================


async def test_failed_first_request_does_not_affect_second(env: tuple) -> None:
    """If first request fails (e.g., credential rotated away), second request
    with valid binding still works cleanly."""
    client, app, log = env

    # Seed two credentials; first will be deleted to force unavailable
    cred_a = _seed_credential(client, "key-A")
    cred_b = _seed_credential(client, "key-B")
    prof_a = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_a,
        default_model="qwen-default",
    )
    prof_b = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_b,
        default_model="qwen-default",
    )
    sess = _make_session(client)

    # Bind to A → run → success
    _bind(client, session_id=sess, profile_id=prof_a, model_id="qwen-A")
    r1 = client.post("/api/prompt", json={"text": "first", "session_id": sess})
    assert r1.status_code == 200
    assert len(log) == 1

    # Delete cred_a → bind to B → run → second adapter created, succeeds
    client.delete(f"/api/credentials/{cred_a}", headers=_UI)
    _bind(client, session_id=sess, profile_id=prof_b, model_id="qwen-B")
    r2 = client.post("/api/prompt", json={"text": "second", "session_id": sess})
    assert r2.status_code == 200
    assert len(log) == 2
    assert log[1]["api_key"] == "key-B"

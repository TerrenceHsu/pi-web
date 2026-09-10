"""Endpoint-scoped model limits must not change persisted provider identity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py import Agent, AgentHarness, DoneEvent, ModelClient, TextDeltaEvent
from pi_agent_core_py.ai.providers.endpoints import capability_provider_id
from pi_agent_core_py.ai.providers.fake import FakeProviderAdapter
from pi_agent_core_py.web.app import create_app

_CUSTOM_URL = "https://gateway.example.test/v1"
_MODEL = "qwen-plus"


def _model_client():
    adapter = FakeProviderAdapter(
        [[TextDeltaEvent(delta="finished"), DoneEvent(stop_reason="stop")]], model=_MODEL,
    )
    adapter.provider_id = "qwen"
    adapter.capability_provider_id = capability_provider_id(
        "qwen", "openai_compatible", _CUSTOM_URL,
    )
    return ModelClient(adapter), adapter


def test_model_client_freezes_capability_scope_separately_from_provider_identity():
    model, adapter = _model_client()
    scope = model.capability_provider_id
    adapter.capability_provider_id = "custom:changed-after-construction"
    assert scope.startswith("custom:")
    assert model.capability_provider_id == scope
    assert model.provider_id == "qwen"


@pytest.mark.parametrize("scope", [None, "", 12])
def test_model_client_without_valid_endpoint_scope_keeps_legacy_provider(scope):
    adapter = FakeProviderAdapter([])
    adapter.capability_provider_id = scope
    assert ModelClient(adapter).capability_provider_id == "fake"


def test_unmodified_legacy_adapter_does_not_require_capability_scope():
    assert ModelClient(FakeProviderAdapter([])).capability_provider_id == "fake"


def test_live_context_preparation_and_admission_use_frozen_scope(tmp_path, monkeypatch):
    model, adapter = _model_client()
    scope = model.capability_provider_id
    app = create_app(
        AgentHarness(Agent(system_prompt="system", client=model)),
        db_path=tmp_path / "live.sqlite",
    )
    with TestClient(app) as client:
        sid = client.get("/api/sessions").json()["sessions"][0]["id"]
        store = app.state.web.model_capability_store

        async def initialize():
            await store.upsert(
                provider_id="qwen", model_id=_MODEL,
                context_window=1024, max_output_tokens=256,
            )
            await store.upsert(
                provider_id=scope, model_id=_MODEL,
                context_window=20000, max_output_tokens=256,
            )

        client.portal.call(initialize)
        preview = client.get(f"/api/sessions/{sid}/context-budget")
        assert preview.status_code == 200, preview.text
        assert preview.json()["estimate"]["context_window"] == 20000
        resolved = []
        prepared = []
        original_resolve = store.resolve
        original_prepare = app.state.context_management.prepare

        async def tracked_resolve(provider_id, model_id):
            resolved.append((provider_id, model_id))
            return await original_resolve(provider_id, model_id)

        async def tracked_prepare(*args, **kwargs):
            prepared.append(kwargs["context_window"])
            return await original_prepare(*args, **kwargs)

        monkeypatch.setattr(store, "resolve", tracked_resolve)
        monkeypatch.setattr(app.state.context_management, "prepare", tracked_prepare)
        # Mutating an adapter after selection must not alter the ModelClient snapshot.
        adapter.capability_provider_id = "qwen"
        response = client.post("/api/prompt", json={"session_id": sid, "text": "Inspect"})
        assert response.status_code == 200, response.text
        assert prepared == [20000]
        assert len(resolved) >= 2  # preparation and final before-model admission
        assert set(resolved) == {(scope, _MODEL)}
        assert len(adapter.all_messages_calls) == 1
        messages = client.get(f"/api/messages?session_id={sid}").json()["messages"]
        assistant = next(message for message in messages if message["role"] == "assistant")
        assert assistant["message"]["provider"] == "qwen"


def test_read_only_preview_scopes_public_profile_without_credential_resolution(tmp_path):
    model, _ = _model_client()
    app = create_app(
        AgentHarness(Agent(system_prompt="system", client=model)),
        db_path=tmp_path / "preview.sqlite",
    )
    with TestClient(app) as client:
        sid = client.get("/api/sessions").json()["sessions"][0]["id"]
        store = app.state.web.model_capability_store
        other_url = "https://other.example.test/v1"
        other_scope = capability_provider_id("qwen", "openai_compatible", other_url)

        async def initialize():
            for provider_id, window in (
                ("qwen", 4096), (model.capability_provider_id, 10000), (other_scope, 30000),
            ):
                await store.upsert(
                    provider_id=provider_id, model_id=_MODEL,
                    context_window=window, max_output_tokens=256,
                )

        async def get_binding(session_id):
            assert session_id == sid
            return SimpleNamespace(profile_id="profile", model_id=_MODEL)

        async def get_profile(profile_id):
            assert profile_id == "profile"
            return SimpleNamespace(
                provider_id="qwen", api_style="openai_compatible", base_url=other_url,
            )

        client.portal.call(initialize)
        original_runtime = app.state.provider_config_runtime
        app.state.provider_config_runtime = SimpleNamespace(service=SimpleNamespace(
            get_session_binding=get_binding, get_profile=get_profile,
        ))
        try:
            response = client.get(f"/api/sessions/{sid}/context-budget")
        finally:
            app.state.provider_config_runtime = original_runtime
        assert response.status_code == 200, response.text
        assert response.json()["provider_id"] == "qwen"
        assert response.json()["estimate"]["context_window"] == 30000


def test_custom_scope_still_enforces_final_context_hard_stop(tmp_path):
    model, _ = _model_client()
    app = create_app(
        AgentHarness(Agent(system_prompt="system", client=model)),
        db_path=tmp_path / "admission.sqlite",
    )
    with TestClient(app) as client:
        async def check_admission():
            store = app.state.web.model_capability_store
            await store.upsert(
                provider_id=model.capability_provider_id, model_id=_MODEL,
                context_window=1024, max_output_tokens=256,
            )
            await store.upsert(
                provider_id="qwen", model_id=_MODEL,
                context_window=100000, max_output_tokens=256,
            )
            return await app.state.web_before_model_call(SimpleNamespace(
                client=model, system_prompt="oversized context " * 2000,
                messages=[], tools=[], turn_index=0,
            ))

        decision = client.portal.call(check_admission)
        assert decision.allow is False
        assert "context budget exceeded" in decision.error_message

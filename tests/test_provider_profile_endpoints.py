"""Custom protocol/endpoints keep Profile identity and legacy defaults intact.

These API tests use synthetic credentials, FakeClient, and temporary SQLite.
No provider or credential-validation request is sent to a network endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from pi_agent_core_py.ai.providers.registry import get_provider_definition  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402

_HEADERS = {"X-PI-Agent-UI": "1"}
_SECRET = "sk-profile-endpoint-test-marker"


def _test_client(db_path: Path) -> TestClient:
    model = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    app = create_app(
        AgentHarness(Agent(system_prompt="base", client=model)),
        db_path=str(db_path),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
    )
    return TestClient(app, base_url="http://testserver")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _test_client(tmp_path / "endpoints.db") as test_client:
        yield test_client


def _credential(client: TestClient) -> str:
    response = client.post(
        "/api/credentials",
        headers=_HEADERS,
        json={
            "label": "Synthetic endpoint credential",
            "storage_mode": "session_only",
            "secret_value": _SECRET,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["credential"]["credential_id"])


def _payload(client: TestClient, provider_id: str = "openai_compatible") -> dict[str, Any]:
    return {
        "name": "Custom endpoint",
        "provider_id": provider_id,
        "credential_id": _credential(client),
        "default_model": "custom-model",
    }


def _profiles(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/provider-profiles", headers=_HEADERS)
    assert response.status_code == 200, response.text
    assert _SECRET not in response.text
    return response.json()["profiles"]


def test_custom_capability_scope_is_internal_and_does_not_cross_endpoints(
    client: TestClient,
) -> None:
    ids = []
    for host in ("first", "second"):
        payload = _payload(client)
        payload["base_url"] = f"https://{host}.example.test/v1"
        created = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
        assert created.status_code == 201, created.text
        ids.append(created.json()["profile"]["id"])
    endpoint = f"/api/provider-profiles/{ids[0]}/model-capabilities"
    saved = client.put(endpoint, headers=_HEADERS, json={
        "model_id": "custom-model", "context_window": 20000, "max_output_tokens": 2000,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["capabilities"]["provider_id"] == "openai_compatible"
    assert "custom:" not in saved.text
    for profile_id, expected_window in zip(ids, (20000, None), strict=True):
        response = client.get(
            f"/api/provider-profiles/{profile_id}/model-capabilities",
            headers=_HEADERS, params={"model_id": "custom-model"},
        )
        assert response.status_code == 200, response.text
        capabilities = response.json()["capabilities"]
        assert capabilities["provider_id"] == "openai_compatible"
        assert capabilities["context_window"] == expected_window
    changed = client.patch(f"/api/provider-profiles/{ids[0]}", headers=_HEADERS, json={
        "base_url": "https://changed.example.test/v1",
    })
    assert changed.status_code == 200
    after = client.get(endpoint, headers=_HEADERS, params={"model_id": "custom-model"})
    assert after.json()["capabilities"]["context_window"] is None
    assert after.json()["capabilities"]["source"] == "unknown"


@pytest.mark.parametrize("provider_id", ["openai_compatible", "anthropic_compatible"])
def test_generic_profile_requires_explicit_base_url(
    client: TestClient, provider_id: str,
) -> None:
    response = client.post(
        "/api/provider-profiles", headers=_HEADERS, json=_payload(client, provider_id)
    )
    assert response.status_code in (400, 422), response.text
    assert _profiles(client) == []


@pytest.mark.parametrize("provider_id", ["openai_compatible", "anthropic_compatible"])
def test_generic_profile_exposes_protocol_and_endpoint(
    client: TestClient, provider_id: str,
) -> None:
    payload = _payload(client, provider_id)
    payload["base_url"] = "https://provider.example.test/api/v1"
    response = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    assert response.status_code == 201, response.text
    profile = response.json()["profile"]
    assert profile["api_style"] == provider_id
    assert profile["base_url"] == payload["base_url"]
    assert profile["credential_id"] == payload["credential_id"]
    assert _profiles(client) == [profile]


def test_profile_rejects_unknown_api_protocol(client: TestClient) -> None:
    payload = _payload(client)
    payload.update(api_style="unsupported-protocol", base_url="https://provider.example.test/v1")
    response = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    assert response.status_code in (400, 422), response.text
    assert _profiles(client) == []


@pytest.mark.parametrize("provider_id", ["anthropic", "glm", "qwen", "kimi"])
def test_legacy_profile_returns_registry_defaults_without_new_fields(
    client: TestClient, provider_id: str,
) -> None:
    response = client.post(
        "/api/provider-profiles", headers=_HEADERS, json=_payload(client, provider_id)
    )
    assert response.status_code == 201, response.text
    definition = get_provider_definition(provider_id)
    assert definition is not None
    profile = response.json()["profile"]
    assert profile["api_style"] == definition.api_style
    assert profile["base_url"] == definition.default_base_url
    assert _profiles(client) == [profile]


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.example.test/v1",
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_custom_endpoint_accepts_https_and_loopback_http(
    client: TestClient, base_url: str,
) -> None:
    payload = _payload(client)
    payload.update(api_style="openai_compatible", base_url=base_url)
    response = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["profile"]["base_url"] == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "http://provider.example.test/v1",
        "http://192.168.1.20/v1",
        "http://localhost.example.test/v1",
        "https://user:do-not-echo@provider.example.test/v1",
        "https://provider.example.test/v1?token=do-not-echo",
        "https://provider.example.test/v1#do-not-echo",
        "https://provider.example.test/\nunsafe",
        "https://provider.example.test/\tunsafe",
        "file:///etc/passwd",
    ],
)
def test_custom_endpoint_rejection_is_safe_and_non_mutating(
    client: TestClient, base_url: str,
) -> None:
    payload = _payload(client, "qwen")
    created = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    assert created.status_code == 201, created.text
    original = created.json()["profile"]

    # Both create and edit use the same restriction, without echoing input.
    payload["base_url"] = base_url
    rejected_create = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    rejected_update = client.patch(
        f"/api/provider-profiles/{original['id']}",
        headers=_HEADERS,
        json={"base_url": base_url},
    )
    for response in (rejected_create, rejected_update):
        assert response.status_code in (400, 422), response.text
        assert "do-not-echo" not in response.text
        assert _SECRET not in response.text
    assert _profiles(client) == [original]


def test_legacy_profile_edit_changes_protocol_not_identity_or_binding(
    client: TestClient,
) -> None:
    payload = _payload(client, "qwen")
    created = client.post("/api/provider-profiles", headers=_HEADERS, json=payload)
    assert created.status_code == 201, created.text
    original = created.json()["profile"]
    session_response = client.post("/api/sessions", headers=_HEADERS, json={"title": "S"})
    assert session_response.status_code == 200, session_response.text
    session_id = session_response.json()["id"]
    bound = client.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_HEADERS,
        json={"profile_id": original["id"], "model_id": "custom-model"},
    )
    assert bound.status_code == 200, bound.text

    response = client.patch(
        f"/api/provider-profiles/{original['id']}",
        headers=_HEADERS,
        json={
            "api_style": "anthropic_compatible",
            "base_url": "https://gateway.example.test/anthropic",
        },
    )
    assert response.status_code == 200, response.text
    changed = response.json()["profile"]
    assert changed["api_style"] == "anthropic_compatible"
    assert changed["base_url"] == "https://gateway.example.test/anthropic"
    for field in ("id", "provider_id", "credential_id", "created_at"):
        assert changed[field] == original[field]
    assert client.get(
        f"/api/sessions/{session_id}/model-binding", headers=_HEADERS
    ).json() == bound.json()

    forbidden = client.patch(
        f"/api/provider-profiles/{original['id']}",
        headers=_HEADERS,
        json={"provider_id": "anthropic_compatible"},
    )
    assert forbidden.status_code == 422, forbidden.text
    assert _profiles(client) == [changed]


def test_custom_endpoint_survives_restart_without_overwriting_legacy(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restart.db"
    with _test_client(db_path) as first:
        legacy_response = first.post(
            "/api/provider-profiles", headers=_HEADERS, json=_payload(first, "kimi")
        )
        assert legacy_response.status_code == 201, legacy_response.text
        legacy = legacy_response.json()["profile"]
        payload = _payload(first)
        payload.update(
            api_style="anthropic_compatible",
            base_url="https://gateway.example.test/messages-api",
        )
        custom_response = first.post("/api/provider-profiles", headers=_HEADERS, json=payload)
        assert custom_response.status_code == 201, custom_response.text
        custom = custom_response.json()["profile"]

    with _test_client(db_path) as restarted:
        listed = {profile["id"]: profile for profile in _profiles(restarted)}
        assert set(listed) == {legacy["id"], custom["id"]}
        # Session-only secrets need re-entry after restart; configuration does not.
        for original in (legacy, custom):
            for field in (
                "name", "provider_id", "credential_id", "default_model",
                "api_style", "base_url", "created_at", "updated_at",
            ):
                assert listed[original["id"]][field] == original[field]

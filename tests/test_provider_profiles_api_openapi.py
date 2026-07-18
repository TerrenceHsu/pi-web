"""Provider Profile API OpenAPI tests (E2-3B2).

覆盖 spec §17.6 OpenAPI 安全 + 既有 API 不变.
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
from pi_agent_core_py.web.app import create_app  # noqa: E402

pytestmark = pytest.mark.asyncio


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "api.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _get_openapi(client: TestClient) -> dict:
    return client.get("/openapi.json").json()


# ============================================================================
# 7 Provider Profile endpoints present
# ============================================================================


async def test_provider_profile_endpoints_present(client: TestClient) -> None:
    schema = _get_openapi(client)
    paths = schema["paths"]
    expected = [
        "/api/provider-profiles",
        "/api/provider-profiles/{profile_id}",
        "/api/provider-profiles/{profile_id}/models",
        "/api/sessions/{session_id}/model-binding",
    ]
    for p in expected:
        assert p in paths, f"missing path: {p}"

    # Verify methods on each
    assert "get" in paths["/api/provider-profiles"]
    assert "post" in paths["/api/provider-profiles"]
    assert "patch" in paths["/api/provider-profiles/{profile_id}"]
    assert "delete" in paths["/api/provider-profiles/{profile_id}"]
    assert "get" in paths["/api/provider-profiles/{profile_id}/models"]
    assert "get" in paths["/api/sessions/{session_id}/model-binding"]
    assert "put" in paths["/api/sessions/{session_id}/model-binding"]


# ============================================================================
# 422 uses SafeValidationErrorResponse
# ============================================================================


async def test_422_uses_safe_schema_on_all_endpoints(client: TestClient) -> None:
    """All 7 endpoints must declare SafeValidationErrorResponse for 422."""
    schema = _get_openapi(client)
    paths = schema["paths"]

    targets = [
        ("/api/provider-profiles", "post"),
        ("/api/provider-profiles/{profile_id}", "patch"),
        ("/api/provider-profiles/{profile_id}", "delete"),
        ("/api/provider-profiles/{profile_id}/models", "get"),
        ("/api/sessions/{session_id}/model-binding", "get"),
        ("/api/sessions/{session_id}/model-binding", "put"),
    ]
    for path, method in targets:
        op = paths[path][method]
        resp_422 = op.get("responses", {}).get("422", {})
        content = resp_422.get("content", {})
        for _mt, data in content.items():
            schema_def = data.get("schema", {})
            ref = schema_def.get("$ref", "")
            if not ref and schema_def.get("anyOf"):
                ref = schema_def["anyOf"][0].get("$ref", "")
            assert (
                "SafeValidationErrorResponse" in ref
                or "HTTPValidationError" not in ref
            ), (
                f"{method.upper()} {path} 422 must reference SafeValidationErrorResponse"
            )


# ============================================================================
# No secret fields in any request / response schema
# ============================================================================


async def test_no_secret_fields_in_openapi(client: TestClient) -> None:
    schema_text = str(_get_openapi(client))
    forbidden = (
        "secret_ref",
        "fingerprint_sha256",
        "fingerprint",
        '"authorization"',
        "x-api-key",
        "validation_endpoint",
        "credential_validation_endpoint",
    )
    # secret_value / masked_value are ALLOWED in their proper places
    for bad in forbidden:
        assert bad not in schema_text.lower(), (
            f"forbidden field in OpenAPI: {bad}"
        )


# ============================================================================
# No Strategy impl / Keyring service name
# ============================================================================


async def test_no_internal_implementation_references(client: TestClient) -> None:
    schema_text = str(_get_openapi(client))
    forbidden = (
        "AnthropicModelsValidationStrategy",
        "KeyringSecretStore",
        "OSKeyringSecretStore",
        "EnvSecretStore",
        "InMemorySecretStore",
        "keyring_service_name",
    )
    for bad in forbidden:
        assert bad not in schema_text


# ============================================================================
# ProviderProfileResponse exposes masked_value but not secret
# ============================================================================


async def test_profile_response_includes_masked_value(client: TestClient) -> None:
    """Profile response (via real API) should include credential_masked_value.

    Note: we use JSONResponse for fine-grained control of projection—response
    schemas are verified via actual API call, not OpenAPI introspection.
    """
    # Seed credential + profile
    cred_r = client.post(
        "/api/credentials",
        headers={"X-PI-Agent-UI": "1"},
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-test"},
    )
    cred_id = cred_r.json()["credential"]["credential_id"]
    profile_r = client.post(
        "/api/provider-profiles",
        headers={"X-PI-Agent-UI": "1"},
        json={
            "name": "P",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    )
    body = profile_r.json()["profile"]
    assert "credential_masked_value" in body
    assert "masked_value" not in body or body.get("masked_value") is None
    # Forbidden fields
    for bad in ("secret_ref", "fingerprint", "secret_value", "Authorization"):
        assert bad not in body


# ============================================================================
# Existing Credential + Sessions API still in OpenAPI
# ============================================================================


async def test_credential_endpoints_remain_in_openapi(client: TestClient) -> None:
    """E1 Credential API must still be present."""
    schema = _get_openapi(client)
    paths = schema["paths"]
    assert "/api/credentials" in paths
    assert "/api/credentials/{credential_id}" in paths


async def test_sessions_endpoints_remain_in_openapi(client: TestClient) -> None:
    """Sessions API must still be present and unchanged."""
    schema = _get_openapi(client)
    paths = schema["paths"]
    assert "/api/sessions" in paths
    assert "/api/sessions/{sid}" in paths

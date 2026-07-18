"""Provider Profile API security tests (E2-3B2).

覆盖 spec §17.6 Security 矩阵.
"""
from __future__ import annotations

import json
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
        db_path=str(tmp_path / "sec.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _ui_headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _seed_credential(c: TestClient) -> str:
    r = c.post(
        "/api/credentials",
        headers=_ui_headers(),
        json={
            "label": "L",
            "storage_mode": "session_only",
            "secret_value": "PI_E2_TEST_MARKER_5C8E27B4",
        },
    )
    assert r.status_code == 201
    return r.json()["credential"]["credential_id"]


def _seed_profile(c: TestClient, cred_id: str, name: str = "P") -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": name,
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 201
    return r.json()["profile"]["id"]


# ============================================================================
# Host validation
# ============================================================================


async def test_invalid_host_rejected_without_body_buffer(
    tmp_path: Path,
) -> None:
    """Invalid Host must be rejected BEFORE body is buffered (TrustedHost outermost)."""
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "host.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),  # only testserver allowed
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        # Send POST with invalid Host + oversized body — expect 400 (TrustedHost)
        # not 413 (body limit). This verifies TrustedHost is outermost.
        r = c.post(
            "/api/provider-profiles",
            headers={**_ui_headers(), "Host": "evil.example.com"},
            json={"name": "x" * 50000, "provider_id": "anthropic",
                  "credential_id": "cred-x", "default_model": "claude-X"},
        )
        assert r.status_code == 400  # TrustedHost rejection


# ============================================================================
# Origin validation
# ============================================================================


async def test_invalid_origin_rejected(client: TestClient) -> None:
    r = client.get(
        "/api/provider-profiles",
        headers={**_ui_headers(), "Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


async def test_origin_null_rejected(client: TestClient) -> None:
    r = client.get(
        "/api/provider-profiles",
        headers={**_ui_headers(), "Origin": "null"},
    )
    assert r.status_code == 403


# ============================================================================
# UI header validation
# ============================================================================


async def test_missing_ui_header_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/provider-profiles",
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": "cred-x",
            "default_model": "claude-X",
        },
        # No X-PI-Agent-UI header
    )
    assert r.status_code == 400


async def test_wrong_ui_header_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/provider-profiles",
        headers={"X-PI-Agent-UI": "0"},  # wrong value
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": "cred-x",
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 400


# ============================================================================
# Body limit
# ============================================================================


async def test_oversized_body_rejected_413(client: TestClient) -> None:
    """Body > 32 KiB → 413."""
    huge_name = "x" * 40000
    r = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": huge_name,
            "provider_id": "anthropic",
            "credential_id": "cred-x",
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 413


async def test_413_does_not_echo_body(client: TestClient) -> None:
    """413 response must not echo the oversized body."""
    secret_marker = "PI_E2_TEST_MARKER_BODY_5C8E"
    r = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "x" * 35000 + secret_marker,
            "provider_id": "anthropic",
            "credential_id": "cred-x",
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 413
    assert secret_marker not in r.text


# ============================================================================
# Safe 422 validation
# ============================================================================


async def test_safe_422_no_input_ctx_url_msg(client: TestClient) -> None:
    r = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": "cred-x",
            # missing default_model
        },
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "request_validation_failed"
    fields = body["error"]["fields"]
    # No input / ctx / url / msg in field entries
    for field in fields:
        assert "input" not in field
        assert "ctx" not in field
        assert "url" not in field
        assert "msg" not in field


# ============================================================================
# Response does not leak secret_ref / fingerprint
# ============================================================================


async def test_response_has_no_secret_ref_or_fingerprint(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = _seed_profile(client, cred_id)

    client.get(
        f"/api/provider-profiles/{pid}/models", headers=_ui_headers()
    )
    # Also check list
    listed = client.get(
        "/api/provider-profiles", headers=_ui_headers()
    ).json()
    text = json.dumps(listed)
    assert "secret_ref" not in text
    assert "fingerprint" not in text
    assert "PI_E2_TEST_MARKER_5C8E27B4" not in text


# ============================================================================
# Logs do not leak credential / model data
# ============================================================================


async def test_error_message_does_not_leak_credential_id(
    client: TestClient,
) -> None:
    """credential_not_found error must NOT echo the credential_id."""
    secret_marker = "PI_E2_TEST_MARKER_CRED_INPUT"
    r = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": secret_marker,
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 409
    assert secret_marker not in r.text


# ============================================================================
# Existing API behavior unchanged
# ============================================================================


async def test_existing_session_api_remains_functional(
    client: TestClient,
) -> None:
    """Sessions API continues to work post-E2-3B2 mount (no regression)."""
    r = client.post("/api/sessions", json={"title": "S"})
    assert r.status_code in (200, 201)
    sess_id = r.json()["id"]

    # List still works
    listed = client.get("/api/sessions").json()
    assert any(s["id"] == sess_id for s in listed["sessions"])

    # Provider Profile API also works alongside
    cred_r = client.post(
        "/api/credentials",
        headers=_ui_headers(),
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-x"},
    )
    assert cred_r.status_code == 201


# ============================================================================
# E2 marker scan: production source has no API Key constants
# ============================================================================


def test_provider_profiles_api_source_no_api_key_literals() -> None:
    """Source must NOT contain HTTP header / api key constants."""
    import inspect

    from pi_agent_core_py.web import provider_profiles_api as mod

    src = inspect.getsource(mod)
    forbidden = (
        '"Authorization"',
        "'Authorization'",
        '"x-api-key"',
        "'x-api-key'",
        '"Bearer "',
    )
    for lit in forbidden:
        assert lit not in src

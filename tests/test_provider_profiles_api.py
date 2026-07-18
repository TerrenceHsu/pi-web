"""Provider Profile REST API tests (E2-3B2).

覆盖 spec §17.2-17.4 Profile / Models / Binding 矩阵.
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


# ============================================================================
# Fixtures
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    """Build app + client with both Credential + Provider Profile APIs enabled."""
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "api.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _seed_credential(c: TestClient, cid: str = "cred-A") -> str:
    """Create a credential via E1 API and return its id."""
    r = c.post(
        "/api/credentials",
        headers={"X-PI-Agent-UI": "1"},
        json={
            "label": f"Label-{cid}",
            "storage_mode": "session_only",
            "secret_value": "sk-test-marker",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


# ============================================================================
# Profile list / create
# ============================================================================


async def test_list_empty(client: TestClient) -> None:
    r = client.get("/api/provider-profiles", headers=_headers())
    assert r.status_code == 200
    assert r.json() == {"profiles": []}


async def test_create_anthropic_profile(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    r = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "Work Anthropic",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()["profile"]
    assert body["provider_id"] == "anthropic"
    assert body["provider_display_name"] == "Anthropic"
    assert body["status"] == "ready"
    assert body["credential_masked_value"] is not None
    assert "secret_ref" not in body
    assert "fingerprint" not in body


async def test_create_glm_profile(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    r = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "GLM",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
        },
    )
    assert r.status_code == 201
    body = r.json()["profile"]
    assert body["provider_id"] == "glm"
    assert body["provider_display_name"] == "Zhipu GLM (Anthropic-compatible)"


async def test_create_unknown_provider_rejected(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    r = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "Bad",
            "provider_id": "openai",  # not registered
            "credential_id": cred_id,
            "default_model": "gpt-4",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_provider"


async def test_create_missing_credential_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "Bad",
            "provider_id": "anthropic",
            "credential_id": "cred-nonexistent",
            "default_model": "claude-X",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "credential_not_found"


async def test_provider_id_immutable_via_extra_field_422(
    client: TestClient,
) -> None:
    """Update request must NOT accept provider_id field—extra='forbid' 422."""
    cred_id = _seed_credential(client)
    create_r = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    )
    profile_id = create_r.json()["profile"]["id"]

    # PATCH with provider_id field → 422 (extra forbidden)
    r = client.patch(
        f"/api/provider-profiles/{profile_id}",
        headers=_headers(),
        json={"provider_id": "glm"},
    )
    assert r.status_code == 422


# ============================================================================
# Profile update / delete
# ============================================================================


async def test_update_name(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "Old",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    r = client.patch(
        f"/api/provider-profiles/{pid}",
        headers=_headers(),
        json={"name": "New Name"},
    )
    assert r.status_code == 200
    assert r.json()["profile"]["name"] == "New Name"


async def test_update_default_model(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-old",
        },
    ).json()["profile"]["id"]

    r = client.patch(
        f"/api/provider-profiles/{pid}",
        headers=_headers(),
        json={"default_model": "claude-new"},
    )
    assert r.status_code == 200
    assert r.json()["profile"]["default_model"] == "claude-new"


async def test_update_credential_must_exist(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    r = client.patch(
        f"/api/provider-profiles/{pid}",
        headers=_headers(),
        json={"credential_id": "cred-nonexistent"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "credential_not_found"


async def test_switch_default(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "A",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "is_default": True,
        },
    ).json()["profile"]
    p2 = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "B",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
        },
    ).json()["profile"]

    r = client.patch(
        f"/api/provider-profiles/{p2['id']}",
        headers=_headers(),
        json={"is_default": True},
    )
    assert r.status_code == 200
    assert r.json()["profile"]["is_default"] is True

    # p1 should no longer be default
    listed = client.get(
        "/api/provider-profiles", headers=_headers()
    ).json()["profiles"]
    defaults = [p for p in listed if p["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == p2["id"]


async def test_disable_default_auto_clears(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "is_default": True,
        },
    ).json()["profile"]["id"]

    r = client.patch(
        f"/api/provider-profiles/{pid}",
        headers=_headers(),
        json={"enabled": False},
    )
    assert r.status_code == 200
    body = r.json()["profile"]
    assert body["enabled"] is False
    assert body["is_default"] is False


async def test_delete_unused_profile(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    r = client.delete(
        f"/api/provider-profiles/{pid}", headers=_headers()
    )
    assert r.status_code == 204


async def test_delete_in_use_returns_409(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    # Create a session + binding first
    sess = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()
    sess_id = sess["id"]
    client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_headers(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )

    r = client.delete(
        f"/api/provider-profiles/{pid}", headers=_headers()
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "profile_in_use"


async def test_list_shows_needs_credential_after_credential_deleted(
    client: TestClient,
) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    # Delete the credential
    client.delete(f"/api/credentials/{cred_id}", headers=_headers())

    listed = client.get(
        "/api/provider-profiles", headers=_headers()
    ).json()["profiles"]
    target = [p for p in listed if p["id"] == pid][0]
    assert target["status"] == "needs_credential"
    assert target["credential_masked_value"] is None


# ============================================================================
# Models endpoint
# ============================================================================


async def test_models_anthropic_returns_empty(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]

    r = client.get(
        f"/api/provider-profiles/{pid}/models", headers=_headers()
    )
    assert r.status_code == 200
    assert r.json() == {"models": []}


async def test_models_glm_returns_static(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "G",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
        },
    ).json()["profile"]["id"]

    r = client.get(
        f"/api/provider-profiles/{pid}/models", headers=_headers()
    )
    assert r.status_code == 200
    models = r.json()["models"]
    assert len(models) >= 1
    ids = {m["id"] for m in models}
    assert "glm-4.5-flash" in ids


async def test_models_for_disabled_profile_still_listed(
    client: TestClient,
) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
            "enabled": False,
        },
    ).json()["profile"]["id"]

    r = client.get(
        f"/api/provider-profiles/{pid}/models", headers=_headers()
    )
    assert r.status_code == 200


async def test_models_for_profile_missing_credential_still_listed(
    client: TestClient,
) -> None:
    """Profile with status=needs_credential can still list static models."""
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
        },
    ).json()["profile"]["id"]
    # Delete credential
    client.delete(f"/api/credentials/{cred_id}", headers=_headers())

    r = client.get(
        f"/api/provider-profiles/{pid}/models", headers=_headers()
    )
    assert r.status_code == 200


# ============================================================================
# Session binding endpoints
# ============================================================================


async def test_get_binding_returns_null_when_no_binding(
    client: TestClient,
) -> None:
    sess = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()
    sess_id = sess["id"]

    r = client.get(
        f"/api/sessions/{sess_id}/model-binding", headers=_headers()
    )
    assert r.status_code == 200
    assert r.json() == {"binding": None}


async def test_get_binding_for_missing_session_returns_404(
    client: TestClient,
) -> None:
    r = client.get(
        "/api/sessions/sess-nonexistent/model-binding", headers=_headers()
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


async def test_put_binding_creates_explicit_binding(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]
    sess_id = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()["id"]

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_headers(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )
    assert r.status_code == 200
    body = r.json()["binding"]
    assert body["profile_id"] == pid
    assert body["source"] == "explicit"


async def test_put_binding_to_disabled_profile_409(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "enabled": False,
        },
    ).json()["profile"]["id"]
    sess_id = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()["id"]

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_headers(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "profile_disabled"


async def test_put_binding_manual_model_id_outside_static_list(
    client: TestClient,
) -> None:
    """Manual model_id NOT in static catalog is still accepted (format-validated only)."""
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]
    sess_id = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()["id"]

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_headers(),
        json={"profile_id": pid, "model_id": "claude-opus-4.7-my-fork"},
    )
    assert r.status_code == 200
    assert r.json()["binding"]["model_id"] == "claude-opus-4.7-my-fork"


async def test_put_binding_invalid_model_id_422(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "X",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]
    sess_id = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()["id"]

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_headers(),
        json={"profile_id": pid, "model_id": "bad\ninject"},
    )
    assert r.status_code == 422


async def test_session_a_b_can_have_different_bindings(
    client: TestClient,
) -> None:
    cred_id = _seed_credential(client)
    p_anthropic = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "A",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    ).json()["profile"]["id"]
    p_glm = client.post(
        "/api/provider-profiles",
        headers=_headers(),
        json={
            "name": "G",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
        },
    ).json()["profile"]["id"]

    s1 = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S1"}
    ).json()["id"]
    s2 = client.post(
        "/api/sessions", headers=_headers(), json={"title": "S2"}
    ).json()["id"]

    client.put(
        f"/api/sessions/{s1}/model-binding",
        headers=_headers(),
        json={"profile_id": p_anthropic, "model_id": "claude-X"},
    )
    client.put(
        f"/api/sessions/{s2}/model-binding",
        headers=_headers(),
        json={"profile_id": p_glm, "model_id": "glm-4.5-flash"},
    )

    b1 = client.get(
        f"/api/sessions/{s1}/model-binding", headers=_headers()
    ).json()["binding"]
    b2 = client.get(
        f"/api/sessions/{s2}/model-binding", headers=_headers()
    ).json()["binding"]
    assert b1["profile_id"] == p_anthropic
    assert b2["profile_id"] == p_glm

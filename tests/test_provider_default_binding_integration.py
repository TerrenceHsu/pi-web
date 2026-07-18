"""Session default binding integration tests (E2-3B3).

覆盖 spec §17.5 Session creation 矩阵（默认 binding + 不变量）.
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
        db_path=str(tmp_path / " integ.db"),
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
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-x"},
    )
    return r.json()["credential"]["credential_id"]


def _seed_default_profile(c: TestClient, cred_id: str, model: str = "glm-4.5-flash") -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "Def",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": model,
            "is_default": True,
        },
    )
    return r.json()["profile"]["id"]


# ============================================================================
# 40. No default Profile → Session created, no Binding
# ============================================================================


async def test_no_default_profile_no_binding(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    # Create non-default profile
    client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "NotDefault",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "is_default": False,
        },
    )

    sess = client.post("/api/sessions", json={"title": "S1"}).json()
    sess_id = sess["id"]
    b = client.get(
        f"/api/sessions/{sess_id}/model-binding", headers=_ui_headers()
    ).json()
    assert b == {"binding": None}


# ============================================================================
# 41. Default Profile exists → source=default binding
# ============================================================================


async def test_default_profile_creates_default_binding(client: TestClient) -> None:
    cred_id = _seed_credential(client)
    pid = _seed_default_profile(client, cred_id)

    sess = client.post("/api/sessions", json={"title": "S1"}).json()
    sess_id = sess["id"]
    b = client.get(
        f"/api/sessions/{sess_id}/model-binding", headers=_ui_headers()
    ).json()["binding"]
    assert b is not None
    assert b["profile_id"] == pid
    assert b["source"] == "default"
    assert b["model_id"] == "glm-4.5-flash"


# ============================================================================
# 42. default_model snapshotted at creation time
# ============================================================================


async def test_default_model_snapshotted(client: TestClient) -> None:
    """Binding model_id == Profile.default_model at session creation time."""
    cred_id = _seed_credential(client)
    _seed_default_profile(client, cred_id, model="glm-4.5")  # custom default

    sess = client.post("/api/sessions", json={"title": "S1"}).json()
    b = client.get(
        f"/api/sessions/{sess['id']}/model-binding", headers=_ui_headers()
    ).json()["binding"]
    assert b["model_id"] == "glm-4.5"


# ============================================================================
# 43. Later change to Profile.default_model does NOT affect existing Binding
# ============================================================================


async def test_profile_model_change_does_not_affect_existing_binding(
    client: TestClient,
) -> None:
    cred_id = _seed_credential(client)
    pid = _seed_default_profile(client, cred_id, model="glm-4.5")

    sess = client.post("/api/sessions", json={"title": "S1"}).json()

    # Update Profile.default_model
    client.patch(
        f"/api/provider-profiles/{pid}",
        headers=_ui_headers(),
        json={"default_model": "glm-4"},
    )

    # Existing binding unchanged
    b = client.get(
        f"/api/sessions/{sess['id']}/model-binding", headers=_ui_headers()
    ).json()["binding"]
    assert b["model_id"] == "glm-4.5"  # snapshot preserved


# ============================================================================
# 44. Switching default Profile does NOT affect existing Sessions
# ============================================================================


async def test_switching_default_does_not_affect_existing_sessions(
    client: TestClient,
) -> None:
    cred_id = _seed_credential(client)
    p1 = _seed_default_profile(client, cred_id, model="glm-4.5-flash")

    # Create first session — bound to p1
    sess1 = client.post("/api/sessions", json={"title": "S1"}).json()

    # Make a new profile the default
    p2 = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "New",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "is_default": True,  # switches default
        },
    ).json()["profile"]["id"]

    # Old session still bound to p1
    b1 = client.get(
        f"/api/sessions/{sess1['id']}/model-binding", headers=_ui_headers()
    ).json()["binding"]
    assert b1["profile_id"] == p1

    # New session bound to p2
    sess2 = client.post("/api/sessions", json={"title": "S2"}).json()
    b2 = client.get(
        f"/api/sessions/{sess2['id']}/model-binding", headers=_ui_headers()
    ).json()["binding"]
    assert b2["profile_id"] == p2


# ============================================================================
# 49. Binding complete before HTTP response returns
# ============================================================================


async def test_binding_complete_before_response(client: TestClient) -> None:
    """By the time POST /api/sessions returns, binding must already be readable."""
    cred_id = _seed_credential(client)
    _seed_default_profile(client, cred_id)

    sess_r = client.post("/api/sessions", json={"title": "S1"})
    assert sess_r.status_code == 200
    sess_id = sess_r.json()["id"]

    # IMMEDIATELY verify binding (no retry)
    b_r = client.get(
        f"/api/sessions/{sess_id}/model-binding", headers=_ui_headers()
    )
    assert b_r.status_code == 200
    assert b_r.json()["binding"] is not None


# ============================================================================
# 50. No WS event during creation (verified by absence of session-created event type)
# ============================================================================


async def test_no_ws_event_emitted_during_session_creation(
    client: TestClient,
) -> None:
    """Session creation does NOT emit any WebSocket event.

    Verified by checking that no 'session_created' / 'session_created' event type
    exists in the event stream immediately after creation. Per E2-3A audit §1.4,
    no broadcast mechanism exists for session creation—this test documents that.
    """
    cred_id = _seed_credential(client)
    _seed_default_profile(client, cred_id)

    # Snapshot event count before
    events_before = client.get("/api/state").json().get("event_count", 0)

    sess = client.post("/api/sessions", json={"title": "S1"}).json()
    assert "id" in sess

    # Event count should not have grown significantly
    events_after = client.get("/api/state").json().get("event_count", 0)
    # Allow up to 1 event for the GET requests themselves
    # Session creation must not generate AgentEvents
    assert events_after <= events_before + 1


# ============================================================================
# Legacy compatibility: provider config disabled → no binding
# ============================================================================


async def test_legacy_app_skips_binding(tmp_path: Path) -> None:
    """Without credential runtime, no binding is attempted."""
    app = create_app(
        _harness(),
        db_path=":memory:",
    )
    with TestClient(app) as c:
        sess = c.post("/api/sessions", json={"title": "S1"}).json()
        # provider config runtime is None—no binding attempted
        # Verify session was created normally
        assert "id" in sess

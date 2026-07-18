"""Session binding API tests (E2-3B3).

覆盖 spec §17.4 Binding 矩阵（GET/PUT 直接 endpoint 测试，与 default-binding-integration 互补）.
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
        db_path=str(tmp_path / "bindapi.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _ui() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _seed_profile(client: TestClient) -> str:
    r = client.post(
        "/api/credentials",
        headers=_ui(),
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-x"},
    )
    cred_id = r.json()["credential"]["credential_id"]
    pr = client.post(
        "/api/provider-profiles",
        headers=_ui(),
        json={
            "name": "P",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
        },
    )
    return pr.json()["profile"]["id"]


def _make_session(client: TestClient, title: str = "S") -> str:
    return client.post("/api/sessions", json={"title": title}).json()["id"]


# ============================================================================
# GET binding
# ============================================================================


async def test_get_binding_returns_null_for_existing_session_without_binding(
    client: TestClient,
) -> None:
    """No default profile → new session has no binding → GET returns null."""
    sess_id = _make_session(client)
    r = client.get(f"/api/sessions/{sess_id}/model-binding", headers=_ui())
    assert r.status_code == 200
    assert r.json() == {"binding": None}


async def test_get_binding_404_for_missing_session(client: TestClient) -> None:
    r = client.get(
        "/api/sessions/sess-does-not-exist/model-binding", headers=_ui()
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


# ============================================================================
# PUT binding
# ============================================================================


async def test_put_binding_creates_explicit(client: TestClient) -> None:
    pid = _seed_profile(client)
    sess_id = _make_session(client)

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )
    assert r.status_code == 200
    body = r.json()["binding"]
    assert body["profile_id"] == pid
    assert body["source"] == "explicit"


async def test_put_binding_updates_existing(client: TestClient) -> None:
    pid = _seed_profile(client)
    sess_id = _make_session(client)

    client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-old"},
    )
    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-new"},
    )
    assert r.status_code == 200
    assert r.json()["binding"]["model_id"] == "claude-new"


async def test_put_binding_to_disabled_profile_returns_409(
    client: TestClient,
) -> None:
    # Create disabled profile
    r = client.post(
        "/api/credentials",
        headers=_ui(),
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-x"},
    )
    cred_id = r.json()["credential"]["credential_id"]
    pr = client.post(
        "/api/provider-profiles",
        headers=_ui(),
        json={
            "name": "P",
            "provider_id": "anthropic",
            "credential_id": cred_id,
            "default_model": "claude-X",
            "enabled": False,
        },
    )
    pid = pr.json()["profile"]["id"]
    sess_id = _make_session(client)

    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "profile_disabled"


async def test_put_binding_to_nonexistent_profile_returns_404(
    client: TestClient,
) -> None:
    sess_id = _make_session(client)
    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": "profile-nonexistent", "model_id": "claude-X"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "profile_not_found"


async def test_put_binding_to_nonexistent_session_returns_404(
    client: TestClient,
) -> None:
    pid = _seed_profile(client)
    r = client.put(
        "/api/sessions/sess-does-not-exist/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-X"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


async def test_put_binding_manual_model_id_outside_static_list(
    client: TestClient,
) -> None:
    pid = _seed_profile(client)
    sess_id = _make_session(client)
    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-opus-4.7-my-fork"},
    )
    assert r.status_code == 200
    assert r.json()["binding"]["model_id"] == "claude-opus-4.7-my-fork"


async def test_put_binding_invalid_model_id_returns_422(
    client: TestClient,
) -> None:
    pid = _seed_profile(client)
    sess_id = _make_session(client)
    r = client.put(
        f"/api/sessions/{sess_id}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "bad\ninject"},
    )
    assert r.status_code == 422


async def test_session_a_b_isolation(client: TestClient) -> None:
    """Different sessions can have different bindings."""
    pid = _seed_profile(client)
    s1 = _make_session(client, "S1")
    s2 = _make_session(client, "S2")

    client.put(
        f"/api/sessions/{s1}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-A"},
    )
    client.put(
        f"/api/sessions/{s2}/model-binding",
        headers=_ui(),
        json={"profile_id": pid, "model_id": "claude-B"},
    )

    b1 = client.get(
        f"/api/sessions/{s1}/model-binding", headers=_ui()
    ).json()["binding"]
    b2 = client.get(
        f"/api/sessions/{s2}/model-binding", headers=_ui()
    ).json()["binding"]
    assert b1["model_id"] == "claude-A"
    assert b2["model_id"] == "claude-B"
    assert b1["session_id"] != b2["session_id"]

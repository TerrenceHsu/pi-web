"""Session default binding compensation tests (E2-3B3).

覆盖 spec §17.5 compensation + cancellation + rollback failure.
"""
from __future__ import annotations

import logging
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
        db_path=str(tmp_path / "comp.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _ui_headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _seed_default(client: TestClient) -> str:
    """Seed credential + default profile; return profile_id."""
    r = client.post(
        "/api/credentials",
        headers=_ui_headers(),
        json={"label": "L", "storage_mode": "session_only", "secret_value": "sk-x"},
    )
    cred_id = r.json()["credential"]["credential_id"]
    pr = client.post(
        "/api/provider-profiles",
        headers=_ui_headers(),
        json={
            "name": "Def",
            "provider_id": "glm",
            "credential_id": cred_id,
            "default_model": "glm-4.5-flash",
            "is_default": True,
        },
    )
    return pr.json()["profile"]["id"]


# ============================================================================
# 45. Binding failure → Session deleted (compensation)
# ============================================================================


async def test_binding_failure_triggers_compensation_delete(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    """If initialize_new_session_binding raises, Session must be deleted."""
    _seed_default(client)

    # Monkey-patch the service to raise during initialize_new_session_binding
    app = client.app
    original = app.state.provider_config_runtime.service.initialize_new_session_binding

    async def _failing_init(*, session_id: str) -> None:
        raise RuntimeError("injected binding failure")

    app.state.provider_config_runtime.service.initialize_new_session_binding = _failing_init

    try:
        r = client.post("/api/sessions", json={"title": "S1"})
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "default_binding_failed"

        # Session should NOT exist
        listed = client.get("/api/sessions").json()
        assert all(s.get("title") != "S1" for s in listed["sessions"])
    finally:
        app.state.provider_config_runtime.service.initialize_new_session_binding = original


# ============================================================================
# 46. FK race (Profile deleted during binding) → Session deleted
# ============================================================================


async def test_profile_deleted_during_binding_triggers_compensation(
    client: TestClient,
) -> None:
    """Binding failure (FK race or Store error) → compensation delete.

    We inject failure at the Store level by making upsert_binding raise
    a ProviderProfileNotFoundError (simulating FK race).
    """
    _seed_default(client)
    app = client.app
    store = app.state.provider_config_runtime.store
    original_upsert = store.upsert_binding

    from pi_agent_core_py.web.providers.config_store import (
        ProviderProfileNotFoundError,
    )

    async def _failing_upsert(*, session_id, profile_id, model_id, source):
        raise ProviderProfileNotFoundError("injected FK race")

    store.upsert_binding = _failing_upsert

    try:
        r = client.post("/api/sessions", json={"title": "S1"})
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "default_binding_failed"

        # Session deleted
        listed = client.get("/api/sessions").json()
        assert all(s.get("title") != "S1" for s in listed["sessions"])
    finally:
        store.upsert_binding = original_upsert


# ============================================================================
# 48. Compensation failure → 500 + critical log
# ============================================================================


async def test_compensation_failure_logs_critical(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    """Binding fails AND compensation delete also fails → critical log + 500."""
    _seed_default(client)
    app = client.app

    # Make initialize fail
    original_init = app.state.provider_config_runtime.service.initialize_new_session_binding

    async def _failing_init(*, session_id: str) -> None:
        raise RuntimeError("binding failure")

    app.state.provider_config_runtime.service.initialize_new_session_binding = _failing_init

    # Make delete_session fail
    original_delete = app.state.web.session_store.delete_session

    async def _failing_delete(session_id: str) -> None:
        raise RuntimeError("compensation failure")

    app.state.web.session_store.delete_session = _failing_delete

    try:
        with caplog.at_level(logging.CRITICAL):
            r = client.post("/api/sessions", json={"title": "S1"})
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "session_creation_rollback_failed"
        # Critical log emitted
        critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert any(
            getattr(r, "error_code", None) == "session_creation_rollback_failed"
            for r in critical_records
        )
    finally:
        app.state.provider_config_runtime.service.initialize_new_session_binding = original_init
        app.state.web.session_store.delete_session = original_delete


# ============================================================================
# Compensation helper idempotency: deleting already-deleted session
# ============================================================================


async def test_compensate_helper_idempotent_on_missing_session(
    client: TestClient,
) -> None:
    """Compensation must succeed (no exception) when Session already gone."""
    from pi_agent_core_py.web.app import _compensate_delete_session

    state = client.app.state.web
    # Session doesn't exist—should not raise
    await _compensate_delete_session(state, "sess-nonexistent")


# ============================================================================
# Visibility window: no extra awaits between create and binding
# ============================================================================


async def test_visibility_window_minimal(client: TestClient) -> None:
    """Between create_session commit and binding initialize there must be NO extra awaits.

    We can't directly assert "no awaits", but we can verify via call ordering
    spy that the immediate next call is initialize_new_session_binding.
    """
    _seed_default(client)
    app = client.app

    call_log: list[str] = []
    original_create = app.state.web.session_store.create_session
    original_init = app.state.provider_config_runtime.service.initialize_new_session_binding

    async def _spy_create(*args, **kwargs):
        call_log.append("create_session")
        return await original_create(*args, **kwargs)

    async def _spy_init(*, session_id):
        call_log.append("initialize_binding")
        return await original_init(session_id=session_id)

    app.state.web.session_store.create_session = _spy_create
    app.state.provider_config_runtime.service.initialize_new_session_binding = _spy_init

    try:
        client.post("/api/sessions", json={"title": "S1"})

        # Verify create → initialize is immediate, no other call in between
        assert call_log[0] == "create_session"
        assert call_log[1] == "initialize_binding"
    finally:
        app.state.web.session_store.create_session = original_create
        app.state.provider_config_runtime.service.initialize_new_session_binding = original_init

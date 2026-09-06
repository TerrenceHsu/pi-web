"""FastAPI composition and security tests for coding sandbox administration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from coding_sandbox import (  # noqa: E402
    FakeSandboxBackend,
)
from coding_sandbox.admin import (  # noqa: E402
    SandboxAdminConfig,
    SandboxRuntimeConfigurationError,
    resolve_sandbox_runtime_configuration,
)
from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    return AgentHarness(Agent(system_prompt="base", client=client, tools=None))


def _headers() -> dict[str, str]:
    return {"X-PI-Agent-UI": "1"}


def test_runtime_resolver_fails_closed_when_explicit_prerequisite_is_missing() -> None:
    with pytest.raises(SandboxRuntimeConfigurationError):
        resolve_sandbox_runtime_configuration(
            enable_api=True,
            credential_runtime_enabled=True,
            credential_api_enabled=False,
            trusted_host_enabled=True,
            db_path="D:/app.db",
        )

    resolved = resolve_sandbox_runtime_configuration(
        enable_api=None,
        credential_runtime_enabled=True,
        credential_api_enabled=False,
        trusted_host_enabled=True,
        db_path="D:/app.db",
    )
    assert resolved.runtime_enabled is False
    assert resolved.api_enabled is False


def test_app_mounts_revisioned_config_and_offline_connection_probe(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    backend = FakeSandboxBackend(id_factory=lambda: "admin-probe")
    resolved_secrets: list[str] = []

    def backend_factory(
        config: SandboxAdminConfig,
        secrets: Mapping[str, str],
    ) -> FakeSandboxBackend:
        resolved_secrets.append(secrets["api_key"])
        assert config.limits.lifetime_seconds == 60
        return backend

    app = create_app(
        _harness(),
        db_path=database,
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
        coding_sandbox_backend_factory=backend_factory,
    )

    with TestClient(app, base_url="http://testserver") as client:
        assert app.state.coding_sandbox_runtime is not None
        missing_header = client.get("/api/coding-sandbox/config")
        assert missing_header.status_code == 400
        assert missing_header.json()["error"]["code"] == "missing_ui_header"

        initial = client.get("/api/coding-sandbox/config", headers=_headers())
        assert initial.status_code == 200
        assert initial.json()["revision"] == 0
        assert initial.json()["config"]["enabled"] is False

        session = client.post(
            "/api/sessions",
            headers=_headers(),
            json={"title": "Sandbox session"},
        )
        assert session.status_code == 200, session.text
        session_id = session.json()["id"]

        latest = client.get(
            f"/api/coding-sandbox/sessions/{session_id}/operation",
            headers=_headers(),
        )
        assert latest.status_code == 200
        assert latest.json() == {"operation": None}

        disabled_start = client.post(
            "/api/coding-sandbox/operations",
            headers=_headers(),
            json={"session_id": session_id},
        )
        assert disabled_start.status_code == 409
        assert disabled_start.json()["error"]["code"] == "approval_required"

        missing_operation = client.get(
            "/api/coding-sandbox/operations/sandbox-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            headers=_headers(),
        )
        assert missing_operation.status_code == 404
        assert missing_operation.json()["error"]["code"] == "operation_not_found"

        credential = client.post(
            "/api/credentials",
            headers=_headers(),
            json={
                "label": "E2B Sandbox",
                "storage_mode": "session_only",
                "secret_value": "e2b_test-api-key-not-real",
            },
        )
        assert credential.status_code == 201, credential.text
        credential_id = credential.json()["credential"]["credential_id"]

        config = SandboxAdminConfig(
            enabled=True,
            runtime_id="base",
            credential_id=credential_id,
        )
        saved = client.put(
            "/api/coding-sandbox/config",
            headers=_headers(),
            json={
                "expected_revision": 0,
                "config": config.model_dump(mode="json"),
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        assert "e2b_test-api-key-not-real" not in saved.text

        stale = client.put(
            "/api/coding-sandbox/config",
            headers=_headers(),
            json={
                "expected_revision": 0,
                "config": config.model_dump(mode="json"),
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "sandbox_config_conflict"

        probe = client.post(
            "/api/coding-sandbox/test-connection",
            headers=_headers(),
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["ok"] is True
        assert probe.json()["python_available"] is True
        assert "e2b_test-api-key-not-real" not in probe.text

        oversized = client.put(
            "/api/coding-sandbox/config",
            headers=_headers(),
            json={"expected_revision": 1, "config": {"runtime_id": "x" * 40_000}},
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "request_body_too_large"

    assert app.state.coding_sandbox_runtime is None
    assert resolved_secrets == ["e2b_test-api-key-not-real"]
    assert backend.destroyed_sandbox_ids == ["admin-probe"]
    assert b"e2b_test-api-key-not-real" not in database.read_bytes()


def test_explicitly_disabled_api_does_not_open_runtime(tmp_path: Path) -> None:
    app = create_app(
        _harness(),
        db_path=tmp_path / "app.db",
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
        enable_coding_sandbox_api=False,
    )

    with TestClient(app):
        assert app.state.credential_runtime is not None
        assert app.state.coding_sandbox_runtime is None

"""Web API coverage for the per-user, non-deletable built-in DDGS MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app


def _harness() -> AgentHarness:
    client = FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    return AgentHarness(Agent(system_prompt="", client=client))


def _app(db_path: Path):
    return create_app(
        _harness(),
        db_path=db_path,
        enable_builtin_ddgs=True,
    )


def test_builtin_ddgs_is_visible_fixed_and_connection_testable(tmp_path: Path) -> None:
    app = _app(tmp_path / "workspace.sqlite")
    with TestClient(app) as client:
        listed = client.get("/api/mcp/servers")
        tested = client.post("/api/mcp/servers/ddgs/test")

    server = listed.json()["servers"][0]
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert server["name"] == "ddgs"
    assert server["command"] == sys.executable
    assert server["builtin"] is True
    assert server["deletable"] is False
    assert server["settings"]["backend"] == "auto"
    assert tested.status_code == 200
    assert tested.json()["tool_count"] == 2
    dispose_app(app)


def test_builtin_ddgs_cannot_be_deleted(tmp_path: Path) -> None:
    app = _app(tmp_path / "workspace.sqlite")
    with TestClient(app) as client:
        deleted = client.delete("/api/mcp/servers/ddgs")
        listed = client.get("/api/mcp/servers")

    assert deleted.status_code == 403
    assert "cannot be deleted" in deleted.json()["detail"]
    assert listed.json()["count"] == 1
    dispose_app(app)


def test_ddgs_settings_update_and_persist_across_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "workspace.sqlite"
    payload = {
        "max_results": 14,
        "region": "cn-zh",
        "safesearch": "off",
        "timelimit": "w",
        "timeout_seconds": 18,
        "backend": "duckduckgo",
    }
    first_app = _app(db_path)
    with TestClient(first_app) as client:
        updated = client.put("/api/mcp/servers/ddgs/settings", json=payload)
    dispose_app(first_app)

    second_app = _app(db_path)
    with TestClient(second_app) as client:
        restored = client.get("/api/mcp/servers")
    dispose_app(second_app)

    assert updated.status_code == 200, updated.text
    assert updated.json()["settings"] == payload
    assert restored.json()["servers"][0]["settings"] == payload


def test_ddgs_settings_validation_rejects_unknown_or_out_of_range_values(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path / "workspace.sqlite")
    with TestClient(app) as client:
        bad_count = client.put(
            "/api/mcp/servers/ddgs/settings",
            json={
                "max_results": 100,
                "region": "wt-wt",
                "safesearch": "moderate",
                "timelimit": None,
                "timeout_seconds": 10,
                "backend": "duckduckgo",
            },
        )
        bad_backend = client.put(
            "/api/mcp/servers/ddgs/settings",
            json={
                "max_results": 5,
                "region": "wt-wt",
                "safesearch": "moderate",
                "timelimit": None,
                "timeout_seconds": 10,
                "backend": "bing",
            },
        )

    assert bad_count.status_code == 400
    assert bad_backend.status_code == 400
    dispose_app(app)

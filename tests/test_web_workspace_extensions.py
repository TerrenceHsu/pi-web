"""Global MCP/Skill catalogs with durable per-Workspace selection."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.harness.skills import Skill, SkillRegistry
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app, dispose_app

_MCP_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_stdio_server.py"


def _harness() -> AgentHarness:
    harness = AgentHarness(
        Agent(
            system_prompt="",
            client=FakeClient([[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]]),
        )
    )
    harness.attach_skills(
        SkillRegistry(
            [
                Skill(name="alpha", description="Alpha skill", prompt="Use alpha."),
                Skill(name="beta", description="Beta skill", prompt="Use beta."),
            ]
        )
    )
    return harness


def test_workspace_selects_from_global_mcp_and_skill_catalogs(tmp_path: Path) -> None:
    app = create_app(_harness(), db_path=tmp_path / "workspace.sqlite")
    with TestClient(app) as client:
        first = client.post("/api/sessions", json={"title": "First"}).json()["id"]
        second = client.post("/api/sessions", json={"title": "Second"}).json()["id"]
        added = client.post(
            "/api/mcp/servers",
            json={
                "name": "fake",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(_MCP_FIXTURE)],
                "enabled": True,
            },
        )
        assert added.status_code == 200, added.text

        saved = client.put(
            f"/api/workspaces/{first}/extensions",
            json={"mcp_server_names": ["fake"], "skill_names": ["alpha"]},
        )
        assert saved.status_code == 200, saved.text
        payload = saved.json()
        assert payload["configured"] is True
        assert payload["selected_mcp_server_names"] == ["fake"]
        assert payload["selected_skill_names"] == ["alpha"]

        untouched = client.get(f"/api/workspaces/{second}/extensions")
        assert untouched.status_code == 200
        assert untouched.json()["configured"] is False
        assert untouched.json()["selected_mcp_server_names"] == []
        assert untouched.json()["selected_skill_names"] == []
    dispose_app(app)


def test_workspace_rejects_disabled_or_unknown_extensions(tmp_path: Path) -> None:
    app = create_app(_harness(), db_path=tmp_path / "workspace.sqlite")
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"title": "First"}).json()["id"]
        response = client.put(
            f"/api/workspaces/{session_id}/extensions",
            json={"mcp_server_names": ["missing"], "skill_names": ["missing"]},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["mcp_server_names"] == ["missing"]
        assert response.json()["detail"]["skill_names"] == ["missing"]
    dispose_app(app)


def test_ddgs_is_fixed_global_default_after_it_is_enabled(tmp_path: Path) -> None:
    app = create_app(
        _harness(),
        db_path=tmp_path / "workspace.sqlite",
        enable_builtin_ddgs=True,
    )
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"title": "First"}).json()["id"]
        enabled = client.post("/api/mcp/servers/ddgs/enable")
        assert enabled.status_code == 200, enabled.text
        payload = client.get(f"/api/workspaces/{session_id}/extensions").json()
        ddgs = next(item for item in payload["mcp_servers"] if item["name"] == "ddgs")
        assert ddgs["builtin"] is True
        assert ddgs["deletable"] is False
        assert ddgs["selected"] is True
        assert payload["selected_mcp_server_names"] == ["ddgs"]
    dispose_app(app)


def test_http_mcp_global_config_persists_only_header_environment_refs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "workspace.sqlite"
    app = create_app(_harness(), db_path=db_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/mcp/servers",
            json={
                "name": "remote",
                "transport": "http",
                "url": "https://example.com/mcp",
                "header_env": {"Authorization": "REMOTE_MCP_AUTH"},
                "enabled": False,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["transport"] == "http"
        assert payload["url"] == "https://example.com/mcp"
        assert payload["header_env"] == {"Authorization": "REMOTE_MCP_AUTH"}
        assert "headers" not in payload
    dispose_app(app)

    reopened = create_app(_harness(), db_path=db_path)
    with TestClient(reopened) as client:
        payload = client.get("/api/mcp/servers").json()["servers"][0]
        assert payload["transport"] == "http"
        assert payload["header_env"] == {"Authorization": "REMOTE_MCP_AUTH"}
    dispose_app(reopened)


def test_deleting_a_global_skill_clears_workspace_selections(tmp_path: Path) -> None:
    app = create_app(_harness(), db_path=tmp_path / "workspace.sqlite")
    markdown = b"---\nname: removable\ndescription: removable\n---\n\nUse it."
    with TestClient(app) as client:
        session_id = client.post("/api/sessions", json={"title": "First"}).json()["id"]
        uploaded = client.post(
            "/api/skills/upload",
            files={"files": ("SKILL.md", markdown, "text/markdown")},
        )
        assert uploaded.status_code == 200, uploaded.text
        selected = client.put(
            f"/api/workspaces/{session_id}/extensions",
            json={"mcp_server_names": [], "skill_names": ["removable"]},
        )
        assert selected.status_code == 200, selected.text

        deleted = client.delete("/api/skills/removable")
        assert deleted.status_code == 200, deleted.text
        reuploaded = client.post(
            "/api/skills/upload",
            files={"files": ("SKILL.md", markdown, "text/markdown")},
        )
        assert reuploaded.status_code == 200, reuploaded.text
        workspace = client.get(f"/api/workspaces/{session_id}/extensions").json()
        assert workspace["selected_skill_names"] == []
    dispose_app(app)

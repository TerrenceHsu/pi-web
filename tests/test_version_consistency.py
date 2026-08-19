"""Release metadata must stay aligned across backend and frontend surfaces."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from fastapi import FastAPI

from pi_agent_core_py import __version__
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient
from pi_agent_core_py.web.app import create_app, dispose_app
from pi_agent_core_py.web.auth.gateway import create_authenticated_app
from pi_agent_core_py.web.auth.models import AuthUser

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND_ROOT = _ROOT / "src" / "pi_agent_core_py" / "web" / "frontend"


def _workspace_factory(_user: AuthUser, _workspace_root: Path) -> FastAPI:
    return FastAPI()


def test_backend_api_versions_follow_python_package(tmp_path: Path) -> None:
    harness = AgentHarness(Agent(system_prompt="test", client=FakeClient([])))
    workspace_app = create_app(harness)
    gateway_app = create_authenticated_app(
        _workspace_factory,
        auth_db_path=tmp_path / "auth.sqlite",
        user_data_root=tmp_path / "users",
    )
    try:
        assert workspace_app.version == __version__
        assert gateway_app.version == __version__
    finally:
        dispose_app(workspace_app)


def test_frontend_versions_follow_python_package() -> None:
    package = json.loads((_FRONTEND_ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((_FRONTEND_ROOT / "package-lock.json").read_text(encoding="utf-8"))

    assert package["version"] == __version__
    assert lock["version"] == __version__
    assert lock["packages"][""]["version"] == __version__


def test_project_metadata_uses_root_license_file() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["license"] == {"file": "LICENSE"}
    assert (_ROOT / "LICENSE").is_file()

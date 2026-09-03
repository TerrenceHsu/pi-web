"""Authenticated administrator Telemetry API integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.agent import Agent  # noqa: E402
from pi_agent_core_py.harness import AgentHarness  # noqa: E402
from pi_agent_core_py.messages import Usage  # noqa: E402
from pi_agent_core_py.model_client import (  # noqa: E402
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.auth import AuthUser, create_authenticated_app  # noqa: E402
from pi_agent_core_py.web.auth.passwords import hash_password  # noqa: E402
from pi_agent_core_py.web.auth.service import AuthService  # noqa: E402
from pi_agent_core_py.web.auth.store import AuthStore  # noqa: E402

UI_HEADERS = {"X-PI-Agent-UI": "1"}


def _seed_accounts(database: Path) -> None:
    async def seed() -> None:
        store = await AuthStore.open(str(database))
        service = AuthService(store)
        await service.ensure_initial_admin()
        await store.create_user("alice", hash_password("alice-pass"))
        await store.close()

    asyncio.run(seed())


def _gateway(tmp_path: Path) -> FastAPI:
    auth_database = tmp_path / "auth.sqlite"
    telemetry_database = tmp_path / "telemetry.sqlite"
    _seed_accounts(auth_database)

    def factory(user: AuthUser, workspace_root: Path) -> FastAPI:
        client = FakeClient(
            [
                [
                    TextDeltaEvent(delta="safe answer"),
                    DoneEvent(
                        stop_reason="stop",
                        usage=Usage(input=12, output=5, total_tokens=17),
                    ),
                ]
            ]
        )
        return create_app(
            AgentHarness(Agent(system_prompt="", client=client)),
            db_path=workspace_root / "workspace.sqlite",
            telemetry_db_path=telemetry_database,
            telemetry_account_id=user.id,
            telemetry_account_name=user.name,
            credential_extra_hosts=("testserver",),
        )

    return create_authenticated_app(
        factory,
        auth_db_path=auth_database,
        user_data_root=tmp_path / "users",
        extra_hosts=("testserver",),
    )


def _login(client: TestClient, name: str, password: str) -> None:
    response = client.post(
        "/api/auth/login",
        headers=UI_HEADERS,
        json={"name": name, "password": password},
    )
    assert response.status_code == 200


def test_admin_can_inspect_cross_account_telemetry_but_user_cannot(tmp_path: Path) -> None:
    marker = "USER_PROMPT_MUST_NOT_BE_RECORDED"
    with TestClient(_gateway(tmp_path)) as client:
        _login(client, "alice", "alice-pass")
        alice_session = client.get("/api/auth/session", headers=UI_HEADERS).json()
        assert alice_session["user"]["is_admin"] is False
        session_id = client.get("/api/sessions").json()["sessions"][0]["id"]
        prompt = client.post(
            "/api/prompt",
            json={"session_id": session_id, "text": marker},
        )
        assert prompt.status_code == 200

        forbidden = client.get("/api/admin/telemetry/summary", headers=UI_HEADERS)
        assert forbidden.status_code == 403
        client.post("/api/auth/logout", headers=UI_HEADERS)

        _login(client, "admin", "123456")
        admin_session = client.get("/api/auth/session", headers=UI_HEADERS).json()
        assert admin_session["user"]["is_admin"] is True
        assert client.get("/api/admin/telemetry/summary").status_code == 400

        summary_response = client.get(
            "/api/admin/telemetry/summary",
            headers=UI_HEADERS,
        )
        assert summary_response.status_code == 200
        summary = summary_response.json()
        assert summary["requests"]["total"] == 1
        assert summary["usage"]["total_tokens"] == 17
        assert {account["name"] for account in summary["accounts"]} == {"alice"}

        spans_response = client.get(
            "/api/admin/telemetry/spans",
            headers=UI_HEADERS,
            params={"name": "web.request", "window_hours": 24},
        )
        assert spans_response.status_code == 200
        span = spans_response.json()["spans"][0]
        assert span["attributes"]["account_name"] == "alice"
        assert span["attributes"]["outcome"] == "completed"
        assert span["attributes"]["input_tokens"] == 12

        detail = client.get(
            f"/api/admin/telemetry/spans/{span['id']}",
            headers=UI_HEADERS,
        )
        assert detail.status_code == 200
        assert "model.end" in {event["name"] for event in detail.json()["events"]}
        assert marker not in detail.text

    assert marker.encode() not in (tmp_path / "telemetry.sqlite").read_bytes()


def test_gateway_serves_telemetry_spa_route(tmp_path: Path) -> None:
    with TestClient(_gateway(tmp_path)) as client:
        response = client.get("/telemetry")
        assert response.status_code == 200
        assert "html" in response.text.lower()

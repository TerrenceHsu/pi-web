"""Login gateway and per-account workspace isolation tests."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from pi_agent_core_py.agent import Agent  # noqa: E402
from pi_agent_core_py.harness import AgentHarness  # noqa: E402
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.auth.gateway import (  # noqa: E402
    AUTH_COOKIE_NAME,
    WorkspaceManager,
    create_authenticated_app,
)
from pi_agent_core_py.web.auth.models import AuthUser  # noqa: E402
from pi_agent_core_py.web.auth.passwords import hash_password, verify_password  # noqa: E402
from pi_agent_core_py.web.auth.service import AuthService  # noqa: E402
from pi_agent_core_py.web.auth.store import AuthStore  # noqa: E402

UI_HEADERS = {"X-PI-Agent-UI": "1"}


def _workspace_factory(user: AuthUser, root: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.values = []
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/api/profile")
    async def profile(request: Request) -> dict[str, Any]:
        delegated_user = request.scope["auth_user"]
        return {
            "user": delegated_user.name,
            "workspace": str(root),
            "values": list(request.app.state.values),
        }

    @app.post("/api/profile/value")
    async def add_value(request: Request) -> dict[str, Any]:
        payload = await request.json()
        request.app.state.values.append(payload["value"])
        return {"values": list(request.app.state.values)}

    @app.websocket("/ws/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        delegated_user = websocket.scope["auth_user"]
        await websocket.send_json({"user": delegated_user.name})
        await websocket.close()

    return app


def _build_gateway(tmp_path: Path) -> FastAPI:
    return create_authenticated_app(
        _workspace_factory,
        auth_db_path=tmp_path / "auth.sqlite",
        user_data_root=tmp_path / "users",
        extra_hosts=("testserver",),
    )


@pytest.mark.parametrize(
    "path",
    [
        "/chat",
        "/chat/",
        "/chat/sess-safe",
        "/chat/bad/path",
        "/knowledge",
        "/knowledge/",
        "/knowledge/pages",
    ],
)
def test_gateway_spa_routes_return_spa_shell(tmp_path: Path, path: str) -> None:
    with TestClient(_build_gateway(tmp_path)) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert "html" in response.text.lower()


def test_password_hash_never_contains_plaintext() -> None:
    encoded = hash_password("123456", salt=b"0123456789abcdef")
    assert verify_password("123456", encoded) is True
    assert verify_password("not-it", encoded) is False
    assert "123456" not in encoded


@pytest.mark.asyncio
async def test_auth_store_bootstrap_and_session_survive_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.sqlite"
    store = await AuthStore.open(str(db_path))
    service = AuthService(store)
    admin, created = await service.ensure_initial_admin()
    assert created is True
    login = await service.login(name="admin", password="123456")
    assert login is not None
    assert login.user == admin
    token = login.token
    await store.close()

    reopened = await AuthStore.open(str(db_path))
    reopened_service = AuthService(reopened)
    resolved = await reopened_service.resolve_session(token)
    assert resolved == admin
    await reopened.close()

    with sqlite3.connect(db_path) as db:
        password_hash = db.execute(
            "SELECT password_hash FROM auth_users WHERE name = 'admin'"
        ).fetchone()[0]
        token_hash = db.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
    assert "123456" not in password_hash
    assert token not in token_hash


def test_login_gate_session_and_logout(tmp_path: Path) -> None:
    app = _build_gateway(tmp_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        blocked = client.get("/api/profile")
        assert blocked.status_code == 401
        with pytest.raises(WebSocketDisconnect) as ws_error:
            with client.websocket_connect("/ws/events"):
                pass
        assert ws_error.value.code == 4401

        wrong = client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "wrong-password"},
        )
        assert wrong.status_code == 401
        assert "wrong-password" not in wrong.text

        logged_in = client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["user"]["name"] == "admin"
        assert "123456" not in logged_in.text
        cookie = logged_in.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert AUTH_COOKIE_NAME in client.cookies

        session = client.get("/api/auth/session", headers=UI_HEADERS)
        assert session.status_code == 200
        assert session.json() == {
            "authenticated": True,
            "user": {"id": session.json()["user"]["id"], "name": "admin"},
        }
        assert client.get("/api/profile").json()["user"] == "admin"
        with client.websocket_connect("/ws/events") as websocket:
            assert websocket.receive_json() == {"user": "admin"}

        logout = client.post("/api/auth/logout", headers=UI_HEADERS)
        assert logout.status_code == 200
        assert client.get("/api/profile").status_code == 401


def test_gateway_restart_requires_login_again(tmp_path: Path) -> None:
    """A backend restart revokes login state but preserves the account."""
    first_app = _build_gateway(tmp_path)
    with TestClient(first_app) as first_client:
        logged_in = first_client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        )
        assert logged_in.status_code == 200
        previous_token = first_client.cookies.get(AUTH_COOKIE_NAME)
        assert previous_token
        # Reopening or refreshing while the same backend is alive keeps login.
        assert first_client.get("/api/auth/session", headers=UI_HEADERS).status_code == 200

    restarted_app = _build_gateway(tmp_path)
    with TestClient(restarted_app) as restarted_client:
        restarted_client.cookies.set(AUTH_COOKIE_NAME, previous_token)
        assert (
            restarted_client.get("/api/auth/session", headers=UI_HEADERS).status_code
            == 401
        )

        # Only the login session is cleared; the persisted admin account still works.
        logged_in_again = restarted_client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        )
        assert logged_in_again.status_code == 200


def test_relogin_preserves_conversation_and_agent_md(tmp_path: Path) -> None:
    """Logout/login changes auth state only; the account workspace remains intact."""

    def workspace_factory(user: AuthUser, root: Path) -> FastAPI:
        del user
        fake = FakeClient([
            [TextDeltaEvent(delta="saved answer"), DoneEvent(stop_reason="stop")]
        ])
        return create_app(
            AgentHarness(Agent(system_prompt="", client=fake)),
            db_path=root / "workspace.sqlite",
            uploads_dir=root / "uploads",
        )

    app = create_authenticated_app(
        workspace_factory,
        auth_db_path=tmp_path / "auth.sqlite",
        user_data_root=tmp_path / "users",
        extra_hosts=("testserver",),
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        ).status_code == 200
        sid = client.post("/api/sessions", json={"title": "persistent"}).json()["id"]
        files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        agent_md = next(f for f in files if f["purpose"] == "agent_instructions")
        custom = "# AGENT.md\n\nKeep this after login.\n"
        assert client.put(
            f"/api/sessions/{sid}/files/{agent_md['id']}/content",
            json={"content": custom, "expected_sha256": agent_md["sha256"]},
        ).status_code == 200
        assert client.post(
            "/api/prompt",
            json={"session_id": sid, "text": "remember"},
        ).status_code == 200

        assert client.post("/api/auth/logout", headers=UI_HEADERS).status_code == 200
        assert client.get("/api/sessions").status_code == 401
        assert client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        ).status_code == 200

        sessions = client.get("/api/sessions").json()["sessions"]
        assert any(session["id"] == sid for session in sessions)
        assert client.get(f"/api/messages?session_id={sid}").json()["count"] == 2
        restored_files = client.get(f"/api/sessions/{sid}/files").json()["files"]
        restored_agent = next(
            f for f in restored_files if f["purpose"] == "agent_instructions"
        )
        assert client.get(
            f"/api/sessions/{sid}/files/{restored_agent['id']}"
        ).text == custom


def test_login_security_envelope_does_not_echo_password(tmp_path: Path) -> None:
    app = _build_gateway(tmp_path)
    marker = "PASSWORD_MARKER_MUST_NOT_ECHO"
    with TestClient(app) as client:
        missing_header = client.post(
            "/api/auth/login",
            json={"name": "admin", "password": "123456"},
        )
        assert missing_header.status_code == 400

        invalid = client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": marker, "extra": marker},
        )
        assert invalid.status_code == 422
        assert marker not in invalid.text

        oversized = client.post(
            "/api/auth/login",
            headers={**UI_HEADERS, "Content-Type": "application/json"},
            content=(marker * 2000).encode(),
        )
        assert oversized.status_code == 413
        assert marker not in oversized.text


async def _seed_two_users(db_path: Path) -> dict[str, AuthUser]:
    store = await AuthStore.open(str(db_path))
    admin_record = await store.create_user("admin", hash_password("123456"))
    alice_record = await store.create_user("alice", hash_password("alice-pass"))
    await store.close()
    return {
        "admin": AuthUser(id=admin_record.id, name=admin_record.name),
        "alice": AuthUser(id=alice_record.id, name=alice_record.name),
    }


def test_each_account_gets_an_isolated_workspace(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.sqlite"
    seeded = asyncio.run(_seed_two_users(db_path))
    app = create_authenticated_app(
        _workspace_factory,
        auth_db_path=db_path,
        user_data_root=tmp_path / "users",
        extra_hosts=("testserver",),
    )

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "admin", "password": "123456"},
        )
        assert admin_login.status_code == 200
        admin_profile = client.get("/api/profile").json()
        client.post("/api/profile/value", json={"value": "admin-only"})
        client.post("/api/auth/logout", headers=UI_HEADERS)

        alice_login = client.post(
            "/api/auth/login",
            headers=UI_HEADERS,
            json={"name": "alice", "password": "alice-pass"},
        )
        assert alice_login.status_code == 200
        alice_profile = client.get("/api/profile").json()
        assert alice_profile["values"] == []
        assert alice_profile["workspace"] != admin_profile["workspace"]
        assert seeded["admin"].id in admin_profile["workspace"]
        assert seeded["alice"].id in alice_profile["workspace"]


@pytest.mark.asyncio
async def test_workspace_path_rejects_escape(tmp_path: Path) -> None:
    manager = WorkspaceManager(_workspace_factory, tmp_path / "users")
    with pytest.raises(RuntimeError, match="escaped"):
        manager.workspace_root_for(AuthUser(id="../escape", name="bad"))
    await manager.close()

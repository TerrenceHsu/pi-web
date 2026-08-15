"""Credential API security envelope tests（P1-E1-4B1）.

覆盖 X-PI-Agent-UI header + Origin 校验 + 路由级 enforcement.

使用 minimal stub router（不接真实 CredentialService）.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.web.credentials.api import build_credential_router  # noqa: E402
from pi_agent_core_py.web.local_web_security import (  # noqa: E402
    default_web_security_config,
)

# ============================================================================
# Stub router for security envelope tests
# ============================================================================


def _build_test_app(
    *,
    extra_ui_origins: tuple[str, ...] = ("http://localhost:5173",),
    extra_hosts: tuple[str, ...] = ("testserver",),
    require_ui_header: bool = True,
) -> FastAPI:
    """Build minimal app with Credential router + stub endpoints."""
    app = FastAPI()
    config = default_web_security_config(
        extra_hosts=extra_hosts,
        extra_ui_origins=extra_ui_origins,
        require_ui_header=require_ui_header,
    )
    router = build_credential_router(config)

    @router.get("/api/provider-definitions")
    async def list_providers() -> dict:
        return {"ok": True}

    @router.post("/api/credentials")
    async def create_credential() -> dict:
        return {"ok": True}

    @router.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str) -> dict:
        return {"deleted": credential_id}

    app.include_router(router)
    return app


# ============================================================================
# X-PI-Agent-UI header
# ============================================================================


class TestUiHeader:
    def test_get_without_header_rejected(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.get("/api/provider-definitions")
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "missing_ui_header"

    def test_get_with_correct_header_accepted(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.get(
                "/api/provider-definitions",
                headers={"X-PI-Agent-UI": "1"},
            )
            assert r.status_code == 200

    def test_post_without_header_rejected(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post("/api/credentials", json={})
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "missing_ui_header"

    def test_post_with_correct_header_accepted(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={"X-PI-Agent-UI": "1"},
            )
            assert r.status_code == 200

    def test_wrong_header_value_rejected(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.get(
                "/api/provider-definitions",
                headers={"X-PI-Agent-UI": "0"},
            )
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "missing_ui_header"

    def test_header_not_required_when_disabled(self) -> None:
        """If require_ui_header=False, no header check."""
        app = _build_test_app(require_ui_header=False)
        with TestClient(app) as client:
            r = client.get("/api/provider-definitions")
            # Origin still checked——absent Origin = OK (CLI)
            assert r.status_code == 200


# ============================================================================
# Origin validation
# ============================================================================


class TestOriginValidation:
    def test_external_origin_rejected(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "https://evil.com",
                },
            )
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "invalid_origin"

    def test_origin_null_rejected(self) -> None:
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "null",
                },
            )
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "invalid_origin"

    def test_allowed_origin_accepted(self) -> None:
        app = _build_test_app(
            extra_ui_origins=("http://localhost:5173",)
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "http://localhost:5173",
                },
            )
            assert r.status_code == 200

    def test_no_origin_accepted_cli(self) -> None:
        """No Origin header——CLI call——allow (Host + UI header handle security)."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={"X-PI-Agent-UI": "1"},
            )
            assert r.status_code == 200

    def test_scheme_mismatch_rejected(self) -> None:
        """Same host but wrong scheme——reject."""
        app = _build_test_app(
            extra_ui_origins=("http://localhost:5173",)
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "https://localhost:5173",  # https vs allowed http
                },
            )
            assert r.status_code == 403

    def test_port_mismatch_rejected(self) -> None:
        """Same scheme+host but wrong port——reject."""
        app = _build_test_app(
            extra_ui_origins=("http://localhost:5173",)
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "http://localhost:8080",
                },
            )
            assert r.status_code == 403

    def test_suffix_match_rejected(self) -> None:
        """evil-localhost.example must not pass——no suffix / regex match."""
        app = _build_test_app(
            extra_ui_origins=("http://localhost:5173",)
        )
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "http://evil-localhost.example",
                },
            )
            assert r.status_code == 403


# ============================================================================
# Wildcard CORS not present
# ============================================================================


class TestCorsPolicy:
    def test_no_wildcard_allow_origin_header(self) -> None:
        """Verify response never includes `Access-Control-Allow-Origin: *`."""
        app = _build_test_app()
        with TestClient(app) as client:
            r = client.post(
                "/api/credentials",
                json={},
                headers={
                    "X-PI-Agent-UI": "1",
                    "Origin": "https://evil.com",
                },
            )
            aco = r.headers.get("access-control-allow-origin", "")
            assert aco != "*"
            # No reflected evil origin either
            assert "evil.com" not in aco

    def test_preflight_for_allowed_origin_returns_limited_headers(self) -> None:
        """OPTIONS preflight——only allowed origin gets ACAO header."""
        app = _build_test_app(
            extra_ui_origins=("http://localhost:5173",)
        )
        with TestClient(app) as client:
            r = client.options(
                "/api/credentials",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,X-PI-Agent-UI",
                },
            )
            # Preflight response——either 200 (CORS ok) or 400 (CORS reject)
            # Either way——no wildcard
            aco = r.headers.get("access-control-allow-origin", "")
            assert aco != "*"


# ============================================================================
# Path-scope——non-credential paths not affected
# ============================================================================


class TestPathScope:
    def test_non_credential_path_not_affected(self) -> None:
        """Existing API paths should NOT require X-PI-Agent-UI."""
        app = _build_test_app()

        @app.get("/api/something-else")
        async def other() -> dict:
            return {"ok": True}

        with TestClient(app) as client:
            r = client.get("/api/something-else")
            assert r.status_code == 200

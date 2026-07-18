"""Credential API body size limit tests（P1-E1-4B1）.

覆盖 32 KiB body limit——ASGI middleware 在 Pydantic 解析前 enforce.

必测场景：
- Content-Length 已知且超限
- 无 Content-Length
- chunked body 超限
- 伪造较小 Content-Length 实际发更多字节
- 多个 ASGI receive chunk
- 请求中途超过限制
- 错误响应不返回 body 片段
- DELETE / GET 无 body 不受影响
- 非 credential path 不受影响
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py.web.credentials_api import (  # noqa: E402
    CredentialBodyLimitMiddleware,
    build_credential_router,
)
from pi_agent_core_py.web.local_web_security import (  # noqa: E402
    default_web_security_config,
)

# ============================================================================
# Test app
# ============================================================================


def _build_test_app(max_bytes: int = 32 * 1024) -> FastAPI:
    """Build app with body limit middleware + stub credential routes.

    Stub endpoints intentionally echo request——don't connect to real service.
    """
    app = FastAPI()
    config = default_web_security_config(
        extra_hosts=("testserver",),
        extra_ui_origins=(),
        require_ui_header=False,  # focus on body limit only
    )
    router = build_credential_router(config)

    @router.post("/api/provider-hints")
    async def provider_hints() -> dict:
        return {"hint": "ok"}

    @router.post("/api/credentials")
    async def create_credential() -> dict:
        return {"created": True}

    @router.patch("/api/credentials/{credential_id}")
    async def patch_credential(credential_id: str) -> dict:
        return {"patched": credential_id}

    @router.put("/api/credentials/{credential_id}/secret")
    async def rotate_credential(credential_id: str) -> dict:
        return {"rotated": credential_id}

    @router.post("/api/credentials/{credential_id}/validate")
    async def validate_credential(credential_id: str) -> dict:
        return {"validated": credential_id}

    @router.get("/api/provider-definitions")
    async def list_providers() -> dict:
        return {"providers": []}

    @router.get("/api/credentials")
    async def list_creds() -> dict:
        return {"creds": []}

    @router.delete("/api/credentials/{credential_id}")
    async def delete_credential(credential_id: str) -> dict:
        return {"deleted": credential_id}

    app.include_router(router)
    # Body limit middleware——outermost after TrustedHost (installed elsewhere)
    app.add_middleware(CredentialBodyLimitMiddleware, max_bytes=max_bytes)
    return app


# ============================================================================
# Content-Length pre-check
# ============================================================================


class TestContentLengthPreCheck:
    def test_content_length_over_limit_rejected_before_parsing(self) -> None:
        """Short path——Content-Length header declares overflow → 413."""
        app = _build_test_app(max_bytes=100)
        big_body = "x" * 200
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                data=big_body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "200",
                },
            )
            assert r.status_code == 413
            assert r.json()["error"]["code"] == "request_body_too_large"

    def test_content_length_exactly_at_limit_accepted(self) -> None:
        """Content-Length == limit——boundary."""
        app = _build_test_app(max_bytes=100)
        body = json.dumps({"secret_value": "x" * 80})
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code != 413


# ============================================================================
# Chunked / no Content-Length
# ============================================================================


class TestChunkedBody:
    def test_chunked_body_over_limit_rejected(self) -> None:
        """Body sent in chunks without Content-Length——counted at stream."""
        app = _build_test_app(max_bytes=100)

        # Build a raw ASGI test——TestClient always sets Content-Length, so we
        # verify chunked via direct middleware invocation
        from starlette.testclient import TestClient as _TC

        # Use httpx content parameter to force chunked
        big_body = "x" * 200

        with _TC(app) as client:
            # httpx may convert to chunked if content is generator
            def _gen():
                yield big_body[:100].encode()
                yield big_body[100:].encode()

            r = client.post(
                "/api/provider-hints",
                content=_gen(),
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 413


# ============================================================================
# Forged Content-Length (declared small, actually large)
# ============================================================================


class TestForgedContentLength:
    def test_forged_small_content_length_still_rejects_actual_body(
        self, tmp_path
    ) -> None:
        """Client lies: Content-Length=10 but sends 200 bytes.

        Middleware must count actual bytes received——reject when total > limit.
        """
        # TestClient won't let us forge Content-Length easily; we test by
        # direct ASGI call.
        import asyncio

        from pi_agent_core_py.web.credentials_api import (
            CredentialBodyLimitMiddleware,
        )

        captured: dict = {}

        async def _inner_app(scope, receive, send):
            captured["called"] = True
            # Read body
            body = b""
            while True:
                msg = await receive()
                if msg.get("type") == "http.request":
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
            captured["body_size"] = len(body)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"ok":true}',
                }
            )

        middleware = CredentialBodyLimitMiddleware(
            _inner_app, max_bytes=100
        )

        big_body = b"x" * 200

        async def _receive():
            # Forge Content-Length=10 but yield 200 bytes
            await asyncio.sleep(0)
            return {
                "type": "http.request",
                "body": big_body,
                "more_body": False,
            }

        sent_messages: list = []

        async def _send(m):
            sent_messages.append(m)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/provider-hints",
            "headers": [(b"content-length", b"10")],  # forged small
        }

        asyncio.run(middleware(scope, _receive, _send))

        # Inner app should NOT have been called with full body——middleware
        # truncated or short-circuited
        status_codes = [
            m.get("status") for m in sent_messages if m["type"] == "http.response.start"
        ]
        assert 413 in status_codes


# ============================================================================
# Multiple ASGI chunks
# ============================================================================


class TestMultipleChunks:
    def test_body_in_small_chunks_accumulated(self) -> None:
        """Body sent as 10 chunks of 50 bytes each = 500 bytes total.

        Limit=100——should reject after 3rd chunk."""
        import asyncio

        from pi_agent_core_py.web.credentials_api import (
            CredentialBodyLimitMiddleware,
        )

        async def _inner_app(scope, receive, send):
            body = b""
            while True:
                msg = await receive()
                if msg.get("type") == "http.request":
                    body += msg.get("body", b"")
                    if not msg.get("more_body", False):
                        break
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = CredentialBodyLimitMiddleware(
            _inner_app, max_bytes=100
        )

        chunks = [b"x" * 50 for _ in range(10)]

        async def _receive():
            if chunks:
                chunk = chunks.pop(0)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": bool(chunks),
                }
            return {"type": "http.request", "body": b"", "more_body": False}

        sent: list = []

        async def _send(m):
            sent.append(m)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/provider-hints",
            "headers": [],
        }
        asyncio.run(middleware(scope, _receive, _send))

        statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
        assert 413 in statuses


# ============================================================================
# Error response does not echo body
# ============================================================================


class TestNoBodyEcho:
    def test_413_response_does_not_echo_body_fragment(self) -> None:
        """Body containing SECRET_MARKER——413 response must not echo it."""
        app = _build_test_app(max_bytes=100)
        body = json.dumps({"secret_value": "PI_E1_SECRET_MARKER_7F3A91D2" + "x" * 200})
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 413
            text = r.text
            assert "PI_E1_SECRET_MARKER_7F3A91D2" not in text


# ============================================================================
# GET / DELETE——no body limit (but body would be unexpected)
# ============================================================================


class TestGetMethodNoBodyEnforcement:
    def test_get_without_body_accepted(self) -> None:
        """GET typically has no body——middleware skips."""
        app = _build_test_app(max_bytes=10)
        with TestClient(app) as client:
            r = client.get("/api/credentials")
            assert r.status_code == 200

    def test_delete_without_body_accepted(self) -> None:
        app = _build_test_app(max_bytes=10)
        with TestClient(app) as client:
            r = client.delete("/api/credentials/cred-x")
            assert r.status_code == 200


# ============================================================================
# Non-credential paths——no body limit
# ============================================================================


class TestNonCredentialPath:
    def test_non_credential_path_large_body_accepted(self) -> None:
        """Large body on non-credential endpoint——middleware skips."""
        app = _build_test_app(max_bytes=10)

        @app.post("/api/something-else")
        async def other() -> dict:
            return {"ok": True}

        big_body = "x" * 1000
        with TestClient(app) as client:
            r = client.post(
                "/api/something-else",
                content=big_body,
                headers={"Content-Type": "application/json"},
            )
            # No body limit——request succeeds (body doesn't match any DTO
            # but our stub doesn't parse)
            assert r.status_code != 413

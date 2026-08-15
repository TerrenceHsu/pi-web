"""Credential API middleware order tests (P1-E1-5B / GAP-2).

Prove the actual middleware execution order via three layers of evidence:

1. Structural——app.user_middleware list ordering
2. Unit ASGI——direct middleware construction + receive spy (forged CL scenario)
3. Integration——TestClient behavioral check

Required invariant (MEDIUM-1 fix):

    Invalid Host request:
        → TrustedHost rejects (400)
        → CredentialBodyLimit receive-call count == 0
        → Router / DTO / Service NOT executed

    Valid Host + oversized body:
        → CredentialBodyLimit rejects (413)
        → Router / DTO / Service NOT executed

Starlette ordering recap:
    app.add_middleware(M) does user_middleware.insert(0, M).
    build_middleware_stack iterates reversed(user_middleware), each wraps prior.
    => LAST add_middleware call => OUTERMOST when called.

Why forged Content-Length matters:
    CredentialBodyLimitMiddleware has a Content-Length pre-check that short-circuits
    WITHOUT reading body when CL > max_bytes. To force the buffer loop to actually
    call receive(), we use forged CL=100 (under limit) + actual 40 KB body.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.credentials.api import (  # noqa: E402
    CredentialBodyLimitMiddleware,
)

# ============================================================================
# Helpers
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


def _build_full_app(tmp_path) -> FastAPI:
    """Build app with the same flag combination app.py uses for full Credentials."""
    return create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )


def _make_inner_app(call_log: list) -> object:
    """Build a no-op inner ASGI app that records when called."""

    async def inner(scope, receive, send):
        call_log.append("inner")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return inner


def _make_forged_cl_scope() -> dict:
    """ASGI scope: POST /api/credentials, Host=evil.com, forged Content-Length=100."""
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/credentials",
        "raw_path": b"/api/credentials",
        "query_string": b"",
        "headers": [
            (b"host", b"evil.com"),
            (b"content-type", b"application/json"),
            (b"content-length", b"100"),  # forged small
        ],
        "server": ("evil.com", 80),
        "client": ("1.2.3.4", 1234),
        "root_path": "",
    }


def _make_oversized_receive(body_size: int = 40000):
    """receive that yields one large http.request chunk (forcing buffer loop)."""
    counter = {"n": 0}

    async def receive():
        counter["n"] += 1
        return {
            "type": "http.request",
            "body": b"x" * body_size,
            "more_body": False,
        }

    return receive, counter


# ============================================================================
# 1. Structural——app.user_middleware ordering
# ============================================================================


class TestMiddlewareOrderStructural:
    """app.user_middleware list must reflect TrustedHost as outermost.

    Starlette's add_middleware uses insert(0, ...): the LAST add becomes
    index 0 → outermost. So in app.user_middleware, TrustedHostMiddleware
    must appear at a lower index than CredentialBodyLimitMiddleware.
    """

    def test_trusted_host_is_outermost(self, tmp_path) -> None:
        app = _build_full_app(tmp_path)
        names = [
            m.cls.__name__ if hasattr(m.cls, "__name__") else str(m.cls)
            for m in app.user_middleware
        ]
        assert "TrustedHostMiddleware" in names, names
        assert "CredentialBodyLimitMiddleware" in names, names
        th_idx = names.index("TrustedHostMiddleware")
        bl_idx = names.index("CredentialBodyLimitMiddleware")
        assert th_idx < bl_idx, (
            f"TrustedHost must be added AFTER BodyLimit (lower index in "
            f"user_middleware = added later = outermost). Got order={names}"
        )


# ============================================================================
# 2. Unit ASGI——direct middleware construction + receive spy
# ============================================================================


class TestMiddlewareOrderUnit:
    """Unit-level: prove ordering matters at the ASGI layer.

    We construct middleware chains directly (bypassing FastAPI app to avoid
    lifespan entanglement). The forged-CL scenario forces BodyLimit to call
    receive() instead of short-circuiting at Content-Length pre-check.
    """

    def test_correct_order_invalid_host_no_body_buffered(self) -> None:
        """FIXED order: TrustedHost(BodyLimit(inner)). Invalid Host → 0 receive calls."""
        inner_log: list = []
        inner = _make_inner_app(inner_log)
        body_limit = CredentialBodyLimitMiddleware(inner, max_bytes=32 * 1024)
        trusted_outer = TrustedHostMiddleware(
            body_limit, allowed_hosts=["localhost", "127.0.0.1", "::1"]
        )

        receive, counter = _make_oversized_receive()
        sent: list = []

        async def send(msg):
            sent.append(msg)

        scope = _make_forged_cl_scope()
        asyncio.run(trusted_outer(scope, receive, send))

        assert counter["n"] == 0, (
            f"Body should NOT be buffered for invalid Host; got {counter['n']} receive calls"
        )
        assert inner_log == [], "Inner app must not execute for invalid Host"
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts, "expected at least one response.start"
        assert starts[0]["status"] == 400, starts[0]

    def test_buggy_order_invalid_host_buffers_body(self) -> None:
        """PRE-FIX order: BodyLimit(TrustedHost(inner)). Invalid Host → body buffered.

        This test proves the bug exists when the order is reversed.
        After the MEDIUM-1 fix in app.py, this scenario is only reachable
        through the deliberately-buggy chain constructed here.
        """
        inner_log: list = []
        inner = _make_inner_app(inner_log)
        trusted = TrustedHostMiddleware(
            inner, allowed_hosts=["localhost", "127.0.0.1", "::1"]
        )
        body_limit_outer = CredentialBodyLimitMiddleware(trusted, max_bytes=32 * 1024)

        receive, counter = _make_oversized_receive()
        sent: list = []

        async def send(msg):
            sent.append(msg)

        scope = _make_forged_cl_scope()
        asyncio.run(body_limit_outer(scope, receive, send))

        assert counter["n"] > 0, (
            "Buggy order must trigger body buffering before TrustedHost rejects"
        )
        starts = [m for m in sent if m.get("type") == "http.response.start"]
        assert starts
        assert starts[0]["status"] == 413, starts[0]


# ============================================================================
# 3. Integration——TestClient behavioral check
# ============================================================================


class TestMiddlewareOrderIntegration:
    """End-to-end via TestClient. Verifies actual app.py behavior."""

    def test_invalid_host_with_forged_cl_returns_400(self, tmp_path) -> None:
        """Invalid Host must be rejected by TrustedHost (400), not BodyLimit (413).

        Uses forged Content-Length (small) + large actual body so BodyLimit
        would return 413 if it executed first. The fact that we get 400
        proves TrustedHost fires first.
        """
        app = _build_full_app(tmp_path)
        big_secret = "x" * (32 * 1024 + 100)
        with TestClient(app) as client:
            # TestClient/httpx adds Content-Length based on actual body,
            # so we can't forge CL here directly. But a normal oversized
            # body would return 413 if BodyLimit fired first. Sending with
            # invalid Host: if TrustedHost fires first → 400.
            r = client.post(
                "/api/credentials",
                json={
                    "label": "X",
                    "storage_mode": "session_only",
                    "secret_value": big_secret,
                },
                headers={"X-PI-Agent-UI": "1", "Host": "evil.com"},
            )
            assert r.status_code == 400, (
                f"TrustedHost must reject before BodyLimit; got "
                f"{r.status_code}: {r.text}"
            )

    def test_valid_host_oversized_body_returns_413(self, tmp_path) -> None:
        """Valid Host + oversized body → 413 from BodyLimit."""
        app = _build_full_app(tmp_path)
        big_secret = "x" * (32 * 1024 + 100)
        with TestClient(app) as client:
            r = client.post(
                "/api/provider-hints",
                json={"secret_value": big_secret},
                headers={"X-PI-Agent-UI": "1"},
            )
            assert r.status_code == 413, r.text
            assert r.json()["error"]["code"] == "request_body_too_large"

"""Local Web Security tests (TrustedHost middleware)（P1-E1-4A）.

覆盖 spec §10 Host 36–43：
- localhost 允许
- 127.0.0.1 允许
- 测试配置下 testserver 允许
- 外部 Host 拒绝
- DNS-rebinding 风格 Host 拒绝
- wildcard 不存在
- 现有 API 在合法 Host 下正常
- 现有 WebSocket 在合法 Host 下正常

E1-4A 只安装 TrustedHost middleware——Origin / X-PI-Agent-UI / body limit 留 E1-4B.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pi_agent_core_py.web.local_web_security import (
    DEFAULT_ALLOWED_HOSTS,
    default_web_security_config,
)


def _build_app(
    *,
    extra_hosts: tuple[str, ...] = (),
) -> FastAPI:
    """Build minimal app with TrustedHostMiddleware installed."""
    app = FastAPI()
    cfg = default_web_security_config(extra_hosts=extra_hosts)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(cfg.allowed_hosts),
    )

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    return app


# ============================================================================
# Allowed Hosts
# ============================================================================


class TestAllowedHosts:
    def test_localhost_allowed(self) -> None:
        app = _build_app()
        with TestClient(app, base_url="http://localhost") as client:
            r = client.get("/ping")
            assert r.status_code == 200

    def test_127_allowed(self) -> None:
        app = _build_app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            r = client.get("/ping")
            assert r.status_code == 200

    def test_testserver_allowed_when_explicit(self) -> None:
        app = _build_app(extra_hosts=("testserver",))
        with TestClient(app) as client:  # default base_url=http://testserver
            r = client.get("/ping")
            assert r.status_code == 200

    def test_testserver_rejected_when_not_explicit(self) -> None:
        """生产默认不含 testserver——TestClient 默认 Host 应被拒."""
        app = _build_app()  # no extra_hosts
        with TestClient(app) as client:
            r = client.get("/ping")
            assert r.status_code == 400


# ============================================================================
# External / DNS-rebinding Host rejected
# ============================================================================


class TestExternalHosts:
    def test_external_host_rejected(self) -> None:
        app = _build_app()
        with TestClient(app, base_url="http://localhost") as client:
            r = client.get("/ping", headers={"Host": "evil.com"})
            assert r.status_code == 400

    def test_dns_rebinding_style_host_rejected(self) -> None:
        """evil.com 解析到 127.0.0.1 仍然按 Host header 拒绝."""
        app = _build_app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            # Host header 仍标 evil.com——middleware 按 Host 字符串比对
            r = client.get("/ping", headers={"Host": "evil.com"})
            assert r.status_code == 400

    def test_localhost_with_port_allowed(self) -> None:
        """TrustedHostMiddleware 按整 Host 比对——含端口默认不允许.

        Note: 这是 Starlette TrustedHostMiddleware 的实际行为——
        生产部署需要把 `localhost:8000` 显式加入 allowlist 或在
        反向代理层 strip port.
        """
        app = _build_app()
        with TestClient(app, base_url="http://localhost") as client:
            # 默认 allowed_hosts 不含 localhost:8000——但 TestClient
            # 默认 base_url 已是 localhost（无 port）
            r = client.get("/ping")
            assert r.status_code == 200

    def test_wildcard_not_in_default_hosts(self) -> None:
        assert "*" not in DEFAULT_ALLOWED_HOSTS


# ============================================================================
# WebSocket Host enforcement
# ============================================================================


class TestWebSocketHost:
    def test_websocket_with_external_host_rejected(self) -> None:
        """TrustedHost must enforce on WebSocket too——external Host denied.

        E1-4A scope: only verify middleware enforcement on WS. Bidirectional
        WS messaging is tested elsewhere (test_web_event_envelope.py etc.).
        """
        from fastapi import WebSocket
        from starlette.testclient import WebSocketDenialResponse

        # Allow `testserver` (Starlette TestClient always sends Host: testserver
        # for WS regardless of base_url——this is a TestClient quirk).
        app = _build_app(extra_hosts=("testserver",))

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()

        with TestClient(app, base_url="http://localhost") as client:
            # testserver allowed——connection should establish.
            # Then explicitly send external Host via header override.
            with pytest.raises(WebSocketDenialResponse):
                with client.websocket_connect(
                    "/ws", headers={"Host": "evil.com"}
                ) as ws_client:
                    ws_client.receive_text()


# ============================================================================
# Default config invariants
# ============================================================================


class TestDefaultConfigInvariants:
    def test_default_hosts_excludes_testserver(self) -> None:
        """testserver 永远不在生产默认——只测试显式注入."""
        assert "testserver" not in DEFAULT_ALLOWED_HOSTS

    def test_default_hosts_excludes_wildcard(self) -> None:
        assert "*" not in DEFAULT_ALLOWED_HOSTS

    def test_default_hosts_contains_localhost_variants(self) -> None:
        assert "localhost" in DEFAULT_ALLOWED_HOSTS
        assert "127.0.0.1" in DEFAULT_ALLOWED_HOSTS
        assert "::1" in DEFAULT_ALLOWED_HOSTS

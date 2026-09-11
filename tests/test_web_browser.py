from __future__ import annotations

import asyncio
import base64
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.network import (
    BrowserNetworkDenied,
    PublicBrowserProxy,
    destination,
    public_address,
    resolve_public,
)
from pi_agent_core_py.web.browser.runtime import (
    BrowserEngine,
    BrowserError,
    BrowserSession,
    LocalBrowserRuntime,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost",
        "https://localhost.",
        "http://10.0.0.1",
        "http://169.254.169.254",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "https://example.com:8000",
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
        "https://user:password@example.com",
        "https://example.com\\@localhost",
        "http://host.local",
    ],
)
def test_browser_rejects_private_or_unsafe_navigation(url: str) -> None:
    with pytest.raises(BrowserNetworkDenied):
        destination(url)


def test_public_urls_and_ip_validation() -> None:
    assert destination("https://example.com/path?q=1") == ("example.com", 443)
    assert destination("http://93.184.216.34") == ("93.184.216.34", 80)
    assert public_address("8.8.8.8")
    assert not public_address("224.0.0.1")
    assert not public_address("192.168.1.1")


async def test_mixed_dns_answer_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(BrowserNetworkDenied):
        await resolve_public("example.com", 443)


async def test_proxy_requires_credentials_and_denies_loopback() -> None:
    proxy = PublicBrowserProxy()
    url = await proxy.start()
    port = int(url.rsplit(":", 1)[1])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert b"407" in await reader.read()
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        auth = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
        writer.write(
            f"CONNECT 127.0.0.1:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode()
        )
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()
    assert not proxy.connections


async def test_runtime_is_lazy_and_deleted_sessions_cannot_reopen() -> None:
    runtime = LocalBrowserRuntime()
    assert await runtime.command("one", None, {"action": "list"}) == {"pages": []}
    assert runtime.thread is None
    await runtime.delete_session("one")
    with pytest.raises(BrowserError, match="deleted"):
        await runtime.command("one", None, {"action": "create"})
    assert runtime.thread is None
    await runtime.close()


def test_api_ownership_validation_no_secret_echo_and_session_cleanup(tmp_path: Path) -> None:
    app = create_app(
        AgentHarness(Agent(system_prompt="test", client=FakeClient([]), tools=None)),
        db_path=str(tmp_path / "web.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
    )
    runtime = app.state.browser_runtime
    runtime.command = AsyncMock(return_value={"pages": []})
    original_delete = runtime.delete_session
    runtime.delete_session = AsyncMock(wraps=original_delete)
    headers = {"X-PI-Agent-UI": "1"}
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "Browser fixture"}).json()["id"]
        prefix = f"/api/sessions/{sid}/browser"
        assert client.get(prefix).status_code == 403
        assert (
            client.get(prefix, headers={**headers, "Origin": "https://hostile.example"}).status_code
            == 403
        )
        missing = client.get("/api/sessions/missing/browser", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "browser_session_not_found"
        result = client.get(prefix, headers=headers)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        result = client.post(
            prefix + "/pages/p/action",
            headers=headers,
            json={
                "action": "evaluate",
                "text": "private-fixture-password",
                "x": 9000,
            },
        )
        assert result.status_code == 422 and "private-fixture-password" not in result.text
        result = client.post(prefix + "/pages/p/action", headers=headers, content="x" * 30_000)
        assert result.status_code == 413
        runtime.command.side_effect = RuntimeError("unused")
        runtime.delete_session.side_effect = BrowserError("browser_timeout")
        assert client.delete(f"/api/sessions/{sid}").status_code == 500
        assert client.get(f"/api/sessions/{sid}").status_code == 200
        runtime.delete_session.side_effect = None
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        missing = client.post(
            prefix + "/pages/p/action", headers=headers,
            json={"action": "media_stop", "media_generation": "owned-generation"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "browser_session_not_found"
        denied = client.post(
            prefix + "/pages/p/action",
            json={"action": "media_stop", "media_generation": "owned-generation"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "browser_request_rejected"
        assert runtime.delete_session.await_count == 2


@pytest.mark.parametrize(
    "data",
    [
        {"action": "click", "x": float("inf")},
        {"action": "text", "text": "x" * 4097},
        {"action": "key", "key": "x" * 65},
    ],
)
def test_action_payload_is_bounded(data: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BrowserAction.model_validate(data)


async def test_browser_context_quota_survives_concurrent_sessions() -> None:
    engine = BrowserEngine()
    engine.start = AsyncMock()  # type: ignore[method-assign]
    context = SimpleNamespace(route=AsyncMock(), on=Mock())
    engine.browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    results = await asyncio.gather(
        *(engine.new_context(str(index)) for index in range(4)), return_exceptions=True
    )
    assert len(engine.sessions) == 3
    assert engine.browser.new_context.await_count == 3
    assert sum(isinstance(result, BrowserError) for result in results) == 1


async def test_browser_polling_keeps_idle_deadline_and_rejects_stale_coordinates() -> None:
    engine = BrowserEngine()
    page = SimpleNamespace(
        viewport_size={"width": 640, "height": 480},
        is_closed=lambda: False,
        title=AsyncMock(return_value="fixture"),
        url="https://example.com",
        screenshot=AsyncMock(return_value=b"frame"),
        set_viewport_size=AsyncMock(),
        mouse=SimpleNamespace(click=AsyncMock()),
    )
    session = BrowserSession(context=None, pages={"p": page}, touched=0)
    engine.sessions["s"] = session
    for action in ["list", "info", "frame", "resize"]:
        await engine.command("s", "p", {"action": action, "width": 640, "height": 480})
    assert session.touched == 0
    with pytest.raises(BrowserError, match="frame_changed"):
        await engine.command("s", "p", BrowserAction(action="click").model_dump())
    page.mouse.click.assert_not_awaited()
    await engine.command(
        "s", "p", BrowserAction(action="click", width=640, height=480, x=10, y=20).model_dump()
    )
    page.mouse.click.assert_awaited_once_with(10, 20, button="left", click_count=1)
    assert session.touched > 0
    with pytest.raises(BrowserError, match="page_not_found"):
        await engine.command("other", "p", {"action": "info"})

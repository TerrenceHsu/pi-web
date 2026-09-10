"""Capture errors retain fixed public codes across runtime, REST and WebSocket."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.frames import BrowserFrame, PageFrames
from pi_agent_core_py.web.browser.runtime import (
    BrowserEngine,
    BrowserError,
    BrowserSession,
    LocalBrowserRuntime,
)

CAPTURE_ERROR = "browser_capture_failed"
PRIVATE_ERROR = "Authorization: Bearer synthetic-secret; Cookie: synthetic-cookie; private-CDP-path"


def install_view(engine: BrowserEngine, sid: str = "s") -> PageFrames:
    page = SimpleNamespace(
        viewport_size={"width": 640, "height": 480},
        is_closed=lambda: False,
        title=AsyncMock(return_value="Capture fixture"),
        url="https://fixture.example.test/",
        set_viewport_size=AsyncMock(),
    )
    cdp = SimpleNamespace(send=AsyncMock(), on=Mock(), remove_listener=Mock(), detach=AsyncMock())
    view = PageFrames(page, cdp)
    engine.sessions[sid] = BrowserSession(
        context=SimpleNamespace(close=AsyncMock()), pages={"p": page}, views={"p": view}
    )
    return view


async def test_failed_subscribe_releases_reserved_slot_and_preserves_other_viewers() -> None:
    engine = BrowserEngine()
    view = install_view(engine)
    view.still = AsyncMock(side_effect=RuntimeError(PRIVATE_ERROR))  # type: ignore[method-assign]
    # The failing subscription reserves the final slot before it awaits capture.
    retained = {f"other-{index}": ("s", "p", view) for index in range(5)}
    engine.streams.update(retained)
    try:
        for index in range(8):
            with pytest.raises(BrowserError) as error:
                await engine.subscribe("s", "p", f"failed-{index}")
            assert error.value.code == CAPTURE_ERROR
            assert str(error.value) == CAPTURE_ERROR
            assert error.value.__suppress_context__
            assert engine.streams == retained
            assert view.subscribers == {}
        assert view.still.await_count == 3

        # Explicit recovery can use the released sixth slot; account limits still
        # reject a seventh viewer and leave the existing reservations untouched.
        await view.resize(640, 480, 1)
        view.start = AsyncMock()  # type: ignore[method-assign]
        await engine.subscribe("s", "p", "recovered")
        assert len(engine.streams) == 6 and "recovered" in view.subscribers
        with pytest.raises(BrowserError, match="^browser_stream_limit$"):
            await engine.subscribe("s", "p", "seventh")
        await engine.unsubscribe("recovered")
        assert engine.streams == retained and view.subscribers == {}
    finally:
        await engine.close()


@pytest.mark.parametrize("error_code", [CAPTURE_ERROR, None])
async def test_terminal_frame_signal_is_capture_failure_not_page_closed(
    error_code: str | None,
) -> None:
    engine = BrowserEngine()
    view = install_view(engine)
    view.error_code = error_code
    queue: asyncio.Queue[BrowserFrame | None] = asyncio.Queue(maxsize=1)
    queue.put_nowait(None)
    view.subscribers["viewer"] = queue
    engine.streams["viewer"] = ("s", "p", view)
    try:
        with pytest.raises(BrowserError, match="^browser_capture_failed$"):
            await engine.next_frame("viewer")
        assert "p" in engine.sessions["s"].pages and not view.closed
        await engine.unsubscribe("viewer")
        assert engine.streams == {} and view.subscribers == {}
    finally:
        await engine.close()


async def test_quiet_page_heartbeat_preserves_subscription_and_is_not_capture_failure() -> None:
    engine = BrowserEngine()
    view = install_view(engine)
    view.start = AsyncMock()  # type: ignore[method-assign]
    await engine.subscribe("s", "p", "viewer")
    touched = engine.sessions["s"].touched
    try:
        assert await engine.next_frame("viewer") is None
        assert view.error_code is None and "viewer" in engine.streams
        assert engine.sessions["s"].touched == touched
        await engine.unsubscribe("viewer")
        assert engine.streams == {} and view.subscribers == {}
    finally:
        await engine.close()


@pytest.mark.parametrize("closed", ["page", "view", "session"])
async def test_real_page_close_takes_precedence_over_capture_failure(closed: str) -> None:
    engine = BrowserEngine()
    view = install_view(engine)
    view.error_code = CAPTURE_ERROR
    engine.streams["viewer"] = ("s", "p", view)
    if closed == "page":
        engine.sessions["s"].pages.clear()
    elif closed == "view":
        view.closed = True
    else:
        engine.sessions.clear()
    try:
        with pytest.raises(BrowserError, match="^browser_page_not_found$"):
            await engine.next_frame("viewer")
    finally:
        await view.close()
        await engine.close()


async def test_describe_and_resize_keep_capture_error_separate_from_sdk_exception() -> None:
    engine = BrowserEngine()
    view = install_view(engine)
    view.error_code = CAPTURE_ERROR
    view.resize = AsyncMock(side_effect=RuntimeError(PRIVATE_ERROR))  # type: ignore[method-assign]
    # A failed title lookup must not accidentally echo the internal exception,
    # either; a still-open page continues to be describable during capture failure.
    view.page.title.side_effect = RuntimeError(PRIVATE_ERROR)
    try:
        info = await engine.describe(engine.sessions["s"], "p")
        assert info["capture_error"] == CAPTURE_ERROR and info["title"] == "Loading…"
        assert PRIVATE_ERROR not in json.dumps(info)
        with pytest.raises(BrowserError) as error:
            await engine.command("s", "p", BrowserAction(action="resize").model_dump())
        assert error.value.code == CAPTURE_ERROR and str(error.value) == CAPTURE_ERROR
        assert error.value.__suppress_context__
    finally:
        await engine.close()


@pytest.fixture
def capture_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, LocalBrowserRuntime, PageFrames]]:
    app = create_app(
        AgentHarness(Agent(system_prompt="test", client=FakeClient([]), tools=None)),
        db_path=str(tmp_path / "web.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
    )
    runtime = app.state.browser_runtime
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "Capture fixture"}).json()["id"]
        # Use the actual runtime thread and HTTP/WS gateways with only the CDP
        # boundary replaced; this does not launch Chromium or make network calls.
        runtime._ensure_thread()

        async def install() -> PageFrames:
            assert runtime.engine
            return install_view(runtime.engine, sid)

        assert client.portal
        view = client.portal.call(runtime._submit, install())
        yield client, sid, runtime, view


def test_rest_capture_error_is_fixed_private_and_page_remains_available(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
) -> None:
    client, sid, runtime, view = capture_client
    view.page.set_viewport_size.side_effect = RuntimeError(PRIVATE_ERROR)
    headers = {"X-PI-Agent-UI": "1", "Origin": "http://testserver"}
    prefix = f"/api/sessions/{sid}/browser/pages/p"
    response = client.post(
        prefix + "/action",
        headers=headers,
        json={"action": "resize", "width": 380, "height": 726, "dpr": 2},
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": CAPTURE_ERROR,
            "message": "The browser frame source could not recover. "
            "Reconnect this view or reopen the tab.",
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert PRIVATE_ERROR not in response.text and "synthetic-secret" not in response.text
    info = client.get(prefix, headers=headers)
    assert info.status_code == 200 and info.json()["capture_error"] == CAPTURE_ERROR
    assert info.headers["cache-control"] == "no-store"
    assert info.headers["x-content-type-options"] == "nosniff"
    assert PRIVATE_ERROR not in info.text
    assert runtime.pending == 0


@pytest.mark.parametrize(
    ("raised", "status", "code", "message"),
    [
        (
            RuntimeError(PRIVATE_ERROR),
            500,
            "browser_internal_error",
            "Local browser unavailable.",
        ),
        (
            HTTPException(
                409, detail=PRIVATE_ERROR, headers={"X-Internal-Diagnostic": PRIVATE_ERROR}
            ),
            409,
            "browser_request_rejected",
            "Browser request rejected.",
        ),
        (
            HTTPException(
                403,
                detail={"code": PRIVATE_ERROR, "message": PRIVATE_ERROR},
                headers={"Set-Cookie": PRIVATE_ERROR},
            ),
            403,
            "browser_request_rejected",
            "Browser request rejected.",
        ),
    ],
    ids=["unknown-error", "http-string-detail", "http-structured-detail"],
)
def test_rest_unexpected_and_http_errors_hide_private_details_and_exception_headers(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
    raised: Exception,
    status: int,
    code: str,
    message: str,
) -> None:
    client, sid, runtime, _ = capture_client
    # Raise at the route boundary to exercise its own cleanup instead of the
    # runtime's earlier BrowserError conversion.
    runtime.command = AsyncMock(side_effect=raised)  # type: ignore[method-assign]
    response = client.get(
        f"/api/sessions/{sid}/browser/pages/p",
        headers={"X-PI-Agent-UI": "1", "Origin": "http://testserver"},
    )
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "message": message}}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-internal-diagnostic" not in response.headers
    assert "synthetic-secret" not in response.text + str(response.headers)
    assert "synthetic-cookie" not in response.text + str(response.headers)
    assert "private-CDP-path" not in response.text + str(response.headers)


def test_rest_validation_retains_safe_field_errors_and_private_headers(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
) -> None:
    client, sid, runtime, _ = capture_client
    runtime.command = AsyncMock()  # type: ignore[method-assign]
    response = client.post(
        f"/api/sessions/{sid}/browser/pages/p/action",
        headers={"X-PI-Agent-UI": "1", "Origin": "http://testserver"},
        json={"action": "resize", "width": PRIVATE_ERROR},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "request_validation_failed",
            "message": "The request body is invalid.",
            "fields": [{"path": "width", "code": "value_error"}],
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert PRIVATE_ERROR not in response.text and "synthetic-secret" not in response.text
    runtime.command.assert_not_awaited()


def test_websocket_failed_capture_setup_is_sanitized_and_does_not_leak_quota(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
) -> None:
    client, sid, runtime, view = capture_client
    view.still = AsyncMock(side_effect=RuntimeError(PRIVATE_ERROR))  # type: ignore[method-assign]
    for _ in range(8):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                f"/ws/browser/{sid}/p",
                headers={"Origin": "http://testserver"},
                subprotocols=["pi-browser-v1"],
            ):
                pytest.fail("A terminal capture source must not establish a Live socket")
        assert error.value.code == 4404 and error.value.reason == ""
        assert PRIVATE_ERROR not in str(error.value)
        assert runtime.engine and runtime.engine.streams == {}
        assert view.subscribers == {}
    assert view.still.await_count == 3 and view.error_code == CAPTURE_ERROR


def test_websocket_inflight_capture_failure_closes_and_unsubscribes_viewer(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
) -> None:
    client, sid, runtime, view = capture_client

    async def start() -> None:
        view.publish(BrowserFrame(b"synthetic pixels", 640, 480, 0, 1, "image/png"))

    view.start = AsyncMock(side_effect=start)  # type: ignore[method-assign]
    with client.websocket_connect(
        f"/ws/browser/{sid}/p",
        headers={"Origin": "http://testserver"},
        subprotocols=["pi-browser-v1"],
    ) as websocket:
        packet = websocket.receive_bytes()
        length = struct.unpack("!I", packet[:4])[0]
        metadata: dict[str, Any] = json.loads(packet[4 : 4 + length])
        assert metadata["seq"] == 1 and metadata["page"]["capture_error"] is None
        assert client.portal
        client.portal.call(runtime._submit, view.fail_source())
        websocket.send_json({"ack": 1})
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_bytes()
        assert error.value.code == 4404 and error.value.reason == ""
    assert runtime.engine and runtime.engine.streams == {}
    assert view.subscribers == {} and view.latest is None
    assert view.error_code == CAPTURE_ERROR and not view.closed


def test_unexpected_websocket_setup_error_is_sanitized_and_releases_reservation(
    capture_client: tuple[TestClient, str, LocalBrowserRuntime, PageFrames],
) -> None:
    client, sid, runtime, view = capture_client
    view.subscribe = AsyncMock(side_effect=OSError(PRIVATE_ERROR))  # type: ignore[method-assign]
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            f"/ws/browser/{sid}/p",
            headers={"Origin": "http://testserver"},
            subprotocols=["pi-browser-v1"],
        ):
            pytest.fail("An unexpected setup error must not establish a Live socket")
    assert error.value.code == 1011 and error.value.reason == ""
    assert PRIVATE_ERROR not in str(error.value)
    assert runtime.engine and runtime.engine.streams == {}
    assert view.subscribers == {}

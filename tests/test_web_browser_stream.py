from __future__ import annotations

import asyncio
import base64
import json
import socket
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.agent.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.browser.frame_geometry import image_size, matches_viewport
from pi_agent_core_py.web.browser.frames import BrowserFrame, PageFrames
from pi_agent_core_py.web.browser.network import connect_public
from pi_agent_core_py.web.browser.runtime import BrowserEngine, LocalBrowserRuntime


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", width, height)
    )


def frame_view() -> PageFrames:
    page = SimpleNamespace(viewport_size={"width": 380, "height": 726})

    async def set_viewport(size: dict[str, int]) -> None:
        page.viewport_size = size

    page.set_viewport_size = AsyncMock(side_effect=set_viewport)
    cdp = SimpleNamespace(send=AsyncMock(), on=Mock(), remove_listener=Mock(), detach=AsyncMock())
    view = PageFrames(page, cdp)
    view.dpr = 2
    return view


def test_image_geometry_rejects_stale_surface_and_malformed_headers() -> None:
    png = b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", 1520, 908)
    jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08" + struct.pack("!HH", 454, 760) + b"\x00"
    assert image_size(png, "image/png") == (1520, 908)
    assert image_size(jpeg, "image/jpeg") == (760, 454)
    assert matches_viewport(png, "image/png", 760, 454, 2)
    assert matches_viewport(jpeg, "image/jpeg", 760, 454, 2)
    assert not matches_viewport(png, "image/png", 760, 454, 1)
    assert not matches_viewport(jpeg, "image/jpeg", 320, 712, 2)
    for malformed in (b"", b"\xff\xd8\xff\xc0\x00\x00", jpeg[:8], b"\xff\xd8\xff\xda\x00\x02"):
        assert image_size(malformed, "image/jpeg") is None
    assert image_size(png[:20], "image/png") is None


async def test_screencast_waits_for_current_surface_before_accepting_motion() -> None:
    order = []
    png = b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + b"IHDR" + struct.pack("!II", 640, 480)

    async def still() -> bytes:
        order.append("paint")
        return png

    async def send(method: str, *args: Any) -> None:
        order.append(method)

    cdp = SimpleNamespace(send=AsyncMock(side_effect=send), on=Mock(), remove_listener=Mock())
    view = PageFrames(SimpleNamespace(viewport_size={"width": 640, "height": 480}), cdp)
    view.still = still  # type: ignore[method-assign]
    try:
        await view.subscribe("viewer")
        assert order[:2] == ["paint", "Page.startScreencast"]
        frame = view.subscribers["viewer"].get_nowait()
        assert frame and frame.mime == "image/png"
    finally:
        await view.unsubscribe("viewer")


async def test_stale_resize_surface_resets_metrics_before_restarting() -> None:
    view = frame_view()
    current = png_header(760, 1452)
    view.still = AsyncMock(side_effect=[png_header(640, 1424), current])  # type: ignore[method-assign]
    try:
        await view.subscribe("viewer")
        frame = view.subscribers["viewer"].get_nowait()
        assert frame and frame.data == current and frame.dpr == 2
        assert view.restarts == 1
        assert [call.args[0] for call in view.cdp.send.await_args_list] == [
            "Emulation.clearDeviceMetricsOverride",
            "Emulation.setDeviceMetricsOverride",
            "Page.startScreencast",
        ]
        assert view.cdp.send.await_args_list[1].args[1] == {
            "width": 380,
            "height": 726,
            "deviceScaleFactor": 2,
            "mobile": False,
        }
    finally:
        await view.close()


async def test_unrecoverable_resize_has_fixed_error_and_no_reconnect_retry_loop() -> None:
    view = frame_view()
    view.still = AsyncMock(return_value=png_header(640, 1424))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="^browser_capture_failed$"):
        await view.subscribe("viewer")
    assert view.restarts == 2
    assert view.still.await_count == 3
    assert view.error_code == "browser_capture_failed"
    assert not view.subscribers and view.worker is None and view.handler is None
    assert view.latest is None
    for index in range(3):
        with pytest.raises(RuntimeError, match="^browser_capture_failed$"):
            await view.subscribe(f"reconnect-{index}")
    assert view.still.await_count == 3
    # An explicit resize, even to the same size, can repair a terminal source.
    view.still = AsyncMock(return_value=png_header(760, 1452))  # type: ignore[method-assign]
    await view.resize(380, 726, 2)
    await view.subscribe("retry")
    assert view.error_code is None and view.version == 1 and view.restarts == 0
    await view.close()


async def test_failed_screencast_start_removes_every_attempts_listener() -> None:
    view = frame_view()
    view.still = AsyncMock(return_value=png_header(760, 1452))  # type: ignore[method-assign]

    async def send(method: str, *args: Any) -> None:
        if method == "Page.startScreencast":
            raise OSError("private CDP error text")

    view.cdp.send.side_effect = send
    with pytest.raises(RuntimeError, match="^browser_capture_failed$"):
        await view.subscribe("viewer")
    assert view.cdp.on.call_count == view.cdp.remove_listener.call_count == 3
    assert view.restarts == 2 and view.worker is None and view.handler is None
    assert view.latest is None and not view.subscribers
    await view.close()


async def test_failed_resize_configuration_closes_existing_viewer() -> None:
    view = frame_view()
    view.still = AsyncMock(return_value=png_header(760, 1452))  # type: ignore[method-assign]
    await view.subscribe("viewer")
    view.page.set_viewport_size.side_effect = OSError("private viewport error")
    with pytest.raises(RuntimeError, match="^browser_capture_failed$"):
        await view.resize(320, 712, 2)
    assert view.subscribers["viewer"].get_nowait() is None
    assert view.error_code == "browser_capture_failed"
    assert view.worker is None and view.handler is None and view.latest is None
    await view.close()


async def test_silent_source_probe_recovers_capture_failure_and_rejects_late_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pi_agent_core_py.web.browser import frames

    monkeypatch.setattr(frames, "SURFACE_PROBE_INTERVAL", 0.01)
    view = frame_view()
    current = png_header(760, 1452)
    view.still = AsyncMock(  # type: ignore[method-assign]
        side_effect=[current, OSError("private capture detail"), TimeoutError(), current]
    )
    # All probe attempts fail until the metrics override is actually reset.
    recovered = asyncio.Event()

    async def send(method: str, *args: Any) -> None:
        if method == "Emulation.setDeviceMetricsOverride":
            view.still = AsyncMock(return_value=current)  # type: ignore[method-assign]
            recovered.set()

    view.cdp.send.side_effect = send
    try:
        await view.subscribe("viewer")
        old_handler = view.handler
        view.subscribers["viewer"].get_nowait()
        # Exhaust the three-probe tolerance without relying on compositor timing.
        view.still = AsyncMock(side_effect=OSError("private capture detail"))  # type: ignore[method-assign]
        await asyncio.wait_for(recovered.wait(), 4)
        frame = await asyncio.wait_for(view.subscribers["viewer"].get(), 1)
        assert frame and frame.data == current and view.restarts == 1
        assert view.handler is not old_handler
        old_handler({"sessionId": 99, "data": base64.b64encode(b"old frame").decode()})
        assert view.events.empty() and 99 not in view.acks
        assert view.error_code is None
    finally:
        await view.close()
    assert view.worker is None and view.handler is None
    view.cdp.detach.assert_awaited_once()


async def test_repeated_recovery_success_does_not_reset_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pi_agent_core_py.web.browser import frames

    monkeypatch.setattr(frames, "SURFACE_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(frames, "MAX_CAPTURE_FAILURES", 1)
    view = frame_view()
    view.still = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            png_header(760, 1452),
            OSError("private"),
            png_header(760, 1452),
            OSError("private"),
            png_header(760, 1452),
            OSError("private"),
        ]
    )
    try:
        await view.subscribe("viewer")
        assert view.worker
        await asyncio.wait_for(asyncio.shield(view.worker), 4)
        assert view.subscribers["viewer"].get_nowait() is None
        assert view.error_code == "browser_capture_failed"
        assert view.restarts == 2 and view.still.await_count == 6
        assert view.latest is None and view.handler is None and view.events.empty()
        assert not view.acks
        count = view.cdp.send.await_count
        await asyncio.sleep(0.05)
        assert view.cdp.send.await_count == count
    finally:
        await view.close()


async def test_unsubscribe_cancels_inflight_recovery_and_releases_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pi_agent_core_py.web.browser import frames

    monkeypatch.setattr(frames, "SURFACE_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(frames, "MAX_CAPTURE_FAILURES", 1)
    view = frame_view()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def send(method: str, *args: Any) -> None:
        if method == "Emulation.clearDeviceMetricsOverride":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    view.cdp.send.side_effect = send
    view.still = AsyncMock(  # type: ignore[method-assign]
        side_effect=[png_header(760, 1452), TimeoutError()]
    )
    await view.subscribe("viewer")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(view.unsubscribe("viewer"), 1)
    assert cancelled.is_set()
    assert not view.subscribers and view.worker is None and view.handler is None
    assert view.latest is None and not view.capturing and view.events.empty() and not view.acks
    assert view.error_code is None
    await view.close()


async def test_stale_motion_is_acknowledged_and_triggers_bounded_recovery() -> None:
    view = frame_view()
    view.still = AsyncMock(return_value=png_header(760, 1452))  # type: ignore[method-assign]
    stale_jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08" + struct.pack("!HH", 712, 320) + b"\x00"
    acked = asyncio.Queue[int]()

    async def send(method: str, params: dict[str, Any] | None = None) -> None:
        if method == "Page.screencastFrameAck":
            assert params
            acked.put_nowait(params["sessionId"])

    view.cdp.send.side_effect = send
    try:
        await view.subscribe("viewer")
        view.subscribers["viewer"].get_nowait()
        for ack in range(3):
            view.handler({"sessionId": ack, "data": base64.b64encode(stale_jpeg).decode()})
            assert await asyncio.wait_for(acked.get(), 1) == ack
        frame = await asyncio.wait_for(view.subscribers["viewer"].get(), 1)
        assert (
            frame
            and frame.mime == "image/png"
            and image_size(frame.data, frame.mime) == (760, 1452)
        )
        assert view.restarts == 1 and not view.acks
    finally:
        await view.close()


async def test_frame_queues_replace_obsolete_pixels_and_replay_latest() -> None:
    view = PageFrames(SimpleNamespace(), SimpleNamespace())
    view.start = AsyncMock()  # type: ignore[method-assign]
    view.stop = AsyncMock()  # type: ignore[method-assign]
    await view.subscribe("first")
    for index in range(100):
        view.publish(BrowserFrame(str(index).encode(), 640, 480, 0, 2, "image/png"))
    assert view.subscribers["first"].qsize() == 1
    await view.subscribe("second")
    assert (await view.subscribers["second"].get()).data == b"99"  # type: ignore[union-attr]
    assert (await view.subscribers["first"].get()).data == b"99"  # type: ignore[union-attr]
    await view.unsubscribe("first")
    view.stop.assert_not_awaited()
    await view.unsubscribe("second")
    view.stop.assert_awaited_once()
    assert view.latest is None


async def test_hd_resize_is_bounded_and_rejects_stale_frames() -> None:
    page = SimpleNamespace(
        viewport_size={"width": 640, "height": 480}, set_viewport_size=AsyncMock()
    )
    cdp = SimpleNamespace(send=AsyncMock())
    view = PageFrames(page, cdp)
    await view.resize(1920, 1400, 2)
    assert 1920 * 1400 * view.dpr**2 <= 4_000_000
    assert view.version == 1
    assert cdp.send.await_args_list[-1].args[0] == "Emulation.setDeviceMetricsOverride"


async def test_slow_navigation_does_not_block_input_or_other_pages() -> None:
    class SlowEngine(BrowserEngine):
        async def command(self, sid: str, pid: str | None, action: dict[str, Any]) -> Any:
            if action["action"] == "navigate":
                await asyncio.sleep(0.4)
            return action["action"]

    runtime = LocalBrowserRuntime()
    runtime.engine_factory = SlowEngine
    navigation = asyncio.create_task(runtime.command("s", "one", {"action": "navigate"}))
    try:
        await asyncio.sleep(0.05)
        assert (
            await asyncio.wait_for(runtime.command("s", "one", {"action": "text"}), 0.2) == "text"
        )
        assert (
            await asyncio.wait_for(runtime.command("s", "two", {"action": "frame"}), 0.2) == "frame"
        )
        assert not navigation.done()
        await navigation
    finally:
        navigation.cancel()
        await asyncio.gather(navigation, return_exceptions=True)
        await runtime.close()


async def test_proxy_falls_back_only_to_prevalidated_numeric_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pi_agent_core_py.web.browser import network

    dns = AsyncMock(
        return_value=[(socket.AF_INET6, "2606:4700:4700::1111"), (socket.AF_INET, "1.1.1.1")]
    )
    reader, writer = Mock(), Mock()
    connect = AsyncMock(side_effect=[OSError("unreachable"), (reader, writer)])
    monkeypatch.setattr(network, "resolve_public", dns)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await connect_public("example.com", 443) == (reader, writer)
    dns.assert_awaited_once_with("example.com", 443)
    assert [call.args[0] for call in connect.await_args_list] == ["2606:4700:4700::1111", "1.1.1.1"]


def test_websocket_origin_protocol_ack_and_cleanup(tmp_path: Path) -> None:
    app = create_app(
        AgentHarness(Agent(system_prompt="test", client=FakeClient([]), tools=None)),
        db_path=str(tmp_path / "web.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
    )
    runtime = app.state.browser_runtime
    runtime.subscribe = AsyncMock(return_value="token")
    runtime.unsubscribe = AsyncMock()
    runtime.next_frame = AsyncMock(return_value=BrowserFrame(b"png", 640, 480, 0, 2, "image/png"))
    runtime.command = AsyncMock(return_value={"width": 320, "height": 200, "view_version": 0})
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "Stream fixture"}).json()["id"]
        url = f"/ws/browser/{sid}/p"
        for origin, protocols in [
            (None, ["pi-browser-v1"]),
            ("null", ["pi-browser-v1"]),
            ("https://hostile.example", ["pi-browser-v1"]),
            ("http://testserver", []),
        ]:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect(
                    url, headers={"Origin": origin} if origin else {}, subprotocols=protocols
                ):
                    pass
            assert exc.value.code == 4403
        runtime.subscribe.assert_not_awaited()
        with client.websocket_connect(
            url, headers={"Origin": "http://testserver"}, subprotocols=["pi-browser-v1"]
        ) as ws:
            raw = ws.receive_bytes()
            length = struct.unpack("!I", raw[:4])[0]
            metadata = json.loads(raw[4 : 4 + length])
            assert metadata["seq"] == 1
            assert metadata["page"] == {"width": 640, "height": 480, "dpr": 2, "view_version": 0}
            assert raw[4 + length :] == b"png"
            ws.send_json({"ack": 1})
            raw = ws.receive_bytes()
            length = struct.unpack("!I", raw[:4])[0]
            assert json.loads(raw[4 : 4 + length])["seq"] == 2
            ws.send_json({"evaluate": "private-not-executed"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4400
        runtime.unsubscribe.assert_awaited_once_with("token")

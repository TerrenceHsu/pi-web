"""WebM transport tests use an isolated ASGI router and no real browser/services."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pi_agent_core_py.web.browser import media_api
from pi_agent_core_py.web.browser.media_api import (
    MIME,
    PROTOCOL,
    PROTOCOL_V2,
    build_browser_media_router,
)
from pi_agent_core_py.web.browser.runtime import BrowserError
from pi_agent_core_py.web.local_web_security import WebSecurityConfig

PRIVATE = "synthetic-private-cookie-and-provider-key"
URL = "/ws/browser-media/session/page"
HEADERS = {"Origin": "http://testserver"}


def chunk(sequence: int = 1, generation: str = "generation-one") -> dict[str, Any]:
    return {
        "generation": generation,
        "seq": sequence,
        "mime": MIME,
        "data": b"\x1a\x45\xdf\xa3fixture-init" if sequence == 1 else b"fixture-cluster",
        "page": {
            "id": "page",
            "width": 1920,
            "height": 1080,
            "dpr": 1,
            "view_version": 3,
            "title": "Owned capture",
            "url": "https://fixture.example/",
            "dialog": None,
            "navigation_ms": None,
            "capture_error": None,
        },
    }


def runtime_fixture() -> SimpleNamespace:
    return SimpleNamespace(
        subscribe_media=AsyncMock(return_value="viewer-token"),
        next_media=AsyncMock(return_value=chunk()),
        renew_media=AsyncMock(),
        unsubscribe_media=AsyncMock(),
    )


@pytest.fixture
def media_client() -> Iterator[tuple[TestClient, SimpleNamespace, AsyncMock]]:
    runtime = runtime_fixture()
    require_session = AsyncMock()
    app = FastAPI()
    app.include_router(build_browser_media_router(WebSecurityConfig(), runtime, require_session))
    with TestClient(app) as client:
        yield client, runtime, require_session


def decode(packet: bytes) -> tuple[dict[str, Any], bytes]:
    length = struct.unpack("!I", packet[:4])[0]
    return json.loads(packet[4 : 4 + length]), packet[4 + length :]


def assert_error(websocket: Any, code: str, close_code: int = 4404) -> None:
    assert websocket.receive_json() == {"type": "error", "code": code}
    with pytest.raises(WebSocketDisconnect) as error:
        websocket.receive_json()
    assert error.value.code == close_code and error.value.reason == ""
    assert PRIVATE not in str(error.value)


@pytest.mark.parametrize(
    ("origin", "protocols"),
    [
        (None, [PROTOCOL]),
        ("null", [PROTOCOL]),
        ("https://hostile.example", [PROTOCOL]),
        ("http://testserver.hostile.example", [PROTOCOL]),
        ("http://testserver", []),
        ("http://testserver", ["pi-browser-v1"]),
    ],
)
def test_media_origin_and_subprotocol_precede_session_or_runtime_access(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
    origin: str | None,
    protocols: list[str],
) -> None:
    client, runtime, guard = media_client
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            URL, headers={"Origin": origin} if origin else {}, subprotocols=protocols
        ):
            pytest.fail("Untrusted media request was accepted")
    assert error.value.code == 4403
    guard.assert_not_awaited()
    runtime.subscribe_media.assert_not_awaited()


def test_media_missing_session_never_subscribes_or_accepts(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, guard = media_client
    guard.side_effect = HTTPException(404, PRIVATE)
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]):
            pytest.fail("Missing Session was accepted")
    assert error.value.code == 4404 and PRIVATE not in str(error.value)
    runtime.subscribe_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_not_awaited()


@pytest.mark.parametrize("code", ["browser_page_not_found", "browser_media_limit"])
def test_media_runtime_owns_page_and_quota_denial_is_fixed(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock], code: str
) -> None:
    client, runtime, _ = media_client
    runtime.subscribe_media.side_effect = BrowserError(code)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        assert_error(websocket, code)
    runtime.subscribe_media.assert_awaited_once_with("session", "page")
    runtime.next_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_not_awaited()


def test_media_packets_preserve_owned_geometry_and_require_ack_before_next_chunk(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    first = chunk()
    first["page"]["internal_cookie"] = PRIVATE
    first["page"]["capture_error"] = PRIVATE
    runtime.next_media.side_effect = [first, chunk(2), chunk(1, "generation-two")]
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        metadata, payload = decode(websocket.receive_bytes())
        assert metadata == {
            "type": "chunk",
            "generation": "generation-one",
            "seq": 1,
            "mime": MIME,
            "page": {**chunk()["page"], "capture_error": "browser_capture_failed"},
        }
        assert payload == first["data"] and PRIVATE not in json.dumps(metadata)
        assert runtime.next_media.await_count == 1
        runtime.renew_media.assert_not_awaited()
        websocket.send_json({"ack": 1})
        metadata, payload = decode(websocket.receive_bytes())
        assert metadata["seq"] == 2 and payload == b"fixture-cluster"
        assert runtime.next_media.await_count == 2 and runtime.renew_media.await_count == 1
        websocket.send_json({"ack": 2})
        metadata, payload = decode(websocket.receive_bytes())
        assert metadata["generation"] == "generation-two" and metadata["seq"] == 1
        assert payload.startswith(b"\x1a\x45\xdf\xa3")
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_media_heartbeat_renews_only_after_explicit_watch_response(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    runtime.next_media.side_effect = [None, chunk()]
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        assert websocket.receive_json() == {"type": "heartbeat"}
        runtime.renew_media.assert_not_awaited()
        websocket.send_json({"watch": True})
        assert decode(websocket.receive_bytes())[0]["seq"] == 1
        runtime.renew_media.assert_awaited_once_with("viewer-token")
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_v2_announces_owned_capture_before_data_and_does_not_need_start_ack(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    runtime.subscribe_media.return_value = "generation-one"
    with client.websocket_connect(
        URL, headers={"Origin": "http://testserver"}, subprotocols=[PROTOCOL_V2]
    ) as websocket:
        assert websocket.accepted_subprotocol == PROTOCOL_V2
        assert websocket.receive_json() == {
            "type": "started", "generation": "generation-one", "page_id": "page",
        }
        assert decode(websocket.receive_bytes())[0]["generation"] == "generation-one"
        runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("generation-one")


def test_v2_never_delivers_a_foreign_generation(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    with client.websocket_connect(
        URL, headers={"Origin": "http://testserver"}, subprotocols=[PROTOCOL_V2]
    ) as websocket:
        assert websocket.receive_json()["generation"] == "viewer-token"
        assert_error(websocket, "browser_media_stream_failed")
    runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_v2_rechecks_session_before_exposing_generation(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, guard = media_client
    guard.side_effect = [None, HTTPException(404, PRIVATE)]
    with client.websocket_connect(
        URL, headers={"Origin": "http://testserver"}, subprotocols=[PROTOCOL_V2]
    ) as websocket:
        assert_error(websocket, "browser_session_deleted")
    runtime.next_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_v2_denied_subscription_does_not_announce_or_stop_a_capture(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    runtime.subscribe_media.side_effect = BrowserError("browser_media_limit")
    with client.websocket_connect(
        URL, headers={"Origin": "http://testserver"}, subprotocols=[PROTOCOL_V2]
    ) as websocket:
        assert_error(websocket, "browser_media_limit")
    runtime.unsubscribe_media.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        '{"ack":true}',
        '{"ack":2}',
        '{"ack":1,"watch":true}',
        '{"watch":true}',
        '{"ack":1,"ack":1}',
        "not json",
        "[]",
        " " * 121 + '{"ack":1}',
        json.dumps({"ack": 1, "note": "界" * 50}, ensure_ascii=False),
        b'{"ack":1}',
    ],
)
def test_invalid_ack_never_renews_or_fetches_another_chunk(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock], message: str | bytes
) -> None:
    client, runtime, _ = media_client
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        websocket.receive_bytes()
        if isinstance(message, bytes):
            websocket.send_bytes(message)
        else:
            websocket.send_text(message)
        assert_error(websocket, "browser_media_protocol_error")
    runtime.renew_media.assert_not_awaited()
    assert runtime.next_media.await_count == 1
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


@pytest.mark.parametrize("reply", [{"watch": 1}, {"watch": False}, {"ack": 1}])
def test_heartbeat_requires_true_watch_not_ack_or_integer(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock], reply: dict[str, Any]
) -> None:
    client, runtime, _ = media_client
    runtime.next_media.return_value = None
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        websocket.receive_json()
        websocket.send_json(reply)
        assert_error(websocket, "browser_media_protocol_error")
    runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


@pytest.mark.parametrize(
    "override",
    [
        {"data": b"x" * (media_api.MAX_CHUNK_BYTES + 1)},
        {"data": b"missing-webm-initialization"},
        {"seq": 2},
        {"seq": True},
        {"generation": ""},
        {"mime": "text/html"},
        {"page": {"id": "another-page", "internal_cookie": PRIVATE}},
        {"page": {"id": "page", "title": "x" * (media_api.MAX_HEADER_BYTES + 1)}},
    ],
    ids=[
        "oversized",
        "no-init",
        "no-first-seq",
        "bool-seq",
        "no-generation",
        "mime",
        "page",
        "header",
    ],
)
def test_invalid_or_crosspage_source_chunks_are_rejected_without_data_exposure(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock], override: dict[str, Any]
) -> None:
    client, runtime, _ = media_client
    runtime.next_media.return_value = {**chunk(), **override}
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        assert_error(websocket, "browser_media_stream_failed")
    runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_media_sequence_gap_is_not_sent_as_a_decodable_chunk(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, _ = media_client
    runtime.next_media.side_effect = [chunk(), chunk(3)]
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        websocket.receive_bytes()
        websocket.send_json({"ack": 1})
        assert_error(websocket, "browser_media_stream_failed")
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


@pytest.mark.parametrize(
    ("raised", "expected", "close_code"),
    [
        (BrowserError("browser_media_capture_failed"), "browser_media_capture_failed", 4404),
        (BrowserError(PRIVATE), "browser_media_unavailable", 4404),
        (RuntimeError(PRIVATE), "browser_media_unavailable", 1011),
    ],
)
def test_media_runtime_errors_have_only_whitelisted_public_codes(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
    raised: Exception,
    expected: str,
    close_code: int,
) -> None:
    client, runtime, _ = media_client
    runtime.next_media.side_effect = raised
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        assert_error(websocket, expected, close_code)
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_session_revoked_while_waiting_for_ack_cannot_renew_lease(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock],
) -> None:
    client, runtime, guard = media_client
    guard.side_effect = [None, None, HTTPException(404, PRIVATE)]
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        websocket.receive_bytes()
        websocket.send_json({"ack": 1})
        assert_error(websocket, "browser_session_deleted")
    runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def test_ack_timeout_stops_media_and_does_not_renew(
    media_client: tuple[TestClient, SimpleNamespace, AsyncMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime, _ = media_client
    monkeypatch.setattr(media_api, "RECEIVE_TIMEOUT_SECONDS", 0.01)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=[PROTOCOL]) as websocket:
        websocket.receive_bytes()
        assert_error(websocket, "browser_media_timeout")
    runtime.renew_media.assert_not_awaited()
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


def fake_websocket() -> SimpleNamespace:
    return SimpleNamespace(
        headers={"origin": "http://testserver"},
        scope={"subprotocols": [PROTOCOL]},
        url=SimpleNamespace(scheme="ws", netloc="testserver"),
        accept=AsyncMock(),
        send_bytes=AsyncMock(),
        send_json=AsyncMock(),
        close=AsyncMock(),
    )


async def test_cancelled_media_handler_unsubscribes_inflight_source() -> None:
    runtime = runtime_fixture()
    entered = asyncio.Event()

    async def pending(token: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    runtime.next_media.side_effect = pending
    router = build_browser_media_router(WebSecurityConfig(), runtime, AsyncMock())
    task = asyncio.create_task(router.routes[0].endpoint(fake_websocket(), "session", "page"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")


async def test_blocked_media_send_times_out_and_cleanup_errors_do_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = runtime_fixture()
    websocket = fake_websocket()
    monkeypatch.setattr(media_api, "SEND_TIMEOUT_SECONDS", 0.01)

    async def blocked_send(data: bytes) -> None:
        await asyncio.Event().wait()

    websocket.send_bytes.side_effect = blocked_send
    runtime.unsubscribe_media.side_effect = RuntimeError(PRIVATE)
    router = build_browser_media_router(WebSecurityConfig(), runtime, AsyncMock())
    await router.routes[0].endpoint(websocket, "session", "page")
    websocket.send_json.assert_awaited_once_with({"type": "error", "code": "browser_media_timeout"})
    websocket.close.assert_awaited_once_with(code=4404)
    runtime.unsubscribe_media.assert_awaited_once_with("viewer-token")

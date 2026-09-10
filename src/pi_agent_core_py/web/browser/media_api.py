"""Session-owned WebM transport with one acknowledged chunk per viewer."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..local_web_security import WebSecurityConfig
from .runtime import BrowserError

PROTOCOL = "pi-browser-media-v1"
MIME = "video/webm;codecs=vp8,opus"
MAX_CHUNK_BYTES = 2 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
SEND_TIMEOUT_SECONDS = 5
RECEIVE_TIMEOUT_SECONDS = 10
PUBLIC_ERROR_CODES = frozenset(
    {
        "browser_media_unavailable",
        "browser_media_dependency_missing",
        "browser_media_capture_failed",
        "browser_media_stream_failed",
        "browser_media_limit",
        "browser_media_busy",
        "browser_media_disabled",
        "browser_media_codec_unsupported",
        "browser_media_protocol_error",
        "browser_media_timeout",
        "browser_capture_failed",
        "browser_page_not_found",
        "browser_session_deleted",
        "browser_stream_limit",
        "browser_start_failed",
        "browser_dependency_missing",
        "browser_timeout",
        "browser_busy",
        "browser_action_failed",
        "browser_unavailable",
    }
)
PUBLIC_PAGE_FIELDS = frozenset(
    {
        "id",
        "title",
        "url",
        "dialog",
        "width",
        "height",
        "dpr",
        "view_version",
        "navigation_ms",
        "capture_error",
    }
)


class BrowserMediaRuntime(Protocol):
    async def subscribe_media(self, sid: str, pid: str) -> str: ...

    async def next_media(self, token: str) -> dict[str, Any] | None: ...

    async def renew_media(self, token: str) -> None: ...

    async def unsubscribe_media(self, token: str) -> None: ...


def media_packet(
    chunk: dict[str, Any], pid: str, generation: str | None, sequence: int
) -> tuple[bytes, str, int]:
    data = chunk["data"]
    next_generation = chunk["generation"]
    next_sequence = chunk["seq"]
    page = chunk["page"]
    if (
        not isinstance(data, bytes)
        or not 0 < len(data) <= MAX_CHUNK_BYTES
        or not isinstance(next_generation, str)
        or not 0 < len(next_generation) <= 128
        or type(next_sequence) is not int
        or not 1 <= next_sequence <= 2**53 - 1
        or chunk["mime"] != MIME
        or not isinstance(page, dict)
        or page.get("id") != pid
    ):
        raise BrowserError("browser_media_stream_failed")
    if next_generation != generation:
        if next_sequence != 1 or not data.startswith(b"\x1a\x45\xdf\xa3"):
            raise BrowserError("browser_media_stream_failed")
    elif next_sequence != sequence + 1:
        raise BrowserError("browser_media_stream_failed")
    # Metadata and pixels belong to the same runtime-owned capture. Never query
    # current page info here or accept client-supplied coordinates/source IDs.
    public_page = {key: value for key, value in page.items() if key in PUBLIC_PAGE_FIELDS}
    if public_page.get("capture_error") not in (None, *PUBLIC_ERROR_CODES):
        public_page["capture_error"] = "browser_capture_failed"
    header = json.dumps(
        {
            "type": "chunk",
            "generation": next_generation,
            "seq": next_sequence,
            "mime": MIME,
            "page": public_page,
        },
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(header) > MAX_HEADER_BYTES:
        raise BrowserError("browser_media_stream_failed")
    return struct.pack("!I", len(header)) + header + data, next_generation, next_sequence


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


async def acknowledge(websocket: WebSocket, sequence: int | None) -> None:
    async with asyncio.timeout(RECEIVE_TIMEOUT_SECONDS):
        event = await websocket.receive()
    if event["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(event.get("code", 1000))
    message = event.get("text")
    if not isinstance(message, str) or len(message.encode()) > 128:
        raise BrowserError("browser_media_protocol_error")
    try:
        decoded = json.loads(message, object_pairs_hook=unique_object)
    except ValueError:
        raise BrowserError("browser_media_protocol_error") from None
    if sequence is None:
        valid = isinstance(decoded, dict) and set(decoded) == {"watch"} and decoded["watch"] is True
    else:
        valid = (
            isinstance(decoded, dict)
            and set(decoded) == {"ack"}
            and type(decoded["ack"]) is int
            and decoded["ack"] == sequence
        )
    if not valid:
        raise BrowserError("browser_media_protocol_error")


def build_browser_media_router(
    config: WebSecurityConfig,
    runtime: BrowserMediaRuntime,
    require_session: Callable[[str], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/browser-media/{sid}/{pid}")
    async def browser_media(websocket: WebSocket, sid: str, pid: str) -> None:
        origin = websocket.headers.get("origin")
        same_origin = (
            ("https" if websocket.url.scheme == "wss" else "http") + "://" + websocket.url.netloc
        )
        # The application's /ws/* Cookie/revocation gateway remains in front of
        # this router. Neither this endpoint nor the media runtime exports CDP.
        if (
            not origin
            or origin == "null"
            or origin not in (*config.allowed_ui_origins, same_origin)
            or PROTOCOL not in websocket.scope.get("subprotocols", [])
        ):
            await websocket.close(code=4403)
            return
        token: str | None = None
        accepted = False

        async def fail(code: str, close_code: int) -> None:
            safe_code = code if code in PUBLIC_ERROR_CODES else "browser_media_unavailable"
            try:
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    if accepted:
                        await websocket.send_json({"type": "error", "code": safe_code})
                    await websocket.close(code=close_code)
            except Exception:
                # Sending an error to an already disconnected client cannot
                # prevent the subscription/track cleanup in the outer finally.
                pass

        try:
            await require_session(sid)
            await websocket.accept(subprotocol=PROTOCOL)
            accepted = True
            token = await runtime.subscribe_media(sid, pid)
            generation: str | None = None
            sequence = 0
            while True:
                await require_session(sid)
                chunk = await runtime.next_media(token)
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    if chunk is None:
                        await websocket.send_json({"type": "heartbeat"})
                    else:
                        packet, generation, sequence = media_packet(
                            chunk, pid, generation, sequence
                        )
                        await websocket.send_bytes(packet)
                await acknowledge(websocket, sequence if chunk is not None else None)
                # ACK proves bounded client consumption; idle heartbeats also
                # require an explicit visible-view response. The UI closes on
                # hiding and caps buffered playback while awaiting user play.
                # Queued chunks and open sockets alone never renew this lease.
                await require_session(sid)
                await runtime.renew_media(token)
        except WebSocketDisconnect:
            pass
        except BrowserError as exc:
            await fail(exc.code, 4404)
        except HTTPException:
            await fail("browser_session_deleted", 4404)
        except TimeoutError:
            await fail("browser_media_timeout", 4404)
        except Exception:
            await fail("browser_media_unavailable", 1011)
        finally:
            if token is not None:
                try:
                    await asyncio.wait_for(runtime.unsubscribe_media(token), SEND_TIMEOUT_SECONDS)
                except Exception:
                    pass  # Runtime/context lease expiry is the final cleanup boundary.

    return router

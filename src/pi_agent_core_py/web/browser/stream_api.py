"""Authenticated latest-frame WebSocket with one unacknowledged frame per viewer."""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..local_web_security import WebSecurityConfig
from .runtime import BrowserError, LocalBrowserRuntime

PROTOCOL = "pi-browser-v1"


def build_browser_stream_router(
    config: WebSecurityConfig,
    runtime: LocalBrowserRuntime,
    require_session: Callable[[str], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/browser/{sid}/{pid}")
    async def browser_stream(websocket: WebSocket, sid: str, pid: str) -> None:
        # Browser WS cannot send X-PI-Agent-UI. Require strict Origin + our subprotocol.
        # /ws/* is also dispatched through the existing account Cookie/revocation gateway.
        origin = websocket.headers.get("origin")
        same_origin = (
            ("https" if websocket.url.scheme == "wss" else "http") + "://" + websocket.url.netloc
        )
        if (
            not origin
            or origin == "null"
            or origin not in (*config.allowed_ui_origins, same_origin)
        ):
            await websocket.close(code=4403)
            return
        if PROTOCOL not in websocket.scope.get("subprotocols", []):
            await websocket.close(code=4403)
            return
        token: str | None = None
        try:
            await require_session(sid)
            token = await runtime.subscribe(sid, pid)
            await websocket.accept(subprotocol=PROTOCOL)
            sequence = 0
            while True:
                await require_session(sid)
                frame = await runtime.next_frame(token)
                info = await runtime.command(sid, pid, {"action": "info"})
                # Don't label an old viewport's pixels with a new viewport's coordinates.
                if frame and frame.version != info["view_version"]:
                    continue
                if frame:
                    # Coordinate metadata belongs to the captured pixels, not a
                    # later asynchronous info request (navigation/resize can race).
                    info = {
                        **info,
                        "width": frame.width,
                        "height": frame.height,
                        "dpr": frame.dpr,
                        "view_version": frame.version,
                    }
                sequence += 1
                metadata = {"seq": sequence, "page": info}
                if frame:
                    metadata.update(mime=frame.mime, capture_ms=frame.capture_ms)
                    header = json.dumps(metadata, separators=(",", ":")).encode()
                    packet = struct.pack("!I", len(header)) + header + frame.data
                    async with asyncio.timeout(5):
                        await websocket.send_bytes(packet)
                else:
                    await websocket.send_json(metadata)
                # Browser ACK follows image decode/display, so slow clients cannot build
                # a backlog of obsolete frames. No input or arbitrary CDP commands on WS.
                async with asyncio.timeout(10):
                    message = await websocket.receive_text()
                if len(message) > 128 or json.loads(message) != {"ack": sequence}:
                    await websocket.close(code=4400)
                    return
        except WebSocketDisconnect:
            pass
        except (BrowserError, HTTPException):
            await websocket.close(code=4404)
        except (ValueError, TimeoutError):
            await websocket.close(code=4400)
        except Exception:
            await websocket.close(code=1011)
        finally:
            if token:
                await runtime.unsubscribe(token)

    return router

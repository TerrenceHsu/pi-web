"""A bounded, ordered media generation. Encoded bytes never go to disk."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .errors import BrowserError

MIME = "video/webm;codecs=vp8,opus"
MAX_CHUNK_BYTES = 2 * 1024 * 1024
MAX_QUEUED_BYTES = 8 * 1024 * 1024
WATCH_SECONDS = 15.0
SOURCE_SECONDS = 8.0


class PendingCaptureCleanup:
    """Reserve teardown ownership even if starting a capture never returns its handle."""

    def __init__(self, bridge: Any, context: Any) -> None:
        self.bridge = bridge
        self.context = context

    async def stop(self) -> None:
        await self.bridge.stop_capture(self.context)


class MediaSubscription:
    def __init__(self, sid: str, pid: str, token: str) -> None:
        self.sid = sid
        self.pid = pid
        self.token = token
        self.queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(maxsize=120)
        self.queued_bytes = 0
        self.sequence = 0
        self.handle: Any = None
        self.closed = False
        self.error: str | None = None
        self.deadline = time.monotonic() + WATCH_SECONDS
        self.source_deadline = time.monotonic() + SOURCE_SECONDS

    async def push(self, sequence: int, data: bytes) -> None:
        if self.closed or self.error:
            return
        if (
            type(sequence) is not int
            or sequence != self.sequence + 1
            or not isinstance(data, bytes)
            or not 0 < len(data) <= MAX_CHUNK_BYTES
            or (sequence == 1 and not data.startswith(b"\x1a\x45\xdf\xa3"))
        ):
            self.fail("browser_media_stream_failed")
            raise BrowserError("browser_media_stream_failed")
        if self.queue.full() or self.queued_bytes + len(data) > MAX_QUEUED_BYTES:
            # WebM chunks depend on earlier chunks; latest-only dropping corrupts playback.
            self.fail("browser_media_stream_failed")
            raise BrowserError("browser_media_stream_failed")
        self.sequence = sequence
        self.source_deadline = time.monotonic() + SOURCE_SECONDS
        self.queued_bytes += len(data)
        self.queue.put_nowait((sequence, data))

    def fail(self, code: str) -> None:
        self.error = code
        self.drain()
        self.queue.put_nowait(None)

    def drain(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queued_bytes = 0

    def check_active(self) -> None:
        if self.error or self.closed:
            raise BrowserError(self.error or "browser_media_unavailable")
        if time.monotonic() > self.deadline:
            raise BrowserError("browser_media_timeout")
        if time.monotonic() > self.source_deadline:
            raise BrowserError("browser_media_stream_failed")

    async def next(self) -> tuple[int, bytes] | None:
        self.check_active()
        try:
            item = await asyncio.wait_for(self.queue.get(), 2)
        except TimeoutError:
            self.check_active()
            return None
        if item is None or self.error or self.closed:
            raise BrowserError(self.error or "browser_media_unavailable")
        self.queued_bytes -= len(item[1])
        self.check_active()
        return item

    def begin_watch(self) -> None:
        self.deadline = time.monotonic() + WATCH_SECONDS
        self.source_deadline = time.monotonic() + SOURCE_SECONDS

    def renew(self) -> None:
        self.check_active()
        self.deadline = time.monotonic() + WATCH_SECONDS

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.drain()
            self.queue.put_nowait(None)
        if self.handle:
            await self.handle.stop()
            self.handle = None

"""Internal CDP frame source. Only encoded pixels and bounded metadata leave it."""

from __future__ import annotations

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from typing import Any

from .frame_geometry import matches_viewport

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_SOURCE_RESTARTS = 2
MAX_CAPTURE_FAILURES = 3
SURFACE_PROBE_INTERVAL = 2.0


@dataclass(frozen=True)
class BrowserFrame:
    data: bytes
    width: int
    height: int
    version: int
    dpr: float
    mime: str
    capture_ms: float = 0


class PageFrames:
    """One producer per page, latest-only queues, no screenshot history on disk."""

    def __init__(self, page: Any, cdp: Any) -> None:
        self.page = page
        self.cdp = cdp
        self.version = 0
        self.dpr = 1.0
        self.lock = asyncio.Lock()
        self.subscribers: dict[str, asyncio.Queue[BrowserFrame | None]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self.worker: asyncio.Task[None] | None = None
        self.handler: Any = None
        self.capturing = False
        self.closed = False
        self.acks: set[int] = set()
        self.latest: BrowserFrame | None = None
        self.active_until = time.monotonic() + 1
        self.source_generation = 0
        self.restarts = 0
        self.error_code: str | None = None

    @staticmethod
    def replace(queue: asyncio.Queue[Any], value: Any) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(value)

    def publish(self, frame: BrowserFrame) -> None:
        if (
            not self.closed
            and self.error_code is None
            and frame.version == self.version
            and len(frame.data) <= MAX_FRAME_BYTES
        ):
            self.latest = frame
            for queue in self.subscribers.values():
                self.replace(queue, frame)

    async def still(self) -> bytes:
        # Playwright's screenshot helper reapplies its context DPR. Capture on the
        # same internal CDP session as our per-page metrics override instead.
        result = await asyncio.wait_for(
            self.cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                },
            ),
            2,
        )
        encoded = result["data"]
        if len(encoded) > MAX_FRAME_BYTES * 4 // 3 + 4:
            raise ValueError("browser_frame_too_large")
        return base64.b64decode(encoded)

    async def capture_current(self, version: int) -> BrowserFrame:
        started = time.monotonic()
        self.capturing = True
        try:
            data = await self.still()
        finally:
            self.capturing = False
        size = self.page.viewport_size
        if not matches_viewport(data, "image/png", size["width"], size["height"], self.dpr):
            raise ValueError("browser_frame_geometry_changed")
        return BrowserFrame(
            data,
            size["width"],
            size["height"],
            version,
            self.dpr,
            "image/png",
            round((time.monotonic() - started) * 1000, 1),
        )

    async def start_source(self, version: int) -> None:
        # Metrics change before the compositor finishes resizing. Starting a
        # screencast immediately can label pixels from the previous surface with
        # the new viewport's metadata (and visibly zoom/crop the first frame).
        # Capture a current surface as a paint barrier before accepting events.
        frame = await self.capture_current(version)
        generation = self.source_generation

        def receive(event: dict[str, Any]) -> None:
            # CDP has its own ACK flow control; only one pending event is retained.
            if (
                not self.closed
                and self.error_code is None
                and version == self.version
                and generation == self.source_generation
            ):
                self.acks.add(event["sessionId"])
                event["still_capture"] = self.capturing
                self.replace(self.events, event)

        self.handler = receive
        self.cdp.on("Page.screencastFrame", receive)
        await asyncio.wait_for(
            self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88}), 2
        )
        self.publish(frame)

    async def restore_source(self, version: int) -> bool:
        # A successful capture alone does not replenish this budget: alternating
        # healthy/broken surfaces must not cause unlimited background restarts.
        while self.restarts < MAX_SOURCE_RESTARTS and not self.closed and version == self.version:
            self.restarts += 1
            await self.stop_source()
            try:
                await asyncio.wait_for(self.cdp.send("Emulation.clearDeviceMetricsOverride"), 2)
                size = self.page.viewport_size
                await asyncio.wait_for(
                    self.cdp.send(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": size["width"],
                            "height": size["height"],
                            "deviceScaleFactor": self.dpr,
                            "mobile": False,
                        },
                    ),
                    2,
                )
                await self.start_source(version)
                return True
            except Exception:
                continue
        await self.fail_source()
        return False

    async def fail_source(self) -> None:
        # Fixed state survives disconnect/reconnect. Only an explicit resize can
        # begin a new recovery budget; a dead source cannot remain falsely Live.
        self.error_code = "browser_capture_failed"
        self.latest = None
        for queue in self.subscribers.values():
            self.replace(queue, None)
        await self.stop_source()

    async def start(self) -> None:
        if self.error_code:
            raise RuntimeError(self.error_code)
        if self.worker or self.closed:
            return
        version = self.version
        try:
            await self.start_source(version)
        except Exception:
            if not await self.restore_source(version):
                raise RuntimeError(self.error_code) from None
        self.worker = asyncio.create_task(self.run(version))

    async def run(self, version: int) -> None:
        while not self.closed and version == self.version:
            try:
                await self.run_source(version)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if not await self.restore_source(version):
                    return

    async def run_source(self, version: int) -> None:
        needs_still = True
        last_sent = time.monotonic()
        last_probe = last_sent
        last_encoded = ""
        capture_failures = 0
        invalid_frames = 0
        while not self.closed and version == self.version:
            now = time.monotonic()
            quiet_interval = 0.3 if now < self.active_until else 0.5
            # Probe even a completely silent source. A compositor stalled after
            # the initial paint must not yield heartbeats indefinitely.
            if (needs_still and now - last_sent >= quiet_interval) or (
                now - last_probe >= SURFACE_PROBE_INTERVAL
            ):
                try:
                    self.publish(await self.capture_current(version))
                    capture_failures = 0
                    needs_still = False
                except Exception:
                    capture_failures += 1
                    needs_still = True
                    if capture_failures >= MAX_CAPTURE_FAILURES:
                        raise
                last_probe = last_sent = time.monotonic()
            try:
                event = await asyncio.wait_for(self.events.get(), 0.3)
            except TimeoutError:
                continue
            delay = 1 / 15 - (time.monotonic() - last_sent)
            if delay > 0:
                await asyncio.sleep(delay)
            # ACK even rejected frames; otherwise Chromium's flow control can
            # stop the source before it has a chance to paint the current size.
            for ack in list(self.acks):
                self.acks.discard(ack)
                await asyncio.wait_for(
                    self.cdp.send("Page.screencastFrameAck", {"sessionId": ack}), 2
                )
            if event.get("still_capture"):
                continue
            encoded = event.get("data", "")
            size = self.page.viewport_size
            try:
                if len(encoded) > MAX_FRAME_BYTES * 4 // 3 + 4:
                    raise ValueError("browser_frame_too_large")
                data = base64.b64decode(encoded)
                if not matches_viewport(
                    data, "image/jpeg", size["width"], size["height"], self.dpr
                ):
                    raise ValueError("browser_frame_geometry_changed")
            except (ValueError, TypeError):
                invalid_frames += 1
                needs_still = True
                if invalid_frames >= MAX_CAPTURE_FAILURES:
                    raise
                continue
            invalid_frames = 0
            if encoded == last_encoded:
                # Do not overwrite a sharp still with identical lossy pixels.
                continue
            last_encoded = encoded
            needs_still = True
            if time.monotonic() >= self.active_until:
                continue
            self.publish(
                BrowserFrame(
                    data,
                    size["width"],
                    size["height"],
                    version,
                    self.dpr,
                    "image/jpeg",
                )
            )
            last_sent = time.monotonic()

    async def stop_source(self) -> None:
        running = self.handler is not None or self.worker is not None
        self.source_generation += 1
        if self.handler:
            self.cdp.remove_listener("Page.screencastFrame", self.handler)
            self.handler = None
        if running:
            try:
                await asyncio.wait_for(self.cdp.send("Page.stopScreencast"), 2)
            except Exception:
                pass  # Context/browser close remains the final resource boundary.
        while not self.events.empty():
            self.events.get_nowait()
        self.acks.clear()

    async def stop(self) -> None:
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        await self.stop_source()

    async def resize(self, width: int, height: int, dpr: float) -> None:
        async with self.lock:
            # Cap physical pixels as well as CSS dimensions, including forged clients.
            dpr = max(1.0, min(2.0, dpr, (4_000_000 / (width * height)) ** 0.5))
            dpr = math.floor(dpr * 100) / 100
            if (
                self.page.viewport_size == {"width": width, "height": height}
                and self.dpr == dpr
                and self.error_code is None
            ):
                return
            await self.stop()
            self.version += 1
            self.restarts = 0
            self.error_code = None
            self.latest = None
            for queue in self.subscribers.values():
                while not queue.empty():
                    queue.get_nowait()
            try:
                await asyncio.wait_for(
                    self.page.set_viewport_size({"width": width, "height": height}), 2
                )
                await asyncio.wait_for(
                    self.cdp.send(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": width,
                            "height": height,
                            "deviceScaleFactor": dpr,
                            "mobile": False,
                        },
                    ),
                    2,
                )
                self.dpr = dpr
            except Exception:
                # A failed resize has already stopped the old worker. Wake its
                # viewers explicitly instead of leaving only socket heartbeats.
                await self.fail_source()
                raise RuntimeError(self.error_code) from None
            if self.subscribers:
                await self.start()

    async def subscribe(self, token: str) -> None:
        async with self.lock:
            if self.closed:
                raise RuntimeError("browser_page_closed")
            if self.error_code:
                raise RuntimeError(self.error_code)
            self.subscribers[token] = asyncio.Queue(maxsize=1)
            if self.latest and self.latest.version == self.version:
                self.subscribers[token].put_nowait(self.latest)
            try:
                await self.start()
            except BaseException:
                self.subscribers.pop(token, None)
                await self.stop()
                raise

    async def unsubscribe(self, token: str) -> None:
        async with self.lock:
            self.subscribers.pop(token, None)
            if not self.subscribers:
                await self.stop()
                self.latest = None

    async def close(self) -> None:
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            await self.stop()
            self.latest = None
            for queue in self.subscribers.values():
                self.replace(queue, None)
            try:
                await asyncio.wait_for(self.cdp.detach(), 2)
            except Exception:
                pass  # A closing page/context also detaches the internal CDP session.

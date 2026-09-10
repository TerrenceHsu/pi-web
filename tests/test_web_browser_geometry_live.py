"""Opt-in pixel geometry regression; synthetic page only, no external websites."""

from __future__ import annotations

import asyncio
import io
import os
import time
from pathlib import Path
from typing import Any

import pytest

from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.frame_geometry import image_size
from pi_agent_core_py.web.browser.frames import PageFrames
from pi_agent_core_py.web.browser.runtime import LocalBrowserRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PI_RUN_LOCAL_BROWSER_TESTS") != "1",
        reason="Requires explicit isolated Chromium opt-in",
    ),
]

HTML = """<!doctype html><style>
body{margin:0;background:white;height:2000px}
#marker{position:fixed;left:100px;top:100px;width:80px;height:60px;background:#f00}
@keyframes blink{to{opacity:.1}}
#motion{position:fixed;right:10px;top:10px;width:10px;height:10px;
background:black;animation:blink .2s infinite alternate}
</style><div id="marker"></div><div id="motion"></div>"""


async def test_motion_and_still_keep_css_geometry(tmp_path: Path) -> None:
    Image = pytest.importorskip("PIL.Image")
    ImageChops = pytest.importorskip("PIL.ImageChops")
    runtime = LocalBrowserRuntime()
    try:
        first = await runtime.command("geometry", None, {"action": "create"})
        pid = first["id"]

        async def install() -> None:
            assert runtime.engine
            session = runtime.engine.sessions["geometry"]
            page = session.pages[pid]
            await page.set_content(HTML)

            async def fixture(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=HTML)

            await session.context.route("https://*.example.test/**", fixture)

        await runtime._submit(install())
        await runtime.command(
            "geometry",
            pid,
            BrowserAction(action="resize", width=760, height=454, dpr=2).model_dump(),
        )

        token = await runtime.subscribe("geometry", pid)
        failures = []
        for phase, (width, height, dpr) in enumerate(
            [
                (760, 454, 2),
                (760, 454, 1),
                (760, 454, 2),
                (320, 712, 2),
                (760, 454, 2),
            ]
        ):
            info = await runtime.command(
                "geometry",
                pid,
                BrowserAction(action="resize", width=width, height=height, dpr=dpr).model_dump(),
            )
            if phase in (2, 4):
                await runtime.command(
                    "geometry",
                    pid,
                    BrowserAction(
                        action="navigate", url=f"https://geometry-{phase}.example.test/"
                    ).model_dump(),
                )
            await runtime.command(
                "geometry", pid, BrowserAction(action="wheel", dy=100).model_dump()
            )
            seen: set[str] = set()
            started = time.monotonic()
            settled = False
            async with asyncio.timeout(10):
                while not settled:
                    frame = await runtime.next_frame(token)
                    if not frame or frame.version != info["view_version"]:
                        continue
                    seen.add(frame.mime)
                    settled = (
                        len(seen) == 2
                        and frame.mime == "image/png"
                        and time.monotonic() - started > 1
                    )
                    image = Image.open(io.BytesIO(frame.data)).convert("RGB")
                    red, green, blue = image.split()
                    red_mask = ImageChops.subtract(red, ImageChops.lighter(green, blue))
                    box = red_mask.point(lambda channel: 255 if channel > 150 else 0).getbbox()
                    assert box
                    actual = tuple(
                        round(
                            value
                            * (frame.width if index % 2 == 0 else frame.height)
                            / (image.width if index % 2 == 0 else image.height),
                            1,
                        )
                        for index, value in enumerate(box)
                    )
                    if actual != pytest.approx((100, 100, 180, 160), abs=2):
                        failures.append((width, height, dpr, frame.mime, image.size, actual))
                    if settled:
                        print(f"Stable CSS geometry at {width}x{height}, DPR {dpr}: {actual}")
                        (tmp_path / f"geometry-{width}x{height}-{dpr}.png").write_bytes(frame.data)
        assert failures == []
        await runtime.unsubscribe(token)
    finally:
        await runtime.close()


async def test_popup_resize_keeps_publishing_current_hd_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active popup stream must survive resize even while still capture is pending."""
    runtime = LocalBrowserRuntime()
    captures: list[dict[str, Any]] = []
    original_still = PageFrames.still

    async def traced_still(view: PageFrames) -> bytes:
        record: dict[str, Any] = {
            "version": view.version,
            "viewport": view.page.viewport_size,
            "dpr": view.dpr,
        }
        captures.append(record)
        try:
            data = await original_still(view)
            record["actual"] = image_size(data, "image/png")
            return data
        except BaseException as error:
            record["error"] = type(error).__name__
            raise

    monkeypatch.setattr(PageFrames, "still", traced_still)
    try:
        parent = await runtime.command("popup-resize", None, {"action": "create"})

        async def install_and_open_popup() -> str:
            assert runtime.engine
            session = runtime.engine.sessions["popup-resize"]

            async def fixture(route: Any) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<!doctype html><title>Resize fixture</title>"
                    "<style>body{margin:40px;font:24px sans-serif;background:#f4f8ff}</style>"
                    "<h1>Workspace browser demo</h1><p>Isolated popup</p><input>",
                )

            await session.context.route("https://browser-fixture.example.test/**", fixture)
            page = session.pages[parent["id"]]
            await page.goto("https://browser-fixture.example.test/login")
            async with session.context.expect_page() as opened:
                await page.evaluate("window.open('https://browser-fixture.example.test/popup')")
            popup = await opened.value
            await popup.wait_for_load_state("domcontentloaded")
            return next(pid for pid, candidate in session.pages.items() if candidate is popup)

        pid = await runtime._submit(install_and_open_popup())
        await runtime.command(
            "popup-resize",
            pid,
            BrowserAction(action="resize", width=320, height=712, dpr=2).model_dump(),
        )
        token = await runtime.subscribe("popup-resize", pid)
        for phase in range(16):
            width, height = (380, 726) if phase % 2 == 0 else (320, 712)
            await asyncio.sleep([0.1, 0.3, 0.5, 0.6][phase % 4])
            info = await runtime.command(
                "popup-resize",
                pid,
                BrowserAction(action="resize", width=width, height=height, dpr=2).model_dump(),
            )
            try:
                async with asyncio.timeout(5):
                    while True:
                        frame = await runtime.next_frame(token)
                        if (
                            frame
                            and frame.version == info["view_version"]
                            and frame.mime == "image/png"
                        ):
                            assert image_size(frame.data, frame.mime) == (width * 2, height * 2)
                            break
            except TimeoutError:

                async def inspect() -> dict[str, Any]:
                    assert runtime.engine
                    view = runtime.engine.sessions["popup-resize"].views[pid]
                    return {
                        "worker_done": view.worker.done() if view.worker else None,
                        "latest_version": view.latest.version if view.latest else None,
                        "version": view.version,
                        "events": view.events.qsize(),
                        "acks": list(view.acks),
                        "capturing": view.capturing,
                        "metrics": await view.page.evaluate(
                            "({width: innerWidth, height: innerHeight, dpr: devicePixelRatio, "
                            "visibility: document.visibilityState})"
                        ),
                    }

                state = await runtime._submit(inspect())
                pytest.fail(f"Popup resize stopped publishing: {state}; captures={captures[-20:]}")
        print(f"Popup HD resize transitions: 16; captures: {len(captures)}")
        await runtime.unsubscribe(token)
    finally:
        await runtime.close()


@pytest.mark.parametrize("fault", ["stale_surface", "capture_failure"])
async def test_resize_repairs_injected_cdp_surface_fault(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Exercise recovery against real CDP pixels, independent of a timing race."""
    runtime = LocalBrowserRuntime()
    original_still = PageFrames.still
    injected = False

    async def faulty_still(view: PageFrames) -> bytes:
        nonlocal injected
        if view.version == 1 and not injected:
            injected = True
            if fault == "capture_failure":
                raise OSError("synthetic private capture failure")
            # Reproduce a compositor/metrics mismatch after resize has returned
            # its requested CSS size. Recovery must restore the real CDP metrics.
            await view.cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": 320, "height": 712, "deviceScaleFactor": 1, "mobile": False},
            )
            data = await original_still(view)
            assert image_size(data, "image/png") != (760, 1452)
            return data
        return await original_still(view)

    monkeypatch.setattr(PageFrames, "still", faulty_still)
    try:
        created = await runtime.command("repair-geometry", None, {"action": "create"})
        pid = created["id"]

        async def install() -> None:
            assert runtime.engine
            await runtime.engine.sessions["repair-geometry"].pages[pid].set_content(HTML)

        await runtime._submit(install())
        token = await runtime.subscribe("repair-geometry", pid)
        assert await runtime.next_frame(token)
        info = await runtime.command(
            "repair-geometry",
            pid,
            BrowserAction(action="resize", width=380, height=726, dpr=2).model_dump(),
        )
        async with asyncio.timeout(5):
            while True:
                frame = await runtime.next_frame(token)
                if frame and frame.version == info["view_version"] and frame.mime == "image/png":
                    assert image_size(frame.data, frame.mime) == (760, 1452)
                    break

        async def inspect() -> dict[str, Any]:
            assert runtime.engine
            view = runtime.engine.sessions["repair-geometry"].views[pid]
            return {
                "restarts": view.restarts,
                "error": view.error_code,
                "metrics": await view.page.evaluate(
                    "({width: innerWidth, height: innerHeight, dpr: devicePixelRatio})"
                ),
            }

        assert injected
        assert await runtime._submit(inspect()) == {
            "restarts": 1,
            "error": None,
            "metrics": {"width": 380, "height": 726, "dpr": 2},
        }
        print(f"Recovered injected {fault}: 380x726 CSS, DPR 2, current 760x1452 pixels")
        await runtime.unsubscribe(token)
    finally:
        await runtime.close()

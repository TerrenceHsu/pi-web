"""Explicit real Chromium checks, with synthetic credentials and isolated contexts only."""

from __future__ import annotations

import asyncio
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.runtime import BrowserError, LocalBrowserRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PI_RUN_LOCAL_BROWSER_TESTS") != "1",
        reason="Requires explicit isolated Chromium opt-in",
    ),
]

LOGIN_HTML = """<!doctype html><title>Browser login fixture</title>
<style>body{font:24px sans-serif}
input,button{font:24px sans-serif;display:block;margin:12px}</style>
<label>Username<input id="username"></label>
<label>Password<input id="password" type="password"></label>
<button id="login" onclick="document.cookie='fixture_login=yes; Secure; SameSite=Lax';
document.title='Signed in';document.querySelector('#result').textContent=
'Signed in as '+document.querySelector('#username').value">Sign in</button>
<p id="result">Not signed in</p>
<a id="popup" href="https://browser-fixture.example.test/second" target="_blank">
Open second page</a>
"""


async def test_real_browser_login_click_keyboard_popup_isolation_cleanup(tmp_path: Path) -> None:
    runtime = LocalBrowserRuntime()
    try:
        first = await runtime.command("one", None, {"action": "create"})
        pid = first["id"]

        async def install_fixture() -> None:
            assert runtime.engine
            context = runtime.engine.sessions["one"].context

            async def fixture(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=LOGIN_HTML)

            await context.route("https://browser-fixture.example.test/**", fixture)

        await runtime._submit(install_fixture())

        async def action(**data: Any) -> Any:
            return await runtime.command(
                "one", pid, BrowserAction.model_validate(data).model_dump()
            )

        await action(action="navigate", url="https://browser-fixture.example.test/login")

        # Coordinates are measured by test-only code; production exposes no locator/evaluate API.
        async def position(selector: str) -> tuple[float, float]:
            assert runtime.engine
            box = await runtime.engine.sessions["one"].pages[pid].locator(selector).bounding_box()
            return float(box["x"] + 10), float(box["y"] + 10)

        x, y = await runtime._submit(position("#username"))
        await action(action="click", x=x, y=y)
        await action(action="text", text="测试用户")
        await action(action="key", key="Tab")
        await action(action="text", text="fixture-not-a-real-password")
        x, y = await runtime._submit(position("#login"))
        result = await action(action="click", x=x, y=y)
        assert result["title"] == "Signed in"
        frame = await runtime.command("one", pid, {"action": "frame"})
        assert frame[:8] == b"\x89PNG\r\n\x1a\n" and len(frame) > 2000
        (tmp_path / "browser-login.png").write_bytes(frame)
        x, y = await runtime._submit(position("#popup"))

        async def click_and_wait_for_popup() -> None:
            assert runtime.engine
            async with runtime.engine.sessions["one"].context.expect_page() as opened:
                await runtime.engine.command(
                    "one",
                    pid,
                    BrowserAction(
                        action="click",
                        x=x,
                        y=y,
                    ).model_dump(),
                )
            await opened.value

        await runtime._submit(click_and_wait_for_popup())

        async def verify_cookies_and_popup() -> None:
            assert runtime.engine
            session = runtime.engine.sessions["one"]
            assert len(session.pages) == 2
            cookies = await session.context.cookies()
            assert any(cookie["name"] == "fixture_login" for cookie in cookies)

        await runtime._submit(verify_cookies_and_popup())
        second = await runtime.command("two", None, {"action": "create"})

        async def verify_isolation() -> None:
            assert runtime.engine
            assert await runtime.engine.sessions["two"].context.cookies() == []

        await runtime._submit(verify_isolation())
        with pytest.raises(BrowserError, match="not_found"):
            await runtime.command("two", pid, {"action": "info"})
        await runtime.delete_session("one")
        with pytest.raises(BrowserError, match="deleted"):
            await runtime.command("one", None, {"action": "create"})
        assert (await runtime.command("two", second["id"], {"action": "info"}))["id"] == second[
            "id"
        ]
        await runtime.command("two", second["id"], {"action": "close"})

        async def verify_released() -> None:
            assert runtime.engine
            assert not runtime.engine.sessions
            assert runtime.engine.browser is None
            assert runtime.engine.proxy.server is None

        await runtime._submit(verify_released())
    finally:
        await runtime.close()
    assert runtime.thread and not runtime.thread.is_alive()


@pytest.mark.skipif(
    os.environ.get("PI_BROWSER_PUBLIC_SMOKE") != "1", reason="Public network opt-in"
)
async def test_real_public_https_without_changing_tls_or_daily_browser() -> None:
    runtime = LocalBrowserRuntime()
    try:
        page = await runtime.command("public", None, {"action": "create"})
        result = await runtime.command(
            "public",
            page["id"],
            BrowserAction(
                action="navigate",
                url="https://example.com",
            ).model_dump(),
        )
        # Navigation now returns at response commit; title/load can arrive later.
        async with asyncio.timeout(10):
            while result["title"] != "Example Domain":
                await asyncio.sleep(0.05)
                result = await runtime.command("public", page["id"], {"action": "info"})
        assert result["url"].startswith("https://example.com")
    finally:
        await runtime.close()


async def test_real_hd_stream_css_coordinates_motion_and_disconnect(tmp_path: Path) -> None:
    runtime = LocalBrowserRuntime()
    try:
        first = await runtime.command("hd", None, {"action": "create"})
        pid = first["id"]

        async def fixture() -> None:
            assert runtime.engine
            page = runtime.engine.sessions["hd"].pages[pid]
            await page.set_content(LOGIN_HTML)

        await runtime._submit(fixture())
        info = await runtime.command(
            "hd",
            pid,
            BrowserAction(
                action="resize",
                width=640,
                height=480,
                dpr=2,
            ).model_dump(),
        )
        token = await runtime.subscribe("hd", pid)
        async with asyncio.timeout(12):
            while True:
                frame = await runtime.next_frame(token)
                if frame and frame.mime == "image/png":
                    break
        assert struct.unpack("!II", frame.data[16:24]) == (1280, 960)
        assert (frame.width, frame.height, frame.dpr) == (640, 480, 2)
        (tmp_path / "hd-1280x960.png").write_bytes(frame.data)

        async def position() -> tuple[float, float]:
            assert runtime.engine
            box = await runtime.engine.sessions["hd"].pages[pid].locator("#username").bounding_box()
            return float(box["x"] + 10), float(box["y"] + 10)

        x, y = await runtime._submit(position())
        await runtime.command(
            "hd",
            pid,
            BrowserAction(
                action="click",
                width=640,
                height=480,
                x=x,
                y=y,
                view_version=info["view_version"],
            ).model_dump(),
        )
        await runtime.command("hd", pid, BrowserAction(action="text", text="高清输入").model_dump())

        async def animate() -> None:
            assert runtime.engine
            page = runtime.engine.sessions["hd"].pages[pid]
            assert await page.locator("#username").input_value() == "高清输入"
            await page.add_style_tag(
                content=(
                    "@keyframes move{to{transform:translateX(100px)}} "
                    "#login{animation:move .5s infinite alternate}"
                )
            )

        await runtime._submit(animate())

        async def interact() -> None:
            for _ in range(10):
                await runtime.command(
                    "hd", pid, BrowserAction(action="wheel", dx=0, dy=0).model_dump()
                )
                await asyncio.sleep(0.25)

        interaction = asyncio.create_task(interact())
        started = time.monotonic()
        count = 0
        try:
            async with asyncio.timeout(8):
                while time.monotonic() - started < 2:
                    if await runtime.next_frame(token):
                        count += 1
        finally:
            interaction.cancel()
            await asyncio.gather(interaction, return_exceptions=True)
        rate = count / (time.monotonic() - started)
        print(f"HD stream observed motion: {rate:.1f} fps ({count} frames)")
        assert count >= 8  # Conservative CI floor, not an advertised FPS guarantee.
        await asyncio.sleep(1.2)
        async with asyncio.timeout(5):
            while True:
                quiet = await runtime.next_frame(token)
                if quiet and quiet.mime == "image/png":
                    break
        assert struct.unpack("!II", quiet.data[16:24]) == (1280, 960)
        await runtime.unsubscribe(token)

        async def stopped() -> None:
            assert runtime.engine
            view = runtime.engine.sessions["hd"].views[pid]
            assert not view.subscribers and view.worker is None
            assert not runtime.engine.streams

        await runtime._submit(stopped())
    finally:
        await runtime.close()


@pytest.mark.skipif(os.environ.get("PI_BROWSER_GOOGLE_SMOKE") != "1", reason="Google opt-in")
async def test_google_hd_in_fresh_context_without_login(tmp_path: Path) -> None:
    runtime = LocalBrowserRuntime()
    try:
        page = await runtime.command("google", None, {"action": "create"})
        pid = page["id"]
        await runtime.command(
            "google",
            pid,
            BrowserAction(
                action="resize",
                width=800,
                height=600,
                dpr=2,
            ).model_dump(),
        )
        started = time.monotonic()
        info = await runtime.command(
            "google",
            pid,
            BrowserAction(
                action="navigate",
                url="https://www.google.com/",
            ).model_dump(),
        )
        async with asyncio.timeout(10):
            while "Google" not in info["title"]:
                await asyncio.sleep(0.1)
                info = await runtime.command("google", pid, {"action": "info"})
        pixels = await runtime.command("google", pid, {"action": "frame"})
        assert struct.unpack("!II", pixels[16:24]) == (1600, 1200)
        print(
            f"Google fresh navigation commit: {info['navigation_ms']} ms; "
            f"title+HD: {(time.monotonic() - started) * 1000:.0f} ms"
        )
        (tmp_path / "google-hd-no-login.png").write_bytes(pixels)
    finally:
        await runtime.close()

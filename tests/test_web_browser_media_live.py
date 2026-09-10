"""Explicit real-browser media regression using synthetic video and a tone only."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest

from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.media import MediaSubscription
from pi_agent_core_py.web.browser.runtime import BrowserError, LocalBrowserRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PI_RUN_LOCAL_BROWSER_TESTS") != "1",
        reason="Requires explicit isolated Chromium opt-in",
    ),
]

HTML = """<!doctype html><title>Synthetic media</title><style>body{margin:0}</style>
<canvas width=1920 height=1080></canvas><script>
const canvas=document.querySelector('canvas'), ctx=canvas.getContext('2d'); let n=0;
function draw(){ctx.fillStyle='white';ctx.fillRect(0,0,1920,1080);
ctx.fillStyle='red';ctx.fillRect((n++*7)%1800,180,120,120);requestAnimationFrame(draw)}draw();
canvas.onclick=async()=>{if(window.ac)return;window.ac=new AudioContext();
const o=ac.createOscillator(),g=ac.createGain();g.gain.value=.08;
o.connect(g);g.connect(ac.destination);o.start();await ac.resume()};
</script>"""


async def test_real_media_generation_is_bounded_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LocalBrowserRuntime()
    original_push = MediaSubscription.push

    async def traced_push(subscription: MediaSubscription, sequence: int, data: bytes) -> None:
        if sequence <= 2:
            print("Media payload", sequence, len(data), data[:4].hex())
        await original_push(subscription, sequence, data)

    monkeypatch.setattr(MediaSubscription, "push", traced_push)
    try:
        pages = [await runtime.command(sid, None, {"action": "create"}) for sid in ("a", "b")]

        async def install() -> None:
            assert runtime.engine
            for sid, info in zip(("a", "b"), pages, strict=True):
                session = runtime.engine.sessions[sid]

                async def fixture(route: Any) -> None:
                    await route.fulfill(status=200, content_type="text/html", body=HTML)

                await session.context.route("https://media.example.test/**", fixture)
                await session.pages[info["id"]].goto("https://media.example.test/same")

        await runtime._submit(install())
        pid = pages[0]["id"]
        token = await runtime.subscribe_media("a", pid)
        with pytest.raises(BrowserError, match="media_limit"):
            await runtime.subscribe_media("b", pages[1]["id"])
        await runtime.command(
            "a",
            pid,
            BrowserAction(action="click", width=1920, height=1080, x=200, y=100).model_dump(),
        )
        started = time.monotonic()
        first_ms = 0.0
        sequence = 0
        total_bytes = 0
        async with asyncio.timeout(12):
            while sequence < 30:
                chunk = await runtime.next_media(token)
                if chunk:
                    sequence += 1
                    assert chunk["seq"] == sequence
                    assert chunk["generation"] == token
                    assert chunk["page"]["id"] == pid
                    assert (chunk["page"]["width"], chunk["page"]["height"]) == (1920, 1080)
                    total_bytes += len(chunk["data"])
                    if sequence == 1:
                        first_ms = (time.monotonic() - started) * 1000
                        assert chunk["data"].startswith(b"\x1a\x45\xdf\xa3")
                await runtime.renew_media(token)
        print(f"Media: first chunk {first_ms:.0f} ms, {sequence} chunks, {total_bytes} bytes")
        await runtime.command("a", pid, {"action": "media_stop"})
        with pytest.raises(BrowserError):
            await runtime.next_media(token)
        # The same URL in another isolated Session gets its own fresh generation.
        second = await runtime.subscribe_media("b", pages[1]["id"])
        assert second != token
        async with asyncio.timeout(8):
            while True:
                chunk = await runtime.next_media(second)
                if chunk:
                    assert chunk["seq"] == 1
                    assert chunk["page"]["id"] == pages[1]["id"]
                    break
        await runtime.delete_session("b")
        with pytest.raises(BrowserError):
            await runtime.next_media(second)
        await runtime.delete_session("a")
        assert runtime.engine and not runtime.engine.media and runtime.engine.browser is None
    finally:
        await runtime.close()

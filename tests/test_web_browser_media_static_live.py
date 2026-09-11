"""Opt-in production tab-capture continuity on a silent, genuinely static page."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest

from pi_agent_core_py.web.browser.media import SOURCE_SECONDS
from pi_agent_core_py.web.browser.runtime import BrowserError, LocalBrowserRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PI_RUN_LOCAL_BROWSER_TESTS") != "1",
        reason="Requires explicit isolated Chromium opt-in",
    ),
]

# No animation, timer, sound, network, or changing document exists until the
# test explicitly calls startMotion after more than one source-watchdog period.
HTML = """<!doctype html><title>Silent static media</title>
<style>body{margin:0;background:white}canvas{display:block}</style>
<canvas width=1920 height=1080></canvas><script>
const canvas=document.querySelector('canvas'), ctx=canvas.getContext('2d');
ctx.fillStyle='white';ctx.fillRect(0,0,1920,1080);
ctx.fillStyle='navy';ctx.font='48px sans-serif';
ctx.fillText('Static 1080p page — no audio or animation',100,200);
window.motionFrames=0;
window.startMotion=()=>{
  if(window.motionStarted)return;
  window.motionStarted=true;
  function draw(){
    ctx.fillStyle='white';ctx.fillRect(0,300,1920,200);
    ctx.fillStyle='red';ctx.fillRect((window.motionFrames++*7)%1800,320,120,120);
    requestAnimationFrame(draw);
  }
  draw();
};
</script>"""


async def test_static_source_survives_then_resumes_motion_without_new_generation() -> None:
    runtime = LocalBrowserRuntime()
    sid = "static-media-fixture"
    static_seconds = 14.0
    motion_seconds = 10.0
    assert static_seconds > SOURCE_SECONDS
    try:
        page_info = await runtime.command(sid, None, {"action": "create"})
        pid = page_info["id"]

        async def install() -> None:
            assert runtime.engine
            session = runtime.engine.sessions[sid]

            async def fixture(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=HTML)

            await session.context.route("https://static-media.example.test/**", fixture)
            await session.pages[pid].goto("https://static-media.example.test/fixture")

        async def fixture_state(*, start_motion: bool = False) -> dict[str, Any]:
            assert runtime.engine
            page = runtime.engine.sessions[sid].pages[pid]
            return await page.evaluate(
                """start=>{
                  if(start)window.startMotion();
                  return {motionStarted:!!window.motionStarted,
                    motionFrames:window.motionFrames,
                    width:innerWidth,height:innerHeight,dpr:devicePixelRatio};
                }""",
                start_motion,
            )

        await runtime._submit(install())
        token = await runtime.subscribe_media(sid, pid)
        started = time.monotonic()
        last_chunk_at = started
        largest_gap = 0.0
        first_chunk_ms = 0.0
        sequence = 0
        total_bytes = 0
        static_chunks = 0
        motion_started_at: float | None = None
        async with asyncio.timeout(40):
            while True:
                now = time.monotonic()
                if motion_started_at is None and now - started >= static_seconds:
                    before = await runtime._submit(fixture_state())
                    assert before == {
                        "motionStarted": False,
                        "motionFrames": 0,
                        "width": 1920,
                        "height": 1080,
                        "dpr": 1,
                    }
                    await runtime._submit(fixture_state(start_motion=True))
                    motion_started_at = time.monotonic()
                if motion_started_at is not None and now - motion_started_at >= motion_seconds:
                    break
                chunk = await runtime.next_media(token)
                if chunk:
                    now = time.monotonic()
                    sequence += 1
                    assert chunk["seq"] == sequence
                    assert chunk["generation"] == token
                    assert chunk["page"]["id"] == pid
                    assert (chunk["page"]["width"], chunk["page"]["height"]) == (1920, 1080)
                    largest_gap = max(largest_gap, now - last_chunk_at)
                    last_chunk_at = now
                    total_bytes += len(chunk["data"])
                    if sequence == 1:
                        first_chunk_ms = (now - started) * 1000
                        assert chunk["data"].startswith(b"\x1a\x45\xdf\xa3")
                    if motion_started_at is None:
                        static_chunks += 1
                # Renew the viewer lease independently of whether this read
                # returns a chunk; a silent source must not be mistaken for a
                # disconnected viewer.
                await runtime.renew_media(token)
        after = await runtime._submit(fixture_state())
        assert after["motionStarted"] and after["motionFrames"] > 100
        assert static_chunks >= 2
        assert sequence - static_chunks >= 3
        assert largest_gap < SOURCE_SECONDS
        print(
            "STATIC_MEDIA_CONTINUITY",
            json.dumps(
                {
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "static_seconds": static_seconds,
                    "motion_seconds": motion_seconds,
                    "first_chunk_ms": round(first_chunk_ms, 1),
                    "largest_chunk_gap_seconds": round(largest_gap, 3),
                    "static_chunks": static_chunks,
                    "motion_chunks": sequence - static_chunks,
                    "encoded_bytes": total_bytes,
                    "fixture_motion_frames": after["motionFrames"],
                    "same_generation": True,
                },
                sort_keys=True,
            ),
        )
        await runtime.command(sid, pid, {"action": "media_stop"})
        with pytest.raises(BrowserError):
            await runtime.next_media(token)
        await runtime.delete_session(sid)
        assert runtime.engine and not runtime.engine.media and runtime.engine.browser is None
    finally:
        await runtime.close()

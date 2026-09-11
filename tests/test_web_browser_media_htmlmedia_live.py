"""Opt-in HTMLMediaElement WebM playback through the production tab audio source."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest

from pi_agent_core_py.web.browser.api import BrowserAction
from pi_agent_core_py.web.browser.runtime import LocalBrowserRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PI_RUN_LOCAL_BROWSER_TESTS") != "1",
        reason="Requires explicit isolated Chromium opt-in",
    ),
]

# Build a short synthetic WebM entirely in memory. The oscillator feeds a
# MediaStreamDestination, never speakers. After preparation it is stopped;
# only the actual HTML video element produces sound during the capture test.
HTML = """<!doctype html><title>Synthetic HTML media</title>
<style>body{margin:0}button{width:240px;height:80px}video{display:block}</style>
<button id=prepare>Prepare synthetic WebM</button><button id=play>Play WebM</button>
<video width=1280 height=720 loop playsinline controls></video><script>
window.fixtureReady=false;window.fixturePlaying=false;
document.querySelector('#prepare').onclick=async()=>{
  if(window.preparing)return;window.preparing=true;
  const canvas=document.createElement('canvas');canvas.width=960;canvas.height=540;
  const ctx=canvas.getContext('2d');let frames=0,done=false;
  function draw(){if(done)return;ctx.fillStyle='white';ctx.fillRect(0,0,960,540);
    ctx.fillStyle='red';ctx.fillRect((frames++*7)%850,100,100,100);
    requestAnimationFrame(draw);}
  draw();
  const ac=new AudioContext(),dest=ac.createMediaStreamDestination();
  const oscillator=ac.createOscillator(),gain=ac.createGain();gain.gain.value=.08;
  oscillator.frequency.value=440;oscillator.connect(gain);gain.connect(dest);
  const stream=new MediaStream([...canvas.captureStream(30).getVideoTracks(),
    ...dest.stream.getAudioTracks()]);
  const chunks=[], recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8,opus'});
  recorder.ondataavailable=event=>{if(event.data.size)chunks.push(event.data);};
  recorder.onstop=async()=>{
    done=true;oscillator.stop();stream.getTracks().forEach(track=>track.stop());
    await ac.close();
    window.fixtureBlob=URL.createObjectURL(new Blob(chunks,{type:recorder.mimeType}));
    document.querySelector('video').src=window.fixtureBlob;window.fixtureReady=true;
  };
  await ac.resume();oscillator.start();recorder.start();setTimeout(()=>recorder.stop(),3000);
};
document.querySelector('#play').onclick=async()=>{
  await document.querySelector('video').play();window.fixturePlaying=true;
};
</script>"""

INSTALL_AUDIO_METER = """()=>{
  const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  window.__piHtmlMeter={samples:0,maxRms:0,state:'waiting'};
  navigator.mediaDevices.getUserMedia=async options=>{
    const stream=await original(options),tracks=stream.getAudioTracks();
    if(tracks.length){
      const ac=new AudioContext(),source=ac.createMediaStreamSource(new MediaStream(tracks));
      const analyser=ac.createAnalyser(),zero=ac.createGain();zero.gain.value=0;
      source.connect(analyser);analyser.connect(zero);zero.connect(ac.destination);
      const values=new Float32Array(analyser.fftSize);
      const timer=setInterval(()=>{
        analyser.getFloatTimeDomainData(values);let sum=0;
        for(const value of values)sum+=value*value;
        const meter=window.__piHtmlMeter;
        meter.maxRms=Math.max(meter.maxRms,Math.sqrt(sum/values.length));
        meter.samples++;meter.state=ac.state;
      },100);
      window.__piStopHtmlMeter=async()=>{
        clearInterval(timer);source.disconnect();analyser.disconnect();zero.disconnect();
        if(ac.state!=='closed')await ac.close();
      };
      void ac.resume();
    }
    return stream;
  };
  return true;
}"""


async def test_html_video_element_audio_reaches_the_production_capture() -> None:
    runtime = LocalBrowserRuntime()
    sid = "html-media-fixture"
    try:
        info = await runtime.command(sid, None, {"action": "create"})
        pid = info["id"]

        async def prepare() -> None:
            assert runtime.engine and runtime.engine.capture_bridge
            session = runtime.engine.sessions[sid]
            page = session.pages[pid]

            async def fixture(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html", body=HTML)

            await session.context.route("https://html-media.example.test/**", fixture)
            await page.goto("https://html-media.example.test/fixture")
            await page.get_by_role("button", name="Prepare synthetic WebM").click()
            await page.wait_for_function("window.fixtureReady===true", timeout=8000)
            helper = runtime.engine.capture_bridge.contexts[session.context].helper
            await helper.evaluate(INSTALL_AUDIO_METER)

        async def measure() -> dict[str, Any]:
            assert runtime.engine and runtime.engine.capture_bridge
            session = runtime.engine.sessions[sid]
            page = session.pages[pid]
            helper = runtime.engine.capture_bridge.contexts[session.context].helper
            state = await page.evaluate(
                """()=>{const v=document.querySelector('video');return {
                  fixturePlaying:window.fixturePlaying,paused:v.paused,muted:v.muted,
                  volume:v.volume,readyState:v.readyState,currentTime:v.currentTime,
                  videoWidth:v.videoWidth,videoHeight:v.videoHeight,error:v.error?.code||null,
                  videoFrames:v.getVideoPlaybackQuality().totalVideoFrames};}"""
            )
            state["captured_audio"] = await helper.evaluate("()=>window.__piHtmlMeter")
            return state

        await runtime._submit(prepare())
        token = await runtime.subscribe_media(sid, pid)
        await runtime.command(
            sid,
            pid,
            BrowserAction(action="click", width=1920, height=1080, x=350, y=40).model_dump(),
        )
        sequence = 0
        total_bytes = 0
        started = time.monotonic()
        async with asyncio.timeout(14):
            while time.monotonic() - started < 8:
                chunk = await runtime.next_media(token)
                if chunk:
                    sequence += 1
                    assert chunk["seq"] == sequence and chunk["generation"] == token
                    assert (chunk["page"]["width"], chunk["page"]["height"]) == (1920, 1080)
                    total_bytes += len(chunk["data"])
                await runtime.renew_media(token)
        result = await runtime._submit(measure())
        result.update({"capture_chunks": sequence, "capture_bytes": total_bytes})
        print("HTML_MEDIA_AUDIO", json.dumps(result, sort_keys=True))
        assert result["fixturePlaying"] and not result["paused"]
        assert result["videoFrames"] >= 100
        assert result["videoWidth"] == 960 and result["videoHeight"] == 540
        assert result["error"] is None
        assert result["captured_audio"]["state"] == "running"
        assert result["captured_audio"]["samples"] >= 20
        assert result["captured_audio"]["maxRms"] > 0.005
        assert sequence >= 20
        await runtime.command(sid, pid, {"action": "media_stop"})
        await runtime.delete_session(sid)
        assert runtime.engine and not runtime.engine.media and runtime.engine.browser is None
    finally:
        await runtime.close()

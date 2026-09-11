"""Synthetic login website, installed only by the explicitly opted-in E2E launcher."""

from typing import Any

from pi_agent_core_py.web.browser.runtime import BrowserEngine, BrowserSession

HTML = """<!doctype html><meta charset="utf-8"><title>Local browser fixture</title>
<style>body{font:24px sans-serif;background:#f4f8ff;margin:40px}input,button{font:22px sans-serif;
position:absolute;left:50px;width:320px;height:40px;box-sizing:border-box}
#user{top:120px}#password{top:180px}#login{top:240px}#result{position:absolute;top:320px}
#popup{position:absolute;top:400px}</style>
<h1>Workspace browser demo</h1><p>Isolated, synthetic sign-in form.</p>
<input id="user" aria-label="Demo user">
<input id="password" type="password" aria-label="Demo password">
<button id="login" onclick="document.cookie='demo_login=yes; Secure; SameSite=Lax';
document.title='Signed in';document.querySelector('#result').textContent=
'Signed in: '+document.querySelector('#user').value">Sign in</button>
<p id="result">Not signed in</p><a id="popup" href="/popup" target="_blank">Open popup</a>
"""

GEOMETRY_HTML = """<!doctype html><title>Geometry fixture</title><style>
body{margin:0;background:white;height:2000px}
#marker{position:fixed;left:100px;top:100px;width:80px;height:60px;background:#f00}
@keyframes blink{to{opacity:.1}}
#motion{position:fixed;right:10px;top:10px;width:10px;height:10px;
background:black;animation:blink .2s infinite alternate}
</style><div id="marker"></div><div id="motion"></div>"""

MEDIA_HTML = """<!doctype html><meta charset="utf-8"><title>Media fixture ready</title>
<style>html,body{margin:0;overflow:hidden;background:#11243a;color:white;font:28px sans-serif}
canvas,video{position:absolute;inset:0;width:100vw;height:100vh}button{position:absolute;left:60px;
top:120px;width:320px;height:64px;font:24px sans-serif}
p{position:absolute;left:60px;top:24px}</style>
<canvas width="1920" height="1080"></canvas><video loop playsinline style="display:none"></video>
<p>Synthetic 1080p HTML video + 440 Hz tone, generated only in memory</p>
<button id="start">Start synthetic tone</button><script>
const canvas=document.querySelector('canvas'),ctx=canvas.getContext('2d');let frame=0;
function draw(){ctx.fillStyle=`hsl(${frame%360} 55% 25%)`;ctx.fillRect(0,0,1920,1080);
ctx.fillStyle='white';ctx.fillRect((frame*11)%1800,350,120,120);ctx.font='64px sans-serif';
ctx.fillText(`Frame ${frame++}`,60,600);
if(!window.fixturePlaying)requestAnimationFrame(draw)}draw();
document.querySelector('#start').onclick=async()=>{
 if(window.fixtureAudio)return;
 const audio=window.fixtureAudio=new AudioContext();const tone=audio.createOscillator();
 const gain=audio.createGain();gain.gain.value=.08;tone.frequency.value=440;
 const destination=audio.createMediaStreamDestination();tone.connect(gain);
 gain.connect(destination);
 tone.start();await audio.resume();
 const stream=new MediaStream([...canvas.captureStream(30).getVideoTracks(),
 ...destination.stream.getAudioTracks()]);const chunks=[];
 const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8,opus',
 videoBitsPerSecond:4000000,audioBitsPerSecond:96000});
 recorder.ondataavailable=event=>{if(event.data.size)chunks.push(event.data)};
 const finished=new Promise(resolve=>recorder.onstop=resolve);recorder.start(100);
 await new Promise(resolve=>setTimeout(resolve,2200));recorder.stop();await finished;
 for(const track of stream.getTracks())track.stop();tone.stop();await audio.close();
 const player=document.querySelector('video');
 player.src=URL.createObjectURL(new Blob(chunks,{type:recorder.mimeType}));
 player.style.display='block';canvas.style.display='none';window.fixturePlaying=true;
 await player.play();document.title='Media fixture playing';
 document.querySelector('#start').textContent='Synthetic video playing';
 let presented=0,lastPresented=performance.now();const started=performance.now();
 function displayed(now){presented++;lastPresented=now;player.requestVideoFrameCallback(displayed)}
 player.requestVideoFrameCallback(displayed);
 const diagnostic=setInterval(()=>{const quality=player.getVideoPlaybackQuality();
 console.debug('PI_E2E_MEDIA_SOURCE '+JSON.stringify({elapsedMs:performance.now()-started,
 currentTime:player.currentTime,duration:player.duration,readyState:player.readyState,
 paused:player.paused,seeking:player.seeking,presented,lastFrameStallMs:performance.now()-lastPresented,
 decoded:quality.totalVideoFrames,dropped:quality.droppedVideoFrames,error:player.error?.code??null}));
 if(performance.now()-started>60000)clearInterval(diagnostic)},1000);
};
</script>"""

PERSISTENT_HTML = """<!doctype html><meta charset="utf-8"><title>Persistent fixture static</title>
<style>html,body{margin:0;overflow:hidden;background:#16334f;color:white;font:28px sans-serif}
canvas{position:absolute;inset:0;width:100vw;height:100vh}button{position:absolute;left:60px;
top:120px;width:320px;height:64px;font:24px sans-serif}
p{position:absolute;left:60px;top:24px}</style>
<canvas width="1920" height="1080"></canvas>
<p>Synthetic static page; motion starts only on click</p>
<button id="start">Start synthetic motion</button><script>
const canvas=document.querySelector('canvas'),ctx=canvas.getContext('2d');let frame=0;
function draw(){ctx.fillStyle=`hsl(${frame%360} 55% 25%)`;ctx.fillRect(0,0,1920,1080);
ctx.fillStyle='white';ctx.fillRect((frame*11)%1800,350,120,120);ctx.font='64px sans-serif';
ctx.fillText(`Frame ${frame++}`,60,600)}draw();
document.querySelector('#start').onclick=()=>{
 if(window.fixtureMoving)return;window.fixtureMoving=true;
 document.title='Persistent fixture moving';
 document.querySelector('#start').textContent='Motion running';
 function animate(){draw();requestAnimationFrame(animate)}requestAnimationFrame(animate);
};
</script>"""


class FixtureBrowserEngine(BrowserEngine):
    async def _new_context(self, sid: str) -> BrowserSession:
        existing = sid in self.sessions
        session = await super()._new_context(sid)
        if not existing:
            def source_diagnostic(message: Any) -> None:
                if (
                    message.page is not None
                    and message.page.url == "https://browser-fixture.example.test/media"
                    and message.text.startswith("PI_E2E_MEDIA_SOURCE ")
                ):
                    print(message.text, flush=True)

            session.context.on("console", source_diagnostic)

            async def fixture(route: Any) -> None:
                if route.request.url.endswith("/persistent"):
                    body = PERSISTENT_HTML
                elif route.request.url.endswith("/media"):
                    body = MEDIA_HTML
                elif route.request.url.endswith("/geometry"):
                    body = GEOMETRY_HTML
                else:
                    body = HTML
                await route.fulfill(status=200, content_type="text/html", body=body)

            await session.context.route("https://browser-fixture.example.test/**", fixture)
        return session

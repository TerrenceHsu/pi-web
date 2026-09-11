from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from pi_agent_core_py.web.browser.errors import BrowserError
from pi_agent_core_py.web.browser.tab_capture import EXTENSION_PATH, MIME, TabCaptureBridge


class FakePage:
    def __init__(self, context: FakeContext, number: int) -> None:
        self.context = context
        self.number = number
        self.url = "about:blank"
        self.main_frame = object()
        self.events: dict[str, Any] = {}
        self.binding: Any = None
        self.closed = False
        self.commands: list[dict[str, Any]] = []
        self.armed: dict[str, Any] = {}
        self.clicked: int | None = None
        self.start_result: dict[str, Any] = {
            "mime": MIME,
            "width": 1920,
            "height": 1080,
            "frameRate": 30,
            "audioSafe": True,
        }

    def on(self, event: str, callback: Any) -> None:
        self.events[event] = callback

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url

    async def expose_binding(self, name: str, callback: Any) -> None:
        self.binding = callback

    async def bring_to_front(self) -> None:
        self.context.browser.root.focused = self

    async def close(self) -> None:
        self.closed = True
        if "close" in self.events:
            self.events["close"]()

    async def evaluate(self, expression: str, options: dict[str, Any]) -> Any:
        self.commands.append(options)
        match options["command"]:
            case "init":
                return {"supported": True, "incognito": self.context.incognito}
            case "arm":
                self.armed = options
                return {"ok": True}
            case "take":
                return {"token": self.armed["token"], "tabId": self.clicked}
            case "start":
                return self.start_result
            case _:
                return True


class FakeRoot:
    def __init__(self) -> None:
        self.targets: dict[str, FakePage] = {}
        self.sessions: dict[str, str] = {}
        self.events: dict[str, Any] = {}
        self.focused: FakePage | None = None
        self.commands: list[tuple[str, Any]] = []
        self.detach = AsyncMock()
        self.bad_click = False
        self.mapping_error = False

    def on(self, event: str, callback: Any) -> None:
        self.events[event] = callback

    def remove_listener(self, event: str, callback: Any) -> None:
        self.events.pop(event, None)

    async def send(self, method: str, params: Any = None) -> Any:
        self.commands.append((method, params))
        if method == "Extensions.loadUnpacked":
            return {"id": "a" * 32}
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {
                        "targetId": target,
                        "type": "tab",
                        "browserContextId": page.context.name,
                        "url": "about:blank",
                    }
                    for target, page in self.targets.items()
                ]
            }
        if method == "Target.attachToTarget":
            session = f"nested-{params['targetId']}"
            self.sessions[session] = params["targetId"]
            return {"sessionId": session}
        if method == "Target.sendMessageToTarget":
            outer = self.sessions[params["sessionId"]]
            page = self.targets[outer]
            receiver = self.events["Target.receivedMessageFromTarget"]
            receiver(
                {
                    "sessionId": params["sessionId"],
                    "message": json.dumps(
                        {
                            "method": "Target.attachedToTarget",
                            "params": {
                                "targetInfo": {
                                    "type": "page",
                                    "targetId": f"page-{page.number}",
                                    "browserContextId": page.context.name,
                                }
                            },
                        }
                    ),
                }
            )
            receiver(
                {
                    "sessionId": params["sessionId"],
                    "message": json.dumps(
                        {"id": 1, "error": "mapping"}
                        if self.mapping_error
                        else {"id": 1, "result": {}}
                    ),
                }
            )
            return {}
        if method == "Target.detachFromTarget":
            self.sessions.pop(params["sessionId"])
        if method == "Extensions.triggerAction":
            assert self.focused is not None
            self.focused.context.helper.clicked = 999 if self.bad_click else self.focused.number
        return {}


class FakeContext:
    def __init__(self, browser: FakeBrowser, name: str, incognito: bool = True) -> None:
        self.browser = browser
        self.name = name
        self.incognito = incognito
        self.helper: FakePage
        self.cdp_sessions: list[Any] = []

    async def new_page(self) -> FakePage:
        self.helper = FakePage(self, -1)
        return self.helper

    def business_page(self, number: int) -> FakePage:
        page = FakePage(self, number)
        self.browser.root.targets[f"outer-{number}"] = page
        return page

    async def new_cdp_session(self, page: FakePage) -> Any:
        session = Mock()
        session.send = AsyncMock(
            return_value={
                "targetInfo": {
                    "targetId": f"page-{page.number}",
                    "type": "page",
                    "browserContextId": self.name,
                }
            }
        )
        session.detach = AsyncMock()
        self.cdp_sessions.append(session)
        return session


class FakeBrowser:
    def __init__(self) -> None:
        self.root = FakeRoot()

    async def new_browser_cdp_session(self) -> FakeRoot:
        return self.root


async def ready() -> tuple[TabCaptureBridge, FakeContext, FakePage]:
    browser = FakeBrowser()
    bridge = TabCaptureBridge(browser)
    context = FakeContext(browser, "context-a")
    await bridge.open_context(context)
    context.business_page(1)  # Same URL/title as the requested page, wrong identity.
    page = context.business_page(2)
    assert await bridge.bind_page(context, page) == 2
    return bridge, context, page


def packet(handle: Any, seq: int = 1, data: bytes = b"webm") -> dict[str, Any]:
    return {
        "kind": "chunk",
        "generation": handle.generation,
        "token": handle.token,
        "seq": seq,
        "data": base64.b64encode(data).decode(),
    }


async def test_exact_nested_mapping_and_private_helper_lifecycle() -> None:
    bridge, context, page = await ready()
    try:
        assert context.helper not in bridge.contexts[context].pages
        assert bridge.contexts[context].pages[page].outer_id == "outer-2"
        assert not bridge.root.sessions and not bridge.root.events
        assert all(session.detach.await_count == 1 for session in context.cdp_sessions)
        assert bridge.root.commands[0][1]["enableInIncognito"] is True
        assert await bridge.bind_page(context, page) == 2
    finally:
        await bridge.close()
    assert context.helper.closed and not bridge.contexts
    assert bridge.extension_id is None


async def test_rejects_non_incognito_context_and_external_initial_binding() -> None:
    bridge = TabCaptureBridge(FakeBrowser())
    context = FakeContext(bridge.browser, "default", incognito=False)
    with pytest.raises(BrowserError, match="browser_media_codec_unsupported"):
        await bridge.open_context(context)
    assert context.helper.closed and not bridge.contexts
    await bridge.close()
    bridge, context, _ = await ready()
    page = context.business_page(3)
    page.url = "https://example.com"
    with pytest.raises(BrowserError, match="browser_media_capture_failed"):
        await bridge.bind_page(context, page)
    await bridge.close()


async def test_wrong_action_identity_is_rejected_before_capture() -> None:
    bridge, context, page = await ready()
    bridge.root.bad_click = True
    with pytest.raises(BrowserError, match="browser_media_capture_failed"):
        await bridge.start_capture(context, page, 1, Mock(), Mock())
    assert not any(item["command"] == "start" for item in context.helper.commands)
    await bridge.close()


async def test_mapping_error_detaches_nested_session() -> None:
    bridge, context, _ = await ready()
    bridge.root.mapping_error = True
    with pytest.raises(BrowserError, match="browser_media_capture_failed"):
        await bridge.bind_page(context, context.business_page(3))
    assert not bridge.root.sessions and not bridge.root.events
    await bridge.close()


async def test_delivery_awaits_callback_and_rejects_cross_context_frame_and_generation() -> None:
    bridge, context, page = await ready()
    entered, released = asyncio.Event(), asyncio.Event()

    async def on_chunk(seq: int, data: bytes) -> None:
        assert (seq, data) == (1, b"webm")
        entered.set()
        await released.wait()

    handle = await bridge.start_capture(context, page, "generation-a", on_chunk, Mock())
    source = {"page": context.helper, "frame": context.helper.main_frame}
    for bad_source, message in [
        ({"page": page, "frame": page.main_frame}, packet(handle)),
        ({"page": context.helper, "frame": object()}, packet(handle)),
        (source, {**packet(handle), "generation": "generation-b"}),
        (source, {**packet(handle), "token": "other"}),
    ]:
        assert await context.helper.binding(bad_source, message) is False
    assert not entered.is_set()
    pending = asyncio.create_task(context.helper.binding(source, packet(handle)))
    await entered.wait()
    assert not pending.done() and handle.next_seq == 1
    released.set()
    assert await pending is True
    assert handle.next_seq == 2
    await handle.stop()
    await handle.stop()
    assert sum(item["command"] == "stop" for item in context.helper.commands) == 1
    assert await context.helper.binding(source, packet(handle, 2)) is False
    await bridge.close()


@pytest.mark.parametrize(
    "change",
    [
        {"seq": 5},
        {"seq": True},
        {"data": "!invalid!"},
        {"data": ""},
    ],
)
async def test_invalid_chunk_fails_closed_with_fixed_error(change: dict[str, Any]) -> None:
    bridge, context, page = await ready()
    on_error, on_chunk = Mock(), Mock()
    handle = await bridge.start_capture(context, page, 1, on_chunk, on_error)
    source = {"page": context.helper, "frame": context.helper.main_frame}
    assert await context.helper.binding(source, {**packet(handle), **change}) is False
    on_error.assert_called_once_with("browser_media_stream_failed")
    on_chunk.assert_not_called()
    await bridge.close()


async def test_chunk_size_limit_and_callback_timeout(monkeypatch: Any) -> None:
    bridge, context, page = await ready()
    monkeypatch.setattr("pi_agent_core_py.web.browser.tab_capture.MAX_CHUNK_BYTES", 2)
    on_error = Mock()
    handle = await bridge.start_capture(context, page, 1, Mock(), on_error)
    source = {"page": context.helper, "frame": context.helper.main_frame}
    assert await context.helper.binding(source, packet(handle)) is False
    await handle.stop()
    monkeypatch.setattr("pi_agent_core_py.web.browser.tab_capture.MAX_CHUNK_BYTES", 1024)
    monkeypatch.setattr("pi_agent_core_py.web.browser.tab_capture.DELIVERY_SECONDS", 0.01)
    blocked = asyncio.Event()
    handle = await bridge.start_capture(
        context, page, 2, lambda seq, data: blocked.wait(), on_error
    )
    assert await context.helper.binding(source, packet(handle)) is False
    assert on_error.call_count == 2
    await bridge.close()


async def test_audio_safety_failure_stops_helper_and_allows_explicit_retry() -> None:
    bridge, context, page = await ready()
    context.helper.start_result["audioSafe"] = False
    with pytest.raises(BrowserError, match="browser_media_capture_failed"):
        await bridge.start_capture(context, page, 1, Mock(), Mock())
    assert bridge.contexts[context].active is None
    assert context.helper.commands[-1]["command"] == "stop"
    await bridge.close()


async def test_context_close_and_page_close_release_owned_capture_only() -> None:
    bridge, first, page = await ready()
    second = FakeContext(bridge.browser, "context-b")
    await bridge.open_context(second)
    other = second.business_page(3)
    await bridge.bind_page(second, other)
    first_handle = await bridge.start_capture(first, page, 1, Mock(), Mock())
    second_handle = await bridge.start_capture(second, other, 1, Mock(), Mock())
    await bridge.close_context(first)
    assert first_handle.stopped and not second_handle.stopped
    await other.close()
    await bridge.close()
    assert second_handle.stopped


async def test_old_generation_and_navigated_helper_cannot_deliver() -> None:
    bridge, context, page = await ready()
    old = await bridge.start_capture(context, page, "old", Mock(), Mock())
    stale = packet(old)
    await old.stop()
    on_chunk = Mock()
    current = await bridge.start_capture(context, page, "new", on_chunk, Mock())
    source = {"page": context.helper, "frame": context.helper.main_frame}
    assert await context.helper.binding(source, stale) is False
    assert not current.failed
    context.helper.url = "https://example.com"
    assert await context.helper.binding(source, packet(current)) is False
    on_chunk.assert_not_called()
    await current.stop()
    assert context.helper.closed
    await bridge.close()


async def test_duplicate_in_flight_chunk_fails_without_unbounded_callback_queue() -> None:
    bridge, context, page = await ready()
    entered, released = asyncio.Event(), asyncio.Event()

    async def block(seq: int, data: bytes) -> None:
        entered.set()
        await released.wait()

    on_error = Mock()
    handle = await bridge.start_capture(context, page, 1, block, on_error)
    source = {"page": context.helper, "frame": context.helper.main_frame}
    delivering = asyncio.create_task(context.helper.binding(source, packet(handle)))
    await entered.wait()
    assert await context.helper.binding(source, packet(handle)) is False
    on_error.assert_called_once_with("browser_media_stream_failed")
    released.set()
    assert await delivering is False
    await bridge.close()


async def test_failed_stop_closes_helper_and_invalidates_context() -> None:
    bridge, context, page = await ready()
    handle = await bridge.start_capture(context, page, 1, Mock(), Mock())
    context.helper.evaluate = AsyncMock(side_effect=RuntimeError("private details"))
    await handle.stop()
    assert context.helper.closed
    with pytest.raises(BrowserError):
        await bridge.start_capture(context, page, 2, Mock(), Mock())
    await bridge.close()


async def test_unconfirmed_stop_retains_owner_slot_and_allows_cleanup_retry() -> None:
    bridge, context, page = await ready()
    handle = await bridge.start_capture(context, page, 1, Mock(), Mock())
    context.helper.evaluate = AsyncMock(side_effect=RuntimeError("stop RPC lost"))
    context.helper.close = AsyncMock(side_effect=RuntimeError("close RPC lost"))
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await handle.stop()
    assert bridge.contexts[context].active is handle
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await bridge.close_context(context)
    assert context in bridge.contexts
    assert bridge.contexts[context].active is handle
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await bridge.stop_capture(context)
    assert bridge.contexts[context].active is handle
    # A subsequent close can be confirmed after the transient RPC failure.
    context.helper.evaluate = AsyncMock(return_value=True)
    context.helper.close = AsyncMock()
    await bridge.stop_capture(context)
    assert bridge.contexts[context].active is None
    await bridge.stop_capture(context)
    await bridge.close_context(context)
    assert context not in bridge.contexts
    await bridge.stop_capture(context)
    await bridge.close()


async def test_load_failure_detaches_root_and_hides_driver_exception() -> None:
    browser = FakeBrowser()
    browser.root.send = AsyncMock(side_effect=RuntimeError("private path or identifier"))
    bridge = TabCaptureBridge(browser)
    with pytest.raises(BrowserError, match="^browser_media_unavailable$"):
        await bridge.start()
    browser.root.detach.assert_awaited_once()
    assert bridge.extension_id is None and bridge.root is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"width": 1280},
        {"height": 720},
        {"frameRate": 31},
        {"frameRate": True},
        {"mime": "video/mp4"},
    ],
)
async def test_noncontract_metadata_stops_capture(metadata: dict[str, Any]) -> None:
    bridge, context, page = await ready()
    context.helper.start_result.update(metadata)
    with pytest.raises(BrowserError):
        await bridge.start_capture(context, page, 1, Mock(), Mock())
    assert context.helper.commands[-1]["command"] == "stop"
    assert bridge.contexts[context].active is None
    await bridge.close()


def test_extension_has_only_approved_permissions_and_no_public_transport() -> None:
    manifest = json.loads((EXTENSION_PATH / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["permissions"]) == {"tabCapture", "activeTab", "tabs"}
    assert manifest["incognito"] == "split"
    assert not {
        "externally_connectable",
        "content_scripts",
        "web_accessible_resources",
        "host_permissions",
    }.intersection(manifest)
    assert "connect-src 'none'" in manifest["content_security_policy"]["extension_pages"]
    script = (EXTENSION_PATH / "capture.js").read_text(encoding="utf-8")
    assert "local_echo=false" in script and "audioSafe: localSuppressed" in script
    assert "record.queuedBytes + event.data.size > MAX_QUEUE" in script
    assert "new MediaRecorder" in script and "record.recorder.start(100)" in script
    assert "videoKeyFrameIntervalCount:" not in script
    assert "videoKeyFrameIntervalDuration: 1000" in script
    assert "minWidth: 1920" in script and "minHeight: 1080" in script
    assert "minFrameRate: 30, maxFrameRate: 30" in script
    assert "if (record.prefixBytes < 4) return" in script
    assert "new Blob(record.prefix)" in script
    assert "initial.slice(offset, offset + MAX_CHUNK)" in script


def test_js_recorder_coalesces_split_ebml_prefix_without_losing_bytes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed only for the extension's isolated JavaScript unit test")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const delivered = [];
const muting = [];
const tracks = [];
let recorder;
class Recorder {
  static isTypeSupported() { return true; }
  constructor(stream, options) {
    recorder = this;
    this.state = 'inactive';
    assert.equal(options.videoKeyFrameIntervalDuration, 1000);
    assert.equal(options.videoKeyFrameIntervalCount, undefined);
  }
  start(interval) { assert.equal(interval, 100); this.state = 'recording'; }
  stop() { this.state = 'inactive'; }
}
function track(kind) {
  const item = {kind, stopped: false, stop() { this.stopped = true; },
    getSettings() { return kind === 'audio' ? {deviceId: 'tab?local_echo=false'} :
      {width: 1920, height: 1080, frameRate: 30}; }};
  tracks.push(item);
  return item;
}
const sandbox = {
  Blob, Uint8Array, setTimeout, clearTimeout, MediaRecorder: Recorder,
  btoa: text => Buffer.from(text, 'binary').toString('base64'),
  addEventListener() {},
  testBinding: async message => {
    assert.equal(message.kind, 'chunk');
    delivered.push({seq: message.seq, bytes: Buffer.from(message.data, 'base64')});
    return true;
  },
  chrome: {
    extension: {inIncognitoContext: true}, action: {},
    tabs: {update: async (id, values) => { muting.push(values.muted); }},
    tabCapture: {
      getMediaStreamId: async () => 'private',
      getCapturedTabs: async () => [{tabId: 7, status: 'active'}],
    },
  },
  navigator: {mediaDevices: {getUserMedia: async options => {
    assert.equal(options.video.mandatory.minFrameRate, 30);
    assert.equal(options.video.mandatory.maxFrameRate, 30);
    assert.equal(options.video.mandatory.chromeMediaSource, 'tab');
    assert.equal(options.audio.mandatory.chromeMediaSource, 'tab');
    const audio = track('audio'), video = track('video');
    return {getAudioTracks: () => [audio], getVideoTracks: () => [video],
      getTracks: () => [audio, video]};
  }}},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const settle = async () => {
  for (let index = 0; index < 5; index++) {
    await new Promise(resolve => setImmediate(resolve));
  }
};
const emit = bytes => recorder.ondataavailable({data: new Blob([bytes])});
(async () => {
  await sandbox.piCapture.call({command: 'init', binding: 'testBinding'});
  await sandbox.piCapture.call({command: 'start', tabId: 7, generation: 1, token: 'one'});
  emit(Buffer.from([0x1a]));
  await settle();
  assert.equal(delivered.length, 0);
  emit(Buffer.from([0x45, 0xdf]));
  await settle();
  assert.equal(delivered.length, 0);
  emit(Buffer.from([0xa3, 0x10]));
  await settle();
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].seq, 1);
  assert.equal(delivered[0].bytes.toString('hex'), '1a45dfa310');
  await sandbox.piCapture.call({command: 'stop', token: 'one'});
  assert.equal(muting.at(-1), true);
  assert.ok(tracks.every(item => item.stopped));

  delivered.length = 0;
  await sandbox.piCapture.call({command: 'start', tabId: 7, generation: 2, token: 'two'});
  const max = 2 * 1024 * 1024;
  const prefix = Buffer.from([0x1a, 0x45, 0xdf]);
  const payload = Buffer.alloc(max, 0x41);
  payload[0] = 0xa3;
  emit(prefix);
  emit(payload);
  await settle();
  assert.equal(delivered.length, 2);
  assert.deepEqual(delivered.map(item => item.seq), [1, 2]);
  assert.deepEqual(delivered.map(item => item.bytes.length), [max, 3]);
  assert.deepEqual(Buffer.concat(delivered.map(item => item.bytes)),
    Buffer.concat([prefix, payload]));
  await sandbox.piCapture.call({command: 'stop', token: 'two'});
  process.stdout.write('ok');
})().catch(error => { process.stderr.write(String(error.stack)); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script, str(EXTENSION_PATH / "capture.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"

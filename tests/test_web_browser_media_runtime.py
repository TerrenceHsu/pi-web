"""Ordered media buffers, watch leases and cleanup without starting Chromium."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from pi_agent_core_py.web.browser import media as media_module
from pi_agent_core_py.web.browser import runtime as runtime_module
from pi_agent_core_py.web.browser.frames import PageFrames
from pi_agent_core_py.web.browser.media import (
    MAX_CHUNK_BYTES,
    MAX_QUEUED_BYTES,
    WATCH_SECONDS,
    MediaSubscription,
)
from pi_agent_core_py.web.browser.runtime import BrowserEngine, BrowserError, BrowserSession

INIT = b"\x1a\x45\xdf\xa3synthetic-init"


def build_engine() -> BrowserEngine:
    engine = BrowserEngine()
    engine.browser = SimpleNamespace(close=AsyncMock())
    engine.capture_bridge = SimpleNamespace(
        start_capture=AsyncMock(return_value=SimpleNamespace(stop=AsyncMock())),
        stop_capture=AsyncMock(),
        close_context=AsyncMock(),
        discard_closed_context=Mock(),
        close=AsyncMock(),
    )
    context = SimpleNamespace(close=AsyncMock())
    session = BrowserSession(context=context, touched=0)
    engine.sessions["s"] = session
    for pid in ("p", "other"):
        page = SimpleNamespace(
            viewport_size={"width": 640, "height": 480},
            title=AsyncMock(return_value="Synthetic media"),
            url="https://fixture.example/",
            is_closed=lambda: False,
            close=AsyncMock(),
        )

        async def resize(size: dict[str, int], page: Any = page) -> None:
            page.viewport_size = size

        page.set_viewport_size = AsyncMock(side_effect=resize)
        cdp = SimpleNamespace(
            send=AsyncMock(), on=Mock(), remove_listener=Mock(), detach=AsyncMock()
        )
        session.pages[pid] = page
        session.views[pid] = PageFrames(page, cdp)
    return engine


@pytest.fixture
async def engine() -> AsyncIterator[BrowserEngine]:
    instance = build_engine()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.parametrize("requested", ["obsolete-generation", "foreign-generation"])
async def test_scoped_stop_does_not_release_a_different_capture(
    engine: BrowserEngine, requested: str,
) -> None:
    subscription = MediaSubscription("s", "p", "current-generation")
    stop = AsyncMock()
    subscription.handle = SimpleNamespace(stop=stop)
    engine.media[subscription.token] = subscription
    await engine.command("s", "p", {"action": "media_stop", "media_generation": requested})
    assert engine.media[subscription.token] is subscription and not subscription.closed
    stop.assert_not_awaited()


async def test_scoped_stop_checks_page_and_then_releases_only_matching_generation(
    engine: BrowserEngine,
) -> None:
    subscription = MediaSubscription("s", "p", "owned-generation")
    stop = AsyncMock()
    subscription.handle = SimpleNamespace(stop=stop)
    engine.media[subscription.token] = subscription
    action = {"action": "media_stop", "media_generation": "owned-generation"}
    await engine.command("s", "other", action)
    stop.assert_not_awaited()
    await engine.command("s", "p", action)
    stop.assert_awaited_once()
    assert not engine.media
    # Late retries remain harmless after the original generation has closed.
    await engine.command("s", "p", action)
    stop.assert_awaited_once()


@pytest.mark.parametrize("generation", ["", "bad token", "x" * 129, 1])
def test_media_stop_generation_is_a_bounded_opaque_id(generation: Any) -> None:
    from pydantic import ValidationError

    from pi_agent_core_py.web.browser.api import BrowserAction

    with pytest.raises(ValidationError):
        BrowserAction(action="media_stop", media_generation=generation)


@pytest.mark.parametrize(
    ("sequence", "data"),
    [(0, INIT), (2, INIT), (True, INIT), (1, b""), (1, b"no-init"), (1, "text")],
)
async def test_invalid_initial_chunks_fail_closed(sequence: Any, data: Any) -> None:
    subscription = MediaSubscription("s", "p", "token")
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await subscription.push(sequence, data)
    assert subscription.error == "browser_media_stream_failed" and subscription.queued_bytes == 0
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await subscription.next()
    await subscription.close()


async def test_chunk_order_and_byte_accounting_preserve_every_dependency() -> None:
    subscription = MediaSubscription("s", "p", "token")
    await subscription.push(1, INIT)
    await subscription.push(2, b"cluster-two")
    assert subscription.queued_bytes == len(INIT) + len(b"cluster-two")
    assert await subscription.next() == (1, INIT)
    assert subscription.queued_bytes == len(b"cluster-two")
    assert await subscription.next() == (2, b"cluster-two")
    assert subscription.queued_bytes == 0
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await subscription.push(4, b"gap")
    assert subscription.queue.qsize() == 1 and subscription.queued_bytes == 0
    await subscription.close()


@pytest.mark.parametrize("limit", ["chunk", "bytes", "count"])
async def test_media_buffer_limits_fail_instead_of_dropping_dependent_chunks(limit: str) -> None:
    subscription = MediaSubscription("s", "p", "token")
    if limit == "chunk":
        next_sequence, overflow = 1, INIT + b"x" * MAX_CHUNK_BYTES
    elif limit == "bytes":
        payload = INIT + b"x" * (MAX_CHUNK_BYTES - len(INIT))
        for sequence in range(1, MAX_QUEUED_BYTES // MAX_CHUNK_BYTES + 1):
            await subscription.push(sequence, payload)
        assert subscription.queued_bytes == MAX_QUEUED_BYTES
        next_sequence, overflow = subscription.sequence + 1, b"x"
    else:
        for sequence in range(1, subscription.queue.maxsize + 1):
            await subscription.push(sequence, INIT if sequence == 1 else b"x")
        next_sequence, overflow = subscription.sequence + 1, b"x"
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await subscription.push(next_sequence, overflow)
    assert subscription.queued_bytes == 0 and subscription.queue.qsize() == 1
    with pytest.raises(BrowserError):
        await subscription.next()
    await subscription.close()


async def test_only_explicit_watch_renewal_extends_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    subscription = MediaSubscription("s", "p", "token")
    deadline = subscription.deadline
    clock.now += 2
    await subscription.push(1, INIT)
    assert await subscription.next() == (1, INIT)
    assert subscription.deadline == deadline
    subscription.renew()
    assert subscription.deadline == clock.now + WATCH_SECONDS
    clock.now = subscription.deadline + 0.01
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        subscription.renew()
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        await subscription.next()
    await subscription.close()


async def test_chunk_arriving_after_watch_deadline_is_not_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    subscription = MediaSubscription("s", "p", "token")
    entered = asyncio.Event()
    original_get = subscription.queue.get

    async def wait_for_chunk() -> tuple[int, bytes] | None:
        entered.set()
        return await original_get()

    monkeypatch.setattr(subscription.queue, "get", wait_for_chunk)
    reader = asyncio.create_task(subscription.next())
    await asyncio.wait_for(entered.wait(), 1)
    clock.now = subscription.deadline + 1
    await subscription.push(1, INIT)
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        await asyncio.wait_for(reader, 1)
    assert subscription.queued_bytes == 0
    await subscription.close()


async def test_capture_failure_between_queue_read_and_delivery_does_not_publish_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = MediaSubscription("s", "p", "token")
    await subscription.push(1, INIT)
    original_get = subscription.queue.get

    async def read_then_capture_fails() -> tuple[int, bytes] | None:
        item = await original_get()
        # A callback can fail after Queue.get removes a chunk but before
        # asyncio.wait_for resumes the reader that would otherwise deliver it.
        subscription.fail("browser_media_capture_failed")
        return item

    monkeypatch.setattr(subscription.queue, "get", read_then_capture_fails)
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await subscription.next()
    assert subscription.queued_bytes == 0
    await subscription.close()


async def test_close_wakes_pending_reader_and_retries_failed_track_stop() -> None:
    subscription = MediaSubscription("s", "p", "token")
    handle = SimpleNamespace(stop=AsyncMock(side_effect=[OSError("synthetic stop failure"), None]))
    subscription.handle = handle
    reader = asyncio.create_task(subscription.next())
    await asyncio.sleep(0)
    with pytest.raises(OSError, match="synthetic stop failure"):
        await subscription.close()
    with pytest.raises(BrowserError, match="^browser_media_unavailable$"):
        await asyncio.wait_for(reader, 1)
    assert subscription.closed and subscription.handle is handle
    await subscription.close()
    assert subscription.handle is None and handle.stop.await_count == 2
    await subscription.close()
    assert handle.stop.await_count == 2


async def test_concurrent_media_start_reserves_single_account_slot(engine: BrowserEngine) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    handle = SimpleNamespace(stop=AsyncMock())

    async def start(*args: Any) -> Any:
        entered.set()
        await release.wait()
        return handle

    engine.capture_bridge.start_capture.side_effect = start
    first = asyncio.create_task(engine.subscribe_media("s", "p", "first"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(engine.subscribe_media("s", "other", "second"))
    assert set(engine.media) == {"first"}
    release.set()
    await asyncio.wait_for(first, 1)
    with pytest.raises(BrowserError, match="^browser_media_limit$"):
        await asyncio.wait_for(second, 1)
    assert engine.capture_bridge.start_capture.await_count == 1
    await engine.unsubscribe_media("first")
    handle.stop.assert_awaited_once()
    assert engine.media == {}


async def test_media_start_releases_only_target_image_stream_then_locks_geometry(
    engine: BrowserEngine,
) -> None:
    session = engine.sessions["s"]
    for pid in ("p", "other"):
        session.views[pid].start = AsyncMock()  # type: ignore[method-assign]
        await engine.subscribe("s", pid, f"image-{pid}")
    await engine.subscribe_media("s", "p", "media")
    assert set(engine.streams) == {"image-other"}
    assert session.pages["p"].viewport_size == {"width": 1920, "height": 1080}
    assert session.views["p"].dpr == 1
    with pytest.raises(BrowserError, match="^browser_media_busy$"):
        await engine.subscribe("s", "p", "new-image")
    with pytest.raises(BrowserError, match="^browser_media_busy$"):
        await engine.command("s", "p", {"action": "resize", "width": 640, "height": 480, "dpr": 2})
    await engine.command("s", "p", {"action": "media_stop"})
    assert engine.media == {} and "image-other" in engine.streams
    await engine.command("s", "p", {"action": "resize", "width": 640, "height": 480, "dpr": 2})
    assert session.pages["p"].viewport_size == {"width": 640, "height": 480}


async def test_runtime_chunk_reads_do_not_extend_session_lease(engine: BrowserEngine) -> None:
    await engine.subscribe_media("s", "p", "media")
    subscription = engine.media["media"]
    await subscription.push(1, INIT)
    deadline = subscription.deadline
    result = await engine.next_media("media")
    assert result and result["data"] == INIT and result["generation"] == "media"
    assert result["page"]["id"] == "p" and result["page"]["width"] == 1920
    assert subscription.deadline == deadline and engine.sessions["s"].touched == 0
    await engine.renew_media("media")
    assert subscription.deadline >= deadline and engine.sessions["s"].touched > 0
    subscription.deadline = 0
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        await engine.renew_media("media")


async def test_source_geometry_mismatch_does_not_escape_as_owned_media(
    engine: BrowserEngine,
) -> None:
    await engine.subscribe_media("s", "p", "media")
    await engine.media["media"].push(1, INIT)
    engine.sessions["s"].pages["p"].viewport_size = {"width": 320, "height": 712}
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await engine.next_media("media")


async def test_start_failure_and_cancellation_release_media_quota(engine: BrowserEngine) -> None:
    bridge = engine.capture_bridge
    bridge.start_capture.side_effect = BrowserError("browser_media_capture_failed")
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await engine.subscribe_media("s", "p", "failed")
    assert engine.media == {}
    entered = asyncio.Event()

    async def pending(*args: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    bridge.start_capture.side_effect = pending
    task = asyncio.create_task(engine.subscribe_media("s", "p", "cancelled"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.media == {}


async def test_failed_track_stop_keeps_quota_until_cleanup_succeeds(engine: BrowserEngine) -> None:
    await engine.subscribe_media("s", "p", "media")
    handle = engine.media["media"].handle
    handle.stop.side_effect = [OSError("synthetic teardown failure"), None]
    with pytest.raises(OSError, match="synthetic teardown failure"):
        await engine.unsubscribe_media("media")
    assert "media" in engine.media and engine.media["media"].closed
    with pytest.raises(BrowserError, match="^browser_media_limit$"):
        await engine.subscribe_media("s", "other", "new")
    await engine.unsubscribe_media("media")
    assert engine.media == {} and handle.stop.await_count == 2


@pytest.mark.parametrize("operation", ["session", "page", "engine"])
async def test_delete_and_close_release_capture_tracks_and_subscriptions(
    engine: BrowserEngine, operation: str
) -> None:
    await engine.subscribe_media("s", "p", "media")
    handle = engine.media["media"].handle
    session = engine.sessions["s"]
    bridge = engine.capture_bridge
    if operation == "session":
        await engine.close_session("s")
        bridge.close_context.assert_awaited_once_with(session.context)
        session.context.close.assert_awaited_once()
        assert "s" not in engine.sessions
    elif operation == "page":
        await engine.command("s", "p", {"action": "close"})
        session.pages["p"].close.assert_awaited_once()
    else:
        await engine.close()
        assert engine.sessions == {} and engine.capture_bridge is None
    handle.stop.assert_awaited_once()
    assert engine.media == {}


async def test_watchdog_expires_unacknowledged_media(engine: BrowserEngine) -> None:
    await engine.subscribe_media("s", "p", "media")
    subscription = engine.media["media"]
    stopped = asyncio.Event()
    subscription.handle.stop.side_effect = stopped.set
    subscription.deadline = 0
    await asyncio.wait_for(stopped.wait(), 2)
    assert engine.media == {} and subscription.closed


async def test_capture_setup_gets_full_initial_watch_lease(
    engine: BrowserEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    handle = SimpleNamespace(stop=AsyncMock())

    async def slow_start(*args: Any) -> Any:
        clock.now += WATCH_SECONDS - 0.5
        return handle

    engine.capture_bridge.start_capture.side_effect = slow_start
    await engine.subscribe_media("s", "p", "media")
    assert engine.media["media"].deadline >= clock.now + WATCH_SECONDS


async def test_cancelling_viewer_does_not_cancel_page_owned_preparation(
    engine: BrowserEngine,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare() -> None:
        entered.set()
        await release.wait()

    preparation = asyncio.create_task(prepare())
    engine.sessions["s"].preparation["p"] = preparation
    await entered.wait()
    viewer = asyncio.create_task(engine.subscribe_media("s", "p", "viewer"))
    await asyncio.sleep(0)
    viewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await viewer
    assert not preparation.cancelled()
    assert engine.media == {}
    release.set()
    await preparation


async def test_local_deleted_session_cannot_reopen_media_or_retain_pending_quota() -> None:
    runtime = runtime_module.LocalBrowserRuntime()
    runtime.engine_factory = build_engine
    try:
        token = await runtime.subscribe_media("s", "p")
        assert runtime.engine
        handle = runtime.engine.media[token].handle
        await runtime.delete_session("s")
        assert runtime.engine.media == {} and "s" not in runtime.engine.sessions
        handle.stop.assert_awaited_once()
        with pytest.raises(BrowserError, match="^browser_session_deleted$"):
            await runtime.subscribe_media("s", "p")
        assert runtime.pending == 0
    finally:
        await runtime.close()


@pytest.mark.parametrize("change", ["stop", "capture_error", "lease", "page", "session"])
async def test_media_metadata_await_rechecks_capture_and_source_ownership(
    engine: BrowserEngine, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    await engine.subscribe_media("s", "p", "media")
    subscription = engine.media["media"]
    session = engine.sessions["s"]
    original_page = session.pages["p"]
    await subscription.push(1, INIT)
    metadata = await engine.describe(session, "p")
    entered, release = asyncio.Event(), asyncio.Event()

    async def describe(*args: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return metadata

    monkeypatch.setattr(engine, "describe", describe)
    reader = asyncio.create_task(engine.next_media("media"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "stop":
            await engine.unsubscribe_media("media")
        elif change == "capture_error":
            subscription.fail("browser_media_capture_failed")
        elif change == "lease":
            subscription.deadline = 0
        elif change == "page":
            session.pages["p"] = SimpleNamespace(**vars(original_page))
        else:
            engine.sessions["s"] = BrowserSession(session.context, pages=dict(session.pages))
        release.set()
        with pytest.raises(BrowserError) as error:
            await asyncio.wait_for(reader, 1)
        assert error.value.code in {
            "browser_media_unavailable",
            "browser_media_capture_failed",
            "browser_media_timeout",
            "browser_page_not_found",
        }
        assert subscription.queued_bytes == 0
    finally:
        release.set()
        await asyncio.gather(reader, return_exceptions=True)
        engine.sessions["s"] = session
        session.pages["p"] = original_page


async def test_failed_capture_setup_retains_pending_handle_until_bridge_stop_succeeds(
    engine: BrowserEngine,
) -> None:
    bridge = engine.capture_bridge
    bridge.start_capture.side_effect = BrowserError("browser_media_capture_failed")
    bridge.stop_capture.side_effect = BrowserError("browser_media_capture_failed")
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await engine.subscribe_media("s", "p", "failed")
    assert set(engine.media) == {"failed"}
    assert engine.media["failed"].handle is not None and engine.media["failed"].closed
    assert bridge.stop_capture.await_count == 1
    assert engine.media_watchdog is not None and not engine.media_watchdog.done()
    with pytest.raises(BrowserError, match="^browser_media_limit$"):
        await engine.subscribe_media("s", "other", "new")
    bridge.stop_capture.side_effect = None
    await engine.unsubscribe_media("failed")
    assert engine.media == {} and bridge.stop_capture.await_count == 2


async def test_failed_pending_capture_is_retried_by_watchdog(engine: BrowserEngine) -> None:
    bridge = engine.capture_bridge
    bridge.start_capture.side_effect = BrowserError("browser_media_capture_failed")
    retried = asyncio.Event()

    async def stop_capture(*args: Any) -> None:
        if bridge.stop_capture.await_count == 1:
            raise BrowserError("browser_media_capture_failed")
        retried.set()

    bridge.stop_capture.side_effect = stop_capture
    with pytest.raises(BrowserError, match="^browser_media_capture_failed$"):
        await engine.subscribe_media("s", "p", "failed")
    assert "failed" in engine.media
    await asyncio.wait_for(retried.wait(), 2)
    assert engine.media == {}


async def test_cancelled_capture_setup_keeps_quota_during_pending_cleanup(
    engine: BrowserEngine,
) -> None:
    bridge = engine.capture_bridge
    started, stopping, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start_capture(*args: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    async def stop_capture(*args: Any) -> None:
        stopping.set()
        await released.wait()

    bridge.start_capture.side_effect = start_capture
    bridge.stop_capture.side_effect = stop_capture
    viewer = asyncio.create_task(engine.subscribe_media("s", "p", "cancelled"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        viewer.cancel()
        await asyncio.wait_for(stopping.wait(), 1)
        assert set(engine.media) == {"cancelled"}
        assert engine.media["cancelled"].handle is not None
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(viewer, 1)
        assert engine.media == {}
    finally:
        released.set()
        await asyncio.gather(viewer, return_exceptions=True)


async def test_engine_close_forces_browser_teardown_after_track_stop_failure(
    engine: BrowserEngine,
) -> None:
    await engine.subscribe_media("s", "p", "media")
    engine.media["media"].handle.stop.side_effect = OSError("synthetic stop failure")
    browser, bridge = engine.browser, engine.capture_bridge
    bridge.close.side_effect = BrowserError("browser_media_capture_failed")
    playwright = SimpleNamespace(stop=AsyncMock())
    engine.playwright = playwright
    await asyncio.gather(engine.close(), return_exceptions=True)
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert engine.browser is None and engine.playwright is None
    assert engine.media == {} and engine.sessions == {} and engine.capture_bridge is None


async def test_session_close_releases_failed_capture_only_after_context_closed(
    engine: BrowserEngine,
) -> None:
    await engine.subscribe_media("s", "p", "media")
    subscription = engine.media["media"]
    subscription.handle.stop.side_effect = OSError("synthetic stop failure")
    bridge = engine.capture_bridge
    bridge.close_context.side_effect = BrowserError("browser_media_capture_failed")
    context = engine.sessions["s"].context

    async def close_context() -> None:
        assert engine.media.get("media") is subscription

    context.close.side_effect = close_context
    await engine.close_session("s")
    context.close.assert_awaited_once()
    bridge.discard_closed_context.assert_called_once_with(context)
    assert engine.media == {} and "s" not in engine.sessions


@pytest.mark.parametrize("had_first_chunk", [False, True])
async def test_watch_ack_cannot_extend_stalled_producer_deadline(
    monkeypatch: pytest.MonkeyPatch, had_first_chunk: bool
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    subscription = MediaSubscription("s", "p", "token")
    subscription.begin_watch()
    if had_first_chunk:
        await subscription.push(1, INIT)
        assert await subscription.next() == (1, INIT)
    clock.now += 7
    subscription.renew()
    assert subscription.deadline == clock.now + WATCH_SECONDS
    clock.now += 2
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        await subscription.next()
    with pytest.raises(BrowserError, match="^browser_media_stream_failed$"):
        subscription.renew()
    await subscription.close()


async def test_setup_crossing_initial_source_deadline_is_not_stopped_after_success(
    engine: BrowserEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=100.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(media_module, "time", fake_time)
    monkeypatch.setattr(runtime_module, "time", fake_time)
    entered, release = asyncio.Event(), asyncio.Event()
    watchdog_sleeping, watchdog_tick = asyncio.Event(), asyncio.Event()
    handle = SimpleNamespace(stop=AsyncMock())

    async def controlled_watchdog_sleep(seconds: float) -> None:
        watchdog_sleeping.set()
        await watchdog_tick.wait()
        watchdog_tick.clear()

    monkeypatch.setattr(
        runtime_module,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "sleep": controlled_watchdog_sleep}),
    )

    async def slow_start(*args: Any) -> Any:
        entered.set()
        await release.wait()
        return handle

    engine.capture_bridge.start_capture.side_effect = slow_start
    starting = asyncio.create_task(engine.subscribe_media("s", "p", "media"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(watchdog_sleeping.wait(), 1)
        clock.now += 9
        # Run exactly one watchdog tick while setup still holds media_lock;
        # successful setup then resets both initial deadlines before releasing it.
        watchdog_tick.set()
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(starting, 1)
        await asyncio.sleep(0)
        assert "media" in engine.media and engine.media["media"].handle is handle
        handle.stop.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)


async def test_continuing_producer_does_not_extend_unacknowledged_watch_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    subscription = MediaSubscription("s", "p", "token")
    for sequence, elapsed in enumerate((0, 5, 10, 16), start=1):
        clock.now = 100.0 + elapsed
        await subscription.push(sequence, INIT if sequence == 1 else b"cluster")
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        await subscription.next()
    with pytest.raises(BrowserError, match="^browser_media_timeout$"):
        subscription.renew()
    await subscription.close()


@pytest.mark.parametrize("elapsed", [9, 16])
async def test_empty_wait_rechecks_source_and_watch_deadlines_before_heartbeat(
    monkeypatch: pytest.MonkeyPatch, elapsed: int
) -> None:
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    subscription = MediaSubscription("s", "p", "token")

    async def expired_wait() -> None:
        clock.now += elapsed
        raise TimeoutError

    monkeypatch.setattr(subscription.queue, "get", expired_wait)
    expected = "browser_media_stream_failed" if elapsed == 9 else "browser_media_timeout"
    with pytest.raises(BrowserError, match=f"^{expected}$"):
        await subscription.next()
    await subscription.close()

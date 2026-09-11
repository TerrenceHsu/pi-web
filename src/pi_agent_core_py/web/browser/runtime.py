"""Dedicated browser event loop (including Proactor subprocess support on Windows).

Only bounded user interaction commands cross this boundary. There is no evaluate,
filesystem, CDP, Cookie export, or Agent tool API. The optional SDK stays lazy.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import threading
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from .errors import BrowserError as BrowserError
from .frames import BrowserFrame, PageFrames
from .media import MIME, MediaSubscription, PendingCaptureCleanup
from .network import BrowserNetworkDenied, PublicBrowserProxy, destination

T = TypeVar("T")
MAX_CONTEXTS = 3
MAX_PAGES = 6
IDLE_SECONDS = 900


@dataclass
class BrowserSession:
    context: Any
    pages: dict[str, Any] = field(default_factory=dict)
    dialogs: dict[str, Any] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)
    views: dict[str, PageFrames] = field(default_factory=dict)
    navigation_ms: dict[str, float] = field(default_factory=dict)
    preparation: dict[str, asyncio.Task[None]] = field(default_factory=dict)


class BrowserEngine:
    """All fields/SDK objects belong exclusively to the browser thread."""

    def __init__(self) -> None:
        self.playwright: Any = None
        self.browser: Any = None
        self.proxy = PublicBrowserProxy()
        self.sessions: dict[str, BrowserSession] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.start_lock = asyncio.Lock()
        self.context_lock = asyncio.Lock()
        self.page_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self.streams: dict[str, tuple[str, str, PageFrames]] = {}
        self.capture_bridge: Any = None
        self.media: dict[str, MediaSubscription] = {}
        self.media_lock = asyncio.Lock()
        self.media_watchdog: asyncio.Task[None] | None = None

    async def start(self) -> None:
        async with self.start_lock:
            if self.browser is not None:
                return
            try:
                sdk = importlib.import_module("playwright.async_api")
            except ImportError:
                raise BrowserError("browser_dependency_missing") from None
            try:
                self.playwright = await sdk.async_playwright().start()
                proxy_url = await self.proxy.start()
                # Do not pass Provider credentials from the backend environment.
                allowed = {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                    "LOCALAPPDATA",
                    "USERPROFILE",
                    "HOME",
                    "DISPLAY",
                    "XDG_RUNTIME_DIR",
                }
                env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
                self.browser = await self.playwright.chromium.launch(
                    channel="chromium",
                    headless=True,
                    chromium_sandbox=True,
                    timeout=25_000,
                    env=env,
                    ignore_default_args=["--disable-extensions", "--mute-audio"],
                    proxy={
                        "server": proxy_url,
                        "username": self.proxy.username,
                        "password": self.proxy.password,
                        "bypass": "<-loopback>",
                    },
                    args=[
                        # The fake output sink consumes PCM for tabCapture but
                        # never opens speakers, including for racing popups.
                        "--disable-audio-output",
                        "--disable-extensions-except="
                        + str(Path(__file__).with_name("capture_extension").resolve()),
                        "--disable-quic",
                        "--proxy-bypass-list=<-loopback>",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                    ],
                )
                from .tab_capture import TabCaptureBridge

                self.capture_bridge = TabCaptureBridge(self.browser)
                await self.capture_bridge.start()
            except Exception:
                if self.browser:
                    await self.browser.close()
                    self.browser = None
                self.capture_bridge = None
                await self.proxy.close()
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None
                raise BrowserError("browser_start_failed") from None

    async def new_context(self, sid: str) -> BrowserSession:
        async with self.context_lock:
            return await self._new_context(sid)

    async def _new_context(self, sid: str) -> BrowserSession:
        if sid in self.sessions:
            return self.sessions[sid]
        if len(self.sessions) >= MAX_CONTEXTS:
            raise BrowserError("browser_context_limit")
        await self.start()
        # Recheck after the browser startup await: another Session can have opened.
        if len(self.sessions) >= MAX_CONTEXTS:
            raise BrowserError("browser_context_limit")
        context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=False,
            service_workers="block",
            permissions=[],
        )
        session = BrowserSession(context)
        try:
            if self.capture_bridge:
                # The trusted helper is never registered as a user page.
                await self.capture_bridge.open_context(context)
        except BaseException:
            await context.close()
            raise
        self.sessions[sid] = session

        async def guard_route(route: Any) -> None:
            try:
                destination(route.request.url)
            except BrowserNetworkDenied:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", guard_route)
        context.on("page", lambda page: self.register_page(session, page))
        return session

    def register_page(self, session: BrowserSession, page: Any) -> None:
        if page in session.pages.values():
            return
        if len(session.pages) >= MAX_PAGES:
            asyncio.create_task(page.close())
            return
        pid = uuid4().hex
        session.pages[pid] = page
        page.set_default_timeout(5000)
        page.set_default_navigation_timeout(15_000)
        page.on("dialog", lambda dialog: session.dialogs.__setitem__(pid, dialog))
        page.on("close", lambda: self.forget_page(session, pid))
        page.on("download", lambda download: asyncio.create_task(download.cancel()))
        if self.capture_bridge:

            async def prepare() -> None:
                try:
                    await self.capture_bridge.bind_page(session.context, page)
                except Exception:
                    # A popup may already be navigated. It can still be viewed,
                    # but media must fail closed rather than guess by its URL.
                    pass

            session.preparation[pid] = asyncio.create_task(prepare())

    def forget_page(self, session: BrowserSession, pid: str) -> None:
        session.pages.pop(pid, None)
        session.dialogs.pop(pid, None)
        session.navigation_ms.pop(pid, None)
        preparing = session.preparation.pop(pid, None)
        if preparing and not preparing.done():
            preparing.cancel()
        for token, media in list(self.media.items()):
            if media.pid == pid:
                media.fail("browser_page_not_found")
                asyncio.create_task(self.unsubscribe_media(token))
        self.page_locks = {key: lock for key, lock in self.page_locks.items() if key[1] != pid}
        view = session.views.pop(pid, None)
        if view:
            asyncio.create_task(view.close())

    async def describe(self, session: BrowserSession, pid: str) -> dict[str, Any]:
        page = session.pages.get(pid)
        if page is None or page.is_closed():
            raise BrowserError("browser_page_not_found")
        # A modal dialog blocks JS evaluation, including document.title.
        dialog = session.dialogs.get(pid)
        title = "Website dialog"
        if not dialog:
            try:
                title = (await asyncio.wait_for(page.title(), 0.2))[:160]
            except Exception:
                title = "Loading…"
        view = session.views.get(pid)
        return {
            "id": pid,
            "url": page.url[:4096],
            "title": title or "Browser",
            "dialog": dialog.message[:2000] if dialog else None,
            "width": page.viewport_size["width"],
            "height": page.viewport_size["height"],
            "dpr": view.dpr if view else 1,
            "view_version": view.version if view else 0,
            "capture_error": view.error_code if view else None,
            "navigation_ms": session.navigation_ms.get(pid),
        }

    async def view(self, sid: str, pid: str) -> PageFrames:
        async with self.context_lock:
            session = self.sessions.get(sid)
            if not session or pid not in session.pages:
                raise BrowserError("browser_page_not_found")
            if pid not in session.views:
                cdp = await session.context.new_cdp_session(session.pages[pid])
                session.views[pid] = PageFrames(session.pages[pid], cdp)
            return session.views[pid]

    async def subscribe(self, sid: str, pid: str, token: str) -> None:
        async with self.media_lock:
            if any(item.pid == pid and item.sid == sid for item in self.media.values()):
                raise BrowserError("browser_media_busy")
            await self._subscribe(sid, pid, token)

    async def _subscribe(self, sid: str, pid: str, token: str) -> None:
        if len(self.streams) >= 6:
            raise BrowserError("browser_stream_limit")
        # Reserve before awaits to make the account quota atomic.
        view = await self.view(sid, pid)
        if len(self.streams) >= 6:
            raise BrowserError("browser_stream_limit")
        self.streams[token] = (sid, pid, view)
        try:
            await view.subscribe(token)
        except RuntimeError:
            self.streams.pop(token, None)
            if view.error_code:
                raise BrowserError(view.error_code) from None
            raise
        except BaseException:
            self.streams.pop(token, None)
            raise

    async def next_frame(self, token: str) -> BrowserFrame | None:
        entry = self.streams.get(token)
        if not entry:
            raise BrowserError("browser_page_not_found")
        sid, pid, view = entry
        if sid not in self.sessions or pid not in self.sessions[sid].pages or view.closed:
            raise BrowserError("browser_page_not_found")
        try:
            frame = await asyncio.wait_for(view.subscribers[token].get(), 2)
        except TimeoutError:
            return None  # Heartbeat; does not renew the browser idle deadline.
        if frame is None:
            raise BrowserError(view.error_code or "browser_capture_failed")
        return frame

    async def unsubscribe(self, token: str) -> None:
        entry = self.streams.pop(token, None)
        if entry:
            await entry[2].unsubscribe(token)

    async def subscribe_media(self, sid: str, pid: str, token: str) -> None:
        async with self.media_lock:
            if self.media:
                raise BrowserError("browser_media_limit")
            session = self.sessions.get(sid)
            if session is None or pid not in session.pages:
                raise BrowserError("browser_page_not_found")
            if self.capture_bridge is None:
                raise BrowserError("browser_media_unavailable")
            subscription = MediaSubscription(sid, pid, token)
            subscription.handle = PendingCaptureCleanup(self.capture_bridge, session.context)
            self.media[token] = subscription
            if not self.media_watchdog or self.media_watchdog.done():
                self.media_watchdog = asyncio.create_task(self.watch_media())
            try:
                async with asyncio.timeout(15):
                    preparation = session.preparation.get(pid)
                    if preparation:
                        await asyncio.shield(preparation)
                    for image_token, entry in list(self.streams.items()):
                        if entry[:2] == (sid, pid):
                            await self.unsubscribe(image_token)
                    view = await self.view(sid, pid)
                    await view.resize(1920, 1080, 1)
                    subscription.handle = await self.capture_bridge.start_capture(
                        session.context,
                        session.pages[pid],
                        token,
                        subscription.push,
                        subscription.fail,
                    )
                if subscription.error:
                    raise BrowserError(subscription.error)
                subscription.begin_watch()
            except BaseException:
                subscription.fail("browser_media_capture_failed")
                await self._stop_media(token)
                raise

    async def next_media(self, token: str) -> dict[str, Any] | None:
        subscription = self.media.get(token)
        if not subscription:
            raise BrowserError("browser_media_unavailable")
        item = await subscription.next()
        if item is None:
            return None
        session = self.sessions.get(subscription.sid)
        if session is None or subscription.pid not in session.pages:
            raise BrowserError("browser_page_not_found")
        source_page = session.pages[subscription.pid]
        page = await self.describe(session, subscription.pid)
        subscription.check_active()
        if (
            self.media.get(token) is not subscription
            or subscription.closed
            or subscription.error
            or time.monotonic() > min(subscription.deadline, subscription.source_deadline)
            or self.sessions.get(subscription.sid) is not session
            or session.pages.get(subscription.pid) is not source_page
        ):
            raise BrowserError(subscription.error or "browser_media_unavailable")
        if (page["width"], page["height"], page["dpr"]) != (1920, 1080, 1):
            raise BrowserError("browser_media_stream_failed")
        return {"generation": token, "seq": item[0], "data": item[1], "mime": MIME, "page": page}

    async def renew_media(self, token: str) -> None:
        subscription = self.media.get(token)
        if not subscription or subscription.sid not in self.sessions:
            raise BrowserError("browser_media_unavailable")
        subscription.renew()
        self.sessions[subscription.sid].touched = time.monotonic()

    async def _stop_media(self, token: str) -> None:
        subscription = self.media.get(token)
        if subscription:
            # Keep the quota reserved until track teardown completes.
            await subscription.close()
            self.media.pop(token, None)

    async def unsubscribe_media(self, token: str) -> None:
        async with self.media_lock:
            await self._stop_media(token)

    async def watch_media(self) -> None:
        while self.media:
            await asyncio.sleep(1)
            async with self.media_lock:
                # Startup holds this lock and establishes fresh deadlines. Check
                # only after acquiring it, not against a stale pre-start snapshot.
                for token, subscription in list(self.media.items()):
                    if (
                        subscription.error
                        or subscription.closed
                        or time.monotonic()
                        > min(subscription.deadline, subscription.source_deadline)
                    ):
                        try:
                            await self._stop_media(token)
                        except Exception:
                            # Keep quota ownership and retry teardown before another capture.
                            pass

    async def command(self, sid: str, pid: str | None, action: dict[str, Any]) -> Any:
        kind = action["action"]
        session = self.sessions.get(sid)
        if kind == "list":
            return {
                "pages": [await self.describe(session, key) for key in list(session.pages)]
                if session
                else []
            }
        if kind == "create":
            session = await self.new_context(sid)
            if len(session.pages) >= MAX_PAGES:
                raise BrowserError("browser_page_limit")
            page = await session.context.new_page()
            self.register_page(session, page)
            pid = next(key for key, value in session.pages.items() if value is page)
            if pid in session.preparation:
                await asyncio.shield(session.preparation[pid])
        if session is None or pid not in session.pages:
            raise BrowserError("browser_page_not_found")
        page = session.pages[pid]
        if kind not in {"frame", "info", "list", "resize"}:
            session.touched = time.monotonic()
            if pid in session.views:
                session.views[pid].active_until = time.monotonic() + 1
        if kind == "close":
            for token, item in list(self.media.items()):
                if (item.sid, item.pid) == (sid, pid):
                    try:
                        await self.unsubscribe_media(token)
                    except Exception:
                        pass  # Closing the target page below also ends its capture tracks.
            view = session.views.pop(pid, None)
            if view:
                await view.close()
            await page.close()
            async with self.media_lock:
                for token, item in list(self.media.items()):
                    if (item.sid, item.pid) == (sid, pid):
                        item.handle = None
                        await self._stop_media(token)
            if not session.pages:
                await self.close_session(sid)
            return None
        if kind == "frame":
            view = session.views.get(pid)
            if view:
                return await view.still()
            return await page.screenshot(type="png", scale="device", timeout=2000)
        if kind == "navigate":
            url = action["url"].strip()
            if "://" not in url:
                url = "https://" + url
            destination(url)
            started = time.monotonic()
            await page.goto(url, wait_until="commit")
            session.navigation_ms[pid] = round((time.monotonic() - started) * 1000, 1)
        elif kind in {"back", "forward", "reload"}:
            callback = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[
                kind
            ]
            started = time.monotonic()
            await callback(wait_until="commit")
            session.navigation_ms[pid] = round((time.monotonic() - started) * 1000, 1)
        elif kind == "click":
            view = session.views.get(pid)
            if action.get("view_version") is not None and action["view_version"] != (
                view.version if view else 0
            ):
                raise BrowserError("browser_frame_changed")
            if (action["width"], action["height"]) != (
                page.viewport_size["width"],
                page.viewport_size["height"],
            ):
                raise BrowserError("browser_frame_changed")
            if (
                action["x"] >= page.viewport_size["width"]
                or action["y"] >= page.viewport_size["height"]
            ):
                raise BrowserError("browser_action_failed")
            await page.mouse.click(
                action["x"], action["y"], button=action["button"], click_count=action["clicks"]
            )
        elif kind == "resize":
            async with self.media_lock:
                if any((item.sid, item.pid) == (sid, pid) for item in self.media.values()):
                    raise BrowserError("browser_media_busy")
                if "dpr" in action:
                    view = await self.view(sid, pid)
                    try:
                        await view.resize(action["width"], action["height"], action["dpr"])
                    except RuntimeError:
                        if view.error_code:
                            raise BrowserError(view.error_code) from None
                        raise
                else:
                    await page.set_viewport_size(
                        {"width": action["width"], "height": action["height"]}
                    )
        elif kind == "media_stop":
            # Automatic view handoff must not stop a newer generation (or a
            # same-page stream owned by another Web window). Missing generation
            # retains the explicit legacy stop action for older UI clients.
            expected = action.get("media_generation")
            async with self.media_lock:
                for token, item in list(self.media.items()):
                    if (item.sid, item.pid) == (sid, pid) and (
                        expected is None or expected == token
                    ):
                        await self._stop_media(token)
        elif kind == "wheel":
            await page.mouse.wheel(action["dx"], action["dy"])
        elif kind == "text":
            await page.keyboard.insert_text(action["text"])
        elif kind == "key":
            key = action["key"]
            if not re.fullmatch(
                r"(?:(?:Control|Shift|Alt)\+){0,3}(?:[a-zA-Z0-9]|Enter|Tab|Backspace|Delete|"
                r"Escape|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Space|[=+\-])",
                key,
            ):
                raise BrowserError("browser_invalid_key")
            await page.keyboard.press(key)
        elif kind == "dialog":
            dialog = session.dialogs.get(pid)
            if dialog:
                if action["accept"]:
                    await dialog.accept(action["text"])
                else:
                    await dialog.dismiss()
                session.dialogs.pop(pid, None)
        elif kind not in {"create", "info"}:
            raise BrowserError("browser_invalid_action")
        return await self.describe(session, pid)

    async def close_session(self, sid: str) -> None:
        async with self.media_lock:
            await self._close_session(sid)

    async def _close_session(self, sid: str) -> None:
        async with self.context_lock:
            session = self.sessions.get(sid)
            if session:
                for token, item in list(self.media.items()):
                    if item.sid == sid:
                        try:
                            await self._stop_media(token)
                        except Exception:
                            pass  # Closing this entire context below is the stronger fallback.
                for preparing in session.preparation.values():
                    preparing.cancel()
                await asyncio.gather(*session.preparation.values(), return_exceptions=True)
                session.preparation.clear()
                for view in list(session.views.values()):
                    await view.close()
                session.views.clear()
                await session.context.close()
                # Context closure confirms every helper and media track is gone,
                # even when the individual recorder's stop RPC failed.
                for token, item in list(self.media.items()):
                    if item.sid == sid:
                        item.handle = None
                        await self._stop_media(token)
                if self.capture_bridge:
                    try:
                        await self.capture_bridge.close_context(session.context)
                    except Exception:
                        # All pages have already been confirmed closed above.
                        self.capture_bridge.discard_closed_context(session.context)
                self.sessions.pop(sid, None)
                self.page_locks = {
                    key: lock for key, lock in self.page_locks.items() if key[0] != sid
                }
            if not self.sessions:
                await self._close()

    async def close(self) -> None:
        async with self.media_lock:
            await self._close()

    async def _close(self) -> None:
        try:
            preparations = [
                task for session in self.sessions.values() for task in session.preparation.values()
            ]
            for task in preparations:
                task.cancel()
            await asyncio.gather(*preparations, return_exceptions=True)
            for token in list(self.media):
                try:
                    await self._stop_media(token)
                except Exception:
                    pass  # The owning browser is unconditionally closed in finally.
            if self.media_watchdog:
                self.media_watchdog.cancel()
                self.media_watchdog = None
            for session in self.sessions.values():
                for view in list(session.views.values()):
                    await view.close()
                session.views.clear()
        finally:
            try:
                if self.browser:
                    await asyncio.wait_for(self.browser.close(), 10)
                    self.browser = None
                    self.sessions.clear()
                    for item in self.media.values():
                        item.handle = None
                        await item.close()
                    self.media.clear()
                if self.capture_bridge:
                    bridge = self.capture_bridge
                    if self.browser is None:
                        self.capture_bridge = None
                    try:
                        await bridge.close()
                    except Exception:
                        if self.browser is not None:
                            raise
                self.streams.clear()
                self.page_locks.clear()
            finally:
                await self.proxy.close()
                if self.playwright:
                    await self.playwright.stop()
                    self.playwright = None


class LocalBrowserRuntime:
    def __init__(self) -> None:
        self.engine: BrowserEngine | None = None
        self.engine_factory = BrowserEngine
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.deleted: set[str] = set()
        self.closed = False
        self.pending = 0

    def _ensure_thread(self) -> None:
        if self.closed:
            raise BrowserError()
        if self.thread:
            return
        ready = threading.Event()

        def run() -> None:
            factory = getattr(asyncio, "ProactorEventLoop", asyncio.new_event_loop)
            loop: asyncio.AbstractEventLoop = factory()
            asyncio.set_event_loop(loop)

            def handle_loop_exception(
                event_loop: asyncio.AbstractEventLoop,
                context: dict[str, Any],
            ) -> None:
                # A peer can reset an already-closing Windows proxy socket.
                # Suppress only this transport teardown callback, not SDK/action errors.
                exc = context.get("exception")
                message = str(context.get("message", ""))
                if (
                    isinstance(exc, ConnectionResetError)
                    and getattr(exc, "winerror", None) == 10054
                    and "_ProactorBasePipeTransport._call_connection_lost" in message
                ):
                    return
                event_loop.default_exception_handler(context)

            loop.set_exception_handler(handle_loop_exception)
            self.loop = loop
            self.engine = self.engine_factory()
            ready.set()
            maintenance = loop.create_task(self._maintain())
            try:
                loop.run_forever()
            finally:
                maintenance.cancel()
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self.thread = threading.Thread(target=run, name="workspace-browser", daemon=True)
        self.thread.start()
        if not ready.wait(5):
            raise BrowserError("browser_start_failed")

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(30)
            assert self.engine
            for sid, session in list(self.engine.sessions.items()):
                if time.monotonic() - session.touched > IDLE_SECONDS:
                    try:
                        async with self.engine.locks.setdefault(sid, asyncio.Lock()):
                            current = self.engine.sessions.get(sid)
                            if (
                                current is None
                                or time.monotonic() - current.touched <= IDLE_SECONDS
                            ):
                                continue
                            async with asyncio.timeout(15):
                                await self.engine.close_session(sid)
                    except Exception:
                        pass  # Retain ownership and retry cleanup next cycle.

    async def _submit(self, operation: Coroutine[Any, Any, T]) -> T:
        assert self.loop
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), 40)
        except TimeoutError:
            future.cancel()
            raise BrowserError("browser_timeout") from None

    async def command(self, sid: str, pid: str | None, action: dict[str, Any]) -> Any:
        if sid in self.deleted or self.closed:
            raise BrowserError("browser_session_deleted")
        if not self.thread and action["action"] == "list":
            return {"pages": []}
        if self.pending >= 32:
            raise BrowserError("browser_busy")
        self._ensure_thread()

        async def invoke() -> Any:
            assert self.engine

            async def dispatch() -> Any:
                assert self.engine
                if sid in self.deleted:
                    raise BrowserError("browser_session_deleted")
                try:
                    return await self.engine.command(sid, pid, action)
                except (BrowserError, BrowserNetworkDenied):
                    raise
                except Exception:
                    raise BrowserError("browser_action_failed") from None

            kind = action["action"]
            if kind in {"info", "list", "frame"}:
                return await dispatch()
            if pid is None or kind == "close":
                lock = self.engine.locks.setdefault(sid, asyncio.Lock())
            else:
                session = self.engine.sessions.get(sid)
                if session is None or pid not in session.pages:
                    return await dispatch()  # Invalid page IDs must not allocate lasting locks.
                lane = (
                    "navigation" if kind in {"navigate", "back", "forward", "reload"} else "input"
                )
                lock = self.engine.page_locks.setdefault((sid, pid, lane), asyncio.Lock())
            async with lock:
                return await dispatch()

        self.pending += 1
        try:
            return await self._submit(invoke())
        finally:
            self.pending -= 1

    async def subscribe(self, sid: str, pid: str) -> str:
        if sid in self.deleted or self.closed:
            raise BrowserError("browser_session_deleted")
        self._ensure_thread()
        token = uuid4().hex
        assert self.engine
        await self._submit(self.engine.subscribe(sid, pid, token))
        return token

    async def next_frame(self, token: str) -> BrowserFrame | None:
        assert self.engine
        return await self._submit(self.engine.next_frame(token))

    async def unsubscribe(self, token: str) -> None:
        if self.engine and not self.closed:
            await self._submit(self.engine.unsubscribe(token))

    async def subscribe_media(self, sid: str, pid: str) -> str:
        if sid in self.deleted or self.closed:
            raise BrowserError("browser_session_deleted")
        if self.pending >= 32:
            raise BrowserError("browser_busy")
        self._ensure_thread()
        token = uuid4().hex

        async def invoke() -> None:
            assert self.engine
            async with self.engine.locks.setdefault(sid, asyncio.Lock()):
                if sid in self.deleted or self.closed:
                    raise BrowserError("browser_session_deleted")
                await self.engine.subscribe_media(sid, pid, token)

        self.pending += 1
        try:
            await self._submit(invoke())
        finally:
            self.pending -= 1
        return token

    async def next_media(self, token: str) -> dict[str, Any] | None:
        if self.closed or self.engine is None:
            raise BrowserError("browser_media_unavailable")
        return await self._submit(self.engine.next_media(token))

    async def renew_media(self, token: str) -> None:
        if self.closed or self.engine is None:
            raise BrowserError("browser_media_unavailable")
        await self._submit(self.engine.renew_media(token))

    async def unsubscribe_media(self, token: str) -> None:
        if self.engine and not self.closed:
            await self._submit(self.engine.unsubscribe_media(token))

    async def delete_session(self, sid: str) -> None:
        self.deleted.add(sid)
        if not self.thread:
            return

        async def remove() -> None:
            assert self.engine
            async with self.engine.locks.setdefault(sid, asyncio.Lock()):
                await self.engine.close_session(sid)
                self.engine.locks.pop(sid, None)

        await self._submit(remove())

    async def close(self) -> None:
        self.closed = True
        if self.loop and self.engine and self.thread:
            try:
                await self._submit(self.engine.close())
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
                await asyncio.to_thread(self.thread.join, 5)

"""Private tab-scoped media bridge. CDP and extension handles never leave this module."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import BrowserError

EXTENSION_PATH = Path(__file__).with_name("capture_extension")
MIME = "video/webm;codecs=vp8,opus"
MAX_CHUNK_BYTES = 2 * 1024 * 1024
MAX_ENCODED_BYTES = ((MAX_CHUNK_BYTES + 2) // 3) * 4
COMMAND_SECONDS = 5.0
DELIVERY_SECONDS = 10.0
START_SECONDS = 15.0
MAX_MAPPING_TARGETS = 32
ERROR_CODES = frozenset(
    {
        "browser_media_capture_failed",
        "browser_media_codec_unsupported",
        "browser_media_stream_failed",
    }
)
ChunkCallback = Callable[[int, bytes], Awaitable[None] | None]
ErrorCallback = Callable[[str], None]


@dataclass
class _PageBinding:
    target_id: str
    outer_id: str
    context_id: str
    tab_id: int


@dataclass
class _Context:
    context: Any
    helper: Any
    helper_url: str
    binding_name: str
    pages: dict[Any, _PageBinding] = field(default_factory=dict)
    active: TabCaptureHandle | None = None
    closed: bool = False


class TabCaptureHandle:
    """One generation of WebM bytes; stopping is idempotent and restores muting."""

    mime = MIME

    def __init__(
        self,
        bridge: TabCaptureBridge,
        state: _Context,
        page: Any,
        generation: str | int,
        on_chunk: ChunkCallback,
        on_error: ErrorCallback,
    ) -> None:
        self.bridge = bridge
        self.state = state
        self.page = page
        self.generation = generation
        self.token = uuid4().hex
        self.on_chunk = on_chunk
        self.on_error = on_error
        self.width = 1920
        self.height = 1080
        self.frame_rate = 30.0
        self.next_seq = 1
        self.stopped = False
        self.failed = False
        self.delivering = False
        self._stop_task: asyncio.Task[None] | None = None

    def fail(self, code: str) -> None:
        if self.failed or self.stopped:
            return
        self.failed = True
        with suppress(Exception):
            self.on_error(code if code in ERROR_CODES else "browser_media_capture_failed")
        self.bridge._schedule(self.stop())

    async def stop(self) -> None:
        if self._stop_task is not None and self._stop_task.done():
            if self._stop_task.cancelled() or self._stop_task.exception() is not None:
                self._stop_task = None
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        self.stopped = True
        if self.state.active is not self:
            return
        try:
            stopped = await self.bridge._call(self.state, "stop", token=self.token)
            if stopped is not True:
                raise BrowserError("browser_media_capture_failed")
        except Exception:
            # Do not report successful cleanup or free the owner slot unless
            # either the stop RPC or destruction of its document is confirmed.
            self.state.closed = True
            try:
                await asyncio.wait_for(self.state.helper.close(), COMMAND_SECONDS)
            except Exception:
                if not self.state.helper.is_closed():
                    raise BrowserError("browser_media_capture_failed") from None
        self.state.active = None


class TabCaptureBridge:
    """One account browser, split extension helpers in isolated Session contexts."""

    def __init__(self, browser: Any) -> None:
        self.browser = browser
        self.root: Any = None
        self.extension_id: str | None = None
        self.contexts: dict[Any, _Context] = {}
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()

    def _schedule(self, coroutine: Awaitable[None]) -> None:
        async def run() -> None:
            with suppress(Exception):
                await coroutine

        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def start(self) -> str:
        async with self.lock:
            if self.extension_id is not None:
                return self.extension_id
            root = await self.browser.new_browser_cdp_session()
            try:
                result = await asyncio.wait_for(
                    root.send(
                        "Extensions.loadUnpacked",
                        {
                            "path": str(EXTENSION_PATH.resolve()),
                            "enableInIncognito": True,
                        },
                    ),
                    START_SECONDS,
                )
                extension_id = result.get("id")
                if not isinstance(extension_id, str) or not re.fullmatch("[a-p]{32}", extension_id):
                    raise BrowserError("browser_media_codec_unsupported")
                self.root = root
                self.extension_id = extension_id
                return extension_id
            except BaseException as error:
                with suppress(Exception):
                    await asyncio.wait_for(root.detach(), COMMAND_SECONDS)
                if isinstance(error, (asyncio.CancelledError, BrowserError)):
                    raise
                raise BrowserError("browser_media_unavailable") from None

    async def _call(self, state: _Context, command: str, **values: Any) -> Any:
        if state.helper.url != state.helper_url:
            raise BrowserError("browser_media_capture_failed")
        try:
            return await asyncio.wait_for(
                state.helper.evaluate(
                    "options => globalThis.piCapture.call(options)",
                    {"command": command, **values},
                ),
                START_SECONDS if command == "start" else COMMAND_SECONDS,
            )
        except Exception:
            raise BrowserError("browser_media_capture_failed") from None

    async def open_context(self, context: Any) -> None:
        await self.start()
        async with self.lock:
            if context in self.contexts:
                return
            helper = await context.new_page()
            state = _Context(
                context,
                helper,
                f"chrome-extension://{self.extension_id}/capture.html",
                f"__pi_media_{uuid4().hex}",
            )
            try:
                await helper.goto(state.helper_url, timeout=5000)

                async def receive(source: Any, message: Any) -> bool:
                    return await self._receive(state, source, message)

                await helper.expose_binding(state.binding_name, receive)
                result = await self._call(state, "init", binding=state.binding_name)
                if not isinstance(result, dict) or result.get("supported") is not True:
                    raise BrowserError("browser_media_codec_unsupported")
                # All business Sessions use incognito BrowserContexts, never the
                # shared default profile. This is part of the isolation contract.
                if result.get("incognito") is not True:
                    raise BrowserError("browser_media_codec_unsupported")
                self.contexts[context] = state
                helper.on("close", lambda: self._helper_lost(state))
                helper.on("crash", lambda: self._helper_lost(state))
            except BaseException as error:
                with suppress(Exception):
                    await asyncio.wait_for(helper.close(), COMMAND_SECONDS)
                if isinstance(error, (asyncio.CancelledError, BrowserError)):
                    raise
                raise BrowserError("browser_media_capture_failed") from None

    def _helper_lost(self, state: _Context) -> None:
        state.closed = True
        if state.active:
            state.active.fail("browser_media_capture_failed")

    def _state(self, context: Any) -> _Context:
        state = self.contexts.get(context)
        if state is None or state.closed:
            raise BrowserError("browser_media_codec_unsupported")
        return state

    async def _related_pages(self, outer_id: str) -> list[dict[str, Any]]:
        attached = await asyncio.wait_for(
            self.root.send(
                "Target.attachToTarget",
                {
                    "targetId": outer_id,
                    "flatten": False,
                },
            ),
            COMMAND_SECONDS,
        )
        session_id = attached["sessionId"]
        response: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pages: list[dict[str, Any]] = []

        def received(event: dict[str, Any]) -> None:
            if event.get("sessionId") != session_id:
                return
            try:
                message = json.loads(event["message"])
                if message.get("method") == "Target.attachedToTarget":
                    target = message["params"]["targetInfo"]
                    if target.get("type") == "page":
                        pages.append(target)
                if message.get("id") == 1 and not response.done():
                    response.set_result(message)
            except (KeyError, TypeError, ValueError):
                if not response.done():
                    response.set_result({"error": True})

        self.root.on("Target.receivedMessageFromTarget", received)
        try:
            await asyncio.wait_for(
                self.root.send(
                    "Target.sendMessageToTarget",
                    {
                        "sessionId": session_id,
                        "message": json.dumps(
                            {
                                "id": 1,
                                "method": "Target.setAutoAttach",
                                "params": {
                                    "autoAttach": True,
                                    "waitForDebuggerOnStart": False,
                                    "flatten": False,
                                    "filter": [{"type": "page"}],
                                },
                            }
                        ),
                    },
                ),
                COMMAND_SECONDS,
            )
            reply = await asyncio.wait_for(response, COMMAND_SECONDS)
            if "error" in reply:
                raise BrowserError("browser_media_capture_failed")
            return pages
        finally:
            self.root.remove_listener("Target.receivedMessageFromTarget", received)
            with suppress(Exception):
                await asyncio.wait_for(
                    self.root.send(
                        "Target.detachFromTarget",
                        {
                            "sessionId": session_id,
                        },
                    ),
                    COMMAND_SECONDS,
                )

    async def _identity(self, context: Any, page: Any) -> dict[str, Any]:
        if page.context is not context or page.is_closed():
            raise BrowserError("browser_media_capture_failed")
        cdp = await context.new_cdp_session(page)
        try:
            result = await asyncio.wait_for(cdp.send("Target.getTargetInfo"), COMMAND_SECONDS)
            info: dict[str, Any] = result["targetInfo"]
            if info.get("type") != "page" or not info.get("browserContextId"):
                raise BrowserError("browser_media_capture_failed")
            return info
        finally:
            with suppress(Exception):
                await asyncio.wait_for(cdp.detach(), COMMAND_SECONDS)

    async def _click(self, state: _Context, page: Any, outer_id: str, expected: int | None) -> int:
        token = uuid4().hex
        armed = await self._call(state, "arm", token=token, expected=expected)
        if not isinstance(armed, dict) or armed.get("ok") is not True:
            raise BrowserError("browser_media_capture_failed")
        try:
            await page.bring_to_front()
            await asyncio.wait_for(
                self.root.send(
                    "Extensions.triggerAction",
                    {
                        "id": self.extension_id,
                        "targetId": outer_id,
                    },
                ),
                COMMAND_SECONDS,
            )
            async with asyncio.timeout(COMMAND_SECONDS):
                while True:
                    result = await self._call(state, "take", token=token)
                    if isinstance(result, dict):
                        tab_id = result.get("tabId")
                        if (
                            result.get("token") != token
                            or type(tab_id) is not int
                            or tab_id < 0
                            or expected is not None
                            and tab_id != expected
                        ):
                            raise BrowserError("browser_media_capture_failed")
                        return tab_id
                    await asyncio.sleep(0.025)
        finally:
            with suppress(Exception):
                await self._call(state, "cancel", token=token)

    async def bind_page(self, context: Any, page: Any) -> int:
        try:
            async with asyncio.timeout(START_SECONDS):
                return await self._bind_page(context, page)
        except BrowserError:
            raise
        except Exception:
            raise BrowserError("browser_media_capture_failed") from None

    async def _bind_page(self, context: Any, page: Any) -> int:
        async with self.lock:
            state = self._state(context)
            if page in state.pages:
                return state.pages[page].tab_id
            if page is state.helper or page.url not in ("", "about:blank"):
                raise BrowserError("browser_media_capture_failed")
            info = await self._identity(context, page)
            targets = await asyncio.wait_for(
                self.root.send(
                    "Target.getTargets",
                    {
                        "filter": [{"type": "tab"}],
                    },
                ),
                COMMAND_SECONDS,
            )
            candidates = [
                item
                for item in targets["targetInfos"]
                if item.get("browserContextId") == info["browserContextId"]
            ]
            if len(candidates) > MAX_MAPPING_TARGETS:
                raise BrowserError("browser_media_capture_failed")
            matches = []
            for outer in candidates:
                children = await self._related_pages(outer["targetId"])
                if any(child["targetId"] == info["targetId"] for child in children):
                    matches.append(outer["targetId"])
            if len(matches) != 1 or page.url not in ("", "about:blank"):
                raise BrowserError("browser_media_capture_failed")
            tab_id = await self._click(state, page, matches[0], None)
            state.pages[page] = _PageBinding(
                info["targetId"],
                matches[0],
                info["browserContextId"],
                tab_id,
            )
            page.on("close", lambda: self._forget_page(state, page))
            return tab_id

    def _forget_page(self, state: _Context, page: Any) -> None:
        state.pages.pop(page, None)
        if state.active and state.active.page is page:
            state.active.fail("browser_media_capture_failed")

    async def start_capture(
        self,
        context: Any,
        page: Any,
        generation: str | int,
        on_chunk: ChunkCallback,
        on_error: ErrorCallback,
    ) -> TabCaptureHandle:
        try:
            async with asyncio.timeout(START_SECONDS + COMMAND_SECONDS):
                return await self._start_capture(context, page, generation, on_chunk, on_error)
        except BrowserError:
            raise
        except Exception:
            raise BrowserError("browser_media_capture_failed") from None

    async def _start_capture(
        self,
        context: Any,
        page: Any,
        generation: str | int,
        on_chunk: ChunkCallback,
        on_error: ErrorCallback,
    ) -> TabCaptureHandle:
        if type(generation) not in (str, int) or len(str(generation)) > 128:
            raise BrowserError("browser_media_capture_failed")
        async with self.lock:
            state = self._state(context)
            binding = state.pages.get(page)
            if binding is None:
                raise BrowserError("browser_media_capture_failed")
            if state.active is not None:
                raise BrowserError("browser_media_busy")
            info = await self._identity(context, page)
            if (
                info["targetId"] != binding.target_id
                or info["browserContextId"] != binding.context_id
            ):
                raise BrowserError("browser_media_capture_failed")
            await self._click(state, page, binding.outer_id, binding.tab_id)
            handle = TabCaptureHandle(self, state, page, generation, on_chunk, on_error)
            state.active = handle
            try:
                result = await self._call(
                    state, "start", token=handle.token, generation=generation, tabId=binding.tab_id
                )
                if (
                    not isinstance(result, dict)
                    or result.get("mime") != MIME
                    or result.get("audioSafe") is not True
                ):
                    raise BrowserError("browser_media_capture_failed")
                width, height = result.get("width"), result.get("height")
                frame_rate = result.get("frameRate")
                if (
                    type(width) is not int
                    or width != 1920
                    or type(height) is not int
                    or height != 1080
                    or not isinstance(frame_rate, (int, float))
                    or isinstance(frame_rate, bool)
                    or not 0 < frame_rate <= 30
                ):
                    raise BrowserError("browser_media_codec_unsupported")
                handle.width, handle.height, handle.frame_rate = width, height, float(frame_rate)
                if handle.failed or handle.stopped:
                    raise BrowserError("browser_media_capture_failed")
                return handle
            except BaseException:
                await handle.stop()
                raise

    async def _receive(self, state: _Context, source: Any, message: Any) -> bool:
        handle = state.active
        helper_frame = state.helper.main_frame
        helper_url = state.helper.url
        if (
            state.closed
            or self.contexts.get(state.context) is not state
            or handle is None
            or handle.stopped
            or handle.failed
            or not isinstance(source, dict)
            or source.get("page") is not state.helper
            or source.get("frame") is not helper_frame
            or helper_url != state.helper_url
            or not isinstance(message, dict)
            or message.get("token") != handle.token
            or type(message.get("generation")) is not type(handle.generation)
            or message.get("generation") != handle.generation
        ):
            return False
        if message.get("kind") == "error":
            code = message.get("code")
            handle.fail(code if isinstance(code, str) else "browser_media_capture_failed")
            return False
        seq, encoded = message.get("seq"), message.get("data")
        if (
            message.get("kind") != "chunk"
            or type(seq) is not int
            or seq != handle.next_seq
            or not isinstance(encoded, str)
            or not 0 < len(encoded) <= MAX_ENCODED_BYTES
            or handle.delivering
        ):
            handle.fail("browser_media_stream_failed")
            return False
        try:
            data = base64.b64decode(encoded, validate=True)
            if not 0 < len(data) <= MAX_CHUNK_BYTES:
                raise ValueError
        except ValueError:
            handle.fail("browser_media_stream_failed")
            return False
        handle.delivering = True
        try:
            async with asyncio.timeout(DELIVERY_SECONDS):
                result = handle.on_chunk(seq, data)
                if inspect.isawaitable(result):
                    await result
            handle.next_seq += 1
            return not handle.stopped and not handle.failed
        except asyncio.CancelledError:
            handle.fail("browser_media_stream_failed")
            raise
        except Exception:
            handle.fail("browser_media_stream_failed")
            return False
        finally:
            handle.delivering = False

    async def stop_capture(self, context: Any) -> None:
        """Reclaim a pending start whose handle was not yet returned to its owner."""
        state = self.contexts.get(context)
        if state is not None and state.active is not None:
            await state.active.stop()

    def discard_closed_context(self, context: Any) -> None:
        """Owner-only fallback after BrowserContext.close has confirmed track teardown."""
        state = self.contexts.pop(context, None)
        if state:
            state.closed = True
            state.pages.clear()
            if state.active:
                state.active.stopped = True
                state.active = None

    async def close_context(self, context: Any) -> None:
        state = self.contexts.get(context)
        if state is None:
            return
        state.closed = True
        if state.active:
            await state.active.stop()
        try:
            await asyncio.wait_for(state.helper.close(), COMMAND_SECONDS)
        except Exception:
            if not state.helper.is_closed():
                raise BrowserError("browser_media_capture_failed") from None
        state.pages.clear()
        self.contexts.pop(context, None)

    async def close(self) -> None:
        for context in list(self.contexts):
            await self.close_context(context)
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.root is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.root.detach(), COMMAND_SECONDS)
        self.root = None
        self.extension_id = None

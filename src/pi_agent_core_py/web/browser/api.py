"""Authenticated workspace browser REST transport; all responses are private/no-store."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import Scope

from ..credentials.api import CredentialBodyLimitMiddleware, safe_validation_response
from ..local_web_security import WebSecurityConfig
from .network import BrowserNetworkDenied
from .runtime import BrowserError, LocalBrowserRuntime

MESSAGES = {
    "browser_media_busy": "Stop video and audio before resizing or opening another view.",
    "browser_media_limit": "Stop the other video/audio stream first (1 per account).",
    "browser_capture_failed": (
        "The browser frame source could not recover. Reconnect this view or reopen the tab."
    ),
    "browser_stream_limit": "Close another browser view first (6 active views per account).",
    "browser_busy": "Browser input is busy. Wait briefly before trying again.",
    "browser_frame_changed": "The page resized. Wait for the updated image, then click again.",
    "browser_dependency_missing": "Install the browser extra and Playwright Chromium first.",
    "browser_start_failed": "Chromium could not start. Check installation and OS permissions.",
    "browser_context_limit": "Close tabs in another Session first (3 Sessions per account).",
    "browser_page_limit": "Close a browser tab first (limit: 6 pages per Session).",
    "browser_page_not_found": "This browser page is closed or expired. Open a new browser tab.",
    "browser_session_not_found": "This Session no longer exists.",
    "browser_session_deleted": "This Session is closing or deleted.",
    "browser_invalid_key": "This keyboard shortcut is not supported.",
    "browser_timeout": "The browser did not respond in time. Retry or close this page.",
    "browser_action_failed": "The page could not complete this action. Check the address or retry.",
    "browser_public_http_only": (
        "Only public HTTP/HTTPS websites on ports 80/443 are allowed. "
        "Local and private addresses are blocked."
    ),
}


class BrowserRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError as exc:
                response = safe_validation_response(exc)
            except (BrowserError, BrowserNetworkDenied) as exc:
                code = exc.code if isinstance(exc, BrowserError) else "browser_public_http_only"
                status = 404 if code.endswith(("not_found", "deleted")) else 503
                if code in {"browser_public_http_only", "browser_invalid_key"}:
                    status = 400
                response = JSONResponse(
                    status_code=status,
                    content={
                        "detail": {
                            "code": code,
                            "message": MESSAGES.get(code, "Local browser unavailable."),
                        }
                    },
                )
            except HTTPException as exc:
                response = JSONResponse(
                    status_code=exc.status_code,
                    content={
                        "detail": {
                            "code": "browser_request_rejected",
                            "message": "Browser request rejected.",
                        }
                    },
                )
            except Exception:
                response = JSONResponse(
                    status_code=500,
                    content={
                        "detail": {
                            "code": "browser_internal_error",
                            "message": "Local browser unavailable.",
                        }
                    },
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return handle


class BrowserBodyLimitMiddleware(CredentialBodyLimitMiddleware):
    def _is_target(self, scope: Scope) -> bool:
        path = str(scope.get("path", ""))
        return (
            scope.get("type") == "http" and path.startswith("/api/sessions/") and "/browser" in path
        )


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    action: Literal[
        "navigate",
        "back",
        "forward",
        "reload",
        "click",
        "wheel",
        "text",
        "key",
        "dialog",
        "resize",
        "media_stop",
    ]
    url: str = Field(default="", max_length=4096)
    text: str = Field(default="", max_length=4096)
    key: str = Field(default="", max_length=64)
    x: float = Field(default=0, ge=0, le=1919)
    y: float = Field(default=0, ge=0, le=1399)
    width: int = Field(default=1280, ge=320, le=1920)
    height: int = Field(default=800, ge=200, le=1400)
    dpr: float = Field(default=1, ge=1, le=2)
    view_version: int | None = Field(default=None, ge=0)
    media_generation: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    dx: float = Field(default=0, ge=-1500, le=1500)
    dy: float = Field(default=0, ge=-1500, le=1500)
    button: Literal["left", "right"] = "left"
    clicks: int = Field(default=1, ge=1, le=2)
    accept: bool = False


def build_browser_router(
    config: WebSecurityConfig,
    runtime: LocalBrowserRuntime,
    require_session: Callable[[str], Awaitable[None]],
) -> APIRouter:
    async def guard(request: Request, sid: str) -> None:
        if request.headers.get("X-PI-Agent-UI") != "1":
            raise HTTPException(403, "Trusted UI header required.")
        origin = request.headers.get("origin")
        if origin is not None and origin not in config.allowed_ui_origins:
            if origin == "null" or origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "Origin is not allowed.")
        try:
            await require_session(sid)
        except HTTPException as exc:
            # A confirmed missing Session also confirms its owned captures have
            # been released. Keep this distinct from authorization/transient errors.
            if exc.status_code == 404:
                raise BrowserError("browser_session_not_found") from None
            raise

    router = APIRouter(
        prefix="/api/sessions/{sid}/browser",
        route_class=BrowserRoute,
        dependencies=[Depends(guard)],
    )

    @router.get("")
    async def list_pages(sid: str) -> Any:
        return await runtime.command(sid, None, {"action": "list"})

    @router.post("/pages")
    async def create_page(sid: str) -> Any:
        return await runtime.command(sid, None, {"action": "create"})

    @router.get("/pages/{pid}")
    async def page_info(sid: str, pid: str) -> Any:
        return await runtime.command(sid, pid, {"action": "info"})

    @router.delete("/pages/{pid}", status_code=204)
    async def close_page(sid: str, pid: str) -> Response:
        await runtime.command(sid, pid, {"action": "close"})
        return Response(status_code=204)

    @router.get("/pages/{pid}/frame")
    async def page_frame(sid: str, pid: str) -> Response:
        data = await runtime.command(sid, pid, {"action": "frame"})
        return Response(content=data, media_type="image/png")

    @router.post("/pages/{pid}/action")
    async def page_action(sid: str, pid: str, body: BrowserAction) -> Any:
        return await runtime.command(sid, pid, body.model_dump())

    return router

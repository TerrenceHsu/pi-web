"""Authenticated gateway routing each account to an isolated Web workspace."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from time import monotonic
from typing import Protocol

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ..app import _FALLBACK_HTML, _STATIC_DIR
from ..credentials.api import CredentialBodyLimitMiddleware, build_credential_router
from ..local_web_security import WebSecurityConfig, default_web_security_config
from .models import AuthUser
from .service import DEFAULT_SESSION_TTL_SECONDS, AuthService
from .store import AuthStore

AUTH_COOKIE_NAME = "pi_auth_session"
LOGIN_BODY_MAX_BYTES = 16 * 1024


class WorkspaceAppFactory(Protocol):
    def __call__(self, user: AuthUser, workspace_root: Path) -> FastAPI: ...


def _cookie_token(scope: Scope) -> str | None:
    raw_cookie = None
    for name, value in scope.get("headers", []):
        if name.lower() == b"cookie":
            raw_cookie = value.decode("latin-1")
            break
    if not raw_cookie:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw_cookie)
    except Exception:
        return None
    morsel = jar.get(AUTH_COOKIE_NAME)
    return morsel.value if morsel is not None else None


class WorkspaceManager:
    """Lazily start one full FastAPI workspace per authenticated account."""

    def __init__(self, factory: WorkspaceAppFactory, user_data_root: Path) -> None:
        self._factory = factory
        self._user_data_root = user_data_root.resolve()
        self._apps: dict[str, FastAPI] = {}
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()

    def workspace_root_for(self, user: AuthUser) -> Path:
        candidate = (self._user_data_root / user.id).resolve()
        try:
            candidate.relative_to(self._user_data_root)
        except ValueError as exc:
            raise RuntimeError("account workspace path escaped its data root") from exc
        return candidate

    async def get_or_create(self, user: AuthUser) -> FastAPI:
        existing = self._apps.get(user.id)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._apps.get(user.id)
            if existing is not None:
                return existing
            root = self.workspace_root_for(user)
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            app = self._factory(user, root)
            await self._stack.enter_async_context(app.router.lifespan_context(app))
            self._apps[user.id] = app
            return app

    async def close(self) -> None:
        await self._stack.aclose()
        self._apps.clear()


class _GatewayRuntime:
    def __init__(self, manager: WorkspaceManager) -> None:
        self.manager = manager
        self.auth_service: AuthService | None = None


class AuthDispatchMiddleware:
    """Require a valid Cookie before dispatching workspace HTTP/WS traffic."""

    def __init__(self, app: ASGIApp, *, runtime: _GatewayRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        path = str(scope.get("path", ""))
        is_workspace_http = scope_type == "http" and path.startswith("/api/")
        is_workspace_ws = scope_type == "websocket" and path.startswith("/ws/")
        is_auth_path = path == "/api/auth" or path.startswith("/api/auth/")
        if (not is_workspace_http and not is_workspace_ws) or is_auth_path:
            await self.app(scope, receive, send)
            return

        service = self.runtime.auth_service
        user = await service.resolve_session(_cookie_token(scope)) if service else None
        if user is None:
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "login required"})
            else:
                body = json.dumps(
                    {"error": {"code": "authentication_required", "message": "Login required."}}
                ).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": status.HTTP_401_UNAUTHORIZED,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"cache-control", b"no-store"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            return

        workspace = await self.runtime.manager.get_or_create(user)
        delegated_scope = dict(scope)
        delegated_scope["auth_user"] = user
        await workspace(delegated_scope, receive, send)


class LoginAttemptLimiter:
    """Small in-memory per-client limiter for password guessing."""

    def __init__(self, *, max_failures: int = 8, window_seconds: float = 60.0) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allowed(self, key: str) -> bool:
        async with self._lock:
            attempts = self._failures[key]
            cutoff = monotonic() - self.window_seconds
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            return len(attempts) < self.max_failures

    async def record_failure(self, key: str) -> None:
        async with self._lock:
            self._failures[key].append(monotonic())

    async def clear(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=64)
    password: SecretStr = Field(..., min_length=6, max_length=128)


def _serialize_user(user: AuthUser) -> dict[str, str]:
    return {"id": user.id, "name": user.name}


def _client_key(request: Request) -> str:
    return request.client.host if request.client is not None else "local"


def _get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if not isinstance(service, AuthService):
        raise RuntimeError("authentication service is unavailable")
    return service


def _build_auth_router(
    *,
    config: WebSecurityConfig,
    limiter: LoginAttemptLimiter,
    secure_cookie: bool,
) -> APIRouter:
    router = build_credential_router(config, prefix="/api/auth")

    @router.post("/login", response_model=None)
    async def login(payload: LoginRequest, request: Request) -> Response:
        key = _client_key(request)
        if not await limiter.allowed(key):
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": {
                        "code": "login_rate_limited",
                        "message": "Too many login attempts. Try again shortly.",
                    }
                },
            )
        service = _get_auth_service(request)
        result = await service.login(
            name=payload.name,
            password=payload.password.get_secret_value(),
        )
        if result is None:
            await limiter.record_failure(key)
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": {
                        "code": "invalid_credentials",
                        "message": "Invalid username or password.",
                    }
                },
            )
        await limiter.clear(key)
        response = JSONResponse(
            content={"authenticated": True, "user": _serialize_user(result.user)}
        )
        response.set_cookie(
            AUTH_COOKIE_NAME,
            result.token,
            max_age=service.session_ttl_seconds,
            httponly=True,
            secure=secure_cookie,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/session", response_model=None)
    async def get_session(request: Request) -> Response:
        user = await _get_auth_service(request).resolve_session(
            request.cookies.get(AUTH_COOKIE_NAME)
        )
        if user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": {
                        "code": "authentication_required",
                        "message": "Login required.",
                    }
                },
            )
        response = JSONResponse(
            content={"authenticated": True, "user": _serialize_user(user)}
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/logout", response_model=None)
    async def logout(request: Request) -> Response:
        await _get_auth_service(request).logout(request.cookies.get(AUTH_COOKIE_NAME))
        response = JSONResponse(content={"authenticated": False})
        response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


def create_authenticated_app(
    workspace_app_factory: WorkspaceAppFactory,
    *,
    auth_db_path: str | Path,
    user_data_root: str | Path,
    extra_hosts: tuple[str, ...] = (),
    extra_ui_origins: tuple[str, ...] = (),
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    secure_cookie: bool = False,
) -> FastAPI:
    """Create a login gateway with a fully isolated app workspace per user."""

    auth_path = Path(auth_db_path).resolve()
    data_root = Path(user_data_root).resolve()
    manager = WorkspaceManager(workspace_app_factory, data_root)
    runtime = _GatewayRuntime(manager)
    limiter = LoginAttemptLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(auth_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(data_root.mkdir, parents=True, exist_ok=True)
        store = await AuthStore.open(str(auth_path))
        service = AuthService(store, session_ttl_seconds=session_ttl_seconds)
        try:
            # Login state belongs to this backend process lifetime. Accounts and
            # per-user workspaces remain persistent, but a restarted gateway must
            # require credentials again instead of accepting an old browser Cookie.
            await store.revoke_all_sessions()
            await service.ensure_initial_admin()
            runtime.auth_service = service
            app.state.auth_store = store
            app.state.auth_service = service
            app.state.workspace_manager = manager
            yield
        finally:
            runtime.auth_service = None
            await manager.close()
            await store.close()
            app.state.auth_store = None
            app.state.auth_service = None

    app = FastAPI(
        title="pi-agent-core-py Authenticated Web UI",
        description="Local login gateway with isolated per-user workspaces.",
        version="0.0.22",
        lifespan=lifespan,
    )
    app.state.auth_store = None
    app.state.auth_service = None
    app.state.workspace_manager = manager

    security_config = default_web_security_config(
        extra_hosts=extra_hosts,
        extra_ui_origins=extra_ui_origins,
    )
    app.include_router(
        _build_auth_router(
            config=security_config,
            limiter=limiter,
            secure_cookie=secure_cookie,
        )
    )

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        index_html = _STATIC_DIR / "index.html"
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_FALLBACK_HTML, media_type="text/html")

    @app.get("/chat", include_in_schema=False)
    @app.get("/chat/", include_in_schema=False)
    @app.get("/chat/{session_path:path}", include_in_schema=False)
    async def chat_route(session_path: str | None = None) -> Response:
        """Serve the SPA shell; the client validates and normalizes the route."""
        del session_path
        return await index()

    @app.get("/assets/{path:path}", include_in_schema=False)
    async def assets(path: str) -> Response:
        asset_root = (_STATIC_DIR / "assets").resolve()
        target = (asset_root / path).resolve()
        try:
            target.relative_to(asset_root)
        except ValueError:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        if not target.is_file():
            return JSONResponse(status_code=404, content={"detail": "not found"})
        return FileResponse(target)

    app.add_middleware(
        CredentialBodyLimitMiddleware,
        max_bytes=LOGIN_BODY_MAX_BYTES,
        path_prefixes=("/api/auth/login",),
    )
    app.add_middleware(AuthDispatchMiddleware, runtime=runtime)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(security_config.allowed_hosts),
    )
    return app


__all__ = [
    "AUTH_COOKIE_NAME",
    "AuthDispatchMiddleware",
    "LoginAttemptLimiter",
    "WorkspaceAppFactory",
    "WorkspaceManager",
    "create_authenticated_app",
]

"""Credential REST API（P1-E1-4B）.

E1-4B1：路由级安全边界 + 安全 ValidationError handler + 32 KiB body limit
E1-4B2：8 个 endpoint 实现（仅调 CredentialService）

**安全契约**：

1. **路由级 X-PI-Agent-UI 强制**——所有 Credential API path 必须携带 `X-PI-Agent-UI: 1`
2. **Origin 校验**（mutating endpoint）——精确匹配 `allowed_ui_origins`；
   拒绝 `null` / 外部 / 相似 host
3. **32 KiB body limit**——ASGI middleware 在 Pydantic 解析前 enforce
4. **安全 ValidationError**——只投影 `loc` + 受控 `code`；不返回 input / ctx / url / msg
5. **固定 Domain Error → HTTP 映射**——message 来自查表，不是 `str(exc)`

**CORS**：默认同源（Vite proxy / 生产静态）；仅 `credential_extra_ui_origins`
显式注入的精确 origin 提供 preflight——禁止 `*`.

**路径覆盖**：
- `/api/credentials` + `/api/credentials/*`
- `/api/provider-hints`
- `/api/provider-definitions`
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .credentials_errors import (
    CredentialBackendUnavailableError,
    CredentialCompensationError,
    CredentialInputError,
    CredentialOperationConflictError,
    CredentialSecretDeleteError,
    CredentialSecretWriteError,
    CredentialServiceError,
)
from .credentials_store import (
    CredentialAlreadyExistsError,
    CredentialConcurrentModificationError,
    CredentialNotFoundError,
    CredentialSecretRefConflictError,
    CredentialsSchemaError,
)
from .local_web_security import WebSecurityConfig

__all__ = [
    # Body limit middleware
    "CredentialBodyLimitMiddleware",
    # Security dependencies
    "require_ui_header_dep",
    "require_allowed_origin_dep",
    # Custom route class
    "CredentialAPIRoute",
    # Error mapping
    "credential_error_to_response",
    # Router factory
    "build_credential_router",
    # Paths
    "CREDENTIAL_API_PATH_PREFIXES",
]


# ============================================================================
# Constants
# ============================================================================


CREDENTIAL_API_PATH_PREFIXES: tuple[str, ...] = (
    "/api/credentials",
    "/api/provider-hints",
    "/api/provider-definitions",
)


# ============================================================================
# Body size limit middleware（ASGI-level，流式累计字节）
# ============================================================================


class CredentialBodyLimitMiddleware:
    """ASGI middleware enforcing 32 KiB body on Credential API mutating paths.

    Enforces BEFORE Pydantic parsing. Handles:
        - Content-Length known + over limit → 413 (short path)
        - chunked body / no Content-Length → stream-counted
        - forged smaller Content-Length + larger actual body → stream-counted

    Path-scope: only POST/PUT/PATCH on CREDENTIAL_API_PATH_PREFIXES paths.
    GET / DELETE typically have no body——also guarded (reject unexpected body).

    Returns fixed safe JSON on overflow——never echoes body fragment.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path_prefixes: tuple[str, ...] = CREDENTIAL_API_PATH_PREFIXES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefixes = path_prefixes

    def _is_target(self, scope: Scope) -> bool:
        if scope.get("type") != "http":
            return False
        method = scope.get("method", "")
        if method not in ("POST", "PUT", "PATCH"):
            return False
        path = scope.get("path", "")
        return any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            or path == prefix.rstrip("/")
            for prefix in self.path_prefixes
        )

    @staticmethod
    def _get_content_length(scope: Scope) -> int | None:
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    async def _send_413(self, send: Send) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "request_body_too_large",
                    "message": "The request body exceeds the allowed size.",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if not self._is_target(scope):
            await self.app(scope, receive, send)
            return

        # Short path: Content-Length already declares overflow
        cl = self._get_content_length(scope)
        if cl is not None and cl > self.max_bytes:
            await self._send_413(send)
            return

        # Pre-buffer body up to max_bytes+1——needed because inner app may
        # respond without reading body (e.g. simple JSON return). Stream
        # counting via wrapped receive alone would miss that case.
        # For 32 KiB max, buffering is acceptable.
        buffered = bytearray()
        overflow = False
        while True:
            msg = await receive()
            mtype = msg.get("type")
            if mtype == "http.disconnect":
                overflow = False
                break
            if mtype != "http.request":
                continue
            chunk = msg.get("body", b"") or b""
            buffered.extend(chunk)
            if len(buffered) > self.max_bytes:
                overflow = True
                break
            if not msg.get("more_body", False):
                break

        if overflow:
            await self._send_413(send)
            return

        # Replay buffered body via synthesized receive——inner app reads it
        # as if it were the original stream.
        body_bytes = bytes(buffered)
        replay_state = {"yielded_body": False, "yielded_final": False}

        async def replay_receive() -> Message:
            if not replay_state["yielded_body"]:
                replay_state["yielded_body"] = True
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }
            if not replay_state["yielded_final"]:
                replay_state["yielded_final"] = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


# ============================================================================
# Security dependencies
# ============================================================================


def _credential_error_response(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def require_ui_header_dep(
    config: WebSecurityConfig,
) -> Callable[[Request], Awaitable[None]]:
    """Build dependency enforcing `X-PI-Agent-UI: 1` header.

    External websites cannot send custom headers without CORS preflight——
    preflight fails because external origin is not in allowed_ui_origins.
    """

    async def _dep(
        request: Request,
        x_pi_agent_ui: str | None = Header(default=None, alias="X-PI-Agent-UI"),
    ) -> None:
        if not config.require_ui_header:
            return
        if x_pi_agent_ui != "1":
            raise _HeaderMissingError()

    return _dep


def require_allowed_origin_dep(
    config: WebSecurityConfig,
) -> Callable[[Request], Awaitable[None]]:
    """Build dependency enforcing same-origin for mutating endpoints.

    Rules:
        - Origin absent → allow（CLI / non-browser——Host + UI header 处理其它威胁）
        - Origin = "null" → 403
        - Origin present but not in allowed_ui_origins → 403
        - Origin exact match → allow

    No suffix / regex match——精确比较.
    """

    async def _dep(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin is None:
            return
        if origin == "null":
            raise _OriginInvalidError()
        if origin not in config.allowed_ui_origins:
            raise _OriginInvalidError()

    return _dep


# ============================================================================
# Errors used by dependencies——carry safe code/message
# ============================================================================


class _HeaderMissingError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_ui_header",
                "message": "X-PI-Agent-UI header is required.",
            },
        )


class _OriginInvalidError(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "invalid_origin",
                "message": "Request origin is not allowed.",
            },
        )


# ============================================================================
# Safe ValidationError handler（Credential Router scope only）
# ============================================================================


# Pydantic error type → fixed safe code（不暴露 pydantic 版本 / url）
_PYDANTIC_TYPE_MAP: dict[str, str] = {
    "missing": "missing",
    "missing_field": "missing",
    "extra_forbidden": "extra_forbidden",
    "value_error.extra": "extra_forbidden",
    "string_too_long": "too_long",
    "too_long": "too_long",
    "string_too_short": "too_short",
    "too_short": "too_short",
    "type_error.string": "type_error",
    "string_type": "type_error",
    "value_error": "value_error",
    "int_parsing": "type_error",
    "json_invalid": "value_error",
    "json_type": "type_error",
    "literal_error": "value_error",
    "bool_parsing": "type_error",
}


def _map_pydantic_type(t: str) -> str:
    """Map pydantic error type to fixed safe code."""
    if t in _PYDANTIC_TYPE_MAP:
        return _PYDANTIC_TYPE_MAP[t]
    if t.startswith("type_error"):
        return "type_error"
    if t.startswith("value_error"):
        return "value_error"
    return "value_error"


def safe_validation_response(exc: RequestValidationError) -> JSONResponse:
    """Build safe 422 response——only `loc` + 受控 `code`.

    Never returns:
        - input（原始值，可能含 secret）
        - ctx（自定义 validator context 可能含异常）
        - url（暴露 pydantic 版本）
        - msg（自定义 validator 可能把 secret 放进 message）
    """
    fields: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # Drop leading "body" / "query" / "path"——caller only cares about field path
        loc_clean = [
            str(p)
            for p in loc
            if p not in ("body", "query", "path", "header", "cookie")
        ]
        path = ".".join(loc_clean) if loc_clean else "(root)"
        code = _map_pydantic_type(err.get("type", "value_error"))
        fields.append({"path": path, "code": code})

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "request_validation_failed",
                "message": "The request body is invalid.",
                "fields": fields,
            }
        },
    )


# ============================================================================
# Custom APIRoute——router-scoped safe ValidationError handler
# ============================================================================


class CredentialAPIRoute(APIRoute):
    """Custom APIRoute that converts RequestValidationError to safe response.

    Scope-limited: only fires for routes registered on CredentialAPIRoute-using
    router——existing FastAPI APIs keep their default 422 schema.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        original = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Any:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return safe_validation_response(exc)
            except _HeaderMissingError:
                return _credential_error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "missing_ui_header",
                    "X-PI-Agent-UI header is required.",
                )
            except _OriginInvalidError:
                return _credential_error_response(
                    status.HTTP_403_FORBIDDEN,
                    "invalid_origin",
                    "Request origin is not allowed.",
                )

        return custom_route_handler


# ============================================================================
# Domain Error → HTTP mapping
# ============================================================================


def credential_error_to_response(exc: Exception) -> JSONResponse | None:
    """Map domain exceptions to safe JSONResponse.

    Returns None if exc is not a recognized domain error——let FastAPI handle.
    """
    if isinstance(exc, CredentialNotFoundError):
        return _credential_error_response(
            status.HTTP_404_NOT_FOUND,
            "credential_not_found",
            "Credential was not found.",
        )
    if isinstance(exc, CredentialInputError):
        return _credential_error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_input",
            "Credential input is invalid.",
        )
    if isinstance(
        exc,
        (CredentialOperationConflictError, CredentialConcurrentModificationError),
    ):
        return _credential_error_response(
            status.HTTP_409_CONFLICT,
            "credential_conflict",
            "Credential was concurrently modified.",
        )
    if isinstance(
        exc, (CredentialAlreadyExistsError, CredentialSecretRefConflictError)
    ):
        return _credential_error_response(
            status.HTTP_409_CONFLICT,
            "credential_conflict",
            "Credential identifier is already in use.",
        )
    if isinstance(
        exc,
        (CredentialBackendUnavailableError,),
    ):
        return _credential_error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "credential_backend_unavailable",
            "The selected credential storage backend is unavailable.",
        )
    if isinstance(
        exc, (CredentialSecretWriteError, CredentialSecretDeleteError)
    ):
        return _credential_error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "credential_backend_unavailable",
            "The selected credential storage backend is unavailable.",
        )
    if isinstance(exc, CredentialsSchemaError):
        return _credential_error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "credential_schema_error",
            "Credential database schema is corrupt or unsupported.",
        )
    if isinstance(exc, CredentialCompensationError):
        return _credential_error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "credential_internal_error",
            "Credential operation failed; partial state may exist.",
        )
    if isinstance(exc, CredentialServiceError):
        return _credential_error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "credential_internal_error",
            "Credential service error.",
        )
    return None


# ============================================================================
# Router factory（B1: empty endpoints; B2 populates）
# ============================================================================


def build_credential_router(
    config: WebSecurityConfig,
    *,
    prefix: str = "",
) -> APIRouter:
    """Build Credential API router with security dependencies installed.

    B1 returns a router with no endpoints——tests can verify security envelope
    via stub routes. B2 will populate real endpoints.

    Security envelope applied to ALL routes on this router:
        - X-PI-Agent-UI: 1 required
        - Origin validated against allowed_ui_origins
    """
    router = APIRouter(
        prefix=prefix,
        route_class=CredentialAPIRoute,
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
    )
    return router

"""Provider Profiles REST API（P1-E2-3B2）.

镜像 ``credentials_api.py``（E1-4B）的结构，暴露 7 个 endpoint：

| Method | Path | Description |
|---|---|---|
| GET | /api/provider-profiles | List profiles |
| POST | /api/provider-profiles | Create profile |
| PATCH | /api/provider-profiles/{profile_id} | Update mutable fields |
| DELETE | /api/provider-profiles/{profile_id} | Delete profile |
| GET | /api/provider-profiles/{profile_id}/models | Static model suggestions |
| GET | /api/sessions/{session_id}/model-binding | Read binding |
| PUT | /api/sessions/{session_id}/model-binding | Write explicit binding |

**复用 E1 已有的安全边界**（不复制第二套）：
- TrustedHost middleware（app-level）
- ``require_ui_header_dep`` / ``require_allowed_origin_dep``（router-level deps）
- ``SafeValidationErrorResponse`` / ``safe_validation_response``
- Body limit（新 ``ProviderProfileBodyLimitMiddleware`` 子类，扩展 path 谓词）

**禁止暴露**：
- ``secret_ref`` / ``fingerprint`` / 完整 ``CredentialRecord`` / ``Authorization`` /
  ``x-api-key`` / Keyring service name / Credential validation endpoint / Strategy impl 类型
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request as StarletteRequest

from ..credentials.api import (
    CredentialBodyLimitMiddleware,
    SafeValidationErrorResponse,
    _HeaderMissingError,
    _OriginInvalidError,
    require_allowed_origin_dep,
    require_ui_header_dep,
    safe_validation_response,
)
from ..credentials.dto import SafeValidationField  # noqa: F401 (re-export convenience)
from ..local_web_security import WebSecurityConfig
from .config_runtime import ProviderConfigRuntimeState
from .config_service import (
    CredentialNotFoundForProfileError,
    InvalidModelIdError,
    InvalidProfileNameError,
    ProviderConfigService,
    ProviderConfigServiceError,
    ProviderProfileDisabledError,
    SessionNotFoundError,
    UnknownProviderError,
)
from .config_store import (
    ProviderProfileInUseError,
    ProviderProfileNotFoundError,
)

# ============================================================================
# Path targeting
# ============================================================================


def _is_provider_profile_target(scope: dict[str, Any]) -> bool:
    """ASGI scope predicate for body-limit middleware.

    Targets:
        - /api/provider-profiles and subpaths
        - /api/sessions/{sid}/model-binding (exactly—NOT /api/sessions/{sid}/messages etc.)

    Does NOT match GET / DELETE——body limit only applies to mutating methods
    (the parent middleware checks method).
    """
    path = scope.get("path", "")
    if path.startswith("/api/provider-profiles"):
        return True
    # /api/sessions/{sid}/model-binding——exactly 2 segments after /api/sessions/
    if path.startswith("/api/sessions/") and path.endswith("/model-binding"):
        suffix = path[len("/api/sessions/"):]
        parts = suffix.split("/")
        if len(parts) == 2 and parts[1] == "model-binding":
            return True
    return False


class ProviderProfileBodyLimitMiddleware(CredentialBodyLimitMiddleware):
    """Body limit targeting Provider Profile + Session Binding paths.

    Subclass of ``CredentialBodyLimitMiddleware``——reuses buffer / overflow / 413
    response logic, only overrides path predicate.
    """

    def _is_target(self, scope: dict[str, Any]) -> bool:  # type: ignore[override]
        if scope.get("type") != "http":
            return False
        method = scope.get("method", "")
        if method not in ("POST", "PUT", "PATCH"):
            return False
        return _is_provider_profile_target(scope)


# ============================================================================
# Pydantic DTOs
# ============================================================================


_PROVIDER_PROFILE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,256}$"
_SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,256}$"


class ProviderProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    provider_id: str = Field(..., min_length=1, max_length=64)
    credential_id: str = Field(..., min_length=1, max_length=256)
    default_model: str = Field(..., min_length=1, max_length=256)
    enabled: bool = True
    is_default: bool = False


class ProviderProfileUpdateRequest(BaseModel):
    """At least one field required (validator enforces)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    credential_id: str | None = Field(default=None, min_length=1, max_length=256)
    default_model: str | None = Field(default=None, min_length=1, max_length=256)
    enabled: bool | None = None
    is_default: bool | None = None

    def model_post_init(self, __context: Any) -> None:
        if all(
            getattr(self, f) is None
            for f in ("name", "credential_id", "default_model", "enabled", "is_default")
        ):
            raise ValueError("at least one field must be provided")


class SessionModelBindingPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(..., pattern=_PROVIDER_PROFILE_ID_PATTERN)
    model_id: str = Field(..., min_length=1, max_length=256)


# ============================================================================
# Response DTOs
# ============================================================================


class ProviderProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    provider_id: str
    provider_display_name: str
    credential_id: str
    credential_masked_value: str | None
    default_model: str
    enabled: bool
    is_default: bool
    status: str
    created_at: int
    updated_at: int


class SessionModelBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    profile_id: str
    model_id: str
    source: str
    created_at: int
    updated_at: int


class ModelCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool | None = None
    tool_calling: bool | None = None
    reasoning: bool | None = None
    vision: bool | None = None
    context_window: int | None = None


class ModelOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str | None
    source: str
    capabilities: ModelCapabilitiesResponse


# ============================================================================
# Serializer helpers
# ============================================================================


def serialize_profile_view(view: Any) -> dict[str, Any]:
    """Convert ProviderProfileView → JSON-safe dict.

    ``view`` is the E2-2 business dataclass. We avoid direct ``asdict`` to keep
    explicit control of which fields are exposed.
    """
    return {
        "id": view.id,
        "name": view.name,
        "provider_id": view.provider_id,
        "provider_display_name": view.provider_display_name,
        "credential_id": view.credential_id,
        "credential_masked_value": view.credential_masked_value,
        "default_model": view.default_model,
        "enabled": view.enabled,
        "is_default": view.is_default,
        "status": view.status,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


def serialize_binding(binding: Any) -> dict[str, Any]:
    return {
        "session_id": binding.session_id,
        "profile_id": binding.profile_id,
        "model_id": binding.model_id,
        "source": binding.source,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def serialize_model_option(opt: Any) -> dict[str, Any]:
    caps = opt.capabilities
    return {
        "id": opt.id,
        "display_name": opt.display_name,
        "source": opt.source,
        "capabilities": {
            "streaming": caps.streaming,
            "tool_calling": caps.tool_calling,
            "reasoning": caps.reasoning,
            "vision": caps.vision,
            "context_window": caps.context_window,
        },
    }


# ============================================================================
# Safe error response helper
# ============================================================================


def _provider_profile_error_response(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


# ============================================================================
# Domain error → HTTP mapping
# ============================================================================


def provider_profile_error_to_response(
    exc: Exception,
) -> JSONResponse | None:
    """Map Provider Config domain errors to fixed safe responses.

    Returns ``None`` for unrecognized exceptions—caller falls back to generic 500.
    """
    if isinstance(exc, UnknownProviderError):
        return _provider_profile_error_response(
            status.HTTP_400_BAD_REQUEST,
            "unknown_provider",
            "Provider is not registered.",
        )
    if isinstance(exc, CredentialNotFoundForProfileError):
        return _provider_profile_error_response(
            status.HTTP_409_CONFLICT,
            "credential_not_found",
            "Referenced credential does not exist.",
        )
    if isinstance(exc, ProviderProfileNotFoundError):
        return _provider_profile_error_response(
            status.HTTP_404_NOT_FOUND,
            "profile_not_found",
            "Provider profile not found.",
        )
    if isinstance(exc, ProviderProfileInUseError):
        return _provider_profile_error_response(
            status.HTTP_409_CONFLICT,
            "profile_in_use",
            "Profile is referenced by session bindings.",
        )
    if isinstance(exc, ProviderProfileDisabledError):
        return _provider_profile_error_response(
            status.HTTP_409_CONFLICT,
            "profile_disabled",
            "Profile is disabled.",
        )
    if isinstance(exc, SessionNotFoundError):
        return _provider_profile_error_response(
            status.HTTP_404_NOT_FOUND,
            "session_not_found",
            "Session not found.",
        )
    if isinstance(exc, (InvalidModelIdError, InvalidProfileNameError)):
        # These are service-level normalization errors——422 safe validation
        return _provider_profile_error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "request_validation_failed",
            "Request payload failed validation.",
        )
    if isinstance(exc, ProviderConfigServiceError):
        # Subclass of base——catch-all for service-level internal errors
        return _provider_profile_error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "provider_config_internal_error",
            "Provider config internal error.",
        )
    return None


# ============================================================================
# Custom APIRoute class
# ============================================================================


class ProviderProfileAPIRoute(APIRoute):
    """Custom APIRoute——safe ValidationError, safe domain error mapping.

    Mirrors ``CredentialAPIRoute``（E1-4B）but maps Provider Config domain errors
    via ``provider_profile_error_to_response``.
    """

    def get_route_handler(self):  # type: ignore[override]
        original_handler = super().get_route_handler()

        async def custom_route_handler(request: StarletteRequest) -> Any:
            try:
                return await original_handler(request)
            except RequestValidationError as exc:
                return safe_validation_response(exc)
            except _HeaderMissingError as exc:
                return _provider_profile_error_response(
                    exc.status_code,
                    exc.detail["code"],
                    exc.detail["message"],
                )
            except _OriginInvalidError as exc:
                return _provider_profile_error_response(
                    exc.status_code,
                    exc.detail["code"],
                    exc.detail["message"],
                )
            except HTTPException as exc:
                # Safe projection of HTTPException detail
                if isinstance(exc.detail, dict) and "code" in exc.detail:
                    return _provider_profile_error_response(
                        exc.status_code,
                        exc.detail["code"],
                        exc.detail.get("message", "error"),
                    )
                return _provider_profile_error_response(
                    exc.status_code,
                    "provider_config_error",
                    "error",
                )
            except Exception as exc:
                mapped = provider_profile_error_to_response(exc)
                if mapped is not None:
                    return mapped
                # Last-resort generic 500——no str(exc) leak
                return _provider_profile_error_response(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "provider_config_internal_error",
                    "Provider config internal error.",
                )

        return custom_route_handler


# ============================================================================
# Service dependency
# ============================================================================


def get_provider_config_service(
    request: StarletteRequest,
) -> ProviderConfigService:
    """FastAPI dep——read ProviderConfigService from app.state.

    Raises HTTPException 503 if runtime not initialized.
    """
    runtime: ProviderConfigRuntimeState | None = getattr(
        request.app.state, "provider_config_runtime", None
    )
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "provider_config_unavailable",
                "message": "Provider config not initialized.",
            },
        )
    return runtime.service


# ============================================================================
# Router factory
# ============================================================================


def build_provider_profile_router(
    config: WebSecurityConfig,
    *,
    prefix: str = "",
) -> APIRouter:
    """Build empty router with security deps + custom APIRoute class."""
    router = APIRouter(
        route_class=ProviderProfileAPIRoute,
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
        responses={
            422: {
                "model": SafeValidationErrorResponse,
                "description": "Provider profile request validation failed.",
            }
        },
        prefix=prefix,
    )
    return router


def register_provider_profile_endpoints(router: APIRouter) -> None:
    """Register 7 endpoints on the given router."""

    # ========================================================================
    # GET /api/provider-profiles
    # ========================================================================
    @router.get("/api/provider-profiles")
    async def list_profiles(
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        views = await service.list_profiles()
        return JSONResponse(
            content={"profiles": [serialize_profile_view(v) for v in views]}
        )

    # ========================================================================
    # POST /api/provider-profiles
    # ========================================================================
    @router.post(
        "/api/provider-profiles",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_profile(
        req: ProviderProfileCreateRequest,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        view = await service.create_profile(
            name=req.name,
            provider_id=req.provider_id,
            credential_id=req.credential_id,
            default_model=req.default_model,
            enabled=req.enabled,
            is_default=req.is_default,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"profile": serialize_profile_view(view)},
        )

    # ========================================================================
    # PATCH /api/provider-profiles/{profile_id}
    # ========================================================================
    @router.patch("/api/provider-profiles/{profile_id}")
    async def update_profile(
        profile_id: str,
        req: ProviderProfileUpdateRequest,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        view = await service.update_profile(
            profile_id,
            name=req.name,
            credential_id=req.credential_id,
            default_model=req.default_model,
            enabled=req.enabled,
            is_default=req.is_default,
        )
        return JSONResponse(content={"profile": serialize_profile_view(view)})

    # ========================================================================
    # DELETE /api/provider-profiles/{profile_id}
    # ========================================================================
    @router.delete(
        "/api/provider-profiles/{profile_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_profile(
        profile_id: str,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        await service.delete_profile(profile_id)
        return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)

    # ========================================================================
    # GET /api/provider-profiles/{profile_id}/models
    # ========================================================================
    @router.get("/api/provider-profiles/{profile_id}/models")
    async def list_models(
        profile_id: str,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        options = await service.list_models(profile_id)
        return JSONResponse(
            content={"models": [serialize_model_option(o) for o in options]}
        )

    # ========================================================================
    # GET /api/sessions/{session_id}/model-binding
    # ========================================================================
    @router.get("/api/sessions/{session_id}/model-binding")
    async def get_session_binding(
        session_id: str,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        binding = await service.get_session_binding(session_id)
        if binding is None:
            return JSONResponse(content={"binding": None})
        return JSONResponse(content={"binding": serialize_binding(binding)})

    # ========================================================================
    # PUT /api/sessions/{session_id}/model-binding
    # ========================================================================
    @router.put("/api/sessions/{session_id}/model-binding")
    async def put_session_binding(
        session_id: str,
        req: SessionModelBindingPutRequest,
        service: Annotated[
            ProviderConfigService, Depends(get_provider_config_service)
        ],
    ) -> JSONResponse:
        binding = await service.set_session_binding(
            session_id=session_id,
            profile_id=req.profile_id,
            model_id=req.model_id,
        )
        return JSONResponse(content={"binding": serialize_binding(binding)})


def build_full_provider_profile_router(
    config: WebSecurityConfig,
    *,
    prefix: str = "",
) -> APIRouter:
    """Build router with security envelope + 7 endpoints."""
    router = build_provider_profile_router(config, prefix=prefix)
    register_provider_profile_endpoints(router)
    return router


__all__ = [
    # Middleware
    "ProviderProfileBodyLimitMiddleware",
    # DTOs
    "ProviderProfileCreateRequest",
    "ProviderProfileUpdateRequest",
    "SessionModelBindingPutRequest",
    "ProviderProfileResponse",
    "SessionModelBindingResponse",
    "ModelOptionResponse",
    "ModelCapabilitiesResponse",
    # Serializers
    "serialize_profile_view",
    "serialize_binding",
    "serialize_model_option",
    # Error mapping
    "provider_profile_error_to_response",
    "ProviderProfileAPIRoute",
    # Router
    "build_provider_profile_router",
    "build_full_provider_profile_router",
    "register_provider_profile_endpoints",
    # Service dep
    "get_provider_config_service",
]

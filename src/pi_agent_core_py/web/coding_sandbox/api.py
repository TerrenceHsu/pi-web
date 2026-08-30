"""Localhost-only REST endpoints for coding sandbox administration."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from coding_sandbox.admin import (
    SandboxAdminConfig,
    SandboxAdminService,
    SandboxConfigConflictError,
    SandboxConfigStoreError,
)
from coding_sandbox.lifecycle import (
    SANDBOX_STATE_MACHINE_VERSION,
    ManagedSandboxLifecycle,
    ManagedSandboxOperationRecord,
    SandboxLifecycleError,
    SandboxOperationStoreError,
)

from ..credentials.api import (
    CredentialBodyLimitMiddleware,
    require_allowed_origin_dep,
    require_ui_header_dep,
    safe_validation_response,
)
from ..local_web_security import WebSecurityConfig
from .runtime import SandboxRuntimeState

_API_PREFIX = "/api/coding-sandbox"


class SandboxConfigPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    config: SandboxAdminConfig


class SandboxOperationStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)


class SandboxBodyLimitMiddleware(CredentialBodyLimitMiddleware):
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        super().__init__(app, max_bytes=max_bytes, path_prefixes=(_API_PREFIX,))


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


class SandboxAPIRoute(APIRoute):
    def get_route_handler(
        self,
    ) -> Callable[[StarletteRequest], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: StarletteRequest) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return safe_validation_response(exc)
            except SandboxConfigConflictError:
                return _error(
                    status.HTTP_409_CONFLICT,
                    "sandbox_config_conflict",
                    "Sandbox configuration changed; reload and retry.",
                )
            except SandboxConfigStoreError:
                return _error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "sandbox_config_unavailable",
                    "Sandbox configuration is unavailable.",
                )
            except SandboxLifecycleError as exc:
                status_code = {
                    "operation_not_found": status.HTTP_404_NOT_FOUND,
                    "operation_conflict": status.HTTP_409_CONFLICT,
                    "operation_not_ready": status.HTTP_409_CONFLICT,
                    "validation_required": status.HTTP_409_CONFLICT,
                    "approval_required": status.HTTP_409_CONFLICT,
                    "sandbox_disabled": status.HTTP_409_CONFLICT,
                    "sandbox_not_configured": status.HTTP_409_CONFLICT,
                    "project_invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "provider_error": status.HTTP_502_BAD_GATEWAY,
                    "publisher_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "publisher_unavailable": status.HTTP_409_CONFLICT,
                    "operation_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "no_changes": status.HTTP_409_CONFLICT,
                    "artifact_unavailable": status.HTTP_409_CONFLICT,
                    "artifact_file_not_found": status.HTTP_404_NOT_FOUND,
                }[exc.code]
                return _error(status_code, exc.code, str(exc))
            except SandboxOperationStoreError:
                return _error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "sandbox_operation_store_unavailable",
                    "Sandbox operation state is unavailable.",
                )
            except HTTPException as exc:
                if isinstance(exc.detail, dict):
                    code = exc.detail.get("code")
                    message = exc.detail.get("message")
                    if isinstance(code, str) and isinstance(message, str):
                        return _error(exc.status_code, code, message)
                return _error(exc.status_code, "sandbox_api_error", "Request failed.")
            except Exception:
                return _error(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "sandbox_internal_error",
                    "Sandbox operation failed.",
                )

        return handler


def get_sandbox_admin_service(request: Request) -> SandboxAdminService:
    runtime = cast(
        SandboxRuntimeState | None,
        getattr(request.app.state, "coding_sandbox_runtime", None),
    )
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "sandbox_runtime_unavailable",
                "message": "Sandbox runtime is not initialized.",
            },
        )
    return runtime.service


def get_sandbox_lifecycle(request: Request) -> ManagedSandboxLifecycle:
    runtime = cast(
        SandboxRuntimeState | None,
        getattr(request.app.state, "coding_sandbox_runtime", None),
    )
    if runtime is None or runtime.lifecycle is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "sandbox_runtime_unavailable",
                "message": "Managed Sandbox lifecycle is not initialized.",
            },
        )
    return cast(ManagedSandboxLifecycle, runtime.lifecycle)


def _operation_payload(record: ManagedSandboxOperationRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["state_machine_version"] = SANDBOX_STATE_MACHINE_VERSION
    payload["allowed_actions"] = list(record.allowed_actions)
    payload["allowed_transitions"] = list(record.allowed_transitions)
    payload["terminal"] = record.terminal
    payload["cancellable"] = record.cancellable
    payload["approval_required"] = record.status == "awaiting_approval"
    return payload


def build_coding_sandbox_router(config: WebSecurityConfig) -> APIRouter:
    router = APIRouter(
        route_class=SandboxAPIRoute,
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
    )

    @router.get(f"{_API_PREFIX}/config")
    async def get_config(
        service: Annotated[
            SandboxAdminService,
            Depends(get_sandbox_admin_service),
        ],
    ) -> JSONResponse:
        record = await service.get_config()
        return JSONResponse(content=record.model_dump(mode="json"))

    @router.put(f"{_API_PREFIX}/config")
    async def put_config(
        body: SandboxConfigPutRequest,
        service: Annotated[
            SandboxAdminService,
            Depends(get_sandbox_admin_service),
        ],
    ) -> JSONResponse:
        record = await service.update_config(
            body.config,
            expected_revision=body.expected_revision,
        )
        return JSONResponse(content=record.model_dump(mode="json"))

    @router.post(f"{_API_PREFIX}/test-connection")
    async def test_connection(
        service: Annotated[
            SandboxAdminService,
            Depends(get_sandbox_admin_service),
        ],
    ) -> JSONResponse:
        result = await service.test_connection()
        return JSONResponse(content=result.model_dump(mode="json"))

    @router.post(f"{_API_PREFIX}/operations")
    async def start_operation(
        body: SandboxOperationStartRequest,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        record = await lifecycle.start(body.session_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=_operation_payload(record),
        )

    @router.get(f"{_API_PREFIX}/sessions/{{session_id}}/operation")
    async def latest_session_operation(
        session_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        record = await lifecycle.latest_for_session(session_id)
        return JSONResponse(
            content={"operation": None if record is None else _operation_payload(record)}
        )

    @router.get(f"{_API_PREFIX}/operations/{{operation_id}}")
    async def get_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return JSONResponse(content=_operation_payload(await lifecycle.get(operation_id)))

    @router.get(f"{_API_PREFIX}/operations/{{operation_id}}/events")
    async def get_operation_events(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> JSONResponse:
        page = await lifecycle.events(
            operation_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return JSONResponse(content=page.model_dump(mode="json"))

    @router.get(f"{_API_PREFIX}/operations/{{operation_id}}/diff")
    async def get_operation_diff(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        result = await lifecycle.diff(operation_id)
        return JSONResponse(content=result.model_dump(mode="json"))

    @router.get(f"{_API_PREFIX}/operations/{{operation_id}}/artifact-file")
    async def get_operation_artifact_file(
        operation_id: str,
        path: Annotated[str, Query(min_length=1, max_length=1024)],
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> Response:
        """Read or download one frozen file without publishing the artifact."""
        item = await lifecycle.read_frozen_file(operation_id, path)
        media_type = mimetypes.guess_type(item.name)[0] or "application/octet-stream"
        return Response(
            content=item.content,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(item.name)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _accepted_action(
        operation_id: str,
        lifecycle: ManagedSandboxLifecycle,
        action: str,
    ) -> JSONResponse:
        handler = {
            "validate": lifecycle.validate,
            "prepare-publish": lifecycle.prepare_publish,
            "publish": lifecycle.publish,
            "refreeze": lifecycle.refreeze,
            "retry-publish": lifecycle.retry_publish,
        }[action]
        record = await handler(operation_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=_operation_payload(record),
        )

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/validate")
    async def validate_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return await _accepted_action(operation_id, lifecycle, "validate")

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/prepare-publish")
    async def prepare_operation_publish(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return await _accepted_action(operation_id, lifecycle, "prepare-publish")

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/publish")
    async def publish_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return await _accepted_action(operation_id, lifecycle, "publish")

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/refreeze")
    async def refreeze_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return await _accepted_action(operation_id, lifecycle, "refreeze")

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/retry-publish")
    async def retry_operation_publish(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return await _accepted_action(operation_id, lifecycle, "retry-publish")

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/cancel")
    async def cancel_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return JSONResponse(content=_operation_payload(await lifecycle.cancel(operation_id)))

    @router.post(f"{_API_PREFIX}/operations/{{operation_id}}/discard")
    async def discard_operation(
        operation_id: str,
        lifecycle: Annotated[
            ManagedSandboxLifecycle,
            Depends(get_sandbox_lifecycle),
        ],
    ) -> JSONResponse:
        return JSONResponse(content=_operation_payload(await lifecycle.discard(operation_id)))

    return router


__all__ = [
    "SandboxBodyLimitMiddleware",
    "SandboxConfigPutRequest",
    "SandboxOperationStartRequest",
    "build_coding_sandbox_router",
    "get_sandbox_admin_service",
    "get_sandbox_lifecycle",
]

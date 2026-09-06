"""Execution administration and account-private history. No execute endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from coding_agent_app.execution.control import ExecutionControl
from coding_agent_app.execution.models import ExecutionGrant

from .credentials.api import require_allowed_origin_dep, require_ui_header_dep
from .execution import WebExecutionRuntime
from .local_web_security import WebSecurityConfig


def _admin(request: Request) -> None:
    if not bool(getattr(request.scope.get("auth_user"), "is_admin", False)):
        raise HTTPException(403, "Administrator access required.")


def _runtime(request: Request) -> WebExecutionRuntime:
    value = getattr(request.app.state, "execution_runtime", None)
    if not isinstance(value, WebExecutionRuntime):
        raise HTTPException(503, "Execution is unavailable.")
    return value


def _control(request: Request) -> ExecutionControl:
    value = getattr(request.app.state, "execution_control", None)
    return value if isinstance(value, ExecutionControl) else _runtime(request).control


def _json(value: object) -> JSONResponse:
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


def _task(grant: ExecutionGrant) -> dict[str, object]:
    scope = grant.scope
    return {
        **scope.identity.model_dump(),
        "state": grant.state,
        "kind": scope.kind,
        "backend": scope.backend,
        "commands_used": grant.commands_used,
        "max_commands": scope.max_commands,
        "charged_ms": grant.charged_ms,
        "max_execution_ms": scope.max_execution_ms,
        "cleanup_pending": grant.cleanup_pending,
        "created_at_ms": grant.created_at_ms,
        "expires_at_ms": grant.expires_at_ms,
    }


def build_execution_router(config: WebSecurityConfig) -> APIRouter:
    router = APIRouter(
        tags=["execution"],
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
    )

    @router.get("/api/admin/local-execution/status")
    async def status(request: Request) -> JSONResponse:
        _admin(request)
        runtime = getattr(request.app.state, "execution_runtime", None)
        return _json(
            {
                **await _control(request).summary(),
                "cache": runtime.cache_status if isinstance(runtime, WebExecutionRuntime) else None,
            }
        )

    @router.post("/api/admin/local-execution/probe")
    async def probe(request: Request) -> JSONResponse:
        _admin(request)
        return _json(await _runtime(request).probe())

    @router.post("/api/admin/local-execution/cleanup")
    async def cleanup(request: Request) -> JSONResponse:
        _admin(request)
        _control(request).request_cleanup()
        return _json({"cleanup_requested": True})

    @router.post("/api/admin/local-execution/tasks/{task_id}/revoke")
    async def revoke_global(task_id: str, request: Request) -> JSONResponse:
        _admin(request)
        if not await _control(request).revoke(task_id):
            raise HTTPException(404, "Execution task not found.")
        return _json({"stop_requested": True})

    @router.get("/api/sessions/{session_id}/execution-tasks")
    async def tasks(session_id: str, request: Request) -> JSONResponse:
        grants = await _runtime(request).runtime.store.list_grants(session_id=session_id)
        return _json({"tasks": [_task(g) for g in grants[:100]]})

    async def owned(session_id: str, task_id: str, request: Request) -> ExecutionGrant:
        grants = await _runtime(request).runtime.store.list_grants(session_id=session_id)
        for grant in grants:
            if grant.scope.identity.task_id == task_id:
                return grant
        raise HTTPException(404, "Execution task not found.")

    @router.get("/api/sessions/{session_id}/execution-tasks/{task_id}")
    async def detail(session_id: str, task_id: str, request: Request) -> JSONResponse:
        grant = await owned(session_id, task_id, request)
        store = _runtime(request).runtime.store
        return _json(
            {
                **_task(grant),
                "commands": [
                    c.model_dump(mode="json") for c in await store.commands(grant.scope.identity)
                ],
                "logs": await store.private_logs(grant.scope.identity),
            }
        )

    @router.post("/api/sessions/{session_id}/execution-tasks/{task_id}/revoke")
    async def revoke_own(session_id: str, task_id: str, request: Request) -> JSONResponse:
        grant = await owned(session_id, task_id, request)
        await _runtime(request).revoke_task(grant.scope.identity)
        return _json({"stop_requested": True})

    return router

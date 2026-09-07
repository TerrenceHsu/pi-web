"""Administrator-only Telemetry REST API."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from ...telemetry import SQLiteTelemetryContext, TelemetryReader
from ..credentials.api import require_allowed_origin_dep, require_ui_header_dep
from ..local_web_security import WebSecurityConfig


def _reader(request: Request) -> TelemetryReader:
    user = request.scope.get("auth_user")
    if not bool(getattr(user, "is_admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required.",
        )
    reader = getattr(request.app.state, "telemetry_reader", None)
    if not isinstance(reader, TelemetryReader):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telemetry storage is unavailable.",
        )
    return reader


def _since_ms(window_hours: int) -> int:
    return time.time_ns() // 1_000_000 - window_hours * 60 * 60 * 1000


def build_telemetry_router(config: WebSecurityConfig) -> APIRouter:
    router = APIRouter(
        prefix="/api/admin/telemetry",
        tags=["telemetry"],
        dependencies=[
            Depends(require_ui_header_dep(config)),
            Depends(require_allowed_origin_dep(config)),
        ],
    )

    @router.get("/summary")
    async def telemetry_summary(
        request: Request,
        window_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
    ) -> JSONResponse:
        reader = _reader(request)
        payload = await reader.summary(since_ms=_since_ms(window_hours))
        if isinstance(reader, SQLiteTelemetryContext):
            payload["retention"] = {
                "days": reader.retention_days,
                "max_completed_spans": reader.max_spans,
                "error_code": reader.maintenance_error,
            }
        response = JSONResponse(content=payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/spans")
    async def telemetry_spans(
        request: Request,
        window_hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        span_status: Annotated[
            Literal["running", "ok", "error"] | None,
            Query(alias="status"),
        ] = None,
        name: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        account_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        session_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    ) -> JSONResponse:
        spans = await _reader(request).list_spans(
            since_ms=_since_ms(window_hours),
            limit=limit,
            status=span_status,
            name=name,
            account_id=account_id,
            session_id=session_id,
        )
        response = JSONResponse(content={"spans": spans, "count": len(spans)})
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/spans/{span_id}")
    async def telemetry_span(span_id: str, request: Request) -> JSONResponse:
        if not 1 <= len(span_id) <= 128:
            raise HTTPException(status_code=404, detail="Telemetry span not found.")
        span = await _reader(request).get_span(span_id)
        if span is None:
            raise HTTPException(status_code=404, detail="Telemetry span not found.")
        response = JSONResponse(content=span)
        response.headers["Cache-Control"] = "no-store"
        return response

    return router


__all__ = ["build_telemetry_router"]

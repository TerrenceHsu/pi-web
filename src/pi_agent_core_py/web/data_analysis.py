"""Web endpoints for optional analysis; the account-scoped app owns Session authorization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from agent_workspace.analysis import AnalysisError, AnalysisRequest
from agent_workspace.store import FileAccessDeniedError, FileStoreError, VirtualFileNotFoundError
from coding_agent_app.data_analysis.service import DataAnalysisService


def register_analysis_routes(
    app: FastAPI,
    service_getter: Callable[[], DataAnalysisService | None],
    require_session: Callable[[str], Awaitable[None]],
) -> None:
    async def service(session_id: str) -> DataAnalysisService:
        await require_session(session_id)
        instance = service_getter()
        if instance is None:
            raise HTTPException(503, "analysis_unavailable")
        return instance

    async def call(operation: Awaitable[Any]) -> Any:
        try:
            return await operation
        except AnalysisError as exc:
            status = 404 if exc.code in {"analysis_not_found", "source_not_found"} else 409
            raise HTTPException(status, exc.code) from None
        except (FileAccessDeniedError, VirtualFileNotFoundError):
            raise HTTPException(404, "source_not_found") from None
        except FileStoreError:
            raise HTTPException(409, "workspace_changed_or_storage_limit") from None

    @app.get("/api/workspaces/{session_id}/analysis")
    async def list_runs(session_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        return {"runs": await call(instance.list_runs(session_id))}

    @app.post("/api/workspaces/{session_id}/analysis", status_code=202)
    async def start_run(session_id: str, request: AnalysisRequest) -> dict[str, Any]:
        instance = await service(session_id)
        run_id = await call(instance.start(session_id, request))
        return dict(await call(instance.get(session_id, run_id)))

    @app.get("/api/workspaces/{session_id}/analysis/{run_id}")
    async def get_run(session_id: str, run_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        return dict(await call(instance.get(session_id, run_id)))

    @app.post("/api/workspaces/{session_id}/analysis/{run_id}/cancel")
    async def cancel_run(session_id: str, run_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        return dict(await call(instance.cancel(session_id, run_id)))

    @app.post("/api/workspaces/{session_id}/analysis/{run_id}/retry", status_code=202)
    async def retry_run(session_id: str, run_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        previous = await call(instance.get(session_id, run_id))
        if previous["status"] not in {"failed", "cancelled", "interrupted"}:
            raise HTTPException(409, "analysis_not_retryable")
        if previous["request"].get("action") == "python":
            raise HTTPException(409, "python_retry_requires_new_chat_approval")
        new_id = await call(
            instance.start(session_id, AnalysisRequest.model_validate(previous["request"]))
        )
        return dict(await call(instance.get(session_id, new_id)))

    @app.post("/api/workspaces/{session_id}/analysis/{run_id}/save")
    async def save_run(session_id: str, run_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        return dict(await call(instance.save(session_id, run_id)))

    @app.get("/api/workspaces/{session_id}/analysis/{run_id}/chart")
    async def chart(session_id: str, run_id: str) -> FileResponse:
        instance = await service(session_id)
        path = await call(instance.output(session_id, run_id, "chart.png"))
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "private, no-store"}
        )

    @app.delete("/api/workspaces/{session_id}/analysis/{run_id}")
    async def delete_run(session_id: str, run_id: str) -> dict[str, Any]:
        instance = await service(session_id)
        await call(instance.delete(session_id, run_id))
        return {"deleted": True}

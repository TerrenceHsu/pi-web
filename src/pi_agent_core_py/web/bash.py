"""Explicit standalone Bash approval, bounded private history and Web tool adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import aiosqlite
from pydantic import ValidationError

from coding_agent_app.execution.models import ExecutionDenied
from coding_agent_app.execution.runtime import ExecutionRequest, PreparedExecution
from coding_agent_app.execution.service import CommandInterrupted
from coding_sandbox.bash import BashRequest
from coding_sandbox.publication import PublicationBinding
from coding_sandbox.workspace_models import SandboxWorkspaceError

from ..agent.tooling import AgentTool, ToolResult, ToolUpdateCallback
from ..ai.messages import TextContent
from ..session_backends.sqlite.database import database_for
from .execution import WebExecutionRuntime


class BashHistory:
    """Per-account SQLite, Session FK, exact input, bounded output; never Telemetry."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._db, self._database = connection, database_for(connection)

    async def init(self) -> None:
        async with self._database.transaction():
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS web_bash_runs ("
                "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) "
                "ON DELETE CASCADE, request_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at_ms INTEGER NOT NULL, payload TEXT NOT NULL, "
                "UNIQUE(request_id,tool_call_id))"
            )
            await self._db.execute(
                "UPDATE web_bash_runs SET status='interrupted' "
                "WHERE status IN ('pending','approved','running')"
            )

    async def create(
        self,
        request: ExecutionRequest,
        tool_call_id: str,
        bash: BashRequest,
    ) -> str:
        run_id, now = f"bash-run-{uuid4().hex}", int(time.time() * 1000)
        async with self._database.transaction():
            # Retain <=200 bounded runs per account, no live run is evicted.
            await self._db.execute(
                "DELETE FROM web_bash_runs WHERE status NOT IN ('pending','approved','running') "
                "AND (created_at_ms < ? OR run_id IN (SELECT run_id FROM web_bash_runs "
                "ORDER BY created_at_ms DESC,run_id DESC LIMIT -1 OFFSET 199))",
                (now - 30 * 86400_000,),
            )
            async with self._db.execute("SELECT count(*) FROM web_bash_runs") as cursor:
                row = await cursor.fetchone()
            if row is not None and int(row[0]) >= 200:
                raise ExecutionDenied("bash_history_full")
            try:
                await self._db.execute(
                    "INSERT INTO web_bash_runs VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        request.session_id,
                        request.request_id,
                        tool_call_id,
                        "pending",
                        now,
                        json.dumps(
                            {**bash.model_dump(), "script_sha256": bash.sha256}, ensure_ascii=False
                        ),
                    ),
                )
            except aiosqlite.IntegrityError:
                raise ExecutionDenied("bash_call_replay_or_invalid_session") from None
        return run_id

    async def finish(
        self,
        session_id: str,
        run_id: str,
        status: str,
        result: dict[str, Any],
    ) -> None:
        async with self._database.transaction():
            async with self._db.execute(
                "SELECT payload FROM web_bash_runs WHERE session_id=? AND run_id=?",
                (session_id, run_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ExecutionDenied("bash_run_not_found")
            payload = json.loads(str(row[0]))
            payload["result"] = result
            serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized.encode()) > 256 * 1024:
                raise ExecutionDenied("bash_history_limit")
            await self._db.execute(
                "UPDATE web_bash_runs SET status=?,payload=? WHERE session_id=? AND run_id=?",
                (status, serialized, session_id, run_id),
            )

    async def interrupt_call(self, request: ExecutionRequest, tool_call_id: str) -> None:
        async with self._database.transaction():
            await self._db.execute(
                "UPDATE web_bash_runs SET status='interrupted' WHERE session_id=? "
                "AND request_id=? AND tool_call_id=? AND status='pending'",
                (request.session_id, request.request_id, tool_call_id),
            )

    async def read(self, session_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        async with self._database.operation():
            async with self._db.execute(
                "SELECT run_id,request_id,status,created_at_ms,payload FROM web_bash_runs "
                "WHERE session_id=? AND (? IS NULL OR run_id=?) "
                "ORDER BY created_at_ms DESC,run_id DESC LIMIT 100",
                (session_id, run_id, run_id),
            ) as cursor:
                rows = await cursor.fetchall()
        records = []
        for row in rows:
            payload = json.loads(str(row[4]))
            records.append(
                {
                    "run_id": row[0],
                    "request_id": row[1],
                    "status": row[2],
                    "created_at_ms": row[3],
                    "script_sha256": payload["script_sha256"],
                    "cwd": payload["cwd"],
                    **(payload if run_id is not None else {}),
                }
            )
        return records


class WebBashTool(AgentTool):
    name = "run_bash"
    label = "Bash · Local Docker · confirm this script"
    description = (
        "Run a complete Bash script once in a NEW offline Docker Workspace copy after human "
        "approval. No host execution, network or extra dependencies. Use existing /workspace "
        "files and a relative cwd. Returns bounded output and change summary; ALL COPY FILE "
        "CHANGES ARE DISCARDED, not saved/published. Each call starts from the real Workspace."
    )
    parameters = BashRequest.model_json_schema()
    execution_mode = "sequential"

    def __init__(
        self,
        execution: WebExecutionRuntime,
        history: BashHistory,
        context: Callable[[], ExecutionRequest],
    ) -> None:
        self.execution, self.history, self._context = execution, history, context

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        try:
            bash = BashRequest.model_validate(args)
            request = self._context()  # No current-Session fallback or model-supplied identity.
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError
            job = asyncio.create_task(self._run(request, tool_call_id, bash, signal))
            stopped = None if signal is None else asyncio.create_task(signal.wait())
            try:
                if stopped is not None:
                    done, _ = await asyncio.wait(
                        (job, stopped),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopped in done:
                        raise asyncio.CancelledError
                details = await job
            finally:
                if not job.done():
                    job.cancel()
                if stopped is not None:
                    stopped.cancel()
                await asyncio.gather(
                    job, *(() if stopped is None else (stopped,)), return_exceptions=True
                )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                details=details,
                is_error=details["status"] != "succeeded",
                content=[TextContent(text=json.dumps(details, ensure_ascii=False))],
            )
        except asyncio.CancelledError:
            if signal is None or not signal.is_set():
                raise
            # Cooperative tool cancellation must return to Agent loop so it can
            # emit its terminal lifecycle; external task cancellation still raises.
            code = "execution_cancelled"
        except ValidationError:
            code = "invalid_bash_request"
        except (ExecutionDenied, SandboxWorkspaceError) as exc:
            code = exc.code
        except Exception:
            code = "bash_execution_failed"
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            is_error=True,
            content=[TextContent(text=code)],
            details={"error_code": code},
        )

    async def _run(
        self,
        request: ExecutionRequest,
        tool_call_id: str,
        bash: BashRequest,
        signal: asyncio.Event | None,
    ) -> dict[str, Any]:
        if request.request_id in self.execution.bash_tasks:
            raise ExecutionDenied("execution_busy")
        previous = await self.execution._lifecycle.latest_for_session(request.session_id)
        if previous is not None and not previous.terminal:
            raise ExecutionDenied("previous_artifact_pending")
        try:
            run_id = await self.history.create(request, tool_call_id, bash)
        except asyncio.CancelledError:
            # SQLite may have committed before the caller received the run ID.
            await asyncio.shield(self.history.interrupt_call(request, tool_call_id))
            raise
        prepared: PreparedExecution | None = None
        adopted = False
        details: dict[str, Any] = {
            "run_id": run_id,
            "status": "interrupted",
            "published": False,
            "publish_required": False,
            "file_changes_saved": False,
            "command_id": None,
            "exit_code": None,
            "termination_reason": None,
        }
        try:
            prepared = await self.execution.runtime.prepare(
                request,
                goal=bash.reason or "Bash",
                bash=bash,
            )
            self.execution.bash_tasks[request.request_id] = prepared
            identity = prepared.scope.identity
            details.update(
                execution_task_id=identity.task_id,
                operation_id=identity.operation_id,
                baseline_revision=prepared.scope.baseline_revision,
                baseline_sha256=prepared.scope.baseline_sha256,
                input_sha256=prepared.scope.input_sha256,
                scope_sha256=prepared.scope.sha256,
                backend=prepared.profile.backend,
                runtime_id=prepared.profile.runtime_id,
                script_sha256=bash.sha256,
                cwd=bash.cwd,
                timeout_seconds=bash.timeout_seconds,
            )
            if not await self.execution.approve_execution(prepared, signal=signal):
                await self.execution.runtime.store.terminate(identity, state="denied")
                details.update(status="denied", error_code="execution_denied")
                return details
            await self.history.finish(request.session_id, run_id, "approved", details)
            await self.execution.runtime.start(identity)
            await self.history.finish(request.session_id, run_id, "running", details)
            async with self.execution.runtime.bind(identity) as operation:
                result = await operation.run_bash(bash, signal=signal)
                details.update(
                    **result.model_dump(mode="json"),
                    status="succeeded" if result.succeeded else "failed",
                    duration_ms=max(0, result.finished_at_ms - result.started_at_ms),
                    copy_revision=operation.workspace_revision,
                )
                diff = await operation.diff(max_patch_bytes=0)
                changed_paths: list[str] = []
                path_bytes = 0
                for entry in diff.entries[:50]:
                    size = len(json.dumps(entry.path, ensure_ascii=False).encode()) + 2
                    if path_bytes + size > 8192:
                        break
                    changed_paths.append(entry.path)
                    path_bytes += size
                details.update(
                    changed_count=len(diff.entries),
                    changed_paths=changed_paths,
                    changes_truncated=len(changed_paths) < len(diff.entries),
                )
                if result.succeeded and diff.entries:

                    async def close_runtime() -> None:
                        await self.execution.runtime.finish(identity)

                    lifecycle = self.execution._lifecycle
                    await lifecycle.adopt_approved_operation(
                        session_id=request.session_id,
                        operation=operation,
                        baseline=prepared.baseline,
                        config_revision=prepared.profile.config_revision,
                        close_runtime=close_runtime,
                        publish_available=True,
                        publication=PublicationBinding(
                            session_id=request.session_id,
                            purpose="bash",
                            backend="local_docker",
                            scope_sha256=prepared.scope.sha256,
                            policy_sha256=prepared.scope.publish_policy_sha256,
                        ),
                    )
                    adopted = True
                    await lifecycle.prepare_bash_publish(identity.operation_id)
                    frozen = await lifecycle.join_action(identity.operation_id)
                    if frozen.status != "awaiting_approval":
                        details.update(
                            status="failed", error_code=frozen.error_code or "artifact_invalid"
                        )
                    else:
                        details.update(
                            publish_required=True,
                            file_changes_saved=True,
                            artifact_id=frozen.artifact_id,
                            artifact_sha256=frozen.artifact_sha256,
                            evidence_kind="bash-output-integrity/v1",
                        )
            details["notice"] = (
                "Frozen files are available in Changes for separate publication approval. "
                "Bash output integrity is not functional validation."
                if details["file_changes_saved"]
                else "No publication artifact was created. Copy files are discarded; "
                "private output is retained."
            )
            return details
        except asyncio.CancelledError:
            details.update(status="cancelled", error_code="execution_cancelled")
            raise
        except (ExecutionDenied, SandboxWorkspaceError) as exc:
            if isinstance(exc, CommandInterrupted):
                details.update(**exc.result.model_dump(mode="json"))
            elif exc.code == "command_timeout":
                details["termination_reason"] = "timed_out"
            details.update(status="interrupted", error_code=exc.code)
            return details
        except Exception:
            details.update(status="interrupted", error_code="bash_execution_failed")
            return details
        finally:

            async def finalize() -> None:
                if prepared is not None:
                    try:
                        if adopted:
                            await self.execution._lifecycle.release_execution(
                                prepared.scope.identity.operation_id
                            )
                        await self.execution.runtime.finish(prepared.scope.identity)
                        grant = await self.execution.runtime.store.get(prepared.scope.identity)
                        details["cleanup_pending"] = grant.cleanup_pending
                        details["commands_used"] = grant.commands_used
                        details["charged_ms"] = grant.charged_ms
                        details["remaining_execution_ms"] = max(
                            0,
                            grant.scope.max_execution_ms - grant.charged_ms,
                        )
                        if grant.cleanup_pending or (adopted and grant.state != "closed"):
                            if adopted:
                                record = await self.execution._lifecycle.get(
                                    prepared.scope.identity.operation_id,
                                )
                                if "cancel" in record.allowed_actions:
                                    await self.execution._lifecycle.cancel(record.operation_id)
                            details.update(publish_required=False, file_changes_saved=False)
                            details.update(
                                status="interrupted",
                                error_code="cleanup_pending"
                                if grant.cleanup_pending
                                else "grant_revoked",
                            )
                        elif adopted:
                            await self.execution._lifecycle.confirm_execution_released(
                                prepared.scope.identity.operation_id,
                            )
                    finally:
                        self.execution.bash_tasks.pop(request.request_id, None)
                await self.history.finish(request.session_id, run_id, details["status"], details)

            cleanup = asyncio.create_task(finalize())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Stop can arrive while a denied/finished call is already closing.
                # Join cleanup AND history persistence before completing the tool.
                await cleanup
                raise

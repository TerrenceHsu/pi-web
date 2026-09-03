"""SQLite source of truth for Plan Mode state and role communication."""

from __future__ import annotations

import json
import time
from typing import Any, cast
from uuid import uuid4

import aiosqlite

from pi_agent_core_py.session_backends.sqlite.database import (
    database_for,
    serialized_operation,
)

from .models import (
    PlanEvent,
    PlanRunStatus,
    PlanRunView,
    PlanSpec,
    PlanTaskStatus,
    PlanTaskView,
    TaskBlockedReport,
    TaskExecutionReport,
    VerificationReport,
)


class PlanStoreError(RuntimeError):
    """Stable Plan control-plane error."""


class PlanNotFoundError(PlanStoreError):
    pass


class PlanConflictError(PlanStoreError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PlanStore:
    """Append-audited plan state sharing the Session SQLite connection."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._db = connection
        self._operation_lock = database_for(connection).operation_lock
        self._write_lock = self._operation_lock

    @serialized_operation
    async def init(self) -> None:
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS plan_runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_version INTEGER NOT NULL DEFAULT 0,
                plan_summary TEXT,
                sandbox_operation_id TEXT,
                artifact_id TEXT,
                failure_code TEXT,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_plan_runs_session_updated
                ON plan_runs(session_id, updated_at_ms DESC);

            CREATE TABLE IF NOT EXISTS plan_versions (
                run_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                PRIMARY KEY (run_id, version),
                FOREIGN KEY (run_id) REFERENCES plan_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS plan_tasks (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                dependencies_json TEXT NOT NULL,
                acceptance_json TEXT NOT NULL,
                allowed_paths_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                execution_json TEXT,
                verification_json TEXT,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (run_id, task_id),
                FOREIGN KEY (run_id) REFERENCES plan_runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS plan_events (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                task_id TEXT,
                attempt INTEGER,
                causation_id TEXT,
                payload_json TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                UNIQUE (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES plan_runs(id) ON DELETE CASCADE
            );
            """
        )
        await self._db.commit()

    @serialized_operation
    async def recover_interrupted(self) -> int:
        """Fail closed after restart; never replay model calls or tool effects."""
        async with self._write_lock:
            now = _now_ms()
            cursor = await self._db.execute(
                """
                SELECT id FROM plan_runs
                WHERE status IN (
                    'planning','awaiting_plan_approval','executing','verifying'
                )
                """
            )
            run_ids = [str(row["id"]) for row in await cursor.fetchall()]
            for run_id in run_ids:
                await self._db.execute(
                    """
                    UPDATE plan_runs SET status='interrupted',
                        failure_code='process_restarted', updated_at_ms=? WHERE id=?
                    """,
                    (now, run_id),
                )
                await self._append_event_locked(
                    run_id,
                    "plan_interrupted",
                    payload={"failure_code": "process_restarted"},
                )
            await self._db.commit()
            return len(run_ids)

    @serialized_operation
    async def create_run(self, session_id: str, request_id: str, goal: str) -> PlanRunView:
        run_id = f"plan_{uuid4().hex}"
        now = _now_ms()
        async with self._write_lock:
            await self._db.execute(
                """
                INSERT INTO plan_runs(
                    id,session_id,request_id,goal,status,created_at_ms,updated_at_ms
                ) VALUES(?,?,?,?,'planning',?,?)
                """,
                (run_id, session_id, request_id, goal, now, now),
            )
            await self._append_event_locked(run_id, "plan_run_started", payload={})
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def save_plan(self, run_id: str, spec: PlanSpec) -> PlanRunView:
        now = _now_ms()
        async with self._write_lock:
            row = await self._run_row_locked(run_id)
            if row["status"] != "planning":
                raise PlanConflictError("plan can only be submitted while planning")
            version = int(row["plan_version"]) + 1
            await self._db.execute(
                "INSERT INTO plan_versions(run_id,version,spec_json,created_at_ms) VALUES(?,?,?,?)",
                (run_id, version, _json(spec.model_dump(mode="json")), now),
            )
            for ordinal, task in enumerate(spec.tasks, start=1):
                await self._db.execute(
                    """
                    INSERT INTO plan_tasks(
                        run_id,task_id,ordinal,title,objective,dependencies_json,
                        acceptance_json,allowed_paths_json,status,updated_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,'pending',?)
                    """,
                    (
                        run_id,
                        task.id,
                        ordinal,
                        task.title,
                        task.objective,
                        _json(task.dependencies),
                        _json(task.acceptance_criteria),
                        _json(task.allowed_paths),
                        now,
                    ),
                )
            await self._db.execute(
                """
                UPDATE plan_runs SET status='awaiting_plan_approval', plan_version=?,
                    plan_summary=?, updated_at_ms=? WHERE id=?
                """,
                (version, spec.summary, now, run_id),
            )
            await self._append_event_locked(
                run_id,
                "plan_created",
                payload={"plan_version": version, "task_count": len(spec.tasks)},
            )
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def approve(self, run_id: str) -> tuple[PlanRunView, bool]:
        async with self._write_lock:
            row = await self._run_row_locked(run_id)
            if row["status"] == "executing":
                return await self._get_run_locked(run_id), True
            if row["status"] != "awaiting_plan_approval":
                raise PlanConflictError("plan is not awaiting approval")
            now = _now_ms()
            await self._db.execute(
                "UPDATE plan_runs SET status='executing', updated_at_ms=? WHERE id=?",
                (now, run_id),
            )
            await self._append_event_locked(run_id, "plan_approved", payload={})
            await self._db.commit()
            return await self._get_run_locked(run_id), False

    @serialized_operation
    async def set_sandbox_operation(self, run_id: str, operation_id: str) -> PlanRunView:
        await self._update_run(
            run_id,
            "executing",
            sandbox_operation_id=operation_id,
            event_type="plan_sandbox_ready",
            payload={"operation_id": operation_id},
        )
        return await self.get_run(run_id)

    @serialized_operation
    async def start_task(self, run_id: str, task_id: str) -> PlanRunView:
        async with self._write_lock:
            row = await self._task_row_locked(run_id, task_id)
            if row["status"] not in {"pending", "failed"}:
                raise PlanConflictError("task cannot enter execution from its current status")
            attempt = int(row["attempt"]) + 1
            now = _now_ms()
            await self._db.execute(
                """
                UPDATE plan_tasks SET status='executing', attempt=?, execution_json=NULL,
                    updated_at_ms=? WHERE run_id=? AND task_id=?
                """,
                (attempt, now, run_id, task_id),
            )
            await self._db.execute(
                "UPDATE plan_runs SET status='executing', updated_at_ms=? WHERE id=?",
                (now, run_id),
            )
            await self._append_event_locked(
                run_id, "plan_task_started", task_id=task_id, attempt=attempt, payload={}
            )
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def submit_execution(
        self, run_id: str, task_id: str, report: TaskExecutionReport
    ) -> PlanRunView:
        async with self._write_lock:
            row = await self._task_row_locked(run_id, task_id)
            if row["status"] != "executing":
                raise PlanConflictError("task is not executing")
            now = _now_ms()
            await self._db.execute(
                """
                UPDATE plan_tasks SET status='awaiting_verification', execution_json=?,
                    updated_at_ms=? WHERE run_id=? AND task_id=?
                """,
                (_json(report.model_dump(mode="json")), now, run_id, task_id),
            )
            await self._db.execute(
                "UPDATE plan_runs SET status='verifying', updated_at_ms=? WHERE id=?",
                (now, run_id),
            )
            await self._append_event_locked(
                run_id,
                "plan_task_execution_submitted",
                task_id=task_id,
                attempt=int(row["attempt"]),
                payload={"changed_paths": list(report.changed_paths)},
            )
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def block_task(
        self, run_id: str, task_id: str, report: TaskBlockedReport
    ) -> PlanRunView:
        async with self._write_lock:
            row = await self._task_row_locked(run_id, task_id)
            if row["status"] != "executing":
                raise PlanConflictError("task is not executing")
            now = _now_ms()
            await self._db.execute(
                """
                UPDATE plan_tasks SET status='blocked', verification_json=?, updated_at_ms=?
                WHERE run_id=? AND task_id=?
                """,
                (_json(report.model_dump(mode="json")), now, run_id, task_id),
            )
            await self._db.execute(
                """
                UPDATE plan_runs SET status='blocked', failure_code='executor_blocked',
                    updated_at_ms=? WHERE id=?
                """,
                (now, run_id),
            )
            await self._append_event_locked(
                run_id,
                "plan_task_blocked",
                task_id=task_id,
                attempt=int(row["attempt"]),
                payload=report.model_dump(mode="json"),
            )
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def submit_verification(
        self, run_id: str, task_id: str, report: VerificationReport
    ) -> PlanRunView:
        async with self._write_lock:
            row = await self._task_row_locked(run_id, task_id)
            if row["status"] != "awaiting_verification":
                raise PlanConflictError("task is not awaiting verification")
            status: PlanTaskStatus = "passed" if report.passed else "failed"
            now = _now_ms()
            await self._db.execute(
                """
                UPDATE plan_tasks SET status=?, verification_json=?, updated_at_ms=?
                WHERE run_id=? AND task_id=?
                """,
                (status, _json(report.model_dump(mode="json")), now, run_id, task_id),
            )
            await self._append_event_locked(
                run_id,
                "plan_task_verified" if report.passed else "plan_task_rejected",
                task_id=task_id,
                attempt=int(row["attempt"]),
                payload=report.model_dump(mode="json"),
            )
            await self._db.commit()
        return await self.get_run(run_id)

    @serialized_operation
    async def mark_artifact_ready(
        self, run_id: str, *, artifact_id: str | None
    ) -> PlanRunView:
        await self._update_run(
            run_id,
            "awaiting_artifact_approval",
            artifact_id=artifact_id,
            event_type="plan_artifact_ready",
            payload={"artifact_id": artifact_id},
        )
        return await self.get_run(run_id)

    @serialized_operation
    async def mark_completed(self, run_id: str) -> PlanRunView:
        async with self._write_lock:
            row = await self._run_row_locked(run_id)
            if row["status"] == "completed":
                return await self._get_run_locked(run_id)
            if row["status"] != "awaiting_artifact_approval":
                raise PlanConflictError("plan artifact is not awaiting approval")
            now = _now_ms()
            await self._db.execute(
                "UPDATE plan_runs SET status='completed', updated_at_ms=? WHERE id=?",
                (now, run_id),
            )
            await self._append_event_locked(run_id, "plan_completed", payload={})
            await self._db.commit()
            return await self._get_run_locked(run_id)

    @serialized_operation
    async def finish(
        self,
        run_id: str,
        status: PlanRunStatus,
        *,
        failure_code: str | None = None,
        event_type: str | None = None,
    ) -> PlanRunView:
        if status not in {"blocked", "failed", "cancelled", "interrupted"}:
            raise ValueError("finish requires a terminal failure status")
        await self._update_run(
            run_id,
            status,
            failure_code=failure_code,
            event_type=event_type or f"plan_{status}",
            payload={"failure_code": failure_code},
        )
        return await self.get_run(run_id)

    @serialized_operation
    async def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        causation_id: str | None = None,
        payload: dict[str, object] | None = None,
        event_id: str | None = None,
    ) -> PlanEvent:
        async with self._write_lock:
            event = await self._append_event_locked(
                run_id,
                event_type,
                task_id=task_id,
                attempt=attempt,
                causation_id=causation_id,
                payload=payload or {},
                event_id=event_id,
            )
            await self._db.commit()
            return event

    @serialized_operation
    async def get_run(self, run_id: str) -> PlanRunView:
        async with self._write_lock:
            return await self._get_run_locked(run_id)

    @serialized_operation
    async def latest_for_session(self, session_id: str) -> PlanRunView | None:
        async with self._write_lock:
            cursor = await self._db.execute(
                "SELECT id FROM plan_runs WHERE session_id=? ORDER BY updated_at_ms DESC LIMIT 1",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return await self._get_run_locked(str(row["id"]))

    @serialized_operation
    async def find_by_sandbox_operation(self, operation_id: str) -> PlanRunView | None:
        async with self._write_lock:
            cursor = await self._db.execute(
                "SELECT id FROM plan_runs WHERE sandbox_operation_id=? LIMIT 1",
                (operation_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return await self._get_run_locked(str(row["id"]))

    @serialized_operation
    async def list_events(self, run_id: str) -> tuple[PlanEvent, ...]:
        async with self._write_lock:
            await self._run_row_locked(run_id)
            cursor = await self._db.execute(
                "SELECT * FROM plan_events WHERE run_id=? ORDER BY sequence", (run_id,)
            )
            return tuple(self._event_from_row(row) for row in await cursor.fetchall())

    async def _update_run(
        self,
        run_id: str,
        status: PlanRunStatus,
        *,
        sandbox_operation_id: str | None = None,
        artifact_id: str | None = None,
        failure_code: str | None = None,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        async with self._write_lock:
            await self._run_row_locked(run_id)
            fields = ["status=?", "updated_at_ms=?"]
            values: list[object] = [status, _now_ms()]
            if sandbox_operation_id is not None:
                fields.append("sandbox_operation_id=?")
                values.append(sandbox_operation_id)
            if artifact_id is not None:
                fields.append("artifact_id=?")
                values.append(artifact_id)
            if failure_code is not None:
                fields.append("failure_code=?")
                values.append(failure_code)
            values.append(run_id)
            await self._db.execute(
                f"UPDATE plan_runs SET {', '.join(fields)} WHERE id=?",  # noqa: S608
                tuple(values),
            )
            await self._append_event_locked(run_id, event_type, payload=payload)
            await self._db.commit()

    async def _append_event_locked(
        self,
        run_id: str,
        event_type: str,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        causation_id: str | None = None,
        payload: dict[str, object],
        event_id: str | None = None,
    ) -> PlanEvent:
        if event_id is not None:
            existing_cursor = await self._db.execute(
                "SELECT * FROM plan_events WHERE id=?",
                (event_id,),
            )
            existing = await existing_cursor.fetchone()
            if existing is not None:
                event = self._event_from_row(existing)
                if event.run_id != run_id or event.event_type != event_type:
                    raise PlanConflictError("plan event id was already used")
                return event
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM plan_events WHERE run_id=?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise PlanStoreError("could not allocate plan event sequence")
        sequence = int(row["next"])
        run_cursor = await self._db.execute(
            "SELECT plan_version FROM plan_runs WHERE id=?",
            (run_id,),
        )
        run_row = await run_cursor.fetchone()
        if run_row is None:
            raise PlanNotFoundError("plan run not found")
        plan_version = int(run_row["plan_version"])
        created = _now_ms()
        resolved_id = event_id or f"plan_event_{uuid4().hex}"
        await self._db.execute(
            """
            INSERT INTO plan_events(
                id,run_id,sequence,event_type,plan_version,task_id,attempt,causation_id,
                payload_json,created_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                resolved_id,
                run_id,
                sequence,
                event_type,
                plan_version,
                task_id,
                attempt,
                causation_id,
                _json(payload),
                created,
            ),
        )
        return PlanEvent(
            id=resolved_id,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            plan_version=plan_version,
            task_id=task_id,
            attempt=attempt,
            causation_id=causation_id,
            payload=payload,
            created_at_ms=created,
        )

    async def _run_row_locked(self, run_id: str) -> aiosqlite.Row:
        cursor = await self._db.execute("SELECT * FROM plan_runs WHERE id=?", (run_id,))
        row = await cursor.fetchone()
        if row is None:
            raise PlanNotFoundError("plan run not found")
        return row

    async def _task_row_locked(self, run_id: str, task_id: str) -> aiosqlite.Row:
        cursor = await self._db.execute(
            "SELECT * FROM plan_tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        )
        row = await cursor.fetchone()
        if row is None:
            raise PlanNotFoundError("plan task not found")
        return row

    async def _get_run_locked(self, run_id: str) -> PlanRunView:
        row = await self._run_row_locked(run_id)
        cursor = await self._db.execute(
            "SELECT * FROM plan_tasks WHERE run_id=? ORDER BY ordinal", (run_id,)
        )
        tasks = tuple(self._task_from_row(item) for item in await cursor.fetchall())
        return PlanRunView(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            request_id=str(row["request_id"]),
            goal=str(row["goal"]),
            status=cast(PlanRunStatus, row["status"]),
            plan_version=int(row["plan_version"]),
            summary=cast(str | None, row["plan_summary"]),
            sandbox_operation_id=cast(str | None, row["sandbox_operation_id"]),
            artifact_id=cast(str | None, row["artifact_id"]),
            failure_code=cast(str | None, row["failure_code"]),
            tasks=tasks,
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )

    @staticmethod
    def _task_from_row(row: aiosqlite.Row) -> PlanTaskView:
        execution_raw = row["execution_json"]
        verification_raw = row["verification_json"]
        execution = (
            TaskExecutionReport.model_validate_json(str(execution_raw))
            if execution_raw is not None
            else None
        )
        verification = None
        blocked = None
        if verification_raw is not None:
            payload = json.loads(str(verification_raw))
            if "passed" in payload:
                verification = VerificationReport.model_validate(payload)
            elif "reason" in payload:
                blocked = TaskBlockedReport.model_validate(payload)
        return PlanTaskView(
            id=str(row["task_id"]),
            ordinal=int(row["ordinal"]),
            title=str(row["title"]),
            objective=str(row["objective"]),
            dependencies=tuple(json.loads(str(row["dependencies_json"]))),
            acceptance_criteria=tuple(json.loads(str(row["acceptance_json"]))),
            allowed_paths=tuple(json.loads(str(row["allowed_paths_json"]))),
            status=cast(PlanTaskStatus, row["status"]),
            attempt=int(row["attempt"]),
            execution=execution,
            verification=verification,
            blocked=blocked,
        )

    @staticmethod
    def _event_from_row(row: aiosqlite.Row) -> PlanEvent:
        return PlanEvent(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            sequence=int(row["sequence"]),
            event_type=str(row["event_type"]),
            plan_version=int(row["plan_version"]),
            task_id=cast(str | None, row["task_id"]),
            attempt=cast(int | None, row["attempt"]),
            causation_id=cast(str | None, row["causation_id"]),
            payload=cast(dict[str, object], json.loads(str(row["payload_json"]))),
            created_at_ms=int(row["created_at_ms"]),
        )


__all__ = ["PlanConflictError", "PlanNotFoundError", "PlanStore", "PlanStoreError"]

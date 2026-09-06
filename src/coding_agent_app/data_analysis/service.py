"""Session-owned local worker jobs, durable results and explicit Workspace export."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from agent_workspace import WorkspacePublishChange, WorkspaceStore
from agent_workspace.analysis import (
    ANALYSIS_SCHEMA,
    MAX_RESULT_BYTES,
    AnalysisError,
    AnalysisJobRequest,
    PythonAnalysisRequest,
    parse_analysis_request,
)
from agent_workspace.store import FileAccessDeniedError, VirtualFileNotFoundError
from pi_agent_core_py.telemetry import (
    NOOP_TELEMETRY_CONTEXT,
    SpanOptions,
    SpanStatus,
    TelemetryContext,
    TelemetryError,
    TelemetrySpan,
)

from .python_runtime import PythonRuntime, stop_worker, worker_environment

PythonApproval = Callable[[str, str, PythonAnalysisRequest, dict[str, str]], Awaitable[bool]]

_POOLS: weakref.WeakKeyDictionary[Any, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_FINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_OUTPUTS = {"result.csv", "chart.png", "report.md", "manifest.json"}


def analysis_capability() -> dict[str, Any]:
    missing = [
        name
        for name in ("pandas", "matplotlib", "openpyxl", "pyarrow")
        if importlib.util.find_spec(name) is None
    ]
    reason = "Missing optional dependencies: " + ", ".join(missing) if missing else None
    if os.name not in {"nt", "posix"}:
        reason = "Worker resource limits are unsupported on this platform."
    return {"available": reason is None, "reason": reason}


class DataAnalysisService:
    def __init__(
        self,
        root: Path,
        store: WorkspaceStore,
        enabled: Callable[[str], Awaitable[bool]],
        *,
        timeout_seconds: float = 90,
        python_enabled: Callable[[str], Awaitable[bool]] | None = None,
        python_approval: PythonApproval | None = None,
        python_runtime: PythonRuntime | None = None,
        approval_timeout_seconds: float = 600,
    ) -> None:
        self._root = root.resolve()
        self._source_root = str(Path(__file__).resolve().parents[2])
        self._store = store
        self._enabled = enabled
        self._timeout = timeout_seconds
        self._python_enabled = python_enabled
        self._python_approval = python_approval
        self.python_runtime = python_runtime or PythonRuntime()
        self._approval_timeout = approval_timeout_seconds
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._deleted_sessions: set[str] = set()
        self._operation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self.telemetry: TelemetryContext = NOOP_TELEMETRY_CONTEXT

    def _operation_lock(self, session_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(session_id, asyncio.Lock())

    async def init(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = await aiosqlite.connect(self._root / "analysis.sqlite")
        db.row_factory = aiosqlite.Row
        self._db = db
        await db.execute("""CREATE TABLE IF NOT EXISTS analysis_runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
            request_json TEXT NOT NULL, result_json TEXT, error_code TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS analysis_session ON analysis_runs(session_id)")
        await db.execute(
            "UPDATE analysis_runs SET status='interrupted', error_code='server_restarted' "
            "WHERE status IN ('queued', 'running', 'awaiting_approval')"
        )
        await db.commit()
        # Sources are disposable copies, never the user's Workspace originals.
        cursor = await db.execute("SELECT id,status FROM analysis_runs")
        for row in await cursor.fetchall():
            directory = self._directory(row["id"])
            if row["status"] != "succeeded" and directory.exists():
                shutil.rmtree(directory)
                continue
            for name in (
                "source.csv",
                "source.tsv",
                "source.xlsx",
                "source.parquet",
                "request.json",
            ):
                (directory / name).unlink(missing_ok=True)
        await cursor.close()

    def _database(self) -> aiosqlite.Connection:
        if self._db is None:
            raise AnalysisError("analysis_unavailable")
        return self._db

    def _directory(self, run_id: str) -> Path:
        if re.fullmatch(r"analysis-[0-9a-f]{32}", run_id) is None:
            raise AnalysisError("analysis_not_found")
        path = (self._root / run_id).resolve()
        if path.parent != self._root:
            raise AnalysisError("analysis_not_found")
        return path

    async def _check_enabled(
        self,
        session_id: str,
        request: AnalysisJobRequest | None = None,
    ) -> None:
        if self._closed or session_id in self._deleted_sessions:
            raise AnalysisError("analysis_unavailable")
        if isinstance(request, PythonAnalysisRequest):
            if self._python_enabled is None or not await self._python_enabled(session_id):
                raise AnalysisError("python_analysis_disabled")
            if self._python_approval is None:
                raise AnalysisError("python_approval_unavailable")
            if not (await self.python_runtime.capability())["available"]:
                raise AnalysisError("python_runtime_unavailable")
            return
        if not await self._enabled(session_id):
            raise AnalysisError("analysis_disabled")
        if not analysis_capability()["available"]:
            raise AnalysisError("analysis_dependencies_missing")

    async def _record(self, session_id: str, run_id: str) -> dict[str, Any]:
        self._directory(run_id)
        async with self._lock:
            cursor = await self._database().execute(
                "SELECT * FROM analysis_runs WHERE id=? AND session_id=?",
                (run_id, session_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise AnalysisError("analysis_not_found")
        return dict(row)

    async def _update(
        self,
        run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._lock:
            db = self._database()
            await db.execute(
                "UPDATE analysis_runs SET status=?,result_json=?,error_code=?,updated_at=? "
                "WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    time.time(),
                    run_id,
                ),
            )
            await db.commit()

    async def start(self, session_id: str, request: AnalysisJobRequest) -> str:
        async with self._operation_lock(session_id):
            return await self._start(session_id, request)

    async def _start(self, session_id: str, request: AnalysisJobRequest) -> str:
        await self._check_enabled(session_id, request)
        max_request = 100_000 if isinstance(request, PythonAnalysisRequest) else 16_384
        if len(request.model_dump_json().encode("utf-8")) > max_request:
            raise AnalysisError("analysis_request_too_large")
        try:
            ref = await self._store.get_for_session(session_id, request.file_id)
        except (FileAccessDeniedError, VirtualFileNotFoundError):
            raise AnalysisError("source_not_found") from None
        if Path(ref.name).suffix.lower() not in {".csv", ".tsv", ".xlsx", ".parquet"}:
            raise AnalysisError("unsupported_format")
        if ref.size > 25 * 1024**2:
            raise AnalysisError("data_limit_exceeded")
        async with self._lock:
            db = self._database()
            cursor = await db.execute(
                "SELECT status,COUNT(*) AS n FROM analysis_runs WHERE session_id=? GROUP BY status",
                (session_id,),
            )
            statuses = {row["status"]: row["n"] for row in await cursor.fetchall()}
            await cursor.close()
            if any(
                statuses.get(status, 0) for status in ("queued", "running", "awaiting_approval")
            ):
                raise AnalysisError("analysis_already_running")
            if sum(statuses.values()) >= 20:
                raise AnalysisError("analysis_history_limit_delete_old_runs")
            run_id = f"analysis-{uuid4().hex}"
            now = time.time()
            await db.execute(
                "INSERT INTO analysis_runs VALUES (?,?,?, ?,NULL,NULL,?,?)",
                (run_id, session_id, "queued", request.model_dump_json(), now, now),
            )
            await db.commit()
        task = asyncio.create_task(self._observed_run(session_id, run_id, request, ref.sha256))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return run_id

    async def analyze(
        self,
        session_id: str,
        request: AnalysisJobRequest,
        *,
        signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if signal is not None and signal.is_set():
            raise AnalysisError("analysis_cancelled")
        run_id = await self.start(session_id, request)
        try:
            while run_id in self._tasks:
                if signal is not None and signal.is_set():
                    await self.cancel(session_id, run_id)
                    raise AnalysisError("analysis_cancelled")
                await asyncio.sleep(0.1)
            record = await self.get(session_id, run_id)
            if record["status"] != "succeeded":
                details = {"run_id": run_id}
                if record["result"]:
                    details.update(
                        {
                            key: str(record["result"][key])[:16_500]
                            for key in ("stdout", "python_error")
                            if record["result"].get(key)
                        }
                    )
                raise AnalysisError(record["error_code"] or "analysis_failed", details)
            return dict(record["result"])
        except asyncio.CancelledError:
            await self.cancel(session_id, run_id)
            raise

    async def _observed_run(
        self,
        session_id: str,
        run_id: str,
        request: AnalysisJobRequest,
        source_sha256: str,
    ) -> None:
        async def compute(span: TelemetrySpan) -> bool:
            await self._run(session_id, run_id, request, source_sha256)
            record = await self.get(session_id, run_id)
            span.set_attributes({"analysis_status": record["status"]})
            if record["result"]:
                span.set_attributes({"rows_processed": record["result"]["analyzed_rows"]})
            if record["status"] != "succeeded":
                span.set_status(SpanStatus("error", TelemetryError(record["error_code"])))
            return True

        _settled: bool = await self.telemetry.start_span(
            SpanOptions(
                "data_analysis.run",
                {
                    "session_id": session_id,
                    "analysis_action": request.action,
                    "tool_name": "run_python_analysis"
                    if isinstance(request, PythonAnalysisRequest)
                    else "analyze_data",
                },
            ),
            compute,
        )

    async def _run(
        self,
        session_id: str,
        run_id: str,
        request: AnalysisJobRequest,
        source_sha256: str,
    ) -> None:
        directory = self._directory(run_id)
        process: asyncio.subprocess.Process | None = None
        try:
            if isinstance(request, PythonAnalysisRequest):
                if self._python_approval is None:
                    raise AnalysisError("python_approval_unavailable")
                ref = await self._store.get_for_session(session_id, request.file_id)
                if ref.sha256 != source_sha256:
                    raise AnalysisError("source_changed")
                await self._update(run_id, "awaiting_approval")
                try:
                    async with asyncio.timeout(self._approval_timeout):
                        approved = await self._python_approval(
                            session_id,
                            run_id,
                            request,
                            {
                                "source_name": ref.name,
                                "source_logical_path": ref.logical_path,
                                "source_sha256": source_sha256,
                                "code_sha256": hashlib.sha256(request.code.encode()).hexdigest(),
                            },
                        )
                except TimeoutError:
                    raise AnalysisError("python_approval_timeout") from None
                if approved is not True:
                    raise AnalysisError("python_execution_denied")
            async with asyncio.timeout(self._timeout):
                loop = asyncio.get_running_loop()
                semaphore = _POOLS.setdefault(loop, asyncio.Semaphore(2))
                async with semaphore:
                    await self._check_enabled(session_id, request)
                    await self._update(run_id, "running")
                    ref = await self._store.get_for_session(session_id, request.file_id)
                    if ref.sha256 != source_sha256:
                        raise AnalysisError("source_changed")
                    directory.mkdir(mode=0o700)
                    source = directory / f"source{Path(ref.name).suffix.lower()}"

                    # Recheck bytes against metadata; a worker never sees unrelated files.
                    def copy_source() -> None:
                        with Path(ref.path).open("rb") as stream:
                            data = stream.read(25 * 1024**2 + 1)
                        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
                            raise AnalysisError("source_changed")
                        source.write_bytes(data)

                    copy_task = asyncio.create_task(asyncio.to_thread(copy_source))
                    try:
                        await asyncio.shield(copy_task)
                    except asyncio.CancelledError:
                        # A Python thread cannot be killed: finish its bounded local copy
                        # before cleanup removes the destination directory.
                        await copy_task
                        raise
                    payload = {"source_path": str(source), "request": request.model_dump()}
                    request_path = directory / "request.json"
                    request_path.write_text(
                        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                    )
                    environment = worker_environment(directory)
                    environment.update(
                        {
                            "PYTHONPATH": self._source_root,
                            "PYTHONIOENCODING": "utf-8",
                            "PYTHONUTF8": "1",
                            "PYTHONNOUSERSITE": "1",
                            "TEMP": str(directory),
                            "TMP": str(directory),
                            "TMPDIR": str(directory),
                            "MPLCONFIGDIR": str(directory / "mpl-cache"),
                            "OPENBLAS_NUM_THREADS": "1",
                            "OMP_NUM_THREADS": "1",
                            "MKL_NUM_THREADS": "1",
                            "NUMEXPR_NUM_THREADS": "1",
                        }
                    )
                    command = [sys.executable, "-m", "coding_agent_app.data_analysis.worker"]
                    if isinstance(request, PythonAnalysisRequest):
                        command = [
                            str(self.python_runtime.executable),
                            "-I",
                            "-B",
                            str(self.python_runtime.bootstrap),
                        ]
                        environment = worker_environment(directory)
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        str(request_path),
                        cwd=directory,
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=os.name == "posix",
                    )
                    assert process.stdout is not None
                    output = await process.stdout.read(MAX_RESULT_BYTES + 1)
                    # read(n) may return a short chunk before EOF.
                    while len(output) <= MAX_RESULT_BYTES:
                        chunk = await process.stdout.read(MAX_RESULT_BYTES + 1 - len(output))
                        if not chunk:
                            break
                        output += chunk
                    if len(output) > MAX_RESULT_BYTES:
                        raise AnalysisError("result_limit_exceeded")
                    returncode = await process.wait()
                    if returncode != 0:
                        raise AnalysisError("worker_failed_or_resource_limit")
                    result = json.loads(output)
                    if not isinstance(result, dict):
                        raise AnalysisError("invalid_worker_result")
                    if "error_code" in result:
                        raise AnalysisError(str(result["error_code"]))
                    if isinstance(request, PythonAnalysisRequest):
                        self._validate_python_result(result)
                        result["code"] = request.code
                        result["code_sha256"] = hashlib.sha256(request.code.encode()).hexdigest()
                    result.update(
                        {
                            "schema_version": ANALYSIS_SCHEMA,
                            "run_id": run_id,
                            "session_id": session_id,
                            "action": request.action,
                            "file_id": ref.id,
                            "source_sha256": ref.sha256,
                            "source_name": ref.name,
                            "source_logical_path": ref.logical_path,
                            "chart_url": f"/api/workspaces/{session_id}/analysis/{run_id}/chart"
                            if result["has_chart"]
                            else None,
                        }
                    )
                    if result.get("python_error"):
                        await self._update(
                            run_id, "failed", result=result, error="python_execution_failed"
                        )
                        return
                    # Approved code is not OS-sandboxed, but never follow output links
                    # or special files when assembling an export on the host.
                    for name in _OUTPUTS:
                        output_path = directory / name
                        if output_path.is_symlink() or (
                            output_path.exists()
                            and (
                                not output_path.is_file()
                                or output_path.resolve().parent != directory
                            )
                        ):
                            raise AnalysisError("invalid_worker_output")
                    report = "# Data analysis\n\nComputed results, not model interpretation.\n\n"
                    report += (
                        "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```\n"
                    )
                    (directory / "report.md").write_text(report, encoding="utf-8")
                    files = {}
                    for name in ("report.md", "result.csv", "chart.png"):
                        path = directory / name
                        if path.exists():
                            size = path.stat().st_size
                            if size > 5 * 1024**2:
                                raise AnalysisError("result_limit_exceeded")
                            files[name] = {
                                "size": size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            }
                    manifest = {
                        "schema_version": ANALYSIS_SCHEMA,
                        "run_id": run_id,
                        "source_file_id": ref.id,
                        "source_sha256": ref.sha256,
                        "request": request.model_dump(),
                        "outputs": files,
                        "engine": "approved-python/v1"
                        if isinstance(request, PythonAnalysisRequest)
                        else "fixed-pandas/v1",
                        "engine_versions": result["engine_versions"],
                        "created_at": time.time(),
                    }
                    (directory / "manifest.json").write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    result["output_hashes"] = {
                        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in (*files, "manifest.json")
                    }
                    await self._update(run_id, "succeeded", result=result)
        except TimeoutError:
            await self._update(run_id, "failed", error="analysis_timeout")
        except asyncio.CancelledError:
            await self._update(run_id, "cancelled", error="analysis_cancelled")
        except Exception as exc:
            await self._update(
                run_id,
                "failed",
                error=exc.code if isinstance(exc, AnalysisError) else "analysis_failed",
            )
        finally:
            if process is not None:
                await stop_worker(process)
            for name in (
                "source.csv",
                "source.tsv",
                "source.xlsx",
                "source.parquet",
                "request.json",
            ):
                (directory / name).unlink(missing_ok=True)
            record = await self._record(session_id, run_id)
            if record["status"] != "succeeded" and directory.exists():
                shutil.rmtree(directory)

    async def get(self, session_id: str, run_id: str) -> dict[str, Any]:
        row = await self._record(session_id, run_id)
        return {
            "id": run_id,
            "session_id": session_id,
            "status": row["status"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    async def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._database().execute(
                "SELECT id,status,error_code,created_at,request_json FROM analysis_runs "
                "WHERE session_id=? ORDER BY created_at DESC LIMIT 20",
                (session_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            await cursor.close()
        for row in rows:
            row["action"] = json.loads(row.pop("request_json"))["action"]
        return rows

    async def cancel(self, session_id: str, run_id: str) -> dict[str, Any]:
        await self._record(session_id, run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            record = await self._record(session_id, run_id)
            if record["status"] not in _FINAL:
                await self._update(run_id, "cancelled", error="analysis_cancelled")
        return await self.get(session_id, run_id)

    async def output(self, session_id: str, run_id: str, name: str) -> Path:
        record = await self.get(session_id, run_id)
        if record["status"] != "succeeded" or name not in _OUTPUTS:
            raise AnalysisError("analysis_output_unavailable")
        path = self._directory(run_id) / name
        expected = record["result"].get("output_hashes", {}).get(name)
        if not expected or not path.is_file() or path.stat().st_size > 5 * 1024**2:
            raise AnalysisError("analysis_output_unavailable")
        if path.resolve().parent != self._directory(run_id):
            raise AnalysisError("analysis_output_unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise AnalysisError("analysis_output_changed")
        return path

    async def save(self, session_id: str, run_id: str) -> dict[str, Any]:
        async with self._operation_lock(session_id):
            return await self._save(session_id, run_id)

    async def _save(self, session_id: str, run_id: str) -> dict[str, Any]:
        record = await self.get(session_id, run_id)
        await self._check_enabled(session_id, parse_analysis_request(record["request"]))
        if record["status"] != "succeeded":
            raise AnalysisError("analysis_not_succeeded")
        result = record["result"]
        changes = []
        existing = []
        for name, digest in result["output_hashes"].items():
            source = await self.output(session_id, run_id, name)
            logical_path = f"artifacts/analysis/{run_id}/{name}"
            ref = await self._store.get_by_logical_path(session_id, logical_path)
            if ref is not None:
                if ref.sha256 != digest:
                    raise AnalysisError("saved_output_changed")
                existing.append(ref)
            changes.append(
                WorkspacePublishChange(
                    logical_path=logical_path,
                    source_path=source,
                    size=source.stat().st_size,
                    sha256=digest,
                )
            )
        if existing and len(existing) != len(changes):
            raise AnalysisError("saved_output_changed")
        if not existing:
            published = await self._store.publish_analysis_result(
                session_id,
                run_id=run_id,
                source_file_id=result["file_id"],
                source_sha256=result["source_sha256"],
                changes=tuple(changes),
            )
            refs = [
                await self._store.get_by_logical_path(session_id, path)
                for path in published.changed_paths
            ]
        else:
            refs = list(existing)
        return {
            "run_id": run_id,
            "saved": True,
            "files": [
                ref.model_dump(exclude={"path"})
                for ref in sorted(
                    (ref for ref in refs if ref is not None),
                    key=lambda ref: ref.logical_path,
                )
            ],
        }

    @staticmethod
    def _validate_python_result(result: dict[str, Any]) -> None:
        """Only bounded JSON primitives are accepted back from approved code."""
        columns, rows = result.get("columns"), result.get("rows")
        if (
            not isinstance(columns, list)
            or len(columns) > 256
            or any(not isinstance(c, str) or len(c) > 200 for c in columns)
            or not isinstance(rows, list)
            or len(rows) > 50
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)
            or any(
                v is not None and not isinstance(v, (str, int, float, bool))
                for row in rows
                for v in row
            )
            or not isinstance(result.get("source"), dict)
            or not isinstance(result.get("engine_versions"), dict)
            or not isinstance(result.get("warnings"), list)
            or len(result["warnings"]) > 50
            or any(not isinstance(w, str) or len(w) > 2000 for w in result["warnings"])
            or not isinstance(result.get("has_chart"), bool)
            or any(
                not isinstance(result.get(k), str) or len(result[k]) > 16_500
                for k in ("stdout", "null_policy", "numeric_policy")
            )
            or (
                result.get("python_error") is not None
                and (
                    not isinstance(result["python_error"], str)
                    or len(result["python_error"]) > 2000
                )
            )
            or any(
                type(result.get(k)) is not int or not 0 <= result[k] <= 200_000
                for k in (
                    "source_rows",
                    "analyzed_rows",
                    "result_rows",
                    "exported_rows",
                    "preview_rows",
                )
            )
        ):
            raise AnalysisError("invalid_worker_result")

    async def delete(self, session_id: str, run_id: str) -> None:
        async with self._operation_lock(session_id):
            await self._delete(session_id, run_id)

    async def _delete(self, session_id: str, run_id: str) -> None:
        await self.cancel(session_id, run_id)
        async with self._lock:
            db = self._database()
            await db.execute(
                "DELETE FROM analysis_runs WHERE id=? AND session_id=?", (run_id, session_id)
            )
            await db.commit()
        directory = self._directory(run_id)
        if directory.exists():
            shutil.rmtree(directory)

    async def delete_session(self, session_id: str) -> None:
        async with self._operation_lock(session_id):
            self._deleted_sessions.add(session_id)
            for run in await self.list_runs(session_id):
                await self._delete(session_id, run["id"])

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._db is not None:
            await self._db.close()
            self._db = None

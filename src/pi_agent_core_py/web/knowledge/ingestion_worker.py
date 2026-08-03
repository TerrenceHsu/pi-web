"""Bounded Ingestion Worker Manager (P2-R2-C2).

App-scoped singleton that drives the R2-C Ingestion pipeline:

::

    FastAPI lifespan startup
        ↓
    IngestionWorkerManager.start()
        ↓
    startup recovery (interrupted Jobs/Documents → failed)
        ↓
    worker_loop task (single asyncio.Task)
        ↓
    wait for wake hint OR poll-interval
        ↓
    drain phase: claim_next_pending_document() → orchestrator.run_claimed_job(job_id)
        ↓ (loop until no pending)
    back to wait

Per C0 contract (frozen @ 6476f96 + post-freeze correction @ 0a8f554):

- §7 SQLite = durable source of truth; ``asyncio.Queue`` is wake hint only
- §14 worker_concurrency = 1, queue_capacity = 32, poll_interval / shutdown_grace
- §15 startup recovery: pending stays; running → failed + ``ingestion_interrupted``
- §16 graceful shutdown: 30s grace; conditional UPDATE prevents late-write race

The Worker Manager does NOT:

- import FastAPI / APIRouter / UploadFile
- write to ``knowledge_chunks`` / FTS5 (R3)
- register Agent Tools (R3)
- create Documents / Jobs (C3 API scope)
- accept ``session_id`` / ``library_id`` / ``generated_at`` parameters
- own multiple workers (concurrency frozen at 1)
- spawn cross-process workers

The Manager is a *friend* of :class:`KnowledgeStore` (accesses ``_db`` /
``_write_lock`` / ``_require_db``) for the C2-specific recovery primitive
that conditionally fails Documents mid-shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from .ingestion_orchestrator import IngestionOrchestrator, IngestionRunResult
from .ingestion_store import (
    INGESTION_INTERRUPTED,
    ClaimedJob,
    IngestionStore,
)
from .models import validate_document_id_or_raise
from .pdf_parser import PdfParser
from .store import KnowledgeStore, KnowledgeStoreError

if TYPE_CHECKING:
    pass


# ============================================================================
# Constants (per C0 §14 / §16)
# ============================================================================

#: Single-worker concurrency (MVP frozen).
DEFAULT_WORKER_CONCURRENCY: Final[int] = 1

#: Wake queue capacity (MVP frozen). Queue stores ``None`` sentinels —
#: bounded backlog of "please scan DB" hints.
DEFAULT_WAKE_QUEUE_CAPACITY: Final[int] = 32

#: DB polling fallback interval. Prevents lost wakeups / startup races
#: from stranding pending Jobs.
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 30.0

#: Graceful shutdown grace period. Current Job given this long to finish.
DEFAULT_SHUTDOWN_GRACE_SECONDS: Final[float] = 30.0

#: Safe error code recorded on interrupted (crash recovery) Jobs + Documents.
_SAFE_CODE_INTERRUPTED: Final[str] = INGESTION_INTERRUPTED


# ============================================================================
# Manager state enum
# ============================================================================


WorkerState = Literal["stopped", "starting", "running", "stopping", "failed"]
_VALID_STATES: Final[frozenset[str]] = frozenset(
    {"stopped", "starting", "running", "stopping", "failed"}
)


# ============================================================================
# Snapshot DTO (per directive §二十五)
# ============================================================================


@dataclass(frozen=True, slots=True)
class IngestionWorkerSnapshot:
    """Read-only Manager state snapshot.

    Safe for C3 API response / log. Excludes:

    - absolute paths
    - raw exceptions
    - PDF / Markdown body
    - asyncio.Task / sqlite connection references
    """

    state: WorkerState
    active_job_id: str | None
    active_document_id: str | None
    pending_wake_signals: int
    last_error_code: str | None
    last_event_at: int  # ms epoch of last claim / run / recover / stop event


# ============================================================================
# IngestionWorkerManager
# ============================================================================


class IngestionWorkerManager:
    """App-scoped singleton that drives R2-C ingestion.

    Typical lifecycle::

        manager = IngestionWorkerManager(
            orchestrator=orchestrator,
            ingestion_store=ingestion_store,
            store=knowledge_store,
            parser=pypdf_parser,
        )
        await manager.start()         # startup recovery + worker_loop task
        ...
        manager.notify_pending_job()  # call after creating new pending Job
        ...
        await manager.stop()          # graceful shutdown + parser.close()

    The Manager is **not** safe to share across multiple FastAPI apps or
    event loops. One app ↔ one Manager ↔ one worker task.
    """

    def __init__(
        self,
        *,
        orchestrator: IngestionOrchestrator,
        ingestion_store: IngestionStore,
        store: KnowledgeStore,
        parser: PdfParser,
        worker_concurrency: int = DEFAULT_WORKER_CONCURRENCY,
        wake_queue_capacity: int = DEFAULT_WAKE_QUEUE_CAPACITY,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
        owns_parser: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        if worker_concurrency != 1:
            raise ValueError(
                f"worker_concurrency must be 1 (MVP); got {worker_concurrency}"
            )
        if wake_queue_capacity <= 0:
            raise ValueError("wake_queue_capacity must be > 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds must be >= 0")

        self._orchestrator = orchestrator
        self._ingestion_store = ingestion_store
        self._store = store
        self._parser = parser
        self._owns_parser = owns_parser
        self._logger = logger or logging.getLogger(__name__)

        self._worker_concurrency = worker_concurrency
        self._wake_queue_capacity = wake_queue_capacity
        self._poll_interval_seconds = poll_interval_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds

        # State (single-threaded asyncio; no lock needed for state field)
        self._state: WorkerState = "stopped"
        self._worker_task: asyncio.Task | None = None
        self._stop_requested: bool = False
        self._active_job_id: str | None = None
        self._active_document_id: str | None = None
        self._last_error_code: str | None = None
        self._last_event_at: int = _now_ms()
        # Wake queue: bounded sentinel queue. Tests can inject a custom one.
        self._wake_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=wake_queue_capacity)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        return self._state

    def snapshot(self) -> IngestionWorkerSnapshot:
        """Read-only Manager snapshot (safe for API / log)."""
        return IngestionWorkerSnapshot(
            state=self._state,
            active_job_id=self._active_job_id,
            active_document_id=self._active_document_id,
            pending_wake_signals=self._wake_queue.qsize(),
            last_error_code=self._last_error_code,
            last_event_at=self._last_event_at,
        )

    async def start(self) -> None:
        """Perform startup recovery + launch worker_loop task.

        Idempotent: calling ``start()`` when already ``running`` is a no-op
        (does NOT spawn a second worker task).
        """
        if self._state in ("running", "starting"):
            self._logger.debug("start() called when state=%s; no-op", self._state)
            return
        if self._state == "stopping":
            raise RuntimeError("cannot start() while stopping; await stop() first")

        self._state = "starting"
        self._stop_requested = False
        self._last_event_at = _now_ms()

        # 1. Startup recovery (per C0 §15)
        try:
            await self._recover_interrupted_state()
        except Exception as exc:
            self._state = "failed"
            self._last_error_code = _safe_exc_code(exc)
            self._logger.exception("ingestion worker startup recovery failed")
            raise

        # 2. Launch worker_loop task
        self._state = "running"
        self._worker_task = asyncio.create_task(
            self._run_loop(), name="ingestion_worker_loop"
        )
        self._last_event_at = _now_ms()

    async def stop(self) -> None:
        """Graceful shutdown with bounded grace period.

        Idempotent: calling ``stop()`` when already ``stopped`` is a no-op.
        """
        if self._state == "stopped":
            return
        if self._state == "starting":
            raise RuntimeError("cannot stop() while start() is in progress")

        if self._state == "stopping":
            # Already stopping; wait for transition to stopped
            await self._await_worker_task_or_cancel(force_after_grace=False)
            return

        self._state = "stopping"
        self._stop_requested = True
        self._last_event_at = _now_ms()

        # Wake any waiting worker so it sees stop_requested
        self._wake_all()

        # Wait for current Job to complete (bounded by grace)
        await self._await_worker_task_or_cancel(force_after_grace=True)

        # Close parser resources (best-effort)
        if self._owns_parser:
            try:
                self._parser.close()
            except Exception:
                self._logger.exception("parser.close() failed during shutdown")

        self._active_job_id = None
        self._active_document_id = None
        self._state = "stopped"
        self._last_event_at = _now_ms()

    def notify_pending_job(self) -> bool:
        """Wake-up hint. Called after creating a new pending Job (C3 API).

        Returns:
            ``True`` if the wake signal was accepted.
            ``False`` if coalesced (queue already full) — the durable
            pending Job remains and will be picked up by polling fallback.

        Raises:
            RuntimeError: if Manager is not ``running`` (caller bug).
        """
        if self._state != "running":
            raise RuntimeError(
                f"notify_pending_job() called when state={self._state!r}; "
                "expected 'running'"
            )
        try:
            self._wake_queue.put_nowait(None)
            return True
        except asyncio.QueueFull:
            # Coalesce — pending Job will be picked up by polling fallback
            return False

    async def wait_until_idle(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        """Wait until Manager is idle (no active Job AND no claimable pending).

        Per directive §三十 — uses bounded wait; not busy-loop.

        Note: ``idle`` is defined as **no active Job AND no claimable pending
        (status='uploaded') Document in the DB**. Uses a non-claiming SELECT
        so it doesn't accidentally move Documents to 'extracting'.

        Returns:
            ``True`` if idle within ``timeout``; ``False`` on timeout.
        """
        deadline: float | None = None
        if timeout is not None:
            deadline = time.monotonic() + timeout

        while True:
            # Idle check: no active Job + no uploaded Documents
            if self._active_job_id is None:
                if not await self._has_uploaded_documents():
                    return True

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.05, remaining))
            else:
                await asyncio.sleep(0.05)

    async def _has_uploaded_documents(self) -> bool:
        """Non-claiming peek — returns True if any 'uploaded' Document exists.

        Friend access to KnowledgeStore (read-only SELECT).
        """
        db = self._store._require_db()
        async with db.execute(
            "SELECT 1 FROM knowledge_documents "
            "WHERE status = 'uploaded' LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Internal: worker loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main worker loop — drains pending work then waits for wake/poll."""
        try:
            while not self._stop_requested:
                # Drain phase: process all pending work
                await self._drain_pending()

                if self._stop_requested:
                    break

                # Consume accumulated wake signals (so they don't re-trigger)
                await self._drain_wake_queue()

                # Wait phase: bounded by poll interval
                await self._wait_for_wake_or_poll()
        except asyncio.CancelledError:
            self._logger.debug("worker_loop cancelled")
            raise
        except Exception as exc:
            # Unrecoverable worker_loop failure — Manager enters 'failed'
            self._state = "failed"
            self._last_error_code = _safe_exc_code(exc)
            self._logger.exception("worker_loop exited with unhandled exception")

    async def _drain_pending(self) -> None:
        """Claim + run pending Jobs until none remain or stop requested."""
        while not self._stop_requested:
            try:
                claimed = await self._ingestion_store.claim_next_pending_document()
            except KnowledgeStoreError as exc:
                # Transient Store error — log + return (loop will retry on next wake/poll)
                self._last_error_code = _safe_exc_code(exc)
                self._logger.warning(
                    "claim_next_pending_document failed: %s", type(exc).__name__
                )
                return

            if claimed is None:
                return

            self._active_job_id = claimed.job.id
            self._active_document_id = claimed.document.id
            self._last_event_at = _now_ms()
            try:
                await self._run_claimed_job_safely(claimed)
            finally:
                self._active_job_id = None
                self._active_document_id = None
                self._last_event_at = _now_ms()

    async def _run_claimed_job_safely(self, claimed: ClaimedJob) -> None:
        """Run one Job via Orchestrator; isolate failures per-Job.

        Per directive §十八 — Orchestrator owns business terminal state;
        Worker only handles cancellation + unknown-exception compensation.
        """
        job_id = claimed.job.id
        try:
            await self._orchestrator.run_claimed_job(job_id)
        except asyncio.CancelledError:
            # Worker task cancelled (shutdown grace expired). Best-effort
            # compensation via conditional UPDATE; late Orchestrator
            # completion is mitigated by C2's conditional fail primitive.
            await self._compensate_active_job_on_cancel(
                job_id, claimed.document.id
            )
            raise
        except Exception as exc:
            # Unknown Orchestrator exception (shouldn't happen — C1 maps
            # everything). Best-effort compensation.
            self._last_error_code = _safe_exc_code(exc)
            self._logger.exception(
                "orchestrator.run_claimed_job raised unexpectedly; "
                "compensating Job %s",
                _safe_job_log(job_id),
            )
            await self._compensate_active_job_on_unknown_failure(
                job_id, claimed.document.id
            )

    async def _compensate_active_job_on_cancel(
        self, job_id: str, document_id: str
    ) -> None:
        """Best-effort compensation when CancelledError propagates.

        Per directive §二十一 — use conditional UPDATE so late Orchestrator
        completion (still in another thread) cannot overwrite our failed
        terminal state.
        """
        await self._conditional_fail_active(
            job_id=job_id,
            document_id=document_id,
            safe_error_code=_SAFE_CODE_INTERRUPTED,
        )

    async def _compensate_active_job_on_unknown_failure(
        self, job_id: str, document_id: str
    ) -> None:
        """Best-effort compensation for unexpected Orchestrator exception."""
        await self._conditional_fail_active(
            job_id=job_id,
            document_id=document_id,
            safe_error_code="internal_ingestion_error",
        )

    async def _drain_wake_queue(self) -> None:
        """Consume accumulated wake signals after a drain phase."""
        while True:
            try:
                self._wake_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _wait_for_wake_or_poll(self) -> None:
        """Wait for next wake signal OR poll-interval timeout."""
        try:
            await asyncio.wait_for(
                self._wake_queue.get(),
                timeout=self._poll_interval_seconds,
            )
        except TimeoutError:
            # Poll interval elapsed — fall through to drain phase to scan DB
            return

    def _wake_all(self) -> None:
        """Wake worker from wait state during shutdown."""
        for _ in range(self._wake_queue.maxsize):
            try:
                self._wake_queue.put_nowait(None)
            except asyncio.QueueFull:
                return

    async def _await_worker_task_or_cancel(
        self, *, force_after_grace: bool
    ) -> None:
        """Wait for worker_task to complete; optionally force-cancel after grace."""
        if self._worker_task is None:
            return
        try:
            if force_after_grace and self._shutdown_grace_seconds > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._worker_task),
                        timeout=self._shutdown_grace_seconds,
                    )
                except TimeoutError:
                    # Grace expired; force-cancel current Job + worker_loop
                    self._logger.warning(
                        "ingestion worker shutdown grace (%.1fs) expired; "
                        "force-cancelling active Job",
                        self._shutdown_grace_seconds,
                    )
                    # Conditional UPDATE before cancel so late completion
                    # cannot overwrite our 'failed' state.
                    if self._active_job_id and self._active_document_id:
                        await self._conditional_fail_active(
                            job_id=self._active_job_id,
                            document_id=self._active_document_id,
                            safe_error_code=_SAFE_CODE_INTERRUPTED,
                        )
                    self._worker_task.cancel()
                    try:
                        await self._worker_task
                    except (asyncio.CancelledError, Exception):
                        pass
            else:
                await self._worker_task
        except asyncio.CancelledError:
            # Outer caller cancelled us — propagate
            raise
        finally:
            self._worker_task = None

    # ------------------------------------------------------------------
    # Internal: startup recovery (per C0 §15)
    # ------------------------------------------------------------------

    async def _recover_interrupted_state(self) -> None:
        """Mark running Jobs + extracting/normalizing Documents as failed.

        Idempotent — second call affects 0 rows. Per C0 §15.2.
        """
        # 1. Mark all 'running' Jobs as 'failed' + ingestion_interrupted
        try:
            jobs_marked = await self._ingestion_store.mark_running_jobs_interrupted()
        except Exception as exc:
            self._last_error_code = _safe_exc_code(exc)
            raise

        # 2. Mark 'extracting' / 'normalizing' Documents as 'failed'
        try:
            docs_marked = await self._mark_active_documents_interrupted()
        except Exception as exc:
            self._last_error_code = _safe_exc_code(exc)
            raise

        if jobs_marked or docs_marked:
            self._logger.info(
                "startup recovery: marked %d job(s) + %d document(s) as "
                "interrupted (failed + ingestion_interrupted)",
                jobs_marked,
                docs_marked,
            )

    async def _mark_active_documents_interrupted(self) -> int:
        """Mark Documents in 'extracting'/'normalizing' as 'failed' + interrupted.

        Conditional UPDATE — only affects docs whose status hasn't already
        been changed by step 1 (e.g., docs whose Job was already terminal).

        Friend access to KnowledgeStore (same package).
        """
        db = self._store._require_db()
        now = _now_ms()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = 'failed', error_code = ?, updated_at = ? "
                    "WHERE status IN ('extracting', 'normalizing')",
                    (_SAFE_CODE_INTERRUPTED, now),
                )
                count = cur.rowcount
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return count

    # ------------------------------------------------------------------
    # Internal: conditional fail primitive (per directive §二十一 方案 B)
    # ------------------------------------------------------------------

    async def _conditional_fail_active(
        self,
        *,
        job_id: str,
        document_id: str,
        safe_error_code: str,
    ) -> None:
        """Atomically transition active Job+Document to 'failed' iff still active.

        Conditional UPDATE prevents late-Orchestrator completion (in another
        thread) from overwriting our failed terminal state:

        - Job: ``UPDATE WHERE id=? AND status='running'`` → rowcount==1 only
          if Job hasn't already moved to completed/failed
        - Document: ``UPDATE WHERE id=? AND status IN ('extracting','normalizing')``
          → rowcount==1 only if Document hasn't already been moved

        Friend access to KnowledgeStore (same package).
        """
        validate_document_id_or_raise(document_id)
        db = self._store._require_db()
        now = _now_ms()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # Conditional Job fail
                await db.execute(
                    "UPDATE knowledge_ingestion_jobs "
                    "SET status = 'failed', finished_at = ?, safe_error_code = ? "
                    "WHERE id = ? AND status = 'running'",
                    (now, safe_error_code, job_id),
                )
                # Conditional Document fail
                await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = 'failed', error_code = ?, updated_at = ? "
                    "WHERE id = ? AND status IN ('extracting', 'normalizing')",
                    (safe_error_code, now, document_id),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                # Don't re-raise — best-effort compensation
                self._logger.warning(
                    "conditional_fail_active failed for job=%s doc=%s; "
                    "recovery will reconcile on next startup",
                    _safe_job_log(job_id),
                    _safe_doc_log(document_id),
                )


# ============================================================================
# Module-private helpers
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_exc_code(exc: BaseException) -> str:
    """Map exception to safe_error_code (no path / body / traceback)."""
    # Check for known attribute (R2-A parser errors, R2-B persistence errors)
    code = getattr(exc, "safe_error_code", None)
    if isinstance(code, str) and code:
        return code
    return "internal_ingestion_error"


def _safe_job_log(job_id: str) -> str:
    """Sanitize job_id for log (allow only valid format)."""
    from .models import is_valid_job_id

    return job_id if is_valid_job_id(job_id) else "(invalid)"


def _safe_doc_log(document_id: str) -> str:
    """Sanitize document_id for log."""
    from .models import is_valid_document_id

    return document_id if is_valid_document_id(document_id) else "(invalid)"


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "DEFAULT_WORKER_CONCURRENCY",
    "DEFAULT_WAKE_QUEUE_CAPACITY",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SHUTDOWN_GRACE_SECONDS",
    "WorkerState",
    "IngestionWorkerSnapshot",
    "IngestionWorkerManager",
]


# Suppress unused-import lint for type-only re-exports.
_: tuple[object, ...] = (
    IngestionRunResult,
    ClaimedJob,
    KnowledgeStoreError,
)

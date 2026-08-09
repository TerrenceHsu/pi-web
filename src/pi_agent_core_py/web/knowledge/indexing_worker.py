"""Bounded Knowledge Index Worker (P2-R3-D1).

Per P2-R3-D startup directive §4 (D0 Composition Gate — Orchestrator is
the sole claim owner) + §7-§33 + §45-§51:

- Single IndexWorkerManager instance per process
- concurrency = 1 (one active indexing at a time)
- DB-driven: ``knowledge_documents.status='normalizing'`` is the durable
  source of truth; no in-memory queue
- Poll interval 2.0s (idle wait); no busy spin
- Shutdown grace 30s (safety-oriented; never closes Store while active)
- startup recovery BEFORE first scan
- immediate scan after start (no startup sleep)
- backlog drain without per-document sleep
- failure isolation (one bad Document does not kill the loop)
- import has zero side effects (no tasks / no DB / no threads)

Composition contract (D0):
- Worker discovers the next candidate via a **read-only** SELECT
  (status='normalizing', ORDER BY created_at ASC, id ASC).
- Worker then calls ``await orchestrator.process_document(id)``.
- The Orchestrator performs the atomic claim (T1). Worker NEVER calls
  ``IndexingStore.claim_next_normalizing_document`` because that would
  double-claim (status would move to ``chunking`` before the
  Orchestrator's T1, causing process_document to return a controlled
  "not in normalizing" result — wasteful but correct; avoided here by
  the read-only discovery pattern).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .chunk_store import ChunkStore
from .indexing_orchestrator import IndexingOrchestrator
from .indexing_store import IndexingStore
from .store import KnowledgeStore

if TYPE_CHECKING:
    pass

# ============================================================================
# Constants — frozen per directive §8
# ============================================================================

#: Hard concurrency cap. MVP = 1 — single local SQLite + deterministic
#: ordering + simple recovery.
INDEXING_WORKER_CONCURRENCY: Final[int] = 1

#: Idle poll interval (seconds). When no candidate is found, wait this
#: long before polling again. Not a busy spin.
INDEXING_POLL_INTERVAL_SECONDS: Final[float] = 2.0

#: Soft shutdown grace (seconds). If the in-flight ``process_document``
#: finishes within this window, graceful stop. Otherwise the manager
#: waits beyond the grace rather than risking Store close while the
#: worker is still accessing it (per directive §30).
INDEXING_SHUTDOWN_GRACE_SECONDS: Final[float] = 30.0


_LOGGER = logging.getLogger(__name__)


# ============================================================================
# Snapshot DTO
# ============================================================================


@dataclass(frozen=True)
class IndexingWorkerSnapshot:
    """Immutable point-in-time snapshot of Manager state.

    Useful for diagnostics, tests, and the future (R3-E+) status API.
    Contains no paths / SQL / secrets.
    """

    state: str  # "stopped" | "starting" | "running" | "stopping" | "failed"
    active_document_id: str | None
    last_event_at: int  # ms epoch
    last_error_code: str | None


# ============================================================================
# IndexingWorkerManager
# ============================================================================


class IndexingWorkerManager:
    """Bounded single-concurrency Index Worker.

    Construction::

        knowledge_store = await KnowledgeStore.open(db_path)
        knowledge_file_store = KnowledgeFileStore(knowledge_root)
        chunk_store = ChunkStore(knowledge_store)
        indexing_store = IndexingStore(knowledge_store)
        orchestrator = IndexingOrchestrator(
            knowledge_store=knowledge_store,
            knowledge_file_store=knowledge_file_store,
            chunk_store=chunk_store,
            indexing_store=indexing_store,
        )
        manager = IndexingWorkerManager(
            knowledge_store=knowledge_store,
            chunk_store=chunk_store,
            indexing_store=indexing_store,
            orchestrator=orchestrator,
        )
        await manager.start()  # recovery + worker_loop task
        ...
        await manager.stop()   # graceful shutdown (bounded by grace)

    Thread-safety: single-thread asyncio only. The Manager is not
    designed for cross-process deployment; the SQLite conditional claim
    in ``IndexingOrchestrator.process_document`` keeps two stray
    Managers safe, but the contract is one Manager per process.
    """

    def __init__(
        self,
        *,
        knowledge_store: KnowledgeStore,
        chunk_store: ChunkStore,
        indexing_store: IndexingStore,
        orchestrator: IndexingOrchestrator,
        poll_interval_seconds: float = INDEXING_POLL_INTERVAL_SECONDS,
        shutdown_grace_seconds: float = INDEXING_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._knowledge_store = knowledge_store
        self._chunk_store = chunk_store
        self._indexing_store = indexing_store
        self._orchestrator = orchestrator
        self._poll_interval = poll_interval_seconds
        self._shutdown_grace = shutdown_grace_seconds

        self._state: str = "stopped"
        self._stop_requested: asyncio.Event = asyncio.Event()
        self._wake_event: asyncio.Event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._active_document_id: str | None = None
        self._last_event_at: int = _now_ms()
        self._last_error_code: str | None = None

    # ------------------------------------------------------------------
    # Public — lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Startup recovery + launch worker_loop task.

        Sequence:
          1. ``recover_interrupted_indexing`` (cleans stale chunking /
             indexing Documents; uses ``ChunkStore.delete_document_chunks``
             as the cleanup callback). Failure → state='failed' and raise.
          2. ``create_task(_run_loop)``.

        Idempotent: calling start() while running/starting is a no-op.
        """
        if self._state in ("running", "starting"):
            _LOGGER.debug("start() called when state=%s; no-op", self._state)
            return
        if self._state == "stopping":
            raise RuntimeError("cannot start() while stopping; await stop() first")

        self._state = "starting"
        self._stop_requested.clear()
        self._wake_event.clear()
        self._last_event_at = _now_ms()

        # 1. Startup recovery (per directive §17 — BEFORE first scan).
        # The recovery primitive invokes the cleanup callback INSIDE its
        # own BEGIN IMMEDIATE transaction while holding the parent
        # KnowledgeStore._write_lock. asyncio.Lock is non-reentrant, so
        # we cannot call ChunkStore.delete_document_chunks here (it
        # would re-acquire the same lock and deadlock). Instead we
        # execute the chunk+FTS cleanup SQL directly on the same
        # connection — the recovery primitive owns the COMMIT/ROLLBACK.
        async def _cleanup(doc_id: str) -> None:
            db = self._knowledge_store._require_db()
            await db.execute(
                "DELETE FROM knowledge_chunks_fts WHERE document_id = ?",
                (doc_id,),
            )
            await db.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = ?",
                (doc_id,),
            )

        try:
            await self._indexing_store.recover_interrupted_indexing(
                cleanup_callback=_cleanup
            )
        except Exception as exc:
            self._state = "failed"
            self._last_error_code = _safe_exc_code(exc)
            _LOGGER.exception("indexing worker startup recovery failed")
            raise

        # 2. Launch worker_loop task.
        self._state = "running"
        self._worker_task = asyncio.create_task(
            self._run_loop(), name="indexing_worker_loop"
        )
        self._last_event_at = _now_ms()

    async def stop(self) -> None:
        """Graceful shutdown bounded by ``shutdown_grace_seconds``.

        Idempotent: calling stop() while already stopped is a no-op.
        Cancellation is propagated correctly (no ``except Exception``
        swallowing ``CancelledError``).
        """
        if self._state == "stopped":
            return
        if self._state == "starting":
            raise RuntimeError("cannot stop() while start() is in progress")

        if self._state == "stopping":
            # Already stopping; await completion.
            await self._await_worker_task_or_force()
            return

        self._state = "stopping"
        self._stop_requested.set()
        self._wake_event.set()  # wake any idle wait
        self._last_event_at = _now_ms()

        await self._await_worker_task_or_force()

        self._active_document_id = None
        self._state = "stopped"
        self._last_event_at = _now_ms()

    def notify_normalizing_available(self) -> bool:
        """Wake-up hint. Called after R2 commits a new ``normalizing``
        Document (optional optimisation; the worker also polls every
        ``poll_interval_seconds``).

        Returns ``True`` if the wake was accepted; ``False`` if the
        manager is not ``running`` (caller may ignore — polling covers
        it).
        """
        if self._state != "running":
            return False
        self._wake_event.set()
        return True

    def snapshot(self) -> IndexingWorkerSnapshot:
        """Return an immutable point-in-time snapshot (no paths / SQL)."""
        return IndexingWorkerSnapshot(
            state=self._state,
            active_document_id=self._active_document_id,
            last_event_at=self._last_event_at,
            last_error_code=self._last_error_code,
        )

    # ------------------------------------------------------------------
    # Internal — worker loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main loop: drain backlog, then idle-poll until stopped.

        Per directive §12 / §13: success → continue immediately (no
        per-document sleep); no candidate → wait ``poll_interval``
        seconds OR until woken, whichever comes first.
        """
        try:
            while not self._stop_requested.is_set():
                candidate_id = await self._discover_next_candidate()
                if candidate_id is None:
                    # Idle: wait for poll_interval OR wake OR stop.
                    await self._idle_wait()
                    continue

                # Failure isolation: any exception inside
                # process_document is caught at this boundary so the
                # loop survives. ``IndexingOrchestrator.process_document``
                # never raises for runtime failures (returns
                # ``IndexingResult`` instead), so this except is for
                # truly unexpected cases (store closed, etc.).
                self._active_document_id = candidate_id
                self._last_event_at = _now_ms()
                try:
                    await self._orchestrator.process_document(candidate_id)
                except asyncio.CancelledError:
                    # Propagate cancellation (per directive §31).
                    raise
                except Exception as exc:
                    # Defensive: orchestrator should not raise, but if it
                    # does, log + record safe error code and continue.
                    self._last_error_code = _safe_exc_code(exc)
                    _LOGGER.exception(
                        "indexing worker: unexpected exception for document %s",
                        _safe_doc_id_for_log(candidate_id),
                    )
                finally:
                    self._active_document_id = None
                    self._last_event_at = _now_ms()
                # Continue immediately — backlog drain without sleep.
        except asyncio.CancelledError:
            # Worker task cancelled. State remains discoverable; status
            # of the in-flight Document (if any) is left in chunking /
            # indexing for the next startup recovery pass.
            _LOGGER.debug("indexing worker_loop cancelled")
            raise

    async def _idle_wait(self) -> None:
        """Wait up to ``poll_interval`` for wake OR stop.

        Uses a short-granularity sleep loop so that ``stop()`` and
        ``notify_normalizing_available()`` are observed within a few
        milliseconds. This avoids the subtle interactions between
        ``asyncio.wait_for`` + nested ``asyncio.wait`` + ``Event.wait``
        that can leave dangling futures. The loop is bounded by
        ``poll_interval``, so worst-case CPU cost is one cheap
        ``asyncio.sleep`` per polling period.
        """
        deadline = _now_monotonic() + self._poll_interval
        while _now_monotonic() < deadline:
            if self._stop_requested.is_set() or self._wake_event.is_set():
                self._wake_event.clear()
                return
            await asyncio.sleep(0.02)

    async def _discover_next_candidate(self) -> str | None:
        """Read-only SELECT — does NOT mutate state.

        Returns the next ``normalizing`` Document ID by frozen order
        (``created_at ASC, id ASC``), or ``None`` if there is no
        candidate.

        This is the D0 Composition Gate's read-only discovery step. The
        atomic claim is performed by the Orchestrator's T1
        (``IndexingStore.claim_document``).
        """
        db = self._knowledge_store._require_db()
        async with db.execute(
            "SELECT id FROM knowledge_documents "
            "WHERE status = 'normalizing' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row is not None else None

    async def _await_worker_task_or_force(self) -> None:
        """Wait for ``_worker_task`` to finish, bounded by shutdown grace.

        Per directive §30: if the in-flight ``process_document`` does
        not finish within ``shutdown_grace``, we still do NOT close the
        Store — the manager waits beyond the grace rather than risk
        data corruption. Cancellation is used as a last resort to break
        the worker task out of an asyncio wait, but the underlying
        SQLite write (if any) is allowed to complete via the
        Orchestrator's atomic transactions.
        """
        if self._worker_task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(self._worker_task),
                timeout=self._shutdown_grace,
            )
        except TimeoutError:
            # Grace exceeded. Cancel the worker task — it will exit at
            # the next await boundary. Any in-flight Orchestrator
            # transaction either commits or rolls back atomically; the
            # Document remains in chunking/indexing for the next
            # startup recovery pass.
            _LOGGER.warning(
                "indexing worker shutdown grace (%.1fs) exceeded; cancelling",
                self._shutdown_grace,
            )
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        except asyncio.CancelledError:
            raise


# ============================================================================
# Helpers
# ============================================================================


def _now_ms() -> int:
    import time as _time

    return int(_time.time() * 1000)


def _now_monotonic() -> float:
    import time as _time

    return _time.monotonic()


def _safe_exc_code(exc: BaseException) -> str:
    """Return a stable, safe error code derived from exception type.

    Never includes exception args (which may contain paths / SQL /
    secrets). Returns the type name lowercased with ``error`` suffix
    stripped if present.
    """
    name = type(exc).__name__
    if name.endswith("Error"):
        name = name[: -len("Error")]
    snake = "".join(
        "_" + c.lower() if c.isupper() else c for c in name
    ).lstrip("_")
    return f"indexing_worker_{snake}"


def _safe_doc_id_for_log(doc_id: str) -> str:
    """Return a safe representation of doc_id for logging.

    doc_id is backend-generated and contains no user data, so it is
    safe to log as-is.
    """
    return doc_id

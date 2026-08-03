"""Ingestion atomic operations (P2-R2-C1).

Wraps :class:`KnowledgeStore` to provide ingestion-specific atomic
primitives required by the R2-C Ingestion Orchestrator and (in C2) the
WorkerManager.

Per C0 contract (frozen @ 6476f96 + post-freeze correction @ 0a8f554):

- §13 Atomic Job Claim — single SQLite ``BEGIN IMMEDIATE`` transaction
  per claim, conditional ``UPDATE`` with rowcount check, FIFO ordering
  by ``(created_at, id)``.
- §13.1 Active Job Uniqueness — same transaction does
  ``SELECT COUNT(*) WHERE status='running'`` then ``INSERT``;
  the in-process ``asyncio.Lock`` on ``KnowledgeStore`` plus SQLite's
  reserved lock double-serialize writers.
- §12 Retry Semantics — only ``failed`` Documents; new Job per retry;
  ``attempt`` auto-incremented by ``(document_id, stage='extract')``
  history count; ``MAX_RETRY_ATTEMPTS = 5``.
- §15 Recovery Primitive — ``mark_running_jobs_interrupted`` for C2
  startup; idempotent.
- §35.5 Post-freeze Correction — **R2-C terminal = ``normalizing``**;
  this module never transitions Documents to ``ready`` (that path is
  reserved for R3 chunking + indexing).

This module deliberately does **not** import Parser / Builder /
Persistence / FastAPI — it is a pure Store-layer helper.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import (
    Document,
    IngestionJob,
    validate_document_id_or_raise,
)
from .store import (
    DocumentNotFoundError,
    KnowledgeStore,
)

if TYPE_CHECKING:
    import aiosqlite


# ============================================================================
# Constants (per C0 §12 / §15 / §9.5)
# ============================================================================

#: Per-document retry ceiling (per C0 §12.3).
MAX_RETRY_ATTEMPTS: int = 5

#: Stage name for the full R2-C PDF→MD pipeline.
#:
#: Per C0 §9.5 — R2-C uses a single Job with ``stage='extract'`` to cover
#: inspect + extract + Quality evaluate + Build + Persist. ``normalize``
#: / ``chunk`` / ``index`` are reserved for future R3 stages.
EXTRACT_STAGE: str = "extract"

#: Safe error code recorded on ``failed`` Jobs recovered after a crash
#: (per C0 §15.2). Stable machine value — no path / body / secret.
INGESTION_INTERRUPTED: str = "ingestion_interrupted"


# ============================================================================
# Errors
# ============================================================================


class IngestionStoreError(Exception):
    """Base class for :class:`IngestionStore` errors."""


class IngestionAlreadyActiveError(IngestionStoreError):
    """Document already has a ``running`` Job — concurrent retry rejected.

    Carries ``document_id`` for safe API mapping; never includes absolute
    paths / raw exceptions / PDF content.
    """

    def __init__(self, document_id: str) -> None:
        self.document_id = document_id
        super().__init__(
            f"document {document_id!r} already has an active ingestion job"
        )


class RetryLimitReachedError(IngestionStoreError):
    """Document exceeded :data:`MAX_RETRY_ATTEMPTS` extract attempts."""

    def __init__(self, document_id: str, attempt_count: int) -> None:
        self.document_id = document_id
        self.attempt_count = attempt_count
        super().__init__(
            f"document {document_id!r} reached retry limit "
            f"({attempt_count}/{MAX_RETRY_ATTEMPTS})"
        )


class RetryNotAllowedError(IngestionStoreError):
    """Document ``status`` is not ``failed`` — retry rejected.

    Per C0 §8.5 — ``normalizing`` (R2-C terminal), ``needs_ocr`` (terminal
    business outcome), ``ready`` (R3 terminal), ``uploaded`` (already
    pending), ``extracting`` (in progress), ``deleting`` (terminal) are
    all rejected.
    """

    def __init__(self, document_id: str, current_status: str) -> None:
        self.document_id = document_id
        self.current_status = current_status
        super().__init__(
            f"retry not allowed for document {document_id!r} "
            f"in status {current_status!r}"
        )


# ============================================================================
# Result DTO
# ============================================================================


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """Result of successful :meth:`IngestionStore.claim_next_pending_document`.

    Bundles the claimed :class:`Document` (now ``status='extracting'``)
    and the freshly-created ``running`` :class:`IngestionJob`.
    """

    document: Document
    job: IngestionJob


# ============================================================================
# IngestionStore
# ============================================================================


class IngestionStore:
    """Ingestion-specific atomic operations on top of :class:`KnowledgeStore`.

    The class is a *friend* of :class:`KnowledgeStore`: it accesses
    ``_db`` / ``_write_lock`` / ``_require_db`` to compose multi-statement
    atomic transactions that the underlying Store does not expose
    directly. It never opens a second SQLite connection or second lock.

    All write methods follow the pattern::

        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                ... conditional UPDATE / INSERT ...
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")  # best-effort
                raise

    Double serialization (SQLite reserved lock + ``asyncio.Lock``) makes
    the active-Job uniqueness check TOCTOU-safe within a single process.
    """

    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Atomic claim (C0 §13.2)
    # ------------------------------------------------------------------

    async def claim_next_pending_document(self) -> ClaimedJob | None:
        """Atomically claim the next ``status='uploaded'`` Document.

        Transaction flow (per C0 §13.2):

        1. ``BEGIN IMMEDIATE``
        2. ``SELECT id ... WHERE status='uploaded' ORDER BY created_at, id LIMIT 1``
        3. If no row: ``ROLLBACK``; return ``None``
        4. Conditional ``UPDATE ... SET status='extracting' WHERE id=? AND status='uploaded'``
           (rowcount==1 expected; 0 ⇒ race within txn ⇒ ROLLBACK + return None)
        5. ``INSERT`` new Job (``status='running'``, ``stage='extract'``, attempt auto)
        6. ``COMMIT``

        Returns ``None`` if no pending work; otherwise a :class:`ClaimedJob`
        with refreshed Document + Job snapshots.
        """
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                doc_id = await self._select_oldest_uploaded_id(db)
                if doc_id is None:
                    await db.execute("ROLLBACK")
                    return None
                if not await self._transition_to_extracting(db, doc_id):
                    # Race inside txn (shouldn't happen given reserved lock)
                    await db.execute("ROLLBACK")
                    return None
                job = await self._insert_running_extract_job(db, doc_id)
                await db.execute("COMMIT")
            except Exception:
                await self._best_effort_rollback(db)
                raise

        document = await self._store.get_document(doc_id)
        return ClaimedJob(document=document, job=job)

    # ------------------------------------------------------------------
    # Retry (C0 §12 + §13.1)
    # ------------------------------------------------------------------

    async def create_retry_job(self, document_id: str) -> IngestionJob:
        """Atomically create a new retry Job for a ``failed`` Document.

        Pre-checks (outside transaction):

        - ``document_id`` format valid
        - Document exists
        - ``status == 'failed'``

        Atomic transaction (per C0 §13.1):

        1. ``BEGIN IMMEDIATE``
        2. ``SELECT COUNT(*) WHERE status='running'`` → if > 0:
           :class:`IngestionAlreadyActiveError`
        3. ``SELECT COUNT(*) WHERE stage='extract'`` → if >= ``MAX_RETRY_ATTEMPTS``:
           :class:`RetryLimitReachedError`
        4. ``INSERT`` new Job (attempt auto-incremented)
        5. Conditional ``UPDATE docs SET status='extracting', error_code='' WHERE status='failed'``
           (rowcount==0 ⇒ race ⇒ ROLLBACK + :class:`RetryNotAllowedError`)
        6. ``COMMIT``

        Raises:
            DocumentNotFoundError: document_id does not exist.
            RetryNotAllowedError: status != 'failed' (or raced).
            IngestionAlreadyActiveError: another Job is already running.
            RetryLimitReachedError: 5 prior extract attempts recorded.
        """
        validate_document_id_or_raise(document_id)
        document = await self._store.get_document(document_id)
        if document.status != "failed":
            raise RetryNotAllowedError(document_id, document.status)

        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")

                if await self._count_active_jobs(db, document_id) > 0:
                    await db.execute("ROLLBACK")
                    raise IngestionAlreadyActiveError(document_id)

                attempt_count = await self._count_extract_attempts(db, document_id)
                if attempt_count >= MAX_RETRY_ATTEMPTS:
                    await db.execute("ROLLBACK")
                    raise RetryLimitReachedError(document_id, attempt_count)

                job = await self._insert_running_extract_job(db, document_id)

                if not await self._transition_failed_to_extracting(db, document_id):
                    await db.execute("ROLLBACK")
                    raise RetryNotAllowedError(document_id, "(raced)")

                await db.execute("COMMIT")
            except (
                IngestionAlreadyActiveError,
                RetryLimitReachedError,
                RetryNotAllowedError,
            ):
                raise
            except Exception:
                await self._best_effort_rollback(db)
                raise

        return job

    # ------------------------------------------------------------------
    # Read queries
    # ------------------------------------------------------------------

    async def count_active_jobs_for_document(self, document_id: str) -> int:
        """Count ``running`` Jobs for ``document_id`` (for delete / retry guards)."""
        validate_document_id_or_raise(document_id)
        db = self._store._require_db()
        async with db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs "
            "WHERE document_id = ? AND status = 'running'",
            (document_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def has_active_job(self, document_id: str) -> bool:
        """Boolean wrapper around :meth:`count_active_jobs_for_document`."""
        return await self.count_active_jobs_for_document(document_id) > 0

    async def count_extract_attempts(self, document_id: str) -> int:
        """Count prior ``stage='extract'`` Jobs for retry-limit tracking."""
        validate_document_id_or_raise(document_id)
        db = self._store._require_db()
        async with db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs "
            "WHERE document_id = ? AND stage = ?",
            (document_id, EXTRACT_STAGE),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def get_latest_extract_job_for_document(
        self, document_id: str
    ) -> IngestionJob | None:
        """Return the latest ``stage='extract'`` Job (``started_at DESC, id DESC``)."""
        validate_document_id_or_raise(document_id)
        db = self._store._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_ingestion_jobs "
            "WHERE document_id = ? AND stage = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (document_id, EXTRACT_STAGE),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_job(row)

    # ------------------------------------------------------------------
    # Recovery primitive (C0 §15.2 — called by C2 WorkerManager startup)
    # ------------------------------------------------------------------

    async def mark_running_jobs_interrupted(self) -> int:
        """Mark every ``running`` Job as ``failed`` with ``ingestion_interrupted``.

        Idempotent — a second call affects 0 rows (no ``running`` Jobs left).
        Returns the count of Jobs transitioned.
        """
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_ingestion_jobs "
                    "SET status = 'failed', finished_at = ?, safe_error_code = ? "
                    "WHERE status = 'running'",
                    (_now_ms(), INGESTION_INTERRUPTED),
                )
                count = cur.rowcount
                await db.execute("COMMIT")
            except Exception:
                await self._best_effort_rollback(db)
                raise
        return count

    # ------------------------------------------------------------------
    # Internal transaction helpers — caller must hold _write_lock + be
    # inside a BEGIN IMMEDIATE transaction.
    # ------------------------------------------------------------------

    @staticmethod
    async def _select_oldest_uploaded_id(db: aiosqlite.Connection) -> str | None:
        async with db.execute(
            "SELECT id FROM knowledge_documents "
            "WHERE status = 'uploaded' "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row is not None else None

    @staticmethod
    async def _transition_to_extracting(
        db: aiosqlite.Connection, document_id: str
    ) -> bool:
        """Conditional UPDATE uploaded→extracting. Returns True if row updated."""
        cur = await db.execute(
            "UPDATE knowledge_documents "
            "SET status = 'extracting', updated_at = ? "
            "WHERE id = ? AND status = 'uploaded'",
            (_now_ms(), document_id),
        )
        return cur.rowcount == 1

    @staticmethod
    async def _transition_failed_to_extracting(
        db: aiosqlite.Connection, document_id: str
    ) -> bool:
        """Conditional UPDATE failed→extracting (clears error_code)."""
        cur = await db.execute(
            "UPDATE knowledge_documents "
            "SET status = 'extracting', error_code = '', updated_at = ? "
            "WHERE id = ? AND status = 'failed'",
            (_now_ms(), document_id),
        )
        return cur.rowcount == 1

    @staticmethod
    async def _count_active_jobs(
        db: aiosqlite.Connection, document_id: str
    ) -> int:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs "
            "WHERE document_id = ? AND status = 'running'",
            (document_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    async def _count_extract_attempts(
        db: aiosqlite.Connection, document_id: str
    ) -> int:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs "
            "WHERE document_id = ? AND stage = ?",
            (document_id, EXTRACT_STAGE),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    async def _insert_running_extract_job(
        db: aiosqlite.Connection, document_id: str
    ) -> IngestionJob:
        """INSERT a new ``running`` Job; auto-computes attempt from history."""
        job_id = f"job_{secrets.token_hex(8)}"
        started_at = _now_ms()
        attempt = (
            await IngestionStore._count_extract_attempts(db, document_id)
        ) + 1
        await db.execute(
            "INSERT INTO knowledge_ingestion_jobs "
            "(id, document_id, stage, status, attempt, started_at, "
            " finished_at, safe_error_code) "
            "VALUES (?, ?, ?, 'running', ?, ?, NULL, '')",
            (job_id, document_id, EXTRACT_STAGE, attempt, started_at),
        )
        return IngestionJob(
            id=job_id,
            document_id=document_id,
            stage=EXTRACT_STAGE,
            status="running",
            attempt=attempt,
            started_at=started_at,
            finished_at=None,
            safe_error_code="",
        )

    @staticmethod
    async def _best_effort_rollback(db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass


# ============================================================================
# Module-private helpers
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_job(row: aiosqlite.Row) -> IngestionJob:
    return IngestionJob(
        id=row["id"],
        document_id=row["document_id"],
        stage=row["stage"],
        status=row["status"],
        attempt=row["attempt"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        safe_error_code=row["safe_error_code"],
    )


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "MAX_RETRY_ATTEMPTS",
    "EXTRACT_STAGE",
    "INGESTION_INTERRUPTED",
    "IngestionStoreError",
    "IngestionAlreadyActiveError",
    "RetryLimitReachedError",
    "RetryNotAllowedError",
    "ClaimedJob",
    "IngestionStore",
]


# Suppress unused-import lint for re-exported symbols.
_: tuple[object, ...] = (DocumentNotFoundError, Document)

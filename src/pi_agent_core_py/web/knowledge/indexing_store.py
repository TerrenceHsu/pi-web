"""Knowledge Indexing State Store (P2-R3-C1).

Per P2-R3-C startup directive §9-§13 + §20-§25 + §31-§34:

- Atomic conditional claim of ``normalizing`` Documents into ``chunking``
- State transitions: ``normalizing → chunking → indexing → ready`` and
  ``chunking | indexing → failed``
- Recovery primitive: ``chunking | indexing`` interrupted states →
  ``failed`` with ``indexing_interrupted`` (idempotent; never produces
  ``ready``)
- ``Document.status`` itself is the durable indexing state — R3-C does
  NOT introduce a new ``indexing_jobs`` table (schema diff = 0)
- Shares the parent ``KnowledgeStore`` connection + ``_write_lock``;
  no second connection, no ``check_same_thread=False``
- No Worker / no polling / no app lifespan wiring / no HTTP API

Out of scope: IndexingOrchestrator (R3-C2 — Markdown read + integrity
+ Chunker / ChunkStore orchestration), IndexWorker (R3-D), app lifespan
wiring (R3-D), HTTP indexing API (none — R4 owns retrieval).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import aiosqlite

from .models import Document, is_valid_document_id
from .store import KnowledgeStore

# ============================================================================
# Constants — frozen safe error codes
# ============================================================================

#: Recorded on ``knowledge_documents.error_code`` when recovery converts a
#: stale ``chunking`` or ``indexing`` Document to ``failed``. Matches the
#: R2 ingestion-vocabulary pattern (``ingestion_interrupted`` for jobs).
INDEXING_INTERRUPTED: Final[str] = "indexing_interrupted"

#: Stable ordering for ``claim_next_normalizing_document``. ``created_at``
#: first (oldest pending wins) then ``id`` for deterministic tie-break.
_CLAIM_ORDER_BY: Final[str] = "created_at ASC, id ASC"


# ============================================================================
# Errors
# ============================================================================


class IndexingStoreError(Exception):
    """Base class for IndexingStore errors. Messages are safe."""


class DocumentNotClaimableError(IndexingStoreError):
    """Document is not in a claimable state (``normalizing``).

    safe_error_code: ``document_not_claimable``
    """


class InvalidStateTransitionError(IndexingStoreError):
    """Attempted transition is not in ``_DOCUMENT_TRANSITIONS``.

    safe_error_code: ``invalid_state_transition``
    """


# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Result of ``IndexingStore.recover_interrupted_indexing``.

    Fields are counts only — no Document IDs, no paths, no SQL. The
    caller (R3-D future) can correlate with ``list_documents`` if it
    needs to know which Documents were recovered.
    """

    chunking_recovered: int
    indexing_recovered: int
    total_recovered: int


# ============================================================================
# IndexingStore
# ============================================================================


class IndexingStore:
    """State-management layer for the R3-C indexing runtime.

    Wraps a ``KnowledgeStore`` connection (does NOT open its own).
    Re-uses ``KnowledgeStore._write_lock`` for serialisation.

    Construction::

        knowledge_store = await KnowledgeStore.open(db_path)
        indexing_store = IndexingStore(knowledge_store)

    All state transitions are atomic conditional writes inside
    ``BEGIN IMMEDIATE``. No Python lock is the source of correctness —
    SQLite row-level conditional UPDATE is.

    The store does NOT own a SQLite connection; therefore no B7-class
    open-failure cleanup is required here (per directive §49, new code
    that *does* own a connection must implement cleanup; this one
    borrows the existing one).
    """

    def __init__(self, knowledge_store: KnowledgeStore) -> None:
        self._store = knowledge_store

    # ------------------------------------------------------------------
    # Atomic claim — explicit document ID
    # ------------------------------------------------------------------

    async def claim_document(self, document_id: str) -> Document | None:
        """Atomically claim ``document_id`` from ``normalizing`` → ``chunking``.

        Transaction flow:

        1. ``BEGIN IMMEDIATE``
        2. Conditional ``UPDATE knowledge_documents SET status='chunking'
           WHERE id=? AND status='normalizing'``
        3. ``rowcount == 1`` → winner; refresh Document snapshot, return it
        4. ``rowcount == 0`` → already moved on (race / wrong state) → ROLLBACK
           + return ``None``

        Returns the post-claim Document snapshot (``status='chunking'``)
        on success, or ``None`` if the Document was not in ``normalizing``.

        Raises:
            DocumentNotClaimableError: ``document_id`` fails format check.
        """
        if not is_valid_document_id(document_id):
            raise DocumentNotClaimableError(
                f"invalid document_id: {document_id!r}"
            )
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = 'chunking', updated_at = ? "
                    "WHERE id = ? AND status = 'normalizing'",
                    (_now_ms(), document_id),
                )
                won = cur.rowcount == 1
                if not won:
                    await db.execute("ROLLBACK")
                    return None
                await db.execute("COMMIT")
            except Exception:
                await self._best_effort_rollback(db)
                raise
        return await self._store.get_document(document_id)

    # ------------------------------------------------------------------
    # Atomic claim — next normalizing by deterministic order
    # ------------------------------------------------------------------

    async def claim_next_normalizing_document(self) -> Document | None:
        """Atomically claim the next ``normalizing`` Document.

        Selection (per directive §13):

        .. code-block:: sql

            ORDER BY created_at ASC, id ASC LIMIT 1

        Transaction flow:

        1. ``BEGIN IMMEDIATE``
        2. ``SELECT id FROM knowledge_documents WHERE status='normalizing'
           ORDER BY created_at ASC, id ASC LIMIT 1``
        3. No row → ROLLBACK; return ``None``
        4. Conditional ``UPDATE ... WHERE id=? AND status='normalizing'``
           (rowcount==0 → race inside txn → ROLLBACK; return None)
        5. ``COMMIT``

        Returns ``None`` if there is no ``normalizing`` Document.
        """
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                doc_id = await self._select_oldest_normalizing_id(db)
                if doc_id is None:
                    await db.execute("ROLLBACK")
                    return None
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = 'chunking', updated_at = ? "
                    "WHERE id = ? AND status = 'normalizing'",
                    (_now_ms(), doc_id),
                )
                if cur.rowcount != 1:
                    # Race inside the same txn — extremely unlikely given
                    # the reserved write lock, but guard anyway.
                    await db.execute("ROLLBACK")
                    return None
                await db.execute("COMMIT")
            except Exception:
                await self._best_effort_rollback(db)
                raise
        return await self._store.get_document(doc_id)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def mark_indexing(self, document_id: str) -> None:
        """Atomically transition ``chunking`` → ``indexing``.

        Raises:
            DocumentNotClaimableError: invalid ID format.
            InvalidStateTransitionError: Document not currently ``chunking``
                (already moved on, or never claimed by this runtime).
        """
        await self._conditional_transition(
            document_id=document_id,
            from_status="chunking",
            to_status="indexing",
        )

    async def mark_ready(self, document_id: str) -> None:
        """Atomically transition ``indexing`` → ``ready``.

        Raises:
            DocumentNotClaimableError: invalid ID format.
            InvalidStateTransitionError: Document not currently ``indexing``.
        """
        await self._conditional_transition(
            document_id=document_id,
            from_status="indexing",
            to_status="ready",
        )

    async def mark_failed(
        self,
        document_id: str,
        *,
        error_code: str,
    ) -> None:
        """Atomically transition ``chunking | indexing`` → ``failed``.

        Records ``error_code`` on the Document (``knowledge_documents.error_code``
        column). ``error_code`` MUST be a stable machine-readable code; it is
        NOT a free-form user message.

        Raises:
            DocumentNotClaimableError: invalid ID format.
            InvalidStateTransitionError: Document not currently in
                ``chunking`` or ``indexing``.
        """
        if not is_valid_document_id(document_id):
            raise DocumentNotClaimableError(
                f"invalid document_id: {document_id!r}"
            )
        if not error_code or not isinstance(error_code, str):
            raise IndexingStoreError("error_code must be a non-empty string")
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = 'failed', error_code = ?, updated_at = ? "
                    "WHERE id = ? AND status IN ('chunking', 'indexing')",
                    (error_code, _now_ms(), document_id),
                )
                if cur.rowcount != 1:
                    await db.execute("ROLLBACK")
                    raise InvalidStateTransitionError(
                        f"document {document_id!r} not in chunking/indexing; "
                        f"cannot mark failed"
                    )
                await db.execute("COMMIT")
            except InvalidStateTransitionError:
                raise
            except Exception:
                await self._best_effort_rollback(db)
                raise

    # ------------------------------------------------------------------
    # Recovery primitive
    # ------------------------------------------------------------------

    async def recover_interrupted_indexing(
        self,
        *,
        cleanup_callback=None,
    ) -> RecoveryResult:
        """Convert stale ``chunking`` / ``indexing`` Documents to ``failed``.

        Per directive §32: any Document still in ``chunking`` or
        ``indexing`` represents a process that may have been interrupted
        mid-flight. MVP recovery does NOT guess; it fails them with
        ``indexing_interrupted`` so the existing R2 Retry API can drive
        re-ingestion.

        ``normalizing`` is a valid pending state — left UNCHANGED.

        ``ready`` / ``failed`` / ``needs_ocr`` / terminal states — UNCHANGED.

        Optional ``cleanup_callback(document_id)``: invoked for each
        recovered Document BEFORE the state transition commits. The
        callback should clean up any partial chunk / FTS rows. If the
        callback raises, the recovery transaction ROLLBACKs and the
        Document remains in its interrupted state (so a future recovery
        pass can retry). The callback MUST be idempotent.

        Idempotent: a second call recovers 0 rows.

        Returns counts only — never returns Document IDs.
        """
        db = self._store._require_db()
        chunking_recovered = 0
        indexing_recovered = 0
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # Snapshot the candidate IDs inside the txn so cleanup
                # callbacks run with a consistent view.
                candidates = await self._select_interrupted_ids(db)
                for doc_id, current_status in candidates:
                    if cleanup_callback is not None:
                        # Callback may be sync or async; the runtime
                        # dispatches via awaitable-or-result. We support
                        # async callbacks because chunk/FTS cleanup goes
                        # through ChunkStore which is async.
                        result = cleanup_callback(doc_id)
                        if hasattr(result, "__await__"):
                            await result
                    cur = await db.execute(
                        "UPDATE knowledge_documents "
                        "SET status = 'failed', "
                        "    error_code = ?, "
                        "    updated_at = ? "
                        "WHERE id = ? AND status = ?",
                        (INDEXING_INTERRUPTED, _now_ms(), doc_id, current_status),
                    )
                    if cur.rowcount == 1:
                        if current_status == "chunking":
                            chunking_recovered += 1
                        else:
                            indexing_recovered += 1
                await db.execute("COMMIT")
            except Exception:
                await self._best_effort_rollback(db)
                raise
        return RecoveryResult(
            chunking_recovered=chunking_recovered,
            indexing_recovered=indexing_recovered,
            total_recovered=chunking_recovered + indexing_recovered,
        )

    # ------------------------------------------------------------------
    # Internal helpers — caller must hold _write_lock
    # ------------------------------------------------------------------

    async def _conditional_transition(
        self,
        *,
        document_id: str,
        from_status: str,
        to_status: str,
    ) -> None:
        if not is_valid_document_id(document_id):
            raise DocumentNotClaimableError(
                f"invalid document_id: {document_id!r}"
            )
        db = self._store._require_db()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ?",
                    (to_status, _now_ms(), document_id, from_status),
                )
                if cur.rowcount != 1:
                    await db.execute("ROLLBACK")
                    raise InvalidStateTransitionError(
                        f"document {document_id!r} not in {from_status!r}; "
                        f"cannot transition to {to_status!r}"
                    )
                await db.execute("COMMIT")
            except InvalidStateTransitionError:
                raise
            except Exception:
                await self._best_effort_rollback(db)
                raise

    @staticmethod
    async def _select_oldest_normalizing_id(
        db: aiosqlite.Connection,
    ) -> str | None:
        async with db.execute(
            "SELECT id FROM knowledge_documents "
            "WHERE status = 'normalizing' "
            f"ORDER BY {_CLAIM_ORDER_BY} LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row is not None else None

    @staticmethod
    async def _select_interrupted_ids(
        db: aiosqlite.Connection,
    ) -> list[tuple[str, str]]:
        async with db.execute(
            "SELECT id, status FROM knowledge_documents "
            "WHERE status IN ('chunking', 'indexing') "
            f"ORDER BY {_CLAIM_ORDER_BY}"
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r["id"], r["status"]) for r in rows]

    @staticmethod
    async def _best_effort_rollback(db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass


# ============================================================================
# Helpers
# ============================================================================


def _now_ms() -> int:
    """Return current epoch milliseconds (mirrors store._now_ms)."""
    import time as _time

    return int(_time.time() * 1000)

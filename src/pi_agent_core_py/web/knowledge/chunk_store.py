"""SQLite FTS5 Chunk Store (P2-R3-B2).

Per P2-R3-B startup directive §16-§33:

- Atomic ``replace_document_chunks`` (chunks + FTS in one transaction)
- FTS sync mode: explicit transaction (no SQLite triggers)
- ``knowledge_chunks`` is durable source-of-truth; ``knowledge_chunks_fts``
  is a derived index
- ``rebuild_fts`` / ``verify_fts_integrity`` primitives for maintenance
- ``search_chunks_fts`` is an **internal** Store primitive (NOT an Agent
  Tool; not registered; not in LLM prompt)
- ``compile_literal_fts_query`` — safe literal FTS query compiler; never
  passes raw user text as FTS syntax
- BM25 ranking with heading-text weight + stable tie-break
- ``ready``-only filter (joins ``knowledge_documents``)
- Library allowlist filter (caller-supplied, trusted IDs only)
- ``chunking``-only state constraint on replace (defense-in-depth;
  R3-C owns the runtime transitions)

Out of scope: IndexingOrchestrator, IndexWorker, normalizing→chunking
runtime, app lifespan, search_knowledge Agent Tool, session binding.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import aiosqlite

from .chunker import (
    KnowledgeChunk,
    compute_chunk_id,
    compute_content_sha256,
)
from .models import is_valid_document_id, is_valid_library_id

if TYPE_CHECKING:
    from .store import KnowledgeStore

# ============================================================================
# Constants — frozen per directive §11 (chunk size) + §30 (FTS query limits)
# ============================================================================

#: Maximum user query length accepted by ``compile_literal_fts_query``.
#: Defensive upper bound — typical knowledge queries are tens to low
#: hundreds of chars. Longer input is rejected rather than truncated.
MAX_FTS_QUERY_CHARS: Final[int] = 512

#: Maximum number of search hits returned by ``search_chunks_fts``.
DEFAULT_FTS_LIMIT: Final[int] = 5
MAX_FTS_LIMIT: Final[int] = 50

#: BM25 column weights — heading_text weighted 3x content. Order matches
#: the FTS5 virtual table column order in P2-R3-B1 schema:
#: ``chunk_id, document_id, library_id, heading_text, content`` (the
#: first three are UNINDEXED; bm25 ignores them in scoring).
_BM25_HEADING_WEIGHT: Final[float] = 3.0
_BM25_CONTENT_WEIGHT: Final[float] = 1.0

#: Match any whitespace run for query term splitting.
_TERM_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

# ============================================================================
# Errors
# ============================================================================


class ChunkStoreError(Exception):
    """Base class for chunk store errors. Messages are safe (no paths / SQL)."""


class ChunkValidationError(ChunkStoreError):
    """Input chunk DTO failed validation (char_count, sha, ID, page range).

    safe_error_code: ``chunk_validation_failed``
    """


class ChunkingEmpty(ChunkStoreError):
    """Empty chunk list supplied to ``replace_document_chunks``.

    safe_error_code: ``chunking_empty``
    """


class DocumentStateError(ChunkStoreError):
    """Document is not in ``chunking`` state; replace rejected.

    safe_error_code: ``document_not_chunking``
    """


class FTSQueryError(ChunkStoreError):
    """User query failed safe-compiler validation (empty / too long).

    safe_error_code: ``fts_query_invalid``
    """


class FTSIntegrityError(ChunkStoreError):
    """``knowledge_chunks`` ↔ ``knowledge_chunks_fts`` row count mismatch.

    safe_error_code: ``fts_integrity_error``
    """


class ChunkPersistenceError(ChunkStoreError):
    """Generic persistence failure (transaction rolled back).

    safe_error_code: ``chunk_persistence_failed``
    """


# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class ChunkSearchHit:
    """Single FTS search result returned by ``ChunkStore.search_chunks_fts``.

    Caller (R4 future) is responsible for any citation rendering. This DTO
    carries no absolute paths, no session_id, no SQL.
    """

    chunk_id: str
    document_id: str
    library_id: str
    ordinal: int
    heading_path: tuple[str, ...]
    content: str
    page_start: int
    page_end: int
    rank: float


@dataclass(frozen=True, slots=True)
class FTSIntegrityReport:
    """Result of ``ChunkStore.verify_fts_integrity``."""

    chunk_count: int
    fts_count: int
    orphan_fts_rows: int
    chunks_missing_fts: int
    ok: bool


# ============================================================================
# Safe literal FTS query compiler
# ============================================================================


def compile_literal_fts_query(query: str) -> str:
    """Compile a raw user query into a safe FTS5 phrase-AND expression.

    Each whitespace-delimited term is wrapped in double quotes with any
    internal double quotes duplicated (FTS5 escape rule). Terms are joined
    with implicit AND (whitespace). The output is therefore a sequence of
    FTS5 phrase tokens, never exposing FTS5 operators (AND/OR/NOT/NEAR,
    column filters, bare ``*``) to user control.

    Examples::

        "radiotherapy dose"          → '"radiotherapy" "dose"'
        'foo OR *'                   → '"foo" "OR" "*"'
        'a"b'                        → '"a""b"'
        '中文 检索'                    → '"中文" "检索"'

    Raises:
        FTSQueryError: if the input is empty, whitespace-only, or exceeds
            ``MAX_FTS_QUERY_CHARS``.
    """
    if query is None:
        raise FTSQueryError("query is None")
    stripped = query.strip()
    if not stripped:
        raise FTSQueryError("query is empty after trim")
    if len(query) > MAX_FTS_QUERY_CHARS:
        raise FTSQueryError(
            f"query exceeds max length {MAX_FTS_QUERY_CHARS} chars"
        )
    terms = [t for t in _TERM_SPLIT_RE.split(stripped) if t]
    if not terms:
        raise FTSQueryError("query has no terms after split")
    quoted = []
    for term in terms:
        # Duplicate any embedded double quote (FTS5 escape rule within a
        # quoted phrase).
        escaped = term.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


# ============================================================================
# ChunkStore
# ============================================================================


class ChunkStore:
    """SQLite FTS5 chunk store.

    Owns the ``knowledge_chunks`` + ``knowledge_chunks_fts`` tables. Wraps a
    ``KnowledgeStore`` connection (does not open its own) and uses the same
    ``_write_lock`` for serialization.

    Construction::

        store = await KnowledgeStore.open(db_path)
        chunk_store = ChunkStore(store)
        await chunk_store.replace_document_chunks(
            document_id="doc_x",
            library_id="lib_x",
            chunks=[...],
        )
    """

    def __init__(self, knowledge_store: KnowledgeStore) -> None:
        self._store = knowledge_store
        self._db: aiosqlite.Connection = knowledge_store._require_db()
        self._write_lock = knowledge_store._write_lock

    # ------------------------------------------------------------------
    # Atomic replace (chunks + FTS)
    # ------------------------------------------------------------------

    async def replace_document_chunks(
        self,
        *,
        document_id: str,
        library_id: str,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        """Atomically replace all chunks for ``document_id``.

        Preconditions:
          * ``document_id`` and ``library_id`` pass ID format validation.
          * Every chunk in ``chunks`` belongs to ``document_id``.
          * Ordinals form a contiguous 0..N-1 sequence.
          * Every chunk's content_sha256 / char_count / id matches the
            frozen R3-A deterministic algorithms.

        Side effects (single ``BEGIN IMMEDIATE`` transaction):
          1. DELETE existing FTS rows for the document.
          2. DELETE existing chunks for the document.
          3. INSERT new chunks.
          4. INSERT new FTS rows.

        Any failure → ROLLBACK; no partial state.

        Raises:
            ChunkingEmpty: ``chunks`` is empty.
            ChunkValidationError: chunk DTO validation failed.
            DocumentStateError: Document not in ``chunking`` state.
            ChunkPersistenceError: transaction failed (rolled back).
        """
        if not is_valid_document_id(document_id):
            raise ChunkValidationError(f"invalid document_id: {document_id!r}")
        if not is_valid_library_id(library_id):
            raise ChunkValidationError(f"invalid library_id: {library_id!r}")
        if not chunks:
            raise ChunkingEmpty("chunks list is empty")
        self._validate_chunk_sequence(document_id, chunks)
        await self._require_document_chunking(document_id)

        now = _now_ms_local()
        try:
            async with self._write_lock:
                await self._db.execute("BEGIN IMMEDIATE")
                # Step 1+2: clear old FTS + chunks.
                await self._db.execute(
                    "DELETE FROM knowledge_chunks_fts WHERE document_id = ?",
                    (document_id,),
                )
                await self._db.execute(
                    "DELETE FROM knowledge_chunks WHERE document_id = ?",
                    (document_id,),
                )
                # Step 3+4: insert new chunks + FTS rows.
                for chunk in chunks:
                    heading_text = chunk.heading_path[-1] if chunk.heading_path else ""
                    heading_path_json = json.dumps(
                        list(chunk.heading_path), ensure_ascii=False
                    )
                    await self._db.execute(
                        "INSERT INTO knowledge_chunks ("
                        " id, library_id, document_id, ordinal, heading_path,"
                        " page_start, page_end, content, content_hash,"
                        " char_count, content_sha256, token_count, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            chunk.id,
                            library_id,
                            document_id,
                            chunk.ordinal,
                            heading_path_json,
                            chunk.page_start,
                            chunk.page_end,
                            chunk.content,
                            chunk.content_sha256,  # content_hash alias for R3-A
                            chunk.char_count,
                            chunk.content_sha256,
                            0,  # token_count unused in R3 (R3-A uses char_count)
                            now,
                        ),
                    )
                    await self._db.execute(
                        "INSERT INTO knowledge_chunks_fts ("
                        " chunk_id, document_id, library_id, heading_text, content"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (
                            chunk.id,
                            document_id,
                            library_id,
                            heading_text,
                            chunk.content,
                        ),
                    )
                await self._db.execute("COMMIT")
        except ChunkStoreError:
            raise
        except Exception as exc:
            try:
                await self._db.execute("ROLLBACK")
            except Exception:
                pass
            raise ChunkPersistenceError(
                f"replace_document_chunks failed: {type(exc).__name__}"
            ) from None

    # ------------------------------------------------------------------
    # Delete chunks (FTS cleanup happens via KnowledgeStore cascade)
    # ------------------------------------------------------------------

    async def delete_document_chunks(self, *, document_id: str) -> None:
        """Delete all chunks + FTS rows for ``document_id`` atomically.

        Used for recovery / re-index paths. The Document itself is not
        modified. ``KnowledgeStore.delete_document_hard`` already cleans
        FTS via its own cascade (see store.py).
        """
        if not is_valid_document_id(document_id):
            raise ChunkValidationError(f"invalid document_id: {document_id!r}")
        try:
            async with self._write_lock:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    "DELETE FROM knowledge_chunks_fts WHERE document_id = ?",
                    (document_id,),
                )
                await self._db.execute(
                    "DELETE FROM knowledge_chunks WHERE document_id = ?",
                    (document_id,),
                )
                await self._db.execute("COMMIT")
        except Exception as exc:
            try:
                await self._db.execute("ROLLBACK")
            except Exception:
                pass
            raise ChunkPersistenceError(
                f"delete_document_chunks failed: {type(exc).__name__}"
            ) from None

    # ------------------------------------------------------------------
    # FTS rebuild
    # ------------------------------------------------------------------

    async def rebuild_fts(self) -> int:
        """Drop and rebuild the entire FTS index from ``knowledge_chunks``.

        Returns the number of FTS rows written. Idempotent — running it
        twice produces byte-identical FTS content. Does not modify
        ``knowledge_chunks`` rows or Document status.

        Implementation: clear FTS inside ``BEGIN IMMEDIATE``; re-insert every
        chunk row; COMMIT. Failure → ROLLBACK; previous FTS preserved.
        """
        try:
            async with self._write_lock:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute("DELETE FROM knowledge_chunks_fts")
                async with self._db.execute(
                    "SELECT id, document_id, library_id, heading_path, content "
                    "FROM knowledge_chunks ORDER BY document_id, ordinal"
                ) as cursor:
                    rows = await cursor.fetchall()
                written = 0
                for row in rows:
                    heading_path_json = row["heading_path"]
                    try:
                        heading_path_list = (
                            json.loads(heading_path_json) if heading_path_json else []
                        )
                    except json.JSONDecodeError:
                        heading_path_list = []
                    heading_text = (
                        heading_path_list[-1]
                        if isinstance(heading_path_list, list) and heading_path_list
                        else ""
                    )
                    await self._db.execute(
                        "INSERT INTO knowledge_chunks_fts ("
                        " chunk_id, document_id, library_id, heading_text, content"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (
                            row["id"],
                            row["document_id"],
                            row["library_id"],
                            heading_text,
                            row["content"],
                        ),
                    )
                    written += 1
                await self._db.execute("COMMIT")
            return written
        except Exception as exc:
            try:
                await self._db.execute("ROLLBACK")
            except Exception:
                pass
            raise ChunkPersistenceError(
                f"rebuild_fts failed: {type(exc).__name__}"
            ) from None

    # ------------------------------------------------------------------
    # FTS integrity check
    # ------------------------------------------------------------------

    async def verify_fts_integrity(self) -> FTSIntegrityReport:
        """Verify ``knowledge_chunks`` ↔ ``knowledge_chunks_fts`` consistency.

        Checks:
          * chunk_count == fts_count
          * every chunk has exactly one FTS row (no chunks-missing-fts)
          * every FTS row points to an existing chunk (no orphan FTS)

        Does NOT scan Markdown or verify content_sha256 round-trip.
        """
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            fts_count = (await cursor.fetchone())["n"]
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks c "
            "WHERE NOT EXISTS ("
            " SELECT 1 FROM knowledge_chunks_fts f WHERE f.chunk_id = c.id"
            ")"
        ) as cursor:
            chunks_missing_fts = (await cursor.fetchone())["n"]
        async with self._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts f "
            "WHERE NOT EXISTS ("
            " SELECT 1 FROM knowledge_chunks c WHERE c.id = f.chunk_id"
            ")"
        ) as cursor:
            orphan_fts_rows = (await cursor.fetchone())["n"]
        ok = (
            chunk_count == fts_count
            and chunks_missing_fts == 0
            and orphan_fts_rows == 0
        )
        return FTSIntegrityReport(
            chunk_count=chunk_count,
            fts_count=fts_count,
            orphan_fts_rows=orphan_fts_rows,
            chunks_missing_fts=chunks_missing_fts,
            ok=ok,
        )

    # ------------------------------------------------------------------
    # FTS search (internal primitive, NOT an Agent Tool)
    # ------------------------------------------------------------------

    async def search_chunks_fts(
        self,
        query: str,
        *,
        library_ids: Sequence[str] | None = None,
        limit: int = DEFAULT_FTS_LIMIT,
    ) -> list[ChunkSearchHit]:
        """Search chunks using BM25 ranking. Returns top-N hits.

        ``library_ids``:
          * ``None`` — search across all libraries (internal / maintenance).
          * ``[]`` (empty sequence) — return ``[]`` immediately.
          * non-empty — restrict to those trusted library IDs (caller must
            have already resolved Session bindings; R3-B does NOT do
            Session authorization).

        ``query`` is compiled via ``compile_literal_fts_query`` before
        being passed to FTS5 MATCH. No raw user text reaches FTS syntax.

        Result is filtered to ``documents.status = 'ready'``. Documents in
        any other state (normalizing / chunking / indexing / failed /
        needs_ocr / deleting) are never returned, even if FTS rows exist.

        Ranking:
          * ``bm25(knowledge_chunks_fts, <heading_weight>, <content_weight>)``
            — lower (more negative) = better.
          * Stable tie-break by ``document_id ASC, ordinal ASC, chunk_id ASC``.

        Raises:
            FTSQueryError: ``query`` failed validation.
            ChunkValidationError: ``limit`` out of range or ``library_ids``
                contains invalid IDs.
        """
        if limit < 1 or limit > MAX_FTS_LIMIT:
            raise ChunkValidationError(
                f"limit must be in [1, {MAX_FTS_LIMIT}]; got {limit}"
            )
        if library_ids is not None:
            if len(library_ids) == 0:
                return []
            for lid in library_ids:
                if not is_valid_library_id(lid):
                    raise ChunkValidationError(f"invalid library_id: {lid!r}")
        compiled = compile_literal_fts_query(query)

        # Build parameterised MATCH. We use the literal compiled query as
        # a single bound parameter so the FTS5 expression is always the
        # safe literal-quoted form.
        sql = (
            "SELECT f.chunk_id, f.document_id, f.library_id, "
            "       c.ordinal, c.heading_path, c.content, "
            "       c.page_start, c.page_end, "
            "       bm25(knowledge_chunks_fts, ?, ?) AS rank "
            "FROM knowledge_chunks_fts AS f "
            "JOIN knowledge_chunks AS c ON c.id = f.chunk_id "
            "JOIN knowledge_documents AS d ON d.id = f.document_id "
            "WHERE knowledge_chunks_fts MATCH ? "
            "  AND d.status = 'ready'"
        )
        params: list[object] = [
            _BM25_HEADING_WEIGHT,
            _BM25_CONTENT_WEIGHT,
            compiled,
        ]
        if library_ids is not None:
            placeholders = ", ".join(["?"] * len(library_ids))
            sql += f"  AND f.library_id IN ({placeholders})"
            params.extend(library_ids)
        sql += " ORDER BY rank ASC, f.document_id ASC, c.ordinal ASC, f.chunk_id ASC"
        sql += " LIMIT ?"
        params.append(limit)
        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        hits: list[ChunkSearchHit] = []
        for row in rows:
            try:
                heading_path_list = json.loads(row["heading_path"]) if row["heading_path"] else []
            except json.JSONDecodeError:
                heading_path_list = []
            heading_path = (
                tuple(heading_path_list)
                if isinstance(heading_path_list, list)
                else ()
            )
            hits.append(
                ChunkSearchHit(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    library_id=row["library_id"],
                    ordinal=row["ordinal"],
                    heading_path=heading_path,
                    content=row["content"],
                    page_start=row["page_start"],
                    page_end=row["page_end"],
                    rank=row["rank"],
                )
            )
        return hits

    # ------------------------------------------------------------------
    # Internal — validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_chunk_sequence(
        document_id: str, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        """Validate every chunk belongs to ``document_id``, ordinals are
        contiguous 0..N-1, IDs are unique, and content_sha256 / char_count /
        id match the frozen R3-A deterministic algorithms.
        """
        seen_ids: set[str] = set()
        for index, chunk in enumerate(chunks):
            if chunk.document_id != document_id:
                raise ChunkValidationError(
                    f"chunk {index} document_id mismatch: "
                    f"{chunk.document_id!r} != {document_id!r}"
                )
            if chunk.ordinal != index:
                raise ChunkValidationError(
                    f"chunk {index} ordinal mismatch: "
                    f"{chunk.ordinal} != {index}"
                )
            if chunk.id in seen_ids:
                raise ChunkValidationError(
                    f"duplicate chunk id at ordinal {index}: {chunk.id}"
                )
            seen_ids.add(chunk.id)
            if chunk.char_count != len(chunk.content):
                raise ChunkValidationError(
                    f"chunk {index} char_count {chunk.char_count} != "
                    f"len(content) {len(chunk.content)}"
                )
            if chunk.char_count <= 0:
                raise ChunkValidationError(
                    f"chunk {index} char_count must be > 0; got {chunk.char_count}"
                )
            expected_sha = compute_content_sha256(chunk.content)
            if chunk.content_sha256 != expected_sha:
                raise ChunkValidationError(
                    f"chunk {index} content_sha256 mismatch"
                )
            if len(chunk.content_sha256) != 64:
                raise ChunkValidationError(
                    f"chunk {index} content_sha256 length must be 64; "
                    f"got {len(chunk.content_sha256)}"
                )
            expected_id = compute_chunk_id(
                document_id, chunk.ordinal, chunk.content_sha256
            )
            if chunk.id != expected_id:
                raise ChunkValidationError(
                    f"chunk {index} id does not match deterministic algorithm"
                )
            if chunk.page_start < 1:
                raise ChunkValidationError(
                    f"chunk {index} page_start must be >= 1; "
                    f"got {chunk.page_start}"
                )
            if chunk.page_end < chunk.page_start:
                raise ChunkValidationError(
                    f"chunk {index} page_end {chunk.page_end} < "
                    f"page_start {chunk.page_start}"
                )

    async def _require_document_chunking(self, document_id: str) -> None:
        """Defense-in-depth: only ``chunking`` Documents may be replaced.

        R3-C owns the runtime transitions; this guard prevents accidental
        writes from non-R3-C code paths. R3-B2 tests bypass it via direct
        SQL state mutation.
        """
        async with self._db.execute(
            "SELECT status FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise DocumentStateError(
                f"document not found: {document_id}"
            )
        if row["status"] != "chunking":
            raise DocumentStateError(
                f"document status must be 'chunking'; got {row['status']!r}"
            )


# ============================================================================
# Helpers
# ============================================================================


def _now_ms_local() -> int:
    """Return current epoch milliseconds (mirrors store._now_ms)."""
    import time as _time

    return int(_time.time() * 1000)

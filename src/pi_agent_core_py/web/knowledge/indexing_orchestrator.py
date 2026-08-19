"""Knowledge Indexing Orchestrator (P2-R3-C2).

Per P2-R3-C startup directive §15-§22 + §24-§28 + §42-§46:

- Explicit-call runtime (NO Worker / NO background polling / NO app
  lifespan wiring / NO HTTP API)
- Wires together the frozen R3-A / R3-B / R3-C1 components into a
  single normalizing → chunking → indexing → ready flow with failure
  compensation and recovery-friendly intermediate states
- Document.status itself is the durable state (R3-C1 contract; no
  indexing_jobs table)

Out of scope (deferred to R3-D / R3-E / R4):
- IndexWorker / background polling / asyncio.Queue / app lifespan
- HTTP indexing API / active-indexing delete guards
- search_knowledge Agent Tool / Session binding / Citation formatting
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, cast

from .chunk_store import ChunkStore
from .chunker import (
    CanonicalMarkdownInvalid,
    ChunkingEmpty,
    HeadingAwareChunker,
)
from .files import FileNotFoundError_ as MarkdownFileMissing
from .files import KnowledgeFileStore
from .indexing_store import IndexingStore
from .models import Document
from .store import DocumentNotFoundError, KnowledgeStore

# ============================================================================
# Constants — frozen document.md filename (R2-B contract)
# ============================================================================

#: The fixed Canonical Markdown filename inside each document dir.
#: Mirrors R2-B ``markdown_persistence.DOCUMENT_MARKDOWN_FILENAME``.
_DOCUMENT_MARKDOWN_FILENAME: Final[str] = "document.md"

#: Frontmatter extractor for ``source_sha256`` (R2-B serialises via
#: ``json.dumps(value, ensure_ascii=False)`` → JSON-quoted 64-hex string).
#: R2-B ``_build_frontmatter`` emits ``key: value`` (colon-separated).
#: Regex accepts optional colon (``:?\s+``) for fixture compatibility.
_FM_SOURCE_SHA256_RE: Final[re.Pattern[str]] = re.compile(
    r"^source_sha256:?\s+(.+?)\s*$", re.MULTILINE
)

# ============================================================================
# Safe error codes
# ============================================================================

#: Stable machine-readable error codes recorded on Document.error_code
#: when indexing fails. Each maps 1:1 to a runtime failure path.
CODE_CANONICAL_MARKDOWN_MISSING: Final[str] = "canonical_markdown_missing"
CODE_CANONICAL_MARKDOWN_INTEGRITY_ERROR: Final[str] = (
    "canonical_markdown_integrity_error"
)
CODE_CANONICAL_MARKDOWN_INVALID: Final[str] = "canonical_markdown_invalid"
CODE_CHUNKING_EMPTY: Final[str] = "chunking_empty"
CODE_CHUNKING_FAILED: Final[str] = "chunking_failed"
CODE_CHUNK_PERSISTENCE_FAILED: Final[str] = "chunk_persistence_failed"
CODE_FTS_INTEGRITY_ERROR: Final[str] = "fts_integrity_error"
CODE_INDEXING_FAILED: Final[str] = "indexing_failed"


# ============================================================================
# Errors — internal (never raised across the public boundary; converted
# to safe ``IndexingResult`` with stable ``error_code``)
# ============================================================================


class IndexingOrchestratorError(Exception):
    """Base class. Use ``IndexingResult`` for caller-facing failures."""


class DocumentNotNormalizingError(IndexingOrchestratorError):
    """Document was not in ``normalizing`` when ``process_document`` claimed."""


# ============================================================================
# DTOs
# ============================================================================


@dataclass(frozen=True, slots=True)
class IndexingResult:
    """Result of ``IndexingOrchestrator.process_document``.

    On success: ``status == "ready"`` and ``chunk_count > 0``.
    On failure: ``status == "failed"`` and ``error_code`` is a stable
    machine-readable code; ``error_message`` is a safe summary (no
    paths / SQL / traceback / Markdown body).
    """

    document_id: str
    status: str
    chunk_count: int
    error_code: str | None = None
    error_message: str | None = None


# ============================================================================
# IndexingOrchestrator
# ============================================================================


class IndexingOrchestrator:
    """Explicit-call indexing runtime.

    Construction::

        knowledge_store = await KnowledgeStore.open(db_path)
        knowledge_file_store = KnowledgeFileStore(knowledge_root, ...)
        chunk_store = ChunkStore(knowledge_store)
        indexing_store = IndexingStore(knowledge_store)
        orchestrator = IndexingOrchestrator(
            knowledge_store=knowledge_store,
            knowledge_file_store=knowledge_file_store,
            chunk_store=chunk_store,
            indexing_store=indexing_store,
        )
        result = await orchestrator.process_document("doc_x")

    ``process_document`` is the single public entry point. It executes
    the full normalizing → chunking → indexing → ready flow with failure
    compensation, or returns a controlled result for non-claimable
    Documents (``ready`` / ``failed`` / ``needs_ocr``).
    """

    def __init__(
        self,
        *,
        knowledge_store: KnowledgeStore,
        knowledge_file_store: KnowledgeFileStore,
        chunk_store: ChunkStore,
        indexing_store: IndexingStore,
        chunker: HeadingAwareChunker | None = None,
    ) -> None:
        self._knowledge_store = knowledge_store
        self._file_store = knowledge_file_store
        self._chunk_store = chunk_store
        self._indexing_store = indexing_store
        self._chunker = chunker or HeadingAwareChunker()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_document(self, document_id: str) -> IndexingResult:
        """Explicitly index a single Document.

        Flow:
          T1. claim_document (normalizing → chunking). If Document is not
              ``normalizing``, return a controlled ``IndexingResult``
              describing the current status without side effects.
          T2. Read + verify document.md (existence + frontmatter SHA +
              Chunker frontmatter/page_count validation).
          T3. ChunkStore.replace_document_chunks.
          T4. mark_indexing (chunking → indexing).
          T5. FTS integrity gate.
          T6. mark_ready (indexing → ready).

        Any failure in T2-T5 triggers ``_fail_document`` which:
          1. Cleans up chunks + FTS for the Document.
          2. ``IndexingStore.mark_failed`` with stable ``error_code``.

        Returns ``IndexingResult`` — never raises for runtime failures
        (only raises for programmer errors like invalid ``document_id``
        format).

        Idempotent across retries: re-running after R2 Retry re-enters
        ``normalizing`` and produces deterministic chunk IDs / SHAs /
        page ranges (per R3-A chunker contract).
        """
        # Pre-check current state to give a useful result without claiming.
        try:
            document = await self._knowledge_store.get_document(document_id)
        except DocumentNotFoundError:
            return IndexingResult(
                document_id=document_id,
                status="missing",
                chunk_count=0,
                error_code=None,
                error_message="document not found",
            )
        if document.status != "normalizing":
            return self._result_for_non_normalizing(document)

        # T1 — claim.
        claimed = await self._indexing_store.claim_document(document_id)
        if claimed is None:
            # Race: status moved on between get_document and claim.
            document = await self._knowledge_store.get_document(document_id)
            return self._result_for_non_normalizing(document)

        # T2 — read + integrity verify.
        try:
            markdown_text, parsed_sha = await self._read_and_verify_markdown(
                claimed
            )
        except _MarkdownMissing:
            return await self._fail_document(
                claimed.id,
                code=CODE_CANONICAL_MARKDOWN_MISSING,
                message="canonical markdown not found on disk",
            )
        except _ShaMismatch as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_CANONICAL_MARKDOWN_INTEGRITY_ERROR,
                message=f"source_sha256 mismatch: {exc}",
            )

        # T2b — chunker frontmatter / page marker / heading parsing.
        try:
            chunks = self._chunker.chunk(
                document_id=claimed.id,
                markdown=markdown_text,
                expected_page_count=claimed.page_count,
            )
        except CanonicalMarkdownInvalid as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_CANONICAL_MARKDOWN_INVALID,
                message=f"canonical markdown invalid: {exc}",
            )
        except ChunkingEmpty:
            return await self._fail_document(
                claimed.id,
                code=CODE_CHUNKING_EMPTY,
                message="canonical markdown produced zero chunks",
            )
        except Exception as exc:  # pragma: no cover — defensive
            return await self._fail_document(
                claimed.id,
                code=CODE_CHUNKING_FAILED,
                message=f"chunker failure: {type(exc).__name__}",
            )

        # T3 — persist chunks + FTS (atomic in ChunkStore).
        try:
            await self._chunk_store.replace_document_chunks(
                document_id=claimed.id,
                library_id=claimed.library_id,
                chunks=chunks,
            )
        except Exception as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_CHUNK_PERSISTENCE_FAILED,
                message=f"chunk persistence failed: {type(exc).__name__}",
            )

        # T4 — chunking → indexing.
        try:
            await self._indexing_store.mark_indexing(claimed.id)
        except Exception as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_INDEXING_FAILED,
                message=f"mark_indexing failed: {type(exc).__name__}",
            )

        # T5 — FTS integrity gate.
        try:
            report = await self._chunk_store.verify_fts_integrity()
        except Exception as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_FTS_INTEGRITY_ERROR,
                message=f"integrity check error: {type(exc).__name__}",
            )
        if not report.ok:
            return await self._fail_document(
                claimed.id,
                code=CODE_FTS_INTEGRITY_ERROR,
                message=(
                    f"integrity failed: chunks={report.chunk_count} "
                    f"fts={report.fts_count} orphan={report.orphan_fts_rows} "
                    f"missing={report.chunks_missing_fts}"
                ),
            )

        # T6 — indexing → ready (the gate that makes R3 the first stage
        # allowed to produce ``ready``).
        try:
            await self._indexing_store.mark_ready(claimed.id)
        except Exception as exc:
            return await self._fail_document(
                claimed.id,
                code=CODE_INDEXING_FAILED,
                message=f"mark_ready failed: {type(exc).__name__}",
            )

        return IndexingResult(
            document_id=claimed.id,
            status="ready",
            chunk_count=len(chunks),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_and_verify_markdown(
        self, document: Document
    ) -> tuple[str, str]:
        """Return ``(markdown_text, parsed_source_sha256)``.

        Raises:
            _MarkdownMissing: file not on disk.
            _ShaMismatch: frontmatter source_sha256 ≠ Document.source_sha256.
        """
        try:
            markdown_text = cast(
                str,
                self._file_store.read_file(
                    document.library_id,
                    document.id,
                    _DOCUMENT_MARKDOWN_FILENAME,
                    as_text=True,
                ),
            )
        except MarkdownFileMissing as exc:
            raise _MarkdownMissing() from exc
        parsed_sha = self._extract_frontmatter_source_sha256(markdown_text)
        if parsed_sha is None:
            # No source_sha256 in frontmatter → integrity error (R2-B
            # always writes it for valid digital PDFs).
            raise _ShaMismatch("frontmatter missing source_sha256")
        if parsed_sha != document.source_sha256:
            raise _ShaMismatch(
                "frontmatter source_sha256 does not match document"
            )
        return markdown_text, parsed_sha

    @staticmethod
    def _extract_frontmatter_source_sha256(markdown: str) -> str | None:
        """Extract source_sha256 from frontmatter (JSON-quoted form)."""
        m = _FM_SOURCE_SHA256_RE.search(markdown)
        if m is None:
            return None
        captured = m.group(1).strip()
        if captured.startswith('"') and captured.endswith('"'):
            captured = captured[1:-1]
        return captured or None

    async def _fail_document(
        self,
        document_id: str,
        *,
        code: str,
        message: str,
    ) -> IndexingResult:
        """Best-effort failure compensation.

        1. ChunkStore.delete_document_chunks (best-effort; failure logged
           internally, never raised).
        2. IndexingStore.mark_failed (records stable code on Document).
        """
        # Best-effort cleanup. ChunkStore.delete_document_chunks is
        # itself atomic (chunks + FTS in one transaction).
        try:
            await self._chunk_store.delete_document_chunks(document_id=document_id)
        except Exception:
            # Cleanup failure is not the primary error; mark_failed
            # below records the original error_code so R3-D recovery
            # can converge on a subsequent pass.
            pass
        try:
            await self._indexing_store.mark_failed(
                document_id, error_code=code
            )
        except Exception:
            # mark_failed itself failed (DB closed / connection issue).
            # Document remains in chunking/indexing; R3-D startup
            # recovery handles this.
            pass
        return IndexingResult(
            document_id=document_id,
            status="failed",
            chunk_count=0,
            error_code=code,
            error_message=message,
        )

    @staticmethod
    def _result_for_non_normalizing(document: Document) -> IndexingResult:
        """Build a controlled ``IndexingResult`` for documents that are
        not in ``normalizing`` (so cannot be claimed)."""
        status = document.status
        if status == "ready":
            return IndexingResult(
                document_id=document.id,
                status="ready",
                chunk_count=0,
                error_code=None,
                error_message="already ready; not re-indexed",
            )
        if status == "failed":
            return IndexingResult(
                document_id=document.id,
                status="failed",
                chunk_count=0,
                error_code=None,
                error_message="failed; use R2 retry to re-ingest",
            )
        if status == "needs_ocr":
            return IndexingResult(
                document_id=document.id,
                status="needs_ocr",
                chunk_count=0,
                error_code=None,
                error_message="needs_ocr terminal; cannot index",
            )
        # chunking / indexing (interrupted) / extracting / uploaded / deleting
        return IndexingResult(
            document_id=document.id,
            status=status,
            chunk_count=0,
            error_code=None,
            error_message=f"document not in normalizing (status={status!r})",
        )


# ============================================================================
# Internal sentinels — never escape the orchestrator
# ============================================================================


class _MarkdownMissing(IndexingOrchestratorError):
    """Internal sentinel: document.md not on disk."""


class _ShaMismatch(IndexingOrchestratorError):
    """Internal sentinel: frontmatter source_sha256 ≠ Document.source_sha256."""


# Internal sentinels are private to this module — never part of the
# public ``IndexingOrchestrator`` API. Callers consume ``IndexingResult``
# with stable ``error_code`` strings.

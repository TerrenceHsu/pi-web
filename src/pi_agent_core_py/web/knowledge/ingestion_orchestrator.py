"""Ingestion Orchestrator (P2-R2-C1-B).

Single-Job async pipeline that composes Parser + QualityEvaluator +
Builder + Persistence + KnowledgeStore state transitions.

Per C0 contract (frozen @ 6476f96 + post-freeze correction @ 0a8f554):

- §18 Orchestrator Sequence — claim → inspect → extract → evaluate →
  build → persist → terminal commit
- §35.5 Post-freeze Correction — R2-C terminal = ``normalizing``
  (NOT ``ready``); this Orchestrator never transitions Documents to
  ``ready``
- §24 generated_at MUST NOT BE PASSED — Builder is called without
  ``generated_at``; same source.pdf + same parser ⇒ byte-identical
  Markdown + same SHA-256 across retries / restarts

Layering (directive §8):

::

    WorkerManager (C2) → IngestionStore.claim_next_pending_document()
                       → IngestionOrchestrator.run_claimed_job(job_id)

The Orchestrator is async (Store is async). Internally, sync Parser /
Builder / Persistence calls are delegated to ``asyncio.to_thread``.

The Orchestrator does NOT:

- import FastAPI / APIRouter / UploadFile
- create asyncio.Task / Queue / Event
- spawn threads / executors (only one-shot ``asyncio.to_thread`` calls)
- write to ``knowledge_chunks`` / FTS5 (R3)
- register Agent Tools (R3)
- accept ``session_id`` / ``library_id`` as parameters (Job-scoped)
- accept ``generated_at`` / ``parser`` / arbitrary user overrides
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .canonical_markdown import (
    CanonicalMarkdownArtifact,
    CanonicalMarkdownBuilder,
    CanonicalMarkdownSource,
    CanonicalMarkdownTooLarge,
    NeedsOcrNotBuildable,
)
from .files import KnowledgeFileStore
from .ingestion_store import (
    EXTRACT_STAGE,
    IngestionStore,
)
from .markdown_persistence import (
    CanonicalMarkdownPersistence,
    CanonicalMarkdownPersistenceError,
    CanonicalMarkdownWriteResult,
)
from .models import Document, IngestionJob
from .pdf_parser import (
    PdfParser,
    PdfParserError,
)
from .pdf_quality import (
    InvalidExtractionResult,
    PdfTextQualityDecision,
    PdfTextQualityEvaluator,
)
from .store import (
    Document,
    DocumentNotFoundError,
    IngestionJob,
    KnowledgeStore,
    KnowledgeStoreError,
)


# ============================================================================
# Constants
# ============================================================================

#: Hard page limit (per R0 §8.1 + R2-C0 §18.1 step 4).
DEFAULT_MAX_PDF_PAGES: Final[int] = 20

#: Stage name (per C0 §9.5; mirrors IngestionStore.EXTRACT_STAGE).
_STAGE: Final[str] = EXTRACT_STAGE

#: Safe error code recorded on terminal business outcome (needs_ocr).
_SAFE_CODE_NEEDS_OCR: Final[str] = "needs_ocr"

#: Safe error code recorded on internal-orchestrator exceptions not
#: mapped to a more specific code.
_SAFE_CODE_INTERNAL: Final[str] = "internal_ingestion_error"


# ============================================================================
# Result DTO
# ============================================================================


@dataclass(frozen=True, slots=True)
class IngestionRunResult:
    """Safe per-Job execution result.

    Per directive §14 — must NOT contain PDF body / Markdown full text /
    absolute paths / file handles / SQLite connection / raw exceptions.
    """

    document_id: str
    job_id: str
    document_status: str  # 'normalizing' / 'needs_ocr' / 'failed'
    job_status: str       # 'completed' / 'failed'
    safe_error_code: str  # '' on success; 'needs_ocr' on terminal business
    parser_id: str | None
    parser_version: str | None
    page_count: int | None
    markdown_sha256: str | None
    markdown_byte_length: int | None
    warnings: tuple[str, ...]
    duration_ms: int


# ============================================================================
# Orchestrator-internal failure wrapper
# ============================================================================


class _OrchestratorFailure(Exception):
    """Internal: Orchestrator-detected failure carrying safe_error_code.

    Carries optional ``parser_version`` / ``page_count`` metadata to write
    back to Document on terminal transition.
    """

    def __init__(
        self,
        safe_error_code: str,
        *,
        parser_version: str | None = None,
        page_count: int | None = None,
    ) -> None:
        super().__init__(safe_error_code)
        self.safe_error_code = safe_error_code
        self.parser_version = parser_version
        self.page_count = page_count


def _source_file_missing() -> _OrchestratorFailure:
    return _OrchestratorFailure("source_file_missing")


def _page_limit_exceeded(
    page_count: int, parser_version: str | None
) -> _OrchestratorFailure:
    return _OrchestratorFailure(
        "pdf_page_limit_exceeded",
        parser_version=parser_version,
        page_count=page_count,
    )


# ============================================================================
# IngestionOrchestrator
# ============================================================================


class IngestionOrchestrator:
    """Single-Job async PDF → Canonical Markdown pipeline.

    Construction::

        orchestrator = IngestionOrchestrator(
            store=knowledge_store,
            ingestion_store=ingestion_store,
            file_store=knowledge_file_store,
            parser=PypdfParser(),
            quality_evaluator=PdfTextQualityEvaluator(),
            builder=CanonicalMarkdownBuilder(),
            persistence=CanonicalMarkdownPersistence(file_store),
        )

    Per-Job execution::

        result = await orchestrator.run_claimed_job(job_id)

    The Orchestrator is stateless across calls.
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        ingestion_store: IngestionStore,
        file_store: KnowledgeFileStore,
        parser: PdfParser,
        quality_evaluator: PdfTextQualityEvaluator,
        builder: CanonicalMarkdownBuilder,
        persistence: CanonicalMarkdownPersistence,
        max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
    ) -> None:
        self._store = store
        self._ingestion_store = ingestion_store
        self._file_store = file_store
        self._parser = parser
        self._quality_evaluator = quality_evaluator
        self._builder = builder
        self._persistence = persistence
        self._max_pdf_pages = max_pdf_pages

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_claimed_job(self, job_id: str) -> IngestionRunResult:
        """Run the full pipeline for an already-claimed Job.

        Contract:
        - ``job_id`` must reference a ``status='running'`` ``stage='extract'`` Job
        - The Job's Document must be ``status='extracting'`` (set by claim)
        - Caller is responsible for cancellation handling (C2 Worker)

        Returns safe :class:`IngestionRunResult`. Maps Parser / Builder /
        Persistence errors to ``failed`` Document + Job + ``safe_error_code``.

        Raises :class:`ValueError` on invalid ``job_id`` format / wrong status.
        """
        started_at_ms = _now_ms()
        warnings: list[str] = []

        # 1-2. Load + validate Job (running + extract stage) + Document
        job = await self._load_and_validate_job(job_id)
        document = await self._store.get_document(job.document_id)

        if document.status != "extracting":
            return await self._terminal_state_conflict(
                job, document, started_at_ms, warnings
            )

        # 3-4. Resolve + verify source.pdf
        try:
            source_path = self._resolve_source_path(
                document.library_id, document.id
            )
        except _OrchestratorFailure as exc:
            return await self._handle_failure(
                exc, job, document, started_at_ms, warnings
            )
        if not source_path.exists() or not source_path.is_file():
            return await self._handle_failure(
                _source_file_missing(), job, document, started_at_ms, warnings
            )

        # 5. parser.inspect
        try:
            inspection = await asyncio.to_thread(
                self._parser.inspect, source_path
            )
        except PdfParserError as exc:
            return await self._handle_failure(
                _OrchestratorFailure(exc.safe_error_code),
                job, document, started_at_ms, warnings,
            )
        except Exception:
            return await self._handle_failure(
                _OrchestratorFailure(_SAFE_CODE_INTERNAL),
                job, document, started_at_ms, warnings,
            )
        warnings.extend(inspection.inspection_warnings)

        parser_id = self._parser.parser_id
        parser_version = self._parser.parser_version
        page_count = inspection.page_count

        # 6. Page limit
        if page_count > self._max_pdf_pages:
            return await self._handle_failure(
                _page_limit_exceeded(page_count, parser_version),
                job, document, started_at_ms, warnings,
            )

        # 7. parser.extract
        try:
            extraction = await asyncio.to_thread(
                self._parser.extract, source_path
            )
        except PdfParserError as exc:
            return await self._handle_failure(
                _OrchestratorFailure(
                    exc.safe_error_code,
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        except Exception:
            return await self._handle_failure(
                _OrchestratorFailure(
                    _SAFE_CODE_INTERNAL,
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        warnings.extend(extraction.warnings)

        # 8. Quality evaluate
        try:
            quality = await asyncio.to_thread(
                self._quality_evaluator.evaluate, extraction
            )
        except InvalidExtractionResult as exc:
            return await self._handle_failure(
                _OrchestratorFailure(
                    exc.safe_error_code,
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        except Exception:
            return await self._handle_failure(
                _OrchestratorFailure(
                    _SAFE_CODE_INTERNAL,
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        warnings.extend(quality.warnings)

        # 9. NEEDS_OCR terminal (normal business outcome)
        if quality.decision is PdfTextQualityDecision.NEEDS_OCR:
            return await self._terminal_needs_ocr(
                job, document, parser_id, parser_version, page_count,
                started_at_ms, warnings,
            )

        # 10. Document extracting → normalizing (set parser_version + page_count)
        try:
            await self._store.transition_document_status(
                document.id,
                "normalizing",
                parser_version=parser_version,
                page_count=page_count,
            )
        except (KnowledgeStoreError, ValueError) as exc:
            return await self._handle_failure(
                _OrchestratorFailure(
                    "ingestion_state_conflict",
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )

        # 11. Build CanonicalMarkdownSource (NO generated_at per §24)
        source = CanonicalMarkdownSource(
            document_id=document.id,
            source_filename=document.source_name,
            source_sha256=document.source_sha256,
            title=inspection.metadata.title,
            # generated_at omitted — per R2-B audit §6 ratified @ c2436c7
        )

        # 12. Builder.build
        try:
            artifact = await asyncio.to_thread(
                self._builder.build,
                source=source,
                extraction=extraction,
                quality=quality,
            )
        except NeedsOcrNotBuildable:
            # Defensive double-check — treat as needs_ocr
            return await self._terminal_needs_ocr(
                job, document, parser_id, parser_version, page_count,
                started_at_ms, warnings,
            )
        except CanonicalMarkdownTooLarge:
            return await self._handle_failure(
                _OrchestratorFailure(
                    "canonical_markdown_too_large",
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        except Exception:
            return await self._handle_failure(
                _OrchestratorFailure(
                    "canonical_markdown_build_failed",
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        warnings.extend(artifact.warnings)

        # 13. Persistence.write
        try:
            write_result = await asyncio.to_thread(
                self._persistence.write,
                library_id=document.library_id,
                document_id=document.id,
                artifact=artifact,
            )
        except CanonicalMarkdownPersistenceError as exc:
            return await self._handle_failure(
                _OrchestratorFailure(
                    exc.safe_error_code,
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )
        except Exception:
            return await self._handle_failure(
                _OrchestratorFailure(
                    "canonical_markdown_write_failed",
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )

        # 14. SHA verification (defensive — Persistence contract guarantees)
        if write_result.sha256 != artifact.content_sha256:
            return await self._handle_failure(
                _OrchestratorFailure(
                    "canonical_markdown_sha_mismatch",
                    parser_version=parser_version,
                    page_count=page_count,
                ),
                job, document, started_at_ms, warnings,
            )

        # 15. Terminal DB commit — per C0 §35.5 corrected:
        # Document STAYS in 'normalizing'; only Job transitions to completed
        try:
            await self._store.finish_job(job.id, status="completed")
        except Exception:
            return await self._handle_terminal_commit_failure(
                job, document, parser_id, parser_version, page_count,
                write_result, started_at_ms, warnings,
            )

        # 16. Success — Document remains 'normalizing' (R3 will take over)
        return self._build_result(
            document_id=document.id,
            job_id=job.id,
            document_status="normalizing",
            job_status="completed",
            safe_error_code="",
            parser_id=parser_id,
            parser_version=parser_version,
            page_count=page_count,
            markdown_sha256=write_result.sha256,
            markdown_byte_length=write_result.byte_length,
            warnings=tuple(warnings),
            started_at_ms=started_at_ms,
        )

    # ------------------------------------------------------------------
    # Terminal paths
    # ------------------------------------------------------------------

    async def _terminal_needs_ocr(
        self,
        job: IngestionJob,
        document: Document,
        parser_id: str,
        parser_version: str,
        page_count: int,
        started_at_ms: int,
        warnings: list[str],
    ) -> IngestionRunResult:
        await self._store.transition_document_status(
            document.id,
            "needs_ocr",
            error_code=_SAFE_CODE_NEEDS_OCR,
            parser_version=parser_version,
            page_count=page_count,
        )
        await self._store.finish_job(job.id, status="completed")
        return self._build_result(
            document_id=document.id,
            job_id=job.id,
            document_status="needs_ocr",
            job_status="completed",
            safe_error_code=_SAFE_CODE_NEEDS_OCR,
            parser_id=parser_id,
            parser_version=parser_version,
            page_count=page_count,
            markdown_sha256=None,
            markdown_byte_length=None,
            warnings=tuple(warnings),
            started_at_ms=started_at_ms,
        )

    async def _terminal_state_conflict(
        self,
        job: IngestionJob,
        document: Document,
        started_at_ms: int,
        warnings: list[str],
    ) -> IngestionRunResult:
        """Document status is not 'extracting' when Orchestrator runs."""
        # Try to fail the Job (Document may already be terminal — best-effort)
        try:
            await self._store.finish_job(
                job.id, status="failed", safe_error_code="ingestion_state_conflict"
            )
        except Exception:
            pass
        return self._build_result(
            document_id=document.id,
            job_id=job.id,
            document_status=document.status,
            job_status="failed",
            safe_error_code="ingestion_state_conflict",
            parser_id=None,
            parser_version=None,
            page_count=None,
            markdown_sha256=None,
            markdown_byte_length=None,
            warnings=tuple(warnings),
            started_at_ms=started_at_ms,
        )

    async def _handle_failure(
        self,
        exc: _OrchestratorFailure,
        job: IngestionJob,
        document: Document,
        started_at_ms: int,
        warnings: list[str],
    ) -> IngestionRunResult:
        """Transition Document + Job to failed and return safe result."""
        await self._fail_document_and_job(
            document=document,
            job=job,
            safe_error_code=exc.safe_error_code,
            parser_version=exc.parser_version,
            page_count=exc.page_count,
        )
        return self._build_result(
            document_id=document.id,
            job_id=job.id,
            document_status="failed",
            job_status="failed",
            safe_error_code=exc.safe_error_code,
            parser_id=self._parser.parser_id if exc.parser_version else None,
            parser_version=exc.parser_version,
            page_count=exc.page_count,
            markdown_sha256=None,
            markdown_byte_length=None,
            warnings=tuple(warnings),
            started_at_ms=started_at_ms,
        )

    async def _handle_terminal_commit_failure(
        self,
        job: IngestionJob,
        document: Document,
        parser_id: str,
        parser_version: str,
        page_count: int,
        write_result: CanonicalMarkdownWriteResult,
        started_at_ms: int,
        warnings: list[str],
    ) -> IngestionRunResult:
        """document.md was written but DB terminal commit failed (Case 23)."""
        await self._fail_document_and_job(
            document=document,
            job=job,
            safe_error_code="terminal_commit_failed",
            parser_version=parser_version,
            page_count=page_count,
        )
        return self._build_result(
            document_id=document.id,
            job_id=job.id,
            document_status="failed",
            job_status="failed",
            safe_error_code="terminal_commit_failed",
            parser_id=parser_id,
            parser_version=parser_version,
            page_count=page_count,
            markdown_sha256=write_result.sha256,
            markdown_byte_length=write_result.byte_length,
            warnings=tuple(warnings),
            started_at_ms=started_at_ms,
        )

    async def _fail_document_and_job(
        self,
        *,
        document: Document,
        job: IngestionJob,
        safe_error_code: str,
        parser_version: str | None,
        page_count: int | None,
    ) -> None:
        """Best-effort failure transition; suppresses secondary errors.

        Document transitions to failed only if currently extracting/normalizing;
        Job transitions to failed unconditionally.
        """
        if document.status in ("extracting", "normalizing"):
            try:
                await self._store.transition_document_status(
                    document.id,
                    "failed",
                    error_code=safe_error_code,
                    parser_version=parser_version,
                    page_count=page_count,
                )
            except (KnowledgeStoreError, ValueError):
                pass
        try:
            await self._store.finish_job(
                job.id, status="failed", safe_error_code=safe_error_code
            )
        except (KnowledgeStoreError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_and_validate_job(self, job_id: str) -> IngestionJob:
        from .models import is_valid_job_id

        if not is_valid_job_id(job_id):
            raise ValueError(f"invalid job_id: {job_id!r}")
        job = await self._store.get_job(job_id)
        if job.status != "running":
            raise ValueError(
                f"job {job_id!r} status is {job.status!r}; expected 'running'"
            )
        if job.stage != _STAGE:
            raise ValueError(
                f"job {job_id!r} stage is {job.stage!r}; expected {_STAGE!r}"
            )
        return job

    def _resolve_source_path(self, library_id: str, document_id: str) -> Path:
        """Resolve ``libraries/{lib}/documents/{doc}/source.pdf`` absolute path.

        Friend access to ``KnowledgeFileStore`` internals (same package).
        Performs full path containment + symlink escape checks.
        """
        doc_dir = self._file_store._document_dir_unchecked(
            library_id, document_id
        )
        source_path = doc_dir / "source.pdf"
        self._file_store._check_containment(source_path)
        KnowledgeFileStore._check_no_symlink_escape(
            source_path, self._file_store._root
        )
        return source_path

    @staticmethod
    def _build_result(
        *,
        document_id: str,
        job_id: str,
        document_status: str,
        job_status: str,
        safe_error_code: str,
        parser_id: str | None,
        parser_version: str | None,
        page_count: int | None,
        markdown_sha256: str | None,
        markdown_byte_length: int | None,
        warnings: tuple[str, ...],
        started_at_ms: int,
    ) -> IngestionRunResult:
        return IngestionRunResult(
            document_id=document_id,
            job_id=job_id,
            document_status=document_status,
            job_status=job_status,
            safe_error_code=safe_error_code,
            parser_id=parser_id,
            parser_version=parser_version,
            page_count=page_count,
            markdown_sha256=markdown_sha256,
            markdown_byte_length=markdown_byte_length,
            warnings=warnings,
            duration_ms=_now_ms() - started_at_ms,
        )


# ============================================================================
# Module-private helpers
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "DEFAULT_MAX_PDF_PAGES",
    "IngestionRunResult",
    "IngestionOrchestrator",
]


# Suppress unused-import lint for re-exported / type-only symbols.
_: tuple[object, ...] = (
    CanonicalMarkdownArtifact,
    CanonicalMarkdownWriteResult,
    Document,
    DocumentNotFoundError,
    IngestionJob,
)

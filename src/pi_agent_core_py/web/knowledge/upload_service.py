"""PDF Upload Service (P2-R2-C3).

Streaming multipart upload → safe staging → SHA-256 → duplicate check →
Document creation → atomic source.pdf finalize → Worker notify.

Per C0 contract (frozen @ 6476f96 + post-freeze correction @ 0a8f554):

- §10 Staged upload order:
  1. Validate library_id + status='active'
  2. Stream-read multipart → staging file (size enforce + SHA + signature)
  3. fsync staging
  4. BEGIN IMMEDIATE: duplicate check + INSERT Document
  5. COMMIT
  6. os.replace(staging → source.pdf)
  7. Cleanup staging
  8. Worker notify
  9. Return 201

- §22.2 MAX_PDF_BYTES = 25 MB (per C0 §22.2 + VirtualFileStore default)
- §11 Duplicate SHA: 409 with status-specific safe reason code
- §13.3 Worker availability gate: check before any resource creation
- §24 generated_at: NOT PASSED (Upload doesn't touch Builder, but Document
  doesn't carry generated_at either)

The Service does NOT:
- Call Parser / Builder / Orchestrator (Worker handles asynchronously)
- Wait for ingestion completion
- Accept session_id / parser / OCR / generated_at parameters
- Write to knowledge_chunks / FTS5
- Render Markdown to HTML
- Create second Document for duplicates

Friend access to KnowledgeFileStore + KnowledgeStore for staging paths
and atomic transactions (same package convention).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final, Protocol

from .files import KnowledgeFileStore
from .ingestion_store import IngestionStore
from .ingestion_worker import IngestionWorkerManager
from .models import (
    MAX_SOURCE_NAME_LENGTH,
    is_valid_library_id,
    validate_library_id_or_raise,
)
from .store import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    KnowledgeStore,
    KnowledgeStoreError,
    LibraryNotActiveError,
    LibraryNotFoundError,
)

if TYPE_CHECKING:
    from .models import Document


# ============================================================================
# Constants (per C0 §22.2 + R1 models + standard streaming)
# ============================================================================

#: Hard ceiling on uploaded PDF size (per C0 §22.2 + VirtualFileStore default).
#:
#: Per directive §十三 — C0 frozen. Matches R1 DEFAULT_MAX_FILE_SIZE.
MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024  # 25 MB

#: Multipart overhead budget (boundary + headers + filename) — used for
#: Content-Length pre-check only (per C0 §22.2 KNOWLEDGE_UPLOAD_MAX_BODY_BYTES).
#: Authoritative enforcement is via streaming byte count against MAX_PDF_BYTES.
_MULTIPART_OVERHEAD_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MB

#: Total request body pre-check threshold (hint only).
MAX_UPLOAD_BODY_BYTES: Final[int] = MAX_PDF_BYTES + _MULTIPART_OVERHEAD_BYTES  # 26 MB

#: Streaming chunk size for reading UploadFile chunks. Standard 64 KiB.
#:
#: Per directive §十三 — chunked-streaming default. C3 implementation constant
#: consistent with C0 §10 streaming requirement.
UPLOAD_CHUNK_SIZE: Final[int] = 64 * 1024  # 64 KiB

#: Maximum length of user-supplied filename (per R1 models.MAX_SOURCE_NAME_LENGTH).
MAX_FILENAME_BYTES: Final[int] = MAX_SOURCE_NAME_LENGTH  # 255

#: Fixed source filename inside the document directory (per R0 §3.1).
_SOURCE_FILENAME: Final[str] = "source.pdf"

#: PDF magic signature (first 5 bytes of any valid PDF file).
_PDF_MAGIC: Final[bytes] = b"%PDF-"

#: Fallback filename when client provides empty/invalid filename.
_FALLBACK_SOURCE_NAME: Final[str] = "upload.pdf"

#: Placeholder relative path used during initial Document INSERT.
#:
#: R1 ``create_document`` rejects empty strings, but ``document.id`` is
#: only known after INSERT. We INSERT with this non-empty placeholder,
#: then UPDATE with the canonical ``documents/{doc_id}/source.pdf`` in
#: a second transaction after the doc_id is known.
_PLACEHOLDER_RELPATH: Final[str] = "documents/_pending/source.pdf"


# ============================================================================
# Errors
# ============================================================================


class UploadServiceError(Exception):
    """Base class for UploadService errors.

    Each subclass carries a stable ``safe_error_code`` attribute — no
    absolute paths / traceback / PDF body / secret leak.
    """

    safe_error_code: str = "upload_failed"

    def __init__(self, message: str = "", *, safe_error_code: str | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        if safe_error_code is not None:
            self.safe_error_code = safe_error_code


class WorkerUnavailableError(UploadServiceError):
    """Ingestion Worker Manager is not running / not initialized."""

    safe_error_code = "worker_unavailable"


class LibraryNotMutableError(UploadServiceError):
    """Library status does not allow mutation (e.g., archived/deleting)."""

    safe_error_code = "library_not_mutable"


class InvalidUploadError(UploadServiceError):
    """Malformed multipart / missing file part / empty upload."""

    safe_error_code = "invalid_upload"


class InvalidFilenameError(UploadServiceError):
    """Filename failed safety validation."""

    safe_error_code = "invalid_filename"


class UploadTooLargeError(UploadServiceError):
    """Streaming byte count (or Content-Length) exceeded MAX_PDF_BYTES."""

    safe_error_code = "upload_too_large"


class InvalidPdfSignatureError(UploadServiceError):
    """First bytes do not match ``%PDF-`` magic."""

    safe_error_code = "invalid_pdf_signature"


class DuplicateDocumentExistsError(UploadServiceError):
    """(library_id, source_sha256) already exists in DB.

    Carries the existing Document id + status for safe response mapping.
    """

    safe_error_code = "duplicate_document"

    def __init__(
        self,
        *,
        existing_document_id: str,
        existing_status: str,
    ) -> None:
        self.existing_document_id = existing_document_id
        self.existing_status = existing_status
        super().__init__(
            f"duplicate document {existing_document_id!r} "
            f"with status {existing_status!r}"
        )


class UploadStagingFailedError(UploadServiceError):
    """Staging file write failed (disk full / permission / IO error)."""

    safe_error_code = "upload_staging_failed"


class SourceFinalizeFailedError(UploadServiceError):
    """Atomic promotion of staging → source.pdf failed."""

    safe_error_code = "source_finalize_failed"


class UploadServiceInternalError(UploadServiceError):
    """Unclassified internal error (last resort)."""

    safe_error_code = "internal_knowledge_error"


# ============================================================================
# Result DTO
# ============================================================================


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Safe upload result.

    Per directive §十二 — only Schema-existing fields; never absolute paths.
    """

    document_id: str
    library_id: str
    source_name: str
    source_sha256: str
    size_bytes: int
    document_status: str  # 'uploaded' on success


# ============================================================================
# Async chunk source protocol
# ============================================================================


class ChunkSource(Protocol):
    """Async readable chunk source (FastAPI UploadFile matches this)."""

    async def read(self, size: int = -1) -> bytes: ...


# ============================================================================
# Helpers
# ============================================================================


def sanitize_source_name(raw: str | None) -> str:
    """Sanitize user-supplied filename to a safe basename.

    Per directive §十五:
    - Treat ``/`` and ``\\`` as path separators → take basename
    - Reject NUL / CR / LF (control chars)
    - Limit to MAX_FILENAME_BYTES
    - Empty after sanitize → fallback
    - Allow safe Unicode (CJK etc.)
    """
    if raw is None:
        return _FALLBACK_SOURCE_NAME

    # Take basename: split on both / and \ (Windows + POSIX)
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]

    # Strip NUL + CR + LF + other control chars (< 0x20 except none) + DEL
    name = "".join(
        c for c in name
        if (ord(c) >= 0x20 and ord(c) != 0x7F)
    )

    # Trim leading dots / spaces (defuse ".." hidden file tricks)
    name = name.lstrip(". ")

    # Truncate to MAX_FILENAME_BYTES (in bytes — split on UTF-8 boundary)
    name_bytes = name.encode("utf-8")[:MAX_FILENAME_BYTES]
    # Decode safely (may cut multi-byte char in middle)
    name = name_bytes.decode("utf-8", errors="ignore")

    if not name:
        return _FALLBACK_SOURCE_NAME
    return name


def _now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================================
# UploadService
# ============================================================================


class UploadService:
    """Streaming PDF upload → Document + pending Job → Worker notify.

    Construction::

        upload_svc = UploadService(
            store=knowledge_store,
            file_store=knowledge_file_store,
            ingestion_store=ingestion_store,
            worker_manager=ingestion_worker_manager,
        )
        result = await upload_svc.upload_stream(
            library_id="lib_xxx",
            source_name="example.pdf",
            chunk_source=upload_file,
            content_length_hint=12345,
        )
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        file_store: KnowledgeFileStore,
        ingestion_store: IngestionStore,
        worker_manager: IngestionWorkerManager,
        max_pdf_bytes: int = MAX_PDF_BYTES,
        upload_chunk_size: int = UPLOAD_CHUNK_SIZE,
    ) -> None:
        self._store = store
        self._file_store = file_store
        self._ingestion_store = ingestion_store
        self._worker_manager = worker_manager
        self._max_pdf_bytes = max_pdf_bytes
        self._upload_chunk_size = upload_chunk_size

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upload_stream(
        self,
        *,
        library_id: str,
        source_name: str,
        chunk_source: ChunkSource,
        content_length_hint: int | None = None,
    ) -> UploadResult:
        """Stream-upload a single PDF, create Document, notify Worker.

        Per C0 §10 staged upload order.

        Raises:
            WorkerUnavailableError: manager not running.
            LibraryNotMutableError: library status != 'active'.
            InvalidUploadError: empty file / missing data.
            InvalidFilenameError: filename failed validation.
            UploadTooLargeError: size > MAX_PDF_BYTES.
            InvalidPdfSignatureError: first bytes != '%PDF-'.
            DuplicateDocumentExistsError: (lib, sha) already in DB.
            UploadStagingFailedError: staging write IO failure.
            SourceFinalizeFailedError: atomic rename failed.
            UploadServiceInternalError: unclassified internal error.
        """
        # 1. Validate library_id format
        try:
            validate_library_id_or_raise(library_id)
        except ValueError:
            raise InvalidFilenameError(f"invalid library_id: {library_id!r}") from None

        # 2. Sanitize filename
        safe_name = sanitize_source_name(source_name)

        # 3. Validate Worker Manager available BEFORE any side effect
        if self._worker_manager.state != "running":
            raise WorkerUnavailableError(
                f"worker manager state is {self._worker_manager.state!r}; "
                "expected 'running'"
            )

        # 4. Validate Library exists + active
        try:
            library = await self._store.get_library(library_id)
        except LibraryNotFoundError as exc:
            raise LibraryNotMutableError(
                f"library {library_id!r} not found"
            ) from exc
        except KnowledgeStoreError as exc:
            raise UploadServiceInternalError(
                f"library lookup failed: {type(exc).__name__}"
            ) from exc
        if library.status != "active":
            raise LibraryNotMutableError(
                f"library status is {library.status!r}; mutation rejected"
            )

        # 5. Pre-check Content-Length hint (if provided)
        if (
            content_length_hint is not None
            and content_length_hint > MAX_UPLOAD_BODY_BYTES
        ):
            raise UploadTooLargeError(
                f"Content-Length {content_length_hint} exceeds body limit "
                f"{MAX_UPLOAD_BODY_BYTES}"
            )

        # 6. Stream to staging file with SHA + size enforcement
        staging_path: Path | None
        staging_path, source_sha, size_bytes, first_bytes = (
            await self._stream_to_staging(
                library_id=library_id,
                chunk_source=chunk_source,
            )
        )

        try:
            # 7. Validate PDF signature
            if not first_bytes.startswith(_PDF_MAGIC):
                raise InvalidPdfSignatureError(
                    "first bytes do not match %PDF- magic"
                )

            # 8. BEGIN IMMEDIATE: duplicate check + INSERT Document
            try:
                document = await self._store.create_document(
                    library_id=library_id,
                    source_name=safe_name,
                    source_sha256=source_sha,
                    source_relpath=_PLACEHOLDER_RELPATH,
                    markdown_relpath=_PLACEHOLDER_RELPATH,
                    mime_type="application/pdf",
                    size_bytes=size_bytes,
                )
            except DuplicateDocumentError:
                # Find existing Document to surface its id + status
                existing = await self._find_existing_document(library_id, source_sha)
                if existing is None:
                    # Rare race — duplicate disappeared between INSERT fail + SELECT
                    raise UploadServiceInternalError(
                        "duplicate detected but existing document not found"
                    ) from None
                raise DuplicateDocumentExistsError(
                    existing_document_id=existing.id,
                    existing_status=existing.status,
                ) from None
            except LibraryNotActiveError as exc:
                # Library status changed between get_library + create_document
                raise LibraryNotMutableError(
                    f"library transitioned to non-active: {exc}"
                ) from exc
            except KnowledgeStoreError as exc:
                raise UploadServiceInternalError(
                    f"document creation failed: {type(exc).__name__}"
                ) from exc

            # 10. Compute canonical relative paths using the new doc_id
            source_relpath = f"documents/{document.id}/source.pdf"
            markdown_relpath = f"documents/{document.id}/document.md"

            # 11. Atomic source finalize: rename staging → fixed source.pdf
            #     The target is the document directory; create it first.
            try:
                await asyncio.to_thread(
                    self._promote_staging_to_source,
                    library_id,
                    document.id,
                    staging_path,
                )
            except Exception as exc:
                # Compensate: delete the just-created Document
                await self._safe_delete_document(document.id)
                raise SourceFinalizeFailedError(
                    f"os.replace staging→source.pdf failed: {type(exc).__name__}"
                ) from exc

            # Staging file is gone (renamed); clear path so finally doesn't re-delete
            staging_path = None

            # 12. Update Document with the canonical relative paths
            #     (DB has empty placeholders from step 9)
            try:
                await self._update_document_relpaths(document.id, source_relpath, markdown_relpath)
            except Exception:
                # Compensation: delete Document + source.pdf
                await asyncio.to_thread(
                    self._safe_delete_source_pdf, library_id, document.id
                )
                await self._safe_delete_document(document.id)
                raise

            # 13. Notify Worker (queue full is OK — polling fallback)
            try:
                self._worker_manager.notify_pending_job()
            except RuntimeError:
                # Manager transitioned out of running during this call.
                # Document + source.pdf are durable; polling will pick it up
                # if/when manager returns to running. Don't fail upload.
                pass

            # 14. Return success — Document status='uploaded'
            return UploadResult(
                document_id=document.id,
                library_id=library_id,
                source_name=safe_name,
                source_sha256=source_sha,
                size_bytes=size_bytes,
                document_status="uploaded",
            )
        finally:
            # Always cleanup staging file if it still exists
            if staging_path is not None:
                await asyncio.to_thread(self._safe_unlink, staging_path)

    # ------------------------------------------------------------------
    # Internal: streaming + staging
    # ------------------------------------------------------------------

    async def _stream_to_staging(
        self,
        *,
        library_id: str,
        chunk_source: ChunkSource,
    ) -> tuple[Path, str, int, bytes]:
        """Stream chunks to staging file; enforce size + compute SHA + capture magic.

        Returns:
            (staging_path, sha256_hex, size_bytes, first_bytes)
        """
        staging_path = self._make_staging_path(library_id)
        # Ensure staging dir exists
        staging_path.parent.mkdir(parents=True, exist_ok=True)

        sha = hashlib.sha256()
        size = 0
        first_bytes = b""

        try:
            # Open staging file for writing (binary)
            f = await asyncio.to_thread(staging_path.open, "wb")
        except OSError as exc:
            raise UploadStagingFailedError(
                f"staging file open failed: {type(exc).__name__}"
            ) from exc

        try:
            while True:
                chunk = await chunk_source.read(self._upload_chunk_size)
                if not chunk:
                    break
                # Capture first bytes (for PDF magic check)
                if size == 0:
                    first_bytes = chunk[:len(_PDF_MAGIC)]
                # Enforce size limit
                if size + len(chunk) > self._max_pdf_bytes:
                    raise UploadTooLargeError(
                        f"upload exceeded {self._max_pdf_bytes} bytes during streaming"
                    )
                # Update SHA + write
                sha.update(chunk)
                try:
                    await asyncio.to_thread(f.write, chunk)
                except OSError as exc:
                    raise UploadStagingFailedError(
                        f"staging write failed: {type(exc).__name__}"
                    ) from exc
                size += len(chunk)
            # Flush + fsync before close (per C0 §10.1 step 4)
            try:
                await asyncio.to_thread(self._flush_and_fsync, f)
            except OSError:
                # Best-effort — Windows fsync on some fds may fail; don't
                # fail upload just for durability hint.
                pass
        finally:
            try:
                await asyncio.to_thread(f.close)
            except Exception:
                pass

        # Validate non-empty
        if size == 0:
            raise InvalidUploadError("upload is empty")

        return staging_path, sha.hexdigest(), size, first_bytes

    @staticmethod
    def _flush_and_fsync(file_obj: BinaryIO) -> None:
        """flush + fsync a writable file object."""
        file_obj.flush()
        try:
            os.fsync(file_obj.fileno())
        except OSError:
            # Best-effort; some platforms / fd types reject fsync
            pass

    def _make_staging_path(self, library_id: str) -> Path:
        """Staging path: ``libraries/{lib}/.staging/{rand}.pdf.tmp``.

        Friend access to KnowledgeFileStore._libraries_root.
        """
        # Generate staging filename (random, no user-supplied content)
        staging_filename = f"{secrets.token_hex(16)}.pdf.tmp"
        return (
            self._file_store._libraries_root
            / library_id
            / ".staging"
            / staging_filename
        )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        """fsync an existing file (open r+b, flush, fsync, close).

        Best-effort: suppresses OSError on platforms that reject fsync
        on read-only fds (Windows fallback).
        """
        try:
            with open(path, "r+b") as f:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except OSError:
            pass

    def _promote_staging_to_source(
        self,
        library_id: str,
        document_id: str,
        staging_path: Path,
    ) -> None:
        """Atomically rename staging file to fixed source.pdf.

        Target directory: ``libraries/{lib}/documents/{doc}/source.pdf``.
        Uses ``os.replace`` (POSIX atomic; Windows also replaces).
        """
        doc_dir = self._file_store._document_dir_unchecked(library_id, document_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        target = doc_dir / _SOURCE_FILENAME
        # Re-use R1 containment + symlink escape checks (defensive)
        self._file_store._check_containment(target)
        KnowledgeFileStore._check_no_symlink_escape(
            target, self._file_store._root
        )
        os.replace(staging_path, target)

    def _safe_unlink(self, path: Path) -> None:
        """Best-effort unlink; suppress FileNotFoundError + OSError."""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    async def _safe_delete_document(self, document_id: str) -> None:
        try:
            await self._store.delete_document_hard(document_id)
        except Exception:
            pass

    def _safe_delete_source_pdf(self, library_id: str, document_id: str) -> None:
        try:
            doc_dir = self._file_store._document_dir_unchecked(library_id, document_id)
            target = doc_dir / _SOURCE_FILENAME
            self._safe_unlink(target)
            # Also try to remove now-empty doc_dir (best-effort)
            try:
                doc_dir.rmdir()
            except OSError:
                pass
        except Exception:
            pass

    async def _find_existing_document(
        self, library_id: str, source_sha256: str
    ) -> Document | None:
        """Find Document by (library_id, source_sha256)."""
        db = self._store._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_documents "
            "WHERE library_id = ? AND source_sha256 = ? LIMIT 1",
            (library_id, source_sha256),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        from .store import _row_to_document
        return _row_to_document(row)

    async def _update_document_relpaths(
        self,
        document_id: str,
        source_relpath: str,
        markdown_relpath: str,
    ) -> None:
        """Update Document.source_relpath + markdown_relpath (placeholders → real)."""
        db = self._store._require_db()
        now = _now_ms()
        async with self._store._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "UPDATE knowledge_documents "
                    "SET source_relpath = ?, markdown_relpath = ?, updated_at = ? "
                    "WHERE id = ?",
                    (source_relpath, markdown_relpath, now, document_id),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "MAX_PDF_BYTES",
    "MAX_UPLOAD_BODY_BYTES",
    "UPLOAD_CHUNK_SIZE",
    "MAX_FILENAME_BYTES",
    "UploadResult",
    "UploadService",
    "UploadServiceError",
    "WorkerUnavailableError",
    "LibraryNotMutableError",
    "InvalidUploadError",
    "InvalidFilenameError",
    "UploadTooLargeError",
    "InvalidPdfSignatureError",
    "DuplicateDocumentExistsError",
    "UploadStagingFailedError",
    "SourceFinalizeFailedError",
    "UploadServiceInternalError",
    "sanitize_source_name",
    "ChunkSource",
]


# Suppress unused-import lint for type-only re-exports.
_: tuple[object, ...] = (
    DocumentNotFoundError,
    is_valid_library_id,
)

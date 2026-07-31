"""Canonical Markdown Persistence (P2-R2-B3).

Per R2-B startup directive §27-§30:

- Thin wrapper around R1 ``KnowledgeFileStore.write_file_atomic`` for
  ``CanonicalMarkdownArtifact`` → fixed ``document.md``
- Atomic write (temp + fsync + os.replace + parent fsync) inherited
  from R1 (per P2-R0 contract §3.3)
- Path containment + symlink escape protection inherited from R1
  (per P2-R0 contract §3.4)
- Never returns absolute paths
- Never modifies Document state in DB (that's R2-C's job)
- Never deletes source.pdf
- Never writes sidecar / cache / log

Layering (directive §8):

::

    pdf_parser.py            → PdfExtractionResult
    pdf_quality.py           → PdfTextQualityResult
    canonical_markdown.py    → CanonicalMarkdownArtifact
    markdown_persistence.py  → wraps KnowledgeFileStore.write_file_atomic
                                 → fixed document.md

This module imports ``CanonicalMarkdownArtifact`` (typed DTO) +
``KnowledgeFileStore`` (R1 primitive). It does NOT import pypdf, FastAPI,
SQLite, LLM, OCR, or any network library.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .canonical_markdown import CanonicalMarkdownArtifact
from .files import (
    KnowledgeFileStore,
    KnowledgeFileStoreError,
)

# ============================================================================
# Constants
# ============================================================================

#: Fixed Markdown filename inside the document directory. MUST match
#: ``KnowledgeFileStore.FIXED_DOCUMENT_FILES`` entry — kept as a local
#: constant so this module doesn't need to import the tuple each call.
DOCUMENT_MARKDOWN_FILENAME: Final[str] = "document.md"

#: Relative path format (relative to library root, per P2-R0 §3.2).
#: ``documents/{document_id}/document.md`` — never contains the library
#: root or storage root.
_DOCUMENT_RELATIVE_PATH_FORMAT: Final[str] = (
    "documents/{document_id}/" + DOCUMENT_MARKDOWN_FILENAME
)


# ============================================================================
# Errors
# ============================================================================


class CanonicalMarkdownPersistenceError(Exception):
    """Base class for Canonical Markdown persistence errors.

    ``safe_error_code`` is a stable short string — no path / body /
    secret / traceback. Underlying ``KnowledgeFileStoreError`` cause is
    detached via ``raise ... from None`` to avoid leaking internal
    exception text (which could include absolute paths).
    """

    safe_error_code: str = "canonical_markdown_write_failed"

    def __init__(
        self, message: str = "", *, safe_error_code: str | None = None
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        if safe_error_code is not None:
            self.safe_error_code = safe_error_code


class InvalidLibraryOrDocumentID(CanonicalMarkdownPersistenceError):
    """``library_id`` / ``document_id`` failed R1 ID format check.

    Per P2-R0 §3.4 — IDs must match ``^(lib|doc)_<body>$`` body ∈
    ``[a-z0-9]{12,32}``. Anything else is rejected before touching the
    filesystem.
    """

    safe_error_code = "invalid_library_or_document_id"


class PathSafetyViolation(CanonicalMarkdownPersistenceError):
    """Resolved path escapes the knowledge root, or symlink escape
    detected by R1 ``KnowledgeFileStore``."""

    safe_error_code = "path_safety_violation"


class CanonicalMarkdownWriteFailed(CanonicalMarkdownPersistenceError):
    """Atomic write failed for any other reason (disk full / permission /
    unexpected OS error). Temp file is cleaned up by R1; old document.md
    is preserved."""

    safe_error_code = "canonical_markdown_write_failed"


class CanonicalMarkdownReadFailed(CanonicalMarkdownPersistenceError):
    """Read failed (file missing, OS error, or path safety violation
    that wasn't caught by the more specific ``PathSafetyViolation``)."""

    safe_error_code = "canonical_markdown_read_failed"


# ============================================================================
# Result DTO (immutable + slots per directive §29)
# ============================================================================


@dataclass(frozen=True, slots=True)
class CanonicalMarkdownWriteResult:
    """Safe persistence result.

    Per directive §29:

    - ``relative_path`` is the fixed relative path (relative to library
      root); NEVER contains the storage root or absolute path
    - ``byte_length`` matches the artifact's ``byte_length`` and equals
      the actual bytes written
    - ``sha256`` matches the artifact's ``content_sha256`` and equals
      the SHA-256 of the on-disk UTF-8 bytes
    """

    relative_path: str
    byte_length: int
    sha256: str


# ============================================================================
# CanonicalMarkdownPersistence
# ============================================================================


class CanonicalMarkdownPersistence:
    """Wrap R1 ``KnowledgeFileStore`` for Canonical Markdown writes.

    Construction::

        store = KnowledgeFileStore(root="data/knowledge/")
        store.ensure_root()
        persistence = CanonicalMarkdownPersistence(store)
        result = persistence.write(
            library_id="lib_xxx",
            document_id="doc_yyy",
            artifact=artifact,
        )
        # result.relative_path == "documents/doc_yyy/document.md"

    Atomic-write guarantees (inherited from R1 ``write_file_atomic``):

    1. Write to ``.{filename}.{rand}.tmp`` in same directory (same
       partition → ``os.replace`` is atomic)
    2. ``flush()`` + ``os.fsync(fd)``
    3. ``os.replace(temp, target)`` (POSIX atomic; Windows also replaces)
    4. fsync parent directory (best-effort on Windows)
    5. Temp file unlinked on any failure — old ``document.md`` preserved

    Path-safety guarantees (inherited from R1):

    - ``library_id`` / ``document_id`` format-validated before any FS op
    - ``Path.resolve()`` must stay inside knowledge root
    - Symlink escape (anywhere along the chain) rejected

    This layer does NOT:

    - Modify ``knowledge_documents`` status (R2-C orchestrator's job)
    - Delete ``source.pdf``
    - Write ``manifest.json`` (separate R2-C concern)
    - Return absolute paths
    - Side-effect outside the document directory
    """

    def __init__(self, file_store: KnowledgeFileStore) -> None:
        self._file_store = file_store

    def write(
        self,
        *,
        library_id: str,
        document_id: str,
        artifact: CanonicalMarkdownArtifact,
    ) -> CanonicalMarkdownWriteResult:
        """Atomically write ``artifact.content`` to the fixed
        ``document.md`` path for ``(library_id, document_id)``.

        Raises:
            InvalidLibraryOrDocumentID: ID format check failed.
            PathSafetyViolation: path containment / symlink escape.
            CanonicalMarkdownWriteFailed: any other write failure.
        """
        # Local imports to keep top-level import graph minimal
        from .files import (
            InvalidDocumentIDError,
            InvalidLibraryIDError,
            PathSafetyError,
        )

        try:
            self._file_store.write_file_atomic(
                library_id=library_id,
                document_id=document_id,
                filename=DOCUMENT_MARKDOWN_FILENAME,
                content=artifact.content,
            )
        except (InvalidLibraryIDError, InvalidDocumentIDError):
            # Detach cause to avoid leaking ID values in error chain
            raise InvalidLibraryOrDocumentID(
                "library_id or document_id failed format check"
            ) from None
        except PathSafetyError:
            raise PathSafetyViolation(
                "resolved path escapes knowledge root or symlink escape"
            ) from None
        except KnowledgeFileStoreError:
            raise CanonicalMarkdownWriteFailed(
                "atomic write failed"
            ) from None
        except OSError as e:
            raise CanonicalMarkdownWriteFailed(
                f"OS error during write: {type(e).__name__}"
            ) from None

        return CanonicalMarkdownWriteResult(
            relative_path=_DOCUMENT_RELATIVE_PATH_FORMAT.format(
                document_id=document_id
            ),
            byte_length=artifact.byte_length,
            sha256=artifact.content_sha256,
        )

    def read(
        self,
        *,
        library_id: str,
        document_id: str,
    ) -> str:
        """Read the canonical Markdown text for ``(library_id, document_id)``.

        Returns the UTF-8 decoded content. Raises ``FileNotFoundError``
        (via R1) if the file is absent.

        Raises:
            InvalidLibraryOrDocumentID: ID format check failed.
            PathSafetyViolation: path containment / symlink escape.
            CanonicalMarkdownReadFailed: any other read failure.
        """
        from .files import (
            FileNotFoundError_,
            InvalidDocumentIDError,
            InvalidLibraryIDError,
            PathSafetyError,
        )

        try:
            data = self._file_store.read_file(
                library_id=library_id,
                document_id=document_id,
                filename=DOCUMENT_MARKDOWN_FILENAME,
                as_text=True,
            )
        except (InvalidLibraryIDError, InvalidDocumentIDError):
            raise InvalidLibraryOrDocumentID(
                "library_id or document_id failed format check"
            ) from None
        except PathSafetyError:
            raise PathSafetyViolation(
                "resolved path escapes knowledge root or symlink escape"
            ) from None
        except FileNotFoundError_:
            raise CanonicalMarkdownReadFailed(
                "document.md not found"
            ) from None
        except KnowledgeFileStoreError:
            raise CanonicalMarkdownReadFailed(
                "read failed"
            ) from None

        # read_file returns ``bytes | str``; with as_text=True it's str.
        return data if isinstance(data, str) else str(data)

    def exists(
        self,
        *,
        library_id: str,
        document_id: str,
    ) -> bool:
        """Return True iff ``document.md`` exists for the given IDs.

        Returns False on invalid IDs (defensive — caller should validate
        upstream, but returning False is safer than raising in
        existence checks).
        """
        try:
            return self._file_store.file_exists(
                library_id=library_id,
                document_id=document_id,
                filename=DOCUMENT_MARKDOWN_FILENAME,
            )
        except (KnowledgeFileStoreError, ValueError):
            return False


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "CanonicalMarkdownPersistence",
    "CanonicalMarkdownPersistenceError",
    "CanonicalMarkdownReadFailed",
    "CanonicalMarkdownWriteFailed",
    "CanonicalMarkdownWriteResult",
    "DOCUMENT_MARKDOWN_FILENAME",
    "InvalidLibraryOrDocumentID",
    "PathSafetyViolation",
]

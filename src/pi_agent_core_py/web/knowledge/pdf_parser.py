"""PDF Parser contract — Protocol + dataclasses + error vocabulary (P2-R2-A2).

**Business layer MUST NOT import pypdf directly.** All access goes through
``PdfParser`` Protocol + ``PypdfParser`` Adapter (see ``pypdf_parser.py``).

Per P2-R2-A §10-§17 + p2-r2-0-pdf-parser-license-gate.md §16:

- Frozen dataclasses for result types (immutable + slots where supported)
- 1-based page numbering (P2-R2-A §10 / PdfPage contract)
- Safe metadata (length-limited, no binary, no path)
- Error vocabulary isolated from pypdf exceptions (P2-R2-A §17)
- Synchronous protocol (no FastAPI / SQLite / Session / Library dep)

This module imports **only** stdlib — pypdf import is lazy and lives
exclusively in ``pypdf_parser.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# ============================================================================
# Constants — text estimate algorithm parameters (P2-R2-A §14)
# ============================================================================

#: Sample at most this many pages for has_extractable_text_estimate.
TEXT_ESTIMATE_SAMPLE_COUNT: int = 3

#: Strategy: first / middle / last pages.
TEXT_ESTIMATE_STRATEGY: str = "first_middle_last"

#: Minimum non-whitespace UTF-8 chars on a sample page to count as "has text".
#: Conservative low threshold — purpose is "does this page have *any* extractable
#: text", not "is the text high-quality". 10 chars ≈ 1-2 short words.
TEXT_ESTIMATE_MIN_CHARS_PER_PAGE: int = 10

#: Maximum metadata string length (truncated safely, no exception).
MAX_METADATA_VALUE_LENGTH: int = 500


# ============================================================================
# Error vocabulary (P2-R2-A §17)
# ============================================================================


class PdfParserError(Exception):
    """Base class for all PDF Parser errors.

    Attributes:
        safe_error_code: stable short string for the API layer / DB storage.
            MUST NOT contain path / body / secret / traceback.
    """

    safe_error_code: str = "pdf_parse_failed"

    def __init__(self, message: str = "", *, safe_error_code: str | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        if safe_error_code is not None:
            self.safe_error_code = safe_error_code


class PdfFileNotFound(PdfParserError):
    safe_error_code = "pdf_file_not_found"


class PdfNotAFile(PdfParserError):
    """Path exists but is not a regular file (e.g. directory)."""
    safe_error_code = "pdf_not_a_file"


class InvalidPdf(PdfParserError):
    """File exists but pypdf cannot parse it as a PDF."""
    safe_error_code = "invalid_pdf"


class EncryptedPdf(PdfParserError):
    """PDF is encrypted; pypdf reports encryption (independent of password)."""
    safe_error_code = "encrypted_pdf"


class PdfPasswordRequired(PdfParserError):
    """Encrypted PDF cannot be opened with empty password."""
    safe_error_code = "pdf_password_required"


class PdfParseFailed(PdfParserError):
    """Generic pypdf parse failure that doesn't fit a more specific class."""
    safe_error_code = "pdf_parse_failed"


class PdfPageExtractFailed(PdfParserError):
    """Single-page extract_text() raised — per P2-R2-A §17 'page-level failure
    semantics option A' this terminates the whole extract()."""
    safe_error_code = "pdf_page_extract_failed"


class PdfParserUnavailable(PdfParserError):
    """Optional rag extra not installed (pypdf missing)."""
    safe_error_code = "pdf_parser_unavailable"


class UnsupportedParserVersion(PdfParserError):
    """Installed pypdf version is outside the allowed range."""
    safe_error_code = "unsupported_parser_version"


# ============================================================================
# Result dataclasses (P2-R2-A §10)
# ============================================================================


@dataclass(frozen=True, slots=True)
class PdfMetadata:
    """Safe, length-limited PDF metadata.

    Raw / oversized / binary values are truncated or replaced with None.
    Never contains paths, file handles, or pypdf internal objects.
    """

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    creator: str | None = None
    producer: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None


@dataclass(frozen=True, slots=True)
class PdfInspection:
    """Result of ``PdfParser.inspect()`` — metadata + text-existence estimate.

    Does NOT contain the file path or file handle.
    """

    file_size_bytes: int
    page_count: int
    encrypted: bool
    password_required: bool
    metadata: PdfMetadata
    #: True = at least one sample page has text. False = all sample pages empty.
    #: None = mixed / unable to determine (see ``inspection_warnings``).
    has_extractable_text_estimate: bool | None
    inspection_warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PdfPage:
    """Single page extraction result.

    ``page_number`` is **1-based** (P2-R2-A §10 / PdfPage contract).
    """

    page_number: int
    text: str
    extraction_warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PdfExtractionResult:
    """Result of ``PdfParser.extract()`` — full per-page extraction.

    Does NOT contain file path / handle / raw PdfReader / absolute path.
    """

    parser_id: str
    parser_version: str
    pages: tuple[PdfPage, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ============================================================================
# PdfParser Protocol (P2-R2-A §11 + p2-r2-0-pdf-parser-license-gate.md §16)
# ============================================================================


@runtime_checkable
class PdfParser(Protocol):
    """Parser Adapter — business layer talks to this Protocol, never to pypdf.

    Implementations:
    - ``PypdfParser`` (R2-A2) — only one in R2; future milestones may add
      marker / PyMuPDF adapters behind the same Protocol.

    Contract:
    - Synchronous.
    - Stateless across calls (``inspect`` / ``extract`` open fresh readers).
    - No network, no model, no OCR, no LLM, no markdown generation.
    - ``close()`` is idempotent; safe to call multiple times.
    """

    @property
    def parser_id(self) -> str:
        """Stable identifier, e.g. ``'pypdf'``."""
        ...

    @property
    def parser_version(self) -> str:
        """Actual installed parser version (e.g. ``'6.14.2'``).

        Read from ``importlib.metadata.version`` at construction time so R2
        reports record the actually-resolved version (per R2-0 §17.1).
        """
        ...

    def inspect(self, path: Path) -> PdfInspection:
        """Quick metadata probe — page_count / encrypted / file_size /
        has_extractable_text_estimate. Does NOT do full text extraction.

        Raises ``PdfParserError`` subclasses on failure; never returns a
        half-populated result.
        """
        ...

    def extract(self, path: Path) -> PdfExtractionResult:
        """Full page-by-page text extraction.

        Raises ``PdfParserError`` subclasses on failure. Page-level failure
        terminates the whole extract (P2-R2-A §17 'page-level failure
        semantics option A') — no silent missing pages.
        """
        ...

    def close(self) -> None:
        """Release any cached resources. Idempotent.

        MUST NOT close caller-owned global resources (e.g. event loops).
        MUST NOT delete the input file.
        """
        ...


# ============================================================================
# Helpers — shared with adapter
# ============================================================================


def truncate_metadata_value(value: object) -> str | None:
    """Coerce a raw pypdf metadata value into a safe, length-limited string.

    - None / empty → None
    - str → strip control chars + truncate to MAX_METADATA_VALUE_LENGTH
    - other types → str() then truncate (no exception)
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value
    else:
        try:
            s = str(value)
        except Exception:
            return None
    s = s.strip()
    if not s:
        return None
    # Strip control characters (except tab/newline which we keep)
    s = "".join(
        c for c in s
        if c == "\t" or c == "\n" or c == "\r" or (ord(c) >= 0x20 and ord(c) != 0x7F)
    )
    if len(s) > MAX_METADATA_VALUE_LENGTH:
        s = s[:MAX_METADATA_VALUE_LENGTH]
    return s or None


__all__ = [
    "EncryptedPdf",
    "InvalidPdf",
    "MAX_METADATA_VALUE_LENGTH",
    "PdfExtractionResult",
    "PdfFileNotFound",
    "PdfInspection",
    "PdfMetadata",
    "PdfNotAFile",
    "PdfPage",
    "PdfPageExtractFailed",
    "PdfParseFailed",
    "PdfParser",
    "PdfParserError",
    "PdfParserUnavailable",
    "PdfPasswordRequired",
    "TEXT_ESTIMATE_MIN_CHARS_PER_PAGE",
    "TEXT_ESTIMATE_SAMPLE_COUNT",
    "TEXT_ESTIMATE_STRATEGY",
    "UnsupportedParserVersion",
    "truncate_metadata_value",
]

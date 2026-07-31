"""PypdfParser — concrete Adapter implementing ``PdfParser`` Protocol (P2-R2-A2).

**This is the ONLY module in src/ allowed to ``import pypdf``.** Business
layer talks to ``PdfParser`` Protocol from ``pdf_parser.py``.

Per P2-R2-A §11-§19:

- Lazy ``import pypdf`` (raises ``PdfParserUnavailable`` if [rag] extra missing)
- ``parser_id = 'pypdf'``; ``parser_version`` from importlib.metadata
- Synchronous, stateless, idempotent ``close()``
- No network / model / OCR / LLM
- Encrypted PDF → ``PdfPasswordRequired`` (no auto-decrypt, no env-var password)
- Page-level extract failure → ``PdfPageExtractFailed`` (option A — no silent
  missing pages)
- Minimal text normalization (\\r\\n → \\n, NUL strip) — no Markdown / heading
  / table / column reorder (those are R2-B's job)
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .pdf_parser import (
    TEXT_ESTIMATE_MIN_CHARS_PER_PAGE,
    TEXT_ESTIMATE_SAMPLE_COUNT,
    EncryptedPdf,
    InvalidPdf,
    PdfExtractionResult,
    PdfFileNotFound,
    PdfInspection,
    PdfMetadata,
    PdfNotAFile,
    PdfPage,
    PdfPageExtractFailed,
    PdfParser,
    PdfParserError,
    PdfParserUnavailable,
    PdfPasswordRequired,
    UnsupportedParserVersion,
    truncate_metadata_value,
)

# ============================================================================
# Allowed version range — must match pyproject [rag] extra
# ============================================================================

_ALLOWED_MAJOR: int = 6  # pypdf>=6.0,<7 per R2-0 License Gate §17.1


# ============================================================================
# PypdfParser
# ============================================================================


class PypdfParser:
    """Adapter wrapping pypdf for the R2 PDF Parser contract.

    Construction::

        parser = PypdfParser()
        inspection = parser.inspect(Path("paper.pdf"))
        result = parser.extract(Path("paper.pdf"))
        parser.close()

    Raises ``PdfParserUnavailable`` if [rag] extra is not installed.
    Raises ``UnsupportedParserVersion`` if installed pypdf is outside 6.x.
    """

    PARSER_ID: str = "pypdf"

    def __init__(self) -> None:
        # Lazy import — base package must be importable without pypdf installed.
        try:
            import pypdf  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as e:
            raise PdfParserUnavailable(
                "pypdf is not installed; install the [rag] optional extra"
            ) from e

        try:
            self._version: str = version("pypdf")
        except PackageNotFoundError as e:
            raise PdfParserUnavailable(
                "pypdf distribution metadata not found"
            ) from e

        # Verify major version is within allowed range
        try:
            major = int(self._version.split(".")[0])
        except (ValueError, IndexError):
            raise UnsupportedParserVersion(
                f"cannot parse pypdf version: {self._version!r}"
            ) from None
        if major != _ALLOWED_MAJOR:
            raise UnsupportedParserVersion(
                f"pypdf {self._version} is outside allowed range "
                f"(expected {_ALLOWED_MAJOR}.x; "
                f"re-run License Gate per p2-r2-0-pdf-parser-license-gate.md §20)"
            )

        self._closed: bool = False

    # ------------------------------------------------------------------
    # PdfParser Protocol implementation
    # ------------------------------------------------------------------

    @property
    def parser_id(self) -> str:
        return self.PARSER_ID

    @property
    def parser_version(self) -> str:
        return self._version

    def inspect(self, path: Path) -> PdfInspection:
        """Quick metadata probe. See ``pdf_parser.PdfParser.inspect``."""
        self._require_open()
        self._require_real_file(path)

        from pypdf import PdfReader
        from pypdf.errors import PdfReadError as _PypdfReadError

        file_size = path.stat().st_size
        try:
            reader = PdfReader(str(path))
        except _PypdfReadError as e:
            raise InvalidPdf(f"pypdf cannot parse: {type(e).__name__}") from e
        except FileNotFoundError as e:
            raise PdfFileNotFound(str(path)) from e
        except Exception as e:
            raise InvalidPdf(f"unexpected pypdf open error: {type(e).__name__}") from e

        try:
            encrypted = bool(getattr(reader, "is_encrypted", False))
            password_required = False
            if encrypted:
                # Try empty-password decrypt; pypdf returns 0 on failure
                try:
                    result_code = reader.decrypt("")
                except Exception:
                    result_code = 0
                password_required = result_code == 0

            if password_required:
                # Cannot probe pages / metadata without password — return
                # minimal inspection with a warning. Business layer (R2-B)
                # decides what to do; R2-A only reports facts.
                return PdfInspection(
                    file_size_bytes=file_size,
                    page_count=0,
                    encrypted=True,
                    password_required=True,
                    metadata=PdfMetadata(),
                    has_extractable_text_estimate=None,
                    inspection_warnings=(
                        "encrypted: password required; page_count / metadata / "
                        "text estimate unavailable without password",
                    ),
                )

            # Either not encrypted, or empty-password decrypt succeeded
            page_count = len(reader.pages)

            metadata = self._safe_metadata(reader)
            text_estimate, warnings = self._estimate_has_text(reader, page_count)

            return PdfInspection(
                file_size_bytes=file_size,
                page_count=page_count,
                encrypted=encrypted,
                password_required=password_required,
                metadata=metadata,
                has_extractable_text_estimate=text_estimate,
                inspection_warnings=tuple(warnings),
            )
        finally:
            # pypdf PdfReader holds an open file stream when given a path;
            # release it explicitly.
            try:
                reader.stream.close()
            except Exception:
                pass

    def extract(self, path: Path) -> PdfExtractionResult:
        """Full page-by-page text extraction. See ``pdf_parser.PdfParser.extract``."""
        self._require_open()
        self._require_real_file(path)

        from pypdf import PdfReader
        from pypdf.errors import PdfReadError as _PypdfReadError

        try:
            reader = PdfReader(str(path))
        except _PypdfReadError as e:
            raise InvalidPdf(f"pypdf cannot parse: {type(e).__name__}") from e
        except FileNotFoundError as e:
            raise PdfFileNotFound(str(path)) from e
        except Exception as e:
            raise InvalidPdf(f"unexpected pypdf open error: {type(e).__name__}") from e

        try:
            # Refuse encrypted PDFs per P2-R2-A §18 (no auto-decrypt)
            if getattr(reader, "is_encrypted", False):
                try:
                    decrypt_result = reader.decrypt("")
                except Exception:
                    decrypt_result = 0
                if decrypt_result == 0:
                    raise PdfPasswordRequired(
                        "PDF is encrypted and requires a password; "
                        "R2 does not accept passwords"
                    )
                # Empty password succeeded — technically decrypted; continue
                # but treat as EncryptedPdf for safety
                raise EncryptedPdf(
                    "PDF is encrypted (empty password accepted); "
                    "R2 refuses encrypted PDFs regardless"
                )

            pages_count = len(reader.pages)
            pages: list[PdfPage] = []
            for i in range(pages_count):
                page_number = i + 1  # 1-based
                try:
                    raw = reader.pages[i].extract_text()
                except Exception as e:
                    # Page-level failure → option A: terminate whole extract
                    raise PdfPageExtractFailed(
                        f"extract_text failed on page {page_number}: "
                        f"{type(e).__name__}"
                    ) from e
                text = self._normalize_text(raw)
                pages.append(
                    PdfPage(page_number=page_number, text=text, extraction_warnings=())
                )

            return PdfExtractionResult(
                parser_id=self.parser_id,
                parser_version=self.parser_version,
                pages=tuple(pages),
                warnings=(),
            )
        finally:
            try:
                reader.stream.close()
            except Exception:
                pass

    def close(self) -> None:
        """Idempotent. PypdfParser holds no long-lived resources between calls
        (each inspect/extract opens a fresh PdfReader). close() is a no-op
        but Protocol requires it for future adapters that may pool resources."""
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            raise PdfParserError("parser is closed")

    @staticmethod
    def _require_real_file(path: Path) -> None:
        if not path.exists():
            raise PdfFileNotFound(f"path does not exist: {type(path).__name__}")
        if not path.is_file():
            raise PdfNotAFile(f"path is not a regular file: {type(path).__name__}")

    @staticmethod
    def _safe_metadata(reader: object) -> PdfMetadata:
        """Extract safe metadata fields. Failures on individual fields do NOT
        abort the whole inspection (P2-R2-A §10 / PdfMetadata)."""
        try:
            raw_meta = reader.metadata  # type: ignore[attr-defined]
        except Exception:
            return PdfMetadata()
        if raw_meta is None:
            return PdfMetadata()

        def _get(key: str) -> str | None:
            try:
                return truncate_metadata_value(raw_meta.get(key))  # type: ignore[attr-defined]
            except Exception:
                return None

        return PdfMetadata(
            title=_get("/Title"),
            author=_get("/Author"),
            subject=_get("/Subject"),
            creator=_get("/Creator"),
            producer=_get("/Producer"),
            creation_date=_get("/CreationDate"),
            modification_date=_get("/ModDate"),
        )

    @staticmethod
    def _estimate_has_text(
        reader: object,
        page_count: int,
    ) -> tuple[bool | None, list[str]]:
        """Sample first / middle / last pages and decide:
        - All sample pages have ≥ MIN_CHARS_PER_PAGE non-whitespace → True
        - All sample pages have 0 non-whitespace chars → False
        - Mixed / unable to determine → None (+ warning)
        """
        warnings: list[str] = []
        if page_count <= 0:
            return None, warnings

        # Choose sample indices (0-based): first, middle, last (deduped)
        sample_indices: list[int] = []
        first = 0
        last = page_count - 1
        middle = page_count // 2
        for idx in (first, middle, last):
            if idx not in sample_indices and idx < page_count:
                sample_indices.append(idx)
        # Cap to TEXT_ESTIMATE_SAMPLE_COUNT
        sample_indices = sample_indices[:TEXT_ESTIMATE_SAMPLE_COUNT]

        results: list[bool] = []  # True if page has text, False if empty
        for idx in sample_indices:
            try:
                raw = reader.pages[idx].extract_text()  # type: ignore[attr-defined]
                text = raw or ""
                non_ws = sum(1 for c in text if not c.isspace())
                results.append(non_ws >= TEXT_ESTIMATE_MIN_CHARS_PER_PAGE)
            except Exception as e:
                warnings.append(
                    f"sample page {idx + 1} extract failed: {type(e).__name__}"
                )

        if not results:
            return None, warnings
        if all(results):
            return True, warnings
        if not any(results):
            return False, warnings
        return None, warnings + ["mixed text presence across sample pages"]

    @staticmethod
    def _normalize_text(raw: object) -> str:
        """Minimal, lossless-leaning normalization (P2-R2-A §16):

        - None → ''
        - Non-str → str()
        - \\r\\n → \\n
        - \\r → \\n
        - Strip NUL (\\x00)
        - Return str

        Does NOT compress whitespace, dedupe newlines, escape markdown, or
        reorder text. Those are R2-B's job.
        """
        if raw is None:
            return ""
        if not isinstance(raw, str):
            try:
                s = str(raw)
            except Exception:
                return ""
        else:
            s = raw
        # Normalize line endings
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        # Strip NUL bytes (PDFs sometimes contain them between glyphs)
        s = s.replace("\x00", "")
        return s


# ============================================================================
# Constructor helper
# ============================================================================


def create_pypdf_parser() -> PdfParser:
    """Factory used by R2-B/R2-C composition to obtain a parser instance.

    Returns a ``PdfParser`` Protocol view; concrete class is implementation
    detail. Raises ``PdfParserUnavailable`` / ``UnsupportedParserVersion`` if
    preconditions fail.
    """
    return PypdfParser()


__all__ = [
    "PypdfParser",
    "create_pypdf_parser",
]

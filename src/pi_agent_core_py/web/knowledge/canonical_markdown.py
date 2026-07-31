"""Canonical Markdown Builder (P2-R2-B2).

Per p2-r0-amendment-2.md §2.2 + R2-B startup directive §13-§23:

- Pure function over ``CanonicalMarkdownSource`` + ``PdfExtractionResult`` +
  ``PdfTextQualityResult`` → ``CanonicalMarkdownArtifact``
- Deterministic — same inputs produce byte-identical UTF-8 output and
  matching SHA-256 (per directive §26)
- Conservative text normalization (no NFKC, no LLM, no reflow)
- Stable page marker ``<!-- page:N -->`` with collision escaping
- Conservative heading heuristic (high-confidence patterns only)
- JSON-quoted string scalars (valid YAML, no PyYAML dependency)
- No I/O / no DB / no FastAPI / no pypdf / no LLM / no OCR

R2-B startup directive §23 — Canonical Markdown is an **evidence artifact,
not trusted HTML**. Future UI rendering must disable raw HTML and remote
images or sanitize them. The Builder itself does NOT render Markdown.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from .models import is_valid_document_id
from .pdf_parser import PdfExtractionResult
from .pdf_quality import PdfTextQualityDecision, PdfTextQualityResult

# ============================================================================
# Constants — schema + format
# ============================================================================

#: Frozen Canonical Markdown schema version (amendment-2 §2.2).
SCHEMA_VERSION: Final[str] = "pi-agent-canonical-markdown/v1"

#: Fixed page marker format (amendment-2 §2.4). 1-based, monotonically
#: increasing, marker alone on its own line.
PAGE_MARKER_FORMAT: Final[str] = "<!-- page:{n} -->"

#: Regex for collision detection in source text (directive §19). Matches any
#: line that — after optional whitespace — looks like a system page marker.
_PAGE_MARKER_COLLISION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\s*)<!--\s*page:(\d+)\s*-->(\s*)$",
    re.MULTILINE,
)

# ============================================================================
# Constants — input size guards
# ============================================================================

#: Maximum length of ``source_filename`` (basename; aligned with R1
#: ``MAX_SOURCE_NAME_LENGTH`` but checked locally to avoid coupling).
MAX_SOURCE_FILENAME_LENGTH: Final[int] = 255

#: Maximum length of ``title`` (from ``PdfMetadata.title`` — already
#: truncated to ``MAX_METADATA_VALUE_LENGTH=500`` by ``pypdf_parser``,
#: re-checked here for defense in depth).
MAX_TITLE_LENGTH: Final[int] = 500

#: Maximum length of ``parser_id`` / ``parser_version``.
MAX_PARSER_FIELD_LENGTH: Final[int] = 64

#: Maximum length of ``generated_at`` (UTC ISO 8601 string).
MAX_GENERATED_AT_LENGTH: Final[int] = 64

#: Output size guard (directive §25). Conservative derived guard based on
#: the 20-page upload cap (contract R3) — extracted Markdown from a
#: 20-page PDF is well under 1 MB; this guard catches only pathologic
#: inflation, not legitimate content.
MAX_CANONICAL_MARKDOWN_BYTES: Final[int] = 50 * 1024 * 1024  # 50 MB

# ============================================================================
# Constants — heading heuristic (directive §20-§21)
# ============================================================================

#: Maximum line length eligible for heading conversion.
_MAX_HEADING_LINE_LENGTH: Final[int] = 80

#: Maximum whitespace-separated word count for English patterns.
_MAX_HEADING_WORDS: Final[int] = 12

#: Sentence-ending punctuation that disqualifies heading conversion.
_SENTENCE_END_PUNCT: Final[frozenset[str]] = frozenset(
    ".!?。！？；;:："
)

#: Regex patterns in evaluation order (most specific first).
#: Each rule: (regex, heading_level). Last group is the title text.
_HEADING_RULES: Final[tuple[tuple[re.Pattern[str], int], ...]] = (
    # 1.2.3 Title — H4 (deepest numeric)
    (re.compile(r"^\d+\.\d+\.\d+[.\s]+(.+)$"), 4),
    # 1.2 Title — H3
    (re.compile(r"^\d+\.\d+[.\s]+(.+)$"), 3),
    # Chapter N [Title] — H2
    (re.compile(r"^Chapter\s+\d+(?:\s+(.+))?$", re.IGNORECASE), 2),
    # SECTION N [Title] — H2
    (re.compile(r"^Section\s+\d+(?:\s+(.+))?$", re.IGNORECASE), 2),
    # 第X章 Title — H2 (Chinese chapter)
    (re.compile(r"^第[一二三四五六七八九十百千零〇\d]+章\s+(.+)$"), 2),
    # 一、 Title — H2 (Chinese numbered)
    (re.compile(r"^[一二三四五六七八九十]+、(.+)$"), 2),
    # （一） Title / (1) Title — H3 (Chinese paren-numbered)
    (re.compile(r"^[（(][一二三四五六七八九十\d]+[)）]\s*(.+)$"), 3),
    # 1. Title / 1 Title — H2 (single-digit numeric, must come after multi-digit)
    (re.compile(r"^\d+[.\s]+(.+)$"), 2),
)

#: All-caps short title — H2 (must contain letters; not just digits/punct).
_ALL_CAPS_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z\s&]{2,39}$"
)

#: Reject patterns that look like numbers / dates / versions / URLs.
_NUMBER_LIKE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)+"          # decimals / versions (3.14, 1.2.3)
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"  # dates (2026-07-31)
    r"|https?://\S+"          # URLs
    r"|ftp://\S+"
    r")$"
)


# ============================================================================
# Errors
# ============================================================================


class CanonicalMarkdownError(Exception):
    """Base class for Canonical Markdown Builder errors.

    ``safe_error_code`` is a stable short string — no path / body / secret.
    """

    safe_error_code: str = "canonical_markdown_build_failed"

    def __init__(
        self, message: str = "", *, safe_error_code: str | None = None
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        if safe_error_code is not None:
            self.safe_error_code = safe_error_code


class InvalidCanonicalMarkdownSource(CanonicalMarkdownError):
    """``CanonicalMarkdownSource`` failed validation (bad document_id /
    source_filename / source_sha256 / title / generated_at)."""

    safe_error_code = "invalid_canonical_markdown_source"


class NeedsOcrNotBuildable(CanonicalMarkdownError):
    """Builder was called with a ``needs_ocr`` quality result.

    Per directive §24 / §28, ``needs_ocr`` documents must NOT have a
    Canonical Markdown artifact. The caller (R2-C orchestrator) is
    responsible for not calling ``build()`` in this case. Raising here
    is defense-in-depth.
    """

    safe_error_code = "needs_ocr_not_buildable"


class CanonicalMarkdownTooLarge(CanonicalMarkdownError):
    """Built artifact exceeds ``MAX_CANONICAL_MARKDOWN_BYTES``."""

    safe_error_code = "canonical_markdown_too_large"


# ============================================================================
# Source / Artifact DTOs (immutable + slots per directive §9)
# ============================================================================


@dataclass(frozen=True, slots=True)
class CanonicalMarkdownSource:
    """Safe metadata required to build a Canonical Markdown artifact.

    Per directive §9 — only safe metadata accepted. No absolute paths,
    no storage roots, no arbitrary metadata dicts.

    ``title`` and ``generated_at`` are optional:

    - ``title=None`` (default) → frontmatter omits the ``title`` field
    - ``generated_at=None`` (default) → frontmatter omits the field; the
      Builder never calls ``datetime.now()``. Callers that want a
      timestamp must pass an explicit UTC ISO 8601 string.
    """

    document_id: str
    source_filename: str
    source_sha256: str
    title: str | None = None
    generated_at: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalMarkdownArtifact:
    """Result of ``CanonicalMarkdownBuilder.build``.

    Per directive §9:

    - ``content`` is the full Canonical Markdown text, ending in a single
      ``\\n`` and encoded as UTF-8 (no BOM) for SHA-256 computation
    - ``content_sha256`` is SHA-256 over the UTF-8 bytes of ``content``
    - ``byte_length`` is the length of the UTF-8 bytes (equals the
      denominator used to compute ``content_sha256``)
    - ``quality`` is the input ``PdfTextQualityResult`` echoed back so
      downstream stages (persistence, future chunker) have it without
      re-running the evaluator
    - ``warnings`` is the merged set of builder warnings + quality
      warnings (stable order: builder warnings first, then quality)
    """

    schema_version: str
    content: str
    content_sha256: str
    page_count: int
    quality: PdfTextQualityResult
    warnings: tuple[str, ...]
    byte_length: int


# ============================================================================
# Builder
# ============================================================================


class CanonicalMarkdownBuilder:
    """Stateless Canonical Markdown builder.

    Construction::

        builder = CanonicalMarkdownBuilder()
        artifact = builder.build(
            source=CanonicalMarkdownSource(...),
            extraction=parser.extract(path),
            quality=PdfTextQualityEvaluator().evaluate(extraction),
        )

    The builder refuses to build when ``quality.decision is NEEDS_OCR``
    (raises ``NeedsOcrNotBuildable``). The caller must skip the build
    for needs_ocr documents and not persist a Markdown artifact.

    Determinism (directive §26):

    - Same inputs → byte-identical UTF-8 ``content`` and matching SHA-256
    - No ``datetime.now()`` / ``uuid4()`` / ``random`` / locale-dependent
      sorting
    - Field order in frontmatter is fixed
    """

    def build(
        self,
        *,
        source: CanonicalMarkdownSource,
        extraction: PdfExtractionResult,
        quality: PdfTextQualityResult,
    ) -> CanonicalMarkdownArtifact:
        self._validate_source(source)
        self._validate_extraction(extraction)
        if quality.decision is PdfTextQualityDecision.NEEDS_OCR:
            raise NeedsOcrNotBuildable(
                "needs_ocr documents must not produce a Canonical Markdown artifact"
            )

        page_count = len(extraction.pages)
        builder_warnings: list[str] = []

        frontmatter = self._build_frontmatter(source, extraction)
        body = self._build_body(extraction, builder_warnings)

        # Single trailing newline (directive §26).
        content = f"{frontmatter}\n\n{body}"
        if not content.endswith("\n"):
            content = content + "\n"
        else:
            # Collapse any accidental multi-trailing-newline to exactly one
            content = content.rstrip("\n") + "\n"

        byte_length = len(content.encode("utf-8"))
        if byte_length > MAX_CANONICAL_MARKDOWN_BYTES:
            raise CanonicalMarkdownTooLarge(
                f"canonical markdown exceeds guard: {byte_length} > "
                f"{MAX_CANONICAL_MARKDOWN_BYTES} bytes"
            )

        content_sha256 = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

        # Merge warnings: builder warnings first, then quality warnings.
        # De-duplicate while preserving order.
        merged: list[str] = []
        seen: set[str] = set()
        for code in (*builder_warnings, *quality.warnings):
            if code not in seen:
                merged.append(code)
                seen.add(code)

        return CanonicalMarkdownArtifact(
            schema_version=SCHEMA_VERSION,
            content=content,
            content_sha256=content_sha256,
            page_count=page_count,
            quality=quality,
            warnings=tuple(merged),
            byte_length=byte_length,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_source(source: CanonicalMarkdownSource) -> None:
        # document_id
        if not is_valid_document_id(source.document_id):
            raise InvalidCanonicalMarkdownSource(
                "invalid document_id format"
            )

        # source_filename — basename only
        fname = source.source_filename
        if not isinstance(fname, str) or not fname:
            raise InvalidCanonicalMarkdownSource(
                "source_filename must be a non-empty string"
            )
        if len(fname) > MAX_SOURCE_FILENAME_LENGTH:
            raise InvalidCanonicalMarkdownSource(
                "source_filename too long"
            )
        # Reject path separators, parent-dir segments, NUL, newlines
        forbidden_chars = ("/", "\\", "\x00", "\n", "\r")
        if any(c in fname for c in forbidden_chars):
            raise InvalidCanonicalMarkdownSource(
                "source_filename must be a basename (no separators)"
            )
        if fname in (".", ".."):
            raise InvalidCanonicalMarkdownSource(
                "source_filename must not be a path segment"
            )

        # source_sha256 — 64 lowercase hex
        if not re.fullmatch(r"[0-9a-f]{64}", source.source_sha256):
            raise InvalidCanonicalMarkdownSource(
                "source_sha256 must be 64 lowercase hex characters"
            )

        # title — optional, length-limited, single-line
        if source.title is not None:
            if not isinstance(source.title, str):
                raise InvalidCanonicalMarkdownSource(
                    "title must be a string or None"
                )
            if len(source.title) > MAX_TITLE_LENGTH:
                raise InvalidCanonicalMarkdownSource("title too long")
            if "\n" in source.title or "\r" in source.title:
                raise InvalidCanonicalMarkdownSource(
                    "title must be single-line"
                )

        # generated_at — optional, length-limited, single-line
        if source.generated_at is not None:
            if not isinstance(source.generated_at, str):
                raise InvalidCanonicalMarkdownSource(
                    "generated_at must be a string or None"
                )
            if len(source.generated_at) > MAX_GENERATED_AT_LENGTH:
                raise InvalidCanonicalMarkdownSource(
                    "generated_at too long"
                )
            if "\n" in source.generated_at or "\r" in source.generated_at:
                raise InvalidCanonicalMarkdownSource(
                    "generated_at must be single-line"
                )

    @staticmethod
    def _validate_extraction(extraction: PdfExtractionResult) -> None:
        # parser_id / parser_version: non-empty, length-limited, no newlines
        for field_value, field_name in (
            (extraction.parser_id, "parser_id"),
            (extraction.parser_version, "parser_version"),
        ):
            if not isinstance(field_value, str) or not field_value:
                raise InvalidCanonicalMarkdownSource(
                    f"{field_name} must be a non-empty string"
                )
            if len(field_value) > MAX_PARSER_FIELD_LENGTH:
                raise InvalidCanonicalMarkdownSource(
                    f"{field_name} too long"
                )
            if "\n" in field_value or "\r" in field_value:
                raise InvalidCanonicalMarkdownSource(
                    f"{field_name} must be single-line"
                )

    # ------------------------------------------------------------------
    # Frontmatter
    # ------------------------------------------------------------------

    @staticmethod
    def _build_frontmatter(
        source: CanonicalMarkdownSource,
        extraction: PdfExtractionResult,
    ) -> str:
        """Build deterministic frontmatter per amendment-2 §2.2 field order.

        Field order is fixed:
        schema / document_id / source_filename / source_sha256 /
        parser_id / parser_version / page_count / title / generated_at

        Optional fields (title, generated_at) are omitted when None.
        String scalars are JSON-quoted (valid YAML scalar) — avoids
        colon / newline / ``---`` / YAML tag injection.
        """
        page_count = len(extraction.pages)

        fields: list[tuple[str, str]] = [
            ("schema", json.dumps(SCHEMA_VERSION, ensure_ascii=False)),
            ("document_id", json.dumps(source.document_id, ensure_ascii=False)),
            (
                "source_filename",
                json.dumps(source.source_filename, ensure_ascii=False),
            ),
            (
                "source_sha256",
                json.dumps(source.source_sha256, ensure_ascii=False),
            ),
            ("parser_id", json.dumps(extraction.parser_id, ensure_ascii=False)),
            (
                "parser_version",
                json.dumps(extraction.parser_version, ensure_ascii=False),
            ),
            ("page_count", str(page_count)),
        ]
        if source.title is not None:
            fields.append(("title", json.dumps(source.title, ensure_ascii=False)))
        if source.generated_at is not None:
            fields.append(
                ("generated_at", json.dumps(source.generated_at, ensure_ascii=False))
            )

        lines = ["---"]
        for key, value in fields:
            lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------

    @staticmethod
    def _build_body(
        extraction: PdfExtractionResult,
        builder_warnings: list[str],
    ) -> str:
        """Build the page-by-page body with system page markers.

        Each page (including empty pages) gets exactly one marker; markers
        are 1-based, monotonically increasing, equal to the page_number
        sequence of ``extraction.pages``.
        """
        sections: list[str] = []
        for page in extraction.pages:
            normalized = CanonicalMarkdownBuilder._normalize_page_text(page.text)
            heading_applied = CanonicalMarkdownBuilder._apply_heading_heuristic(
                normalized
            )
            marker = PAGE_MARKER_FORMAT.format(n=page.page_number)
            # Marker on its own line, blank line between marker and body,
            # blank line between sections.
            sections.append(f"{marker}\n\n{heading_applied}")

        # Join sections with blank line; ensure single trailing newline at the
        # top-level (handled by the caller).
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Text normalization (directive §13)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_page_text(text: str) -> str:
        """Deterministic, conservative normalization.

        - ``\\r\\n`` → ``\\n``
        - ``\\r`` → ``\\n``
        - Strip NUL
        - Remove control chars except ``\\n`` (0x0A) and ``\\t`` (0x09)
        - Strip trailing whitespace per line
        - Strip leading/trailing blank lines
        - Compress 3+ consecutive blank lines to 2
        - Escape source-text page marker collisions
        """
        # CRLF / CR normalization
        s = text.replace("\r\n", "\n").replace("\r", "\n")
        # Strip NUL
        s = s.replace("\x00", "")
        # Remove dangerous control chars (keep \n and \t)
        s = "".join(
            c
            for c in s
            if c in ("\n", "\t") or (0x20 <= ord(c) != 0x7F)
        )
        # Trailing whitespace per line
        s = "\n".join(line.rstrip() for line in s.split("\n"))
        # Strip leading/trailing blank lines
        s = s.strip("\n")
        # Compress 3+ blank lines to 2
        while "\n\n\n\n" in s:
            s = s.replace("\n\n\n\n", "\n\n\n")
        # Escape page marker collisions in source
        s = CanonicalMarkdownBuilder._escape_page_marker_collisions(s)
        return s

    @staticmethod
    def _escape_page_marker_collisions(text: str) -> str:
        """Escape source-text lines that match the system marker pattern.

        Per directive §19, lines matching ``^(\\s*)<!--\\s*page:\\d+\\s*-->\\s*$``
        must be deterministically escaped so they no longer match the
        system marker parser. The escape is a leading backslash before
        ``!`` — visible to humans, breaks the regex, preserves the
        original information.

        System-generated markers are NOT subject to this escape (they
        are inserted after normalization).
        """

        def _escape(match: re.Match[str]) -> str:
            leading_ws = match.group(1)
            page_num = match.group(2)
            trailing_ws = match.group(3)
            return f"{leading_ws}\\<!-- page:{page_num} -->{trailing_ws}"

        return _PAGE_MARKER_COLLISION_RE.sub(_escape, text)

    # ------------------------------------------------------------------
    # Heading heuristic (directive §20-§22)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_heading_heuristic(page_text: str) -> str:
        """Conservatively convert high-confidence heading patterns.

        Rules (directive §20-§21):

        - Line must satisfy block boundary (preceded/followed by blank or
          page edge)
        - Line length ≤ ``_MAX_HEADING_LINE_LENGTH``
        - Line must not end with sentence-ending punctuation
        - Line must not look like a number / date / version / URL
        - Line must not already start with ``#`` (don't double-process)
        - Level capped at 4 (no H5/H6); body never produces H1
        """
        if not page_text:
            return page_text

        lines = page_text.split("\n")
        output: list[str] = []
        last_idx = len(lines) - 1

        for i, line in enumerate(lines):
            converted = None
            # Block boundary: previous line blank or start; next line blank or end
            at_start = (i == 0) or (lines[i - 1].strip() == "")
            at_end = (i == last_idx) or (lines[i + 1].strip() == "")
            if at_start and at_end:
                converted = CanonicalMarkdownBuilder._try_match_heading(line)
            if converted is not None:
                level, title = converted
                output.append(f"{'#' * level} {title}")
            else:
                output.append(line)
        return "\n".join(output)

    @staticmethod
    def _try_match_heading(line: str) -> tuple[int, str] | None:
        """Return ``(heading_level, title_text)`` or ``None``.

        Multi-rule match: tries specific patterns first (multi-digit
        numeric, Chinese chapter, etc.) before falling back to single-
        digit numeric and all-caps.
        """
        stripped = line.strip()
        if not stripped:
            return None
        # Don't double-process existing Markdown headings
        if stripped.startswith("#"):
            return None
        if len(stripped) > _MAX_HEADING_LINE_LENGTH:
            return None
        if stripped[-1] in _SENTENCE_END_PUNCT:
            return None
        # Reject pure numbers / versions / dates / URLs
        if _NUMBER_LIKE_RE.match(stripped):
            return None

        # Try numbered / Chinese patterns first (most specific)
        for pattern, level in _HEADING_RULES:
            match = pattern.match(stripped)
            if match:
                groups = match.groups()
                # Last group is title text; if optional, may be None (e.g.
                # "Chapter 1" with no title — treat as marker, not heading)
                title = groups[-1]
                if title is None:
                    continue
                title = title.strip()
                if not title:
                    continue
                # English: cap word count
                if not _has_cjk(title) and len(title.split()) > _MAX_HEADING_WORDS:
                    continue
                return (level, title)

        # All-caps short title (last, lowest priority)
        if _ALL_CAPS_HEADING_RE.match(stripped):
            # Must have at least one space (multi-word) OR be reasonably long
            # (single-word all-caps like "INTRODUCTION" is OK)
            words = stripped.split()
            if len(words) > _MAX_HEADING_WORDS:
                return None
            return (2, " ".join(words))

        return None


# ============================================================================
# Internal helpers
# ============================================================================


def _has_cjk(text: str) -> bool:
    """Return True if ``text`` contains any CJK ideograph."""
    return any(
        0x4E00 <= ord(c) <= 0x9FFF  # CJK Unified Ideographs
        or 0x3400 <= ord(c) <= 0x4DBF  # CJK Extension A
        or 0xF900 <= ord(c) <= 0xFAFF  # CJK Compatibility Ideographs
        for c in text
    )


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "CanonicalMarkdownArtifact",
    "CanonicalMarkdownBuilder",
    "CanonicalMarkdownError",
    "CanonicalMarkdownSource",
    "CanonicalMarkdownTooLarge",
    "InvalidCanonicalMarkdownSource",
    "MAX_CANONICAL_MARKDOWN_BYTES",
    "MAX_SOURCE_FILENAME_LENGTH",
    "MAX_TITLE_LENGTH",
    "NeedsOcrNotBuildable",
    "PAGE_MARKER_FORMAT",
    "SCHEMA_VERSION",
]

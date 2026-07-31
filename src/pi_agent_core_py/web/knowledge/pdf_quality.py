"""PDF Text Quality Evaluator (P2-R2-B1).

Per p2-r0-amendment-2.md §2.1 + R2-B startup directive §10-§12:

- Pure function over ``PdfExtractionResult`` (from ``pdf_parser.py``)
- Deterministic — same input produces same metrics / decision / warnings
- No I/O / no LLM / no OCR / no network
- Whole-document statistics, not sample-based

Decision rules (amendment-2 §2.1):

- ``page_count == 0`` → raises ``InvalidExtractionResult``
- ``total_non_whitespace_chars == 0`` → ``NEEDS_OCR``
- ``total_non_whitespace_chars > 0`` → ``USABLE`` (low density → warning)

Warning thresholds (R2-B startup directive §12) are module-level constants
and do **not** flip ``USABLE`` to ``NEEDS_OCR`` — they only annotate the
result so downstream stages (Builder, retrieval) can surface concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .pdf_parser import PdfExtractionResult

# ============================================================================
# Warning thresholds (R2-B startup directive §12; values frozen here so tests
# can pin them. Tuning is a no-op for now — values are deliberately conservative
# per amendment-2 §2.1 rationale.)
# ============================================================================

#: ``non_empty_page_count / page_count`` below this → ``low_non_empty_page_ratio``
LOW_NON_EMPTY_PAGE_RATIO: Final[float] = 0.20

#: ``total_non_whitespace_chars / non_empty_page_count`` below this →
#: ``low_average_non_ws_chars``
LOW_AVERAGE_NON_WS_CHARS: Final[float] = 20.0

#: ``replacement_character_count / total_char_count`` above this →
#: ``replacement_character_noise``
HIGH_REPLACEMENT_CHAR_RATIO: Final[float] = 0.05

#: ``control_character_count / total_char_count`` above this →
#: ``control_character_noise``
HIGH_CONTROL_CHAR_RATIO: Final[float] = 0.05


# ============================================================================
# Errors
# ============================================================================


class PdfTextQualityError(Exception):
    """Base class for PDF text quality errors.

    Attributes:
        safe_error_code: stable short string for the API layer / DB storage.
            MUST NOT contain path / body / secret / traceback.
    """

    safe_error_code: str = "pdf_quality_failed"

    def __init__(
        self, message: str = "", *, safe_error_code: str | None = None
    ) -> None:
        super().__init__(message or self.__class__.__name__)
        if safe_error_code is not None:
            self.safe_error_code = safe_error_code


class InvalidExtractionResult(PdfTextQualityError):
    """``PdfExtractionResult`` is structurally invalid (e.g. zero pages, page
    number sequence not strictly 1..N).

    Per amendment-2 §2.1, ``page_count == 0`` is NOT a ``needs_ocr`` outcome —
    it's an error. ``needs_ocr`` is reserved for "parses fine, but has zero
    extractable text".
    """

    safe_error_code = "invalid_extraction_result"


# ============================================================================
# Result types (immutable + slots per R2-B startup directive §9)
# ============================================================================


@dataclass(frozen=True, slots=True)
class PdfTextQualityMetrics:
    """Whole-document text statistics (R2-B startup directive §9).

    All fields are deterministic functions of ``PdfExtractionResult.pages``.
    Ratios are pre-computed so callers don't have to re-derive them.

    Ranges:
    - ``non_empty_page_ratio`` ∈ [0.0, 1.0]; ``0.0`` when ``page_count == 0``
      (caller should have rejected that case already via
      ``InvalidExtractionResult``)
    - ``average_non_whitespace_chars_per_page`` ∈ [0.0, +∞); ``0.0`` when
      ``page_count == 0``
    - All int counts ≥ 0
    """

    page_count: int
    non_empty_page_count: int
    empty_page_count: int
    total_char_count: int
    total_non_whitespace_chars: int
    replacement_character_count: int
    control_character_count: int
    non_empty_page_ratio: float
    average_non_whitespace_chars_per_page: float
    maximum_page_chars: int
    minimum_non_empty_page_chars: int


class PdfTextQualityDecision(StrEnum):
    """Two-valued decision (R2-B startup directive §9).

    Do NOT add ``partial`` / ``degraded`` / ``review_required`` / ``unknown`` —
    low-quality-but-has-text documents stay ``USABLE`` with warnings rather
    than creating intermediate states.
    """

    USABLE = "usable"
    NEEDS_OCR = "needs_ocr"


@dataclass(frozen=True, slots=True)
class PdfTextQualityResult:
    """Output of ``PdfTextQualityEvaluator.evaluate``.

    ``reason_codes`` are stable machine tokens (no natural language); order
    is deterministic. ``warnings`` are also stable tokens; order matches the
    evaluation order in ``_collect_warnings``.

    Neither field contains page text / absolute paths / secrets.
    """

    decision: PdfTextQualityDecision
    metrics: PdfTextQualityMetrics
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]


# ============================================================================
# PdfTextQualityEvaluator
# ============================================================================


class PdfTextQualityEvaluator:
    """Stateless whole-document text quality evaluator.

    Construction::

        evaluator = PdfTextQualityEvaluator()
        result = evaluator.evaluate(extraction_result)
        if result.decision is PdfTextQualityDecision.NEEDS_OCR:
            ...  # do not build Markdown
        else:
            canonical_builder.build(..., quality=result)

    Raises ``InvalidExtractionResult`` (with ``safe_error_code``) when the
    input ``PdfExtractionResult`` is structurally invalid:

    - ``pages`` is empty
    - page numbers are not exactly ``1, 2, ..., len(pages)``

    The evaluator does **not** mutate the input.
    """

    def evaluate(self, extraction: PdfExtractionResult) -> PdfTextQualityResult:
        pages = extraction.pages
        page_count = len(pages)

        if page_count == 0:
            raise InvalidExtractionResult(
                "extraction result has 0 pages"
            )
        self._validate_page_number_sequence(pages)

        # Per-page statistics
        per_page_non_ws: list[int] = []
        per_page_total: list[int] = []
        total_non_ws = 0
        total_chars = 0
        replacement_count = 0
        control_count = 0
        non_empty_pages = 0

        for page in pages:
            text = page.text
            page_total = len(text)
            page_non_ws = sum(1 for c in text if not c.isspace())
            page_replacement = text.count("\ufffd")
            # Count "unsafe" control chars: anything with ord < 0x20 except
            # \n (0x0A) and \t (0x09), plus DEL (0x7F).
            page_control = sum(
                1
                for c in text
                if (
                    (ord(c) < 0x20 and ord(c) not in (0x09, 0x0A))
                    or ord(c) == 0x7F
                )
            )

            per_page_total.append(page_total)
            per_page_non_ws.append(page_non_ws)
            total_chars += page_total
            total_non_ws += page_non_ws
            replacement_count += page_replacement
            control_count += page_control
            if page_non_ws > 0:
                non_empty_pages += 1

        empty_pages = page_count - non_empty_pages
        non_empty_ratio = non_empty_pages / page_count
        avg_non_ws_per_page = total_non_ws / page_count
        maximum_page_chars = max(per_page_total) if per_page_total else 0
        # min over non-empty pages; 0 when no non-empty pages
        non_empty_page_sizes = [n for n in per_page_non_ws if n > 0]
        minimum_non_empty_page_chars = (
            min(non_empty_page_sizes) if non_empty_page_sizes else 0
        )

        metrics = PdfTextQualityMetrics(
            page_count=page_count,
            non_empty_page_count=non_empty_pages,
            empty_page_count=empty_pages,
            total_char_count=total_chars,
            total_non_whitespace_chars=total_non_ws,
            replacement_character_count=replacement_count,
            control_character_count=control_count,
            non_empty_page_ratio=non_empty_ratio,
            average_non_whitespace_chars_per_page=avg_non_ws_per_page,
            maximum_page_chars=maximum_page_chars,
            minimum_non_empty_page_chars=minimum_non_empty_page_chars,
        )

        decision = (
            PdfTextQualityDecision.NEEDS_OCR
            if total_non_ws == 0
            else PdfTextQualityDecision.USABLE
        )
        reason_codes = self._collect_reason_codes(decision)
        warnings = self._collect_warnings(metrics)

        return PdfTextQualityResult(
            decision=decision,
            metrics=metrics,
            reason_codes=reason_codes,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_page_number_sequence(pages: tuple) -> None:
        """Ensure page numbers are exactly ``1, 2, ..., len(pages)``.

        Per R2-B startup directive §14: page numbers must be strictly
        sequential starting at 1; gaps / duplicates / off-by-one →
        ``invalid_extraction_result`` (do not silently fix).
        """
        for expected_zero_based, page in enumerate(pages):
            expected = expected_zero_based + 1
            actual = page.page_number
            if actual != expected:
                raise InvalidExtractionResult(
                    f"page number sequence broken at index {expected_zero_based}: "
                    f"expected {expected}, got {actual!r}"
                )

    @staticmethod
    def _collect_reason_codes(
        decision: PdfTextQualityDecision,
    ) -> tuple[str, ...]:
        """Stable reason codes (R2-B startup directive §9).

        Currently the only reason is the decision itself; the tuple shape
        leaves room for future refinements without breaking the contract.
        """
        return (decision.value,)

    @staticmethod
    def _collect_warnings(metrics: PdfTextQualityMetrics) -> tuple[str, ...]:
        """Stable, de-duplicated warning codes.

        Order is fixed (amendment-2 §2.1 table order):

        1. ``low_non_empty_page_ratio``
        2. ``low_average_non_ws_chars``
        3. ``replacement_character_noise``
        4. ``control_character_noise``
        5. ``many_empty_pages``

        Only ``USABLE`` decisions get warnings — ``NEEDS_OCR`` returns no
        warnings (the decision itself is the signal).
        """
        warnings: list[str] = []

        # Skip warnings for NEEDS_OCR — decision itself is the message.
        # (Caller never sees USABLE+low_average while total_non_ws==0 because
        # low_average implies non_empty_pages==0 which implies total_non_ws==0
        # which means NEEDS_OCR. Defensive check anyway.)
        if (
            metrics.total_non_whitespace_chars == 0
            or metrics.page_count == 0
        ):
            return ()

        if metrics.non_empty_page_ratio < LOW_NON_EMPTY_PAGE_RATIO:
            warnings.append("low_non_empty_page_ratio")

        if metrics.average_non_whitespace_chars_per_page < LOW_AVERAGE_NON_WS_CHARS:
            warnings.append("low_average_non_ws_chars")

        if (
            metrics.total_char_count > 0
            and metrics.replacement_character_count / metrics.total_char_count
            > HIGH_REPLACEMENT_CHAR_RATIO
        ):
            warnings.append("replacement_character_noise")

        if (
            metrics.total_char_count > 0
            and metrics.control_character_count / metrics.total_char_count
            > HIGH_CONTROL_CHAR_RATIO
        ):
            warnings.append("control_character_noise")

        if metrics.empty_page_count > 0 and metrics.page_count > 1:
            warnings.append("many_empty_pages")

        return tuple(warnings)


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "HIGH_CONTROL_CHAR_RATIO",
    "HIGH_REPLACEMENT_CHAR_RATIO",
    "InvalidExtractionResult",
    "LOW_AVERAGE_NON_WS_CHARS",
    "LOW_NON_EMPTY_PAGE_RATIO",
    "PdfTextQualityDecision",
    "PdfTextQualityError",
    "PdfTextQualityEvaluator",
    "PdfTextQualityMetrics",
    "PdfTextQualityResult",
]

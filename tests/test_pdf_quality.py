"""PdfTextQualityEvaluator tests (P2-R2-B1).

Per R2-B startup directive §33.A (Quality Metrics) + §33.B (needs_ocr) +
§33.G (Determinism):

- Whole-document statistics (page counts / char counts / ratios)
- ``needs_ocr`` decision boundary
- Warning rules (low density / replacement char / control char / empty pages)
- Determinism (same input → same result)
- ``invalid_extraction_result`` raised for zero pages / page-number gaps
- No I/O / no LLM / no OCR / no network / no mutable input

The evaluator is a pure function over ``PdfExtractionResult`` — no fixture
PDFs needed (we construct ``PdfExtractionResult`` directly).
"""
from __future__ import annotations

import copy

import pytest

from pi_agent_core_py.web.knowledge.pdf_parser import (
    PdfExtractionResult,
    PdfPage,
)
from pi_agent_core_py.web.knowledge.pdf_quality import (
    HIGH_CONTROL_CHAR_RATIO,
    HIGH_REPLACEMENT_CHAR_RATIO,
    LOW_AVERAGE_NON_WS_CHARS,
    LOW_NON_EMPTY_PAGE_RATIO,
    InvalidExtractionResult,
    PdfTextQualityDecision,
    PdfTextQualityEvaluator,
)

# ============================================================================
# Helpers
# ============================================================================


def make_extraction(
    texts: list[str],
    *,
    parser_id: str = "pypdf",
    parser_version: str = "6.14.2",
    warnings: tuple[str, ...] = (),
) -> PdfExtractionResult:
    """Build a ``PdfExtractionResult`` with 1-based sequential page numbers."""
    pages = tuple(
        PdfPage(page_number=i + 1, text=t, extraction_warnings=())
        for i, t in enumerate(texts)
    )
    return PdfExtractionResult(
        parser_id=parser_id,
        parser_version=parser_version,
        pages=pages,
        warnings=warnings,
    )


def make_extraction_with_pages(
    pages: list[PdfPage],
    *,
    parser_id: str = "pypdf",
    parser_version: str = "6.14.2",
) -> PdfExtractionResult:
    return PdfExtractionResult(
        parser_id=parser_id,
        parser_version=parser_version,
        pages=tuple(pages),
        warnings=(),
    )


@pytest.fixture
def evaluator():
    return PdfTextQualityEvaluator()


# ============================================================================
# A. Quality Metrics
# ============================================================================


class TestQualityMetrics:
    def test_single_page_text_metrics(self, evaluator):
        result = evaluator.evaluate(make_extraction(["Hello World"]))
        m = result.metrics
        assert m.page_count == 1
        assert m.non_empty_page_count == 1
        assert m.empty_page_count == 0
        assert m.total_non_whitespace_chars == 10  # "Hello World" → 10
        assert m.total_char_count == 11  # one space
        assert m.non_empty_page_ratio == 1.0
        assert m.average_non_whitespace_chars_per_page == 10.0
        assert m.maximum_page_chars == 11
        assert m.minimum_non_empty_page_chars == 10

    def test_multi_page_text_metrics(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["Page one text", "Page two text", "Page three text"])
        )
        m = result.metrics
        assert m.page_count == 3
        assert m.non_empty_page_count == 3
        assert m.empty_page_count == 0
        # All three pages have non-whitespace chars
        assert m.total_non_whitespace_chars > 0
        assert m.non_empty_page_ratio == 1.0

    def test_blank_page_count(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["Text here", "", "   ", "More text"])
        )
        m = result.metrics
        assert m.page_count == 4
        assert m.non_empty_page_count == 2
        assert m.empty_page_count == 2

    def test_all_empty_document_metrics(self, evaluator):
        result = evaluator.evaluate(make_extraction(["", "   ", "\n\t"]))
        m = result.metrics
        assert m.page_count == 3
        assert m.non_empty_page_count == 0
        assert m.empty_page_count == 3
        assert m.total_non_whitespace_chars == 0
        assert m.non_empty_page_ratio == 0.0

    def test_non_empty_page_ratio(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["text", "", "text", "", "text"])
        )
        assert result.metrics.non_empty_page_ratio == 3 / 5

    def test_replacement_character_count(self, evaluator):
        text = "Hello \ufffd world \ufffd\ufffd"
        result = evaluator.evaluate(make_extraction([text]))
        assert result.metrics.replacement_character_count == 3

    def test_control_character_count_excludes_newline_tab(self, evaluator):
        # \n (0x0A) and \t (0x09) are NOT counted as control chars
        text = "line1\nline2\ttabbed\x07bell\x02stx"
        result = evaluator.evaluate(make_extraction([text]))
        # \x07 (BEL) + \x02 (STX) = 2 control chars
        assert result.metrics.control_character_count == 2

    def test_control_character_count_includes_del(self, evaluator):
        # DEL (0x7F) is counted as a control char
        text = "text\x7fdel"
        result = evaluator.evaluate(make_extraction([text]))
        assert result.metrics.control_character_count == 1

    def test_maximum_page_chars(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["short", "this is a longer page with more chars"])
        )
        assert (
            result.metrics.maximum_page_chars
            == len("this is a longer page with more chars")
        )

    def test_minimum_non_empty_page_chars(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["short", "longer text content"])
        )
        assert result.metrics.minimum_non_empty_page_chars == len("short")

    def test_minimum_non_empty_page_chars_when_all_empty(self, evaluator):
        result = evaluator.evaluate(make_extraction(["", "  "]))
        assert result.metrics.minimum_non_empty_page_chars == 0

    def test_average_non_whitespace_chars_per_page(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["ab cd", "ef gh", "ij kl"])
        )
        # 4 non-ws per page × 3 pages / 3 pages = 4
        assert (
            result.metrics.average_non_whitespace_chars_per_page == 4.0
        )

    def test_page_count_zero_raises(self, evaluator):
        with pytest.raises(InvalidExtractionResult) as exc_info:
            evaluator.evaluate(make_extraction([]))
        assert exc_info.value.safe_error_code == "invalid_extraction_result"


# ============================================================================
# B. needs_ocr decision
# ============================================================================


class TestNeedsOcrDecision:
    def test_all_empty_document_is_needs_ocr(self, evaluator):
        result = evaluator.evaluate(make_extraction(["", "  ", "\n"]))
        assert result.decision is PdfTextQualityDecision.NEEDS_OCR

    def test_whitespace_only_document_is_needs_ocr(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["   ", "\t\n", " \r\n ", ""])
        )
        assert result.decision is PdfTextQualityDecision.NEEDS_OCR

    def test_single_visible_char_is_usable(self, evaluator):
        # Conservative rule: total_non_whitespace_chars > 0 → USABLE
        result = evaluator.evaluate(make_extraction(["a"]))
        assert result.decision is PdfTextQualityDecision.USABLE

    def test_sparse_text_is_usable_with_warning(self, evaluator):
        # Very sparse — one short page among many blanks (1/6 < 0.20)
        result = evaluator.evaluate(
            make_extraction(["", "", "ab", "", "", ""])
        )
        assert result.decision is PdfTextQualityDecision.USABLE
        # But should have warnings
        assert len(result.warnings) > 0
        assert "low_non_empty_page_ratio" in result.warnings

    def test_many_empty_pages_with_some_text_is_usable_with_warning(
        self, evaluator
    ):
        result = evaluator.evaluate(
            make_extraction(
                ["decent page with text content", "", "", "", "another text page"]
            )
        )
        assert result.decision is PdfTextQualityDecision.USABLE
        assert "many_empty_pages" in result.warnings

    def test_dense_text_document_is_usable_no_warnings(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(
                [
                    "This is a substantial paragraph with enough text to "
                    "avoid any low-density warning being triggered.",
                    "Another substantial paragraph follows on the second "
                    "page with similarly sufficient text body content.",
                ]
            )
        )
        assert result.decision is PdfTextQualityDecision.USABLE
        assert result.warnings == ()

    def test_needs_ocr_has_no_warnings(self, evaluator):
        # Even if the document would otherwise trip a warning rule, the
        # NEEDS_OCR decision returns no warnings (decision is the message)
        result = evaluator.evaluate(make_extraction(["", "", "", ""]))
        assert result.decision is PdfTextQualityDecision.NEEDS_OCR
        assert result.warnings == ()


# ============================================================================
# C. Warning rules
# ============================================================================


class TestWarningRules:
    def test_low_non_empty_page_ratio_warning(self, evaluator):
        # 1 of 10 pages non-empty → ratio 0.1 < 0.20
        result = evaluator.evaluate(
            make_extraction(
                ["text content here", "", "", "", "", "", "", "", "", ""]
            )
        )
        assert "low_non_empty_page_ratio" in result.warnings

    def test_low_average_non_ws_chars_warning(self, evaluator):
        # 1 non-empty page with 5 chars → average 5/3 < 20
        result = evaluator.evaluate(make_extraction(["short", "", ""]))
        assert "low_average_non_ws_chars" in result.warnings

    def test_replacement_character_noise_warning(self, evaluator):
        # Build text where >5% are U+FFFD
        # 10 normal chars + 1 replacement = 9% > 5%
        result = evaluator.evaluate(make_extraction(["abcdefghij\ufffd"]))
        assert "replacement_character_noise" in result.warnings

    def test_control_character_noise_warning(self, evaluator):
        # 10 normal chars + 1 BEL = 9% > 5%
        result = evaluator.evaluate(make_extraction(["abcdefghij\x07"]))
        assert "control_character_noise" in result.warnings

    def test_many_empty_pages_warning(self, evaluator):
        result = evaluator.evaluate(
            make_extraction(["text content here", "", "more text here"])
        )
        assert "many_empty_pages" in result.warnings

    def test_warning_order_is_stable(self, evaluator):
        # Construct a result that triggers multiple warnings and verify
        # the order matches the directive §12 table.
        # 1 non-empty page out of 6 → ratio 0.167 < 0.20
        sparse_with_noise = "ab\ufffd\x07"  # triggers low_avg + replacement + control
        result = evaluator.evaluate(
            make_extraction([sparse_with_noise, "", "", "", "", ""])
        )
        # Expected order per _collect_warnings:
        # 1. low_non_empty_page_ratio
        # 2. low_average_non_ws_chars
        # 3. replacement_character_noise
        # 4. control_character_noise
        # 5. many_empty_pages
        assert result.warnings == (
            "low_non_empty_page_ratio",
            "low_average_non_ws_chars",
            "replacement_character_noise",
            "control_character_noise",
            "many_empty_pages",
        )

    def test_warnings_do_not_duplicate(self, evaluator):
        # Same warning code should never appear twice in the tuple.
        # 1/10 non-empty + replacement + low_avg → multiple warnings, no dup
        result = evaluator.evaluate(
            make_extraction(
                ["x\ufffd", "", "", "", "", "", "", "", "", ""]
            )
        )
        assert len(result.warnings) == len(set(result.warnings))

    def test_thresholds_are_module_constants(self):
        # Values frozen so tests can pin behavior
        assert LOW_NON_EMPTY_PAGE_RATIO == 0.20
        assert LOW_AVERAGE_NON_WS_CHARS == 20.0
        assert HIGH_REPLACEMENT_CHAR_RATIO == 0.05
        assert HIGH_CONTROL_CHAR_RATIO == 0.05


# ============================================================================
# D. Determinism
# ============================================================================


class TestDeterminism:
    def test_same_input_same_result(self, evaluator):
        extraction = make_extraction(["Hello World", "Second page", "Third"])
        r1 = evaluator.evaluate(extraction)
        r2 = evaluator.evaluate(extraction)
        # Dataclasses with same field values compare equal
        assert r1 == r2
        assert r1.metrics == r2.metrics

    def test_same_input_same_warnings_order(self, evaluator):
        extraction = make_extraction(["ab\ufffd", "", ""])
        r1 = evaluator.evaluate(extraction)
        r2 = evaluator.evaluate(extraction)
        assert r1.warnings == r2.warnings

    def test_does_not_mutate_input(self, evaluator):
        extraction = make_extraction(["Hello", "World"])
        original_pages = copy.deepcopy(extraction.pages)
        evaluator.evaluate(extraction)
        assert extraction.pages == original_pages

    def test_decision_enum_values(self):
        # StrEnum values are stable strings
        assert PdfTextQualityDecision.USABLE.value == "usable"
        assert PdfTextQualityDecision.NEEDS_OCR.value == "needs_ocr"


# ============================================================================
# E. Invalid extraction result
# ============================================================================


class TestInvalidExtraction:
    def test_zero_pages_raises(self, evaluator):
        with pytest.raises(InvalidExtractionResult) as exc_info:
            evaluator.evaluate(make_extraction([]))
        assert exc_info.value.safe_error_code == "invalid_extraction_result"

    def test_duplicate_page_number_raises(self, evaluator):
        pages = [
            PdfPage(page_number=1, text="one"),
            PdfPage(page_number=1, text="two"),  # duplicate
        ]
        with pytest.raises(InvalidExtractionResult):
            evaluator.evaluate(make_extraction_with_pages(pages))

    def test_page_number_gap_raises(self, evaluator):
        pages = [
            PdfPage(page_number=1, text="one"),
            PdfPage(page_number=3, text="three"),  # gap
        ]
        with pytest.raises(InvalidExtractionResult):
            evaluator.evaluate(make_extraction_with_pages(pages))

    def test_page_number_does_not_start_at_1(self, evaluator):
        pages = [
            PdfPage(page_number=0, text="zero"),  # 0-based mistakenly
            PdfPage(page_number=1, text="one"),
        ]
        with pytest.raises(InvalidExtractionResult):
            evaluator.evaluate(make_extraction_with_pages(pages))


# ============================================================================
# F. Boundary — no I/O / no LLM / no OCR / no network
# ============================================================================


class TestEvaluatorBoundary:
    def test_module_does_not_import_pypdf(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        assert "import pypdf" not in mod_source
        assert "from pypdf" not in mod_source

    def test_module_does_not_import_fastapi(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        assert "import fastapi" not in mod_source
        assert "from fastapi" not in mod_source

    def test_module_does_not_import_sqlite(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        assert "sqlite3" not in mod_source
        assert "aiosqlite" not in mod_source

    def test_module_does_not_import_network_libs(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("httpx", "requests", "urllib", "aiohttp"):
            assert f"import {forbidden}" not in mod_source

    def test_module_does_not_import_llm_providers(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("openai", "anthropic", "google.genai"):
            assert forbidden not in mod_source

    def test_module_does_not_import_ocr_or_models(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        for forbidden in (
            "tesseract",
            "surya",
            "torch",
            "transformers",
            "huggingface",
            "marker",
        ):
            assert forbidden not in mod_source

    def test_no_non_deterministic_calls(self):
        import pi_agent_core_py.web.knowledge.pdf_quality as mod

        mod_source = open(mod.__file__, encoding="utf-8").read()
        for forbidden in (
            "datetime.now",
            "time.time",
            "uuid4",
            "random.random",
        ):
            assert forbidden not in mod_source

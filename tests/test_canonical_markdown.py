"""CanonicalMarkdownBuilder tests (P2-R2-B2).

Per R2-B startup directive §33.C-H + §34 (golden fixtures):

- Text normalization (CRLF/NUL/control/whitespace)
- Frontmatter (field order / schema / escaping / Unicode / no-time)
- Page markers (one per page, blank pages, sequential, collision escape)
- Heading heuristic (English / Chinese / all-caps / counter-examples)
- Determinism (same bytes / same SHA / no locale)
- Output size guard
- Source validation (bad document_id / filename / SHA / etc.)
- needs_ocr not buildable
- Module boundary (no pypdf / FastAPI / SQLite / network / LLM / OCR)

All inputs are constructed in-memory (no PDF fixtures required because
the Builder is a pure function over ``CanonicalMarkdownSource`` +
``PdfExtractionResult`` + ``PdfTextQualityResult``).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from pi_agent_core_py.web.knowledge.canonical_markdown import (
    MAX_CANONICAL_MARKDOWN_BYTES,
    PAGE_MARKER_FORMAT,
    SCHEMA_VERSION,
    CanonicalMarkdownBuilder,
    CanonicalMarkdownSource,
    InvalidCanonicalMarkdownSource,
    NeedsOcrNotBuildable,
)
from pi_agent_core_py.web.knowledge.pdf_parser import (
    PdfExtractionResult,
    PdfPage,
)
from pi_agent_core_py.web.knowledge.pdf_quality import (
    PdfTextQualityDecision,
    PdfTextQualityEvaluator,
    PdfTextQualityMetrics,
    PdfTextQualityResult,
)

# ============================================================================
# Constants
# ============================================================================

_DOC_ID = "doc_abc123def456"
_SHA256 = "a" * 64
_PARSER_ID = "pypdf"
_PARSER_VERSION = "6.14.2"


# ============================================================================
# Helpers
# ============================================================================


def make_source(
    *,
    document_id: str = _DOC_ID,
    source_filename: str = "test.pdf",
    source_sha256: str = _SHA256,
    title: str | None = None,
    generated_at: str | None = None,
) -> CanonicalMarkdownSource:
    return CanonicalMarkdownSource(
        document_id=document_id,
        source_filename=source_filename,
        source_sha256=source_sha256,
        title=title,
        generated_at=generated_at,
    )


def make_extraction(
    texts: list[str],
    *,
    parser_id: str = _PARSER_ID,
    parser_version: str = _PARSER_VERSION,
) -> PdfExtractionResult:
    pages = tuple(
        PdfPage(page_number=i + 1, text=t, extraction_warnings=())
        for i, t in enumerate(texts)
    )
    return PdfExtractionResult(
        parser_id=parser_id,
        parser_version=parser_version,
        pages=pages,
        warnings=(),
    )


def make_usable_quality(
    texts: list[str], *, warnings: tuple[str, ...] = ()
) -> PdfTextQualityResult:
    """Build a minimal USABLE quality result without invoking the evaluator
    (so we can stub warnings and metrics independently)."""
    page_count = len(texts)
    non_empty = sum(1 for t in texts if any(not c.isspace() for c in t))
    total_non_ws = sum(
        sum(1 for c in t if not c.isspace()) for t in texts
    )
    metrics = PdfTextQualityMetrics(
        page_count=page_count,
        non_empty_page_count=non_empty,
        empty_page_count=page_count - non_empty,
        total_char_count=sum(len(t) for t in texts),
        total_non_whitespace_chars=total_non_ws,
        replacement_character_count=0,
        control_character_count=0,
        non_empty_page_ratio=(non_empty / page_count) if page_count else 0.0,
        average_non_whitespace_chars_per_page=(
            total_non_ws / page_count if page_count else 0.0
        ),
        maximum_page_chars=max((len(t) for t in texts), default=0),
        minimum_non_empty_page_chars=0,
    )
    return PdfTextQualityResult(
        decision=PdfTextQualityDecision.USABLE,
        metrics=metrics,
        reason_codes=("usable",),
        warnings=warnings,
    )


@pytest.fixture
def builder():
    return CanonicalMarkdownBuilder()


@pytest.fixture
def evaluator():
    return PdfTextQualityEvaluator()


# ============================================================================
# C. Text normalization
# ============================================================================


class TestTextNormalization:
    def test_crlf_normalized_to_lf(self, builder):
        extraction = make_extraction(["line1\r\nline2"])
        quality = make_usable_quality(["line1\r\nline2"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "line1\r\n" not in artifact.content
        assert "line1\nline2" in artifact.content

    def test_cr_normalized_to_lf(self, builder):
        extraction = make_extraction(["line1\rline2"])
        quality = make_usable_quality(["line1\rline2"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "line1\rline2" not in artifact.content
        assert "line1\nline2" in artifact.content

    def test_nul_stripped(self, builder):
        extraction = make_extraction(["text\x00with\x00nuls"])
        quality = make_usable_quality(["text\x00with\x00nuls"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "\x00" not in artifact.content

    def test_dangerous_control_chars_removed(self, builder):
        # BEL, BS, STX removed; \n and \t preserved
        extraction = make_extraction(["text\x07bell\x08bs\x02stx\ntab\there"])
        quality = make_usable_quality(["text\x07bell\x08bs\x02stx\ntab\there"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        body = artifact.content.split("---\n\n", 1)[1]
        assert "\x07" not in body
        assert "\x08" not in body
        assert "\x02" not in body
        assert "\n" in body
        assert "\t" in body

    def test_trailing_whitespace_per_line_stripped(self, builder):
        extraction = make_extraction(["line1   \nline2\t\t"])
        quality = make_usable_quality(["line1   \nline2\t\t"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        body = artifact.content.split("---\n\n", 1)[1]
        # The line ends should not have trailing spaces/tabs (but internal
        # tab in "line2\t\t" — the second \t is trailing)
        assert "line1   \n" not in body
        assert "line2\t\t" not in body

    def test_leading_and_trailing_blank_lines_stripped(self, builder):
        extraction = make_extraction(["\n\n\nactual content\n\n\n"])
        quality = make_usable_quality(["\n\n\nactual content\n\n\n"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        body = artifact.content.split("---\n\n", 1)[1]
        # Body starts with page marker, then content; content shouldn't have
        # leading blank lines after the marker
        assert "<!-- page:1 -->\n\nactual content" in body

    def test_compress_three_or_more_blank_lines(self, builder):
        extraction = make_extraction(["para1\n\n\n\n\npara2"])
        quality = make_usable_quality(["para1\n\n\n\n\npara2"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "\n\n\n\n" not in artifact.content

    def test_no_cross_page_merge(self, builder):
        # Pages are joined by section breaks, not paragraph merging
        extraction = make_extraction(["page one content", "page two content"])
        quality = make_usable_quality(["page one content", "page two content"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Two page markers, content separated
        assert artifact.content.count("<!-- page:") == 2
        assert "page one content" in artifact.content
        assert "page two content" in artifact.content


# ============================================================================
# D. Frontmatter
# ============================================================================


class TestFrontmatter:
    def test_field_order_fixed(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(title="Some Title"), extraction=extraction, quality=quality
        )
        frontmatter = artifact.content.split("---\n", 2)[1]
        field_names = [
            line.split(":", 1)[0] for line in frontmatter.strip().split("\n")
        ]
        assert field_names == [
            "schema",
            "document_id",
            "source_filename",
            "source_sha256",
            "parser_id",
            "parser_version",
            "page_count",
            "title",
        ]

    def test_schema_version_is_frozen(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert artifact.schema_version == "pi-agent-canonical-markdown/v1"
        assert f'schema: {json.dumps(SCHEMA_VERSION)}' in artifact.content

    def test_document_id_in_frontmatter(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(document_id="doc_xyz123abc456"),
            extraction=extraction,
            quality=quality,
        )
        assert f'document_id: {json.dumps("doc_xyz123abc456")}' in artifact.content

    def test_source_filename_quoted(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(source_filename="my file.pdf"),
            extraction=extraction,
            quality=quality,
        )
        # Space-containing filename must be safely quoted
        assert f'source_filename: {json.dumps("my file.pdf")}' in artifact.content

    def test_source_sha256_in_frontmatter(self, builder):
        sha = "deadbeef" * 8  # 64 hex chars
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(source_sha256=sha),
            extraction=extraction,
            quality=quality,
        )
        assert f'source_sha256: {json.dumps(sha)}' in artifact.content

    def test_parser_id_and_version_in_frontmatter(self, builder):
        extraction = make_extraction(["text"], parser_id="pypdf", parser_version="6.14.2")
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert f'parser_id: {json.dumps("pypdf")}' in artifact.content
        assert f'parser_version: {json.dumps("6.14.2")}' in artifact.content

    def test_page_count_in_frontmatter(self, builder):
        extraction = make_extraction(["page1", "page2", "page3"])
        quality = make_usable_quality(["page1", "page2", "page3"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "page_count: 3" in artifact.content

    def test_title_optional_omitted_when_none(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(title=None), extraction=extraction, quality=quality
        )
        assert "title:" not in artifact.content

    def test_title_included_when_set(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(title="My PDF Title"),
            extraction=extraction,
            quality=quality,
        )
        assert f'title: {json.dumps("My PDF Title")}' in artifact.content

    def test_colon_injection_is_safe(self, builder):
        # A title with colon must not break YAML parsing
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        malicious_title = "title: malicious_injection"
        artifact = builder.build(
            source=make_source(title=malicious_title),
            extraction=extraction,
            quality=quality,
        )
        # The whole malicious string is JSON-quoted as a single scalar
        assert json.dumps(malicious_title) in artifact.content

    def test_newline_injection_in_filename_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_filename="bad\n---\nmalign: true"),
                extraction=extraction,
                quality=quality,
            )

    def test_yaml_document_break_injection_rejected(self, builder):
        # Filename containing only `---` would not be rejected by the
        # filename check alone, but the JSON-quoted serialization makes
        # it a string scalar — verify it doesn't break frontmatter.
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(source_filename="---"),
            extraction=extraction,
            quality=quality,
        )
        # "---" is JSON-quoted as "\"---\"" so it's a valid scalar
        assert 'source_filename: "---"' in artifact.content

    def test_unicode_metadata_in_frontmatter(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(
                source_filename="中文文档.pdf",
                title="Title with 中文",
            ),
            extraction=extraction,
            quality=quality,
        )
        # Unicode chars should be present unescaped (ensure_ascii=False)
        assert "中文文档.pdf" in artifact.content
        assert "Title with 中文" in artifact.content

    def test_absolute_path_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_filename="/etc/passwd"),
                extraction=extraction,
                quality=quality,
            )

    def test_windows_absolute_path_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_filename="C:\\Users\\secret.pdf"),
                extraction=extraction,
                quality=quality,
            )

    def test_parent_dir_segment_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_filename="../etc/passwd"),
                extraction=extraction,
                quality=quality,
            )

    def test_invalid_sha256_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_sha256="XYZ" * 21 + "0"),
                extraction=extraction,
                quality=quality,
            )

    def test_short_sha256_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(source_sha256="abc123"),
                extraction=extraction,
                quality=quality,
            )

    def test_invalid_document_id_rejected(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        with pytest.raises(InvalidCanonicalMarkdownSource):
            builder.build(
                source=make_source(document_id="not_a_doc_id"),
                extraction=extraction,
                quality=quality,
            )

    def test_no_current_time_in_artifact(self, builder):
        # Build twice rapidly; bytes should be identical (no time-based field)
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        a1 = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        a2 = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert a1.content == a2.content
        # generated_at must not appear when not explicitly passed
        assert "generated_at" not in a1.content

    def test_no_random_fields(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        # Build 3 times — must produce identical content
        artifacts = [
            builder.build(
                source=make_source(), extraction=extraction, quality=quality
            ).content
            for _ in range(3)
        ]
        assert all(a == artifacts[0] for a in artifacts)


# ============================================================================
# E. Page markers
# ============================================================================


class TestPageMarkers:
    def test_one_marker_per_page(self, builder):
        extraction = make_extraction(["page1", "page2", "page3"])
        quality = make_usable_quality(["page1", "page2", "page3"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert artifact.content.count("<!-- page:") == 3

    def test_blank_pages_have_markers(self, builder):
        extraction = make_extraction(["content", "", "more"])
        quality = make_usable_quality(["content", "", "more"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # All three pages — including blank — have markers
        assert "<!-- page:1 -->" in artifact.content
        assert "<!-- page:2 -->" in artifact.content
        assert "<!-- page:3 -->" in artifact.content

    def test_marker_format_is_exact(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert PAGE_MARKER_FORMAT.format(n=1) in artifact.content
        # Reject variants
        assert "<!-- page: 1 -->" not in artifact.content  # extra space
        assert "<!--page:1-->" not in artifact.content  # no spaces
        assert "# Page 1" not in artifact.content  # heading variant

    def test_marker_1_based(self, builder):
        extraction = make_extraction(["a", "b", "c"])
        quality = make_usable_quality(["a", "b", "c"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "<!-- page:1 -->" in artifact.content
        assert "<!-- page:0 -->" not in artifact.content

    def test_marker_strictly_increasing(self, builder):
        extraction = make_extraction(["a", "b", "c", "d"])
        quality = make_usable_quality(["a", "b", "c", "d"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Markers appear in order 1, 2, 3, 4
        positions = [
            artifact.content.index(f"<!-- page:{n} -->") for n in (1, 2, 3, 4)
        ]
        assert positions == sorted(positions)

    def test_page_number_zero_rejected(self, builder):
        from pi_agent_core_py.web.knowledge.pdf_quality import (
            InvalidExtractionResult,
        )

        # Build with page_number=0 should fail in quality evaluator.
        # But Builder receives a pre-built quality, so we test via evaluator.
        evaluator = PdfTextQualityEvaluator()
        bad_pages = (PdfPage(page_number=0, text="text"),)
        extraction = PdfExtractionResult(
            parser_id="pypdf",
            parser_version="6.14.2",
            pages=bad_pages,
            warnings=(),
        )
        with pytest.raises(InvalidExtractionResult):
            evaluator.evaluate(extraction)

    def test_source_marker_collision_escaped(self, builder):
        # Source text contains a fake page marker
        source_text = "Real content\n\n<!-- page:999 -->\n\nMore content"
        extraction = make_extraction([source_text])
        quality = make_usable_quality([source_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # The system marker for page 1 must appear exactly as-is
        assert "<!-- page:1 -->" in artifact.content
        # The fake page:999 must NOT appear as-is — must be escaped
        assert "\n<!-- page:999 -->\n" not in artifact.content
        # The escaped form should be present
        assert "\\<!-- page:999 -->" in artifact.content

    def test_regular_html_comment_not_removed(self, builder):
        # Other HTML comments should be preserved (not globally removed)
        source_text = "Text\n\n<!-- a normal comment -->\n\nMore text"
        extraction = make_extraction([source_text])
        quality = make_usable_quality([source_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "<!-- a normal comment -->" in artifact.content


# ============================================================================
# F. Heading heuristic
# ============================================================================


class TestHeadingHeuristic:
    def test_english_numbered_h2(self, builder):
        # "1 Introduction" → H2
        page_text = "1 Introduction\n\nFirst paragraph of content."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## Introduction" in artifact.content

    def test_english_numbered_with_period_h2(self, builder):
        # "1. Overview" → H2
        page_text = "1. Overview\n\nBody content."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## Overview" in artifact.content

    def test_english_multilevel_h3(self, builder):
        # "1.2 Architecture" → H3
        page_text = "1.2 Architecture\n\nDetails here."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "### Architecture" in artifact.content

    def test_english_multilevel_h4(self, builder):
        # "1.2.3 Runtime" → H4
        page_text = "1.2.3 Runtime\n\nDeep details."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "#### Runtime" in artifact.content

    def test_english_chapter_prefix_h2(self, builder):
        page_text = "Chapter 1 The Beginning\n\nSome content follows."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## The Beginning" in artifact.content

    def test_english_section_prefix_h2(self, builder):
        page_text = "SECTION 2 Methodology\n\nSome content."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## Methodology" in artifact.content

    def test_chinese_chapter_h2(self, builder):
        page_text = "第一章 系统架构\n\n本章内容。"
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## 系统架构" in artifact.content

    def test_chinese_numbered_h2(self, builder):
        page_text = "一、总体设计\n\n本节内容。"
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## 总体设计" in artifact.content

    def test_chinese_paren_numbered_h3(self, builder):
        page_text = "（一）运行时\n\n具体说明。"
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "### 运行时" in artifact.content

    def test_all_caps_short_title_h2(self, builder):
        page_text = "INTRODUCTION\n\nFirst paragraph."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## INTRODUCTION" in artifact.content

    def test_normal_short_sentence_not_converted(self, builder):
        # A short sentence ending with period should not become a heading
        page_text = "This is a normal sentence.\n\nMore content."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Should remain as plain text, not converted to heading
        assert "## This is a normal sentence." not in artifact.content

    def test_sentence_ending_punctuation_not_converted(self, builder):
        # Heading-like line but ending with ? stays plain
        page_text = "1 What is this?\n\nBody content."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## What is this?" not in artifact.content

    def test_version_number_not_converted(self, builder):
        # "1.2.3" alone (no title) should not match H4 pattern
        page_text = "1.2.3\n\nDescription follows."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "#### " not in artifact.content

    def test_decimal_not_converted(self, builder):
        # "3.14" alone is a number, not a heading
        page_text = "3.14\n\nPi value."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## 14" not in artifact.content
        assert "## 3.14" not in artifact.content

    def test_date_not_converted(self, builder):
        page_text = "2026-07-31\n\nMeeting notes."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## " not in artifact.content.split("page:1", 1)[1].split("\n", 0)[0]

    def test_url_not_converted(self, builder):
        page_text = "https://example.com\n\nLink reference."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # The URL should remain, not be converted to heading
        assert "## https://example.com" not in artifact.content

    def test_list_not_converted_to_heading(self, builder):
        # List items shouldn't be headings
        page_text = "- item one\n- item two\n- item three"
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert "## item one" not in artifact.content

    def test_no_h1_in_body(self, builder):
        # Body should never produce single-# headings
        page_text = "1 Title\n\n2 Another Title\n\nBody."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Get the body (after frontmatter)
        body = artifact.content.split("---\n\n", 1)[1]
        # No line in body should start with "# " (single hash + space = H1)
        for line in body.split("\n"):
            assert not line.startswith("# "), f"H1 found: {line!r}"

    def test_heading_level_capped_at_4(self, builder):
        # Even 4-level numbering produces H4 max
        page_text = "1.2.3.4 Deep\n\nContent."  # 4-level, but capped
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Should not produce H5 (5 hashes)
        assert "#####" not in artifact.content

    def test_existing_markdown_hash_preserved(self, builder):
        # Lines starting with # should not be double-processed
        page_text = "## Existing Heading\n\nBody."
        extraction = make_extraction([page_text])
        quality = make_usable_quality([page_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # The existing ## should remain, not be doubled
        assert "## Existing Heading" in artifact.content
        assert "#### Existing Heading" not in artifact.content


# ============================================================================
# G. Determinism
# ============================================================================


class TestDeterminism:
    def test_repeat_build_produces_identical_bytes(self, builder):
        extraction = make_extraction(["Hello", "World content"])
        quality = make_usable_quality(["Hello", "World content"])
        a1 = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        a2 = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert a1.content == a2.content
        assert a1.content_sha256 == a2.content_sha256

    def test_sha256_matches_utf8_bytes(self, builder):
        extraction = make_extraction(["sample content"])
        quality = make_usable_quality(["sample content"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        expected = hashlib.sha256(
            artifact.content.encode("utf-8")
        ).hexdigest()
        assert artifact.content_sha256 == expected

    def test_byte_length_matches_utf8_bytes(self, builder):
        extraction = make_extraction(["sample content with 中文"])
        quality = make_usable_quality(["sample content with 中文"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert artifact.byte_length == len(artifact.content.encode("utf-8"))

    def test_warnings_order_stable(self, builder):
        extraction = make_extraction(["ab"])
        quality = make_usable_quality(
            ["ab"], warnings=("low_density", "another_warning")
        )
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Quality warnings should appear in their input order
        assert artifact.warnings == ("low_density", "another_warning")

    def test_metadata_order_stable_across_runs(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        a1 = builder.build(
            source=make_source(title="Title One"),
            extraction=extraction,
            quality=quality,
        )
        a2 = builder.build(
            source=make_source(title="Different Title"),
            extraction=extraction,
            quality=quality,
        )
        # Field order should be the same; only the title value differs
        f1 = a1.content.split("---\n", 2)[1]
        f2 = a2.content.split("---\n", 2)[1]
        fields1 = [line.split(":", 1)[0] for line in f1.strip().split("\n")]
        fields2 = [line.split(":", 1)[0] for line in f2.strip().split("\n")]
        assert fields1 == fields2

    def test_single_trailing_newline(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert artifact.content.endswith("\n")
        assert not artifact.content.endswith("\n\n")

    def test_utf8_no_bom(self, builder):
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # UTF-8 BOM is 0xEF 0xBB 0xBF
        assert not artifact.content.encode("utf-8").startswith(b"\xef\xbb\xbf")


# ============================================================================
# H. Output size guard
# ============================================================================


class TestOutputSizeGuard:
    def test_size_guard_constant_is_reasonable(self):
        # 50 MB — see directive §25 rationale
        assert MAX_CANONICAL_MARKDOWN_BYTES == 50 * 1024 * 1024

    def test_normal_content_does_not_trip_guard(self, builder):
        # Even with 20 pages of reasonable text, should be far below 50 MB
        texts = [f"Page {i} content with substantial text." for i in range(20)]
        extraction = make_extraction(texts)
        quality = make_usable_quality(texts)
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        assert artifact.byte_length < MAX_CANONICAL_MARKDOWN_BYTES


# ============================================================================
# I. needs_ocr rejection
# ============================================================================


class TestNeedsOcrNotBuildable:
    def test_needs_ocr_quality_raises(self, builder):
        extraction = make_extraction(["", "  "])
        metrics = PdfTextQualityMetrics(
            page_count=2,
            non_empty_page_count=0,
            empty_page_count=2,
            total_char_count=2,
            total_non_whitespace_chars=0,
            replacement_character_count=0,
            control_character_count=0,
            non_empty_page_ratio=0.0,
            average_non_whitespace_chars_per_page=0.0,
            maximum_page_chars=2,
            minimum_non_empty_page_chars=0,
        )
        quality = PdfTextQualityResult(
            decision=PdfTextQualityDecision.NEEDS_OCR,
            metrics=metrics,
            reason_codes=("needs_ocr",),
            warnings=(),
        )
        with pytest.raises(NeedsOcrNotBuildable) as exc_info:
            builder.build(
                source=make_source(), extraction=extraction, quality=quality
            )
        assert exc_info.value.safe_error_code == "needs_ocr_not_buildable"


# ============================================================================
# J. Module boundary
# ============================================================================


class TestModuleBoundary:
    def test_module_does_not_import_pypdf(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "import pypdf" not in src
        assert "from pypdf" not in src

    def test_module_does_not_import_fastapi(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "import fastapi" not in src
        assert "from fastapi" not in src

    def test_module_does_not_import_sqlite(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "sqlite3" not in src
        assert "aiosqlite" not in src

    def test_module_does_not_import_network_libs(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("httpx", "requests", "urllib", "aiohttp"):
            assert f"import {forbidden}" not in src

    def test_module_does_not_import_llm_providers(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("openai", "anthropic", "google.genai"):
            assert forbidden not in src

    def test_module_does_not_import_ocr_or_models(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        # Check import statements only — not docstring mentions
        import_lines = [
            line for line in src.split("\n")
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            for forbidden in (
                "tesseract",
                "surya",
                "torch",
                "transformers",
                "huggingface",
                "marker_pdf",
                "fitz",
                "pymupdf",
            ):
                assert forbidden not in line.lower(), f"forbidden import: {line}"

    def test_no_non_deterministic_calls(self):
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        # Strip docstrings/quotes; check for actual call expressions
        # Look only at lines that aren't comments or string literals
        code_lines = []
        in_string = False
        for line in src.split("\n"):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_string = not in_string
                continue
            if in_string:
                continue
            if stripped.startswith("#"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        for forbidden in (
            "datetime.now",
            "time.time",
            "uuid4",
            "random.random",
        ):
            assert forbidden not in code

    def test_does_not_import_knowledge_store(self):
        # Builder must not couple to SQLite layer
        import pi_agent_core_py.web.knowledge.canonical_markdown as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "from .store" not in src
        assert "KnowledgeStore" not in src


# ============================================================================
# K. Golden / snapshot tests (directive §34)
# ============================================================================


class TestGoldenSnapshots:
    """Small, hand-pinned golden Markdown strings for the most important
    scenarios. Updating these requires explicit review of the diff."""

    def test_single_page_english_golden(self, builder):
        extraction = make_extraction(["Hello World this is page one."])
        quality = make_usable_quality(["Hello World this is page one."])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        expected = (
            "---\n"
            f'schema: "{SCHEMA_VERSION}"\n'
            f'document_id: "{_DOC_ID}"\n'
            'source_filename: "test.pdf"\n'
            f'source_sha256: "{_SHA256}"\n'
            f'parser_id: "{_PARSER_ID}"\n'
            f'parser_version: "{_PARSER_VERSION}"\n'
            "page_count: 1\n"
            "---\n"
            "\n"
            "<!-- page:1 -->\n"
            "\n"
            "Hello World this is page one.\n"
        )
        assert artifact.content == expected

    def test_two_pages_chinese_english_golden(self, builder):
        extraction = make_extraction(["English content", "中文内容"])
        quality = make_usable_quality(["English content", "中文内容"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # Verify structure (not full content equality — keep snapshot loose)
        assert artifact.content.startswith("---\n")
        assert "<!-- page:1 -->\n\nEnglish content" in artifact.content
        assert "<!-- page:2 -->\n\n中文内容" in artifact.content
        assert artifact.content.endswith("\n")
        assert artifact.page_count == 2

    def test_blank_middle_page_marker_preserved(self, builder):
        extraction = make_extraction(["first", "", "third"])
        quality = make_usable_quality(["first", "", "third"])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # All three page markers present, including blank middle page
        assert "<!-- page:1 -->" in artifact.content
        assert "<!-- page:2 -->" in artifact.content
        assert "<!-- page:3 -->" in artifact.content

    def test_marker_collision_golden(self, builder):
        # Source text injects a fake page marker — must be escaped
        source_text = "Real\n\n<!-- page:999 -->\n\nTail"
        extraction = make_extraction([source_text])
        quality = make_usable_quality([source_text])
        artifact = builder.build(
            source=make_source(), extraction=extraction, quality=quality
        )
        # System marker for page 1 is intact, fake 999 is escaped
        assert "<!-- page:1 -->" in artifact.content
        assert "\\<!-- page:999 -->" in artifact.content
        # SHA-256 stable
        expected_sha = hashlib.sha256(
            artifact.content.encode("utf-8")
        ).hexdigest()
        assert artifact.content_sha256 == expected_sha

    def test_frontmatter_escaping_golden(self, builder):
        title_value = 'Title "with quotes" and : colons'
        extraction = make_extraction(["text"])
        quality = make_usable_quality(["text"])
        artifact = builder.build(
            source=make_source(
                source_filename="weird: file.pdf",
                title=title_value,
            ),
            extraction=extraction,
            quality=quality,
        )
        # Both must be safely JSON-quoted
        assert (
            f'source_filename: {json.dumps("weird: file.pdf")}'
            in artifact.content
        )
        assert (
            f'title: {json.dumps(title_value)}'
            in artifact.content
        )

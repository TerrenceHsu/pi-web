"""P2-R4-C1 — Citation parsing, validation, rendering unit tests.

Covers directive §77 (parser tests) / §78 (validation tests) /
§79 (renderer tests).

Uses real EvidenceRegistry with seeded evidence — no DB needed.
"""
from __future__ import annotations

from pi_agent_core_py.web.knowledge.citations import (
    CitationEntry,
    parse_citations,
    process_citations,
    render_citation_inline,
    render_source_footer,
)
from pi_agent_core_py.web.knowledge.evidence import EvidenceRegistry

# ============================================================================


# ============================================================================
# Helpers
# ============================================================================


def _seed_registry(count: int = 1) -> EvidenceRegistry:
    """Create a registry with N evidence entries (E1..EN)."""
    r = EvidenceRegistry()
    for i in range(count):
        r.register(
            document_id=f"doc_{i}",
            chunk_id=f"chunk_{i}",
            source_filename=f"file_{i}.pdf",
            heading_path=("H",),
            page_start=i + 1,
            page_end=i + 2,
            content=f"content {i}",
            rank=-1.0 - i * 0.1,
        )
    return r


# ============================================================================
# 1. CitationParser
# ============================================================================


class TestParseCitations:
    def test_single_citation(self):
        assert parse_citations("hello [cite:E1] world") == ["E1"]

    def test_multiple_citations(self):
        result = parse_citations("[cite:E1] and [cite:E3]")
        assert result == ["E1", "E3"]

    def test_duplicate_citations_preserved(self):
        result = parse_citations("[cite:E1] again [cite:E1]")
        assert result == ["E1", "E1"]

    def test_no_citations(self):
        assert parse_citations("plain text") == []

    def test_empty_text(self):
        assert parse_citations("") == []

    def test_malformed_cite_empty_not_matched(self):
        assert parse_citations("[cite:]") == []

    def test_lowercase_e_not_matched(self):
        assert parse_citations("[cite:e1]") == []

    def test_uppercase_cite_not_matched(self):
        assert parse_citations("[CITE:E1]") == []

    def test_e0_not_matched(self):
        assert parse_citations("[cite:E0]") == []

    def test_negative_not_matched(self):
        assert parse_citations("[cite:E-1]") == []

    def test_text_adjacent_to_citation(self):
        assert parse_citations("text[cite:E1]more") == ["E1"]

    def test_citation_at_boundaries(self):
        assert parse_citations("[cite:E1]start") == ["E1"]
        assert parse_citations("end[cite:E1]") == ["E1"]

    def test_large_number(self):
        assert parse_citations("[cite:E999]") == ["E999"]


# ============================================================================
# 2. CitationProcessor — validation + rendering
# ============================================================================


class TestProcessCitations:
    def test_valid_single_citation(self):
        r = _seed_registry(1)
        result = process_citations("hello [cite:E1] world", r)
        assert result.rendered_content == "hello [1] world"
        assert len(result.citations) == 1
        assert result.citations[0].number == 1
        assert result.citations[0].evidence_id == "E1"
        assert result.warnings == ()

    def test_valid_multiple_citations(self):
        r = _seed_registry(3)
        result = process_citations("[cite:E1] [cite:E2] [cite:E3]", r)
        assert result.rendered_content == "[1] [2] [3]"
        assert len(result.citations) == 3

    def test_first_use_numbering_order(self):
        """E4 cited before E2 → E4=[1], E2=[2]."""
        r = _seed_registry(4)
        result = process_citations("[cite:E4] then [cite:E2]", r)
        assert result.rendered_content == "[1] then [2]"
        assert result.citations[0].evidence_id == "E4"
        assert result.citations[1].evidence_id == "E2"

    def test_same_evidence_same_number(self):
        r = _seed_registry(1)
        result = process_citations("[cite:E1] again [cite:E1]", r)
        assert result.rendered_content == "[1] again [1]"
        assert len(result.citations) == 1

    def test_unknown_evidence_removed(self):
        r = _seed_registry(1)
        result = process_citations("text [cite:E999] end", r)
        assert result.rendered_content == "text  end"
        assert "E999" in result.warnings

    def test_mixed_valid_and_invalid(self):
        r = _seed_registry(2)
        result = process_citations("[cite:E1] [cite:E99] [cite:E2]", r)
        assert result.rendered_content == "[1]  [2]"
        assert len(result.citations) == 2
        assert "E99" in result.warnings

    def test_multiple_unknowns_all_warned(self):
        r = _seed_registry(1)
        result = process_citations("[cite:E99] [cite:E98]", r)
        assert "E99" in result.warnings
        assert "E98" in result.warnings
        assert result.rendered_content == " "  # space between tokens remains

    def test_malformed_tokens_left_as_text(self):
        r = _seed_registry(1)
        result = process_citations("[cite:] [CITE:E1] [cite:e1]", r)
        # None matched → all left as-is
        assert result.rendered_content == "[cite:] [CITE:E1] [cite:e1]"
        assert result.citations == ()
        assert result.warnings == ()

    def test_citation_metadata_correct(self):
        r = EvidenceRegistry()
        r.register(
            document_id="doc_x",
            chunk_id="chunk_x",
            source_filename="radiotherapy.pdf",
            heading_path=("Methods", "IMRT"),
            page_start=12,
            page_end=13,
            content="IMRT uses modulated beams",
            rank=-1.5,
        )
        result = process_citations("[cite:E1]", r)
        c = result.citations[0]
        assert c.source_filename == "radiotherapy.pdf"
        assert c.page_start == 12
        assert c.page_end == 13

    def test_no_citations_in_text(self):
        r = _seed_registry(2)
        result = process_citations("plain text no citations", r)
        assert result.rendered_content == "plain text no citations"
        assert result.citations == ()
        assert result.warnings == ()

    def test_empty_text(self):
        r = _seed_registry(1)
        result = process_citations("", r)
        assert result.rendered_content == ""
        assert result.citations == ()


# ============================================================================
# 3. CitationRenderer — source footer + inline
# ============================================================================


class TestRenderSourceFooter:
    def test_single_page(self):
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="report.pdf",
                page_start=5, page_end=5,
            ),
        )
        footer = render_source_footer(entries)
        assert "[1] report.pdf · p.5" in footer
        assert "Sources:" in footer

    def test_page_range(self):
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="report.pdf",
                page_start=12, page_end=13,
            ),
        )
        footer = render_source_footer(entries)
        assert "[1] report.pdf · pp.12–13" in footer

    def test_multiple_entries(self):
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="a.pdf", page_start=3, page_end=3,
            ),
            CitationEntry(
                number=2, evidence_id="E2",
                source_filename="b.pdf", page_start=7, page_end=9,
            ),
        )
        footer = render_source_footer(entries)
        assert "[1] a.pdf · p.3" in footer
        assert "[2] b.pdf · pp.7–9" in footer

    def test_empty_entries(self):
        assert render_source_footer(()) == ""

    def test_unicode_filename(self):
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="放疗资料.pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "放疗资料.pdf" in footer

    def test_no_path_leakage(self):
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="safe.pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "D:\\" not in footer
        assert "/home/" not in footer
        assert "documents/" not in footer

    def test_markdown_link_injection_prevented(self):
        """Filename with [text](url) must NOT form a Markdown link."""
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="[click-me](https://evil.com).pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        # Escaped brackets/parens prevent Markdown link parsing
        assert "\\[click-me\\]" in footer
        assert "\\(https://evil.com\\)" in footer

    def test_markdown_emphasis_injection_prevented(self):
        """Filename with **text** must NOT form bold."""
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="**fake-heading**.pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "\\*\\*fake-heading\\*\\*" in footer

    def test_markdown_image_injection_prevented(self):
        """Filename with ![alt](url) must NOT form an image."""
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="![img](https://evil.com/x.png).pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "\\!" in footer
        assert "\\[img\\]" in footer

    def test_markdown_code_injection_prevented(self):
        """Filename with backticks must NOT form code span."""
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="`code`.pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "\\`code\\`" in footer

    def test_normal_filename_not_escaped(self):
        """Common filenames (underscores, hyphens, dots) stay readable."""
        entries = (
            CitationEntry(
                number=1, evidence_id="E1",
                source_filename="my_report_v2.pdf",
                page_start=1, page_end=1,
            ),
        )
        footer = render_source_footer(entries)
        assert "my_report_v2.pdf" in footer
        assert "\\" not in footer  # no escaping needed

    def test_inline_renderer_also_escapes(self):
        """render_citation_inline must also escape Markdown."""
        entry = CitationEntry(
            number=1, evidence_id="E1",
            source_filename="[link](url).pdf",
            page_start=1, page_end=1,
        )
        inline = render_citation_inline(entry)
        assert "\\[link\\]" in inline


class TestRenderCitationInline:
    def test_single_page(self):
        entry = CitationEntry(
            number=1, evidence_id="E1",
            source_filename="a.pdf", page_start=5, page_end=5,
        )
        assert render_citation_inline(entry) == "a.pdf · p.5"

    def test_page_range(self):
        entry = CitationEntry(
            number=1, evidence_id="E1",
            source_filename="a.pdf", page_start=10, page_end=12,
        )
        assert render_citation_inline(entry) == "a.pdf · pp.10–12"

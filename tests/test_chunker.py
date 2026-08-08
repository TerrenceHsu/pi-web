"""P2-R3-A — Heading-aware Chunker unit tests.

Covers the directive §37 test matrix: frontmatter / page markers / headings /
code fences / overlap / determinism / page range / Unicode / size limits.
"""
from __future__ import annotations

import hashlib
from typing import Final

import pytest

from pi_agent_core_py.web.knowledge.chunker import (
    CHUNK_MAX_CHARS,
    CanonicalMarkdownInvalid,
    ChunkingEmpty,
    HeadingAwareChunker,
    compute_chunk_id,
    compute_content_sha256,
)

# ============================================================================
# Constants
# ============================================================================

_DOC_ID: Final[str] = "doc_test0001"


def _frontmatter(doc_id: str = _DOC_ID, page_count: int = 1) -> str:
    return (
        "---\n"
        f'schema "pi-agent-canonical-markdown/v1"\n'
        f'document_id "{doc_id}"\n'
        f"page_count {page_count}\n"
        "---\n"
    )


def _page(n: int, body: str = "") -> str:
    return f"<!-- page:{n} -->\n\n{body}" if body else f"<!-- page:{n} -->"


def _markdown(pages: list[str], doc_id: str = _DOC_ID) -> str:
    body = "\n\n".join(pages)
    page_count = len(pages)
    return _frontmatter(doc_id, page_count) + "\n" + body + "\n"


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
def chunker() -> HeadingAwareChunker:
    return HeadingAwareChunker()


# ============================================================================
# 1. Minimal / single-page
# ============================================================================


class TestMinimal:
    def test_single_page_simple_text_produces_one_chunk(self, chunker):
        md = _markdown([_page(1, "Hello world.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert len(chunks) == 1
        assert chunks[0].ordinal == 0
        assert chunks[0].document_id == _DOC_ID
        assert "Hello world." in chunks[0].content
        assert chunks[0].page_start == 1
        assert chunks[0].page_end == 1

    def test_empty_body_raises_chunking_empty(self, chunker):
        md = _markdown([_page(1)])
        with pytest.raises(ChunkingEmpty):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_whitespace_only_body_raises_chunking_empty(self, chunker):
        md = _markdown([_page(1, "   \n\n\t\n")])
        with pytest.raises(ChunkingEmpty):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)


# ============================================================================
# 2. Frontmatter
# ============================================================================


class TestFrontmatter:
    def test_missing_frontmatter_raises(self, chunker):
        md = "<!-- page:1 -->\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="frontmatter"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_missing_closing_delimiter_raises(self, chunker):
        md = "---\ndocument_id \"doc_test0001\"\npage_count 1\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="closing"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_missing_document_id_field_raises(self, chunker):
        md = "---\npage_count 1\n---\n<!-- page:1 -->\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="document_id"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_missing_page_count_field_raises(self, chunker):
        md = '---\ndocument_id "doc_test0001"\n---\n<!-- page:1 -->\n\nhello\n'
        with pytest.raises(CanonicalMarkdownInvalid, match="page_count"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_document_id_mismatch_raises(self, chunker):
        md = _markdown([_page(1, "hello")], doc_id="doc_other0001")
        with pytest.raises(CanonicalMarkdownInvalid, match="document_id"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_page_count_mismatch_raises(self, chunker):
        md = _frontmatter(_DOC_ID, 5) + "\n<!-- page:1 -->\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="page_count"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_bom_is_stripped(self, chunker):
        md = "\ufeff" + _markdown([_page(1, "hello")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert len(chunks) == 1


# ============================================================================
# 3. Page markers
# ============================================================================


class TestPageMarkers:
    def test_multi_page_content_split_by_marker(self, chunker):
        md = _markdown(
            [
                _page(1, "page one content"),
                _page(2, "page two content"),
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=2)
        # Two distinct headings implicitly absent; should pack into 1-2 chunks.
        assert 1 <= len(chunks) <= 2
        # No chunk content should contain a page marker line.
        for ch in chunks:
            assert "<!-- page:" not in ch.content

    def test_marker_not_emitted_in_chunk_content(self, chunker):
        md = _markdown([_page(1, "hello")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert "<!-- page:1 -->" not in chunks[0].content

    def test_marker_out_of_range_raises(self, chunker):
        md = _frontmatter(_DOC_ID, 1) + "\n<!-- page:5 -->\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="out of range"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)

    def test_duplicate_marker_raises(self, chunker):
        md = (
            _frontmatter(_DOC_ID, 2)
            + "\n<!-- page:1 -->\n\nhello\n\n<!-- page:1 -->\n\nworld\n\n<!-- page:2 -->\n\nfoo\n"
        )
        with pytest.raises(CanonicalMarkdownInvalid, match="duplicate"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=2)

    def test_missing_marker_raises(self, chunker):
        md = _frontmatter(_DOC_ID, 3) + "\n<!-- page:1 -->\n\nhello\n\n<!-- page:3 -->\n\nfoo\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="missing page marker"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=3)

    def test_body_before_first_marker_raises(self, chunker):
        md = _frontmatter(_DOC_ID, 1) + "\nstray content\n\n<!-- page:1 -->\n\nhello\n"
        with pytest.raises(CanonicalMarkdownInvalid, match="before first"):
            chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)


# ============================================================================
# 4. Heading parsing
# ============================================================================


class TestHeadings:
    def test_h1_sets_heading_path(self, chunker):
        md = _markdown([_page(1, "# Introduction\n\nBody text here.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert chunks[0].heading_path == ("Introduction",)

    def test_h1_h2_stack(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "# Top\n\nFirst paragraph.\n\n## Sub\n\nSecond paragraph.",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # Both blocks may pack into one chunk; the second block's heading
        # context is the deeper one. heading_path reflects last seen.
        assert chunks[-1].heading_path[-1] == "Sub"
        assert chunks[-1].heading_path[0] == "Top"

    def test_h1_h2_h3_stack(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "# A\n\np1\n\n## B\n\np2\n\n### C\n\np3",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # Last chunk should carry full stack.
        assert chunks[-1].heading_path == ("A", "B", "C")

    def test_sibling_heading_replaces(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "# A\n\np1\n\n## B\n\np2\n\n## C\n\np3",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # Find the chunk containing p3; its heading_path should be ("A", "C")
        c3 = next(c for c in chunks if "p3" in c.content)
        assert c3.heading_path == ("A", "C")

    def test_h6_max_level(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "# A\n\np1\n\n## B\n\np2\n\n### C\n\np3\n\n"
                    "#### D\n\np4\n\n##### E\n\np5\n\n###### F\n\np6",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        c6 = next(c for c in chunks if "p6" in c.content)
        assert c6.heading_path == ("A", "B", "C", "D", "E", "F")

    def test_hash_in_code_fence_not_heading(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "```\n# not a heading\nimport os\n```\n\nReal paragraph.",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # heading_path should be empty (no real heading seen).
        for ch in chunks:
            assert ch.heading_path == ()

    def test_hash_after_text_not_heading(self, chunker):
        md = _markdown([_page(1, "This is text. # not heading")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert chunks[0].heading_path == ()


# ============================================================================
# 5. Chunk size + boundaries
# ============================================================================


class TestChunkSize:
    def test_short_chunks_retained(self, chunker):
        md = _markdown([_page(1, "Short one.\n\nShort two.\n\nShort three.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # All paragraphs should appear somewhere.
        all_content = " ".join(c.content for c in chunks)
        assert "Short one." in all_content
        assert "Short two." in all_content
        assert "Short three." in all_content

    def test_oversized_paragraph_force_split(self, chunker):
        para = "word " * 500  # ~2500 chars > MAX
        md = _markdown([_page(1, para.strip())])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # Multiple chunks; each <= MAX.
        assert len(chunks) >= 2
        for ch in chunks:
            assert ch.char_count <= CHUNK_MAX_CHARS

    def test_no_chunk_exceeds_max(self, chunker):
        # Many paragraphs each ~ 600 chars.
        paras = "\n\n".join("x" * 600 for _ in range(10))
        md = _markdown([_page(1, paras)])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        for ch in chunks:
            assert ch.char_count <= CHUNK_MAX_CHARS, (
                f"chunk {ch.ordinal} size {ch.char_count} > MAX {CHUNK_MAX_CHARS}"
            )


# ============================================================================
# 6. Determinism
# ============================================================================


class TestDeterminism:
    def test_same_input_byte_identical(self, chunker):
        md = _markdown(
            [
                _page(1, "# A\n\npara one.\n\n## B\n\npara two."),
                _page(2, "# C\n\npara three."),
            ]
        )
        a = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=2)
        b = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=2)
        assert len(a) == len(b)
        for x, y in zip(a, b, strict=True):
            assert x.id == y.id
            assert x.content == y.content
            assert x.content_sha256 == y.content_sha256
            assert x.heading_path == y.heading_path
            assert x.page_start == y.page_start
            assert x.page_end == y.page_end
            assert x.char_count == y.char_count

    def test_stable_ordinal(self, chunker):
        md = _markdown([_page(1, "para one.\n\npara two.\n\npara three.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    def test_chunk_id_depends_only_on_inputs(self):
        sha = compute_content_sha256("hello")
        cid = compute_chunk_id("doc_a", 0, sha)
        cid_b = compute_chunk_id("doc_b", 0, sha)
        cid_same = compute_chunk_id("doc_a", 0, sha)
        assert cid == cid_same
        assert cid != cid_b

    def test_chunk_id_changes_with_ordinal(self):
        sha = compute_content_sha256("hello")
        assert compute_chunk_id("doc_a", 0, sha) != compute_chunk_id("doc_a", 1, sha)

    def test_chunk_id_changes_with_content(self):
        sha1 = compute_content_sha256("hello")
        sha2 = compute_content_sha256("world")
        assert compute_chunk_id("doc_a", 0, sha1) != compute_chunk_id("doc_a", 0, sha2)

    def test_compute_chunk_id_format(self):
        sha = compute_content_sha256("hello")
        cid = compute_chunk_id("doc_a", 0, sha)
        assert len(cid) == 64
        assert all(c in "0123456789abcdef" for c in cid)

    def test_compute_content_sha256_matches_hashlib(self):
        s = "héllo wörld 中文"
        assert compute_content_sha256(s) == hashlib.sha256(s.encode("utf-8")).hexdigest()


# ============================================================================
# 7. Page range correctness
# ============================================================================


class TestPageRange:
    def test_single_page_chunk_range(self, chunker):
        md = _markdown([_page(1, "hello")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert chunks[0].page_start == 1
        assert chunks[0].page_end == 1

    def test_multi_page_chunk_range(self, chunker):
        md = _markdown(
            [
                _page(1, "page one content goes here."),
                _page(2, "page two content here."),
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=2)
        # Pages seen across all chunks must include 1 and 2.
        all_pages = set()
        for ch in chunks:
            for p in range(ch.page_start, ch.page_end + 1):
                all_pages.add(p)
        assert 1 in all_pages
        assert 2 in all_pages


# ============================================================================
# 8. Unicode / encoding fidelity
# ============================================================================


class TestUnicode:
    def test_chinese_content_preserved(self, chunker):
        md = _markdown([_page(1, "这是中文段落，需要保留所有字符。")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert "这是中文段落" in chunks[0].content

    def test_emoji_preserved(self, chunker):
        md = _markdown([_page(1, "Rocket 🚀 and checkmark ✅ preserved.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert "🚀" in chunks[0].content
        assert "✅" in chunks[0].content

    def test_crlf_input_normalized(self, chunker):
        # Convert all \n to \r\n to simulate Windows line endings.
        md = _markdown([_page(1, "para one.\n\npara two.")]).replace("\n", "\r\n")
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        # Content should not contain \r (normalized to \n in pack output).
        for ch in chunks:
            assert "\r" not in ch.content


# ============================================================================
# 9. Code fence handling
# ============================================================================


class TestCodeFence:
    def test_code_block_preserved(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "```\ndef hello():\n    return 'world'\n```\n\nAfter code.",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        all_content = "\n\n".join(c.content for c in chunks)
        assert "def hello():" in all_content
        assert "return 'world'" in all_content

    def test_tilde_fence_supported(self, chunker):
        md = _markdown(
            [
                _page(
                    1,
                    "~~~\ncode line\n~~~\n\nAfter.",
                )
            ]
        )
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        all_content = "\n\n".join(c.content for c in chunks)
        assert "code line" in all_content


# ============================================================================
# 10. Content preservation (no loss)
# ============================================================================


class TestContentPreservation:
    def test_no_paragraph_lost(self, chunker):
        paras = [f"Paragraph {i} content." for i in range(20)]
        md = _markdown([_page(1, "\n\n".join(paras))])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        all_content = " ".join(c.content for c in chunks)
        for p in paras:
            assert p in all_content, f"missing: {p}"

    def test_escaped_page_marker_kept_as_text(self, chunker):
        # The collision-escaped form is `\<!-- page:N -->`. Our chunker
        # only matches unescaped `<!-- page:N -->` so this should be
        # preserved as body text.
        md = _markdown([_page(1, r"Some text \<!-- page:999 --> end.")])
        chunks = chunker.chunk(document_id=_DOC_ID, markdown=md, expected_page_count=1)
        assert r"\<!-- page:999 -->" in chunks[0].content


# ============================================================================
# 11. Compute helpers
# ============================================================================


class TestComputeHelpers:
    def test_compute_content_sha256_returns_hex(self):
        s = "test"
        h = compute_content_sha256(s)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_compute_chunk_id_rejects_negative_ordinal(self):
        with pytest.raises(ValueError, match="ordinal"):
            compute_chunk_id("doc_x", -1, "x" * 64)

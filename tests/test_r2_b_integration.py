"""End-to-end integration tests for R2-B (P2-R2-B4).

Per R2-B startup directive §35 (Real Parser → Builder integration):

    PDF fixture (pypdf.PdfWriter-generated)
        ↓
    PypdfParser.inspect / PypdfParser.extract
        ↓
    PdfTextQualityEvaluator
        ↓
    CanonicalMarkdownBuilder
        ↓
    CanonicalMarkdownPersistence
        ↓
    document.md on disk + read-back SHA matches

Constraints (directive §35):

- Fully offline
- No DB
- May write to tmp_path
- No HTTP / no worker / no Job

These tests confirm the full chain works end-to-end with real pypdf
output, not just hand-constructed ``PdfExtractionResult`` instances.
"""
from __future__ import annotations

import hashlib

import pytest

from pi_agent_core_py.web.knowledge.canonical_markdown import (
    CanonicalMarkdownBuilder,
    CanonicalMarkdownSource,
    NeedsOcrNotBuildable,
)
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.markdown_persistence import (
    CanonicalMarkdownPersistence,
)
from pi_agent_core_py.web.knowledge.pdf_parser import (
    PdfExtractionResult,
)
from pi_agent_core_py.web.knowledge.pdf_quality import (
    PdfTextQualityDecision,
    PdfTextQualityEvaluator,
)
from pi_agent_core_py.web.knowledge.pypdf_parser import PypdfParser
from tests._pdf_fixture_factory import (
    file_sha256,
    write_blank_pdf,
    write_text_pdf,
)

# ============================================================================
# Constants
# ============================================================================

_LIB_ID = "lib_integration01abc"
_DOC_ID = "doc_integration01def"
_PARSER_ID = "pypdf"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def parser():
    p = PypdfParser()
    yield p
    p.close()


@pytest.fixture
def evaluator():
    return PdfTextQualityEvaluator()


@pytest.fixture
def builder():
    return CanonicalMarkdownBuilder()


@pytest.fixture
def file_store(tmp_path):
    store = KnowledgeFileStore(tmp_path)
    store.ensure_root()
    store.create_library_dir(_LIB_ID)
    store.create_document_dir(_LIB_ID, _DOC_ID)
    return store


@pytest.fixture
def persistence(file_store):
    return CanonicalMarkdownPersistence(file_store)


def _make_source(pdf_path) -> CanonicalMarkdownSource:
    return CanonicalMarkdownSource(
        document_id=_DOC_ID,
        source_filename=pdf_path.name,
        source_sha256=file_sha256(pdf_path),
    )


# ============================================================================
# Integration: digital PDF → ready Markdown
# ============================================================================


class TestDigitalPdfEndToEnd:
    def test_text_pdf_produces_ready_markdown(
        self, parser, evaluator, builder, persistence, tmp_path
    ):
        pdf_path = tmp_path / "text.pdf"
        write_text_pdf(
            pdf_path,
            pages=[
                "First page with sufficient text content to avoid warnings.",
                "Second page also has enough body content for normal flow.",
            ],
            metadata={"/Title": "Integration Test Document"},
        )
        source = _make_source(pdf_path)
        sha_before = file_sha256(pdf_path)

        # Stage 1: extract
        extraction = parser.extract(pdf_path)
        assert isinstance(extraction, PdfExtractionResult)
        assert extraction.parser_id == _PARSER_ID
        assert len(extraction.pages) == 2

        # Stage 2: evaluate quality
        quality = evaluator.evaluate(extraction)
        assert quality.decision is PdfTextQualityDecision.USABLE

        # Stage 3: build canonical markdown
        artifact = builder.build(
            source=source, extraction=extraction, quality=quality
        )
        assert artifact.page_count == 2
        assert "<!-- page:1 -->" in artifact.content
        assert "<!-- page:2 -->" in artifact.content
        # Schema version frozen
        assert "pi-agent-canonical-markdown/v1" in artifact.content
        # Source filename propagated
        assert "text.pdf" in artifact.content

        # Stage 4: persist
        result = persistence.write(
            library_id=_LIB_ID, document_id=_DOC_ID, artifact=artifact
        )

        # Stage 5: read back and verify SHA matches
        on_disk = persistence.read(library_id=_LIB_ID, document_id=_DOC_ID)
        assert on_disk == artifact.content
        disk_sha = hashlib.sha256(on_disk.encode("utf-8")).hexdigest()
        assert disk_sha == artifact.content_sha256 == result.sha256

        # Source PDF untouched
        sha_after = file_sha256(pdf_path)
        assert sha_before == sha_after

    def test_quality_metrics_reflect_real_pdf(
        self, parser, evaluator, tmp_path
    ):
        # Real pypdf output: verify the evaluator picks up correct page_count
        # and non-zero text
        pdf_path = tmp_path / "real.pdf"
        write_text_pdf(
            pdf_path,
            pages=["alpha beta gamma delta epsilon zeta", "page two body text"],
        )
        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        assert quality.metrics.page_count == 2
        assert quality.metrics.total_non_whitespace_chars > 0
        assert quality.decision is PdfTextQualityDecision.USABLE

    def test_parser_id_and_version_propagate_to_artifact(
        self, parser, evaluator, builder, tmp_path
    ):
        pdf_path = tmp_path / "ver.pdf"
        write_text_pdf(pdf_path, pages=["some text content here"])
        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        artifact = builder.build(
            source=_make_source(pdf_path),
            extraction=extraction,
            quality=quality,
        )
        # The actual installed version propagates through (per R2-0 §17.1
        # requirement that R2 records actual resolved version)
        assert 'parser_id: "pypdf"' in artifact.content
        assert f'parser_version: "{parser.parser_version}"' in artifact.content

    def test_sha256_deterministic_across_runs(
        self, parser, evaluator, builder, tmp_path
    ):
        # Same PDF extracted twice → identical artifact bytes + SHA
        pdf_path = tmp_path / "det.pdf"
        write_text_pdf(
            pdf_path, pages=["deterministic content page one", "page two"]
        )
        e1 = parser.extract(pdf_path)
        e2 = parser.extract(pdf_path)
        q1 = evaluator.evaluate(e1)
        q2 = evaluator.evaluate(e2)
        a1 = builder.build(
            source=_make_source(pdf_path), extraction=e1, quality=q1
        )
        a2 = builder.build(
            source=_make_source(pdf_path), extraction=e2, quality=q2
        )
        assert a1.content == a2.content
        assert a1.content_sha256 == a2.content_sha256


# ============================================================================
# Integration: blank PDF → needs_ocr skip path
# ============================================================================


class TestBlankPdfNeedsOcrPath:
    def test_blank_pdf_decision_is_needs_ocr(
        self, parser, evaluator, tmp_path
    ):
        pdf_path = tmp_path / "blank.pdf"
        write_blank_pdf(pdf_path, page_count=2)
        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        assert quality.decision is PdfTextQualityDecision.NEEDS_OCR

    def test_blank_pdf_does_not_produce_artifact(
        self, parser, evaluator, builder, tmp_path
    ):
        pdf_path = tmp_path / "blank.pdf"
        write_blank_pdf(pdf_path, page_count=1)
        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        # Builder refuses needs_ocr
        with pytest.raises(NeedsOcrNotBuildable):
            builder.build(
                source=_make_source(pdf_path),
                extraction=extraction,
                quality=quality,
            )


# ============================================================================
# Integration: file integrity
# ============================================================================


class TestFileIntegrity:
    def test_source_pdf_sha_unchanged_after_full_pipeline(
        self, parser, evaluator, builder, persistence, tmp_path
    ):
        pdf_path = tmp_path / "integrity.pdf"
        write_text_pdf(
            pdf_path,
            pages=[
                "Page one content for integrity check.",
                "Page two content for integrity check.",
            ],
        )
        sha_before = file_sha256(pdf_path)

        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        artifact = builder.build(
            source=_make_source(pdf_path),
            extraction=extraction,
            quality=quality,
        )
        persistence.write(
            library_id=_LIB_ID, document_id=_DOC_ID, artifact=artifact
        )

        sha_after = file_sha256(pdf_path)
        assert sha_before == sha_after

    def test_persistence_only_writes_document_md(
        self, parser, evaluator, builder, persistence, file_store, tmp_path
    ):
        pdf_path = tmp_path / "sidecar.pdf"
        write_text_pdf(pdf_path, pages=["page content"])
        extraction = parser.extract(pdf_path)
        quality = evaluator.evaluate(extraction)
        artifact = builder.build(
            source=_make_source(pdf_path),
            extraction=extraction,
            quality=quality,
        )
        persistence.write(
            library_id=_LIB_ID, document_id=_DOC_ID, artifact=artifact
        )

        # Document directory should contain only document.md
        doc_dir = file_store.root / "libraries" / _LIB_ID / "documents" / _DOC_ID
        files = sorted(p.name for p in doc_dir.iterdir() if p.is_file())
        assert files == ["document.md"]

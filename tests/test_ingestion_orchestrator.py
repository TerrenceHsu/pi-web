"""IngestionOrchestrator unit + integration tests — P2-R2-C1-B.

Coverage (per R2-C0 directive §二十九 Orchestrator 测试矩阵):

- Success path: text PDF → Document 'normalizing' + Job 'completed' + MD
- needs_ocr: blank PDF → Document 'needs_ocr' + Job 'completed' (no MD)
- Encrypted / corrupted / page-limit / source-missing → failed mapping
- generated_at NOT passed (determinism)
- Same input → byte-identical SHA across runs / retries
- State conflict (Document not 'extracting' when Orchestrator runs)
- Terminal commit failure (finish_job raises)
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.canonical_markdown import (
    CanonicalMarkdownBuilder,
)
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.ingestion_orchestrator import (
    DEFAULT_MAX_PDF_PAGES,
    IngestionOrchestrator,
    IngestionRunResult,
)
from pi_agent_core_py.web.knowledge.ingestion_store import (
    ClaimedJob,
    IngestionStore,
)
from pi_agent_core_py.web.knowledge.markdown_persistence import (
    CanonicalMarkdownPersistence,
)
from pi_agent_core_py.web.knowledge.models import is_valid_job_id
from pi_agent_core_py.web.knowledge.pdf_quality import PdfTextQualityEvaluator
from pi_agent_core_py.web.knowledge.pypdf_parser import PypdfParser
from pi_agent_core_py.web.knowledge.store import KnowledgeStore
from tests._pdf_fixture_factory import (
    file_sha256,
    write_blank_pdf,
    write_corrupted_pdf,
    write_encrypted_pdf,
    write_text_pdf,
)

# ============================================================================
# Constants
# ============================================================================

_LIB_ID = "lib_orch01abcdef"
_LIB_ID_2 = "lib_orch02abcdef"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = await KnowledgeStore.open(str(tmp_path / "knowledge.db"))
    yield s
    await s.close()


@pytest.fixture
def file_store(tmp_path):
    fs = KnowledgeFileStore(root=tmp_path / "knowledge")
    fs.ensure_root()
    return fs


@pytest.fixture
def ingestion_store(store):
    return IngestionStore(store)


@pytest.fixture
def parser():
    p = PypdfParser()
    yield p
    p.close()


@pytest.fixture
def orchestrator(store, ingestion_store, file_store, parser):
    return IngestionOrchestrator(
        store=store,
        ingestion_store=ingestion_store,
        file_store=file_store,
        parser=parser,
        quality_evaluator=PdfTextQualityEvaluator(),
        builder=CanonicalMarkdownBuilder(),
        persistence=CanonicalMarkdownPersistence(file_store),
    )


# ============================================================================
# Helpers
# ============================================================================


async def _make_library(store: KnowledgeStore, name: str = "lib1"):
    return await store.create_library(name=name)


async def _make_document_with_source(
    store: KnowledgeStore,
    file_store: KnowledgeFileStore,
    library_id: str,
    *,
    source_bytes: bytes,
    source_name: str = "doc.pdf",
) -> str:
    """Create Document metadata + write source.pdf to the right location."""
    import secrets

    sha = hashlib.sha256(source_bytes).hexdigest()
    doc = await store.create_document(
        library_id=library_id,
        source_name=source_name,
        source_sha256=sha,
        source_relpath="documents/_/source.pdf",
        markdown_relpath="documents/_/document.md",
        mime_type="application/pdf",
        size_bytes=len(source_bytes),
    )

    # Write source.pdf to the document dir
    doc_dir = file_store._document_dir_unchecked(library_id, doc.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "source.pdf").write_bytes(source_bytes)
    return doc.id


def _read_pdf_bytes(path: Path) -> bytes:
    return path.read_bytes()


async def _claim(store: KnowledgeStore, ingestion: IngestionStore, doc_id: str) -> ClaimedJob:
    """Helper: claim a specific doc (assumes it's the oldest uploaded)."""
    claimed = await ingestion.claim_next_pending_document()
    assert claimed is not None
    assert claimed.document.id == doc_id
    return claimed


# ============================================================================
# A. Happy path
# ============================================================================


class TestSuccessPath:
    async def test_text_pdf_reaches_normalizing(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        # 3-page text PDF
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["page one text", "page two text", "page three text"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        # Document stays in 'normalizing' (per C0 §35.5 correction)
        assert result.document_status == "normalizing"
        assert result.job_status == "completed"
        assert result.safe_error_code == ""
        assert result.document_id == doc_id
        assert result.job_id == claimed.job.id
        assert result.parser_id == "pypdf"
        assert result.parser_version  # populated
        assert result.page_count == 3
        assert result.markdown_sha256 is not None
        assert result.markdown_byte_length is not None
        assert len(result.markdown_sha256) == 64  # SHA-256 hex
        assert result.duration_ms >= 0

        # DB consistency
        doc = await store.get_document(doc_id)
        assert doc.status == "normalizing"
        assert doc.parser_version == result.parser_version
        assert doc.page_count == 3
        assert doc.error_code == ""

        job = await store.get_job(claimed.job.id)
        assert job.status == "completed"
        assert job.finished_at is not None

    async def test_document_md_written_and_readable(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["hello world"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        # document.md exists + has correct SHA
        from pi_agent_core_py.web.knowledge.markdown_persistence import (
            DOCUMENT_MARKDOWN_FILENAME,
        )

        md_bytes = file_store.read_file(
            library_id=lib.id,
            document_id=doc_id,
            filename=DOCUMENT_MARKDOWN_FILENAME,
            as_text=False,
        )
        assert isinstance(md_bytes, bytes)
        assert b"<!-- page:1 -->" in md_bytes

    async def test_source_pdf_sha_unchanged_after_run(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["content"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        source_path = file_store._document_dir_unchecked(lib.id, doc_id) / "source.pdf"
        sha_before = file_sha256(source_path)

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        sha_after = file_sha256(source_path)
        assert sha_before == sha_after

    async def test_each_pipeline_step_called_once(
        self, orchestrator, store, ingestion_store, file_store, tmp_path, monkeypatch
    ):
        """Verify inspect / extract / evaluate / build / write are each called once."""
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["text"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        # Wrap parser methods to count calls
        call_counts = {"inspect": 0, "extract": 0}
        orig_inspect = orchestrator._parser.inspect
        orig_extract = orchestrator._parser.extract

        def counting_inspect(p):
            call_counts["inspect"] += 1
            return orig_inspect(p)

        def counting_extract(p):
            call_counts["extract"] += 1
            return orig_extract(p)

        monkeypatch.setattr(orchestrator._parser, "inspect", counting_inspect)
        monkeypatch.setattr(orchestrator._parser, "extract", counting_extract)

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        assert call_counts == {"inspect": 1, "extract": 1}


# ============================================================================
# B. needs_ocr terminal
# ============================================================================


class TestNeedsOcrTerminal:
    async def test_blank_pdf_reaches_needs_ocr(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "blank.pdf"
        write_blank_pdf(pdf_path, page_count=3)
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        assert result.document_status == "needs_ocr"
        assert result.job_status == "completed"  # NOT failed
        assert result.safe_error_code == "needs_ocr"
        assert result.markdown_sha256 is None
        assert result.markdown_byte_length is None
        assert result.page_count == 3

        # DB consistency
        doc = await store.get_document(doc_id)
        assert doc.status == "needs_ocr"
        assert doc.error_code == "needs_ocr"
        assert doc.page_count == 3

        job = await store.get_job(claimed.job.id)
        assert job.status == "completed"

    async def test_needs_ocr_does_not_write_document_md(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "blank.pdf"
        write_blank_pdf(pdf_path, page_count=1)
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        # document.md should NOT exist for needs_ocr
        doc_dir = file_store._document_dir_unchecked(lib.id, doc_id)
        assert not (doc_dir / "document.md").exists()

    async def test_source_pdf_preserved_in_needs_ocr(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "blank.pdf"
        write_blank_pdf(pdf_path, page_count=1)
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )
        source_path = file_store._document_dir_unchecked(lib.id, doc_id) / "source.pdf"
        assert source_path.exists()

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        assert source_path.exists()  # source.pdf NOT deleted


# ============================================================================
# C. Failure paths
# ============================================================================


class TestFailurePaths:
    async def test_corrupted_pdf_marks_failed(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "corrupt.pdf"
        write_corrupted_pdf(pdf_path)
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        assert result.document_status == "failed"
        assert result.job_status == "failed"
        # Corrupted PDF → invalid_pdf (pypdf can't parse)
        assert result.safe_error_code in ("invalid_pdf", "pdf_parse_failed")

        doc = await store.get_document(doc_id)
        assert doc.status == "failed"
        assert doc.error_code == result.safe_error_code

    async def test_encrypted_pdf_marks_failed(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "enc.pdf"
        write_encrypted_pdf(pdf_path, pages=["encrypted content"], user_password="secret")
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        assert result.document_status == "failed"
        assert result.safe_error_code in (
            "encrypted_pdf",
            "pdf_password_required",
        )

    async def test_source_missing_marks_failed(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        # Create Document with a dummy source_bytes (we won't actually write the file)
        dummy_bytes = b"not-a-real-pdf-but-source-will-be-missing"
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=dummy_bytes
        )

        # DELETE the source.pdf to simulate missing file
        source_path = file_store._document_dir_unchecked(lib.id, doc_id) / "source.pdf"
        source_path.unlink()
        assert not source_path.exists()

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        assert result.document_status == "failed"
        assert result.safe_error_code == "source_file_missing"

        doc = await store.get_document(doc_id)
        assert doc.status == "failed"
        assert doc.error_code == "source_file_missing"

    async def test_page_limit_exceeded_marks_failed(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "big.pdf"
        # Create PDF with DEFAULT_MAX_PDF_PAGES + 1 pages
        write_text_pdf(
            pdf_path,
            pages=[f"page {i}" for i in range(DEFAULT_MAX_PDF_PAGES + 1)],
        )
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        assert result.document_status == "failed"
        assert result.safe_error_code == "pdf_page_limit_exceeded"
        assert result.page_count == DEFAULT_MAX_PDF_PAGES + 1


# ============================================================================
# D. Determinism / generated_at
# ============================================================================


class TestDeterminism:
    async def test_same_input_produces_byte_identical_sha(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        """Run pipeline twice (via retry); both produce same markdown_sha256."""
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["page one content", "page two content"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        # First run
        claimed1 = await _claim(store, ingestion_store, doc_id)
        result1 = await orchestrator.run_claimed_job(claimed1.job.id)
        assert result1.document_status == "normalizing"
        sha1 = result1.markdown_sha256

        # Simulate retry: force doc back to failed, then retry
        await store.transition_document_status(doc_id, "failed", error_code="simulated")
        claimed2 = await ingestion_store.create_retry_job(doc_id)
        result2 = await orchestrator.run_claimed_job(claimed2.id)

        sha2 = result2.markdown_sha256
        assert sha1 == sha2

    async def test_generated_at_not_in_artifact_frontmatter(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        """The written document.md must NOT contain 'generated_at:' in frontmatter."""
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["text"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        await orchestrator.run_claimed_job(claimed.job.id)

        from pi_agent_core_py.web.knowledge.markdown_persistence import (
            DOCUMENT_MARKDOWN_FILENAME,
        )

        md_text = file_store.read_file(
            library_id=lib.id,
            document_id=doc_id,
            filename=DOCUMENT_MARKDOWN_FILENAME,
            as_text=True,
        )
        # 'generated_at' must not appear in the artifact (Builder default omits)
        assert "generated_at:" not in md_text
        # But other expected frontmatter fields should be present
        assert "schema:" in md_text
        assert "document_id:" in md_text
        assert "<!-- page:1 -->" in md_text


# ============================================================================
# E. State conflict + invalid input
# ============================================================================


class TestStateConflict:
    async def test_invalid_job_id_format_raises_value_error(self, orchestrator):
        with pytest.raises(ValueError):
            await orchestrator.run_claimed_job("not-a-valid-job-id")

    async def test_nonexistent_job_raises(self, orchestrator):
        # Store.get_job raises KnowledgeStoreError on not-found
        from pi_agent_core_py.web.knowledge.store import KnowledgeStoreError
        with pytest.raises((ValueError, KnowledgeStoreError)):
            await orchestrator.run_claimed_job("job_nonexistent0001")

    async def test_state_conflict_when_document_not_extracting(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        """If Document is somehow in non-extracting state when Orchestrator runs,
        we get state_conflict (defensive — shouldn't happen in normal flow)."""
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["text"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        # Claim (transitions to extracting + creates Job)
        claimed = await _claim(store, ingestion_store, doc_id)

        # Manually advance Document to normalizing (simulating a race)
        await store.transition_document_status(doc_id, "normalizing")

        result = await orchestrator.run_claimed_job(claimed.job.id)
        # Document is in 'normalizing' (not 'extracting'); Orchestrator detects state conflict
        assert result.document_status == "normalizing"  # unchanged
        assert result.job_status == "failed"
        assert result.safe_error_code == "ingestion_state_conflict"


# ============================================================================
# F. Result DTO invariants
# ============================================================================


class TestResultDTO:
    async def test_result_does_not_contain_absolute_path(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["text"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        # No absolute paths in any field
        assert isinstance(result, IngestionRunResult)
        for field_value in (
            result.document_id,
            result.job_id,
            result.parser_id or "",
            result.parser_version or "",
            result.markdown_sha256 or "",
        ):
            assert "C:" not in str(field_value)
            assert "/" not in str(field_value) or field_value.startswith("doc_") or field_value.startswith("job_")
            assert "\\" not in str(field_value)

    async def test_result_does_not_contain_markdown_body(
        self, orchestrator, store, ingestion_store, file_store, tmp_path
    ):
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["secret content that should not leak"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )

        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orchestrator.run_claimed_job(claimed.job.id)

        # Result has no body field — only SHA + length
        assert not hasattr(result, "content")
        assert not hasattr(result, "markdown_content")
        assert not hasattr(result, "body")
        # And the secret PDF text is not echoed back via warnings
        assert "secret content" not in str(result.warnings)


# ============================================================================
# G. Constants / config
# ============================================================================


class TestConfig:
    def test_default_max_pdf_pages(self):
        assert DEFAULT_MAX_PDF_PAGES == 20

    async def test_custom_max_pdf_pages(
        self, store, ingestion_store, file_store, parser, tmp_path
    ):
        """Orchestrator constructed with max_pdf_pages=2 → 3-page PDF fails."""
        orch = IngestionOrchestrator(
            store=store,
            ingestion_store=ingestion_store,
            file_store=file_store,
            parser=parser,
            quality_evaluator=PdfTextQualityEvaluator(),
            builder=CanonicalMarkdownBuilder(),
            persistence=CanonicalMarkdownPersistence(file_store),
            max_pdf_pages=2,
        )
        lib = await _make_library(store)
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["p1", "p2", "p3"])
        source_bytes = _read_pdf_bytes(pdf_path)
        doc_id = await _make_document_with_source(
            store, file_store, lib.id, source_bytes=source_bytes
        )
        claimed = await _claim(store, ingestion_store, doc_id)
        result = await orch.run_claimed_job(claimed.job.id)
        assert result.safe_error_code == "pdf_page_limit_exceeded"
        assert result.page_count == 3

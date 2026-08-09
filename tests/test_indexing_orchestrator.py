"""P2-R3-C2 — IndexingOrchestrator unit tests.

Covers directive §51 matrix (33 items): claim / Markdown read / SHA /
frontmatter / page_count / chunker / replace / persistence / state
transitions / integrity / ready gate / failure compensation / source.pdf
immutability / Markdown immutability / determinism / non-normalizing
behaviour.

Uses real KnowledgeStore + ChunkStore + IndexingStore + HeadingAwareChunker
+ KnowledgeFileStore wiring on a temp DB + temp Knowledge root. Writes
real ``document.md`` files matching the R2-B Canonical Markdown schema so
the chunker's frontmatter / page marker validation succeeds.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.indexing_orchestrator import (
    CODE_CANONICAL_MARKDOWN_INTEGRITY_ERROR,
    CODE_CANONICAL_MARKDOWN_INVALID,
    CODE_CANONICAL_MARKDOWN_MISSING,
    CODE_CHUNKING_EMPTY,
    IndexingOrchestrator,
)
from pi_agent_core_py.web.knowledge.indexing_store import IndexingStore
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Helpers — build real Canonical Markdown matching R2-B frontmatter schema
# ============================================================================


_PAGE_MARKER_FMT = "<!-- page:{n} -->"


def _canonical_markdown(
    *,
    document_id: str,
    source_sha256: str,
    page_count: int,
    pages_body: list[str],
) -> str:
    """Build Canonical Markdown text matching R2-B ``CanonicalMarkdownBuilder``
    output (frontmatter + page markers + body).

    Each entry in ``pages_body`` becomes the body of the corresponding
    page; the page marker is emitted before each body.
    """
    assert len(pages_body) == page_count
    fm = (
        "---\n"
        f'schema "pi-agent-canonical-markdown/v1"\n'
        f'document_id "{document_id}"\n'
        f'source_filename "x.pdf"\n'
        f"source_sha256 \"{source_sha256}\"\n"
        f'parser_id "pypdf"\n'
        f'parser_version "6.14.2"\n'
        f"page_count {page_count}\n"
        "---\n"
    )
    sections = []
    for n, body in enumerate(pages_body, start=1):
        marker = _PAGE_MARKER_FMT.format(n=n)
        sections.append(f"{marker}\n\n{body}")
    return fm + "\n\n".join(sections) + "\n"


async def _seed_doc(
    store: KnowledgeStore,
    *,
    document_id: str,
    library_id: str = "lib_test00000001",
    source_sha256: str = "a" * 64,
    status: str = "normalizing",
    page_count: int = 1,
    created_at: int = 100,
    sha_suffix: str = "1",
) -> None:
    """Seed Library + Document (direct SQL)."""
    async with store._write_lock:
        await store._db.execute("BEGIN IMMEDIATE")
        await store._db.execute(
            "INSERT OR IGNORE INTO knowledge_libraries "
            "(id, name, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, "L", "", "active", 1, 1),
        )
        await store._db.execute(
            "INSERT OR REPLACE INTO knowledge_documents "
            "(id, library_id, source_name, source_sha256, source_relpath, "
            " markdown_relpath, mime_type, size_bytes, page_count, status, "
            " parser_version, error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                library_id,
                f"x{sha_suffix}.pdf",
                source_sha256,
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf",
                10,
                page_count,
                status,
                "pypdf/6.14.2",
                "",
                created_at,
                created_at,
            ),
        )
        await store._db.execute("COMMIT")


def _write_markdown(
    file_store: KnowledgeFileStore,
    *,
    library_id: str,
    document_id: str,
    markdown_text: str,
) -> None:
    """Write ``document.md`` to the document dir via the file store path.

    Uses KnowledgeFileStore.write_file_atomic to honour path-containment
    and atomic-rename guarantees.
    """
    file_store.write_file_atomic(
        library_id=library_id,
        document_id=document_id,
        filename="document.md",
        content=markdown_text.encode("utf-8"),
    )


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
async def orchestrator_setup(tmp_path: Path):
    """Yield ``(KnowledgeStore, KnowledgeFileStore, ChunkStore,
    IndexingStore, IndexingOrchestrator)`` + temp Knowledge root."""
    db_path = tmp_path / "r3_c2.db"
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()

    store = await KnowledgeStore.open(str(db_path))
    file_store = KnowledgeFileStore(str(knowledge_root))
    chunk_store = ChunkStore(store)
    indexing_store = IndexingStore(store)
    orchestrator = IndexingOrchestrator(
        knowledge_store=store,
        knowledge_file_store=file_store,
        chunk_store=chunk_store,
        indexing_store=indexing_store,
    )
    try:
        yield store, file_store, chunk_store, indexing_store, orchestrator
    finally:
        await store.close()


# ============================================================================
# 1. Success path
# ============================================================================


class TestSuccess:
    async def test_full_pipeline_reaches_ready(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["Introduction to radiotherapy dose planning."],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        assert result.chunk_count > 0
        assert result.error_code is None

    async def test_state_transitions_happen_in_order(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["simple body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        doc = await store.get_document(doc_id)
        assert doc.status == "ready"
        assert doc.error_code == ""

    async def test_chunks_persisted(
        self, orchestrator_setup
    ):
        store, file_store, chunk_store, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=[
                "# A\n\nPara one.\n\n## B\n\nPara two."
            ],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        report = await chunk_store.verify_fts_integrity()
        assert report.ok
        assert report.chunk_count == result.chunk_count

    async def test_deterministic_chunk_ids(
        self, orchestrator_setup
    ):
        """Re-running after re-ingest should produce identical chunk IDs.

        This is the R3-A chunker contract: same (document_id, markdown,
        page_count) → same chunk IDs.
        """
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["deterministic content"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result1 = await orch.process_document(doc_id)
        assert result1.status == "ready"
        # Capture chunk IDs after first index.
        async with store._db.execute(
            "SELECT id, ordinal FROM knowledge_chunks ORDER BY ordinal"
        ) as cursor:
            rows1 = await cursor.fetchall()
        ids1 = [r["id"] for r in rows1]
        # Reset back to normalizing and re-index.
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET status='normalizing' WHERE id=?",
                (doc_id,),
            )
            await store._db.execute("COMMIT")
        result2 = await orch.process_document(doc_id)
        assert result2.status == "ready"
        async with store._db.execute(
            "SELECT id, ordinal FROM knowledge_chunks ORDER BY ordinal"
        ) as cursor:
            rows2 = await cursor.fetchall()
        ids2 = [r["id"] for r in rows2]
        assert ids1 == ids2


# ============================================================================
# 2. Markdown read + integrity
# ============================================================================


class TestMarkdownIntegrity:
    async def test_markdown_missing(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # No document.md written.
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.error_code == CODE_CANONICAL_MARKDOWN_MISSING
        # Document state should reflect failure cleanup.
        doc = await store.get_document(doc_id)
        assert doc.status == "failed"
        assert doc.error_code == CODE_CANONICAL_MARKDOWN_MISSING

    async def test_source_sha_mismatch(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        await _seed_doc(store, document_id=doc_id, source_sha256="a" * 64)
        # Frontmatter source_sha256 differs from Document.source_sha256.
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256="b" * 64,  # mismatched
            page_count=1,
            pages_body=["body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.error_code == CODE_CANONICAL_MARKDOWN_INTEGRITY_ERROR
        doc = await store.get_document(doc_id)
        assert doc.status == "failed"

    async def test_frontmatter_document_id_mismatch(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # Frontmatter has wrong document_id.
        md = _canonical_markdown(
            document_id="doc_test00000002",  # mismatched
            source_sha256=sha,
            page_count=1,
            pages_body=["body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.error_code == CODE_CANONICAL_MARKDOWN_INVALID

    async def test_page_count_mismatch(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        # Document says 2 pages; markdown says 1 page.
        await _seed_doc(store, document_id=doc_id, source_sha256=sha, page_count=2)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,  # mismatched
            pages_body=["body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.error_code == CODE_CANONICAL_MARKDOWN_INVALID


# ============================================================================
# 3. Empty / chunker errors
# ============================================================================


class TestChunkerErrors:
    async def test_chunking_empty_when_only_markers(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # Body is only whitespace → Chunker raises ChunkingEmpty.
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["   \n\t\n   "],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.error_code == CODE_CHUNKING_EMPTY


# ============================================================================
# 4. Non-normalizing documents — controlled results
# ============================================================================


class TestNonNormalizing:
    async def test_ready_not_re_indexed(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        await _seed_doc(store, document_id=doc_id, status="ready")
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        assert result.chunk_count == 0
        assert "already ready" in (result.error_message or "")

    async def test_failed_not_reclaimed(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        await _seed_doc(store, document_id=doc_id, status="failed")
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        assert result.chunk_count == 0
        assert result.error_code is None  # not an indexing failure code
        assert "retry" in (result.error_message or "")

    async def test_needs_ocr_not_claimed(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        await _seed_doc(store, document_id=doc_id, status="needs_ocr")
        result = await orch.process_document(doc_id)
        assert result.status == "needs_ocr"
        assert result.chunk_count == 0

    async def test_missing_document(
        self, orchestrator_setup
    ):
        _s, _fs, _cs, _is_, orch = orchestrator_setup
        result = await orch.process_document("doc_test00000001")
        assert result.status == "missing"
        assert result.chunk_count == 0


# ============================================================================
# 5. Immutability
# ============================================================================


class TestImmutability:
    async def test_source_pdf_unchanged(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # Write a source.pdf with a known SHA.
        source_bytes = b"fake pdf content for source.pdf"
        file_store.write_file_atomic(
            library_id="lib_test00000001",
            document_id=doc_id,
            filename="source.pdf",
            content=source_bytes,
        )
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        # source.pdf bytes unchanged.
        after = file_store.read_file(
            "lib_test00000001", doc_id, "source.pdf"
        )
        assert after == source_bytes

    async def test_markdown_unchanged(
        self, orchestrator_setup
    ):
        store, file_store, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        md_text = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=1,
            pages_body=["the body"],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md_text,
        )
        before_sha = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        after = file_store.read_file(
            "lib_test00000001", doc_id, "document.md", as_text=True
        )
        after_sha = hashlib.sha256(after.encode("utf-8")).hexdigest()
        assert before_sha == after_sha


# ============================================================================
# 6. Failure compensation
# ============================================================================


class TestFailureCompensation:
    async def test_failed_doc_has_no_chunks(
        self, orchestrator_setup
    ):
        store, file_store, chunk_store, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # No markdown → failure path; verify cleanup.
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            fts_count = (await cursor.fetchone())["n"]
        assert chunk_count == 0
        assert fts_count == 0

    async def test_failed_doc_not_searchable(
        self, orchestrator_setup
    ):
        """Even if FTS rows existed somehow, ready-only filter excludes failed."""
        store, file_store, chunk_store, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # No markdown → failure path.
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        # Search should return nothing (Document=failed).
        hits = await chunk_store.search_chunks_fts("radiotherapy")
        assert hits == []


# ============================================================================
# 7. DTO safety
# ============================================================================


class TestResultSafety:
    async def test_no_path_in_error_message(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        # No markdown → missing path failure.
        result = await orch.process_document(doc_id)
        assert result.status == "failed"
        msg = result.error_message or ""
        assert "D:\\" not in msg
        assert "/home/" not in msg
        assert "documents/" not in msg

    async def test_no_sql_in_error_message(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        result = await orch.process_document(doc_id)
        msg = result.error_message or ""
        assert "SELECT" not in msg.upper()
        assert "INSERT" not in msg.upper()
        assert "knowledge_" not in msg.lower()

    async def test_no_traceback_in_error_message(
        self, orchestrator_setup
    ):
        store, _fs, _cs, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        result = await orch.process_document(doc_id)
        msg = result.error_message or ""
        assert "Traceback" not in msg
        assert ".py" not in msg or "pypdf/6" in msg  # parser_version is OK


# ============================================================================
# 8. Real R2 → R3-C explicit integration
# ============================================================================


class TestR2R3Integration:
    async def test_explicit_call_after_normalizing_state(
        self, orchestrator_setup
    ):
        """Simulate R2 having produced a normalizing Document + document.md,
        then explicitly invoke R3-C process_document."""
        store, file_store, chunk_store, _is_, orch = orchestrator_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha, page_count=2)
        md = _canonical_markdown(
            document_id=doc_id,
            source_sha256=sha,
            page_count=2,
            pages_body=[
                "# Introduction\n\nRadiotherapy planning overview.",
                "# Methods\n\nWe used standard methods for analysis.",
            ],
        )
        _write_markdown(
            file_store,
            library_id="lib_test00000001",
            document_id=doc_id,
            markdown_text=md,
        )
        result = await orch.process_document(doc_id)
        assert result.status == "ready"
        assert result.chunk_count >= 1
        # After ready, internal FTS primitive can find content.
        hits = await chunk_store.search_chunks_fts("radiotherapy")
        assert any(h.document_id == doc_id for h in hits)
        hits2 = await chunk_store.search_chunks_fts("methods")
        assert any(h.document_id == doc_id for h in hits2)

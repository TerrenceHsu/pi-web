"""P2-R2-C4 — Restart + shutdown tests.

Per directive §十五/§十六/§十七/§十八:
- Pending startup recovery (DB pending + no notify → manager picks up)
- Running startup recovery (Job=running + extracting/normalizing → failed+interrupted)
- Recovery idempotency (second start() affects 0 rows)
- Graceful shutdown within grace period
- Shutdown late-success protection (conditional UPDATE prevents overwrite)

Most of these scenarios are covered by C2-A tests at the unit level.
This file adds integration-level tests via direct Worker Manager lifecycle.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from pi_agent_core_py.web.knowledge.canonical_markdown import (
    CanonicalMarkdownBuilder,
)
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.ingestion_orchestrator import (
    IngestionOrchestrator,
)
from pi_agent_core_py.web.knowledge.ingestion_store import (
    EXTRACT_STAGE,
    INGESTION_INTERRUPTED,
    IngestionStore,
)
from pi_agent_core_py.web.knowledge.ingestion_worker import (
    IngestionWorkerManager,
)
from pi_agent_core_py.web.knowledge.markdown_persistence import (
    CanonicalMarkdownPersistence,
)
from pi_agent_core_py.web.knowledge.pdf_quality import PdfTextQualityEvaluator
from pi_agent_core_py.web.knowledge.pypdf_parser import PypdfParser
from pi_agent_core_py.web.knowledge.store import KnowledgeStore
from tests._pdf_fixture_factory import write_text_pdf

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


def _make_manager(
    store, ingestion_store, file_store, parser, *, poll=10.0, grace=2.0
) -> IngestionWorkerManager:
    """Construct a Manager with long poll (so we can test startup recovery
    in isolation, before polling kicks in)."""
    orchestrator = IngestionOrchestrator(
        store=store,
        ingestion_store=ingestion_store,
        file_store=file_store,
        parser=parser,
        quality_evaluator=PdfTextQualityEvaluator(),
        builder=CanonicalMarkdownBuilder(),
        persistence=CanonicalMarkdownPersistence(file_store),
    )
    return IngestionWorkerManager(
        orchestrator=orchestrator,
        ingestion_store=ingestion_store,
        store=store,
        parser=parser,
        poll_interval_seconds=poll,
        shutdown_grace_seconds=grace,
        owns_parser=False,
    )


# ============================================================================
# A. Pending startup recovery
# ============================================================================


class TestPendingStartupRecovery:
    async def test_pending_doc_processed_without_notify(
        self, store, file_store, parser, tmp_path
    ):
        """Pre-create Document in 'uploaded' state; start Manager WITHOUT notify;
        Manager's polling fallback or initial scan picks it up.
        """
        ingestion_store = IngestionStore(store)
        mgr = _make_manager(
            store, ingestion_store, file_store, parser, poll=0.1
        )

        lib = await store.create_library(name="lib1")
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["pending content"])
        pdf_bytes = pdf_path.read_bytes()

        sha = hashlib.sha256(pdf_bytes).hexdigest()
        doc = await store.create_document(
            library_id=lib.id,
            source_name="pending.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        # Write source.pdf
        doc_dir = file_store._document_dir_unchecked(lib.id, doc.id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "source.pdf").write_bytes(pdf_bytes)

        # Start manager — NO notify call
        await mgr.start()
        try:
            # Wait for idle (worker picks up via initial drain)
            idle = await mgr.wait_until_idle(timeout=30.0)
            assert idle is True

            doc_after = await store.get_document(doc.id)
            assert doc_after.status == "normalizing"
        finally:
            await mgr.stop()


# ============================================================================
# B. Running startup recovery
# ============================================================================


class TestRunningStartupRecovery:
    async def test_running_job_extracting_doc_marked_failed(
        self, store, ingestion_store, file_store, parser
    ):
        """Pre-create Document=extracting + Job=running; Manager.start() recovers."""
        mgr = _make_manager(store, ingestion_store, file_store, parser)

        lib = await store.create_library(name="lib1")
        sha = hashlib.sha256(b"some pdf bytes").hexdigest()
        doc = await store.create_document(
            library_id=lib.id,
            source_name="preset.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        await store.transition_document_status(doc.id, "extracting")
        await store.create_job(document_id=doc.id, stage=EXTRACT_STAGE)

        await mgr.start()
        try:
            # Recovery happens during start() — both Job + Document marked failed
            doc_after = await store.get_document(doc.id)
            assert doc_after.status == "failed"
            assert doc_after.error_code == INGESTION_INTERRUPTED

            jobs = await store.list_jobs_for_document(doc.id)
            failed_jobs = [
                j for j in jobs
                if j.status == "failed"
                and j.safe_error_code == INGESTION_INTERRUPTED
            ]
            assert len(failed_jobs) >= 1
        finally:
            await mgr.stop()

    async def test_running_job_normalizing_doc_marked_failed(
        self, store, ingestion_store, file_store, parser
    ):
        """Same recovery for normalizing Documents."""
        mgr = _make_manager(store, ingestion_store, file_store, parser)

        lib = await store.create_library(name="lib1")
        sha = hashlib.sha256(b"other bytes").hexdigest()
        doc = await store.create_document(
            library_id=lib.id,
            source_name="preset.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        await store.transition_document_status(doc.id, "extracting")
        await store.transition_document_status(doc.id, "normalizing")
        await store.create_job(document_id=doc.id, stage=EXTRACT_STAGE)

        await mgr.start()
        try:
            doc_after = await store.get_document(doc.id)
            assert doc_after.status == "failed"
            assert doc_after.error_code == INGESTION_INTERRUPTED
        finally:
            await mgr.stop()

    async def test_terminal_records_untouched_by_recovery(
        self, store, ingestion_store, file_store, parser, tmp_path
    ):
        """needs_ocr / failed / ready Documents must NOT be modified by recovery."""
        mgr = _make_manager(store, ingestion_store, file_store, parser)

        lib = await store.create_library(name="lib1")
        pdf_path = tmp_path / "blank.pdf"
        from tests._pdf_fixture_factory import write_blank_pdf

        write_blank_pdf(pdf_path, page_count=1)
        pdf_bytes = pdf_path.read_bytes()
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        # Manually create + advance to needs_ocr
        doc = await store.create_document(
            library_id=lib.id,
            source_name="blank.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        await store.transition_document_status(doc.id, "extracting")
        await store.transition_document_status(
            doc.id, "needs_ocr", error_code="needs_ocr"
        )
        # No running Job — terminal state

        await mgr.start()
        try:
            doc_after = await store.get_document(doc.id)
            # needs_ocr preserved — recovery doesn't touch terminal records
            assert doc_after.status == "needs_ocr"
            assert doc_after.error_code == "needs_ocr"
        finally:
            await mgr.stop()


# ============================================================================
# C. Recovery idempotency
# ============================================================================


class TestRecoveryIdempotency:
    async def test_two_starts_dont_double_mark(
        self, store, ingestion_store, file_store, parser
    ):
        """Calling start() twice (via stop+start) doesn't double-mark records."""
        mgr = _make_manager(store, ingestion_store, file_store, parser)

        lib = await store.create_library(name="lib1")
        sha = hashlib.sha256(b"bytes").hexdigest()
        doc = await store.create_document(
            library_id=lib.id,
            source_name="preset.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        await store.transition_document_status(doc.id, "extracting")
        await store.create_job(document_id=doc.id, stage=EXTRACT_STAGE)

        await mgr.start()
        await mgr.stop()
        await mgr.start()  # second start
        await mgr.stop()

        # Single Job still in DB (not duplicated)
        jobs = await store.list_jobs_for_document(doc.id)
        assert len(jobs) == 1
        # Status is failed (from recovery)
        assert jobs[0].status == "failed"


# ============================================================================
# D. Graceful shutdown within grace
# ============================================================================


class TestGracefulShutdown:
    async def test_idle_shutdown_completes_quickly(
        self, store, ingestion_store, file_store, parser
    ):
        """When idle (no active Job), stop() returns quickly."""
        import time as _time

        mgr = _make_manager(
            store, ingestion_store, file_store, parser, grace=2.0
        )
        await mgr.start()
        await mgr.wait_until_idle(timeout=1.0)

        t0 = _time.monotonic()
        await mgr.stop()
        elapsed = _time.monotonic() - t0

        # Should be well under 2s grace
        assert elapsed < 1.5
        assert mgr.state == "stopped"

    async def test_shutdown_stops_accepting_new_claims(
        self, store, ingestion_store, file_store, parser, tmp_path
    ):
        """After stop(), Manager doesn't claim new pending Jobs.

        Pre-create one pending Job, stop Manager, then add another pending Job.
        Second Job should remain 'uploaded' (no worker to claim).
        """
        mgr = _make_manager(
            store, ingestion_store, file_store, parser, poll=10.0
        )
        await mgr.start()
        await mgr.stop()

        lib = await store.create_library(name="lib1")
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["content"])
        pdf_bytes = pdf_path.read_bytes()
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        doc = await store.create_document(
            library_id=lib.id,
            source_name="t.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        doc_dir = file_store._document_dir_unchecked(lib.id, doc.id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "source.pdf").write_bytes(pdf_bytes)

        # Wait briefly — Manager is stopped, doc should stay uploaded
        await asyncio.sleep(0.5)
        doc_after = await store.get_document(doc.id)
        assert doc_after.status == "uploaded"  # not claimed

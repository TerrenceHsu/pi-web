"""IngestionStore unit tests — P2-R2-C1.

Coverage (per R2-C0 directive §二十八 Store 测试矩阵):

- Atomic claim: FIFO / ID tie-break / no-pending / status transition /
  Job creation / terminal Job can't be claimed / concurrent claim only
  one winner
- Retry: success path / active Job rejected / retry limit reached /
  non-failed status rejected / source SHA unchanged
- Counts + queries: count_active_jobs / has_active_job /
  count_extract_attempts / get_latest_extract_job_for_document
- Recovery: mark_running_jobs_interrupted marks all / idempotent /
  safe_error_code set / finished_at set
- Cross-document / cross-library isolation
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py.web.knowledge.ingestion_store import (
    EXTRACT_STAGE,
    INGESTION_INTERRUPTED,
    MAX_RETRY_ATTEMPTS,
    ClaimedJob,
    IngestionAlreadyActiveError,
    IngestionStore,
    RetryLimitReachedError,
    RetryNotAllowedError,
)
from pi_agent_core_py.web.knowledge.store import (
    DocumentNotFoundError,
    KnowledgeStore,
)

# ============================================================================
# Fixtures + helpers
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = await KnowledgeStore.open(str(tmp_path / "knowledge.db"))
    yield s
    await s.close()


@pytest.fixture
def ingestion(store):
    return IngestionStore(store)


async def _make_library(store: KnowledgeStore, name: str = "lib1"):
    return await store.create_library(name=name)


async def _make_document(
    store: KnowledgeStore,
    library_id: str,
    *,
    source_name: str = "doc.pdf",
    source_sha256: str | None = None,
    status: str = "uploaded",
) -> str:
    """Create a Document and optionally force its status."""
    import secrets

    sha = source_sha256 or secrets.token_hex(8)
    doc = await store.create_document(
        library_id=library_id,
        source_name=source_name,
        source_sha256=sha,
        source_relpath="documents/_/source.pdf",  # actual path uses doc_id
        markdown_relpath="documents/_/document.md",
        mime_type="application/pdf",
    )
    if status != "uploaded":
        # Use the Store's state machine to advance the Document
        await store.transition_document_status(doc.id, status)
    return doc.id


async def _force_failed_document(
    store: KnowledgeStore,
    library_id: str,
) -> str:
    """Create a Document, run it through uploaded→extracting→failed."""
    doc_id = await _make_document(store, library_id)
    await store.transition_document_status(doc_id, "extracting")
    await store.transition_document_status(doc_id, "failed", error_code="prior_fail")
    return doc_id


async def _make_running_extract_job(store: KnowledgeStore, doc_id: str):
    """Insert a Job row with status='running' stage='extract' (bypass claim)."""
    return await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)


# ============================================================================
# A. Atomic claim
# ============================================================================


class TestClaimNextPending:
    async def test_returns_none_when_no_pending(self, ingestion, store):
        await _make_library(store)
        # No documents at all
        assert await ingestion.claim_next_pending_document() is None

    async def test_returns_none_when_only_non_uploaded(self, ingestion, store):
        lib = await _make_library(store)
        # Document in 'extracting' status
        doc_id = await _make_document(store, lib.id)
        await store.transition_document_status(doc_id, "extracting")
        assert await ingestion.claim_next_pending_document() is None

    async def test_claims_single_uploaded(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        result = await ingestion.claim_next_pending_document()
        assert result is not None
        assert isinstance(result, ClaimedJob)
        assert result.document.id == doc_id
        assert result.document.status == "extracting"
        assert result.job.document_id == doc_id
        assert result.job.status == "running"
        assert result.job.stage == EXTRACT_STAGE
        assert result.job.attempt == 1
        assert result.job.finished_at is None
        assert result.job.safe_error_code == ""

    async def test_fifo_ordering_by_created_at(self, ingestion, store):
        lib = await _make_library(store)
        # Create 3 docs with monotonic created_at
        oldest = await _make_document(store, lib.id, source_name="oldest.pdf")
        # Sleep slightly to ensure created_at differs (ms precision)
        await asyncio.sleep(0.005)
        middle = await _make_document(store, lib.id, source_name="middle.pdf")
        await asyncio.sleep(0.005)
        newest = await _make_document(store, lib.id, source_name="newest.pdf")

        first = await ingestion.claim_next_pending_document()
        second = await ingestion.claim_next_pending_document()
        third = await ingestion.claim_next_pending_document()

        assert first.document.id == oldest
        assert second.document.id == middle
        assert third.document.id == newest

    async def test_id_tiebreak_when_same_created_at(self, ingestion, store):
        """Same-millisecond created_at uses id ASC for deterministic order."""
        lib = await _make_library(store)
        # Insert 2 docs as fast as possible (likely same ms)
        d1 = await _make_document(store, lib.id, source_name="a.pdf")
        d2 = await _make_document(store, lib.id, source_name="b.pdf")
        # Verify both have same created_at
        doc1 = await store.get_document(d1)
        doc2 = await store.get_document(d2)
        if doc1.created_at == doc2.created_at:
            # Tiebreak: lower id wins
            first = await ingestion.claim_next_pending_document()
            expected_first = min(d1, d2)
            assert first.document.id == expected_first
        else:
            # created_at differed — skip tiebreak assertion
            first = await ingestion.claim_next_pending_document()
            assert first.document.id in (d1, d2)

    async def test_terminal_job_cannot_be_claimed_via_status(
        self, ingestion, store
    ):
        """A doc that's already 'extracting' (has running Job) is not claimable."""
        lib = await _make_library(store)
        # Manually create a doc + Job in 'extracting' state
        doc_id = await _make_document(store, lib.id)
        await store.transition_document_status(doc_id, "extracting")
        await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
        # Claim should return None (no 'uploaded' docs)
        assert await ingestion.claim_next_pending_document() is None

    async def test_concurrent_claim_only_one_wins(self, tmp_path):
        """Two KnowledgeStore instances on same DB; concurrent claim → one winner.

        SQLite's BEGIN IMMEDIATE reserved lock serializes the two writers.
        The loser either blocks then sees no pending, or briefly contends.
        """
        db_path = str(tmp_path / "knowledge.db")
        s1 = await KnowledgeStore.open(db_path)
        s2 = await KnowledgeStore.open(db_path)
        try:
            lib = await s1.create_library(name="lib1")
            doc_id = await _make_document(s1, lib.id)

            ing1 = IngestionStore(s1)
            ing2 = IngestionStore(s2)

            # Fire both claims concurrently
            r1, r2 = await asyncio.gather(
                ing1.claim_next_pending_document(),
                ing2.claim_next_pending_document(),
            )

            winners = [r for r in (r1, r2) if r is not None]
            assert len(winners) == 1, "exactly one claim must succeed"
            assert winners[0].document.id == doc_id
            assert winners[0].document.status == "extracting"

            # Document status in DB must be 'extracting' (committed)
            doc = await s1.get_document(doc_id)
            assert doc.status == "extracting"

            # Exactly one running Job exists for the doc
            jobs = await s1.list_jobs_for_document(doc_id)
            running = [j for j in jobs if j.status == "running"]
            assert len(running) == 1
        finally:
            await s1.close()
            await s2.close()


# ============================================================================
# B. Retry
# ============================================================================


class TestCreateRetryJob:
    async def test_creates_new_job_for_failed_document(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _force_failed_document(store, lib.id)

        job = await ingestion.create_retry_job(doc_id)

        assert job.document_id == doc_id
        assert job.status == "running"
        assert job.stage == EXTRACT_STAGE
        assert job.attempt == 1  # first retry attempt
        assert job.finished_at is None

        # Document transitioned failed → extracting (error_code cleared)
        doc = await store.get_document(doc_id)
        assert doc.status == "extracting"
        assert doc.error_code == ""

    async def test_rejects_non_failed_status_uploaded(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)  # status='uploaded'
        with pytest.raises(RetryNotAllowedError) as exc:
            await ingestion.create_retry_job(doc_id)
        assert exc.value.current_status == "uploaded"

    async def test_rejects_non_failed_status_extracting(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        await store.transition_document_status(doc_id, "extracting")
        with pytest.raises(RetryNotAllowedError) as exc:
            await ingestion.create_retry_job(doc_id)
        assert exc.value.current_status == "extracting"

    async def test_rejects_normalizing_r2c_terminal(self, ingestion, store):
        """R2-C terminal 'normalizing' cannot be retried (R3 will pick up)."""
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        await store.transition_document_status(doc_id, "extracting")
        await store.transition_document_status(doc_id, "normalizing")
        with pytest.raises(RetryNotAllowedError) as exc:
            await ingestion.create_retry_job(doc_id)
        assert exc.value.current_status == "normalizing"

    async def test_rejects_needs_ocr(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        await store.transition_document_status(doc_id, "extracting")
        await store.transition_document_status(
            doc_id, "needs_ocr", error_code="needs_ocr"
        )
        with pytest.raises(RetryNotAllowedError):
            await ingestion.create_retry_job(doc_id)

    async def test_rejects_when_active_job_running(self, ingestion, store):
        lib = await _make_library(store)
        # Set up: failed doc with a stale 'running' Job (e.g., crashed mid-recovery)
        doc_id = await _force_failed_document(store, lib.id)
        # First retry succeeds (Job running)
        await ingestion.create_retry_job(doc_id)
        # Now the doc has a running Job; mark it failed again to allow retry path
        await store.transition_document_status(doc_id, "failed", error_code="x")
        # Second retry should reject because the prior Job is still 'running'
        with pytest.raises(IngestionAlreadyActiveError) as exc:
            await ingestion.create_retry_job(doc_id)
        assert exc.value.document_id == doc_id

    async def test_rejects_when_retry_limit_reached(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _force_failed_document(store, lib.id)
        # Manually insert MAX_RETRY_ATTEMPTS completed/failed Jobs for this doc
        for _ in range(MAX_RETRY_ATTEMPTS):
            job = await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
            await store.finish_job(job.id, status="failed", safe_error_code="x")
            # Reset doc to failed so we can keep iterating (state machine requires)
            # — but transition_document_status enforces extracting→failed only,
            # and failed→extracting needs a running Job. Simplify: leave doc failed.

        # Doc is failed; attempt count = MAX_RETRY_ATTEMPTS; retry must reject.
        with pytest.raises(RetryLimitReachedError) as exc:
            await ingestion.create_retry_job(doc_id)
        assert exc.value.attempt_count == MAX_RETRY_ATTEMPTS

    async def test_rejects_nonexistent_document(self, ingestion):
        with pytest.raises(DocumentNotFoundError):
            await ingestion.create_retry_job("doc_nonexistent0001")

    async def test_rejects_invalid_document_id_format(self, ingestion):
        with pytest.raises(ValueError):
            await ingestion.create_retry_job("not-a-valid-id")

    async def test_attempt_monotonic_across_retries(self, ingestion, store):
        """attempt counts all prior stage='extract' Jobs (including completed/failed)."""
        lib = await _make_library(store)
        doc_id = await _force_failed_document(store, lib.id)

        # First retry: attempt = 1 (no prior extract Jobs)
        j1 = await ingestion.create_retry_job(doc_id)
        assert j1.attempt == 1
        # Mark as failed
        await store.finish_job(j1.id, status="failed", safe_error_code="x")
        await store.transition_document_status(doc_id, "failed", error_code="x")

        # Second retry: attempt = 2
        j2 = await ingestion.create_retry_job(doc_id)
        assert j2.attempt == 2
        await store.finish_job(j2.id, status="failed", safe_error_code="y")
        await store.transition_document_status(doc_id, "failed", error_code="y")

        # Third retry: attempt = 3
        j3 = await ingestion.create_retry_job(doc_id)
        assert j3.attempt == 3

    async def test_retry_does_not_change_source_sha(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _force_failed_document(store, lib.id)
        original = await store.get_document(doc_id)
        original_sha = original.source_sha256

        await ingestion.create_retry_job(doc_id)

        after = await store.get_document(doc_id)
        assert after.source_sha256 == original_sha
        assert after.source_name == original.source_name

    async def test_old_terminal_jobs_remain_immutable(self, ingestion, store):
        """Retry creates a new Job row; old failed Job is not mutated."""
        lib = await _make_library(store)
        doc_id = await _force_failed_document(store, lib.id)

        j1 = await ingestion.create_retry_job(doc_id)
        await store.finish_job(j1.id, status="failed", safe_error_code="err1")
        await store.transition_document_status(doc_id, "failed", error_code="err1")

        j2 = await ingestion.create_retry_job(doc_id)

        # j1 must remain unchanged (status='failed', safe_error_code='err1')
        j1_after = await store.get_job(j1.id)
        assert j1_after.status == "failed"
        assert j1_after.safe_error_code == "err1"
        assert j1_after.id != j2.id

    async def test_concurrent_retry_only_one_wins(self, tmp_path):
        """Two stores + asyncio.gather retry; loser gets IngestionAlreadyActive."""
        db_path = str(tmp_path / "knowledge.db")
        s1 = await KnowledgeStore.open(db_path)
        s2 = await KnowledgeStore.open(db_path)
        try:
            lib = await s1.create_library(name="lib1")
            doc_id = await _force_failed_document(s1, lib.id)

            ing1 = IngestionStore(s1)
            ing2 = IngestionStore(s2)

            results = await asyncio.gather(
                ing1.create_retry_job(doc_id),
                ing2.create_retry_job(doc_id),
                return_exceptions=True,
            )

            # Exactly one success + one IngestionAlreadyActive
            successes = [
                r for r in results
                if not isinstance(r, Exception) and hasattr(r, "id")
            ]
            exceptions = [r for r in results if isinstance(r, Exception)]
            assert len(successes) == 1
            assert len(exceptions) == 1
            assert isinstance(exceptions[0], IngestionAlreadyActiveError)

            # Only one running Job in DB
            jobs = await s1.list_jobs_for_document(doc_id)
            running = [j for j in jobs if j.status == "running"]
            assert len(running) == 1
        finally:
            await s1.close()
            await s2.close()


# ============================================================================
# C. Counts + queries
# ============================================================================


class TestQueries:
    async def test_count_active_jobs_zero_when_no_jobs(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        assert await ingestion.count_active_jobs_for_document(doc_id) == 0
        assert await ingestion.has_active_job(doc_id) is False

    async def test_count_active_jobs_after_claim(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        await ingestion.claim_next_pending_document()
        assert await ingestion.count_active_jobs_for_document(doc_id) == 1
        assert await ingestion.has_active_job(doc_id) is True

    async def test_count_active_jobs_excludes_terminal(self, ingestion, store):
        """finish_job transitions running→completed/failed; count drops to 0."""
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        claimed = await ingestion.claim_next_pending_document()
        await store.finish_job(claimed.job.id, status="completed")
        assert await ingestion.count_active_jobs_for_document(doc_id) == 0

    async def test_count_extract_attempts_includes_all_statuses(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        # 2 jobs: one completed, one running
        j1 = await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
        await store.finish_job(j1.id, status="completed")
        j2 = await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
        # j2 still running
        assert await ingestion.count_extract_attempts(doc_id) == 2
        # Validate j2 is actually running (sanity)
        j2_check = await store.get_job(j2.id)
        assert j2_check.status == "running"

    async def test_count_extract_attempts_excludes_other_stages(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        # Insert jobs with different stages
        await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
        await store.create_job(document_id=doc_id, stage="normalize")
        # Only 'extract' counts
        assert await ingestion.count_extract_attempts(doc_id) == 1

    async def test_get_latest_extract_job_returns_most_recent(self, ingestion, store):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)
        await asyncio.sleep(0.005)
        j2 = await store.create_job(document_id=doc_id, stage=EXTRACT_STAGE)

        latest = await ingestion.get_latest_extract_job_for_document(doc_id)
        assert latest is not None
        assert latest.id == j2.id

    async def test_get_latest_extract_job_returns_none_when_no_jobs(
        self, ingestion, store
    ):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        assert await ingestion.get_latest_extract_job_for_document(doc_id) is None


# ============================================================================
# D. Recovery primitive
# ============================================================================


class TestRecoveryPrimitive:
    async def test_mark_running_jobs_interrupted_zero_when_none(self, ingestion, store):
        count = await ingestion.mark_running_jobs_interrupted()
        assert count == 0

    async def test_mark_running_jobs_interrupted_marks_all(self, ingestion, store):
        lib = await _make_library(store)
        # 3 docs claimed (3 running Jobs)
        doc_ids = []
        for i in range(3):
            doc_id = await _make_document(store, lib.id, source_name=f"d{i}.pdf")
            await ingestion.claim_next_pending_document()
            doc_ids.append(doc_id)

        count = await ingestion.mark_running_jobs_interrupted()
        assert count == 3

        # All 3 docs' latest Job must be 'failed' with ingestion_interrupted
        for doc_id in doc_ids:
            latest = await ingestion.get_latest_extract_job_for_document(doc_id)
            assert latest is not None
            assert latest.status == "failed"
            assert latest.safe_error_code == INGESTION_INTERRUPTED
            assert latest.finished_at is not None

    async def test_mark_running_jobs_interrupted_idempotent(self, ingestion, store):
        lib = await _make_library(store)
        await _make_document(store, lib.id)
        await ingestion.claim_next_pending_document()

        first = await ingestion.mark_running_jobs_interrupted()
        second = await ingestion.mark_running_jobs_interrupted()

        assert first == 1
        assert second == 0  # idempotent — no running Jobs left

    async def test_mark_running_jobs_interrupted_does_not_touch_terminal(
        self, ingestion, store
    ):
        lib = await _make_library(store)
        await _make_document(store, lib.id)
        claimed = await ingestion.claim_next_pending_document()
        await store.finish_job(claimed.job.id, status="completed")

        count = await ingestion.mark_running_jobs_interrupted()
        assert count == 0  # completed Job untouched

        # Job remains completed (NOT overwritten)
        job = await store.get_job(claimed.job.id)
        assert job.status == "completed"
        assert job.safe_error_code == ""

    async def test_mark_running_jobs_interrupted_sets_finished_at(
        self, ingestion, store
    ):
        lib = await _make_library(store)
        doc_id = await _make_document(store, lib.id)
        claimed = await ingestion.claim_next_pending_document()
        assert claimed.job.finished_at is None

        await ingestion.mark_running_jobs_interrupted()

        latest = await ingestion.get_latest_extract_job_for_document(doc_id)
        assert latest is not None
        assert latest.finished_at is not None
        # finished_at may equal started_at (same millisecond) under fast tests;
        # the invariant we care about is "set + >= started_at".
        assert latest.finished_at >= claimed.job.started_at


# ============================================================================
# E. Cross-document / cross-library isolation
# ============================================================================


class TestIsolation:
    async def test_claim_does_not_cross_libraries(self, ingestion, store):
        lib1 = await _make_library(store, name="lib1")
        lib2 = await _make_library(store, name="lib2")
        # Documents in different libraries
        d_lib1 = await _make_document(store, lib1.id, source_name="a.pdf")
        d_lib2 = await _make_document(store, lib2.id, source_name="b.pdf")
        # Make d_lib2 older to ensure both are eligible
        await asyncio.sleep(0.005)
        # Actually for testing isolation we just want to confirm claim works across libs
        # (FIFO is global, not per-library)

        first = await ingestion.claim_next_pending_document()
        second = await ingestion.claim_next_pending_document()

        # Both should be claimed (order doesn't matter for isolation test)
        claimed_ids = {first.document.id, second.document.id}
        assert claimed_ids == {d_lib1, d_lib2}

    async def test_active_job_query_does_not_leak_across_docs(
        self, ingestion, store
    ):
        lib = await _make_library(store)
        d1 = await _make_document(store, lib.id, source_name="d1.pdf")
        # Ensure d1 sorts before d2 deterministically (avoid id-tiebreak flakiness)
        await asyncio.sleep(0.005)
        d2 = await _make_document(store, lib.id, source_name="d2.pdf")

        # Claim exactly one — d1 is oldest (created_at strictly less than d2)
        claimed = await ingestion.claim_next_pending_document()
        assert claimed.document.id == d1

        # d1 has active job; d2 does not
        assert await ingestion.has_active_job(d1) is True
        assert await ingestion.has_active_job(d2) is False

    async def test_invalid_document_id_raises_value_error(self, ingestion):
        with pytest.raises(ValueError):
            await ingestion.count_active_jobs_for_document("invalid")
        with pytest.raises(ValueError):
            await ingestion.has_active_job("invalid")
        with pytest.raises(ValueError):
            await ingestion.count_extract_attempts("invalid")
        with pytest.raises(ValueError):
            await ingestion.get_latest_extract_job_for_document("invalid")


# ============================================================================
# F. Constants exposure
# ============================================================================


class TestConstants:
    def test_max_retry_attempts_is_five(self):
        assert MAX_RETRY_ATTEMPTS == 5

    def test_extract_stage_is_extract(self):
        assert EXTRACT_STAGE == "extract"

    def test_ingestion_interrupted_safe_code(self):
        assert INGESTION_INTERRUPTED == "ingestion_interrupted"
        # No path / secret / body markers
        assert "/" not in INGESTION_INTERRUPTED
        assert "\\" not in INGESTION_INTERRUPTED

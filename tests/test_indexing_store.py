"""P2-R3-C1 — IndexingStore unit tests.

Covers directive §50 matrix (30 items): atomic claim, state transitions,
concurrent claim, recovery primitive (idempotency + per-status behaviour).
Uses real KnowledgeStore + IndexingStore wiring on a temp SQLite DB.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.indexing_store import (
    INDEXING_INTERRUPTED,
    DocumentNotClaimableError,
    IndexingStore,
    InvalidStateTransitionError,
    RecoveryResult,
)
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Helpers
# ============================================================================


async def _seed_doc(
    store: KnowledgeStore,
    *,
    document_id: str,
    library_id: str = "lib_test00000001",
    status: str = "normalizing",
    created_at: int = 100,
    sha_suffix: str = "1",
) -> None:
    """Insert a Library (if absent) + Document with the given status (direct SQL).

    Library is INSERT OR IGNORE so multiple documents in the same library
    can be seeded without raising the (library_id, source_sha256) UNIQUE.
    """
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
                "a" * 63 + sha_suffix,  # unique per doc
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf",
                10,
                1,
                status,
                "",
                "",
                created_at,
                created_at,
            ),
        )
        await store._db.execute("COMMIT")


async def _get_status(store: KnowledgeStore, document_id: str) -> str:
    async with store._db.execute(
        "SELECT status, error_code FROM knowledge_documents WHERE id = ?",
        (document_id,),
    ) as cursor:
        return await cursor.fetchone()


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
async def store_and_indexing_store(tmp_path: Path):
    store = await KnowledgeStore.open(str(tmp_path / "r3_c1.db"))
    indexing_store = IndexingStore(store)
    try:
        yield store, indexing_store
    finally:
        await store.close()


# ============================================================================
# 1. claim_document — explicit ID
# ============================================================================


class TestClaimDocument:
    async def test_claim_normalizing_succeeds(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001")
        d = await idx.claim_document("doc_test00000001")
        assert d is not None
        assert d.id == "doc_test00000001"
        assert d.status == "chunking"

    async def test_claim_non_normalizing_returns_none(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        # Seed as ready (not normalizing) → claim should return None.
        await _seed_doc(store, document_id="doc_test00000001", status="ready")
        d = await idx.claim_document("doc_test00000001")
        assert d is None

    async def test_claim_already_chunking_returns_none(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        d = await idx.claim_document("doc_test00000001")
        assert d is None

    async def test_claim_missing_document_returns_none(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        _store, idx = store_and_indexing_store
        d = await idx.claim_document("doc_test00000001")
        assert d is None

    async def test_claim_invalid_id_raises(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        _store, idx = store_and_indexing_store
        with pytest.raises(DocumentNotClaimableError, match="invalid document_id"):
            await idx.claim_document("bad")


# ============================================================================
# 2. claim_next_normalizing_document — deterministic ordering
# ============================================================================


class TestClaimNext:
    async def test_claim_next_picks_oldest(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        # Seed three normalizing documents with different created_at.
        await _seed_doc(
            store, document_id="doc_test00000003", created_at=300, sha_suffix="3"
        )
        await _seed_doc(
            store, document_id="doc_test00000001", created_at=100, sha_suffix="1"
        )
        await _seed_doc(
            store, document_id="doc_test00000002", created_at=200, sha_suffix="2"
        )
        d = await idx.claim_next_normalizing_document()
        assert d is not None
        assert d.id == "doc_test00000001"  # oldest by created_at
        assert d.status == "chunking"

    async def test_claim_next_tiebreak_by_id(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        # Same created_at → tiebreak by id ASC.
        await _seed_doc(
            store, document_id="doc_bbb000000001", created_at=100, sha_suffix="b"
        )
        await _seed_doc(
            store, document_id="doc_aaa000000001", created_at=100, sha_suffix="a"
        )
        d = await idx.claim_next_normalizing_document()
        assert d is not None
        assert d.id == "doc_aaa000000001"

    async def test_claim_next_empty_returns_none(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        _store, idx = store_and_indexing_store
        d = await idx.claim_next_normalizing_document()
        assert d is None

    async def test_claim_next_skips_ready(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="ready")
        d = await idx.claim_next_normalizing_document()
        assert d is None

    async def test_claim_next_skips_failed(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="failed")
        d = await idx.claim_next_normalizing_document()
        assert d is None

    async def test_claim_next_skips_needs_ocr(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="needs_ocr")
        d = await idx.claim_next_normalizing_document()
        assert d is None

    async def test_claim_next_skips_uploaded(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="uploaded")
        d = await idx.claim_next_normalizing_document()
        assert d is None

    async def test_claim_next_drains_multiple_in_order(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        for i, did in enumerate(["doc_aaa000000001", "doc_bbb000000001", "doc_ccc000000001"]):
            await _seed_doc(
                store, document_id=did, created_at=100 + i, sha_suffix=str(i)
            )
        order = []
        while True:
            d = await idx.claim_next_normalizing_document()
            if d is None:
                break
            order.append(d.id)
        assert order == ["doc_aaa000000001", "doc_bbb000000001", "doc_ccc000000001"]


# ============================================================================
# 3. State transitions — mark_indexing / mark_ready / mark_failed
# ============================================================================


class TestStateTransitions:
    async def test_mark_indexing_chunking_only(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        await idx.mark_indexing("doc_test00000001")
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "indexing"

    async def test_mark_indexing_rejects_normalizing(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="normalizing")
        with pytest.raises(InvalidStateTransitionError, match="not in 'chunking'"):
            await idx.mark_indexing("doc_test00000001")

    async def test_mark_indexing_rejects_ready(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="ready")
        with pytest.raises(InvalidStateTransitionError):
            await idx.mark_indexing("doc_test00000001")

    async def test_mark_ready_indexing_only(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="indexing")
        await idx.mark_ready("doc_test00000001")
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "ready"

    async def test_mark_ready_rejects_chunking(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        with pytest.raises(InvalidStateTransitionError, match="not in 'indexing'"):
            await idx.mark_ready("doc_test00000001")

    async def test_mark_failed_from_chunking(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        await idx.mark_failed(
            "doc_test00000001", error_code="chunk_persistence_failed"
        )
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "failed"
        assert row["error_code"] == "chunk_persistence_failed"

    async def test_mark_failed_from_indexing(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="indexing")
        await idx.mark_failed(
            "doc_test00000001", error_code="fts_integrity_error"
        )
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "failed"
        assert row["error_code"] == "fts_integrity_error"

    async def test_mark_failed_rejects_normalizing(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="normalizing")
        with pytest.raises(InvalidStateTransitionError):
            await idx.mark_failed("doc_test00000001", error_code="x")

    async def test_mark_failed_rejects_ready(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="ready")
        with pytest.raises(InvalidStateTransitionError):
            await idx.mark_failed("doc_test00000001", error_code="x")

    async def test_mark_failed_empty_error_code_rejected(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        with pytest.raises(Exception, match="error_code"):
            await idx.mark_failed("doc_test00000001", error_code="")


# ============================================================================
# 4. Concurrent claim — single SQLite connection race
# ============================================================================


class TestConcurrentClaim:
    async def test_two_async_tasks_one_winner(
        self,
        store_and_indexing_store: tuple[KnowledgeStore, IndexingStore],
    ):
        """Two concurrent claim_document coroutines on the same Document.

        The ``_write_lock`` serialises them; exactly one should win, the
        other should see ``None``. No raw SQLite IntegrityError leaks.
        """
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001")
        # Run two claim coroutines "concurrently" — _write_lock serialises.
        d_a, d_b = await asyncio.gather(
            idx.claim_document("doc_test00000001"),
            idx.claim_document("doc_test00000001"),
        )
        winners = [d for d in (d_a, d_b) if d is not None]
        assert len(winners) == 1
        assert winners[0].status == "chunking"

    async def test_two_async_tasks_different_documents(
        self,
        store_and_indexing_store: tuple[KnowledgeStore, IndexingStore],
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(
            store, document_id="doc_aaa000000001", created_at=100, sha_suffix="a"
        )
        await _seed_doc(
            store, document_id="doc_bbb000000001", created_at=200, sha_suffix="b"
        )
        d_a, d_b = await asyncio.gather(
            idx.claim_document("doc_aaa000000001"),
            idx.claim_document("doc_bbb000000001"),
        )
        assert d_a is not None and d_a.id == "doc_aaa000000001"
        assert d_b is not None and d_b.id == "doc_bbb000000001"


# ============================================================================
# 5. Recovery primitive
# ============================================================================


class TestRecovery:
    async def test_recover_chunking(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        result = await idx.recover_interrupted_indexing()
        assert isinstance(result, RecoveryResult)
        assert result.chunking_recovered == 1
        assert result.indexing_recovered == 0
        assert result.total_recovered == 1
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "failed"
        assert row["error_code"] == INDEXING_INTERRUPTED

    async def test_recover_indexing(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="indexing")
        result = await idx.recover_interrupted_indexing()
        assert result.indexing_recovered == 1
        assert result.chunking_recovered == 0
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "failed"

    async def test_recover_normalizing_unchanged(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="normalizing")
        result = await idx.recover_interrupted_indexing()
        assert result.total_recovered == 0
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "normalizing"

    async def test_recover_ready_unchanged(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="ready")
        result = await idx.recover_interrupted_indexing()
        assert result.total_recovered == 0
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "ready"

    async def test_recover_failed_unchanged(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(
            store,
            document_id="doc_test00000001",
            status="failed",
        )
        # Set an existing error_code to confirm recovery does not overwrite
        # (failed is terminal; recovery only touches chunking/indexing).
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET error_code = 'prior_failure' "
                "WHERE id = ?",
                ("doc_test00000001",),
            )
            await store._db.execute("COMMIT")
        result = await idx.recover_interrupted_indexing()
        assert result.total_recovered == 0
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "failed"
        assert row["error_code"] == "prior_failure"

    async def test_recover_needs_ocr_unchanged(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="needs_ocr")
        result = await idx.recover_interrupted_indexing()
        assert result.total_recovered == 0
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "needs_ocr"

    async def test_recover_idempotent(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        first = await idx.recover_interrupted_indexing()
        second = await idx.recover_interrupted_indexing()
        assert first.total_recovered == 1
        assert second.total_recovered == 0

    async def test_recover_two_documents(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(
            store,
            document_id="doc_aaa000000001",
            status="chunking",
            created_at=100,
            sha_suffix="a",
        )
        await _seed_doc(
            store,
            document_id="doc_bbb000000001",
            status="indexing",
            created_at=200,
            sha_suffix="b",
        )
        result = await idx.recover_interrupted_indexing()
        assert result.chunking_recovered == 1
        assert result.indexing_recovered == 1
        assert result.total_recovered == 2

    async def test_recover_with_cleanup_callback(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        """Cleanup callback is invoked once per recovered Document before
        the state transition commits. If it raises, the whole txn rolls back.
        """
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        called_with: list[str] = []

        async def cleanup(doc_id: str) -> None:
            called_with.append(doc_id)

        result = await idx.recover_interrupted_indexing(cleanup_callback=cleanup)
        assert result.total_recovered == 1
        assert called_with == ["doc_test00000001"]

    async def test_recover_cleanup_failure_rolls_back(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")

        async def boom(_doc_id: str) -> None:
            raise RuntimeError("simulated cleanup failure")

        with pytest.raises(RuntimeError, match="simulated"):
            await idx.recover_interrupted_indexing(cleanup_callback=boom)
        # Document should remain in chunking (recovery rolled back).
        row = await _get_status(store, "doc_test00000001")
        assert row["status"] == "chunking"

    async def test_recover_sync_callback_supported(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        """Sync callbacks (no await) are also supported via duck-typing."""
        store, idx = store_and_indexing_store
        await _seed_doc(store, document_id="doc_test00000001", status="chunking")
        called_with: list[str] = []

        def cleanup(doc_id: str) -> None:
            called_with.append(doc_id)

        result = await idx.recover_interrupted_indexing(cleanup_callback=cleanup)
        assert result.total_recovered == 1
        assert called_with == ["doc_test00000001"]


# ============================================================================
# 6. Error safety — no raw SQLite leak
# ============================================================================


class TestErrorSafety:
    async def test_invalid_id_format_raises_safe_error(
        self, store_and_indexing_store: tuple[KnowledgeStore, IndexingStore]
    ):
        _store, idx = store_and_indexing_store
        with pytest.raises(DocumentNotClaimableError) as exc_info:
            await idx.claim_document("bad_id")
        # Message must not contain SQL / traceback / absolute path.
        msg = str(exc_info.value)
        assert "SELECT" not in msg
        assert "UPDATE" not in msg
        assert "knowledge_" not in msg
        assert "D:\\" not in msg and "/home/" not in msg

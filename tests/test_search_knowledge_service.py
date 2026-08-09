"""P2-R4-B1 — SearchKnowledgeService unit tests.

Covers directive §78 items 1-22 (Service test matrix) + §51 (empty ACL
escalation hard test).

Uses real KnowledgeStore + ChunkStore + IndexingStore wiring on a temp
DB. Seeds ready Documents with chunks/FTS + session bindings.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore
from pi_agent_core_py.web.knowledge.evidence import EvidenceRegistry
from pi_agent_core_py.web.knowledge.search_models import (
    DEFAULT_TOOL_LIMIT,
    MAX_TOOL_LIMIT,
    KnowledgeSearchError,
)
from pi_agent_core_py.web.knowledge.search_service import SearchKnowledgeService
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Helpers
# ============================================================================


async def _seed_library_doc_chunks(
    store: KnowledgeStore,
    chunk_store: ChunkStore,
    *,
    library_id: str,
    document_id: str,
    source_name: str,
    chunk_content: str,
    sha_suffix: str = "1",
) -> None:
    """Seed a Library + ready Document with one chunk + FTS row."""
    async with store._write_lock:
        await store._db.execute("BEGIN IMMEDIATE")
        await store._db.execute(
            "INSERT OR IGNORE INTO knowledge_libraries "
            "(id, name, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, library_id, "", "active", 1, 1),
        )
        await store._db.execute(
            "INSERT OR REPLACE INTO knowledge_documents "
            "(id, library_id, source_name, source_sha256, source_relpath, "
            " markdown_relpath, mime_type, size_bytes, page_count, status, "
            " parser_version, error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', '', '', 100, 100)",
            (
                document_id, library_id, source_name,
                "a" * 63 + sha_suffix,
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf", 100, 1,
            ),
        )
        await store._db.execute("COMMIT")
    # Insert chunk + FTS via direct SQL (bypass ChunkStore validation
    # which requires valid KnowledgeChunk DTO — for tests we just need
    # searchable rows).
    import hashlib
    content_sha = hashlib.sha256(chunk_content.encode("utf-8")).hexdigest()
    chunk_id = "chunk_" + sha_suffix.rjust(12, "0")
    async with store._write_lock:
        await store._db.execute("BEGIN IMMEDIATE")
        await store._db.execute(
            "INSERT INTO knowledge_chunks (id, library_id, document_id, ordinal, "
            " heading_path, page_start, page_end, content, content_hash, "
            " char_count, content_sha256, token_count, created_at) "
            "VALUES (?, ?, ?, 0, ?, 1, 1, ?, ?, ?, ?, 0, 1)",
            (
                chunk_id, library_id, document_id, '["Heading"]',
                chunk_content, content_sha, len(chunk_content), content_sha,
            ),
        )
        await store._db.execute(
            "INSERT INTO knowledge_chunks_fts (chunk_id, document_id, "
            " library_id, heading_text, content) VALUES (?, ?, ?, ?, ?)",
            (chunk_id, document_id, library_id, "Heading", chunk_content),
        )
        await store._db.execute("COMMIT")


async def _bind_session(
    store: KnowledgeStore,
    *,
    session_id: str,
    library_ids: list[str],
) -> None:
    async with store._write_lock:
        await store._db.execute("BEGIN IMMEDIATE")
        await store._db.execute(
            "DELETE FROM session_knowledge_libraries WHERE session_id = ?",
            (session_id,),
        )
        for lid in library_ids:
            await store._db.execute(
                "INSERT INTO session_knowledge_libraries "
                "(session_id, library_id, access_mode, created_at) "
                "VALUES (?, ?, 'read', 1)",
                (session_id, lid),
            )
        await store._db.execute("COMMIT")


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
async def search_setup(tmp_path: Path):
    store = await KnowledgeStore.open(str(tmp_path / "r4_b1.db"))
    chunk_store = ChunkStore(store)
    service = SearchKnowledgeService(
        knowledge_store=store, chunk_store=chunk_store,
    )
    try:
        yield store, chunk_store, service
    finally:
        await store.close()


# ============================================================================
# 1. Query / Limit validation
# ============================================================================


class TestQueryValidation:
    async def test_empty_query_raises(
        self, search_setup
    ):
        _s, _cs, svc = search_setup
        r = EvidenceRegistry()
        with pytest.raises(KnowledgeSearchError) as exc_info:
            await svc.search(
                session_id="sess_test0001", query="", registry=r,
            )
        assert exc_info.value.code == "knowledge_search_invalid_query"

    async def test_whitespace_query_raises(
        self, search_setup
    ):
        _s, _cs, svc = search_setup
        r = EvidenceRegistry()
        with pytest.raises(KnowledgeSearchError) as exc_info:
            await svc.search(
                session_id="sess_test0001", query="   ", registry=r,
            )
        assert exc_info.value.code == "knowledge_search_invalid_query"

    async def test_limit_zero_raises(self, search_setup):
        _s, _cs, svc = search_setup
        r = EvidenceRegistry()
        with pytest.raises(KnowledgeSearchError) as exc_info:
            await svc.search(
                session_id="sess_test0001", query="x",
                limit=0, registry=r,
            )
        assert exc_info.value.code == "knowledge_search_invalid_query"

    async def test_limit_over_max_raises(self, search_setup):
        _s, _cs, svc = search_setup
        r = EvidenceRegistry()
        with pytest.raises(KnowledgeSearchError) as exc_info:
            await svc.search(
                session_id="sess_test0001", query="x",
                limit=MAX_TOOL_LIMIT + 1, registry=r,
            )
        assert exc_info.value.code == "knowledge_search_invalid_query"


# ============================================================================
# 2. Empty binding hard gate
# ============================================================================


class TestEmptyBinding:
    async def test_empty_binding_returns_zero_hits(
        self, search_setup
    ):
        store, _cs, svc = search_setup
        # Seed a doc but don't bind session to its library.
        await _seed_library_doc_chunks(
            store, _cs,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy secret",
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001",
            query="radiotherapy",
            registry=r,
        )
        assert result.hits == ()

    async def test_empty_binding_does_not_call_chunkstore(
        self, search_setup, monkeypatch
    ):
        """Hard Gate: ChunkStore.search_chunks_fts must NOT be called
        when session has no bound libraries."""
        store, chunk_store, svc = search_setup
        call_count = [0]
        original = chunk_store.search_chunks_fts

        async def counting_search(*args, **kwargs):
            call_count[0] += 1
            return await original(*args, **kwargs)

        chunk_store.search_chunks_fts = counting_search

        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001",
            query="anything",
            registry=r,
        )
        assert result.hits == ()
        assert call_count[0] == 0, (
            "ChunkStore was called despite empty binding — security violation"
        )

    async def test_empty_binding_never_passes_none(
        self, search_setup, monkeypatch
    ):
        """library_ids=None must NEVER reach ChunkStore from production."""
        store, chunk_store, svc = search_setup
        passed_library_ids = []
        original = chunk_store.search_chunks_fts

        async def spy_search(query, *, library_ids=None, limit=5):
            passed_library_ids.append(library_ids)
            return await original(query, library_ids=library_ids, limit=limit)

        chunk_store.search_chunks_fts = spy_search

        # With a binding, it should pass a list (not None).
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy",
        )
        await _bind_session(store, session_id="sess_test0001",
                            library_ids=["lib_aaaaaaaaaaaa"])

        r = EvidenceRegistry()
        await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert len(passed_library_ids) > 0
        for ids in passed_library_ids:
            assert ids is not None, "library_ids=None passed to ChunkStore"


# ============================================================================
# 3. ACL: one library, cross-library isolation
# ============================================================================


class TestACL:
    async def test_one_bound_library_returns_hits(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy dose planning",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert len(result.hits) >= 1
        assert result.hits[0].evidence_id == "E1"
        assert result.hits[0].source_filename == "a.pdf"
        assert "radiotherapy" in result.hits[0].content

    async def test_cross_library_isolation(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="public apple radiotherapy",
        )
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_bbbbbbbbbbbb",
            document_id="doc_bbbbbbbbbbbb01",
            source_name="b.pdf",
            chunk_content="secret banana classified",
            sha_suffix="2",
        )
        # Session bound to A only — search for "banana" should return 0.
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="banana", registry=r,
        )
        assert result.hits == ()

    async def test_cross_session_isolation(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="alpha",
        )
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_bbbbbbbbbbbb",
            document_id="doc_bbbbbbbbbbbb01",
            source_name="b.pdf",
            chunk_content="beta",
            sha_suffix="2",
        )
        await _bind_session(
            store, session_id="sess_aaaaaaaaaa01",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        await _bind_session(
            store, session_id="sess_bbbbbbbbbb01",
            library_ids=["lib_bbbbbbbbbbbb"],
        )
        # sess_A searches "beta" → 0 hits (B not bound)
        r_a = EvidenceRegistry()
        result_a = await svc.search(
            session_id="sess_aaaaaaaaaa01", query="beta", registry=r_a,
        )
        assert result_a.hits == ()
        # sess_B searches "beta" → hit
        r_b = EvidenceRegistry()
        result_b = await svc.search(
            session_id="sess_bbbbbbbbbb01", query="beta", registry=r_b,
        )
        assert len(result_b.hits) >= 1

    async def test_unbind_takes_effect(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r1 = EvidenceRegistry()
        result1 = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r1,
        )
        assert len(result1.hits) >= 1
        # Unbind (replace with empty).
        await _bind_session(
            store, session_id="sess_test0001", library_ids=[],
        )
        r2 = EvidenceRegistry()
        result2 = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r2,
        )
        assert result2.hits == ()

    async def test_multiple_bound_libraries(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy alpha",
        )
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_bbbbbbbbbbbb",
            document_id="doc_bbbbbbbbbbbb01",
            source_name="b.pdf",
            chunk_content="radiotherapy beta",
            sha_suffix="2",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa", "lib_bbbbbbbbbbbb"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert len(result.hits) >= 2


# ============================================================================
# 4. Evidence Registry integration
# ============================================================================


class TestEvidenceIntegration:
    async def test_evidence_ids_assigned(self, search_setup):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="test.pdf",
            chunk_content="radiotherapy planning dose",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert len(result.hits) >= 1
        assert result.hits[0].evidence_id == "E1"
        # Registry also has it.
        assert r.lookup("E1") is not None
        assert r.lookup("E1").chunk_id == result.hits[0].chunk_id

    async def test_same_query_different_registries(
        self, search_setup
    ):
        """Two separate registries → both start from E1."""
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="test.pdf",
            chunk_content="radiotherapy",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r1 = EvidenceRegistry()
        result1 = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r1,
        )
        r2 = EvidenceRegistry()
        result2 = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r2,
        )
        assert result1.hits[0].evidence_id == "E1"
        assert result2.hits[0].evidence_id == "E1"

    async def test_source_filename_from_source_name(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="my_report.pdf",
            chunk_content="radiotherapy",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert result.hits[0].source_filename == "my_report.pdf"

    async def test_no_results_returns_empty_not_error(
        self, search_setup
    ):
        store, chunk_store, svc = search_setup
        await _seed_library_doc_chunks(
            store, chunk_store,
            library_id="lib_aaaaaaaaaaaa",
            document_id="doc_aaaaaaaaaaaa01",
            source_name="a.pdf",
            chunk_content="radiotherapy",
        )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001",
            query="nonexistent_xyzzy_99999",
            registry=r,
        )
        assert result.hits == ()
        assert len(r) == 0


# ============================================================================
# 5. Default limit
# ============================================================================


class TestLimit:
    async def test_default_limit_is_5(self, search_setup):
        store, chunk_store, svc = search_setup
        # Seed 6 chunks with same term.
        for i in range(6):
            await _seed_library_doc_chunks(
                store, chunk_store,
                library_id="lib_aaaaaaaaaaaa",
                document_id=f"doc_aaaaaaaaaa{i:02d}",
                source_name=f"doc_{i}.pdf",
                chunk_content=f"radiotherapy content {i}",
                sha_suffix=str(i + 1),
            )
        await _bind_session(
            store, session_id="sess_test0001",
            library_ids=["lib_aaaaaaaaaaaa"],
        )
        r = EvidenceRegistry()
        result = await svc.search(
            session_id="sess_test0001", query="radiotherapy", registry=r,
        )
        assert len(result.hits) == DEFAULT_TOOL_LIMIT

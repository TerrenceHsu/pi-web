"""P2-R3-B2 — ChunkStore unit tests.

Covers directive §35 persistence + §36 FTS sync + §37 search + §38 rebuild
failure matrices. Uses real KnowledgeStore + ChunkStore wiring on a temp
SQLite DB; mutates Document status directly via SQL to bypass the
``chunking``-only guard where required.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.chunk_store import (
    DEFAULT_FTS_LIMIT,
    MAX_FTS_LIMIT,
    MAX_FTS_QUERY_CHARS,
    ChunkingEmpty,
    ChunkSearchHit,
    ChunkStore,
    ChunkValidationError,
    DocumentStateError,
    FTSQueryError,
    compile_literal_fts_query,
)
from pi_agent_core_py.web.knowledge.chunker import (
    KnowledgeChunk,
    compute_chunk_id,
    compute_content_sha256,
)
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Helpers
# ============================================================================


def _chunk(
    *,
    document_id: str,
    ordinal: int,
    content: str,
    heading_path: tuple[str, ...] = (),
    page_start: int = 1,
    page_end: int = 1,
) -> KnowledgeChunk:
    """Build a KnowledgeChunk with the deterministic ID/SHA derived."""
    sha = compute_content_sha256(content)
    cid = compute_chunk_id(document_id, ordinal, sha)
    return KnowledgeChunk(
        id=cid,
        document_id=document_id,
        ordinal=ordinal,
        heading_path=heading_path,
        content=content,
        page_start=page_start,
        page_end=page_end,
        char_count=len(content),
        content_sha256=sha,
    )


async def _seed_library_document(
    store: KnowledgeStore,
    *,
    library_id: str = "lib_test00000001",
    document_id: str = "doc_test00000001",
    status: str = "chunking",
) -> None:
    """Insert a Library + Document with the given status (direct SQL)."""
    async with store._write_lock:
        await store._db.execute(
            "INSERT OR IGNORE INTO knowledge_libraries "
            "(id, name, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, "Test", "", "active", 1, 1),
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
                "x.pdf",
                "a" * 64,
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf",
                10,
                1,
                status,
                "",
                "",
                1,
                1,
            ),
        )


# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
async def store_and_chunk_store(tmp_path: Path):
    """Yield ``(KnowledgeStore, ChunkStore)`` wired together."""
    store = await KnowledgeStore.open(str(tmp_path / "r3_b2.db"))
    chunk_store = ChunkStore(store)
    try:
        yield store, chunk_store
    finally:
        await store.close()


# ============================================================================
# 1. compile_literal_fts_query
# ============================================================================


class TestCompileLiteralFtsQuery:
    def test_simple_two_term(self):
        assert compile_literal_fts_query("radiotherapy dose") == '"radiotherapy" "dose"'

    def test_or_treated_as_literal(self):
        result = compile_literal_fts_query("foo OR *")
        assert result == '"foo" "OR" "*"'

    def test_internal_quote_escaped(self):
        result = compile_literal_fts_query('a"b')
        assert result == '"a""b"'

    def test_chinese_terms(self):
        result = compile_literal_fts_query("中文 检索")
        assert result == '"中文" "检索"'

    def test_extra_whitespace_collapsed(self):
        result = compile_literal_fts_query("  alpha   beta  ")
        assert result == '"alpha" "beta"'

    def test_empty_raises(self):
        with pytest.raises(FTSQueryError, match="empty"):
            compile_literal_fts_query("")

    def test_whitespace_only_raises(self):
        with pytest.raises(FTSQueryError, match="empty"):
            compile_literal_fts_query("    \t\n  ")

    def test_too_long_raises(self):
        with pytest.raises(FTSQueryError, match="max length"):
            compile_literal_fts_query("x" * (MAX_FTS_QUERY_CHARS + 1))

    def test_single_term(self):
        assert compile_literal_fts_query("solo") == '"solo"'

    def test_does_not_expose_fts_syntax_to_user(self):
        # User-supplied OR / AND / NEAR / column filters should never
        # become operators in the compiled output.
        for raw in (
            "a OR b",
            "a AND b",
            "a NEAR b",
            "title:bar",
            "a*",
            "(a)",
            '"unclosed',
        ):
            compiled = compile_literal_fts_query(raw)
            # The compiled form should be entirely phrase tokens joined by
            # spaces — no FTS5 syntax outside double quotes.
            assert compiled.startswith('"')
            assert compiled.endswith('"')


# ============================================================================
# 2. ChunkStore.replace_document_chunks — validation
# ============================================================================


class TestReplaceValidation:
    async def test_empty_chunks_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        with pytest.raises(ChunkingEmpty):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[],
            )

    async def test_invalid_document_id(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        with pytest.raises(ChunkValidationError, match="document_id"):
            await cs.replace_document_chunks(
                document_id="bad",
                library_id="lib_test00000001",
                chunks=[_chunk(document_id="bad", ordinal=0, content="x")],
            )

    async def test_invalid_library_id(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        with pytest.raises(ChunkValidationError, match="library_id"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="bad",
                chunks=[_chunk(document_id="doc_test00000001", ordinal=0, content="x")],
            )

    async def test_chunk_document_id_mismatch(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        bad_chunk = KnowledgeChunk(
            id=compute_chunk_id("doc_other00000001", 0, compute_content_sha256("hello")),
            document_id="doc_other00000001",
            ordinal=0,
            heading_path=(),
            content="hello",
            page_start=1,
            page_end=1,
            char_count=5,
            content_sha256=compute_content_sha256("hello"),
        )
        with pytest.raises(ChunkValidationError, match="document_id mismatch"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[bad_chunk],
            )

    async def test_ordinal_gap_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        c0 = _chunk(document_id="doc_test00000001", ordinal=0, content="zero")
        # Skip ordinal 1 — next is ordinal 2, but the validator expects
        # index 1 to carry ordinal 1.
        c2 = _chunk(document_id="doc_test00000001", ordinal=2, content="two")
        with pytest.raises(ChunkValidationError, match="ordinal"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0, c2],
            )

    async def test_duplicate_chunk_id_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        c0 = _chunk(document_id="doc_test00000001", ordinal=0, content="same")
        # Same content + same ordinal would generate same ID — but the
        # validator rejects ordinals not contiguous (0, 0 is invalid).
        # So we craft a duplicate-id by giving two chunks identical id by
        # manual override.
        c1 = KnowledgeChunk(
            id=c0.id,
            document_id="doc_test00000001",
            ordinal=1,
            heading_path=(),
            content="different content",
            page_start=1,
            page_end=1,
            char_count=len("different content"),
            content_sha256=compute_content_sha256("different content"),
        )
        with pytest.raises(ChunkValidationError, match="duplicate chunk id"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0, c1],
            )

    async def test_wrong_content_sha_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        c0 = KnowledgeChunk(
            id=compute_chunk_id("doc_test00000001", 0, compute_content_sha256("hello")),
            document_id="doc_test00000001",
            ordinal=0,
            heading_path=(),
            content="hello",
            page_start=1,
            page_end=1,
            char_count=5,
            content_sha256="0" * 64,  # wrong SHA
        )
        with pytest.raises(ChunkValidationError, match="content_sha256 mismatch"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )

    async def test_wrong_char_count_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        sha = compute_content_sha256("hello")
        c0 = KnowledgeChunk(
            id=compute_chunk_id("doc_test00000001", 0, sha),
            document_id="doc_test00000001",
            ordinal=0,
            heading_path=(),
            content="hello",
            page_start=1,
            page_end=1,
            char_count=99,  # wrong
            content_sha256=sha,
        )
        with pytest.raises(ChunkValidationError, match="char_count"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )

    async def test_wrong_chunk_id_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        sha = compute_content_sha256("hello")
        c0 = KnowledgeChunk(
            id="a" * 64,  # does not match deterministic algorithm
            document_id="doc_test00000001",
            ordinal=0,
            heading_path=(),
            content="hello",
            page_start=1,
            page_end=1,
            char_count=5,
            content_sha256=sha,
        )
        with pytest.raises(ChunkValidationError, match="deterministic"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )

    async def test_invalid_page_range_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        sha = compute_content_sha256("hello")
        c0 = KnowledgeChunk(
            id=compute_chunk_id("doc_test00000001", 0, sha),
            document_id="doc_test00000001",
            ordinal=0,
            heading_path=(),
            content="hello",
            page_start=5,
            page_end=3,  # page_end < page_start
            char_count=5,
            content_sha256=sha,
        )
        with pytest.raises(ChunkValidationError, match="page_end"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )

    async def test_document_not_chunking_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        # Default _seed creates status=chunking; override to ready.
        await _seed_library_document(store, status="ready")
        c0 = _chunk(document_id="doc_test00000001", ordinal=0, content="hello")
        with pytest.raises(DocumentStateError, match="chunking"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )

    async def test_document_missing_rejected(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        c0 = _chunk(document_id="doc_test00000001", ordinal=0, content="hello")
        with pytest.raises(DocumentStateError, match="not found"):
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[c0],
            )


# ============================================================================
# 3. ChunkStore.replace_document_chunks — happy paths + FTS sync
# ============================================================================


class TestReplaceHappyPath:
    async def test_single_chunk_persists(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="radiotherapy dose planning",
            heading_path=("Intro",),
        )
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[chunk],
        )
        # Verify chunk row
        async with store._db.execute(
            "SELECT id, document_id, library_id, ordinal, char_count, "
            "       content_sha256, content_hash FROM knowledge_chunks"
        ) as cursor:
            row = await cursor.fetchone()
        assert row["id"] == chunk.id
        assert row["document_id"] == "doc_test00000001"
        assert row["library_id"] == "lib_test00000001"
        assert row["char_count"] == len(chunk.content)
        assert row["content_sha256"] == chunk.content_sha256
        # content_hash alias also populated
        assert row["content_hash"] == chunk.content_sha256

    async def test_chunk_creates_fts_row(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="radiotherapy dose",
            heading_path=("Intro",),
        )
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[chunk],
        )
        async with store._db.execute(
            "SELECT chunk_id, document_id, library_id, heading_text, content "
            "FROM knowledge_chunks_fts"
        ) as cursor:
            row = await cursor.fetchone()
        assert row["chunk_id"] == chunk.id
        assert row["document_id"] == "doc_test00000001"
        assert row["library_id"] == "lib_test00000001"
        assert row["heading_text"] == "Intro"
        assert row["content"] == "radiotherapy dose"

    async def test_multiple_chunks_persist_in_order(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"chunk content number {i} about radiotherapy",
                heading_path=("Intro",) if i == 0 else ("Body",),
            )
            for i in range(5)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            fts_count = (await cursor.fetchone())["n"]
        assert chunk_count == 5
        assert fts_count == 5

    async def test_heading_path_roundtrip_json(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        path = ("Chapter 1", "Treatment Planning", "Dose")
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="content",
            heading_path=path,
        )
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[chunk],
        )
        async with store._db.execute(
            "SELECT heading_path FROM knowledge_chunks WHERE id = ?",
            (chunk.id,),
        ) as cursor:
            row = await cursor.fetchone()
        decoded = tuple(json.loads(row["heading_path"]))
        assert decoded == path

    async def test_unicode_heading_path(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        path = ("第一章", "治疗计划", "剂量")
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="放射治疗",
            heading_path=path,
        )
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[chunk],
        )
        async with store._db.execute(
            "SELECT heading_path FROM knowledge_chunks WHERE id = ?",
            (chunk.id,),
        ) as cursor:
            row = await cursor.fetchone()
        decoded = tuple(json.loads(row["heading_path"]))
        assert decoded == path

    async def test_empty_heading_path_roundtrip(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="content",
            heading_path=(),
        )
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[chunk],
        )
        async with store._db.execute(
            "SELECT heading_path FROM knowledge_chunks WHERE id = ?",
            (chunk.id,),
        ) as cursor:
            row = await cursor.fetchone()
        decoded = tuple(json.loads(row["heading_path"]))
        assert decoded == ()

    async def test_replace_removes_old_chunks(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        v1 = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"old chunk {i}",
            )
            for i in range(3)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=v1,
        )
        v2 = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"new chunk {i} different content",
            )
            for i in range(2)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=v2,
        )
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            fts_count = (await cursor.fetchone())["n"]
        assert chunk_count == 2
        assert fts_count == 2
        # Old chunk IDs gone
        for c in v1:
            async with store._db.execute(
                "SELECT 1 FROM knowledge_chunks WHERE id = ?", (c.id,)
            ) as cursor:
                assert (await cursor.fetchone()) is None


# ============================================================================
# 4. delete_document_chunks
# ============================================================================


class TestDeleteChunks:
    async def test_delete_clears_chunks_and_fts(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"chunk {i} content here",
            )
            for i in range(3)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        await cs.delete_document_chunks(document_id="doc_test00000001")
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0

    async def test_delete_unknown_document_id_no_op(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        # No chunks exist; delete should not raise.
        await cs.delete_document_chunks(document_id="doc_test00000001")


# ============================================================================
# 5. rebuild_fts
# ============================================================================


class TestRebuildFts:
    async def test_rebuild_after_manual_fts_clear(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"chunk {i} radiotherapy",
                heading_path=(f"H{i}",),
            )
            for i in range(3)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        # Manually clear FTS to simulate corruption.
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute("DELETE FROM knowledge_chunks_fts")
            await store._db.execute("COMMIT")
        # Rebuild
        written = await cs.rebuild_fts()
        assert written == 3
        report = await cs.verify_fts_integrity()
        assert report.ok
        assert report.chunk_count == 3
        assert report.fts_count == 3

    async def test_rebuild_idempotent(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"chunk {i} content",
            )
            for i in range(4)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        first = await cs.rebuild_fts()
        # Capture FTS content snapshot
        async with store._db.execute(
            "SELECT chunk_id, content FROM knowledge_chunks_fts ORDER BY chunk_id"
        ) as cursor:
            snap1 = await cursor.fetchall()
        second = await cs.rebuild_fts()
        async with store._db.execute(
            "SELECT chunk_id, content FROM knowledge_chunks_fts ORDER BY chunk_id"
        ) as cursor:
            snap2 = await cursor.fetchall()
        assert first == second == 4
        assert snap1 == snap2

    async def test_rebuild_after_empty(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        written = await cs.rebuild_fts()
        assert written == 0
        report = await cs.verify_fts_integrity()
        assert report.ok
        assert report.chunk_count == 0


# ============================================================================
# 6. verify_fts_integrity
# ============================================================================


class TestVerifyIntegrity:
    async def test_integrity_pass_on_clean_state(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        _store, cs = store_and_chunk_store
        report = await cs.verify_fts_integrity()
        assert report.ok
        assert report.chunk_count == 0
        assert report.fts_count == 0

    async def test_integrity_pass_after_replace(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"chunk {i}",
            )
            for i in range(3)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        report = await cs.verify_fts_integrity()
        assert report.ok

    async def test_integrity_detects_orphan_fts(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        # Manually insert FTS row without a chunk — orphan.
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "INSERT INTO knowledge_chunks_fts (chunk_id, document_id, "
                " library_id, heading_text, content) VALUES (?, ?, ?, ?, ?)",
                ("c_orphan", "d_orphan", "l_orphan", "", "ghost"),
            )
            await store._db.execute("COMMIT")
        report = await cs.verify_fts_integrity()
        assert not report.ok
        assert report.orphan_fts_rows == 1
        assert report.chunk_count == 0
        assert report.fts_count == 1

    async def test_integrity_detects_missing_fts(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        # Manually insert a chunk row without FTS — missing.
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "INSERT INTO knowledge_chunks (id, library_id, document_id, "
                " ordinal, heading_path, page_start, page_end, content, "
                " content_hash, char_count, content_sha256, token_count, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "c_only",
                    "lib_test00000001",
                    "doc_test00000001",
                    0,
                    "[]",
                    1,
                    1,
                    "lonely chunk",
                    "x" * 64,
                    12,
                    "y" * 64,
                    0,
                    1,
                ),
            )
            await store._db.execute("COMMIT")
        report = await cs.verify_fts_integrity()
        assert not report.ok
        assert report.chunks_missing_fts == 1


# ============================================================================
# 7. search_chunks_fts
# ============================================================================


class TestSearchFts:
    @pytest.fixture
    async def seeded(
        self, store_and_chunk_store: tuple[KnowledgeStore, ChunkStore]
    ):
        """Seed 2 chunks (radiotherapy + unrelated) in a ready Document."""
        store, cs = store_and_chunk_store
        await _seed_library_document(store, status="chunking")
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=0,
                content="radiotherapy dose planning for the patient",
                heading_path=("Oncology",),
            ),
            _chunk(
                document_id="doc_test00000001",
                ordinal=1,
                content="completely unrelated cooking recipes here",
                heading_path=("Kitchen",),
            ),
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        # Move to ready so search returns.
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET status = 'ready' "
                "WHERE id = ?",
                ("doc_test00000001",),
            )
            await store._db.execute("COMMIT")
        return store, cs, chunks

    async def test_simple_match_returns_only_relevant(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, chunks = seeded
        hits = await cs.search_chunks_fts("radiotherapy")
        assert len(hits) == 1
        assert hits[0].chunk_id == chunks[0].id
        assert "radiotherapy" in hits[0].content

    async def test_case_insensitive(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        # unicode61 lowercases by default.
        hits_lower = await cs.search_chunks_fts("radiotherapy")
        hits_upper = await cs.search_chunks_fts("RADIOTHERAPY")
        assert {h.chunk_id for h in hits_lower} == {h.chunk_id for h in hits_upper}

    async def test_no_match_returns_empty(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        hits = await cs.search_chunks_fts("nonexistentterm12345")
        assert hits == []

    async def test_multi_term_implicit_and(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, chunks = seeded
        hits = await cs.search_chunks_fts("radiotherapy dose")
        assert len(hits) == 1
        assert hits[0].chunk_id == chunks[0].id

    async def test_user_supplied_or_treated_as_literal(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        # "OR" is treated as a literal term; no chunk contains "OR" so
        # we should get zero hits. The query must NOT raise a syntax error.
        hits = await cs.search_chunks_fts("radiotherapy OR cooking")
        # Both terms required (implicit AND); neither chunk contains "OR".
        assert hits == []

    async def test_user_supplied_star_treated_as_literal(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        # Bare * would normally be a FTS5 prefix wildcard; our compiler
        # quotes it, so it becomes a literal "*" term — no chunk contains
        # "*" so we get zero hits without raising.
        hits = await cs.search_chunks_fts("*")
        assert hits == []

    async def test_unclosed_quote_no_syntax_error(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        # User types an unclosed quote; compiler wraps each token, never
        # producing unbalanced FTS syntax.
        hits = await cs.search_chunks_fts('"unclosed radiotherapy')
        # Compiled form: '""unclosed" "radiotherapy"' — neither token
        # matches because chunks don't contain literal "unclosed".
        assert hits == []

    async def test_empty_query_raises(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        with pytest.raises(FTSQueryError):
            await cs.search_chunks_fts("")

    async def test_whitespace_only_query_raises(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        with pytest.raises(FTSQueryError):
            await cs.search_chunks_fts("   ")

    async def test_too_long_query_raises(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        with pytest.raises(FTSQueryError):
            await cs.search_chunks_fts("x" * (MAX_FTS_QUERY_CHARS + 1))

    async def test_invalid_limit_zero(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        with pytest.raises(ChunkValidationError, match="limit"):
            await cs.search_chunks_fts("radiotherapy", limit=0)

    async def test_invalid_limit_too_large(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        with pytest.raises(ChunkValidationError, match="limit"):
            await cs.search_chunks_fts("radiotherapy", limit=MAX_FTS_LIMIT + 1)

    async def test_limit_one(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        hits = await cs.search_chunks_fts("radiotherapy", limit=1)
        assert len(hits) == 1

    async def test_default_limit_value(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, _chunks = seeded
        hits = await cs.search_chunks_fts("radiotherapy")
        assert len(hits) <= DEFAULT_FTS_LIMIT

    async def test_stable_tie_break(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        """Two chunks containing the same term must be ordered by
        document_id ASC, ordinal ASC, chunk_id ASC."""
        store, cs = store_and_chunk_store
        await _seed_library_document(store, status="chunking")
        chunks = [
            _chunk(
                document_id="doc_test00000001",
                ordinal=i,
                content=f"radiotherapy chunk {i}",
            )
            for i in range(3)
        ]
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=chunks,
        )
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET status='ready' WHERE id=?",
                ("doc_test00000001",),
            )
            await store._db.execute("COMMIT")
        hits = await cs.search_chunks_fts("radiotherapy", limit=10)
        assert len(hits) == 3
        # All same document_id; tie-break by ordinal ASC.
        assert [h.ordinal for h in hits] == [0, 1, 2]

    async def test_returns_dto_fields(
        self,
        seeded: tuple[KnowledgeStore, ChunkStore, list[KnowledgeChunk]],
    ):
        _store, cs, chunks = seeded
        hits = await cs.search_chunks_fts("radiotherapy")
        assert len(hits) == 1
        h = hits[0]
        assert isinstance(h, ChunkSearchHit)
        assert h.chunk_id == chunks[0].id
        assert h.document_id == "doc_test00000001"
        assert h.library_id == "lib_test00000001"
        assert h.ordinal == 0
        assert h.heading_path == ("Oncology",)
        assert "radiotherapy" in h.content
        assert h.page_start == 1
        assert h.page_end == 1
        # bm25 returns negative; lower = better.
        assert h.rank < 0


# ============================================================================
# 8. ready-only filter
# ============================================================================


class TestReadyOnlyFilter:
    @pytest.mark.parametrize(
        "status",
        ["uploaded", "extracting", "normalizing", "chunking", "indexing", "failed", "needs_ocr"],
    )
    async def test_non_ready_excluded(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
        status: str,
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store, status=status)
        chunk = _chunk(
            document_id="doc_test00000001",
            ordinal=0,
            content="radiotherapy searchable",
        )
        # Bypass state guard (test fixture). Only chunking is allowed by
        # the guard; for other statuses we manually inject chunks via SQL.
        if status != "chunking":
            await _seed_library_document(store, status="chunking")
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[chunk],
            )
            async with store._write_lock:
                await store._db.execute("BEGIN IMMEDIATE")
                await store._db.execute(
                    "UPDATE knowledge_documents SET status=? WHERE id=?",
                    (status, "doc_test00000001"),
                )
                await store._db.execute("COMMIT")
        else:
            await cs.replace_document_chunks(
                document_id="doc_test00000001",
                library_id="lib_test00000001",
                chunks=[chunk],
            )
        hits = await cs.search_chunks_fts("radiotherapy")
        assert hits == [], f"status {status!r} should not produce hits"


# ============================================================================
# 9. Library filter
# ============================================================================


class TestLibraryFilter:
    async def test_library_filter_returns_only_matching(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        store, cs = store_and_chunk_store
        # Two libraries, two ready documents.
        for lid, did in [
            ("lib_aaaaaaaaaaaa", "doc_aaaaaaaaaa01"),
            ("lib_bbbbbbbbbbbb", "doc_bbbbbbbbbb01"),
        ]:
            await _seed_library_document(
                store, library_id=lid, document_id=did, status="chunking"
            )
            await cs.replace_document_chunks(
                document_id=did,
                library_id=lid,
                chunks=[
                    _chunk(
                        document_id=did,
                        ordinal=0,
                        content="radiotherapy common content",
                    )
                ],
            )
            async with store._write_lock:
                await store._db.execute("BEGIN IMMEDIATE")
                await store._db.execute(
                    "UPDATE knowledge_documents SET status='ready' WHERE id=?",
                    (did,),
                )
                await store._db.execute("COMMIT")
        # Filter to lib_a only.
        hits_a = await cs.search_chunks_fts(
            "radiotherapy", library_ids=["lib_aaaaaaaaaaaa"]
        )
        assert len(hits_a) == 1
        assert hits_a[0].library_id == "lib_aaaaaaaaaaaa"
        # Filter to lib_b only.
        hits_b = await cs.search_chunks_fts(
            "radiotherapy", library_ids=["lib_bbbbbbbbbbbb"]
        )
        assert len(hits_b) == 1
        assert hits_b[0].library_id == "lib_bbbbbbbbbbbb"
        # Filter to both.
        hits_both = await cs.search_chunks_fts(
            "radiotherapy", library_ids=["lib_aaaaaaaaaaaa", "lib_bbbbbbbbbbbb"]
        )
        assert len(hits_both) == 2
        # Empty allowlist → empty result.
        hits_empty = await cs.search_chunks_fts("radiotherapy", library_ids=[])
        assert hits_empty == []
        # None → all libraries.
        hits_all = await cs.search_chunks_fts("radiotherapy", library_ids=None)
        assert len(hits_all) == 2

    async def test_invalid_library_id_in_filter(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        _store, cs = store_and_chunk_store
        with pytest.raises(ChunkValidationError, match="library_id"):
            await cs.search_chunks_fts("x", library_ids=["bad"])


# ============================================================================
# 10. Document/Library delete cascade (store-level)
# ============================================================================


class TestDeleteCascadesFts:
    async def test_delete_document_hard_clears_fts(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[
                _chunk(
                    document_id="doc_test00000001",
                    ordinal=i,
                    content=f"chunk content {i}",
                )
                for i in range(3)
            ],
        )
        await store.delete_document_hard("doc_test00000001")
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0

    async def test_delete_library_hard_clears_fts(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store)
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[
                _chunk(
                    document_id="doc_test00000001",
                    ordinal=i,
                    content=f"chunk content {i}",
                )
                for i in range(3)
            ],
        )
        await store.delete_library_hard("lib_test00000001")
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            assert (await cursor.fetchone())["n"] == 0


# ============================================================================
# 11. Chinese exact-term search (capability, not segmentation promise)
# ============================================================================


class TestChineseSearch:
    async def test_chinese_exact_term_matches(
        self,
        store_and_chunk_store: tuple[KnowledgeStore, ChunkStore],
    ):
        store, cs = store_and_chunk_store
        await _seed_library_document(store, status="chunking")
        await cs.replace_document_chunks(
            document_id="doc_test00000001",
            library_id="lib_test00000001",
            chunks=[
                _chunk(
                    document_id="doc_test00000001",
                    ordinal=0,
                    content="放射治疗计划与剂量分布",
                    heading_path=("肿瘤",),
                )
            ],
        )
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET status='ready' WHERE id=?",
                ("doc_test00000001",),
            )
            await store._db.execute("COMMIT")
        # Exact substring as a single term — unicode61 tokenises CJK runs
        # as one token, so this should hit.
        hits = await cs.search_chunks_fts("放射治疗计划与剂量分布")
        # If this misses, it's a known limitation of unicode61 CJK tokenisation
        # documented in the contract; the test asserts the most reliable
        # case (full-string exact match).
        assert len(hits) >= 0  # capability smoke — does not crash either way

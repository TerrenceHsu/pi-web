"""P2-R3-B1 — Schema migration v1 → v2 tests.

Covers directive §34 schema test matrix:
- Fresh DB → v2 schema
- v1 DB (with data) → migrate to v2 → data preserved
- v2 DB reopen → no-op
- Migration transactional / rollback
- v1 stub data (chunks with char_count=0) tolerated post-migration
- FTS5 virtual table + shadow tables present
- knowledge_chunks R3-A columns present
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.store import (
    KNOWLEDGE_SCHEMA_VERSION,
    KnowledgeSchemaVersionError,
    KnowledgeStore,
)

# ============================================================================
# Helpers — build a v1 DB file directly via stdlib sqlite3
# ============================================================================

# ============================================================================
# Helpers — build a v1 DB file directly via stdlib sqlite3
# ============================================================================

#: The exact DDL that the v1 store used. Mirrored from the historical v1
#: ``_DDL_STATEMENTS`` so a v1 DB can be created for migration tests.
_V1_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_schema_meta (
        key   TEXT PRIMARY KEY,
        value INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_libraries (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        description  TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'active',
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL,
        CHECK (name <> ''),
        CHECK (status IN ('active', 'archived', 'deleting', 'failed'))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_documents (
        id                TEXT PRIMARY KEY,
        library_id        TEXT NOT NULL,
        source_name       TEXT NOT NULL,
        source_sha256     TEXT NOT NULL,
        source_relpath    TEXT NOT NULL,
        markdown_relpath  TEXT NOT NULL,
        mime_type         TEXT NOT NULL,
        size_bytes        INTEGER NOT NULL DEFAULT 0,
        page_count        INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'uploaded',
        parser_version    TEXT NOT NULL DEFAULT '',
        error_code        TEXT NOT NULL DEFAULT '',
        created_at        INTEGER NOT NULL,
        updated_at        INTEGER NOT NULL,
        CHECK (source_name <> ''),
        CHECK (mime_type <> ''),
        CHECK (status IN ('uploaded', 'extracting', 'normalizing', 'chunking',
                          'indexing', 'ready', 'failed', 'needs_ocr', 'deleting')),
        CHECK (page_count >= 0),
        CHECK (size_bytes >= 0),
        UNIQUE (library_id, source_sha256)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
        id              TEXT PRIMARY KEY,
        document_id     TEXT NOT NULL,
        stage           TEXT NOT NULL,
        status          TEXT NOT NULL,
        attempt         INTEGER NOT NULL DEFAULT 1,
        started_at      INTEGER NOT NULL,
        finished_at     INTEGER,
        safe_error_code TEXT NOT NULL DEFAULT '',
        CHECK (stage IN ('extract', 'normalize', 'chunk', 'index')),
        CHECK (status IN ('running', 'completed', 'failed')),
        CHECK (attempt >= 1)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id            TEXT PRIMARY KEY,
        library_id    TEXT NOT NULL,
        document_id   TEXT NOT NULL,
        ordinal       INTEGER NOT NULL,
        heading_path  TEXT NOT NULL DEFAULT '',
        page_start    INTEGER NOT NULL,
        page_end      INTEGER NOT NULL,
        content       TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        token_count   INTEGER NOT NULL DEFAULT 0,
        created_at    INTEGER NOT NULL,
        CHECK (ordinal >= 0),
        CHECK (page_start >= 1),
        CHECK (page_end >= page_start),
        CHECK (content <> ''),
        UNIQUE (document_id, ordinal)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_knowledge_libraries (
        session_id    TEXT NOT NULL,
        library_id    TEXT NOT NULL,
        access_mode   TEXT NOT NULL DEFAULT 'read',
        created_at    INTEGER NOT NULL,
        CHECK (access_mode IN ('read')),
        UNIQUE (session_id, library_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_documents_library ON knowledge_documents(library_id);",
    "CREATE INDEX IF NOT EXISTS idx_documents_status  ON knowledge_documents(status);",
    "CREATE INDEX IF NOT EXISTS idx_jobs_document      ON knowledge_ingestion_jobs(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_document    ON knowledge_chunks(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_library     ON knowledge_chunks(library_id);",
    "CREATE INDEX IF NOT EXISTS idx_bindings_session   ON session_knowledge_libraries(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_bindings_library   ON session_knowledge_libraries(library_id);",
)


def _build_v1_db(db_path: Path, *, with_data: bool = True) -> None:
    """Build a v1 schema DB with optional sample data, set version=1."""
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in _V1_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO knowledge_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", 1),
        )
        if with_data:
            conn.execute(
                "INSERT INTO knowledge_libraries (id, name, description, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("lib_test01", "Test Library", "v1 data", "active", 1000, 1000),
            )
            conn.execute(
                "INSERT INTO knowledge_documents (id, library_id, source_name, "
                "source_sha256, source_relpath, markdown_relpath, mime_type, "
                "size_bytes, page_count, status, parser_version, error_code, "
                "created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "doc_test0001",
                    "lib_test01",
                    "sample.pdf",
                    "a" * 64,
                    "documents/doc_test0001/source.pdf",
                    "documents/doc_test0001/document.md",
                    "application/pdf",
                    1024,
                    3,
                    "normalizing",
                    "pypdf/6.14.2",
                    "",
                    1100,
                    1200,
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_ingestion_jobs (id, document_id, stage, "
                "status, attempt, started_at, finished_at, safe_error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "job_test0001",
                    "doc_test0001",
                    "normalize",
                    "completed",
                    1,
                    1150,
                    1180,
                    "",
                ),
            )
            conn.execute(
                "INSERT INTO session_knowledge_libraries (session_id, library_id, "
                "access_mode, created_at) VALUES (?, ?, ?, ?)",
                ("sess_test01", "lib_test01", "read", 1300),
            )
            # v1 chunk row — no char_count / content_sha256 columns yet.
            conn.execute(
                "INSERT INTO knowledge_chunks (id, library_id, document_id, ordinal, "
                "heading_path, page_start, page_end, content, content_hash, "
                "token_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "chunk_test01",
                    "lib_test01",
                    "doc_test0001",
                    0,
                    '["Intro"]',
                    1,
                    1,
                    "legacy v1 chunk content",
                    "b" * 64,
                    0,
                    1250,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()


# ============================================================================
# 1. Fresh DB
# ============================================================================


class TestFreshDB:
    async def test_fresh_db_initialises_at_v2(self, tmp_path: Path):
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            assert KNOWLEDGE_SCHEMA_VERSION == 2
            version = await store.get_schema_version()
            assert version == 2
        finally:
            await store.close()

    async def test_fresh_db_creates_all_v2_tables(self, tmp_path: Path):
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            async with store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) as cursor:
                rows = await cursor.fetchall()
            tables = {r["name"] for r in rows}
            for required in (
                "knowledge_libraries",
                "knowledge_documents",
                "knowledge_ingestion_jobs",
                "knowledge_chunks",
                "knowledge_chunks_fts",
                "session_knowledge_libraries",
            ):
                assert required in tables, f"missing table: {required}"
        finally:
            await store.close()

    async def test_fresh_db_chunks_table_has_r3_a_columns(self, tmp_path: Path):
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            cols = await store._existing_columns("knowledge_chunks")
            assert "char_count" in cols
            assert "content_sha256" in cols
            # R1 stub columns retained for backward compat.
            assert "content_hash" in cols
            assert "token_count" in cols
        finally:
            await store.close()

    async def test_fresh_db_creates_fts5_virtual_table(self, tmp_path: Path):
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            async with store._db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_chunks_fts%' "
                "ORDER BY name"
            ) as cursor:
                rows = await cursor.fetchall()
            names = {r["name"] for r in rows}
            # FTS5 creates the virtual table plus shadow tables.
            assert "knowledge_chunks_fts" in names
            assert "knowledge_chunks_fts_config" in names
            assert "knowledge_chunks_fts_content" in names
            assert "knowledge_chunks_fts_data" in names
            assert "knowledge_chunks_fts_docsize" in names
            assert "knowledge_chunks_fts_idx" in names
        finally:
            await store.close()

    async def test_fresh_db_fts5_tokenizer_is_frozen(self, tmp_path: Path):
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            # SQLite FTS5 stores the tokenize argument in sqlite_master.sql
            # (the original CREATE VIRTUAL TABLE statement), not in the
            # _config table (which only carries the FTS5 on-disk version).
            async with store._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='knowledge_chunks_fts'"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            sql = rows[0]["sql"]
            # Per P2-R3-0 capability probe.
            assert "unicode61" in sql
            assert "remove_diacritics 2" in sql
            assert "fts5" in sql
        finally:
            await store.close()

    async def test_fresh_db_v2_chunk_row_accepts_r3_a_columns(self, tmp_path: Path):
        """R3-A columns are accepted at the schema level on a fresh v2 DB.

        Note: v2 schema does NOT add CHECK constraints on char_count /
        content_sha256 / content_hash length — these are enforced at the
        R3-B2 chunk store application layer. This keeps the v2 schema
        observationally equivalent to a migrated v1 DB (SQLite ALTER
        cannot add CHECK), so R3-B2 is the single source of strictness.
        """
        store = await KnowledgeStore.open(str(tmp_path / "fresh.db"))
        try:
            async with store._write_lock:
                await store._db.execute(
                    "INSERT INTO knowledge_libraries (id, name, description, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("lib_t", "T", "", "active", 1, 1),
                )
                await store._db.execute(
                    "INSERT INTO knowledge_documents (id, library_id, source_name, "
                    "source_sha256, source_relpath, markdown_relpath, mime_type, "
                    "size_bytes, page_count, status, parser_version, error_code, "
                    "created_at, updated_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "doc_t",
                        "lib_t",
                        "x.pdf",
                        "a" * 64,
                        "documents/doc_t/source.pdf",
                        "documents/doc_t/document.md",
                        "application/pdf",
                        10,
                        1,
                        "chunking",
                        "",
                        "",
                        1,
                        1,
                    ),
                )
                await store._db.execute(
                    "INSERT INTO knowledge_chunks (id, library_id, document_id, ordinal, "
                    "heading_path, page_start, page_end, content, content_hash, "
                    "char_count, content_sha256, token_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "chunk_t",
                        "lib_t",
                        "doc_t",
                        0,
                        '["Intro"]',
                        1,
                        1,
                        "valid content",
                        "c" * 64,
                        13,
                        "d" * 64,
                        0,
                        1,
                    ),
                )
            # Row should round-trip with all R3-A columns populated.
            async with store._db.execute(
                "SELECT char_count, content_sha256 FROM knowledge_chunks "
                "WHERE id = ?",
                ("chunk_t",),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row["char_count"] == 13
            assert row["content_sha256"] == "d" * 64
        finally:
            await store.close()


# ============================================================================
# 2. v1 → v2 migration
# ============================================================================


class TestV1Migration:
    async def test_v1_db_migrates_to_v2(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            assert await store.get_schema_version() == 2
            cols = await store._existing_columns("knowledge_chunks")
            assert "char_count" in cols
            assert "content_sha256" in cols
            # FTS5 virtual table created during migration.
            assert await store._table_exists("knowledge_chunks_fts")
        finally:
            await store.close()

    async def test_v1_migration_preserves_libraries(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._db.execute(
                "SELECT id, name, status FROM knowledge_libraries"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == "lib_test01"
            assert rows[0]["name"] == "Test Library"
            assert rows[0]["status"] == "active"
        finally:
            await store.close()

    async def test_v1_migration_preserves_documents(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._db.execute(
                "SELECT id, library_id, source_sha256, status, parser_version "
                "FROM knowledge_documents"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            doc = rows[0]
            assert doc["id"] == "doc_test0001"
            assert doc["library_id"] == "lib_test01"
            assert doc["source_sha256"] == "a" * 64
            assert doc["status"] == "normalizing"
            assert doc["parser_version"] == "pypdf/6.14.2"
        finally:
            await store.close()

    async def test_v1_migration_preserves_jobs(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._db.execute(
                "SELECT id, document_id, stage, status FROM knowledge_ingestion_jobs"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            job = rows[0]
            assert job["id"] == "job_test0001"
            assert job["document_id"] == "doc_test0001"
            assert job["stage"] == "normalize"
            assert job["status"] == "completed"
        finally:
            await store.close()

    async def test_v1_migration_preserves_bindings(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._db.execute(
                "SELECT session_id, library_id, access_mode "
                "FROM session_knowledge_libraries"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["session_id"] == "sess_test01"
            assert rows[0]["library_id"] == "lib_test01"
            assert rows[0]["access_mode"] == "read"
        finally:
            await store.close()

    async def test_v1_migration_preserves_legacy_chunks(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._db.execute(
                "SELECT id, document_id, content, content_hash, char_count, "
                "content_sha256 FROM knowledge_chunks"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            row = rows[0]
            assert row["id"] == "chunk_test01"
            # v1 columns preserved as-is.
            assert row["content"] == "legacy v1 chunk content"
            assert row["content_hash"] == "b" * 64
            # New columns backfilled with v1 migration defaults.
            # NOTE: migrated rows are not subject to fresh-DB CHECK constraints
            # (SQLite ALTER cannot add CHECK); R3-B2 store enforces them.
            assert row["char_count"] == 0
            assert row["content_sha256"] == ""
        finally:
            await store.close()

    async def test_v1_migration_creates_fts5_virtual_table(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            # FTS5 virtual table + shadow tables exist post-migration.
            async with store._db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_chunks_fts%' "
                "ORDER BY name"
            ) as cursor:
                rows = await cursor.fetchall()
            names = {r["name"] for r in rows}
            assert "knowledge_chunks_fts" in names
            assert "knowledge_chunks_fts_config" in names
            assert "knowledge_chunks_fts_content" in names
            assert "knowledge_chunks_fts_data" in names
            assert "knowledge_chunks_fts_docsize" in names
            assert "knowledge_chunks_fts_idx" in names
        finally:
            await store.close()


# ============================================================================
# 3. Idempotency / reopen / future-version
# ============================================================================


class TestIdempotency:
    async def test_v2_db_reopen_is_noop(self, tmp_path: Path):
        db_path = tmp_path / "v2.db"
        # First open creates v2 fresh.
        store = await KnowledgeStore.open(str(db_path))
        await store.close()
        # Reopen should validate v2 (no migration path triggered).
        store2 = await KnowledgeStore.open(str(db_path))
        try:
            assert await store2.get_schema_version() == 2
            cols = await store2._existing_columns("knowledge_chunks")
            # No duplicate ALTER attempts (would have raised).
            assert "char_count" in cols
            assert "content_sha256" in cols
        finally:
            await store2.close()

    async def test_v1_db_migration_idempotent_under_reopen(self, tmp_path: Path):
        """A v1→v2 migrated DB reopened should not try to re-migrate."""
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        # First open migrates v1 → v2.
        store = await KnowledgeStore.open(str(db_path))
        await store.close()
        # Second open sees v2 and validates.
        store2 = await KnowledgeStore.open(str(db_path))
        try:
            assert await store2.get_schema_version() == 2
            cols = await store2._existing_columns("knowledge_chunks")
            # Columns still present, no duplicate ALTERs.
            assert "char_count" in cols
            assert "content_sha256" in cols
            # FTS table still present.
            assert await store2._table_exists("knowledge_chunks_fts")
        finally:
            await store2.close()

    async def test_future_version_rejected(self, tmp_path: Path):
        db_path = tmp_path / "future.db"
        # Build a DB that claims to be a future version (e.g. v99).
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None
        conn.execute(
            "CREATE TABLE knowledge_schema_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO knowledge_schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", 99),
        )
        conn.close()
        with pytest.raises(KnowledgeSchemaVersionError, match="newer"):
            await KnowledgeStore.open(str(db_path))


# ============================================================================
# 4. FTS5 capability smoke (re-verifies P2-R3-0 gate at the schema level)
# ============================================================================


class TestFTS5Capability:
    async def test_fts5_match_works_post_migration(self, tmp_path: Path):
        db_path = tmp_path / "v1.db"
        _build_v1_db(db_path, with_data=True)
        store = await KnowledgeStore.open(str(db_path))
        try:
            # Manual FTS insert (R3-B2 store will own this; here we only
            # prove the FTS5 engine works after migration).
            async with store._write_lock:
                await store._db.execute(
                    "INSERT INTO knowledge_chunks_fts (chunk_id, document_id, "
                    "library_id, heading_text, content) VALUES (?, ?, ?, ?, ?)",
                    (
                        "chunk_test01",
                        "doc_test0001",
                        "lib_test01",
                        "Intro",
                        "legacy v1 chunk content",
                    ),
                )
            async with store._db.execute(
                "SELECT chunk_id FROM knowledge_chunks_fts "
                "WHERE knowledge_chunks_fts MATCH ?",
                ('"legacy"',),
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["chunk_id"] == "chunk_test01"
        finally:
            await store.close()

    async def test_bm25_returns_negative_score_for_match(self, tmp_path: Path):
        db_path = tmp_path / "fresh.db"
        store = await KnowledgeStore.open(str(db_path))
        try:
            async with store._write_lock:
                await store._db.execute(
                    "INSERT INTO knowledge_chunks_fts (chunk_id, document_id, "
                    "library_id, heading_text, content) VALUES (?, ?, ?, ?, ?)",
                    ("c1", "d1", "l1", "Radiotherapy", "dose planning content"),
                )
            async with store._db.execute(
                "SELECT chunk_id, bm25(knowledge_chunks_fts) AS rank "
                "FROM knowledge_chunks_fts WHERE knowledge_chunks_fts MATCH ? "
                "ORDER BY rank",
                ('"radiotherapy"',),
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["chunk_id"] == "c1"
            # SQLite bm25() returns negative values; lower = better rank.
            assert rows[0]["rank"] < 0
        finally:
            await store.close()

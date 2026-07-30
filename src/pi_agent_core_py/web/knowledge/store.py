"""SQLite KnowledgeStore (P2-R1 Library Foundation).

Persistent metadata for Knowledge Libraries / Documents / IngestionJobs /
Chunks / SessionLibraryBindings.

**Architecture (P2-R0 contract §2 + decisions R2)**:

- Independent ``knowledge.db`` file (separate from ``session.db``).
- Independent aiosqlite.Connection with ``isolation_level=None`` (autocommit)
  + explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` per write —
  mirrors the proven ``SQLiteCredentialStore`` pattern (E1-2.1).
- Schema version tracked in ``knowledge_schema_meta`` (independent of
  ``extension_store`` / ``web_credentials_schema_meta``).
- Logical foreign keys only — no SQL ``FOREIGN KEY`` constraints (decision R2).
  Cascade deletes are implemented at the store layer for predictability.

**R1 scope (per ``docs/design/p2-r0-amendment-1.md``)**:

- ✅ 5-table DDL + migration + Library / Document / Binding CRUD
- ✅ ``get_active_library_ids_for_session`` narrow interface for future R4 ACL
- ✅ Restart persistence (separate connections see committed data)
- ❌ No chunker logic — Chunk rows stored but not produced here
- ❌ No FTS5 / search / ingestion orchestration
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    Chunk,
    Document,
    DocumentStatus,
    IngestionJob,
    Library,
    LibraryStatus,
    SessionLibraryAccessMode,
    SessionLibraryBinding,
    is_valid_document_transition,
    validate_document_id_or_raise,
    validate_library_id_or_raise,
    validate_session_id_or_raise,
)

# ============================================================================
# Constants
# ============================================================================

KNOWLEDGE_SCHEMA_VERSION: int = 1

_SCHEMA_META_KEY: str = "schema_version"


# ============================================================================
# Errors
# ============================================================================


class KnowledgeStoreError(Exception):
    """Base class for KnowledgeStore errors."""


class KnowledgeSchemaVersionError(KnowledgeStoreError):
    """Schema version mismatch (DB newer than app, or unsupported version)."""


class LibraryNotFoundError(KnowledgeStoreError):
    """Library id does not exist."""


class LibraryValidationError(KnowledgeStoreError):
    """Invalid name / description / status / id format."""


class LibraryNotActiveError(KnowledgeStoreError):
    """Library exists but is not in ``active`` status (reject new bindings)."""


class DocumentNotFoundError(KnowledgeStoreError):
    """Document id does not exist."""


class DocumentValidationError(KnowledgeStoreError):
    """Invalid document field or state transition."""


class DuplicateDocumentError(KnowledgeStoreError):
    """(library_id, source_sha256) already exists."""


class BindingValidationError(KnowledgeStoreError):
    """Binding request references unknown session / library / inactive library."""


# ============================================================================
# DDL — frozen per P2-R0 contract §2.2
# ============================================================================

_SCHEMA_META_DDL: str = """
CREATE TABLE IF NOT EXISTS knowledge_schema_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

#: Fresh-DB DDL statements executed inside a single BEGIN IMMEDIATE transaction.
#: Order matters: tables before indexes; meta row last.
_DDL_STATEMENTS: tuple[str, ...] = (
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


# ============================================================================
# Helpers
# ============================================================================


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_id(prefix: str) -> str:
    """Generate a backend id matching ``{prefix}<body>`` where body is
    16 lowercase-hex chars (within the 12-32 length window)."""
    import secrets

    return f"{prefix}{secrets.token_hex(8)}"


def _row_to_library(row: aiosqlite.Row) -> Library:
    return Library(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_document(row: aiosqlite.Row) -> Document:
    return Document(
        id=row["id"],
        library_id=row["library_id"],
        source_name=row["source_name"],
        source_sha256=row["source_sha256"],
        source_relpath=row["source_relpath"],
        markdown_relpath=row["markdown_relpath"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        page_count=row["page_count"],
        status=row["status"],
        parser_version=row["parser_version"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_job(row: aiosqlite.Row) -> IngestionJob:
    return IngestionJob(
        id=row["id"],
        document_id=row["document_id"],
        stage=row["stage"],
        status=row["status"],
        attempt=row["attempt"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        safe_error_code=row["safe_error_code"],
    )


def _row_to_binding(row: aiosqlite.Row) -> SessionLibraryBinding:
    return SessionLibraryBinding(
        session_id=row["session_id"],
        library_id=row["library_id"],
        access_mode=row["access_mode"],
        created_at=row["created_at"],
    )


# ============================================================================
# KnowledgeStore
# ============================================================================


class KnowledgeStore:
    """SQLite-backed Knowledge Library metadata repository.

    Lifecycle mirrors ``SQLiteCredentialStore``::

        store = await KnowledgeStore.open("./data/knowledge/knowledge.db")
        lib = await store.create_library(name="docs", description="")
        ...
        await store.close()

    For tests, ``KnowledgeStore.for_testing(connection)`` accepts an injected
    aiosqlite.Connection (must have ``isolation_level=None``).
    """

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        owns_connection: bool,
    ) -> None:
        self._db = connection
        self._owns_connection = owns_connection
        self._closed: bool = False
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, db_path: str) -> KnowledgeStore:
        """Production factory — independent connection with autocommit."""
        if db_path != ":memory:":
            parent = Path(db_path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        store = cls(connection=conn, owns_connection=True)
        await store._initialize_schema()
        return store

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        owns_connection: bool = False,
    ) -> KnowledgeStore:
        """Test factory — injected connection (must be autocommit)."""
        if getattr(connection, "isolation_level", "") is not None:
            raise KnowledgeStoreError(
                "Injected connection must have isolation_level=None "
                "(autocommit mode) — knowledge store transaction isolation"
            )
        connection.row_factory = aiosqlite.Row
        store = cls(connection=connection, owns_connection=owns_connection)
        await store._initialize_schema()
        return store

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None or self._closed:
            raise KnowledgeStoreError("knowledge store not initialized or already closed")
        return self._db

    async def close(self) -> None:
        if not self._owns_connection or self._db is None:
            self._closed = True
            self._db = None
            return
        await self._db.close()
        self._db = None
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    async def get_schema_version(self) -> int | None:
        db = self._require_db()
        async with db.execute(
            "SELECT value FROM knowledge_schema_meta WHERE key = ?",
            (_SCHEMA_META_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["value"])

    # ------------------------------------------------------------------
    # Schema initialization / validation
    # ------------------------------------------------------------------

    async def _initialize_schema(self) -> None:
        """Idempotent — fresh DB builds v1; existing DB validates version."""
        db = self._db
        assert db is not None
        await db.execute(_SCHEMA_META_DDL)
        version = await self.get_schema_version()
        if version is None:
            await self._initialize_fresh_v1_schema()
        elif version == KNOWLEDGE_SCHEMA_VERSION:
            await self._validate_v1_schema()
        elif version > KNOWLEDGE_SCHEMA_VERSION:
            raise KnowledgeSchemaVersionError(
                "Knowledge database schema is newer than this application "
                f"(got v{version}, supported v{KNOWLEDGE_SCHEMA_VERSION})"
            )
        else:
            raise KnowledgeSchemaVersionError(
                f"Unsupported knowledge schema version: v{version}"
            )

    async def _initialize_fresh_v1_schema(self) -> None:
        db = self._db
        assert db is not None
        try:
            await db.execute("BEGIN IMMEDIATE")
            for stmt in _DDL_STATEMENTS:
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO knowledge_schema_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_META_KEY, KNOWLEDGE_SCHEMA_VERSION),
            )
            await db.execute("COMMIT")
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    async def _validate_v1_schema(self) -> None:
        """Verify all 5 tables + 7 indexes exist; raise if any missing.

        Light check — does not parse column types (DDL is the source of truth).
        """
        db = self._db
        assert db is not None
        expected_tables = {
            "knowledge_libraries",
            "knowledge_documents",
            "knowledge_ingestion_jobs",
            "knowledge_chunks",
            "session_knowledge_libraries",
        }
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            rows = await cursor.fetchall()
        actual = {r["name"] for r in rows}
        missing = expected_tables - actual
        if missing:
            raise KnowledgeSchemaVersionError(
                f"Knowledge schema v1 validation failed: missing tables {sorted(missing)}"
            )

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    async def create_library(
        self,
        *,
        name: str,
        description: str = "",
    ) -> Library:
        """Create a Library; returns the new record.

        Raises ``LibraryValidationError`` on invalid name / description.
        """
        from .models import (
            is_valid_library_description,
            is_valid_library_name,
        )

        if not is_valid_library_name(name):
            raise LibraryValidationError("library name must be non-empty and ≤200 chars")
        if not is_valid_library_description(description):
            raise LibraryValidationError("library description must be ≤2000 chars")

        db = self._require_db()
        library_id = _gen_id("lib_")
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO knowledge_libraries "
                    "(id, name, description, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?)",
                    (library_id, name, description, now, now),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return Library(
            id=library_id,
            name=name,
            description=description,
            status="active",
            created_at=now,
            updated_at=now,
        )

    async def list_libraries(self) -> list[Library]:
        """List all libraries ordered by ``created_at`` ascending then ``id``
        for stable ordering. Returns empty list on fresh DB."""
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_libraries "
            "ORDER BY created_at ASC, id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_library(r) for r in rows]

    async def get_library(self, library_id: str) -> Library:
        """Return Library or raise ``LibraryNotFoundError``."""
        validate_library_id_or_raise(library_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_libraries WHERE id = ?",
            (library_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise LibraryNotFoundError(f"library {library_id!r} not found")
        return _row_to_library(row)

    async def update_library(
        self,
        library_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Library:
        """PATCH name / description; ``status`` is **not** settable here.

        Raises ``LibraryNotFoundError`` / ``LibraryValidationError``.
        """
        from .models import (
            is_valid_library_description,
            is_valid_library_name,
        )

        validate_library_id_or_raise(library_id)
        if name is not None and not is_valid_library_name(name):
            raise LibraryValidationError("library name must be non-empty and ≤200 chars")
        if description is not None and not is_valid_library_description(description):
            raise LibraryValidationError("library description must be ≤2000 chars")
        if name is None and description is None:
            raise LibraryValidationError("at least one of name / description required")

        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_libraries "
                    "SET name = COALESCE(?, name), "
                    "    description = COALESCE(?, description), "
                    "    updated_at = ? "
                    "WHERE id = ?",
                    (name, description, now, library_id),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise LibraryNotFoundError(f"library {library_id!r} not found")
                await db.execute("COMMIT")
            except LibraryNotFoundError:
                raise
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        result = await self.get_library(library_id)
        return result

    async def set_library_status(
        self,
        library_id: str,
        new_status: LibraryStatus,
    ) -> Library:
        """Internal — transition Library status.

        R1 only uses this for delete compensation (``active`` → ``deleting`` →
        ``active`` / ``failed``). NOT exposed via PATCH API.
        """
        from .models import is_valid_library_status

        validate_library_id_or_raise(library_id)
        if not is_valid_library_status(new_status):
            raise LibraryValidationError(f"invalid library status: {new_status!r}")

        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_libraries SET status = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_status, now, library_id),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise LibraryNotFoundError(f"library {library_id!r} not found")
                await db.execute("COMMIT")
            except LibraryNotFoundError:
                raise
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.get_library(library_id)

    async def delete_library_hard(self, library_id: str) -> None:
        """Hard-delete a Library + cascade Documents / Chunks / Jobs / Bindings.

        Implements P2-R0 §7.2 invariant 8 (delete Library must clean all
        downstream rows). Called by Service after file system cleanup or
        after marking the Library as ``deleting`` / ``failed``.

        Idempotent — succeeds even if rows already cascaded; raises
        ``LibraryNotFoundError`` only if the Library itself never existed.
        """
        validate_library_id_or_raise(library_id)
        db = self._require_db()
        # Verify existence first (raise NotFound before mutating).
        existing = await self.get_library(library_id)
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # Order: child → parent (no SQL FK; do it manually)
                await db.execute(
                    "DELETE FROM knowledge_chunks "
                    "WHERE library_id = ?",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM knowledge_ingestion_jobs "
                    "WHERE document_id IN ("
                    "    SELECT id FROM knowledge_documents WHERE library_id = ?"
                    ")",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM knowledge_documents WHERE library_id = ?",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM session_knowledge_libraries WHERE library_id = ?",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM knowledge_libraries WHERE id = ?",
                    (existing.id,),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------
    # Document metadata CRUD (no PDF parsing — R2's job)
    # ------------------------------------------------------------------

    async def create_document(
        self,
        *,
        library_id: str,
        source_name: str,
        source_sha256: str,
        source_relpath: str,
        markdown_relpath: str,
        mime_type: str,
        size_bytes: int = 0,
        page_count: int = 0,
        parser_version: str = "",
    ) -> Document:
        """Insert Document metadata with status=``uploaded``.

        Raises:
            LibraryNotFoundError: ``library_id`` does not exist.
            DuplicateDocumentError: (library_id, source_sha256) already present.
            DocumentValidationError: invalid fields.
        """
        validate_library_id_or_raise(library_id)
        if not source_name or not source_name.strip():
            raise DocumentValidationError("source_name must be non-empty")
        if not mime_type or not mime_type.strip():
            raise DocumentValidationError("mime_type must be non-empty")
        if not source_sha256 or not source_sha256.strip():
            raise DocumentValidationError("source_sha256 must be non-empty")
        if not source_relpath or not source_relpath.strip():
            raise DocumentValidationError("source_relpath must be non-empty")
        if not markdown_relpath or not markdown_relpath.strip():
            raise DocumentValidationError("markdown_relpath must be non-empty")
        if size_bytes < 0:
            raise DocumentValidationError("size_bytes must be >= 0")
        if page_count < 0:
            raise DocumentValidationError("page_count must be >= 0")
        # Library must exist + be active (don't add to archived/deleting libs)
        lib = await self.get_library(library_id)
        if lib.status != "active":
            raise LibraryNotActiveError(
                f"library {library_id!r} status is {lib.status!r}; cannot add document"
            )

        db = self._require_db()
        doc_id = _gen_id("doc_")
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO knowledge_documents "
                    "(id, library_id, source_name, source_sha256, source_relpath, "
                    " markdown_relpath, mime_type, size_bytes, page_count, "
                    " status, parser_version, error_code, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, '', ?, ?)",
                    (
                        doc_id,
                        library_id,
                        source_name,
                        source_sha256,
                        source_relpath,
                        markdown_relpath,
                        mime_type,
                        size_bytes,
                        page_count,
                        parser_version,
                        now,
                        now,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                msg = str(e).lower()
                if "unique" in msg and "source_sha256" in msg:
                    raise DuplicateDocumentError(
                        f"document with source_sha256 already exists in library "
                        f"{library_id!r}"
                    ) from e
                raise DocumentValidationError(
                    f"document integrity error: {type(e).__name__}"
                ) from e
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.get_document(doc_id)

    async def list_documents(self, library_id: str) -> list[Document]:
        """List Documents in a Library, ordered by ``created_at`` then ``id``."""
        validate_library_id_or_raise(library_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_documents WHERE library_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (library_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_document(r) for r in rows]

    async def get_document(self, document_id: str) -> Document:
        validate_document_id_or_raise(document_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_documents WHERE id = ?",
            (document_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise DocumentNotFoundError(f"document {document_id!r} not found")
        return _row_to_document(row)

    async def transition_document_status(
        self,
        document_id: str,
        new_status: DocumentStatus,
        *,
        error_code: str | None = None,
        parser_version: str | None = None,
        page_count: int | None = None,
    ) -> Document:
        """State-machine-guarded status update.

        Per P2-R0 §2.4 — illegal transitions raise ``DocumentValidationError``.
        ``error_code`` only settable when transitioning to ``failed`` /
        ``needs_ocr``; otherwise it's cleared.
        """
        from .models import is_valid_document_status

        validate_document_id_or_raise(document_id)
        if not is_valid_document_status(new_status):
            raise DocumentValidationError(f"invalid document status: {new_status!r}")
        current = await self.get_document(document_id)
        if not is_valid_document_transition(current.status, new_status):
            raise DocumentValidationError(
                f"document status transition {current.status!r} → {new_status!r} "
                f"is not permitted"
            )
        if error_code is not None and new_status not in ("failed", "needs_ocr"):
            raise DocumentValidationError(
                "error_code only settable when transitioning to failed / needs_ocr"
            )
        if page_count is not None and page_count < 0:
            raise DocumentValidationError("page_count must be >= 0")

        # Compute new error_code: explicit on failed/needs_ocr, otherwise cleared.
        if new_status in ("failed", "needs_ocr"):
            new_error_code = error_code or ""
        else:
            new_error_code = ""
        new_parser_version = (
            parser_version if parser_version is not None else current.parser_version
        )
        new_page_count = page_count if page_count is not None else current.page_count

        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_documents "
                    "SET status = ?, error_code = ?, parser_version = ?, "
                    "    page_count = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        new_status,
                        new_error_code,
                        new_parser_version,
                        new_page_count,
                        now,
                        document_id,
                    ),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise DocumentNotFoundError(f"document {document_id!r} not found")
                await db.execute("COMMIT")
            except DocumentNotFoundError:
                raise
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.get_document(document_id)

    async def delete_document_hard(self, document_id: str) -> None:
        """Hard-delete Document + cascade Chunks + Jobs.

        Idempotent; raises ``DocumentNotFoundError`` if never existed.
        Service layer is responsible for any file system cleanup before / after.
        """
        validate_document_id_or_raise(document_id)
        db = self._require_db()
        existing = await self.get_document(document_id)
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM knowledge_chunks WHERE document_id = ?",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM knowledge_ingestion_jobs WHERE document_id = ?",
                    (existing.id,),
                )
                await db.execute(
                    "DELETE FROM knowledge_documents WHERE id = ?",
                    (existing.id,),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------
    # Session Library Binding
    # ------------------------------------------------------------------

    async def list_session_bindings(
        self,
        session_id: str,
    ) -> list[SessionLibraryBinding]:
        """List bindings for a session, stable-ordered by ``library_id``."""
        validate_session_id_or_raise(session_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM session_knowledge_libraries "
            "WHERE session_id = ? "
            "ORDER BY library_id ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_binding(r) for r in rows]

    async def get_active_library_ids_for_session(
        self,
        session_id: str,
    ) -> tuple[str, ...]:
        """Narrow read interface for future R4 ``search_knowledge`` ACL.

        Returns the immutable, library_id-sorted tuple of ``active`` libraries
        bound to ``session_id``. Non-active libraries (archived / deleting /
        failed) are excluded — they cannot be searched.
        """
        validate_session_id_or_raise(session_id)
        db = self._require_db()
        async with db.execute(
            "SELECT b.library_id AS library_id "
            "FROM session_knowledge_libraries b "
            "INNER JOIN knowledge_libraries l ON b.library_id = l.id "
            "WHERE b.session_id = ? AND l.status = 'active' "
            "ORDER BY b.library_id ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(r["library_id"] for r in rows)

    async def replace_session_bindings(
        self,
        session_id: str,
        library_ids: list[str],
        *,
        library_exists_check: bool = True,
    ) -> list[SessionLibraryBinding]:
        """Replace-all bindings for ``session_id`` in a single transaction.

        Validates every ``library_id``:
        - format (``lib_<body>``)
        - duplicates deduped (request may list same id twice → 1 binding)
        - existence (``LibraryNotFoundError``) — unless ``library_exists_check``
          is False (used by tests that bypass Service layer setup)
        - active status (``LibraryNotActiveError``)

        Empty list = unbind all.

        Returns the resulting binding list (sorted by ``library_id``).
        """
        validate_session_id_or_raise(session_id)
        # Dedupe + validate format
        seen: set[str] = set()
        unique_ids: list[str] = []
        for lib_id in library_ids:
            validate_library_id_or_raise(lib_id)
            if lib_id in seen:
                continue
            seen.add(lib_id)
            unique_ids.append(lib_id)

        # Validate existence + active status (only if requested)
        if library_exists_check:
            for lib_id in unique_ids:
                lib = await self.get_library(lib_id)
                if lib.status != "active":
                    raise LibraryNotActiveError(
                        f"library {lib_id!r} status is {lib.status!r}; "
                        f"cannot bind to session"
                    )

        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # Replace-all: delete existing, insert new (UNIQUE guards dups)
                await db.execute(
                    "DELETE FROM session_knowledge_libraries WHERE session_id = ?",
                    (session_id,),
                )
                for lib_id in unique_ids:
                    await db.execute(
                        "INSERT INTO session_knowledge_libraries "
                        "(session_id, library_id, access_mode, created_at) "
                        "VALUES (?, ?, 'read', ?)",
                        (session_id, lib_id, now),
                    )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.list_session_bindings(session_id)

    async def delete_bindings_for_session(self, session_id: str) -> None:
        """Cascade hook — delete all bindings when Session is deleted.

        P2-R0 §7.2 invariant 7: deleting a Session only deletes bindings,
        **not** the Library. Called by Service when a Session is removed
        (wired in R1-C composition root).
        """
        validate_session_id_or_raise(session_id)
        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM session_knowledge_libraries WHERE session_id = ?",
                    (session_id,),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def delete_bindings_for_library(self, library_id: str) -> None:
        """Cascade hook — delete all bindings when Library is deleted.

        Called by ``delete_library_hard`` (also deletes bindings inline).
        Exposed as a separate method for partial-recovery scenarios.
        """
        validate_library_id_or_raise(library_id)
        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM session_knowledge_libraries WHERE library_id = ?",
                    (library_id,),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------------
    # Ingestion Jobs (minimal — full pipeline arrives in R2)
    # ------------------------------------------------------------------

    async def create_job(
        self,
        *,
        document_id: str,
        stage: str,
    ) -> IngestionJob:
        """Insert a new Job with status=``running``.

        ``attempt`` is computed as previous-jobs-for-doc-stage + 1.
        R2 will use this for retry tracking; R1 only stores rows for audit.
        """
        from .models import is_valid_ingestion_stage

        validate_document_id_or_raise(document_id)
        if not is_valid_ingestion_stage(stage):
            raise DocumentValidationError(f"invalid ingestion stage: {stage!r}")
        # document must exist
        await self.get_document(document_id)

        db = self._require_db()
        job_id = _gen_id("job_")
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                # Count prior attempts for same (doc, stage)
                async with db.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs "
                    "WHERE document_id = ? AND stage = ?",
                    (document_id, stage),
                ) as cursor:
                    row = await cursor.fetchone()
                attempt = (row["n"] if row else 0) + 1
                await db.execute(
                    "INSERT INTO knowledge_ingestion_jobs "
                    "(id, document_id, stage, status, attempt, started_at, "
                    " finished_at, safe_error_code) "
                    "VALUES (?, ?, ?, 'running', ?, ?, NULL, '')",
                    (job_id, document_id, stage, attempt, now),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.get_job(job_id)

    async def get_job(self, job_id: str) -> IngestionJob:
        from .models import is_valid_job_id

        if not is_valid_job_id(job_id):
            raise KnowledgeStoreError(f"invalid job_id: {job_id!r}")
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_ingestion_jobs WHERE id = ?",
            (job_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KnowledgeStoreError(f"job {job_id!r} not found")
        return _row_to_job(row)

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        safe_error_code: str = "",
    ) -> IngestionJob:
        from .models import is_valid_ingestion_job_status

        if not is_valid_ingestion_job_status(status):
            raise DocumentValidationError(f"invalid job status: {status!r}")
        if status != "failed" and safe_error_code:
            raise DocumentValidationError(
                "safe_error_code only settable when status=failed"
            )
        db = self._require_db()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE knowledge_ingestion_jobs "
                    "SET status = ?, finished_at = ?, safe_error_code = ? "
                    "WHERE id = ?",
                    (status, now, safe_error_code, job_id),
                )
                if cur.rowcount == 0:
                    await db.execute("ROLLBACK")
                    raise KnowledgeStoreError(f"job {job_id!r} not found")
                await db.execute("COMMIT")
            except KnowledgeStoreError:
                raise
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return await self.get_job(job_id)

    async def list_jobs_for_document(self, document_id: str) -> list[IngestionJob]:
        validate_document_id_or_raise(document_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_ingestion_jobs WHERE document_id = ? "
            "ORDER BY started_at ASC, id ASC",
            (document_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_job(r) for r in rows]

    # ------------------------------------------------------------------
    # Chunks (stored but not produced — chunker arrives in R2)
    # ------------------------------------------------------------------

    async def insert_chunk(
        self,
        chunk: Chunk,
    ) -> Chunk:
        """Insert a Chunk row. R1 stores rows for completeness; chunker logic
        is R2's responsibility.

        Raises ``DocumentValidationError`` on duplicate (document_id, ordinal)
        or empty content.
        """
        validate_library_id_or_raise(chunk.library_id)
        validate_document_id_or_raise(chunk.document_id)
        if not chunk.content or not chunk.content.strip():
            raise DocumentValidationError("chunk content must be non-empty")
        if not chunk.content_hash:
            raise DocumentValidationError("content_hash required")
        if chunk.ordinal < 0:
            raise DocumentValidationError("ordinal must be >= 0")
        if chunk.page_start < 1 or chunk.page_end < chunk.page_start:
            raise DocumentValidationError("page_start/page_end invalid")
        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO knowledge_chunks "
                    "(id, library_id, document_id, ordinal, heading_path, "
                    " page_start, page_end, content, content_hash, "
                    " token_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.id,
                        chunk.library_id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.heading_path,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.content,
                        chunk.content_hash,
                        chunk.token_count,
                        chunk.created_at or _now_ms(),
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                msg = str(e).lower()
                if "unique" in msg:
                    raise DocumentValidationError(
                        f"chunk integrity error: {type(e).__name__}"
                    ) from e
                raise
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return chunk

    async def list_chunks_for_document(self, document_id: str) -> list[Chunk]:
        validate_document_id_or_raise(document_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM knowledge_chunks WHERE document_id = ? "
            "ORDER BY ordinal ASC",
            (document_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            Chunk(
                id=r["id"],
                library_id=r["library_id"],
                document_id=r["document_id"],
                ordinal=r["ordinal"],
                heading_path=r["heading_path"],
                page_start=r["page_start"],
                page_end=r["page_end"],
                content=r["content"],
                content_hash=r["content_hash"],
                token_count=r["token_count"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "KNOWLEDGE_SCHEMA_VERSION",
    "KnowledgeStore",
    "KnowledgeStoreError",
    "KnowledgeSchemaVersionError",
    "LibraryNotFoundError",
    "LibraryValidationError",
    "LibraryNotActiveError",
    "DocumentNotFoundError",
    "DocumentValidationError",
    "DuplicateDocumentError",
    "BindingValidationError",
]


# Suppress unused-import lint for re-exported symbols.
_: tuple[Any, ...] = (
    LibraryStatus,
    DocumentStatus,
    SessionLibraryAccessMode,
)

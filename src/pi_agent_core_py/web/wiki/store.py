"""SQLite canonical store for the page-centric LLM Wiki schema v1."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

import aiosqlite

from .errors import WikiMirrorError, WikiSchemaError, WikiStoreError
from .files import WikiFileStore
from .legacy import retire_legacy_knowledge
from .models import (
    WIKI_SCHEMA_VERSION,
    WikiLegacyBackupReceipt,
    WikiMirrorRepairReport,
    WikiSpace,
    WikiSpaceStatus,
    validate_space_id,
)

WIKI_APPLICATION_ID = 1_464_421_193  # ASCII "WIKI"
_SCHEMA_META_KEY = "schema_version"
LegacyPolicy = Literal["preserve", "retire"]

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS wiki_schema_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS wiki_spaces (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        description    TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'active',
        graph_revision INTEGER NOT NULL DEFAULT 0,
        created_at_ms  INTEGER NOT NULL,
        updated_at_ms  INTEGER NOT NULL,
        CHECK (length(name) BETWEEN 1 AND 120),
        CHECK (length(description) <= 4000),
        CHECK (status IN ('active', 'archived', 'deleting', 'failed')),
        CHECK (graph_revision >= 0),
        CHECK (updated_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_sources (
        id                       TEXT PRIMARY KEY,
        space_id                 TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        display_name             TEXT NOT NULL,
        mime_type                TEXT NOT NULL,
        size_bytes               INTEGER NOT NULL,
        source_sha256            TEXT NOT NULL,
        source_relpath           TEXT NOT NULL,
        parsed_markdown_relpath  TEXT NOT NULL DEFAULT '',
        manifest_relpath         TEXT NOT NULL DEFAULT '',
        status                   TEXT NOT NULL DEFAULT 'uploaded',
        parser_provider          TEXT NOT NULL DEFAULT '',
        parser_version           TEXT NOT NULL DEFAULT '',
        safe_error_code          TEXT NOT NULL DEFAULT '',
        created_at_ms            INTEGER NOT NULL,
        updated_at_ms            INTEGER NOT NULL,
        CHECK (display_name <> ''),
        CHECK (mime_type IN ('application/pdf', 'text/html')),
        CHECK (size_bytes > 0),
        CHECK (length(source_sha256) = 64 AND source_sha256 = lower(source_sha256)),
        CHECK (source_relpath <> ''),
        CHECK (status IN ('uploaded', 'parsing', 'parsed', 'failed', 'deleting')),
        CHECK (updated_at_ms >= created_at_ms),
        UNIQUE (space_id, source_sha256)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_artifacts (
        id                  TEXT PRIMARY KEY,
        source_id           TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE CASCADE,
        kind                TEXT NOT NULL,
        relpath             TEXT NOT NULL,
        mime_type           TEXT NOT NULL,
        size_bytes          INTEGER NOT NULL,
        sha256              TEXT NOT NULL,
        width               INTEGER,
        height              INTEGER,
        source_locator_json TEXT NOT NULL DEFAULT '{}',
        created_at_ms       INTEGER NOT NULL,
        CHECK (kind IN ('parsed_markdown', 'embedded_image', 'manifest')),
        CHECK (relpath <> ''),
        CHECK (mime_type <> ''),
        CHECK (size_bytes > 0),
        CHECK (length(sha256) = 64 AND sha256 = lower(sha256)),
        CHECK (width IS NULL OR width > 0),
        CHECK (height IS NULL OR height > 0),
        CHECK (json_valid(source_locator_json)),
        UNIQUE (source_id, relpath)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_conversations (
        id            TEXT PRIMARY KEY,
        space_id      TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        session_id    TEXT NOT NULL UNIQUE,
        title         TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active',
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        CHECK (title <> ''),
        CHECK (status IN ('active', 'archived')),
        CHECK (updated_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_change_sets (
        id                  TEXT PRIMARY KEY,
        space_id            TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        conversation_id     TEXT REFERENCES wiki_conversations(id) ON DELETE SET NULL,
        status              TEXT NOT NULL DEFAULT 'draft',
        base_graph_revision INTEGER NOT NULL,
        summary             TEXT NOT NULL DEFAULT '',
        safe_error_code     TEXT NOT NULL DEFAULT '',
        created_at_ms       INTEGER NOT NULL,
        decided_at_ms       INTEGER,
        published_at_ms     INTEGER,
        CHECK (status IN ('draft', 'awaiting_approval', 'approved', 'rejected',
                          'stale', 'failed')),
        CHECK (base_graph_revision >= 0),
        CHECK (decided_at_ms IS NULL OR decided_at_ms >= created_at_ms),
        CHECK (published_at_ms IS NULL OR published_at_ms >= created_at_ms)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_pages (
        id                  TEXT PRIMARY KEY,
        space_id            TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        slug                TEXT NOT NULL,
        title               TEXT NOT NULL,
        aliases_json        TEXT NOT NULL DEFAULT '[]',
        status              TEXT NOT NULL DEFAULT 'active',
        current_revision_id TEXT REFERENCES wiki_page_revisions(id) ON DELETE SET NULL
                                   DEFERRABLE INITIALLY DEFERRED,
        version             INTEGER NOT NULL DEFAULT 0,
        created_at_ms       INTEGER NOT NULL,
        updated_at_ms       INTEGER NOT NULL,
        CHECK (slug <> ''),
        CHECK (title <> ''),
        CHECK (json_valid(aliases_json) AND json_type(aliases_json) = 'array'),
        CHECK (status IN ('active', 'deleted')),
        CHECK (version >= 0),
        CHECK (updated_at_ms >= created_at_ms),
        UNIQUE (space_id, slug)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_page_revisions (
        id             TEXT PRIMARY KEY,
        page_id        TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        version        INTEGER NOT NULL,
        title          TEXT NOT NULL,
        markdown       TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        change_set_id  TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE RESTRICT,
        author_kind    TEXT NOT NULL,
        created_at_ms  INTEGER NOT NULL,
        CHECK (version >= 1),
        CHECK (title <> ''),
        CHECK (markdown <> ''),
        CHECK (length(content_sha256) = 64 AND content_sha256 = lower(content_sha256)),
        CHECK (author_kind IN ('agent', 'user', 'system')),
        UNIQUE (page_id, version)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_page_sources (
        page_id             TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        source_id           TEXT NOT NULL REFERENCES wiki_sources(id) ON DELETE RESTRICT,
        source_locator_json TEXT NOT NULL DEFAULT '{}',
        created_at_ms       INTEGER NOT NULL,
        CHECK (json_valid(source_locator_json)),
        PRIMARY KEY (page_id, source_id)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_edges (
        id             TEXT PRIMARY KEY,
        space_id       TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        from_page_id   TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        to_page_id     TEXT NOT NULL REFERENCES wiki_pages(id) ON DELETE CASCADE,
        relation_type  TEXT NOT NULL,
        change_set_id  TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE RESTRICT,
        created_at_ms  INTEGER NOT NULL,
        CHECK (from_page_id <> to_page_id),
        CHECK (relation_type IN ('related_to', 'references', 'extends',
                                 'contradicts', 'part_of')),
        UNIQUE (space_id, from_page_id, to_page_id, relation_type)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_change_set_items (
        id               TEXT PRIMARY KEY,
        change_set_id    TEXT NOT NULL REFERENCES wiki_change_sets(id) ON DELETE CASCADE,
        ordinal          INTEGER NOT NULL,
        operation_kind   TEXT NOT NULL,
        target_id        TEXT NOT NULL DEFAULT '',
        base_version     INTEGER,
        before_sha256    TEXT NOT NULL DEFAULT '',
        payload_json     TEXT NOT NULL,
        unified_diff     TEXT NOT NULL DEFAULT '',
        created_at_ms    INTEGER NOT NULL,
        CHECK (ordinal >= 0),
        CHECK (operation_kind IN ('page_create', 'page_update', 'page_delete',
                                  'edge_add', 'edge_delete')),
        CHECK (base_version IS NULL OR base_version >= 0),
        CHECK (before_sha256 = '' OR length(before_sha256) = 64),
        CHECK (json_valid(payload_json)),
        UNIQUE (change_set_id, ordinal)
    ) STRICT;
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_jobs (
        id              TEXT PRIMARY KEY,
        space_id        TEXT NOT NULL REFERENCES wiki_spaces(id) ON DELETE CASCADE,
        source_id       TEXT REFERENCES wiki_sources(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'queued',
        attempt         INTEGER NOT NULL DEFAULT 1,
        safe_error_code TEXT NOT NULL DEFAULT '',
        created_at_ms   INTEGER NOT NULL,
        started_at_ms   INTEGER,
        finished_at_ms  INTEGER,
        CHECK (kind IN ('parse', 'synthesize_entry_page', 'rebuild_search',
                        'rebuild_graph_projection')),
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        CHECK (attempt >= 1),
        CHECK (started_at_ms IS NULL OR started_at_ms >= created_at_ms),
        CHECK (finished_at_ms IS NULL OR finished_at_ms >= created_at_ms)
    ) STRICT;
    """,
    "CREATE INDEX IF NOT EXISTS idx_wiki_sources_space ON wiki_sources(space_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_sources_status ON wiki_sources(status);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_artifacts_source ON wiki_artifacts(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_pages_space_status ON wiki_pages(space_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page ON wiki_page_revisions(page_id, version);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_page_sources_source ON wiki_page_sources(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_edges_space ON wiki_edges(space_id);",
    (
        "CREATE INDEX IF NOT EXISTS idx_wiki_changes_space_status "
        "ON wiki_change_sets(space_id, status);"
    ),
    "CREATE INDEX IF NOT EXISTS idx_wiki_conversations_space ON wiki_conversations(space_id);",
    "CREATE INDEX IF NOT EXISTS idx_wiki_jobs_space_status ON wiki_jobs(space_id, status);",
)

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "wiki_spaces": frozenset(
        {
            "id",
            "name",
            "description",
            "status",
            "graph_revision",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_sources": frozenset(
        {
            "id",
            "space_id",
            "display_name",
            "mime_type",
            "size_bytes",
            "source_sha256",
            "source_relpath",
            "parsed_markdown_relpath",
            "manifest_relpath",
            "status",
            "parser_provider",
            "parser_version",
            "safe_error_code",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_artifacts": frozenset(
        {
            "id",
            "source_id",
            "kind",
            "relpath",
            "mime_type",
            "size_bytes",
            "sha256",
            "width",
            "height",
            "source_locator_json",
            "created_at_ms",
        }
    ),
    "wiki_pages": frozenset(
        {
            "id",
            "space_id",
            "slug",
            "title",
            "aliases_json",
            "status",
            "current_revision_id",
            "version",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_page_revisions": frozenset(
        {
            "id",
            "page_id",
            "version",
            "title",
            "markdown",
            "content_sha256",
            "change_set_id",
            "author_kind",
            "created_at_ms",
        }
    ),
    "wiki_page_sources": frozenset(
        {"page_id", "source_id", "source_locator_json", "created_at_ms"}
    ),
    "wiki_edges": frozenset(
        {
            "id",
            "space_id",
            "from_page_id",
            "to_page_id",
            "relation_type",
            "change_set_id",
            "created_at_ms",
        }
    ),
    "wiki_change_sets": frozenset(
        {
            "id",
            "space_id",
            "conversation_id",
            "status",
            "base_graph_revision",
            "summary",
            "safe_error_code",
            "created_at_ms",
            "decided_at_ms",
            "published_at_ms",
        }
    ),
    "wiki_change_set_items": frozenset(
        {
            "id",
            "change_set_id",
            "ordinal",
            "operation_kind",
            "target_id",
            "base_version",
            "before_sha256",
            "payload_json",
            "unified_diff",
            "created_at_ms",
        }
    ),
    "wiki_conversations": frozenset(
        {
            "id",
            "space_id",
            "session_id",
            "title",
            "status",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "wiki_jobs": frozenset(
        {
            "id",
            "space_id",
            "source_id",
            "kind",
            "status",
            "attempt",
            "safe_error_code",
            "created_at_ms",
            "started_at_ms",
            "finished_at_ms",
        }
    ),
}

_STATUS_TRANSITIONS: dict[WikiSpaceStatus, frozenset[WikiSpaceStatus]] = {
    "active": frozenset({"archived", "deleting", "failed"}),
    "archived": frozenset({"active", "deleting", "failed"}),
    "failed": frozenset({"active", "deleting"}),
    "deleting": frozenset(),
}


def _row_to_space(row: aiosqlite.Row) -> WikiSpace:
    return WikiSpace(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        graph_revision=row["graph_revision"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


class WikiStore:
    """Own ``wiki.db`` and the rebuildable account-scoped Wiki mirror."""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        file_store: WikiFileStore,
        owns_connection: bool,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        legacy_backup_receipt: WikiLegacyBackupReceipt | None = None,
    ) -> None:
        self._db: aiosqlite.Connection | None = connection
        self._file_store = file_store
        self._owns_connection = owns_connection
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._id_factory = id_factory or (lambda: f"space_{secrets.token_hex(12)}")
        self._legacy_backup_receipt = legacy_backup_receipt
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._startup_repair_report = WikiMirrorRepairReport()

    @classmethod
    async def open(
        cls,
        root: str | Path,
        *,
        legacy_policy: LegacyPolicy = "preserve",
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        backup_id_factory: Callable[[], str] | None = None,
    ) -> WikiStore:
        if legacy_policy not in {"preserve", "retire"}:
            raise WikiStoreError("invalid_configuration")
        file_store = WikiFileStore(root)
        try:
            await asyncio.to_thread(file_store.ensure_root)
            await asyncio.to_thread(file_store.validate_database_paths)
        except WikiStoreError:
            raise
        except OSError as exc:
            raise WikiStoreError("invalid_configuration") from exc
        backup_receipt = None
        if legacy_policy == "retire":
            backup_receipt = await asyncio.to_thread(
                retire_legacy_knowledge,
                file_store.root,
                clock_ms=clock_ms,
                id_factory=backup_id_factory,
            )
        connection = await aiosqlite.connect(
            str(file_store.database_path),
            isolation_level=None,
        )
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA trusted_schema=OFF")
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("PRAGMA synchronous=FULL")
            store = cls(
                connection=connection,
                file_store=file_store,
                owns_connection=True,
                clock_ms=clock_ms,
                id_factory=id_factory,
                legacy_backup_receipt=backup_receipt,
            )
            await store._initialize_schema()
            await connection.execute("PRAGMA journal_mode=WAL")
            store._startup_repair_report = await store.repair_space_mirrors()
        except BaseException:
            await connection.close()
            raise
        return store

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        root: str | Path,
        owns_connection: bool = False,
        clock_ms: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> WikiStore:
        if getattr(connection, "isolation_level", "") is not None:
            raise WikiStoreError("invalid_configuration")
        file_store = WikiFileStore(root)
        await asyncio.to_thread(file_store.ensure_root)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        store = cls(
            connection=connection,
            file_store=file_store,
            owns_connection=owns_connection,
            clock_ms=clock_ms,
            id_factory=id_factory,
        )
        await store._initialize_schema()
        store._startup_repair_report = await store.repair_space_mirrors()
        return store

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def file_store(self) -> WikiFileStore:
        return self._file_store

    @property
    def legacy_backup_receipt(self) -> WikiLegacyBackupReceipt | None:
        return self._legacy_backup_receipt

    @property
    def startup_repair_report(self) -> WikiMirrorRepairReport:
        return self._startup_repair_report

    async def close(self) -> None:
        connection = self._db
        self._db = None
        self._closed = True
        if self._owns_connection and connection is not None:
            await connection.close()

    def _require_db(self) -> aiosqlite.Connection:
        if self._closed or self._db is None:
            raise WikiStoreError("invalid_configuration")
        return self._db

    async def get_schema_version(self) -> int | None:
        db = self._require_db()
        async with db.execute(
            "SELECT value FROM wiki_schema_meta WHERE key = ?",
            (_SCHEMA_META_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else int(row["value"])

    async def create_space(self, *, name: str, description: str = "") -> WikiSpace:
        now = self._clock_ms()
        try:
            space = WikiSpace(
                id=self._id_factory(),
                name=name,
                description=description,
                created_at_ms=now,
                updated_at_ms=now,
            )
        except ValueError as exc:
            raise WikiStoreError("invalid_space") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    """
                    INSERT INTO wiki_spaces (
                        id, name, description, status, graph_revision,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        space.id,
                        space.name,
                        space.description,
                        space.status,
                        space.graph_revision,
                        space.created_at_ms,
                        space.updated_at_ms,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                await self._rollback_quietly(db)
                raise WikiStoreError("space_conflict") from exc
            except BaseException:
                await self._rollback_quietly(db)
                raise
        try:
            await asyncio.to_thread(self._file_store.create_or_repair_space_layout, space)
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc
        return space

    async def get_space(self, space_id: str) -> WikiSpace:
        try:
            validate_space_id(space_id)
        except ValueError as exc:
            raise WikiStoreError("invalid_identifier") from exc
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM wiki_spaces WHERE id = ?",
            (space_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiStoreError("space_not_found")
        return _row_to_space(row)

    async def list_spaces(
        self,
        *,
        statuses: Sequence[WikiSpaceStatus] | None = None,
    ) -> tuple[WikiSpace, ...]:
        db = self._require_db()
        if statuses is None:
            query = "SELECT * FROM wiki_spaces ORDER BY created_at_ms, id"
            parameters: tuple[object, ...] = ()
        else:
            if not statuses or any(status not in _STATUS_TRANSITIONS for status in statuses):
                raise WikiStoreError("invalid_space")
            unique = tuple(dict.fromkeys(statuses))
            placeholders = ",".join("?" for _ in unique)
            query = (
                f"SELECT * FROM wiki_spaces WHERE status IN ({placeholders}) "
                "ORDER BY created_at_ms, id"
            )
            parameters = tuple(unique)
        async with db.execute(query, parameters) as cursor:
            rows = await cursor.fetchall()
        return tuple(_row_to_space(row) for row in rows)

    async def update_space(
        self,
        space_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        if name is None and description is None:
            raise WikiStoreError("invalid_space")
        current = await self.get_space(space_id)
        try:
            candidate = current.model_copy(
                update={
                    "name": current.name if name is None else name,
                    "description": (
                        current.description if description is None else description
                    ),
                }
            )
            candidate = WikiSpace.model_validate(candidate.model_dump())
        except ValueError as exc:
            raise WikiStoreError("invalid_space") from exc
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                locked = _row_to_space(row)
                if (
                    expected_updated_at_ms is not None
                    and locked.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("space_conflict")
                updated_at = max(self._clock_ms(), locked.updated_at_ms + 1)
                await db.execute(
                    """
                    UPDATE wiki_spaces
                    SET name = ?, description = ?, updated_at_ms = ?
                    WHERE id = ?
                    """,
                    (candidate.name, candidate.description, updated_at, space_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        updated = await self.get_space(space_id)
        await self._sync_mirror_or_raise(updated)
        return updated

    async def set_space_status(
        self,
        space_id: str,
        status: WikiSpaceStatus,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        if status not in _STATUS_TRANSITIONS:
            raise WikiStoreError("invalid_space")
        async with self._write_lock:
            db = self._require_db()
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT * FROM wiki_spaces WHERE id = ?",
                    (space_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise WikiStoreError("space_not_found")
                current = _row_to_space(row)
                if (
                    expected_updated_at_ms is not None
                    and current.updated_at_ms != expected_updated_at_ms
                ):
                    raise WikiStoreError("space_conflict")
                if status == current.status:
                    await db.execute("COMMIT")
                    return current
                if status not in _STATUS_TRANSITIONS[current.status]:
                    raise WikiStoreError("invalid_status_transition")
                updated_at = max(self._clock_ms(), current.updated_at_ms + 1)
                await db.execute(
                    "UPDATE wiki_spaces SET status = ?, updated_at_ms = ? WHERE id = ?",
                    (status, updated_at, space_id),
                )
                await db.execute("COMMIT")
            except BaseException:
                await self._rollback_quietly(db)
                raise
        updated = await self.get_space(space_id)
        await self._sync_mirror_or_raise(updated)
        return updated

    async def delete_space(
        self,
        space_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> WikiSpace:
        """Soft-delete by entering ``deleting``; physical deletion is a later workflow."""
        return await self.set_space_status(
            space_id,
            "deleting",
            expected_updated_at_ms=expected_updated_at_ms,
        )

    async def repair_space_mirrors(self) -> WikiMirrorRepairReport:
        spaces = await self.list_spaces()
        try:
            return await asyncio.to_thread(
                self._file_store.reconcile_space_layouts,
                spaces,
            )
        except WikiStoreError:
            raise
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc

    async def _sync_mirror_or_raise(self, space: WikiSpace) -> None:
        try:
            await asyncio.to_thread(self._file_store.create_or_repair_space_layout, space)
        except Exception as exc:
            raise WikiMirrorError("mirror_failed") from exc

    async def _initialize_schema(self) -> None:
        db = self._require_db()
        await self._validate_sqlite_capabilities()
        application_id = await self._read_pragma_int("application_id")
        if application_id not in {0, WIKI_APPLICATION_ID}:
            raise WikiSchemaError("schema_incompatible")
        await db.execute(_SCHEMA_META_DDL)
        version = await self.get_schema_version()
        if version is None:
            existing = await self._user_tables()
            if existing - {"wiki_schema_meta"}:
                raise WikiSchemaError("schema_incompatible")
            await self._initialize_fresh_schema()
        elif version != WIKI_SCHEMA_VERSION:
            raise WikiSchemaError("schema_incompatible")
        elif await self._read_pragma_int("application_id") != WIKI_APPLICATION_ID:
            raise WikiSchemaError("schema_incompatible")
        await self._validate_schema()

    async def _initialize_fresh_schema(self) -> None:
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT value FROM wiki_schema_meta WHERE key = ?",
                (_SCHEMA_META_KEY,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                await db.execute("COMMIT")
                return
            for statement in _DDL_STATEMENTS:
                await db.execute(statement)
            await db.execute(
                "INSERT INTO wiki_schema_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_META_KEY, WIKI_SCHEMA_VERSION),
            )
            await db.execute(f"PRAGMA application_id = {WIKI_APPLICATION_ID}")
            await db.execute(f"PRAGMA user_version = {WIKI_SCHEMA_VERSION}")
            await db.execute("COMMIT")
        except BaseException:
            await self._rollback_quietly(db)
            raise

    async def _validate_schema(self) -> None:
        db = self._require_db()
        if await self._read_pragma_int("application_id") != WIKI_APPLICATION_ID:
            raise WikiSchemaError("schema_incompatible")
        if await self._read_pragma_int("user_version") != WIKI_SCHEMA_VERSION:
            raise WikiSchemaError("schema_incompatible")
        async with db.execute("PRAGMA foreign_keys") as cursor:
            foreign_keys = await cursor.fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise WikiSchemaError("schema_incompatible")
        tables = await self._user_tables()
        required_tables = set(_REQUIRED_COLUMNS) | {"wiki_schema_meta"}
        if not required_tables.issubset(tables):
            raise WikiSchemaError("schema_incompatible")
        if any(name.startswith("knowledge_") for name in tables):
            raise WikiSchemaError("schema_incompatible")
        for table, required_columns in _REQUIRED_COLUMNS.items():
            columns = await self._table_columns(table)
            if not required_columns.issubset(columns):
                raise WikiSchemaError("schema_incompatible")
        async with db.execute("PRAGMA quick_check") as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] != "ok":
            raise WikiSchemaError("database_corrupt")
        async with db.execute("PRAGMA foreign_key_check") as cursor:
            violation = await cursor.fetchone()
        if violation is not None:
            raise WikiSchemaError("database_corrupt")

    async def _validate_sqlite_capabilities(self) -> None:
        db = self._require_db()
        try:
            async with db.execute(
                "SELECT sqlite_version(), json_valid('[]'), json_type('[]')"
            ) as cursor:
                row = await cursor.fetchone()
        except aiosqlite.Error as exc:
            raise WikiSchemaError("schema_incompatible") from exc
        if row is None:
            raise WikiSchemaError("schema_incompatible")
        try:
            version = tuple(int(part) for part in str(row[0]).split(".")[:3])
        except ValueError as exc:
            raise WikiSchemaError("schema_incompatible") from exc
        if len(version) != 3 or version < (3, 37, 0) or int(row[1]) != 1 or row[2] != "array":
            raise WikiSchemaError("schema_incompatible")

    async def _user_tables(self) -> set[str]:
        db = self._require_db()
        async with db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row["name"]) for row in rows}

    async def _table_columns(self, table: str) -> frozenset[str]:
        if table not in _REQUIRED_COLUMNS:
            raise WikiSchemaError("schema_incompatible")
        db = self._require_db()
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return frozenset(str(row["name"]) for row in rows)

    async def _read_pragma_int(self, name: str) -> int:
        if name not in {"application_id", "user_version"}:
            raise WikiSchemaError("schema_incompatible")
        db = self._require_db()
        async with db.execute(f"PRAGMA {name}") as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise WikiSchemaError("schema_incompatible")
        return int(row[0])

    @staticmethod
    async def _rollback_quietly(db: aiosqlite.Connection) -> None:
        try:
            await db.execute("ROLLBACK")
        except aiosqlite.Error:
            pass


__all__ = [
    "LegacyPolicy",
    "WIKI_APPLICATION_ID",
    "WikiStore",
]

"""Independent FTS5 search over the canonical SQLite Session database."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite

from ...agent.harness.session.types import (
    SessionMetadata,
    SessionSearchHit,
    SessionSearchOptions,
)
from .database import database_for
from .repo import SessionSerializationError


class SQLiteSessionSearch:
    """Read-only SDK search; index initialization belongs to the Session writer."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._read_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self._injected_connection = connection

    async def _open(self) -> tuple[aiosqlite.Connection, bool]:
        if self._injected_connection is not None:
            return self._injected_connection, False
        db = await aiosqlite.connect(
            self._read_uri, uri=True,
        )
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        return db, True

    @staticmethod
    async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def initialize(self, db: aiosqlite.Connection) -> None:
        existed = await self._table_exists(db, "session_search_fts")
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS session_search_fts USING fts5("
                "payload_json, content='session_entries', content_rowid='rowid', "
                "tokenize='trigram remove_diacritics 1')"
            )
            await db.execute(
                "CREATE TRIGGER IF NOT EXISTS session_search_fts_ai "
                "AFTER INSERT ON session_entries BEGIN "
                "INSERT INTO session_search_fts(rowid, payload_json) "
                "VALUES (new.rowid, new.payload_json); END"
            )
            await db.execute(
                "CREATE TRIGGER IF NOT EXISTS session_search_fts_ad "
                "AFTER DELETE ON session_entries BEGIN "
                "INSERT INTO session_search_fts(session_search_fts, rowid, payload_json) "
                "VALUES ('delete', old.rowid, old.payload_json); END"
            )
            await db.execute(
                "CREATE TRIGGER IF NOT EXISTS session_search_fts_au "
                "AFTER UPDATE OF payload_json ON session_entries BEGIN "
                "INSERT INTO session_search_fts(session_search_fts, rowid, payload_json) "
                "VALUES ('delete', old.rowid, old.payload_json); "
                "INSERT INTO session_search_fts(rowid, payload_json) "
                "VALUES (new.rowid, new.payload_json); END"
            )
            if not existed:
                await db.execute(
                    "INSERT INTO session_search_fts(session_search_fts) VALUES('rebuild')"
                )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    @staticmethod
    def _metadata(row: aiosqlite.Row) -> SessionMetadata:
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError as error:
            raise SessionSerializationError(
                f"session {row['session_id']!r} metadata JSON 解析失败"
            ) from error
        if not isinstance(metadata, dict):
            raise SessionSerializationError("session metadata must be a JSON object")
        return SessionMetadata(
            id=str(row["session_id"]),
            created_at=int(row["session_created_at"]),
            updated_at=int(row["session_updated_at"]),
            parent_session_id=(
                str(row["parent_session_id"])
                if row["parent_session_id"] is not None
                else None
            ),
            title=str(row["title"]),
            metadata=metadata,
        )

    async def search(
        self,
        text: str,
        options: SessionSearchOptions | None = None,
    ) -> AsyncIterator[SessionSearchHit]:
        resolved = options or SessionSearchOptions()
        query_text = text.strip()
        if not query_text or (resolved.limit is not None and resolved.limit <= 0):
            return
        if resolved.entry_types is not None and not resolved.entry_types:
            return
        if resolved.cancel_event is not None and resolved.cancel_event.is_set():
            raise asyncio.CancelledError
        db, owns = await self._open()
        coordinator = database_for(db)
        try:
            async with coordinator.operation_lock:
                predicates = ["session_search_fts MATCH ?"]
                values: list[object] = [f'"{query_text.replace(chr(34), chr(34) * 2)}"']
                if resolved.entry_types is not None:
                    placeholders = ",".join("?" for _ in resolved.entry_types)
                    predicates.append(f"e.entry_type IN ({placeholders})")
                    values.extend(resolved.entry_types)
                values.append(resolved.limit if resolved.limit is not None else -1)
                cursor = await db.execute(
                    "SELECT s.id AS session_id, s.created_at AS session_created_at, "
                    "s.updated_at AS session_updated_at, s.parent_session_id, "
                    "s.title, s.metadata_json, e.id AS entry_id, "
                    "e.created_at AS entry_timestamp, bm25(session_search_fts) AS score "
                    "FROM session_search_fts JOIN session_entries AS e "
                    "ON e.rowid = session_search_fts.rowid "
                    "JOIN sessions AS s ON s.id = e.session_id WHERE "
                    + " AND ".join(predicates)
                    + " ORDER BY score LIMIT ?",
                    values,
                )
                rows = list(await cursor.fetchall())
                await cursor.close()
            for row in rows:
                if resolved.cancel_event is not None and resolved.cancel_event.is_set():
                    raise asyncio.CancelledError
                yield SessionSearchHit(
                    session_id=str(row["session_id"]),
                    entry_id=str(row["entry_id"]),
                    timestamp=int(row["entry_timestamp"]),
                    score=float(row["score"]),
                    metadata=self._metadata(row),
                )
        finally:
            if owns:
                await db.close()


def create_sqlite_session_search(
    db_path: str | Path,
    *,
    connection: aiosqlite.Connection | None = None,
) -> SQLiteSessionSearch:
    return SQLiteSessionSearch(db_path, connection=connection)


__all__ = ["SQLiteSessionSearch", "create_sqlite_session_search"]

"""Bounded, body-only history projection and genuinely read-only retrieval.

The writer initializes and maintains the derived index. Readers never migrate,
bootstrap, attach databases, or accept SQL/paths from a model.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite


async def initialize_history_index(db: aiosqlite.Connection) -> None:
    """Idempotent writer-owned initialization; triggers share message commits."""
    cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE name='session_history_text'")
    existed = await cursor.fetchone() is not None
    await cursor.close()
    await db.execute(
        "CREATE TABLE IF NOT EXISTS session_history_text ("
        "entry_id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL, body TEXT NOT NULL, "
        "FOREIGN KEY(entry_id) REFERENCES session_entries(id) ON DELETE CASCADE)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_session ON session_history_text(session_id)"
    )
    await db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS session_history_fts USING fts5("
        "body, content='session_history_text', content_rowid='rowid', tokenize='trigram')"
    )

    # Only public text blocks, never thinking, metadata, arbitrary custom entries,
    # tool argument payloads or attachment bytes. Both historical encodings work.
    def projection(prefix: str) -> str:
        return (
            "COALESCE((SELECT group_concat(json_extract(b.value,'$.text'), char(10)) "
            "FROM json_each(CASE WHEN json_valid(" + prefix + "content_json) THEN "
            "COALESCE(json_extract(" + prefix + "content_json,'$.data.content'), "
            "json_extract(" + prefix + "content_json,'$.content')) ELSE '[]' END) b "
            "WHERE json_extract(b.value,'$.type')='text'), '')"
        )

    await db.execute(
        "CREATE TRIGGER IF NOT EXISTS session_history_ai AFTER INSERT ON session_entries "
        "WHEN new.entry_type='message' AND new.role IN ('user','assistant','toolResult') "
        "BEGIN INSERT INTO session_history_text(entry_id,session_id,body) VALUES "
        "(new.id,new.session_id," + projection("new.") + "); END"
    )
    for suffix, timing, values in (
        ("ai", "INSERT", "INSERT INTO session_history_fts(rowid,body) VALUES(new.rowid,new.body);"),
        (
            "ad",
            "DELETE",
            "INSERT INTO session_history_fts(session_history_fts,rowid,body) "
            "VALUES('delete',old.rowid,old.body);",
        ),
        (
            "au",
            "UPDATE",
            "INSERT INTO session_history_fts(session_history_fts,rowid,body) "
            "VALUES('delete',old.rowid,old.body); INSERT INTO session_history_fts(rowid,body) "
            "VALUES(new.rowid,new.body);",
        ),
    ):
        await db.execute(
            f"CREATE TRIGGER IF NOT EXISTS session_history_fts_{suffix} AFTER {timing} "
            f"ON session_history_text BEGIN {values} END"
        )
    if not existed:
        await db.execute(
            "INSERT INTO session_history_text(entry_id,session_id,body) "
            "SELECT id,session_id," + projection("e.") + " FROM session_entries e "
            "WHERE entry_type='message' AND role IN ('user','assistant','toolResult')"
        )


class HistoryReadError(ValueError):
    pass


_ACTIVE_CTE = """WITH RECURSIVE active(id,parent_id) AS (
 SELECT e.id,e.parent_id FROM session_entries e JOIN session_lanes l
 ON l.session_id=e.session_id AND l.leaf_entry_id=e.id
 JOIN sessions s ON s.id=l.session_id AND s.active_lane=l.name WHERE s.id=?
 UNION ALL SELECT e.id,e.parent_id FROM session_entries e JOIN active a
 ON e.id=a.parent_id WHERE e.session_id=?
) """


class SQLiteHistoryReader:
    def __init__(self, db_path: str | Path) -> None:
        self._uri = Path(db_path).resolve().as_uri() + "?mode=ro"

    @asynccontextmanager
    async def connection(
        self, signal: asyncio.Event | None = None
    ) -> AsyncIterator[aiosqlite.Connection]:
        deadline = time.monotonic() + 2.0
        async with aiosqlite.connect(self._uri, uri=True, timeout=1.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.set_progress_handler(
                lambda: int(time.monotonic() >= deadline or bool(signal and signal.is_set())),
                1000,
            )
            if signal and signal.is_set():
                raise asyncio.CancelledError
            try:
                # All pages in one read operation use the same WAL snapshot.
                await db.execute("BEGIN")
                yield db
            except aiosqlite.OperationalError as exc:
                if signal and signal.is_set():
                    raise asyncio.CancelledError from None
                raise HistoryReadError("history_query_unavailable_or_timed_out") from exc

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 5,
        before_seq: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query or len(query) > 200 or not 1 <= limit <= 20:
            raise HistoryReadError("invalid_history_query")
        async with self.connection(signal) as db:
            predicate = "instr(lower(h.body),lower(?))>0"
            match_value = query
            if len(query) >= 3:
                predicate = "h.rowid IN (SELECT rowid FROM session_history_fts WHERE body MATCH ?)"
                match_value = '"' + query.replace('"', '""') + '"'
            cursor = await db.execute(
                _ACTIVE_CTE + "SELECT e.id,e.seq,e.role,e.created_at, "
                "e.id IN (SELECT id FROM active) AS is_active, "
                "substr(h.body,max(1,instr(lower(h.body),lower(?))-160),800) AS body "
                "FROM session_history_text h JOIN session_entries e ON e.id=h.entry_id "
                "WHERE h.session_id=? AND e.session_id=? AND "
                + predicate
                + " AND (? IS NULL OR e.seq<?) ORDER BY e.seq DESC LIMIT ?",
                (
                    session_id,
                    session_id,
                    query,
                    session_id,
                    session_id,
                    match_value,
                    before_seq,
                    before_seq,
                    limit + 1,
                ),
            )
            rows = list(await cursor.fetchall())
            await cursor.close()
        hits = [self._public(row) for row in rows[:limit]]
        return {
            "hits": hits,
            "next_before_seq": rows[limit - 1]["seq"] if len(rows) > limit else None,
            "trust": "untrusted_historical_data",
            "order": "newest_first",
            "notice": (
                "Archived entries may be compacted history OR superseded answers; "
                "verify current decisions."
            ),
        }

    async def read(
        self,
        session_id: str,
        entry_id: str,
        *,
        offset: int = 0,
        max_chars: int = 6000,
        signal: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        if offset < 0 or offset > 10_000_000 or not 1 <= max_chars <= 12000:
            raise HistoryReadError("invalid_history_page")
        async with self.connection(signal) as db:
            cursor = await db.execute(
                _ACTIVE_CTE + "SELECT e.id,e.seq,e.role,e.created_at,e.parent_id, "
                "e.id IN (SELECT id FROM active) AS is_active, "
                "substr(h.body,?,?) AS body,length(h.body) AS total_chars "
                "FROM session_history_text h JOIN session_entries e ON e.id=h.entry_id "
                "WHERE e.session_id=? AND h.session_id=? AND e.id=?",
                (session_id, session_id, offset + 1, max_chars, session_id, session_id, entry_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise HistoryReadError("history_entry_not_found")
            # Neighbor IDs never cross sessions and do not imply all children are current.
            cursor = await db.execute(
                "SELECT id FROM session_entries WHERE session_id=? AND parent_id=? "
                "AND entry_type='message' ORDER BY seq LIMIT 10",
                (session_id, entry_id),
            )
            children = [str(item["id"]) for item in await cursor.fetchall()]
            await cursor.close()
        result = self._public(row)
        result.update(
            {
                "offset": offset,
                "next_offset": offset + max_chars
                if offset + max_chars < row["total_chars"]
                else None,
                "parent_entry_id": row["parent_id"],
                "child_entry_ids": children,
                "trust": "untrusted_historical_data",
            }
        )
        return result

    @staticmethod
    def _public(row: aiosqlite.Row) -> dict[str, Any]:
        # Deliberately no raw payload_json, file paths, thinking or metadata.
        text = re.sub(
            r"(?i)\b(api[_ -]?key|password|passwd|token|secret)(\s*[:=]\s*)([^\s`]+)",
            r"\1\2[REDACTED]",
            str(row["body"]),
        )
        text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
        return {
            "entry_id": str(row["id"]),
            "seq": int(row["seq"]),
            "role": str(row["role"]),
            "timestamp": int(row["created_at"]),
            "text": text,
            "branch_status": "active" if row["is_active"] else "archived_or_superseded",
        }

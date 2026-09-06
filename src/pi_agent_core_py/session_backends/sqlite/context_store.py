"""Durable context projections and immutable, Session-scoped tool outputs.

These records never replace messages or move a lane. Compaction changes the
model's view, not the immutable evidence tree used by Memory and history tools.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import aiosqlite

from ...agent.messages import AgentMessage
from .database import database_for
from .repo import SQLiteSessionStore

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024


class ContextStoreError(ValueError):
    """Stable, path-free persistence error for the application boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ContextSnapshot:
    session_id: str
    lane: str
    leaf_id: str | None
    entry_ids: list[str]
    messages: list[AgentMessage]


def _encode_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ContextStoreError("invalid_context_payload")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContextStoreError("invalid_context_payload") from exc
    try:
        encoded_size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ContextStoreError("invalid_context_payload") from exc
    if encoded_size > MAX_PAYLOAD_BYTES:
        raise ContextStoreError("context_payload_too_large")
    return encoded


class SQLiteContextStore:
    def __init__(self, repository: SQLiteSessionStore) -> None:
        self.repository = repository
        self._initialized = False

    @asynccontextmanager
    async def _operation(self, *, write: bool = False) -> AsyncIterator[aiosqlite.Connection]:
        db = self.repository.connection
        if db is None:
            raise ContextStoreError("context_database_unavailable")
        coordinator = database_for(db)
        async with coordinator.operation():
            try:
                if write:
                    async with coordinator.transaction() as transaction:
                        yield transaction
                else:
                    # A snapshot must also be consistent with another connection.
                    await db.execute("BEGIN")
                    try:
                        yield db
                    finally:
                        await db.rollback()
            except BaseException:
                # Also cover cancellation while BEGIN is queued in aiosqlite,
                # before the shared transaction helper enters its try block.
                # Unconditional: BEGIN may still be queued when cancellation
                # arrives, so checking in_transaction here would race its worker.
                await db.rollback()
                raise

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ContextStoreError("context_store_uninitialized")

    async def initialize(self) -> None:
        """Run once at startup; recover incomplete publications atomically.

        Claiming each affected Session's writer lease prevents a second live
        process from declaring another writer's prepared work interrupted.
        """
        async with self._operation(write=True) as db:
            if self._initialized:
                return
            await db.execute(
                "CREATE TABLE IF NOT EXISTS session_context_records ("
                "id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) "
                "ON DELETE CASCADE, lane TEXT NOT NULL, base_leaf_id TEXT, "
                "status TEXT NOT NULL CHECK(status IN "
                "('prepared','committed','failed','interrupted')), "
                "prepared_payload_json TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, error_code TEXT)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_session "
                "ON session_context_records(session_id,created_at)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS session_context_settings ("
                "session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, "
                "auto_compact INTEGER NOT NULL CHECK(auto_compact IN (0,1)))"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS session_tool_outputs ("
                "ref TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) "
                "ON DELETE CASCADE, request_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, "
                "body TEXT NOT NULL, sha256 TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "UNIQUE(session_id,request_id,tool_call_id,sha256))"
            )
            async with db.execute(
                "SELECT DISTINCT session_id FROM session_context_records WHERE status='prepared'"
            ) as cursor:
                sessions = [str(row[0]) for row in await cursor.fetchall()]
            for session_id in sessions:
                await self.repository._ensure_writer_lease(session_id)
                await db.execute(
                    "UPDATE session_context_records SET status='interrupted', "
                    "error_code='startup_interrupted', updated_at=? "
                    "WHERE session_id=? AND status='prepared'",
                    (int(time.time() * 1000), session_id),
                )
        self._initialized = True

    async def snapshot(self, session_id: str) -> ContextSnapshot:
        self._require_initialized()
        async with self._operation():
            lane = await self.repository._active_lane_name(session_id)
            lane_row = await self.repository._lane_row(session_id, lane)
            leaf = lane_row["leaf_entry_id"]
            rows = await self.repository._entry_path_rows(session_id, leaf)
            entries = await self.repository._rows_to_entries(session_id, rows)
            messages = [(entry.id, entry.message) for entry in entries if entry.message is not None]
            return ContextSnapshot(
                session_id=session_id,
                lane=lane,
                leaf_id=leaf,
                entry_ids=[entry_id for entry_id, _ in messages],
                messages=[message for _, message in messages],
            )

    async def _assert_base(self, session_id: str, leaf: str | None, lane: str | None = None) -> str:
        current_lane = await self.repository._active_lane_name(session_id)
        current = await self.repository._lane_row(session_id, current_lane)
        if current["leaf_entry_id"] != leaf or (lane is not None and lane != current_lane):
            raise ContextStoreError("context_source_changed")
        return current_lane

    async def begin(
        self, session_id: str, base_leaf_id: str | None, payload: dict[str, Any]
    ) -> str:
        self._require_initialized()
        encoded = _encode_payload(payload)
        record_id = "ctx-" + uuid.uuid4().hex
        now = int(time.time() * 1000)
        async with self._operation(write=True) as db:
            await self.repository._ensure_writer_lease(session_id)
            lane = await self._assert_base(session_id, base_leaf_id)
            await db.execute(
                "INSERT INTO session_context_records "
                "(id,session_id,lane,base_leaf_id,status,prepared_payload_json,payload_json,"
                "created_at,updated_at) VALUES (?,?,?,?,'prepared',?,?,?,?)",
                (record_id, session_id, lane, base_leaf_id, encoded, encoded, now, now),
            )
        return record_id

    @staticmethod
    def _public_record(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "base_leaf_id": row["base_leaf_id"],
            "lane": str(row["lane"]),
            "payload": json.loads(row["payload_json"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "error_code": row["error_code"],
        }

    @staticmethod
    async def _record(db: aiosqlite.Connection, session_id: str, record_id: str) -> aiosqlite.Row:
        async with db.execute(
            "SELECT * FROM session_context_records WHERE session_id=? AND id=?",
            (session_id, record_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ContextStoreError("context_record_not_found")
        return row

    async def commit(
        self, session_id: str, record_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_initialized()
        encoded = _encode_payload(payload)
        async with self._operation(write=True) as db:
            await self.repository._ensure_writer_lease(session_id)
            row = await self._record(db, session_id, record_id)
            if row["status"] == "committed" and row["payload_json"] == encoded:
                return self._public_record(row)
            if row["status"] != "prepared":
                raise ContextStoreError("context_record_not_prepared")
            await self._assert_base(session_id, row["base_leaf_id"], row["lane"])
            await db.execute(
                "UPDATE session_context_records SET status='committed',payload_json=?,updated_at=? "
                "WHERE session_id=? AND id=?",
                (encoded, int(time.time() * 1000), session_id, record_id),
            )
            return self._public_record(await self._record(db, session_id, record_id))

    async def fail(self, session_id: str, record_id: str, error_code: str) -> None:
        self._require_initialized()
        if not isinstance(error_code, str) or not error_code or len(error_code) > 128:
            raise ContextStoreError("invalid_context_error_code")
        async with self._operation(write=True) as db:
            await self.repository._ensure_writer_lease(session_id)
            await self._record(db, session_id, record_id)
            # Never turn a successful publication into a failure on a retry.
            await db.execute(
                "UPDATE session_context_records SET status='failed',error_code=?,updated_at=? "
                "WHERE session_id=? AND id=? AND status='prepared'",
                (error_code, int(time.time() * 1000), session_id, record_id),
            )

    async def records(self, session_id: str) -> list[dict[str, Any]]:
        self._require_initialized()
        async with self._operation() as db:
            await self.repository._require_session(session_id)
            async with db.execute(
                "SELECT * FROM session_context_records WHERE session_id=? "
                "ORDER BY created_at DESC,rowid DESC",
                (session_id,),
            ) as cursor:
                return [self._public_record(row) for row in await cursor.fetchall()]

    async def settings(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        async with self._operation() as db:
            await self.repository._require_session(session_id)
            async with db.execute(
                "SELECT auto_compact FROM session_context_settings WHERE session_id=?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            return {"auto_compact": bool(row[0]) if row is not None else True}

    async def set_settings(self, session_id: str, auto_compact: bool) -> dict[str, Any]:
        self._require_initialized()
        if not isinstance(auto_compact, bool):
            raise ContextStoreError("invalid_auto_compact")
        async with self._operation(write=True) as db:
            await self.repository._ensure_writer_lease(session_id)
            await db.execute(
                "INSERT INTO session_context_settings(session_id,auto_compact) VALUES (?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET auto_compact=excluded.auto_compact",
                (session_id, int(auto_compact)),
            )
        return {"auto_compact": auto_compact}

    async def put_tool_output(
        self, session_id: str, request_id: str, tool_call_id: str, body: str
    ) -> str:
        """Publish before returning a preview; no future transcript ID is needed."""
        self._require_initialized()
        if not all(
            isinstance(value, str) and 0 < len(value) <= 512 for value in (request_id, tool_call_id)
        ) or not isinstance(body, str):
            raise ContextStoreError("invalid_tool_output")
        # SQLite TEXT length/substr stop at NUL. Such binary-like outputs must
        # use an artifact, otherwise a text page could silently lose its suffix.
        if "\x00" in body:
            raise ContextStoreError("invalid_tool_output")
        try:
            encoded = body.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContextStoreError("invalid_tool_output") from exc
        if len(encoded) > MAX_TOOL_OUTPUT_BYTES:
            raise ContextStoreError("tool_output_too_large")
        digest = sha256(encoded).hexdigest()
        async with self._operation(write=True) as db:
            await self.repository._ensure_writer_lease(session_id)
            async with db.execute(
                "SELECT ref FROM session_tool_outputs "
                "WHERE session_id=? AND request_id=? AND tool_call_id=? AND sha256=?",
                (session_id, request_id, tool_call_id, digest),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                return str(existing[0])
            ref = "output-" + uuid.uuid4().hex
            await db.execute(
                "INSERT INTO session_tool_outputs "
                "(ref,session_id,request_id,tool_call_id,body,sha256,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (ref, session_id, request_id, tool_call_id, body, digest, int(time.time() * 1000)),
            )
        return ref

    async def find_tool_output(
        self, session_id: str, request_id: str | None, tool_call_id: str, body_sha256: str
    ) -> str | None:
        """Find an already-published handle without writing during budget previews."""
        self._require_initialized()
        async with self._operation() as db:
            await self.repository._require_session(session_id)
            async with db.execute(
                "SELECT ref FROM session_tool_outputs "
                "WHERE session_id=? AND (? IS NULL OR request_id=?) "
                "AND tool_call_id=? AND sha256=? ORDER BY ref LIMIT 1",
                (session_id, request_id, request_id, tool_call_id, body_sha256),
            ) as cursor:
                row = await cursor.fetchone()
            return str(row[0]) if row is not None else None

    async def read_tool_output(
        self, session_id: str, ref: str, offset: int = 0, max_chars: int = 6000
    ) -> dict[str, Any]:
        self._require_initialized()
        if (
            not isinstance(ref, str)
            or not 0 < len(ref) <= 512
            or type(offset) is not int
            or type(max_chars) is not int
            or not 0 <= offset <= MAX_TOOL_OUTPUT_BYTES
            or not 1 <= max_chars <= 12_000
        ):
            raise ContextStoreError("invalid_tool_output_page")
        async with self._operation() as db:
            await self.repository._require_session(session_id)
            async with db.execute(
                "SELECT substr(body,?,?) AS text,length(body) AS total_chars,sha256 "
                "FROM session_tool_outputs WHERE session_id=? AND ref=?",
                (offset + 1, max_chars, session_id, ref),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ContextStoreError("tool_output_not_found")
            total = int(row["total_chars"])
            return {
                "ref": ref,
                "text": str(row["text"]),
                "offset": offset,
                "next_offset": offset + max_chars if offset + max_chars < total else None,
                "total_chars": total,
                "sha256": str(row["sha256"]),
                "status": "stored",
                "trust": "untrusted_tool_output",
            }

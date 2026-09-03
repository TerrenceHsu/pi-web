"""Immutable Session entry row operations."""

from __future__ import annotations

import aiosqlite


async def id_exists_in_entries(db: aiosqlite.Connection, entry_id: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM session_entries WHERE id = ? LIMIT 1", (entry_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def read_entry_row(
    db: aiosqlite.Connection, session_id: str, entry_id: str
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT id, parent_id, entry_type, payload_json, global_seq, created_at "
        "FROM session_entries WHERE session_id = ? AND id = ?",
        (session_id, entry_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def delete_entry_rows(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM session_entries WHERE session_id = ?", (session_id,))


__all__ = ["delete_entry_rows", "id_exists_in_entries", "read_entry_row"]

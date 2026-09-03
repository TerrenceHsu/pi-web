"""Append-only Session record row operations."""

from __future__ import annotations

import aiosqlite


async def id_exists_in_records(
    db: aiosqlite.Connection, session_id: str, record_id: str
) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM session_records WHERE session_id = ? AND id = ? LIMIT 1",
        (session_id, record_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def delete_record_rows(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM session_records WHERE session_id = ?", (session_id,))


__all__ = ["delete_record_rows", "id_exists_in_records"]

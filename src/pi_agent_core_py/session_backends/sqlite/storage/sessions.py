"""Session catalog row operations."""

from __future__ import annotations

import aiosqlite


async def session_exists(db: aiosqlite.Connection, session_id: str) -> bool:
    cursor = await db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def read_session_row(
    db: aiosqlite.Connection, session_id: str
) -> aiosqlite.Row | None:
    cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def read_session_rows(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
    rows = list(await cursor.fetchall())
    await cursor.close()
    return rows


async def delete_session_row(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


__all__ = [
    "delete_session_row",
    "read_session_row",
    "read_session_rows",
    "session_exists",
]

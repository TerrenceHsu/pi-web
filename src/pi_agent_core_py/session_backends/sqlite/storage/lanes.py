"""Session lane row operations."""

from __future__ import annotations

import aiosqlite


async def read_lane_row(
    db: aiosqlite.Connection, session_id: str, lane: str
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT session_id, name, leaf_entry_id, open_operation_id, "
        "created_at, updated_at FROM session_lanes "
        "WHERE session_id = ? AND name = ?",
        (session_id, lane),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def delete_lane_rows(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM session_lane_moves WHERE session_id = ?", (session_id,))
    await db.execute("DELETE FROM session_lanes WHERE session_id = ?", (session_id,))


__all__ = ["delete_lane_rows", "read_lane_row"]

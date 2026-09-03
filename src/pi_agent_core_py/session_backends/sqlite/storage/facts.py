"""Append-only global fact row operations."""

from __future__ import annotations

import aiosqlite


async def read_latest_fact(
    db: aiosqlite.Connection,
    session_id: str,
    kind: str,
    key: str | None,
) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT session_id, seq, kind, key, value_json, timestamp "
        "FROM session_global_facts WHERE session_id = ? AND kind = ? "
        "AND key IS ? ORDER BY seq DESC LIMIT 1",
        (session_id, kind, key),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def delete_fact_rows(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute(
        "DELETE FROM session_global_facts WHERE session_id = ?", (session_id,)
    )


__all__ = ["delete_fact_rows", "read_latest_fact"]

"""Per-Session shared sequence allocation."""

from __future__ import annotations

import aiosqlite


async def create_sequence(
    db: aiosqlite.Connection, session_id: str, *, next_seq: int = 1
) -> None:
    await db.execute(
        "INSERT INTO session_sequences (session_id, next_seq) VALUES (?, ?)",
        (session_id, next_seq),
    )


async def read_next_sequence(db: aiosqlite.Connection, session_id: str) -> int:
    cursor = await db.execute(
        "SELECT next_seq FROM session_sequences WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise RuntimeError(f"missing sequence row for session {session_id!r}")
    return int(row["next_seq"])


async def allocate_sequence(db: aiosqlite.Connection, session_id: str) -> int:
    cursor = await db.execute(
        "UPDATE session_sequences SET next_seq = next_seq + 1 "
        "WHERE session_id = ? RETURNING next_seq - 1 AS seq",
        (session_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise RuntimeError(f"missing sequence row for session {session_id!r}")
    return int(row["seq"])


__all__ = ["allocate_sequence", "create_sequence", "read_next_sequence"]

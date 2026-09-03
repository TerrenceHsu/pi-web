"""Materialized Session statistics."""

from __future__ import annotations

import aiosqlite

from ....agent.harness.session.types import SessionStats


async def create_stats(
    db: aiosqlite.Connection, session_id: str, *, message_count: int = 0
) -> None:
    await db.execute(
        "INSERT INTO session_stats "
        "(session_id, message_count, cached_tokens, uncached_tokens, "
        "total_tokens, cost_total) VALUES (?, ?, 0, 0, 0, 0)",
        (session_id, message_count),
    )


async def read_stats(db: aiosqlite.Connection, session_id: str) -> SessionStats:
    cursor = await db.execute(
        "SELECT message_count, cached_tokens, uncached_tokens, total_tokens, "
        "cost_total FROM session_stats WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise RuntimeError(f"missing stats row for session {session_id!r}")
    return SessionStats(
        message_count=int(row["message_count"]),
        cached_tokens=float(row["cached_tokens"]),
        uncached_tokens=float(row["uncached_tokens"]),
        total_tokens=float(row["total_tokens"]),
        cost_total=float(row["cost_total"]),
    )


async def increment_messages(
    db: aiosqlite.Connection, session_id: str, *, count: int = 1
) -> None:
    cursor = await db.execute(
        "UPDATE session_stats SET message_count = message_count + ? "
        "WHERE session_id = ?",
        (count, session_id),
    )
    if cursor.rowcount != 1:
        await cursor.close()
        raise RuntimeError(f"missing stats row for session {session_id!r}")
    await cursor.close()


async def add_usage(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    cached_tokens: float,
    uncached_tokens: float,
    total_tokens: float,
    cost_total: float,
) -> None:
    cursor = await db.execute(
        "UPDATE session_stats SET cached_tokens = cached_tokens + ?, "
        "uncached_tokens = uncached_tokens + ?, total_tokens = total_tokens + ?, "
        "cost_total = cost_total + ? WHERE session_id = ?",
        (
            cached_tokens,
            uncached_tokens,
            total_tokens,
            cost_total,
            session_id,
        ),
    )
    if cursor.rowcount != 1:
        await cursor.close()
        raise RuntimeError(f"missing stats row for session {session_id!r}")
    await cursor.close()


__all__ = ["add_usage", "create_stats", "increment_messages", "read_stats"]

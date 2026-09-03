"""Fenced, expiring per-Session writer leases."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import aiosqlite

from ....agent.harness.session.types import SessionBackendError


class WriterLeaseError(SessionBackendError):
    def __init__(self, message: str) -> None:
        super().__init__("storage", message)


@dataclass(slots=True)
class WriterLease:
    owner_id: str
    fence: int
    expires_at_ms: int


async def claim_writer_lease(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    now_ms: int,
    ttl_ms: int,
    owner_id: str | None = None,
) -> WriterLease:
    resolved_owner = owner_id or uuid4().hex
    cursor = await db.execute(
        "INSERT INTO writer_leases (session_id, owner_id, fence, expires_at_ms) "
        "VALUES (?, ?, 1, ?) ON CONFLICT(session_id) DO UPDATE SET "
        "owner_id = excluded.owner_id, fence = writer_leases.fence + 1, "
        "expires_at_ms = excluded.expires_at_ms "
        "WHERE writer_leases.expires_at_ms <= ? "
        "RETURNING owner_id, fence, expires_at_ms",
        (session_id, resolved_owner, now_ms + ttl_ms, now_ms),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        raise WriterLeaseError(f"session {session_id!r} already has an active writer")
    return WriterLease(
        owner_id=str(row["owner_id"]),
        fence=int(row["fence"]),
        expires_at_ms=int(row["expires_at_ms"]),
    )


async def renew_writer_lease(
    db: aiosqlite.Connection,
    session_id: str,
    lease: WriterLease,
    *,
    now_ms: int,
    ttl_ms: int,
) -> bool:
    expires_at = now_ms + ttl_ms
    cursor = await db.execute(
        "UPDATE writer_leases SET expires_at_ms = ? WHERE session_id = ? "
        "AND owner_id = ? AND fence = ? AND expires_at_ms > ?",
        (expires_at, session_id, lease.owner_id, lease.fence, now_ms),
    )
    renewed = cursor.rowcount == 1
    await cursor.close()
    if renewed:
        lease.expires_at_ms = expires_at
    return renewed


async def release_writer_lease(
    db: aiosqlite.Connection, session_id: str, lease: WriterLease
) -> None:
    await db.execute(
        "DELETE FROM writer_leases WHERE session_id = ? "
        "AND owner_id = ? AND fence = ?",
        (session_id, lease.owner_id, lease.fence),
    )


__all__ = [
    "WriterLease",
    "WriterLeaseError",
    "claim_writer_lease",
    "release_writer_lease",
    "renew_writer_lease",
]

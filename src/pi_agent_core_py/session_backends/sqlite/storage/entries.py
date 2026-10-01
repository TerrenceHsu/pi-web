"""Immutable Session entry row operations."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ....agent.messages import AssistantMessage
from ..branch_cache import append_entry_to_branch_cache, rebuild_branch_cache
from .session_sequences import allocate_sequence
from .session_stats import add_usage, increment_messages


async def _record_projection(
    db: aiosqlite.Connection, *, session_id: str, entry_id: str,
    parent_id: str | None, message_id: str, role: str, created_at: int,
    entry_type: str, content_json: str, payload: dict[str, Any],
) -> int:
    global_seq = await allocate_sequence(db, session_id)
    await db.execute(
        "INSERT INTO session_log (session_id, seq, kind, item_id, timestamp, payload_json) "
        "VALUES (?, ?, 'entry', ?, ?, ?)",
        (session_id, global_seq, entry_id, created_at, json.dumps({
            "id": entry_id, "type": entry_type, "parent_id": parent_id,
            "message_id": message_id, "role": role, "payload": payload,
        }, ensure_ascii=False)),
    )
    if entry_type == "message":
        await increment_messages(db, session_id)
        message_content = json.loads(content_json)
        if message_content.get("type") == "AssistantMessage":
            usage = AssistantMessage.model_validate(message_content["data"]).usage
            await add_usage(
                db, session_id, cached_tokens=usage.cache_read,
                uncached_tokens=usage.input + usage.cache_write, total_tokens=usage.total_tokens,
                cost_total=usage.cost.total if usage.cost is not None else 0,
            )
    return global_seq


async def append_entry(
    db: aiosqlite.Connection, *, session_id: str, entry_id: str,
    parent_id: str | None, message_id: str, role: str, content_json: str,
    created_at: int, entry_type: str = "message", payload: dict[str, Any] | None = None,
) -> int:
    """Append a canonical entry and its projections within the caller's transaction.

    Callers own admission, the writer lease and rollback. Regeneration and
    ordinary messages must share sequence, log, branch cache and stats updates.
    """
    if payload is None:
        loaded = json.loads(content_json)
        payload = loaded if isinstance(loaded, dict) else {}
    cursor = await db.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM session_entries WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    local_seq = int(row[0]) if row is not None else 0
    global_seq = await _record_projection(
        db, session_id=session_id, entry_id=entry_id, parent_id=parent_id,
        message_id=message_id, role=role, created_at=created_at,
        entry_type=entry_type, content_json=content_json, payload=payload,
    )
    await db.execute(
        "INSERT INTO session_entries "
        "(id, session_id, seq, parent_id, message_id, role, content_json, "
        "created_at, entry_type, payload_json, global_seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, session_id, local_seq, parent_id, message_id, role, content_json,
         created_at, entry_type, json.dumps(payload, ensure_ascii=False), global_seq),
    )
    await append_entry_to_branch_cache(
        db, session_id=session_id, entry_id=entry_id, entry_seq=global_seq,
        entry_type=entry_type, parent_id=parent_id,
    )
    return global_seq


async def repair_unsequenced_entries(db: aiosqlite.Connection) -> None:
    """Backfill projections omitted by the old regeneration writer.

    Preserve entry IDs, content, parent links and existing log sequences. Only
    unsequenced rows receive new log positions; rebuild affected branch caches.
    The migration transaction makes this restart-safe and idempotent.
    """
    cursor = await db.execute(
        "SELECT * FROM session_entries WHERE global_seq IS NULL ORDER BY session_id, seq",
    )
    rows = list(await cursor.fetchall())
    await cursor.close()
    sessions: set[str] = set()
    for row in rows:
        source_json = row["content_json"] if row["entry_type"] == "message" else row["payload_json"]
        payload = json.loads(source_json)
        global_seq = await _record_projection(
            db, session_id=row["session_id"], entry_id=row["id"], parent_id=row["parent_id"],
            message_id=row["message_id"], role=row["role"], created_at=row["created_at"],
            entry_type=row["entry_type"], content_json=row["content_json"], payload=payload,
        )
        await db.execute(
            "UPDATE session_entries SET global_seq = ?, payload_json = ? WHERE id = ?",
            (global_seq, json.dumps(payload, ensure_ascii=False), row["id"]),
        )
        sessions.add(row["session_id"])
    for session_id in sessions:
        await rebuild_branch_cache(db, session_id)


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


__all__ = [
    "append_entry", "delete_entry_rows", "id_exists_in_entries", "read_entry_row",
    "repair_unsequenced_entries",
]

"""Materialized branch paths derived from canonical entry parent links."""

from __future__ import annotations

from uuid import uuid4

import aiosqlite


class BranchCacheError(RuntimeError):
    pass


async def delete_branch_cache(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM branch_tips WHERE session_id = ?", (session_id,))
    await db.execute("DELETE FROM branch_entries WHERE session_id = ?", (session_id,))


async def _build_branch(
    db: aiosqlite.Connection, session_id: str, tip_id: str
) -> None:
    branch_id = uuid4().hex
    cursor = await db.execute(
        "WITH RECURSIVE path(id, parent_id, entry_seq, entry_type, payload_json, depth) "
        "AS (SELECT id, parent_id, global_seq, entry_type, payload_json, 0 "
        "FROM session_entries WHERE session_id = ? AND id = ? "
        "UNION ALL SELECT parent.id, parent.parent_id, parent.global_seq, "
        "parent.entry_type, parent.payload_json, path.depth + 1 "
        "FROM session_entries AS parent JOIN path ON parent.id = path.parent_id "
        "WHERE parent.session_id = ?) "
        "SELECT id, entry_seq, entry_type, payload_json FROM path ORDER BY depth DESC",
        (session_id, tip_id, session_id),
    )
    rows = list(await cursor.fetchall())
    await cursor.close()
    if not rows or str(rows[-1]["id"]) != tip_id:
        raise BranchCacheError(f"entry {tip_id!r} does not belong to session")
    for row in rows:
        if row["entry_seq"] is None:
            raise BranchCacheError(f"entry {row['id']!r} has no global sequence")
        await db.execute(
            "INSERT INTO branch_entries "
            "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (
                session_id,
                branch_id,
                row["id"],
                row["entry_seq"],
                row["entry_type"],
            ),
        )
    await db.execute(
        "INSERT INTO branch_tips (session_id, branch_id, tip_id) VALUES (?, ?, ?)",
        (session_id, branch_id, tip_id),
    )


async def rebuild_branch_cache(db: aiosqlite.Connection, session_id: str) -> None:
    await delete_branch_cache(db, session_id)
    cursor = await db.execute(
        "SELECT leaf.id FROM session_entries AS leaf "
        "WHERE leaf.session_id = ? AND NOT EXISTS ("
        "SELECT 1 FROM session_entries AS child "
        "WHERE child.session_id = leaf.session_id AND child.parent_id = leaf.id) "
        "ORDER BY leaf.global_seq",
        (session_id,),
    )
    tips = list(await cursor.fetchall())
    await cursor.close()
    for tip in tips:
        await _build_branch(db, session_id, str(tip["id"]))


async def append_entry_to_branch_cache(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    entry_id: str,
    entry_seq: int,
    entry_type: str,
    parent_id: str | None,
) -> None:
    if parent_id is None:
        branch_id = uuid4().hex
        await db.execute(
            "INSERT INTO branch_entries "
            "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (session_id, branch_id, entry_id, entry_seq, entry_type),
        )
        await db.execute(
            "INSERT INTO branch_tips (session_id, branch_id, tip_id) VALUES (?, ?, ?)",
            (session_id, branch_id, entry_id),
        )
        return

    cursor = await db.execute(
        "SELECT branch_id FROM branch_tips "
        "WHERE session_id = ? AND tip_id = ?",
        (session_id, parent_id),
    )
    tip = await cursor.fetchone()
    await cursor.close()
    if tip is not None:
        branch_id = str(tip["branch_id"])
        await db.execute(
            "INSERT INTO branch_entries "
            "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (session_id, branch_id, entry_id, entry_seq, entry_type),
        )
        cursor = await db.execute(
            "UPDATE branch_tips SET tip_id = ? WHERE session_id = ? "
            "AND branch_id = ? AND tip_id = ?",
            (entry_id, session_id, branch_id, parent_id),
        )
        changed = cursor.rowcount
        await cursor.close()
        if changed != 1:
            raise BranchCacheError("branch tip changed during append")
        return

    cursor = await db.execute(
        "SELECT branch_id, entry_seq FROM branch_entries "
        "WHERE session_id = ? AND entry_id = ? ORDER BY branch_id LIMIT 1",
        (session_id, parent_id),
    )
    source = await cursor.fetchone()
    await cursor.close()
    if source is None:
        raise BranchCacheError(
            f"branch cache has no branch containing parent {parent_id!r}"
        )
    branch_id = uuid4().hex
    await db.execute(
        "INSERT INTO branch_entries "
        "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
        "SELECT session_id, ?, entry_id, entry_seq, entry_type, custom_type "
        "FROM branch_entries WHERE session_id = ? AND branch_id = ? "
        "AND entry_seq <= ?",
        (branch_id, session_id, source["branch_id"], source["entry_seq"]),
    )
    await db.execute(
        "INSERT INTO branch_entries "
        "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (session_id, branch_id, entry_id, entry_seq, entry_type),
    )
    await db.execute(
        "INSERT INTO branch_tips (session_id, branch_id, tip_id) VALUES (?, ?, ?)",
        (session_id, branch_id, entry_id),
    )


__all__ = [
    "BranchCacheError",
    "append_entry_to_branch_cache",
    "delete_branch_cache",
    "rebuild_branch_cache",
]

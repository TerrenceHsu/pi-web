"""Ordered, transactional schema migrations for the SQLite Session backend."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from importlib.resources import files

import aiosqlite


class SQLiteMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SQLiteMigration:
    id: str
    order: int
    sql_resource: str | None = None
    apply: Callable[[], Awaitable[None]] | None = None


def _sql_statements(script: str) -> Iterator[str]:
    statement = ""
    for character in script:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            stripped = statement.strip()
            if stripped:
                yield stripped
            statement = ""
    if statement.strip():
        raise SQLiteMigrationError("incomplete SQL statement in migration resource")


def _load_sql(resource: str) -> str:
    target = files(__package__).joinpath("migrations", resource)
    return target.read_text(encoding="utf-8")


def _usage_from_message(content_json: str) -> tuple[float, float, float, float]:
    try:
        payload = json.loads(content_json)
        if payload.get("type") != "AssistantMessage":
            return 0, 0, 0, 0
        usage = payload.get("data", {}).get("usage", {})
        cache_read = float(usage.get("cache_read", 0) or 0)
        cache_write = float(usage.get("cache_write", 0) or 0)
        uncached = float(usage.get("input", 0) or 0) + cache_write
        total = float(usage.get("total_tokens", 0) or 0)
        cost = float((usage.get("cost") or {}).get("total", 0) or 0)
        return cache_read, uncached, total, cost
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return 0, 0, 0, 0


async def _backfill_repository_state(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT id, title, created_at, updated_at FROM sessions ORDER BY created_at, id"
    )
    sessions = list(await cursor.fetchall())
    await cursor.close()
    for session in sessions:
        session_id = str(session["id"])
        timeline: list[tuple[int, int, int, str, str, dict[str, object]]] = []

        cursor = await db.execute(
            "SELECT id, seq, parent_id, message_id, role, content_json, "
            "created_at FROM session_entries WHERE session_id = ?",
            (session_id,),
        )
        entries = list(await cursor.fetchall())
        await cursor.close()
        cached_tokens = 0.0
        uncached_tokens = 0.0
        total_tokens = 0.0
        cost_total = 0.0
        for row in entries:
            try:
                message_payload: object = json.loads(str(row["content_json"]))
            except json.JSONDecodeError:
                message_payload = {"corrupt": True}
            payload = {
                "id": str(row["id"]),
                "type": "message",
                "parent_id": row["parent_id"],
                "message_id": str(row["message_id"]),
                "role": str(row["role"]),
                "payload": message_payload,
            }
            timeline.append(
                (
                    int(row["created_at"]),
                    0,
                    int(row["seq"]),
                    "entry",
                    str(row["id"]),
                    payload,
                )
            )
            usage = _usage_from_message(str(row["content_json"]))
            cached_tokens += usage[0]
            uncached_tokens += usage[1]
            total_tokens += usage[2]
            cost_total += usage[3]

        cursor = await db.execute(
            "SELECT id, seq, entry_id, kind, value_json, created_at "
            "FROM session_facts WHERE session_id = ?",
            (session_id,),
        )
        facts = list(await cursor.fetchall())
        await cursor.close()
        for row in facts:
            timeline.append(
                (
                    int(row["created_at"]),
                    1,
                    int(row["seq"]),
                    "fact",
                    str(row["id"]),
                    {
                        "fact": str(row["kind"]),
                        "target_id": str(row["entry_id"]),
                        "value": json.loads(str(row["value_json"])),
                    },
                )
            )

        cursor = await db.execute(
            "SELECT id, operation_id, seq, record_type, payload_json, created_at "
            "FROM session_operation_records WHERE session_id = ?",
            (session_id,),
        )
        records = list(await cursor.fetchall())
        await cursor.close()
        for row in records:
            timeline.append(
                (
                    int(row["created_at"]),
                    2,
                    int(row["seq"]),
                    "record",
                    str(row["id"]),
                    {
                        "operation_id": str(row["operation_id"]),
                        "type": str(row["record_type"]),
                        "payload": json.loads(str(row["payload_json"])),
                    },
                )
            )

        timeline.sort(key=lambda item: (item[0], item[1], item[2], item[4]))
        next_seq = 1
        for timestamp, _priority, local_seq, kind, item_id, payload in timeline:
            await db.execute(
                "INSERT INTO session_log "
                "(session_id, seq, kind, item_id, timestamp, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    next_seq,
                    kind,
                    item_id,
                    timestamp,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            if kind == "entry":
                await db.execute(
                    "UPDATE session_entries SET entry_type = 'message', "
                    "payload_json = content_json, global_seq = ? "
                    "WHERE session_id = ? AND id = ? AND seq = ?",
                    (next_seq, session_id, item_id, local_seq),
                )
            elif kind == "fact":
                await db.execute(
                    "UPDATE session_facts SET global_seq = ? "
                    "WHERE session_id = ? AND id = ?",
                    (next_seq, session_id, item_id),
                )
            else:
                await db.execute(
                    "UPDATE session_operation_records SET global_seq = ? "
                    "WHERE session_id = ? AND id = ?",
                    (next_seq, session_id, item_id),
                )
            next_seq += 1

        title = str(session["title"])
        await db.execute(
            "INSERT INTO session_global_facts "
            "(session_id, seq, kind, key, value_json, timestamp) "
            "VALUES (?, ?, 'name', NULL, ?, ?)",
            (session_id, next_seq, json.dumps(title, ensure_ascii=False), session["updated_at"]),
        )
        await db.execute(
            "INSERT INTO session_log "
            "(session_id, seq, kind, item_id, timestamp, payload_json) "
            "VALUES (?, ?, 'fact', NULL, ?, ?)",
            (
                session_id,
                next_seq,
                session["updated_at"],
                json.dumps({"fact": "name", "value": title}, ensure_ascii=False),
            ),
        )
        next_seq += 1
        await db.execute(
            "INSERT INTO session_sequences (session_id, next_seq) VALUES (?, ?)",
            (session_id, next_seq),
        )
        await db.execute(
            "INSERT INTO session_stats "
            "(session_id, message_count, cached_tokens, uncached_tokens, "
            "total_tokens, cost_total) VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                len(entries),
                cached_tokens,
                uncached_tokens,
                total_tokens,
                cost_total,
            ),
        )

        cursor = await db.execute(
            "SELECT leaf.id FROM session_entries AS leaf "
            "WHERE leaf.session_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM session_entries AS child "
            "WHERE child.session_id = leaf.session_id "
            "AND child.parent_id = leaf.id) ORDER BY leaf.seq",
            (session_id,),
        )
        tips = list(await cursor.fetchall())
        await cursor.close()
        for tip in tips:
            tip_id = str(tip["id"])
            branch_id = f"legacy:{tip_id}"
            cursor = await db.execute(
                "WITH RECURSIVE path(id, parent_id, entry_seq, entry_type, depth) AS ("
                "SELECT id, parent_id, global_seq, entry_type, 0 "
                "FROM session_entries WHERE session_id = ? AND id = ? "
                "UNION ALL SELECT parent.id, parent.parent_id, parent.global_seq, "
                "parent.entry_type, path.depth + 1 FROM session_entries AS parent "
                "JOIN path ON parent.id = path.parent_id "
                "WHERE parent.session_id = ?) "
                "SELECT id, entry_seq, entry_type FROM path ORDER BY depth DESC",
                (session_id, tip_id, session_id),
            )
            path = list(await cursor.fetchall())
            await cursor.close()
            for entry in path:
                await db.execute(
                    "INSERT INTO branch_entries "
                    "(session_id, branch_id, entry_id, entry_seq, entry_type, custom_type) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (
                        session_id,
                        branch_id,
                        entry["id"],
                        entry["entry_seq"],
                        entry["entry_type"],
                    ),
                )
            await db.execute(
                "INSERT INTO branch_tips (session_id, branch_id, tip_id) "
                "VALUES (?, ?, ?)",
                (session_id, branch_id, tip_id),
            )


async def apply_migrations(
    db: aiosqlite.Connection,
    *,
    legacy_tree_migration: Callable[[], Awaitable[None]],
) -> None:
    migrations = (
        SQLiteMigration("001_baseline.sql", 1, sql_resource="001_baseline.sql"),
        SQLiteMigration("002_legacy_tree", 2, apply=legacy_tree_migration),
        SQLiteMigration("003_repository.sql", 3, sql_resource="003_repository.sql"),
        SQLiteMigration(
            "004_repository_backfill",
            4,
            apply=lambda: _backfill_repository_state(db),
        ),
    )

    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'session_migrations'"
    )
    migrations_table_exists = await cursor.fetchone() is not None
    await cursor.close()
    if not migrations_table_exists:
        await db.execute(
            "CREATE TABLE session_migrations ("
            "id TEXT PRIMARY KEY, migration_order INTEGER NOT NULL UNIQUE, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.commit()

    cursor = await db.execute(
        "SELECT id, migration_order FROM session_migrations "
        "ORDER BY migration_order, id"
    )
    applied_rows = list(await cursor.fetchall())
    await cursor.close()
    known = {migration.id: migration.order for migration in migrations}
    unknown = [str(row[0]) for row in applied_rows if row[0] not in known]
    if unknown:
        raise SQLiteMigrationError(
            "session database was created by a newer application: "
            + ", ".join(unknown)
        )
    for row in applied_rows:
        if known[str(row[0])] != int(row[1]):
            raise SQLiteMigrationError(
                f"migration order mismatch for {row[0]!r}"
            )

    applied = {str(row[0]) for row in applied_rows}
    for migration in migrations:
        if migration.id in applied:
            continue
        try:
            await db.execute("BEGIN IMMEDIATE")
            if migration.sql_resource is not None:
                for statement in _sql_statements(_load_sql(migration.sql_resource)):
                    await db.execute(statement)
            if migration.apply is not None:
                await migration.apply()
            await db.execute(
                "INSERT INTO session_migrations (id, migration_order) VALUES (?, ?)",
                (migration.id, migration.order),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


__all__ = [
    "SQLiteMigration",
    "SQLiteMigrationError",
    "apply_migrations",
]

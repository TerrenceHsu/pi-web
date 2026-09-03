"""Durable, privacy-bounded SQLite Telemetry context and admin queries."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

import aiosqlite

from ._attributes import copy_attributes, merge_attributes
from .noop import NOOP_TELEMETRY_CONTEXT
from .types import (
    AttributeValue,
    RecordedTelemetryEvent,
    SpanAttributes,
    SpanCallback,
    SpanOptions,
    SpanStatus,
    T,
    TelemetryError,
)

TELEMETRY_SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SPANS = 50_000
_INIT_LOCK_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _json(attributes: Mapping[str, AttributeValue]) -> str:
    return json.dumps(attributes, ensure_ascii=False, separators=(",", ":"))


def _load_json(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error(error: BaseException) -> TelemetryError:
    # Exception messages can contain provider payloads or credentials.  The
    # durable adapter stores the exception class only.
    return TelemetryError(name=type(error).__name__[:128])


def _safe_status(status: SpanStatus) -> SpanStatus:
    if status.status == "ok" or status.error is None:
        return SpanStatus(status=status.status)
    return SpanStatus(
        status="error",
        error=TelemetryError(name=status.error.name[:128]),
    )


async def _enable_wal(connection: aiosqlite.Connection) -> None:
    """Enable WAL despite SQLite's immediate-lock behavior for this PRAGMA."""

    for delay in (*_INIT_LOCK_RETRY_DELAYS, None):
        try:
            await connection.execute("PRAGMA journal_mode = WAL")
            return
        except aiosqlite.OperationalError as error:
            if "locked" not in str(error).casefold() or delay is None:
                raise
            await asyncio.sleep(delay)


class _SQLiteSpan:
    def __init__(
        self,
        context: SQLiteTelemetryContext,
        *,
        span_id: str,
        trace_id: str,
        parent_id: str | None,
        name: str,
        attributes: dict[str, AttributeValue],
        started_at_ms: int,
    ) -> None:
        self._context = context
        self.id = span_id
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.name = name
        self.attributes = attributes
        self.started_at_ms = started_at_ms
        self.events: list[RecordedTelemetryEvent] = []
        self.status = SpanStatus()
        self.explicit_status = False
        self.settled = False

    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T:
        if self.settled:
            return await NOOP_TELEMETRY_CONTEXT.start_span(options, callback)
        return await self._context._start(
            options,
            callback,
            trace_id=self.trace_id,
            parent_id=self.id,
        )

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        if self.settled:
            return
        try:
            self.events.append(
                RecordedTelemetryEvent(
                    name=str(name)[:128],
                    attributes=copy_attributes(attributes, persistent=True),
                    timestamp_ms=_now_ms(),
                )
            )
        except Exception:
            return

    def set_attributes(self, attributes: SpanAttributes) -> None:
        if self.settled:
            return
        try:
            self.attributes = merge_attributes(
                self.attributes,
                attributes,
                persistent=True,
            )
        except Exception:
            return

    def set_status(self, status: SpanStatus) -> None:
        if self.settled:
            return
        try:
            self.status = _safe_status(status)
            self.explicit_status = True
        except Exception:
            return


class SQLiteTelemetryContext:
    """Telemetry recorder whose failures never affect the wrapped operation."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        base_attributes: SpanAttributes | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_spans: int = DEFAULT_MAX_SPANS,
    ) -> None:
        if retention_days <= 0 or max_spans <= 0:
            raise ValueError("telemetry retention limits must be positive")
        self.database_path = str(database_path)
        self.base_attributes = copy_attributes(base_attributes, persistent=True)
        self.retention_days = retention_days
        self.max_spans = max_spans
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def init(self) -> None:
        if self._connection is not None:
            return
        connection = await aiosqlite.connect(self.database_path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout = 5000")
            await _enable_wal(connection)
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS telemetry_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL CHECK (version >= 1)
                );
                CREATE TABLE IF NOT EXISTS telemetry_spans (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_id TEXT REFERENCES telemetry_spans(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    duration_ms INTEGER,
                    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
                    error_name TEXT,
                    error_message TEXT,
                    attributes_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    span_id TEXT NOT NULL REFERENCES telemetry_spans(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    timestamp_ms INTEGER NOT NULL,
                    attributes_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_spans_started
                    ON telemetry_spans(started_at_ms DESC, id);
                CREATE INDEX IF NOT EXISTS idx_telemetry_spans_trace
                    ON telemetry_spans(trace_id, started_at_ms, id);
                CREATE INDEX IF NOT EXISTS idx_telemetry_spans_status
                    ON telemetry_spans(status, started_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_telemetry_events_span
                    ON telemetry_events(span_id, timestamp_ms, id);
                """
            )
            # Several account workspaces can initialize against the shared
            # Telemetry database concurrently.  The idempotent insert avoids
            # turning that normal race into a disabled recorder.
            await connection.execute(
                "INSERT OR IGNORE INTO telemetry_meta(singleton, version) VALUES (1, ?)",
                (TELEMETRY_SCHEMA_VERSION,),
            )
            async with connection.execute(
                "SELECT version FROM telemetry_meta WHERE singleton = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or int(row["version"]) != TELEMETRY_SCHEMA_VERSION:
                raise RuntimeError("telemetry schema version is unsupported")
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        await self.prune()

    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T:
        return await self._start(options, callback, trace_id=None, parent_id=None)

    async def _start(
        self,
        options: SpanOptions,
        callback: SpanCallback[T],
        *,
        trace_id: str | None,
        parent_id: str | None,
    ) -> T:
        span_id = uuid4().hex
        resolved_trace_id = trace_id or uuid4().hex
        started_at_ms = _now_ms()
        attributes = dict(self.base_attributes)
        attributes.update(copy_attributes(options.attributes, persistent=True))
        span = _SQLiteSpan(
            self,
            span_id=span_id,
            trace_id=resolved_trace_id,
            parent_id=parent_id,
            name=options.name[:128],
            attributes=attributes,
            started_at_ms=started_at_ms,
        )
        if not await self._begin(span):
            return await NOOP_TELEMETRY_CONTEXT.start_span(options, callback)

        try:
            result = callback(span)
            value = (
                await cast("object", result)  # type: ignore[misc]
                if inspect.isawaitable(result)
                else result
            )
        except BaseException as error:
            if not span.explicit_status:
                span.status = SpanStatus(status="error", error=_safe_error(error))
            await self._finish(span)
            raise
        await self._finish(span)
        return value

    async def _begin(self, span: _SQLiteSpan) -> bool:
        connection = self._connection
        if connection is None or self._closed:
            return False
        try:
            async with self._lock:
                if self._closed or self._connection is not connection:
                    return False
                await connection.execute(
                    "INSERT INTO telemetry_spans ("
                    "id, trace_id, parent_id, name, started_at_ms, status, attributes_json"
                    ") VALUES (?, ?, ?, ?, ?, 'running', ?)",
                    (
                        span.id,
                        span.trace_id,
                        span.parent_id,
                        span.name,
                        span.started_at_ms,
                        _json(span.attributes),
                    ),
                )
            return True
        except Exception:
            return False

    async def _finish(self, span: _SQLiteSpan) -> None:
        if span.settled:
            return
        span.settled = True
        ended_at_ms = _now_ms()
        duration_ms = max(0, ended_at_ms - span.started_at_ms)
        error = span.status.error
        connection = self._connection
        if connection is None or self._closed:
            return
        try:
            async with self._lock:
                if self._closed or self._connection is not connection:
                    return
                try:
                    await connection.execute("BEGIN IMMEDIATE")
                    await connection.execute(
                        "UPDATE telemetry_spans SET ended_at_ms = ?, duration_ms = ?, "
                        "status = ?, error_name = ?, error_message = ?, "
                        "attributes_json = ? WHERE id = ?",
                        (
                            ended_at_ms,
                            duration_ms,
                            span.status.status,
                            error.name if error is not None else None,
                            error.message if error is not None else None,
                            _json(span.attributes),
                            span.id,
                        ),
                    )
                    if span.events:
                        await connection.executemany(
                            "INSERT INTO telemetry_events "
                            "(span_id, name, timestamp_ms, attributes_json) "
                            "VALUES (?, ?, ?, ?)",
                            [
                                (
                                    span.id,
                                    event.name,
                                    event.timestamp_ms,
                                    _json(event.attributes),
                                )
                                for event in span.events
                            ],
                        )
                    await connection.execute("COMMIT")
                except BaseException:
                    try:
                        await connection.execute("ROLLBACK")
                    except BaseException:
                        pass
        except BaseException:
            pass

    async def prune(self) -> None:
        connection = self._connection
        if connection is None or self._closed:
            return
        cutoff = _now_ms() - self.retention_days * 24 * 60 * 60 * 1000
        async with self._lock:
            await connection.execute(
                "DELETE FROM telemetry_spans WHERE started_at_ms < ?",
                (cutoff,),
            )
            await connection.execute(
                "DELETE FROM telemetry_spans WHERE id IN ("
                "SELECT id FROM telemetry_spans ORDER BY started_at_ms DESC, id DESC "
                "LIMIT -1 OFFSET ?)",
                (self.max_spans,),
            )

    async def summary(self, *, since_ms: int) -> dict[str, object]:
        connection = self._require_connection()
        async with self._lock:
            async with connection.execute(
                "SELECT id, name, started_at_ms, ended_at_ms, duration_ms, status, "
                "attributes_json FROM telemetry_spans WHERE started_at_ms >= ? "
                "ORDER BY started_at_ms ASC",
                (since_ms,),
            ) as cursor:
                span_rows = await cursor.fetchall()
            async with connection.execute(
                "SELECT e.name, e.attributes_json FROM telemetry_events AS e "
                "JOIN telemetry_spans AS s ON s.id = e.span_id "
                "WHERE s.started_at_ms >= ? AND s.name = 'web.request'",
                (since_ms,),
            ) as cursor:
                event_rows = await cursor.fetchall()

        request_rows = [row for row in span_rows if row["name"] == "web.request"]
        outcomes: Counter[str] = Counter()
        providers: Counter[str] = Counter()
        accounts: dict[str, str] = {}
        durations: list[int] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        buckets: dict[int, Counter[str]] = defaultdict(Counter)
        for row in request_rows:
            attrs = _load_json(row["attributes_json"])
            outcome = str(attrs.get("outcome") or row["status"])
            outcomes[outcome] += 1
            provider = attrs.get("provider")
            if isinstance(provider, str) and provider:
                providers[provider] += 1
            account_id = attrs.get("account_id")
            account_name = attrs.get("account_name")
            if isinstance(account_id, str) and isinstance(account_name, str):
                accounts[account_id] = account_name
            if isinstance(row["duration_ms"], int):
                durations.append(int(row["duration_ms"]))
            for key, target in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
            ):
                value = attrs.get(key)
                if isinstance(value, (int, float)):
                    if target == "input":
                        total_input_tokens += int(value)
                    else:
                        total_output_tokens += int(value)
            cost = attrs.get("cost")
            if isinstance(cost, (int, float)) and math.isfinite(float(cost)):
                total_cost += float(cost)
            bucket = int(row["started_at_ms"]) // 3_600_000 * 3_600_000
            buckets[bucket][outcome] += 1

        tool_names: Counter[str] = Counter()
        tool_errors = 0
        for row in event_rows:
            if row["name"] != "tool.end":
                continue
            attrs = _load_json(row["attributes_json"])
            name = attrs.get("tool_name")
            if isinstance(name, str) and name:
                tool_names[name] += 1
            if attrs.get("is_error") is True:
                tool_errors += 1

        ordered_durations = sorted(durations)
        p95_index = max(0, math.ceil(len(ordered_durations) * 0.95) - 1)
        return {
            "since_ms": since_ms,
            "generated_at_ms": _now_ms(),
            "requests": {
                "total": len(request_rows),
                "completed": outcomes["completed"],
                "error": outcomes["error"],
                "aborted": outcomes["aborted"],
                "running": outcomes["running"],
                "error_rate": (
                    outcomes["error"] / len(request_rows) if request_rows else 0.0
                ),
                "average_duration_ms": (
                    round(sum(durations) / len(durations)) if durations else 0
                ),
                "p95_duration_ms": ordered_durations[p95_index] if durations else 0,
            },
            "usage": {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "cost": round(total_cost, 6),
            },
            "tools": {
                "calls": sum(tool_names.values()),
                "errors": tool_errors,
                "top": [
                    {"name": name, "calls": calls}
                    for name, calls in tool_names.most_common(8)
                ],
            },
            "providers": [
                {"name": name, "requests": count}
                for name, count in providers.most_common()
            ],
            "accounts": [
                {"id": account_id, "name": account_name}
                for account_id, account_name in sorted(
                    accounts.items(), key=lambda item: item[1].casefold()
                )
            ],
            "timeline": [
                {
                    "timestamp_ms": bucket,
                    "total": sum(counts.values()),
                    "completed": counts["completed"],
                    "error": counts["error"],
                    "aborted": counts["aborted"],
                    "running": counts["running"],
                }
                for bucket, counts in sorted(buckets.items())
            ],
        }

    async def list_spans(
        self,
        *,
        since_ms: int,
        limit: int,
        status: str | None = None,
        name: str | None = None,
        account_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        connection = self._require_connection()
        clauses = ["started_at_ms >= ?"]
        parameters: list[object] = [since_ms]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if name is not None:
            clauses.append("name = ?")
            parameters.append(name)
        if account_id is not None:
            clauses.append("json_extract(attributes_json, '$.account_id') = ?")
            parameters.append(account_id)
        if session_id is not None:
            clauses.append("json_extract(attributes_json, '$.session_id') = ?")
            parameters.append(session_id)
        parameters.append(limit)
        async with self._lock:
            async with connection.execute(
                "SELECT id, trace_id, parent_id, name, started_at_ms, ended_at_ms, "
                "duration_ms, status, error_name, error_message, attributes_json "
                f"FROM telemetry_spans WHERE {' AND '.join(clauses)} "
                "ORDER BY started_at_ms DESC, id DESC LIMIT ?",
                parameters,
            ) as cursor:
                rows = await cursor.fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            item = self._serialize_span_row(row)
            result.append(item)
        return result

    async def get_span(self, span_id: str) -> dict[str, object] | None:
        connection = self._require_connection()
        async with self._lock:
            async with connection.execute(
                "SELECT id, trace_id, parent_id, name, started_at_ms, ended_at_ms, "
                "duration_ms, status, error_name, error_message, attributes_json "
                "FROM telemetry_spans WHERE id = ?",
                (span_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            async with connection.execute(
                "SELECT name, timestamp_ms, attributes_json FROM telemetry_events "
                "WHERE span_id = ? ORDER BY timestamp_ms ASC, id ASC",
                (span_id,),
            ) as cursor:
                event_rows = await cursor.fetchall()
        item = self._serialize_span_row(row)
        item["events"] = [
            {
                "name": str(event["name"]),
                "timestamp_ms": int(event["timestamp_ms"]),
                "attributes": _load_json(event["attributes_json"]),
            }
            for event in event_rows
        ]
        return item

    @staticmethod
    def _serialize_span_row(row: aiosqlite.Row) -> dict[str, object]:
        return {
            "id": str(row["id"]),
            "trace_id": str(row["trace_id"]),
            "parent_id": str(row["parent_id"]) if row["parent_id"] is not None else None,
            "name": str(row["name"]),
            "started_at_ms": int(row["started_at_ms"]),
            "ended_at_ms": int(row["ended_at_ms"]) if row["ended_at_ms"] is not None else None,
            "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
            "status": str(row["status"]),
            "error": (
                {
                    "name": str(row["error_name"]),
                    "message": (
                        str(row["error_message"])
                        if row["error_message"] is not None
                        else None
                    ),
                }
                if row["error_name"] is not None
                else None
            ),
            "attributes": _load_json(row["attributes_json"]),
        }

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            self._connection = None
            if connection is not None:
                await connection.close()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._closed or self._connection is None:
            raise RuntimeError("telemetry context is unavailable")
        return self._connection


__all__ = [
    "DEFAULT_MAX_SPANS",
    "DEFAULT_RETENTION_DAYS",
    "SQLiteTelemetryContext",
    "TELEMETRY_SCHEMA_VERSION",
]

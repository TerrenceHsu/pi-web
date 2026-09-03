"""Telemetry protocol, passivity, persistence, and privacy tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from pi_agent_core_py.agent.events import (
    AgentAbortEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from pi_agent_core_py.messages import AssistantMessage, ToolCall, Usage, UsageCost
from pi_agent_core_py.telemetry import (
    NOOP_TELEMETRY_CONTEXT,
    InMemoryTelemetryContext,
    SpanOptions,
    SpanStatus,
    SQLiteTelemetryContext,
    TelemetryError,
    TelemetrySpan,
)
from pi_agent_core_py.tools import ToolResult
from pi_agent_core_py.web.telemetry import RequestTelemetryStats, record_agent_event


@pytest.mark.asyncio
async def test_noop_context_preserves_callback_result_and_exception() -> None:
    async def success(span: TelemetrySpan) -> str:
        span.add_event("ignored", {"value": 1})
        return "result"

    assert await NOOP_TELEMETRY_CONTEXT.start_span(SpanOptions("noop"), success) == "result"

    def failure(span: TelemetrySpan) -> None:
        del span
        raise ValueError("expected")

    with pytest.raises(ValueError, match="expected"):
        await NOOP_TELEMETRY_CONTEXT.start_span(SpanOptions("noop-error"), failure)


@pytest.mark.asyncio
async def test_memory_context_records_nested_spans_and_automatic_error() -> None:
    context = InMemoryTelemetryContext()

    async def root(span: TelemetrySpan) -> str:
        span.add_event("started", {"tags": ("one", "two")})
        span.set_attributes({"phase": "running"})

        def child(child_span: TelemetrySpan) -> str:
            child_span.set_status(SpanStatus(status="ok"))
            return "done"

        return await span.start_span(SpanOptions("child", {"index": 1}), child)

    assert await context.start_span(SpanOptions("root"), root) == "done"

    async def fail(span: TelemetrySpan) -> None:
        span.add_event("before-error")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await context.start_span(SpanOptions("failure"), fail)

    root_record, child_record, failure_record = context.get_spans()
    assert child_record.parent_id == root_record.id
    assert child_record.trace_id == root_record.trace_id
    assert root_record.attributes["phase"] == "running"
    assert root_record.events[0].name == "started"
    assert failure_record.status.status == "error"
    assert failure_record.status.error is not None
    assert failure_record.status.error.name == "RuntimeError"


@pytest.mark.asyncio
async def test_sqlite_context_persists_queries_and_redacts_content(tmp_path: Path) -> None:
    database = tmp_path / "telemetry.sqlite"
    marker = "PROMPT_MARKER_MUST_NOT_PERSIST"
    context = SQLiteTelemetryContext(
        database,
        base_attributes={"account_id": "usr-1", "account_name": "alice"},
    )
    await context.init()

    async def record(span: TelemetrySpan) -> None:
        span.set_attributes(
            {
                "outcome": "completed",
                "input_tokens": 10,
                "output_tokens": 5,
                "cost": 0.01,
                "invalid_metric": float("nan"),
                "prompt": marker,
            }
        )
        span.add_event("tool.end", {"tool_name": "read_file", "is_error": False})
        span.set_status(
            SpanStatus(
                status="error",
                error=TelemetryError(name="ProviderError", message=marker),
            )
        )

    await context.start_span(
        SpanOptions("web.request", {"session_id": "session-1"}),
        record,
    )

    summary = await context.summary(since_ms=0)
    requests = summary["requests"]
    usage = summary["usage"]
    tools = summary["tools"]
    assert isinstance(requests, dict) and requests["total"] == 1
    assert isinstance(usage, dict) and usage["total_tokens"] == 15
    assert isinstance(tools, dict) and tools["calls"] == 1

    spans = await context.list_spans(since_ms=0, limit=10, account_id="usr-1")
    assert len(spans) == 1
    assert spans[0]["attributes"]["prompt"] == "[redacted]"  # type: ignore[index]
    assert "invalid_metric" not in spans[0]["attributes"]  # type: ignore[operator]
    detail = await context.get_span(str(spans[0]["id"]))
    assert detail is not None
    assert detail["events"][0]["name"] == "tool.end"  # type: ignore[index]
    await context.close()

    raw = database.read_bytes()
    assert marker.encode() not in raw

    reopened = SQLiteTelemetryContext(database)
    await reopened.init()
    assert len(await reopened.list_spans(since_ms=0, limit=10)) == 1
    await reopened.close()


@pytest.mark.asyncio
async def test_sqlite_recording_failure_is_passive(tmp_path: Path) -> None:
    context = SQLiteTelemetryContext(tmp_path / "telemetry.sqlite")
    await context.init()
    await context.close()

    called = False

    async def callback(span: TelemetrySpan) -> int:
        nonlocal called
        called = True
        span.add_event("still-safe")
        return 42

    assert await context.start_span(SpanOptions("after-close"), callback) == 42
    assert called


@pytest.mark.asyncio
async def test_sqlite_shared_database_supports_concurrent_account_startup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "telemetry.sqlite"
    first = SQLiteTelemetryContext(database, base_attributes={"account_id": "one"})
    second = SQLiteTelemetryContext(database, base_attributes={"account_id": "two"})
    await asyncio.gather(first.init(), second.init())

    async def complete(span: TelemetrySpan) -> None:
        span.set_attributes({"outcome": "completed"})

    await asyncio.gather(
        first.start_span(SpanOptions("web.request"), complete),
        second.start_span(SpanOptions("web.request"), complete),
    )
    assert len(await first.list_spans(since_ms=0, limit=10)) == 2
    await asyncio.gather(first.close(), second.close())


@pytest.mark.asyncio
async def test_sqlite_span_is_visible_as_running_while_callback_waits(tmp_path: Path) -> None:
    context = SQLiteTelemetryContext(tmp_path / "telemetry.sqlite")
    await context.init()
    release = asyncio.Event()

    async def callback(span: TelemetrySpan) -> None:
        del span
        await release.wait()

    task = asyncio.create_task(context.start_span(SpanOptions("web.request"), callback))
    await asyncio.sleep(0.05)
    running = await context.list_spans(since_ms=0, limit=10, status="running")
    assert len(running) == 1
    release.set()
    await task
    await context.close()


@pytest.mark.asyncio
async def test_sqlite_filters_before_applying_result_limit(tmp_path: Path) -> None:
    context = SQLiteTelemetryContext(tmp_path / "telemetry.sqlite")
    await context.init()

    async def complete(span: TelemetrySpan) -> None:
        span.set_attributes({"outcome": "completed"})

    await context.start_span(
        SpanOptions("web.request", {"account_id": "target", "session_id": "wanted"}),
        complete,
    )
    for index in range(3):
        await context.start_span(
            SpanOptions(
                "web.request",
                {"account_id": "other", "session_id": f"other-{index}"},
            ),
            complete,
        )

    filtered = await context.list_spans(
        since_ms=0,
        limit=1,
        account_id="target",
        session_id="wanted",
    )
    assert len(filtered) == 1
    assert filtered[0]["attributes"]["account_id"] == "target"  # type: ignore[index]
    await context.close()


@pytest.mark.asyncio
async def test_agent_event_projection_records_only_safe_operational_metadata() -> None:
    context = InMemoryTelemetryContext()
    secret_argument = "TOOL_ARGUMENT_MUST_NOT_APPEAR"
    secret_abort_reason = "ABORT_REASON_MUST_NOT_APPEAR"

    async def callback(span: TelemetrySpan) -> None:
        stats = RequestTelemetryStats()
        call = ToolCall(id="call-1", name="read_file", arguments={"path": secret_argument})
        record_agent_event(span, ToolExecutionStartEvent(tool_call=call), stats)
        record_agent_event(
            span,
            ToolExecutionEndEvent(
                tool_call=call,
                result=ToolResult(tool_call_id="call-1", name="read_file"),
            ),
            stats,
        )
        record_agent_event(
            span,
            MessageEndEvent(
                message=AssistantMessage(
                    api="test",
                    provider="provider-a",
                    model="model-a",
                    usage=Usage(
                        input=12,
                        output=4,
                        total_tokens=16,
                        cost=UsageCost(total=0.25),
                    ),
                )
            ),
            stats,
        )
        record_agent_event(
            span,
            AgentAbortEvent(request_id=None, reason=secret_abort_reason),
            stats,
        )
        span.set_attributes(stats.end_attributes())

    await context.start_span(SpanOptions("web.request"), callback)
    record = context.get_spans()[0]
    serialized = repr(record)
    assert secret_argument not in serialized
    assert secret_abort_reason not in serialized
    assert [event.name for event in record.events] == [
        "tool.start",
        "tool.end",
        "model.end",
        "agent.abort",
    ]
    assert record.attributes["tool_calls"] == 1
    assert record.attributes["input_tokens"] == 12


def test_auth_v1_database_migrates_admin_role(tmp_path: Path) -> None:
    from pi_agent_core_py.web.auth.passwords import hash_password
    from pi_agent_core_py.web.auth.store import AuthStore

    database = tmp_path / "auth.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE auth_schema_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL CHECK (version >= 1)
            );
            INSERT INTO auth_schema_meta(singleton, version) VALUES (1, 1);
            CREATE TABLE auth_users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO auth_users VALUES (?, ?, ?, ?, ?)",
            ("usr_admin", "admin", hash_password("123456"), 1, 1),
        )

    async def verify() -> None:
        store = await AuthStore.open(str(database))
        record = await store.get_user_by_name("admin")
        assert record is not None and record.is_admin
        await store.close()

    asyncio.run(verify())
    with sqlite3.connect(database) as db:
        assert db.execute(
            "SELECT version FROM auth_schema_meta WHERE singleton = 1"
        ).fetchone() == (2,)

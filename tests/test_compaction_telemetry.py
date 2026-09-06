"""Compaction observation never persists content or affects the request."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pi_agent_core_py.telemetry import InMemoryTelemetryContext, SQLiteTelemetryContext
from pi_agent_core_py.telemetry.compaction import record_compaction_event


async def test_compaction_event_has_only_safe_metrics() -> None:
    context = InMemoryTelemetryContext()
    await record_compaction_event(
        context,
        session_id="session-1",
        request_id="request-1",
        trigger="auto",
        before_tokens=16000,
        after_tokens=9000,
        covered_count=12,
        status="committed",
        duration_ms=25.5,
    )
    span = context.get_spans()[0]
    assert span.name == "context.compaction" and span.settled
    assert span.attributes == {
        "session_id": "session-1",
        "request_id": "request-1",
        "trigger": "auto",
        "before_tokens": 16000,
        "after_tokens": 9000,
        "covered_count": 12,
        "outcome": "committed",
        "elapsed_ms": 25.5,
    }
    assert span.events[0].attributes == span.attributes
    assert span.status.status == "ok"


async def test_compaction_event_drops_paths_free_text_and_invalid_metrics(tmp_path: Path) -> None:
    marker = "PRIVATE_SUMMARY_DO_NOT_STORE"
    context = SQLiteTelemetryContext(tmp_path / "telemetry.sqlite")
    await context.init()
    try:
        await record_compaction_event(
            context,
            session_id="D:\\private\\workspace",
            request_id="/private/session",
            trigger=marker,
            status="failed",
            error_code="ValueError: " + marker,
            before_tokens=-1,
            after_tokens=None,
            covered_count=True,
            duration_ms=float("nan"),
        )
        spans = await context.list_spans(since_ms=0, limit=10)
        detail = await context.get_span(str(spans[0]["id"]))
        rendered = json.dumps(detail)
        assert marker not in rendered and "private" not in rendered
        assert "compaction_error" in rendered
        assert "before_tokens" not in rendered and "covered_count" not in rendered
    finally:
        await context.close()


@pytest.mark.parametrize("error", [RuntimeError("private data"), TimeoutError("private data")])
async def test_compaction_recorder_failures_are_passive(error: Exception) -> None:
    class Broken:
        async def start_span(self, options, callback):
            raise error

    await record_compaction_event(
        Broken(),
        session_id="s",
        request_id=None,
        trigger="manual",
        status="failed",
        before_tokens=1,
        after_tokens=None,
        covered_count=0,
        error_code="summary_timeout",
    )
    await record_compaction_event(
        None,
        session_id="s",
        request_id=None,
        trigger="manual",
        status="failed",
        before_tokens=1,
        after_tokens=None,
        covered_count=0,
    )


async def test_compaction_recorder_timeout_is_bounded_and_cancellation_propagates() -> None:
    class Slow:
        async def start_span(self, options, callback):
            await asyncio.Event().wait()

    async def record() -> None:
        await record_compaction_event(
            Slow(),
            session_id="s",
            request_id=None,
            trigger="auto",
            status="skipped",
            before_tokens=1,
            after_tokens=1,
            covered_count=0,
        )

    await asyncio.wait_for(record(), timeout=1.5)
    task = asyncio.create_task(record())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_compaction_errors_and_metric_clamps_are_deterministic() -> None:
    context = InMemoryTelemetryContext()
    await record_compaction_event(
        context,
        session_id="s",
        request_id=None,
        trigger="threshold",
        status="failed",
        error_code="context_source_changed",
        before_tokens=10**20,
        after_tokens=0,
        covered_count=0,
        duration_ms=10**12,
    )
    span = context.get_spans()[0]
    assert span.attributes["before_tokens"] == 1_000_000_000
    assert span.attributes["elapsed_ms"] == 86_400_000.0
    assert span.status.error is not None
    assert span.status.error.name == "context_source_changed"
    assert span.status.error.message is None

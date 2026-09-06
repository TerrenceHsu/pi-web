"""Content-free, failure-isolated observation of context-management outcomes."""

from __future__ import annotations

import asyncio
import math
import re

from .types import (
    AttributeValue,
    SpanOptions,
    SpanStatus,
    TelemetryContext,
    TelemetryError,
    TelemetrySpan,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_TRIGGERS = frozenset(
    {"automatic", "auto", "manual", "user", "threshold", "model_call", "recovery", "budget"}
)
_STATUSES = frozenset(
    {
        "prepared",
        "committed",
        "completed",
        "failed",
        "skipped",
        "interrupted",
        "blocked",
        "cancelled",
        "no_change",
        "cooldown",
        "idle",
    }
)
_ERROR_CODES = frozenset(
    {
        "invalid_summary",
        "invalid_summary_json",
        "summary_provider_error",
        "summary_timeout",
        "summary_source_mismatch",
        "summary_capacity_exceeded",
        "summary_no_progress",
        "context_source_changed",
        "context_store_uninitialized",
        "context_database_unavailable",
        "context_record_not_found",
        "context_payload_too_large",
        "invalid_context_payload",
        "context_budget_unavailable",
        "context_budget_exceeded",
        "nothing_to_compact",
        "invalid_compaction_coverage",
        "invalid_compaction_summary",
        "compaction_failed",
        "compaction_timeout",
        "compaction_no_progress",
        "compaction_disabled",
        "compaction_call_limit",
        "compaction_failure_limit",
        "compaction_budget_exceeded",
        "compaction_source_changed",
        "compaction_summary_invalid",
        "compaction_cancelled",
        "summary_invalid",
        "summary_too_large",
        "summary_too_long",
        "summary_generation_failed",
        "summary_provider_unavailable",
        "summary_incomplete",
        "summary_output_too_large",
        "startup_interrupted",
        "source_changed",
        "not_enough_messages",
        "cancelled",
        "unknown_context_window",
        "auto_compaction_disabled",
        "summary_request_limit",
        "context_messages_changed",
        "no_new_complete_prefix",
        "summary_input_budget_exceeded",
        "summary_incomplete_response",
        "summary_unexpected_tool_call",
        "summary_missing_done",
        "invalid_working_summary",
        "summary_does_not_reduce_context",
        "summary_journal_unavailable",
        "summary_cancelled",
        "context_record_not_prepared",
    }
)


def _count(value: object) -> int | None:
    return min(value, 1_000_000_000) if type(value) is int and value >= 0 else None


async def record_compaction_event(
    context: TelemetryContext | None,
    *,
    session_id: str,
    request_id: str | None,
    trigger: str,
    before_tokens: int | None,
    after_tokens: int | None,
    covered_count: int,
    status: str,
    error_code: str | None = None,
    duration_ms: float = 0,
) -> None:
    """Record only explicit safe metrics; never accept transcript/summary payloads.

    Callers supply server-owned identifiers and stable error codes, not exception
    strings. Unknown labels are mapped to constants. Ordinary recorder failures
    and a bounded recording timeout are isolated; caller cancellation propagates.
    ``elapsed_ms`` measures the operation, whereas the span's own duration is the
    short recording operation itself.
    """
    if context is None:
        return
    safe_status = status if isinstance(status, str) and status in _STATUSES else "unknown"
    attributes: dict[str, AttributeValue] = {
        "trigger": trigger if isinstance(trigger, str) and trigger in _TRIGGERS else "other",
        "outcome": safe_status,
    }
    for key, value in (("session_id", session_id), ("request_id", request_id)):
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            attributes[key] = value
    for key, count in (
        ("before_tokens", before_tokens),
        ("after_tokens", after_tokens),
        ("covered_count", covered_count),
    ):
        safe = _count(count)
        if safe is not None:
            attributes[key] = safe
    if isinstance(duration_ms, int | float) and not isinstance(duration_ms, bool):
        bounded_duration = min(duration_ms, 86_400_000)
        if math.isfinite(bounded_duration) and bounded_duration >= 0:
            attributes["elapsed_ms"] = float(bounded_duration)
    code = (
        error_code
        if isinstance(error_code, str) and error_code in _ERROR_CODES
        else "compaction_error"
    )
    if error_code is not None:
        attributes["error_code"] = code

    def record(span: TelemetrySpan) -> None:
        span.add_event("context.compaction", attributes)
        if safe_status in {"failed", "interrupted", "blocked", "cancelled"}:
            span.set_status(SpanStatus("error", TelemetryError(code)))

    try:
        async with asyncio.timeout(0.5):
            await context.start_span(SpanOptions("context.compaction", attributes), record)
    except Exception:
        return


__all__ = ["record_compaction_event"]

"""In-process reference Telemetry implementation."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

from ._attributes import copy_attributes, merge_attributes
from .noop import NOOP_TELEMETRY_CONTEXT
from .types import (
    AttributeValue,
    RecordedTelemetryEvent,
    RecordedTelemetrySpan,
    SpanAttributes,
    SpanCallback,
    SpanOptions,
    SpanStatus,
    T,
    TelemetryError,
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class _MutableSpan:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    attributes: dict[str, AttributeValue]
    started_at_ms: int
    events: list[RecordedTelemetryEvent] = field(default_factory=list)
    status: SpanStatus = field(default_factory=SpanStatus)
    explicit_status: bool = False
    ended_at_ms: int | None = None


class _MemorySpan:
    def __init__(self, context: InMemoryTelemetryContext, state: _MutableSpan) -> None:
        self._context = context
        self._state = state

    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T:
        if self._state.ended_at_ms is not None:
            return await NOOP_TELEMETRY_CONTEXT.start_span(options, callback)
        return await self._context._start(options, callback, parent=self._state)

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        if self._state.ended_at_ms is not None:
            return
        try:
            self._state.events.append(
                RecordedTelemetryEvent(
                    name=str(name)[:128],
                    attributes=copy_attributes(attributes),
                    timestamp_ms=_now_ms(),
                )
            )
        except Exception:
            return

    def set_attributes(self, attributes: SpanAttributes) -> None:
        if self._state.ended_at_ms is not None:
            return
        try:
            self._state.attributes = merge_attributes(self._state.attributes, attributes)
        except Exception:
            return

    def set_status(self, status: SpanStatus) -> None:
        if self._state.ended_at_ms is not None:
            return
        try:
            self._state.status = status
            self._state.explicit_status = True
        except Exception:
            return


class InMemoryTelemetryContext:
    """Records detached spans for tests and embedders without persistence."""

    def __init__(self) -> None:
        self._spans: list[_MutableSpan] = []

    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T:
        return await self._start(options, callback, parent=None)

    async def _start(
        self,
        options: SpanOptions,
        callback: SpanCallback[T],
        *,
        parent: _MutableSpan | None,
    ) -> T:
        try:
            span_id = uuid4().hex
            state = _MutableSpan(
                id=span_id,
                trace_id=parent.trace_id if parent is not None else uuid4().hex,
                parent_id=parent.id if parent is not None else None,
                name=options.name[:128],
                attributes=copy_attributes(options.attributes),
                started_at_ms=_now_ms(),
            )
            self._spans.append(state)
        except Exception:
            return await NOOP_TELEMETRY_CONTEXT.start_span(options, callback)

        span = _MemorySpan(self, state)
        try:
            result = callback(span)
            value = (
                await cast("object", result)  # type: ignore[misc]
                if inspect.isawaitable(result)
                else result
            )
        except BaseException as error:
            if not state.explicit_status:
                state.status = SpanStatus(
                    status="error",
                    error=TelemetryError(type(error).__name__, str(error)[:512] or None),
                )
            state.ended_at_ms = _now_ms()
            raise
        state.ended_at_ms = _now_ms()
        return value

    def get_spans(self) -> tuple[RecordedTelemetrySpan, ...]:
        return tuple(
            RecordedTelemetrySpan(
                id=span.id,
                trace_id=span.trace_id,
                parent_id=span.parent_id,
                name=span.name,
                attributes=dict(span.attributes),
                events=tuple(
                    RecordedTelemetryEvent(
                        name=event.name,
                        attributes=dict(event.attributes),
                        timestamp_ms=event.timestamp_ms,
                    )
                    for event in span.events
                ),
                status=span.status,
                started_at_ms=span.started_at_ms,
                ended_at_ms=span.ended_at_ms,
                duration_ms=(
                    None
                    if span.ended_at_ms is None
                    else max(0, span.ended_at_ms - span.started_at_ms)
                ),
                settled=span.ended_at_ms is not None,
            )
            for span in self._spans
        )


__all__ = ["InMemoryTelemetryContext"]

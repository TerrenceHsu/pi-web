"""Backend-neutral Telemetry contracts.

The callback-shaped API mirrors pi telemetry: a span always settles when the
callback returns, raises, or is cancelled.  Implementations must be passive;
recording failures are never allowed to change the application result.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar, runtime_checkable

AttributeScalar = str | int | float | bool
AttributeValue = (
    AttributeScalar
    | tuple[str, ...]
    | tuple[int, ...]
    | tuple[float, ...]
    | tuple[bool, ...]
)
SpanAttributes = Mapping[str, AttributeValue | None]


@dataclass(frozen=True, slots=True)
class SpanOptions:
    name: str
    attributes: SpanAttributes = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TelemetryError:
    name: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SpanStatus:
    status: Literal["ok", "error"] = "ok"
    error: TelemetryError | None = None


@dataclass(frozen=True, slots=True)
class RecordedTelemetryEvent:
    name: str
    attributes: Mapping[str, AttributeValue]
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class RecordedTelemetrySpan:
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    attributes: Mapping[str, AttributeValue]
    events: tuple[RecordedTelemetryEvent, ...]
    status: SpanStatus
    started_at_ms: int
    ended_at_ms: int | None
    duration_ms: int | None
    settled: bool


T = TypeVar("T")
SpanCallback = Callable[["TelemetrySpan"], T | Awaitable[T]]


@runtime_checkable
class TelemetryContext(Protocol):
    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T: ...


@runtime_checkable
class TelemetrySpan(TelemetryContext, Protocol):
    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None: ...

    def set_attributes(self, attributes: SpanAttributes) -> None: ...

    def set_status(self, status: SpanStatus) -> None: ...


@runtime_checkable
class TelemetryReader(Protocol):
    async def summary(self, *, since_ms: int) -> dict[str, object]: ...

    async def list_spans(
        self,
        *,
        since_ms: int,
        limit: int,
        status: str | None = None,
        name: str | None = None,
        account_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, object]]: ...

    async def get_span(self, span_id: str) -> dict[str, object] | None: ...


__all__ = [
    "AttributeScalar",
    "AttributeValue",
    "RecordedTelemetryEvent",
    "RecordedTelemetrySpan",
    "SpanAttributes",
    "SpanCallback",
    "SpanOptions",
    "SpanStatus",
    "TelemetryContext",
    "TelemetryError",
    "TelemetryReader",
    "TelemetrySpan",
]

"""Zero-overhead application fallback for disabled Telemetry."""

from __future__ import annotations

import inspect
from typing import cast

from .types import SpanAttributes, SpanCallback, SpanOptions, SpanStatus, T, TelemetrySpan


class _NoopTelemetryContext:
    async def start_span(self, options: SpanOptions, callback: SpanCallback[T]) -> T:
        del options
        result = callback(self)
        if inspect.isawaitable(result):
            return await cast("object", result)  # type: ignore[misc, no-any-return]
        return result

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        del name, attributes

    def set_attributes(self, attributes: SpanAttributes) -> None:
        del attributes

    def set_status(self, status: SpanStatus) -> None:
        del status


NOOP_TELEMETRY_CONTEXT: TelemetrySpan = _NoopTelemetryContext()

__all__ = ["NOOP_TELEMETRY_CONTEXT"]

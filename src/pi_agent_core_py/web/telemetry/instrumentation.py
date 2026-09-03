"""Safe Agent-event projection into request Telemetry spans."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ...agent.events import (
    AgentAbortEvent,
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from ...agent.messages import AssistantMessage
from ...telemetry import AttributeValue, TelemetrySpan


def _monotonic_ms() -> int:
    return time.perf_counter_ns() // 1_000_000


@dataclass(slots=True)
class RequestTelemetryStats:
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    provider: str = ""
    model: str = ""
    _tool_started_ms: dict[str, int] = field(default_factory=dict)

    def end_attributes(self) -> dict[str, AttributeValue]:
        return {
            "turns": self.turns,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost": self.cost,
            "provider": self.provider,
            "model": self.model,
        }


def record_agent_event(
    span: TelemetrySpan,
    event: AgentEvent,
    stats: RequestTelemetryStats,
) -> None:
    """Record structural metadata only; never record content or tool arguments."""

    try:
        if isinstance(event, TurnStartEvent):
            span.add_event("turn.start", {"turn_index": stats.turns + 1})
            return
        if isinstance(event, TurnEndEvent):
            stats.turns += 1
            span.add_event(
                "turn.end",
                {
                    "turn_index": stats.turns,
                    "tool_results": len(event.tool_results),
                    "stop_reason": event.message.stop_reason,
                    "has_error": event.message.error_message is not None,
                },
            )
            return
        if isinstance(event, ToolExecutionStartEvent):
            stats._tool_started_ms[event.tool_call.id] = _monotonic_ms()
            span.add_event("tool.start", {"tool_name": event.tool_call.name})
            return
        if isinstance(event, ToolExecutionEndEvent):
            stats.tool_calls += 1
            if event.result.is_error:
                stats.tool_errors += 1
            started = stats._tool_started_ms.pop(event.tool_call.id, None)
            duration = None if started is None else max(0, _monotonic_ms() - started)
            span.add_event(
                "tool.end",
                {
                    "tool_name": event.tool_call.name,
                    "is_error": event.result.is_error,
                    "terminate": event.result.terminate,
                    "duration_ms": duration,
                },
            )
            return
        if isinstance(event, MessageEndEvent) and isinstance(
            event.message, AssistantMessage
        ):
            message = event.message
            stats.model_calls += 1
            stats.provider = message.provider
            stats.model = message.model
            stats.input_tokens += message.usage.input
            stats.output_tokens += message.usage.output
            stats.cache_read_tokens += message.usage.cache_read
            stats.cache_write_tokens += message.usage.cache_write
            if message.usage.cost is not None:
                stats.cost += message.usage.cost.total
            metrics = message.generation_metrics
            span.add_event(
                "model.end",
                {
                    "provider": message.provider,
                    "model": message.model,
                    "stop_reason": message.stop_reason,
                    "has_error": message.error_message is not None,
                    "input_tokens": message.usage.input,
                    "output_tokens": message.usage.output,
                    "cache_read_tokens": message.usage.cache_read,
                    "cache_write_tokens": message.usage.cache_write,
                    "latency_ms": metrics.latency_ms if metrics is not None else None,
                    "time_to_first_token_ms": (
                        metrics.time_to_first_token_ms if metrics is not None else None
                    ),
                },
            )
            return
        if isinstance(event, AgentAbortEvent):
            # Abort reasons can be supplied by extensions and may contain
            # user/provider text.  Persist only whether a reason was supplied.
            span.add_event("agent.abort", {"has_reason": bool(event.reason)})
            return
        if isinstance(event, AgentEndEvent):
            span.add_event("agent.end", {"new_message_count": len(event.new_messages)})
    except Exception:
        # Telemetry must not affect Agent event delivery.
        return


__all__ = ["RequestTelemetryStats", "record_agent_event"]

"""Built-in offline evaluation suites."""

from ..harness import EvalSuite
from .context_compaction import build_suite as context_compaction_suite
from .resource_composition import build_suite as resource_composition_suite
from .session_reload import build_suite as session_reload_suite
from .smoke import build_suite as smoke_suite
from .telemetry_safety import build_suite as telemetry_safety_suite
from .tool_lifecycle import build_suite as tool_lifecycle_suite


def built_in_suites() -> tuple[EvalSuite, ...]:
    return (
        smoke_suite(),
        tool_lifecycle_suite(),
        resource_composition_suite(),
        session_reload_suite(),
        telemetry_safety_suite(),
        context_compaction_suite(),
    )


__all__ = [
    "built_in_suites",
    "context_compaction_suite",
    "resource_composition_suite",
    "session_reload_suite",
    "smoke_suite",
    "telemetry_safety_suite",
    "tool_lifecycle_suite",
]

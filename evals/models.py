"""JSON-safe contracts shared by local eval harnesses, judges and reporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias

from coding_agent_app.core import CodingAgentMode

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class PromptStep:
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("prompt content must not be empty")


@dataclass(frozen=True, slots=True)
class ReloadStep:
    """Recreate the product Session while retaining its durable Session ID."""


EvalStep: TypeAlias = PromptStep | ReloadStep


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    steps: tuple[EvalStep, ...]
    mode: CodingAgentMode = "direct"
    coding_tool_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    system_prompt_suffix: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("eval case id must not be empty")
        if not self.steps:
            raise ValueError("eval case must contain at least one step")
        if not any(isinstance(step, PromptStep) for step in self.steps):
            raise ValueError("eval case must contain at least one prompt step")


@dataclass(frozen=True, slots=True)
class EvalError:
    type: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class EvalToolCall:
    id: str
    name: str
    arguments: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalToolResult:
    tool_call_id: str
    name: str
    text: str
    is_error: bool
    terminate: bool


@dataclass(frozen=True, slots=True)
class EvalUsage:
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    estimated_cost: float | None = None

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "estimated_cost": self.estimated_cost,
        }


@dataclass(frozen=True, slots=True)
class EvalReloadCheck:
    before_message_count: int
    after_message_count: int


@dataclass(frozen=True, slots=True)
class EvalObservation:
    eval_set: str
    case_id: str
    harness: str
    repetition: int
    group_key: str
    response: str | None
    stop_reason: str | None
    messages: tuple[dict[str, Any], ...]
    tool_calls: tuple[EvalToolCall, ...]
    tool_results: tuple[EvalToolResult, ...]
    system_prompts: tuple[str, ...]
    workspace_files: tuple[str, ...]
    workspace_text: dict[str, str]
    resource_skills: tuple[str, ...]
    resource_tools: tuple[str, ...]
    resource_mcp_tools: tuple[str, ...]
    context_fragments: tuple[str, ...]
    workspace_bound: bool
    telemetry: tuple[dict[str, JsonValue], ...]
    reload_checks: tuple[EvalReloadCheck, ...]
    message_counts: tuple[int, ...]
    usage: EvalUsage
    total_ms: float
    error: EvalError | None = None

    @property
    def infrastructure_ok(self) -> bool:
        return self.error is None

    def to_record(self, *, include_content: bool = False) -> dict[str, JsonValue]:
        record: dict[str, JsonValue] = {
            "schema_version": 1,
            "eval_set": self.eval_set,
            "case_id": self.case_id,
            "harness": self.harness,
            "repetition": self.repetition,
            "group_key": self.group_key,
            "stop_reason": self.stop_reason,
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    **({"arguments": call.arguments} if include_content else {}),
                }
                for call in self.tool_calls
            ],
            "tool_results": [
                {
                    "tool_call_id": result.tool_call_id,
                    "name": result.name,
                    "is_error": result.is_error,
                    "terminate": result.terminate,
                    **({"text": result.text} if include_content else {}),
                }
                for result in self.tool_results
            ],
            "workspace_file_count": len(self.workspace_files),
            "resource_skills": list(self.resource_skills),
            "resource_tools": list(self.resource_tools),
            "resource_mcp_tools": list(self.resource_mcp_tools),
            "workspace_bound": self.workspace_bound,
            "telemetry": [_safe_telemetry(span) for span in self.telemetry],
            "reload_checks": [
                {
                    "before_message_count": check.before_message_count,
                    "after_message_count": check.after_message_count,
                }
                for check in self.reload_checks
            ],
            "message_counts": list(self.message_counts),
            "usage": self.usage.safe_dict(),
            "total_ms": self.total_ms,
            "infrastructure_ok": self.infrastructure_ok,
            "error_type": self.error.type if self.error is not None else None,
        }
        if include_content:
            record.update(
                {
                    "response": self.response,
                    "messages": _json_value(list(self.messages)),
                    "system_prompts": list(self.system_prompts),
                    "telemetry": list(self.telemetry),
                    "workspace_files": list(self.workspace_files),
                    "workspace_text": dict(self.workspace_text),
                    "context_fragments": list(self.context_fragments),
                    "error_message": self.error.message if self.error is not None else None,
                }
            )
        return record


@dataclass(frozen=True, slots=True)
class EvalJudgeResult:
    name: str
    score: float
    rationale: str
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("judge name must not be empty")
        if not 0 <= self.score <= 1:
            raise ValueError("judge score must be between 0 and 1")

    def to_record(self, *, include_content: bool = False) -> dict[str, JsonValue]:
        record: dict[str, JsonValue] = {
            "name": self.name,
            "score": self.score,
        }
        if include_content:
            record.update(
                {
                    "rationale": self.rationale,
                    "metadata": self.metadata,
                }
            )
        return record


@dataclass(frozen=True, slots=True)
class EvalScoredObservation:
    observation: EvalObservation
    judgments: tuple[EvalJudgeResult, ...]

    @property
    def score(self) -> float | None:
        if not self.judgments:
            return None
        return sum(result.score for result in self.judgments) / len(self.judgments)

    def to_record(self, *, include_content: bool = False) -> dict[str, JsonValue]:
        record = self.observation.to_record(include_content=include_content)
        record["score"] = self.score
        record["judgments"] = [
            result.to_record(include_content=include_content)
            for result in self.judgments
        ]
        return record


def _json_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _safe_telemetry(span: dict[str, JsonValue]) -> dict[str, JsonValue]:
    raw_events = span.get("events")
    events = raw_events if isinstance(raw_events, list) else []
    return {
        "name": span.get("name"),
        "status": span.get("status"),
        "duration_ms": span.get("duration_ms"),
        "settled": span.get("settled"),
        "events": [
            {"name": event.get("name")}
            for event in events
            if isinstance(event, dict)
        ],
    }


__all__ = [
    "EvalCase",
    "EvalError",
    "EvalJudgeResult",
    "EvalObservation",
    "EvalReloadCheck",
    "EvalScoredObservation",
    "EvalStep",
    "EvalToolCall",
    "EvalToolResult",
    "EvalUsage",
    "JsonValue",
    "PromptStep",
    "ReloadStep",
]

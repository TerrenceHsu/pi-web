"""Deterministic judges for offline agent orchestration evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import jsonschema

from .models import EvalCase, EvalJudgeResult, EvalObservation, JsonValue


class EvalJudge(Protocol):
    @property
    def name(self) -> str: ...

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult: ...


def _result(
    name: str,
    passed: bool,
    success: str,
    failure: str,
    *,
    metadata: dict[str, JsonValue] | None = None,
) -> EvalJudgeResult:
    return EvalJudgeResult(
        name=name,
        score=1.0 if passed else 0.0,
        rationale=success if passed else failure,
        metadata=metadata or {},
    )


@dataclass(frozen=True, slots=True)
class ExactTextJudge:
    expected: str
    name: str = "exact_text"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        actual = (observation.response or "").strip()
        expected = self.expected.strip()
        return _result(
            self.name,
            actual == expected,
            "response matched expected text",
            "response did not match expected text",
        )


@dataclass(frozen=True, slots=True)
class ContainsTextJudge:
    expected: str
    case_sensitive: bool = True
    name: str = "contains_text"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        response = observation.response or ""
        expected = self.expected
        if not self.case_sensitive:
            response = response.casefold()
            expected = expected.casefold()
        return _result(
            self.name,
            expected in response,
            "response contained expected text",
            "response did not contain expected text",
        )


@dataclass(frozen=True, slots=True)
class StopReasonJudge:
    expected: str = "stop"
    name: str = "stop_reason"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        return _result(
            self.name,
            observation.stop_reason == self.expected,
            "agent reached the expected terminal state",
            "agent reached an unexpected terminal state",
            metadata={"actual": observation.stop_reason, "expected": self.expected},
        )


@dataclass(frozen=True, slots=True)
class JsonSchemaJudge:
    schema: dict[str, Any]
    name: str = "json_schema"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        try:
            payload = json.loads(observation.response or "")
            jsonschema.validate(payload, self.schema)
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            return _result(
                self.name,
                False,
                "response matched the JSON schema",
                "response was not valid JSON for the required schema",
                metadata={"error_type": type(exc).__name__},
            )
        return _result(
            self.name,
            True,
            "response matched the JSON schema",
            "response was not valid JSON for the required schema",
        )


@dataclass(frozen=True, slots=True)
class ToolSequenceJudge:
    expected: tuple[str, ...]
    name: str = "tool_sequence"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        actual = tuple(call.name for call in observation.tool_calls)
        return _result(
            self.name,
            actual == self.expected,
            "tool call sequence matched",
            "tool call sequence differed",
            metadata={
                "actual": [str(item) for item in actual],
                "expected": [str(item) for item in self.expected],
            },
        )


@dataclass(frozen=True, slots=True)
class NoToolErrorsJudge:
    name: str = "no_tool_errors"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        count = sum(result.is_error for result in observation.tool_results)
        return _result(
            self.name,
            count == 0,
            "all tool calls completed without error",
            "one or more tool calls failed",
            metadata={"tool_errors": count},
        )


@dataclass(frozen=True, slots=True)
class WorkspaceFilesJudge:
    required: tuple[str, ...]
    name: str = "workspace_files"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        missing = sorted(set(self.required) - set(observation.workspace_files))
        return _result(
            self.name,
            not missing,
            "required workspace files were produced",
            "required workspace files were missing",
            metadata={"missing": [str(item) for item in missing]},
        )


@dataclass(frozen=True, slots=True)
class ResourceAssemblyJudge:
    required_skills: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    required_mcp_tools: tuple[str, ...] = ()
    required_prompt_fragments: tuple[str, ...] = ()
    provider: str | None = "fake"
    model: str | None = "fake-1"
    require_workspace: bool = True
    name: str = "resource_assembly"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        failures: list[str] = []
        if set(self.required_skills) - set(observation.resource_skills):
            failures.append("skills")
        if set(self.required_tools) - set(observation.resource_tools):
            failures.append("tools")
        if set(self.required_mcp_tools) - set(observation.resource_mcp_tools):
            failures.append("mcp_tools")
        combined_prompt = "\n".join(observation.system_prompts)
        if any(fragment not in combined_prompt for fragment in self.required_prompt_fragments):
            failures.append("prompt")
        if self.provider is not None and observation.usage.provider != self.provider:
            failures.append("provider")
        if self.model is not None and observation.usage.model != self.model:
            failures.append("model")
        if self.require_workspace and not observation.workspace_bound:
            failures.append("workspace")
        return _result(
            self.name,
            not failures,
            "provider, skills, MCP, workspace and prompt resources were assembled",
            "one or more product resources were not assembled",
            metadata={"failed_components": [str(item) for item in failures]},
        )


@dataclass(frozen=True, slots=True)
class SessionReloadJudge:
    minimum_reloads: int = 1
    name: str = "session_reload"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        stable = len(observation.reload_checks) >= self.minimum_reloads and all(
            check.before_message_count == check.after_message_count
            for check in observation.reload_checks
        )
        return _result(
            self.name,
            stable,
            "durable messages survived every Session reload",
            "Session reload lost or duplicated durable messages",
            metadata={
                "reloads": len(observation.reload_checks),
                "checks": [
                    [check.before_message_count, check.after_message_count]
                    for check in observation.reload_checks
                ],
            },
        )


@dataclass(frozen=True, slots=True)
class TelemetryPrivacyJudge:
    forbidden_fragments: tuple[str, ...]
    name: str = "telemetry_privacy"

    def evaluate(
        self,
        case: EvalCase,
        observation: EvalObservation,
    ) -> EvalJudgeResult:
        del case
        serialized = json.dumps(observation.telemetry, ensure_ascii=False)
        leaked = [fragment for fragment in self.forbidden_fragments if fragment in serialized]
        required_spans = {"coding_agent.request", "agent.run"}
        recorded_spans = {str(span.get("name")) for span in observation.telemetry}
        missing = sorted(required_spans - recorded_spans)
        passed = not leaked and not missing
        return _result(
            self.name,
            passed,
            "telemetry retained operational metadata without evaluated content",
            "telemetry leaked content or omitted required spans",
            metadata={
                "leak_count": len(leaked),
                "missing_spans": [str(item) for item in missing],
            },
        )


__all__ = [
    "ContainsTextJudge",
    "EvalJudge",
    "ExactTextJudge",
    "JsonSchemaJudge",
    "NoToolErrorsJudge",
    "ResourceAssemblyJudge",
    "SessionReloadJudge",
    "StopReasonJudge",
    "TelemetryPrivacyJudge",
    "ToolSequenceJudge",
    "WorkspaceFilesJudge",
]

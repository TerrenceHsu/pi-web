"""Terminal structured tools used as the only role-to-orchestrator protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from pi_agent_core_py.agent.tooling import AgentTool, ToolResult, ToolUpdateCallback
from pi_agent_core_py.ai.messages import TextContent

from .models import (
    PlanSpec,
    TaskBlockedReport,
    TaskExecutionReport,
    VerificationReport,
)

SubmissionT = TypeVar("SubmissionT", bound=BaseModel)


@dataclass
class SubmissionBox(Generic[SubmissionT]):
    value: SubmissionT | None = None

    def submit(self, value: SubmissionT) -> bool:
        if self.value is not None:
            return False
        self.value = value
        return True


class _SubmissionTool(AgentTool, Generic[SubmissionT]):
    execution_mode = "sequential"

    def __init__(
        self,
        *,
        name: str,
        label: str,
        description: str,
        model: type[SubmissionT],
        box: SubmissionBox[SubmissionT],
    ) -> None:
        self.name = name
        self.label = label
        self.description = description
        self.parameters = model.model_json_schema()
        self._model = model
        self._box = box

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        del on_update
        if signal is not None and signal.is_set():
            return self._result(tool_call_id, "submission cancelled", error=True)
        try:
            value = self._model.model_validate(args)
        except ValidationError:
            return self._result(
                tool_call_id,
                "structured submission does not match the required schema",
                error=True,
            )
        if not self._box.submit(value):
            return self._result(tool_call_id, "a terminal submission already exists", error=True)
        return self._result(tool_call_id, "structured submission accepted", terminate=True)

    def _result(
        self,
        tool_call_id: str,
        text: str,
        *,
        error: bool = False,
        terminate: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            is_error=error,
            terminate=terminate,
        )


ExecutorSubmissionKind = Literal["complete", "blocked"]


@dataclass(frozen=True)
class ExecutorSubmission:
    kind: ExecutorSubmissionKind
    report: TaskExecutionReport | TaskBlockedReport


class ExecutorSubmissionBox:
    def __init__(self) -> None:
        self.value: ExecutorSubmission | None = None

    def submit(self, submission: ExecutorSubmission) -> bool:
        if self.value is not None:
            return False
        self.value = submission
        return True


class _ExecutorSubmissionTool(AgentTool):
    execution_mode = "sequential"

    def __init__(
        self,
        *,
        kind: ExecutorSubmissionKind,
        model: type[TaskExecutionReport] | type[TaskBlockedReport],
        box: ExecutorSubmissionBox,
    ) -> None:
        self._kind = kind
        self._model = model
        self._box = box
        self.name = "plan_task_complete" if kind == "complete" else "plan_task_blocked"
        self.label = "Complete Plan Task" if kind == "complete" else "Block Plan Task"
        self.description = (
            "Submit the task execution evidence and stop this Executor role."
            if kind == "complete"
            else "Report a concrete blocker and stop this Executor role."
        )
        self.parameters = model.model_json_schema()

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        del on_update
        if signal is not None and signal.is_set():
            return self._result(tool_call_id, "submission cancelled", error=True)
        try:
            report = self._model.model_validate(args)
        except ValidationError:
            return self._result(tool_call_id, "invalid task report", error=True)
        submission = ExecutorSubmission(kind=self._kind, report=report)
        if not self._box.submit(submission):
            return self._result(tool_call_id, "a terminal submission already exists", error=True)
        return self._result(tool_call_id, "task report accepted", terminate=True)

    def _result(
        self,
        tool_call_id: str,
        text: str,
        *,
        error: bool = False,
        terminate: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text=text)],
            is_error=error,
            terminate=terminate,
        )


def create_planner_submission_tool(
    box: SubmissionBox[PlanSpec],
) -> AgentTool:
    return _SubmissionTool(
        name="plan_submit",
        label="Submit Plan",
        description=(
            "Submit the complete dependency-aware coding plan. This is the Planner's "
            "terminal action."
        ),
        model=PlanSpec,
        box=box,
    )


def create_executor_submission_tools(box: ExecutorSubmissionBox) -> list[AgentTool]:
    return [
        _ExecutorSubmissionTool(kind="complete", model=TaskExecutionReport, box=box),
        _ExecutorSubmissionTool(kind="blocked", model=TaskBlockedReport, box=box),
    ]


def create_verifier_submission_tool(
    box: SubmissionBox[VerificationReport],
) -> AgentTool:
    return _SubmissionTool(
        name="plan_verdict",
        label="Submit Verification Verdict",
        description=(
            "Submit a passed or failed verdict against the task acceptance criteria. "
            "This is the Verifier's terminal action."
        ),
        model=VerificationReport,
        box=box,
    )


def require_executor_report(
    submission: ExecutorSubmission,
) -> TaskExecutionReport:
    if submission.kind != "complete":
        raise TypeError("executor submission is not complete")
    return cast(TaskExecutionReport, submission.report)


def require_blocked_report(
    submission: ExecutorSubmission,
) -> TaskBlockedReport:
    if submission.kind != "blocked":
        raise TypeError("executor submission is not blocked")
    return cast(TaskBlockedReport, submission.report)


__all__ = [
    "ExecutorSubmission",
    "ExecutorSubmissionBox",
    "SubmissionBox",
    "create_executor_submission_tools",
    "create_planner_submission_tool",
    "create_verifier_submission_tool",
    "require_blocked_report",
    "require_executor_report",
]

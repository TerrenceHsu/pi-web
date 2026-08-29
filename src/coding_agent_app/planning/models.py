"""Strict, browser-safe DTOs for Plan Mode."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PlanRunStatus = Literal[
    "planning",
    "awaiting_plan_approval",
    "executing",
    "verifying",
    "awaiting_artifact_approval",
    "completed",
    "blocked",
    "failed",
    "cancelled",
    "interrupted",
]
PlanTaskStatus = Literal[
    "pending",
    "executing",
    "awaiting_verification",
    "passed",
    "failed",
    "blocked",
]
VerificationFailureClass = Literal[
    "retry_executor",
    "replan_required",
    "user_input_required",
]

SafeText = Annotated[str, Field(min_length=1, max_length=4_000)]
SafePath = Annotated[str, Field(min_length=1, max_length=500)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanTaskSpec(_StrictModel):
    """One dependency-aware unit proposed by Planner."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=160)
    objective: SafeText
    dependencies: tuple[str, ...] = Field(default=(), max_length=12)
    acceptance_criteria: tuple[SafeText, ...] = Field(min_length=1, max_length=12)
    allowed_paths: tuple[SafePath, ...] = Field(default=(), max_length=32)


class PlanSpec(_StrictModel):
    """Immutable version submitted by Planner."""

    goal: SafeText
    summary: str = Field(min_length=1, max_length=2_000)
    tasks: tuple[PlanTaskSpec, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_graph(self) -> PlanSpec:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique")
        known = set(task_ids)
        graph: dict[str, tuple[str, ...]] = {}
        for task in self.tasks:
            if task.id in task.dependencies:
                raise ValueError(f"task {task.id!r} cannot depend on itself")
            missing = set(task.dependencies) - known
            if missing:
                raise ValueError(f"task {task.id!r} has unknown dependencies")
            graph[task.id] = task.dependencies

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependency graph must be acyclic")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class TaskExecutionReport(_StrictModel):
    summary: SafeText
    changed_paths: tuple[SafePath, ...] = Field(default=(), max_length=100)
    validation_summary: str = Field(min_length=1, max_length=4_000)


class TaskBlockedReport(_StrictModel):
    reason: SafeText
    suggestions: tuple[SafeText, ...] = Field(default=(), max_length=8)


class VerificationReport(_StrictModel):
    passed: bool
    reason: SafeText
    suggestions: tuple[SafeText, ...] = Field(default=(), max_length=8)
    classification: VerificationFailureClass | None = None

    @model_validator(mode="after")
    def validate_verdict(self) -> VerificationReport:
        if self.passed and self.classification is not None:
            raise ValueError("passed verdict cannot have a failure classification")
        if not self.passed and self.classification is None:
            raise ValueError("failed verdict requires a failure classification")
        return self


class PlanTaskView(_StrictModel):
    id: str
    ordinal: int
    title: str
    objective: str
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    status: PlanTaskStatus
    attempt: int
    execution: TaskExecutionReport | None = None
    verification: VerificationReport | None = None
    blocked: TaskBlockedReport | None = None


class PlanRunView(_StrictModel):
    id: str
    session_id: str
    request_id: str
    goal: str
    status: PlanRunStatus
    plan_version: int
    summary: str | None = None
    sandbox_operation_id: str | None = None
    artifact_id: str | None = None
    failure_code: str | None = None
    tasks: tuple[PlanTaskView, ...] = ()
    created_at_ms: int
    updated_at_ms: int


class PlanEvent(_StrictModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    plan_version: int
    task_id: str | None = None
    attempt: int | None = None
    causation_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    created_at_ms: int


class PlanRunResult(_StrictModel):
    run: PlanRunView
    summary: str
    sandbox: dict[str, object] | None = None


__all__ = [
    "PlanEvent",
    "PlanRunResult",
    "PlanRunStatus",
    "PlanRunView",
    "PlanSpec",
    "PlanTaskSpec",
    "PlanTaskStatus",
    "PlanTaskView",
    "TaskBlockedReport",
    "TaskExecutionReport",
    "VerificationFailureClass",
    "VerificationReport",
]

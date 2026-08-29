"""Central, sequential Planner–Executor–Verifier state machine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import ModelClient
from pi_agent_core_py.policy import AllowAllToolPermissionPolicy
from pi_agent_core_py.tools import AgentTool, ToolRegistry
from pi_agent_core_py.web.coding_sandbox.automation import (
    CodingSandboxAutomation,
    CodingSandboxAutomationError,
)

from .models import (
    PlanRunResult,
    PlanRunView,
    PlanSpec,
    PlanTaskView,
    TaskBlockedReport,
    TaskExecutionReport,
    VerificationReport,
)
from .store import PlanStore
from .tools import (
    ExecutorSubmissionBox,
    SubmissionBox,
    create_executor_submission_tools,
    create_planner_submission_tool,
    create_verifier_submission_tool,
    require_blocked_report,
    require_executor_report,
)

ApprovalWaiter = Callable[[str], Awaitable[None]]
PlanNotifier = Callable[[str, PlanRunView], Awaitable[None]]
Cancelled = Callable[[], bool]

_READ_ONLY_CODING_TOOLS = frozenset(
    {"coding_list_files", "coding_read_file", "coding_search", "coding_diff"}
)

PLANNER_SYSTEM_PROMPT = """You are the Planner in a coding-agent control system.
Inspect the available read-only context when useful. Decompose the user's goal into a small,
dependency-aware list of observable tasks. Each task needs concrete acceptance criteria and
optional allowed workspace paths. Do not implement code. Submit exactly one complete plan with
plan_submit; prose is not a terminal result. Never include hidden reasoning in the plan.
"""

EXECUTOR_SYSTEM_PROMPT = """You are the Executor in a coding-agent control system.
Implement only the current approved task in the existing isolated Sandbox. Respect its allowed
paths and acceptance criteria, inspect before editing, and run proportionate validation. Use
exactly one terminal tool: plan_task_complete with truthful evidence, or plan_task_blocked with
a concrete blocker. Never claim publication to the Session Workspace.
"""

VERIFIER_SYSTEM_PROMPT = """You are the independent Verifier in a coding-agent control system.
You have read-only Sandbox inspection tools. Check the current task against every acceptance
criterion and the actual cumulative diff; do not trust the Executor's prose alone. Submit exactly
one plan_verdict. A failed verdict must give a concise reason, actionable suggestions, and one of
retry_executor, replan_required, or user_input_required. Never modify files or run code.
"""


class PlanOrchestrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlanOrchestratorConfig:
    max_executor_attempts: int = 2
    role_poll_seconds: float = 0.05
    role_max_turns: int = 20


class PlanOrchestrator:
    """Own role isolation, approvals, retries, and the final Sandbox barrier."""

    def __init__(
        self,
        *,
        store: PlanStore,
        client: ModelClient,
        automation: CodingSandboxAutomation,
        read_tools: Iterable[AgentTool],
        coding_tools: Iterable[AgentTool],
        wait_for_approval: ApprovalWaiter,
        notify: PlanNotifier,
        cancelled: Cancelled,
        config: PlanOrchestratorConfig | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._automation = automation
        self._read_tools = tuple(read_tools)
        self._coding_tools = tuple(coding_tools)
        self._wait_for_approval = wait_for_approval
        self._notify = notify
        self._cancelled = cancelled
        self._config = config or PlanOrchestratorConfig()

    async def run(
        self,
        *,
        session_id: str,
        request_id: str,
        goal: str,
        planning_context: str | None = None,
    ) -> PlanRunResult:
        run = await self._store.create_run(session_id, request_id, goal)
        await self._notify("plan_run_started", run)
        try:
            plan_box: SubmissionBox[PlanSpec] = SubmissionBox()
            planner_tools = self._registry(
                [*self._read_tools, create_planner_submission_tool(plan_box)]
            )
            await self._run_role(
                system_prompt=PLANNER_SYSTEM_PROMPT,
                prompt=(
                    f"User goal:\n{goal}"
                    if not planning_context
                    else f"User goal:\n{goal}\n\nWorkspace context:\n{planning_context}"
                ),
                tools=planner_tools,
            )
            if plan_box.value is None:
                raise PlanOrchestrationError(
                    "planner_missing_submission", "Planner did not submit a valid plan."
                )
            run = await self._store.save_plan(run.id, plan_box.value)
            await self._notify("plan_created", run)

            await self._wait_for_approval(run.id)
            self._raise_if_cancelled()
            run = await self._store.get_run(run.id)
            if run.status != "executing":
                raise PlanOrchestrationError(
                    "plan_not_approved", "Plan execution did not receive approval."
                )
            await self._notify("plan_approved", run)

            operation = await self._automation.prepare(
                session_id,
                cancelled=self._cancelled,
            )
            run = await self._store.set_sandbox_operation(run.id, operation.operation_id)
            await self._notify("plan_sandbox_ready", run)

            passed: set[str] = set()
            pending = list(run.tasks)
            while pending:
                task = next(
                    (candidate for candidate in pending if set(candidate.dependencies) <= passed),
                    None,
                )
                if task is None:
                    raise PlanOrchestrationError(
                        "plan_dependency_deadlock", "No executable plan task remains."
                    )
                completed = await self._execute_and_verify(run.id, task, goal=goal)
                if not completed:
                    blocked = await self._store.get_run(run.id)
                    return PlanRunResult(
                        run=blocked,
                        summary=self._summary(blocked),
                        sandbox={
                            "operation_id": operation.operation_id,
                            "status": "ready",
                            "workspace_revision": operation.workspace_revision,
                            "artifact_id": None,
                            "approval_required": False,
                        },
                    )
                passed.add(task.id)
                pending.remove(task)

            frozen = await self._automation.validate_and_freeze(
                operation.operation_id,
                cancelled=self._cancelled,
            )
            run = await self._store.mark_artifact_ready(
                run.id,
                artifact_id=frozen.artifact_id,
            )
            await self._notify("plan_artifact_ready", run)
            return PlanRunResult(
                run=run,
                summary=self._summary(run),
                sandbox=frozen.public(),
            )
        except PlanOrchestrationError as exc:
            current = await self._store.get_run(run.id)
            if exc.code == "plan_cancelled":
                await self._automation.cancel_if_possible(current.sandbox_operation_id)
            if current.status not in {
                "blocked",
                "failed",
                "cancelled",
                "interrupted",
                "awaiting_artifact_approval",
            }:
                if exc.code == "plan_cancelled":
                    status: Literal["cancelled", "failed"] = "cancelled"
                else:
                    status = "failed"
                current = await self._store.finish(
                    run.id,
                    status,
                    failure_code=exc.code,
                )
                await self._notify(f"plan_{status}", current)
            raise
        except CodingSandboxAutomationError as exc:
            current = await self._store.get_run(run.id)
            status = "cancelled" if exc.code == "coding_request_aborted" else "failed"
            if status == "cancelled":
                await self._automation.cancel_if_possible(current.sandbox_operation_id)
            if current.status not in {
                "blocked",
                "failed",
                "cancelled",
                "interrupted",
                "awaiting_artifact_approval",
            }:
                current = await self._store.finish(
                    run.id,
                    status,
                    failure_code=exc.code,
                )
                await self._notify(f"plan_{status}", current)
            raise
        except Exception:
            current = await self._store.get_run(run.id)
            if current.status not in {
                "blocked",
                "failed",
                "cancelled",
                "interrupted",
                "awaiting_artifact_approval",
            }:
                current = await self._store.finish(
                    run.id,
                    "failed",
                    failure_code="plan_runtime_failed",
                )
                await self._notify("plan_failed", current)
            raise

    async def _execute_and_verify(
        self,
        run_id: str,
        task: PlanTaskView,
        *,
        goal: str,
    ) -> bool:
        feedback: VerificationReport | None = None
        for _ in range(self._config.max_executor_attempts):
            self._raise_if_cancelled()
            run = await self._store.start_task(run_id, task.id)
            current = self._task(run, task.id)
            await self._notify("plan_task_started", run)

            executor_box = ExecutorSubmissionBox()
            executor_tools = self._registry(
                [*self._coding_tools, *create_executor_submission_tools(executor_box)]
            )
            prompt = self._executor_prompt(goal, current, feedback)
            await self._run_role(
                system_prompt=EXECUTOR_SYSTEM_PROMPT,
                prompt=prompt,
                tools=executor_tools,
            )
            if executor_box.value is None:
                raise PlanOrchestrationError(
                    "executor_missing_submission", "Executor did not submit a task report."
                )
            if executor_box.value.kind == "blocked":
                block_report: TaskBlockedReport = require_blocked_report(executor_box.value)
                run = await self._store.block_task(run_id, task.id, block_report)
                await self._notify("plan_task_blocked", run)
                return False

            execution = require_executor_report(executor_box.value)
            run = await self._store.submit_execution(run_id, task.id, execution)
            await self._notify("plan_task_execution_submitted", run)

            verifier_box: SubmissionBox[VerificationReport] = SubmissionBox()
            verifier_tools = self._registry(
                [
                    *(
                        tool
                        for tool in self._coding_tools
                        if tool.name in _READ_ONLY_CODING_TOOLS
                    ),
                    create_verifier_submission_tool(verifier_box),
                ]
            )
            await self._run_role(
                system_prompt=VERIFIER_SYSTEM_PROMPT,
                prompt=self._verifier_prompt(current, execution),
                tools=verifier_tools,
            )
            if verifier_box.value is None:
                raise PlanOrchestrationError(
                    "verifier_missing_submission", "Verifier did not submit a verdict."
                )
            feedback = verifier_box.value
            run = await self._store.submit_verification(run_id, task.id, feedback)
            await self._notify(
                "plan_task_verified" if feedback.passed else "plan_task_rejected",
                run,
            )
            if feedback.passed:
                return True
            if feedback.classification != "retry_executor":
                blocked_run = await self._store.finish(
                    run_id,
                    "blocked",
                    failure_code=feedback.classification,
                )
                await self._notify("plan_blocked", blocked_run)
                return False

        blocked_run = await self._store.finish(
            run_id,
            "blocked",
            failure_code="executor_retry_exhausted",
        )
        await self._notify("plan_blocked", blocked_run)
        return False

    async def _run_role(
        self,
        *,
        system_prompt: str,
        prompt: str,
        tools: ToolRegistry,
    ) -> None:
        self._raise_if_cancelled()
        agent = Agent(
            system_prompt=system_prompt,
            client=self._client,
            tools=tools,
            permission_policy=AllowAllToolPermissionPolicy(),
            tool_execution="sequential",
            max_turns=self._config.role_max_turns,
        )
        role_harness = AgentHarness(
            agent,
            permission_policy=AllowAllToolPermissionPolicy(),
        )
        role_task = asyncio.create_task(role_harness.run_prompt(prompt))
        try:
            while not role_task.done():
                done, _ = await asyncio.wait(
                    {role_task}, timeout=self._config.role_poll_seconds
                )
                if done:
                    break
                if self._cancelled():
                    try:
                        await agent.abort("plan_cancelled")
                    except RuntimeError:
                        pass
                    await asyncio.gather(role_task, return_exceptions=True)
                    raise PlanOrchestrationError("plan_cancelled", "Plan run was cancelled.")
            await role_task
        finally:
            if not role_task.done():
                role_task.cancel()
                try:
                    await role_task
                except asyncio.CancelledError:
                    pass

    def _raise_if_cancelled(self) -> None:
        if self._cancelled():
            raise PlanOrchestrationError("plan_cancelled", "Plan run was cancelled.")

    @staticmethod
    def _registry(tools: Iterable[AgentTool]) -> ToolRegistry:
        unique: dict[str, AgentTool] = {}
        for tool in tools:
            unique[tool.name] = tool
        return ToolRegistry(list(unique.values()))

    @staticmethod
    def _task(run: PlanRunView, task_id: str) -> PlanTaskView:
        return next(task for task in run.tasks if task.id == task_id)

    @staticmethod
    def _executor_prompt(
        goal: str,
        task: PlanTaskView,
        feedback: VerificationReport | None,
    ) -> str:
        payload = {
            "goal": goal,
            "task": task.model_dump(mode="json"),
            "verifier_feedback": (
                feedback.model_dump(mode="json") if feedback is not None else None
            ),
        }
        return "Execute this approved task:\n" + json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _verifier_prompt(task: PlanTaskView, execution: TaskExecutionReport) -> str:
        payload = {
            "task": task.model_dump(mode="json"),
            "executor_report": execution.model_dump(mode="json"),
        }
        return "Verify this task independently:\n" + json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _summary(run: PlanRunView) -> str:
        passed = sum(task.status == "passed" for task in run.tasks)
        if run.status == "awaiting_artifact_approval":
            return (
                f"Plan completed: {passed}/{len(run.tasks)} tasks passed independent "
                "verification. The frozen artifact is awaiting user approval."
            )
        failed_task = next(
            (task for task in run.tasks if task.status in {"failed", "blocked"}),
            None,
        )
        if failed_task is not None and failed_task.verification is not None:
            return (
                f"Plan stopped at {failed_task.title}: "
                f"{failed_task.verification.reason}"
            )
        return f"Plan stopped with status {run.status} after {passed}/{len(run.tasks)} tasks."


__all__ = ["PlanOrchestrationError", "PlanOrchestrator", "PlanOrchestratorConfig"]

"""Request-owned approved Coding/Plan orchestration and frozen-artifact handoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal, TypeVar

from coding_sandbox.lifecycle import ManagedSandboxLifecycle, ManagedSandboxOperationRecord

from ..planning.models import PlanSpec
from ..planning.store import PlanStore
from ..sandbox.automation import AutomatedCodingResult, CodingSandboxAutomation
from .models import ExecutionDenied, digest_json
from .runtime import ExecutionPlan, ExecutionRequest, ExecutionTaskRuntime, PreparedExecution

ExecutionApproval = Callable[[PreparedExecution, PlanSpec | None], Awaitable[bool]]
_Result = TypeVar("_Result")


class ApprovedCodingAutomation(CodingSandboxAutomation):
    """One request, one exact approval, one runtime; never reuse a Session grant."""

    def __init__(
        self,
        lifecycle: ManagedSandboxLifecycle,
        *,
        runtime: ExecutionTaskRuntime,
        request: ExecutionRequest,
        goal: str,
        approve: ExecutionApproval,
        plan_store: PlanStore | None = None,
    ) -> None:
        super().__init__(lifecycle)
        self.runtime, self.request, self.goal = runtime, request, goal
        self._approve, self._plans = approve, plan_store
        self.prepared: PreparedExecution | None = None
        self._adopted = False

    async def request_approval(self, run_id: str | None = None) -> None:
        if self.prepared is not None:
            raise ExecutionDenied("execution_busy")
        previous = await self._lifecycle.latest_for_session(self.request.session_id)
        if previous is not None and not previous.terminal:
            raise ExecutionDenied("previous_artifact_pending")
        plan, spec = None, None
        if run_id is not None:
            if self._plans is None:
                raise ExecutionDenied("plan_approval_required")
            version, spec = await self._plans.execution_plan_spec(run_id)
            plan = ExecutionPlan(
                plan_id=run_id, version=version, sha256=digest_json(spec.model_dump(mode="json"))
            )
        self.prepared = await self.runtime.prepare(self.request, goal=self.goal, plan=plan)
        if not await self._approve(self.prepared, spec):
            await self.runtime.store.terminate(self.prepared.scope.identity, state="denied")
            raise ExecutionDenied("execution_denied")
        # The approval port MUST commit the grant (and Plan) before returning true.
        grant = await self.runtime.store.get(self.prepared.scope.identity)
        if grant.state != "approved":
            raise ExecutionDenied("approval_required")

    async def prepare(
        self,
        session_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ManagedSandboxOperationRecord:
        self._raise_if_cancelled(cancelled)
        if session_id != self.request.session_id or self.prepared is None or self._adopted:
            raise ExecutionDenied("approval_required")
        prepared = self.prepared
        operation = await self.runtime.start(prepared.scope.identity)

        async def finish() -> None:
            await self.runtime.finish(prepared.scope.identity)

        record = await self._lifecycle.adopt_approved_operation(
            session_id=session_id,
            operation=operation,
            baseline=prepared.baseline,
            config_revision=prepared.profile.config_revision,
            close_runtime=finish,
            # Docker publication has a separate stage-3 gate. Retain its diff for review.
            publish_available=prepared.profile.backend == "e2b",
        )
        self._adopted = True
        return record

    @asynccontextmanager
    async def role_context(
        self,
        role: Literal["executor", "planner", "verifier", "read_only"],
    ) -> AsyncIterator[None]:
        if role == "planner":
            # Planning never owns execution authority, even when no tools are exposed.
            from .context import clear_execution_context

            with clear_execution_context():
                yield
        else:
            if self.prepared is None:
                raise ExecutionDenied("approval_required")
            async with self.runtime.bind(self.prepared.scope.identity, role=role):
                yield

    async def run_with_no_change_retry(
        self,
        operation_id: str,
        run_attempt: Callable[[bool], Awaitable[_Result]],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> _Result:
        async with self.role_context("executor"):
            return await super().run_with_no_change_retry(
                operation_id,
                run_attempt,
                cancelled=cancelled,
            )

    async def validate_and_freeze(
        self,
        operation_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> AutomatedCodingResult:
        async with self.role_context("executor"):
            return await super().validate_and_freeze(operation_id, cancelled=cancelled)

    async def close(self) -> None:
        if self.prepared is None:
            return
        try:
            if self._adopted:
                await self._lifecycle.release_execution(self.prepared.scope.identity.operation_id)
        finally:
            await asyncio.shield(self.runtime.finish(self.prepared.scope.identity))


def approval_arguments(prepared: PreparedExecution, plan: PlanSpec | None) -> dict[str, object]:
    """Complete trusted scope for the owning browser, never a log/Telemetry payload."""
    scope, profile = prepared.scope, prepared.profile
    return {
        "goal": prepared.goal,
        "task_id": scope.identity.task_id,
        "kind": scope.kind,
        "scope_sha256": scope.sha256,
        "backend": profile.backend,
        "runtime_id": profile.runtime_id,
        "network": profile.network.model_dump(mode="json"),
        "data_location": "local Docker" if profile.backend == "local_docker" else "remote E2B",
        "workspace_revision": scope.baseline_revision,
        "workspace_sha256": scope.baseline_sha256,
        "input_sha256": scope.input_sha256,
        "input_paths": [entry.path for entry in prepared.baseline.snapshot.manifest.entries],
        "capabilities": list(scope.capabilities),
        "limits": profile.limits.model_dump(mode="json"),
        "max_commands": scope.max_commands,
        "max_execution_ms": scope.max_execution_ms,
        "lifetime_ms": scope.lifetime_ms,
        "plan_id": scope.plan_id,
        "plan_version": scope.plan_version,
        "plan_sha256": scope.plan_sha256,
        "plan": None if plan is None else plan.model_dump(mode="json"),
        "publication": "Separate signed-artifact approval required; no automatic writeback.",
        "publish_paths": "scripts/**, artifacts/**, ordinary Markdown; protected paths excluded",
        "docker_publication_enabled": False,
    }

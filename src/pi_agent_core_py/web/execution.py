"""Trusted Web composition for request-scoped Coding/Plan execution approval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from agent_workspace.store import WorkspaceStore
from coding_agent_app.execution.automation import ApprovedCodingAutomation, approval_arguments
from coding_agent_app.execution.models import ExecutionDenied, ExecutionScope, digest_json
from coding_agent_app.execution.runtime import (
    ExecutionProfile,
    ExecutionRequest,
    ExecutionTaskRuntime,
    PreparedExecution,
)
from coding_agent_app.execution.store import ExecutionStore
from coding_agent_app.planning.models import PlanSpec
from coding_agent_app.planning.store import PlanStore
from coding_agent_app.sandbox.workspace import WorkspaceSandboxBaselineProvider
from coding_sandbox import ArtifactSigner
from coding_sandbox.admin.service import SandboxAdminService
from coding_sandbox.backend import SandboxBackend
from coding_sandbox.lifecycle import ManagedSandboxLifecycle
from coding_sandbox.local_docker import LocalDockerExecutionConfig

from ..agent.messages import ToolCall
from ..agent.tooling import AgentTool
from ..policy import ToolApprovalContext, ToolPermissionDecision
from ..session_backends.sqlite.database import database_for
from .approvals import ToolApprovalManager
from .extension_store import ExtensionSQLiteStore

BackendChoice = Literal["disabled", "e2b", "local_docker"]
LocalBackendFactory = Callable[[LocalDockerExecutionConfig], SandboxBackend]


class ExecutionSelectionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: BackendChoice
    expected_revision: int = Field(ge=0)


class WebExecutionRuntime:
    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        account_id: str,
        workspace: WorkspaceStore,
        extensions: ExtensionSQLiteStore,
        admin: SandboxAdminService,
        lifecycle: ManagedSandboxLifecycle,
        signer: ArtifactSigner,
        staging_root: Path,
        plans: PlanStore | None,
        request_allowed: Callable[[ExecutionRequest], Awaitable[bool]],
        approvals: ToolApprovalManager,
        local_config: LocalDockerExecutionConfig | None = None,
        local_backend_factory: LocalBackendFactory | None = None,
    ) -> None:
        self._db, self._database = connection, database_for(connection)
        self._account, self._extensions = account_id, extensions
        self._admin, self._lifecycle, self._plans = admin, lifecycle, plans
        self._local = local_config or LocalDockerExecutionConfig()
        self._local_backend = local_backend_factory
        self._approvals = approvals
        self._tasks: dict[str, ApprovedCodingAutomation] = {}
        self.bash_tasks: dict[str, PreparedExecution] = {}
        self._watcher: asyncio.Task[None] | None = None
        baseline = WorkspaceSandboxBaselineProvider(
            workspace,
            materialization_root=staging_root / "materializations",
        )

        async def version(request: ExecutionRequest) -> tuple[int, str]:
            return await baseline.current_version(request.session_id)

        async def enabled(scope: ExecutionScope) -> bool:
            selected = await extensions.get_workspace_extension_selection(scope.identity.session_id)
            return scope.kind != "bash" or "run_bash" in selected.tool_names

        self.runtime = ExecutionTaskRuntime(
            store=ExecutionStore(connection),
            profile_provider=self._profile,
            baseline_provider=baseline,
            baseline_version=version,
            backend_resolver=self._backend,
            scope_enabled=enabled,
            request_allowed=request_allowed,
            artifact_signer=signer,
            staging_root=staging_root,
            plan_store=plans,
        )

    async def init(self) -> None:
        async with self._database.operation():
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS web_execution_selection ("
                "session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, "
                "backend TEXT NOT NULL CHECK(backend IN ('disabled','e2b','local_docker')), "
                "revision INTEGER NOT NULL CHECK(revision > 0))"
            )
            await self._db.commit()
        await self.runtime.store.init()
        await self.runtime.recover_startup()
        self._watcher = asyncio.create_task(self._watch(), name="web_execution_revocation")

    async def selection(self, session_id: str) -> dict[str, Any]:
        async with self._database.operation():
            async with self._db.execute(
                "SELECT backend,revision FROM web_execution_selection WHERE session_id=?",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
        # Keep the existing E2B backend choice, but never its old implicit authorization.
        return (
            {"backend": "e2b", "revision": 0}
            if row is None
            else {
                "backend": str(row[0]),
                "revision": int(row[1]),
            }
        )

    async def capability(self, session_id: str) -> dict[str, Any]:
        config = (await self._admin.get_config()).config
        return {
            **await self.selection(session_id),
            "backends": [
                {"id": "disabled", "available": True, "label": "Disabled"},
                {
                    "id": "e2b",
                    "available": config.enabled,
                    "label": "E2B · remote copy · approve each task",
                },
                {
                    "id": "local_docker",
                    "available": self._local.enabled and self._local_backend is not None,
                    "label": "Local Docker · no network · task approval · publication unavailable",
                },
            ],
            "approval_required": True,
            "bash_tool_enabled": (await self.bash_capability(session_id))["available"],
        }

    async def bash_capability(self, session_id: str) -> dict[str, Any]:
        available = (
            self._local.enabled and self._local_backend is not None
            and (await self.selection(session_id))["backend"] == "local_docker"
        )
        return {
            "available": available,
            "reason": None if available else "Select a configured Local Docker backend first.",
        }

    def request_identity(self, session_id: str, request_id: str) -> ExecutionRequest:
        return ExecutionRequest(
            account_id=self._account, workspace_id=session_id,
            session_id=session_id, request_id=request_id,
        )

    async def coding_bash_tool(
        self, session_id: str, request_id: str | None,
    ) -> AgentTool | None:
        """Request-local adapter, never another approval, history or runtime owner."""
        from coding_agent_app.execution.context import current_execution_context
        from coding_sandbox.operation import get_current_coding_workspace

        from ..tools.bash import RunBashTool

        selected = await self._extensions.get_workspace_extension_selection(session_id)
        if "run_bash" not in selected.tool_names or not (
            await self.bash_capability(session_id)
        )["available"]:
            return None

        def workspace() -> object:
            # Preview may construct an adapter, but it can never execute. A
            # captured adapter cannot borrow a later request's execution context.
            task = self._tasks.get(request_id or "")
            if task is None or task.prepared is None or task.request.session_id != session_id:
                raise ExecutionDenied("execution_request_denied")
            scope = task.prepared.scope
            context = current_execution_context()
            if (
                scope.kind not in {"coding", "plan"} or scope.backend != "local_docker"
                or context.identity != scope.identity or context.scope_sha256 != scope.sha256
                or context.role != "executor"
            ):
                raise ExecutionDenied("execution_role_or_scope_denied")
            return get_current_coding_workspace()

        return RunBashTool(workspace)

    async def approve_execution(
        self, prepared: PreparedExecution, plan: PlanSpec | None = None,
        *, signal: asyncio.Event | None = None,
    ) -> bool:
        identity = prepared.scope.identity

        async def commit_approval() -> None:
            await self.runtime.approve(identity, expected_scope_sha256=prepared.scope.sha256)

        arguments = approval_arguments(prepared, plan)
        selected = await self._extensions.get_workspace_extension_selection(identity.session_id)
        arguments["task_bash_enabled"] = (
            prepared.scope.kind in {"coding", "plan"}
            and prepared.profile.backend == "local_docker" and "run_bash" in selected.tool_names
        )
        reason = (
            "Approve this exact plan/version and isolated scope for this request. "
            "The Agent chooses future commands within these limits. "
            "Input files may be sent to the displayed backend. "
            "Real Workspace publication requires separate approval."
        )
        if prepared.bash is not None:
            arguments.update(
                **prepared.bash.model_dump(), script_sha256=prepared.bash.sha256,
                publication="Unavailable: copy changes are discarded after execution.",
            )
            reason = (
                "Review the COMPLETE Bash script, cwd and input scope. Approve this script once "
                "in a fresh local, offline Docker copy. Output may be sent to your model. "
                "Copy changes will be discarded; no file publication or host execution."
            )
        grant = await self.runtime.store.get(identity)
        return await self._approvals.request_approval(
            request_id=identity.request_id, session_id=identity.session_id,
            context=ToolApprovalContext(
                tool_call=ToolCall(id=identity.task_id, name="execution_task", arguments={}),
                tool=None, signal=signal,
                decision=ToolPermissionDecision(
                    decision="require_approval", policy_name="execution_task", reason=reason,
                ),
            ),
            exact_arguments=arguments, on_approve=commit_approval,
            timeout_seconds=max(1, (grant.approval_deadline_ms - grant.created_at_ms) / 1000),
        )

    async def select(self, session_id: str, value: ExecutionSelectionPut) -> dict[str, Any]:
        capabilities = await self.capability(session_id)
        if not any(
            item["id"] == value.backend and item["available"] for item in capabilities["backends"]
        ):
            raise ExecutionDenied("execution_disabled")
        async with self._database.transaction():
            current = await self.selection(session_id)
            if current["revision"] != value.expected_revision:
                raise ExecutionDenied("approval_stale")
            await self._db.execute(
                "INSERT INTO web_execution_selection VALUES (?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "backend=excluded.backend,revision=excluded.revision",
                (session_id, value.backend, value.expected_revision + 1),
            )
        await self.revoke_session(session_id)
        return await self.capability(session_id)

    async def _profile(self, request: ExecutionRequest) -> ExecutionProfile:
        selection = await self.selection(request.session_id)
        extensions = await self._extensions.get_workspace_extension_selection(request.session_id)
        choice = selection["backend"]
        if choice == "disabled":
            raise ExecutionDenied("execution_disabled")
        if choice == "local_docker":
            config = self._local
            if not config.enabled or config.image_id is None or self._local_backend is None:
                raise ExecutionDenied("execution_disabled")
            return ExecutionProfile(
                enabled=True,
                backend="local_docker",
                runtime_id=config.image_id,
                config_revision=1,
                limits=config.limits,
                selection_sha256=digest_json(
                    [selection, asdict(extensions), config.model_dump(mode="json")]
                ),
                publish_policy_sha256=digest_json({"publication": "disabled"}),
            )
        record = await self._admin.get_config()
        limits = record.config.limits.model_copy(
            update={
                "command_timeout_seconds": min(300, record.config.limits.command_timeout_seconds),
                "lifetime_seconds": min(1800, record.config.limits.lifetime_seconds),
            }
        )
        return ExecutionProfile(
            enabled=record.config.enabled,
            backend="e2b",
            runtime_id=record.config.runtime_id,
            config_revision=record.revision,
            limits=limits,
            network=record.config.network,
            selection_sha256=digest_json([selection, asdict(extensions)]),
            publish_policy_sha256=digest_json({"publisher": "workspace-signed-v1"}),
        )

    async def _backend(self, profile: ExecutionProfile) -> SandboxBackend:
        if profile.backend == "local_docker":
            if self._local_backend is None:
                raise ExecutionDenied("execution_disabled")
            return self._local_backend(self._local)
        record = await self._admin.get_config()
        if record.revision != profile.config_revision:
            raise ExecutionDenied("approval_stale")
        return await self._admin.resolve_operation_backend(record.config)

    def task(self, *, session_id: str, request_id: str, goal: str) -> ApprovedCodingAutomation:
        if request_id in self._tasks:
            raise ExecutionDenied("execution_busy")
        request = ExecutionRequest(
            account_id=self._account,
            workspace_id=session_id,
            session_id=session_id,
            request_id=request_id,
        )

        async def approve(prepared: PreparedExecution, plan: PlanSpec | None) -> bool:
            return await self.approve_execution(prepared, plan)

        task = ApprovedCodingAutomation(
            self._lifecycle,
            runtime=self.runtime,
            request=request,
            goal=goal,
            approve=approve,
            plan_store=self._plans,
        )
        self._tasks[request_id] = task
        return task

    async def close_task(self, request_id: str) -> None:
        task = self._tasks.pop(request_id, None)
        if task is not None:
            try:
                await self._approvals.cancel_request(request_id)
            finally:
                await task.close()

    async def revoke_session(self, session_id: str) -> None:
        for task in tuple(self._tasks.values()):
            if task.request.session_id == session_id and task.prepared is not None:
                await self._approvals.cancel_request(task.request.request_id)
                await self.runtime.cancel(task.prepared.scope.identity)
        for prepared in tuple(self.bash_tasks.values()):
            identity = prepared.scope.identity
            if identity.session_id == session_id:
                await self._approvals.cancel_request(identity.request_id)
                await self.runtime.cancel(identity)

    async def cancel_request(self, request_id: str) -> None:
        task = self._tasks.get(request_id)
        if task is not None and task.prepared is not None:
            await self.runtime.cancel(task.prepared.scope.identity)
        prepared = self.bash_tasks.get(request_id)
        if prepared is not None:
            await self.runtime.cancel(prepared.scope.identity)

    @asynccontextmanager
    async def read_operation(self, operation_id: str) -> AsyncIterator[None]:
        """UI reads may inspect their live task but never gain execution authority."""
        for task in tuple(self._tasks.values()):
            if (
                task.prepared is not None
                and task.prepared.scope.identity.operation_id == operation_id
            ):
                async with task.role_context("read_only"):
                    yield
                return
        yield  # After release, lifecycle serves only the retained signed artifact/diff.

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            pending = [t.prepared for t in tuple(self._tasks.values()) if t.prepared is not None]
            for prepared in [*pending, *tuple(self.bash_tasks.values())]:
                scope = prepared.scope
                try:
                    grant = await self.runtime.store.get(scope.identity)
                    # Activation is durable before seed; cleanup_pending is also
                    # its startup barrier. start() owns checks/cancellation there.
                    if grant.state == "active" and not grant.cleanup_pending:
                        await self.runtime.service.check(
                            self.runtime.context(scope.identity, scope.sha256)
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self.runtime.cancel(scope.identity)

    async def shutdown(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        for request_id in tuple(self._tasks):
            await self.close_task(request_id)
        await self.runtime.shutdown()

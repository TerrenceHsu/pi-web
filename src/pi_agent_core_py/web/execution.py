"""Trusted Web composition for request-scoped Coding/Plan execution approval."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from agent_workspace.store import WorkspaceStore
from coding_agent_app.execution.automation import ApprovedCodingAutomation, approval_arguments
from coding_agent_app.execution.cache import sweep_cache
from coding_agent_app.execution.control import ExecutionControl
from coding_agent_app.execution.models import (
    ExecutionDenied,
    ExecutionIdentity,
    ExecutionScope,
    digest_json,
)
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
from coding_sandbox.local_docker import LocalDockerExecutionConfig, LocalDockerSandboxBackend
from coding_sandbox.models import SandboxHandle
from coding_sandbox.publication import WORKSPACE_PUBLISH_POLICY_SHA256

from ..agent.messages import ToolCall
from ..agent.tooling import AgentTool
from ..policy import ToolApprovalContext, ToolPermissionDecision
from ..session_backends.sqlite.database import database_for
from ..telemetry import (
    NOOP_TELEMETRY_CONTEXT,
    SpanAttributes,
    SpanOptions,
    SpanStatus,
    TelemetryContext,
    TelemetrySpan,
)
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
        control: ExecutionControl | None = None,
        telemetry: TelemetryContext = NOOP_TELEMETRY_CONTEXT,
    ) -> None:
        self._db, self._database = connection, database_for(connection)
        self._account, self._extensions = account_id, extensions
        self._admin, self._lifecycle, self._plans = admin, lifecycle, plans
        self._local = local_config or LocalDockerExecutionConfig()
        self._local_backend = local_backend_factory
        self.control = control or ExecutionControl(staging_root.parent / "execution-control.sqlite")
        self._legacy_local = self._local
        self._local = self._local.model_copy(
            update={
                "namespace": self.control.namespace(self._local.namespace),
            }
        )
        self._owns_control = control is None
        self._telemetry = telemetry
        self._staging_root = staging_root
        self._maintenance: asyncio.Task[None] | None = None
        self._cache_job: asyncio.Task[None] | None = None
        self._last_cache_ms = 0
        self._maintenance_lock = asyncio.Lock()
        self.cache_status: dict[str, Any] = {"bytes": 0, "removed_bytes": 0, "removed_files": 0}
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
            store=ExecutionStore(
                connection,
                account_limit=self.control.quota.account_tasks,
                global_limit=self.control.quota.global_tasks,
            ),
            profile_provider=self._profile,
            baseline_provider=baseline,
            baseline_version=version,
            backend_resolver=self._backend,
            scope_enabled=enabled,
            request_allowed=request_allowed,
            artifact_signer=signer,
            staging_root=staging_root,
            plan_store=plans,
            control=self.control,
            namespace=self._local.namespace,
            reconcile_runtime=self._reconcile,
            preparation_check=self._check_preparation,
        )

    async def init(self) -> None:
        if self._owns_control:
            await self.control.init()
        self.control.register(
            self._account,
            self.revoke_task,
            self._local_backend,
            self._local,
            observer=self._record_cleanup,
        )
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
        await self.control.maintain()
        self._watcher = asyncio.create_task(self._watch(), name="web_execution_revocation")
        self._maintenance = asyncio.create_task(
            self._maintain_loop(), name="web_execution_maintenance"
        )

    async def _reconcile(self, scope: ExecutionScope, handle: SandboxHandle | None) -> bool:
        if scope.backend != "local_docker" or self._local_backend is None:
            return False
        lease = await self.control.recovery_lease(scope.identity)
        if lease is not None:
            if lease.scope != scope:
                return False
            config = LocalDockerExecutionConfig(
                image_id=scope.runtime_id,
                namespace=lease.namespace,
                limits=lease.limits,
            )
        else:
            # Legacy grants lack a namespace receipt. Only an unchanged trusted
            # profile can establish which deployment must be reconciled.
            config = self._local
            for candidate in (self._local, self._legacy_local):
                profile = await self._profile(
                    self.runtime._request(scope.identity),
                    local_override=candidate,
                )
                if digest_json(profile.model_dump(mode="json")) == scope.policy_sha256:
                    config = candidate
                    break
            else:
                return False
        backend = self._local_backend(config)
        if not isinstance(backend, LocalDockerSandboxBackend):
            return False
        await backend.reconcile_operation(scope.identity.operation_id)
        return True

    async def _check_preparation(self, profile: ExecutionProfile) -> None:
        grants = await self.runtime.store.list_grants()
        if sum(g.state in {"pending", "approved", "active"} for g in grants) >= 2:
            raise ExecutionDenied("execution_preparation_busy")
        try:
            usage = await asyncio.to_thread(
                sweep_cache,
                self._staging_root.parent,
                set(),
                ttl_seconds=2**40,
            )
        except Exception:
            raise ExecutionDenied("execution_cache_unavailable") from None
        # Reserve conservative headroom for two preparations plus two output
        # archives, including concurrent writers not yet reflected in disk usage.
        headroom = profile.limits.max_upload_bytes * 4
        if usage["bytes"] + headroom > self.control.quota.account_cache_bytes:
            raise ExecutionDenied("execution_cache_quota_exceeded")

    async def probe(self) -> dict[str, Any]:
        if self._local_backend is None:
            return {"configured": False, "environment_ready": False, "error_code": "bash_disabled"}
        backend = self._local_backend(self._local)
        if not isinstance(backend, LocalDockerSandboxBackend):
            return {
                "configured": self._local.enabled,
                "environment_ready": False,
                "error_code": "probe_unavailable",
            }
        return (await backend.probe()).model_dump()

    async def _record_cleanup(self, attributes: dict[str, object]) -> None:
        await self._telemetry.start_span(
            SpanOptions(name="execution.cleanup", attributes=cast(SpanAttributes, attributes)),
            lambda span: span.set_status(
                SpanStatus(
                    status="error" if attributes.get("phase") == "cleanup_pending" else "ok",
                )
            ),
        )

    async def maintain(self) -> None:
        async with self._maintenance_lock:
            now = time.time_ns() // 1_000_000
            grants = await self.runtime.store.list_grants(needs_maintenance=True)
            for grant in grants:
                identity = grant.scope.identity
                deadline = (
                    grant.approval_deadline_ms
                    if grant.state == "pending"
                    else grant.queue_deadline_ms
                    if grant.state == "approved"
                    else grant.expires_at_ms
                )
                if grant.state in {"pending", "approved", "active"}:
                    if deadline is not None and deadline <= now:
                        await self.runtime.store.terminate(identity, state="expired")
                        await self._approvals.cancel_request(identity.request_id)
                        await self.runtime.cancel(identity)
                    elif grant.state == "active":
                        if await self.control.allowed(identity):
                            await self.control.update(identity, heartbeat=True)
                        else:
                            await self.runtime.cancel(identity)
                elif grant.cleanup_pending:
                    await self.runtime.cancel(identity)
                await self.control.update(identity, grant=await self.runtime.store.get(identity))
            # Export only an explicit content-free projection; no grant/script serialization.
            for identifier, attributes in await self.runtime.store.observations():

                def record(span: TelemetrySpan, phase: object = attributes.get("phase")) -> None:
                    if phase in {"failed", "interrupted", "revoked", "expired"}:
                        span.set_status(SpanStatus(status="error"))

                await self._telemetry.start_span(
                    SpanOptions(
                        name="execution.command"
                        if "command_id" in attributes
                        else "execution.task",
                        attributes=cast(
                            SpanAttributes, {**attributes, "observation_id": identifier}
                        ),
                    ),
                    record,
                )
                await self.runtime.store.acknowledge_observation(identifier)
            await self.runtime.store.prune_history()
            await self.runtime.release_terminal_resources()
            from .bash import BashHistory

            await BashHistory(self._db).prune()
            if (
                self._cache_job is None or self._cache_job.done()
            ) and now - self._last_cache_ms >= 60_000:
                self._last_cache_ms = now
                self._cache_job = asyncio.create_task(
                    self._sweep_cache(), name="execution_cache_cleanup"
                )

    async def _sweep_cache(self) -> None:
        # Slow disk IO must not delay grant heartbeats, revocation or expiration.
        try:
            protected = await self._lifecycle.retained_cache_paths()
            for grant in await self.runtime.store.list_grants(needs_maintenance=True):
                if grant.state in {"pending", "approved", "active"} or grant.cleanup_pending:
                    protected.add(
                        (
                            self._staging_root
                            / "snapshots"
                            / f"{grant.scope.identity.operation_id}.tar.gz"
                        ).resolve()
                    )
            self.cache_status = await asyncio.to_thread(
                sweep_cache,
                self._staging_root.parent,
                protected,
            )
        except Exception:
            self.cache_status["error_code"] = "cache_cleanup_failed"

    async def _maintain_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.maintain()
            except Exception:
                self.cache_status["error_code"] = "execution_maintenance_failed"

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
        admission = await self.control.summary()
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
                    "label": "Local Docker · offline · separate execution/publication approvals",
                },
            ],
            "approval_required": True,
            "admission_paused": admission["admission_paused"],
            "cleanup_pending": admission["cleanup_pending"],
            "quota": admission["quota"],
            "bash_tool_enabled": (await self.bash_capability(session_id))["available"],
        }

    async def bash_capability(self, session_id: str) -> dict[str, Any]:
        available = (
            self._local.enabled
            and self._local_backend is not None
            and (await self.selection(session_id))["backend"] == "local_docker"
        )
        return {
            "available": available,
            "reason": None if available else "Select a configured Local Docker backend first.",
        }

    def request_identity(self, session_id: str, request_id: str) -> ExecutionRequest:
        return ExecutionRequest(
            account_id=self._account,
            workspace_id=session_id,
            session_id=session_id,
            request_id=request_id,
        )

    async def coding_bash_tool(
        self,
        session_id: str,
        request_id: str | None,
    ) -> AgentTool | None:
        """Request-local adapter, never another approval, history or runtime owner."""
        from coding_agent_app.execution.context import current_execution_context
        from coding_sandbox.operation import get_current_coding_workspace

        from ..tools.bash import RunBashTool

        selected = await self._extensions.get_workspace_extension_selection(session_id)
        if (
            "run_bash" not in selected.tool_names
            or not (await self.bash_capability(session_id))["available"]
        ):
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
                scope.kind not in {"coding", "plan"}
                or scope.backend != "local_docker"
                or context.identity != scope.identity
                or context.scope_sha256 != scope.sha256
                or context.role != "executor"
            ):
                raise ExecutionDenied("execution_role_or_scope_denied")
            return get_current_coding_workspace()

        return RunBashTool(workspace)

    async def approve_execution(
        self,
        prepared: PreparedExecution,
        plan: PlanSpec | None = None,
        *,
        signal: asyncio.Event | None = None,
    ) -> bool:
        identity = prepared.scope.identity

        async def commit_approval() -> None:
            await self.runtime.approve(identity, expected_scope_sha256=prepared.scope.sha256)

        arguments = approval_arguments(prepared, plan)
        selected = await self._extensions.get_workspace_extension_selection(identity.session_id)
        arguments["task_bash_enabled"] = (
            prepared.scope.kind in {"coding", "plan"}
            and prepared.profile.backend == "local_docker"
            and "run_bash" in selected.tool_names
        )
        reason = (
            "Approve this exact plan/version and isolated scope for this request. "
            "The Agent chooses future commands within these limits. "
            "Input files may be sent to the displayed backend. "
            "Real Workspace publication requires separate approval."
        )
        if prepared.bash is not None:
            arguments.update(
                **prepared.bash.model_dump(),
                script_sha256=prepared.bash.sha256,
                publication="Successful safe output may be frozen; separate approval required.",
            )
            reason = (
                "Review the COMPLETE Bash script, cwd and input scope. Approve this script once "
                "in a fresh local, offline Docker copy. Output may be sent to your model. "
                "Output requires separate review and publication approval; no host execution."
            )
        grant = await self.runtime.store.get(identity)
        return await self._approvals.request_approval(
            request_id=identity.request_id,
            session_id=identity.session_id,
            context=ToolApprovalContext(
                tool_call=ToolCall(id=identity.task_id, name="execution_task", arguments={}),
                tool=None,
                signal=signal,
                decision=ToolPermissionDecision(
                    decision="require_approval",
                    policy_name="execution_task",
                    reason=reason,
                ),
            ),
            exact_arguments=arguments,
            on_approve=commit_approval,
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

    async def _profile(
        self,
        request: ExecutionRequest,
        *,
        local_override: LocalDockerExecutionConfig | None = None,
    ) -> ExecutionProfile:
        selection = await self.selection(request.session_id)
        extensions = await self._extensions.get_workspace_extension_selection(request.session_id)
        choice = selection["backend"]
        if choice == "disabled":
            raise ExecutionDenied("execution_disabled")
        if choice == "local_docker":
            config = local_override or self._local
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
                publish_policy_sha256=WORKSPACE_PUBLISH_POLICY_SHA256,
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
            publish_policy_sha256=WORKSPACE_PUBLISH_POLICY_SHA256,
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

    async def revoke_task(self, identity: ExecutionIdentity) -> None:
        await self.runtime.store.get(identity)
        await self.runtime.cancel(identity)
        await self._approvals.cancel_request(identity.request_id)

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
        if self._maintenance is not None:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        if self._cache_job is not None:
            await self._cache_job
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        for request_id in tuple(self._tasks):
            await self.close_task(request_id)
        await self.runtime.shutdown()
        self.control.unregister(self._account)
        if self._owns_control:
            await self.control.close()

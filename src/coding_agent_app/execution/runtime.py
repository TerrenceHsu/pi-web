"""Two-phase task runtime for trusted Web composition, never an Agent entry point.

Preparation only materializes a local snapshot. Explicit approval and a second
scope check precede runtime creation. No method publishes to the real Workspace.
Web authentication, profile selection and approval UI are separate adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from coding_sandbox.artifact import ArtifactSigner, hash_regular_file
from coding_sandbox.backend import SandboxBackend
from coding_sandbox.bash import BashRequest
from coding_sandbox.docker_transport import require_plain_path
from coding_sandbox.lifecycle import (
    DEFAULT_VALIDATION_CONFIG,
    SandboxBaseline,
    SandboxBaselineProvider,
)
from coding_sandbox.local_docker import LocalDockerExecutionConfig
from coding_sandbox.models import (
    SandboxCreateSpec,
    SandboxHandle,
    SandboxLimits,
    SandboxNetworkPolicy,
)
from coding_sandbox.operation import SandboxOperation, bind_coding_workspace
from coding_sandbox.snapshot import SnapshotPolicy, read_snapshot_file, validate_snapshot_archive
from coding_sandbox.validation import SandboxValidationPlan, load_sandbox_validation_plan
from coding_sandbox.workspace_models import SandboxFileEntry

from ..planning.store import PlanStore
from .context import bind_execution_context
from .models import (
    Digest,
    ExecutionContext,
    ExecutionDenied,
    ExecutionGrant,
    ExecutionIdentity,
    ExecutionScope,
    FrozenModel,
    Identifier,
    digest_json,
)
from .operation import GrantedOperationAccess
from .service import ExecutionService, ScopeCheck
from .store import ExecutionStore


class ExecutionRequest(FrozenModel):
    """Authenticated identity resolved by the server, not tool/model arguments."""

    account_id: Identifier
    session_id: Identifier
    request_id: Identifier
    workspace_id: Identifier


class ExecutionProfile(FrozenModel):
    """Explicit deployment/Workspace selection; never resolved from a tool call."""

    enabled: bool = False
    backend: Literal["local_docker", "e2b"]
    runtime_id: Identifier
    config_revision: int = Field(ge=0)
    selection_sha256: Digest
    publish_policy_sha256: Digest
    network: SandboxNetworkPolicy = Field(default_factory=SandboxNetworkPolicy)
    limits: SandboxLimits

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.limits.command_timeout_seconds > 300 or self.limits.lifetime_seconds > 1800:
            raise ValueError("execution profile exceeds task time ceilings")
        if self.backend == "local_docker":
            LocalDockerExecutionConfig(image_id=self.runtime_id, limits=self.limits)
            if self.network.mode != "none":
                raise ValueError("local execution requires no network")
        return self


class ExecutionPlan(FrozenModel):
    plan_id: Identifier
    version: int = Field(ge=1)
    sha256: Digest


@dataclass(frozen=True)
class PreparedExecution:
    scope: ExecutionScope
    profile: ExecutionProfile
    baseline: SandboxBaseline
    validation_plan: SandboxValidationPlan
    goal: str
    bash: BashRequest | None


@dataclass
class _Resource:
    prepared: PreparedExecution
    backend: SandboxBackend | None = None
    handle: SandboxHandle | None = None
    operation: SandboxOperation | None = None
    create_attempted: bool = False
    closed: bool = False


ProfileProvider = Callable[[ExecutionRequest], Awaitable[ExecutionProfile]]
BaselineVersion = Callable[[ExecutionRequest], Awaitable[tuple[int, str]]]
BackendResolver = Callable[[ExecutionProfile], Awaitable[SandboxBackend]]
# A missing create response or a restarted process requires provider-specific
# reconciliation. True means verified absence, never merely "not in memory".
ReconcileRuntime = Callable[[ExecutionScope, SandboxHandle | None], Awaitable[bool]]
RequestAllowed = Callable[[ExecutionRequest], Awaitable[bool]]


class ExecutionTaskRuntime:
    def __init__(
        self,
        *,
        store: ExecutionStore,
        profile_provider: ProfileProvider,
        baseline_provider: SandboxBaselineProvider,
        baseline_version: BaselineVersion,
        backend_resolver: BackendResolver,
        scope_enabled: ScopeCheck,
        request_allowed: RequestAllowed,
        artifact_signer: ArtifactSigner,
        staging_root: Path,
        plan_store: PlanStore | None = None,
        reconcile_runtime: ReconcileRuntime | None = None,
        startup_timeout_seconds: float = 120,
    ) -> None:
        if not staging_root.is_absolute() or not 0 < startup_timeout_seconds <= 120:
            raise ValueError("absolute staging root and bounded startup timeout required")
        existing_parent = staging_root
        while not existing_parent.exists():
            existing_parent = existing_parent.parent
        require_plain_path(existing_parent, directory=True)
        self.store = store
        self._profiles, self._baselines = profile_provider, baseline_provider
        self._version, self._resolve = baseline_version, backend_resolver
        self._enabled, self._signer = scope_enabled, artifact_signer
        self._request_allowed = request_allowed
        # Windows temporary paths may use an 8.3 alias (ADMINI~1). Pin the same
        # canonical spelling as ProjectSnapshot, after checking existing parents.
        self._root, self._plans = staging_root.resolve(strict=False), plan_store
        self._reconcile = reconcile_runtime
        self._startup_timeout = startup_timeout_seconds
        self._resources: dict[str, _Resource] = {}
        self._starting: dict[str, asyncio.Task[SandboxOperation]] = {}
        self._preparing: set[ExecutionRequest] = set()
        self._closing = False
        self.service = ExecutionService(
            store, scope_check=self._active_scope, cleanup=self._cleanup
        )

    @staticmethod
    def _request(identity: ExecutionIdentity) -> ExecutionRequest:
        return ExecutionRequest(
            **{field: getattr(identity, field) for field in ExecutionRequest.model_fields}
        )

    @staticmethod
    def context(
        identity: ExecutionIdentity,
        sha256: str,
        *,
        role: Literal["executor", "planner", "verifier", "read_only"] = "executor",
    ) -> ExecutionContext:
        return ExecutionContext(identity=identity, scope_sha256=sha256, role=role)

    async def prepare(
        self,
        request: ExecutionRequest,
        *,
        goal: str,
        plan: ExecutionPlan | None = None,
        bash: BashRequest | None = None,
    ) -> PreparedExecution:
        if self._closing or request in self._preparing:
            raise ExecutionDenied("execution_busy")
        if not goal or len(goal.encode("utf-8")) > 64_000 or plan is not None and bash is not None:
            raise ExecutionDenied("grant_scope_mismatch")
        if plan is not None and self._plans is None:
            raise ExecutionDenied("plan_approval_required")
        self._preparing.add(request)
        identity = ExecutionIdentity(
            **request.model_dump(),
            task_id=f"task-{uuid4().hex}",
            operation_id=f"sandbox-{uuid4().hex}",
        )
        archive = self._root / "snapshots" / f"{identity.operation_id}.tar.gz"
        saved = False
        try:
            if not await self._request_allowed(request):
                raise ExecutionDenied("execution_request_denied")
            profile = await self._profiles(request)
            if not profile.enabled:
                raise ExecutionDenied("execution_disabled")
            if bash is not None and (
                profile.backend != "local_docker"
                or bash.timeout_seconds > profile.limits.command_timeout_seconds
            ):
                raise ExecutionDenied("grant_scope_mismatch")
            policy = SnapshotPolicy.from_limits(profile.limits)
            archive.parent.mkdir(parents=True, exist_ok=True)
            require_plain_path(archive.parent, directory=True)
            if archive.exists() or archive.is_symlink():
                raise ExecutionDenied("approval_stale")
            baseline = await self._baselines(
                request.session_id, archive, policy, DEFAULT_VALIDATION_CONFIG
            )
            if (
                baseline.snapshot.archive_path != archive
                or baseline.source_workspace_revision is None
                or baseline.source_workspace_sha256 is None
            ):
                raise ExecutionDenied("baseline_required")
            await asyncio.to_thread(self._validate_snapshot, baseline, policy)
            validation = load_sandbox_validation_plan(baseline.snapshot, policy=policy)
            if bash is None and any(
                check.timeout_seconds > profile.limits.command_timeout_seconds
                for check in validation.required_checks
            ):
                raise ExecutionDenied("validation_budget_invalid")
            scope = ExecutionScope(
                identity=identity,
                kind="plan" if plan else ("bash" if bash else "coding"),
                backend=profile.backend,
                runtime_id=profile.runtime_id,
                config_revision=profile.config_revision,
                policy_sha256=digest_json(profile.model_dump(mode="json")),
                baseline_revision=baseline.source_workspace_revision,
                baseline_sha256=baseline.source_workspace_sha256,
                input_sha256=baseline.snapshot.manifest.manifest_sha256,
                publish_policy_sha256=profile.publish_policy_sha256,
                capabilities=("execute",) if bash else ("edit", "execute", "validate"),
                request_sha256=hashlib.sha256(goal.encode()).hexdigest(),
                plan_id=None if plan is None else plan.plan_id,
                plan_version=None if plan is None else plan.version,
                plan_sha256=None if plan is None else plan.sha256,
                script_sha256=None if bash is None else bash.sha256,
                cwd=None if bash is None else bash.cwd,
                max_commands=1 if bash else 50,
                max_execution_ms=bash.timeout_seconds * 1000 if bash else 600_000,
                lifetime_ms=min(
                    profile.limits.lifetime_seconds * 1000, 600_000 if bash else 1_800_000
                ),
            )
            prepared = PreparedExecution(scope, profile, baseline, validation, goal, bash)
            if not await self._pending_scope(prepared):
                raise ExecutionDenied("approval_stale")
            await self.store.prepare(
                scope,
                plan_binding=None
                if plan is None or self._plans is None
                else self._plans.bind_execution,
            )
            saved = True
            self._resources[identity.task_id] = _Resource(prepared)
            return prepared
        finally:
            self._preparing.discard(request)
            if not saved:
                # Cancellation may arrive after SQLite committed but before the
                # caller received the prepared object. Do not strand its lease.
                await asyncio.shield(self._abandon_preparation(identity))
                archive.unlink(missing_ok=True)

    async def _abandon_preparation(self, identity: ExecutionIdentity) -> None:
        try:
            await self.store.terminate(identity, state="interrupted")
        except ExecutionDenied as exc:
            if exc.code != "grant_required":
                raise

    async def _pending_scope(self, prepared: PreparedExecution) -> bool:
        scope = prepared.scope
        if not await self._active_scope(scope):
            return False
        return await self._version(self._request(scope.identity)) == (
            scope.baseline_revision,
            scope.baseline_sha256,
        )

    async def _active_scope(self, scope: ExecutionScope) -> bool:
        if (
            self._closing
            or not await self._request_allowed(self._request(scope.identity))
            or not await self._enabled(scope)
        ):
            return False
        if scope.kind == "plan" and (
            self._plans is None or not await self._plans.execution_scope_matches(scope)
        ):
            return False
        profile = await self._profiles(self._request(scope.identity))
        return (
            profile.enabled and digest_json(profile.model_dump(mode="json")) == scope.policy_sha256
        )

    async def _resource(self, identity: ExecutionIdentity) -> _Resource:
        await self.store.get(identity)  # Validate ALL identity fields before any local effects.
        resource = self._resources.get(identity.task_id)
        if resource is None or resource.prepared.scope.identity != identity:
            raise ExecutionDenied("execution_interrupted")
        return resource

    async def approve(
        self, identity: ExecutionIdentity, *, expected_scope_sha256: str
    ) -> ExecutionGrant:
        resource = await self._resource(identity)
        if resource.prepared.scope.sha256 != expected_scope_sha256:
            raise ExecutionDenied("approval_stale")
        if not await self._pending_scope(resource.prepared):
            await self.cancel(identity)
            raise ExecutionDenied("approval_stale")
        return await self.store.approve(
            identity,
            expected_scope_sha256=expected_scope_sha256,
            plan_approval=None if self._plans is None else self._plans.approve_execution,
        )

    async def start(self, identity: ExecutionIdentity) -> SandboxOperation:
        resource = await self._resource(identity)
        if identity.task_id in self._starting:
            raise ExecutionDenied("execution_busy")
        # No await between ownership of startup and publishing its cancellation handle.
        job = asyncio.create_task(self._start(resource), name=f"execution_start_{identity.task_id}")
        self._starting[identity.task_id] = job
        try:
            return await job
        finally:
            if self._starting.get(identity.task_id) is job:
                self._starting.pop(identity.task_id, None)

    async def _start(self, resource: _Resource) -> SandboxOperation:
        prepared = resource.prepared
        scope, profile = prepared.scope, prepared.profile
        context = self.context(scope.identity, scope.sha256)
        activated = False
        approval_checked = False
        try:
            grant = await self.store.get(scope.identity)
            if grant.state != "approved":
                raise ExecutionDenied("approval_required")
            approval_checked = True
            async with asyncio.timeout(self._startup_timeout):
                if not await self._pending_scope(prepared):
                    await self.cancel(scope.identity)
                    raise ExecutionDenied("approval_stale")
                policy = SnapshotPolicy.from_limits(profile.limits)
                await asyncio.to_thread(self._validate_snapshot, prepared.baseline, policy)
                await self.store.activate(context, current_scope=scope)
                activated = True
                resource.backend = await self._resolve(profile)
                if resource.backend.backend_name() != profile.backend:
                    raise ExecutionDenied("grant_scope_mismatch")
                await self.store.check_active(context)
                if not await self._pending_scope(prepared):
                    raise ExecutionDenied("approval_stale")
                resource.create_attempted = True
                handle = await resource.backend.create(
                    SandboxCreateSpec(
                        operation_id=scope.identity.operation_id,
                        runtime_id=profile.runtime_id,
                        network=profile.network,
                        limits=profile.limits,
                    )
                )
                if (
                    handle.operation_id != scope.identity.operation_id
                    or handle.runtime_id != scope.runtime_id
                    or handle.provider != scope.backend
                ):
                    raise ExecutionDenied("grant_scope_mismatch")
                resource.handle = handle
                await self.store.check_active(context)
                if not await self._pending_scope(prepared):
                    raise ExecutionDenied("approval_stale")

                async def read_baseline(path: str) -> bytes | None:
                    return await asyncio.to_thread(
                        read_snapshot_file, prepared.baseline.snapshot, path
                    )

                operation = SandboxOperation(
                    backend=resource.backend,
                    handle=handle,
                    workdir="/workspace",
                    limits=profile.limits,
                    # Upload/download names and final artifacts are already unique.
                    # Avoid repeating UUID directories: Windows may enforce MAX_PATH.
                    staging_root=self._root / "files",
                    baseline_entries=tuple(
                        SandboxFileEntry(path=e.path, size=e.size, sha256=e.sha256)
                        for e in prepared.baseline.snapshot.manifest.entries
                    ),
                    baseline_reader=read_baseline,
                    validation_plan=prepared.validation_plan,
                    artifact_signer=self._signer,
                    snapshot_policy=policy,
                    execution_guard=GrantedOperationAccess(self.service, scope),
                )
                resource.operation = operation
                await operation.seed_from_snapshot(prepared.baseline.snapshot)
                await self.store.check_active(context)
                if not await self._pending_scope(prepared):
                    raise ExecutionDenied("approval_stale")
                await self.store.runtime_ready(context)
                return operation
        except BaseException as exc:
            if activated:
                await asyncio.shield(self.store.terminate(scope.identity, state="interrupted"))
                await asyncio.shield(self.service.revoke(scope.identity))
            elif approval_checked and not (
                isinstance(exc, ExecutionDenied)
                and exc.code in {"execution_busy", "cleanup_pending"}
            ):
                await asyncio.shield(self.store.terminate(scope.identity, state="interrupted"))
            raise

    @asynccontextmanager
    async def bind(
        self,
        identity: ExecutionIdentity,
        *,
        role: Literal["executor", "planner", "verifier", "read_only"] = "executor",
    ) -> AsyncIterator[SandboxOperation]:
        """Trusted orchestrator role binding, never a browser-selected role."""
        resource = await self._resource(identity)
        context = self.context(identity, resource.prepared.scope.sha256, role=role)
        with bind_execution_context(context):
            if resource.operation is None:
                raise ExecutionDenied("cleanup_pending")
            # The original role is rechecked by the operation for every call.
            await self.service.check(context.model_copy(update={"role": "executor"}))
            with bind_coding_workspace(resource.operation):
                yield resource.operation

    async def cancel(self, identity: ExecutionIdentity) -> None:
        await self.store.terminate(identity)
        job = self._starting.get(identity.task_id)
        if job is not None and job is not asyncio.current_task():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await self.service.revoke(identity)
        resource = self._resources.get(identity.task_id)
        if resource is not None and not resource.create_attempted:
            resource.closed = True

    async def _cleanup(self, identity: ExecutionIdentity) -> bool:
        grant = await self.store.get(identity)
        resource = self._resources.get(identity.task_id)
        if resource is not None:
            if resource.closed or not resource.create_attempted:
                return True
            if resource.backend is not None and resource.handle is not None:
                await resource.backend.destroy(resource.handle)
                resource.closed = True
                return True
        if self._reconcile is None:
            return False
        return await self._reconcile(grant.scope, None if resource is None else resource.handle)

    async def recover_startup(self) -> tuple[ExecutionIdentity, ...]:
        """Call before admitting Web traffic. Never replay or recreate a task."""
        pending = await self.store.recover_interrupted()
        for identity in pending:
            await self.service.revoke(identity)
        remaining = []
        for identity in pending:
            if (await self.store.get(identity)).cleanup_pending:
                remaining.append(identity)
        return tuple(remaining)

    @staticmethod
    def _validate_snapshot(baseline: SandboxBaseline, policy: SnapshotPolicy) -> None:
        snapshot = baseline.snapshot
        require_plain_path(snapshot.archive_path)
        manifest = validate_snapshot_archive(snapshot.archive_path, policy=policy)
        if manifest != snapshot.manifest or hash_regular_file(
            snapshot.archive_path, max_bytes=policy.max_total_bytes
        ) != (snapshot.archive_size, snapshot.archive_sha256):
            raise ExecutionDenied("approval_stale")

    async def shutdown(self) -> None:
        self._closing = True
        for resource in tuple(self._resources.values()):
            await self.cancel(resource.prepared.scope.identity)

    async def finish(self, identity: ExecutionIdentity) -> None:
        """Close execution authority and the runtime, without publishing any file."""
        await self.store.terminate(identity, state="closed")
        await self.cancel(identity)

    @asynccontextmanager
    async def task(self, identity: ExecutionIdentity) -> AsyncIterator[SandboxOperation]:
        """One explicitly approved task. No approval is inferred by entering it."""
        await self.start(identity)
        try:
            async with self.bind(identity) as operation:
                yield operation
        except BaseException:
            await asyncio.shield(self.cancel(identity))
            raise
        else:
            await self.finish(identity)

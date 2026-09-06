"""Durable grant/command CAS using the existing shared Session SQLite coordinator.

No backend is called inside a transaction. Authorization is recorded before any
effect. Restart recovery invalidates unfinished work; it never resumes commands.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Literal

import aiosqlite

from pi_agent_core_py.session_backends.sqlite.database import database_for

from .models import (
    CommandIntent,
    ExecutionCommand,
    ExecutionContext,
    ExecutionDenied,
    ExecutionGrant,
    ExecutionIdentity,
    ExecutionScope,
)

PlanApproval = Callable[[aiosqlite.Connection, ExecutionScope], Awaitable[None]]


class ExecutionStore:
    def __init__(
        self, connection: aiosqlite.Connection, *, clock_ms: Callable[[], int] | None = None
    ) -> None:
        self._db = connection
        self._database = database_for(connection)
        self._clock = clock_ms or (lambda: int(time.time() * 1000))

    async def init(self) -> None:
        async with self._database.operation():
            await self._db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_grants (
                    task_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    cleanup_pending INTEGER NOT NULL DEFAULT 0,
                    grant_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_workspace_lease
                    ON execution_grants(account_id, workspace_id)
                    WHERE state IN ('pending', 'approved', 'active') OR cleanup_pending = 1;
                CREATE TABLE IF NOT EXISTS execution_commands (
                    task_id TEXT NOT NULL REFERENCES execution_grants(task_id),
                    command_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, command_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_serial_command
                    ON execution_commands(task_id) WHERE state IN ('preparing', 'running');
            """)
            await self._db.commit()

    async def _get(self, identity: ExecutionIdentity) -> ExecutionGrant:
        async with self._db.execute(
            "SELECT grant_json FROM execution_grants WHERE task_id=?", (identity.task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionDenied("grant_required")
        grant = ExecutionGrant.model_validate_json(row[0])
        if grant.scope.identity != identity:
            raise ExecutionDenied("grant_required")
        return grant

    async def get(self, identity: ExecutionIdentity) -> ExecutionGrant:
        async with self._database.operation():
            return await self._get(identity)

    async def _save(self, grant: ExecutionGrant) -> None:
        await self._db.execute(
            "UPDATE execution_grants SET state=?, cleanup_pending=?, grant_json=? WHERE task_id=?",
            (
                grant.state,
                int(grant.cleanup_pending),
                grant.model_dump_json(),
                grant.scope.identity.task_id,
            ),
        )

    async def prepare(
        self, scope: ExecutionScope, *, plan_binding: PlanApproval | None = None
    ) -> ExecutionGrant:
        now = self._clock()
        grant = ExecutionGrant(
            scope=scope, state="pending", created_at_ms=now, approval_deadline_ms=now + 600_000
        )
        async with self._database.transaction():
            try:
                await self._db.execute(
                    "INSERT INTO execution_grants VALUES (?, ?, ?, ?, 'pending', 0, ?)",
                    (
                        scope.identity.task_id,
                        scope.identity.operation_id,
                        scope.identity.account_id,
                        scope.identity.workspace_id,
                        grant.model_dump_json(),
                    ),
                )
            except aiosqlite.IntegrityError:
                raise ExecutionDenied("workspace_busy_or_task_exists") from None
            if plan_binding is not None:
                if scope.kind != "plan":
                    raise ExecutionDenied("grant_scope_mismatch")
                await plan_binding(self._db, scope)
        return grant

    async def approve(
        self,
        identity: ExecutionIdentity,
        *,
        expected_scope_sha256: str,
        plan_approval: PlanApproval | None = None,
    ) -> ExecutionGrant:
        """Trusted user-decision boundary, never callable by a tool.

        Plan requires a trusted callback on THIS transaction/connection, which
        must CAS the canonical plan version and append its approval event without
        committing. Until the Plan adapter supplies it, Plan approval fails closed.
        """
        async with self._database.transaction():
            grant = await self._get(identity)
            if grant.scope.sha256 != expected_scope_sha256:
                raise ExecutionDenied("approval_stale")
            if grant.state != "pending":
                raise ExecutionDenied("approval_already_decided")
            if self._clock() >= grant.approval_deadline_ms:
                raise ExecutionDenied("approval_expired")
            if grant.scope.kind == "plan":
                if plan_approval is None:
                    raise ExecutionDenied("plan_approval_required")
                await plan_approval(self._db, grant.scope)
            updated = grant.model_copy(
                update={"state": "approved", "queue_deadline_ms": self._clock() + 60_000}
            )
            await self._save(updated)
            return updated

    async def activate(
        self, context: ExecutionContext, *, current_scope: ExecutionScope
    ) -> ExecutionGrant:
        async with self._database.transaction():
            grant = await self._get(context.identity)
            self._context(grant, context)
            if (
                grant.state != "approved"
                or current_scope != grant.scope
                or self._clock() >= (grant.queue_deadline_ms or 0)
            ):
                raise ExecutionDenied("approval_stale")
            # Cleanup uncertainty holds the global admission barrier until reconciled.
            async with self._db.execute(
                "SELECT account_id, state, cleanup_pending FROM execution_grants "
                "WHERE state='active' OR cleanup_pending=1"
            ) as cursor:
                rows = list(await cursor.fetchall())
            if any(row[2] for row in rows):
                raise ExecutionDenied("cleanup_pending")
            if len(rows) >= 2 or any(row[0] == context.identity.account_id for row in rows):
                raise ExecutionDenied("execution_busy")
            updated = grant.model_copy(
                update={
                    "state": "active",
                    "expires_at_ms": self._clock() + grant.scope.lifetime_ms,
                    # Startup intention is durable before creating any runtime resource.
                    "cleanup_pending": True,
                }
            )
            await self._save(updated)
            return updated

    @staticmethod
    def _context(grant: ExecutionGrant, context: ExecutionContext) -> None:
        if context.role != "executor":
            raise ExecutionDenied("execution_role_denied")
        if context.scope_sha256 != grant.scope.sha256:
            raise ExecutionDenied("grant_scope_mismatch")

    def _active(self, grant: ExecutionGrant, context: ExecutionContext) -> None:
        self._context(grant, context)
        if grant.state != "active":
            raise ExecutionDenied("grant_revoked")
        if self._clock() >= (grant.expires_at_ms or 0):
            raise ExecutionDenied("grant_expired")

    async def check_active(self, context: ExecutionContext) -> ExecutionGrant:
        async with self._database.operation():
            grant = await self._get(context.identity)
            self._active(grant, context)
            return grant

    async def runtime_ready(self, context: ExecutionContext) -> None:
        """Trusted backend verified startup; not a task-code or browser assertion."""
        async with self._database.transaction():
            grant = await self._get(context.identity)
            self._active(grant, context)
            await self._save(grant.model_copy(update={"cleanup_pending": False}))

    async def claim(self, context: ExecutionContext, intent: CommandIntent) -> ExecutionCommand:
        async with self._database.transaction():
            grant = await self._get(context.identity)
            self._active(grant, context)
            if grant.cleanup_pending:
                raise ExecutionDenied("cleanup_pending")
            if intent.capability not in grant.scope.capabilities:
                raise ExecutionDenied("grant_scope_mismatch")
            if grant.scope.kind == "bash" and (
                intent.kind != "bash"
                or intent.payload_sha256 != grant.scope.script_sha256
                or intent.cwd != grant.scope.cwd
            ):
                raise ExecutionDenied("grant_scope_mismatch")
            if intent.copy_revision != grant.copy_revision:
                raise ExecutionDenied("copy_revision_stale")
            if (
                grant.commands_used >= grant.scope.max_commands
                or grant.charged_ms + intent.timeout_ms > grant.scope.max_execution_ms
                or self._clock() + intent.timeout_ms > (grant.expires_at_ms or 0)
            ):
                raise ExecutionDenied("task_budget_exhausted")
            command = ExecutionCommand(
                task_id=context.identity.task_id, intent=intent, state="preparing"
            )
            try:
                await self._db.execute(
                    "INSERT INTO execution_commands VALUES (?, ?, 'preparing', ?)",
                    (command.task_id, intent.command_id, command.model_dump_json()),
                )
            except aiosqlite.IntegrityError:
                raise ExecutionDenied("command_replay_or_busy") from None
            # Conservative reservation: even edits use a command/time slot. No
            # attempt, tool, failed test or model turn can reset the task budgets.
            await self._save(
                grant.model_copy(
                    update={
                        "commands_used": grant.commands_used + 1,
                        "charged_ms": grant.charged_ms + intent.timeout_ms,
                        "copy_revision": grant.copy_revision + 1,
                    }
                )
            )
            return command

    async def _command(self, task_id: str, command_id: str) -> ExecutionCommand:
        async with self._db.execute(
            "SELECT command_json FROM execution_commands WHERE task_id=? AND command_id=?",
            (task_id, command_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ExecutionDenied("command_required")
        return ExecutionCommand.model_validate_json(row[0])

    async def _save_command(self, command: ExecutionCommand) -> None:
        await self._db.execute(
            "UPDATE execution_commands SET state=?, command_json=? "
            "WHERE task_id=? AND command_id=?",
            (command.state, command.model_dump_json(), command.task_id, command.intent.command_id),
        )

    async def start(self, context: ExecutionContext, intent: CommandIntent) -> ExecutionCommand:
        async with self._database.transaction():
            grant = await self._get(context.identity)
            self._active(grant, context)
            command = await self._command(context.identity.task_id, intent.command_id)
            if command.state != "preparing" or command.intent != intent:
                raise ExecutionDenied("command_replay_or_changed")
            if self._clock() + intent.timeout_ms > (grant.expires_at_ms or 0):
                raise ExecutionDenied("task_budget_exhausted")
            updated = command.model_copy(
                update={"state": "running", "started_at_ms": self._clock()}
            )
            await self._save_command(updated)
            return updated

    async def finish(
        self,
        context: ExecutionContext,
        intent: CommandIntent,
        *,
        exit_code: int,
    ) -> ExecutionCommand:
        async with self._database.transaction():
            grant = await self._get(context.identity)
            self._active(grant, context)
            command = await self._command(context.identity.task_id, intent.command_id)
            if command.state != "running" or command.intent != intent:
                raise ExecutionDenied("command_replay_or_changed")
            elapsed = max(0, self._clock() - (command.started_at_ms or 0))
            if elapsed > intent.timeout_ms:
                raise ExecutionDenied("command_timeout")
            updated = command.model_copy(
                update={
                    "state": "succeeded" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "finished_at_ms": self._clock(),
                }
            )
            await self._save_command(updated)
            await self._save(
                grant.model_copy(
                    update={"charged_ms": grant.charged_ms - intent.timeout_ms + elapsed}
                )
            )
            return updated

    async def terminate(
        self,
        identity: ExecutionIdentity,
        *,
        state: Literal["closed", "denied", "revoked", "expired", "interrupted"] = "revoked",
    ) -> ExecutionGrant:
        async with self._database.transaction():
            grant = await self._get(identity)
            if grant.state not in {"pending", "approved", "active"}:
                return grant
            updated = grant.model_copy(
                update={
                    "state": state,
                    "cleanup_pending": grant.cleanup_pending or grant.state == "active",
                }
            )
            await self._save(updated)
            async with self._db.execute(
                "SELECT command_json FROM execution_commands "
                "WHERE task_id=? AND state IN ('preparing','running')",
                (identity.task_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                command = ExecutionCommand.model_validate_json(row[0])
                await self._save_command(
                    command.model_copy(
                        update={"state": "interrupted", "finished_at_ms": self._clock()}
                    )
                )
            return updated

    async def confirm_cleanup(self, identity: ExecutionIdentity) -> None:
        """Only after trusted backend reconciliation verifies the resource absent."""
        async with self._database.transaction():
            grant = await self._get(identity)
            if grant.state in {"pending", "approved", "active"}:
                raise ExecutionDenied("task_not_closed")
            await self._save(grant.model_copy(update={"cleanup_pending": False}))

    async def recover_interrupted(self) -> list[ExecutionIdentity]:
        # Startup only, before accepting requests. Return cleanup obligations;
        # pending/approved tasks are invalidated too, but need no container cleanup.
        async with self._database.operation():
            async with self._db.execute(
                "SELECT grant_json FROM execution_grants "
                "WHERE state IN ('pending','approved','active') OR cleanup_pending=1"
            ) as cursor:
                rows = await cursor.fetchall()
            pending: list[ExecutionIdentity] = []
            for row in rows:
                grant = ExecutionGrant.model_validate_json(row[0])
                updated = await self.terminate(grant.scope.identity, state="interrupted")
                if updated.cleanup_pending:
                    pending.append(updated.scope.identity)
            return pending

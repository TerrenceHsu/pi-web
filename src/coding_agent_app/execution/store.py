"""Durable grant/command CAS using the existing shared Session SQLite coordinator.

No backend is called inside a transaction. Authorization is recorded before any
effect. Restart recovery invalidates unfinished work; it never resumes commands.
"""

from __future__ import annotations

import json
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
        self,
        connection: aiosqlite.Connection,
        *,
        clock_ms: Callable[[], int] | None = None,
        account_limit: int = 1,
        global_limit: int = 2,
    ) -> None:
        self._db = connection
        self._database = database_for(connection)
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._account_limit, self._global_limit = account_limit, global_limit

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
                CREATE TABLE IF NOT EXISTS execution_private_logs (
                    task_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    input_json TEXT NOT NULL, result_json TEXT,
                    PRIMARY KEY(task_id,command_id)
                );
                CREATE TABLE IF NOT EXISTS execution_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    created_ms INTEGER NOT NULL, attributes_json TEXT NOT NULL
                );
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
        previous = await self._get(grant.scope.identity)
        await self._db.execute(
            "UPDATE execution_grants SET state=?, cleanup_pending=?, grant_json=? WHERE task_id=?",
            (
                grant.state,
                int(grant.cleanup_pending),
                grant.model_dump_json(),
                grant.scope.identity.task_id,
            ),
        )
        if (previous.state, previous.cleanup_pending) != (grant.state, grant.cleanup_pending):
            await self._observe(grant)

    async def _observe(self, grant: ExecutionGrant) -> None:
        scope = grant.scope
        attributes = {
            **scope.identity.model_dump(),
            "kind": scope.kind,
            "backend": scope.backend,
            "phase": grant.state,
            "cleanup_pending": grant.cleanup_pending,
            "commands_used": grant.commands_used,
            "charged_ms": grant.charged_ms,
            "max_commands": scope.max_commands,
            "max_execution_ms": scope.max_execution_ms,
            "runtime_id": scope.runtime_id,
            "policy_sha256": scope.policy_sha256,
        }
        await self._db.execute(
            "INSERT INTO execution_observations(task_id,created_ms,attributes_json) VALUES(?,?,?)",
            (scope.identity.task_id, self._clock(), json.dumps(attributes)),
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
            await self._observe(grant)
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
            if any(row[2] and row[1] != "active" for row in rows):
                raise ExecutionDenied("cleanup_pending")
            if (
                len(rows) >= self._global_limit
                or sum(row[0] == context.identity.account_id for row in rows) >= self._account_limit
            ):
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
        if command.state in {"succeeded", "failed", "interrupted"}:
            async with self._db.execute(
                "SELECT grant_json FROM execution_grants WHERE task_id=?", (command.task_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                grant = ExecutionGrant.model_validate_json(row[0])
                attributes = {
                    **grant.scope.identity.model_dump(),
                    "kind": grant.scope.kind,
                    "backend": grant.scope.backend,
                    "command_id": command.intent.command_id,
                    "command_kind": command.intent.kind,
                    "phase": command.state,
                    "exit_code": command.exit_code,
                    "duration_ms": max(
                        0, (command.finished_at_ms or 0) - (command.started_at_ms or 0)
                    ),
                }
                async with self._db.execute(
                    "SELECT result_json FROM execution_private_logs "
                    "WHERE task_id=? AND command_id=?",
                    (command.task_id, command.intent.command_id),
                ) as cursor:
                    logged = await cursor.fetchone()
                if logged is not None and logged[0] is not None:
                    result = json.loads(logged[0])
                    for key in (
                        "stdout_bytes",
                        "stderr_bytes",
                        "output_truncated",
                        "termination_reason",
                        "error_code",
                    ):
                        attributes[key] = result.get(key)
                await self._db.execute(
                    "INSERT INTO execution_observations(task_id,created_ms,attributes_json) "
                    "VALUES(?,?,?)",
                    (command.task_id, self._clock(), json.dumps(attributes)),
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

    async def list_grants(self, *, session_id: str | None = None) -> list[ExecutionGrant]:
        async with self._database.operation():
            async with self._db.execute(
                "SELECT grant_json FROM execution_grants ORDER BY rowid DESC"
            ) as cursor:
                grants = [
                    ExecutionGrant.model_validate_json(row[0]) for row in await cursor.fetchall()
                ]
        return [
            g for g in grants if session_id is None or g.scope.identity.session_id == session_id
        ]

    async def commands(self, identity: ExecutionIdentity) -> list[ExecutionCommand]:
        async with self._database.operation():
            await self._get(identity)
            async with self._db.execute(
                "SELECT command_json FROM execution_commands WHERE task_id=? ORDER BY rowid",
                (identity.task_id,),
            ) as cursor:
                return [ExecutionCommand.model_validate_json(r[0]) for r in await cursor.fetchall()]

    async def private_input(
        self, identity: ExecutionIdentity, command_id: str, payload: str
    ) -> None:
        async with self._database.transaction():
            await self._get(identity)
            await self._db.execute(
                "INSERT INTO execution_private_logs VALUES(?,?,?,NULL)",
                (identity.task_id, command_id, payload[:131072]),
            )

    async def private_result(
        self, identity: ExecutionIdentity, command_id: str, payload: str
    ) -> None:
        async with self._database.transaction():
            await self._get(identity)
            await self._db.execute(
                "UPDATE execution_private_logs SET result_json=? "
                "WHERE task_id=? AND command_id=? AND result_json IS NULL",
                (payload, identity.task_id, command_id),
            )

    async def private_logs(self, identity: ExecutionIdentity) -> list[dict[str, object]]:
        async with self._database.operation():
            await self._get(identity)
            async with self._db.execute(
                "SELECT command_id,input_json,result_json FROM execution_private_logs "
                "WHERE task_id=?",
                (identity.task_id,),
            ) as cursor:
                return [
                    {
                        "command_id": r[0],
                        "input": r[1],
                        "result": None if r[2] is None else json.loads(r[2]),
                    }
                    for r in await cursor.fetchall()
                ]

    async def observations(self) -> list[tuple[int, dict[str, object]]]:
        async with self._database.operation():
            async with self._db.execute(
                "SELECT id,attributes_json FROM execution_observations ORDER BY id LIMIT 100"
            ) as cursor:
                return [(r[0], json.loads(r[1])) for r in await cursor.fetchall()]

    async def acknowledge_observation(self, identifier: int) -> None:
        async with self._database.transaction():
            await self._db.execute("DELETE FROM execution_observations WHERE id=?", (identifier,))

    async def prune_history(self) -> None:
        """Retain at most 200 terminal tasks / 30 days, never prune cleanup debt."""
        async with self._database.transaction():
            async with self._db.execute(
                "SELECT grant_json FROM execution_grants WHERE "
                "state NOT IN ('pending','approved','active') AND cleanup_pending=0 "
                "ORDER BY rowid DESC"
            ) as cursor:
                rows = await cursor.fetchall()
            for index, row in enumerate(rows):
                grant = ExecutionGrant.model_validate_json(row[0])
                if index < 200 and grant.created_at_ms >= self._clock() - 30 * 86400_000:
                    continue
                identifier = (grant.scope.identity.task_id,)
                for table in ("execution_private_logs", "execution_commands"):
                    await self._db.execute(f"DELETE FROM {table} WHERE task_id=?", identifier)
            await self._db.execute(
                "DELETE FROM execution_observations WHERE created_ms<?",
                (self._clock() - 30 * 86400_000,),
            )

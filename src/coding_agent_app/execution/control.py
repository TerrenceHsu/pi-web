"""Shared durable admission and cleanup ledger, outside per-account Session databases.

Only trusted runtime composition reserves/releases resources. Admin revocation is
an irreversible stop flag, not execution authority. Cleanup never resumes work.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from coding_sandbox.local_docker import LocalDockerExecutionConfig, LocalDockerSandboxBackend
from coding_sandbox.models import SandboxLimits

from .models import ExecutionDenied, ExecutionGrant, ExecutionIdentity, ExecutionScope


class ExecutionQuota(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    account_tasks: int = Field(default=2, ge=1, le=8)
    global_tasks: int = Field(default=4, ge=1, le=32)
    cpu: int = Field(default=8, ge=1, le=32)
    memory_mb: int = Field(default=8192, ge=1024, le=65536)
    account_cache_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=100 * 1024 * 1024)


class ExecutionLease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scope: ExecutionScope
    limits: SandboxLimits
    namespace: str
    expires_ms: int
    heartbeat_ms: int
    stop: bool = False
    released: bool = False
    error_code: str | None = None
    phase: str = "active"
    commands_used: int = 0
    charged_ms: int = 0


LocalFactory = Callable[[LocalDockerExecutionConfig], object]
Revoke = Callable[[ExecutionIdentity], Awaitable[None]]
CleanupObserver = Callable[[dict[str, object]], Awaitable[None]]


class ExecutionControl:
    def __init__(self, path: Path, *, quota: ExecutionQuota | None = None) -> None:
        self.path = path
        self.quota = quota or ExecutionQuota()
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._maintenance_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._factories: dict[str, LocalFactory] = {}
        self._revokers: dict[str, Revoke] = {}
        self._observers: dict[str, CleanupObserver] = {}
        self._deployments: dict[str, tuple[LocalDockerExecutionConfig, LocalFactory]] = {}
        self.last_cleanup_ms: int | None = None
        self.maintenance_error: str | None = None

    @staticmethod
    def now() -> int:
        return time.time_ns() // 1_000_000

    def namespace(self, prefix: str) -> str:
        """Do not let two independent installations sweep each other's default namespace."""
        digest = hashlib.sha256(os.path.normcase(str(self.path.resolve())).encode()).hexdigest()[
            :16
        ]
        return f"{prefix[:15]}-{digest}"

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise ExecutionDenied("execution_control_unavailable")
        return self._db

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS execution_leases (
                task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL, released INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS execution_global_workspace
                ON execution_leases(account_id,workspace_id) WHERE released=0;
            CREATE TABLE IF NOT EXISTS execution_control_health (
                namespace TEXT PRIMARY KEY, error_code TEXT
            );
        """)
        await self._db.commit()
        # Heartbeat expiry, not unconditional startup invalidation: a second
        # connection/process must not revoke another live server's leases.
        self._worker = asyncio.create_task(self._watch(), name="execution_cleanup")

    def register(
        self,
        account: str,
        revoke: Revoke,
        factory: LocalFactory | None,
        config: LocalDockerExecutionConfig | None = None,
        observer: CleanupObserver | None = None,
    ) -> None:
        self._revokers[account] = revoke
        if observer is not None:
            self._observers[account] = observer
        if factory is not None:
            self._factories[account] = factory
            if config is not None and config.enabled:
                self._deployments[config.namespace] = (config, factory)

    def unregister(self, account: str) -> None:
        self._revokers.pop(account, None)
        self._observers.pop(account, None)
        # Keep transport factories for cleanup of unopened/restarted accounts.

    async def _rows(self, *, active_only: bool = False) -> list[ExecutionLease]:
        sql = "SELECT payload FROM execution_leases"
        if active_only:
            sql += " WHERE released=0"
        async with self._connection().execute(sql + " ORDER BY rowid DESC") as cursor:
            return [ExecutionLease.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def _save(self, lease: ExecutionLease) -> None:
        identity = lease.scope.identity
        await self._connection().execute(
            "INSERT INTO execution_leases VALUES (?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE "
            "SET released=excluded.released,payload=excluded.payload",
            (
                identity.task_id,
                identity.account_id,
                identity.workspace_id,
                int(lease.released),
                lease.model_dump_json(),
            ),
        )

    async def reserve(
        self, scope: ExecutionScope, limits: SandboxLimits, *, namespace: str
    ) -> None:
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                rows = await self._rows(active_only=True)
                if scope.backend == "local_docker":
                    async with db.execute(
                        "SELECT 1 FROM execution_control_health WHERE error_code IS NOT NULL"
                    ) as cursor:
                        if await cursor.fetchone():
                            raise ExecutionDenied("cleanup_pending")
                now, quota = self.now(), self.quota
                if any(
                    r.stop or r.heartbeat_ms + 60_000 <= now or r.expires_ms <= now for r in rows
                ):
                    raise ExecutionDenied("cleanup_pending")
                own = [r for r in rows if r.scope.identity.account_id == scope.identity.account_id]
                if (
                    len(rows) >= quota.global_tasks
                    or len(own) >= quota.account_tasks
                    or sum(r.limits.cpu for r in rows) + limits.cpu > quota.cpu
                    or sum(r.limits.memory_mb for r in rows) + limits.memory_mb > quota.memory_mb
                ):
                    raise ExecutionDenied("execution_quota_exceeded")
                async with db.execute(
                    "SELECT 1 FROM execution_leases WHERE task_id=?", (scope.identity.task_id,)
                ) as cursor:
                    if await cursor.fetchone():
                        raise ExecutionDenied("execution_replay_denied")
                await self._save(
                    ExecutionLease(
                        scope=scope,
                        limits=limits,
                        namespace=namespace,
                        heartbeat_ms=now,
                        expires_ms=now + scope.lifetime_ms,
                    )
                )
                await db.commit()
            except BaseException as exc:
                await db.rollback()
                if isinstance(exc, aiosqlite.IntegrityError):
                    raise ExecutionDenied("workspace_busy_or_task_exists") from None
                raise

    async def _get(self, task_id: str) -> ExecutionLease | None:
        async with self._connection().execute(
            "SELECT payload FROM execution_leases WHERE task_id=?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else ExecutionLease.model_validate_json(row[0])

    async def allowed(self, identity: ExecutionIdentity) -> bool:
        async with self._lock:
            lease = await self._get(identity.task_id)
            # Preparation has no resource reservation yet.
            return lease is None or (
                lease.scope.identity == identity
                and not lease.stop
                and not lease.released
                and lease.expires_ms > self.now()
                and lease.heartbeat_ms + 60_000 > self.now()
            )

    async def recovery_lease(self, identity: ExecutionIdentity) -> ExecutionLease | None:
        async with self._lock:
            lease = await self._get(identity.task_id)
            if lease is not None and lease.scope.identity != identity:
                raise ExecutionDenied("grant_scope_mismatch")
            return lease

    async def update(
        self,
        identity: ExecutionIdentity,
        *,
        released: bool = False,
        stop: bool = False,
        heartbeat: bool = False,
        error_code: str | None = None,
        expired_only: bool = False,
        grant: ExecutionGrant | None = None,
    ) -> bool:
        async with self._lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                lease = await self._get(identity.task_id)
                if lease is not None:
                    if lease.scope.identity != identity:
                        raise ExecutionDenied("grant_scope_mismatch")
                    expired = (
                        lease.expires_ms <= self.now() or lease.heartbeat_ms + 60_000 <= self.now()
                    )
                    if expired_only and not lease.stop and not expired:
                        await db.commit()
                        return False
                    await self._save(
                        lease.model_copy(
                            update={
                                "stop": lease.stop or stop or (heartbeat and expired),
                                "released": lease.released or released,
                                "heartbeat_ms": self.now()
                                if heartbeat and not expired
                                else lease.heartbeat_ms,
                                "error_code": error_code,
                                "phase": grant.state if grant is not None else lease.phase,
                                "commands_used": grant.commands_used
                                if grant is not None
                                else lease.commands_used,
                                "charged_ms": grant.charged_ms
                                if grant is not None
                                else lease.charged_ms,
                            }
                        )
                    )
                await db.commit()
                return lease is not None
            except BaseException:
                await db.rollback()
                raise

    async def revoke(self, task_id: str) -> bool:
        async with self._lock:
            lease = await self._get(task_id)
        if lease is None:
            return False
        await self.update(lease.scope.identity, stop=True)
        self._wake.set()
        return True

    def request_cleanup(self) -> None:
        self._wake.set()

    async def maintain(self) -> None:
        async with self._maintenance_lock:
            async with self._lock:
                rows = await self._rows(active_only=True)
            for lease in rows:
                now = self.now()
                if not (
                    lease.stop or lease.expires_ms <= now or lease.heartbeat_ms + 60_000 <= now
                ):
                    continue
                identity = lease.scope.identity
                if not await self.update(
                    identity,
                    stop=True,
                    error_code="cleanup_pending",
                    expired_only=True,
                ):
                    continue
                revoke = self._revokers.get(identity.account_id)
                if revoke is not None:
                    try:
                        async with asyncio.timeout(35):
                            await revoke(identity)
                    except Exception:
                        pass
                async with self._lock:
                    current = await self._get(identity.task_id)
                if current is None or current.released:
                    continue
                # A namespace + image + exact operation label is mandatory for deletion.
                factory = self._factories.get(identity.account_id)
                if factory is None and self._factories:
                    factory = next(iter(self._factories.values()))
                if lease.scope.backend == "local_docker" and factory is not None:
                    try:
                        backend = factory(
                            LocalDockerExecutionConfig(
                                enabled=True,
                                image_id=lease.scope.runtime_id,
                                namespace=lease.namespace,
                                limits=lease.limits,
                            )
                        )
                        if isinstance(backend, LocalDockerSandboxBackend):
                            async with asyncio.timeout(35):
                                await backend.reconcile_operation(identity.operation_id)
                            await self.update(identity, released=True)
                            await self._observe_cleanup(
                                {
                                    **identity.model_dump(),
                                    "phase": "resource_released",
                                    "backend": "local_docker",
                                    "kind": lease.scope.kind,
                                }
                            )
                    except Exception:
                        await self.update(identity, stop=True, error_code="cleanup_pending")
                        if lease.error_code != "cleanup_pending":
                            await self._observe_cleanup(
                                {
                                    **identity.model_dump(),
                                    "phase": "cleanup_pending",
                                    "backend": "local_docker",
                                    "kind": lease.scope.kind,
                                }
                            )
            await self._orphans()
            async with self._lock:
                await self._connection().execute(
                    "DELETE FROM execution_leases WHERE released=1 "
                    "AND json_extract(payload,'$.expires_ms')<?",
                    (self.now() - 30 * 86400_000,),
                )
                await self._connection().commit()
            self.last_cleanup_ms = self.now()
            self.maintenance_error = None

    async def _orphans(self) -> None:
        for namespace, (config, factory) in tuple(self._deployments.items()):
            error = None
            try:
                backend = factory(config)
                if not isinstance(backend, LocalDockerSandboxBackend):
                    continue  # Contract fakes are not proof of Docker availability.
                async with asyncio.timeout(35):
                    operations = await backend.managed_operations()
                    for operation, image in operations:
                        # Read AFTER listing. A concurrent creator commits its lease
                        # before Docker create, so it cannot be mistaken for an orphan.
                        async with self._lock:
                            known = await self._rows(active_only=True)
                        if any(r.scope.identity.operation_id == operation for r in known):
                            continue
                        cleaner = factory(config.model_copy(update={"image_id": image}))
                        if isinstance(cleaner, LocalDockerSandboxBackend):
                            await cleaner.reconcile_operation(operation)
                            await self._observe_cleanup(
                                {
                                    "account_id": "system",
                                    "account_name": "Local execution",
                                    "operation_id": operation,
                                    "phase": "orphan_removed",
                                    "backend": "local_docker",
                                    "runtime_id": image,
                                }
                            )
            except Exception:
                error = "docker_cleanup_unavailable"
            async with self._lock:
                await self._connection().execute(
                    "INSERT INTO execution_control_health VALUES(?,?) "
                    "ON CONFLICT(namespace) DO UPDATE SET error_code=excluded.error_code",
                    (namespace, error),
                )
                await self._connection().commit()

    async def _observe_cleanup(self, attributes: dict[str, object]) -> None:
        observer = self._observers.get(str(attributes.get("account_id")))
        if observer is None:
            observer = next(iter(self._observers.values()), None)
        if observer is not None:
            try:
                await observer(attributes)
            except Exception:
                pass  # Cleanup is authoritative; Telemetry is always passive.

    async def summary(self) -> dict[str, Any]:
        async with self._lock:
            rows = await self._rows(active_only=True)
            async with self._connection().execute(
                "SELECT payload FROM execution_leases WHERE released=1 "
                "ORDER BY rowid DESC LIMIT 100"
            ) as cursor:
                recent = [ExecutionLease.model_validate_json(r[0]) for r in await cursor.fetchall()]
            async with self._connection().execute(
                "SELECT error_code FROM execution_control_health WHERE error_code IS NOT NULL"
            ) as cursor:
                health = [r[0] for r in await cursor.fetchall()]
        pending = sum(
            r.stop or r.expires_ms <= self.now() or r.heartbeat_ms + 60_000 <= self.now()
            for r in rows
        )
        return {
            "quota": self.quota.model_dump(),
            "active_tasks": len(rows),
            "cpu": sum(r.limits.cpu for r in rows),
            "memory_mb": sum(r.limits.memory_mb for r in rows),
            "cleanup_pending": pending,
            "admission_paused": bool(pending or health),
            "last_cleanup_ms": self.last_cleanup_ms,
            "error_code": self.maintenance_error or next(iter(health), None),
            "tasks": [
                {
                    **r.scope.identity.model_dump(),
                    "backend": r.scope.backend,
                    "kind": r.scope.kind,
                    "runtime_id": r.scope.runtime_id,
                    "policy_sha256": r.scope.policy_sha256,
                    "expires_ms": r.expires_ms,
                    "stop_requested": r.stop,
                    "released": r.released,
                    "phase": r.phase,
                    "commands_used": r.commands_used,
                    "max_commands": r.scope.max_commands,
                    "charged_ms": r.charged_ms,
                    "max_execution_ms": r.scope.max_execution_ms,
                    "error_code": r.error_code,
                }
                for r in [*rows, *recent]
            ],
        }

    async def _watch(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.maintain()
            except Exception:
                self.maintenance_error = "cleanup_scan_failed"

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._db is not None:
            await self._db.close()
            self._db = None

"""Fail-closed command boundary for the upcoming Web/Coding/Plan adapters.

This module does not register a tool or infer consent from a mode/AllowAll policy.
All callbacks are trusted composition ports, not values accepted from an Agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from coding_sandbox.models import SandboxCommandResult

from .models import (
    CommandIntent,
    ExecutionContext,
    ExecutionDenied,
    ExecutionGrant,
    ExecutionIdentity,
    ExecutionScope,
)
from .store import ExecutionStore

ScopeCheck = Callable[[ExecutionScope], Awaitable[bool]]
Cleanup = Callable[[ExecutionIdentity], Awaitable[bool]]
RunCommand = Callable[[asyncio.Event], Awaitable[SandboxCommandResult]]


class CommandInterrupted(ExecutionDenied):
    """Known bounded backend termination; not an authorization to retry it."""

    def __init__(self, result: SandboxCommandResult) -> None:
        super().__init__("execution_interrupted")
        self.result = result


class ExecutionService:
    def __init__(self, store: ExecutionStore, *, scope_check: ScopeCheck, cleanup: Cleanup) -> None:
        self.store = store
        self._scope_check = scope_check
        self._cleanup = cleanup
        self._signals: dict[str, asyncio.Event] = {}

    async def check(self, context: ExecutionContext) -> ExecutionGrant:
        try:
            grant = await self.store.check_active(context)
        except ExecutionDenied as exc:
            if exc.code == "grant_expired":
                await self.store.terminate(context.identity, state="expired")
                await self._reconcile(context.identity)
            raise
        if grant.cleanup_pending:
            raise ExecutionDenied("cleanup_pending")
        if not await self._scope_check(grant.scope):
            await self.revoke(context.identity)
            raise ExecutionDenied("grant_revoked")
        return grant

    async def revoke(self, identity: ExecutionIdentity) -> None:
        # Identity is checked by the store BEFORE touching another task's signal.
        grant = await self.store.terminate(identity)
        signal = self._signals.get(identity.task_id)
        if signal is not None:
            signal.set()
        if grant.cleanup_pending:
            await self._reconcile(identity)

    async def _reconcile(self, identity: ExecutionIdentity) -> None:
        try:
            if await self._cleanup(identity):
                await self.store.confirm_cleanup(identity)
        except Exception:
            # Durable cleanup_pending remains true. The runtime cannot accept a
            # new task until a trusted reconciler confirms resource absence.
            pass

    async def execute(
        self, context: ExecutionContext, intent: CommandIntent, *, run: RunCommand
    ) -> SandboxCommandResult:
        # Invalid role/identity/hash never revokes or affects somebody else's grant.
        try:
            grant = await self.store.check_active(context)
        except ExecutionDenied as exc:
            if exc.code == "grant_expired":
                await self.store.terminate(context.identity, state="expired")
                await self._reconcile(context.identity)
            raise
        if context.identity.task_id in self._signals:
            raise ExecutionDenied("command_replay_or_busy")
        signal = asyncio.Event()
        self._signals[context.identity.task_id] = signal
        job: asyncio.Task[SandboxCommandResult] | None = None
        try:
            if not await self._scope_check(grant.scope):
                raise ExecutionDenied("grant_scope_mismatch")
            await self.store.claim(context, intent)
            await self.store.start(context, intent)
            # Durable running CAS precedes effects. Never retry an unknown outcome.
            async with asyncio.timeout(intent.timeout_ms / 1000):

                async def invoke() -> SandboxCommandResult:
                    return await run(signal)

                job = asyncio.create_task(invoke())
                while not job.done():
                    await asyncio.wait((job,), timeout=0.1)
                    await self.store.check_active(context)
                    if signal.is_set() or not await self._scope_check(grant.scope):
                        raise ExecutionDenied("grant_revoked")
                result = await job
            if result.command_id != intent.command_id:
                # Output from another command cannot be attributed to this call.
                raise ExecutionDenied("execution_interrupted")
            if result.termination_reason != "exited" or result.exit_code is None:
                raise CommandInterrupted(result)
            await self.store.finish(context, intent, exit_code=result.exit_code)
            return result
        except BaseException as exc:
            signal.set()
            if job is not None and not job.done():
                job.cancel()
            # Revoke before waiting for transport teardown, including caller cancellation.
            await asyncio.shield(self.store.terminate(context.identity, state="interrupted"))
            await asyncio.shield(self._reconcile(context.identity))
            if job is not None:
                await asyncio.gather(job, return_exceptions=True)
            if isinstance(exc, (asyncio.CancelledError, ExecutionDenied)):
                raise
            code = "command_timeout" if isinstance(exc, TimeoutError) else "execution_interrupted"
            raise ExecutionDenied(code) from None
        finally:
            self._signals.pop(context.identity.task_id, None)

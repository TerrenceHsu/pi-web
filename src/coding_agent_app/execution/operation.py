"""ExecutionGrant adapter at the public SandboxOperation boundary."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Literal, TypeVar
from uuid import uuid4

from coding_sandbox.access import Invocation
from coding_sandbox.models import SandboxCommand, SandboxCommandResult, SandboxHandle

from .context import current_execution_context
from .models import CommandIntent, ExecutionContext, ExecutionDenied, ExecutionScope, digest_json
from .service import ExecutionService

T = TypeVar("T")


class GrantedOperationAccess:
    def __init__(self, service: ExecutionService, scope: ExecutionScope) -> None:
        self._service, self._scope = service, scope

    def _context(self, handle: SandboxHandle, *, writing: bool) -> ExecutionContext:
        context = current_execution_context()
        if (
            context.identity != self._scope.identity
            or context.scope_sha256 != self._scope.sha256
            or handle.operation_id != context.identity.operation_id
            or handle.provider != self._scope.backend
            or handle.runtime_id != self._scope.runtime_id
        ):
            raise ExecutionDenied("grant_scope_mismatch")
        if context.role == "planner" or writing and context.role != "executor":
            raise ExecutionDenied("execution_role_denied")
        return context

    async def check(self, handle: SandboxHandle, *, writing: bool) -> None:
        context = self._context(handle, writing=writing)
        # A Verifier may inspect the copy, but never use this adapted context for
        # an execute/mutate call. Those always recheck the original server role.
        await self._service.check(context.model_copy(update={"role": "executor"}))

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        kind: Literal["argv", "bash", "validation"],
        script_sha256: str | None,
        operation_revision: int,
        invoke: Invocation,
    ) -> SandboxCommandResult:
        context = self._context(handle, writing=True)
        grant = await self._service.check(context)
        cwd = PurePosixPath(command.cwd).relative_to("/workspace").as_posix()
        payload = (
            script_sha256
            if kind == "bash"
            else digest_json(
                {
                    "argv": command.argv,
                    "cwd": cwd,
                    "operation_revision": operation_revision,
                    "capture_bytes": command.max_output_bytes,
                }
            )
        )
        if payload is None:
            raise ExecutionDenied("grant_scope_mismatch")
        intent = CommandIntent(
            command_id=command.command_id,
            capability="validate" if kind == "validation" else "execute",
            kind=kind,
            payload_sha256=payload,
            cwd=cwd,
            copy_revision=grant.copy_revision,
            timeout_ms=command.timeout_seconds * 1000,
        )
        return await self._service.execute(context, intent, run=invoke)

    async def mutate(
        self,
        handle: SandboxHandle,
        *,
        payload: bytes,
        operation_revision: int,
        timeout_seconds: int,
        invoke: Callable[[], Awaitable[T]],
    ) -> T:
        context = self._context(handle, writing=True)
        grant = await self._service.check(context)
        identifier = f"edit-{uuid4().hex}"
        intent = CommandIntent(
            command_id=identifier,
            capability="edit",
            kind="edit",
            timeout_ms=timeout_seconds * 1000,
            payload_sha256=digest_json(
                {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "operation_revision": operation_revision,
                }
            ),
            copy_revision=grant.copy_revision,
        )
        result: list[T] = []

        async def run(signal: asyncio.Event) -> SandboxCommandResult:
            if signal.is_set():
                raise asyncio.CancelledError
            started = int(time.time() * 1000)
            result.append(await invoke())
            return SandboxCommandResult(
                command_id=identifier, started_at_ms=started, finished_at_ms=int(time.time() * 1000)
            )

        await self._service.execute(context, intent, run=run)
        return result[0]

"""Product-supplied authorization port; the backend package never imports Web."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TypeVar

from .models import SandboxCommand, SandboxCommandResult, SandboxHandle

T = TypeVar("T")
Invocation = Callable[[asyncio.Event], Awaitable[SandboxCommandResult]]


class OperationExecutionGuard(Protocol):
    async def check(self, handle: SandboxHandle, *, writing: bool) -> None: ...

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        kind: Literal["argv", "bash", "validation"],
        script_sha256: str | None,
        operation_revision: int,
        invoke: Invocation,
    ) -> SandboxCommandResult: ...

    async def mutate(
        self,
        handle: SandboxHandle,
        *,
        payload: bytes,
        operation_revision: int,
        timeout_seconds: int,
        invoke: Callable[[], Awaitable[T]],
    ) -> T: ...

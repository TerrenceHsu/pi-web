"""Provider-neutral async backend contract for disposable coding sandboxes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    SandboxBackendName,
    SandboxCommand,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxHandle,
    SandboxOutputCallback,
    SandboxStatus,
    SandboxTransferReceipt,
)


@runtime_checkable
class SandboxBackend(Protocol):
    """Uniform contract implemented by E2B, Modal, local and fake backends.

    Contract highlights:

    - all methods are async and safe to call from the FastAPI request runtime;
    - ``create`` returns a credential-free handle suitable for SQLite;
    - ``attach`` never creates a replacement sandbox;
    - transfers return size/hash evidence;
    - non-zero command exits are normal ``SandboxCommandResult`` values;
    - ``destroy`` is idempotent;
    - provider/transport failures use ``SandboxError`` fixed codes.
    """

    def backend_name(self) -> SandboxBackendName:
        """Return the stable provider identifier without contacting it."""
        ...

    async def create(self, spec: SandboxCreateSpec) -> SandboxHandle:
        """Create one disposable sandbox from a secret-free specification."""
        ...

    async def attach(self, handle: SandboxHandle) -> SandboxHandle:
        """Reconnect to the exact existing sandbox or raise sandbox_not_found."""
        ...

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        """Return current provider state without creating or resuming work."""
        ...

    async def upload_file(
        self,
        handle: SandboxHandle,
        *,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
    ) -> SandboxTransferReceipt:
        """Upload a bounded local file and verify its content digest."""
        ...

    async def download_file(
        self,
        handle: SandboxHandle,
        *,
        remote_path: str,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> SandboxTransferReceipt:
        """Download one artifact and verify the optional expected digest."""
        ...

    async def execute(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult:
        """Execute argv without accepting a shell source string."""
        ...

    async def destroy(self, handle: SandboxHandle) -> None:
        """Idempotently terminate the sandbox and release provider resources."""
        ...


__all__ = ["SandboxBackend"]

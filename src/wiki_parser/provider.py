"""Provider-neutral asynchronous contract for isolated Wiki parsers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ParserArtifactReceipt,
    ParserJobHandle,
    ParserJobSpec,
    ParserJobStatus,
    ParserProbe,
    ParserProviderName,
)


@runtime_checkable
class ParserProvider(Protocol):
    """Legacy Contract v1 lifecycle retained while Raw Ingestion migrates.

    Provider implementations must translate raw process/container/transport
    exceptions into fixed ``ParserError`` codes.  Handles and statuses are
    safe to persist; source paths and document content are not.
    """

    def provider_name(self) -> ParserProviderName:
        """Return the stable provider identifier without contacting it."""
        ...

    async def probe(self) -> ParserProbe:
        """Return bounded readiness, capability and license-mode metadata."""
        ...

    async def create_job(
        self,
        spec: ParserJobSpec,
        *,
        source_path: Path,
    ) -> ParserJobHandle:
        """Create one exact job after verifying source size and SHA-256."""
        ...

    async def status(self, handle: ParserJobHandle) -> ParserJobStatus:
        """Observe the exact job without starting, resuming or replacing it."""
        ...

    async def wait(
        self,
        handle: ParserJobHandle,
        *,
        signal: asyncio.Event | None = None,
    ) -> ParserJobStatus:
        """Wait for a terminal state while cooperating with cancellation."""
        ...

    async def download_artifact(
        self,
        handle: ParserJobHandle,
        *,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> ParserArtifactReceipt:
        """Atomically download the immutable tar and verify its digest."""
        ...

    async def cancel(self, handle: ParserJobHandle) -> ParserJobStatus:
        """Idempotently request cancellation and return the observed state."""
        ...

    async def destroy(self, handle: ParserJobHandle) -> None:
        """Idempotently release job staging and provider resources."""
        ...


__all__ = ["ParserProvider"]

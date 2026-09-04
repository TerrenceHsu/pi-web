"""Asynchronous provider lifecycle for MinerU Contract v2."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from .contract_v2 import (
    ParserArtifactReceiptV2,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserProbeV2,
    ParserProviderV2Name,
)


@runtime_checkable
class ParserProviderV2(Protocol):
    """Uniform lifecycle for Fake v2 and the isolated MinerU worker."""

    def provider_name(self) -> ParserProviderV2Name:
        """Return the provider identity without contacting the runtime."""
        ...

    async def probe(self) -> ParserProbeV2:
        """Return bounded readiness, license and fixed capability evidence."""
        ...

    async def create_job(
        self,
        spec: ParserJobSpecV2,
        *,
        source_path: Path,
    ) -> ParserJobHandleV2:
        """Create one exact v2 job after source size/SHA verification."""
        ...

    async def status(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        """Observe the job without starting, resuming or replacing it."""
        ...

    async def wait(
        self,
        handle: ParserJobHandleV2,
        *,
        signal: asyncio.Event | None = None,
    ) -> ParserJobStatusV2:
        """Wait for one terminal state while cooperating with cancellation."""
        ...

    async def download_artifact(
        self,
        handle: ParserJobHandleV2,
        *,
        local_path: Path,
        expected_sha256: str | None = None,
    ) -> ParserArtifactReceiptV2:
        """Atomically download the immutable v2 tar with digest evidence."""
        ...

    async def cancel(self, handle: ParserJobHandleV2) -> ParserJobStatusV2:
        """Idempotently request cancellation and return observed state."""
        ...

    async def destroy(self, handle: ParserJobHandleV2) -> None:
        """Idempotently release all job-local resources."""
        ...


__all__ = ["ParserProviderV2"]

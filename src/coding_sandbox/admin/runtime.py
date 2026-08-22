"""Lifespan composition for sandbox admin persistence and credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .service import (
    SandboxAdminService,
    SandboxBackendFactory,
    SandboxCredentialService,
)
from .store import SQLiteSandboxConfigStore

if TYPE_CHECKING:
    from ..artifact import ArtifactSigner
    from ..lifecycle import ManagedSandboxEvent


class SandboxRuntimeConfigurationError(RuntimeError):
    """Sandbox runtime cannot be safely enabled with the requested flags."""


@dataclass(frozen=True)
class ResolvedSandboxRuntimeConfig:
    runtime_enabled: bool
    api_enabled: bool


@dataclass(repr=False)
class SandboxRuntimeState:
    store: SQLiteSandboxConfigStore
    service: SandboxAdminService
    operation_store: Any | None = None
    lifecycle: Any | None = None


def resolve_sandbox_runtime_configuration(
    *,
    enable_api: bool | None,
    credential_runtime_enabled: bool,
    credential_api_enabled: bool,
    trusted_host_enabled: bool,
    db_path: str | Path | None,
) -> ResolvedSandboxRuntimeConfig:
    file_database = db_path is not None and str(db_path) != ":memory:"
    prerequisites = (
        credential_runtime_enabled
        and credential_api_enabled
        and trusted_host_enabled
        and file_database
    )
    if enable_api is True and not prerequisites:
        raise SandboxRuntimeConfigurationError(
            "coding sandbox API requires credential runtime/API, TrustedHost, "
            "and a file SQLite database"
        )
    api_enabled = prerequisites if enable_api is None else enable_api
    return ResolvedSandboxRuntimeConfig(
        runtime_enabled=api_enabled,
        api_enabled=api_enabled,
    )


@asynccontextmanager
async def sandbox_runtime_context(
    *,
    database_path: str | Path,
    credential_service: SandboxCredentialService,
    backend_factory: SandboxBackendFactory | None = None,
    artifact_signer: ArtifactSigner | None = None,
    session_exists: Callable[[str], Awaitable[bool]] | None = None,
    projects_root: Path | None = None,
    publisher_state_root: Path | None = None,
    staging_root: Path | None = None,
    event_sink: Callable[[ManagedSandboxEvent], Awaitable[None]] | None = None,
) -> AsyncIterator[SandboxRuntimeState]:
    store = await SQLiteSandboxConfigStore.open(database_path)
    operation_store = None
    lifecycle = None
    try:
        service = SandboxAdminService(
            store=store,
            credential_service=credential_service,
            backend_factory=backend_factory,
        )
        if artifact_signer is not None:
            if (
                session_exists is None
                or projects_root is None
                or publisher_state_root is None
                or staging_root is None
            ):
                raise SandboxRuntimeConfigurationError(
                    "managed lifecycle requires session and absolute storage roots"
                )
            from ..lifecycle import (
                ManagedSandboxLifecycle,
                SQLiteSandboxOperationStore,
            )

            operation_store = await SQLiteSandboxOperationStore.open(database_path)
            lifecycle = ManagedSandboxLifecycle(
                store=operation_store,
                config_provider=service.get_config,
                backend_resolver=service.resolve_operation_backend,
                artifact_signer=artifact_signer,
                session_exists=session_exists,
                projects_root=projects_root,
                state_root=publisher_state_root,
                staging_root=staging_root,
                event_sink=event_sink,
            )
            await lifecycle.recover_startup()
        yield SandboxRuntimeState(
            store=store,
            service=service,
            operation_store=operation_store,
            lifecycle=lifecycle,
        )
    finally:
        try:
            if lifecycle is not None:
                await lifecycle.shutdown()
        finally:
            try:
                if operation_store is not None:
                    await operation_store.close()
            finally:
                await store.close()


__all__ = [
    "ResolvedSandboxRuntimeConfig",
    "SandboxRuntimeConfigurationError",
    "SandboxRuntimeState",
    "resolve_sandbox_runtime_configuration",
    "sandbox_runtime_context",
]

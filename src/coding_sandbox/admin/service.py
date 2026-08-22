"""Sandbox administration service and destructive connection probe."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Literal, Protocol
from uuid import uuid4

from ..backend import SandboxBackend
from ..e2b import E2BSandboxBackend
from ..errors import SandboxError
from ..models import (
    SandboxCommand,
    SandboxCreateSpec,
    SandboxLimits,
    SandboxNetworkPolicy,
)
from .models import (
    SandboxAdminConfig,
    SandboxConfigRecord,
    SandboxConnectionErrorCode,
    SandboxConnectionTestResult,
)
from .store import SQLiteSandboxConfigStore


class SandboxCredentialService(Protocol):
    """Existing CredentialService surface used without exposing secret refs."""

    async def get(self, credential_id: str) -> object: ...

    async def resolve_secret_for_request(self, credential_id: str) -> str: ...


SandboxBackendFactory = Callable[
    [SandboxAdminConfig, Mapping[str, str]],
    SandboxBackend,
]
SandboxOperationBackendErrorCode = Literal[
    "sandbox_disabled",
    "sandbox_not_configured",
    "credential_unavailable",
    "backend_unavailable",
]


class SandboxOperationBackendError(Exception):
    """Secret-free failure while resolving an operation backend."""

    def __init__(self, code: SandboxOperationBackendErrorCode) -> None:
        super().__init__(code)
        self.code = code


class SandboxAdminService:
    """Coordinates config persistence, credential resolution and probe cleanup."""

    def __init__(
        self,
        *,
        store: SQLiteSandboxConfigStore,
        credential_service: SandboxCredentialService,
        backend_factory: SandboxBackendFactory | None = None,
        now_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._credential_service = credential_service
        self._backend_factory = backend_factory or _default_backend_factory
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._connection_test_lock = asyncio.Lock()

    async def get_config(self) -> SandboxConfigRecord:
        return await self._store.get()

    async def update_config(
        self,
        config: SandboxAdminConfig,
        *,
        expected_revision: int,
    ) -> SandboxConfigRecord:
        for _, credential_id in config.credential_references():
            await self._credential_service.get(credential_id)
        # Exercise the full provider-neutral conversion before persistence.
        config.to_service_config()
        return await self._store.put(config, expected_revision=expected_revision)

    async def test_connection(self) -> SandboxConnectionTestResult:
        async with self._connection_test_lock:
            return await self._test_connection()

    async def resolve_operation_backend(
        self,
        config: SandboxAdminConfig,
    ) -> SandboxBackend:
        """Resolve one backend without retaining or returning credential material."""
        if not config.enabled:
            raise SandboxOperationBackendError("sandbox_disabled")
        credential_references = config.credential_references()
        if not credential_references:
            raise SandboxOperationBackendError("sandbox_not_configured")
        try:
            secrets = {
                name: await self._credential_service.resolve_secret_for_request(credential_id)
                for name, credential_id in credential_references
            }
        except Exception as exc:
            raise SandboxOperationBackendError("credential_unavailable") from exc
        try:
            return self._backend_factory(config, secrets)
        except Exception as exc:
            raise SandboxOperationBackendError("backend_unavailable") from exc
        finally:
            secrets.clear()

    async def _test_connection(self) -> SandboxConnectionTestResult:
        record = await self._store.get()
        config = record.config
        checked_at = self._now_ms()
        started = self._monotonic()
        credential_references = config.credential_references()
        if not credential_references:
            return self._result(
                config,
                checked_at,
                started,
                error_code="not_configured",
            )

        try:
            secrets = {
                name: await self._credential_service.resolve_secret_for_request(credential_id)
                for name, credential_id in credential_references
            }
        except Exception:
            return self._result(
                config,
                checked_at,
                started,
                error_code="credential_unavailable",
            )

        probe_limits = _connection_probe_limits(config.limits)
        try:
            probe_config = config.model_copy(update={"limits": probe_limits})
            backend = self._backend_factory(probe_config, secrets)
        except Exception:
            return self._result(
                config,
                checked_at,
                started,
                error_code="unknown_error",
            )
        finally:
            secrets.clear()

        handle = None
        error_code: SandboxConnectionErrorCode | None = None
        python_available = False
        try:
            handle = await backend.create(
                SandboxCreateSpec(
                    operation_id=f"connection-{uuid4().hex}",
                    runtime_id=config.runtime_id,
                    workdir=config.workdir,
                    # A provider/template probe does not require project egress.
                    network=SandboxNetworkPolicy(),
                    limits=probe_limits,
                )
            )
            result = await backend.execute(
                handle,
                SandboxCommand(
                    command_id=f"probe-{uuid4().hex}",
                    argv=("python3", "--version"),
                    cwd=config.workdir,
                    timeout_seconds=probe_limits.command_timeout_seconds,
                    max_output_bytes=probe_limits.max_output_bytes,
                ),
            )
            if not result.succeeded:
                error_code = "command_failed"
            else:
                python_available = True
        except SandboxError as exc:
            error_code = _map_sandbox_error(exc)
        except Exception:
            error_code = "unknown_error"
        finally:
            if handle is not None:
                try:
                    await backend.destroy(handle)
                except Exception:
                    error_code = "cleanup_failed"
                    python_available = False

        return self._result(
            config,
            checked_at,
            started,
            error_code=error_code,
            python_available=python_available,
        )

    def _result(
        self,
        config: SandboxAdminConfig,
        checked_at: int,
        started: float,
        *,
        error_code: SandboxConnectionErrorCode | None,
        python_available: bool = False,
    ) -> SandboxConnectionTestResult:
        return SandboxConnectionTestResult(
            ok=error_code is None,
            provider=config.provider,
            runtime_id=config.runtime_id,
            duration_ms=max(0, int((self._monotonic() - started) * 1000)),
            checked_at_ms=checked_at,
            python_available=python_available,
            error_code=error_code,
        )


def _default_backend_factory(
    config: SandboxAdminConfig,
    secrets: Mapping[str, str],
) -> SandboxBackend:
    return E2BSandboxBackend(
        api_key=secrets["api_key"],
        default_limits=config.limits,
    )


def _connection_probe_limits(limits: SandboxLimits) -> SandboxLimits:
    lifetime = max(30, min(limits.lifetime_seconds, 60))
    return limits.model_copy(
        update={
            "lifetime_seconds": lifetime,
            "command_timeout_seconds": min(
                limits.command_timeout_seconds,
                lifetime,
                20,
            ),
            "max_output_bytes": min(limits.max_output_bytes, 64 * 1024),
        }
    )


def _map_sandbox_error(exc: SandboxError) -> SandboxConnectionErrorCode:
    mapping: dict[str, SandboxConnectionErrorCode] = {
        "authentication_failed": "authentication_failed",
        "permission_denied": "permission_denied",
        "provider_unavailable": "provider_unavailable",
        "rate_limited": "rate_limited",
        "request_timeout": "request_timeout",
        "command_timeout": "request_timeout",
        "invalid_configuration": "template_invalid",
        "sandbox_not_found": "provider_unavailable",
        "sandbox_not_ready": "provider_unavailable",
        "resource_limit": "template_invalid",
    }
    return mapping.get(exc.code, "unknown_error")


__all__ = [
    "SandboxAdminService",
    "SandboxBackendFactory",
    "SandboxCredentialService",
    "SandboxOperationBackendError",
    "SandboxOperationBackendErrorCode",
]

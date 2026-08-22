"""Secret-free administration DTOs for the managed coding sandbox."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import SandboxBackendConfig, SandboxSecretRef, SandboxServiceConfig
from ..models import SandboxLimits, SandboxNetworkPolicy

SandboxConnectionErrorCode = Literal[
    "not_configured",
    "credential_unavailable",
    "authentication_failed",
    "permission_denied",
    "provider_unavailable",
    "rate_limited",
    "request_timeout",
    "template_invalid",
    "command_failed",
    "cleanup_failed",
    "unknown_error",
]


class SandboxAdminConfig(BaseModel):
    """Persisted user configuration; ``credential_id`` is not a secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: Literal["e2b"] = "e2b"
    runtime_id: str = Field(default="base", min_length=1, max_length=160)
    credential_id: str | None = Field(default=None, min_length=1, max_length=256)
    workdir: str = "/workspace"
    network: SandboxNetworkPolicy = Field(default_factory=SandboxNetworkPolicy)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    max_concurrent_per_user: int = Field(default=1, ge=1, le=16)
    artifact_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)

    @field_validator(
        "runtime_id",
        "credential_id",
    )
    @classmethod
    def _validate_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("sandbox identifiers must be trimmed and contain no controls")
        return value

    @model_validator(mode="after")
    def _validate_enabled(self) -> SandboxAdminConfig:
        if self.enabled and self.credential_id is None:
            raise ValueError("enabled E2B sandbox requires credential_id")
        # Reuse the provider-neutral model as the canonical workdir validator.
        SandboxServiceConfig(workdir=self.workdir)
        return self

    def credential_references(self) -> tuple[tuple[str, str], ...]:
        """Return semantic secret names paired with non-secret Credential IDs."""
        if self.credential_id is None:
            return ()
        return (("api_key", self.credential_id),)

    def to_service_config(self) -> SandboxServiceConfig:
        backend = None
        credential_references = self.credential_references()
        if credential_references:
            backend = SandboxBackendConfig(
                provider=self.provider,
                runtime_id=self.runtime_id,
                secret_refs=tuple(
                    SandboxSecretRef(
                        name=name,
                        secret_ref=credential_id,
                    )
                    for name, credential_id in credential_references
                ),
            )
        return SandboxServiceConfig(
            enabled=self.enabled,
            backend=backend,
            workdir=self.workdir,
            network=self.network,
            limits=self.limits,
            max_concurrent_per_user=self.max_concurrent_per_user,
            artifact_ttl_hours=self.artifact_ttl_hours,
        )


class SandboxConfigRecord(BaseModel):
    """Revisioned singleton configuration returned by Store and API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: SandboxAdminConfig
    revision: int = Field(ge=0)
    updated_at_ms: int | None = Field(default=None, ge=0)


class SandboxConnectionTestResult(BaseModel):
    """Safe connection-test evidence; contains no provider response body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    provider: Literal["e2b"] = "e2b"
    runtime_id: str
    duration_ms: int = Field(ge=0)
    checked_at_ms: int = Field(ge=0)
    python_available: bool = False
    error_code: SandboxConnectionErrorCode | None = None

    @model_validator(mode="after")
    def _validate_result(self) -> SandboxConnectionTestResult:
        if self.ok == (self.error_code is not None):
            raise ValueError("successful test has no error; failed test requires error")
        if self.python_available and not self.ok:
            raise ValueError("python_available requires successful connection test")
        return self


__all__ = [
    "SandboxAdminConfig",
    "SandboxConfigRecord",
    "SandboxConnectionErrorCode",
    "SandboxConnectionTestResult",
]

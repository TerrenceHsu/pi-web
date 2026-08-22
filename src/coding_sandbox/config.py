"""Persistent, secret-reference-only configuration for coding sandboxes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import SandboxBackendName, SandboxLimits, SandboxNetworkPolicy

_REQUIRED_SECRET_NAMES: dict[SandboxBackendName, frozenset[str]] = {
    "fake": frozenset(),
    "e2b": frozenset({"api_key"}),
    "modal": frozenset({"token_id", "token_secret"}),
    "local_docker": frozenset(),
}

_KNOWN_SECRET_VALUE_PREFIXES = ("e2b_", "ak-", "as-")


class SandboxSecretRef(BaseModel):
    """One semantic provider credential reference; never the secret value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    secret_ref: str = Field(min_length=1, max_length=255)

    @field_validator("secret_ref")
    @classmethod
    def _validate_secret_ref(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("secret reference must be trimmed and contain no controls")
        if value.lower().startswith(_KNOWN_SECRET_VALUE_PREFIXES):
            raise ValueError("secret reference looks like a credential value")
        return value


class SandboxBackendConfig(BaseModel):
    """Backend selection without secret values.

    ``secret_refs`` maps stable semantic names to a SecretStore reference.  For
    example, E2B uses ``SandboxSecretRef(name="api_key",
    secret_ref="sandbox-e2b-production")``; the API key
    itself is resolved only for the provider call and never enters this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: SandboxBackendName
    runtime_id: str = Field(min_length=1, max_length=160)
    secret_refs: tuple[SandboxSecretRef, ...] = ()

    @field_validator("runtime_id")
    @classmethod
    def _validate_runtime_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("runtime_id must be trimmed and contain no control characters")
        return value

    @model_validator(mode="after")
    def _validate_provider_secret_refs(self) -> SandboxBackendConfig:
        expected = _REQUIRED_SECRET_NAMES[self.provider]
        names = tuple(item.name for item in self.secret_refs)
        actual = frozenset(names)
        if len(names) != len(actual):
            raise ValueError("provider secret reference names must be unique")
        if actual != expected:
            raise ValueError(
                f"provider {self.provider!r} requires secret refs {sorted(expected)!r}"
            )
        return self

    def get_secret_ref(self, name: str) -> str | None:
        """Return one non-secret reference without exposing a mutable mapping."""
        return next(
            (item.secret_ref for item in self.secret_refs if item.name == name),
            None,
        )


class SandboxServiceConfig(BaseModel):
    """Account/workspace-level feature configuration.

    Disabled is the safe default.  ``backend`` may be preconfigured while the
    feature remains disabled, but enabling requires an explicit backend.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    backend: SandboxBackendConfig | None = None
    workdir: str = "/workspace"
    network: SandboxNetworkPolicy = Field(default_factory=SandboxNetworkPolicy)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    max_concurrent_per_user: int = Field(default=1, ge=1, le=16)
    artifact_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)

    @field_validator("workdir")
    @classmethod
    def _validate_workdir(cls, value: str) -> str:
        # Reuse the public create DTO so configuration and runtime cannot drift.
        from .models import SandboxCreateSpec

        probe = SandboxCreateSpec(
            operation_id="config-probe",
            runtime_id="config-probe",
            workdir=value,
        )
        return probe.workdir

    @model_validator(mode="after")
    def _validate_enabled_backend(self) -> SandboxServiceConfig:
        if self.enabled and self.backend is None:
            raise ValueError("enabled sandbox service requires a backend")
        return self


__all__ = ["SandboxBackendConfig", "SandboxSecretRef", "SandboxServiceConfig"]

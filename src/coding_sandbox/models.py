"""Immutable, provider-neutral DTOs for coding sandbox backends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SandboxBackendName = Literal["fake", "e2b", "modal", "local_docker"]
SandboxNetworkMode = Literal["none", "allowlist"]
SandboxLifecycleState = Literal[
    "creating",
    "ready",
    "busy",
    "paused",
    "terminated",
    "failed",
]
SandboxOutputStream = Literal["stdout", "stderr"]
SandboxTerminationReason = Literal[
    "exited",
    "timed_out",
    "cancelled",
    "sandbox_lost",
]


def validate_sandbox_path(value: str) -> str:
    """Validate an absolute Linux path without normalizing unsafe input."""
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("sandbox path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ValueError("sandbox path must be absolute")
    if ".." in path.parts:
        raise ValueError("sandbox path must not contain '..'")
    normalized = str(path)
    if normalized != value:
        raise ValueError("sandbox path must already be normalized")
    return value


class SandboxNetworkPolicy(BaseModel):
    """Outbound network policy applied when the sandbox is created."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: SandboxNetworkMode = "none"
    allowed_domains: tuple[str, ...] = ()
    allowed_cidrs: tuple[str, ...] = ()

    @field_validator("allowed_domains", "allowed_cidrs")
    @classmethod
    def _validate_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value or len(value) > 255 for value in cleaned):
            raise ValueError("network allowlist entries must be 1-255 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("network allowlist entries must be unique")
        return cleaned

    @model_validator(mode="after")
    def _validate_mode(self) -> SandboxNetworkPolicy:
        has_entries = bool(self.allowed_domains or self.allowed_cidrs)
        if self.mode == "none" and has_entries:
            raise ValueError("network mode 'none' cannot include allowlist entries")
        if self.mode == "allowlist" and not has_entries:
            raise ValueError("network mode 'allowlist' requires at least one entry")
        return self


class SandboxLimits(BaseModel):
    """Provider-independent resource and transfer ceilings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu: float = Field(default=2.0, ge=0.25, le=32.0)
    memory_mb: int = Field(default=4096, ge=128, le=131_072)
    lifetime_seconds: int = Field(default=1800, ge=30, le=86_400)
    command_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    max_upload_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_file_count: int = Field(default=20_000, ge=1, le=1_000_000)
    max_output_bytes: int = Field(default=5 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def _validate_timeouts(self) -> SandboxLimits:
        if self.command_timeout_seconds > self.lifetime_seconds:
            raise ValueError("command timeout cannot exceed sandbox lifetime")
        return self


class SandboxCreateSpec(BaseModel):
    """Complete secret-free input used to create one disposable sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    runtime_id: str = Field(min_length=1, max_length=160)
    workdir: str = "/workspace"
    network: SandboxNetworkPolicy = Field(default_factory=SandboxNetworkPolicy)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    @field_validator("runtime_id")
    @classmethod
    def _validate_runtime_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("runtime_id must be trimmed and contain no control characters")
        return value

    @field_validator("workdir")
    @classmethod
    def _validate_workdir(cls, value: str) -> str:
        return validate_sandbox_path(value)


class SandboxHandle(BaseModel):
    """Persistable reference to a remote sandbox; deliberately credential-free."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: SandboxBackendName
    sandbox_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=160)
    created_at_ms: int = Field(ge=0)
    expires_at_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_expiry(self) -> SandboxHandle:
        if self.expires_at_ms is not None and self.expires_at_ms <= self.created_at_ms:
            raise ValueError("expires_at_ms must be later than created_at_ms")
        return self


class SandboxStatus(BaseModel):
    """Observed provider state for recovery and lifecycle reconciliation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: SandboxHandle
    state: SandboxLifecycleState
    observed_at_ms: int = Field(ge=0)


class SandboxCommand(BaseModel):
    """One argv-based command. Shell source strings are intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    argv: tuple[str, ...]
    cwd: str = "/workspace"
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    max_output_bytes: int = Field(default=5 * 1024 * 1024, ge=1)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or not value[0]:
            raise ValueError("argv must contain a non-empty executable")
        if any("\x00" in part for part in value):
            raise ValueError("argv must not contain NUL bytes")
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return validate_sandbox_path(value)


class SandboxOutputChunk(BaseModel):
    """Bounded command output update emitted in provider order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str
    sequence: int = Field(ge=0)
    stream: SandboxOutputStream
    text: str


SandboxOutputCallback = Callable[[SandboxOutputChunk], Awaitable[None]]


class SandboxCommandResult(BaseModel):
    """Terminal command result with explicit non-exit termination semantics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str
    termination_reason: SandboxTerminationReason = "exited"
    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)
    output_truncated: bool = False

    @model_validator(mode="after")
    def _validate_terminal_state(self) -> SandboxCommandResult:
        if self.finished_at_ms < self.started_at_ms:
            raise ValueError("finished_at_ms must not precede started_at_ms")
        if self.termination_reason == "exited" and self.exit_code is None:
            raise ValueError("an exited command requires exit_code")
        if self.termination_reason != "exited" and self.exit_code is not None:
            raise ValueError("a non-exit termination must not include exit_code")
        return self

    @property
    def succeeded(self) -> bool:
        return self.termination_reason == "exited" and self.exit_code == 0


class SandboxTransferReceipt(BaseModel):
    """Evidence for a completed upload or download."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("remote_path")
    @classmethod
    def _validate_remote_path(cls, value: str) -> str:
        return validate_sandbox_path(value)


__all__ = [
    "SandboxBackendName",
    "SandboxCommand",
    "SandboxCommandResult",
    "SandboxCreateSpec",
    "SandboxHandle",
    "SandboxLimits",
    "SandboxLifecycleState",
    "SandboxNetworkMode",
    "SandboxNetworkPolicy",
    "SandboxOutputCallback",
    "SandboxOutputChunk",
    "SandboxOutputStream",
    "SandboxTerminationReason",
    "SandboxTransferReceipt",
    "SandboxStatus",
    "validate_sandbox_path",
]

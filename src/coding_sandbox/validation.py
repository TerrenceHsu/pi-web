"""Restricted validation plans and immutable server-generated evidence."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tomllib
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import SandboxTerminationReason
from .snapshot import (
    ARCHIVE_WORKSPACE_PREFIX,
    ProjectSnapshot,
    SnapshotError,
    SnapshotPolicy,
    validate_snapshot_archive,
)
from .workspace_models import SandboxWorkspaceError, validate_workspace_relative_path

VALIDATION_CONFIG_PATH: Literal[".pi-agent/sandbox.toml"] = ".pi-agent/sandbox.toml"
MAX_VALIDATION_CONFIG_BYTES = 64 * 1024
MAX_VALIDATION_CHECKS = 32
MAX_VALIDATION_ARGV_PARTS = 128
MAX_VALIDATION_ARG_BYTES = 8192
VALIDATION_CAPTURE_BYTES = 256 * 1024

ValidationConfigErrorCode = Literal[
    "missing_config",
    "snapshot_invalid",
    "invalid_encoding",
    "invalid_toml",
    "unsupported_version",
    "invalid_schema",
    "resource_limit",
]

_CONFIG_ERROR_MESSAGES: dict[ValidationConfigErrorCode, str] = {
    "missing_config": "Project snapshot has no sandbox validation config.",
    "snapshot_invalid": "Project snapshot cannot provide trusted validation config.",
    "invalid_encoding": "Sandbox validation config must be UTF-8 text.",
    "invalid_toml": "Sandbox validation config is not valid TOML.",
    "unsupported_version": "Sandbox validation config version is unsupported.",
    "invalid_schema": "Sandbox validation config has an invalid schema.",
    "resource_limit": "Sandbox validation config exceeds a fixed limit.",
}


class SandboxValidationConfigError(Exception):
    """Safe parser failure without echoing project-controlled config text."""

    def __init__(self, code: ValidationConfigErrorCode) -> None:
        super().__init__(_CONFIG_ERROR_MESSAGES[code])
        self.code = code

    def __repr__(self) -> str:
        return f"SandboxValidationConfigError(code={self.code!r})"


class SandboxValidationCheck(BaseModel):
    """One required argv check from the immutable server-side plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = Field(default=600, ge=1, le=3600)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > MAX_VALIDATION_ARGV_PARTS:
            raise ValueError("validation argv count is outside the fixed limit")
        if any(
            not part
            or len(part.encode("utf-8")) > MAX_VALIDATION_ARG_BYTES
            or "\x00" in part
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in value
        ):
            raise ValueError("validation argv contains an unsafe value")
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        return validate_workspace_relative_path(value, allow_root=True)


class SandboxValidationPlan(BaseModel):
    """Canonical plan pinned to the exact source config bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    source_path: Literal[".pi-agent/sandbox.toml"] = VALIDATION_CONFIG_PATH
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_checks: tuple[SandboxValidationCheck, ...]
    capture_bytes: int = Field(
        default=VALIDATION_CAPTURE_BYTES,
        ge=1,
        le=VALIDATION_CAPTURE_BYTES,
    )

    @field_validator("required_checks")
    @classmethod
    def _validate_checks(
        cls,
        value: tuple[SandboxValidationCheck, ...],
    ) -> tuple[SandboxValidationCheck, ...]:
        if not value or len(value) > MAX_VALIDATION_CHECKS:
            raise ValueError("required check count is outside the fixed limit")
        names = tuple(check.check_id for check in value)
        if len(set(names)) != len(names):
            raise ValueError("required check ids must be unique")
        return value


SandboxValidationCheckStatus = Literal[
    "passed",
    "failed",
    "timed_out",
    "cancelled",
    "sandbox_lost",
    "execution_error",
    "not_run",
]
SandboxValidationFailureCode = Literal[
    "configuration_changed",
    "check_failed",
    "workspace_changed",
    "cancelled",
    "sandbox_lost",
    "execution_error",
]


class SandboxValidationCheckEvidence(BaseModel):
    """Bounded terminal evidence for one server-selected required check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int = Field(ge=1)
    status: SandboxValidationCheckStatus
    exit_code: int | None = None
    termination_reason: SandboxTerminationReason | None = None
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    captured_stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_terminal_evidence(self) -> SandboxValidationCheckEvidence:
        if (
            self.finished_at_ms < self.started_at_ms
            or self.duration_ms != self.finished_at_ms - self.started_at_ms
            or self.captured_stdout_sha256 != digest_captured_text(self.stdout)
            or self.captured_stderr_sha256 != digest_captured_text(self.stderr)
        ):
            raise ValueError("validation check evidence is inconsistent")
        if self.status == "passed" and not (
            self.termination_reason == "exited" and self.exit_code == 0
        ):
            raise ValueError("passed validation evidence requires exit code zero")
        if self.status == "failed" and not (
            self.termination_reason == "exited"
            and self.exit_code is not None
            and self.exit_code != 0
        ):
            raise ValueError("failed validation evidence requires a nonzero exit")
        if self.status == "not_run" and (
            self.termination_reason is not None
            or self.exit_code is not None
            or self.stdout
            or self.stderr
        ):
            raise ValueError("a check that was not run cannot contain output")
        return self


class SandboxValidationEvidence(BaseModel):
    """Operation-owned evidence; callers cannot install or replace it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^validation-[0-9a-f]{32}$")
    operation_id: str
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_revision: int = Field(ge=0)
    workspace_sha256_before: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    workspace_sha256_after: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    checks: tuple[SandboxValidationCheckEvidence, ...]
    passed: bool
    failure_code: SandboxValidationFailureCode | None = None
    started_at_ms: int = Field(ge=0)
    finished_at_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_overall_evidence(self) -> SandboxValidationEvidence:
        if (
            self.finished_at_ms < self.started_at_ms
            or self.duration_ms != self.finished_at_ms - self.started_at_ms
            or self.passed != (self.failure_code is None)
            or not self.checks
        ):
            raise ValueError("validation evidence is inconsistent")
        if self.passed and (
            self.workspace_sha256_before is None
            or self.workspace_sha256_before != self.workspace_sha256_after
            or any(check.status != "passed" for check in self.checks)
        ):
            raise ValueError("passed validation requires stable successful checks")
        return self


class SandboxWorkspaceFingerprint(BaseModel):
    """Bounded manifest digest returned by the fixed remote helper."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def parse_sandbox_validation_config(
    content: bytes,
    *,
    source_path: str = VALIDATION_CONFIG_PATH,
) -> SandboxValidationPlan:
    """Parse the only accepted TOML shape into a canonical immutable plan."""
    if source_path != VALIDATION_CONFIG_PATH:
        raise SandboxValidationConfigError("invalid_schema")
    if not content or len(content) > MAX_VALIDATION_CONFIG_BYTES:
        raise SandboxValidationConfigError("resource_limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SandboxValidationConfigError("invalid_encoding") from exc
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SandboxValidationConfigError("invalid_toml") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "required_checks"}:
        raise SandboxValidationConfigError("invalid_schema")
    version = payload.get("version")
    if version != 1 or isinstance(version, bool):
        raise SandboxValidationConfigError("unsupported_version")
    raw_checks = payload.get("required_checks")
    if not isinstance(raw_checks, list):
        raise SandboxValidationConfigError("invalid_schema")
    if not raw_checks or len(raw_checks) > MAX_VALIDATION_CHECKS:
        raise SandboxValidationConfigError("resource_limit")
    checks: list[SandboxValidationCheck] = []
    try:
        for raw_check in raw_checks:
            checks.append(_parse_check(raw_check))
        canonical = {
            "version": 1,
            "required_checks": [check.model_dump(mode="json") for check in checks],
        }
        serialized = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return SandboxValidationPlan(
            source_sha256=hashlib.sha256(content).hexdigest(),
            plan_sha256=hashlib.sha256(serialized).hexdigest(),
            required_checks=tuple(checks),
        )
    except (SandboxWorkspaceError, ValidationError, ValueError) as exc:
        raise SandboxValidationConfigError("invalid_schema") from exc


def load_sandbox_validation_plan(
    snapshot: ProjectSnapshot,
    *,
    policy: SnapshotPolicy | None = None,
) -> SandboxValidationPlan:
    """Revalidate a frozen snapshot and parse its exact config member."""
    try:
        archive_size = snapshot.archive_path.stat().st_size
        if archive_size != snapshot.archive_size:
            raise SandboxValidationConfigError("snapshot_invalid")
        digest = hashlib.sha256()
        observed_size = 0
        with snapshot.archive_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                observed_size += len(chunk)
                if observed_size > snapshot.archive_size:
                    raise SandboxValidationConfigError("snapshot_invalid")
                digest.update(chunk)
        if observed_size != snapshot.archive_size or digest.hexdigest() != snapshot.archive_sha256:
            raise SandboxValidationConfigError("snapshot_invalid")
        manifest = validate_snapshot_archive(snapshot.archive_path, policy=policy)
        if manifest.manifest_sha256 != snapshot.manifest.manifest_sha256:
            raise SandboxValidationConfigError("snapshot_invalid")
        expected_entry = next(
            (entry for entry in manifest.entries if entry.path == VALIDATION_CONFIG_PATH),
            None,
        )
        if expected_entry is None:
            raise SandboxValidationConfigError("missing_config")
        if expected_entry.size > MAX_VALIDATION_CONFIG_BYTES:
            raise SandboxValidationConfigError("resource_limit")
        member_name = ARCHIVE_WORKSPACE_PREFIX + VALIDATION_CONFIG_PATH
        with tarfile.open(snapshot.archive_path, mode="r:gz") as archive:
            member = archive.getmember(member_name)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SandboxValidationConfigError("snapshot_invalid")
            content = extracted.read(MAX_VALIDATION_CONFIG_BYTES + 1)
        if (
            len(content) != expected_entry.size
            or hashlib.sha256(content).hexdigest() != expected_entry.sha256
        ):
            raise SandboxValidationConfigError("snapshot_invalid")
        return parse_sandbox_validation_config(content)
    except SandboxValidationConfigError:
        raise
    except (KeyError, OSError, SnapshotError, tarfile.TarError) as exc:
        raise SandboxValidationConfigError("snapshot_invalid") from exc


def _parse_check(value: object) -> SandboxValidationCheck:
    if not isinstance(value, Mapping):
        raise ValueError("required check must be a table")
    allowed = {"id", "argv", "cwd", "timeout_seconds"}
    if not set(value).issubset(allowed) or not {"id", "argv"}.issubset(value):
        raise ValueError("required check fields are invalid")
    payload: dict[str, Any] = dict(value)
    payload["check_id"] = payload.pop("id")
    return SandboxValidationCheck.model_validate(payload)


def digest_captured_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_VALIDATION_CONFIG_BYTES",
    "SandboxValidationCheck",
    "SandboxValidationCheckEvidence",
    "SandboxValidationCheckStatus",
    "SandboxValidationConfigError",
    "SandboxValidationEvidence",
    "SandboxValidationFailureCode",
    "SandboxValidationPlan",
    "SandboxWorkspaceFingerprint",
    "VALIDATION_CAPTURE_BYTES",
    "VALIDATION_CONFIG_PATH",
    "ValidationConfigErrorCode",
    "digest_captured_text",
    "load_sandbox_validation_plan",
    "parse_sandbox_validation_config",
]

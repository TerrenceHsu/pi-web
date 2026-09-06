"""High-level, provider-neutral coding workspace contracts and DTOs."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import SandboxCommandResult, SandboxOutputCallback

if TYPE_CHECKING:
    from .artifact import SandboxOutputArtifact
    from .validation import SandboxValidationEvidence

WorkspaceErrorCode = Literal[
    "execution_approval_required",
    "bash_unavailable",
    "no_active_operation",
    "operation_closed",
    "unsafe_path",
    "not_found",
    "not_file",
    "file_too_large",
    "invalid_encoding",
    "resource_limit",
    "command_failed",
    "protocol_error",
    "patch_invalid",
    "patch_conflict",
    "validation_not_configured",
    "validation_config_invalid",
    "validation_stale",
    "operation_frozen",
    "artifact_signing_unavailable",
    "artifact_baseline_unavailable",
    "artifact_export_failed",
    "artifact_invalid",
    "artifact_stale",
]

_ERROR_MESSAGES: dict[WorkspaceErrorCode, str] = {
    "execution_approval_required": "An active execution grant is required.",
    "bash_unavailable": "Bash requires the approved local Docker runtime.",
    "no_active_operation": "No coding sandbox operation is active.",
    "operation_closed": "The coding sandbox operation is closed.",
    "unsafe_path": "The requested workspace path is unsafe.",
    "not_found": "The requested workspace path does not exist.",
    "not_file": "The requested workspace path is not a regular file.",
    "file_too_large": "The requested file exceeds the operation limit.",
    "invalid_encoding": "The requested file is not valid UTF-8 text.",
    "resource_limit": "The coding workspace resource limit was exceeded.",
    "command_failed": "The coding workspace command failed.",
    "protocol_error": "The coding workspace returned an invalid response.",
    "patch_invalid": "The submitted patch is invalid.",
    "patch_conflict": "The submitted patch does not match the workspace.",
    "validation_not_configured": "No fixed validation plan is configured.",
    "validation_config_invalid": "The fixed validation plan is invalid.",
    "validation_stale": "Required validation is missing, failed, or stale.",
    "operation_frozen": "The coding Sandbox is frozen and cannot be modified.",
    "artifact_signing_unavailable": "Artifact signing is not configured.",
    "artifact_baseline_unavailable": "The artifact baseline cannot be verified.",
    "artifact_export_failed": "The immutable Sandbox artifact could not be exported.",
    "artifact_invalid": "The downloaded Sandbox artifact is invalid.",
    "artifact_stale": "The Sandbox changed while its artifact was being frozen.",
}


class SandboxWorkspaceError(Exception):
    """Fixed, secret-free failure raised by high-level workspace operations."""

    def __init__(
        self,
        code: WorkspaceErrorCode,
        *,
        relative_path: str | None = None,
    ) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.relative_path = relative_path

    def __repr__(self) -> str:
        return f"SandboxWorkspaceError(code={self.code!r}, relative_path={self.relative_path!r})"


def validate_workspace_relative_path(value: str, *, allow_root: bool = False) -> str:
    """Reject absolute, normalized, parent, control and Windows-style paths."""
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or "\x00" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SandboxWorkspaceError("unsafe_path")
    if allow_root and value == ".":
        return value
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value == "."
        or ".." in path.parts
        or str(path) != value
        or not path.parts
    ):
        raise SandboxWorkspaceError("unsafe_path")
    return value


class SandboxFileEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxFileList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str
    files: tuple[SandboxFileEntry, ...]
    truncated: bool = False

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return validate_workspace_relative_path(value, allow_root=True)


class SandboxFileRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    content: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxSearchMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    text: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    root: str
    matches: tuple[SandboxSearchMatch, ...]
    truncated: bool = False

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return validate_workspace_relative_path(value, allow_root=True)


class SandboxWriteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created: bool

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxDeleteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


SandboxDiffStatus = Literal["added", "modified", "deleted"]


class SandboxDiffEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    status: SandboxDiffStatus
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_workspace_relative_path(value)


class SandboxDiffResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[SandboxDiffEntry, ...]
    patch: str = ""
    patch_truncated: bool = False


class CodingWorkspace(Protocol):
    """One already-created, request-owned coding sandbox workspace."""

    @property
    def operation_id(self) -> str: ...

    @property
    def workspace_revision(self) -> int: ...

    @property
    def last_validation_evidence(self) -> SandboxValidationEvidence | None: ...

    @property
    def frozen(self) -> bool: ...

    @property
    def output_artifact(self) -> SandboxOutputArtifact | None: ...

    async def list_files(
        self,
        path: str = ".",
        *,
        max_files: int = 1000,
    ) -> SandboxFileList: ...

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = 64 * 1024,
    ) -> SandboxFileRead: ...

    async def search(
        self,
        query: str,
        *,
        path: str = ".",
        case_sensitive: bool = True,
        max_matches: int = 200,
    ) -> SandboxSearchResult: ...

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = True,
    ) -> SandboxWriteResult: ...

    async def apply_patch(self, patch: str) -> SandboxDiffResult: ...

    async def delete_file(self, path: str) -> SandboxDeleteResult: ...

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        on_output: SandboxOutputCallback | None = None,
        signal: asyncio.Event | None = None,
    ) -> SandboxCommandResult: ...

    async def diff(self, *, max_patch_bytes: int = 256 * 1024) -> SandboxDiffResult: ...

    async def validate_required_checks(
        self,
        *,
        on_output: SandboxOutputCallback | None = None,
        signal: asyncio.Event | None = None,
    ) -> SandboxValidationEvidence: ...

    async def require_current_validation(self) -> SandboxValidationEvidence: ...

    async def freeze_output_artifact(self) -> SandboxOutputArtifact: ...


__all__ = [
    "CodingWorkspace",
    "SandboxDeleteResult",
    "SandboxDiffEntry",
    "SandboxDiffResult",
    "SandboxDiffStatus",
    "SandboxFileEntry",
    "SandboxFileList",
    "SandboxFileRead",
    "SandboxSearchMatch",
    "SandboxSearchResult",
    "SandboxWorkspaceError",
    "SandboxWriteResult",
    "WorkspaceErrorCode",
    "validate_workspace_relative_path",
]

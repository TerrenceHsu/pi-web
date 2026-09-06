"""Strict Bash input and optional local backend contract, with no host fallback."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import SandboxCommand, SandboxCommandResult, SandboxHandle, SandboxOutputCallback
from .workspace_models import SandboxWorkspaceError, validate_workspace_relative_path

CONTROL_SCRIPT = "/run/pi-command/command.sh"
BASH_ARGV = ("/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", CONTROL_SCRIPT)


def script_bytes(value: str) -> bytes:
    try:
        data = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError("script must be valid UTF-8") from None
    if not data or len(data) > 16 * 1024 or b"\0" in data:
        raise ValueError("script must be 1-16384 UTF-8 bytes without NUL")
    return data


class BashRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    script: str = Field(min_length=1, max_length=16 * 1024)
    cwd: str = "."
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    reason: str = Field(default="", max_length=512)

    @field_validator("cwd")
    @classmethod
    def check_cwd(cls, value: str) -> str:
        try:
            value.encode("utf-8", "strict")
            if ":" in value:
                raise ValueError("cwd must be a Workspace-relative directory")
            return validate_workspace_relative_path(value, allow_root=True)
        except (UnicodeError, SandboxWorkspaceError):
            raise ValueError("cwd must be a Workspace-relative directory") from None

    @model_validator(mode="after")
    def check_script(self) -> Self:
        script_bytes(self.script)
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(script_bytes(self.script)).hexdigest()


@runtime_checkable
class BashBackend(Protocol):
    async def execute_bash(
        self,
        handle: SandboxHandle,
        command: SandboxCommand,
        *,
        script: str,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult: ...


@runtime_checkable
class BashWorkspace(Protocol):
    async def run_bash(
        self,
        request: BashRequest,
        *,
        signal: asyncio.Event | None = None,
        on_output: SandboxOutputCallback | None = None,
    ) -> SandboxCommandResult: ...

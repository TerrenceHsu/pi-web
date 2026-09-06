"""Immutable, secret-free task scope and command authorization contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=160)]
Capability = Literal["edit", "execute", "validate"]
GrantState = Literal[
    "pending", "approved", "active", "closed", "denied", "revoked", "expired", "interrupted"
]
CommandState = Literal["preparing", "running", "succeeded", "failed", "interrupted"]


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def relative_cwd(value: str) -> str:
    if value == ".":
        return value
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or any(char in value for char in ("\\", "\0", ":"))
    ):
        raise ValueError("cwd must be a normalized relative Workspace directory")
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionIdentity(FrozenModel):
    account_id: Identifier
    session_id: Identifier
    request_id: Identifier
    workspace_id: Identifier
    task_id: Identifier
    operation_id: Identifier


class ExecutionScope(FrozenModel):
    """Constructed by the server, never deserialized directly from tool arguments."""

    identity: ExecutionIdentity
    kind: Literal["coding", "plan", "bash"]
    backend: Literal["local_docker", "e2b"]
    runtime_id: Identifier
    config_revision: int = Field(ge=0)
    policy_sha256: Digest
    baseline_revision: int = Field(ge=0)
    baseline_sha256: Digest
    input_sha256: Digest
    publish_policy_sha256: Digest
    capabilities: tuple[Capability, ...]
    request_sha256: Digest
    plan_id: Identifier | None = None
    plan_version: int | None = Field(default=None, ge=1)
    plan_sha256: Digest | None = None
    script_sha256: Digest | None = None
    cwd: str | None = None
    max_commands: int = Field(default=50, ge=1, le=50)
    max_execution_ms: int = Field(default=600_000, ge=1000, le=600_000)
    lifetime_ms: int = Field(default=1_800_000, ge=1000, le=1_800_000)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if not self.capabilities or len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be nonempty and unique")
        plan = (self.plan_id, self.plan_version, self.plan_sha256)
        if self.kind == "plan":
            if any(value is None for value in plan):
                raise ValueError("a plan grant binds an exact plan version")
        elif any(value is not None for value in plan):
            raise ValueError("non-plan scope cannot contain plan authority")
        if self.kind == "bash":
            if (
                self.backend != "local_docker"
                or self.script_sha256 is None
                or self.cwd is None
                or self.max_commands != 1
                or self.lifetime_ms > 600_000
                or self.capabilities != ("execute",)
            ):
                raise ValueError("standalone Bash requires a local exact single-command scope")
            relative_cwd(self.cwd)
        elif self.script_sha256 is not None or self.cwd is not None:
            raise ValueError("task scope must not masquerade as single-command approval")
        if self.backend == "local_docker":
            if (
                not self.runtime_id.startswith("sha256:")
                or len(self.runtime_id) != 71
                or any(c not in "0123456789abcdef" for c in self.runtime_id[7:])
            ):
                raise ValueError("local runtime must be an immutable image ID")
        return self

    @property
    def sha256(self) -> str:
        return digest_json(self.model_dump(mode="json"))


class ExecutionContext(FrozenModel):
    """Immutable server call context; Planner/Verifier are explicitly excluded."""

    identity: ExecutionIdentity
    scope_sha256: Digest
    role: Literal["executor", "planner", "verifier", "read_only"]


class CommandIntent(FrozenModel):
    command_id: Identifier
    capability: Capability
    kind: Literal["bash", "argv", "edit", "validation"]
    payload_sha256: Digest
    cwd: str = "."
    copy_revision: int = Field(ge=0)
    timeout_ms: int = Field(ge=1000, le=300_000)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return relative_cwd(value)

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        expected = {"bash": "execute", "argv": "execute", "edit": "edit", "validation": "validate"}
        if self.capability != expected[self.kind]:
            raise ValueError("command kind and capability disagree")
        return self

    @property
    def sha256(self) -> str:
        return digest_json(self.model_dump(mode="json"))


class ExecutionGrant(FrozenModel):
    scope: ExecutionScope
    state: GrantState
    created_at_ms: int
    approval_deadline_ms: int
    queue_deadline_ms: int | None = None
    expires_at_ms: int | None = None
    commands_used: int = 0
    charged_ms: int = 0
    copy_revision: int = 0
    cleanup_pending: bool = False


class ExecutionCommand(FrozenModel):
    task_id: str
    intent: CommandIntent
    state: CommandState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    exit_code: int | None = None


class ExecutionDenied(RuntimeError):
    """Stable codes only: no script, identity or runtime error is logged."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

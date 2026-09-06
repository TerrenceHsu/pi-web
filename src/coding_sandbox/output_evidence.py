"""Explicit Bash output integrity evidence, never a substitute for code validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BashArtifactScope(BaseModel):
    """Trusted product-owned scope installed at operation construction, not by a tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cwd: str
    baseline_revision: int = Field(ge=0)
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publish_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BashOutputIntegrityEvidence(BashArtifactScope):
    """Exit zero and frozen output hashes; no claim of functional validation."""

    schema_version: Literal["bash-output-integrity/v1"] = "bash-output-integrity/v1"
    operation_id: str
    command_id: str = Field(pattern=r"^bash-[0-9a-f]{32}$")
    workspace_revision: int = Field(ge=0)
    workspace_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: Literal[0] = 0
    termination_reason: Literal["exited"] = "exited"
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

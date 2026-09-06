"""Trusted publication purpose and exact review receipt, independent of execution grants."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump when path/purpose/strict-baseline semantics change.
WORKSPACE_PUBLISH_POLICY_SHA256 = hashlib.sha256(
    b"workspace-signed-output/v2:protected-paths:strict-baseline:explicit-review"
).hexdigest()


class PublicationBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    purpose: Literal["coding", "plan", "bash"]
    backend: Literal["local_docker", "e2b"]
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationApproval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(pattern=r"^artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

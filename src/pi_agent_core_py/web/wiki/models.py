"""Immutable DTOs for LLM Wiki schema v1 and filesystem recovery."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WIKI_SCHEMA_VERSION: Literal[1] = 1
WIKI_SPACE_MANIFEST_SCHEMA: Literal["llm-wiki-space/v1"] = "llm-wiki-space/v1"
WIKI_LEGACY_MANIFEST_SCHEMA: Literal["llm-wiki-legacy-backup/v1"] = (
    "llm-wiki-legacy-backup/v1"
)

WikiSpaceStatus = Literal["active", "archived", "deleting", "failed"]
WikiSourceStatus = Literal["uploaded", "parsing", "parsed", "failed", "deleting"]
WikiSourceMimeType = Literal["application/pdf", "text/html"]
WikiArtifactKind = Literal["parsed_markdown", "embedded_image", "manifest"]
WikiRelationType = Literal[
    "related_to",
    "references",
    "extends",
    "contradicts",
    "part_of",
]
WikiChangeSetStatus = Literal[
    "draft",
    "awaiting_approval",
    "approved",
    "rejected",
    "stale",
    "failed",
]
WikiConversationStatus = Literal["active", "archived"]
WikiJobKind = Literal[
    "parse",
    "synthesize_entry_page",
    "rebuild_search",
    "rebuild_graph_projection",
]
WikiJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

_SPACE_ID_RE = re.compile(r"^space_[0-9a-f]{24}$")


def validate_space_id(value: str) -> str:
    if _SPACE_ID_RE.fullmatch(value) is None:
        raise ValueError("space_id has an invalid format")
    return value


class WikiSpace(BaseModel):
    """Canonical Wiki Space row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    status: WikiSpaceStatus = "active"
    graph_revision: int = Field(default=0, ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("space name must be trimmed and contain no controls")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if "\x00" in value or "\r" in value:
            raise ValueError("space description contains invalid characters")
        return value

    @model_validator(mode="after")
    def _validate_timestamps(self) -> WikiSpace:
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms must not precede created_at_ms")
        return self


class WikiSpaceManifest(BaseModel):
    """Rebuildable, secret-free ``space.json`` mirror."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["llm-wiki-space/v1"] = Field(
        default=WIKI_SPACE_MANIFEST_SCHEMA,
        alias="schema",
    )
    space: WikiSpace


class WikiMirrorRepairReport(BaseModel):
    """Bounded startup mirror reconciliation evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repaired_space_ids: tuple[str, ...] = ()
    orphan_space_ids: tuple[str, ...] = ()
    quarantined_staging_count: int = Field(default=0, ge=0)


class WikiLegacyBackupReceipt(BaseModel):
    """Persistable evidence for one completed legacy retirement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_id: str = Field(pattern=r"^backup_[0-9a-f]{24}$")
    backup_relpath: str = Field(pattern=r"^legacy/knowledge-[0-9]+-backup_[0-9a-f]{24}$")
    created_at_ms: int = Field(ge=0)
    file_count: int = Field(ge=1)
    total_size_bytes: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retired_names: tuple[Literal["knowledge.db", "libraries"], ...]
    cleanup_complete: bool

    @field_validator("retired_names")
    @classmethod
    def _validate_retired_names(
        cls,
        values: tuple[Literal["knowledge.db", "libraries"], ...],
    ) -> tuple[Literal["knowledge.db", "libraries"], ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("retired_names must be non-empty and unique")
        return values


__all__ = [
    "WIKI_LEGACY_MANIFEST_SCHEMA",
    "WIKI_SCHEMA_VERSION",
    "WIKI_SPACE_MANIFEST_SCHEMA",
    "WikiArtifactKind",
    "WikiChangeSetStatus",
    "WikiConversationStatus",
    "WikiJobKind",
    "WikiJobStatus",
    "WikiLegacyBackupReceipt",
    "WikiMirrorRepairReport",
    "WikiRelationType",
    "WikiSourceMimeType",
    "WikiSourceStatus",
    "WikiSpace",
    "WikiSpaceManifest",
    "WikiSpaceStatus",
    "validate_space_id",
]

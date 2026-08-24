"""Immutable DTOs for LLM Wiki schema v2 and filesystem recovery."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WIKI_SCHEMA_VERSION: Literal[2] = 2
WIKI_SPACE_MANIFEST_SCHEMA: Literal["llm-wiki-space/v1"] = "llm-wiki-space/v1"
WIKI_LEGACY_MANIFEST_SCHEMA: Literal["llm-wiki-legacy-backup/v1"] = "llm-wiki-legacy-backup/v1"
WIKI_SELECTED_PARSE_SCHEMA: Literal["llm-wiki-selected-parse/v1"] = (
    "llm-wiki-selected-parse/v1"
)

WikiSpaceStatus = Literal["active", "archived", "deleting", "failed"]
WikiSourceStatus = Literal["uploaded", "parsing", "parsed", "failed", "deleting"]
WikiSourceMimeType = Literal["application/pdf", "text/html"]
WikiArtifactKind = Literal[
    "parsed_markdown",
    "page_markdown",
    "embedded_image",
    "table_image",
    "manifest",
]
WikiParseMode = Literal["builtin", "auto", "fast", "accurate"]
WikiParseAttemptState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "quality_rejected",
    "cancelled",
]
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
_SOURCE_ID_RE = re.compile(r"^source_[0-9a-f]{24}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_[0-9a-f]{24}$")
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{24}$")
_PARSE_ATTEMPT_ID_RE = re.compile(r"^parse_attempt_[0-9a-f]{24}$")
_PARSE_REVISION_ID_RE = re.compile(r"^parse_revision_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_METADATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-]{0,127}$")


def validate_space_id(value: str) -> str:
    if _SPACE_ID_RE.fullmatch(value) is None:
        raise ValueError("space_id has an invalid format")
    return value


def _validate_id(value: str, pattern: re.Pattern[str], label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def validate_source_id(value: str) -> str:
    return _validate_id(value, _SOURCE_ID_RE, "source_id")


def validate_artifact_id(value: str) -> str:
    return _validate_id(value, _ARTIFACT_ID_RE, "artifact_id")


def validate_job_id(value: str) -> str:
    return _validate_id(value, _JOB_ID_RE, "job_id")


def validate_parse_attempt_id(value: str) -> str:
    return _validate_id(value, _PARSE_ATTEMPT_ID_RE, "parse_attempt_id")


def validate_parse_revision_id(value: str) -> str:
    return _validate_id(value, _PARSE_REVISION_ID_RE, "parse_revision_id")


def _validate_sha256(value: str, label: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def _validate_safe_metadata(value: str, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return value
    if _SAFE_METADATA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def _validate_json_object(value: str, label: str) -> str:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_json_string_list(value: str, label: str) -> str:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or len(decoded) != len(set(decoded))
        or any(
            not isinstance(item, str) or _SAFE_METADATA_RE.fullmatch(item) is None
            for item in decoded
        )
    ):
        raise ValueError(f"{label} must be a non-empty unique string list")
    return value


def _validate_safe_basename(value: str) -> str:
    if (
        value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("display_name must be a safe basename")
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


class WikiSource(BaseModel):
    """Immutable source metadata; source bytes remain owned by ``raw/``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: WikiSourceMimeType
    size_bytes: int = Field(ge=1)
    source_sha256: str
    source_relpath: str = Field(min_length=1, max_length=240)
    selected_parse_revision_id: str | None = None
    selection_version: int = Field(default=0, ge=0)
    selected_at_ms: int | None = Field(default=None, ge=0)
    status: WikiSourceStatus = "uploaded"
    safe_error_code: str = Field(default="", max_length=80)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        return _validate_safe_basename(value)

    @field_validator("source_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "source_sha256")

    @field_validator("selected_parse_revision_id")
    @classmethod
    def _validate_selected_revision(cls, value: str | None) -> str | None:
        return None if value is None else validate_parse_revision_id(value)

    @field_validator("safe_error_code")
    @classmethod
    def _validate_safe_metadata(cls, value: str) -> str:
        return _validate_safe_metadata(value, "source error", allow_empty=True)

    @model_validator(mode="after")
    def _validate_source_state(self) -> WikiSource:
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("updated_at_ms must not precede created_at_ms")
        if self.status == "parsed" and self.selected_parse_revision_id is None:
            raise ValueError("parsed source requires a selected parse revision")
        if self.selected_parse_revision_id is None and (
            self.selection_version != 0 or self.selected_at_ms is not None
        ):
            raise ValueError("unselected source cannot have selection evidence")
        if self.selected_parse_revision_id is not None and self.selected_at_ms is None:
            raise ValueError("selected source requires a selection timestamp")
        if self.status == "failed" and not self.safe_error_code:
            raise ValueError("failed source requires a safe error code")
        if self.status != "failed" and self.safe_error_code:
            raise ValueError("only a failed source may expose a safe error code")
        return self


class WikiArtifact(BaseModel):
    """One content-addressed derived file imported below a source directory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source_id: str
    parse_revision_id: str
    kind: WikiArtifactKind
    relpath: str = Field(min_length=1, max_length=240)
    mime_type: str = Field(min_length=1, max_length=80)
    size_bytes: int = Field(ge=0)
    sha256: str
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_locator_json: str = "{}"
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_artifact_id(cls, value: str) -> str:
        return validate_artifact_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("parse_revision_id")
    @classmethod
    def _validate_parse_revision_id(cls, value: str) -> str:
        return validate_parse_revision_id(value)

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "artifact sha256")

    @field_validator("source_locator_json")
    @classmethod
    def _validate_source_locator(cls, value: str) -> str:
        return _validate_json_object(value, "source_locator_json")

    @model_validator(mode="after")
    def _validate_artifact_size(self) -> WikiArtifact:
        if self.kind != "page_markdown" and self.size_bytes == 0:
            raise ValueError("only page Markdown may be empty")
        return self


class WikiParseAttempt(BaseModel):
    """Immutable evidence for one concrete parser engine invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    provider_attempt_id: str
    source_id: str
    job_id: str
    ordinal: int = Field(ge=1)
    requested_mode: WikiParseMode
    source_sha256: str
    parser: str
    parser_version: str
    preset: str
    routing_config_revision: str = ""
    routing_config_sha256: str = ""
    route_reasons_json: str
    state: WikiParseAttemptState
    safe_error_code: str = ""
    quality_report_json: str = "{}"
    output_size_bytes: int | None = Field(default=None, ge=1)
    output_sha256: str | None = None
    fallback_from_attempt_id: str | None = None
    started_at_ms: int | None = Field(default=None, ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_parse_attempt_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @field_validator("fallback_from_attempt_id")
    @classmethod
    def _validate_fallback_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_parse_attempt_id(value)

    @field_validator("source_sha256")
    @classmethod
    def _validate_source_sha256(cls, value: str) -> str:
        return _validate_sha256(value, "attempt source sha256")

    @field_validator("output_sha256")
    @classmethod
    def _validate_output_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _validate_sha256(value, "attempt output sha256")

    @field_validator(
        "provider_attempt_id",
        "parser",
        "parser_version",
        "preset",
    )
    @classmethod
    def _validate_identity_metadata(cls, value: str) -> str:
        return _validate_safe_metadata(value, "attempt identity")

    @field_validator("routing_config_revision", "safe_error_code")
    @classmethod
    def _validate_optional_metadata(cls, value: str) -> str:
        return _validate_safe_metadata(value, "attempt metadata", allow_empty=True)

    @field_validator("routing_config_sha256")
    @classmethod
    def _validate_optional_config_sha(cls, value: str) -> str:
        return value if value == "" else _validate_sha256(value, "routing config sha256")

    @field_validator("route_reasons_json")
    @classmethod
    def _validate_route_reasons(cls, value: str) -> str:
        return _validate_json_string_list(value, "route_reasons_json")

    @field_validator("quality_report_json")
    @classmethod
    def _validate_quality(cls, value: str) -> str:
        return _validate_json_object(value, "quality_report_json")

    @model_validator(mode="after")
    def _validate_state(self) -> WikiParseAttempt:
        if bool(self.routing_config_revision) != bool(self.routing_config_sha256):
            raise ValueError("routing config identity must be complete")
        if (self.output_size_bytes is None) != (self.output_sha256 is None):
            raise ValueError("attempt output evidence must be complete")
        if self.started_at_ms is None and self.finished_at_ms is not None:
            raise ValueError("attempt finish requires a start timestamp")
        if (
            self.started_at_ms is not None
            and self.finished_at_ms is not None
            and self.finished_at_ms < self.started_at_ms
        ):
            raise ValueError("attempt finish precedes start")
        terminal = self.state in {"succeeded", "failed", "quality_rejected", "cancelled"}
        if terminal != (self.finished_at_ms is not None):
            raise ValueError("attempt terminal state and timestamp disagree")
        if self.state == "succeeded":
            if self.output_sha256 is None or self.safe_error_code:
                raise ValueError("succeeded attempt requires clean output evidence")
        elif self.state in {"failed", "quality_rejected", "cancelled"}:
            if self.output_sha256 is not None or not self.safe_error_code:
                raise ValueError("unsuccessful attempt requires a safe error and no output")
        elif self.safe_error_code or self.output_sha256 is not None:
            raise ValueError("active attempt cannot expose terminal evidence")
        return self


class WikiParseRevision(BaseModel):
    """One immutable, fully validated Raw parse bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    source_id: str
    job_id: str
    selected_attempt_id: str
    contract_version: int = Field(ge=1)
    artifact_schema: str
    requested_mode: WikiParseMode
    source_sha256: str
    parser: str
    parser_version: str
    preset: str
    routing_config_revision: str = ""
    routing_config_sha256: str = ""
    parsed_markdown_relpath: str = Field(min_length=1, max_length=240)
    parsed_markdown_sha256: str
    manifest_relpath: str = Field(min_length=1, max_length=240)
    manifest_sha256: str
    page_count: int = Field(ge=1)
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_parse_revision_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @field_validator("selected_attempt_id")
    @classmethod
    def _validate_attempt_id(cls, value: str) -> str:
        return validate_parse_attempt_id(value)

    @field_validator(
        "source_sha256",
        "parsed_markdown_sha256",
        "manifest_sha256",
    )
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "parse revision sha256")

    @field_validator("parser", "parser_version", "preset")
    @classmethod
    def _validate_metadata(cls, value: str) -> str:
        return _validate_safe_metadata(value, "parse revision metadata")

    @field_validator("artifact_schema")
    @classmethod
    def _validate_artifact_schema(cls, value: str) -> str:
        if (
            len(value) > 128
            or not value.startswith("llm-wiki-")
            or re.fullmatch(r"[A-Za-z0-9._+\-/]+", value) is None
        ):
            raise ValueError("artifact schema has an invalid format")
        return value

    @field_validator("routing_config_revision")
    @classmethod
    def _validate_optional_config(cls, value: str) -> str:
        return _validate_safe_metadata(value, "routing config revision", allow_empty=True)

    @field_validator("routing_config_sha256")
    @classmethod
    def _validate_optional_config_hash(cls, value: str) -> str:
        return value if value == "" else _validate_sha256(value, "routing config sha256")

    @model_validator(mode="after")
    def _validate_config_identity(self) -> WikiParseRevision:
        if bool(self.routing_config_revision) != bool(self.routing_config_sha256):
            raise ValueError("routing config identity must be complete")
        return self


class WikiSelectedParsePointer(BaseModel):
    """Rebuildable selected.json mirror of the canonical Source CAS pointer."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["llm-wiki-selected-parse/v1"] = Field(
        default=WIKI_SELECTED_PARSE_SCHEMA,
        alias="schema",
    )
    source_id: str
    source_sha256: str
    parse_revision_id: str
    selection_version: int = Field(ge=1)
    manifest_relpath: str = Field(min_length=1, max_length=240)
    manifest_sha256: str
    selected_at_ms: int = Field(ge=0)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("parse_revision_id")
    @classmethod
    def _validate_revision_id(cls, value: str) -> str:
        return validate_parse_revision_id(value)

    @field_validator("source_sha256", "manifest_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "selected parse sha256")


class WikiJob(BaseModel):
    """Durable orchestration state; provider handles are deliberately ephemeral."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    source_id: str | None = None
    kind: WikiJobKind
    status: WikiJobStatus = "queued"
    attempt: int = Field(default=1, ge=1)
    requested_mode: WikiParseMode = "builtin"
    base_selection_version: int = Field(default=0, ge=0)
    base_selected_parse_revision_id: str | None = None
    safe_error_code: str = Field(default="", max_length=80)
    created_at_ms: int = Field(ge=0)
    started_at_ms: int | None = Field(default=None, ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)

    @field_validator("id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_optional_source_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_source_id(value)

    @field_validator("base_selected_parse_revision_id")
    @classmethod
    def _validate_base_revision(cls, value: str | None) -> str | None:
        return None if value is None else validate_parse_revision_id(value)

    @model_validator(mode="after")
    def _validate_job_state(self) -> WikiJob:
        if self.kind in {"parse", "synthesize_entry_page"} and self.source_id is None:
            raise ValueError("source job requires source_id")
        if self.kind == "parse" and self.source_id is not None:
            if (self.base_selected_parse_revision_id is None) != (
                self.base_selection_version == 0
            ):
                raise ValueError("parse job base selection identity is inconsistent")
        if self.status == "running" and self.started_at_ms is None:
            raise ValueError("running job requires started_at_ms")
        if self.status in {"succeeded", "failed", "cancelled"}:
            if self.finished_at_ms is None:
                raise ValueError("terminal job requires finished_at_ms")
        elif self.finished_at_ms is not None:
            raise ValueError("non-terminal job cannot have finished_at_ms")
        if self.status in {"failed", "cancelled"} and not self.safe_error_code:
            raise ValueError("failed or cancelled job requires a safe error code")
        if self.status not in {"failed", "cancelled"} and self.safe_error_code:
            raise ValueError("successful job cannot expose a safe error code")
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
    "WIKI_SELECTED_PARSE_SCHEMA",
    "WIKI_SPACE_MANIFEST_SCHEMA",
    "WikiArtifactKind",
    "WikiArtifact",
    "WikiChangeSetStatus",
    "WikiConversationStatus",
    "WikiJobKind",
    "WikiJobStatus",
    "WikiJob",
    "WikiLegacyBackupReceipt",
    "WikiMirrorRepairReport",
    "WikiParseAttempt",
    "WikiParseAttemptState",
    "WikiParseMode",
    "WikiParseRevision",
    "WikiRelationType",
    "WikiSourceMimeType",
    "WikiSourceStatus",
    "WikiSelectedParsePointer",
    "WikiSource",
    "WikiSpace",
    "WikiSpaceManifest",
    "WikiSpaceStatus",
    "validate_space_id",
    "validate_artifact_id",
    "validate_job_id",
    "validate_parse_attempt_id",
    "validate_parse_revision_id",
    "validate_source_id",
]

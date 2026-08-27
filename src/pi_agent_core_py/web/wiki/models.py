"""Immutable DTOs for LLM Wiki schema v7 and filesystem recovery."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WIKI_SCHEMA_VERSION: Literal[7] = 7
WIKI_SPACE_MANIFEST_SCHEMA: Literal["llm-wiki-space/v1"] = "llm-wiki-space/v1"
WIKI_LEGACY_MANIFEST_SCHEMA: Literal["llm-wiki-legacy-backup/v1"] = "llm-wiki-legacy-backup/v1"
WIKI_SELECTED_PARSE_SCHEMA: Literal["llm-wiki-selected-parse/v1"] = "llm-wiki-selected-parse/v1"

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
    "summarize_source",
    "synthesize_entry_page",
    "synthesize_topic_pages",
    "rebuild_search",
    "rebuild_graph_projection",
]
WikiJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
WikiPageProposalKind = Literal["entry", "topic"]
WikiPageStatus = Literal["active", "deleted"]
WikiPageAuthorKind = Literal["agent", "user", "system"]
WikiGraphNodeKind = Literal["page", "source"]
WikiGraphRelationType = Literal[
    "related_to",
    "references",
    "extends",
    "contradicts",
    "part_of",
    "derived_from",
]
WikiChangeSetOperationKind = Literal[
    "page_create",
    "page_update",
    "page_delete",
    "edge_add",
    "edge_delete",
]

_SPACE_ID_RE = re.compile(r"^space_[0-9a-f]{24}$")
_SOURCE_ID_RE = re.compile(r"^source_[0-9a-f]{24}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_[0-9a-f]{24}$")
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{24}$")
_PARSE_ATTEMPT_ID_RE = re.compile(r"^parse_attempt_[0-9a-f]{24}$")
_PARSE_REVISION_ID_RE = re.compile(r"^parse_revision_[0-9a-f]{24}$")
_SUMMARY_ID_RE = re.compile(r"^summary_[0-9a-f]{24}$")
_PAGE_PROPOSAL_ID_RE = re.compile(r"^page_proposal_[0-9a-f]{24}$")
_CHANGE_SET_ID_RE = re.compile(r"^change_set_[0-9a-f]{24}$")
_CHANGE_SET_ITEM_ID_RE = re.compile(r"^change_item_[0-9a-f]{24}$")
_PAGE_ID_RE = re.compile(r"^page_[0-9a-f]{24}$")
_PAGE_REVISION_ID_RE = re.compile(r"^page_revision_[0-9a-f]{24}$")
_EDGE_ID_RE = re.compile(r"^edge_[0-9a-f]{24}$")
_CONVERSATION_ID_RE = re.compile(r"^conversation_[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_METADATA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-]{0,127}$")
_MODEL_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-/]{0,127}$")


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


def validate_summary_id(value: str) -> str:
    return _validate_id(value, _SUMMARY_ID_RE, "summary_id")


def validate_page_proposal_id(value: str) -> str:
    return _validate_id(value, _PAGE_PROPOSAL_ID_RE, "page_proposal_id")


def validate_change_set_id(value: str) -> str:
    return _validate_id(value, _CHANGE_SET_ID_RE, "change_set_id")


def validate_conversation_id(value: str) -> str:
    return _validate_id(value, _CONVERSATION_ID_RE, "conversation_id")


def validate_change_set_item_id(value: str) -> str:
    return _validate_id(value, _CHANGE_SET_ITEM_ID_RE, "change_set_item_id")


def validate_page_id(value: str) -> str:
    return _validate_id(value, _PAGE_ID_RE, "page_id")


def validate_page_revision_id(value: str) -> str:
    return _validate_id(value, _PAGE_REVISION_ID_RE, "page_revision_id")


def validate_edge_id(value: str) -> str:
    return _validate_id(value, _EDGE_ID_RE, "edge_id")


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


def _validate_page_slug(value: str) -> str:
    if (
        value != value.casefold()
        or value.startswith("-")
        or value.endswith("-")
        or "--" in value
        or any(not (char.isalnum() or char == "-") for char in value)
    ):
        raise ValueError("page slug has an invalid format")
    return value


def _validate_page_aliases(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) != len(set(value)) or any(
        not alias or alias != alias.strip() or len(alias) > 160 or "\x00" in alias or "\r" in alias
        for alias in value
    ):
        raise ValueError("page aliases are invalid")
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


class WikiSummaryKeyPoint(BaseModel):
    """One source-grounded statement proposed by the summary Agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=32)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("summary text contains invalid characters")
        return value

    @field_validator("page_numbers")
    @classmethod
    def _validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page < 1 for page in value) or tuple(sorted(set(value))) != value:
            raise ValueError("summary page numbers must be sorted and unique")
        return value


class WikiSummaryTopic(BaseModel):
    """A topic candidate that may become a child page in a later phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4000)
    page_numbers: tuple[int, ...] = Field(min_length=1, max_length=64)

    @field_validator("title", "summary")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("summary topic contains invalid characters")
        return value

    @field_validator("page_numbers")
    @classmethod
    def _validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page < 1 for page in value) or tuple(sorted(set(value))) != value:
            raise ValueError("topic page numbers must be sorted and unique")
        return value


class WikiSourceSummaryContent(BaseModel):
    """Strict model output; it is a draft and never a published Wiki page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suggested_title: str = Field(min_length=1, max_length=160)
    overview: str = Field(min_length=1, max_length=12000)
    key_points: tuple[WikiSummaryKeyPoint, ...] = Field(min_length=1, max_length=64)
    topics: tuple[WikiSummaryTopic, ...] = Field(default=(), max_length=32)
    caveats: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("suggested_title", "overview")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("summary content contains invalid characters")
        return value

    @field_validator("caveats")
    @classmethod
    def _validate_caveats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            not item or item != item.strip() or "\x00" in item or "\r" in item for item in value
        ):
            raise ValueError("summary caveats must be non-empty and unique")
        return value


class WikiSourceSummaryDraft(BaseModel):
    """Immutable Agent summary pinned to one selected Raw parse revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    source_id: str
    parse_revision_id: str
    job_id: str
    selection_version: int = Field(ge=1)
    source_sha256: str
    parsed_markdown_sha256: str
    manifest_sha256: str
    page_count: int = Field(ge=1)
    prompt_revision: str
    provider: str
    model: str
    content: WikiSourceSummaryContent
    content_sha256: str
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_summary_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("parse_revision_id")
    @classmethod
    def _validate_revision_id(cls, value: str) -> str:
        return validate_parse_revision_id(value)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @field_validator(
        "source_sha256",
        "parsed_markdown_sha256",
        "manifest_sha256",
        "content_sha256",
    )
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "summary sha256")

    @field_validator("prompt_revision", "provider", "model")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        if _MODEL_IDENTITY_RE.fullmatch(value) is None:
            raise ValueError("summary identity has an invalid format")
        return value

    @model_validator(mode="after")
    def _validate_citations(self) -> WikiSourceSummaryDraft:
        page_groups = [item.page_numbers for item in self.content.key_points]
        page_groups.extend(item.page_numbers for item in self.content.topics)
        if any(page > self.page_count for group in page_groups for page in group):
            raise ValueError("summary citation exceeds the selected parse revision")
        return self


class WikiPage(BaseModel):
    """Canonical current page pointer; content lives in immutable revisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    slug: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=160)
    aliases: tuple[str, ...] = Field(default=(), max_length=32)
    status: WikiPageStatus = "active"
    current_revision_id: str | None = None
    version: int = Field(ge=0)
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("current_revision_id")
    @classmethod
    def _validate_revision_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_page_revision_id(value)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        return _validate_page_slug(value)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("page title contains invalid characters")
        return value

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_page_aliases(value)

    @model_validator(mode="after")
    def _validate_state(self) -> WikiPage:
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("page update precedes creation")
        if (self.current_revision_id is None) != (self.version == 0):
            raise ValueError("page current revision and version disagree")
        if self.status == "active" and self.current_revision_id is None:
            raise ValueError("active page requires a current revision")
        return self


class WikiPageRevision(BaseModel):
    """Immutable approved page content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    page_id: str
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    markdown: str = Field(min_length=1, max_length=500_000)
    content_sha256: str
    change_set_id: str
    author_kind: WikiPageAuthorKind
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_page_revision_id(value)

    @field_validator("page_id")
    @classmethod
    def _validate_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("change_set_id")
    @classmethod
    def _validate_change_set_id(cls, value: str) -> str:
        return validate_change_set_id(value)

    @field_validator("title", "markdown")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("page revision contains invalid characters")
        return value

    @field_validator("content_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _validate_sha256(value, "page revision sha256")


class WikiPageSearchResult(BaseModel):
    """Space-scoped FTS hit for one active current approved page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_id: str
    space_id: str
    revision_id: str
    version: int = Field(ge=1)
    slug: str
    title: str
    snippet: str = Field(max_length=4000)
    rank: float

    @field_validator("page_id")
    @classmethod
    def _validate_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("revision_id")
    @classmethod
    def _validate_revision_id(cls, value: str) -> str:
        return validate_page_revision_id(value)


class WikiEdge(BaseModel):
    """One approved page-to-page relationship."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    from_page_id: str
    to_page_id: str
    relation_type: WikiRelationType
    change_set_id: str
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_edge_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("from_page_id", "to_page_id")
    @classmethod
    def _validate_page_id(cls, value: str) -> str:
        return validate_page_id(value)

    @field_validator("change_set_id")
    @classmethod
    def _validate_change_set_id(cls, value: str) -> str:
        return validate_change_set_id(value)

    @model_validator(mode="after")
    def _validate_endpoints(self) -> WikiEdge:
        if self.from_page_id == self.to_page_id:
            raise ValueError("Wiki edge cannot be a self-loop")
        if self.relation_type == "related_to" and self.from_page_id > self.to_page_id:
            raise ValueError("related_to endpoints must be canonicalized")
        return self


class WikiGraphNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: WikiGraphNodeKind
    label: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _validate_id(self) -> WikiGraphNode:
        if self.kind == "page":
            validate_page_id(self.id)
        else:
            validate_source_id(self.id)
        return self


class WikiGraphEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    from_node_id: str
    to_node_id: str
    relation_type: WikiGraphRelationType
    system_managed: bool = False

    @model_validator(mode="after")
    def _validate_system_relation(self) -> WikiGraphEdge:
        if (self.relation_type == "derived_from") != self.system_managed:
            raise ValueError("derived_from system ownership is invalid")
        if self.relation_type == "derived_from":
            validate_page_id(self.from_node_id)
            validate_source_id(self.to_node_id)
        else:
            validate_page_id(self.from_node_id)
            validate_page_id(self.to_node_id)
            validate_edge_id(self.id)
        return self


class WikiGraphSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    space_id: str
    graph_revision: int = Field(ge=0)
    nodes: tuple[WikiGraphNode, ...]
    edges: tuple[WikiGraphEdge, ...]

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)


class WikiConversation(BaseModel):
    """Trusted binding between one Wiki Space and one durable Agent Session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    session_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=160)
    status: WikiConversationStatus = "active"
    created_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_conversation_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("session_id contains invalid characters")
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("conversation title contains invalid characters")
        return value

    @model_validator(mode="after")
    def _validate_timestamps(self) -> WikiConversation:
        if self.updated_at_ms < self.created_at_ms:
            raise ValueError("conversation update precedes creation")
        return self


class WikiPageProposal(BaseModel):
    """Immutable entry/topic page proposal; it is not a published Wiki page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    source_id: str
    summary_id: str
    job_id: str
    kind: WikiPageProposalKind
    topic_ordinal: int | None = Field(default=None, ge=0)
    parent_proposal_id: str | None = None
    title: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=1, max_length=120)
    aliases: tuple[str, ...] = Field(default=(), max_length=32)
    markdown: str = Field(min_length=1, max_length=500_000)
    content_sha256: str
    source_locator_json: str
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_page_proposal_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_source_id(value)

    @field_validator("summary_id")
    @classmethod
    def _validate_summary_id(cls, value: str) -> str:
        return validate_summary_id(value)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return validate_job_id(value)

    @field_validator("parent_proposal_id")
    @classmethod
    def _validate_parent_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_page_proposal_id(value)

    @field_validator("title", "markdown")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if value != value.strip() or "\x00" in value or "\r" in value:
            raise ValueError("page proposal text contains invalid characters")
        return value

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        return _validate_page_slug(value)

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_page_aliases(value)

    @field_validator("content_sha256")
    @classmethod
    def _validate_content_hash(cls, value: str) -> str:
        return _validate_sha256(value, "page proposal sha256")

    @field_validator("source_locator_json")
    @classmethod
    def _validate_source_locator(cls, value: str) -> str:
        return _validate_json_object(value, "source_locator_json")

    @model_validator(mode="after")
    def _validate_kind(self) -> WikiPageProposal:
        if self.kind == "entry":
            if self.topic_ordinal is not None or self.parent_proposal_id is not None:
                raise ValueError("entry proposal cannot have topic ancestry")
        elif self.topic_ordinal is None or self.parent_proposal_id is None:
            raise ValueError("topic proposal requires ordinal and parent")
        return self


class WikiChangeSet(BaseModel):
    """One approval unit; no item is published while status is awaiting approval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    space_id: str
    conversation_id: str | None = Field(default=None, max_length=128)
    source_summary_id: str | None = None
    status: WikiChangeSetStatus = "draft"
    base_graph_revision: int = Field(ge=0)
    summary: str = Field(default="", max_length=4000)
    safe_error_code: str = Field(default="", max_length=80)
    created_at_ms: int = Field(ge=0)
    decided_at_ms: int | None = Field(default=None, ge=0)
    published_at_ms: int | None = Field(default=None, ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_change_set_id(value)

    @field_validator("space_id")
    @classmethod
    def _validate_space_id(cls, value: str) -> str:
        return validate_space_id(value)

    @field_validator("source_summary_id")
    @classmethod
    def _validate_summary_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_summary_id(value)

    @field_validator("conversation_id")
    @classmethod
    def _validate_conversation_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_conversation_id(value)

    @field_validator("summary")
    @classmethod
    def _validate_summary(cls, value: str) -> str:
        if "\x00" in value or "\r" in value:
            raise ValueError("change set summary contains invalid characters")
        return value

    @field_validator("safe_error_code")
    @classmethod
    def _validate_error(cls, value: str) -> str:
        return _validate_safe_metadata(value, "change set error", allow_empty=True)

    @model_validator(mode="after")
    def _validate_state(self) -> WikiChangeSet:
        if self.decided_at_ms is not None and self.decided_at_ms < self.created_at_ms:
            raise ValueError("change set decision precedes creation")
        if self.published_at_ms is not None and self.published_at_ms < self.created_at_ms:
            raise ValueError("change set publication precedes creation")
        if self.status in {"rejected", "stale"} and self.decided_at_ms is None:
            raise ValueError("decided change set requires decided_at_ms")
        if self.status == "approved" and (
            self.decided_at_ms is None or self.published_at_ms is None
        ):
            raise ValueError("approved change set requires publication timestamps")
        if self.status == "failed" and not self.safe_error_code:
            raise ValueError("failed change set requires a safe error")
        if self.status != "failed" and self.safe_error_code:
            raise ValueError("only failed change set may expose a safe error")
        return self


class WikiChangeSetItem(BaseModel):
    """One stable operation and its user-visible diff inside a Change Set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    change_set_id: str
    ordinal: int = Field(ge=0)
    operation_kind: WikiChangeSetOperationKind
    target_id: str = Field(default="", max_length=128)
    base_version: int | None = Field(default=None, ge=0)
    before_sha256: str = ""
    payload_json: str = Field(min_length=2, max_length=1_000_000)
    unified_diff: str = Field(default="", max_length=1_000_000)
    created_at_ms: int = Field(ge=0)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_change_set_item_id(value)

    @field_validator("change_set_id")
    @classmethod
    def _validate_change_set_id(cls, value: str) -> str:
        return validate_change_set_id(value)

    @field_validator("before_sha256")
    @classmethod
    def _validate_before_hash(cls, value: str) -> str:
        return value if value == "" else _validate_sha256(value, "before sha256")

    @field_validator("payload_json")
    @classmethod
    def _validate_payload(cls, value: str) -> str:
        return _validate_json_object(value, "payload_json")

    @model_validator(mode="after")
    def _validate_operation(self) -> WikiChangeSetItem:
        if self.operation_kind == "page_create" and (
            self.base_version is not None or self.before_sha256 or not self.unified_diff
        ):
            raise ValueError("page_create item has invalid base evidence")
        if self.operation_kind == "page_create" and self.target_id:
            validate_page_id(self.target_id)
        if self.operation_kind == "edge_add" and (
            self.base_version is not None or self.before_sha256 or not self.unified_diff
        ):
            raise ValueError("edge_add item has invalid base evidence")
        if self.operation_kind == "edge_add" and self.target_id:
            validate_edge_id(self.target_id)
        if self.operation_kind == "page_update" and (
            not self.target_id
            or self.base_version is None
            or self.base_version < 1
            or not self.before_sha256
            or not self.unified_diff
        ):
            raise ValueError("page_update item has invalid base evidence")
        if self.operation_kind == "page_update":
            validate_page_id(self.target_id)
        if self.operation_kind == "page_delete" and (
            not self.target_id
            or self.base_version is None
            or self.base_version < 1
            or not self.before_sha256
            or not self.unified_diff
        ):
            raise ValueError("page_delete item has invalid base evidence")
        if self.operation_kind == "page_delete":
            validate_page_id(self.target_id)
        if self.operation_kind == "edge_delete" and (
            not self.target_id
            or self.base_version is not None
            or self.before_sha256
            or not self.unified_diff
        ):
            raise ValueError("edge_delete item has invalid base evidence")
        if self.operation_kind == "edge_delete":
            validate_edge_id(self.target_id)
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
        if (
            self.kind
            in {
                "parse",
                "summarize_source",
                "synthesize_entry_page",
                "synthesize_topic_pages",
            }
            and self.source_id is None
        ):
            raise ValueError("source job requires source_id")
        if self.kind in {
            "parse",
            "summarize_source",
            "synthesize_entry_page",
            "synthesize_topic_pages",
        }:
            if (self.base_selected_parse_revision_id is None) != (self.base_selection_version == 0):
                raise ValueError("source job base selection identity is inconsistent")
        if (
            self.kind
            in {
                "summarize_source",
                "synthesize_entry_page",
                "synthesize_topic_pages",
            }
            and self.base_selected_parse_revision_id is None
        ):
            raise ValueError("derived source job requires a selected parse revision")
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
    "WikiChangeSet",
    "WikiChangeSetItem",
    "WikiChangeSetOperationKind",
    "WikiEdge",
    "WikiConversation",
    "WikiConversationStatus",
    "WikiGraphEdge",
    "WikiGraphNode",
    "WikiGraphNodeKind",
    "WikiGraphRelationType",
    "WikiGraphSnapshot",
    "WikiJobKind",
    "WikiJobStatus",
    "WikiJob",
    "WikiLegacyBackupReceipt",
    "WikiMirrorRepairReport",
    "WikiParseAttempt",
    "WikiParseAttemptState",
    "WikiParseMode",
    "WikiParseRevision",
    "WikiPageProposal",
    "WikiPageProposalKind",
    "WikiPage",
    "WikiPageAuthorKind",
    "WikiPageRevision",
    "WikiPageSearchResult",
    "WikiPageStatus",
    "WikiRelationType",
    "WikiSourceMimeType",
    "WikiSourceStatus",
    "WikiSourceSummaryContent",
    "WikiSourceSummaryDraft",
    "WikiSummaryKeyPoint",
    "WikiSummaryTopic",
    "WikiSelectedParsePointer",
    "WikiSource",
    "WikiSpace",
    "WikiSpaceManifest",
    "WikiSpaceStatus",
    "validate_space_id",
    "validate_artifact_id",
    "validate_change_set_id",
    "validate_change_set_item_id",
    "validate_conversation_id",
    "validate_edge_id",
    "validate_job_id",
    "validate_parse_attempt_id",
    "validate_parse_revision_id",
    "validate_page_proposal_id",
    "validate_page_id",
    "validate_page_revision_id",
    "validate_source_id",
    "validate_summary_id",
]

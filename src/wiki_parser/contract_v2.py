"""Contract v2 for isolated MinerU PDF parsing.

This module contains data only. It deliberately does not import either parser
runtime, the main application, a transport, or a container SDK.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ParserErrorCode
from .models import ParserLimits, ParserSourceSpec, validate_artifact_path

PARSER_CONTRACT_VERSION_V2: Literal[2] = 2
PARSER_ARTIFACT_SCHEMA_V2: Literal["llm-wiki-parser-artifact/v2"] = (
    "llm-wiki-parser-artifact/v2"
)

ParserRequestedMode = Literal["pipeline", "gpu-medium", "gpu-high"]
ParserEngineName = Literal["mineru"]
ParserPresetName = Literal[
    "mineru_pipeline",
    "mineru_gpu_medium",
    "mineru_gpu_high",
]
ParserProviderV2Name = Literal["mineru", "fake_mineru"]
ParserLicenseModeV2 = Literal["mineru_open_source", "not_required"]
ParserRouteReason = Literal[
    "explicit_pipeline",
    "explicit_gpu_medium",
    "explicit_gpu_high",
]
ParserQualityFailureCode = Literal[
    "missing_pages",
    "empty_content",
    "replacement_characters",
    "excessive_controls",
    "incomplete_content",
    "excessive_repetition",
    "markdown_invalid",
    "asset_reference_invalid",
]
ParserAttemptStateV2 = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "quality_rejected",
    "cancelled",
]
ParserJobStateV2 = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "destroyed",
]
ParserJobPhaseV2 = Literal[
    "queued",
    "preflight",
    "routing",
    "mineru_parse",
    "quality_check",
    "packaging",
    "terminal",
]
ParserArtifactKindV2 = Literal[
    "document_markdown",
    "page_markdown",
    "embedded_image",
    "table_image",
]
ParserImageMimeTypeV2 = Literal["image/png", "image/jpeg", "image/webp"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")
_IMAGE_SUFFIX_BY_MIME: dict[ParserImageMimeTypeV2, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_PAGE_BOUNDARY_PREFIX = "<!-- llm-wiki-pdf-page:"


def _validate_identifier(value: str, label: str) -> str:
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def _validate_version(value: str) -> str:
    if _SAFE_VERSION_RE.fullmatch(value) is None:
        raise ValueError("version has an invalid format")
    return value


def _validate_safe_codes(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} values must be unique")
    if any(_IDENTIFIER_RE.fullmatch(value) is None for value in values):
        raise ValueError(f"{label} contains an invalid code")
    return values


def _validate_parser_preset(
    parser: ParserEngineName,
    preset: ParserPresetName,
) -> None:
    if parser != "mineru" or preset not in {
        "mineru_pipeline",
        "mineru_gpu_medium",
        "mineru_gpu_high",
    }:
        raise ValueError("MinerU parser identity and preset are inconsistent")


def canonical_markdown_v2(pages: tuple[ParserParsedPage, ...]) -> bytes:
    """Build the byte-exact document Markdown from ordered original pages."""
    sections: list[str] = []
    for page in pages:
        markdown = page.markdown
        if _PAGE_BOUNDARY_PREFIX in markdown:
            raise ValueError("page Markdown contains a reserved boundary marker")
        if markdown and not markdown.endswith("\n"):
            markdown += "\n"
        sections.append(
            f"{_PAGE_BOUNDARY_PREFIX}{page.page_number} -->\n{markdown}"
        )
    return "\n".join(sections).encode("utf-8")


def canonical_preflight_sha256(report: ParserPreflightReport) -> str:
    """Hash one preflight report with stable JSON key ordering."""
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ParserRoutingConfigIdentity(BaseModel):
    """Hash-pinned routing/quality configuration selected by an administrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    revision: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("revision")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        return _validate_identifier(value, "routing config revision")


class ParserJobSpecV2(BaseModel):
    """One exact v2 request without arbitrary parser arguments or thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    job_id: str
    source: ParserSourceSpec
    requested_mode: ParserRequestedMode = "pipeline"
    routing_config: ParserRoutingConfigIdentity
    output_schema: Literal["llm-wiki-parser-artifact/v2"] = PARSER_ARTIFACT_SCHEMA_V2
    limits: ParserLimits = Field(default_factory=ParserLimits)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return _validate_identifier(value, "job_id")

    @model_validator(mode="after")
    def _validate_source_limit(self) -> ParserJobSpecV2:
        if self.source.size_bytes > self.limits.max_source_bytes:
            raise ValueError("source size exceeds parser job limit")
        return self


class ParserPreflightReport(BaseModel):
    """Content-free PDF features used by the versioned router."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    text_page_count: int = Field(ge=0)
    image_dominant_page_count: int = Field(ge=0)
    multicolumn_page_count: int = Field(ge=0)
    table_candidate_page_count: int = Field(ge=0)
    text_page_ratio: float = Field(ge=0.0, le=1.0)
    image_dominant_page_ratio: float = Field(ge=0.0, le=1.0)
    complexity_score: float = Field(ge=0.0, le=1.0)
    encrypted: Literal[False] = False
    has_javascript: bool = False
    has_embedded_files: bool = False
    duration_ms: int = Field(ge=0)
    observed_at_ms: int = Field(ge=0)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return _validate_identifier(value, "source_id")

    @model_validator(mode="after")
    def _validate_page_features(self) -> ParserPreflightReport:
        counts = (
            self.text_page_count,
            self.image_dominant_page_count,
            self.multicolumn_page_count,
            self.table_candidate_page_count,
        )
        if any(value > self.page_count for value in counts):
            raise ValueError("preflight page feature count exceeds page_count")
        expected_text_ratio = self.text_page_count / self.page_count
        expected_image_ratio = self.image_dominant_page_count / self.page_count
        if abs(self.text_page_ratio - expected_text_ratio) > 1e-9:
            raise ValueError("text_page_ratio does not match page counts")
        if abs(self.image_dominant_page_ratio - expected_image_ratio) > 1e-9:
            raise ValueError("image_dominant_page_ratio does not match page counts")
        return self


class ParserRouteDecision(BaseModel):
    """Auditable mapping from one product preset to one MinerU backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_mode: ParserRequestedMode
    initial_parser: ParserEngineName
    initial_preset: ParserPresetName
    reasons: tuple[ParserRouteReason, ...]
    preflight_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fallback_parser: None = None
    fallback_preset: None = None

    @field_validator("reasons")
    @classmethod
    def _validate_reasons(
        cls,
        values: tuple[ParserRouteReason, ...],
    ) -> tuple[ParserRouteReason, ...]:
        if not values:
            raise ValueError("route decision requires at least one reason")
        _validate_safe_codes(cast(tuple[str, ...], values), "route reason")
        return values

    @model_validator(mode="after")
    def _validate_route(self) -> ParserRouteDecision:
        _validate_parser_preset(self.initial_parser, self.initial_preset)
        expected = {
            "pipeline": ("mineru_pipeline", ("explicit_pipeline",)),
            "gpu-medium": ("mineru_gpu_medium", ("explicit_gpu_medium",)),
            "gpu-high": ("mineru_gpu_high", ("explicit_gpu_high",)),
        }[self.requested_mode]
        if self.initial_parser != "mineru" or (
            self.initial_preset,
            self.reasons,
        ) != expected:
            raise ValueError("MinerU route does not match the requested preset")
        return self


class ParserQualityMetrics(BaseModel):
    """Raw explainable metrics; no opaque score replaces these values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_page_count: int = Field(ge=1)
    output_page_count: int = Field(ge=0)
    pages_with_text: int = Field(ge=0)
    output_character_count: int = Field(ge=0)
    text_page_coverage: float = Field(ge=0.0, le=1.0)
    replacement_char_ratio: float = Field(ge=0.0, le=1.0)
    control_char_ratio: float = Field(ge=0.0, le=1.0)
    content_completeness: float = Field(ge=0.0, le=1.0)
    repetition_ratio: float = Field(ge=0.0, le=1.0)
    page_count_match: bool
    markdown_health: float = Field(ge=0.0, le=1.0)
    asset_reference_health: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_counts(self) -> ParserQualityMetrics:
        if self.pages_with_text > self.output_page_count:
            raise ValueError("pages_with_text exceeds output_page_count")
        expected_coverage = self.pages_with_text / self.input_page_count
        if abs(self.text_page_coverage - min(expected_coverage, 1.0)) > 1e-9:
            raise ValueError("text_page_coverage does not match page counts")
        if self.page_count_match != (self.input_page_count == self.output_page_count):
            raise ValueError("page_count_match does not match page counts")
        return self


class ParserQualityReport(BaseModel):
    """Versioned quality decision for one exact parser attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parser: ParserEngineName
    parser_version: str
    preset: ParserPresetName
    routing_config: ParserRoutingConfigIdentity
    metrics: ParserQualityMetrics
    score: float = Field(ge=0.0, le=1.0)
    pass_threshold: float = Field(ge=0.0, le=1.0)
    passed: bool
    critical_failures: tuple[ParserQualityFailureCode, ...] = ()
    evaluated_at_ms: int = Field(ge=0)

    @field_validator("parser_version")
    @classmethod
    def _validate_parser_version(cls, value: str) -> str:
        return _validate_version(value)

    @field_validator("critical_failures")
    @classmethod
    def _validate_critical_failures(
        cls,
        values: tuple[ParserQualityFailureCode, ...],
    ) -> tuple[ParserQualityFailureCode, ...]:
        _validate_safe_codes(cast(tuple[str, ...], values), "quality failure")
        return values

    @model_validator(mode="after")
    def _validate_decision(self) -> ParserQualityReport:
        _validate_parser_preset(self.parser, self.preset)
        expected = self.score >= self.pass_threshold and not self.critical_failures
        if self.passed != expected:
            raise ValueError("quality passed flag disagrees with score or critical failures")
        return self


class ParserBoundingBox(BaseModel):
    """Top-left PDF point coordinates with explicit page dimensions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coordinate_space: Literal["pdf_points_top_left"] = "pdf_points_top_left"
    left: float = Field(ge=0.0)
    top: float = Field(ge=0.0)
    right: float = Field(gt=0.0)
    bottom: float = Field(gt=0.0)
    page_width: float = Field(gt=0.0)
    page_height: float = Field(gt=0.0)

    @model_validator(mode="after")
    def _validate_box(self) -> ParserBoundingBox:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("bounding box must have positive area")
        if self.right > self.page_width or self.bottom > self.page_height:
            raise ValueError("bounding box exceeds page dimensions")
        return self


class ParserSourceSpan(BaseModel):
    """Optional parser-neutral provenance for one page block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    block_id: str | None = None
    bbox: ParserBoundingBox | None = None

    @field_validator("block_id")
    @classmethod
    def _validate_block_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_identifier(value, "block_id")
        return value


class ParserParsedPage(BaseModel):
    """One original PDF page; this is provenance, never a retrieval chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1)
    markdown: str = Field(repr=False)
    markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plain_text: str = Field(repr=False)
    plain_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_spans: tuple[ParserSourceSpan, ...] = ()

    @field_validator("markdown", "plain_text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if "\ufffd" in value or "\x00" in value:
            raise ValueError("parsed page contains replacement or NUL characters")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise ValueError("parsed page contains invalid control characters")
        return value

    @model_validator(mode="after")
    def _validate_content_evidence(self) -> ParserParsedPage:
        markdown_digest = hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()
        plain_digest = hashlib.sha256(self.plain_text.encode("utf-8")).hexdigest()
        if self.markdown_sha256 != markdown_digest:
            raise ValueError("page Markdown SHA-256 does not match content")
        if self.plain_text_sha256 != plain_digest:
            raise ValueError("page plain-text SHA-256 does not match content")
        if any(span.page_number != self.page_number for span in self.source_spans):
            raise ValueError("source span page does not match ParsedPage")
        return self


class ParserParsedAsset(BaseModel):
    """Content-addressed image asset with optional page provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: Literal["embedded_image", "table_image"]
    mime_type: ParserImageMimeTypeV2
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1)
    bbox: ParserBoundingBox | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_artifact_path(value)

    @model_validator(mode="after")
    def _validate_asset(self) -> ParserParsedAsset:
        path = PurePosixPath(self.path)
        if path.parent.as_posix() != "images":
            raise ValueError("parsed asset must be directly inside images/")
        suffix = _IMAGE_SUFFIX_BY_MIME[self.mime_type]
        if not self.path.lower().endswith(suffix):
            raise ValueError("parsed asset suffix does not match MIME type")
        if self.bbox is not None and self.page_number is None:
            raise ValueError("asset bounding box requires page_number")
        return self


class ParserAttemptEvidence(BaseModel):
    """Persistable, content-free evidence for one routed parser attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str
    ordinal: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser: ParserEngineName
    parser_version: str
    preset: ParserPresetName
    routing_config: ParserRoutingConfigIdentity
    route_reasons: tuple[ParserRouteReason, ...]
    state: ParserAttemptStateV2
    started_at_ms: int | None = Field(default=None, ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    safe_error_code: ParserErrorCode | None = None
    quality_report: ParserQualityReport | None = None
    # Digest of the normalized attempt output tree before final tar packaging.
    output_size_bytes: int | None = Field(default=None, ge=1)
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fallback_from_attempt_id: str | None = None

    @field_validator("attempt_id")
    @classmethod
    def _validate_attempt_id(cls, value: str) -> str:
        return _validate_identifier(value, "attempt_id")

    @field_validator("fallback_from_attempt_id")
    @classmethod
    def _validate_fallback_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_identifier(value, "fallback_from_attempt_id")
        return value

    @field_validator("parser_version")
    @classmethod
    def _validate_parser_version(cls, value: str) -> str:
        return _validate_version(value)

    @field_validator("route_reasons")
    @classmethod
    def _validate_route_reasons(
        cls,
        values: tuple[ParserRouteReason, ...],
    ) -> tuple[ParserRouteReason, ...]:
        if not values:
            raise ValueError("attempt requires route reasons")
        _validate_safe_codes(cast(tuple[str, ...], values), "route reason")
        return values

    @model_validator(mode="after")
    def _validate_attempt(self) -> ParserAttemptEvidence:
        _validate_parser_preset(self.parser, self.preset)
        if self.started_at_ms is None:
            if any(
                value is not None
                for value in (self.finished_at_ms, self.duration_ms)
            ):
                raise ValueError("attempt timestamps require started_at_ms")
        elif self.finished_at_ms is not None:
            expected_duration = self.finished_at_ms - self.started_at_ms
            if expected_duration < 0 or self.duration_ms != expected_duration:
                raise ValueError("attempt duration does not match timestamps")
        elif self.duration_ms is not None:
            raise ValueError("running attempt cannot include duration")

        has_output = self.output_size_bytes is not None or self.output_sha256 is not None
        if (self.output_size_bytes is None) != (self.output_sha256 is None):
            raise ValueError("attempt output evidence must be complete")
        if self.quality_report is not None and (
            self.quality_report.parser != self.parser
            or self.quality_report.parser_version != self.parser_version
            or self.quality_report.preset != self.preset
            or self.quality_report.routing_config != self.routing_config
        ):
            raise ValueError("attempt quality report does not match parser identity")

        if self.state == "succeeded":
            if (
                not has_output
                or self.finished_at_ms is None
                or self.safe_error_code is not None
                or self.quality_report is None
                or not self.quality_report.passed
            ):
                raise ValueError("succeeded attempt requires clean artifact and quality evidence")
        elif self.state == "quality_rejected":
            if (
                has_output
                or self.finished_at_ms is None
                or self.safe_error_code != "quality_rejected"
                or self.quality_report is None
                or self.quality_report.passed
            ):
                raise ValueError("quality-rejected attempt requires failed quality evidence")
        elif self.state == "failed":
            if has_output or self.finished_at_ms is None or self.safe_error_code is None:
                raise ValueError("failed attempt requires a safe error and no output")
        elif self.state == "cancelled":
            if (
                has_output
                or self.finished_at_ms is None
                or self.safe_error_code != "cancelled"
            ):
                raise ValueError("cancelled attempt requires cancelled error and no artifact")
        elif any(
            value is not None
            for value in (
                self.finished_at_ms,
                self.safe_error_code,
                self.quality_report,
                self.output_size_bytes,
                self.output_sha256,
            )
        ):
            raise ValueError("non-terminal attempt cannot include terminal evidence")
        if self.state == "queued" and self.started_at_ms is not None:
            raise ValueError("queued attempt cannot have a start timestamp")
        if self.state == "running" and self.started_at_ms is None:
            raise ValueError("running attempt requires a start timestamp")
        if self.fallback_from_attempt_id is not None:
            raise ValueError("MinerU jobs do not support parser fallback attempts")
        return self


class ParserParsedDocument(BaseModel):
    """Selected parser-neutral document with complete route/quality evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    source_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_mode: ParserRequestedMode
    selected_attempt_id: str
    parser: ParserEngineName
    parser_version: str
    preset: ParserPresetName
    routing_config: ParserRoutingConfigIdentity
    preflight: ParserPreflightReport
    route_decision: ParserRouteDecision
    canonical_markdown_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    pages: tuple[ParserParsedPage, ...]
    assets: tuple[ParserParsedAsset, ...] = ()
    quality_report: ParserQualityReport
    attempts: tuple[ParserAttemptEvidence, ...]
    warnings: tuple[str, ...] = ()

    @field_validator("source_id", "selected_attempt_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")

    @field_validator("parser_version")
    @classmethod
    def _validate_parser_version(cls, value: str) -> str:
        return _validate_version(value)

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_safe_codes(values, "warning")

    @model_validator(mode="after")
    def _validate_document(self) -> ParserParsedDocument:
        _validate_parser_preset(self.parser, self.preset)
        if (
            self.preflight.source_id != self.source_id
            or self.preflight.source_sha256 != self.source_sha256
        ):
            raise ValueError("preflight identity does not match ParsedDocument")
        if (
            self.route_decision.requested_mode != self.requested_mode
            or self.route_decision.preflight_sha256
            != canonical_preflight_sha256(self.preflight)
        ):
            raise ValueError("route decision does not match request or preflight")
        if self.page_count != len(self.pages):
            raise ValueError("page_count does not match ParsedPage count")
        if [page.page_number for page in self.pages] != list(
            range(1, self.page_count + 1)
        ):
            raise ValueError("ParsedPage numbers must be contiguous and one-based")
        canonical_digest = hashlib.sha256(canonical_markdown_v2(self.pages)).hexdigest()
        if self.canonical_markdown_sha256 != canonical_digest:
            raise ValueError("canonical Markdown SHA-256 does not match pages")
        asset_paths = [asset.path.casefold() for asset in self.assets]
        if len(set(asset_paths)) != len(asset_paths):
            raise ValueError("ParsedDocument asset paths must be unique")
        if any(
            asset.page_number is not None and asset.page_number > self.page_count
            for asset in self.assets
        ):
            raise ValueError("asset page exceeds document page_count")
        if not self.attempts:
            raise ValueError("ParsedDocument requires attempt evidence")
        if [attempt.ordinal for attempt in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("attempt ordinals must be contiguous and one-based")
        attempt_by_id = {attempt.attempt_id: attempt for attempt in self.attempts}
        if len(attempt_by_id) != len(self.attempts):
            raise ValueError("attempt identifiers must be unique")
        for index, attempt in enumerate(self.attempts):
            if attempt.source_sha256 != self.source_sha256:
                raise ValueError("attempt source SHA does not match ParsedDocument")
            if attempt.routing_config != self.routing_config:
                raise ValueError("attempt uses a different routing configuration")
            fallback_id = attempt.fallback_from_attempt_id
            if fallback_id is not None and fallback_id not in {
                previous.attempt_id for previous in self.attempts[:index]
            }:
                raise ValueError("fallback must reference an earlier attempt")
        selected = attempt_by_id.get(self.selected_attempt_id)
        if (
            selected is None
            or selected.state != "succeeded"
            or selected != self.attempts[-1]
            or selected.parser != self.parser
            or selected.parser_version != self.parser_version
            or selected.preset != self.preset
            or selected.quality_report != self.quality_report
        ):
            raise ValueError("selected attempt does not match ParsedDocument identity")
        if self.quality_report.routing_config != self.routing_config:
            raise ValueError("quality report uses a different routing configuration")
        first_attempt = self.attempts[0]
        if (
            first_attempt.parser != self.route_decision.initial_parser
            or first_attempt.preset != self.route_decision.initial_preset
            or first_attempt.route_reasons != self.route_decision.reasons
        ):
            raise ValueError("initial attempt does not match route decision")
        if len(self.attempts) != 1 or self.parser != "mineru":
            raise ValueError("MinerU ParsedDocument requires exactly one attempt")
        return self


class ParserCapabilitiesV2(BaseModel):
    """Stable MinerU capabilities without runtime paths or model details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_types: tuple[Literal["application/pdf"], ...] = ("application/pdf",)
    requested_modes: tuple[ParserRequestedMode, ...] = (
        "pipeline",
        "gpu-medium",
        "gpu-high",
    )
    parsers: tuple[ParserEngineName, ...] = ("mineru",)
    presets: tuple[ParserPresetName, ...] = (
        "mineru_pipeline",
        "mineru_gpu_medium",
        "mineru_gpu_high",
    )
    output_schemas: tuple[str, ...] = (PARSER_ARTIFACT_SCHEMA_V2,)
    pipeline_supports_cpu: Literal[True] = True
    gpu_presets_require_cuda: Literal[True] = True
    network_during_job: Literal[False] = False

    @field_validator(
        "media_types",
        "requested_modes",
        "parsers",
        "presets",
        "output_schemas",
    )
    @classmethod
    def _validate_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("capability values must be non-empty and unique")
        return values


class ParserProbeV2(BaseModel):
    """Readiness snapshot for the MinerU worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ParserProviderV2Name
    available: bool
    worker_version: str
    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    license_mode: ParserLicenseModeV2
    routing_config: ParserRoutingConfigIdentity
    capabilities: ParserCapabilitiesV2 = Field(default_factory=ParserCapabilitiesV2)
    observed_at_ms: int = Field(ge=0)
    error_code: ParserErrorCode | None = None

    @field_validator("worker_version")
    @classmethod
    def _validate_worker_version(cls, value: str) -> str:
        return _validate_version(value)

    @model_validator(mode="after")
    def _validate_probe(self) -> ParserProbeV2:
        if self.available != (self.error_code is None):
            raise ValueError("probe availability and error code disagree")
        if self.provider == "mineru" and self.license_mode != "mineru_open_source":
            raise ValueError("real MinerU provider requires its declared license mode")
        if self.provider == "fake_mineru" and self.license_mode != "not_required":
            raise ValueError("fake MinerU provider must not claim a runtime license")
        return self


class ParserJobHandleV2(BaseModel):
    """Persistable, content-free identity for one v2 parser job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    provider: ParserProviderV2Name
    provider_job_id: str
    job_id: str
    source_id: str
    created_at_ms: int = Field(ge=0)

    @field_validator("provider_job_id", "job_id", "source_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")


class ParserJobStatusV2(BaseModel):
    """Observed v2 job state with route and attempt progress evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: ParserJobHandleV2
    state: ParserJobStateV2
    phase: ParserJobPhaseV2
    observed_at_ms: int = Field(ge=0)
    started_at_ms: int | None = Field(default=None, ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)
    current_attempt_ordinal: int | None = Field(default=None, ge=1)
    route_decision: ParserRouteDecision | None = None
    attempts: tuple[ParserAttemptEvidence, ...] = ()
    safe_error_code: ParserErrorCode | None = None
    artifact_size_bytes: int | None = Field(default=None, ge=1)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_status(self) -> ParserJobStatusV2:
        if self.started_at_ms is not None and self.finished_at_ms is not None:
            if self.finished_at_ms < self.started_at_ms:
                raise ValueError("job finished_at_ms precedes started_at_ms")
        has_artifact = self.artifact_size_bytes is not None or self.artifact_sha256 is not None
        if (self.artifact_size_bytes is None) != (self.artifact_sha256 is None):
            raise ValueError("job artifact evidence must be complete")
        if self.current_attempt_ordinal is not None and (
            self.current_attempt_ordinal > len(self.attempts) + 1
        ):
            raise ValueError("current attempt ordinal exceeds observed attempts")
        if [attempt.ordinal for attempt in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("job attempt ordinals must be contiguous")
        if len({attempt.attempt_id for attempt in self.attempts}) != len(self.attempts):
            raise ValueError("job attempt identifiers must be unique")
        if len({attempt.source_sha256 for attempt in self.attempts}) > 1:
            raise ValueError("job attempts must use one original PDF SHA")
        if len({attempt.routing_config for attempt in self.attempts}) > 1:
            raise ValueError("job attempts must use one routing configuration")
        if len(self.attempts) > 1 or any(
            attempt.fallback_from_attempt_id is not None for attempt in self.attempts
        ):
            raise ValueError("MinerU jobs support exactly one parser attempt")
        if self.route_decision is not None and self.attempts:
            first_attempt = self.attempts[0]
            if (
                first_attempt.parser != self.route_decision.initial_parser
                or first_attempt.preset != self.route_decision.initial_preset
                or first_attempt.route_reasons != self.route_decision.reasons
            ):
                raise ValueError("job initial attempt does not match route decision")
        if self.state == "succeeded":
            if (
                not has_artifact
                or self.finished_at_ms is None
                or self.safe_error_code is not None
                or self.phase != "terminal"
                or self.route_decision is None
                or not self.attempts
                or self.attempts[-1].state != "succeeded"
            ):
                raise ValueError("succeeded job requires terminal artifact and attempt evidence")
        elif self.state == "failed":
            if (
                has_artifact
                or self.finished_at_ms is None
                or self.safe_error_code is None
                or self.phase != "terminal"
            ):
                raise ValueError("failed job requires a safe error and no artifact")
        elif self.state == "cancelled":
            if (
                has_artifact
                or self.finished_at_ms is None
                or self.safe_error_code != "cancelled"
                or self.phase != "terminal"
            ):
                raise ValueError("cancelled job requires cancelled error and no artifact")
        elif self.state == "destroyed":
            if self.phase != "terminal":
                raise ValueError("destroyed job must be terminal")
            if has_artifact or self.safe_error_code is not None:
                raise ValueError("destroyed job cannot expose artifact or error evidence")
        elif has_artifact or self.safe_error_code is not None:
            raise ValueError("non-terminal job cannot expose artifact or error evidence")
        if self.state == "queued" and self.started_at_ms is not None:
            raise ValueError("queued job cannot have a start timestamp")
        if self.state == "queued" and self.phase != "queued":
            raise ValueError("queued job must use the queued phase")
        if self.state == "running" and self.started_at_ms is None:
            raise ValueError("running job requires a start timestamp")
        if self.state == "running" and self.phase in {"queued", "terminal"}:
            raise ValueError("running job requires an active phase")
        return self


class ParserArtifactFileV2(BaseModel):
    """One declared immutable file inside a v2 artifact tar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: ParserArtifactKindV2
    mime_type: str = Field(min_length=1, max_length=80)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_artifact_path(value)

    @model_validator(mode="after")
    def _validate_kind(self) -> ParserArtifactFileV2:
        if self.kind == "document_markdown":
            if (
                self.path != "parsed.md"
                or self.mime_type != "text/markdown"
                or self.page_number is not None
                or self.size_bytes == 0
            ):
                raise ValueError("document Markdown must be parsed.md")
            return self
        if self.kind == "page_markdown":
            if self.page_number is None or self.mime_type != "text/markdown":
                raise ValueError("page Markdown requires page number and text/markdown")
            expected = f"pages/{self.page_number:06d}.md"
            if self.path != expected:
                raise ValueError("page Markdown path does not match page number")
            return self
        if self.page_number is None:
            raise ValueError("image artifact requires page number")
        if self.size_bytes == 0:
            raise ValueError("image artifact cannot be empty")
        if PurePosixPath(self.path).parent.as_posix() != "images":
            raise ValueError("image artifact must be directly inside images/")
        suffix = _IMAGE_SUFFIX_BY_MIME.get(cast(ParserImageMimeTypeV2, self.mime_type))
        if suffix is None or not self.path.lower().endswith(suffix):
            raise ValueError("image artifact MIME and suffix are not allowed")
        return self


class ParserArtifactManifestV2(BaseModel):
    """Canonical v2 manifest with route, quality and attempt evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["llm-wiki-parser-artifact/v2"] = Field(
        default=PARSER_ARTIFACT_SCHEMA_V2,
        alias="schema",
    )
    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    job_id: str
    source_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_mode: ParserRequestedMode
    selected_attempt_id: str
    parser: ParserEngineName
    parser_version: str
    preset: ParserPresetName
    routing_config: ParserRoutingConfigIdentity
    preflight: ParserPreflightReport
    route_decision: ParserRouteDecision
    page_count: int = Field(ge=1)
    quality_report: ParserQualityReport
    attempts: tuple[ParserAttemptEvidence, ...]
    markdown: ParserArtifactFileV2
    pages: tuple[ParserArtifactFileV2, ...]
    assets: tuple[ParserArtifactFileV2, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("job_id", "source_id", "selected_attempt_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")

    @field_validator("parser_version")
    @classmethod
    def _validate_parser_version(cls, value: str) -> str:
        return _validate_version(value)

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_safe_codes(values, "warning")

    @model_validator(mode="after")
    def _validate_manifest(self) -> ParserArtifactManifestV2:
        _validate_parser_preset(self.parser, self.preset)
        if (
            self.preflight.source_id != self.source_id
            or self.preflight.source_sha256 != self.source_sha256
        ):
            raise ValueError("manifest preflight identity does not match source")
        if (
            self.route_decision.requested_mode != self.requested_mode
            or self.route_decision.preflight_sha256
            != canonical_preflight_sha256(self.preflight)
        ):
            raise ValueError("manifest route decision does not match preflight")
        if self.markdown.kind != "document_markdown":
            raise ValueError("manifest markdown entry has the wrong kind")
        if self.page_count != len(self.pages):
            raise ValueError("manifest page_count does not match page files")
        if [page.page_number for page in self.pages] != list(
            range(1, self.page_count + 1)
        ) or any(page.kind != "page_markdown" for page in self.pages):
            raise ValueError("manifest page files must be contiguous page Markdown")
        if any(asset.kind not in {"embedded_image", "table_image"} for asset in self.assets):
            raise ValueError("manifest asset has the wrong kind")
        if any(
            asset.page_number is not None and asset.page_number > self.page_count
            for asset in self.assets
        ):
            raise ValueError("manifest asset page exceeds page_count")
        files = (self.markdown, *self.pages, *self.assets)
        paths = [file.path for file in files]
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("manifest artifact paths must be unique")
        attempts = {attempt.attempt_id: attempt for attempt in self.attempts}
        selected = attempts.get(self.selected_attempt_id)
        if (
            len(attempts) != len(self.attempts)
            or selected is None
            or selected.state != "succeeded"
            or selected != self.attempts[-1]
            or selected.parser != self.parser
            or selected.parser_version != self.parser_version
            or selected.preset != self.preset
            or selected.quality_report != self.quality_report
        ):
            raise ValueError("manifest selected attempt evidence is inconsistent")
        if [attempt.ordinal for attempt in self.attempts] != list(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("manifest attempt ordinals must be contiguous")
        for attempt in self.attempts:
            if attempt.source_sha256 != self.source_sha256:
                raise ValueError("manifest attempt source SHA does not match source")
            if attempt.routing_config != self.routing_config:
                raise ValueError("manifest attempt uses a different routing config")
            if attempt.fallback_from_attempt_id is not None:
                raise ValueError("MinerU manifests cannot contain fallback attempts")
        if self.quality_report.routing_config != self.routing_config:
            raise ValueError("manifest quality config does not match routing config")
        first_attempt = self.attempts[0]
        if (
            first_attempt.parser != self.route_decision.initial_parser
            or first_attempt.preset != self.route_decision.initial_preset
            or first_attempt.route_reasons != self.route_decision.reasons
        ):
            raise ValueError("manifest initial attempt does not match route decision")
        if len(self.attempts) != 1:
            raise ValueError("MinerU manifests require exactly one attempt")
        return self


class ParserArtifactReceiptV2(BaseModel):
    """Evidence for one atomically downloaded v2 artifact tar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[2] = PARSER_CONTRACT_VERSION_V2
    schema_id: Literal["llm-wiki-parser-artifact/v2"] = PARSER_ARTIFACT_SCHEMA_V2
    job_id: str
    source_id: str
    archive_format: Literal["tar"] = "tar"
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=3)

    @field_validator("job_id", "source_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")


__all__ = [
    "PARSER_ARTIFACT_SCHEMA_V2",
    "PARSER_CONTRACT_VERSION_V2",
    "ParserArtifactFileV2",
    "ParserArtifactKindV2",
    "ParserArtifactManifestV2",
    "ParserArtifactReceiptV2",
    "ParserAttemptEvidence",
    "ParserAttemptStateV2",
    "ParserBoundingBox",
    "ParserCapabilitiesV2",
    "ParserEngineName",
    "ParserImageMimeTypeV2",
    "ParserJobHandleV2",
    "ParserJobPhaseV2",
    "ParserJobSpecV2",
    "ParserJobStateV2",
    "ParserJobStatusV2",
    "ParserLicenseModeV2",
    "ParserParsedAsset",
    "ParserParsedDocument",
    "ParserParsedPage",
    "ParserPreflightReport",
    "ParserPresetName",
    "ParserProbeV2",
    "ParserProviderV2Name",
    "ParserQualityFailureCode",
    "ParserQualityMetrics",
    "ParserQualityReport",
    "ParserRequestedMode",
    "ParserRouteDecision",
    "ParserRouteReason",
    "ParserRoutingConfigIdentity",
    "ParserSourceSpan",
    "canonical_markdown_v2",
    "canonical_preflight_sha256",
]

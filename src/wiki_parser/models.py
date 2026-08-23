"""Immutable provider-neutral DTOs for isolated Wiki parser workers."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ParserErrorCode

PARSER_CONTRACT_VERSION: Literal[1] = 1
PARSER_ARTIFACT_SCHEMA: Literal["llm-wiki-parser-artifact/v1"] = (
    "llm-wiki-parser-artifact/v1"
)

ParserProviderName = Literal["fake", "marker_sidecar"]
ParserJobState = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "destroyed",
]
ParserMode = Literal["fast_no_ocr"]
ParserLicenseMode = Literal[
    "not_required",
    "unconfigured",
    "self_hosted_eligible",
    "commercial",
]
ParserMediaType = Literal["application/pdf"]
ParserArtifactKind = Literal["markdown", "embedded_image"]
ParserImageMimeType = Literal["image/png", "image/jpeg", "image/webp"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_IMAGE_SUFFIX_BY_MIME: dict[ParserImageMimeType, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


def _validate_identifier(value: str, label: str) -> str:
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def validate_artifact_path(value: str) -> str:
    """Require a normalized, relative POSIX path inside one artifact bundle."""
    if (
        not value
        or len(value) > 240
        or "\x00" in value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("artifact path must be a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("artifact path must be normalized and stay relative")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("artifact path contains an invalid segment")
    return value


class ParserLimits(BaseModel):
    """Source, artifact and runtime ceilings enforced by every provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    max_source_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    max_artifact_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    max_artifact_files: int = Field(default=2048, ge=2, le=100_000)
    max_image_count: int = Field(default=1024, ge=0, le=50_000)
    max_image_bytes: int = Field(default=64 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def _validate_related_limits(self) -> ParserLimits:
        if self.max_image_bytes > self.max_artifact_bytes:
            raise ValueError("max image bytes cannot exceed max artifact bytes")
        if self.max_image_count + 2 > self.max_artifact_files:
            raise ValueError("artifact file limit must include manifest and markdown")
        return self


class ParserSourceSpec(BaseModel):
    """Persistable source identity; the local staging path is passed separately."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: ParserMediaType = "application/pdf"
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return _validate_identifier(value, "source_id")

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        if (
            value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("display_name must be a safe basename")
        return value


class ParserJobSpec(BaseModel):
    """Fixed MVP request for Markdown plus embedded-image extraction.

    There is deliberately no arbitrary Marker configuration, LLM flag,
    network setting or output path.  The first contract supports only the
    audited CPU ``fast --disable_ocr`` behavior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal[1] = PARSER_CONTRACT_VERSION
    job_id: str
    source: ParserSourceSpec
    mode: ParserMode = "fast_no_ocr"
    output_schema: Literal["llm-wiki-parser-artifact/v1"] = PARSER_ARTIFACT_SCHEMA
    extract_embedded_images: Literal[True] = True
    limits: ParserLimits = Field(default_factory=ParserLimits)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return _validate_identifier(value, "job_id")

    @model_validator(mode="after")
    def _validate_source_limit(self) -> ParserJobSpec:
        if self.source.size_bytes > self.limits.max_source_bytes:
            raise ValueError("source size exceeds parser job limit")
        return self


class ParserCapabilities(BaseModel):
    """Stable feature report returned by ``probe``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_types: tuple[ParserMediaType, ...] = ("application/pdf",)
    modes: tuple[ParserMode, ...] = ("fast_no_ocr",)
    output_schemas: tuple[str, ...] = (PARSER_ARTIFACT_SCHEMA,)
    extracts_embedded_images: bool = True
    network_during_job: Literal[False] = False

    @field_validator("media_types", "modes", "output_schemas")
    @classmethod
    def _validate_unique_nonempty(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("capability values must be non-empty and unique")
        return values


class ParserProbe(BaseModel):
    """Credential- and path-free provider readiness snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ParserProviderName
    available: bool
    provider_version: str = Field(min_length=1, max_length=128)
    contract_version: Literal[1] = PARSER_CONTRACT_VERSION
    license_mode: ParserLicenseMode
    capabilities: ParserCapabilities = Field(default_factory=ParserCapabilities)
    observed_at_ms: int = Field(ge=0)
    error_code: ParserErrorCode | None = None

    @field_validator("provider_version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("provider_version contains invalid characters")
        return value

    @model_validator(mode="after")
    def _validate_availability(self) -> ParserProbe:
        if self.available and self.error_code is not None:
            raise ValueError("available probe cannot include an error code")
        if not self.available and self.error_code is None:
            raise ValueError("unavailable probe requires an error code")
        if self.provider == "marker_sidecar" and self.license_mode == "not_required":
            raise ValueError("Marker probe must report an explicit license mode")
        return self


class ParserJobHandle(BaseModel):
    """Persistable, source-content-free identity for one exact parser job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ParserProviderName
    provider_job_id: str = Field(min_length=1, max_length=256)
    job_id: str
    source_id: str
    created_at_ms: int = Field(ge=0)

    @field_validator("job_id", "source_id")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")

    @field_validator("provider_job_id")
    @classmethod
    def _validate_provider_job_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("provider_job_id contains invalid characters")
        return value


class ParserJobStatus(BaseModel):
    """Observed state with fixed errors and optional terminal artifact evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: ParserJobHandle
    state: ParserJobState
    observed_at_ms: int = Field(ge=0)
    started_at_ms: int | None = Field(default=None, ge=0)
    finished_at_ms: int | None = Field(default=None, ge=0)
    safe_error_code: ParserErrorCode | None = None
    artifact_size_bytes: int | None = Field(default=None, ge=1)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_state(self) -> ParserJobStatus:
        if (
            self.started_at_ms is not None
            and self.finished_at_ms is not None
            and self.finished_at_ms < self.started_at_ms
        ):
            raise ValueError("finished_at_ms must not precede started_at_ms")
        has_artifact = self.artifact_size_bytes is not None or self.artifact_sha256 is not None
        if self.state == "succeeded":
            if self.artifact_size_bytes is None or self.artifact_sha256 is None:
                raise ValueError("succeeded job requires complete artifact evidence")
            if self.finished_at_ms is None or self.safe_error_code is not None:
                raise ValueError("succeeded job requires a clean terminal timestamp")
        elif has_artifact:
            raise ValueError("non-succeeded job cannot expose artifact evidence")
        if self.state == "failed" and self.safe_error_code is None:
            raise ValueError("failed job requires a safe error code")
        if self.state == "cancelled" and self.safe_error_code != "cancelled":
            raise ValueError("cancelled job requires the cancelled error code")
        if self.state in {"queued", "running", "destroyed"} and self.safe_error_code is not None:
            raise ValueError("non-error state cannot include a safe error code")
        return self


class ParserArtifactFile(BaseModel):
    """One declared, content-addressed file inside the provider artifact tar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: ParserArtifactKind
    mime_type: str = Field(min_length=1, max_length=80)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_number: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_artifact_path(value)

    @model_validator(mode="after")
    def _validate_kind(self) -> ParserArtifactFile:
        if self.kind == "markdown":
            if self.path != "parsed.md" or self.mime_type != "text/markdown":
                raise ValueError("markdown artifact must be parsed.md with text/markdown")
            if self.page_number is not None:
                raise ValueError("markdown artifact cannot have a page number")
            return self
        if (
            not self.path.startswith("images/")
            or PurePosixPath(self.path).parent.as_posix() != "images"
        ):
            raise ValueError("embedded image must be directly inside images/")
        expected_suffix = _IMAGE_SUFFIX_BY_MIME.get(
            cast(ParserImageMimeType, self.mime_type)
        )
        if expected_suffix is None:
            raise ValueError("embedded image MIME type is not allowed")
        if not self.path.lower().endswith(expected_suffix):
            raise ValueError("embedded image suffix does not match its MIME type")
        return self


class ParserArtifactManifest(BaseModel):
    """Canonical provider output manifest embedded as ``manifest.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_id: Literal["llm-wiki-parser-artifact/v1"] = Field(
        default=PARSER_ARTIFACT_SCHEMA,
        alias="schema",
    )
    job_id: str
    source_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: ParserProviderName
    provider_version: str = Field(min_length=1, max_length=128)
    mode: ParserMode = "fast_no_ocr"
    page_count: int = Field(ge=0)
    markdown: ParserArtifactFile
    images: tuple[ParserArtifactFile, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("job_id", "source_id")
    @classmethod
    def _validate_manifest_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")

    @field_validator("warnings")
    @classmethod
    def _validate_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("manifest warnings must be unique")
        if any(
            not value
            or len(value) > 80
            or _IDENTIFIER_RE.fullmatch(value) is None
            for value in values
        ):
            raise ValueError("manifest warnings must be safe codes")
        return values

    @model_validator(mode="after")
    def _validate_files(self) -> ParserArtifactManifest:
        if self.markdown.kind != "markdown":
            raise ValueError("manifest markdown entry has the wrong kind")
        if any(image.kind != "embedded_image" for image in self.images):
            raise ValueError("manifest images must be embedded_image entries")
        paths = [self.markdown.path, *(image.path for image in self.images)]
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("manifest artifact paths must be unique")
        return self


class ParserArtifactReceipt(BaseModel):
    """Evidence for an atomically downloaded parser artifact tar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    source_id: str
    archive_format: Literal["tar"] = "tar"
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=2)

    @field_validator("job_id", "source_id")
    @classmethod
    def _validate_receipt_ids(cls, value: str) -> str:
        return _validate_identifier(value, "identifier")


__all__ = [
    "PARSER_ARTIFACT_SCHEMA",
    "PARSER_CONTRACT_VERSION",
    "ParserArtifactFile",
    "ParserArtifactKind",
    "ParserArtifactManifest",
    "ParserArtifactReceipt",
    "ParserCapabilities",
    "ParserImageMimeType",
    "ParserJobHandle",
    "ParserJobSpec",
    "ParserJobState",
    "ParserJobStatus",
    "ParserLicenseMode",
    "ParserLimits",
    "ParserMediaType",
    "ParserMode",
    "ParserProbe",
    "ParserProviderName",
    "ParserSourceSpec",
    "validate_artifact_path",
]

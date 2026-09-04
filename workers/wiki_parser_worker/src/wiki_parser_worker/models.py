"""Parser-neutral runtime models used inside the isolated MinerU Worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ParserName = Literal["mineru"]
ParserPreset = Literal[
    "mineru_pipeline",
    "mineru_gpu_medium",
    "mineru_gpu_high",
]
RequestedMode = Literal["pipeline", "gpu-medium", "gpu-high"]
RouteReason = Literal[
    "explicit_pipeline",
    "explicit_gpu_medium",
    "explicit_gpu_high",
]
QualityFailure = Literal[
    "missing_pages",
    "empty_content",
    "replacement_characters",
    "excessive_controls",
    "incomplete_content",
    "excessive_repetition",
    "markdown_invalid",
    "asset_reference_invalid",
]


@dataclass(frozen=True, slots=True)
class WorkerPreflightReport:
    source_id: str
    source_sha256: str
    page_count: int
    text_page_count: int
    image_dominant_page_count: int
    multicolumn_page_count: int
    table_candidate_page_count: int
    native_text_character_count: int
    text_page_ratio: float
    image_dominant_page_ratio: float
    complexity_score: float
    encrypted: bool
    has_javascript: bool
    has_embedded_files: bool
    duration_ms: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class WorkerRouteDecision:
    requested_mode: RequestedMode
    parser: ParserName
    preset: ParserPreset
    reasons: tuple[RouteReason, ...]
    backend: Literal["pipeline", "hybrid-engine"]
    effort: Literal["medium", "high"] | None


@dataclass(frozen=True, slots=True)
class WorkerParsedPage:
    page_number: int
    markdown: str
    plain_text: str


@dataclass(frozen=True, slots=True)
class WorkerParsedAsset:
    path: str
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    sha256: str
    size_bytes: int
    page_number: int
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkerParsedDocument:
    parser: ParserName
    parser_version: str
    preset: ParserPreset
    pages: tuple[WorkerParsedPage, ...]
    assets: tuple[WorkerParsedAsset, ...]


@dataclass(frozen=True, slots=True)
class WorkerQualityMetrics:
    input_page_count: int
    output_page_count: int
    pages_with_text: int
    output_character_count: int
    text_page_coverage: float
    replacement_char_ratio: float
    control_char_ratio: float
    content_completeness: float
    repetition_ratio: float
    page_count_match: bool
    markdown_health: float
    asset_reference_health: float


@dataclass(frozen=True, slots=True)
class WorkerQualityReport:
    metrics: WorkerQualityMetrics
    score: float
    pass_threshold: float
    passed: bool
    critical_failures: tuple[QualityFailure, ...]
    evaluated_at_ms: int


__all__ = [
    "ParserName",
    "ParserPreset",
    "QualityFailure",
    "RequestedMode",
    "RouteReason",
    "WorkerParsedAsset",
    "WorkerParsedDocument",
    "WorkerParsedPage",
    "WorkerPreflightReport",
    "WorkerQualityMetrics",
    "WorkerQualityReport",
    "WorkerRouteDecision",
]

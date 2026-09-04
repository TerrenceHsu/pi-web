"""Strict versioned routing and quality configuration.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import WorkerRuntimeError


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_job_seconds: int
    max_source_bytes: int
    max_artifact_bytes: int
    max_artifact_files: int
    max_pages: int
    max_embedded_images: int
    max_single_image_bytes: int
    max_total_image_bytes: int


@dataclass(frozen=True, slots=True)
class PreflightThresholds:
    min_text_characters_per_page: int


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    pass_threshold: float
    minimum_text_page_coverage: float
    maximum_replacement_character_ratio: float
    maximum_control_character_ratio: float
    minimum_content_completeness: float
    maximum_repetition_ratio: float
    minimum_markdown_health: float
    minimum_asset_reference_health: float
    text_page_coverage_weight: float
    content_completeness_weight: float
    repetition_weight: float
    markdown_health_weight: float
    asset_reference_health_weight: float


@dataclass(frozen=True, slots=True)
class WorkerRoutingConfig:
    schema_version: int
    revision: str
    sha256: str
    limits: WorkerLimits
    preflight: PreflightThresholds
    quality: QualityThresholds


def _object(value: object, label: str, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkerRuntimeError("invalid_configuration")
    if not all(isinstance(key, str) for key in value):
        raise WorkerRuntimeError("invalid_configuration")
    return cast(dict[str, object], value)


def _integer(value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise WorkerRuntimeError("invalid_configuration")
    return value


def _ratio(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise WorkerRuntimeError("invalid_configuration")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise WorkerRuntimeError("invalid_configuration")
    return result


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_routing_config(path: Path) -> WorkerRoutingConfig:
    """Load one immutable config and derive its canonical SHA-256 identity."""

    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeError("invalid_configuration") from error

    root = _object(
        payload,
        "root",
        {"schema_version", "revision", "limits", "preflight", "quality"},
    )
    schema_version = _integer(root["schema_version"], minimum=1)
    revision = root["revision"]
    if schema_version != 1 or not isinstance(revision, str) or not revision:
        raise WorkerRuntimeError("invalid_configuration")

    limits = _object(
        root["limits"],
        "limits",
        {
            "max_job_seconds",
            "max_source_bytes",
            "max_artifact_bytes",
            "max_artifact_files",
            "max_pages",
            "max_embedded_images",
            "max_single_image_bytes",
            "max_total_image_bytes",
        },
    )
    preflight = _object(
        root["preflight"],
        "preflight",
        {"min_text_characters_per_page"},
    )
    quality = _object(
        root["quality"],
        "quality",
        {
            "pass_threshold",
            "minimum_text_page_coverage",
            "maximum_replacement_character_ratio",
            "maximum_control_character_ratio",
            "minimum_content_completeness",
            "maximum_repetition_ratio",
            "minimum_markdown_health",
            "minimum_asset_reference_health",
            "weights",
        },
    )
    weights = _object(
        quality["weights"],
        "quality weights",
        {
            "text_page_coverage",
            "content_completeness",
            "repetition",
            "markdown_health",
            "asset_reference_health",
        },
    )

    quality_weights = tuple(_ratio(weights[key]) for key in weights)
    if abs(sum(quality_weights) - 1.0) > 1e-9:
        raise WorkerRuntimeError("invalid_configuration")

    parsed_limits = WorkerLimits(
        max_job_seconds=_integer(limits["max_job_seconds"], minimum=30),
        max_source_bytes=_integer(limits["max_source_bytes"], minimum=1),
        max_artifact_bytes=_integer(limits["max_artifact_bytes"], minimum=1),
        max_artifact_files=_integer(limits["max_artifact_files"], minimum=3),
        max_pages=_integer(limits["max_pages"], minimum=1),
        max_embedded_images=_integer(limits["max_embedded_images"]),
        max_single_image_bytes=_integer(limits["max_single_image_bytes"], minimum=1),
        max_total_image_bytes=_integer(limits["max_total_image_bytes"], minimum=1),
    )
    if (
        parsed_limits.max_single_image_bytes > parsed_limits.max_total_image_bytes
        or parsed_limits.max_total_image_bytes > parsed_limits.max_artifact_bytes
        or parsed_limits.max_embedded_images + 2 > parsed_limits.max_artifact_files
    ):
        raise WorkerRuntimeError("invalid_configuration")

    return WorkerRoutingConfig(
        schema_version=schema_version,
        revision=revision,
        sha256=hashlib.sha256(_canonical_bytes(root)).hexdigest(),
        limits=parsed_limits,
        preflight=PreflightThresholds(
            min_text_characters_per_page=_integer(
                preflight["min_text_characters_per_page"], minimum=1
            ),
        ),
        quality=QualityThresholds(
            pass_threshold=_ratio(quality["pass_threshold"]),
            minimum_text_page_coverage=_ratio(quality["minimum_text_page_coverage"]),
            maximum_replacement_character_ratio=_ratio(
                quality["maximum_replacement_character_ratio"]
            ),
            maximum_control_character_ratio=_ratio(
                quality["maximum_control_character_ratio"]
            ),
            minimum_content_completeness=_ratio(
                quality["minimum_content_completeness"]
            ),
            maximum_repetition_ratio=_ratio(quality["maximum_repetition_ratio"]),
            minimum_markdown_health=_ratio(quality["minimum_markdown_health"]),
            minimum_asset_reference_health=_ratio(
                quality["minimum_asset_reference_health"]
            ),
            text_page_coverage_weight=_ratio(weights["text_page_coverage"]),
            content_completeness_weight=_ratio(weights["content_completeness"]),
            repetition_weight=_ratio(weights["repetition"]),
            markdown_health_weight=_ratio(weights["markdown_health"]),
            asset_reference_health_weight=_ratio(weights["asset_reference_health"]),
        ),
    )


__all__ = [
    "PreflightThresholds",
    "QualityThresholds",
    "WorkerLimits",
    "WorkerRoutingConfig",
    "load_routing_config",
]

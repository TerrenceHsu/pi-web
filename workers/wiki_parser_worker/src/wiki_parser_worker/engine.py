"""Contract v2 orchestration inside the isolated parser runtime.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from .artifact import prepare_artifact_payload, write_artifact
from .config import WorkerRoutingConfig, load_routing_config
from .errors import WorkerErrorCode, WorkerRuntimeError
from .models import (
    RequestedMode,
    WorkerParsedDocument,
    WorkerPreflightReport,
    WorkerQualityReport,
    WorkerRouteDecision,
)
from .parsers import DoclingAccurateParser, PyMuPdf4LlmFastParser
from .preflight import inspect_pdf, route_pdf, verify_source_identity
from .protocol import (
    CANCEL_NAME,
    SOURCE_NAME,
    STATUS_NAME,
    QueueRequest,
    atomic_write_json,
    canonical_json_bytes,
    load_queue_request,
)
from .quality import QualityEvaluator


class FastParser(Protocol):
    def parse(
        self,
        path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
    ) -> WorkerParsedDocument: ...


class AccurateParser(Protocol):
    def parse(
        self,
        path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
        preset: Literal["docling_standard", "docling_ocr"],
    ) -> WorkerParsedDocument: ...


PreflightInspector = Callable[..., WorkerPreflightReport]
Router = Callable[
    [RequestedMode, WorkerPreflightReport, WorkerRoutingConfig], WorkerRouteDecision
]


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    timeout_seconds: int
    max_source_bytes: int
    max_artifact_bytes: int
    max_artifact_files: int
    max_image_count: int
    max_image_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeJobSpec:
    job_id: str
    source_id: str
    source_size_bytes: int
    source_sha256: str
    requested_mode: RequestedMode
    routing_revision: str
    routing_sha256: str
    limits: RuntimeLimits


class _Cancelled(RuntimeError):
    pass


def _exact(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkerRuntimeError("invalid_source")
    return cast(dict[str, object], value)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkerRuntimeError("invalid_source")
    return value


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkerRuntimeError("invalid_source")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise WorkerRuntimeError("invalid_source")
    if not value[0].isalnum() or any(not (char.isalnum() or char in "_-") for char in value):
        raise WorkerRuntimeError("invalid_source")
    return value


def _parse_spec(
    request: QueueRequest,
    config: WorkerRoutingConfig,
) -> RuntimeJobSpec:
    spec = _exact(
        request.spec,
        {
            "contract_version",
            "job_id",
            "limits",
            "output_schema",
            "requested_mode",
            "routing_config",
            "source",
        },
    )
    source = _exact(
        spec["source"],
        {"display_name", "mime_type", "sha256", "size_bytes", "source_id"},
    )
    routing = _exact(spec["routing_config"], {"revision", "schema_version", "sha256"})
    limits = _exact(
        spec["limits"],
        {
            "max_artifact_bytes",
            "max_artifact_files",
            "max_image_bytes",
            "max_image_count",
            "max_source_bytes",
            "timeout_seconds",
        },
    )
    requested_mode = spec["requested_mode"]
    digest = source["sha256"]
    if (
        spec["contract_version"] != 2
        or spec["output_schema"] != "llm-wiki-parser-artifact/v2"
        or source["mime_type"] != "application/pdf"
        or requested_mode not in {"auto", "fast", "accurate"}
        or routing["schema_version"] != 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise WorkerRuntimeError("invalid_source")
    job_id = _identifier(spec["job_id"])
    source_id = _identifier(source["source_id"])
    if request.handle["job_id"] != job_id or request.handle["source_id"] != source_id:
        raise WorkerRuntimeError("invalid_source")
    revision = routing["revision"]
    routing_sha = routing["sha256"]
    if not isinstance(revision, str) or not isinstance(routing_sha, str):
        raise WorkerRuntimeError("invalid_source")
    parsed_limits = RuntimeLimits(
        timeout_seconds=_positive_int(limits["timeout_seconds"]),
        max_source_bytes=_positive_int(limits["max_source_bytes"]),
        max_artifact_bytes=_positive_int(limits["max_artifact_bytes"]),
        max_artifact_files=_positive_int(limits["max_artifact_files"]),
        max_image_count=_nonnegative_int(limits["max_image_count"]),
        max_image_bytes=_positive_int(limits["max_image_bytes"]),
    )
    size = _positive_int(source["size_bytes"])
    if size > parsed_limits.max_source_bytes:
        raise WorkerRuntimeError("artifact_limit_exceeded")
    hard_limits = config.limits
    if (
        parsed_limits.timeout_seconds > hard_limits.max_job_seconds
        or parsed_limits.max_source_bytes > hard_limits.max_source_bytes
        or parsed_limits.max_artifact_bytes > hard_limits.max_artifact_bytes
        or parsed_limits.max_artifact_files > hard_limits.max_artifact_files
        or parsed_limits.max_image_count > hard_limits.max_embedded_images
        or parsed_limits.max_image_bytes > hard_limits.max_total_image_bytes
    ):
        raise WorkerRuntimeError("artifact_limit_exceeded")
    return RuntimeJobSpec(
        job_id=job_id,
        source_id=source_id,
        source_size_bytes=size,
        source_sha256=digest,
        requested_mode=cast(RequestedMode, requested_mode),
        routing_revision=revision,
        routing_sha256=routing_sha,
        limits=parsed_limits,
    )


def _preflight_dict(report: WorkerPreflightReport) -> dict[str, object]:
    return {
        "duration_ms": report.duration_ms,
        "encrypted": False,
        "has_embedded_files": report.has_embedded_files,
        "has_javascript": report.has_javascript,
        "image_dominant_page_count": report.image_dominant_page_count,
        "image_dominant_page_ratio": report.image_dominant_page_ratio,
        "multicolumn_page_count": report.multicolumn_page_count,
        "observed_at_ms": report.observed_at_ms,
        "page_count": report.page_count,
        "source_id": report.source_id,
        "source_sha256": report.source_sha256,
        "table_candidate_page_count": report.table_candidate_page_count,
        "text_page_count": report.text_page_count,
        "text_page_ratio": report.text_page_ratio,
        "complexity_score": report.complexity_score,
    }


def _routing_identity(spec: RuntimeJobSpec) -> dict[str, object]:
    return {
        "revision": spec.routing_revision,
        "schema_version": 1,
        "sha256": spec.routing_sha256,
    }


def _route_dict(
    route: WorkerRouteDecision,
    preflight: dict[str, object],
) -> dict[str, object]:
    return {
        "fallback_parser": route.fallback_parser,
        "fallback_preset": route.fallback_preset,
        "initial_parser": route.parser,
        "initial_preset": route.preset,
        "preflight_sha256": hashlib.sha256(canonical_json_bytes(preflight)).hexdigest(),
        "reasons": list(route.reasons),
        "requested_mode": route.requested_mode,
    }


def _quality_dict(
    report: WorkerQualityReport,
    document: WorkerParsedDocument,
    spec: RuntimeJobSpec,
) -> dict[str, object]:
    metrics = report.metrics
    return {
        "critical_failures": list(report.critical_failures),
        "evaluated_at_ms": report.evaluated_at_ms,
        "metrics": {
            "asset_reference_health": metrics.asset_reference_health,
            "content_completeness": metrics.content_completeness,
            "control_char_ratio": metrics.control_char_ratio,
            "input_page_count": metrics.input_page_count,
            "markdown_health": metrics.markdown_health,
            "output_character_count": metrics.output_character_count,
            "output_page_count": metrics.output_page_count,
            "page_count_match": metrics.page_count_match,
            "pages_with_text": metrics.pages_with_text,
            "repetition_ratio": metrics.repetition_ratio,
            "replacement_char_ratio": metrics.replacement_char_ratio,
            "text_page_coverage": metrics.text_page_coverage,
        },
        "parser": document.parser,
        "parser_version": document.parser_version,
        "pass_threshold": report.pass_threshold,
        "passed": report.passed,
        "preset": document.preset,
        "routing_config": _routing_identity(spec),
        "score": report.score,
    }


def _attempt(
    *,
    attempt_id: str,
    ordinal: int,
    spec: RuntimeJobSpec,
    parser: str,
    parser_version: str,
    preset: str,
    route_reasons: tuple[str, ...],
    state: str,
    started_at_ms: int,
    finished_at_ms: int | None = None,
    safe_error_code: str | None = None,
    quality_report: dict[str, object] | None = None,
    output_size_bytes: int | None = None,
    output_sha256: str | None = None,
    fallback_from_attempt_id: str | None = None,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "duration_ms": (
            finished_at_ms - started_at_ms if finished_at_ms is not None else None
        ),
        "fallback_from_attempt_id": fallback_from_attempt_id,
        "finished_at_ms": finished_at_ms,
        "ordinal": ordinal,
        "output_sha256": output_sha256,
        "output_size_bytes": output_size_bytes,
        "parser": parser,
        "parser_version": parser_version,
        "preset": preset,
        "quality_report": quality_report,
        "route_reasons": list(route_reasons),
        "routing_config": _routing_identity(spec),
        "safe_error_code": safe_error_code,
        "source_sha256": spec.source_sha256,
        "started_at_ms": started_at_ms,
        "state": state,
    }


def _map_error(code: WorkerErrorCode, phase: str) -> str:
    if code in {"dependency_unavailable", "dependency_version_mismatch"}:
        return "provider_unavailable"
    if code == "invalid_configuration":
        return "invalid_configuration"
    if code in {"source_too_complex", "artifact_limit_exceeded"}:
        return "resource_limit"
    if code == "quality_rejected":
        return "quality_rejected"
    if code in {"invalid_source", "source_changed"}:
        return "invalid_source"
    if code == "unsafe_source" or phase in {"preflight", "routing"}:
        return "preflight_failed"
    return "parsing_failed"


class DualPdfJobEngine:
    """Run exactly one routed job while reusing startup-created parsers."""

    def __init__(
        self,
        *,
        config: WorkerRoutingConfig,
        fast_parser: FastParser,
        accurate_parser: AccurateParser,
        quality_evaluator: QualityEvaluator,
        preflight_inspector: PreflightInspector = inspect_pdf,
        router: Router = route_pdf,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._fast = fast_parser
        self._accurate = accurate_parser
        self._quality = quality_evaluator
        self._inspect = preflight_inspector
        self._route = router
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    @property
    def config(self) -> WorkerRoutingConfig:
        return self._config

    def _write_status(
        self,
        job_dir: Path,
        request: QueueRequest,
        *,
        state: str,
        phase: str,
        started_at_ms: int | None,
        finished_at_ms: int | None = None,
        current_attempt_ordinal: int | None = None,
        route_decision: dict[str, object] | None = None,
        attempts: list[dict[str, object]] | None = None,
        safe_error_code: str | None = None,
        receipt: dict[str, object] | None = None,
    ) -> None:
        atomic_write_json(
            job_dir / STATUS_NAME,
            {
                "artifact_sha256": receipt["sha256"] if receipt else None,
                "artifact_size_bytes": receipt["size_bytes"] if receipt else None,
                "attempts": attempts or [],
                "current_attempt_ordinal": current_attempt_ordinal,
                "finished_at_ms": finished_at_ms,
                "handle": request.handle,
                "observed_at_ms": self._clock_ms(),
                "phase": phase,
                "route_decision": route_decision,
                "safe_error_code": safe_error_code,
                "started_at_ms": started_at_ms,
                "state": state,
            },
        )

    def _check_cancelled(self, job_dir: Path) -> None:
        if (job_dir / CANCEL_NAME).exists():
            raise _Cancelled

    def execute(self, job_dir: Path) -> None:
        request = load_queue_request(job_dir)
        started = self._clock_ms()
        attempts: list[dict[str, object]] = []
        route_payload: dict[str, object] | None = None
        phase = "preflight"
        self._write_status(
            job_dir,
            request,
            state="running",
            phase=phase,
            started_at_ms=started,
        )
        try:
            spec = _parse_spec(request, self._config)
            if (
                spec.routing_revision != self._config.revision
                or spec.routing_sha256 != self._config.sha256
            ):
                raise WorkerRuntimeError("invalid_configuration")
            source_path = job_dir / SOURCE_NAME
            identity = verify_source_identity(source_path, spec.source_sha256)
            if identity[2] != spec.source_size_bytes:
                raise WorkerRuntimeError("invalid_source")
            self._check_cancelled(job_dir)
            preflight = self._inspect(
                source_path,
                source_id=spec.source_id,
                expected_sha256=spec.source_sha256,
                config=self._config,
            )
            preflight_payload = _preflight_dict(preflight)
            phase = "routing"
            self._write_status(
                job_dir,
                request,
                state="running",
                phase=phase,
                started_at_ms=started,
            )
            route = self._route(spec.requested_mode, preflight, self._config)
            route_payload = _route_dict(route, preflight_payload)
            self._check_cancelled(job_dir)

            first_id = f"{request.provider_job_id}-a1"
            attempt_started = self._clock_ms()
            attempts.append(
                _attempt(
                    attempt_id=first_id,
                    ordinal=1,
                    spec=spec,
                    parser=route.parser,
                    parser_version=("1.28.2" if route.parser == "pymupdf4llm" else "2.119.0"),
                    preset=route.preset,
                    route_reasons=cast(tuple[str, ...], route.reasons),
                    state="running",
                    started_at_ms=attempt_started,
                )
            )
            phase = "fast_parse" if route.parser == "pymupdf4llm" else "accurate_parse"
            self._write_status(
                job_dir,
                request,
                state="running",
                phase=phase,
                started_at_ms=started,
                current_attempt_ordinal=1,
                route_decision=route_payload,
                attempts=attempts,
            )
            document = (
                self._fast.parse(
                    source_path,
                    expected_sha256=spec.source_sha256,
                    preflight=preflight,
                )
                if route.parser == "pymupdf4llm"
                else self._accurate.parse(
                    source_path,
                    expected_sha256=spec.source_sha256,
                    preflight=preflight,
                    preset=cast(
                        Literal["docling_standard", "docling_ocr"], route.preset
                    ),
                )
            )
            self._check_cancelled(job_dir)
            phase = "quality_check"
            quality = self._quality.evaluate(document, preflight)
            quality_payload = _quality_dict(quality, document, spec)

            if not quality.passed:
                rejected_at = self._clock_ms()
                attempts[-1] = _attempt(
                    attempt_id=first_id,
                    ordinal=1,
                    spec=spec,
                    parser=document.parser,
                    parser_version=document.parser_version,
                    preset=document.preset,
                    route_reasons=cast(tuple[str, ...], route.reasons),
                    state="quality_rejected",
                    started_at_ms=attempt_started,
                    finished_at_ms=rejected_at,
                    safe_error_code="quality_rejected",
                    quality_report=quality_payload,
                )
                if route.fallback_parser != "docling" or route.fallback_preset is None:
                    raise WorkerRuntimeError("quality_rejected")
                self._check_cancelled(job_dir)
                phase = "fallback"
                second_id = f"{request.provider_job_id}-a2"
                second_started = self._clock_ms()
                attempts.append(
                    _attempt(
                        attempt_id=second_id,
                        ordinal=2,
                        spec=spec,
                        parser="docling",
                        parser_version="2.119.0",
                        preset=route.fallback_preset,
                        route_reasons=("fast_quality_fallback",),
                        state="running",
                        started_at_ms=second_started,
                        fallback_from_attempt_id=first_id,
                    )
                )
                self._write_status(
                    job_dir,
                    request,
                    state="running",
                    phase=phase,
                    started_at_ms=started,
                    current_attempt_ordinal=2,
                    route_decision=route_payload,
                    attempts=attempts,
                )
                document = self._accurate.parse(
                    source_path,
                    expected_sha256=spec.source_sha256,
                    preflight=preflight,
                    preset=route.fallback_preset,
                )
                self._check_cancelled(job_dir)
                quality = self._quality.evaluate(document, preflight)
                quality_payload = _quality_dict(quality, document, spec)
                if not quality.passed:
                    rejected_at = self._clock_ms()
                    attempts[-1] = _attempt(
                        attempt_id=second_id,
                        ordinal=2,
                        spec=spec,
                        parser=document.parser,
                        parser_version=document.parser_version,
                        preset=document.preset,
                        route_reasons=("fast_quality_fallback",),
                        state="quality_rejected",
                        started_at_ms=second_started,
                        finished_at_ms=rejected_at,
                        safe_error_code="quality_rejected",
                        quality_report=quality_payload,
                        fallback_from_attempt_id=first_id,
                    )
                    raise WorkerRuntimeError("quality_rejected")
                selected_reasons: tuple[str, ...]
                selected_id = second_id
                selected_started = second_started
                selected_reasons = ("fast_quality_fallback",)
                fallback_from = first_id
            else:
                selected_id = first_id
                selected_started = attempt_started
                selected_reasons = cast(tuple[str, ...], route.reasons)
                fallback_from = None

            phase = "packaging"
            payload = prepare_artifact_payload(
                document,
                max_files=spec.limits.max_artifact_files,
                max_image_count=spec.limits.max_image_count,
                max_image_bytes=spec.limits.max_image_bytes,
            )
            finished_attempt = self._clock_ms()
            attempts[-1] = _attempt(
                attempt_id=selected_id,
                ordinal=len(attempts),
                spec=spec,
                parser=document.parser,
                parser_version=document.parser_version,
                preset=document.preset,
                route_reasons=selected_reasons,
                state="succeeded",
                started_at_ms=selected_started,
                finished_at_ms=finished_attempt,
                quality_report=quality_payload,
                output_size_bytes=payload.output_size_bytes,
                output_sha256=payload.output_sha256,
                fallback_from_attempt_id=fallback_from,
            )
            manifest: dict[str, object] = {
                "assets": [item.manifest_dict() for item in payload.assets],
                "attempts": attempts,
                "contract_version": 2,
                "job_id": spec.job_id,
                "markdown": payload.markdown.manifest_dict(),
                "page_count": len(document.pages),
                "pages": [item.manifest_dict() for item in payload.pages],
                "parser": document.parser,
                "parser_version": document.parser_version,
                "preflight": preflight_payload,
                "preset": document.preset,
                "quality_report": quality_payload,
                "requested_mode": spec.requested_mode,
                "route_decision": route_payload,
                "routing_config": _routing_identity(spec),
                "schema": "llm-wiki-parser-artifact/v2",
                "selected_attempt_id": selected_id,
                "source_id": spec.source_id,
                "source_sha256": spec.source_sha256,
                "warnings": [],
            }
            receipt = write_artifact(
                job_dir,
                job_id=spec.job_id,
                source_id=spec.source_id,
                manifest=manifest,
                payload=payload,
                max_artifact_bytes=spec.limits.max_artifact_bytes,
            )
            finished = self._clock_ms()
            self._write_status(
                job_dir,
                request,
                state="succeeded",
                phase="terminal",
                started_at_ms=started,
                finished_at_ms=finished,
                current_attempt_ordinal=len(attempts),
                route_decision=route_payload,
                attempts=attempts,
                receipt=receipt,
            )
        except _Cancelled:
            finished = self._clock_ms()
            if attempts and attempts[-1]["state"] == "running":
                attempts[-1] = {
                    **attempts[-1],
                    "duration_ms": finished - cast(int, attempts[-1]["started_at_ms"]),
                    "finished_at_ms": finished,
                    "safe_error_code": "cancelled",
                    "state": "cancelled",
                }
            self._write_status(
                job_dir,
                request,
                state="cancelled",
                phase="terminal",
                started_at_ms=started,
                finished_at_ms=finished,
                current_attempt_ordinal=len(attempts) or None,
                route_decision=route_payload,
                attempts=attempts,
                safe_error_code="cancelled",
            )
        except WorkerRuntimeError as error:
            finished = self._clock_ms()
            safe_code = _map_error(error.code, phase)
            if attempts and attempts[-1]["state"] == "running":
                attempts[-1] = {
                    **attempts[-1],
                    "duration_ms": finished - cast(int, attempts[-1]["started_at_ms"]),
                    "finished_at_ms": finished,
                    "safe_error_code": safe_code,
                    "state": "failed",
                }
            self._write_status(
                job_dir,
                request,
                state="failed",
                phase="terminal",
                started_at_ms=started,
                finished_at_ms=finished,
                current_attempt_ordinal=len(attempts) or None,
                route_decision=route_payload,
                attempts=attempts,
                safe_error_code=safe_code,
            )


def create_runtime_engine(
    *,
    config_path: Path,
    model_root: Path,
) -> DualPdfJobEngine:
    config = load_routing_config(config_path)
    return DualPdfJobEngine(
        config=config,
        fast_parser=PyMuPdf4LlmFastParser(config=config),
        accurate_parser=DoclingAccurateParser(config=config, model_root=model_root),
        quality_evaluator=QualityEvaluator(config),
    )


__all__ = [
    "AccurateParser",
    "DualPdfJobEngine",
    "FastParser",
    "RuntimeJobSpec",
    "RuntimeLimits",
    "create_runtime_engine",
]

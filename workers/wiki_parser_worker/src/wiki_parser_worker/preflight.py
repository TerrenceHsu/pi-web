"""PyMuPDF-only source verification, preflight, and deterministic routing.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import importlib
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from .config import WorkerRoutingConfig
from .errors import WorkerRuntimeError
from .models import (
    RequestedMode,
    RouteReason,
    WorkerPreflightReport,
    WorkerRouteDecision,
)

_EXPECTED_PYMUPDF_VERSION = "1.28.2"


def verify_source_identity(path: Path, expected_sha256: str) -> tuple[int, int, int, int]:
    """Verify an ordinary immutable source and return its stable file identity."""

    try:
        before = os.lstat(path)
    except OSError as error:
        raise WorkerRuntimeError("invalid_source") from error
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise WorkerRuntimeError("invalid_source")

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise WorkerRuntimeError("invalid_source")
            source.seek(0)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.lstat(path)
    except OSError as error:
        raise WorkerRuntimeError("source_changed") from error

    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != after_identity or digest.hexdigest() != expected_sha256:
        raise WorkerRuntimeError("source_changed")
    return identity


def _load_pymupdf() -> Any:
    try:
        module = importlib.import_module("pymupdf")
    except ImportError as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    version = getattr(module, "__version__", None)
    if version != _EXPECTED_PYMUPDF_VERSION:
        raise WorkerRuntimeError("dependency_version_mismatch")
    return module


def _rect_values(rect: object) -> tuple[float, float, float, float] | None:
    attributes = [getattr(rect, name, None) for name in ("x0", "y0", "x1", "y1")]
    if all(isinstance(value, int | float) for value in attributes):
        numeric = cast(list[int | float], attributes)
        return cast(
            tuple[float, float, float, float],
            tuple(float(value) for value in numeric),
        )
    if isinstance(rect, list | tuple) and len(rect) >= 4:
        values = rect[:4]
        if all(isinstance(value, int | float) for value in values):
            return cast(tuple[float, float, float, float], tuple(float(v) for v in values))
    return None


def _page_size(page: Any) -> tuple[float, float]:
    values = _rect_values(page.rect)
    if values is None:
        raise WorkerRuntimeError("parser_failed")
    x0, y0, x1, y1 = values
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        raise WorkerRuntimeError("parser_failed")
    return width, height


def _text_blocks(page: Any) -> list[tuple[float, float, float, float, str]]:
    raw = page.get_text("blocks")
    if not isinstance(raw, list | tuple):
        return []
    blocks: list[tuple[float, float, float, float, str]] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) < 5:
            continue
        coords = _rect_values(item)
        text = item[4]
        if coords is None or not isinstance(text, str) or not text.strip():
            continue
        blocks.append((*coords, text))
    return blocks


def _is_multicolumn(
    blocks: list[tuple[float, float, float, float, str]],
    width: float,
    config: WorkerRoutingConfig,
) -> bool:
    minimum = config.preflight.multicolumn_min_text_blocks
    candidates = [block for block in blocks if len(block[4].strip()) >= 8]
    if len(candidates) < minimum:
        return False
    midpoint = width / 2.0
    gap = width * config.preflight.multicolumn_min_column_gap_ratio / 2.0
    left = [block for block in candidates if block[2] <= midpoint - gap]
    right = [block for block in candidates if block[0] >= midpoint + gap]
    return len(left) >= minimum // 2 and len(right) >= minimum // 2


def _image_area_ratio(page: Any, width: float, height: float) -> float:
    raw_images = page.get_images(full=True)
    if not isinstance(raw_images, list | tuple):
        return 0.0
    area = 0.0
    seen: set[int] = set()
    for item in raw_images:
        if not isinstance(item, list | tuple) or not item or not isinstance(item[0], int):
            continue
        xref = item[0]
        if xref <= 0 or xref in seen:
            continue
        seen.add(xref)
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        if not isinstance(rects, list | tuple):
            continue
        for rect in rects:
            values = _rect_values(rect)
            if values is None:
                continue
            x0, y0, x1, y1 = values
            area += max(0.0, min(width, x1) - max(0.0, x0)) * max(
                0.0, min(height, y1) - max(0.0, y0)
            )
    return min(area / (width * height), 1.0)


def _has_javascript(document: Any, maximum_xrefs: int) -> bool:
    length = document.xref_length()
    if not isinstance(length, int) or length < 1 or length > maximum_xrefs:
        raise WorkerRuntimeError("source_too_complex")
    for xref in range(1, length):
        try:
            value = document.xref_object(xref, compressed=False)
        except Exception:
            continue
        if isinstance(value, str) and ("/JavaScript" in value or "/JS" in value):
            return True
    return False


def inspect_pdf(
    path: Path,
    *,
    source_id: str,
    expected_sha256: str,
    config: WorkerRoutingConfig,
    pymupdf_module: object | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> WorkerPreflightReport:
    """Inspect the original PDF without OCR, layout models, or network access."""

    identity = verify_source_identity(path, expected_sha256)
    module = pymupdf_module if pymupdf_module is not None else _load_pymupdf()
    if (
        pymupdf_module is not None
        and getattr(module, "__version__", None) != _EXPECTED_PYMUPDF_VERSION
    ):
        raise WorkerRuntimeError("dependency_version_mismatch")
    now = clock_ms or (lambda: time.time_ns() // 1_000_000)
    started = now()
    try:
        document = cast(Any, module).open(str(path))
    except Exception as error:
        raise WorkerRuntimeError("invalid_source") from error

    try:
        page_count = document.page_count
        if not isinstance(page_count, int) or page_count < 1:
            raise WorkerRuntimeError("invalid_source")
        if page_count > config.limits.max_pages:
            raise WorkerRuntimeError("source_too_complex")
        encrypted = bool(getattr(document, "needs_pass", False)) or bool(
            getattr(document, "is_encrypted", False)
        )
        has_javascript = _has_javascript(document, config.limits.max_xref_objects)
        embedded_count = document.embfile_count()
        has_embedded_files = isinstance(embedded_count, int) and embedded_count > 0
        if encrypted or has_javascript or has_embedded_files:
            raise WorkerRuntimeError("unsafe_source")

        text_pages = image_pages = multicolumn_pages = table_pages = native_chars = 0
        for page_index in range(page_count):
            page = document.load_page(page_index)
            width, height = _page_size(page)
            raw_text = page.get_text("text")
            text = raw_text if isinstance(raw_text, str) else ""
            character_count = len("".join(text.split()))
            native_chars += character_count
            if character_count >= config.preflight.min_text_characters_per_page:
                text_pages += 1
            blocks = _text_blocks(page)
            if _is_multicolumn(blocks, width, config):
                multicolumn_pages += 1
            if (
                _image_area_ratio(page, width, height)
                >= config.preflight.image_dominant_page_area_ratio
            ):
                image_pages += 1
            drawings = page.get_drawings()
            if not isinstance(drawings, list | tuple):
                drawings = ()
            if len(drawings) > config.limits.max_drawings_per_page:
                raise WorkerRuntimeError("source_too_complex")
            if len(drawings) >= config.preflight.table_candidate_min_drawings:
                table_pages += 1
    except WorkerRuntimeError:
        raise
    except Exception as error:
        raise WorkerRuntimeError("parser_failed") from error
    finally:
        try:
            document.close()
        except Exception:
            pass

    if verify_source_identity(path, expected_sha256) != identity:
        raise WorkerRuntimeError("source_changed")
    observed = now()
    image_ratio = image_pages / page_count
    multicolumn_ratio = multicolumn_pages / page_count
    table_ratio = table_pages / page_count
    complexity = min(
        1.0,
        image_ratio * config.preflight.image_weight
        + multicolumn_ratio * config.preflight.multicolumn_weight
        + table_ratio * config.preflight.table_weight,
    )
    return WorkerPreflightReport(
        source_id=source_id,
        source_sha256=expected_sha256,
        page_count=page_count,
        text_page_count=text_pages,
        image_dominant_page_count=image_pages,
        multicolumn_page_count=multicolumn_pages,
        table_candidate_page_count=table_pages,
        native_text_character_count=native_chars,
        text_page_ratio=text_pages / page_count,
        image_dominant_page_ratio=image_ratio,
        complexity_score=complexity,
        encrypted=False,
        has_javascript=False,
        has_embedded_files=False,
        duration_ms=max(0, observed - started),
        observed_at_ms=observed,
    )


def route_pdf(
    requested_mode: RequestedMode,
    report: WorkerPreflightReport,
    config: WorkerRoutingConfig,
) -> WorkerRouteDecision:
    """Return one deterministic initial route; only auto fast has a fallback."""

    scanned = (
        report.text_page_count == 0
        or report.text_page_ratio < config.preflight.scan_max_text_page_ratio
    )
    image_dominant = (
        report.image_dominant_page_ratio
        >= config.preflight.direct_docling_image_page_ratio
    )
    if requested_mode == "fast":
        return WorkerRouteDecision(
            requested_mode="fast",
            parser="pymupdf4llm",
            preset="pymupdf4llm_fast_no_ocr",
            reasons=("explicit_fast",),
        )
    if requested_mode == "accurate":
        return WorkerRouteDecision(
            requested_mode="accurate",
            parser="docling",
            preset="docling_ocr" if scanned or image_dominant else "docling_standard",
            reasons=("explicit_accurate",),
        )
    if scanned:
        return WorkerRouteDecision(
            requested_mode="auto",
            parser="docling",
            preset="docling_ocr",
            reasons=("scan_text_layer_missing",),
        )
    if image_dominant:
        return WorkerRouteDecision(
            requested_mode="auto",
            parser="docling",
            preset="docling_ocr",
            reasons=("scan_image_dominant",),
        )

    page_count = report.page_count
    reasons: list[RouteReason] = []
    if (
        report.multicolumn_page_count / page_count
        >= config.preflight.direct_docling_multicolumn_page_ratio
    ):
        reasons.append("complex_multicolumn")
    if (
        report.table_candidate_page_count / page_count
        >= config.preflight.direct_docling_table_page_ratio
    ):
        reasons.append("complex_table_dense")
    if len(reasons) > 1:
        reasons.append("complex_mixed_layout")
    if reasons:
        return WorkerRouteDecision(
            requested_mode="auto",
            parser="docling",
            preset="docling_standard",
            reasons=tuple(reasons),
        )
    return WorkerRouteDecision(
        requested_mode="auto",
        parser="pymupdf4llm",
        preset="pymupdf4llm_fast_no_ocr",
        reasons=("simple_digital",),
        fallback_parser="docling",
        fallback_preset="docling_standard",
    )


__all__ = ["inspect_pdf", "route_pdf", "verify_source_identity"]

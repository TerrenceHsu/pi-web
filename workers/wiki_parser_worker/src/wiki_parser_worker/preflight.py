"""Source verification, safe PDF preflight, and MinerU preset mapping."""

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
from .models import RequestedMode, WorkerPreflightReport, WorkerRouteDecision


def verify_source_identity(path: Path, expected_sha256: str) -> tuple[int, int, int, int]:
    """Verify an ordinary immutable PDF and return its stable file identity."""

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


def _load_pypdf() -> Any:
    try:
        return importlib.import_module("pypdf")
    except ImportError as error:
        raise WorkerRuntimeError("dependency_unavailable") from error


def _catalog_flag(reader: Any, key: str) -> bool:
    try:
        root = reader.trailer["/Root"]
        if hasattr(root, "get_object"):
            root = root.get_object()
        if key in root:
            return True
        names = root.get("/Names")
        if hasattr(names, "get_object"):
            names = names.get_object()
        return isinstance(names, dict) and key in names
    except Exception:
        return False


def inspect_pdf(
    path: Path,
    *,
    source_id: str,
    expected_sha256: str,
    config: WorkerRoutingConfig,
    pypdf_module: object | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> WorkerPreflightReport:
    """Inspect a PDF with MinerU's lightweight PDF dependency before model work."""

    identity = verify_source_identity(path, expected_sha256)
    module = pypdf_module if pypdf_module is not None else _load_pypdf()
    now = clock_ms or (lambda: time.time_ns() // 1_000_000)
    started = now()
    try:
        reader = cast(Any, module).PdfReader(str(path), strict=True)
        encrypted = bool(reader.is_encrypted)
        if encrypted:
            raise WorkerRuntimeError("unsafe_source")
        page_count = len(reader.pages)
        if page_count < 1:
            raise WorkerRuntimeError("invalid_source")
        if page_count > config.limits.max_pages:
            raise WorkerRuntimeError("source_too_complex")
        has_javascript = _catalog_flag(reader, "/JavaScript") or _catalog_flag(
            reader, "/OpenAction"
        )
        has_embedded_files = _catalog_flag(reader, "/EmbeddedFiles")
        if has_javascript or has_embedded_files:
            raise WorkerRuntimeError("unsafe_source")

        text_pages = 0
        image_pages = 0
        native_chars = 0
        for page in reader.pages:
            raw_text = page.extract_text()
            text = raw_text if isinstance(raw_text, str) else ""
            character_count = len("".join(text.split()))
            native_chars += character_count
            if character_count >= config.preflight.min_text_characters_per_page:
                text_pages += 1
            images = getattr(page, "images", ())
            if character_count == 0 and bool(images):
                image_pages += 1
    except WorkerRuntimeError:
        raise
    except Exception as error:
        raise WorkerRuntimeError("invalid_source") from error

    if verify_source_identity(path, expected_sha256) != identity:
        raise WorkerRuntimeError("source_changed")
    observed = now()
    image_ratio = image_pages / page_count
    return WorkerPreflightReport(
        source_id=source_id,
        source_sha256=expected_sha256,
        page_count=page_count,
        text_page_count=text_pages,
        image_dominant_page_count=image_pages,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        native_text_character_count=native_chars,
        text_page_ratio=text_pages / page_count,
        image_dominant_page_ratio=image_ratio,
        complexity_score=image_ratio,
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
    """Map a public product preset to fixed MinerU backend arguments."""

    del report, config
    if requested_mode == "pipeline":
        return WorkerRouteDecision(
            requested_mode=requested_mode,
            parser="mineru",
            preset="mineru_pipeline",
            reasons=("explicit_pipeline",),
            backend="pipeline",
            effort=None,
        )
    if requested_mode == "gpu-medium":
        return WorkerRouteDecision(
            requested_mode=requested_mode,
            parser="mineru",
            preset="mineru_gpu_medium",
            reasons=("explicit_gpu_medium",),
            backend="hybrid-engine",
            effort="medium",
        )
    return WorkerRouteDecision(
        requested_mode=requested_mode,
        parser="mineru",
        preset="mineru_gpu_high",
        reasons=("explicit_gpu_high",),
        backend="hybrid-engine",
        effort="high",
    )


__all__ = ["inspect_pdf", "route_pdf", "verify_source_identity"]

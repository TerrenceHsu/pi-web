"""Runtime manifest, dependency-version, and model-artifact gates.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import cast

from .config import WorkerRoutingConfig
from .errors import WorkerRuntimeError

_MODEL_ROOTS = {
    "docling-layout-heron": "docling-project--docling-layout-heron",
    "tableformer-accurate": "docling-project--docling-models",
    "rapidocr-ppocrv6-multilingual-onnx": "RapidOcr",
}


def _manifest_path() -> Path:
    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "compliance_assets" / "runtime-manifest.json",
        package_root.parents[1] / "runtime-manifest.json",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise WorkerRuntimeError("invalid_configuration")


def load_runtime_manifest() -> dict[str, object]:
    try:
        payload = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeError("invalid_configuration") from error
    if not isinstance(payload, dict):
        raise WorkerRuntimeError("invalid_configuration")
    result = cast(dict[str, object], payload)
    if result.get("schema_version") != 1:
        raise WorkerRuntimeError("invalid_configuration")
    gate = result.get("gate")
    if (
        not isinstance(gate, dict)
        or gate.get("adapter_source_ready") is not True
        or gate.get("offline_source_bundle_materialized") is not True
        or gate.get("offline_oci_image_verified") is not True
        or gate.get("representative_pdf_smoke_passed") is not True
        or gate.get("runtime_ready") is not True
    ):
        raise WorkerRuntimeError("invalid_configuration")
    return result


def verify_routing_config_identity(config: WorkerRoutingConfig) -> None:
    manifest = load_runtime_manifest()
    identity = manifest.get("routing_config")
    if not isinstance(identity, dict):
        raise WorkerRuntimeError("invalid_configuration")
    if (
        identity.get("revision") != config.revision
        or identity.get("sha256") != config.sha256
    ):
        raise WorkerRuntimeError("invalid_configuration")


def _packages_by_name() -> dict[str, dict[str, object]]:
    raw = load_runtime_manifest().get("packages")
    if not isinstance(raw, list):
        raise WorkerRuntimeError("invalid_configuration")
    packages: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise WorkerRuntimeError("invalid_configuration")
        package = cast(dict[str, object], item)
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or name in packages:
            raise WorkerRuntimeError("invalid_configuration")
        packages[name] = package
    return packages


def verify_distribution_versions(distributions: tuple[str, ...]) -> None:
    packages = _packages_by_name()
    for distribution in distributions:
        package = packages.get(distribution)
        if package is None:
            raise WorkerRuntimeError("invalid_configuration")
        expected = package["version"]
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise WorkerRuntimeError("dependency_unavailable") from error
        if installed != expected:
            raise WorkerRuntimeError("dependency_version_mismatch")


def _safe_artifact_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or str(pure) != relative:
        raise WorkerRuntimeError("invalid_configuration")
    current = root
    for part in pure.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise WorkerRuntimeError("dependency_unavailable") from error
        if stat.S_ISLNK(info.st_mode):
            raise WorkerRuntimeError("invalid_configuration")
    return current


def _verify_artifact(path: Path, expected_size: int, expected_sha256: str) -> None:
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise WorkerRuntimeError("dependency_unavailable")
        digest = hashlib.sha256()
        with path.open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.lstat(path)
    except WorkerRuntimeError:
        raise
    except OSError as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != after_identity or digest.hexdigest() != expected_sha256:
        raise WorkerRuntimeError("dependency_version_mismatch")


def verify_docling_model_artifacts(model_root: Path) -> None:
    """Verify every selected model byte before either converter initializes."""

    manifest = load_runtime_manifest()
    raw_models = manifest.get("models")
    if not isinstance(raw_models, list) or len(raw_models) != len(_MODEL_ROOTS):
        raise WorkerRuntimeError("invalid_configuration")
    seen: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            raise WorkerRuntimeError("invalid_configuration")
        model = cast(dict[str, object], raw_model)
        name = model.get("name")
        files = model.get("files")
        if not isinstance(name, str) or name not in _MODEL_ROOTS or name in seen:
            raise WorkerRuntimeError("invalid_configuration")
        if not isinstance(files, list) or not files:
            raise WorkerRuntimeError("invalid_configuration")
        seen.add(name)
        model_prefix = _MODEL_ROOTS[name]
        for raw_file in files:
            if not isinstance(raw_file, dict):
                raise WorkerRuntimeError("invalid_configuration")
            item = cast(dict[str, object], raw_file)
            path = item.get("path")
            size = item.get("size_bytes")
            sha256 = item.get("sha256")
            if (
                not isinstance(path, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 1
                or not isinstance(sha256, str)
                or len(sha256) != 64
            ):
                raise WorkerRuntimeError("invalid_configuration")
            if name == "rapidocr-ppocrv6-multilingual-onnx":
                path = PurePosixPath(path).name
            relative = f"{model_prefix}/{path}"
            _verify_artifact(_safe_artifact_path(model_root, relative), size, sha256)
    if seen != set(_MODEL_ROOTS):
        raise WorkerRuntimeError("invalid_configuration")


__all__ = [
    "load_runtime_manifest",
    "verify_distribution_versions",
    "verify_docling_model_artifacts",
    "verify_routing_config_identity",
]

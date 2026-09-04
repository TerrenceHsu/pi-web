"""Runtime-manifest and dependency-version gates for the MinerU Worker."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from typing import cast

from .config import WorkerRoutingConfig
from .errors import WorkerRuntimeError


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
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkerRuntimeError("invalid_configuration")
    gate = payload.get("gate")
    if not isinstance(gate, dict) or gate.get("adapter_source_ready") is not True:
        raise WorkerRuntimeError("invalid_configuration")
    return cast(dict[str, object], payload)


def verify_routing_config_identity(config: WorkerRoutingConfig) -> None:
    identity = load_runtime_manifest().get("routing_config")
    if not isinstance(identity, dict):
        raise WorkerRuntimeError("invalid_configuration")
    if identity.get("revision") != config.revision or identity.get("sha256") != config.sha256:
        raise WorkerRuntimeError("invalid_configuration")


def verify_distribution_versions(distributions: tuple[str, ...]) -> None:
    raw = load_runtime_manifest().get("packages")
    if not isinstance(raw, list):
        raise WorkerRuntimeError("invalid_configuration")
    packages: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise WorkerRuntimeError("invalid_configuration")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise WorkerRuntimeError("invalid_configuration")
        packages[name.casefold()] = version
    for distribution in distributions:
        expected = packages.get(distribution.casefold())
        if expected is None:
            raise WorkerRuntimeError("invalid_configuration")
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise WorkerRuntimeError("dependency_unavailable") from error
        if installed != expected:
            raise WorkerRuntimeError("dependency_version_mismatch")


__all__ = [
    "load_runtime_manifest",
    "verify_distribution_versions",
    "verify_routing_config_identity",
]

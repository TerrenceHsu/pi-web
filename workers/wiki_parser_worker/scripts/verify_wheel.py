"""Verify that a built Worker wheel contains every compliance asset.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only

This program is free software under GNU AGPL version 3 only. It comes with
ABSOLUTELY NO WARRANTY. See the LICENSE file in the component source root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

_ASSET_ROOT = "wiki_parser_worker/compliance_assets"
_REQUIRED_MEMBERS = {
    "wiki_parser_worker/__init__.py",
    "wiki_parser_worker/__main__.py",
    "wiki_parser_worker/artifact.py",
    "wiki_parser_worker/cli.py",
    "wiki_parser_worker/compliance.py",
    "wiki_parser_worker/config.py",
    "wiki_parser_worker/engine.py",
    "wiki_parser_worker/errors.py",
    "wiki_parser_worker/models.py",
    "wiki_parser_worker/parsers.py",
    "wiki_parser_worker/preflight.py",
    "wiki_parser_worker/protocol.py",
    "wiki_parser_worker/quality.py",
    "wiki_parser_worker/runtime_child.py",
    "wiki_parser_worker/service.py",
    "wiki_parser_worker/supply_chain.py",
    "wiki_parser_worker/config/routing-quality-v1.json",
    f"{_ASSET_ROOT}/LICENSE",
    f"{_ASSET_ROOT}/NOTICE.md",
    f"{_ASSET_ROOT}/SOURCE_OFFER.md",
    f"{_ASSET_ROOT}/component-manifest.json",
    f"{_ASSET_ROOT}/runtime-manifest.json",
    f"{_ASSET_ROOT}/sbom.spdx.json",
}
_OFFICIAL_AGPL_TEXT_SHA256 = (
    "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
)
_FORBIDDEN_PACKAGE_ROOTS = (
    "docling/",
    "pymupdf/",
    "pymupdf4llm/",
    "torch/",
    "transformers/",
)


def verify_wheel(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        members = set(wheel.namelist())
        missing = _REQUIRED_MEMBERS - members
        if missing:
            raise ValueError(f"wheel omits compliance assets: {sorted(missing)}")
        license_bytes = wheel.read(f"{_ASSET_ROOT}/LICENSE")
        if hashlib.sha256(license_bytes).hexdigest() != _OFFICIAL_AGPL_TEXT_SHA256:
            raise ValueError("wheel contains modified GNU AGPL license text")
        manifest = json.loads(wheel.read(f"{_ASSET_ROOT}/component-manifest.json"))
        runtime = json.loads(wheel.read(f"{_ASSET_ROOT}/runtime-manifest.json"))
        sbom = json.loads(wheel.read(f"{_ASSET_ROOT}/sbom.spdx.json"))
        if manifest.get("license_expression") != "AGPL-3.0-only":
            raise ValueError("wheel manifest license identity is invalid")
        if manifest.get("runtime_ready") is not True:
            raise ValueError("wheel manifest omits verified OCI runtime readiness")
        if sbom.get("spdxVersion") != "SPDX-2.3":
            raise ValueError("wheel SBOM version is invalid")
        gate = runtime.get("gate", {})
        if gate.get("adapter_source_ready") is not True:
            raise ValueError("wheel runtime manifest omits audited adapter source")
        if gate.get("full_transitive_lock_ready") is not True:
            raise ValueError("wheel runtime manifest omits the transitive lock gate")
        for completed_gate in (
            "offline_source_bundle_materialized",
            "offline_oci_image_verified",
            "representative_pdf_smoke_passed",
            "runtime_ready",
        ):
            if gate.get(completed_gate) is not True:
                raise ValueError(f"wheel runtime manifest omits gate: {completed_gate}")
        config = json.loads(wheel.read("wiki_parser_worker/config/routing-quality-v1.json"))
        config_bytes = json.dumps(
            config,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if hashlib.sha256(config_bytes).hexdigest() != runtime["routing_config"]["sha256"]:
            raise ValueError("wheel routing configuration hash is invalid")
        for forbidden in _FORBIDDEN_PACKAGE_ROOTS:
            if any(member.casefold().startswith(forbidden) for member in members):
                raise ValueError(f"wheel unexpectedly vendors parser package: {forbidden}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    arguments = parser.parse_args()
    verify_wheel(arguments.wheel)
    print(f"worker_wheel_compliance=ok path={arguments.wheel.name}")


if __name__ == "__main__":
    main()

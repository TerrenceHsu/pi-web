"""Verify that a built Worker wheel contains every compliance asset.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
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
    f"{_ASSET_ROOT}/MINERU_LICENSE.md",
    f"{_ASSET_ROOT}/NOTICE.md",
    f"{_ASSET_ROOT}/SOURCE_OFFER.md",
    f"{_ASSET_ROOT}/component-manifest.json",
    f"{_ASSET_ROOT}/runtime-manifest.json",
    f"{_ASSET_ROOT}/sbom.spdx.json",
}
_FORBIDDEN_PACKAGE_ROOTS = ("mineru/", "torch/", "transformers/")


def verify_wheel(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        members = set(wheel.namelist())
        missing = _REQUIRED_MEMBERS - members
        if missing:
            raise ValueError(f"wheel omits compliance assets: {sorted(missing)}")
        license_bytes = wheel.read(f"{_ASSET_ROOT}/LICENSE")
        if not license_bytes.startswith(b"MIT License"):
            raise ValueError("wheel omits the Worker MIT license")
        manifest = json.loads(wheel.read(f"{_ASSET_ROOT}/component-manifest.json"))
        runtime = json.loads(wheel.read(f"{_ASSET_ROOT}/runtime-manifest.json"))
        sbom = json.loads(wheel.read(f"{_ASSET_ROOT}/sbom.spdx.json"))
        if manifest.get("license_expression") != "MIT":
            raise ValueError("wheel manifest license identity is invalid")
        if manifest.get("runtime_ready") is not False:
            raise ValueError("wheel manifest must not claim an unverified runtime")
        if sbom.get("spdxVersion") != "SPDX-2.3":
            raise ValueError("wheel SBOM version is invalid")
        gate = runtime.get("gate", {})
        if gate.get("adapter_source_ready") is not True:
            raise ValueError("wheel runtime manifest omits audited adapter source")
        if gate.get("runtime_ready") is not False:
            raise ValueError("wheel runtime manifest must keep runtime_ready false")
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

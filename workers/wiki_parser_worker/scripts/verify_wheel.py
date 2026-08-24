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
    "wiki_parser_worker/compliance.py",
    f"{_ASSET_ROOT}/LICENSE",
    f"{_ASSET_ROOT}/NOTICE.md",
    f"{_ASSET_ROOT}/SOURCE_OFFER.md",
    f"{_ASSET_ROOT}/component-manifest.json",
    f"{_ASSET_ROOT}/sbom.spdx.json",
}
_OFFICIAL_AGPL_TEXT_SHA256 = (
    "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
)
_FORBIDDEN_NAMES = ("docling", "pymupdf", "pymupdf4llm", "torch", "transformers")


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
        sbom = json.loads(wheel.read(f"{_ASSET_ROOT}/sbom.spdx.json"))
        if manifest.get("license_expression") != "AGPL-3.0-only":
            raise ValueError("wheel manifest license identity is invalid")
        if manifest.get("runtime_ready") is not False:
            raise ValueError("compliance scaffold cannot advertise runtime readiness")
        if sbom.get("spdxVersion") != "SPDX-2.3":
            raise ValueError("wheel SBOM version is invalid")
        lowered_members = "\n".join(sorted(members)).casefold()
        for forbidden in _FORBIDDEN_NAMES:
            if forbidden in lowered_members:
                raise ValueError(f"wheel unexpectedly contains parser runtime: {forbidden}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    arguments = parser.parse_args()
    verify_wheel(arguments.wheel)
    print(f"worker_wheel_compliance=ok path={arguments.wheel.name}")


if __name__ == "__main__":
    main()

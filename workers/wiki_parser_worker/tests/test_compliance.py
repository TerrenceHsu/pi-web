"""Self-contained MinerU Worker compliance smoke."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wiki_parser_worker import __version__, compliance_identity  # noqa: E402


def test_metadata_is_consistent_and_runtime_remains_unverified() -> None:
    manifest = json.loads((_ROOT / "component-manifest.json").read_text(encoding="utf-8"))
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sbom = json.loads((_ROOT / "sbom.spdx.json").read_text(encoding="utf-8"))
    runtime = json.loads((_ROOT / "runtime-manifest.json").read_text(encoding="utf-8"))
    identity = compliance_identity()

    assert __version__ == manifest["version"] == project["project"]["version"]
    assert sbom["packages"][0]["versionInfo"] == manifest["version"]
    assert identity.license_expression == "MIT"
    assert identity.runtime_ready is False
    assert manifest["runtime_ready"] is False
    assert runtime["gate"]["adapter_source_ready"] is True
    assert runtime["gate"]["runtime_ready"] is False
    assert runtime["packages"][0]["name"] == "mineru"

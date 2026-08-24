"""Self-contained compliance smoke for the Worker source package.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only

This program is free software under GNU AGPL version 3 only. It comes with
ABSOLUTELY NO WARRANTY. See the LICENSE file in the component source root.
"""

from __future__ import annotations

import json
import sys
import tomllib
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wiki_parser_worker import __version__, compliance_identity  # noqa: E402


class ComplianceIdentityTests(unittest.TestCase):
    def test_metadata_is_consistent_and_runtime_is_not_advertised(self) -> None:
        manifest = json.loads(
            (_ROOT / "component-manifest.json").read_text(encoding="utf-8")
        )
        project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        sbom = json.loads((_ROOT / "sbom.spdx.json").read_text(encoding="utf-8"))
        identity = compliance_identity()

        self.assertEqual(__version__, manifest["version"])
        self.assertEqual(project["project"]["version"], manifest["version"])
        self.assertEqual(sbom["packages"][0]["versionInfo"], manifest["version"])
        self.assertEqual(identity.license_expression, "AGPL-3.0-only")
        self.assertFalse(identity.runtime_ready)
        self.assertFalse(manifest["runtime_ready"])


if __name__ == "__main__":
    unittest.main()

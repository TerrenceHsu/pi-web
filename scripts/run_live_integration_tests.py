"""Run the canonical DDGS + GLM live smoke set without exposing secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PI_RUN_INTEGRATION"] = "1"
    environment["PYTHONPATH"] = str(project_root / "src")

    tests = (
        "tests/test_ddgs_mcp_integration.py::test_real_ddgs_search_over_project_mcp_client",
        "tests/test_step_21_glm_real_smoke.py::test_real_glm_returns_text_or_done",
        "tests/test_step_21_glm_real_smoke.py::test_real_glm_done_event_has_usage",
    )
    command = (
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-q",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        "-m",
        "integration",
    )
    print(
        "Running 3 opt-in live smokes (DDGS + GLM) from the current interactive "
        "Windows session using secret-safe config resolution...",
        flush=True,
    )
    return subprocess.run(command, cwd=project_root, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

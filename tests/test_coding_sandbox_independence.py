"""Architecture guardrails for the standalone coding-sandbox package."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX_SOURCE = _REPOSITORY_ROOT / "src" / "coding_sandbox"


def test_standalone_package_never_imports_pi_agent_core() -> None:
    forbidden = "pi_agent_core_py"
    for source_path in _SANDBOX_SOURCE.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = [node.module]
            else:
                continue
            assert all(
                name != forbidden and not name.startswith(f"{forbidden}.")
                for name in imported
            ), f"standalone package imports pi-agent in {source_path}"


def test_standalone_package_import_does_not_load_pi_agent_core() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPOSITORY_ROOT / "src")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import coding_sandbox; import coding_sandbox.admin; "
                "assert 'pi_agent_core_py' not in sys.modules"
            ),
        ],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_pi_agent_no_longer_owns_sandbox_implementation() -> None:
    assert not (_REPOSITORY_ROOT / "src" / "pi_agent_core_py" / "coding_workspace").exists()

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = REPO_ROOT / "evals"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_eval_package_does_not_depend_on_web_or_network_clients() -> None:
    forbidden = {
        "anthropic",
        "httpx",
        "openai",
        "requests",
        "socket",
    }
    offenders: list[str] = []
    for path in EVAL_ROOT.rglob("*.py"):
        for module in _imports(path):
            if (
                module in forbidden
                or module.startswith("pi_agent_core_py.web")
                or module.startswith("pi_agent_core_py.mcp")
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {module}")
    assert offenders == []


def test_eval_package_remains_outside_the_runtime_wheel() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    wheel_section = pyproject.split("[tool.hatch.build.targets.wheel]", 1)[1]
    assert '"evals"' not in wheel_section

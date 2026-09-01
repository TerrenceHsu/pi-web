from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT = REPO_ROOT / "src" / "coding_agent_app"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_product_package_never_imports_web_transport() -> None:
    offenders: list[str] = []
    for path in PRODUCT_ROOT.rglob("*.py"):
        for module in _imported_modules(path):
            if module == "pi_agent_core_py.web" or module.startswith(
                "pi_agent_core_py.web."
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {module}")
    assert offenders == []


def test_canonical_product_modules_exist() -> None:
    expected = {
        "core/application.py",
        "core/resources.py",
        "core/runtime.py",
        "core/sdk.py",
        "core/services.py",
        "core/session.py",
        "core/settings.py",
        "core/toolsets.py",
        "sandbox/automation.py",
        "sandbox/workspace.py",
    }
    actual = {
        path.relative_to(PRODUCT_ROOT).as_posix()
        for path in PRODUCT_ROOT.rglob("*.py")
    }
    assert expected <= actual


def test_legacy_sandbox_imports_preserve_object_identity() -> None:
    from coding_agent_app.sandbox import (
        CodingSandboxAutomation as CanonicalAutomation,
    )
    from coding_agent_app.sandbox import (
        WorkspaceSandboxBaselineProvider as CanonicalBaseline,
    )
    from coding_agent_app.sandbox_workspace import (
        WorkspaceSandboxBaselineProvider as RootCompatibilityBaseline,
    )
    from pi_agent_core_py.web.coding_sandbox.automation import (
        CodingSandboxAutomation as WebCompatibilityAutomation,
    )
    from pi_agent_core_py.web.coding_sandbox.workspace import (
        WorkspaceSandboxBaselineProvider as WebCompatibilityBaseline,
    )

    assert WebCompatibilityAutomation is CanonicalAutomation
    assert RootCompatibilityBaseline is CanonicalBaseline
    assert WebCompatibilityBaseline is CanonicalBaseline


def test_web_transport_does_not_mutate_harness_tools_or_client_directly() -> None:
    source = (REPO_ROOT / "src" / "pi_agent_core_py" / "web" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "harness.agent.tools =" not in source
    assert "harness.agent.client =" not in source
    assert ".coding_sandbox.automation import" not in source

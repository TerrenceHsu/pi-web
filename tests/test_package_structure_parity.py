"""Package-boundary contracts for the pi-agent aligned layout.

These tests intentionally exercise both the new canonical imports and the
legacy public imports.  The old paths remain supported as compatibility
facades, but implementation ownership lives in the nested packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pi_agent_core_py as public_api

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "pi_agent_core_py"


def _imports_banned_root(module_path: Path, banned_roots: set[str]) -> list[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    violations: list[str] = []
    package_parts = module_path.relative_to(PACKAGE_ROOT).with_suffix("").parts
    current_package = ("pi_agent_core_py", *package_parts[:-1])

    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(current_package) - node.level + 1)
                prefix = current_package[:keep]
                absolute = ".".join((*prefix, *(node.module or "").split(".")))
                candidates.append(absolute.rstrip("."))
            elif node.module:
                candidates.append(node.module)

        for imported in candidates:
            parts = imported.split(".")
            if parts[:1] == ["pi_agent_core_py"] and len(parts) > 1:
                root = parts[1]
                if root in banned_roots:
                    violations.append(f"{module_path.relative_to(PACKAGE_ROOT)} -> {imported}")
    return violations


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_old_and_new_agent_imports_share_identity() -> None:
    from pi_agent_core_py.agent import Agent as package_agent
    from pi_agent_core_py.agent.runtime import Agent as runtime_agent

    assert public_api.Agent is package_agent is runtime_agent


def test_old_and_new_ai_imports_share_identity() -> None:
    from pi_agent_core_py.ai.messages import UserMessage as canonical_message
    from pi_agent_core_py.ai.model_client import ModelClient as canonical_client
    from pi_agent_core_py.ai.providers import ProviderAdapter as canonical_provider
    from pi_agent_core_py.messages import UserMessage as legacy_message
    from pi_agent_core_py.model_client import ModelClient as legacy_client
    from pi_agent_core_py.providers import ProviderAdapter as legacy_provider

    assert canonical_message is legacy_message
    assert canonical_client is legacy_client
    assert canonical_provider is legacy_provider


def test_old_and_new_harness_and_session_imports_share_identity() -> None:
    from pi_agent_core_py.agent.harness.runtime import AgentHarness as canonical_harness
    from pi_agent_core_py.agent.harness.session.memory import SessionMemory as canonical_session
    from pi_agent_core_py.agent.harness.tools import ViewFileTool as canonical_view_file
    from pi_agent_core_py.harness import AgentHarness as legacy_harness
    from pi_agent_core_py.session import SessionMemory as legacy_session
    from pi_agent_core_py.tools import ViewFileTool as legacy_view_file

    assert canonical_harness is legacy_harness
    assert canonical_session is legacy_session
    assert canonical_view_file is legacy_view_file


def test_old_and_new_sqlite_imports_share_identity() -> None:
    from pi_agent_core_py.session_backends.sqlite import SQLiteSessionStore as canonical
    from pi_agent_core_py.session_sqlite import SQLiteSessionStore as legacy

    assert canonical is legacy


def test_ai_package_does_not_depend_on_higher_layers() -> None:
    banned = {"agent", "coding_agent", "session_backends", "web"}
    violations = [
        violation
        for path in _python_files(PACKAGE_ROOT / "ai")
        for violation in _imports_banned_root(path, banned)
    ]
    assert violations == []


def test_agent_core_does_not_depend_on_harness_or_product_layers() -> None:
    banned = {"coding_agent", "session_backends", "web"}
    core_files = [
        path
        for path in _python_files(PACKAGE_ROOT / "agent")
        if "harness" not in path.relative_to(PACKAGE_ROOT / "agent").parts
    ]
    violations = [
        violation
        for path in core_files
        for violation in _imports_banned_root(path, banned)
    ]
    assert violations == []


def test_legacy_modules_are_thin_facades() -> None:
    legacy_modules = [
        "compaction.py",
        "context.py",
        "context_budget.py",
        "events.py",
        "harness.py",
        "hooks.py",
        "llm_messages.py",
        "messages.py",
        "model_client.py",
        "session.py",
        "session_sqlite.py",
        "session_sync.py",
        "skill_loader.py",
        "skills.py",
        "snapshot.py",
        "stream_events.py",
        "system_prompt.py",
        "tool_validation.py",
        "providers/anthropic_compat.py",
        "providers/base.py",
        "providers/errors.py",
        "providers/factory.py",
        "providers/fake.py",
        "providers/glm.py",
        "providers/openai_compat.py",
        "providers/registry.py",
        "providers/retry.py",
        "providers/transform.py",
        "tools/list_files.py",
        "tools/view_file.py",
        "tools/web_search.py",
        "tools/write_file.py",
    ]
    oversized: list[str] = []
    for name in legacy_modules:
        source = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        executable = [
            node
            for node in tree.body
            if not isinstance(
                node,
                (ast.Expr, ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign),
            )
        ]
        if executable:
            oversized.append(name)
    assert oversized == []

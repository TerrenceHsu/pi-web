"""Multi-Provider Runtime static architecture contract for M1-7（spec §十三）.

AST / 源码扫描锁定：

Core Runtime M1 diff = 0:
- loop.py / agent.py / context.py / events.py / stream_events.py / messages.py
 不得引入 Provider Runtime 接线（resolve_selection / bind_to_harness / build_adapter）

Provider Runtime 接线约束:
- resolve_selection / bind_to_harness / build_adapter 只允许出现在:
    * RequestProviderRuntime 自身
    * _execute_prompt 唯一生产调用点
    * 测试代码
- 禁止出现在:
    * _run_prompt_core / _run_regeneration_core / _run_regeneration_background
    * Prompt / Regenerate HTTP handler
    * Tool loop

模块依赖方向:
- provider_runtime.py 不直接导入 SecretStoreRouter / CredentialRepository
- provider_runtime.py 不导入 SDK（anthropic / openai）
- factory.py 不导入 Web
- openai_compat.py 不导入 Registry / Web
"""
from __future__ import annotations

import ast
import importlib
import pathlib
from typing import Any

import pytest

# asyncio_mode=auto in pyproject——all tests in this file are sync AST checks.

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "pi_agent_core_py"


# ============================================================================
# Helper: walk a function's lexical body (excluding nested defs)
# ============================================================================


def _walk_lexical_body(fn: ast.AST) -> Any:
    """Yield AST nodes in the body of `fn` WITHOUT descending into nested
    function/class definitions."""
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_lexical_body(child)


def _parse_module(rel_path: str) -> ast.Module:
    path = _SRC_ROOT / rel_path
    return ast.parse(path.read_text(encoding="utf-8"))


def _parse_app_module() -> ast.Module:
    return _parse_module("web" / "app.py")  # type: ignore[arg-type]


# ============================================================================
# §十三 Core Runtime: M1 diff = 0
# ============================================================================


CORE_RUNTIME_MODULES = (
    "loop.py",
    "agent.py",
    "context.py",
    "events.py",
    "stream_events.py",
    "messages.py",
)


@pytest.mark.parametrize("module_rel", CORE_RUNTIME_MODULES)
def test_core_runtime_has_no_provider_runtime_binding(module_rel: str) -> None:
    """Core Runtime 模块不得调用 resolve_selection / bind_to_harness /
    build_adapter——这是 Web 层职责."""
    tree = _parse_module(module_rel)
    forbidden = {"resolve_selection", "bind_to_harness", "build_adapter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            pytest.fail(
                f"{module_rel}: forbidden call '{node.attr}' at line {node.lineno}"
            )


@pytest.mark.parametrize("module_rel", CORE_RUNTIME_MODULES)
def test_core_runtime_does_not_import_provider_runtime(module_rel: str) -> None:
    """Core Runtime 不得 import provider_runtime / RequestProviderRuntime."""
    tree = _parse_module(module_rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "provider_runtime", (
                    f"{module_rel}: imports provider_runtime"
                )
                assert alias.name != "RequestProviderRuntime", (
                    f"{module_rel}: imports RequestProviderRuntime"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "pi_agent_core_py.web.providers.runtime", (
                    f"{module_rel}: imports provider_runtime module"
                )


@pytest.mark.parametrize("module_rel", CORE_RUNTIME_MODULES)
def test_core_runtime_does_not_import_web(module_rel: str) -> None:
    """Core Runtime 不得 import pi_agent_core_py.web（除自身）——web 依赖 core，
    反向不行."""
    tree = _parse_module(module_rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("pi_agent_core_py.web"), (
                f"{module_rel}: imports {node.module}——core must not depend on web"
            )


# ============================================================================
# §十三 Provider Runtime 接线约束
# ============================================================================


def test_resolve_selection_bind_to_harness_only_in_execute_prompt() -> None:
    """resolve_selection / bind_to_harness / build_adapter 必须只在
    _execute_prompt 的 lexical body 中调用（除了 provider_runtime.py 自身和
    测试）。"""
    tree = _parse_module("web/app.py")
    forbidden_attrs = ("resolve_selection", "bind_to_harness", "build_adapter")

    all_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns.append(node)

    offending: dict[str, list[int]] = {}
    for fn in all_fns:
        for inner in _walk_lexical_body(fn):
            if isinstance(inner, ast.Attribute) and inner.attr in forbidden_attrs:
                offending.setdefault(fn.name, []).append(inner.lineno)

    for fn_name, lines in offending.items():
        if fn_name != "_execute_prompt":
            pytest.fail(
                f"Provider Runtime binding call in {fn_name}() at lines {lines}"
            )


def test_no_direct_runtime_calls_in_prompt_or_regenerate_handlers() -> None:
    """HTTP handlers (post_prompt, post_regenerate, post_prompt_async) must
    NOT call resolve_selection / bind_to_harness / build_adapter directly."""
    tree = _parse_module("web/app.py")
    forbidden_attrs = ("resolve_selection", "bind_to_harness", "build_adapter")
    handler_names = {
        "post_prompt",
        "post_prompt_async",
        "post_regenerate",
        "abort_request",
        "get_request",
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in handler_names:
                for inner in _walk_lexical_body(node):
                    if (
                        isinstance(inner, ast.Attribute)
                        and inner.attr in forbidden_attrs
                    ):
                        pytest.fail(
                            f"HTTP handler {node.name}() directly calls "
                            f"{inner.attr} at line {inner.lineno}"
                        )


def test_no_direct_runtime_calls_in_run_prompt_core_or_regenerators() -> None:
    """_run_prompt_core / _run_regeneration_core / _run_regeneration_background
    must NOT directly call Provider Runtime binding methods."""
    tree = _parse_module("web/app.py")
    forbidden_attrs = ("resolve_selection", "bind_to_harness", "build_adapter")
    target_fns = {
        "_run_prompt_core",
        "_run_prompt_background",
        "_run_regeneration_core",
        "_run_regeneration_background",
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in target_fns:
                for inner in _walk_lexical_body(node):
                    if (
                        isinstance(inner, ast.Attribute)
                        and inner.attr in forbidden_attrs
                    ):
                        pytest.fail(
                            f"{node.name}() directly calls {inner.attr} at "
                            f"line {inner.lineno}——must go through _execute_prompt"
                        )


# ============================================================================
# §十三 RequestProviderRuntime 构造位置
# ============================================================================


def test_request_provider_runtime_constructed_only_in_composition_root() -> None:
    """RequestProviderRuntime(...) constructor calls only at composition root."""
    tree = _parse_module("web/app.py")
    all_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns.append(node)

    locations: list[tuple[str, int]] = []
    for fn in all_fns:
        for inner in _walk_lexical_body(fn):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "RequestProviderRuntime"
            ):
                locations.append((fn.name, inner.lineno))

    allowed = ("create_app", "_lifespan", "lifespan")
    for fn_name, line in locations:
        assert fn_name in allowed, (
            f"RequestProviderRuntime constructed in {fn_name}() at line "
            f"{line}——must be only in composition root"
        )


# ============================================================================
# §十三 Module dependency direction
# ============================================================================


def test_provider_runtime_does_not_import_secret_store_router() -> None:
    """provider_runtime.py 不直接导入 SecretStoreRouter."""
    tree = _parse_module("web/providers/runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "SecretStoreRouter", (
                    "provider_runtime.py imports SecretStoreRouter"
                )


def test_provider_runtime_does_not_import_credential_repository() -> None:
    """provider_runtime.py 不导入 CredentialRepository / SQLiteCredentialStore."""
    tree = _parse_module("web/providers/runtime.py")
    forbidden_names = {
        "CredentialRepository",
        "SQLiteCredentialStore",
        "OSKeyringSecretStore",
        "InMemorySecretStore",
        "EnvSecretStore",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"provider_runtime.py imports {alias.name}"
                )


def test_provider_runtime_does_not_import_sdk() -> None:
    """provider_runtime.py 不导入 anthropic / openai SDK 直接."""
    tree = _parse_module("web/providers/runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in ("anthropic", "openai"), (
                    f"provider_runtime.py imports SDK {alias.name}"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("anthropic."), (
                f"provider_runtime.py imports {node.module}"
            )
            assert not node.module.startswith("openai."), (
                f"provider_runtime.py imports {node.module}"
            )


def test_factory_does_not_import_web() -> None:
    """factory.py 不导入 pi_agent_core_py.web."""
    tree = _parse_module("providers/factory.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("pi_agent_core_py.web"), (
                f"factory.py imports {node.module}——factory must not depend on web"
            )


def test_openai_compat_does_not_import_registry_or_web() -> None:
    """openai_compat.py 不导入 Registry / Web."""
    tree = _parse_module("providers/openai_compat.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("pi_agent_core_py.web"), (
                f"openai_compat.py imports {node.module}"
            )
            assert node.module != "pi_agent_core_py.providers.registry", (
                "openai_compat.py imports registry——circular"
            )


def test_glm_does_not_import_registry_or_web() -> None:
    """glm.py 不导入 Registry / Web."""
    tree = _parse_module("providers/glm.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("pi_agent_core_py.web"), (
                f"glm.py imports {node.module}"
            )
            assert node.module != "pi_agent_core_py.providers.registry", (
                "glm.py imports registry——circular"
            )


# ============================================================================
# §十三 Provider Runtime module surface stable
# ============================================================================


def test_provider_runtime_public_api_stable() -> None:
    """Spot-check: provider_runtime.py still exports the expected names."""
    module = importlib.import_module("pi_agent_core_py.web.providers.runtime")
    for name in (
        "RequestProviderRuntime",
        "RequestProviderSelection",
        "ProviderSelectionNotFoundError",
        "ProviderSelectionDisabledError",
        "ProviderSelectionUnavailableError",
        "ProviderInitializationError",
        "ProviderFactoryCallable",
    ):
        assert hasattr(module, name), f"provider_runtime missing {name}"


def test_factory_public_api_stable() -> None:
    """factory.py exposes create_provider + UnsupportedProviderError."""
    module = importlib.import_module("pi_agent_core_py.providers.factory")
    for name in ("create_provider", "UnsupportedProviderError"):
        assert hasattr(module, name), f"factory missing {name}"


# ============================================================================
# §十三 Adapter import boundary
# ============================================================================


def test_adapters_only_imported_via_factory_or_type_hints() -> None:
    """web/app.py 不得直接 import GLMProviderAdapter / OpenAICompatibleProvider——
    只能通过 Factory 间接获取."""
    tree = _parse_module("web/app.py")
    forbidden = {"GLMProviderAdapter", "OpenAICompatibleProvider", "AnthropicCompatAdapter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"web/app.py imports Adapter {alias.name} directly——must "
                    "go through ProviderFactory"
                )


def test_factory_maps_provider_ids_correctly() -> None:
    """Static check: create_provider maps glm/qwen/kimi to the documented
    Adapter classes. Re-asserted dynamically in freeze tests."""
    from pi_agent_core_py.providers.factory import create_provider
    from pi_agent_core_py.providers.glm import GLMProviderAdapter
    from pi_agent_core_py.providers.openai_compat import OpenAICompatibleProvider
    from pi_agent_core_py.providers.registry import _DEFAULT_REGISTRY

    for pid, expected_type in (
        ("glm", GLMProviderAdapter),
        ("qwen", OpenAICompatibleProvider),
        ("kimi", OpenAICompatibleProvider),
    ):
        defn = _DEFAULT_REGISTRY.get(pid)
        assert defn is not None, f"registry missing {pid}"
        adapter = create_provider(
            provider_definition=defn, api_key="sk", model_id=f"{pid}-m"
        )
        assert isinstance(adapter, expected_type), (
            f"{pid} → {type(adapter).__name__}, expected {expected_type.__name__}"
        )

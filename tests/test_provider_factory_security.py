"""ProviderFactory 安全 / 边界测试（M1-3 §九.25–§九.35）.

覆盖：
- Secret marker 不泄漏到异常 str / repr / Adapter repr / 日志
- Pydantic 原始错误文本不传播
- Base URL / Model ID 不进入错误消息
- 构造阶段真实网络调用 = 0
- Factory 源码不导入 SecretStore / Web 模块 / ModelClient
"""
from __future__ import annotations

import logging
import socket

import pytest

from pi_agent_core_py.providers.anthropic_compat import AnthropicCompatAdapter
from pi_agent_core_py.providers.errors import ProviderConfigError
from pi_agent_core_py.providers.factory import (
    create_provider,
)
from pi_agent_core_py.providers.glm import GLMProviderAdapter
from pi_agent_core_py.providers.openai_compat import OpenAICompatibleProvider
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    get_provider_definition,
)

SECRET_MARKER = "sk-M1-3-FACTORY-SECRET-MARKER-DO-NOT-LEAK"
MODEL_MARKER = "MODEL-MARKER-DO-NOT-LEAK"
BASE_URL_MARKER = "base-url-marker.example.test"

_VALID_BASE_URL = "https://factory-security.example.test/v1"


def _make_openai_compat_def(base_url: str = _VALID_BASE_URL) -> ProviderDefinition:
    return ProviderDefinition(
        id="custom-openai",
        display_name="Custom OpenAI-compat",
        api_style="openai_compatible",
        default_base_url=base_url,
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


def _make_anthropic_compat_def(base_url: str = _VALID_BASE_URL) -> ProviderDefinition:
    return ProviderDefinition(
        id="custom-anthropic",
        display_name="Custom Anthropic-compat",
        api_style="anthropic_compatible",
        default_base_url=base_url,
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


def _walk_cause_chain(exc: BaseException) -> None:
    """Assert SECRET_MARKER / MODEL_MARKER / BASE_URL_MARKER absent in every
    exception str/repr along __cause__."""
    current: BaseException | None = exc
    while current is not None:
        for marker in (SECRET_MARKER, MODEL_MARKER, BASE_URL_MARKER):
            text = str(current)
            r = repr(current)
            assert marker not in text, (
                f"marker {marker!r} leaked in str: {text!r}"
            )
            assert marker not in r, (
                f"marker {marker!r} leaked in repr: {r!r}"
            )
        current = current.__cause__


# ============================================================================
# 25-27: Secret marker 不泄漏
# ============================================================================


@pytest.mark.parametrize(
    "defn, expected_type",
    [
        (get_provider_definition("glm"), GLMProviderAdapter),
        (get_provider_definition("anthropic"), AnthropicCompatAdapter),
        (get_provider_definition("qwen"), OpenAICompatibleProvider),
        (get_provider_definition("kimi"), OpenAICompatibleProvider),
    ],
)
def test_secret_marker_absent_from_adapter_repr(
    defn: ProviderDefinition,
    expected_type: type,
) -> None:
    """构造成功后 Adapter 的 repr 不应暴露 api_key（SecretStr masking / str 不存）."""
    adapter = create_provider(
        provider_definition=defn,
        api_key=SECRET_MARKER,
        model_id=MODEL_MARKER,
    )
    assert isinstance(adapter, expected_type)
    r = repr(adapter)
    s = str(adapter)
    assert SECRET_MARKER not in r, f"repr leaks marker: {r!r}"
    assert SECRET_MARKER not in s, f"str leaks marker: {s!r}"


def test_secret_marker_absent_from_config_repr() -> None:
    """AnthropicCompatConfig.api_key 是 plain str——但 Adapter repr / config repr
    仍不应原样暴露完整 marker（OpenAICompatConfig.api_key 是 SecretStr）."""
    # OpenAI-compatible 路径：SecretStr masking 强制保证
    adapter = create_provider(
        provider_definition=get_provider_definition("qwen"),
        api_key=SECRET_MARKER,
        model_id=MODEL_MARKER,
    )
    cfg_repr = repr(adapter.config)  # type: ignore[attr-defined]
    assert SECRET_MARKER not in cfg_repr


def test_secret_marker_absent_from_error_messages() -> None:
    """构造失败时，异常 str / repr 不含 secret marker / model marker / base_url marker."""
    bad_def = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        # Pydantic 会拒绝这个 base_url（非 http/https）——触发 wrap path
        default_base_url=f"ftp://{BASE_URL_MARKER}",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=bad_def,
            api_key=SECRET_MARKER,
            model_id=MODEL_MARKER,
        )
    _walk_cause_chain(exc_info.value)


def test_secret_marker_absent_from_empty_key_error() -> None:
    """空 key 路径的异常消息也不应包含任何 marker（即使 key 为空，model / base_url
    可能含 marker 也不应进入错误）."""
    defn = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url=f"https://{BASE_URL_MARKER}",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=defn,
            api_key="",
            model_id=MODEL_MARKER,
        )
    _walk_cause_chain(exc_info.value)


# ============================================================================
# 28: 不写日志
# ============================================================================


def test_factory_does_not_log_secret_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Factory 模块不使用 logger——任何级别都不应捕获到 marker."""
    caplog.set_level(logging.DEBUG)
    # 触发各路径
    create_provider(
        provider_definition=get_provider_definition("qwen"),
        api_key=SECRET_MARKER,
        model_id=MODEL_MARKER,
    )
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=get_provider_definition("qwen"),
            api_key="",
            model_id=MODEL_MARKER,
        )
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=_make_anthropic_compat_def(),
            api_key=SECRET_MARKER,
            model_id=MODEL_MARKER,
        )

    full_log = caplog.text
    assert SECRET_MARKER not in full_log
    assert MODEL_MARKER not in full_log
    assert BASE_URL_MARKER not in full_log


# ============================================================================
# 29: Pydantic 原始错误不传播
# ============================================================================


def test_pydantic_validation_error_not_propagated() -> None:
    """OpenAICompatConfig ValidationError 被 wrap 为 ProviderConfigError，且 cause 链
    上的 str/repr 不含 base_url / model 标记."""
    bad_def = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url=f"ftp://{BASE_URL_MARKER}",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=bad_def,
            api_key=SECRET_MARKER,
            model_id=MODEL_MARKER,
        )
    # __cause__ 必须为 None（from None 的语义）
    assert exc_info.value.__cause__ is None
    # 固定安全消息
    assert str(exc_info.value) == "provider configuration is invalid"
    # 类型严格——不是 Pydantic ValidationError
    from pydantic import ValidationError

    assert not isinstance(exc_info.value, ValidationError)


# ============================================================================
# 30-31: Base URL / Model ID 不进入错误消息
# ============================================================================


def test_base_url_not_in_error_message() -> None:
    """Pydantic 拒绝 bad base_url 时，URL 不应出现在 wrap 后的异常 str/repr."""
    bad_def = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url=f"ftp://{BASE_URL_MARKER}",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=bad_def,
            api_key=SECRET_MARKER,
            model_id="ok-model",
        )
    assert BASE_URL_MARKER not in str(exc_info.value)
    assert BASE_URL_MARKER not in repr(exc_info.value)


def test_model_id_not_in_error_message() -> None:
    """model_id 含控制字符触发 ValidationError 时，model_id 不应进入 wrap 后异常."""
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=get_provider_definition("qwen"),
            api_key=SECRET_MARKER,
            model_id=f"{MODEL_MARKER}\x00",
        )
    assert MODEL_MARKER not in str(exc_info.value)
    assert MODEL_MARKER not in repr(exc_info.value)


# ============================================================================
# 32: 构造阶段真实网络调用为 0
# ============================================================================


def test_no_network_calls_during_construction() -> None:
    """把 socket.socket 替换为 raise——证明 Factory 构造阶段不发任何网络请求."""
    original_socket = socket.socket

    def _blocking_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError(
            "Factory 不应在构造阶段创建 socket / 发起网络调用"
        )

    # 四种 Adapter 都构造一遍
    socket.socket = _blocking_socket  # type: ignore[assignment]
    try:
        for defn in (
            get_provider_definition("glm"),
            get_provider_definition("anthropic"),
            get_provider_definition("qwen"),
            get_provider_definition("kimi"),
        ):
            assert defn is not None
            adapter = create_provider(
                provider_definition=defn,
                api_key=SECRET_MARKER,
                model_id=MODEL_MARKER,
            )
            assert adapter is not None
    finally:
        socket.socket = original_socket  # type: ignore[assignment]


# ============================================================================
# 33-35: Factory 源码不导入 SecretStore / Web / ModelClient
# ============================================================================


def _factory_imported_modules() -> set[str]:
    """Parse factory.py via AST and return the set of modules it imports."""
    import ast
    import pathlib

    factory_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "providers"
        / "factory.py"
    )
    tree = ast.parse(factory_path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # 相对导入：还原绝对路径前缀
                if node.level > 0:
                    modules.add(node.module)
                else:
                    modules.add(node.module)
    return modules


def test_factory_source_does_not_import_secret_store() -> None:
    modules = _factory_imported_modules()
    for mod in modules:
        assert "secret_store" not in mod, f"factory imports {mod!r}"
        assert "secrets" not in mod, f"factory imports {mod!r}"


def test_factory_source_does_not_import_web_modules() -> None:
    modules = _factory_imported_modules()
    for mod in modules:
        assert not mod.startswith("pi_agent_core_py.web"), (
            f"factory imports {mod!r}"
        )
        assert "provider_config_service" not in mod, (
            f"factory imports {mod!r}"
        )
        assert "provider_runtime" not in mod, f"factory imports {mod!r}"
        assert "credentials_service" not in mod, f"factory imports {mod!r}"


def test_factory_source_does_not_import_model_client() -> None:
    modules = _factory_imported_modules()
    for mod in modules:
        assert "model_client" not in mod, f"factory imports {mod!r}"


def test_factory_imports_are_minimal_and_local() -> None:
    """Sanity：Factory 只应从同包（providers / errors / base / registry / 三个
    Adapter 模块）import——结构上不应跨出 providers 子包."""
    modules = _factory_imported_modules()
    expected_prefixes = (
        "__future__",
        "anthropic_compat",
        "base",
        "errors",
        "factory",
        "glm",
        "openai_compat",
        "registry",
    )
    for mod in modules:
        # 相对导入（node.level>0）的 module 名是不带前缀的叶子名
        leaf = mod.rsplit(".", 1)[-1]
        assert leaf in expected_prefixes, (
            f"factory imports unexpected module {mod!r}"
        )

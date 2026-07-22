"""ProviderFactory 精确映射 + 配置校验测试（M1-3 §九.1–§九.24）.

覆盖：
- GLM / Anthropic / Qwen / Kimi 精确 Adapter 映射
- provider_id / model / base_url 透传
- ProviderAdapter 契约满足 / 不返回 ModelClient
- openai_compatible 通用复用能力
- 未知 anthropic_compatible 拒绝 / 未知 api_style 拒绝
- provider_id 不被自动 lowercase
- GLM/Anthropic 优先于泛化分支
- 空 / 空白 Key / model / base_url 拒绝
- Pydantic ValidationError 安全映射
- 异常 `from None`
- 入参 Definition 不被修改
- Factory 不保存调用参数
"""
from __future__ import annotations

import copy

import pytest

# 避免从 web 层或 model_client 引入——测试本身也要保持依赖最小
from pi_agent_core_py.model_client import ModelClient  # noqa: F401  # for isinstance check
from pi_agent_core_py.providers.anthropic_compat import (
    AnthropicCompatAdapter,
)
from pi_agent_core_py.providers.base import ProviderAdapter
from pi_agent_core_py.providers.errors import ProviderConfigError
from pi_agent_core_py.providers.factory import (
    UnsupportedProviderError,
    create_provider,
)
from pi_agent_core_py.providers.glm import GLMProviderAdapter
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatibleProvider,
)
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    get_provider_definition,
)

_SECRET = "sk-test-key-not-real"
_MODEL = "test-model-id"


def _glm_def() -> ProviderDefinition:
    d = get_provider_definition("glm")
    assert d is not None
    return d


def _anthropic_def() -> ProviderDefinition:
    d = get_provider_definition("anthropic")
    assert d is not None
    return d


def _qwen_def() -> ProviderDefinition:
    d = get_provider_definition("qwen")
    assert d is not None
    return d


def _kimi_def() -> ProviderDefinition:
    d = get_provider_definition("kimi")
    assert d is not None
    return d


def _custom_openai_compat_def(
    *,
    provider_id: str = "custom-openai",
    base_url: str = "https://custom.example.test/v1",
) -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id,
        display_name="Custom OpenAI-compat",
        api_style="openai_compatible",
        default_base_url=base_url,
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


def _custom_anthropic_compat_def(
    *,
    provider_id: str = "custom-anthropic",
    base_url: str = "https://custom-anthropic.example.test",
) -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id,
        display_name="Custom Anthropic-compat",
        api_style="anthropic_compatible",
        default_base_url=base_url,
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


def _custom_unknown_style_def(
    *,
    api_style: str = "unknown_style",
) -> ProviderDefinition:
    return ProviderDefinition(
        id="custom-unknown",
        display_name="Custom Unknown",
        api_style=api_style,  # type: ignore[arg-type]
        default_base_url="https://custom-unknown.example.test",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


# ============================================================================
# 1-10: Adapter 精确映射 + 字段透传
# ============================================================================


def test_glm_maps_to_glm_adapter() -> None:
    adapter = create_provider(
        provider_definition=_glm_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, GLMProviderAdapter)


def test_anthropic_maps_to_anthropic_compat_adapter() -> None:
    adapter = create_provider(
        provider_definition=_anthropic_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, AnthropicCompatAdapter)
    # GLMProviderAdapter 是 AnthropicCompatAdapter 的子类——精确类型校验
    assert type(adapter) is AnthropicCompatAdapter


def test_qwen_maps_to_openai_compat_adapter() -> None:
    adapter = create_provider(
        provider_definition=_qwen_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, OpenAICompatibleProvider)


def test_kimi_maps_to_openai_compat_adapter() -> None:
    adapter = create_provider(
        provider_definition=_kimi_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, OpenAICompatibleProvider)


def test_qwen_adapter_provider_id_is_qwen() -> None:
    adapter = create_provider(
        provider_definition=_qwen_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert adapter.provider_id == "qwen"


def test_kimi_adapter_provider_id_is_kimi() -> None:
    adapter = create_provider(
        provider_definition=_kimi_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert adapter.provider_id == "kimi"


@pytest.mark.parametrize(
    "defn",
    [_glm_def(), _anthropic_def(), _qwen_def(), _kimi_def()],
)
def test_model_equals_input(defn: ProviderDefinition) -> None:
    adapter = create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert adapter.model == _MODEL


@pytest.mark.parametrize(
    "defn",
    [_glm_def(), _anthropic_def(), _qwen_def(), _kimi_def()],
)
def test_base_url_from_definition(defn: ProviderDefinition) -> None:
    adapter = create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    # 所有 Adapter 都把 config 暴露为 .config（Anthropic / OpenAI-compat 一致）
    cfg = adapter.config  # type: ignore[attr-defined]
    assert cfg.base_url == defn.default_base_url


@pytest.mark.parametrize(
    "defn",
    [_glm_def(), _anthropic_def(), _qwen_def(), _kimi_def()],
)
def test_return_satisfies_provider_adapter_contract(
    defn: ProviderDefinition,
) -> None:
    adapter = create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, ProviderAdapter)


@pytest.mark.parametrize(
    "defn",
    [_glm_def(), _anthropic_def(), _qwen_def(), _kimi_def()],
)
def test_factory_does_not_return_model_client(
    defn: ProviderDefinition,
) -> None:
    adapter = create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert not isinstance(adapter, ModelClient)


# ============================================================================
# 11-15: 协议路由
# ============================================================================


def test_generic_openai_compatible_definition_uses_openai_adapter() -> None:
    """任意来自受信 Registry 的 openai_compatible Definition 复用通用 Adapter."""
    defn = _custom_openai_compat_def()
    adapter = create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(adapter, OpenAICompatibleProvider)
    assert adapter.provider_id == "custom-openai"


def test_unknown_anthropic_compat_definition_rejected() -> None:
    """未知 anthropic_compatible Provider 不得自动套用 Anthropic Adapter."""
    defn = _custom_anthropic_compat_def(provider_id="custom-anthropic")
    with pytest.raises(UnsupportedProviderError):
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )


def test_unknown_api_style_rejected() -> None:
    defn = _custom_unknown_style_def(api_style="totally_unknown")
    with pytest.raises(UnsupportedProviderError):
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )


def test_provider_id_not_lowercased() -> None:
    """provider_id == 'GLM' (uppercase) 不应被自动 lowercase 后匹配到 glm 分支."""
    defn = ProviderDefinition(
        id="GLM",  # uppercase——不应匹配 "glm" 分支
        display_name="Uppercase GLM",
        api_style="anthropic_compatible",
        default_base_url="https://upper.example.test",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(UnsupportedProviderError):
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )


def test_glm_anthropic_precedence_over_generic_branch() -> None:
    """GLM / Anthropic 都注册为 anthropic_compatible api_style——必须命中专门分支，
    而不是落进（不存在的）openai_compatible 兜底分支。"""
    # GLM/Anthropic api_style == "anthropic_compatible"——专门分支必须命中
    glm_adapter = create_provider(
        provider_definition=_glm_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert isinstance(glm_adapter, GLMProviderAdapter)

    anthropic_adapter = create_provider(
        provider_definition=_anthropic_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert type(anthropic_adapter) is AnthropicCompatAdapter


# ============================================================================
# 16-24: 配置校验
# ============================================================================


def test_empty_api_key_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=_qwen_def(),
            api_key="",
            model_id=_MODEL,
        )


def test_whitespace_api_key_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=_qwen_def(),
            api_key="   \t  ",
            model_id=_MODEL,
        )


def test_empty_model_id_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=_qwen_def(),
            api_key=_SECRET,
            model_id="",
        )


def test_whitespace_model_id_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=_qwen_def(),
            api_key=_SECRET,
            model_id="  \t ",
        )


def test_missing_base_url_rejected() -> None:
    defn = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url="",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )


def test_whitespace_base_url_rejected() -> None:
    defn = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url="   ",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError):
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )


def test_pydantic_validation_error_mapped_to_provider_config_error() -> None:
    """OpenAICompatConfig 字段 validator 抛 ValueError → Pydantic ValidationError
    → Factory 必须映射为固定安全的 ProviderConfigError."""
    # 用一个 base_url 不合法（非 http/https scheme）的 Definition 触发
    # OpenAICompatConfig 的 ValidationError；ProviderDefinition 直接构造
    # 不走 registry 校验，便于注入边界条件。
    defn = ProviderDefinition(
        id="custom-openai",
        display_name="Custom",
        api_style="openai_compatible",
        default_base_url="ftp://bad-scheme.example.test",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=defn,
            api_key=_SECRET,
            model_id=_MODEL,
        )
    # 固定安全消息——不含 base_url
    assert str(exc_info.value) == "provider configuration is invalid"


def test_pydantic_validation_error_on_model_control_char_mapped() -> None:
    """model_id 含控制字符 → OpenAICompatConfig ValidationError → 安全映射."""
    with pytest.raises(ProviderConfigError) as exc_info:
        create_provider(
            provider_definition=_qwen_def(),
            api_key=_SECRET,
            model_id="model\x00bad",
        )
    assert str(exc_info.value) == "provider configuration is invalid"


def test_factory_exceptions_use_from_none() -> None:
    """所有 Factory 抛出的异常必须 `from None`——__cause__ is None."""
    cases: list[tuple[ProviderDefinition, str, str]] = [
        (_qwen_def(), "", _MODEL),
        (_qwen_def(), "   ", _MODEL),
        (_qwen_def(), _SECRET, ""),
        (_qwen_def(), _SECRET, "  "),
        (_custom_anthropic_compat_def(), _SECRET, _MODEL),
        (_custom_unknown_style_def(), _SECRET, _MODEL),
    ]
    for defn, key, model in cases:
        with pytest.raises(ProviderConfigError) as exc_info:
            create_provider(
                provider_definition=defn,
                api_key=key,
                model_id=model,
            )
        assert exc_info.value.__cause__ is None, (
            f"__cause__ must be None for {defn.id!r} + key={key!r} + model={model!r}"
        )


def test_input_definition_not_mutated() -> None:
    defn = _qwen_def()
    snapshot = copy.deepcopy(defn)
    create_provider(
        provider_definition=defn,
        api_key=_SECRET,
        model_id=_MODEL,
    )
    # frozen dataclass——字段不可变；这里验证整体相等
    assert defn == snapshot


def test_factory_does_not_save_arguments() -> None:
    """Factory 是无状态同步函数——多次调用互不影响；模块层不持有调用参数."""
    import pi_agent_core_py.providers.factory as factory_mod

    # 模块层不应有 api_key / model / adapter 之类的属性
    forbidden_attrs = {"api_key", "model_id", "_last_provider", "_cache", "_instances"}
    for attr in forbidden_attrs:
        assert not hasattr(factory_mod, attr), f"factory module leaks state: {attr!r}"

    # 两次调用应返回不同实例（不缓存）
    a1 = create_provider(
        provider_definition=_qwen_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    a2 = create_provider(
        provider_definition=_qwen_def(),
        api_key=_SECRET,
        model_id=_MODEL,
    )
    assert a1 is not a2

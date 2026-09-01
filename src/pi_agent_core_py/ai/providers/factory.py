"""Provider adapter factory.

无状态同步工厂——把 (ProviderDefinition, api_key, model_id) 三元组映射到对应的
ProviderAdapter 实例。这是项目里**唯一**知道「哪个 Provider 用哪个 Adapter」的
位置；其它模块通过此函数获得 Adapter，不自行决定映射关系。

职责边界（M1-3 仅负责构造）：
- ✅ 读取 ProviderDefinition（id / api_style / default_base_url）
- ✅ 校验 api_key / model_id 非空
- ✅ 构造对应 Adapter（GLM / Anthropic / OpenAI-compatible）
- ❌ 不读 Credential / SecretStore（M1-4 负责）
- ❌ 不解析 Session Binding（M1-4 负责）
- ❌ 不包装 ModelClient（M1-4 负责）
- ❌ 不绑定 Harness（M1-4 负责）
- ❌ 不调用 stream() / aclose()（runtime 负责）
- ❌ 不缓存 Adapter（每次返回新实例）
- ❌ 不做自动 fallback
- ❌ 不发任何网络请求（构造阶段纯本地）

固定映射：
- `provider_id == "glm"`           → GLMProviderAdapter
- `provider_id == "anthropic"`     → AnthropicCompatAdapter
- `api_style == "openai_compatible"` → OpenAICompatibleProvider
- 其它                              → UnsupportedProviderError

GLM / Anthropic 分支**必须**先于 api_style 分支判断——否则 Anthropic-compatible
的 GLM/Anthropic 会被误路由到 OpenAI-compatible Adapter。未知
`anthropic_compatible` Definition（既非 glm 也非 anthropic）必须拒绝，
**不得**自动套用 Anthropic Adapter。

安全约束：
- API Key 仅用于构造 Config，不保存为 Factory 字段、不写日志、不进异常消息
- 所有包装异常使用 `from None`——中断 `__cause__` 链，避免 SDK /
  Pydantic 原始错误文本泄漏
- 不修改入参 ProviderDefinition（frozen dataclass，亦不应被本地修改）
"""
from __future__ import annotations

from .anthropic_compat import AnthropicCompatAdapter, AnthropicCompatConfig
from .base import ProviderAdapter
from .errors import ProviderConfigError
from .glm import GLMConfig, GLMProviderAdapter
from .openai_compat import OpenAICompatConfig, OpenAICompatibleProvider
from .registry import ProviderDefinition

__all__ = [
    "UnsupportedProviderError",
    "create_provider",
]


class UnsupportedProviderError(ProviderConfigError):
    """ProviderDefinition 没有受支持的 Adapter 映射."""


_MSG_CREDENTIAL_UNAVAILABLE = "provider credential is unavailable"
_MSG_MODEL_INVALID = "provider model is invalid"
_MSG_ENDPOINT_UNAVAILABLE = "provider endpoint is unavailable"
_MSG_CONFIG_INVALID = "provider configuration is invalid"
_MSG_UNSUPPORTED = "provider is unsupported"


def create_provider(
    *,
    provider_definition: ProviderDefinition,
    api_key: str,
    model_id: str,
) -> ProviderAdapter:
    """Construct a ProviderAdapter from a definition + credential + model.

    同步、无状态、无网络。每次调用返回新的 Adapter 实例——不缓存、不单例。
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderConfigError(_MSG_CREDENTIAL_UNAVAILABLE) from None
    if not isinstance(model_id, str) or not model_id.strip():
        raise ProviderConfigError(_MSG_MODEL_INVALID) from None

    base_url = provider_definition.default_base_url
    if not base_url or not base_url.strip():
        raise ProviderConfigError(_MSG_ENDPOINT_UNAVAILABLE) from None

    provider_id = provider_definition.id

    try:
        if provider_id == "glm":
            return GLMProviderAdapter(
                GLMConfig(
                    api_key=api_key,
                    base_url=base_url,
                    model=model_id,
                ),
            )

        if provider_id == "anthropic":
            return AnthropicCompatAdapter(
                AnthropicCompatConfig(
                    api_key=api_key,
                    base_url=base_url,
                    model=model_id,
                ),
                provider_id="anthropic",
                supports_images=True,
            )

        if provider_definition.api_style == "openai_compatible":
            return OpenAICompatibleProvider(
                OpenAICompatConfig(
                    api_key=api_key,
                    base_url=base_url,
                    model=model_id,
                ),
                provider_id=provider_id,
            )
    except ProviderConfigError:
        raise
    except Exception:
        raise ProviderConfigError(_MSG_CONFIG_INVALID) from None

    raise UnsupportedProviderError(_MSG_UNSUPPORTED) from None

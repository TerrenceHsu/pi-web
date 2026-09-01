"""GLM provider adapter and configuration.

智谱 GLM 通过 Anthropic-compatible 端点（`https://open.bigmodel.cn/api/anthropic`）
对外提供服务——直接继承 `AnthropicCompatAdapter`，仅覆盖 `provider_id`。

环境变量优先级（高 → 低）：

```
api_key:   显式参数 > GLM_API_KEY > ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY
base_url:  显式参数 > GLM_BASE_URL > ANTHROPIC_BASE_URL > BASE_URL > 默认（智谱官方兼容端点）
model:     显式参数 > GLM_MODEL > ANTHROPIC_MODEL > MODEL > 默认 glm-4.5-flash
```

`GLMClient` 是 `ModelClient` 的子类，构造 `GLMProviderAdapter` 后委托给它。
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import model_validator

from .anthropic_compat import AnthropicCompatAdapter, AnthropicCompatConfig
from .errors import ProviderConfigError

# ============================================================================
# 默认值
# ============================================================================

#: 智谱 GLM 官方 Anthropic 兼容端点。
_DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/anthropic"

#: 默认模型——与项目其它 demo / .env 保持一致。
_DEFAULT_GLM_MODEL = "glm-4.5-flash"


# ============================================================================
# Config
# ============================================================================


class GLMConfig(AnthropicCompatConfig):
    """GLM adapter 配置。

    `provider_id` 字段固定为 `"glm"`——通过子类化 `AnthropicCompatConfig`
    标识 provider 身份，便于上层在 audit / metadata 中区分。
    """

    provider_id: str = "glm"

    @model_validator(mode="after")
    def _check_required(self) -> GLMConfig:
        if not self.api_key:
            raise ProviderConfigError(
                "GLMConfig.api_key 不能为空——请设置 GLM_API_KEY 或 ANTHROPIC_AUTH_TOKEN",
            )
        if not self.model:
            raise ProviderConfigError("GLMConfig.model 不能为空")
        return self


# ============================================================================
# Adapter
# ============================================================================


class GLMProviderAdapter(AnthropicCompatAdapter):
    """智谱 GLM via Anthropic-compatible 协议。

    除 `provider_id="glm"` 外，行为与 `AnthropicCompatAdapter` 完全一致。
    """

    provider_id = "glm"
    # The configured GLM Anthropic-compatible endpoint is text-only unless a
    # concrete deployment advertises vision support.
    supports_images = False


# ============================================================================
# Env-var resolver（GLMClient thin wrapper 用）
# ============================================================================


def resolve_glm_credentials(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """解析 GLM 凭证——按优先级合并显式参数 + 环境变量。

    返回 dict（api_key / base_url / model）。若缺关键凭证，抛 ProviderConfigError。
    """
    resolved_key = (
        api_key
        or os.environ.get("GLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    if not resolved_key:
        raise ProviderConfigError(
            "未找到 GLM 凭证。请在 .env 中配置 GLM_API_KEY 或 ANTHROPIC_AUTH_TOKEN。",
        )

    resolved_base = (
        base_url
        or os.environ.get("GLM_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or os.environ.get("BASE_URL")
        or _DEFAULT_GLM_BASE_URL
    )

    resolved_model = (
        model
        or os.environ.get("GLM_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("MODEL")
        or _DEFAULT_GLM_MODEL
    )

    return {"api_key": resolved_key, "base_url": resolved_base, "model": resolved_model}


__all__ = [
    "GLMConfig",
    "GLMProviderAdapter",
    "resolve_glm_credentials",
]

"""Step 21 — GLMClient / GLMConfig 凭证解析 + 向后兼容。

不调用真实 GLM API。

覆盖：
- 显式参数优先于 env
- GLM_API_KEY 可用
- ANTHROPIC_AUTH_TOKEN fallback 可用
- GLM_MODEL / ANTHROPIC_MODEL fallback 可用
- 缺所有 key → ProviderConfigError
- GLMClient 是 ModelClient 子类
- 旧 keyword `auth_token=` 仍可用
- 默认 base_url 是智谱官方兼容端点
"""
from __future__ import annotations

import pytest

from pi_agent_core_py import (
    GLMClient,
    GLMConfig,
    GLMProviderAdapter,
    ModelClient,
    ProviderConfigError,
    ProviderError,
)
from pi_agent_core_py.providers.glm import resolve_glm_credentials

# ============================================================================
# resolve_glm_credentials
# ============================================================================


def test_resolve_prefers_explicit_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "from-glm-env")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-anthropic-env")

    creds = resolve_glm_credentials(api_key="explicit")
    assert creds["api_key"] == "explicit"


def test_resolve_uses_glm_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "from-glm-env")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-anthropic-env")

    creds = resolve_glm_credentials()
    assert creds["api_key"] == "from-glm-env"


def test_resolve_falls_back_to_anthropic_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 env 变量 ANTHROPIC_AUTH_TOKEN 仍可用（不突然失效）。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-anthropic-env")

    creds = resolve_glm_credentials()
    assert creds["api_key"] == "from-anthropic-env"


def test_resolve_falls_back_to_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """第三优先级：ANTHROPIC_API_KEY。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-anthropic-api-key")

    creds = resolve_glm_credentials()
    assert creds["api_key"] == "from-anthropic-api-key"


def test_resolve_raises_when_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ProviderConfigError):
        resolve_glm_credentials()


def test_resolve_default_base_url_is_glm_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """没显式 base_url，没 env → 用默认智谱端点。"""
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.setenv("GLM_API_KEY", "x")

    creds = resolve_glm_credentials()
    assert creds["base_url"] == "https://open.bigmodel.cn/api/anthropic"


def test_resolve_uses_unprefixed_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """BASE_URL（无前缀，旧 .env 习惯）也能读——优先级最低的 env fallback。"""
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("BASE_URL", "https://unprefixed.test")
    monkeypatch.setenv("GLM_API_KEY", "x")

    creds = resolve_glm_credentials()
    assert creds["base_url"] == "https://unprefixed.test"


def test_resolve_uses_unprefixed_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODEL（无前缀）也能读。"""
    monkeypatch.delenv("GLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("MODEL", "unprefixed-model")
    monkeypatch.setenv("GLM_API_KEY", "x")

    creds = resolve_glm_credentials()
    assert creds["model"] == "unprefixed-model"


def test_resolve_prefers_prefixed_over_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLM_BASE_URL > BASE_URL；GLM_MODEL > MODEL。"""
    monkeypatch.setenv("GLM_API_KEY", "x")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm.test")
    monkeypatch.setenv("BASE_URL", "https://unprefixed.test")
    monkeypatch.setenv("GLM_MODEL", "glm-prefixed")
    monkeypatch.setenv("MODEL", "unprefixed")

    creds = resolve_glm_credentials()
    assert creds["base_url"] == "https://glm.test"
    assert creds["model"] == "glm-prefixed"


def test_resolve_prefers_glm_base_url_over_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "x")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm.test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.test")

    creds = resolve_glm_credentials()
    assert creds["base_url"] == "https://glm.test"


def test_resolve_model_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """model: 显式 > GLM_MODEL > ANTHROPIC_MODEL > 默认 glm-4.5-flash。"""
    monkeypatch.setenv("GLM_API_KEY", "x")

    # 默认
    monkeypatch.delenv("GLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert resolve_glm_credentials()["model"] == "glm-4.5-flash"

    # ANTHROPIC_MODEL
    monkeypatch.setenv("ANTHROPIC_MODEL", "legacy-model")
    assert resolve_glm_credentials()["model"] == "legacy-model"

    # GLM_MODEL 优先于 ANTHROPIC_MODEL
    monkeypatch.setenv("GLM_MODEL", "glm-new")
    assert resolve_glm_credentials()["model"] == "glm-new"

    # 显式最高
    assert resolve_glm_credentials(model="explicit")["model"] == "explicit"


# ============================================================================
# GLMConfig
# ============================================================================


def test_glm_config_default_provider_id_is_glm() -> None:
    cfg = GLMConfig(api_key="x", model="m")
    assert cfg.provider_id == "glm"


def test_glm_config_missing_api_key_raises() -> None:
    with pytest.raises(ProviderConfigError):
        GLMConfig(api_key="", model="m")


def test_glm_config_missing_model_raises() -> None:
    with pytest.raises(ProviderConfigError):
        GLMConfig(api_key="x", model="")


# ============================================================================
# GLMClient 向后兼容
# ============================================================================


def test_glm_client_is_model_client_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "x")
    client = GLMClient()
    assert isinstance(client, ModelClient)
    assert client.provider_id == "glm"
    assert client.api_id == "anthropic-messages"


def test_glm_client_uses_adapter_internally(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLMClient 内部持有 GLMProviderAdapter。"""
    monkeypatch.setenv("GLM_API_KEY", "x")
    client = GLMClient()
    assert isinstance(client.adapter, GLMProviderAdapter)
    assert client.adapter.provider_id == "glm"


def test_glm_client_legacy_auth_token_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 keyword auth_token='...' 不破坏。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = GLMClient(auth_token="legacy-token")
    assert client.adapter.config.api_key == "legacy-token"


def test_glm_client_new_api_key_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 keyword api_key='...' 优先于 auth_token。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client = GLMClient(api_key="new", auth_token="old")
    assert client.adapter.config.api_key == "new"


def test_glm_client_legacy_anthropic_env_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 .env 用 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://legacy.test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "legacy-model")

    client = GLMClient()
    assert client.adapter.config.api_key == "legacy-token"
    assert client.adapter.config.base_url == "https://legacy.test"
    assert client.adapter.config.model == "legacy-model"


def test_glm_client_new_glm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 .env 用 GLM_API_KEY / GLM_BASE_URL / GLM_MODEL。"""
    monkeypatch.setenv("GLM_API_KEY", "glm-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm.test")
    monkeypatch.setenv("GLM_MODEL", "glm-4.5-air")

    client = GLMClient()
    assert client.adapter.config.api_key == "glm-key"
    assert client.adapter.config.base_url == "https://glm.test"
    assert client.adapter.config.model == "glm-4.5-air"


def test_glm_client_no_credential_raises_provider_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺所有凭证 → 清晰的 ProviderConfigError，不是 RuntimeError。"""
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ProviderError) as exc_info:
        GLMClient()
    assert isinstance(exc_info.value, ProviderConfigError)


def test_glm_client_default_max_tokens_backcompat(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLMClient(max_tokens=1024) 旧默认值保留。"""
    monkeypatch.setenv("GLM_API_KEY", "x")
    client = GLMClient()
    assert client.max_tokens == 1024
    assert client.adapter.config.max_tokens == 1024


def test_glm_client_custom_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_API_KEY", "x")
    client = GLMClient(max_tokens=2048)
    assert client.adapter.config.max_tokens == 2048

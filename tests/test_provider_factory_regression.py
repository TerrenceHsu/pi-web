"""ProviderFactory 回归测试（M1-3 §九.36–§九.41）.

验证 M1-3 新增 factory.py 没有破坏：
- M1-1 OpenAI-compatible Adapter 模块
- M1-2 Qwen / Kimi preset
- 现有 GLM / Anthropic / model_client / registry 模块
- E1 Credential 子系统
- E2 Profile / Binding 子系统
- Core Runtime

具体的全量 124 + 64 + E1 + E2 回归通过完整 pytest 命令执行（见 §十一.验证）；
本文件只做结构性 smoke——证明关键模块在 factory 加入后仍可正常导入 + 直接构造。
"""
from __future__ import annotations

from pi_agent_core_py.providers.anthropic_compat import (
    AnthropicCompatAdapter,
    AnthropicCompatConfig,
)
from pi_agent_core_py.providers.factory import create_provider
from pi_agent_core_py.providers.glm import GLMConfig, GLMProviderAdapter
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from pi_agent_core_py.providers.registry import (
    ProviderRegistry,
    list_provider_definitions,
)

# ============================================================================
# M1-1: OpenAI-compatible Adapter 仍可直接构造
# ============================================================================


def test_m1_1_openai_compat_adapter_still_constructable() -> None:
    cfg = OpenAICompatConfig(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model="qwen-plus",
    )
    adapter = OpenAICompatibleProvider(cfg, provider_id="qwen")
    assert adapter.provider_id == "qwen"
    assert adapter.model == "qwen-plus"


def test_m1_1_openai_compat_via_factory_matches_direct_construction() -> None:
    """Factory 路径与直接构造路径产出 Adapter 类型一致 + 字段一致."""
    from pi_agent_core_py.providers.registry import get_provider_definition

    defn = get_provider_definition("qwen")
    assert defn is not None

    via_factory = create_provider(
        provider_definition=defn,
        api_key="sk-test",
        model_id="qwen-plus",
    )
    cfg = OpenAICompatConfig(
        api_key="sk-test",
        base_url=defn.default_base_url,
        model="qwen-plus",
    )
    direct = OpenAICompatibleProvider(cfg, provider_id="qwen")

    assert type(via_factory) is type(direct)
    assert via_factory.provider_id == direct.provider_id
    assert via_factory.model == direct.model
    assert via_factory.config.base_url == direct.config.base_url  # type: ignore[attr-defined]


# ============================================================================
# M1-2: Qwen / Kimi preset 仍可访问
# ============================================================================


def test_m1_2_registry_still_lists_four_providers() -> None:
    defs = list_provider_definitions()
    ids = {d.id for d in defs}
    assert ids == {"glm", "anthropic", "qwen", "kimi"}


def test_m1_2_qwen_kimi_definitions_intact() -> None:
    from pi_agent_core_py.providers.registry import get_provider_definition

    qwen = get_provider_definition("qwen")
    kimi = get_provider_definition("kimi")
    assert qwen is not None and kimi is not None
    assert qwen.api_style == "openai_compatible"
    assert kimi.api_style == "openai_compatible"
    assert qwen.default_base_url.startswith("https://")
    assert kimi.default_base_url.startswith("https://")


def test_m1_2_registry_construction_still_strict() -> None:
    """ProviderRegistry 构造校验未被任何 M1-3 改动削弱."""
    import pytest

    from pi_agent_core_py.providers.registry import ProviderDefinition

    # 重复 id 必须拒绝
    d = ProviderDefinition(
        id="glm",  # duplicate
        display_name="dup",
        api_style="anthropic_compatible",
        default_base_url="https://example.test",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )
    with pytest.raises(ValueError):
        ProviderRegistry(
            (
                ProviderDefinition(
                    id="glm",
                    display_name="orig",
                    api_style="anthropic_compatible",
                    default_base_url="https://open.bigmodel.cn/api/anthropic",
                    credential_validation_strategy="unsupported",
                    credential_validation_endpoint=None,
                    supports_model_listing=False,
                ),
                d,
            ),
        )


# ============================================================================
# 现有 GLM / Anthropic 仍可直接构造
# ============================================================================


def test_glm_adapter_direct_construction_unchanged() -> None:
    cfg = GLMConfig(
        api_key="test-glm-key",
        base_url="https://open.bigmodel.cn/api/anthropic",
        model="glm-4.5-flash",
    )
    adapter = GLMProviderAdapter(cfg)
    assert adapter.provider_id == "glm"
    assert adapter.model == "glm-4.5-flash"


def test_anthropic_adapter_direct_construction_unchanged() -> None:
    cfg = AnthropicCompatConfig(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        model="claude-3-5-sonnet-20240620",
    )
    adapter = AnthropicCompatAdapter(cfg)
    assert adapter.provider_id == "anthropic_compat"
    assert adapter.model == "claude-3-5-sonnet-20240620"


# ============================================================================
# Core Runtime 文件未受 factory 加入影响（结构性 smoke）
# ============================================================================


def test_core_runtime_modules_still_importable() -> None:
    """factory.py 不修改 Core Runtime——loop / agent / context / events /
    stream_events / messages 模块应原样可导入."""
    import pi_agent_core_py.agent as agent_mod
    import pi_agent_core_py.context as context_mod
    import pi_agent_core_py.events as events_mod
    import pi_agent_core_py.loop as loop_mod
    import pi_agent_core_py.messages as messages_mod
    import pi_agent_core_py.stream_events as stream_events_mod

    # 触发属性访问以消除 unused warning
    assert agent_mod is not None
    assert context_mod is not None
    assert events_mod is not None
    assert loop_mod is not None
    assert messages_mod is not None
    assert stream_events_mod is not None


def test_e1_credential_modules_still_importable() -> None:
    """E1 Credential 子系统模块未受 factory 加入影响."""
    import pi_agent_core_py.secrets.memory as mem_mod
    import pi_agent_core_py.web.credentials_service as svc_mod
    import pi_agent_core_py.web.credentials_store as store_mod

    assert mem_mod is not None
    assert svc_mod is not None
    assert store_mod is not None


def test_e2_profile_binding_modules_still_importable() -> None:
    """E2 Profile / Binding 子系统模块未受 factory 加入影响."""
    import pi_agent_core_py.web.provider_config_service as svc_mod
    import pi_agent_core_py.web.provider_config_store as store_mod

    assert svc_mod is not None
    assert store_mod is not None

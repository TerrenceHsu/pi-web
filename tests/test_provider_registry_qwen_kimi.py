"""Qwen / Kimi ProviderDefinition preset tests（M1-2 §十.Registry）.

覆盖 14 项：
- qwen / kimi 存在
- id 精确小写
- 两者均 openai_compatible
- base_url 精确
- credential_validation_strategy="unsupported"
- credential_validation_endpoint=None
- supports_model_listing=False
- key_prefix_hints=()
- Registry 无重复 ID
- 原 GLM / Anthropic Definition 不变
- Registry 列表顺序稳定：glm → anthropic → qwen → kimi
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
    get_provider_definition,
    list_provider_definitions,
)

# ============================================================================
# 存在性
# ============================================================================


class TestQwenKimiExist:
    def test_qwen_definition_exists(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.id == "qwen"

    def test_kimi_definition_exists(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.id == "kimi"


# ============================================================================
# id 精确小写
# ============================================================================


class TestIdLowercase:
    def test_qwen_id_is_lowercase(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.id == "qwen"
        assert d.id.islower()

    def test_kimi_id_is_lowercase(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.id == "kimi"
        assert d.id.islower()


# ============================================================================
# api_style
# ============================================================================


class TestApiStyle:
    def test_qwen_is_openai_compatible(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.api_style == "openai_compatible"

    def test_kimi_is_openai_compatible(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.api_style == "openai_compatible"


# ============================================================================
# base_url 精确
# ============================================================================


class TestBaseUrl:
    def test_qwen_base_url_is_beijing_shared_dashscope(self) -> None:
        """Qwen M1 preset: China Beijing shared endpoint only."""
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.default_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_kimi_base_url_is_moonshot(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.default_base_url == "https://api.moonshot.cn/v1"

    def test_qwen_base_url_is_https(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.default_base_url.startswith("https://")

    def test_kimi_base_url_is_https(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.default_base_url.startswith("https://")

    def test_qwen_base_url_has_no_userinfo(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert "@" not in d.default_base_url.split("://", 1)[-1].split("/", 1)[0]

    def test_kimi_base_url_has_no_userinfo(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert "@" not in d.default_base_url.split("://", 1)[-1].split("/", 1)[0]


# ============================================================================
# credential validation
# ============================================================================


class TestCredentialValidation:
    def test_qwen_uses_unsupported_strategy(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.credential_validation_strategy == "unsupported"

    def test_kimi_uses_unsupported_strategy(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.credential_validation_strategy == "unsupported"

    def test_qwen_validation_endpoint_is_none(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.credential_validation_endpoint is None

    def test_kimi_validation_endpoint_is_none(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.credential_validation_endpoint is None


# ============================================================================
# supports_model_listing
# ============================================================================


class TestModelListing:
    def test_qwen_does_not_support_model_listing(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.supports_model_listing is False

    def test_kimi_does_not_support_model_listing(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.supports_model_listing is False


# ============================================================================
# key_prefix_hints
# ============================================================================


class TestKeyPrefixHints:
    def test_qwen_has_no_key_prefix_hints(self) -> None:
        """sk- 不能区分 Qwen / Kimi / OpenAI——避免 heuristic 错误推荐."""
        d = get_provider_definition("qwen")
        assert d is not None
        assert d.key_prefix_hints == ()

    def test_kimi_has_no_key_prefix_hints(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        assert d.key_prefix_hints == ()


# ============================================================================
# Registry 不变量
# ============================================================================


class TestRegistryInvariants:
    def test_registry_has_no_duplicate_ids(self) -> None:
        ids = [d.id for d in list_provider_definitions()]
        assert len(ids) == len(set(ids))

    def test_registry_order_is_stable(self) -> None:
        """M1-2 保留 GLM/Anthropic 顺序，追加 Qwen/Kimi 在末尾."""
        ids = [d.id for d in list_provider_definitions()]
        assert ids[:2] == ["glm", "anthropic"]
        assert ids[2:] == ["qwen", "kimi"]

    def test_registry_has_at_least_four_providers(self) -> None:
        defs = list_provider_definitions()
        assert len(defs) >= 4

    def test_registry_rejects_duplicate_qwen(self) -> None:
        """重复 qwen ID 拒绝（_validate_definition 内部校验）."""
        qwen = get_provider_definition("qwen")
        assert qwen is not None
        with pytest.raises(ValueError, match="duplicate provider id"):
            ProviderRegistry((qwen, qwen))


# ============================================================================
# 原 GLM / Anthropic 不变
# ============================================================================


class TestLegacyDefinitionsUnchanged:
    def test_glm_definition_unchanged(self) -> None:
        d = get_provider_definition("glm")
        assert d is not None
        assert d.id == "glm"
        assert d.display_name == "Zhipu GLM (Anthropic-compatible)"
        assert d.api_style == "anthropic_compatible"
        assert d.default_base_url == "https://open.bigmodel.cn/api/anthropic"
        assert d.credential_validation_strategy == "unsupported"
        assert d.credential_validation_endpoint is None
        assert d.supports_model_listing is False
        assert d.key_prefix_hints == ()

    def test_anthropic_definition_unchanged(self) -> None:
        d = get_provider_definition("anthropic")
        assert d is not None
        assert d.id == "anthropic"
        assert d.display_name == "Anthropic"
        assert d.api_style == "anthropic_compatible"
        assert d.default_base_url == "https://api.anthropic.com"
        assert d.credential_validation_strategy == "anthropic_models"
        assert d.credential_validation_endpoint == "https://api.anthropic.com/v1/models"
        assert d.supports_model_listing is True
        assert d.key_prefix_hints == ("sk-ant-",)


# ============================================================================
# Frozen dataclass 不可变
# ============================================================================


class TestDefinitionsAreFrozen:
    def test_qwen_definition_is_frozen(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        with pytest.raises(AttributeError):
            d.id = "tampered"  # type: ignore[misc]

    def test_kimi_definition_is_frozen(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        with pytest.raises(AttributeError):
            d.id = "tampered"  # type: ignore[misc]

    def test_qwen_base_url_immutable(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        with pytest.raises(AttributeError):
            d.default_base_url = "https://evil.test"  # type: ignore[misc]


# ============================================================================
# 模块级 ProviderDefinition 直接引用（防意外漂移）
# ============================================================================


class TestPresetDefinitionsModuleLevel:
    """M1-2 文档冻结 _QWEN_DEFINITION / _KIMI_DEFINITION 字段——防意外漂移."""

    def test_qwen_definition_field_snapshot(self) -> None:
        d = get_provider_definition("qwen")
        assert d is not None
        # 冻结字段全集——任何字段变化需显式更新本测试
        snapshot: dict[str, object] = {
            "id": d.id,
            "display_name": d.display_name,
            "api_style": d.api_style,
            "default_base_url": d.default_base_url,
            "credential_validation_strategy": d.credential_validation_strategy,
            "credential_validation_endpoint": d.credential_validation_endpoint,
            "supports_model_listing": d.supports_model_listing,
            "key_prefix_hints": d.key_prefix_hints,
        }
        assert snapshot == {
            "id": "qwen",
            "display_name": "Qwen",
            "api_style": "openai_compatible",
            "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "credential_validation_strategy": "unsupported",
            "credential_validation_endpoint": None,
            "supports_model_listing": False,
            "key_prefix_hints": (),
        }

    def test_kimi_definition_field_snapshot(self) -> None:
        d = get_provider_definition("kimi")
        assert d is not None
        snapshot: dict[str, object] = {
            "id": d.id,
            "display_name": d.display_name,
            "api_style": d.api_style,
            "default_base_url": d.default_base_url,
            "credential_validation_strategy": d.credential_validation_strategy,
            "credential_validation_endpoint": d.credential_validation_endpoint,
            "supports_model_listing": d.supports_model_listing,
            "key_prefix_hints": d.key_prefix_hints,
        }
        assert snapshot == {
            "id": "kimi",
            "display_name": "Kimi",
            "api_style": "openai_compatible",
            "default_base_url": "https://api.moonshot.cn/v1",
            "credential_validation_strategy": "unsupported",
            "credential_validation_endpoint": None,
            "supports_model_listing": False,
            "key_prefix_hints": (),
        }

    def test_qwen_and_kimi_share_openai_compatible_style(self) -> None:
        """两者共用 OpenAICompatibleProvider（M1-3 Factory 路由依据）."""
        qwen = get_provider_definition("qwen")
        kimi = get_provider_definition("kimi")
        assert qwen is not None and kimi is not None
        assert qwen.api_style == kimi.api_style == "openai_compatible"

    def test_qwen_kimi_are_providerdefinition_instances(self) -> None:
        qwen = get_provider_definition("qwen")
        kimi = get_provider_definition("kimi")
        assert isinstance(qwen, ProviderDefinition)
        assert isinstance(kimi, ProviderDefinition)

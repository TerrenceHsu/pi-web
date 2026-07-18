"""Provider Registry 单元测试（P1-E1-1 + E1-3B1）.

覆盖：
- list_provider_definitions 返回所有内置定义
- get known / get unknown
- has 检查
- 重复 ID 拒绝
- 非 HTTPS default_base_url 拒绝
- URL 内嵌 userinfo 拒绝
- 内置定义都是 HTTPS / 无 userinfo
- 不创建网络连接（monkeypatch network）
- credential_validation_strategy ↔ endpoint 一致性（E1-3B1）
- Anthropic strategy=anthropic_models + endpoint HTTPS
- GLM strategy=unsupported + endpoint=None
- unsupported 携带 endpoint 拒绝
- 非 unsupported 缺 endpoint 拒绝
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.providers.registry import (
    CredentialValidationStrategyId,
    ProviderAPIStyle,
    ProviderDefinition,
    ProviderRegistry,
    get_provider_definition,
    list_provider_definitions,
)

# ============================================================================
# Built-in registry
# ============================================================================


class TestBuiltInRegistry:
    def test_list_returns_definitions(self) -> None:
        defs = list_provider_definitions()
        assert len(defs) >= 2
        ids = {d.id for d in defs}
        assert "glm" in ids
        assert "anthropic" in ids

    def test_get_known_provider(self) -> None:
        d = get_provider_definition("glm")
        assert d is not None
        assert d.id == "glm"
        assert d.api_style == "anthropic_compatible"
        assert d.default_base_url == "https://open.bigmodel.cn/api/anthropic"

    def test_get_anthropic_definition(self) -> None:
        d = get_provider_definition("anthropic")
        assert d is not None
        assert d.id == "anthropic"
        assert d.key_prefix_hints == ("sk-ant-",)

    def test_get_unknown_returns_none(self) -> None:
        assert get_provider_definition("nonexistent") is None

    def test_all_built_in_base_urls_are_https(self) -> None:
        for d in list_provider_definitions():
            assert d.default_base_url.startswith("https://"), (
                f"{d.id}.default_base_url must be HTTPS"
            )

    def test_no_built_in_has_url_userinfo(self) -> None:
        for d in list_provider_definitions():
            assert "@" not in d.default_base_url.split("://", 1)[-1].split("/", 1)[0]

    def test_definitions_are_frozen(self) -> None:
        """ProviderDefinition 是 frozen dataclass——不可变."""
        d = get_provider_definition("glm")
        assert d is not None
        # frozen dataclass 抛 FrozenInstanceError（AttributeError 子类）
        with pytest.raises(AttributeError):
            d.id = "tampered"  # type: ignore[misc]


# ============================================================================
# E1-3B1: credential_validation_strategy / endpoint
# ============================================================================


class TestCredentialValidationFields:
    def test_anthropic_uses_anthropic_models_strategy(self) -> None:
        d = get_provider_definition("anthropic")
        assert d is not None
        assert d.credential_validation_strategy == "anthropic_models"
        assert d.credential_validation_endpoint == "https://api.anthropic.com/v1/models"
        assert d.supports_model_listing is True

    def test_glm_uses_unsupported_strategy(self) -> None:
        d = get_provider_definition("glm")
        assert d is not None
        assert d.credential_validation_strategy == "unsupported"
        assert d.credential_validation_endpoint is None
        assert d.supports_model_listing is False

    def test_anthropic_endpoint_has_no_userinfo(self) -> None:
        d = get_provider_definition("anthropic")
        assert d is not None
        endpoint = d.credential_validation_endpoint
        assert endpoint is not None
        assert endpoint.startswith("https://")
        assert "@" not in endpoint.split("://", 1)[-1].split("/", 1)[0]


# ============================================================================
# Custom registry construction
# ============================================================================


class TestCustomRegistry:
    def _make_def(self, **overrides: object) -> ProviderDefinition:
        defaults: dict[str, object] = {
            "id": "test-provider",
            "display_name": "Test",
            "api_style": "anthropic_compatible",
            "default_base_url": "https://example.com",
            "credential_validation_strategy": "unsupported",
            "credential_validation_endpoint": None,
            "supports_model_listing": False,
            "key_prefix_hints": (),
        }
        defaults.update(overrides)
        return ProviderDefinition(**defaults)  # type: ignore[arg-type]

    def test_construct_valid_registry(self) -> None:
        d = self._make_def()
        reg = ProviderRegistry((d,))
        assert reg.has("test-provider")
        assert reg.get("test-provider") == d
        assert reg.list() == (d,)

    def test_duplicate_id_rejected(self) -> None:
        d = self._make_def()
        with pytest.raises(ValueError, match="duplicate provider id"):
            ProviderRegistry((d, d))

    def test_non_https_default_base_url_rejected(self) -> None:
        d = self._make_def(default_base_url="http://insecure.example.com")
        with pytest.raises(ValueError, match="HTTPS"):
            ProviderRegistry((d,))

    def test_non_https_validation_endpoint_rejected(self) -> None:
        d = self._make_def(
            credential_validation_strategy="anthropic_models",
            credential_validation_endpoint="http://insecure.example.com/v1/models",
        )
        with pytest.raises(ValueError, match="HTTPS"):
            ProviderRegistry((d,))

    def test_url_with_userinfo_rejected(self) -> None:
        d = self._make_def(
            default_base_url="https://user:pass@example.com",
        )
        with pytest.raises(ValueError, match="userinfo"):
            ProviderRegistry((d,))

    def test_validation_endpoint_userinfo_rejected(self) -> None:
        d = self._make_def(
            credential_validation_strategy="anthropic_models",
            credential_validation_endpoint="https://user:pass@example.com/v1/models",
        )
        with pytest.raises(ValueError, match="userinfo"):
            ProviderRegistry((d,))

    def test_empty_id_rejected(self) -> None:
        d = self._make_def(id="")
        with pytest.raises(ValueError, match="provider id"):
            ProviderRegistry((d,))

    def test_empty_display_name_rejected(self) -> None:
        d = self._make_def(display_name="")
        with pytest.raises(ValueError, match="display_name"):
            ProviderRegistry((d,))

    def test_file_scheme_rejected(self) -> None:
        d = self._make_def(default_base_url="file:///etc/passwd")
        with pytest.raises(ValueError, match="HTTPS"):
            ProviderRegistry((d,))

    def test_unknown_provider_get_returns_none(self) -> None:
        reg = ProviderRegistry((self._make_def(),))
        assert reg.get("nonexistent") is None
        assert not reg.has("nonexistent")

    # ------------------------------------------------------------------
    # strategy ↔ endpoint 一致性（E1-3B1）
    # ------------------------------------------------------------------

    def test_unsupported_with_endpoint_rejected(self) -> None:
        """strategy='unsupported' 不允许同时给 endpoint."""
        d = self._make_def(
            credential_validation_strategy="unsupported",
            credential_validation_endpoint="https://example.com/v1/models",
        )
        with pytest.raises(ValueError, match="unsupported"):
            ProviderRegistry((d,))

    def test_anthropic_models_strategy_without_endpoint_rejected(self) -> None:
        """strategy='anthropic_models' 必须给 HTTPS endpoint."""
        d = self._make_def(
            credential_validation_strategy="anthropic_models",
            credential_validation_endpoint=None,
        )
        with pytest.raises(ValueError, match="HTTPS endpoint"):
            ProviderRegistry((d,))

    def test_unsupported_with_none_endpoint_accepted(self) -> None:
        d = self._make_def(
            credential_validation_strategy="unsupported",
            credential_validation_endpoint=None,
        )
        reg = ProviderRegistry((d,))
        assert reg.has("test-provider")


# ============================================================================
# Network safety——registry 不创建连接
# ============================================================================


class TestNetworkSafety:
    def test_no_network_module_imported_at_module_level(self) -> None:
        """registry.py 顶层不应 import httpx / requests / socket / urllib.request 等."""
        import pi_agent_core_py.providers.registry as mod

        forbidden_prefixes = ("httpx", "requests", "aiohttp", "urllib.request", "socket")
        # 检查模块的全局符号
        for name in vars(mod):
            if any(name.startswith(p) or name == p.split(".")[-1] for p in forbidden_prefixes):
                # 允许局部 import 在函数内（不出现在 vars）
                # 如果出现在 vars() 说明是顶层 import——失败
                pytest.fail(f"registry module exposes network symbol at top level: {name}")

    def test_monkeypatched_network_does_not_break_registry_construction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """即使禁用 socket / httpx，registry 仍然能构造（无副作用）."""
        # 把 socket 模块的关键函数替换为抛异常
        import socket

        def _raise(*a: object, **k: object) -> None:
            raise RuntimeError("network call blocked by test")

        monkeypatch.setattr(socket, "socket", _raise)
        monkeypatch.setattr(socket, "create_connection", _raise)
        monkeypatch.setattr(socket, "getaddrinfo", _raise)

        # 应当全部不调用网络——所以不抛
        d = ProviderDefinition(
            id="net-safe-test",
            display_name="Net Safe Test",
            api_style="anthropic_compatible",
            default_base_url="https://example.com",
            credential_validation_strategy="unsupported",
            credential_validation_endpoint=None,
            supports_model_listing=False,
        )
        reg = ProviderRegistry((d,))
        assert reg.has("net-safe-test")

    def test_api_style_literal_has_expected_values(self) -> None:
        # ProviderAPIStyle 是 Literal——不能直接遍历，但应该接受两个值
        def _accept(_s: ProviderAPIStyle) -> None:
            pass

        _accept("anthropic_compatible")
        _accept("openai_compatible")

    def test_strategy_id_literal_has_expected_values(self) -> None:
        def _accept(_s: CredentialValidationStrategyId) -> None:
            pass

        _accept("anthropic_models")
        _accept("unsupported")

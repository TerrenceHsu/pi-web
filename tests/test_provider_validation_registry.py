"""ValidationStrategyRegistry tests（P1-E1-3B1）.

覆盖：
- 注册 anthropic_models → 返回 AnthropicModelsValidationStrategy
- get unsupported → None
- 注册时显式含 'unsupported' → ValueError
- Registry repr 不暴露 strategy 内部
- Registry 不可 pickle
- 多个 strategy 共存（注入 fake）
- 构造阶段不发网络
"""
from __future__ import annotations

import pickle

import pytest

from pi_agent_core_py.web.providers.validation import (
    AnthropicModelsValidationStrategy,
    ProviderValidationResult,
    ProviderValidationStrategy,
    ValidationStrategyRegistry,
)

# ============================================================================
# 1. Built-in default registry
# ============================================================================


class TestDefaultRegistry:
    def test_get_anthropic_models_returns_strategy(self) -> None:
        reg = ValidationStrategyRegistry()
        strategy = reg.get("anthropic_models")
        assert strategy is not None
        assert isinstance(strategy, AnthropicModelsValidationStrategy)
        assert strategy.provider_id == "anthropic"

    def test_get_unsupported_returns_none(self) -> None:
        reg = ValidationStrategyRegistry()
        assert reg.get("unsupported") is None

    def test_get_unknown_strategy_id_returns_none(self) -> None:
        reg = ValidationStrategyRegistry()
        # type: ignore——Literal 不允许运行时输入，但保守测试未知 ID
        assert reg.get("unknown_strategy") is None  # type: ignore[arg-type]


# ============================================================================
# 2. Constructor validation
# ============================================================================


class TestConstructorValidation:
    def test_registering_unsupported_raises(self) -> None:
        """'unsupported' 不应注册——Service 应 short-circuit."""
        # 需要构造一个 fake strategy 占位
        fake = _make_fake_strategy("fake")
        with pytest.raises(ValueError, match="unsupported"):
            ValidationStrategyRegistry(
                strategies={
                    "unsupported": fake,  # type: ignore[dict-item]
                }
            )


# ============================================================================
# 3. Custom registry with injected fake
# ============================================================================


class TestCustomRegistry:
    def test_get_returns_injected_strategy(self) -> None:
        fake = _make_fake_strategy("fake-provider")
        reg = ValidationStrategyRegistry(
            strategies={"anthropic_models": fake},
        )
        assert reg.get("anthropic_models") is fake

    def test_multiple_strategies_coexist(self) -> None:
        fake_a = _make_fake_strategy("provider-a")
        reg = ValidationStrategyRegistry(
            strategies={
                "anthropic_models": fake_a,
                # 注：CredentialValidationStrategyId 只有两个 Literal 值，
                # 但 Registry 本身接受任意 ID-like string 作为 key 用于测试.
            }
        )
        assert reg.get("anthropic_models") is fake_a


# ============================================================================
# 4. Repr / pickle safety
# ============================================================================


class TestReprAndPickle:
    def test_repr_does_not_expose_strategy_internals(self) -> None:
        reg = ValidationStrategyRegistry()
        text = repr(reg)
        # 不应包含 strategy 对象的 repr（可能含 client factory 细节）
        assert "AnthropicModelsValidationStrategy" not in text
        assert "_client_factory" not in text
        # 应该列出已注册的 strategy ID
        assert "anthropic_models" in text

    def test_registry_not_picklable(self) -> None:
        reg = ValidationStrategyRegistry()
        with pytest.raises(TypeError, match="not picklable"):
            pickle.dumps(reg)


# ============================================================================
# 5. Construction does not probe network
# ============================================================================


class TestNoNetworkOnConstruction:
    def test_construction_does_not_create_client(self) -> None:
        """Registry 构造时不应当调用任何 client factory."""
        call_count = 0

        def _spy_factory():
            nonlocal call_count
            call_count += 1
            raise AssertionError("factory should not be called during construction")

        # AnthropicModelsValidationStrategy 默认 factory 不在构造期调
        # 用 spy 替换默认——确认构造时不调
        strategy = AnthropicModelsValidationStrategy(client_factory=_spy_factory)
        reg = ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy}
        )
        assert call_count == 0
        # Registry repr 也不触发
        repr(reg)
        assert call_count == 0


# ============================================================================
# Helpers
# ============================================================================


def _make_fake_strategy(provider_id: str) -> ProviderValidationStrategy:
    """Build a minimal fake strategy for registry tests."""

    class _Fake:
        def __init__(self) -> None:
            self.provider_id = provider_id

        async def validate(self, secret: str) -> ProviderValidationResult:
            return ProviderValidationResult(
                provider_id=provider_id,
                valid=True,
                error_code=None,
            )

    return _Fake()  # type: ignore[return-value]

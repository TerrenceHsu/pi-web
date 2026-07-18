"""detect_provider_hint 单元测试（P1-E1-1）.

覆盖：
- sk-ant- 前缀 → high confidence + candidates=('anthropic',)
- 通用 sk- 前缀 → unknown confidence + 空 candidates
- 无 sk- 前缀 → unknown
- 空字符串 / 非字符串 → unknown
- 前端文案约束："可能属于" 而非 "已识别"
- 纯本地——monkeypatch socket / urllib / httpx 证明无网络调用
- ProviderHintResult 是 frozen dataclass
"""
from __future__ import annotations

import socket
import sys

import pytest

from pi_agent_core_py.providers.registry import (
    ProviderHintResult,
    detect_provider_hint,
)

# ============================================================================
# Hint behavior
# ============================================================================


class TestHighConfidenceMatch:
    def test_anthropic_prefix_high_confidence(self) -> None:
        result = detect_provider_hint("sk-ant-abc1234567890")
        assert result.candidates == ("anthropic",)
        assert result.confidence == "high"
        assert "sk_ant" in result.reason_code

    def test_anthropic_prefix_case_sensitive(self) -> None:
        # sk-ANT- 不应被识别（不区分大小写会带来歧义）
        result = detect_provider_hint("sk-ANT-abc")
        # 不是 high confidence——会 fall through 到 sk- 前缀
        assert result.confidence != "high" or "anthropic" not in result.candidates


class TestAmbiguousPrefix:
    def test_generic_sk_prefix_is_unknown(self) -> None:
        """通用 sk- 前缀但无 provider-specific——返回 unknown + 空 candidates.

        不把 OpenAI-compatible 当成已确认 provider 身份。
        """
        result = detect_provider_hint("sk-abc1234567890")
        assert result.candidates == ()
        assert result.confidence == "unknown"
        assert result.reason_code == "ambiguous_sk_prefix"

    def test_glm_typical_key_is_unknown(self) -> None:
        """GLM keys 不带确定性前缀——未知."""
        # GLM key 实际格式可能多种；我们保守返回 unknown
        result = detect_provider_hint("abcdef1234567890abcdef1234567890")
        assert result.confidence == "unknown"


class TestUnknownInput:
    def test_no_hint_match(self) -> None:
        result = detect_provider_hint("just-some-random-string-no-sk-prefix")
        assert result.candidates == ()
        assert result.confidence == "unknown"
        assert result.reason_code == "no_hint_match"

    def test_empty_string(self) -> None:
        result = detect_provider_hint("")
        assert result.candidates == ()
        assert result.confidence == "unknown"
        assert result.reason_code == "empty_or_invalid_input"

    def test_non_string_input(self) -> None:
        result = detect_provider_hint(None)  # type: ignore[arg-type]
        assert result.candidates == ()
        assert result.confidence == "unknown"

    def test_whitespace_only(self) -> None:
        # 纯空格不是有效 Key——unknown
        result = detect_provider_hint("   ")
        # 不以 sk- 开头——fall to no_hint_match
        assert result.confidence == "unknown"


# ============================================================================
# Result dataclass
# ============================================================================


class TestProviderHintResult:
    def test_result_is_frozen(self) -> None:
        r = ProviderHintResult(
            candidates=("anthropic",),
            confidence="high",
            reason_code="test",
        )
        # frozen dataclass 抛 FrozenInstanceError（AttributeError 子类）
        with pytest.raises(AttributeError):
            r.confidence = "tampered"  # type: ignore[misc]

    def test_candidates_is_tuple(self) -> None:
        r = detect_provider_hint("sk-ant-test")
        assert isinstance(r.candidates, tuple)


# ============================================================================
# Network safety——必须纯本地
# ============================================================================


class TestNoNetworkCalls:
    """monkeypatch 所有网络入口——detect_provider_hint 仍能正常工作."""

    def test_socket_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*a: object, **k: object) -> None:
            raise RuntimeError("socket call blocked")

        monkeypatch.setattr(socket, "socket", _raise)
        monkeypatch.setattr(socket, "create_connection", _raise)
        monkeypatch.setattr(socket, "getaddrinfo", _raise)

        # 应当不抛——detect_provider_hint 不调用 socket
        r = detect_provider_hint("sk-ant-abc123")
        assert r.candidates == ("anthropic",)

    def test_urllib_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """禁用 urllib.request——detect_provider_hint 应当不依赖."""
        import urllib.request

        def _raise(*a: object, **k: object) -> None:
            raise RuntimeError("urllib call blocked")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        monkeypatch.setattr(urllib.request, "Request", _raise)

        r = detect_provider_hint("sk-ant-abc123")
        assert r.candidates == ("anthropic",)

    def test_httpx_not_imported_at_module_level(self) -> None:
        """registry 模块顶层不应 import httpx."""
        # 检查 detect_provider_hint 函数的 globals 里没有 httpx
        import pi_agent_core_py.providers.registry as mod

        assert not hasattr(mod, "httpx")
        assert not hasattr(mod, "requests")
        assert not hasattr(mod, "aiohttp")

    def test_socket_not_imported_at_module_level(self) -> None:
        """registry 模块顶层不应 import socket."""
        import pi_agent_core_py.providers.registry as mod

        assert not hasattr(mod, "socket")

    def test_sys_modules_spy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """通过 sys.modules 拦截 httpx / requests / aiohttp 的 import.

        任何对它们的 import 都会失败——detect_provider_hint 应当不受影响.
        """
        blocked = ("httpx", "requests", "aiohttp")

        builtins_mod = __builtins__
        real_import = (
            builtins_mod["__import__"]
            if isinstance(builtins_mod, dict)
            else builtins_mod.__import__
        )

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name in blocked or any(name.startswith(b + ".") for b in blocked):
                raise ImportError(f"blocked by test: {name}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("builtins.__import__", fake_import)

        # 重新 import registry——如果它顶层 import 了 httpx 会失败
        # 但顶层不应 import，所以 OK
        if "pi_agent_core_py.providers.registry" in sys.modules:
            del sys.modules["pi_agent_core_py.providers.registry"]
        try:
            import pi_agent_core_py.providers.registry  # noqa: F401

            # 再次调用——不应触发 import
            r = detect_provider_hint("sk-ant-abc")
            assert r.candidates == ("anthropic",)
        finally:
            # 确保后续测试拿回模块
            import importlib

            importlib.reload(sys.modules["pi_agent_core_py.providers.registry"])


# ============================================================================
# Front-end messaging constraint——"可能属于" 而非 "已识别"
# ============================================================================


class TestFrontendWordingHint:
    """detect_provider_hint 只提供 hint——不提供确认.

    前端（P1-E4）必须用 "可能属于 X" 措辞，**绝不**写 "已识别为 X".
    本测试仅校验 reason_code 与 confidence 字段语义——不直接测文案.
    """

    def test_high_confidence_still_returns_candidates_not_assertion(self) -> None:
        """即使 high confidence，candidates 仍是 tuple——不是单 string."""
        r = detect_provider_hint("sk-ant-abc")
        # high confidence 但 candidates 仍是 tuple——前端选择是否使用
        assert r.confidence == "high"
        assert isinstance(r.candidates, tuple)

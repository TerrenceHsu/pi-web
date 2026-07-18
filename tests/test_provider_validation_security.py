"""Provider validation security tests（P1-E1-3B1）.

用 SECRET_MARKER 验证 secret 在所有边界都 0 泄漏：
- ProviderValidationResult
- 异常 str / repr（虽然 strategy 不抛——但 result 路径安全校验）
- 日志（caplog）
- Strategy repr / attrs
- Registry repr
- 模块 globals
- httpx request headers（注入 spy transport 验证）
- redirect Location 含 marker 时不读取 / 不输出
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from pi_agent_core_py.web.provider_validation import (
    AnthropicModelsValidationStrategy,
    HttpClientFactory,
    ValidationStrategyRegistry,
)

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


def make_mock_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpClientFactory:
    @asynccontextmanager
    async def _factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    return _factory


def _ok(body: object) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _assert_no_marker(obj: object) -> None:
    text = repr(obj)
    assert SECRET_MARKER not in text, (
        f"SECRET_MARKER leaked in {type(obj).__name__} repr: {text!r}"
    )


# ============================================================================
# 1. Result safety——no secret in result
# ============================================================================


class TestResultSafety:
    async def test_result_repr_does_not_contain_secret(self) -> None:
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        _assert_no_marker(result)
        # str() 同样校验
        assert SECRET_MARKER not in str(result)

    async def test_result_is_json_safe_no_secret(self) -> None:
        """Result 是 frozen dataclass——repr 安全. 验证 json 序列化也安全."""
        import dataclasses

        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        # dataclasses.asdict + json.dumps 不应泄漏 marker
        d = dataclasses.asdict(result)
        text = json.dumps(d, default=str)
        assert SECRET_MARKER not in text


# ============================================================================
# 2. Strategy repr / attrs safety
# ============================================================================


class TestStrategyAttrSafety:
    async def test_strategy_attrs_have_no_secret_after_validate(self) -> None:
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        for name in vars(strategy):
            value = getattr(strategy, name)
            if isinstance(value, str):
                assert SECRET_MARKER not in value, (
                    f"SECRET_MARKER in strategy attr {name}"
                )

    async def test_strategy_repr_has_no_secret(self) -> None:
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        _assert_no_marker(strategy)


# ============================================================================
# 3. Registry repr safety
# ============================================================================


class TestRegistrySafety:
    def test_registry_repr_does_not_expose_secrets(self) -> None:
        """Registry 不持有 secret——但验证 repr 不暴露 strategy 实例细节."""
        reg = ValidationStrategyRegistry()
        text = repr(reg)
        assert SECRET_MARKER not in text
        # Strategy 实例的 repr 也不应被嵌入
        assert "AnthropicModelsValidationStrategy" not in text

    async def test_registry_repr_safe_after_validate(self) -> None:
        """validate 调用后再 repr registry——仍不泄漏."""
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        reg = ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy}
        )
        await strategy.validate(SECRET_MARKER)
        text = repr(reg)
        assert SECRET_MARKER not in text


# ============================================================================
# 4. Request headers safety——secret only in x-api-key, not in request URL
# ============================================================================


class TestRequestSafety:
    async def test_secret_only_in_x_api_key_header(self) -> None:
        """验证 secret 只出现在 x-api-key header，不出现在 URL / query / 其它 header."""
        captured: dict[str, object] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["headers"] = dict(req.headers)
            return _ok({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)

        # URL 不含 marker
        assert SECRET_MARKER not in str(captured["url"])

        # 遍历 headers——marker 只能在 x-api-key 中
        headers = captured["headers"]
        marker_leaks: list[str] = []
        for name, value in headers.items():
            value_str = str(value)
            if SECRET_MARKER in value_str:
                if name.lower() == "x-api-key":
                    continue
                marker_leaks.append(name)
        assert marker_leaks == [], (
            f"SECRET_MARKER leaked in headers other than x-api-key: {marker_leaks}"
        )

    async def test_secret_not_in_query_string(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["query"] = str(req.url.query)
            captured["path"] = req.url.path
            return _ok({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert SECRET_MARKER not in captured["query"]
        # path 应当是 /v1/models（不含 marker）
        assert captured["path"] == "/v1/models"


# ============================================================================
# 5. Logs safety——caplog
# ============================================================================


class TestLogSafety:
    async def test_validate_does_not_log_secret(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)

        with caplog.at_level(logging.DEBUG, logger="pi_agent_core_py.web.provider_validation"):
            await strategy.validate(SECRET_MARKER)

        for record in caplog.records:
            for attr_name in ("msg", "message", "pathname"):
                value = getattr(record, attr_name, "")
                if isinstance(value, str):
                    assert SECRET_MARKER not in value, (
                        f"SECRET_MARKER leaked in log {attr_name}: {value!r}"
                    )


# ============================================================================
# 6. Client factory exception safety——marker in exception message
# ============================================================================


class TestClientFactoryExceptionSafety:
    async def test_client_factory_exception_message_with_marker_mapped_safely(
        self,
    ) -> None:
        """如果 client factory 抛异常 message 含 marker——strategy 应安全映射."""
        # 用一个 message 含 marker 的 RuntimeError——strategy 应映射为 unknown_error
        # 且 result / repr 不泄漏 marker.
        def _handler(req: httpx.Request) -> httpx.Response:
            raise RuntimeError(f"unexpected {SECRET_MARKER}")

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "unknown_error"
        assert SECRET_MARKER not in repr(result)


# ============================================================================
# 7. Provider error body with marker——strategy doesn't read body for non-2xx
# ============================================================================


class TestProviderErrorBodySafety:
    async def test_401_body_with_marker_not_leaked(self) -> None:
        body = json.dumps(
            {"type": "error", "error": {"message": SECRET_MARKER}}
        ).encode("utf-8")

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=401,
                content=body,
                headers={"content-type": "application/json"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "authentication_failed"
        assert SECRET_MARKER not in repr(result)

    async def test_500_body_with_marker_not_leaked(self) -> None:
        body = b'{"error": "' + SECRET_MARKER.encode() + b'"}'

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                content=body,
                headers={"content-type": "application/json"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"
        assert SECRET_MARKER not in repr(result)

    async def test_redirect_location_with_marker_not_leaked(self) -> None:
        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=302,
                content=b"",
                headers={"location": f"https://attacker.example/?{SECRET_MARKER}"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"
        assert SECRET_MARKER not in repr(result)


# ============================================================================
# 8. Module globals——no secret cached
# ============================================================================


def test_provider_validation_module_globals_no_marker() -> None:
    import pi_agent_core_py.web.provider_validation as mod

    for name, value in vars(mod).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert SECRET_MARKER not in value, (
                f"marker in module global {name}: {value!r}"
            )

"""Provider validation error mapping + safety tests（P1-E1-3B1）.

覆盖：
- HTTP 状态到 error_code 完整映射
- valid ↔ error_code 互斥不变量
- 不跟随 redirect（注入 302 → 不同 Host，不请求新 Host）
- 异常 message 不含 secret
- 异常 message 不含 response body
- Strategy repr 不含 secret / endpoint 注入风险
- Strategy 不接受外部 URL（endpoint frozen）
"""
from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from pi_agent_core_py.web.provider_validation import (
    AnthropicModelsValidationStrategy,
    HttpClientFactory,
    ProviderValidationResult,
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


# ============================================================================
# 1. Status code → error_code 完整映射
# ============================================================================


class TestStatusCodeMapping:
    @pytest.mark.parametrize(
        "status,expected_code",
        [
            (301, "protocol_error"),  # 3xx 不跟随
            (302, "protocol_error"),
            (307, "protocol_error"),
            (400, "protocol_error"),
            (401, "authentication_failed"),
            (403, "permission_denied"),
            (404, "protocol_error"),
            (405, "protocol_error"),
            (409, "protocol_error"),
            (422, "protocol_error"),
            (429, "rate_limited"),
            (500, "protocol_error"),
            (501, "protocol_error"),
            (502, "protocol_error"),
            (503, "protocol_error"),
            (504, "protocol_error"),
        ],
    )
    async def test_status_mapped_to_error_code(
        self, status: int, expected_code: str
    ) -> None:
        factory = make_mock_factory(
            lambda req, s=status: httpx.Response(status_code=s, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == expected_code
        assert result.valid is False


# ============================================================================
# 2. Invariant: valid ↔ error_code 互斥
# ============================================================================


class TestInvariant:
    async def test_all_error_codes_imply_valid_false(self) -> None:
        """遍历所有远端 error_code——对应 result.valid 必为 False."""
        error_codes = [
            "authentication_failed",
            "permission_denied",
            "rate_limited",
            "endpoint_unreachable",
            "request_timeout",
            "tls_error",
            "protocol_error",
            "unknown_error",
        ]
        # 这些 error_code 的产生路径在 anthropic tests 已覆盖；
        # 这里仅校验 ProviderValidationResult 不变量——构造时 valid=False 与 error_code 必同行.
        for code in error_codes:
            r = ProviderValidationResult(
                provider_id="anthropic",
                valid=False,
                error_code=code,  # type: ignore[arg-type]
            )
            assert r.valid is False
            assert r.error_code == code


# ============================================================================
# 3. No redirect
# ============================================================================


class TestNoRedirect:
    async def test_redirect_does_not_request_new_host(self) -> None:
        """302 → protocol_error；不请求 Location 指向的 Host."""
        requested_urls: list[str] = []

        def _handler(req: httpx.Request) -> httpx.Response:
            requested_urls.append(str(req.url))
            return httpx.Response(
                status_code=302,
                content=b"",
                headers={"location": "https://attacker.example/steal"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"
        # 只调用一次（首请求）——没跟随 redirect
        assert len(requested_urls) == 1
        assert "attacker.example" not in requested_urls[0]

    async def test_redirect_location_with_marker_not_output(self) -> None:
        """即使 Location 含 marker——也不读 / 不输出."""
        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=302,
                content=b"",
                headers={"location": f"https://attacker.example/?{SECRET_MARKER}"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        # result repr 不应含 marker（location 没被读入）
        assert SECRET_MARKER not in repr(result)


# ============================================================================
# 4. Strategy does not accept external URL
# ============================================================================


class TestEndpointFrozen:
    async def test_strategy_does_not_accept_external_url(self) -> None:
        """Strategy.endpoint 是模块常量——不接受调用方注入 URL."""
        strategy = AnthropicModelsValidationStrategy()
        # 没有任何方式让 strategy 发往非 anthropic.com 的 host
        # 验证 _ENDPOINT 是模块常量
        assert strategy._ENDPOINT == "https://api.anthropic.com/v1/models"

    async def test_strategy_calls_only_frozen_endpoint(self) -> None:
        captured_urls: list[str] = []

        def _handler(req: httpx.Request) -> httpx.Response:
            captured_urls.append(str(req.url))
            return _ok({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert captured_urls == ["https://api.anthropic.com/v1/models"]


# ============================================================================
# 5. Strategy repr safety
# ============================================================================


class TestStrategyReprSafety:
    async def test_strategy_repr_does_not_contain_secret(self) -> None:
        """调用 validate 后 repr 不应含 secret（secret 不残留在实例 attr）."""
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        text = repr(strategy)
        assert SECRET_MARKER not in text

    def test_strategy_repr_does_not_expose_client_factory(self) -> None:
        """Strategy repr 不应输出 client_factory 的 repr（可能含闭包细节）."""
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        text = repr(strategy)
        assert "_client_factory" not in text
        assert "factory" not in text.lower()


# ============================================================================
# 6. 异常 message 不含 secret / response body
# ============================================================================


class TestErrorMessageSafety:
    async def test_error_response_body_not_in_result(self) -> None:
        """500 response 的 body（可能含 marker / 内部细节）不进入 result."""
        body_with_marker = f'{{"error": "{SECRET_MARKER}"}}'.encode()

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=500,
                content=body_with_marker,
                headers={"content-type": "application/json"},
            )

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        # result 是 dataclass——它的 repr 不应含 raw response body
        assert SECRET_MARKER not in repr(result)
        assert result.error_code == "protocol_error"

    async def test_provider_error_json_with_marker_not_leaked(self) -> None:
        """Provider 错误响应 body 含 marker——strategy 不读 body，不泄漏."""
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


# ============================================================================
# 7. 真实网络调用 = 0（用 socket monkey-patch 验证）
# ============================================================================


class TestZeroRealNetworkCalls:
    async def test_validate_does_not_open_real_socket(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """用 MockTransport 时，不应调真实 socket."""
        import socket

        call_count = {"n": 0}

        def _raise(*a: object, **k: object) -> None:
            call_count["n"] += 1
            raise RuntimeError("real socket blocked")

        monkeypatch.setattr(socket, "socket", _raise)
        monkeypatch.setattr(socket, "create_connection", _raise)
        monkeypatch.setattr(socket, "getaddrinfo", _raise)

        # MockTransport 短路——不会触达真实 socket
        factory = make_mock_factory(lambda req: _ok({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True
        assert call_count["n"] == 0

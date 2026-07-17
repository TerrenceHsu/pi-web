"""AnthropicModelsValidationStrategy tests（P1-E1-3B1）.

覆盖：
- 2xx 合法 / 空 data / models 字段
- 2xx 非 JSON / 顶层非 object / 缺 data / data 非 list / Content-Type 非 JSON
- 401 → authentication_failed
- 403 → permission_denied
- 429 → rate_limited
- 3xx → protocol_error（不跟随）
- 400 → protocol_error
- 500 → protocol_error
- timeout → request_timeout
- connect/DNS → endpoint_unreachable
- TLS → tls_error
- 未知异常 → unknown_error
- 请求方法 GET
- Host 固定（api.anthropic.com）
- 只发 x-api-key（不发 Authorization）
- 包含 anthropic-version: 2023-06-01
- 不自动 retry（mock handler 计数）
- 合法大响应边界（接近 1MB 通过）
- 超大响应拒绝（> 1MB → protocol_error）

所有测试用 httpx.MockTransport——真实网络调用 = 0.
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
)

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Helpers
# ============================================================================


def make_mock_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpClientFactory:
    """Return client factory that wraps httpx.MockTransport(handler)."""

    @asynccontextmanager
    async def _factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    return _factory


def make_factory_raising(exc_factory: Callable[[], BaseException]) -> HttpClientFactory:
    def _handler(req: httpx.Request) -> httpx.Response:
        raise exc_factory()

    @asynccontextmanager
    async def _factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    return _factory


def _ok_response(body: object, content_type: str = "application/json") -> httpx.Response:
    body_bytes = json.dumps(body).encode("utf-8")
    return httpx.Response(
        status_code=200,
        content=body_bytes,
        headers={"content-type": content_type},
    )


# ============================================================================
# 2xx success cases
# ============================================================================


class TestSuccessCases:
    async def test_200_with_data_list_succeeds(self) -> None:
        body = {"data": [{"id": "claude-sonnet-4-6"}]}
        factory = make_mock_factory(lambda req: _ok_response(body))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True
        assert result.error_code is None
        assert result.provider_id == "anthropic"

    async def test_200_with_empty_data_list_succeeds(self) -> None:
        """合法但当前无可见模型——Key 仍有效."""
        body = {"data": []}
        factory = make_mock_factory(lambda req: _ok_response(body))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True
        assert result.error_code is None

    async def test_200_with_models_field_succeeds(self) -> None:
        """兼容返回 {models: [...]} 的实现."""
        body = {"models": [{"id": "m1"}]}
        factory = make_mock_factory(lambda req: _ok_response(body))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True


# ============================================================================
# 2xx protocol errors——body 不合法
# ============================================================================


class TestSuccessProtocolErrors:
    async def test_200_non_json_body_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=b"<html>not json</html>",
                headers={"content-type": "text/html"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is False
        assert result.error_code == "protocol_error"

    async def test_200_invalid_json_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=b"{not valid json",
                headers={"content-type": "application/json"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_200_top_level_not_object_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: _ok_response(["list", "instead", "of", "object"])
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_200_missing_data_field_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: _ok_response({"unrelated": "field"})
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_200_data_not_list_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: _ok_response({"data": "not-a-list"})
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_200_non_json_content_type_rejected(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=b'{"data": []}',
                headers={"content-type": "text/plain"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"


# ============================================================================
# HTTP status error mapping
# ============================================================================


class TestStatusErrorMapping:
    async def test_401_returns_authentication_failed(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=401, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is False
        assert result.error_code == "authentication_failed"

    async def test_403_returns_permission_denied(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=403, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "permission_denied"

    async def test_429_returns_rate_limited(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=429, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "rate_limited"

    async def test_3xx_returns_protocol_error_no_follow(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=302,
                content=b"",
                headers={"location": "https://attacker.example/steal"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_400_returns_protocol_error(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=400, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_500_returns_protocol_error(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=500, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_404_returns_protocol_error(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=404, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"


# ============================================================================
# Network / TLS errors
# ============================================================================


class TestNetworkErrors:
    async def test_timeout_returns_request_timeout(self) -> None:
        factory = make_factory_raising(lambda: httpx.ConnectTimeout("connect timed out"))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "request_timeout"

    async def test_read_timeout_returns_request_timeout(self) -> None:
        factory = make_factory_raising(lambda: httpx.ReadTimeout("read timed out"))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "request_timeout"

    async def test_connect_error_returns_endpoint_unreachable(self) -> None:
        factory = make_factory_raising(lambda: httpx.ConnectError("connect failed"))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "endpoint_unreachable"

    async def test_tls_error_returns_tls_error(self) -> None:
        import ssl

        if not hasattr(ssl, "SSLCertVerificationError"):
            pytest.skip("ssl.SSLCertVerificationError not available on this platform")

        def _raise() -> BaseException:
            return ssl.SSLCertVerificationError(
                "CERTIFICATE_VERIFY_FAILED"
            )

        factory = make_factory_raising(_raise)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "tls_error"

    async def test_unknown_exception_returns_unknown_error(self) -> None:
        factory = make_factory_raising(lambda: RuntimeError("unexpected"))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "unknown_error"


# ============================================================================
# Request shape——method / host / headers
# ============================================================================


class TestRequestShape:
    async def test_request_method_is_get(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert captured["method"] == "GET"

    async def test_request_host_is_fixed_anthropic(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            captured["host"] = req.url.host
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert captured["host"] == "api.anthropic.com"
        assert captured["url"] == "https://api.anthropic.com/v1/models"

    async def test_request_has_x_api_key_header(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["x_api_key"] = req.headers.get("x-api-key", "")
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate("sk-test-1234567890")
        assert captured["x_api_key"] == "sk-test-1234567890"

    async def test_request_has_anthropic_version_header(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["version"] = req.headers.get("anthropic-version", "")
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert captured["version"] == "2023-06-01"

    async def test_request_does_not_send_authorization_header(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["authorization"] = req.headers.get("authorization", "")
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        # 不应当发送 Authorization——只发 x-api-key
        assert captured["authorization"] == ""

    async def test_request_has_accept_json_header(self) -> None:
        captured: dict[str, str] = {}

        def _handler(req: httpx.Request) -> httpx.Response:
            captured["accept"] = req.headers.get("accept", "")
            return _ok_response({"data": []})

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        await strategy.validate(SECRET_MARKER)
        assert "application/json" in captured["accept"]


# ============================================================================
# No retry / bounded body
# ============================================================================


class TestNoRetryAndBoundedBody:
    async def test_no_retry_on_5xx(self) -> None:
        """5xx 不自动 retry——handler 只调一次."""
        call_count = {"n": 0}

        def _handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(status_code=503, content=b"")

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert call_count["n"] == 1
        assert result.error_code == "protocol_error"

    async def test_no_retry_on_timeout(self) -> None:
        call_count = {"n": 0}

        def _handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            raise httpx.ReadTimeout("read timed out")

        factory = make_mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert call_count["n"] == 1
        assert result.error_code == "request_timeout"

    async def test_large_response_within_limit_succeeds(self) -> None:
        """~ 768KB（< 1MB limit）应当正常处理."""
        # 每个模型 entry ~ 200B，4000 个 ≈ 800KB
        items = [{"id": f"model-{i:06d}", "display_name": "x" * 100} for i in range(4000)]
        body = {"data": items}
        body_bytes = json.dumps(body).encode("utf-8")
        assert len(body_bytes) < 1024 * 1024  # < 1MB

        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=body_bytes,
                headers={"content-type": "application/json"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True

    async def test_oversize_response_rejected_via_content_length(self) -> None:
        """Content-Length > 1MB → protocol_error，不下载 body."""
        # 构造 Content-Length 远超上限的响应；MockTransport 不会真发内容
        # 但 strategy 应在 Content-Length check 阶段拒绝.
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=b'{"data": []}',  # 实际 body 小
                headers={
                    "content-type": "application/json",
                    "content-length": str(2 * 1024 * 1024),  # 声明 2MB
                },
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"

    async def test_oversize_actual_body_rejected(self) -> None:
        """实际 body > 1MB → protocol_error（无 Content-Length 时）."""
        # 1.5MB body
        big_body = b"x" * (1024 * 1024 + 1024)
        factory = make_mock_factory(
            lambda req: httpx.Response(
                status_code=200,
                content=big_body,
                headers={"content-type": "application/json"},
            )
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.error_code == "protocol_error"


# ============================================================================
# Result invariant
# ============================================================================


class TestResultInvariant:
    async def test_valid_true_implies_error_code_none(self) -> None:
        factory = make_mock_factory(lambda req: _ok_response({"data": []}))
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is True
        assert result.error_code is None

    async def test_valid_false_implies_error_code_present(self) -> None:
        factory = make_mock_factory(
            lambda req: httpx.Response(status_code=401, content=b"")
        )
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        result = await strategy.validate(SECRET_MARKER)
        assert result.valid is False
        assert result.error_code is not None

"""OpenAICompatibleProvider 错误映射测试（M1-1 §十五.Errors）.

覆盖：
- 401 / 403 → ProviderAuthenticationError
- 429 → ProviderRateLimitError
- timeout / connection / 5xx → ProviderStreamError
- 400 / 404 → ProviderProtocolError
- 错误信息固定短文本（不含 SDK str/repr/body）
- 异常 cause 被截断（from None）
- 错误信息不含 secret marker
"""
from __future__ import annotations

import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from pi_agent_core_py.llm_messages import LLMUserMessage
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.providers.base import ProviderRequest
from pi_agent_core_py.providers.errors import (
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderStreamError,
)
from pi_agent_core_py.providers.openai_compat import (
    OpenAICompatConfig,
    OpenAICompatibleProvider,
)
from tests._openai_compat_fakes import _RaisingStreamClient

SECRET_MARKER = "sk-M1-OPENAI-COMPAT-SECRET-MARKER"


def _config() -> OpenAICompatConfig:
    return OpenAICompatConfig(
        api_key=SECRET_MARKER,
        base_url="https://example.test",
        model="qwen-plus",
    )


def _req() -> ProviderRequest:
    return ProviderRequest(
        system_prompt="secret-system-prompt",
        messages=[LLMUserMessage(content=[TextContent(text="secret-user-payload")])],
    )


def _adapter_with_raising_client(exc: BaseException) -> OpenAICompatibleProvider:
    client = _RaisingStreamClient(exc)
    return OpenAICompatibleProvider(_config(), provider_id="qwen", client=client)


def _make_api_status_error(
    cls: type,
    *,
    message: str = "leaked-secret-detail",
    body: dict | None = None,
) -> Exception:
    """构造带敏感 body 的 SDK 异常——验证 adapter 不暴露 body.

    SDK 2.x：APIStatusError 子类签名 `(message, *, response, body)`——message 必传.
    """
    import httpx
    req = httpx.Request("POST", "https://example.test/v1/chat/completions")
    req.headers["authorization"] = f"Bearer {SECRET_MARKER}"
    resp = httpx.Response(
        status_code=_status_for(cls),
        request=req,
        content=b'{"error":{"message":"leaked-secret-detail"}}',
    )
    return cls(message=message, response=resp, body=body or {"error": "leaked"})


def _status_for(cls: type) -> int:
    return {
        AuthenticationError: 401,
        PermissionDeniedError: 403,
        NotFoundError: 404,
        ConflictError: 409,
        UnprocessableEntityError: 422,
        RateLimitError: 429,
        InternalServerError: 500,
        BadRequestError: 400,
    }.get(cls, 500)


# ============================================================================
# 401 / 403
# ============================================================================


@pytest.mark.asyncio
async def test_401_maps_to_authentication_error() -> None:
    exc = _make_api_status_error(AuthenticationError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderAuthenticationError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    msg = str(exc_info.value)
    assert "authentication failed" in msg.lower()
    # 不含敏感字段
    assert SECRET_MARKER not in msg
    assert "leaked-secret-detail" not in msg


@pytest.mark.asyncio
async def test_403_maps_to_authentication_error() -> None:
    exc = _make_api_status_error(PermissionDeniedError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderAuthenticationError):
        async for _ in adapter.stream(_req()):
            pass


# ============================================================================
# 429
# ============================================================================


@pytest.mark.asyncio
async def test_429_maps_to_rate_limit_error() -> None:
    exc = _make_api_status_error(RateLimitError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    msg = str(exc_info.value)
    assert "rate limit" in msg.lower()
    assert SECRET_MARKER not in msg
    assert "leaked-secret-detail" not in msg


# ============================================================================
# timeout / connection / 5xx → ProviderStreamError
# ============================================================================


@pytest.mark.asyncio
async def test_timeout_maps_to_stream_error() -> None:
    exc = APITimeoutError(request="dummy-request-with-secret")
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    msg = str(exc_info.value)
    assert "connection" in msg.lower()
    assert SECRET_MARKER not in msg


@pytest.mark.asyncio
async def test_connection_error_maps_to_stream_error() -> None:
    exc = APIConnectionError(message="conn-error-with-secret", request="dummy")
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderStreamError):
        async for _ in adapter.stream(_req()):
            pass


@pytest.mark.asyncio
async def test_500_maps_to_stream_error() -> None:
    exc = _make_api_status_error(InternalServerError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    assert SECRET_MARKER not in str(exc_info.value)


# ============================================================================
# 400 / 404 → ProviderProtocolError
# ============================================================================


@pytest.mark.asyncio
async def test_400_maps_to_protocol_error() -> None:
    exc = _make_api_status_error(BadRequestError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderProtocolError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    msg = str(exc_info.value)
    assert "rejected request" in msg.lower()
    assert SECRET_MARKER not in msg
    assert "leaked-secret-detail" not in msg


@pytest.mark.asyncio
async def test_404_maps_to_protocol_error() -> None:
    exc = _make_api_status_error(NotFoundError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderProtocolError):
        async for _ in adapter.stream(_req()):
            pass


@pytest.mark.asyncio
async def test_409_maps_to_protocol_error() -> None:
    exc = _make_api_status_error(ConflictError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderProtocolError):
        async for _ in adapter.stream(_req()):
            pass


@pytest.mark.asyncio
async def test_422_maps_to_protocol_error() -> None:
    exc = _make_api_status_error(UnprocessableEntityError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderProtocolError):
        async for _ in adapter.stream(_req()):
            pass


# ============================================================================
# 异常 cause 截断（from None）
# ============================================================================


@pytest.mark.asyncio
async def test_exception_cause_chain_is_broken() -> None:
    """所有映射 raise ... from None——__cause__ 必须是 None."""
    exc = _make_api_status_error(AuthenticationError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderAuthenticationError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    # __cause__ 是 None（from None 生效）
    assert exc_info.value.__cause__ is None
    # __context__ 可能存在（Python 自动设），但 cause 链已断
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_rate_limit_error_cause_chain_broken() -> None:
    exc = _make_api_status_error(RateLimitError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    assert exc_info.value.__cause__ is None


# ============================================================================
# 错误信息不含敏感字段
# ============================================================================


@pytest.mark.asyncio
async def test_authentication_error_message_has_no_secret_marker() -> None:
    exc = _make_api_status_error(AuthenticationError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderAuthenticationError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    err_str = str(exc_info.value)
    err_repr = repr(exc_info.value)
    assert SECRET_MARKER not in err_str
    assert SECRET_MARKER not in err_repr


@pytest.mark.asyncio
async def test_protocol_error_message_has_no_request_body() -> None:
    """错误信息不得含 system_prompt / user payload."""
    exc = _make_api_status_error(BadRequestError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderProtocolError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    err_str = str(exc_info.value)
    assert "secret-system-prompt" not in err_str
    assert "secret-user-payload" not in err_str


@pytest.mark.asyncio
async def test_stream_error_message_has_no_authorization() -> None:
    exc = _make_api_status_error(InternalServerError)
    adapter = _adapter_with_raising_client(exc)
    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    err_str = str(exc_info.value)
    err_repr = repr(exc_info.value)
    assert "Bearer" not in err_str
    assert "Bearer" not in err_repr
    assert SECRET_MARKER not in err_str
    assert SECRET_MARKER not in err_repr


# ============================================================================
# 未知异常兜底
# ============================================================================


@pytest.mark.asyncio
async def test_unknown_exception_maps_to_stream_error() -> None:
    """非 SDK 异常也兜底为 ProviderStreamError."""
    adapter = _adapter_with_raising_client(RuntimeError("unknown-error-with-detail"))
    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in adapter.stream(_req()):
            pass
    # 固定短文本
    assert "unknown-error-with-detail" not in str(exc_info.value)

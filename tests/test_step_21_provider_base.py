"""Step 21 — ProviderAdapter base / ProviderRequest / ProviderError 层级。

覆盖：
- ProviderRequest 可构造
- ProviderAdapter 抽象不可直接实例化
- ProviderError 子类层级正确
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.llm_messages import LLMUserMessage
from pi_agent_core_py.messages import TextContent
from pi_agent_core_py.providers import (
    ProviderAdapter,
    ProviderAuthenticationError,
    ProviderConfigError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequest,
    ProviderStreamError,
)


def test_provider_request_default_tools_is_empty_list() -> None:
    """ProviderRequest.tools 缺省 = []（不是 None），便于 adapter 直接 if request.tools。"""
    req = ProviderRequest(
        system_prompt="hello",
        messages=[LLMUserMessage(content=[TextContent(text="hi")])],
    )
    assert req.tools == []
    assert req.signal is None
    assert req.metadata == {}


def test_provider_request_can_carry_metadata_and_signal() -> None:
    """metadata / signal 字段存在，便于后续扩展（trace context / cost tags）。"""
    sentinel = object()
    req = ProviderRequest(
        system_prompt="x",
        messages=[],
        tools=[],
        signal=sentinel,
        metadata={"request_id": "abc"},
    )
    assert req.signal is sentinel
    assert req.metadata["request_id"] == "abc"


def test_provider_adapter_is_abstract() -> None:
    """ProviderAdapter 是 ABC，不能直接实例化。"""
    with pytest.raises(TypeError):
        ProviderAdapter()  # type: ignore[abstract]


def test_provider_adapter_stream_is_abstract_method() -> None:
    """子类必须实现 stream()。"""
    assert "stream" in ProviderAdapter.__abstractmethods__

    class Incomplete(ProviderAdapter):  # 缺 stream 实现
        provider_id = "x"
        model = "y"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_provider_error_hierarchy() -> None:
    """ProviderError 子类都是 ProviderError 的子类。"""
    for cls in (
        ProviderConfigError,
        ProviderProtocolError,
        ProviderAuthenticationError,
        ProviderRateLimitError,
        ProviderStreamError,
    ):
        assert issubclass(cls, ProviderError)
        assert issubclass(cls, Exception)


def test_provider_error_can_be_raised_and_caught() -> None:
    """统一用 ProviderError 兜底捕获。"""
    with pytest.raises(ProviderError):
        raise ProviderProtocolError("bad json")
    with pytest.raises(ProviderError):
        raise ProviderConfigError("missing api_key")


def test_provider_adapter_class_attributes_default_empty() -> None:
    """基类默认 provider_id / model 都是空串，由子类覆盖。"""
    assert ProviderAdapter.provider_id == ""
    assert ProviderAdapter.model == ""

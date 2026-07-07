"""WebSearchTool 单元测试——参数校验 + 缺 key 抛错 + execute 成功路径（mock httpx）。

不依赖真实 Tavily API；用 fake httpx.AsyncClient 注入。
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.tools.web_search import WebSearchTool

# ============================================================================
# 1. 构造校验
# ============================================================================


def test_construct_without_api_key_raises(monkeypatch):
    """没 TAVILY_API_KEY 环境变量 + 没传 api_key → RuntimeError。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        WebSearchTool()


def test_construct_with_env_var_ok(monkeypatch):
    """环境变量 TAVILY_API_KEY 设了 → 构造成功。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test_key_xxx")
    tool = WebSearchTool()
    assert tool._api_key == "test_key_xxx"
    assert tool._base_url == "https://api.tavily.com"


def test_construct_with_explicit_api_key_overrides_env(monkeypatch):
    """显式 api_key 优先于环境变量。"""
    monkeypatch.setenv("TAVILY_API_KEY", "from_env")
    tool = WebSearchTool(api_key="from_arg")
    assert tool._api_key == "from_arg"


def test_construct_with_custom_base_url():
    """自定义 base_url，且 rstrip /。"""
    tool = WebSearchTool(api_key="k", base_url="https://custom.example.com/")
    assert tool._base_url == "https://custom.example.com"


# ============================================================================
# 2. execute 参数校验（不需要网络）
# ============================================================================


@pytest.fixture
def tool():
    return WebSearchTool(api_key="test_key")


@pytest.mark.asyncio
async def test_execute_empty_query_raises(tool):
    with pytest.raises(ValueError, match="query"):
        await tool.execute("call-1", {"query": ""})


@pytest.mark.asyncio
async def test_execute_non_string_query_raises(tool):
    with pytest.raises(ValueError, match="query"):
        await tool.execute("call-1", {"query": 123})


@pytest.mark.asyncio
async def test_execute_max_results_out_of_range(tool):
    with pytest.raises(ValueError, match="max_results"):
        await tool.execute("call-1", {"query": "test", "max_results": 0})
    with pytest.raises(ValueError, match="max_results"):
        await tool.execute("call-1", {"query": "test", "max_results": 11})


@pytest.mark.asyncio
async def test_execute_invalid_search_depth(tool):
    with pytest.raises(ValueError, match="search_depth"):
        await tool.execute(
            "call-1", {"query": "test", "search_depth": "invalid"},
        )


# ============================================================================
# 3. execute 成功路径（mock httpx.AsyncClient）
# ============================================================================


class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._json


class _FakeAsyncClient:
    """伪 httpx.AsyncClient——记录 post 调用，返回预设 response。"""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append((url, json, headers))
        return self._response


@pytest.mark.asyncio
async def test_execute_success_with_results():
    """execute 成功——返回 markdown sections + details。"""
    fake_response = _FakeResponse({
        "results": [
            {"title": "Result One", "url": "https://one.example", "content": "content one"},
            {"title": "Result Two", "url": "https://two.example", "content": "content two"},
        ],
        "answer": "TL;DR answer",
    })
    fake_client = _FakeAsyncClient(fake_response)

    tool = WebSearchTool(api_key="k", http_client=fake_client)
    result = await tool.execute("call-1", {"query": "python asyncio"})

    assert result.tool_call_id == "call-1"
    assert result.name == "web_search"
    assert len(result.content) == 1
    text = result.content[0].text
    assert "**TL;DR**: TL;DR answer" in text
    assert "## 1. Result One" in text
    assert "https://one.example" in text
    assert "## 2. Result Two" in text
    # details
    assert result.details["query"] == "python asyncio"
    assert result.details["result_count"] == 2
    assert result.details["answer"] == "TL;DR answer"
    assert result.details["search_depth"] == "basic"
    # http 调用参数
    assert len(fake_client.calls) == 1
    url, payload, headers = fake_client.calls[0]
    assert url == "https://api.tavily.com/search"
    assert payload["query"] == "python asyncio"
    assert payload["max_results"] == 5
    assert payload["include_answer"] is True
    assert headers["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_execute_no_results_no_answer():
    """空结果 + 无 answer → "(no results)" 兜底。"""
    fake_response = _FakeResponse({"results": [], "answer": None})
    fake_client = _FakeAsyncClient(fake_response)
    tool = WebSearchTool(api_key="k", http_client=fake_client)
    result = await tool.execute("call-1", {"query": "test"})
    assert result.content[0].text == "(no results)"
    assert result.details["result_count"] == 0
    assert result.details["answer"] == ""


@pytest.mark.asyncio
async def test_execute_max_results_passed_through():
    """max_results 参数透传到 payload。"""
    fake_response = _FakeResponse({"results": [], "answer": ""})
    fake_client = _FakeAsyncClient(fake_response)
    tool = WebSearchTool(api_key="k", http_client=fake_client)
    await tool.execute(
        "call-1", {"query": "x", "max_results": 3, "search_depth": "advanced"},
    )
    _, payload, _ = fake_client.calls[0]
    assert payload["max_results"] == 3
    assert payload["search_depth"] == "advanced"

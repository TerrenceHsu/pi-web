"""Harness web-search tool.

实现 AgentTool 接口，execute() 内部用 httpx.AsyncClient 调
https://api.tavily.com/search，把 title/url/content 喂回 LLM。

设计要点：
- 注入式依赖：api_key / base_url / http_client 可在构造时传入，
  方便测试注入 mock；默认从环境变量 TAVILY_API_KEY 读 key。
- 错误冒泡：HTTP 4xx/5xx、网络异常一律 raise；run_event_loop 的
  _execute_tool_safely 会捕获并包成 is_error=True 的 ToolResult。
- 结果格式：每个 result 拼成 markdown section（title + url + content），
  前置可选的 TL;DR（Tavily 的 answer 字段）。
- details：原始结果数、query、answer 存进 details，给 UI/日志用。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, cast

import httpx

from ...messages import TextContent
from ...tooling import AgentTool, ToolResult, ToolUpdateCallback


class WebSearchTool(AgentTool):
    """网络搜索工具（Tavily Search API）。"""

    name = "web_search"
    label = "Web Search"
    description = (
        "Search the public web for up-to-date information using Tavily. "
        "Use this when the user asks about current events, recent data, "
        "or anything that may not be in your training data."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (1-10).",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "'basic' is faster; 'advanced' is more thorough.",
                "default": "basic",
            },
        },
        "required": ["query"],
    }
    execution_mode = "sequential"  # 联网工具默认串行，避免突发限流

    DEFAULT_BASE_URL = "https://api.tavily.com"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "未找到 TAVILY_API_KEY。请在 .env 设置或构造时传入 api_key。"
            )
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._http_client = http_client
        self._owns_client = http_client is None

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /search 并返回 JSON。HTTP 错误和网络错误都抛出。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        client = self._http_client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                resp = await c.post(
                    f"{self._base_url}/search", json=payload, headers=headers,
                )
                resp.raise_for_status()
                return cast("dict[str, Any]", resp.json())
        resp = await client.post(
            f"{self._base_url}/search", json=payload, headers=headers,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
        *,
        signal: asyncio.Event | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> ToolResult:
        query = args.get("query") or ""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        max_results = int(args.get("max_results", 5))
        if max_results < 1 or max_results > 10:
            raise ValueError(f"max_results 必须在 1-10 之间，得到 {max_results}")
        search_depth = args.get("search_depth", "basic")
        if search_depth not in ("basic", "advanced"):
            raise ValueError(f"search_depth 必须是 basic 或 advanced，得到 {search_depth!r}")

        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": True,
        }
        data = await self._post(payload)

        results = data.get("results") or []
        answer = data.get("answer") or ""

        sections: list[str] = []
        if answer:
            sections.append(f"**TL;DR**: {answer}\n")
        for i, r in enumerate(results, start=1):
            title = r.get("title", "(no title)")
            url = r.get("url", "")
            content = r.get("content", "")
            sections.append(f"## {i}. {title}\nURL: {url}\n{content}\n")

        text = "\n".join(sections) if sections else "(no results)"

        return ToolResult(
            tool_call_id=tool_call_id, name=self.name,
            content=[TextContent(text=text)],
            details={
                "query": query,
                "result_count": len(results),
                "answer": answer,
                "search_depth": search_depth,
            },
        )

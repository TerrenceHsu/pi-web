"""MCP tool → AgentTool 适配器（Step 16 新增 + 修订版）。

`MCPAgentTool` 把一个 MCP tool（来自 tools/list）包装成项目内部的 `AgentTool`，
注册进 `ToolRegistry` 后即可被 `run_event_loop` 执行。

字段映射：
    AgentTool.name        = "mcp__{server_name}__{tool_info.name}"
    AgentTool.label       = tool_info.name
    AgentTool.description = tool_info.description or fallback
    AgentTool.parameters  = tool_info.input_schema
    AgentTool.execution_mode = 构造传入（默认 parallel）

工具名 namespace（`mcp__{server}__{tool}`）保证：
- 不同 server 下的同名 MCP tool 不冲突
- MCP 工具与本地工具不冲突
- 调用 MCP 时用**原始** tool name（MCP 协议不认识 namespace）

修订要点（针对评审反馈）：
- `__init__` 加 server_name + tool name 兜底校验（防御外部手动构造 MCPToolInfo
  绕过 _parse_tool_info 的场景）
- details.raw 加体积限制（`max_raw_chars` 默认 4000 字符），避免 snapshot /
  session 持久化时被巨型 raw 拖累

错误边界契约：
- MCP 异常 / 任何 Exception → ToolResult(is_error=True)，
  details 含 source/server/mcp_tool + error_type/error
- **不让异常穿透到 run_event_loop**——loop 的 _execute_tool_with_hooks 兜底也会处理，
  但我们在 adapter 边界主动转换，details 字段更精确（含 mcp_tool 等）。
"""
from __future__ import annotations

import json
from typing import Any

from ..messages import TextContent
from ..tools import AgentTool, ToolExecutionMode, ToolResult
from .client import MCPClient, MCPToolInfo
from .naming import make_namespaced_tool_name

#: details.raw 的字符上限。raw 是 server 原始返回 dict 的字符串化结果，
#: 可能很大；snapshot / session 会持久化 details，需要节流。
#: 超过上限时截断并追加 "[truncated]" 标记。
DEFAULT_MAX_RAW_CHARS = 4000


def _stringify_raw(raw: Any, max_chars: int) -> tuple[str, bool]:
    """把 raw 转成稳定 JSON 字符串并截断到 max_chars。

    返回 (truncated_str, was_truncated)。
    - 用 `json.dumps(ensure_ascii=False, default=str)`，比 repr 更稳定、
      前端 / 缓存友好
    - `default=str` 兜底非 JSON-serializable 字段（如 datetime）
    """
    s = json.dumps(raw, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars] + "...[truncated]", True


def _attach_raw(
    details: dict[str, Any],
    raw: Any,
    *,
    include_raw: bool,
    max_chars: int,
) -> None:
    """把 raw 截断后写入 details（in-place）；raw 为 None 或 include_raw=False 跳过。

    成功 / is_error 分支共用：debug 时两类结果都需要看 server 原始返回。
    """
    if not include_raw or raw is None:
        return
    raw_str, truncated = _stringify_raw(raw, max_chars)
    details["raw"] = raw_str
    details["raw_truncated"] = truncated


class MCPAgentTool(AgentTool):
    """MCP tool 包装器。

    使用方式：
        client = MCPClient(config)
        await client.connect()
        await client.initialize()
        tools = await client.list_tools()
        agent_tools = [
            MCPAgentTool(server_name=config.name, client=client, tool_info=t)
            for t in tools
        ]
        registry = ToolRegistry(agent_tools)
    """

    def __init__(
        self,
        *,
        server_name: str,
        client: MCPClient,
        tool_info: MCPToolInfo,
        execution_mode: ToolExecutionMode = "parallel",
        max_raw_chars: int = DEFAULT_MAX_RAW_CHARS,
        include_raw: bool = True,
    ) -> None:
        """构造 MCPAgentTool。

        参数：
            server_name: MCP server 名（必须 [A-Za-z0-9_-]+，与 provider
                tool name 限制对齐；详见 naming.py）
            client: 已连接的 MCPClient
            tool_info: 来自 tools/list 的工具元信息
            execution_mode: Step 7 batch 模式（默认 parallel）
            max_raw_chars: details.raw 字段字符上限（默认 4000）
            include_raw: 是否在 details 写 raw 字段。生产场景若不想把
                server 原始 result 持久化进 session/snapshot，可设 False；
                Step 20 Web UI 调试时默认 True 以便排查。
        """
        # 兜底校验：用 naming 模块的统一字符集 + 长度限制
        namespaced = make_namespaced_tool_name(server_name, tool_info.name)

        self._server_name = server_name
        self._client = client
        self._tool_info = tool_info
        self._max_raw_chars = max_raw_chars
        self._include_raw = include_raw

        # —— AgentTool 字段（instance attribute 覆盖 class attribute）——
        self.name = namespaced
        self.label = tool_info.name
        self.description = (
            tool_info.description
            or f"MCP tool {tool_info.name} from {server_name}"
        )
        self.parameters = tool_info.input_schema or {
            "type": "object",
            "properties": {},
        }
        self.execution_mode = execution_mode

    # —— 属性 ——
    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def mcp_tool_name(self) -> str:
        """原始 MCP 工具名（不含 namespace 前缀）。"""
        return self._tool_info.name

    @property
    def tool_info(self) -> MCPToolInfo:
        return self._tool_info

    @property
    def client(self) -> MCPClient:
        return self._client

    # —— AgentTool 实现 ——

    async def execute(
        self,
        tool_call_id: str,
        args: dict[str, Any],
    ) -> ToolResult:
        """调用 MCP tool 并转成 ToolResult。

        所有异常（含 MCP 协议错误、网络错误）都转成 is_error=True 的 ToolResult，
        不穿透到 run_event_loop。
        """
        base_details: dict[str, Any] = {
            "source": "mcp",
            "server": self._server_name,
            "mcp_tool": self._tool_info.name,
        }
        try:
            result = await self._client.call_tool(self._tool_info.name, args)
        except Exception as exc:
            fail_details = dict(base_details)
            fail_details["error_type"] = type(exc).__name__
            fail_details["error"] = str(exc)
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=[TextContent(
                    text=f"MCP call failed: {type(exc).__name__}: {exc}",
                )],
                is_error=True,
                details=fail_details,
            )

        # server 端 isError=true（如工具内部报错）——也保留 raw 给 debug
        if result.is_error:
            err_details: dict[str, Any] = {
                **base_details,
                "error_type": "MCPToolCallError",
                "error": "server returned isError=true",
            }
            _attach_raw(
                err_details, result.raw,
                include_raw=self._include_raw, max_chars=self._max_raw_chars,
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=self.name,
                content=result.content or [TextContent(text="MCP tool error")],
                is_error=True,
                details=err_details,
            )

        # 成功路径
        details: dict[str, Any] = {
            **base_details,
            "content_count": len(result.content),
            "content_types": [
                c.type for c in result.content if hasattr(c, "type")
            ],
        }
        _attach_raw(
            details, result.raw,
            include_raw=self._include_raw, max_chars=self._max_raw_chars,
        )

        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=result.content,
            is_error=False,
            details=details,
        )


__all__ = ["MCPAgentTool"]

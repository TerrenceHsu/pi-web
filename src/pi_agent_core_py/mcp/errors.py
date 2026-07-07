"""MCP 异常体系（Step 16 新增）。

层级：
- MCPError                  所有 MCP 异常的基类
  ├─ MCPConnectionError     连接失败 / 进程起不来 / 网络层错误
  ├─ MCPProtocolError       JSON-RPC 协议错误（response 含 error、解析失败）
  ├─ MCPToolNotFoundError   tools/call 时 server 报告工具不存在
  ├─ MCPToolCallError       tools/call 通用失败（不含 protocol error）
  └─ MCPTransportClosedError  transport 已关闭（stdin EOF / 进程退出）

边界契约：
- 这些异常只在 mcp/ 包**内部**抛出。
- 到 `MCPAgentTool.execute()` 边界时**必须**捕获并转换成
  `ToolResult(is_error=True)`，**不要**让异常穿透到 `run_event_loop`。
"""
from __future__ import annotations


class MCPError(Exception):
    """MCP 所有异常的基类。"""


class MCPConnectionError(MCPError):
    """连接 MCP server 失败（subprocess 起不来、网络层错误等）。"""


class MCPProtocolError(MCPError):
    """JSON-RPC 协议错误：
    - response 不是合法 JSON
    - response 缺 result/error 字段
    - JSON-RPC error code 不属于已分类（非 -32602 tool not found 等）
    """


class MCPToolNotFoundError(MCPError):
    """`tools/call` 时 server 报告工具不存在。"""


class MCPToolCallError(MCPError):
    """`tools/call` 通用失败（server 返回 isError=true 或协议外的错误）。"""


class MCPTransportClosedError(MCPError):
    """transport 已关闭（stdin EOF / 进程退出 / 显式 close 后再调）。"""


__all__ = [
    "MCPError",
    "MCPConnectionError",
    "MCPProtocolError",
    "MCPToolNotFoundError",
    "MCPToolCallError",
    "MCPTransportClosedError",
]

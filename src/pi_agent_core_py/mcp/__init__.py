"""MCP 子包入口（Step 16 新增 + Step 19 prompts）。

导出：
- 配置 / 异常
- Transport（抽象 + local stdio + fake）
- Client
- Adapter（MCPAgentTool）
- Registry（MCPRegistry + MCPServerState）
- Prompts（Step 19）：MCPPromptArgument / MCPPromptInfo / MCPPromptMessage /
  MCPPromptResult / MCPPromptSkillAdapter / make_mcp_prompt_skill_name

不导出：内部辅助函数（_build_default_transport / _parse_message_content 等）。
"""
from __future__ import annotations

from .adapter import MCPAgentTool
from .client import MCPCallResult, MCPClient, MCPToolInfo
from .config import MCPServerConfig
from .errors import (
    MCPConnectionError,
    MCPError,
    MCPProtocolError,
    MCPToolCallError,
    MCPToolNotFoundError,
    MCPTransportClosedError,
)
from .naming import (
    MAX_NAMESPACED_NAME_LEN,
    MCP_NAME_RE,
    make_namespaced_tool_name,
    validate_namespace_part,
)
from .prompts import (
    MAX_MCP_PROMPT_SKILL_NAME_LEN,
    MCPPromptArgument,
    MCPPromptInfo,
    MCPPromptMessage,
    MCPPromptResult,
    MCPPromptSkillAdapter,
    make_mcp_prompt_skill_name,
)
from .registry import MCPRegistry, MCPServerState
from .transport import (
    FakeMCPTransport,
    MCPTransport,
    StdioMCPTransport,
)

__all__ = [
    # config
    "MCPServerConfig",
    # naming
    "MCP_NAME_RE",
    "MAX_NAMESPACED_NAME_LEN",
    "validate_namespace_part",
    "make_namespaced_tool_name",
    # errors
    "MCPError",
    "MCPConnectionError",
    "MCPProtocolError",
    "MCPToolNotFoundError",
    "MCPToolCallError",
    "MCPTransportClosedError",
    # transport
    "MCPTransport",
    "StdioMCPTransport",
    "FakeMCPTransport",
    # client
    "MCPClient",
    "MCPToolInfo",
    "MCPCallResult",
    # adapter
    "MCPAgentTool",
    # registry
    "MCPRegistry",
    "MCPServerState",
    # prompts (Step 19)
    "MCPPromptArgument",
    "MCPPromptInfo",
    "MCPPromptMessage",
    "MCPPromptResult",
    "MCPPromptSkillAdapter",
    "make_mcp_prompt_skill_name",
    "MAX_MCP_PROMPT_SKILL_NAME_LEN",
]

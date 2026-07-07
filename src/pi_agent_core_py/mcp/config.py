"""MCP Server 配置（Step 16 新增）。

`MCPServerConfig` 描述如何连接一个 MCP server：
- `transport="stdio"`：本地子进程（命令行启动 server binary / python -m xxx）
- `transport="http"`：远程 HTTP server（Step 16 仅占位）

校验：
- name 必须非空；只允许 `[A-Za-z0-9_-]`（因为会拼进工具名 mcp__{name}__tool）
- stdio 必须有 command
- http 必须有 url
- timeout_s 必须 > 0
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .naming import MCP_NAME_RE


class MCPServerConfig(BaseModel):
    """连接一个 MCP server 所需的全部信息。

    Step 16 MVP 完整支持 stdio；http 仅做抽象占位（连接时抛 NotImplementedError）。
    """
    name: str
    transport: Literal["stdio", "http"] = "stdio"

    # stdio 专属
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    # http 专属
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # 通用
    timeout_s: float = 30.0
    enabled: bool = True

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def _validate(self) -> MCPServerConfig:
        if not self.name or not self.name.strip():
            raise ValueError("MCPServerConfig.name 必须非空")
        if not MCP_NAME_RE.match(self.name):
            raise ValueError(
                f"MCPServerConfig.name '{self.name}' 含非法字符；"
                "只允许 [A-Za-z0-9_-]"
            )
        if self.transport == "stdio" and not self.command:
            raise ValueError(
                f"MCP server '{self.name}': transport=stdio 时必须提供 command"
            )
        if self.transport == "http" and not self.url:
            raise ValueError(
                f"MCP server '{self.name}': transport=http 时必须提供 url"
            )
        if self.timeout_s <= 0:
            raise ValueError(
                f"MCP server '{self.name}': timeout_s must be > 0, got {self.timeout_s}"
            )
        return self


__all__ = ["MCPServerConfig"]

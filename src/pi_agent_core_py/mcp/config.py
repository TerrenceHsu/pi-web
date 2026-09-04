"""MCP Server 配置（Step 16 新增）。

`MCPServerConfig` 描述如何连接一个 MCP server：
- `transport="stdio"`：本地子进程（命令行启动 server binary / python -m xxx）
- `transport="http"`：远程 Streamable HTTP endpoint

校验：
- name 必须非空；只允许 `[A-Za-z0-9_-]`（因为会拼进工具名 mcp__{name}__tool）
- stdio 必须有 command
- timeout_s 必须 > 0
"""
from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .naming import MCP_NAME_RE


class MCPServerConfig(BaseModel):
    """连接一个 MCP server 所需的全部信息。"""
    name: str
    transport: Literal["stdio", "http"] = "stdio"

    # stdio 专属
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    # Streamable HTTP 专属。headers 只存在于运行时；Web 持久化层保存的是
    # header -> environment variable 的引用，绝不把 secret value 写入 SQLite。
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    protocol_version: str | None = None

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
        if self.transport == "http":
            if not self.url:
                raise ValueError(
                    f"MCP server '{self.name}': transport=http 时必须提供 url"
                )
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    f"MCP server '{self.name}': url 必须是 http(s) absolute URL"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(
                    f"MCP server '{self.name}': url 不允许嵌入 credentials"
                )
            reserved = {
                "accept",
                "content-type",
                "mcp-session-id",
                "mcp-protocol-version",
            }
            conflict = sorted(k for k in self.headers if k.lower() in reserved)
            if conflict:
                raise ValueError(
                    f"MCP server '{self.name}': headers 包含 transport 保留字段: "
                    + ", ".join(conflict)
                )
        if self.timeout_s <= 0:
            raise ValueError(
                f"MCP server '{self.name}': timeout_s must be > 0, got {self.timeout_s}"
            )
        return self


__all__ = ["MCPServerConfig"]

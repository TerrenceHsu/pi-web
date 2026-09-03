"""MCP Client（Step 16 新增 + 修订版：并发安全 / 协议校验 / timeout）。

`MCPClient` 封装 JSON-RPC 2.0 over transport 的细节，向上暴露：
- `connect()`         建立 transport
- `initialize()`      MCP 握手
- `list_tools()`      tools/list → list[MCPToolInfo]
- `call_tool(name, arguments)`  tools/call → MCPCallResult
- `close()`           关闭 transport

修订要点（针对评审反馈）：
- **并发安全**：`_request` 用 `asyncio.Lock` 串行化同一 client 上的请求。
  parallel 模式下多工具调同一 server 也不会出现 response 错位。
- **JSON-RPC 校验**：response 必须 `jsonrpc == "2.0"` 且 `id == req_id`，
  否则报 MCPProtocolError。
- **timeout**：`asyncio.wait_for(receive, timeout=config.timeout_s)`，
  server 卡死不会让 agent turn 永久挂住。
- **tool name 校验**：tools/list 解析时拒绝非法 name（`[A-Za-z0-9_-]+`，
  与 provider tool name 限制对齐；详见 `naming.py`）。
- **initialize 幂等保留 server result**：缓存 `_initialize_result`，重复调用返回副本。
- **list_tools 严格**：缺 tools 字段 / tools 非 list 时报 MCPProtocolError。
- **MCPAgentTool details 含 raw**：成功结果 details 加 raw 字段（已经在 adapter.py 改）。

兼容字段：
- MCP 标准 inputSchema，转内部 input_schema
- MCP 标准 isError，转内部 is_error
- content 只解析 {"type":"text","text":"..."}；其它类型转占位说明
"""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..ai.messages import TextContent
from .config import MCPServerConfig
from .errors import (
    MCPError,
    MCPProtocolError,
    MCPToolNotFoundError,
)
from .naming import MCP_NAME_RE
from .prompts import (
    MCPPromptInfo,
    MCPPromptMessage,
    MCPPromptResult,
    _parse_message_content,
)
from .transport import MCPTransport, StdioMCPTransport


class MCPToolInfo(BaseModel):
    """MCP server 暴露的一个工具的元信息（来自 tools/list）。

    `input_schema` 用 JSON Schema dict（MCP 原始字段 `inputSchema`）。
    `name` 字段强制校验字符集（`[A-Za-z0-9_-]+`），保证构造 MCPAgentTool
    时拼出的 namespaced name 合法。
    """
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
    )
    raw: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not MCP_NAME_RE.match(v):
            raise ValueError(
                f"invalid MCP tool name {v!r}; only [A-Za-z0-9_-] allowed"
            )
        return v


class MCPCallResult(BaseModel):
    """`tools/call` 的解析结果。

    `content` 是给 LLM 看的 TextContent 列表；
    `is_error` 为 True 时，MCPAgentTool 会包成 is_error=True 的 ToolResult。
    `raw` 保留 server 原始 result dict，给 UI / observability。
    """
    content: list[TextContent] = Field(default_factory=list)
    is_error: bool = False
    raw: dict[str, Any] | None = None


# ============================================================================
# Client
# ============================================================================


_CLIENT_INFO = {"name": "pi-agent-core-py", "version": "0.1.0"}
_PROTOCOL_VERSION = "2024-11-05"


def _build_default_transport(config: MCPServerConfig) -> MCPTransport:
    """Build the local stdio transport supported by the Web product."""
    if config.transport != "stdio":  # defensive guard for unchecked construction
        raise MCPProtocolError(f"unsupported transport type: {config.transport}")
    assert config.command is not None  # 由 MCPServerConfig 校验保证
    return StdioMCPTransport(
        config.command,
        config.args,
        cwd=config.cwd,
        env=config.env,
    )


class MCPClient:
    """MCP server 客户端。

    生命周期：`connect()` → `initialize()` → 业务调用 → `close()`。
    `close()` 幂等。

    **并发**：同一 client 上的请求自动串行化（`asyncio.Lock`）。parallel
    模式下多工具调同一 server 不会出现 response 错位。
    """

    def __init__(
        self,
        config: MCPServerConfig,
        transport: MCPTransport | None = None,
    ) -> None:
        self._config = config
        self._transport: MCPTransport = transport or _build_default_transport(config)
        self._next_id: int = 1
        self._initialized: bool = False
        self._closed: bool = False
        self._initialize_result: dict[str, Any] | None = None
        # 串行化同一 client 上的请求——parallel 模式下多工具调同一 server
        # 时，防止 send/receive 错位（_request 内部加锁）。
        self._request_lock: asyncio.Lock = asyncio.Lock()

    # —— 属性 ——
    @property
    def config(self) -> MCPServerConfig:
        return self._config

    @property
    def server_name(self) -> str:
        return self._config.name

    @property
    def transport(self) -> MCPTransport:
        return self._transport

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def closed(self) -> bool:
        """client 是否已关闭（registry / harness 据此判断连接状态）。"""
        return self._closed

    @property
    def initialize_result(self) -> dict[str, Any] | None:
        """server 在 initialize 阶段返回的能力描述（缓存副本）。"""
        return dict(self._initialize_result) if self._initialize_result else None

    # —— 生命周期 ——

    async def connect(self) -> None:
        """建立 transport 连接。

        一旦 client 被 `close()`，就不能再 connect——close 是终结语义。
        """
        if self._closed:
            raise MCPProtocolError(
                f"MCPClient({self._config.name}): cannot connect after close"
            )
        await self._transport.connect()

    async def initialize(self) -> dict[str, Any]:
        """MCP 握手；返回 server 返回的 result dict（缓存）。

        重复调用幂等：已 initialized 时返回缓存的 result 副本。
        """
        if self._initialized:
            return dict(self._initialize_result or {})
        params = {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        }
        result = await self._request("initialize", params)
        self._initialize_result = result
        self._initialized = True
        return result

    async def list_tools(self) -> list[MCPToolInfo]:
        """tools/list → list[MCPToolInfo]。

        协议：server 返回 {"tools": [{name, description, inputSchema}, ...]}

        严格模式：缺 tools 字段 / tools 非 list / 单个 tool 名非法时
        抛 MCPProtocolError。
        """
        result = await self._request("tools/list", {})
        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): tools/list result is not a dict"
            )
        if "tools" not in result:
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): tools/list response missing 'tools'"
            )
        tools_raw = result["tools"]
        if not isinstance(tools_raw, list):
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): tools/list 'tools' must be a list, "
                f"got {type(tools_raw).__name__}"
            )

        out: list[MCPToolInfo] = []
        for t in tools_raw:
            if not isinstance(t, dict):
                # 严格模式：坏 server 应该报告协议错误，不静默跳过
                raise MCPProtocolError(
                    f"MCPClient({self.server_name}): tools/list entry must be an object, "
                    f"got {type(t).__name__}: {t!r}"
                )
            info = self._parse_tool_info(t)
            out.append(info)
        return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> MCPCallResult:
        """tools/call → MCPCallResult。

        - server 返回 isError=true → MCPCallResult(is_error=True)（不抛异常）
        - JSON-RPC error 且 message 含 "not found" → MCPToolNotFoundError
        - 其它 JSON-RPC error → MCPProtocolError
        """
        params = {"name": name, "arguments": arguments}
        result = await self._request("tools/call", params)
        return self._parse_call_result(result)

    async def list_prompts(self) -> list[MCPPromptInfo]:
        """prompts/list → list[MCPPromptInfo]（Step 19 新增）。

        协议：server 返回 {"prompts": [{name, description, arguments}, ...]}

        严格模式：缺 prompts 字段 / prompts 非 list / 单个 prompt 名非法 →
        MCPProtocolError（与 list_tools 同样的严格性）。
        """
        result = await self._request("prompts/list", {})
        if not isinstance(result, dict):
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): prompts/list result is not a dict"
            )
        if "prompts" not in result:
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): prompts/list response missing 'prompts'"
            )
        prompts_raw = result["prompts"]
        if not isinstance(prompts_raw, list):
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): prompts/list 'prompts' must be a list, "
                f"got {type(prompts_raw).__name__}"
            )

        out: list[MCPPromptInfo] = []
        for p in prompts_raw:
            if not isinstance(p, dict):
                raise MCPProtocolError(
                    f"MCPClient({self.server_name}): prompts/list entry must be an object, "
                    f"got {type(p).__name__}: {p!r}"
                )
            out.append(self._parse_prompt_info(p))
        return out

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        include_raw: bool = False,
    ) -> MCPPromptResult:
        """prompts/get → MCPPromptResult（Step 19 新增）。

        - 只解析 text content；非 text content 转占位
        - include_raw=True 时保留 server 原始 result dict（默认 False——避免
          把巨型 raw 带进 Skill.prompt）
        - JSON-RPC error 含 "not found" + method == prompts/get →
          MCPProtocolError（统一按协议错；Step 19 暂不区分 prompt not found）
        """
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        result = await self._request("prompts/get", params)
        return self._parse_prompt_result(result, include_raw=include_raw)

    async def close(self) -> None:
        """关闭 transport。幂等。"""
        if self._closed:
            return
        self._closed = True
        self._initialized = False
        await self._transport.close()

    # —— 内部 ——

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """发送 JSON-RPC request，await 一个 response，返回 result dict。

        并发安全：同一 client 上的请求自动串行化（`_request_lock`）。
        transport / 协议 / timeout 错误都转成 MCPError 子类抛出。
        """
        if self._closed:
            raise MCPProtocolError(f"MCPClient({self.server_name}): client closed")

        # 锁内串行化：send → receive 配对完整，避免并发场景错位。
        async with self._request_lock:
            return await self._request_unlocked(method, params)

    async def _request_unlocked(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """实际 send + receive（无锁）。调用方持 `_request_lock`。"""
        req_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params

        # send timeout：transport.send 通常即时返回，但若卡住说明 transport 不可靠
        # —— close client，避免后续请求处于未知状态。
        try:
            await asyncio.wait_for(
                self._transport.send(message),
                timeout=self._config.timeout_s,
            )
        except TimeoutError as e:
            try:
                await self.close()
            except Exception:
                pass
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): {method} send timeout "
                f"after {self._config.timeout_s}s (client closed)"
            ) from e
        except MCPError:
            # transport 层抛 MCPError（如 MCPTransportClosedError / MCPProtocolError
            # from invalid JSON line / empty line）——transport 已经异常，必须 close
            # client，避免 _closed=False 但 transport 不可用的状态不一致
            try:
                await self.close()
            except Exception:
                pass
            raise

        # receive 是最可能卡住的环节——server 卡死时 stdout.readline 永久等待。
        try:
            resp = await asyncio.wait_for(
                self._transport.receive(),
                timeout=self._config.timeout_s,
            )
        except TimeoutError as e:
            # timeout 后 transport 处于"半开"状态：server 之后才返回的旧 response
            # 可能被下一次 request 的 receive 读到，触发 id mismatch。最安全的
            # 做法是直接 close client——后续请求会拿到 client closed 错误，
            # 调用方知道要重连。
            try:
                await self.close()
            except Exception:
                pass
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): {method} receive timeout "
                f"after {self._config.timeout_s}s (client closed)"
            ) from e
        except MCPError:
            # 同 send：transport 层抛 MCPError 后 transport 已不可用，close client
            try:
                await self.close()
            except Exception:
                pass
            raise

        # —— JSON-RPC framing 错误：stream 已不可靠，必须 close client ——
        if not isinstance(resp, dict):
            await self._safe_close_after_protocol_error()
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): response is not a JSON object "
                f"(client closed)"
            )

        if resp.get("jsonrpc") != "2.0":
            await self._safe_close_after_protocol_error()
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): response jsonrpc field must be '2.0', "
                f"got {resp.get('jsonrpc')!r} (client closed)"
            )
        if resp.get("id") != req_id:
            # id mismatch 通常意味着 stream 错位（旧 response 被读到）
            await self._safe_close_after_protocol_error()
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): response id mismatch: "
                f"expected {req_id}, got {resp.get('id')!r} (client closed)"
            )

        # —— JSON-RPC error：server 端报错（如 tools/call tool not found）——
        # 这种情况下 transport 本身仍然健康（一问一答配对完整），**不 close**。
        if "error" in resp:
            err = resp.get("error") or {}
            if not isinstance(err, dict):
                err = {"message": str(err)}
            code = err.get("code")
            msg = str(err.get("message") or "unknown error")
            lower = msg.lower()
            # tools/call 找不到工具：-32602 invalid params + "tool ... not found"
            if "not found" in lower and ("tool" in lower or method == "tools/call"):
                raise MCPToolNotFoundError(
                    f"MCPClient({self.server_name}): tool not found: {msg}"
                )
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): {method} failed: "
                f"code={code} message={msg}"
            )

        if "result" not in resp:
            # 缺 result 字段属于 framing 异常，stream 已不可靠
            await self._safe_close_after_protocol_error()
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): {method} response missing 'result' "
                f"(client closed)"
            )
        result = resp["result"]
        if result is None:
            return {}
        if not isinstance(result, dict):
            # MCP initialize / tools/list / tools/call 的 result 都必须是 object。
            # 非 dict（如 list / string）属于协议错——close client，避免掩盖 bug。
            await self._safe_close_after_protocol_error()
            raise MCPProtocolError(
                f"MCPClient({self.server_name}): {method} result must be object, "
                f"got {type(result).__name__} (client closed)"
            )
        return result

    async def _safe_close_after_protocol_error(self) -> None:
        """协议 framing 错误后 close client；吞掉 close 本身的异常。"""
        try:
            await self.close()
        except Exception:
            pass

    @staticmethod
    def _parse_tool_info(raw: dict[str, Any]) -> MCPToolInfo:
        """tools/list item → MCPToolInfo。

        兼容 inputSchema / input_schema。校验 tool name 字符集（MCP_NAME_RE）。
        """
        name = str(raw.get("name", ""))
        if not name:
            raise MCPProtocolError(
                f"MCP tools/list: tool entry missing 'name' field: {raw!r}"
            )
        if not MCP_NAME_RE.match(name):
            raise MCPProtocolError(
                f"MCP tools/list: invalid tool name {name!r}; "
                "only [A-Za-z0-9_-] allowed (provider tool name 限制)"
            )

        schema = raw.get("inputSchema")
        if schema is None:
            schema = raw.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        return MCPToolInfo(
            name=name,
            description=str(raw.get("description", "")),
            input_schema=schema,
            raw=raw,
        )

    @staticmethod
    def _parse_call_result(result: Any) -> MCPCallResult:
        """tools/call result → MCPCallResult。

        - 兼容 isError / is_error
        - content 只解析 text 项；其它类型转占位说明
        """
        if not isinstance(result, dict):
            return MCPCallResult(
                content=[TextContent(text=f"(non-object result: {result!r})")],
                is_error=True,
            )

        is_error = bool(
            result.get("isError")
            or result.get("is_error")
            or False
        )

        raw_items = result.get("content") or []
        if not isinstance(raw_items, list):
            raw_items = []

        contents: list[TextContent] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                contents.append(TextContent(text=str(item.get("text", ""))))
            else:
                contents.append(TextContent(
                    text=f"(unsupported content type: {t!r})"
                ))

        return MCPCallResult(content=contents, is_error=is_error, raw=result)

    @staticmethod
    def _parse_prompt_info(raw: dict[str, Any]) -> MCPPromptInfo:
        """prompts/list item → MCPPromptInfo（Step 19）。

        校验 prompt name 字符集（MCP_NAME_RE）。
        arguments 列表中每项必须有 name 字段；其它字段缺失时给默认。
        """
        from .prompts import MCPPromptArgument  # 局部 import 避免循环

        name = str(raw.get("name", ""))
        if not name:
            raise MCPProtocolError(
                f"MCP prompts/list: prompt entry missing 'name' field: {raw!r}"
            )
        if not MCP_NAME_RE.match(name):
            raise MCPProtocolError(
                f"MCP prompts/list: invalid prompt name {name!r}; "
                "only [A-Za-z0-9_-] allowed"
            )

        args_raw = raw.get("arguments") or []
        if not isinstance(args_raw, list):
            args_raw = []
        args: list[MCPPromptArgument] = []
        for a in args_raw:
            if not isinstance(a, dict):
                continue
            arg_name = str(a.get("name", ""))
            if not arg_name:
                # 缺 name 字段跳过；与 tool inputSchema 不一样，prompt arg
                # 是声明性 metadata，不强制 schema
                continue
            args.append(MCPPromptArgument(
                name=arg_name,
                description=str(a.get("description", "")),
                required=bool(a.get("required", False)),
            ))

        return MCPPromptInfo(
            name=name,
            description=str(raw.get("description", "")),
            arguments=args,
        )

    @staticmethod
    def _parse_prompt_result(
        result: Any,
        *,
        include_raw: bool,
    ) -> MCPPromptResult:
        """prompts/get result → MCPPromptResult（Step 19）。

        - 兼容 description 缺失（默认空字符串）
        - messages 缺失 → 空 list；messages 非 list → MCPProtocolError（P1 修订：
          原来静默返回空，会让"server 返回坏数据"被掩盖）
        - 单条 message 非 dict：跳过（防御坏 server，不抛错）
        - non-dict result is normally rejected by _request() (which closes
          the client and raises MCPProtocolError); the non-dict branch below
          is defensive only.
        """
        if not isinstance(result, dict):
            # Defensive only —— _request() 已经在 result 不是 dict 时 close
            # client 并抛 MCPProtocolError，正常路径下不会走到这里。
            return MCPPromptResult()

        description = str(result.get("description", ""))

        msgs_raw = result.get("messages")
        if msgs_raw is None:
            msgs_raw = []
        elif not isinstance(msgs_raw, list):
            # P1 修订：协议错——server 返回坏数据。原来静默 [] 会掩盖问题
            raise MCPProtocolError(
                f"MCP prompts/get: 'messages' must be a list, "
                f"got {type(msgs_raw).__name__}"
            )

        messages: list[MCPPromptMessage] = []
        for m in msgs_raw:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "user"))
            content = _parse_message_content(m.get("content"))
            messages.append(MCPPromptMessage(role=role, content=content))

        return MCPPromptResult(
            description=description,
            messages=messages,
            raw=dict(result) if include_raw else None,
        )


__all__ = [
    "MCPToolInfo",
    "MCPCallResult",
    "MCPClient",
    # Step 19 重新导出（便于 from .client import ...）
    "MCPPromptInfo",
    "MCPPromptMessage",
    "MCPPromptResult",
]

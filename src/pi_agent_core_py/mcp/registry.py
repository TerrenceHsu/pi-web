"""MCP Registry——多 MCP server 管理
（Step 16 + 修订版 + Step 17 client_factory + Step 19 prompts）。

`MCPRegistry` 维护一组 `MCPClient` + 每个 server 的连接状态 / 工具列表：
- `connect_all()`     逐个连接所有 enabled server；单个失败不影响其它
- `refresh_tools()`   重新拉 tools/list，构造 MCPAgentTool
- `to_agent_tools()`  返回所有 connected server 的 MCPAgentTool
- `close_all()`       幂等关闭所有 client
- `list_servers()`    返回 server 状态
- `list_tools()`      返回所有 MCPToolInfo
- `list_agent_tools()` 返回 MCPAgentTool（含 namespaced name + server 来源，给 UI 用）

Step 17 新增：`client_factory` 参数。生产默认 `MCPClient(cfg)`；测试可注入
使用 FakeMCPTransport 的 factory，避免起真实子进程。

Step 19 新增（prompts）：
- `refresh_prompts()` 重新拉 prompts/list，缓存 MCPPromptInfo（不影响 tools）
- `list_prompts()`     返回 [(server_name, MCPPromptInfo), ...]
- `get_prompt(...)`    调对应 server 的 prompts/get
- `get_client(name)`   暴露 live MCPClient 给 adapter 用
- `close_all()`        现在也清 prompts cache

修订要点（针对评审反馈）：
- 文档明确"逐个连接"而非"并发连接"（避免状态时序复杂）。
- `_connect_one`：connect 成功但 initialize 失败时**主动 close client**，
  避免遗留 stdio 子进程。
- 重复 `connect_all()` 不泄露旧 client：进入 `_connect_one` 时若已存在
  旧 client，先 pop 并 close。
- 新增 `list_agent_tools()`：返回 MCPAgentTool，调用方无需绕到 ToolRegistry
  就能拿到 namespaced name + server 来源（Step 17 / 20 Web UI 需要）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from .adapter import MCPAgentTool
from .client import MCPClient, MCPToolInfo
from .config import MCPServerConfig
from .prompts import MCPPromptInfo, MCPPromptResult

# ============================================================================
# Server 状态
# ============================================================================


class MCPServerState(BaseModel):
    """单个 MCP server 的运行态快照。

    用于 UI / observability；不影响协议。
    """
    name: str
    connected: bool = False
    tool_count: int = 0
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Registry
# ============================================================================


class MCPRegistry:
    """多 MCP server 注册中心。

    生命周期：
        connect_all() → refresh_tools() / to_agent_tools() → close_all()
    """

    def __init__(
        self,
        configs: list[MCPServerConfig] | None = None,
        *,
        client_factory: Callable[[MCPServerConfig], MCPClient] | None = None,
    ) -> None:
        """初始化 MCPRegistry。

        参数：
            configs: MCP server 配置列表
            client_factory: 自定义 MCPClient 构造函数（测试友好）。
                默认 None → 用 `MCPClient(cfg)`（生产路径，stdio/http transport）。
                测试可传入使用 FakeMCPTransport 的 factory，避免起真实子进程。
                factory 必须接受 MCPServerConfig 返回 MCPClient。
        """
        self._configs: list[MCPServerConfig] = list(configs or [])
        self._client_factory: Callable[[MCPServerConfig], MCPClient] = (
            client_factory or (lambda cfg: MCPClient(cfg))
        )
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list[MCPAgentTool]] = {}
        self._states: dict[str, MCPServerState] = {
            cfg.name: MCPServerState(name=cfg.name)
            for cfg in self._configs
        }
        # Step 19：prompts cache（server_name → list[MCPPromptInfo]）
        # 与 _tools 类似——refresh_prompts 填充；close_all / detach 清空。
        self._prompts: dict[str, list[MCPPromptInfo]] = {}

    # —— 属性 ——
    @property
    def configs(self) -> list[MCPServerConfig]:
        return list(self._configs)

    def get_state(self, name: str) -> MCPServerState | None:
        return self._states.get(name)

    # —— 生命周期 ——

    async def connect_all(self) -> None:
        """**逐个**连接所有 enabled 的 server；单个失败不影响其它。

        Step 16 MVP 不做并发连接——避免状态时序、日志交错、event loop 压力
        等复杂度。如果某个 server 长时间 hang，整体 connect_all 会变慢；
        timeout 由 `MCPServerConfig.timeout_s` + MCPClient 内 wait_for 兜底。
        """
        for cfg in self._configs:
            if not cfg.enabled:
                continue
            await self._connect_one(cfg)

    async def _connect_one(self, cfg: MCPServerConfig) -> None:
        """连接单个 server，记录状态。

        - 如果该 server 已有旧 client，先 close 旧 client（避免泄露）
        - 同时清掉旧 _tools / _prompts 缓存，避免返回指向已断开 server 的工具 / prompts
        - connect 成功但 initialize 失败：主动 close 新 client
        """
        state = self._states.setdefault(cfg.name, MCPServerState(name=cfg.name))

        # 释放可能存在的旧 client（重复 connect_all 场景）
        old = self._clients.pop(cfg.name, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        # 同步清掉旧工具 / prompts 缓存——避免 to_agent_tools() /
        # list_agent_tools() / list_prompts() 返回指向已断开 server 的对象
        self._tools.pop(cfg.name, None)
        self._prompts.pop(cfg.name, None)
        state.tool_count = 0

        client: MCPClient | None = None
        try:
            client = self._client_factory(cfg)
            await client.connect()
            await client.initialize()
            self._clients[cfg.name] = client
            state.connected = True
            state.last_error = None
        except Exception as e:
            # 关键：connect 成功但 initialize 失败时，client 已经持有 transport
            # （如 stdio 子进程），必须主动 close 释放资源
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            state.connected = False
            state.last_error = f"{type(e).__name__}: {e}"

    async def refresh_tools(self) -> list[MCPAgentTool]:
        """从所有 connected server 拉 tools/list，构造 MCPAgentTool。

        - 单个 server 拉取失败：state.last_error 记录，不影响其它
        - 失败时同步状态：`state.connected=False` + pop client + close client
          （MCPClient 内部对 framing / timeout 错误已 close，但 registry
          必须把 client 从 `_clients` 移除，否则 list_servers 显示连接但
          client.closed=True 状态不一致）
        - 返回所有成功的 MCPAgentTool 列表（按 server 配置顺序）
        """
        all_tools: list[MCPAgentTool] = []
        for cfg in self._configs:
            client = self._clients.get(cfg.name)
            state = self._states.get(cfg.name)
            if client is None or state is None:
                continue
            try:
                infos = await client.list_tools()
                tools = [
                    MCPAgentTool(
                        server_name=cfg.name,
                        client=client,
                        tool_info=info,
                    )
                    for info in infos
                ]
                self._tools[cfg.name] = tools
                state.tool_count = len(tools)
                state.last_error = None
                all_tools.extend(tools)
            except Exception as e:
                # 失败：清工具缓存 + 标 disconnected + 把 client 从 _clients 移除
                self._tools[cfg.name] = []
                state.tool_count = 0
                state.connected = False
                state.last_error = f"{type(e).__name__}: {e}"
                # Step 19：同步清 prompts cache——client 失败时既有的 tools
                # 和 prompts 缓存都不再可信
                self._prompts.pop(cfg.name, None)
                # 移除并 close 失败的 client（MCPClient 可能已经内部 close，
                # 这里再 close 一次是幂等的）
                failed_client = self._clients.pop(cfg.name, None)
                if failed_client is not None:
                    try:
                        await failed_client.close()
                    except Exception:
                        pass
        return all_tools

    async def to_agent_tools(self) -> list[MCPAgentTool]:
        """返回所有 connected server 的 MCPAgentTool。

        首次调用触发 `refresh_tools`；之后返回缓存（除非显式 refresh）。
        """
        if not self._tools:
            return await self.refresh_tools()
        out: list[MCPAgentTool] = []
        for cfg in self._configs:
            out.extend(self._tools.get(cfg.name, []))
        return out

    # —— Step 19：prompts ——

    async def refresh_prompts(self) -> list[tuple[str, MCPPromptInfo]]:
        """从所有 connected server 拉 prompts/list，缓存结果。

        - 单个 server 失败：清 prompts cache + 写 state.metadata，不影响其它 server
        - **失败处理按"连接是否仍可用"分级**（P0 修订 + 本轮 P1 修订）：
            * client.closed=True（framing error / timeout / transport closed）：
              连接确实不可用——标 disconnected + 写 state.last_error +
              pop client + close client。这种情况下 tools cache 也不再可信
              （client 已断），由调用方按需 refresh_tools。
            * client.closed=False（如 JSON-RPC method not found / "prompts
              not supported"）：只清 prompts cache + **只**写
              state.metadata["prompts_last_error"]。**不动 state.last_error
              和 state.connected**——避免 UI 把"可选 prompts capability 失败"
              误读成"server 整体失败"。一个不支持 prompts 的 server 仍可以
              正常提供 tools。
        - 返回 [(server_name, MCPPromptInfo), ...]（按 server 配置顺序 + prompt 顺序）

        不主动清 tools 缓存（tools / prompts 在 MCP spec 中是独立 capability）。
        """
        all_prompts: list[tuple[str, MCPPromptInfo]] = []
        for cfg in self._configs:
            client = self._clients.get(cfg.name)
            state = self._states.get(cfg.name)
            if client is None or state is None:
                continue
            try:
                infos = await client.list_prompts()
                self._prompts[cfg.name] = list(infos)
                if state is not None:
                    # 成功：清掉之前可能的 prompts_last_error；不动 last_error
                    # （last_error 是 tools/transport 级别的状态，由 refresh_tools
                    # / connect_all 维护）
                    state.metadata.pop("prompts_last_error", None)
                for info in infos:
                    all_prompts.append((cfg.name, info))
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                # 失败一定清 prompts cache
                self._prompts[cfg.name] = []

                # P0 修订：只有 client 已 closed 时才认为连接不可用
                # （framing error / timeout / transport closed）。其它情况
                # （method not found / 协议级 reject）保留 connected=True——
                # 这个 server 仍可能正常提供 tools / call_tool。
                client_closed = bool(getattr(client, "closed", False))
                if client_closed:
                    # 连接真的断了——更新 state.connected + state.last_error
                    if state is not None:
                        state.connected = False
                        state.last_error = err_msg
                    failed_client = self._clients.pop(cfg.name, None)
                    if failed_client is not None:
                        try:
                            await failed_client.close()
                        except Exception:
                            pass
                else:
                    # 软失败（method not found 等）——只写 prompts_last_error，
                    # **不动 state.last_error / state.connected**（本轮 P1 修订）
                    if state is not None:
                        state.metadata["prompts_last_error"] = err_msg
        return all_prompts

    def list_prompts(self) -> list[tuple[str, MCPPromptInfo]]:
        """返回所有缓存的 prompts（按 server 配置顺序 + prompt 顺序）。

        没有 refresh_prompts 过 / refresh 失败 → 返回空列表。
        """
        out: list[tuple[str, MCPPromptInfo]] = []
        for cfg in self._configs:
            for info in self._prompts.get(cfg.name, []):
                out.append((cfg.name, info))
        return out

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPPromptResult:
        """对指定 server 调用 prompts/get。

        - server 不存在 / 未连接 → ValueError（这是调用方的程序错误，不是协议错）
        - 协议错误（MCPProtocolError 等）穿透抛出
        """
        client = self.get_client(server_name)
        if client is None:
            raise ValueError(
                f"MCPRegistry.get_prompt: server {server_name!r} "
                f"not connected or not in registry"
            )
        return await client.get_prompt(prompt_name, arguments=arguments)

    def get_client(self, name: str) -> MCPClient | None:
        """取指定 server 的 live MCPClient（未连接 / 不存在返回 None）。

        Step 19 引入：MCPPromptSkillAdapter 等组件需要直接持有 client
        做 prompts/get，而不破坏 registry 的连接管理。
        """
        return self._clients.get(name)

    async def close_all(self) -> None:
        """关闭所有 client 并清理工具 / prompts 缓存 / 状态。幂等。

        注意：**不能**因 `_clients` 为空就提前 return——可能存在 `_clients` 已
        被清但 `_tools` / `_prompts` 还残留缓存的场景（外部测试 fixture 或异常
        路径），那样 to_agent_tools() / list_prompts() 仍会返回 stale 数据。
        """
        clients = list(self._clients.values())

        async def _safe_close(c: MCPClient) -> None:
            try:
                await c.close()
            except Exception:
                pass

        if clients:
            await asyncio.gather(
                *[_safe_close(c) for c in clients],
                return_exceptions=True,
            )

        # 无论 _clients 是否为空，都清 _tools + _prompts + states，避免 stale 缓存
        self._clients.clear()
        self._tools.clear()
        self._prompts.clear()
        for state in self._states.values():
            state.connected = False
            state.tool_count = 0

    # —— 查询 ——

    def list_servers(self) -> list[MCPServerState]:
        """所有 server 状态（按配置顺序）。"""
        return [self._states[cfg.name] for cfg in self._configs
                if cfg.name in self._states]

    def list_tools(self) -> list[MCPToolInfo]:
        """所有缓存的 MCPToolInfo（按 server 配置顺序 + tool 顺序）。

        注意：MCPToolInfo 本身不含 server 来源；要拿 server 名请用
        `list_agent_tools()`。
        """
        out: list[MCPToolInfo] = []
        for cfg in self._configs:
            for tool in self._tools.get(cfg.name, []):
                out.append(tool.tool_info)
        return out

    def list_agent_tools(self) -> list[MCPAgentTool]:
        """所有缓存的 MCPAgentTool（按 server 配置顺序 + tool 顺序）。

        比 list_tools() 多了 namespaced name + server 来源，给 Step 17
        Harness / Step 20 Web UI 用。
        """
        out: list[MCPAgentTool] = []
        for cfg in self._configs:
            out.extend(self._tools.get(cfg.name, []))
        return out


__all__ = [
    "MCPServerState",
    "MCPRegistry",
]

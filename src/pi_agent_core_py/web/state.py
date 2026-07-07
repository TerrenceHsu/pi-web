"""WebAppState / TraceEventBuffer —— Step 20 Web App 状态层。

只负责**状态**，不接 FastAPI / 不做 HTTP。这层可以被测试（test_step_20_trace_viewer_state）
独立验证，无需 FastAPI。

设计：

- `TraceEventBuffer` —— deque(maxlen=N) 环形缓冲；只存 JSON-safe dict；超过
  max_size 自动丢弃最旧事件
- `WebAppState`     —— Pydantic 模型；持有 harness 引用 + event_buffer +
                       event_queue（SSE 用）+ running/last_error 标记

```text
AgentHarness.on_event_hooks
↓（web_event_hook 把 AgentEvent 序列化）
TraceEventBuffer.append(json_safe_dict)   ← /api/events 读
asyncio.Queue.put(json_safe_dict)         ← /api/stream SSE 读
```

**不保存**：

- live MCPClient / MCPTransport / MCPAgentTool 对象
- 完整 prompt raw / 巨型 tool schema
- 任何 secrets / env

只保存 JSON-safe dict——serialize 由 `serializers.to_json_safe` 保证。
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TraceEventBuffer:
    """环形事件缓冲——deque(maxlen=max_size)。

    - 只存 JSON-safe dict（调用方负责序列化）
    - 超过 max_size 自动丢最旧（deque maxlen 行为）
    - list() 返回浅拷贝（防止外部 mutate）
    - clear() 清空所有事件

    单 event loop 内 append/list/clear 不加锁——FastAPI / asyncio
    单 loop 安全；多 loop / 多线程场景需要自己加锁（Step 20 不做）。
    """

    def __init__(self, max_size: int = 1000) -> None:
        if max_size <= 0:
            raise ValueError(
                f"TraceEventBuffer.max_size must be > 0, got {max_size}"
            )
        self.max_size = max_size
        self._events: deque[dict[str, Any]] = deque(maxlen=max_size)

    def append(self, event: dict[str, Any]) -> None:
        """追加事件。

        假设 event 已经是 JSON-safe dict；本方法不做二次校验（性能考虑）。
        若 event 不是 dict，静默跳过（防御坏调用方，不抛错）。
        """
        if not isinstance(event, dict):
            return
        self._events.append(event)

    def list(self) -> list[dict[str, Any]]:
        """返回 list 浅拷贝（list 顺序保持，但内部 dict 仍是原引用）。

        若调用方要 mutate 单个 dict 字段又不影响 buffer，应自行 deepcopy。
        本方法只防止"调用方 append/sort 掉元素"污染 buffer——这是 deque 的
        典型使用模式。
        """
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)


# ============================================================================
# P0-4 Step 2: WebMCPServerConfig
# ============================================================================


class WebMCPServerConfig(BaseModel):
    """Web 层 MCP server 配置（P0-4 Step 2）。

    保存用户在 MCP Manager 中添加的 server 配置。**仅在服务端内存**，
    不持久化；server 重启后丢失。

    字段语义：
        name        server 唯一名；只允许 [A-Za-z0-9_-]（与 MCPServerConfig 一致）
        command     stdio 启动命令（如 "python" / "node" / "npx"）
        args        命令参数 list[str]
        env         完整环境变量 dict[str, str]——**仅在服务端内存**，
                    API response 永远只输出 env_keys
        enabled     用户期望启用状态。enabled=True 不一定代表 runtime attach 成功
                    （attach 失败时仍为 True，但 status / last_error 反映失败）
        last_error  最近一次 attach/test/refresh 错误描述；None 表示无错误
        tool_count  最近一次成功 attach 后看到的工具数（cache；非实时）

    序列化规则（`_serialize_mcp_server` in app.py）：
        - env 完整 dict **绝对不**进入 response
        - 只导出 env_keys: list[str]（sorted）
        - 不导出敏感字段
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False
    last_error: str | None = None
    tool_count: int = 0


class WebAppState(BaseModel):
    """Web App 单实例运行态。

    字段：
        harness         AgentHarness 引用（live object，不进 JSON）
        event_buffer    TraceEventBuffer（deque 包装）
        event_queue     asyncio.Queue——SSE 消费者从此 pull；每个 SSE client
                        应该有自己的队列副本，这里只存"广播源"队列；
                        create_app 内部会把 event 同时广播到所有 SSE client
        running         当前是否正在处理 prompt 请求（防止并发 prompt）
        last_error      最近一次请求的错误描述（None 表示无错误）
        session_store   SQLiteSessionStore 引用（P0-1）；可为 None（不启用多会话）
        current_session_id  当前激活 session 的 id；None 表示用 default
        mcp_server_configs   P0-4 用户添加的 MCP server 配置 dict（name → config）；
                             仅服务端内存，不持久化
        disabled_mcp_tools   P0-4 用户标记 disabled 的 MCP tool 全名集合
                             （mcp__{server}__{tool} 形式）；再次 enable 时会
                             重新应用过滤

    arbitrary_types_allowed=True：harness / event_buffer / event_queue /
    session_store 都不是 Pydantic 原生类型。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    harness: Any
    event_buffer: TraceEventBuffer = Field(default_factory=TraceEventBuffer)
    event_queue: asyncio.Queue[dict[str, Any]] = Field(default_factory=asyncio.Queue)
    running: bool = False
    last_error: str | None = None
    # P0-1: SQLiteSessionStore 引用（None 表示未启用多会话路径）
    session_store: Any = None
    current_session_id: str | None = None
    # P0-2: VirtualFileStore 引用（None 表示未启用文件上传路径）
    file_store: Any = None
    uploads_dir: Any = None
    # P0-4 Step 2: MCP server 配置（name → WebMCPServerConfig）
    mcp_server_configs: dict[str, WebMCPServerConfig] = Field(default_factory=dict)
    # P0-4 Step 2: disabled MCP tool 全名集合
    disabled_mcp_tools: set[str] = Field(default_factory=set)


__all__ = ["TraceEventBuffer", "WebAppState", "WebMCPServerConfig"]

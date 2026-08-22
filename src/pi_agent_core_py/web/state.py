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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceEventBuffer:
    """环形事件缓冲——deque(maxlen=max_size)。

    - 只存 JSON-safe dict（调用方负责序列化）
    - 超过 max_size 自动丢最旧（deque maxlen 行为）
    - list() 返回浅拷贝（防止外部 mutate）
    - clear() 清空所有事件
    - first_sequence / last_sequence 跟踪当前 buffer 内 sequence 范围
      （P1-B2：用于 GET /api/events after_sequence gap 检测）

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
        # P1-B2: sequence 跟踪——None 表示 buffer 空
        self.first_sequence: int | None = None
        self.last_sequence: int | None = None

    def append(self, event: dict[str, Any]) -> None:
        """追加事件。

        假设 event 已经是 JSON-safe dict；本方法不做二次校验（性能考虑）。
        若 event 不是 dict，静默跳过（防御坏调用方，不抛错）。

        P1-B2：first_sequence / last_sequence 跟随 buffer 实际头尾 sequence。
        deque maxlen 截断最旧时 first_sequence 自动更新到新的 head。
        """
        if not isinstance(event, dict):
            return
        self._events.append(event)
        # last_sequence = 刚 append 的 sequence
        seq = event.get("sequence")
        if isinstance(seq, int):
            self.last_sequence = seq
        # first_sequence = buffer 实际 head 的 sequence（截断后会变）
        if self._events:
            head = self._events[0]
            head_seq = head.get("sequence") if isinstance(head, dict) else None
            if isinstance(head_seq, int):
                self.first_sequence = head_seq

    def list(self) -> list[dict[str, Any]]:
        """返回 list 浅拷贝（list 顺序保持，但内部 dict 仍是原引用）。

        若调用方要 mutate 单个 dict 字段又不影响 buffer，应自行 deepcopy。
        本方法只防止"调用方 append/sort 掉元素"污染 buffer——这是 deque 的
        典型使用模式。
        """
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()
        self.first_sequence = None
        self.last_sequence = None

    def __len__(self) -> int:
        return len(self._events)


# ============================================================================
# P0-4 Step 2: WebMCPServerConfig
# ============================================================================


class WebMCPServerConfig(BaseModel):
    """Web 层 MCP server 配置（P0-4 Step 2 + P1-C3 持久化）。

    字段语义（P1-C3 修正）：
        name            server 唯一名
        command         stdio 启动命令
        args            命令参数 list[str]
        env             完整环境变量 dict[str, str]——**仅在服务端内存**，
                        API response 永远只输出 env_keys；**绝不写入 SQLite**
        enabled         desired_enabled（用户期望）——持久化字段；
                        enabled=True 不代表 attached=True
        attached        runtime 派生——当前进程是否成功连接；
                        attach 成功 True / 失败 False；不持久化
        last_error      最近一次 attach/test/refresh 错误描述；None 表示无错误
        tool_count      最近一次成功 attach 后看到的工具数
        builtin         是否为应用提供的内置 server
        deletable       前端与 API 是否允许删除
        settings        内置 server 的可编辑非敏感参数

    **兼容映射**（P1-C3）：API response 的 `enabled` = `desired_enabled`；
    新增 `attached` 字段表示运行时连接状态。现有前端 / E2E 的 `enabled` 语义不变。
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False  # desired_enabled（用户期望）
    attached: bool = False  # runtime 派生（attach 成功 True）
    last_error: str | None = None
    tool_count: int = 0
    # P1-C5: 结构化 restore 状态——避免前端解析 last_error 字符串
    # not_requested: desired_enabled=false，未请求 attach
    # attached: attach 成功
    # needs_env: env value 缺失（missing_env_keys 非空）
    # error: attach 失败 / timeout / 命令不存在等
    restore_status: str = "not_requested"
    missing_env_keys: list[str] = Field(default_factory=list)
    builtin: bool = False
    deletable: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


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

    P1-B1 异步架构字段：
        active_requests          request_id → WebRunRequest（运行中或刚结束）
        active_request_by_session  session_id → request_id（单 active per session）
        request_history          deque[WebRunRequest]（completed/aborted/error 后转入；
                                 maxlen=request_history_maxlen，默认 100）
        shutting_down            lifespan shutdown 阶段设 True，拒绝新 async prompt

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
    # Session WorkspaceStore 引用（None 表示未启用 Workspace）
    file_store: Any = None
    uploads_dir: Any = None
    # 启动时 durable operation reducer 的 secret-free 计数。
    durable_recovery_summary: dict[str, int] = Field(default_factory=dict)
    # P0-4 Step 2: MCP server 配置（name → WebMCPServerConfig）
    mcp_server_configs: dict[str, WebMCPServerConfig] = Field(default_factory=dict)
    # P0-4 Step 2: disabled MCP tool 全名集合
    disabled_mcp_tools: set[str] = Field(default_factory=set)
    # P1-B1: 异步 prompt request registry
    active_requests: dict[str, Any] = Field(default_factory=dict)
    active_request_by_session: dict[str, str] = Field(default_factory=dict)
    request_history: Any = Field(default_factory=lambda: deque(maxlen=100))
    request_history_maxlen: int = 100
    shutting_down: bool = False
    # P1-B2: 全局单调递增 event sequence + 当前 active request context
    # current_request_id / current_request_session_id 在 _run_prompt_background
    # set/clear（不用 contextvar——hook 在 Agent 内部 task 触发，跨 task 不可靠）
    next_event_sequence: int = 1
    current_request_id: str | None = None
    current_request_session_id: str | None = None
    # P1-C2: Extension 配置持久化 store + Skill mutation lock
    # extension_store 与 session_store 共享 connection（:memory: 模式必须）
    # skill_mutation_lock 保证 upload/enable/disable/delete/restore 原子性
    extension_store: Any = None
    model_capability_store: Any = None
    skill_mutation_lock: Any = None  # asyncio.Lock——在 create_app 内 init
    # P1-C3: MCP mutation lock——覆盖 add/enable/disable/delete server + tool
    mcp_mutation_lock: Any = None
    # P2-R1: Knowledge subsystem composition (None = disabled).
    # Service / Store / FileStore 同时存在或同时为 None。
    knowledge_service: Any = None
    knowledge_store: Any = None
    knowledge_file_store: Any = None
    # P2-R2-C2: Ingestion Worker Manager (None = disabled / [rag] extra missing).
    # Holds app-scoped singleton that drives PDF→Canonical Markdown pipeline.
    # Constructed in lifespan AFTER knowledge subsystem; started before yield;
    # stopped in lifespan finally BEFORE knowledge_store.close().
    ingestion_worker_manager: Any = None
    # P2-R3-D2: Indexing Worker Manager (None = disabled / [rag] extra missing).
    # Holds app-scoped singleton that drives normalizing → chunking →
    # indexing → ready background execution. Constructed in lifespan AFTER
    # Ingestion Worker Manager; started after Ingestion; stopped in lifespan
    # finally AFTER Ingestion Worker stop, BEFORE knowledge_store.close().
    indexing_worker_manager: Any = None
    # P2-R4-B2: Turn-scoped Evidence Registry for search_knowledge tool.
    # Lazily created by the tool's evidence_registry_getter closure.
    # Reset to None at the start of each prompt request (turn boundary).
    _evidence_registry: Any = None


# ============================================================================
# P1-B1: WebRunRequest —— 异步 prompt 请求 record
# ============================================================================

RequestStatus = Literal["queued", "running", "completed", "error", "aborted"]

# D2-5 + slash commands：区分普通 prompt、regenerate 与 checkpointer
RequestOperation = Literal["prompt", "regenerate", "checkpointer"]


@dataclass
class WebRunRequest:
    """异步 prompt 请求 record。

    生命周期：queued → running → (completed | error | aborted)。
    完成后从 active_requests 移除，放入 request_history。

    字段分两类：
        **JSON-safe**（serialize_request 输出）：
            id / session_id / status / created_at / started_at / ended_at /
            error / error_type / abort_reason / result_summary /
            event_start_sequence / event_end_sequence / operation /
            regeneration_id / target_message_id
        **仅内存**（不进 JSON）：
            task（asyncio.Task 引用——用于 abort / shutdown 收敛）
            payload（原始请求 dict——用于 debug，**绝不**进 JSON response；
                    不保存 secret 字段的副本，原 payload 中的 file_ids 引用
                    不构成 secret 泄露）

    安全约束（用户原指令 §10）：
        - error / error_type 仅 safe summary（safe_error 截断）
        - result_summary 不含 message 全文，只含计数 / stop_reason / applied_skills
        - 不保存 GLM API key / MCP env values / 完整 system prompt
    """

    id: str
    session_id: str | None
    status: RequestStatus = "queued"
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None
    error_type: str | None = None
    abort_reason: str | None = None
    result_summary: dict[str, Any] | None = None
    # P1-B2 会填充这两个字段（事件 sequence 范围）；P1-B1 占位
    event_start_sequence: int | None = None
    event_end_sequence: int | None = None
    # D2-5：operation 类型 + regenerate 关联（向后兼容——普通 prompt 默认值）
    operation: RequestOperation = "prompt"
    regeneration_id: str | None = None  # revision.id（regenerate 路径）
    target_message_id: str | None = None  # assistant_message_id（regenerate 路径）
    # 仅内存——不进 JSON
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    payload: dict[str, Any] | None = field(default=None, repr=False)


__all__ = [
    "TraceEventBuffer",
    "WebAppState",
    "WebMCPServerConfig",
    "WebRunRequest",
    "RequestStatus",
    "RequestOperation",
]

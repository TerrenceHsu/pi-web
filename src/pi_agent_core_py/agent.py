"""Agent 状态机 + Queue / Abort（Step 9）。

在 Step 8 的单 turn 状态机之上加入：
- **single active request**：活跃期间普通 prompt / continue 立即拒绝
- **control queues**：steering / follow-up 独立排队，支持 all / one-at-a-time
- **abort**：`abort(reason)` 通过 signal 协作中止当前 request
- **新事件**：`RequestQueuedEvent` / `RequestStartEvent` / `RequestEndEvent` / `AgentAbortEvent`
- **新状态**：`AgentStatus` 加 `"aborting"`

设计要点：
- `AgentRequest`：内部请求对象，含 `future: asyncio.Future[list[Message]]`
- 单一 `_run_queue_worker` task 串行处理 queue
- `prompt()` / `continue_()` 入队后 `await request.future`
- abort 用 `asyncio.Event` 作为 signal 传入 `run_event_loop`，loop 检测后生成
  `AssistantMessage(stop_reason="aborted")` 正常收敛
- Agent 自身异常 → 清空 queue，所有排队 future `set_exception`
- `wait_for_idle()` 等待 queue 全部排空
- `reset()` 在 running / aborting / queue 非空时仍抛 RuntimeError

Step 9 **不实现**：
- AgentHarness（Step 10）/ Turn Snapshot（Step 11）/ Session（Step 12）
- signal 会透传到 Tool、before hook 与 after hook
- 强制 cancel running task——只用 signal 协作
"""

from __future__ import annotations

import asyncio
import inspect
import time
import typing
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from .context import TransformContextFn
from .events import (
    AgentAbortEvent,
    AgentEndEvent,
    AgentEvent,
    AgentRequestType,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    RequestEndEvent,
    RequestEndStatus,
    RequestQueuedEvent,
    RequestStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
)
from .hooks import (
    AfterToolCallFn,
    BeforeToolCallFn,
)
from .loop import (
    BeforeModelCallFn,
    PrepareNextTurnFn,
    ShouldStopAfterTurnFn,
    run_event_loop,
)
from .messages import AssistantMessage, Message, TextContent, UserMessage
from .model_client import ModelClient
from .policy import (
    InMemoryToolPermissionAuditLog,
    ToolApprovalHandler,
    ToolPermissionPolicy,
)
from .tools import (
    AgentTool,
    ToolExecutionMode,
    ToolRegistry,
    _validate_tool_execution_mode,
)

# ============================================================================
# AgentStatus / AgentState
# ============================================================================


#: Agent 运行状态。
#: - "idle"      queue 空，无 running request
#: - "running"   正在执行一个 request
#: - "aborting"  已 abort，等当前 request 收敛
#: - "error"     Agent 自身异常（不是 LLM ErrorEvent / Tool is_error）
AgentStatus = Literal["idle", "running", "aborting", "error"]
QueueMode = Literal["all", "one-at-a-time"]
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class AgentModelState(BaseModel):
    """Secret-free identity of the model currently bound to the Agent."""

    id: str = "unknown"
    provider: str = "unknown"
    api: str = "unknown"


def _model_state_from_client(client: ModelClient) -> AgentModelState:
    """Project a client into stable, JSON-safe public model metadata."""
    return AgentModelState(
        id=getattr(client, "model", "") or "unknown",
        provider=getattr(client, "provider_id", "") or "unknown",
        api=getattr(client, "api_id", "") or "unknown",
    )


class AgentState(BaseModel):
    """Agent 持有的可变状态。"""

    # pi-agent compatible public configuration/runtime projection.
    model: AgentModelState = Field(default_factory=AgentModelState)
    thinking_level: ThinkingLevel = "off"
    is_streaming: bool = False
    streaming_message: Message | None = None
    pending_tool_calls: frozenset[str] = Field(default_factory=frozenset)
    error_message: str | None = None

    status: AgentStatus = "idle"
    messages: list[Message] = Field(default_factory=list)
    last_event: AgentEvent | None = None
    last_error: str | None = None
    turn_count: int = 0
    # Step 9 新增
    queue_size: int = 0
    current_request_id: str | None = None
    aborted_count: int = 0


# ============================================================================
# AgentRequest
# ============================================================================


@dataclass
class AgentRequest:
    """queue 里的一个待执行请求。

    `prompt()` / `continue_()` 入队时构造；worker 取出后调用 `_run_request`；
    调用方 `await request.future` 等到结果。
    """

    id: str
    type: AgentRequestType
    user_text: str | None
    future: asyncio.Future[Any] = field(repr=False)
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))


class _PendingMessageQueue:
    """FIFO queue with pi-agent's two drain modes."""

    def __init__(self, mode: QueueMode) -> None:
        self.mode = mode
        self._messages: list[Message] = []

    def enqueue(self, message: Message) -> None:
        self._messages.append(message)

    def drain(self) -> list[Message]:
        if self.mode == "all":
            drained = self._messages
            self._messages = []
            return drained
        if not self._messages:
            return []
        return [self._messages.pop(0)]

    def clear(self) -> None:
        self._messages.clear()

    def has_items(self) -> bool:
        return bool(self._messages)


# ============================================================================
# Subscriber
# ============================================================================


#: 事件订阅者。可以是 sync 或 async。
#: 签名 (event, state) -> object | Awaitable[object]
Subscriber = Callable[[AgentEvent, AgentState], object | Awaitable[object]]


# ============================================================================
# Agent 类
# ============================================================================


class Agent:
    """有状态的 Agent + queue / abort（Step 9）。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        client: ModelClient,
        tools: ToolRegistry | Iterable[AgentTool] | None = None,
        transform_context_fn: TransformContextFn | None = None,
        before_tool_call: BeforeToolCallFn | None = None,
        after_tool_call: AfterToolCallFn | None = None,
        tool_execution: ToolExecutionMode = "parallel",
        permission_policy: ToolPermissionPolicy | None = None,
        permission_audit_log: InMemoryToolPermissionAuditLog | None = None,
        tool_approval_handler: ToolApprovalHandler | None = None,
        should_stop_after_turn: ShouldStopAfterTurnFn | None = None,
        prepare_next_turn: PrepareNextTurnFn | None = None,
        before_model_call: BeforeModelCallFn | None = None,
        steering_mode: QueueMode = "one-at-a-time",
        follow_up_mode: QueueMode = "one-at-a-time",
        thinking_level: ThinkingLevel = "off",
        max_turns: int = 50,
    ):
        self.system_prompt = system_prompt
        self._client = client
        if isinstance(tools, ToolRegistry):
            self.tools: ToolRegistry = tools
        elif tools is None:
            self.tools = ToolRegistry()
        else:
            self.tools = ToolRegistry(list(tools))
        self.transform_context_fn = transform_context_fn
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.tool_execution = tool_execution
        # Step 18：工具权限策略 / 审计日志——透传给 run_event_loop
        # Agent 只做"持有 + 透传"，不做决策；Harness 可在运行期替换。
        self.permission_policy: ToolPermissionPolicy | None = permission_policy
        self.permission_audit_log: InMemoryToolPermissionAuditLog | None = permission_audit_log
        self.tool_approval_handler: ToolApprovalHandler | None = tool_approval_handler
        self.should_stop_after_turn = should_stop_after_turn
        self.prepare_next_turn = prepare_next_turn
        self.before_model_call = before_model_call
        self._steering_queue = _PendingMessageQueue(
            self._validate_queue_mode(steering_mode),
        )
        self._follow_up_queue = _PendingMessageQueue(
            self._validate_queue_mode(follow_up_mode),
        )
        # Bug-fix：max_turns 安全网——透传给 run_event_loop
        self.max_turns: int = max_turns

        self.state: AgentState = AgentState(
            model=_model_state_from_client(client),
            thinking_level=thinking_level,
        )
        self._subscribers: list[Subscriber] = []

        # Step 9 queue / worker 状态
        self._queue: asyncio.Queue[AgentRequest] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._abort_signal: asyncio.Event | None = None
        self._next_id: int = 0

        # _idle_event：set 表示 worker 已退出（queue 全空）
        self._idle_event: asyncio.Event = asyncio.Event()
        self._idle_event.set()

    @property
    def client(self) -> ModelClient:
        """Currently bound model client."""
        return self._client

    @client.setter
    def client(self, client: ModelClient) -> None:
        """Replace the client and keep the public model projection in sync."""
        self._client = client
        if hasattr(self, "state"):
            self.state.model = _model_state_from_client(client)

    # ----------------------------------------------------------------------
    # 订阅
    # ----------------------------------------------------------------------

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """注册事件订阅者；返回 unsubscribe 函数。"""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    # ----------------------------------------------------------------------
    # 入口：prompt / continue_ / steering / follow-up / abort
    # ----------------------------------------------------------------------

    async def prompt(self, user_text: str) -> list[Message]:
        """启动一个 prompt；活跃请求期间必须改用 steer / follow_up。"""
        self._ensure_request_admission("prompt")
        req = self._make_request(type_="prompt", user_text=user_text)
        await self._enqueue(req)
        return await typing.cast("asyncio.Future[list[Message]]", req.future)

    @property
    def tool_execution(self) -> ToolExecutionMode:
        return self._tool_execution

    @tool_execution.setter
    def tool_execution(self, mode: ToolExecutionMode) -> None:
        self._tool_execution = _validate_tool_execution_mode(mode)

    @property
    def steering_mode(self) -> QueueMode:
        return self._steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self._steering_queue.mode = self._validate_queue_mode(mode)

    @property
    def follow_up_mode(self) -> QueueMode:
        return self._follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self._follow_up_queue.mode = self._validate_queue_mode(mode)

    def steer(self, message: Message | str) -> None:
        """Queue a message for the next assistant turn boundary."""
        self._steering_queue.enqueue(self._normalize_control_message(message))

    def follow_up(self, message: Message | str) -> None:
        """Queue work for when the agent would otherwise stop."""
        self._follow_up_queue.enqueue(self._normalize_control_message(message))

    def clear_steering_queue(self) -> None:
        self._steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    async def continue_(self) -> list[Message]:
        """启动一个 continue 请求；await 直到完成。

        无 messages 或最后一条是 assistant 时抛 ValueError（队列前校验）。
        """
        # 活跃检查必须先于 transcript 校验：运行中的 state.messages 可能尚未
        # 收到 AgentEndEvent，此时应返回明确的控制面错误，而不是误报无历史。
        self._ensure_request_admission("continue_")
        if not self.state.messages:
            raise ValueError("Agent.continue_(): 当前无 messages，请先 prompt(...) 建立上下文")
        if isinstance(self.state.messages[-1], AssistantMessage):
            raise ValueError(
                "Agent.continue_(): 不能从 assistant 尾消息继续；请先追加 user/toolResult 消息"
            )
        req = self._make_request(type_="continue", user_text=None)
        await self._enqueue(req)
        return await typing.cast("asyncio.Future[list[Message]]", req.future)

    async def abort(self, reason: str | None = None) -> None:
        """中止当前 running request；idle / aborting / error 时无操作。

        实现：set `_abort_signal` → run_event_loop 在检查点生成 aborted assistant 收敛。
        当前 request 结束后 worker 自动处理 queue 中的下一个。

        注意：aborting 态下再调 abort 是 no-op——避免对同一个 request 重复计数。
        """
        # 只在 running 时触发；aborting / idle / error 都 no-op
        if self.state.status != "running":
            return
        self.state.status = "aborting"
        self.state.aborted_count += 1
        if self._abort_signal is not None:
            self._abort_signal.set()
        await self._handle_event(
            AgentAbortEvent(
                request_id=self.state.current_request_id,
                reason=reason,
            )
        )

    # ----------------------------------------------------------------------
    # reset / wait_for_idle
    # ----------------------------------------------------------------------

    def reset(self) -> None:
        """清空状态——running / aborting / queue 非空时仍抛 RuntimeError。

        Step 9 不做 auto-abort；调方需要先 `await abort()` + `await wait_for_idle()`。
        """
        if self.state.status in ("running", "aborting"):
            raise RuntimeError(f"Cannot reset while agent is {self.state.status}")
        if not self._queue.empty():
            raise RuntimeError("Cannot reset while queue is not empty")
        self.state.messages = []
        self.state.last_event = None
        self.state.last_error = None
        self.state.turn_count = 0
        self.state.queue_size = 0
        self.state.current_request_id = None
        self.state.aborted_count = 0
        self._clear_runtime_state()
        self.state.status = "idle"
        self.clear_all_queues()

    async def wait_for_idle(self) -> None:
        """等待 worker 把 queue 全部跑完。

        - 已 idle：立即返回
        - running / aborting：阻塞到 worker 退出
        - error：阻塞到 worker 退出（worker 在 error 时也会 set _idle_event）
        """
        await self._idle_event.wait()

    # ----------------------------------------------------------------------
    # 内部：构造 / 入队 / worker
    # ----------------------------------------------------------------------

    def _ensure_request_admission(self, operation: str) -> None:
        """Atomically reject a second ordinary request before it is enqueued."""
        worker_active = self._worker_task is not None and not self._worker_task.done()
        if (
            self.state.status in ("running", "aborting")
            or not self._queue.empty()
            or worker_active
        ):
            raise RuntimeError(
                f"Agent is already processing a request; cannot {operation}(). "
                "Use steer() or follow_up() to queue messages, or wait_for_idle()."
            )

    @staticmethod
    def _validate_queue_mode(mode: QueueMode) -> QueueMode:
        if mode not in ("all", "one-at-a-time"):
            raise ValueError("queue mode must be 'all' or 'one-at-a-time'")
        return mode

    @staticmethod
    def _normalize_control_message(message: Message | str) -> Message:
        if isinstance(message, str):
            return UserMessage(content=[TextContent(text=message)])
        return message

    def _make_request(self, *, type_: AgentRequestType, user_text: str | None) -> AgentRequest:
        self._next_id += 1
        req_id = f"req-{self._next_id}"
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        return AgentRequest(
            id=req_id,
            type=type_,
            user_text=user_text,
            future=future,
        )

    async def _enqueue(self, req: AgentRequest) -> None:
        # 先 clear _idle_event——必须在 put 之前，否则下面的 _handle_event await
        # subscriber 时，外部 wait_for_idle 可能误判为已 idle（queue 非空但 event 仍 set）
        self._idle_event.clear()
        await self._queue.put(req)
        # 入队后立即更新 state.queue_size（worker 还没取走）
        self.state.queue_size = self._queue.qsize()
        await self._handle_event(
            RequestQueuedEvent(
                request_id=req.id,
                request_type=req.type,
                queue_size=self._queue.qsize(),
            )
        )
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        """如果 worker 不存在或已退出，重建。"""
        if self._worker_task is None or self._worker_task.done():
            # 在创建 task 之前 clear _idle_event，避免 wait_for_idle 在 worker
            # 真正开始前误判为 idle
            self._idle_event.clear()
            self._worker_task = asyncio.ensure_future(self._run_queue_worker())

    async def _run_queue_worker(self) -> None:
        """串行处理 queue 中所有 request，直到空。"""
        try:
            while not self._queue.empty():
                req = await self._queue.get()
                try:
                    await self._process_request(req)
                except Exception as e:
                    # request 自身异常 → 进入 error 态 + 清空 queue
                    self.state.status = "error"
                    self.state.last_error = f"{type(e).__name__}: {e}"
                    self.state.error_message = str(e)
                    self._clear_runtime_state(preserve_error=True)
                    await self._drain_queue_on_error(e)
                    return
                finally:
                    self._queue.task_done()
            # 全部处理完
            self.state.status = "idle"
            self.state.queue_size = 0
        finally:
            self._idle_event.set()

    async def _process_request(self, req: AgentRequest) -> None:
        """处理单个 request：emit start → run → emit end；异常则 re-raise。"""
        # 进入 running 态（即便上一轮是 aborting / error，新一轮覆盖）
        self.state.current_request_id = req.id
        self.state.queue_size = self._queue.qsize()
        self.state.status = "running"
        self.state.model = _model_state_from_client(self.client)
        self.state.is_streaming = True
        self.state.streaming_message = None
        self.state.pending_tool_calls = frozenset()
        self.state.error_message = None

        await self._handle_event(
            RequestStartEvent(
                request_id=req.id,
                request_type=req.type,
            )
        )

        # 为本轮 request 建独立 signal
        self._abort_signal = asyncio.Event()

        self._current_task = asyncio.ensure_future(self._run_request(req, self._abort_signal))

        end_status: RequestEndStatus = "completed"
        exc: Exception | None = None
        try:
            await self._current_task
            if self._abort_signal.is_set():
                end_status = "aborted"
        except Exception as e:
            end_status = "error"
            exc = e

        # 设置 future
        if not req.future.done():
            if exc is not None:
                req.future.set_exception(exc)
            else:
                req.future.set_result(self.state.messages)

        # 清理 request 级状态
        self.state.current_request_id = None
        self._current_task = None
        self._abort_signal = None

        await self._handle_event(
            RequestEndEvent(
                request_id=req.id,
                request_type=req.type,
                status=end_status,
            )
        )
        self._clear_runtime_state(preserve_error=True)

        if exc is not None:
            raise exc

    async def _run_request(self, req: AgentRequest, signal: asyncio.Event) -> None:
        """跑一个 request：把 run_event_loop 的事件流转发给 _handle_event。"""
        if req.type == "prompt":
            user_text = req.user_text
        else:
            user_text = None

        async for ev in run_event_loop(
            system_prompt=self.system_prompt,
            user_text=user_text,
            initial_messages=list(self.state.messages),
            client=self.client,
            tools=self.tools,
            transform_context_fn=self.transform_context_fn,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            tool_execution=self.tool_execution,
            signal=signal,
            permission_policy=self.permission_policy,
            permission_audit_log=self.permission_audit_log,
            tool_approval_handler=self.tool_approval_handler,
            should_stop_after_turn=self.should_stop_after_turn,
            prepare_next_turn=self.prepare_next_turn,
            before_model_call=self.before_model_call,
            get_steering_messages=self._steering_queue.drain,
            get_follow_up_messages=self._follow_up_queue.drain,
            max_turns=self.max_turns,
        ):
            await self._handle_event(ev)

    async def _drain_queue_on_error(self, exc: Exception) -> None:
        """Agent 自身异常后，把剩余 queue 全部 fail 掉。"""
        while not self._queue.empty():
            req = await self._queue.get()
            if not req.future.done():
                req.future.set_exception(exc)
            await self._handle_event(
                RequestEndEvent(
                    request_id=req.id,
                    request_type=req.type,
                    status="error",
                )
            )
            self._queue.task_done()
        self.state.queue_size = 0

    # ----------------------------------------------------------------------
    # 内部：事件处理 + 派发
    # ----------------------------------------------------------------------

    async def _handle_event(self, event: AgentEvent) -> None:
        """更新 state，再派发给订阅者。"""
        self.state.last_event = event

        if isinstance(event, AgentStartEvent):
            self.state.is_streaming = True
        elif isinstance(event, MessageStartEvent):
            self.state.streaming_message = event.message
        elif isinstance(event, MessageUpdateEvent):
            self.state.streaming_message = event.message
        elif isinstance(event, MessageEndEvent):
            self.state.streaming_message = None
            self.state.messages.append(event.message)
        elif isinstance(event, ToolExecutionStartEvent):
            self.state.pending_tool_calls = frozenset(
                (*self.state.pending_tool_calls, event.tool_call.id)
            )
        elif isinstance(event, ToolExecutionEndEvent):
            pending = set(self.state.pending_tool_calls)
            pending.discard(event.tool_call.id)
            self.state.pending_tool_calls = frozenset(pending)
        # AgentEndEvent is the authoritative final transcript. This also
        # replaces an assistant message whose terminal fields changed at
        # turn_end (for example max_turns).
        elif isinstance(event, AgentEndEvent):
            self.state.streaming_message = None
            self.state.messages = list(event.messages)
        elif isinstance(event, TurnEndEvent):
            self.state.turn_count += 1
            if event.message.error_message is not None:
                self.state.error_message = event.message.error_message
        elif isinstance(event, RequestQueuedEvent):
            self.state.queue_size = event.queue_size
        elif isinstance(event, RequestStartEvent):
            self.state.current_request_id = event.request_id
        elif isinstance(event, RequestEndEvent):
            self.state.current_request_id = None
        elif isinstance(event, AgentAbortEvent):
            # aborted_count 在 abort() 入口已 +1；这里避免重复计数
            pass

        await self._emit(event)

    def _clear_runtime_state(self, *, preserve_error: bool = False) -> None:
        """Clear request-owned public fields while preserving configuration."""
        self.state.is_streaming = False
        self.state.streaming_message = None
        self.state.pending_tool_calls = frozenset()
        if not preserve_error:
            self.state.error_message = None

    async def _emit(self, event: AgentEvent) -> None:
        """派发事件给所有订阅者；单个抛异常不让主 loop 崩。"""
        for sub in list(self._subscribers):
            try:
                result = sub(event, self.state)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                self.state.last_error = f"subscriber error: {type(e).__name__}: {e}"


__all__ = [
    "AgentStatus",
    "QueueMode",
    "ThinkingLevel",
    "AgentModelState",
    "AgentState",
    "Agent",
    "AgentRequest",
    "Subscriber",
]

"""FastAPI app factory —— Step 20 Web App。

`create_app(harness)` 创建一个 FastAPI 实例：

- 注册 on_event hook，把 AgentEvent 序列化后写入 TraceEventBuffer + 广播给所有 SSE client
- 提供 REST API 查询 harness / agent / session / MCP / skills / policy audit 状态
- 提供 SSE endpoint `/api/stream` 实时推送事件
- 托管 Vue build 产物（`web/static/index.html`）；未 build 时返回 fallback HTML

```text
Browser (Vue)                  FastAPI                       AgentHarness
   │  fetch /api/...              │                              │
   ├─────────────────────────────>│                              │
   │                              │  harness.run_prompt(...)     │
   │                              ├─────────────────────────────>│
   │                              │                              │
   │  EventSource /api/stream     │  on_event hook               │
   │⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯>│<⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
   │   data: {...}                │  broadcast event             │
   │<⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯│                              │
```

**安全**：仅用于本地调试。不实现认证 / 多用户 / 公网部署。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

from ..harness import AgentHarness
from ..skills import SkillSelection
from .serializers import (
    serialize_event,
    serialize_mcp_server_state,
    serialize_message,
    serialize_policy_audit_record,
    serialize_session,
    serialize_skill,
    serialize_snapshot_full,
    serialize_snapshot_summary,
    to_json_safe,
)
from .state import WebAppState, WebMCPServerConfig, WebRunRequest

# ============================================================================
# 常量
# ============================================================================


_STATIC_DIR: Path = Path(__file__).parent / "static"

#: fallback HTML——Vue 未 build 时返回，告诉用户怎么 build。
#: 不能 import 任何 frontend 产物；保持单文件可读。
_FALLBACK_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>pi-agent-core-py · Trace Viewer</title>
  <style>
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
           max-width: 760px; margin: 60px auto; padding: 0 20px;
           color: #222; background: #fafafa; }
    pre { background: #eee; padding: 12px; border-radius: 6px; overflow-x: auto; }
    h1 { font-size: 1.4rem; }
  </style>
</head>
<body>
  <h1>Frontend has not been built.</h1>
  <p>Run:</p>
  <pre>cd src/pi_agent_core_py/web/frontend
npm install
npm run build</pre>
  <p>Then refresh this page.</p>
  <p>API endpoints (e.g. <code>/api/state</code>) are still usable.</p>
</body>
</html>
"""

#: heartbeat 间隔（秒）。SSE 在 idle 时定期发空注释行，保持连接不被代理超时关闭。
_SSE_HEARTBEAT_SECONDS: float = 15.0


# ============================================================================
# P1-B1: 异步 prompt 公共数据类型（exceptions / validated / result）
# ============================================================================


class PromptValidationError(Exception):
    """4xx 校验错误——HTTP 层转 JSONResponse；async 层不创建 request。

    保留 status_code + detail + extra（如 missing_skill_names）以让 HTTP 层
    重建与原 POST /api/prompt 完全一致的错误响应 schema。
    """

    def __init__(self, status_code: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra: dict[str, Any] = extra


class PromptRuntimeError(Exception):
    """5xx / 409 harness 执行错误——同步路径走 JSONResponse，异步路径转 status=error。"""

    def __init__(self, status_code: int, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type


@dataclass
class _PromptValidated:
    """_validate_prompt_payload 成功后的产物——传给 _run_prompt_core。

    所有"已校验好"的字段集中在这里，避免 _run_prompt_core 重复解析 / 重复校验，
    也避免 async runner 在 task 内重复 await 校验（task 抛 PromptValidationError
    会变成 status=error 而非 4xx，违反用户原指令 §5.3）。
    """

    text: str
    skill_selection: SkillSelection | None
    session_id: str | None
    store: Any
    original_messages: list[Any] | None
    attached_blocks: list[Any]
    attached_summary: list[dict[str, Any]]


@dataclass
class PromptExecutionResult:
    """_run_prompt_core 成功后的产物。"""

    messages: list[Any]
    serialized_messages: list[dict[str, Any]]
    session_id: str | None
    attachment_meta: dict[str, Any]
    applied_skill_names: list[str]


# ============================================================================
# create_app
# ============================================================================


def create_app(
    harness: AgentHarness,
    *,
    event_buffer_max_size: int = 1000,
    allow_prompt_preview: bool = False,
    db_path: str | Path | None = None,
    uploads_dir: str | Path | None = None,
    max_file_size: int = 25 * 1024 * 1024,
    max_session_upload_size: int = 100 * 1024 * 1024,
    request_history_maxlen: int = 100,
    shutdown_grace_s: float = 5.0,
) -> FastAPI:
    """构造一个 FastAPI 实例。

    副作用：会向 `harness.on_event_hooks` 追加一个 web_event_hook，把
    AgentEvent 序列化后写入 WebAppState.event_buffer + 广播到所有 SSE / WS client。

    `harness` 应该是已经初始化好（attach_skills / attach_mcp_servers / etc.）
    的实例；create_app 不会重新配置 harness。

    多次 create_app 同一个 harness 是安全的：每次创建会在 `app.state.web`
    里登记 hook；调用 `dispose_app(app)` 或 FastAPI shutdown event 可清理
    自己注册的 hook，避免累积广播。

    参数：
        event_buffer_max_size: TraceEventBuffer 容量，默认 1000；超出后丢最旧
        allow_prompt_preview:  是否允许 `?include_prompt=true` 暴露 Skill 的
            完整 prompt 模板。默认 False（生产保守）；本地调试可设 True。
            即使 True，非 localhost 请求仍返回 403。
        db_path: P0-1 SQLiteSessionStore 的 sqlite 文件路径。None（默认）= 用
            `:memory:` 内存库（每个 app 实例独立，进程退出即丢）；测试场景常用。
            生产场景传文件路径以持久化。
        uploads_dir: P0-2 VirtualFileStore 的根目录。None（默认）= 不启用文件
            上传路径；所有 `/api/.../files` endpoint 返回 503。生产场景传目录路径。
        max_file_size: 单文件大小上限，默认 25 MB
        max_session_upload_size: 单 session 总上传上限，默认 100 MB
    """
    # 用 closure 持有 hook / clients——lifespan 退出时清理
    container: dict[str, Any] = {
        "hook": None,
        "sse_clients": set(),
        "ws_clients": set(),
    }

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # startup：初始化 SQLiteSessionStore + 默认 session
        from ..session_sqlite import SQLiteSessionStore

        store_path = str(db_path) if db_path is not None else ":memory:"
        session_store = SQLiteSessionStore(store_path)
        await session_store.init()
        # 自动创建 / 复用 default session，作为 current_session_id
        try:
            default_session = await session_store.ensure_default_session()
            state.session_store = session_store
            state.current_session_id = default_session.id
        except Exception:
            # 初始化失败不应阻塞 app 启动——session_store 仍可用 None 路径
            state.session_store = session_store
            state.current_session_id = None

        # P0-2：初始化 VirtualFileStore（uploads_dir=None 时不启用）
        if uploads_dir is not None:
            from .files import VirtualFileStore

            file_store = VirtualFileStore(
                uploads_dir,
                max_file_size=max_file_size,
                max_session_size=max_session_upload_size,
            )
            try:
                await file_store.init()
                state.file_store = file_store
                state.uploads_dir = Path(uploads_dir)
            except Exception:
                # 文件存储不可用不阻塞 app 启动；endpoint 走 503
                state.file_store = None
                state.uploads_dir = None
        else:
            state.file_store = None
            state.uploads_dir = None

        # P0-3：file_store 可用 → 注册 list_files / view_file 工具到 agent.tools
        # session_id_getter 在每次工具执行时返回当前 state.current_session_id，
        # 工具用 VirtualFileStore.get_for_session 做 session 隔离。
        if state.file_store is not None:
            from ..tools.list_files import create_list_files_tool
            from ..tools.view_file import create_view_file_tool

            def _session_id_getter() -> str | None:
                return state.current_session_id

            list_tool = create_list_files_tool(
                file_store=state.file_store,
                session_id_getter=_session_id_getter,
            )
            view_tool = create_view_file_tool(
                file_store=state.file_store,
                session_id_getter=_session_id_getter,
            )
            tools_registry = harness.agent.tools
            if not tools_registry.has("list_files"):
                tools_registry.register(list_tool)
            if not tools_registry.has("view_file"):
                tools_registry.register(view_tool)

        yield

        # ====================================================================
        # P1-B1: shutdown 收敛——先收敛 active request，再走原清理流程
        # ====================================================================
        # 1. 拒绝新 async prompt（POST /api/prompt/async 看到 shutting_down=True 返回 503）
        state.shutting_down = True

        # 2. 收集所有 active request 的 task——abort queued（cancel）+ abort running（harness.abort）
        #    用 list 快照——_abort_request_internal 会修改 state.active_requests
        active_reqs = list(state.active_requests.values())
        active_tasks: list[asyncio.Task] = []
        for req in active_reqs:
            try:
                await _abort_request_internal(req, "server_shutdown")
            except Exception:
                pass
            if req.task is not None and not req.task.done():
                active_tasks.append(req.task)

        # 3. 等 grace timeout——让 runner 通过正常路径 finalize（保留事件广播 + 状态写入）
        if active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=shutdown_grace_s,
                )
            except TimeoutError:
                # grace 超时——强制 cancel 剩余 task
                for t in active_tasks:
                    if not t.done():
                        t.cancel()
                # 再等一次让 cancel 生效
                await asyncio.gather(*active_tasks, return_exceptions=True)

        # ====================================================================
        # 原清理流程
        # ====================================================================
        # shutdown：精确移除自己注册的 hook，避免累积
        hook = container.get("hook")
        if hook is not None:
            harness.remove_on_event_hook(hook)
            container["hook"] = None
        # 通知 SSE / WS client 关闭（不抛错）
        # container 里两个都是 set——用 set 联合（list | list 不合法）
        all_clients = set(container["sse_clients"])
        all_clients.update(container["ws_clients"])
        for q in all_clients:
            try:
                q.put_nowait({"type": "shutdown"})
            except asyncio.QueueFull:
                pass
        # 关闭 SQLiteSessionStore
        if state.session_store is not None:
            try:
                await state.session_store.close()
            except Exception:
                pass
        # VirtualFileStore 不需要 close（纯文件 IO），保留目录给后续进程用

    app = FastAPI(
        title="pi-agent-core-py · Trace Viewer",
        description=(
            "Local-only development UI for inspecting Agent runtime state. "
            "DO NOT expose publicly."
        ),
        version="0.0.21",
        lifespan=_lifespan,
    )

    state = WebAppState(harness=harness)
    # 用入参覆盖默认 maxlen
    state.event_buffer = type(state.event_buffer)(max_size=event_buffer_max_size)
    # P1-B1: request_history deque 的 maxlen 也用入参覆盖
    state.request_history = deque(maxlen=request_history_maxlen)
    state.request_history_maxlen = request_history_maxlen
    app.state.web = state
    app.state.allow_prompt_preview = allow_prompt_preview
    app.state.event_buffer_max_size = event_buffer_max_size

    # SSE 客户端队列集合——每个 SSE 连接独立 asyncio.Queue；
    # on_event hook 把 event 广播（put_nowait）到所有客户端队列
    sse_clients: set[asyncio.Queue[dict[str, Any]]] = container["sse_clients"]
    # WebSocket 客户端队列集合——同 SSE，独立 queue 池
    ws_clients: set[asyncio.Queue[dict[str, Any]]] = container["ws_clients"]

    async def _web_event_hook(event: Any, _ctx: Any) -> None:
        """on_event hook：序列化 event → 包装 WebEventEnvelope → 写入 buffer + 广播。

        P1-B2：所有 WS / SSE / GET /api/events 客户端看到的是同一个 envelope
        （event_id / sequence 在本入口一次性生成，不为不同客户端重复生成）。

        envelope schema:
            event_id    str        "evt_<uuid4 hex>"
            request_id  str|None   state.current_request_id（_run_prompt_background set）
            session_id  str|None   state.current_request_session_id
            sequence    int        全局单调递增（state.next_event_sequence）
            type        str        event 类型名（冗余字段，方便客户端快速判断）
            timestamp   str        ISO8601 UTC
            payload     dict       serialize_event(event) + _received_at_ms

        任何异常都被吞掉——Hook 失败不应影响 Agent 主流程（_handle_agent_event
        本身有 try/except 兜底，这里再加一层防御）。
        """
        try:
            payload = serialize_event(event)
            if not isinstance(payload, dict):
                # 防御坏 event——不可能发生但兜底
                payload = {}
            # 加上 server-side 接收时间，便于 UI 排序（保留旧字段向后兼容）
            payload.setdefault("_received_at_ms", int(time.time() * 1000))

            # 分配 sequence + 生成 envelope（在本入口一次性完成）
            sequence = state.next_event_sequence
            state.next_event_sequence = sequence + 1

            envelope = {
                "event_id": f"evt_{uuid4().hex[:16]}",
                "request_id": state.current_request_id,
                "session_id": state.current_request_session_id,
                "sequence": sequence,
                "type": payload.get("type") or type(event).__name__,
                "timestamp": _now_utc().isoformat(),
                "payload": payload,
            }

            state.event_buffer.append(envelope)
            # 广播到所有 SSE / WS client——慢客户端 put_nowait 抛 QueueFull 时
            # 丢弃该 event（不阻塞其它 client / 不阻塞主 loop）
            for q in list(sse_clients) + list(ws_clients):
                try:
                    q.put_nowait(envelope)
                except asyncio.QueueFull:
                    continue
        except Exception:
            return

    # 把 hook 挂到 harness，并登记到 container / app.state，便于 dispose 精确 remove
    harness.add_on_event_hook(_web_event_hook)
    container["hook"] = _web_event_hook
    app.state.web_event_hook = _web_event_hook

    # ========================================================================
    # Helpers
    # ========================================================================

    def _agent_status() -> str:
        try:
            return harness.agent.state.status
        except Exception:
            return "unknown"

    def _ensure_idle() -> None:
        """检查 harness 和 agent 都处于 idle；否则抛 409。

        用于 POST /api/prompt / /api/reset 等"独占式"操作。

        多重检查避免外部直接 await harness.run_prompt(...) 时 Web 漏判：
        1. state.running（Web 自己的 POST 路径）
        2. harness.context.phase（外部直接调 harness.run_prompt 时由 harness 自己 set）
        3. agent.state.status（Agent 内核 queue 状态）
        """
        if state.running:
            raise HTTPException(
                status_code=409,
                detail="harness is already running a request",
            )
        try:
            phase = harness.context.phase
        except Exception:
            phase = "unknown"
        if phase != "idle":
            raise HTTPException(
                status_code=409,
                detail=f"harness phase is {phase!r}",
            )
        try:
            agent_status: str = harness.agent.state.status
        except Exception:
            agent_status = "unknown"
        if agent_status in ("running", "aborting"):
            raise HTTPException(
                status_code=409,
                detail=f"agent is {agent_status!r}",
            )

    # ========================================================================
    # P1-B1: Prompt 公共执行逻辑（同步 /api/prompt + 异步 /api/prompt/async 共享）
    # ========================================================================

    def _merge_skill_names(
        skill_sel_raw: dict[str, Any] | None,
        skill_names_raw: Any,
    ) -> tuple[list[str] | None, list[str], list[str]]:
        """合并顶层 skill_names + skill_selection.names（去重保序）+ 语法校验。

        Returns (merged_names, sel_names, top_names)。
        merged_names=None 表示没有 skill；其余两者为原始 list（空 list 也算）。
        """
        sel_names = list((skill_sel_raw or {}).get("names") or [])
        top_names = list(skill_names_raw or [])
        if sel_names or top_names:
            seen: set[str] = set()
            merged: list[str] = []
            for n in [*sel_names, *top_names]:
                if n and n not in seen:
                    seen.add(n)
                    merged.append(n)
            return merged or None, sel_names, top_names
        return None, sel_names, top_names

    def _build_skill_selection(
        skill_sel_raw: dict[str, Any] | None,
        merged_names: list[str] | None,
    ) -> SkillSelection | None:
        if not (skill_sel_raw or merged_names):
            return None
        return SkillSelection(
            names=merged_names,
            tags=(skill_sel_raw or {}).get("tags"),
            values=(skill_sel_raw or {}).get("values") or {},
        )

    async def _resolve_file_blocks(
        session_id: str | None,
        file_ids_raw: list[Any],
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """校验 file_ids + 构造 FileBlock。抛 PromptValidationError。"""
        if not file_ids_raw:
            return [], []
        if state.file_store is None:
            raise PromptValidationError(
                503, "file store not initialized; cannot accept file_ids"
            )
        if session_id is None:
            raise PromptValidationError(
                400, "session_id required when file_ids present"
            )

        from ..messages import FileBlock
        from ..tools.view_file import _classify_format
        from .files import (
            FileAccessDeniedError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
        )

        attached_blocks: list[Any] = []
        attached_summary: list[dict[str, Any]] = []
        for fid in file_ids_raw:
            if not isinstance(fid, str) or not fid:
                raise PromptValidationError(
                    400,
                    f"file_ids entries must be non-empty strings (got {fid!r})",
                )
            try:
                ref = await state.file_store.get_for_session(session_id, fid)
            except VirtualFileNotFoundError as e:
                raise PromptValidationError(404, str(e)) from None
            except FileAccessDeniedError as e:
                raise PromptValidationError(403, str(e)) from None
            except UnsafeFilenameError as e:
                raise PromptValidationError(400, str(e)) from None

            fmt = _classify_format(ref.name, ref.mime)
            block = FileBlock(
                file_id=ref.id,
                name=ref.name,
                mime=ref.mime,
                size=ref.size,
                sha256=ref.sha256,
                format=fmt,
            )
            attached_blocks.append(block)
            attached_summary.append(
                {
                    "id": ref.id,
                    "name": ref.name,
                    "mime": ref.mime,
                    "size": ref.size,
                    "sha256": ref.sha256,
                    "format": fmt,
                }
            )
        return attached_blocks, attached_summary

    def _build_attachment_meta(
        attached_summary: list[dict[str, Any]],
    ) -> dict[str, Any]:
        supported_count = sum(
            1
            for s in attached_summary
            if s["format"]
            not in ("image_unsupported", "binary", "unsupported", "pdf")
        )
        unsupported_count = len(attached_summary) - supported_count
        return {
            "attached_file_ids": [s["id"] for s in attached_summary],
            "attached_file_names": [s["name"] for s in attached_summary],
            "attached_file_count": len(attached_summary),
            "attached_supported_file_count": supported_count,
            "attached_unsupported_file_count": unsupported_count,
        }

    async def _validate_prompt_payload(
        payload: dict[str, Any],
    ) -> _PromptValidated:
        """所有乐观校验——成功返回 _PromptValidated，失败抛 PromptValidationError。

        异常 → HTTP 层 catch 转 JSONResponse（4xx）；async 层 catch 后不创建 request。
        """
        _ensure_idle()  # HTTPException(409)——HTTP 层 FastAPI 自动处理；async 层 catch

        text = (payload or {}).get("text") or ""
        if not text.strip():
            raise PromptValidationError(400, "text is required")

        skill_sel_raw = (payload or {}).get("skill_selection") or {}
        skill_names_raw = (payload or {}).get("skill_names")
        if skill_names_raw is not None:
            if not isinstance(skill_names_raw, list):
                raise PromptValidationError(
                    400, "skill_names must be a list of strings"
                )
            bad = [
                n
                for n in skill_names_raw
                if not isinstance(n, str) or not n
            ]
            if bad:
                raise PromptValidationError(
                    400,
                    f"skill_names entries must be non-empty strings (got {bad[0]!r})",
                )

        merged_names, _sel_names, _top_names = _merge_skill_names(
            skill_sel_raw, skill_names_raw
        )
        skill_selection = _build_skill_selection(skill_sel_raw, merged_names)

        if merged_names and harness.skill_registry is not None:
            missing = [
                n for n in merged_names if not harness.skill_registry.has(n)
            ]
            if missing:
                raise PromptValidationError(
                    400,
                    f"Unknown skill: {missing[0]!r}",
                    missing_skill_names=missing,
                )

        session_id = (payload or {}).get("session_id") or state.current_session_id
        store = state.session_store

        original_messages: list[Any] | None = None
        if store is not None and session_id is not None:
            from ..session_sqlite import SessionNotFoundError

            try:
                history = await store.list_messages(session_id)
            except SessionNotFoundError:
                raise PromptValidationError(
                    404, f"session {session_id!r} not found"
                ) from None
            original_messages = list(harness.agent.state.messages)
            harness.agent.state.messages = list(history)

        file_ids_raw = (payload or {}).get("file_ids") or []
        if not isinstance(file_ids_raw, list):
            raise PromptValidationError(
                400, "file_ids must be a list of strings"
            )
        attached_blocks, attached_summary = await _resolve_file_blocks(
            session_id, list(file_ids_raw)
        )

        return _PromptValidated(
            text=text,
            skill_selection=skill_selection,
            session_id=session_id,
            store=store,
            original_messages=original_messages,
            attached_blocks=attached_blocks,
            attached_summary=attached_summary,
        )

    async def _run_prompt_core(
        validated: _PromptValidated,
    ) -> PromptExecutionResult:
        """执行 prompt——假定已校验完毕。抛 PromptRuntimeError 表示 harness 失败。"""
        state.running = True
        state.last_error = None
        try:
            if validated.attached_blocks:
                from ..messages import TextContent, UserMessage

                user_msg = UserMessage(
                    content=[TextContent(text=validated.text), *validated.attached_blocks]
                )
                harness.agent.state.messages.append(user_msg)
                messages = await harness.run_continue(
                    skill_selection=validated.skill_selection
                )
            else:
                messages = await harness.run_prompt(
                    validated.text, skill_selection=validated.skill_selection
                )
        except RuntimeError as e:
            msg = str(e)
            state.last_error = f"{type(e).__name__}: {msg}"
            status = 409 if "already running" in msg.lower() else 500
            raise PromptRuntimeError(
                status, state.last_error, type(e).__name__
            ) from None
        except Exception as e:
            state.last_error = f"{type(e).__name__}: {e}"
            raise PromptRuntimeError(500, state.last_error, type(e).__name__) from None
        finally:
            state.running = False

        if validated.store is not None and validated.session_id is not None:
            try:
                await validated.store.replace_messages(
                    validated.session_id, list(messages)
                )
                if harness.last_snapshot is not None:
                    await validated.store.append_snapshot(
                        validated.session_id, harness.last_snapshot
                    )
            except Exception as e:
                state.last_error = f"persist: {type(e).__name__}: {e}"
            finally:
                if validated.original_messages is not None:
                    harness.agent.state.messages = validated.original_messages

        attachment_meta = _build_attachment_meta(validated.attached_summary)
        applied_skill_names: list[str] = list(
            validated.skill_selection.names
        ) if (
            validated.skill_selection is not None
            and validated.skill_selection.names
        ) else []

        return PromptExecutionResult(
            messages=messages,
            serialized_messages=[serialize_message(m) for m in messages],
            session_id=validated.session_id,
            attachment_meta=attachment_meta,
            applied_skill_names=applied_skill_names,
        )

    def _serialize_prompt_validation_error(
        e: PromptValidationError,
    ) -> JSONResponse:
        body: dict[str, Any] = {"detail": e.detail}
        body.update(e.extra)
        return JSONResponse(status_code=e.status_code, content=body)

    def _serialize_prompt_runtime_error(
        e: PromptRuntimeError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=e.status_code,
            content={
                "ok": False,
                "error": e.message,
                "error_type": e.error_type,
            },
        )

    # ========================================================================
    # P1-B1: 异步 prompt runner + request 序列化 + abort helper
    # ========================================================================

    def _safe_error(e: BaseException) -> str:
        """安全错误摘要——截断到 type + message，不输出 traceback / repr。"""
        return f"{type(e).__name__}: {e}"[:500]

    def _now_utc() -> datetime:
        return datetime.now(UTC)

    def _serialize_request(req: WebRunRequest) -> dict[str, Any]:
        """JSON-safe WebRunRequest 视图。task / payload 不进 JSON。"""
        return {
            "request_id": req.id,
            "session_id": req.session_id,
            "status": req.status,
            "created_at": req.created_at.isoformat() if req.created_at else None,
            "started_at": req.started_at.isoformat() if req.started_at else None,
            "ended_at": req.ended_at.isoformat() if req.ended_at else None,
            "error": req.error,
            "error_type": req.error_type,
            "abort_reason": req.abort_reason,
            "result_summary": req.result_summary,
            "event_start_sequence": req.event_start_sequence,
            "event_end_sequence": req.event_end_sequence,
        }

    def _find_request(request_id: str) -> WebRunRequest | None:
        """从 active + history 找 request record。"""
        req = state.active_requests.get(request_id)
        if req is not None:
            return req
        for r in state.request_history:
            if r.id == request_id:
                return r
        return None

    def _remove_from_active(req: WebRunRequest) -> None:
        """从 active_requests / active_request_by_session 移除；保留 history append 给调用方做。"""
        state.active_requests.pop(req.id, None)
        if (
            req.session_id
            and state.active_request_by_session.get(req.session_id) == req.id
        ):
            state.active_request_by_session.pop(req.session_id, None)

    async def _run_prompt_background(
        web_request: WebRunRequest,
        validated: _PromptValidated,
    ) -> None:
        """后台 task 入口——_run_prompt_core 包一层 + 状态流转 + 异常收敛。

        关键不变量：
        - 任何异常都不让 task 成为 "Task exception was never retrieved"
          （PromptRuntimeError / Exception 都在 except 里消化）
        - asyncio.CancelledError 必须重抛（asyncio 要求）
        - finally 移除 active 并 append history——保证 shutdown 后 history 可查
        - abort 路径：web_request.abort_reason 由 abort endpoint 设置；
          runner 在 success 路径检查此 flag → status=aborted

        P1-B2：set/clear state.current_request_id / current_request_session_id
        让 _web_event_hook 能给 envelope 注入 request_id / session_id；
        记录 event_start_sequence / event_end_sequence 到 web_request。
        """
        web_request.status = "running"
        web_request.started_at = _now_utc()
        # set request context（hook 跨 task 不可靠，用 web-level state 而非 contextvar）
        state.current_request_id = web_request.id
        state.current_request_session_id = web_request.session_id
        # 占位 sequence 起点——下一个分配的 sequence 将是此值
        web_request.event_start_sequence = state.next_event_sequence

        try:
            result = await _run_prompt_core(validated)
        except asyncio.CancelledError:
            web_request.status = "aborted"
            web_request.ended_at = _now_utc()
            web_request.error = "cancelled"
            if web_request.abort_reason is None:
                web_request.abort_reason = "task_cancelled"
            raise
        except PromptRuntimeError as e:
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = e.message
            web_request.error_type = e.error_type
        except HTTPException as e:
            # _run_prompt_core 内部不应抛 HTTPException，但兜底
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = "HTTPException"
        except Exception as e:
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = type(e).__name__
        else:
            # 成功——但检查是否被 abort 过（abort 不 cancel task，走 run_prompt 收敛路径）
            if web_request.abort_reason is not None:
                web_request.status = "aborted"
            else:
                web_request.status = "completed"
            web_request.ended_at = _now_utc()
            web_request.result_summary = {
                "message_count": len(result.messages),
                "applied_skill_names": list(result.applied_skill_names),
                "session_id": result.session_id,
                # 不放 message 全文（安全 + 内存）
            }
        finally:
            # 记录最后一个 event 的 sequence（next - 1；如果没事件则 = start - 1）
            web_request.event_end_sequence = state.next_event_sequence - 1
            # clear request context——避免非 prompt 事件误关联
            state.current_request_id = None
            state.current_request_session_id = None
            _remove_from_active(web_request)
            state.request_history.append(web_request)

    async def _abort_request_internal(
        req: WebRunRequest,
        reason: str | None,
    ) -> dict[str, Any]:
        """abort 共享逻辑——POST /api/abort 别名 + POST /api/requests/{id}/abort 都走这里。"""
        reason_str = reason or "user_requested"

        if req.status == "queued":
            # task 尚未进 running 状态（理论上 create_task 立即调度；保险起见支持）
            req.abort_reason = reason_str
            if req.task is not None and not req.task.done():
                req.task.cancel()
            # 主动 finalize（runner 可能还没机会跑 finally）
            if req.status not in ("aborted", "error", "completed"):
                req.status = "aborted"
                req.ended_at = _now_utc()
                _remove_from_active(req)
                state.request_history.append(req)
        elif req.status == "running":
            # 设置 flag——runner 在 success 路径会读到
            req.abort_reason = reason_str
            # 调 harness.abort() 让模型 finalize（不 cancel task，避免孤儿）
            try:
                await harness.abort(req.abort_reason)
            except Exception as e:
                return {
                    "ok": False,
                    "error": _safe_error(e),
                    "request_id": req.id,
                    "status": req.status,
                }
        # completed/error/aborted → 幂等返回当前状态
        return {
            "ok": True,
            "request_id": req.id,
            "status": req.status,
            "abort_reason": req.abort_reason,
        }

    # ========================================================================
    # Static + index
    # ========================================================================

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_html = _STATIC_DIR / "index.html"
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_FALLBACK_HTML, media_type="text/html")

    @app.get("/assets/{path:path}", include_in_schema=False)
    async def assets(path: str) -> Any:
        # 只允许相对文件名，禁止 .. 逃逸
        target = (_STATIC_DIR / "assets" / path).resolve()
        try:
            target.relative_to((_STATIC_DIR / "assets").resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="not found") from None
        if not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target)

    # ========================================================================
    # State snapshot
    # ========================================================================

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        agent = harness.agent
        try:
            queue_size = agent.state.queue.qsize()  # type: ignore[attr-defined]
        except Exception:
            queue_size = 0
        return {
            "running": state.running,
            "last_error": state.last_error,
            "agent_status": _agent_status(),
            "queue_size": queue_size,
            "turn_count": getattr(agent.state, "turn_count", 0),
            "message_count": len(agent.state.messages),
            "snapshot_count": len(harness.snapshots),
            "event_count": len(state.event_buffer),
        }

    # ========================================================================
    # Messages
    # ========================================================================

    @app.get("/api/messages")
    async def get_messages(session_id: str | None = None) -> dict[str, Any]:
        """列出 messages。

        P0-1：支持 `?session_id=` 查 sqlite 历史消息。
        - 不传 session_id → 返回当前 agent.state.messages（fallback 旧路径）
        - 传 session_id → 返回 sqlite 中该 session 的 messages（按 idx 升序，
          强类型对象，不会退化成 dict）

        若指定 session 不存在，返回 404。
        """
        if session_id is None:
            msgs = list(harness.agent.state.messages)
            return {
                "count": len(msgs),
                "messages": [serialize_message(m) for m in msgs],
            }

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        from ..session_sqlite import SessionNotFoundError
        try:
            msgs = await store.list_messages(session_id)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {session_id!r} not found"},
            )
        return {
            "count": len(msgs),
            "session_id": session_id,
            "messages": [serialize_message(m) for m in msgs],
        }

    # ========================================================================
    # Events
    # ========================================================================

    @app.get("/api/events")
    async def get_events(
        session_id: str | None = None,
        request_id: str | None = None,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """列出 buffer 内事件（envelope-aware，P1-B2）。

        过滤参数：
            session_id     仅返回 envelope.session_id 匹配的事件
            request_id     仅返回 envelope.request_id 匹配的事件
            after_sequence 仅返回 envelope.sequence > after_sequence 的事件
            limit          最多返回 N 个事件（按 sequence 升序）；超限 has_more=True

        gap 检测（用户原指令 §6.3）：
            after_sequence + 1 < first_available_sequence → gap=True
            表示客户端期望的起点已被 buffer 截断丢弃。

        响应 schema：
            {
              "count": N,                       # 兼容旧字段 = len(events)
              "events": [...],                  # 过滤后的事件 list
              "first_available_sequence": int,  # 当前 buffer 内最小 sequence
              "last_available_sequence": int,   # 当前 buffer 内最大 sequence
              "has_more": bool,                 # limit 截断标志
              "gap": bool                       # after_sequence 已过期标志
            }
        """
        events = state.event_buffer.list()

        # 过滤
        filtered: list[dict[str, Any]] = []
        for ev in events:
            if session_id is not None and ev.get("session_id") != session_id:
                continue
            if request_id is not None and ev.get("request_id") != request_id:
                continue
            if after_sequence is not None:
                ev_seq = ev.get("sequence")
                if not isinstance(ev_seq, int) or ev_seq <= after_sequence:
                    continue
            filtered.append(ev)

        # limit 截断
        has_more = False
        if limit is not None and limit >= 0 and len(filtered) > limit:
            filtered = filtered[:limit]
            has_more = True

        # gap 检测：客户端期望起点 after_sequence+1，但 buffer 最早是 first_available_sequence
        gap = False
        if after_sequence is not None:
            first_avail = state.event_buffer.first_sequence
            if first_avail is not None and after_sequence + 1 < first_avail:
                gap = True

        return {
            "count": len(filtered),  # 兼容旧字段
            "events": filtered,
            "first_available_sequence": state.event_buffer.first_sequence,
            "last_available_sequence": state.event_buffer.last_sequence,
            "has_more": has_more,
            "gap": gap,
        }

    @app.post("/api/events/clear")
    async def clear_events() -> dict[str, Any]:
        state.event_buffer.clear()
        return {"ok": True, "count": 0}

    # ========================================================================
    # Snapshots
    # ========================================================================

    @app.get("/api/snapshots")
    async def get_snapshots() -> dict[str, Any]:
        snaps = list(harness.snapshots)
        return {
            "count": len(snaps),
            "snapshots": [
                {**serialize_snapshot_summary(s), "index": i}
                for i, s in enumerate(snaps)
            ],
        }

    @app.get("/api/snapshots/{index}")
    async def get_snapshot(index: int) -> dict[str, Any]:
        snaps = list(harness.snapshots)
        if not snaps:
            raise HTTPException(status_code=404, detail="no snapshots")
        try:
            snapshot = snaps[index]
        except IndexError:
            raise HTTPException(
                status_code=404,
                detail=f"snapshot index out of range: {index} (have {len(snaps)})",
            ) from None
        return serialize_snapshot_full(snapshot)

    # ========================================================================
    # Session
    # ========================================================================

    @app.get("/api/session")
    async def get_session() -> dict[str, Any]:
        """单数：当前 attached session（向后兼容）。"""
        return serialize_session(harness.session)

    @app.get("/api/sessions")
    async def get_sessions() -> dict[str, Any]:
        """复数：session 列表（spec endpoint）。

        P0-1：从 SQLiteSessionStore 读真实多会话列表（按 updated_at desc）。
        旧 fallback：若 session_store 未启用，回退到 harness 单 session。
        """
        store = state.session_store
        if store is None:
            # fallback：旧路径
            sess = serialize_session(harness.session)
            return {"count": 1, "sessions": [sess]}

        sessions = await store.list_sessions()
        items = [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "metadata": s.metadata,
                "is_current": s.id == state.current_session_id,
            }
            for s in sessions
        ]
        return {"count": len(items), "sessions": items}

    @app.post("/api/sessions", response_model=None)
    async def post_sessions(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        """创建新 session（spec endpoint，P0-1）。

        Body: `{"title": "..."}` （title 可选）
        """
        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session store not initialized"},
            )
        title = (payload or {}).get("title") or "default"
        meta = (payload or {}).get("metadata") or {}
        s = await store.create_session(title=title, metadata=meta)
        return {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
        }

    @app.patch("/api/sessions/{sid}", response_model=None)
    async def patch_session(
        sid: str, payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """重命名 session（spec endpoint，P0-1）。"""
        from ..session_sqlite import SessionNotFoundError

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session store not initialized"},
            )
        title = (payload or {}).get("title")
        if not title:
            return JSONResponse(
                status_code=400, content={"detail": "title is required"},
            )
        try:
            s = await store.rename_session(sid, title)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404, content={"detail": f"session {sid!r} not found"},
            )
        return {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
        }

    @app.delete("/api/sessions/{sid}", response_model=None)
    async def delete_session(
        sid: str,
    ) -> dict[str, Any] | JSONResponse:
        """删除 session（spec endpoint，P0-1）。级联删除 messages / snapshots / 上传文件。

        P0-2：先删 uploads/{sid}/，再删 sqlite session——避免孤儿目录。
        文件删除失败不阻塞 sqlite 删除（记 warning 到 metadata）。
        """
        from ..session_sqlite import SessionNotFoundError

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session store not initialized"},
            )

        # 先校验 session 存在（不存在直接 404，不动文件）
        existing = await store.get_session(sid)
        if existing is None:
            return JSONResponse(
                status_code=404, content={"detail": f"session {sid!r} not found"},
            )

        # P0-2：先删 uploads/{sid}/
        deleted_files = 0
        if state.file_store is not None:
            try:
                deleted_files = await state.file_store.delete_session_files(sid)
            except Exception as e:
                # 文件删除失败不阻塞 sqlite 删除；记 warning
                state.last_error = (
                    f"delete_session_files({sid}) failed: {type(e).__name__}: {e}"
                )

        try:
            await store.delete_session(sid)
        except SessionNotFoundError:
            # 二次防御——理论上前面 get_session 已校验
            return JSONResponse(
                status_code=404, content={"detail": f"session {sid!r} not found"},
            )
        # 删的是 current session → 自动切到 default（或新建一个）
        if state.current_session_id == sid:
            state.current_session_id = None
            try:
                default_session = await store.ensure_default_session()
                state.current_session_id = default_session.id
            except Exception:
                pass
        return {"ok": True, "deleted_files": deleted_files}

    # ========================================================================
    # Files（P0-2）
    # ========================================================================

    def _require_file_store():
        """统一拿 file_store；未启用返回 503 detail。"""
        if state.file_store is None:
            raise HTTPException(
                status_code=503,
                detail="file store not initialized; create_app(uploads_dir=...)",
            )
        return state.file_store

    @app.post("/api/sessions/{sid}/files", response_model=None)
    async def post_session_files(
        sid: str,
        files: list[UploadFile] = File(default=[]),  # noqa: B008
    ) -> dict[str, Any] | JSONResponse:
        """上传一个或多个文件到指定 session。

        - multipart/form-data，字段名 `files` 可重复
        - sid 不存在 → 404
        - 单文件超 max_file_size → 413（已写部分清理）
        - session 总量超 max_session_size → 413
        """
        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        # 校验 session 存在
        try:
            existing = await store.get_session(sid)
        except Exception:
            existing = None
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )

        file_store = _require_file_store()
        from .files import (
            FileStoreError,
            FileTooLargeError,
            SessionStorageLimitError,
            UnsafeFilenameError,
        )

        saved: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for upload in files:
            try:
                ref = await file_store.save(sid, upload)
                saved.append(ref.model_dump(mode="json"))
            except FileTooLargeError as e:
                errors.append({
                    "filename": upload.filename or "<unknown>",
                    "error_type": "FileTooLargeError",
                    "error": str(e),
                })
            except SessionStorageLimitError as e:
                errors.append({
                    "filename": upload.filename or "<unknown>",
                    "error_type": "SessionStorageLimitError",
                    "error": str(e),
                })
            except UnsafeFilenameError as e:
                errors.append({
                    "filename": upload.filename or "<unknown>",
                    "error_type": "UnsafeFilenameError",
                    "error": str(e),
                })
            except FileStoreError as e:
                errors.append({
                    "filename": upload.filename or "<unknown>",
                    "error_type": type(e).__name__,
                    "error": str(e),
                })
        status_code = 200
        if not saved and errors:
            # 全部失败——客户端可以据此显示
            status_code = 413 if any(
                e["error_type"] in ("FileTooLargeError", "SessionStorageLimitError")
                for e in errors
            ) else 400
        return JSONResponse(
            status_code=status_code,
            content={
                "count": len(saved),
                "files": saved,
                "errors": errors,
            },
        )

    @app.get("/api/sessions/{sid}/files", response_model=None)
    async def get_session_files(sid: str) -> dict[str, Any] | JSONResponse:
        """列出 session 所有文件 metadata。"""
        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        existing = await store.get_session(sid)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        file_store = _require_file_store()
        files = await file_store.list_session(sid)
        return {
            "count": len(files),
            "files": [f.model_dump(mode="json") for f in files],
        }

    @app.get("/api/sessions/{sid}/files/{fid}", response_model=None)
    async def get_session_file_content(
        sid: str, fid: str,
    ) -> Any:
        """下载 session 内单文件。

        - sid 不存在 → 404
        - fid 不存在 → 404
        - fid 属于其它 session → 403
        """
        from .files import (
            FileAccessDeniedError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        existing = await store.get_session(sid)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        file_store = _require_file_store()
        try:
            ref = await file_store.get_for_session(sid, fid)
        except VirtualFileNotFoundError as e:
            return JSONResponse(
                status_code=404, content={"detail": str(e)},
            )
        except FileAccessDeniedError as e:
            return JSONResponse(
                status_code=403, content={"detail": str(e)},
            )
        except UnsafeFilenameError as e:
            return JSONResponse(
                status_code=400, content={"detail": str(e)},
            )
        # FileResponse 把 Content-Type / filename 设对
        return FileResponse(
            ref.path, media_type=ref.mime, filename=ref.name,
        )

    @app.get("/api/files/{fid}", response_model=None)
    async def get_file_compat(
        fid: str, session_id: str | None = None,
    ) -> Any:
        """兼容下载入口：必须 query 参数 session_id。"""
        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "session_id query parameter is required"},
            )
        return await get_session_file_content(session_id, fid)

    @app.delete("/api/files/{fid}", response_model=None)
    async def delete_file_compat(
        fid: str, session_id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """兼容删除入口：必须 query 参数 session_id。"""
        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "session_id query parameter is required"},
            )
        return await delete_session_file(session_id, fid)

    @app.delete("/api/sessions/{sid}/files/{fid}", response_model=None)
    async def delete_session_file(
        sid: str, fid: str,
    ) -> dict[str, Any] | JSONResponse:
        """删除 session 内单文件（推荐入口）。"""
        from .files import (
            FileAccessDeniedError,
            FileStoreError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        existing = await store.get_session(sid)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        file_store = _require_file_store()
        try:
            await file_store.delete_for_session(sid, fid)
        except VirtualFileNotFoundError as e:
            return JSONResponse(
                status_code=404, content={"detail": str(e)},
            )
        except FileAccessDeniedError as e:
            return JSONResponse(
                status_code=403, content={"detail": str(e)},
            )
        except UnsafeFilenameError as e:
            return JSONResponse(
                status_code=400, content={"detail": str(e)},
            )
        except FileStoreError as e:
            return JSONResponse(
                status_code=500, content={"detail": str(e)},
            )
        return {"deleted": True, "file_id": fid}

    # ========================================================================
    # MCP
    # ========================================================================

    @app.get("/api/mcp")
    async def get_mcp() -> dict[str, Any]:
        registry = harness.mcp_registry
        if registry is None:
            return {"attached": False, "servers": [], "tools": [], "prompts": []}
        servers = [serialize_mcp_server_state(s) for s in registry.list_servers()]
        tools: list[dict[str, Any]] = []
        for tool in registry.list_agent_tools():
            tools.append({
                "name": getattr(tool, "name", None),
                "server": getattr(tool, "server_name", None),
                "mcp_tool": getattr(tool, "mcp_tool_name", None),
                "description": getattr(tool, "description", ""),
            })
        prompts = [
            {"server": server, "name": info.name, "description": info.description}
            for server, info in registry.list_prompts()
        ]
        return {
            "attached": True,
            "servers": servers,
            "tools": tools,
            "prompts": prompts,
        }

    @app.get("/api/mcp/tools")
    async def get_mcp_tools() -> dict[str, Any]:
        """spec endpoint：仅返回 MCP tools 列表（不含 servers / prompts）。

        与 GET /api/mcp 的 tools 字段同源；单独提供是因为 spec/UI 可能按需取。

        P0-4 Step 2：每条 tool 多一个 `enabled` 字段：
            enabled = tool_name not in state.disabled_mcp_tools
                      AND tool 当前在 harness.agent.tools 中（即真正暴露给 LLM）
        server 当前 disabled 时，mcp_registry 整体被 detach，list_agent_tools
        返回空——前端通过 GET /api/mcp/servers 看 server 状态，本 endpoint 只
        反映"当前 active server 的工具可见性"。
        """
        registry = harness.mcp_registry
        if registry is None:
            return {"attached": False, "tools": [], "count": 0}
        active_tool_names = set(harness.agent.tools.names()) if (
            getattr(harness.agent, "tools", None) is not None
        ) else set()
        tools: list[dict[str, Any]] = []
        for tool in registry.list_agent_tools():
            tname = getattr(tool, "name", None)
            # 真正 active = 在 agent.tools 中且未在 disabled_mcp_tools set 中
            enabled = (
                tname in active_tool_names
                and tname not in state.disabled_mcp_tools
            )
            tools.append({
                "name": tname,
                "server": getattr(tool, "server_name", None),
                "mcp_tool": getattr(tool, "mcp_tool_name", None),
                "description": getattr(tool, "description", ""),
                "enabled": enabled,
            })
        return {
            "attached": True,
            "tools": tools,
            "count": len(tools),
        }

    # ========================================================================
    # MCP server management（P0-4 Step 2 新增）
    # ========================================================================

    #: MCP server name 合法字符——与 mcp/config.py 中 MCPServerConfig 保持一致。
    #: 防御性同步：MCPServerConfig 内部用 MCP_NAME_RE 校验，这里 web 层先校验
    #: 可以给出更清晰的 400 错误。
    _MCP_NAME_RE_PATTERN = r"^[A-Za-z0-9_-]+$"

    #: MCP tool name 全名前缀，与 mcp/adapter.py MCPAgentTool.name 一致
    _MCP_TOOL_NAME_PREFIX = "mcp__"

    def _serialize_mcp_server(cfg: Any) -> dict[str, Any]:
        """WebMCPServerConfig → JSON-safe dict。

        **绝不**返回 env value——只返回 env_keys（sorted）。
        command / args 可以返回（本地开发配置）。
        """
        return {
            "name": cfg.name,
            "command": cfg.command,
            "args": list(cfg.args or []),
            "enabled": cfg.enabled,
            "last_error": cfg.last_error,
            "tool_count": cfg.tool_count,
            "env_keys": sorted((cfg.env or {}).keys()),
        }

    def _validate_mcp_server_payload(
        payload: dict[str, Any],
    ) -> tuple[WebMCPServerConfig | None, str | None]:
        """校验 POST /api/mcp/servers body，返回 (config, error)。

        - name 必须匹配 [A-Za-z0-9_-]+
        - command 必须非空 str
        - args 必须是 list[str]
        - env 必须是 dict[str, str]
        - enabled 可选，默认 False

        任一失败返回 (None, error_message)。
        """
        import re

        name = (payload or {}).get("name") or ""
        if not isinstance(name, str) or not name:
            return None, "name is required"
        if not re.match(_MCP_NAME_RE_PATTERN, name):
            return (
                None,
                "name must match [A-Za-z0-9_-]+ (got "
                f"{name!r})",
            )

        command = (payload or {}).get("command")
        if not isinstance(command, str) or not command.strip():
            return None, "command is required (non-empty string)"

        args_raw = (payload or {}).get("args") or []
        if not isinstance(args_raw, list):
            return None, "args must be a list of strings"
        for a in args_raw:
            if not isinstance(a, str):
                return None, f"args entries must be strings (got {type(a).__name__})"

        env_raw = (payload or {}).get("env") or {}
        if not isinstance(env_raw, dict):
            return None, "env must be a dict[str, str]"
        for k, v in env_raw.items():
            if not isinstance(k, str) or not k:
                return None, "env keys must be non-empty strings"
            if not isinstance(v, str):
                return None, f"env[{k!r}] must be string (got {type(v).__name__})"

        enabled = bool((payload or {}).get("enabled", False))

        cfg = WebMCPServerConfig(
            name=name,
            command=command.strip(),
            args=list(args_raw),
            env=dict(env_raw),
            enabled=enabled,
        )
        return cfg, None

    def _to_mcp_server_config(cfg: WebMCPServerConfig) -> Any:
        """WebMCPServerConfig → MCPServerConfig（harness.attach_mcp_servers 用）。

        timeout_s 默认 10s——避免坏 server 卡死 web 请求；调用方可覆盖。
        """
        from ..mcp import MCPServerConfig

        return MCPServerConfig(
            name=cfg.name,
            transport="stdio",
            command=cfg.command,
            args=list(cfg.args),
            env=dict(cfg.env),
            timeout_s=10.0,
            enabled=True,  # attach 时只传 enabled=True 的；MCPServerConfig.enabled
            # 本身不影响 attach 行为，attach_mcp_servers 用的是 configs list
        )

    async def _refresh_enabled_mcp_servers() -> None:
        """从 state.mcp_server_configs 取 enabled=True 的 configs，重 attach。

        - 失败不抛——把 last_error 写到对应 WebMCPServerConfig
        - 成功后从 harness.mcp_registry.list_servers() 同步 tool_count / last_error
        - **关键**：末尾必须重新应用 disabled_mcp_tools——
          harness.attach_mcp_servers 会 unregister 旧工具并注册新工具，
          之前 disabled 的工具会重新暴露；必须 unregister 一遍。

        约束：本函数只在 web 层操作；不动 harness 内部状态。
        """
        enabled_cfgs = [
            cfg for cfg in state.mcp_server_configs.values() if cfg.enabled
        ]
        if not enabled_cfgs:
            # 没有 enabled server：detach 所有
            try:
                await harness.detach_mcp_servers()
            except Exception as e:
                state.last_error = f"detach_mcp_servers: {type(e).__name__}: {e}"
            return

        mcp_cfgs = [_to_mcp_server_config(c) for c in enabled_cfgs]
        try:
            await harness.attach_mcp_servers(mcp_cfgs)
        except Exception as e:
            # 整批失败——给所有 enabled server 写 last_error；不动 enabled
            err = f"{type(e).__name__}: {e}"
            for cfg in enabled_cfgs:
                cfg.last_error = err
                cfg.tool_count = 0
            return

        # 成功——同步每个 server 的 tool_count / last_error
        server_states = {
            s.name: s for s in harness.list_mcp_servers()
        }
        for cfg in enabled_cfgs:
            st = server_states.get(cfg.name)
            if st is None:
                cfg.last_error = "missing from mcp_registry after attach"
                cfg.tool_count = 0
            else:
                cfg.tool_count = st.tool_count
                cfg.last_error = st.last_error

        # 关键：重新应用 disabled_mcp_tools 过滤——attach 之后所有工具都被注册到
        # agent.tools；这里把用户 disabled 的工具 unregister 掉
        if state.disabled_mcp_tools:
            for tname in list(state.disabled_mcp_tools):
                # 只 unregister 还存在的——避免对已不存在的工具调 unregister
                if harness.agent.tools.has(tname):
                    try:
                        harness.agent.tools.unregister(tname)
                    except Exception:
                        pass  # unregister 文档承诺不存在静默

    @app.get("/api/mcp/servers", response_model=None)
    async def get_mcp_servers() -> dict[str, Any]:
        """列出所有用户添加的 MCP server 配置。

        - 不返回 env values（只 env_keys）
        - command / args 可以返回
        - enabled / last_error / tool_count 同步自 state
        """
        cfgs = list(state.mcp_server_configs.values())
        return {
            "count": len(cfgs),
            "servers": [_serialize_mcp_server(c) for c in cfgs],
        }

    @app.post("/api/mcp/servers", response_model=None)
    async def post_mcp_server(
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """添加一个 MCP server 配置。

        - name 重复 → 409
        - name 不合法 / command 空 / args 不是 list[str] / env 不是 dict[str, str]
          → 400
        - enabled=true 时立即 refresh（attach 失败返回 400/502 但 config 已保存）
        - P0 推荐 enabled=false 默认；前端可显式 enable

        不返回 env values。
        """
        cfg, err = _validate_mcp_server_payload(payload)
        if cfg is None:
            return JSONResponse(status_code=400, content={"detail": err})
        if cfg.name in state.mcp_server_configs:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": f"MCP server {cfg.name!r} already exists",
                    "server_name": cfg.name,
                },
            )
        state.mcp_server_configs[cfg.name] = cfg

        # enabled=True 时立即 refresh——失败不撤销保存，仅写 last_error
        attach_error: str | None = None
        if cfg.enabled:
            try:
                await _refresh_enabled_mcp_servers()
            except Exception as e:
                attach_error = f"{type(e).__name__}: {e}"
                cfg.last_error = attach_error

        resp = _serialize_mcp_server(cfg)
        if attach_error is not None:
            return JSONResponse(status_code=502, content=resp)
        return resp

    @app.post("/api/mcp/servers/{name}/test", response_model=None)
    async def test_mcp_server(
        name: str,
    ) -> dict[str, Any] | JSONResponse:
        """测试 MCP server 连接——**不污染** harness 当前已启用 server。

        行为：
        - 从 state.mcp_server_configs 取 cfg（不存在 → 404）
        - 用 MCPClient 临时连接：connect / initialize / list_tools
        - finally: close（保证 stdio transport 关闭 + 子进程退出）
        - 不写 state（不更新 last_error / tool_count）

        成功返回：
            {"ok": true, "server": "...", "tools": [{name, description, input_schema}]}
        失败返回 502：
            {"ok": false, "server": "...", "error": "..."}
        """
        from ..mcp import MCPClient

        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )

        mcp_cfg = _to_mcp_server_config(cfg)
        client = MCPClient(mcp_cfg)
        try:
            await client.connect()
            await client.initialize()
            tools = await client.list_tools()
        except Exception as e:
            # 不写 state（test connection 是临时操作）
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "server": name,
                    "error": f"{type(e).__name__}: {e}",
                },
            )
        finally:
            try:
                await client.close()
            except Exception:
                pass

        tool_summaries = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]
        return {
            "ok": True,
            "server": name,
            "tools": tool_summaries,
            "tool_count": len(tool_summaries),
        }

    @app.post("/api/mcp/servers/{name}/enable", response_model=None)
    async def enable_mcp_server(
        name: str,
    ) -> dict[str, Any] | JSONResponse:
        """启用 MCP server。

        - 不存在 → 404
        - cfg.enabled = True；调 _refresh_enabled_mcp_servers（全量 attach）
        - 失败不抛——写 last_error，response 含 502
        """
        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )
        cfg.enabled = True
        try:
            await _refresh_enabled_mcp_servers()
        except Exception as e:
            cfg.last_error = f"{type(e).__name__}: {e}"
            return JSONResponse(
                status_code=502,
                content=_serialize_mcp_server(cfg),
            )
        return _serialize_mcp_server(cfg)

    @app.post("/api/mcp/servers/{name}/disable", response_model=None)
    async def disable_mcp_server(
        name: str,
    ) -> dict[str, Any] | JSONResponse:
        """禁用 MCP server。

        - 不存在 → 404
        - cfg.enabled = False；调 _refresh_enabled_mcp_servers（全量 attach，
          其余 enabled server 仍保留）
        - 同步清理 disabled_mcp_tools 中该 server 的工具（孤儿清理）
          —— 实际我们仍保留 state.disabled_mcp_tools 中的项，避免重新 enable
          时旧设置丢失；但 GET /api/mcp/tools 会因 server detach 而不展示。
        """
        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )
        cfg.enabled = False
        try:
            await _refresh_enabled_mcp_servers()
        except Exception as e:
            cfg.last_error = f"{type(e).__name__}: {e}"
            return JSONResponse(
                status_code=502,
                content=_serialize_mcp_server(cfg),
            )
        # 清掉 last_error——disable 成功后 server 不应有残留错误
        cfg.last_error = None
        cfg.tool_count = 0
        return _serialize_mcp_server(cfg)

    @app.delete("/api/mcp/servers/{name}", response_model=None)
    async def delete_mcp_server(
        name: str,
    ) -> dict[str, Any] | JSONResponse:
        """删除 MCP server 配置。

        - 不存在 → 404
        - 如果 enabled：先 disable 并 refresh（释放 transport）
        - 从 state.mcp_server_configs 删除
        - **同时清理 disabled_mcp_tools 中 mcp__{name}__ 前缀的项**
          ——避免孤儿 + 避免同名 server 重新添加时旧设置意外生效
        """
        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )
        if cfg.enabled:
            cfg.enabled = False
            try:
                await _refresh_enabled_mcp_servers()
            except Exception as e:
                state.last_error = (
                    f"delete_mcp_server({name}) detach failed: "
                    f"{type(e).__name__}: {e}"
                )
        # 删除配置
        state.mcp_server_configs.pop(name, None)
        # 清理孤儿 disabled tool names（前缀 mcp__{name}__）
        prefix = f"{_MCP_TOOL_NAME_PREFIX}{name}__"
        stale = {t for t in state.disabled_mcp_tools if t.startswith(prefix)}
        if stale:
            state.disabled_mcp_tools -= stale
        return {"deleted": True, "name": name, "cleaned_disabled_tools": sorted(stale)}

    @app.post("/api/mcp/tools/{tool_name}/enable", response_model=None)
    async def enable_mcp_tool(
        tool_name: str,
    ) -> dict[str, Any] | JSONResponse:
        """启用单个 MCP tool。

        - tool_name 必须是合法 MCP tool 全名（mcp__{server}__{tool}）
        - 不在 disabled_mcp_tools 中 → 200，已是 enabled
        - server 已 disabled → 409（先 enable server）
        - tool 不在 mcp_registry 中 → 404

        实现：
        - 从 disabled_mcp_tools 移除
        - 从 harness.mcp_registry.list_agent_tools() 找回 MCPAgentTool，重新 register
        """
        if not tool_name.startswith(_MCP_TOOL_NAME_PREFIX):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"tool_name must be MCP tool full name "
                        f"(mcp__server__tool); got {tool_name!r}"
                    ),
                },
            )

        # 解析 server name 用于校验 server 状态
        rest = tool_name[len(_MCP_TOOL_NAME_PREFIX):]
        if "__" not in rest:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"invalid MCP tool name {tool_name!r}; "
                        "expected mcp__{server}__{tool}"
                    ),
                },
            )
        server_name = rest.split("__", 1)[0]
        server_cfg = state.mcp_server_configs.get(server_name)
        if server_cfg is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"MCP server {server_name!r} not configured",
                },
            )
        if not server_cfg.enabled:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        f"MCP server {server_name!r} is disabled; "
                        "enable the server first"
                    ),
                },
            )

        # 找回 MCPAgentTool——harness.mcp_registry 此时不为 None（server enabled）
        registry = harness.mcp_registry
        if registry is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "mcp_registry not attached"},
            )
        target = next(
            (t for t in registry.list_agent_tools() if t.name == tool_name),
            None,
        )
        if target is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": (
                        f"MCP tool {tool_name!r} not found in registry "
                        f"(server {server_name!r})"
                    ),
                },
            )

        state.disabled_mcp_tools.discard(tool_name)
        if not harness.agent.tools.has(tool_name):
            try:
                harness.agent.tools.register(target)
            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={
                        "detail": f"register {tool_name!r} failed: {type(e).__name__}: {e}",
                    },
                )
        return {"tool_name": tool_name, "enabled": True}

    @app.post("/api/mcp/tools/{tool_name}/disable", response_model=None)
    async def disable_mcp_tool(
        tool_name: str,
    ) -> dict[str, Any] | JSONResponse:
        """禁用单个 MCP tool。

        - tool_name 必须是 MCP tool 全名
        - 加入 disabled_mcp_tools set
        - 如果当前在 agent.tools 中，unregister（**真实生效**——LLM 不再看到）
        - 幂等：tool 已 disabled → 仍返回 200

        不强制要求 server enabled——server disabled 时工具本来就不在 agent.tools，
        但仍把 tool_name 加入 set，server 重新 enable 时过滤会生效。
        """
        if not tool_name.startswith(_MCP_TOOL_NAME_PREFIX):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        f"tool_name must be MCP tool full name "
                        f"(mcp__server__tool); got {tool_name!r}"
                    ),
                },
            )

        state.disabled_mcp_tools.add(tool_name)
        if harness.agent.tools.has(tool_name):
            try:
                harness.agent.tools.unregister(tool_name)
            except Exception:
                # unregister 文档承诺不存在静默；防御性 try
                pass
        return {"tool_name": tool_name, "enabled": False}

    # ========================================================================
    # Skills
    # ========================================================================

    def _is_localhost(request: Request) -> bool:
        """请求是否来自 127.0.0.1 / ::1。

        支持代理透传场景：优先看 X-Forwarded-For（如有），否则 client.host。
        本地 only 模型——非 localhost 一律视为远程。

        特殊：TestClient 的 client.host 可能是 'testclient' 字面量——也算
        localhost（测试场景），避免测试永远 403。
        """
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip().lower()
            if first in ("127.0.0.1", "::1", "localhost"):
                return True
            return False
        client = request.client
        if client is None:
            return False
        return client.host in ("127.0.0.1", "::1", "localhost", "testclient")

    @app.get("/api/skills")
    async def get_skills(
        request: Request,
        include_prompt: bool = False,
    ) -> dict[str, Any]:
        registry = harness.skill_registry
        if registry is None:
            return {
                "attached": False,
                "skills": [],
                "skill_loader": harness.context.metadata.get("skill_loader"),
            }
        # include_prompt=True 时强保护：默认禁用；allow_prompt_preview=True
        # 才放行；非 localhost 即使开启也拒。
        if include_prompt:
            if not app.state.allow_prompt_preview:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "include_prompt=true requires create_app(allow_prompt_preview=True); "
                        "this server has it disabled"
                    ),
                )
            if not _is_localhost(request):
                raise HTTPException(
                    status_code=403,
                    detail="include_prompt=true is only allowed from localhost",
                )
        skills = [
            serialize_skill(s, include_prompt=include_prompt)
            for s in registry.list()
        ]
        return {
            "attached": True,
            "skills": skills,
            "skill_loader": to_json_safe(
                harness.context.metadata.get("skill_loader") or {}
            ),
        }

    # ------------------------------------------------------------------
    # Skills management（P0-4 新增）
    # ------------------------------------------------------------------

    # P0-4：上传 SKILL.md 单文件大小上限。SkillFileLoader 默认 256KB，
    # 这里直接校验上传文本大小，避免依赖临时文件。
    _SKILL_UPLOAD_MAX_BYTES = 256_000

    def _require_skill_registry() -> Any:
        """获取已 attach 的 SkillRegistry；未 attach → 422。

        Skill 上传 / enable / disable / 详情都要求 harness 已经有 registry。
        WebAppState 在 lifespan 中通常会 attach 一个空 registry；调用方
        显式 detach 后才会变 None。
        """
        registry = harness.skill_registry
        if registry is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "skill_registry is not attached; "
                    "create_app must attach an empty SkillRegistry first"
                ),
            )
        return registry

    def _secure_skill_filename(name: str | None) -> str:
        """从上传 filename 推导 fallback_name（取 stem，限制字符集）。

        - 仅保留 [A-Za-z0-9_-]，其它字符替换为 `_`
        - 空 / 全非法 → 兜底 `"uploaded_skill"`
        """
        if not name:
            return "uploaded_skill"
        stem = name.rsplit(".", 1)[0] if "." in name else name
        cleaned = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in stem)
        cleaned = cleaned.strip("._-")
        return cleaned or "uploaded_skill"

    @app.post("/api/skills/upload", response_model=None)
    async def upload_skills(
        files: list[UploadFile] = File(default=[]),  # noqa: B008
    ) -> dict[str, Any] | JSONResponse:
        """上传一个或多个 SKILL.md 文件并注册到 harness.skill_registry。

        - multipart/form-data，字段名 `files` 可重复
        - 单文件大小上限 256KB（`_SKILL_UPLOAD_MAX_BYTES`）
        - 解析失败（frontmatter YAML 不合法 / 类型错误 / name 推断失败）
          → 400，错误信息含 filename + error_type
        - 重名（SkillRegistrationError）→ 409，含 detail.skill_name
        - registry 未 attach → 422
        - 成功返回 `{count, skills: [SkillSummary]}`，不包含 prompt 正文
        """
        from ..skill_loader import (
            SkillFileFormatError,
            parse_skill_markdown,
        )
        from ..skills import SkillRegistrationError

        registry = _require_skill_registry()

        if not files:
            return JSONResponse(
                status_code=400,
                content={"detail": "no files uploaded (field name: 'files')"},
            )

        saved_skills: list[Any] = []
        errors: list[dict[str, Any]] = []
        for upload in files:
            filename = upload.filename or ""
            try:
                raw = await upload.read()
            except Exception as e:
                errors.append({
                    "filename": filename or "<unknown>",
                    "error_type": type(e).__name__,
                    "error": f"failed to read upload: {e}",
                })
                continue
            if len(raw) > _SKILL_UPLOAD_MAX_BYTES:
                errors.append({
                    "filename": filename or "<unknown>",
                    "error_type": "SkillFileSecurityError",
                    "error": (
                        f"uploaded skill file size {len(raw)} exceeds "
                        f"max_file_size_bytes={_SKILL_UPLOAD_MAX_BYTES}"
                    ),
                })
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                errors.append({
                    "filename": filename or "<unknown>",
                    "error_type": "SkillFileFormatError",
                    "error": f"file is not valid utf-8: {e}",
                })
                continue

            fallback_name = _secure_skill_filename(filename)
            try:
                skill = parse_skill_markdown(
                    text,
                    fallback_name=fallback_name,
                    source_path=f"upload:{filename or fallback_name}",
                )
            except SkillFileFormatError as e:
                errors.append({
                    "filename": filename or fallback_name,
                    "error_type": "SkillFileFormatError",
                    "error": str(e),
                })
                continue

            try:
                registry.register(skill)
            except SkillRegistrationError as e:
                # 重名 → 409，detail 含 skill_name 让前端区分
                msg = str(e)
                # 不直接抛 HTTPException，因为我们要返回 multiple-error 响应
                errors.append({
                    "filename": filename or fallback_name,
                    "skill_name": skill.name,
                    "error_type": "SkillRegistrationError",
                    "error": msg,
                    "status": 409,
                })
                continue
            saved_skills.append(skill)

        # 至少一个成功 → 200；全部失败 → 用首个 error status 作整体 status
        out_skills = [
            serialize_skill(s, include_prompt=False) for s in saved_skills
        ]
        if saved_skills and not errors:
            return {"count": len(saved_skills), "skills": out_skills, "errors": []}
        if saved_skills and errors:
            return JSONResponse(
                status_code=207,
                content={
                    "count": len(saved_skills),
                    "skills": out_skills,
                    "errors": errors,
                },
            )
        # 全失败
        first_status = errors[0].get("status") if errors else 400
        status_code = first_status if isinstance(first_status, int) else 400
        # SkillRegistrationError（重名）应映射成 409
        if status_code == 409:
            status_code = 409
        elif errors and errors[0].get("error_type") == "SkillRegistrationError":
            status_code = 409
        elif errors and errors[0].get("error_type") == "SkillFileFormatError":
            status_code = 400
        return JSONResponse(
            status_code=status_code,
            content={
                "count": 0,
                "skills": [],
                "errors": errors,
            },
        )

    @app.get("/api/skills/{name}", response_model=None)
    async def get_skill(
        name: str,
        request: Request,
        include_prompt: bool = False,
    ) -> dict[str, Any] | JSONResponse:
        """单条 skill 详情。

        - include_prompt=True 默认 403；allow_prompt_preview=True + localhost 才放行
        - skill 不存在 → 404
        """
        from ..skills import SkillNotFoundError

        registry = _require_skill_registry()
        if include_prompt:
            if not app.state.allow_prompt_preview:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "include_prompt=true requires create_app(allow_prompt_preview=True); "
                        "this server has it disabled"
                    ),
                )
            if not _is_localhost(request):
                raise HTTPException(
                    status_code=403,
                    detail="include_prompt=true is only allowed from localhost",
                )
        try:
            skill = registry.get(name)
        except SkillNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"skill {name!r} not found"},
            )
        return serialize_skill(skill, include_prompt=include_prompt)

    @app.post("/api/skills/{name}/enable", response_model=None)
    async def enable_skill(name: str) -> dict[str, Any] | JSONResponse:
        """启用 skill。不存在 → 404；registry 未 attach → 422。"""
        from ..skills import SkillNotFoundError

        registry = _require_skill_registry()
        try:
            registry.enable(name)
        except SkillNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"skill {name!r} not found"},
            )
        return {"ok": True, "name": name, "status": "enabled"}

    @app.post("/api/skills/{name}/disable", response_model=None)
    async def disable_skill(name: str) -> dict[str, Any] | JSONResponse:
        """禁用 skill。不存在 → 404；registry 未 attach → 422。"""
        from ..skills import SkillNotFoundError

        registry = _require_skill_registry()
        try:
            registry.disable(name)
        except SkillNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"skill {name!r} not found"},
            )
        return {"ok": True, "name": name, "status": "disabled"}

    # ========================================================================
    # Policy audit
    # ========================================================================

    @app.get("/api/policy/audit")
    async def get_policy_audit(limit: int = 100) -> dict[str, Any]:
        if limit <= 0 or limit > 10_000:
            limit = max(0, min(limit, 10_000))
        records = harness.list_permission_audit_records()
        # 取最近 limit 条（按写入顺序的尾部）
        sliced = records[-limit:] if limit else records
        policy_name = (
            harness.permission_policy.name
            if harness.permission_policy is not None
            else None
        )
        return {
            "policy_name": policy_name,
            "count": len(sliced),
            "records": [serialize_policy_audit_record(r) for r in sliced],
        }

    # ========================================================================
    # Actions: prompt / abort / reset
    # ========================================================================

    @app.post("/api/prompt", response_model=None)
    async def post_prompt(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        """触发一次 prompt（同步阻塞；P1-B1 起逻辑与 /api/prompt/async 共享）。

        行为与 v0.0.23.1 完全一致——_run_prompt_core 是从原 endpoint 抽出的公共逻辑。
        旧的 4xx / 409 / 500 错误 schema 与字段（detail / missing_skill_names /
        ok=false / error / error_type）严格保持不变。

        P0-1：body.session_id
        P0-3：body.file_ids（FileBlock 注入到 UserMessage.content）
        P0-4：body.skill_names + body.skill_selection.names 合并去重

        P1-B2：set/clear state.current_request_id（sync 路径用 req_sync_ 前缀）
        让 _web_event_hook 给 envelope 注入 request_id / session_id，
        与 /api/prompt/async 路径行为一致。sync 路径不创建 WebRunRequest record。
        """
        try:
            validated = await _validate_prompt_payload(payload)
            # set request context（sync 路径用临时 request_id，不进 active_requests）
            sync_request_id = f"req_sync_{uuid4().hex[:12]}"
            state.current_request_id = sync_request_id
            state.current_request_session_id = validated.session_id
            try:
                result = await _run_prompt_core(validated)
            finally:
                state.current_request_id = None
                state.current_request_session_id = None
        except PromptValidationError as e:
            return _serialize_prompt_validation_error(e)
        except PromptRuntimeError as e:
            return _serialize_prompt_runtime_error(e)
        return {
            "ok": True,
            "session_id": result.session_id,
            "messages": result.serialized_messages,
            "attachments": result.attachment_meta,
            "applied_skill_names": result.applied_skill_names,
        }

    # ========================================================================
    # P1-B1: 异步 prompt endpoint + request lifecycle API
    # ========================================================================

    @app.post("/api/prompt/async", response_model=None)
    async def post_prompt_async(
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """异步触发 prompt——立即返回 request_id + HTTP 202。

        行为（用户原指令 §5.3）：
        1. 跑完整乐观校验（与同步路径一致）—— 失败立即 4xx，不创建 request
        2. session 级别并发检查（同 session 已有 queued/running → 409）
        3. 创建 request record + asyncio.create_task(_run_prompt_background)
        4. 立即返回 202 + request_id + status=queued + 各资源 URL

        并发限制（用户原指令 §3.16 / §5.3）：
        - 单 agent 实例全局单 active request（_ensure_idle 保证）
        - session_id 字段保留为未来扩展点；当前不虚假宣称多 session 并行
        """
        if state.shutting_down:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "server shutting down; cannot accept new prompts"
                },
            )

        # 1. 完整乐观校验——_ensure_idle / text / skill_names / unknown skill /
        #    session 存在 / file ownership 都在 _validate_prompt_payload 里
        try:
            validated = await _validate_prompt_payload(payload)
        except PromptValidationError as e:
            return _serialize_prompt_validation_error(e)
        except HTTPException as e:
            # _ensure_idle 抛 HTTPException(409)——转与同步路径一致的 schema
            return JSONResponse(
                status_code=e.status_code, content={"detail": e.detail}
            )

        # 2. session 级并发检查
        session_id = validated.session_id
        if session_id and session_id in state.active_request_by_session:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        f"session {session_id!r} already has an active request"
                    ),
                },
            )

        # 3. 创建 request record
        request_id = f"req_{uuid4().hex[:16]}"
        web_request = WebRunRequest(
            id=request_id,
            session_id=session_id,
            status="queued",
            created_at=_now_utc(),
            # payload 仅内存——debug 用；不进任何 JSON response
            payload=dict(payload) if isinstance(payload, dict) else None,
        )
        state.active_requests[request_id] = web_request
        if session_id:
            state.active_request_by_session[session_id] = request_id

        # 4. 启动受管理 background task
        task = asyncio.create_task(
            _run_prompt_background(web_request, validated),
            name=f"prompt_async_{request_id}",
        )
        web_request.task = task

        # 5. 立即返回 202——不 await task
        events_url = "/api/events" + (
            f"?session_id={session_id}" if session_id else ""
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "request_id": request_id,
                "session_id": session_id,
                "status": "queued",
                "events_url": events_url,
                "request_url": f"/api/requests/{request_id}",
                "abort_url": f"/api/requests/{request_id}/abort",
            },
        )

    @app.get("/api/requests", response_model=None)
    async def list_requests(
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """列出 request——支持 ?session_id / ?status=active / ?limit（P1-B3-3）。

        status 过滤：
            active → queued OR running
            terminal → completed OR error OR aborted
            其它（或不传）→ 不按 status 过滤

        排序：created_at DESC（最新优先）。
        默认 limit=50；上限 200。

        **安全**：响应只含 _serialize_request 字段——不含 task / payload /
        system prompt / MCP env / traceback。

        典型用途：页面刷新后查 active session 是否有未完成 request。
        """
        if limit <= 0 or limit > 200:
            limit = max(0, min(limit, 200))

        # 收集 active + history
        all_reqs: list[WebRunRequest] = list(state.active_requests.values())
        all_reqs.extend(state.request_history)

        # 过滤 session_id
        if session_id is not None:
            all_reqs = [r for r in all_reqs if r.session_id == session_id]

        # 过滤 status
        if status == "active":
            all_reqs = [r for r in all_reqs if r.status in ("queued", "running")]
        elif status == "terminal":
            all_reqs = [
                r
                for r in all_reqs
                if r.status in ("completed", "error", "aborted")
            ]
        elif status is not None:
            # 精确匹配 status
            all_reqs = [r for r in all_reqs if r.status == status]

        # 排序：created_at DESC（None 视为最早）
        all_reqs.sort(
            key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

        # limit
        all_reqs = all_reqs[:limit]

        return {
            "count": len(all_reqs),
            "requests": [_serialize_request(r) for r in all_reqs],
        }

    @app.get("/api/requests/{request_id}", response_model=None)
    async def get_request(
        request_id: str,
    ) -> dict[str, Any] | JSONResponse:
        """查询单个 request 状态——active + history 都查；不存在 404。"""
        req = _find_request(request_id)
        if req is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"request {request_id!r} not found"},
            )
        return _serialize_request(req)

    @app.post("/api/requests/{request_id}/abort", response_model=None)
    async def abort_request_endpoint(
        request_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """abort 单个 request——queued/running/completed/error/aborted 全部幂等。"""
        req = _find_request(request_id)
        if req is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"request {request_id!r} not found"},
            )
        reason = None
        if isinstance(payload, dict):
            r_val = payload.get("reason")
            if isinstance(r_val, str):
                reason = r_val
        result = await _abort_request_internal(req, reason)
        if not result.get("ok", True):
            return JSONResponse(status_code=500, content=result)
        return result

    @app.post("/api/abort", response_model=None)
    async def post_abort(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        """兼容别名——转发到当前 active request（如有）；否则直调 harness.abort()。

        保留旧 endpoint 是为了不破坏前端 Stop 按钮（B1 阶段不切前端）和旧测试。
        """
        reason = (payload or {}).get("reason") if isinstance(payload, dict) else None
        reason_str = reason if isinstance(reason, str) else None

        # 有 active request → 转发到 _abort_request_internal
        if state.active_requests:
            # 单 active（_ensure_idle 保证）—— 取第一个
            req_id = next(iter(state.active_requests))
            req = state.active_requests[req_id]
            result = await _abort_request_internal(req, reason_str)
            if not result.get("ok", True):
                return JSONResponse(status_code=500, content=result)
            return {"ok": True, **{k: v for k, v in result.items() if k != "ok"}}

        # 无 active request → 旧行为：直调 harness.abort()（兼容尚未走 async 路径的场景）
        try:
            await harness.abort(reason_str)
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": f"{type(e).__name__}: {e}"},
            )
        return {"ok": True}


    @app.post("/api/reset", response_model=None)
    async def post_reset(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        _ensure_idle()
        opts = payload or {}
        clear_events = bool(opts.get("clear_events", True))
        clear_snapshots = bool(opts.get("clear_snapshots", False))
        clear_audit = bool(opts.get("clear_audit", False))

        # 1. agent.reset()——清 messages / events / turn_count
        try:
            harness.agent.reset()
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": f"{type(e).__name__}: {e}"},
            )

        if clear_events:
            state.event_buffer.clear()
        if clear_snapshots:
            harness.clear_snapshots()
        if clear_audit:
            harness.clear_permission_audit_records()

        return {
            "ok": True,
            "cleared": {
                "events": clear_events,
                "snapshots": clear_snapshots,
                "audit": clear_audit,
            },
        }

    # ========================================================================
    # SSE stream
    # ========================================================================

    @app.get("/api/stream")
    async def sse_stream(
        request: Request,
        limit: int | None = None,
    ) -> StreamingResponse:
        """SSE endpoint——为每个连接建独立 asyncio.Queue，on_event hook 广播。

        - 客户端断开（request.is_disconnected()）→ 退出循环 + 从 sse_clients 移除
        - idle 时定期发 heartbeat（注释行 `: heartbeat\\n\\n`），防止代理超时
        - 单个 event payload 用 json.dumps + ensure_ascii=False
        - `?limit=N`（测试用）：发出 N 个真实 event 后关闭 stream；hello/heartbeat
          不计入 N。不传 limit 时仍是无限流（默认行为不变）。
        """
        if limit is not None and limit < 0:
            raise HTTPException(
                status_code=400, detail="limit must be >= 0 (or omitted)",
            )

        client_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        sse_clients.add(client_queue)
        sent_count = {"n": 0}

        async def event_gen() -> AsyncIterator[str]:
            try:
                # 先发一个 hello event——让前端立刻知道连接成功
                yield _sse_format("hello", {"agent_status": _agent_status()})
                # limit=0：hello 后立即关闭，用于自动化测试
                if limit is not None and limit == 0:
                    return
                while True:
                    # limit 到达——正常关闭 stream
                    if limit is not None and sent_count["n"] >= limit:
                        return
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(
                            client_queue.get(), timeout=_SSE_HEARTBEAT_SECONDS,
                        )
                        # shutdown sentinel：服务端关闭，立即结束
                        if payload.get("type") == "shutdown" and limit is None:
                            yield _sse_format("shutdown", payload)
                            return
                        yield _sse_format("event", payload)
                        sent_count["n"] += 1
                    except TimeoutError:
                        # heartbeat——空注释行
                        yield ": heartbeat\n\n"
            finally:
                sse_clients.discard(client_queue)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # nginx 不缓冲
                "Connection": "keep-alive",
            },
        )

    # ========================================================================
    # WebSocket（spec /ws/events）
    # ========================================================================

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket) -> None:
        """WebSocket endpoint——每连接一个 asyncio.Queue(maxsize=100)。

        行为：
        - 连接建立：发送 hello event，等待客户端消息保持连接（不限协议）
        - on_event hook 广播：通过 ws_clients set 广播到所有连接
        - 慢客户端 queue 满（maxsize=100）：直接丢弃该 event，不阻塞其它客户端
        - 客户端断开：从 ws_clients 移除，回收 queue
        """
        await websocket.accept()
        client_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        ws_clients.add(client_queue)
        try:
            # 先发 hello（P1-B3-0c 控制帧）
            #
            # **关键不变量**（B3-0c §5.3）：
            # - hello 是裸 dict，**不**走 _web_event_hook 包装
            # - **不**进 TraceEventBuffer
            # - **不**消耗 state.next_event_sequence
            # - 前端**不**让它进 seenEventIds / lastGlobalSequence
            # - 前端**不**让它进 ChatStreamItem mapper
            #
            # first_available_sequence / last_available_sequence 帮助客户端建立 baseline：
            # - 首次连接 + 无 active request：lastGlobalSequence = last_available_sequence
            # - 避免"第一个真实事件 sequence=500 被误判缺失 1-499"
            await websocket.send_json({
                "type": "hello",
                "agent_status": _agent_status(),
                "first_available_sequence": state.event_buffer.first_sequence,
                "last_available_sequence": state.event_buffer.last_sequence,
                "server_time": _now_utc().isoformat(),
                "_received_at_ms": int(time.time() * 1000),
            })
            while True:
                # 不真的从客户端读——这里只为检测断连。约定客户端可发任意 keepalive。
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except WebSocketDisconnect:
                    return
                except TimeoutError:
                    pass
                # 把 queue 中累积的事件批量 send；满了就丢弃
                while not client_queue.empty():
                    payload = client_queue.get_nowait()
                    try:
                        await websocket.send_json(payload)
                    except WebSocketDisconnect:
                        return
                    except Exception:
                        # 客户端 socket 异常——退出
                        return
        finally:
            ws_clients.discard(client_queue)

    return app


# ============================================================================
# App 生命周期辅助
# ============================================================================


def dispose_app(app: FastAPI) -> None:
    """显式清理 create_app 注册的 hook。

    用于测试场景（不启动 uvicorn / TestClient lifecycle）下避免 hook 累积。
    会从 app.state.web.harness 的 on_event_hooks 中移除自己的 _web_event_hook。

    TestClient 的 with 块退出时会触发 shutdown 自动调；本函数用于不进
    with 的场景（如 fixture 创建后立即丢弃）。
    """
    hook = getattr(app.state, "web_event_hook", None)
    harness = getattr(getattr(app.state, "web", None), "harness", None)
    if hook is not None and harness is not None:
        harness.remove_on_event_hook(hook)
        app.state.web_event_hook = None


# ============================================================================
# 内部辅助
# ============================================================================


def _sse_format(event_name: str, payload: Any) -> str:
    """构造一条 SSE 消息——`event: {name}\\ndata: {json}\\n\\n`。"""
    data_str = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {data_str}\n\n"


__all__ = ["create_app", "dispose_app"]
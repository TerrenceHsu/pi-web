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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
from .state import WebAppState, WebMCPServerConfig

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
    app.state.web = state
    app.state.allow_prompt_preview = allow_prompt_preview
    app.state.event_buffer_max_size = event_buffer_max_size

    # SSE 客户端队列集合——每个 SSE 连接独立 asyncio.Queue；
    # on_event hook 把 event 广播（put_nowait）到所有客户端队列
    sse_clients: set[asyncio.Queue[dict[str, Any]]] = container["sse_clients"]
    # WebSocket 客户端队列集合——同 SSE，独立 queue 池
    ws_clients: set[asyncio.Queue[dict[str, Any]]] = container["ws_clients"]

    async def _web_event_hook(event: Any, _ctx: Any) -> None:
        """on_event hook：序列化 event，写入 buffer + 广播 SSE / WS。

        任何异常都被吞掉——Hook 失败不应影响 Agent 主流程（_handle_agent_event
        本身有 try/except 兜底，这里再加一层防御）。
        """
        try:
            payload = serialize_event(event)
            # 加上 server-side 接收时间，便于 UI 排序
            if isinstance(payload, dict):
                payload.setdefault("_received_at_ms", int(time.time() * 1000))
            state.event_buffer.append(payload if isinstance(payload, dict) else {})
            # 广播到所有 SSE / WS client——慢客户端 put_nowait 抛 QueueFull 时
            # 丢弃该 event（不阻塞其它 client / 不阻塞主 loop）
            for q in list(sse_clients) + list(ws_clients):
                try:
                    q.put_nowait(payload)
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
    async def get_events() -> dict[str, Any]:
        events = state.event_buffer.list()
        return {"count": len(events), "events": events}

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
        """触发一次 prompt。

        P0-1：支持 body 中传 `session_id`。
        - 传 session_id → 从 sqlite 加载该 session 历史 messages 注入
          agent.state.messages；run_prompt 后用 final_messages 覆盖回 sqlite；
          append snapshot
        - 不传 session_id → 用 state.current_session_id（lifespan 时创建的 default）

        P0-3：支持 body 中传 `file_ids: list[str]`。
        - 校验每个 file_id 属于该 session（不存在 404 / 跨 session 403）
        - 把每个附件作为 FileBlock 注入 UserMessage.content
        - 图片也以 format="image_unsupported" 注入（不做图片理解）
        - 不新增 ImageBlock；不向 provider 传 image block
        - snapshot metadata 记录 attached_file_ids / names / counts
        """
        _ensure_idle()
        text = (payload or {}).get("text") or ""
        if not text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        skill_sel_raw = (payload or {}).get("skill_selection") or {}
        # P0-4：顶层 skill_names（list[str]）—— 与 skill_selection.names 等价合并
        # 前端 ChatInput 直接传 ["a", "b"] 比包一层 skill_selection 更顺手。
        # 校验：必须是 list[str]；空 list 当 None 处理。
        skill_names_raw = (payload or {}).get("skill_names")
        if skill_names_raw is not None:
            if not isinstance(skill_names_raw, list):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "skill_names must be a list of strings"},
                )
            bad = [n for n in skill_names_raw if not isinstance(n, str) or not n]
            if bad:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": (
                            "skill_names entries must be non-empty strings "
                            f"(got {bad[0]!r})"
                        ),
                    },
                )
        # 合并：顶层 skill_names + skill_selection.names（去重保序）
        merged_names: list[str] | None = None
        sel_names = list(skill_sel_raw.get("names") or [])
        top_names = list(skill_names_raw or [])
        if sel_names or top_names:
            seen: set[str] = set()
            merged_names = []
            for n in [*sel_names, *top_names]:
                if n and n not in seen:
                    seen.add(n)
                    merged_names.append(n)

        skill_selection: SkillSelection | None = None
        if skill_sel_raw or merged_names:
            skill_selection = SkillSelection(
                names=merged_names,
                tags=skill_sel_raw.get("tags"),
                values=skill_sel_raw.get("values") or {},
            )

        # P0-4 Step 2：校验 skill_names 都在 registry 中——避免 SkillRegistry.select
        # 内部抛 SkillNotFoundError 走到 500。这是 web 层最小修复，不动 harness。
        if merged_names and harness.skill_registry is not None:
            missing = [
                n for n in merged_names if not harness.skill_registry.has(n)
            ]
            if missing:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": f"Unknown skill: {missing[0]!r}",
                        "missing_skill_names": missing,
                    },
                )

        # 解析 session_id：显式 > current_session_id
        session_id = (payload or {}).get("session_id") or state.current_session_id
        store = state.session_store

        # 若有 store + session_id：加载历史到 agent.state.messages
        if store is not None and session_id is not None:
            from ..session_sqlite import SessionNotFoundError
            try:
                history = await store.list_messages(session_id)
            except SessionNotFoundError:
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"session {session_id!r} not found"},
                )
            # 暂存原 messages，run_prompt 完成后还原（避免 sqlite 路径污染 agent 长期状态）
            original_messages = list(harness.agent.state.messages)
            harness.agent.state.messages = list(history)
        else:
            original_messages = None

        # P0-3：file_ids 解析 + 校验 + 注入
        file_ids_raw = (payload or {}).get("file_ids") or []
        if not isinstance(file_ids_raw, list):
            return JSONResponse(
                status_code=400,
                content={"detail": "file_ids must be a list of strings"},
            )
        attached_blocks: list[Any] = []  # FileBlock 实例
        attached_summary: list[dict[str, Any]] = []
        if file_ids_raw:
            if state.file_store is None:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "file store not initialized; cannot accept file_ids"},
                )
            if session_id is None:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "session_id required when file_ids present"},
                )
            from ..messages import FileBlock
            from ..tools.view_file import _classify_format
            from .files import (
                FileAccessDeniedError,
                UnsafeFilenameError,
                VirtualFileNotFoundError,
            )

            for fid in file_ids_raw:
                if not isinstance(fid, str) or not fid:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": (
                                f"file_ids entries must be non-empty strings "
                                f"(got {fid!r})"
                            ),
                        },
                    )
                try:
                    ref = await state.file_store.get_for_session(session_id, fid)
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
                attached_summary.append({
                    "id": ref.id, "name": ref.name, "mime": ref.mime,
                    "size": ref.size, "sha256": ref.sha256, "format": fmt,
                })

        state.running = True
        state.last_error = None
        try:
            if attached_blocks:
                # P0-3：把 text + FileBlocks 合并到一条 UserMessage，再走 run_continue。
                # 这样 convert_to_llm 处理后是单条 user message（含 [text, file, file]），
                # LLM 看到文件元信息 + 用户提问在同一个 user turn。
                from ..messages import TextContent, UserMessage

                user_msg = UserMessage(content=[
                    TextContent(text=text), *attached_blocks,
                ])
                # 把构造好的 UserMessage 追加到 agent.state.messages 末尾
                # （此时已被 sqlite history 替换为正确历史）
                harness.agent.state.messages.append(user_msg)
                messages = await harness.run_continue(skill_selection=skill_selection)
            else:
                messages = await harness.run_prompt(text, skill_selection=skill_selection)
        except HTTPException:
            raise
        except RuntimeError as e:
            # Harness "already running" 等 RuntimeError 转 409 而非 500——
            # 客户端可据此重试 / 显示占用提示
            msg = str(e)
            state.last_error = f"{type(e).__name__}: {msg}"
            status = 409 if "already running" in msg.lower() else 500
            return JSONResponse(
                status_code=status,
                content={
                    "ok": False,
                    "error": state.last_error,
                    "error_type": type(e).__name__,
                },
            )
        except Exception as e:
            state.last_error = f"{type(e).__name__}: {e}"
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": state.last_error,
                    "error_type": type(e).__name__,
                },
            )
        finally:
            state.running = False

        # 若走了 sqlite 路径：用 final_messages 覆盖回 sqlite；append snapshot
        if store is not None and session_id is not None:
            try:
                await store.replace_messages(session_id, list(messages))
                if harness.last_snapshot is not None:
                    await store.append_snapshot(session_id, harness.last_snapshot)
            except Exception as e:
                # 持久化失败不应覆盖 prompt 成功返回；写入 metadata 供诊断
                state.last_error = f"persist: {type(e).__name__}: {e}"
            finally:
                # 还原 agent.state.messages（避免 sqlite 路径污染后续非 sqlite 请求）
                if original_messages is not None:
                    harness.agent.state.messages = original_messages

        # P0-3：snapshot metadata 追加附件信息（snapshot 已 finish，但 context.metadata
        # 仍然可读；记录到 response 里方便前端显示）
        supported_count = sum(
            1 for s in attached_summary
            if s["format"] not in ("image_unsupported", "binary", "unsupported", "pdf")
        )
        unsupported_count = len(attached_summary) - supported_count
        attachment_meta = {
            "attached_file_ids": [s["id"] for s in attached_summary],
            "attached_file_names": [s["name"] for s in attached_summary],
            "attached_file_count": len(attached_summary),
            "attached_supported_file_count": supported_count,
            "attached_unsupported_file_count": unsupported_count,
        }

        # P0-4：把本轮实际启用的 skill_names 写入 response，便于前端展示 SkillUsedCard
        # （registry 中不存在的 name 已由 SkillRegistry.select 抛 SkillNotFoundError；
        # 但我们走 run_continue/run_prompt 路径会自己 select，失败时变 500——前端
        # 应只发已上传的 skill name）
        applied_skill_names: list[str] = []
        if skill_selection is not None and skill_selection.names:
            applied_skill_names = list(skill_selection.names)

        return {
            "ok": True,
            "session_id": session_id,
            "messages": [serialize_message(m) for m in messages],
            "attachments": attachment_meta,
            "applied_skill_names": applied_skill_names,
        }

    @app.post("/api/abort", response_model=None)
    async def post_abort(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        reason = (payload or {}).get("reason")
        try:
            await harness.abort(reason if isinstance(reason, str) else None)
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
            # 先发 hello
            await websocket.send_json({
                "type": "hello",
                "agent_status": _agent_status(),
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
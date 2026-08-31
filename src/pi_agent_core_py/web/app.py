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

**安全**：此工厂仍是单工作区应用。需要登录与账号隔离时，用
``create_authenticated_app`` 作为外层网关；两者都不面向公网部署。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
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
    Response,
    StreamingResponse,
)

from agent_workspace.continuity import (
    AUTO_MEMORY_OPERATION_KIND,
    AutoMemoryOperationEvidence,
    checkpoint_source_from_operation_payload,
    checkpoint_source_to_operation_payload,
    merge_checkpoint_sources,
    recover_auto_memory_operations,
)
from coding_agent_app.intent_router import (
    READ_ONLY_SYSTEM_PROMPT,
    IntentDecision,
    is_read_only_tool_name,
    parse_intent_mode,
    route_intent,
)

from .. import __version__
from ..harness import AgentHarness
from ..session_sqlite import SessionOperationConflictError
from ..skills import SkillSelection
from .approvals import ToolApprovalManager
from .checkpointer import (
    CHECKPOINTER_COMMAND,
    SESSION_MEMORY_PATH,
    SLASH_COMMANDS,
    CheckpointerError,
    CheckpointSource,
    build_checkpoint_source,
    extract_checkpoint_source_hash,
    generate_checkpoint_memory,
    parse_slash_command,
    recover_checkpointer_operations,
)
from .content_integrity import summarize_content_integrity
from .providers.runtime import (
    ProviderInitializationError,
    ProviderSelectionDisabledError,
    ProviderSelectionNotFoundError,
    ProviderSelectionUnavailableError,
    RequestProviderSelection,
)
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

if TYPE_CHECKING:
    from agent_workspace.code_continuity import CodeContinuityTrigger
    from agent_workspace.context_assembler import (
        SandboxContinuationState,
        WorkspaceContextAssembly,
    )
    from agent_workspace.documents import WorkspaceDocumentConverterRegistry
    from agent_workspace.store import WorkspaceStore
    from coding_agent_app.planning.models import PlanRunResult
    from coding_sandbox import ArtifactSigner
    from coding_sandbox.admin import SandboxBackendFactory
    from wiki_parser import ParserProvider, ParserProviderV2

    from .wiki.summary import WikiSummaryAgent

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

#: 删除 Session 前等待其活动请求安全退出的最长时间。超时必须保留 Session，
#: 避免后台任务继续读写已经删除的数据，或留下占用全局 Harness 的幽灵请求。
_SESSION_DELETE_REQUEST_STOP_TIMEOUT_SECONDS: float = 10.0


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
    knowledge_conversation: Any = None
    coding_mode: bool = False
    execution_mode: Literal["direct", "plan"] = "direct"
    intent: IntentDecision | None = None
    pending_continuity_evidence: AutoMemoryOperationEvidence | None = None


@dataclass(frozen=True)
class ValidatedRegenerationRequest:
    """D2-5：_validate_regeneration_payload 成功产物——传给 _run_regeneration_core。

    所有校验通过后才构造——失败抛 HTTPException，不构造此对象。
    `history` 不含待替换的旧 assistant（含 preceding user）。
    `original_harness_messages` 用于 fallback（reset 失败时恢复）。
    """

    session_id: str
    assistant_message_id: str
    preceding_user_message_id: str
    history: tuple[Any, ...]
    original_harness_messages: tuple[Any, ...]
    knowledge_conversation: Any = None


class RegenerationValidationError(Exception):
    """D2-5：regenerate 校验失败——含稳定 code + HTTP status。

    不含 candidate 正文 / SQL / 绝对路径——安全错误响应。
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PromptExecutionResult:
    """D2-4：纯执行产物——_execute_prompt 的返回值。

    **不含任何持久化状态**——只反映模型/Agent 的执行结果。持久化由
    `_persist_normal_prompt_result` / `_persist_regeneration_result` 负责。

    字段语义：
    - `messages`：本次执行**结束后** harness.agent.state.messages 完整列表
    - `assistant_message`：本次执行产生的最终 assistant candidate（来自 suffix，
      排除中间 tool-call-only assistant；如果没有合格 candidate 则为 None）
    - `messages_before`：执行前 harness state 的 messages 快照
    - `messages_after`：执行后 harness state 的 messages 快照（同 messages）
    - `stop_reason`：本次终态 AssistantMessage 的停止原因
    - `usage`：本次终态 AssistantMessage 的 Provider usage
    - `snapshot_payload`：harness.last_snapshot 的可序列化视图（如果有）
    - `result_summary`：额外元数据（applied_skill_names / attachment_meta 等）
    """

    messages: list[Any]
    assistant_message: Any | None
    messages_before: list[Any]
    messages_after: list[Any]
    stop_reason: str | None
    usage: Any | None
    snapshot_payload: dict[str, Any] | None
    result_summary: dict[str, Any] | None


@dataclass
class PromptRunOutcome:
    """_run_prompt_core 成功后的产物——执行 + 持久化后的最终视图（caller 兼容）。

    D2-4 起 _run_prompt_core 是 thin wrapper：调 _execute_prompt + 普通 persist；
    返回此结构兼容 _run_prompt_background / sync /api/prompt。
    """

    messages: list[Any]
    serialized_messages: list[dict[str, Any]]
    session_id: str | None
    attachment_meta: dict[str, Any]
    applied_skill_names: list[str]
    coding_sandbox: dict[str, object] | None = None
    continuity: dict[str, object] | None = None
    workspace_context: dict[str, Any] | None = None
    intent: dict[str, object] | None = None
    plan_run: dict[str, object] | None = None
    turn_messages: tuple[Any, ...] = ()
    session_persisted: bool = False


# ============================================================================
# create_app
# ============================================================================


def _apply_citation_transform(
    execution: PromptExecutionResult,
    state: WebAppState,
) -> None:
    """P2-R4-C2: Post-execution citation transform.

    After the LLM generates text containing ``[cite:E1]`` tokens,
    validate them against the turn-scoped EvidenceRegistry and
    render numbered citations + source footer before persistence.

    Operates in-place on ``execution.messages`` (mutates the last
    qualifying AssistantMessage's text content). No-op if registry
    is empty or no citations found.
    """
    registry = getattr(state, "_evidence_registry", None)
    if registry is None or len(registry) == 0:
        return

    from ..messages import AssistantMessage, TextContent
    from .knowledge.citations import (
        process_citations,
        render_source_footer,
    )

    for msg in reversed(execution.messages):
        if not isinstance(msg, AssistantMessage):
            continue
        if getattr(msg, "error_message", None):
            continue
        for i in range(len(msg.content) - 1, -1, -1):
            block = msg.content[i]
            if not isinstance(block, TextContent):
                continue
            if "[cite:" not in block.text:
                continue
            result = process_citations(block.text, registry)
            if not result.citations:
                msg.content[i] = TextContent(text=result.rendered_content)
                continue
            footer = render_source_footer(result.citations)
            if footer:
                final_text = result.rendered_content.rstrip() + "\n\n" + footer
            else:
                final_text = result.rendered_content
            msg.content[i] = TextContent(text=final_text)
            return
        break


def create_app(
    harness: AgentHarness,
    *,
    event_buffer_max_size: int = 1000,
    allow_prompt_preview: bool = False,
    db_path: str | Path | None = None,
    uploads_dir: str | Path | None = None,
    max_file_size: int = 25 * 1024 * 1024,
    max_session_upload_size: int = 100 * 1024 * 1024,
    workspace_document_converters: WorkspaceDocumentConverterRegistry | None = None,
    request_history_maxlen: int = 100,
    shutdown_grace_s: float = 5.0,
    # P1-E1-4A: Credential runtime composition（可选）
    # None / ":memory:" 时跳过 credential runtime——保持向后兼容
    credential_secret_backend: str = "auto",
    enable_credential_runtime: bool | None = None,
    enable_trusted_host: bool = False,
    credential_extra_hosts: tuple[str, ...] = (),
    credential_extra_ui_origins: tuple[str, ...] = (),
    # P1-E1-4B3: Credential REST API（8 endpoints）
    # None=auto（runtime 启用时自动 mount）
    # True=强制启用（需 runtime + TrustedHost + file SQLite）
    # False=禁用
    enable_credentials_api: bool | None = None,
    # P1-E2-3B1: Provider Profiles REST API（7 endpoints）+ default binding
    # None=auto（仅当 Credential API enabled + TrustedHost + file SQLite 时启用）
    # True=强制启用（前置条件同 Credential API）
    # False=禁用——不打开 Store、不挂路由、不改 Session 创建
    enable_provider_profiles_api: bool | None = None,
    # Managed coding sandbox admin API. None follows the credential API's
    # localhost/file-database safety prerequisites; True enforces them.
    enable_coding_sandbox_api: bool | None = None,
    coding_sandbox_backend_factory: SandboxBackendFactory | None = None,
    coding_sandbox_artifact_signer: ArtifactSigner | None = None,
    # Low-level embedders opt in explicitly; the coding-agent product entrypoint
    # enables this by default. Requires a Session Workspace.
    enable_auto_memory: bool = False,
    # Trusted architecture/code-flow/validation summaries for published code.
    # Product composition enables this; embedders opt in explicitly.
    enable_code_continuity: bool = False,
    # Deterministic read-only / coding / Knowledge routing for the product app.
    # Disabled by default to preserve low-level embedder tool semantics.
    enable_intent_routing: bool = False,
    # Independent Planner–Executor–Verifier execution for Coding requests.
    # The product entrypoint enables it; low-level embedders opt in.
    enable_plan_mode: bool = False,
    # Deprecated Chunk Knowledge compatibility root.
    # The legacy DB, workers, Tool and REST API only start when
    # enable_knowledge_api=True is also explicit. Product composition uses
    # wiki_root and leaves this disabled.
    #   - 在 <knowledge_root>/knowledge.db 打开独立 aiosqlite connection
    #   - <knowledge_root>/libraries/{library_id}/documents/... 物理文件
    #   - 挂载 Knowledge Library CRUD + Session Binding REST API（仅当
    #     trusted_host + UI header deps 启用时；否则不挂 router，service
    #     仍可用于内部 / 测试）
    knowledge_root: str | Path | None = None,
    # True is an explicit compatibility opt-in. None/False retires the entire
    # legacy runtime rather than merely hiding its router.
    enable_knowledge_api: bool | None = None,
    # Page-centric LLM Wiki product runtime. The deprecated Chunk Knowledge
    # compatibility path stays off unless independently and explicitly enabled.
    wiki_root: str | Path | None = None,
    enable_wiki_api: bool | None = None,
    wiki_pdf_provider: ParserProvider | None = None,
    wiki_pdf_provider_v2: ParserProviderV2 | None = None,
    wiki_summary_agent: WikiSummaryAgent | None = None,
    wiki_source_retention_seconds: float = 7 * 24 * 60 * 60,
    wiki_source_purge_interval_seconds: float = 5 * 60,
    # Corresponding Source root for the separately AGPL-licensed PDF Worker.
    # None auto-discovers a source checkout and otherwise reports unavailable.
    # The main application reads compliance assets only; it never imports the
    # Worker package or concrete parser runtime.
    wiki_parser_worker_source_root: str | Path | None = None,
    # Built-in DuckDuckGo MCP server. Disabled by default so existing library
    # users/tests retain an empty MCP list; the authenticated dev app enables it.
    enable_builtin_ddgs: bool = False,
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
        uploads_dir: WorkspaceStore 的物理数据根。None（默认）= 不启用 Workspace
            上传路径；所有 `/api/.../files` endpoint 返回 503。生产场景传目录路径。
        max_file_size: 单文件大小上限，默认 25 MB
        max_session_upload_size: 单 session 总上传上限，默认 100 MB
        workspace_document_converters: 固定文档 converter registry；None 使用
            内建 PDF/DOCX/XLSX 实现，测试可注入同一 Protocol 的确定性实现。
        enable_code_continuity: 是否从已发布 `scripts/**` revision 生成固定
            architecture/code-flow/validation 文档。需要 `uploads_dir`；低层
            工厂默认关闭，Coding Agent 产品入口默认开启。
    """
    if (
        wiki_source_retention_seconds < 0
        or wiki_source_purge_interval_seconds <= 0
        or not math.isfinite(wiki_source_retention_seconds)
        or not math.isfinite(wiki_source_purge_interval_seconds)
    ):
        raise ValueError("Wiki Source retention durations are invalid")
    if enable_auto_memory and uploads_dir is None:
        raise ValueError("automatic Memory requires uploads_dir")
    if enable_code_continuity and uploads_dir is None:
        raise ValueError("code continuity requires uploads_dir")
    # 用 closure 持有 hook / clients——lifespan 退出时清理
    container: dict[str, Any] = {
        "hook": None,
        "sse_clients": set(),
        "ws_clients": set(),
        "coding_sandbox_tool_names": set(),
        "continuity_tasks": set(),
        "plan_approval_events": {},
    }
    # Agent file tools may run concurrently for different sessions. A task-local
    # binding prevents one request from observing another request's global web
    # state while preserving current_session_id as a non-request fallback.
    tool_session_context: ContextVar[str | None] = ContextVar(
        "pi_agent_web_tool_session",
        default=None,
    )

    # ========================================================================
    # P1-E1-4B3: Resolve Credential API configuration at app creation
    # ========================================================================
    from .credentials.runtime import (
        CredentialWebSecurityConfigurationError,
        resolve_credential_api_configuration,
    )

    try:
        _cred_resolved = resolve_credential_api_configuration(
            enable_credentials_api=enable_credentials_api,
            enable_credential_runtime=enable_credential_runtime,
            enable_trusted_host=enable_trusted_host,
            db_path=db_path,
        )
    except CredentialWebSecurityConfigurationError as e:
        # 不静默降级——必须显式修正
        raise RuntimeError(f"credential web security configuration error: {e}") from e

    from .coding_sandbox.runtime import (
        SandboxRuntimeConfigurationError,
        resolve_sandbox_runtime_configuration,
    )

    try:
        _sandbox_resolved = resolve_sandbox_runtime_configuration(
            enable_api=enable_coding_sandbox_api,
            credential_runtime_enabled=_cred_resolved.runtime_enabled,
            credential_api_enabled=_cred_resolved.api_enabled,
            trusted_host_enabled=_cred_resolved.trusted_host_enabled,
            db_path=db_path,
        )
    except SandboxRuntimeConfigurationError as e:
        raise RuntimeError(f"coding sandbox web security configuration error: {e}") from e

    if enable_knowledge_api is True and knowledge_root is None:
        raise RuntimeError("legacy knowledge API requires knowledge_root")
    _legacy_knowledge_enabled = enable_knowledge_api is True

    # ========================================================================
    # P1-E2-3B1: Resolve Provider Profiles API configuration at app creation
    # Depends on Credential resolver—must be called AFTER _cred_resolved.
    # ========================================================================
    from .providers.config_runtime import (
        ProviderConfigWebSecurityConfigurationError,
        resolve_provider_profiles_api_configuration,
    )

    try:
        _pc_resolved = resolve_provider_profiles_api_configuration(
            enable_provider_profiles_api=enable_provider_profiles_api,
            credential_api_enabled=_cred_resolved.api_enabled,
            credential_runtime_enabled=_cred_resolved.runtime_enabled,
            trusted_host_enabled=_cred_resolved.trusted_host_enabled,
            db_path=db_path,
        )
    except ProviderConfigWebSecurityConfigurationError as e:
        raise RuntimeError(f"provider config web security configuration error: {e}") from e

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

        # P2-C: exact provider/model limits are user workspace data and share
        # the authenticated workspace DB connection.
        from .model_capabilities import SQLiteModelCapabilityStore

        session_connection = session_store.connection
        if session_connection is None:
            raise RuntimeError("session store connection unavailable after init")
        model_capability_store = SQLiteModelCapabilityStore(session_connection)
        await model_capability_store.init()
        state.model_capability_store = model_capability_store

        if state.plan_mode_enabled:
            from coding_agent_app.planning import PlanStore

            plan_store = PlanStore(session_connection)
            await plan_store.init()
            state.plan_store = plan_store
            await plan_store.recover_interrupted()
        else:
            state.plan_store = None

        # Session Workspace：初始化唯一 WorkspaceStore，并为所有已有 session
        # 立即创建独立目录（而不是等第一次上传时才惰性出现）。
        if uploads_dir is not None:
            from agent_workspace.store import WorkspaceStore

            file_store = WorkspaceStore(
                uploads_dir,
                max_file_size=max_file_size,
                max_session_size=max_session_upload_size,
            )
            try:
                await file_store.init()
                for existing_session in await session_store.list_sessions():
                    await file_store.ensure_session_workspace(existing_session.id)
                state.file_store = file_store
                state.uploads_dir = Path(uploads_dir)
                from agent_workspace.documents import WorkspaceDocumentService

                uploads_path = await asyncio.to_thread(
                    Path(uploads_dir).resolve,
                    strict=False,
                )
                state.workspace_document_service = WorkspaceDocumentService(
                    file_store,
                    staging_root=(
                        uploads_path.parent / f".{uploads_path.name}-document-conversions"
                    ),
                    registry=workspace_document_converters,
                )
                from agent_workspace.context_assembler import (
                    WorkspaceContextAssembler,
                )

                state.workspace_context_assembler = WorkspaceContextAssembler(
                    file_store
                )
                if enable_code_continuity:
                    from agent_workspace.code_continuity import CodeContinuityService

                    state.code_continuity_service = CodeContinuityService(
                        file_store,
                        staging_root=(
                            uploads_path.parent
                            / f".{uploads_path.name}-code-continuity"
                        ),
                    )
                else:
                    state.code_continuity_service = None
            except Exception:
                # 文件存储不可用不阻塞 app 启动；endpoint 走 503。
                # 但必须留下完整 traceback，避免深层文件系统错误只表现为下游 503。
                _logger.exception(
                    "WorkspaceStore init failed; file endpoints will return 503 (uploads_dir=%s)",
                    uploads_dir,
                )
                state.file_store = None
                state.uploads_dir = None
                state.workspace_document_service = None
                state.code_continuity_service = None
                state.workspace_context_assembler = None
        else:
            state.file_store = None
            state.uploads_dir = None
            state.workspace_document_service = None
            state.code_continuity_service = None
            state.workspace_context_assembler = None

        # P2 durable operations: reduce accepted checkpointer intents before
        # requests can observe the workspace. Recovery is evidence-only and
        # never calls the provider; a changed source leaf preserves messages.
        if state.file_store is not None:
            recovery = await recover_checkpointer_operations(session_store, state.file_store)
            state.durable_recovery_summary = {
                "scanned": recovery.scanned,
                "completed": recovery.completed,
                "aborted": recovery.aborted,
                "conflicts": recovery.conflicts,
            }
            continuity_recovery = await recover_auto_memory_operations(
                session_store,
                state.file_store,
            )
            state.continuity_recovery_summary = {
                "scanned": continuity_recovery.scanned,
                "completed": continuity_recovery.completed,
                "pending": continuity_recovery.pending,
                "conflicts": continuity_recovery.conflicts,
            }
            if state.code_continuity_service is not None:
                for existing_session in await session_store.list_sessions():
                    workspace = await state.file_store.get_workspace_state(
                        existing_session.id
                    )
                    if workspace.code_continuity.status == "current":
                        continue
                    try:
                        await state.code_continuity_service.refresh(
                            existing_session.id,
                            trigger="recovery",
                        )
                    except Exception:
                        _logger.warning(
                            "Code continuity recovery remains stale (session_id=%s)",
                            existing_session.id,
                        )
        else:
            state.durable_recovery_summary = {
                "scanned": 0,
                "completed": 0,
                "aborted": 0,
                "conflicts": 0,
            }
            state.continuity_recovery_summary = {
                "scanned": 0,
                "completed": 0,
                "pending": 0,
                "conflicts": 0,
            }

        # file_store 可用 → 注册 list_files / view_file / write_file。
        # 工具执行期间必须优先绑定 request 的 session；UI 当前选中项只作为
        # 非请求调用的 fallback，避免请求指定 sid 时误读/误写默认目录。
        if state.file_store is not None:
            from ..tools.list_files import create_list_files_tool
            from ..tools.view_file import create_view_file_tool
            from ..tools.write_file import create_write_file_tool

            def _session_id_getter() -> str | None:
                return (
                    tool_session_context.get()
                    or state.current_request_session_id
                    or state.current_session_id
                )

            list_tool = create_list_files_tool(
                file_store=state.file_store,
                session_id_getter=_session_id_getter,
            )
            view_tool = create_view_file_tool(
                file_store=state.file_store,
                session_id_getter=_session_id_getter,
            )
            write_tool = create_write_file_tool(
                file_store=state.file_store,
                session_id_getter=_session_id_getter,
            )
            tools_registry = harness.agent.tools
            if not tools_registry.has("list_files"):
                tools_registry.register(list_tool)
            if not tools_registry.has("view_file"):
                tools_registry.register(view_tool)
            if not tools_registry.has("write_file"):
                tools_registry.register(write_tool)

        # P1-C2: 初始化 extension_store（与 session_store 共享 connection）
        # + skill_mutation_lock + 启动恢复 uploaded Skills
        from .extension_store import ExtensionSQLiteStore

        extension_store = ExtensionSQLiteStore(store_path, connection=session_store.connection)
        await extension_store.init()
        state.extension_store = extension_store
        state.skill_mutation_lock = asyncio.Lock()

        async def _close_startup_sqlite_stores() -> None:
            """Close core stores when startup fails before lifespan yield."""

            try:
                await extension_store.close()
            except Exception:
                pass
            try:
                await session_store.close()
            except Exception:
                pass
            state.extension_store = None
            state.session_store = None

        # P1-D2-6: sweep 遗留 running revisions → interrupted
        # **必须在 restore Skills/MCP 之前**——sweep 只依赖 session/extension SQLite，
        # 不应被 MCP 连接超时延迟；即使 MCP restore 失败，stale running revision
        # 也应先被清理（否则 partial unique running index 会阻止后续 regenerate）。
        # Sweep 失败 = 启动失败（不吞掉）——避免半损坏状态。
        try:
            await extension_store.mark_running_revisions_interrupted(
                completed_at=datetime.now(UTC).isoformat()
            )
        except Exception as e:
            # The lifespan has not yielded yet, so FastAPI will not execute the
            # normal shutdown section below. Close both startup-owned stores
            # here or a failed sweep leaks the aiosqlite connection into GC.
            await _close_startup_sqlite_stores()
            # 安全摘要——不含 content / SQL / 绝对路径 / traceback / secret
            raise RuntimeError(f"startup sweep failed: {type(e).__name__}") from e

        # 启动恢复 uploaded Skills（逐行隔离 + sha256 校验 + model_validate）
        await _restore_uploaded_skills(extension_store)

        # 内置 DDGS 先写入/修正持久化配置，再走统一 restore 流程。这样即使
        # 历史数据库里有同名的任意命令，也不会在规范化前被启动。
        ddgs_settings = None
        if enable_builtin_ddgs:
            ddgs_settings = await _prepare_builtin_ddgs(extension_store)

        # P1-C4: 恢复 MCP server 配置 + auto attach + apply disabled tools
        await _restore_mcp_servers(extension_store)
        if ddgs_settings is not None:
            _mark_builtin_ddgs_runtime(ddgs_settings)

        # ====================================================================
        # Deprecated Chunk Knowledge compatibility composition. This is never
        # auto-started; explicit enable_knowledge_api=True is required.
        # ====================================================================
        knowledge_service = None
        if _legacy_knowledge_enabled:
            assert knowledge_root is not None
            from .knowledge.files import KnowledgeFileStore
            from .knowledge.service import KnowledgeService
            from .knowledge.store import KnowledgeStore

            kroot = Path(knowledge_root)
            await asyncio.to_thread(kroot.mkdir, parents=True, exist_ok=True)
            k_file_store = KnowledgeFileStore(root=kroot)
            await asyncio.to_thread(k_file_store.ensure_root)
            k_store = await KnowledgeStore.open(str(kroot / "knowledge.db"))

            async def _session_exists_for_knowledge(session_id: str) -> bool:
                if state.session_store is None:
                    return False
                try:
                    sess = await state.session_store.get_session(session_id)
                except Exception:
                    return False
                return sess is not None

            knowledge_service = KnowledgeService(
                store=k_store,
                file_store=k_file_store,
                session_exists=_session_exists_for_knowledge,
            )
            state.knowledge_service = knowledge_service
            state.knowledge_store = k_store
            state.knowledge_file_store = k_file_store

            # ============================================================
            # P2-R2-C2: Ingestion Worker Manager — app-scoped singleton
            # that drives PDF → Canonical Markdown pipeline.
            #
            # Constructed only if [rag] extra is available (PypdfParser
            # import succeeds). If pypdf not installed, manager stays
            # None — knowledge subsystem still works for metadata-only
            # operations.
            # ============================================================
            ingestion_manager = None
            try:
                from .knowledge.canonical_markdown import (
                    CanonicalMarkdownBuilder,
                )
                from .knowledge.ingestion_orchestrator import (
                    IngestionOrchestrator,
                )
                from .knowledge.ingestion_store import IngestionStore
                from .knowledge.ingestion_worker import (
                    IngestionWorkerManager,
                )
                from .knowledge.markdown_persistence import (
                    CanonicalMarkdownPersistence,
                )
                from .knowledge.pdf_quality import PdfTextQualityEvaluator
                from .knowledge.pypdf_parser import PypdfParser

                ingestion_store_obj = IngestionStore(k_store)
                parser_obj = PypdfParser()
                orchestrator_obj = IngestionOrchestrator(
                    store=k_store,
                    ingestion_store=ingestion_store_obj,
                    file_store=k_file_store,
                    parser=parser_obj,
                    quality_evaluator=PdfTextQualityEvaluator(),
                    builder=CanonicalMarkdownBuilder(),
                    persistence=CanonicalMarkdownPersistence(k_file_store),
                )
                ingestion_manager = IngestionWorkerManager(
                    orchestrator=orchestrator_obj,
                    ingestion_store=ingestion_store_obj,
                    store=k_store,
                    parser=parser_obj,
                    owns_parser=True,
                )
            except ImportError:
                # [rag] extra (pypdf) not installed — skip ingestion pipeline
                pass
            except Exception as e:
                raise RuntimeError(
                    f"ingestion worker manager init failed: {type(e).__name__}"
                ) from e

            if ingestion_manager is not None:
                try:
                    await ingestion_manager.start()
                    state.ingestion_worker_manager = ingestion_manager
                except Exception as e:
                    raise RuntimeError(
                        f"ingestion worker manager start failed: {type(e).__name__}"
                    ) from e

            # ============================================================
            # P2-R3-D2: Indexing Worker Manager — app-scoped singleton
            # that drives normalizing → chunking → indexing → ready.
            #
            # Constructed only if Ingestion Worker is available (depends
            # on the same [rag] extra). Started AFTER Ingestion Worker
            # (producer before consumer); stopped BEFORE Ingestion Worker
            # stop in shutdown (consumer before producer — actually
            # directive §28 says producer stops first to prevent new
            # normalizing Docs during Index Worker drain; so Index
            # Worker stops AFTER Ingestion Worker in shutdown).
            # ============================================================
            if ingestion_manager is not None:
                try:
                    from .knowledge.chunk_store import ChunkStore
                    from .knowledge.indexing_orchestrator import (
                        IndexingOrchestrator,
                    )
                    from .knowledge.indexing_store import IndexingStore
                    from .knowledge.indexing_worker import (
                        IndexingWorkerManager,
                    )

                    chunk_store_obj = ChunkStore(k_store)
                    indexing_store_obj = IndexingStore(k_store)
                    indexing_orchestrator_obj = IndexingOrchestrator(
                        knowledge_store=k_store,
                        knowledge_file_store=k_file_store,
                        chunk_store=chunk_store_obj,
                        indexing_store=indexing_store_obj,
                    )
                    indexing_manager = IndexingWorkerManager(
                        knowledge_store=k_store,
                        chunk_store=chunk_store_obj,
                        indexing_store=indexing_store_obj,
                        orchestrator=indexing_orchestrator_obj,
                    )
                    await indexing_manager.start()
                    state.indexing_worker_manager = indexing_manager
                except Exception as e:
                    raise RuntimeError(
                        f"indexing worker manager start failed: {type(e).__name__}"
                    ) from e

            # ============================================================
            # P2-R4-B2: search_knowledge Agent Tool — Session-scoped
            # FTS5 retrieval via SearchKnowledgeService. Registered only
            # when Knowledge subsystem + [rag] extra are available.
            # ============================================================
            if ingestion_manager is not None:
                try:
                    from .knowledge.chunk_store import ChunkStore as _CS
                    from .knowledge.evidence import EvidenceRegistry as _ER
                    from .knowledge.search_service import (
                        SearchKnowledgeService as _SKS,
                    )
                    from .knowledge.search_tool import (
                        create_search_knowledge_tool,
                    )

                    _chunk_store_for_search = _CS(k_store)
                    _search_service = _SKS(
                        knowledge_store=k_store,
                        chunk_store=_chunk_store_for_search,
                    )

                    def _evidence_registry_getter() -> Any:
                        if state._evidence_registry is None:
                            state._evidence_registry = _ER()
                        return state._evidence_registry

                    _search_tool = create_search_knowledge_tool(
                        search_service=_search_service,
                        session_id_getter=_session_id_getter,
                        evidence_registry_getter=_evidence_registry_getter,
                    )
                    if not harness.agent.tools.has("search_knowledge"):
                        harness.agent.tools.register(_search_tool)
                except Exception as e:
                    raise RuntimeError(
                        f"search_knowledge tool init failed: {type(e).__name__}"
                    ) from e
        else:
            state.knowledge_service = None
            state.knowledge_store = None
            state.knowledge_file_store = None
            state.ingestion_worker_manager = None
            state.indexing_worker_manager = None

        @asynccontextmanager
        async def _wiki_runtime_context() -> AsyncIterator[None]:
            if wiki_root is None:
                state.wiki_store = None
                state.wiki_ingestion_service = None
                state.wiki_ingestion_worker = None
                state.wiki_summary_service = None
                state.wiki_entry_page_service = None
                state.wiki_change_set_service = None
                state.wiki_conversation_service = None
                state.wiki_knowledge_tools = None
                yield
                return
            from .wiki import WikiIngestionService, WikiStore
            from .wiki.changes import WikiChangeSetService
            from .wiki.conversations import WikiConversationService
            from .wiki.knowledge_agent import build_knowledge_tool_registry
            from .wiki.pages import WikiEntryPageService
            from .wiki.summary import CoreAgentWikiSummaryAgent, WikiSummaryService
            from .wiki.worker import WikiIngestionWorkerManager

            wiki_store = await WikiStore.open(wiki_root, legacy_policy="preserve")
            wiki_service = WikiIngestionService(
                wiki_store,
                pdf_provider=wiki_pdf_provider,
                pdf_provider_v2=wiki_pdf_provider_v2,
            )
            wiki_worker = WikiIngestionWorkerManager(
                store=wiki_store,
                service=wiki_service,
                source_retention_seconds=wiki_source_retention_seconds,
                purge_interval_seconds=wiki_source_purge_interval_seconds,
            )
            summary_agent = wiki_summary_agent or CoreAgentWikiSummaryAgent(
                lambda: harness.agent.client
            )
            wiki_summary_service = WikiSummaryService(wiki_store, summary_agent)
            wiki_entry_page_service = WikiEntryPageService(wiki_store)
            wiki_change_set_service = WikiChangeSetService(wiki_store)
            wiki_conversation_service = WikiConversationService(
                wiki_store,
                session_store=session_store,
                file_store=state.file_store,
                provider_config_runtime=getattr(
                    _app.state,
                    "provider_config_runtime",
                    None,
                ),
            )
            wiki_knowledge_tools = build_knowledge_tool_registry(
                wiki_store,
                wiki_change_set_service,
            )
            try:
                await wiki_worker.start()
            except BaseException:
                await wiki_store.close()
                raise
            state.wiki_store = wiki_store
            state.wiki_ingestion_service = wiki_service
            state.wiki_ingestion_worker = wiki_worker
            state.wiki_source_retention_ms = int(wiki_source_retention_seconds * 1000)
            state.wiki_summary_service = wiki_summary_service
            state.wiki_entry_page_service = wiki_entry_page_service
            state.wiki_change_set_service = wiki_change_set_service
            state.wiki_conversation_service = wiki_conversation_service
            state.wiki_knowledge_tools = wiki_knowledge_tools
            try:
                yield
            finally:
                state.wiki_ingestion_worker = None
                state.wiki_summary_service = None
                state.wiki_entry_page_service = None
                state.wiki_change_set_service = None
                state.wiki_conversation_service = None
                state.wiki_knowledge_tools = None
                try:
                    await wiki_worker.stop()
                finally:
                    state.wiki_ingestion_service = None
                    state.wiki_store = None
                    await wiki_store.close()

        # ====================================================================
        # P1-E1-4A: Credential Runtime Composition Root
        # 仅在文件型 DB 路径 + 显式 / 默认 enable 时启动；独立 connection
        # 与 session/extension store 共享 DB 文件但生命周期独立
        # ====================================================================
        from .credentials.runtime import (
            build_credential_runtime_config,
            credential_runtime_context,
        )
        from .local_web_security import default_web_security_config

        cred_runtime_cm = None
        if _cred_resolved.runtime_enabled:
            try:
                cred_cfg = build_credential_runtime_config(
                    database_path=str(db_path),
                    secret_backend_mode=credential_secret_backend,
                    web_security=default_web_security_config(
                        extra_hosts=credential_extra_hosts,
                        extra_ui_origins=credential_extra_ui_origins,
                    ),
                )
                cred_runtime_cm = credential_runtime_context(cred_cfg)
            except Exception as e:
                # 配置错误——拒绝启动（不静默降级）
                await _close_startup_sqlite_stores()
                raise RuntimeError(f"credential runtime config error: {type(e).__name__}") from e

        if cred_runtime_cm is not None:
            _app.state.credential_runtime = await cred_runtime_cm.__aenter__()
            try:

                async def _session_exists_cb(session_id: str) -> bool:
                    if state.session_store is None:
                        return False
                    try:
                        session = await state.session_store.get_session(session_id)
                    except Exception:
                        return False
                    return session is not None

                sandbox_runtime_cm = None
                if _sandbox_resolved.runtime_enabled:
                    from coding_agent_app.sandbox_workspace import (
                        WorkspaceSandboxArtifactPublisher,
                        WorkspaceSandboxBaselineProvider,
                    )
                    from coding_sandbox import HMACSHA256ArtifactSigner

                    from .coding_sandbox.runtime import sandbox_runtime_context

                    database_path = await asyncio.to_thread(
                        Path(str(db_path)).resolve,
                        strict=False,
                    )
                    artifact_signer = coding_sandbox_artifact_signer
                    if artifact_signer is None:
                        artifact_signer = HMACSHA256ArtifactSigner(
                            key_id="web-runtime-v1",
                            secret=uuid4().bytes + uuid4().bytes,
                        )

                    async def _sandbox_event_sink(event: Any) -> None:
                        await _emit_web_payload(
                            {
                                "type": event.event_type,
                                "operation_id": event.operation_id,
                                "operation_sequence": event.sequence,
                                **event.payload,
                            },
                            f"sandbox:{event.operation_id}",
                            event.session_id,
                        )
                        if event.event_type in {
                            "sandbox_publish_finished",
                            "sandbox_operation_cancelled",
                            "sandbox_operation_discarded",
                        } and state.plan_store is not None:
                            try:
                                plan = await state.plan_store.find_by_sandbox_operation(
                                    event.operation_id
                                )
                                if (
                                    plan is not None
                                    and event.event_type == "sandbox_publish_finished"
                                    and plan.status == "awaiting_artifact_approval"
                                ):
                                    plan = await state.plan_store.mark_completed(plan.id)
                                    await _emit_web_payload(
                                        {
                                            "type": "plan_completed",
                                            "plan": plan.model_dump(mode="json"),
                                        },
                                        None,
                                        plan.session_id,
                                    )
                                elif (
                                    plan is not None
                                    and plan.status == "awaiting_artifact_approval"
                                ):
                                    plan = await state.plan_store.finish(
                                        plan.id,
                                        "cancelled",
                                        failure_code="artifact_not_published",
                                    )
                                    await _emit_web_payload(
                                        {
                                            "type": "plan_cancelled",
                                            "plan": plan.model_dump(mode="json"),
                                        },
                                        None,
                                        plan.session_id,
                                    )
                            except Exception:
                                _logger.warning(
                                    "Plan state did not follow Sandbox terminal event "
                                    "(operation_id=%s)",
                                    event.operation_id,
                                )
                        if event.event_type == "sandbox_publish_finished":
                            workspace_revision = event.payload.get("workspace_revision")
                            raw_changed_paths = event.payload.get("changed_paths", [])
                            raw_deleted_paths = event.payload.get("deleted_paths", [])
                            changed_paths = tuple(
                                path for path in raw_changed_paths if isinstance(path, str)
                            )
                            deleted_paths = tuple(
                                path for path in raw_deleted_paths if isinstance(path, str)
                            )
                            if isinstance(workspace_revision, int):
                                await _emit_web_payload(
                                    {
                                        "type": "workspace_changed",
                                        "source": "coding_sandbox",
                                        "operation_id": event.operation_id,
                                        "workspace_revision": workspace_revision,
                                        "changed_paths": list(changed_paths),
                                        "deleted_paths": list(deleted_paths),
                                    },
                                    f"workspace:{event.operation_id}",
                                    event.session_id,
                                )
                                validation: dict[str, Any] | None = None
                                runtime = getattr(
                                    _app.state,
                                    "coding_sandbox_runtime",
                                    None,
                                )
                                lifecycle = None if runtime is None else runtime.lifecycle
                                if lifecycle is not None:
                                    try:
                                        published = await lifecycle.get(event.operation_id)
                                    except Exception:
                                        published = None
                                    if published is not None and published.validation is not None:
                                        validation = published.validation.model_dump(mode="json")
                                await _refresh_code_continuity(
                                    event.session_id,
                                    trigger="workspace_published",
                                    changed_paths=changed_paths,
                                    deleted_paths=deleted_paths,
                                    validation=validation,
                                    operation_id=event.operation_id,
                                )
                        if event.event_type in {
                            "sandbox_publish_finished",
                            "sandbox_operation_cancelled",
                            "sandbox_operation_discarded",
                            "sandbox_operation_failed",
                            "sandbox_operation_interrupted",
                        }:
                            _schedule_auto_memory_resume(event.session_id)

                    sandbox_staging_root = database_path.parent / "coding-sandbox-staging"
                    sandbox_projects_root: Path | None = (
                        database_path.parent / "coding-sandbox-projects"
                    )
                    sandbox_baseline_provider = None
                    sandbox_artifact_publisher = None
                    if state.file_store is not None:
                        sandbox_projects_root = None
                        sandbox_baseline_provider = WorkspaceSandboxBaselineProvider(
                            state.file_store,
                            materialization_root=(
                                sandbox_staging_root / "workspace-materializations"
                            ),
                        )
                        sandbox_artifact_publisher = WorkspaceSandboxArtifactPublisher(
                            state.file_store,
                            staging_root=(sandbox_staging_root / "workspace-publishes"),
                        )

                    sandbox_runtime_cm = sandbox_runtime_context(
                        database_path=str(db_path),
                        credential_service=_app.state.credential_runtime.service,
                        backend_factory=coding_sandbox_backend_factory,
                        artifact_signer=artifact_signer,
                        session_exists=_session_exists_cb,
                        projects_root=sandbox_projects_root,
                        baseline_provider=sandbox_baseline_provider,
                        artifact_publisher=sandbox_artifact_publisher,
                        publisher_state_root=(database_path.parent / "coding-sandbox-publisher"),
                        staging_root=sandbox_staging_root,
                        event_sink=_sandbox_event_sink,
                    )
                    _app.state.coding_sandbox_runtime = await sandbox_runtime_cm.__aenter__()
                    lifecycle = _app.state.coding_sandbox_runtime.lifecycle
                    if lifecycle is not None:
                        from ..tools import (
                            create_coding_sandbox_tools,
                            create_coding_validation_tool,
                        )

                        def _coding_workspace() -> Any:
                            session_id = (
                                tool_session_context.get() or state.current_request_session_id
                            )
                            return lifecycle.workspace_for_session(session_id)

                        coding_tools = create_coding_sandbox_tools(
                            workspace_getter=_coding_workspace
                        )
                        coding_tools.append(
                            create_coding_validation_tool(workspace_getter=_coding_workspace)
                        )
                        registered_names: set[str] = container["coding_sandbox_tool_names"]
                        for coding_tool in coding_tools:
                            if not harness.agent.tools.has(coding_tool.name):
                                harness.agent.tools.register(coding_tool)
                                registered_names.add(coding_tool.name)

                # P1-E2-3B1: Provider Config runtime nested inside credential runtime.
                # Depends on CredentialService (safe API) — must init AFTER credential
                # runtime entered, shutdown BEFORE credential runtime exits.
                if _pc_resolved.runtime_enabled:
                    from .providers.config_runtime import (
                        provider_config_runtime_context,
                    )

                    pc_runtime_cm = provider_config_runtime_context(
                        database_path=str(db_path),
                        credential_service=_app.state.credential_runtime.service,
                        session_exists=_session_exists_cb,
                    )
                    _app.state.provider_config_runtime = await pc_runtime_cm.__aenter__()
                    # M1-5: 构造 RequestProviderRuntime——仅在 Credential + Provider
                    # Config 两个 runtime 都启动时. 无独立 lifespan——纯 Python 对象
                    # 无长期网络资源. Prompt 路径通过 app.state.request_provider_runtime
                    # 读取；为 None 时 _execute_prompt 走 legacy client 兼容路径.
                    from ..providers.factory import create_provider
                    from ..providers.registry import _DEFAULT_REGISTRY
                    from .providers.runtime import RequestProviderRuntime

                    _app.state.request_provider_runtime = RequestProviderRuntime(
                        provider_config_service=_app.state.provider_config_runtime.service,
                        credential_service=_app.state.credential_runtime.service,
                        provider_registry=_DEFAULT_REGISTRY,
                        provider_factory=create_provider,
                    )
                    try:
                        async with _wiki_runtime_context():
                            yield
                    finally:
                        _app.state.provider_config_runtime = None
                        _app.state.request_provider_runtime = None
                        try:
                            await pc_runtime_cm.__aexit__(None, None, None)
                        except Exception:
                            pass
                else:
                    _app.state.provider_config_runtime = None
                    _app.state.request_provider_runtime = None
                    async with _wiki_runtime_context():
                        yield
            finally:
                registered_names = container["coding_sandbox_tool_names"]
                for tool_name in tuple(registered_names):
                    harness.agent.tools.unregister(tool_name)
                registered_names.clear()
                _app.state.coding_sandbox_runtime = None
                if sandbox_runtime_cm is not None:
                    try:
                        await sandbox_runtime_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                _app.state.credential_runtime = None
                _app.state.request_provider_runtime = None
                try:
                    await cred_runtime_cm.__aexit__(None, None, None)
                except Exception:
                    pass
        else:
            _app.state.credential_runtime = None
            _app.state.coding_sandbox_runtime = None
            _app.state.provider_config_runtime = None
            _app.state.request_provider_runtime = None
            async with _wiki_runtime_context():
                yield

        # ====================================================================
        # P1-B1: shutdown 收敛——先收敛 active request，再走原清理流程
        # ====================================================================
        # 1. 拒绝新 async prompt（POST /api/prompt/async 看到 shutting_down=True 返回 503）
        state.shutting_down = True
        await approval_manager.cancel_all()

        # 2. 收集所有 active request 的 task——abort queued（cancel）+ abort running（harness.abort）
        #    用 list 快照——_abort_request_internal 会修改 state.active_requests
        active_reqs = list(state.active_requests.values())
        active_tasks: list[asyncio.Task[Any]] = []
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

        continuity_tasks: set[asyncio.Task[Any]] = container["continuity_tasks"]
        pending_continuity_tasks = [task for task in continuity_tasks if not task.done()]
        if pending_continuity_tasks:
            for task in pending_continuity_tasks:
                task.cancel()
            await asyncio.gather(*pending_continuity_tasks, return_exceptions=True)

        # ====================================================================
        # 原清理流程
        # ====================================================================
        # shutdown：精确移除自己注册的 hook，避免累积
        hook = container.get("hook")
        if hook is not None:
            harness.remove_on_event_hook(hook)
            container["hook"] = None
        if harness.agent.tool_approval_handler is _web_tool_approval_handler:
            harness.set_tool_approval_handler(previous_tool_approval_handler)
        if harness.agent.before_model_call is _web_before_model_call:
            harness.agent.before_model_call = previous_before_model_call
        # 通知 SSE / WS client 关闭（不抛错）
        # container 里两个都是 set——用 set 联合（list | list 不合法）
        all_clients = set(container["sse_clients"])
        all_clients.update(container["ws_clients"])
        for q in all_clients:
            try:
                q.put_nowait({"type": "shutdown"})
            except asyncio.QueueFull:
                pass
        # P1-C4: detach 所有 attached MCP servers——单个失败不阻塞其他
        try:
            await harness.detach_mcp_servers()
        except Exception:
            pass  # 单个 detach 失败不阻塞 shutdown

        # 关闭 SQLiteSessionStore
        if state.session_store is not None:
            try:
                await state.session_store.close()
            except Exception:
                pass
        # P1-C2: 关闭 extension_store（injected connection 不 close——由 session_store 负责）
        if state.extension_store is not None:
            try:
                await state.extension_store.close()
            except Exception:
                pass
        # P2-R2-C2: 关闭 Ingestion Worker Manager（在 KnowledgeStore.close 之前）
        # Manager.stop() 会触发 startup recovery（已 done）+ graceful shutdown
        # + parser.close(). 必须在 KnowledgeStore.close() 之前完成，否则
        # Manager 的 worker_loop 会访问已关闭的 connection.
        ingestion_mgr = (
            state.ingestion_worker_manager if hasattr(state, "ingestion_worker_manager") else None
        )
        if ingestion_mgr is not None:
            try:
                await ingestion_mgr.stop()
            except Exception:
                pass
            state.ingestion_worker_manager = None
        # P2-R3-D2: 关闭 Indexing Worker Manager（在 Ingestion Worker stop 之后，
        # KnowledgeStore.close 之前）。Per directive §28: producer stops
        # first to prevent new normalizing during Index Worker drain.
        indexing_mgr = (
            state.indexing_worker_manager if hasattr(state, "indexing_worker_manager") else None
        )
        if indexing_mgr is not None:
            try:
                await indexing_mgr.stop()
            except Exception:
                pass
            state.indexing_worker_manager = None
        # P2-R1: 关闭 KnowledgeStore（独立 connection）
        knowledge_store_to_close = (
            state.knowledge_store if hasattr(state, "knowledge_store") else None
        )
        if knowledge_store_to_close is not None:
            try:
                await knowledge_store_to_close.close()
            except Exception:
                pass
        # WorkspaceStore 不需要 close（纯文件 IO），保留目录给后续进程用

    app = FastAPI(
        title="pi-agent-core-py · Trace Viewer",
        description=(
            "Local-only development UI for inspecting Agent runtime state. DO NOT expose publicly."
        ),
        version=__version__,
        lifespan=_lifespan,
    )

    state = WebAppState(
        harness=harness,
        auto_memory_enabled=enable_auto_memory,
        code_continuity_enabled=enable_code_continuity,
        intent_routing_enabled=enable_intent_routing,
        plan_mode_enabled=enable_plan_mode,
    )
    checkpointer_locks: dict[str, asyncio.Lock] = {}
    continuity_locks: dict[str, asyncio.Lock] = {}
    deleting_session_ids: set[str] = set()
    # 用入参覆盖默认 maxlen
    state.event_buffer = type(state.event_buffer)(max_size=event_buffer_max_size)
    # P1-B1: request_history deque 的 maxlen 也用入参覆盖
    state.request_history = deque(maxlen=request_history_maxlen)
    state.request_history_maxlen = request_history_maxlen
    # P1-C2: skill_mutation_lock 在 state 创建时立即 init（不依赖 lifespan）
    state.skill_mutation_lock = asyncio.Lock()
    # P1-C3: mcp_mutation_lock 同理
    state.mcp_mutation_lock = asyncio.Lock()
    app.state.web = state
    app.state.allow_prompt_preview = allow_prompt_preview
    app.state.event_buffer_max_size = event_buffer_max_size
    # P1-E1-4A: credential_runtime placeholder——lifespan 启动时填入
    app.state.credential_runtime = None
    app.state.coding_sandbox_runtime = None
    # P1-E2-3B1: provider_config_runtime placeholder——lifespan 启动时填入
    app.state.provider_config_runtime = None
    # M1-5: request_provider_runtime placeholder——仅 cred + pc runtime 都启用时填入
    app.state.request_provider_runtime = None
    from .source_offer import (
        SourceOfferError,
        build_about_router,
        build_source_offer_service,
    )

    try:
        source_offer_service = build_source_offer_service(wiki_parser_worker_source_root)
    except SourceOfferError as exc:
        raise RuntimeError(
            "wiki parser Worker Corresponding Source configuration is invalid"
        ) from exc
    app.state.wiki_parser_source_offer = source_offer_service
    app.include_router(build_about_router(source_offer_service))
    # P1-E1-4A / B3: TrustedHost middleware（resolve 后的 flag 决定）
    # 默认 False 保留所有既有 create_app 调用点不变；启用 Credentials API
    # 时强制 True（已在 resolve_credential_api_configuration 中校验）
    #
    # Middleware 顺序（P1-E1-5B / MEDIUM-1 修复）：
    # Starlette add_middleware 用 insert(0,...)——last add = outermost。
    # 必须先 add BodyLimit（inner），再 add TrustedHost（outer），这样
    # TrustedHost 在最外层——非法 Host 在 CredentialBodyLimit 读取请求体
    # 前被拒绝。两个 if 块按 TrustedHost / BodyLimit 各自条件独立 add，
    # 但通过统一的 _pending_middlewares 列表收集后按 TrustedHost 后 add
    # 的顺序 flush，确保跨 if 块的顺序也正确。
    _pending_middlewares: list[tuple[Any, dict[str, Any]]] = []

    if _cred_resolved.trusted_host_enabled:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        from .local_web_security import default_web_security_config

        _ws_cfg = default_web_security_config(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        _pending_middlewares.append(
            (
                TrustedHostMiddleware,
                {"allowed_hosts": list(_ws_cfg.allowed_hosts)},
            )
        )

    # P1-E1-4B3: Credential REST API（8 endpoints）+ 32 KiB body limit
    # 仅在 API enabled 时 mount——Router 自带 X-PI-Agent-UI / Origin 强制
    if _cred_resolved.api_enabled:
        from .credentials.api import (
            CredentialBodyLimitMiddleware,
            build_full_credential_router,
        )
        from .local_web_security import default_web_security_config as _dws

        _cred_ws_cfg = _dws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        # Mount Credential Router——security deps 由 router 自带
        app.include_router(build_full_credential_router(_cred_ws_cfg))
        # Body limit middleware——insert at HEAD of pending list so it's
        # flushed FIRST (innermost); TrustedHost (already in list) is
        # flushed LAST (outermost) per Starlette's insert(0, ...) semantics.
        _pending_middlewares.insert(
            0,
            (
                CredentialBodyLimitMiddleware,
                {"max_bytes": _cred_ws_cfg.max_request_body_bytes},
            ),
        )

    # P1-E2-3B2: Provider Profiles REST API（7 endpoints）
    # 与 Credential API 共用 WebSecurityConfig；Router 复用 E1 安全 deps
    if _pc_resolved.api_enabled:
        from .local_web_security import default_web_security_config as _pc_ws
        from .providers.api import (
            ProviderProfileBodyLimitMiddleware,
            build_full_provider_profile_router,
        )

        _pc_ws_cfg = _pc_ws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        app.include_router(build_full_provider_profile_router(_pc_ws_cfg))
        # Body limit middleware—reuses CredentialBodyLimitMiddleware via subclass
        # with Provider-Profile-specific path predicate. Flushed innermost.
        _pending_middlewares.insert(
            0,
            (
                ProviderProfileBodyLimitMiddleware,
                {"max_bytes": _pc_ws_cfg.max_request_body_bytes},
            ),
        )

    if _sandbox_resolved.api_enabled:
        from .coding_sandbox.api import (
            SandboxBodyLimitMiddleware,
            build_coding_sandbox_router,
        )
        from .local_web_security import default_web_security_config as _sandbox_ws

        _sandbox_ws_cfg = _sandbox_ws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        app.include_router(build_coding_sandbox_router(_sandbox_ws_cfg))
        _pending_middlewares.insert(
            0,
            (
                SandboxBodyLimitMiddleware,
                {"max_bytes": _sandbox_ws_cfg.max_request_body_bytes},
            ),
        )

    # Deprecated Chunk Knowledge REST API. Explicit compatibility opt-in only.
    if _legacy_knowledge_enabled:
        assert knowledge_root is not None
        from .knowledge.api import (
            build_knowledge_router,
            build_session_knowledge_router,
        )
        from .local_web_security import default_web_security_config as _k_ws

        _k_ws_cfg = _k_ws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        app.include_router(build_knowledge_router(_k_ws_cfg), prefix="/api/knowledge")
        app.include_router(build_session_knowledge_router(_k_ws_cfg), prefix="/api/sessions")

    if enable_wiki_api is True and wiki_root is None:
        raise RuntimeError("wiki API requires wiki_root")
    if wiki_root is not None:
        from .local_web_security import default_web_security_config as _wiki_ws
        from .wiki.api import build_wiki_router

        _wiki_ws_cfg = _wiki_ws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        _wiki_api_enabled = (
            enable_wiki_api if enable_wiki_api is not None else _cred_resolved.trusted_host_enabled
        )
        if _wiki_api_enabled:
            app.include_router(build_wiki_router(_wiki_ws_cfg), prefix="/api/wiki")

    # Flush in list order: each add_middleware does insert(0, ...).
    # After flushing [BodyLimit, TrustedHost] in order:
    #   user_middleware == [TrustedHost, BodyLimit]
    #   build_middleware_stack iterates reversed → [BodyLimit, TrustedHost]
    #   final wrap: router → BodyLimit(router) → TrustedHost(BodyLimit(router))
    #   call order: TrustedHost first (outermost), then BodyLimit, then router.
    for cls, kwargs in _pending_middlewares:
        app.add_middleware(cls, **kwargs)

    # SSE 客户端队列集合——每个 SSE 连接独立 asyncio.Queue；
    # on_event hook 把 event 广播（put_nowait）到所有客户端队列
    sse_clients: set[asyncio.Queue[dict[str, Any]]] = container["sse_clients"]
    # WebSocket 客户端队列集合——同 SSE，独立 queue 池
    ws_clients: set[asyncio.Queue[dict[str, Any]]] = container["ws_clients"]

    async def _emit_web_payload(
        payload: dict[str, Any],
        request_id: str | None,
        session_id: str | None,
    ) -> None:
        """Assign one envelope sequence and broadcast a JSON-safe Web event."""
        safe_payload = dict(payload)
        safe_payload.setdefault("_received_at_ms", int(time.time() * 1000))
        sequence = state.next_event_sequence
        state.next_event_sequence = sequence + 1
        envelope = {
            "event_id": f"evt_{uuid4().hex[:16]}",
            "request_id": request_id,
            "session_id": session_id,
            "sequence": sequence,
            "type": safe_payload.get("type") or "unknown",
            "timestamp": _now_utc().isoformat(),
            "payload": safe_payload,
        }
        state.event_buffer.append(envelope)
        for q in list(sse_clients) + list(ws_clients):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                continue

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
            payload.setdefault("type", type(event).__name__)
            await _emit_web_payload(
                payload,
                state.current_request_id,
                state.current_request_session_id,
            )
        except Exception:
            return

    approval_manager = ToolApprovalManager(_emit_web_payload)
    app.state.tool_approval_manager = approval_manager
    previous_tool_approval_handler = harness.tool_approval_handler

    async def _web_tool_approval_handler(context: Any) -> bool:
        request_id = state.current_request_id
        if request_id is None:
            return False
        request_record = state.active_requests.get(request_id)
        if request_record is None:
            return False
        return await approval_manager.request_approval(
            request_id=request_id,
            session_id=request_record.session_id,
            context=context,
        )

    # Respect an explicitly configured host handler.  The Web UI fills only the
    # missing interaction layer and restores it during dispose/shutdown.
    if previous_tool_approval_handler is None:
        harness.set_tool_approval_handler(_web_tool_approval_handler)
    app.state.web_tool_approval_handler = _web_tool_approval_handler
    app.state.previous_tool_approval_handler = previous_tool_approval_handler

    from ..context_budget import ContextEstimate, estimate_context
    from ..loop import ModelCallDecision

    previous_before_model_call = harness.agent.before_model_call

    async def _web_before_model_call(context: Any) -> Any:
        if previous_before_model_call is not None:
            previous_result = previous_before_model_call(context)
            if inspect.isawaitable(previous_result):
                previous_result = await previous_result
            if previous_result is False or (
                isinstance(previous_result, ModelCallDecision) and not previous_result.allow
            ):
                return previous_result

        capability_store = state.model_capability_store
        capabilities = None
        if capability_store is not None:
            capabilities = await capability_store.resolve(
                context.client.provider_id or "legacy",
                getattr(context.client, "model", "unknown") or "unknown",
            )
        estimate = estimate_context(
            system_prompt=context.system_prompt,
            messages=context.messages,
            tools=context.tools,
            context_window=(capabilities.context_window if capabilities else None),
            reserved_output_tokens=(capabilities.max_output_tokens if capabilities else None),
        )
        await _emit_web_payload(
            {
                "type": "context_budget_updated",
                "turn_index": context.turn_index,
                "provider_id": context.client.provider_id or "legacy",
                "model_id": getattr(context.client, "model", "unknown") or "unknown",
                "capability_source": capabilities.source if capabilities else "unknown",
                "estimate": estimate.to_dict(),
            },
            state.current_request_id,
            state.current_request_session_id,
        )
        if not estimate.can_send:
            return ModelCallDecision(
                allow=False,
                error_message=("context budget exceeded; compact the session before continuing"),
            )
        return ModelCallDecision()

    harness.agent.before_model_call = _web_before_model_call
    app.state.web_before_model_call = _web_before_model_call
    app.state.previous_before_model_call = previous_before_model_call

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
        phase: str
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
    # P1-C2: Skill persistence helpers
    # ========================================================================

    async def _restore_uploaded_skills(ext_store: Any) -> None:
        """启动时恢复 uploaded Skills——逐行隔离 + sha256 校验 + model_validate。

        **规则**（用户原指令 C2 §6/§7）：
        - filesystem/built-in Skill 优先——同名冲突不覆盖，记录 restore_error
        - 单行损坏（JSON / hash / model_validate）记录 restore_error + 跳过
        - 成功恢复清除 last_restore_error
        - 不信任 Markdown 自声明的 source_kind——服务端强制覆盖
        - 错误不含 prompt / raw markdown / 绝对路径
        """
        import hashlib
        import json as _json

        from ..skills import Skill, SkillRegistrationError
        from .extension_store import ExtensionSQLiteStore

        registry = harness.skill_registry
        if registry is None:
            return

        rows = await ext_store.list_uploaded_skill_rows()
        for row in rows:
            result = ExtensionSQLiteStore.decode_uploaded_skill(row)
            name = row["name"] if "name" in row.keys() else "<unknown>"

            if result.error is not None or result.skill is None:
                await ext_store.set_skill_restore_error(name, result.error or "decode failed")
                continue

            persisted = result.skill

            # sha256 校验 raw_markdown
            if persisted.content_sha256:
                actual_sha = hashlib.sha256(persisted.raw_markdown.encode("utf-8")).hexdigest()
                if actual_sha != persisted.content_sha256:
                    await ext_store.set_skill_restore_error(name, "content hash mismatch")
                    continue

            # decode skill_json + model_validate
            try:
                skill_data = _json.loads(persisted.skill_json)
                skill = Skill.model_validate(skill_data)
            except Exception as e:
                await ext_store.set_skill_restore_error(name, f"decode failed: {type(e).__name__}")
                continue

            # 服务端强制覆盖 source metadata（不信任 Markdown 自声明）
            skill.metadata = skill.metadata or {}
            skill.metadata["source_kind"] = "upload"
            skill.metadata["persisted"] = True

            # 同名冲突——filesystem/built-in 优先
            if registry.has(name):
                await ext_store.set_skill_restore_error(name, "name conflict with existing skill")
                continue

            # register + apply enabled
            try:
                registry.register(skill)
                if persisted.enabled:
                    registry.enable(name)
                else:
                    registry.disable(name)
            except SkillRegistrationError as e:
                await ext_store.set_skill_restore_error(
                    name, f"register failed: {type(e).__name__}"
                )
                continue

            # 成功恢复——清除 last_restore_error
            await ext_store.set_skill_restore_error(name, None)

    async def _is_uploaded_skill(name: str) -> bool:
        """检查 Skill 是否为上传来源——以 DB row 为准（不信任 metadata）。

        **规则**（用户原指令 C2 §4）：非 uploaded Skill 的 enable/disable 不写 DB。
        """
        if state.extension_store is None:
            return False
        try:
            return await state.extension_store.get_uploaded_skill(name) is not None
        except Exception:
            return False

    # ========================================================================
    # P1-C4: MCP startup restore + env resolution + failure isolation
    # ========================================================================

    def _resolve_mcp_env(
        env_keys: list[str],
    ) -> tuple[dict[str, str], list[str]]:
        """从 os.environ 解析 env values。返回 (resolved_env, missing_keys)。

        **安全**：只读 os.environ；missing 时返回 key name（不含 value）。
        """
        import os

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for key in env_keys:
            val = os.environ.get(key)
            if val is not None:
                resolved[key] = val
            else:
                missing.append(key)
        return resolved, missing

    def _safe_extension_error(
        exc: BaseException,
        secret_values: list[str] | None = None,
    ) -> str:
        """安全错误摘要——替换 secret values / 不返回 traceback / 限长 500。

        P1-C5 修正：过滤空字符串 + 按长度降序替换（避免短 secret 破坏长 secret 匹配）。
        """
        msg = f"{type(exc).__name__}: {exc}"
        # 过滤空字符串 + 按长度降序（长 secret 先替换，避免子串问题）
        non_empty = sorted(
            [v for v in (secret_values or []) if v],
            key=len,
            reverse=True,
        )
        for val in non_empty:
            msg = msg.replace(val, "***")
        return msg[:500]

    async def _prepare_builtin_ddgs(ext_store: Any) -> Any:
        """Create/normalize the reserved DDGS row before MCP restoration."""

        import sys

        from ..mcp.ddgs_server import (
            DDGS_SERVER_NAME,
            DDGSSearchSettings,
            build_ddgs_server_args,
            settings_from_ddgs_server_args,
        )

        persisted = await ext_store.get_mcp_server(DDGS_SERVER_NAME)
        desired_enabled = persisted.desired_enabled if persisted is not None else False
        settings = DDGSSearchSettings()
        if persisted is not None:
            try:
                stored_args = json.loads(persisted.args_json)
                if isinstance(stored_args, list) and all(
                    isinstance(item, str) for item in stored_args
                ):
                    settings = settings_from_ddgs_server_args(stored_args)
            except (TypeError, json.JSONDecodeError):
                settings = DDGSSearchSettings()

        await ext_store.upsert_mcp_server(
            name=DDGS_SERVER_NAME,
            transport="stdio",
            command=sys.executable,
            args=build_ddgs_server_args(settings),
            desired_enabled=desired_enabled,
            env_keys=[],
        )
        return settings

    def _mark_builtin_ddgs_runtime(settings: Any) -> None:
        """Attach immutable built-in metadata after the normal restore pass."""

        from ..mcp.ddgs_server import DDGS_SERVER_NAME

        cfg = state.mcp_server_configs.get(DDGS_SERVER_NAME)
        if cfg is None:
            return
        cfg.builtin = True
        cfg.deletable = False
        cfg.settings = settings.model_dump(mode="json")

    def _mcp_request_timeout(cfg: WebMCPServerConfig) -> float:
        """Allow DDGS network timeout plus protocol/process overhead."""

        if enable_builtin_ddgs and cfg.name == "ddgs":
            from ..mcp.ddgs_server import settings_from_ddgs_server_args

            settings = settings_from_ddgs_server_args(cfg.args)
            return float(settings.timeout_seconds + 5)
        return 10.0

    async def _restore_mcp_servers(ext_store: Any) -> None:
        """P1-C4: 启动时恢复 MCP server 配置 + auto attach + apply disabled tools。

        **顺序**（用户原指令 C4 §1）：
        1. 读取所有 MCP server rows（逐行隔离）
        2. decode + 校验 args_json / env_keys_json
        3. 写入 WebAppState.mcp_server_configs（env 从 os.environ 解析）
        4. desired_enabled=true → resolve env → auto attach (timeout 10s)
        5. attach 成功 → apply disabled tools
        6. missing env → 不 attach，记录 missing keys
        7. attach 失败 → desired_enabled 仍 true，记录 error
        8. 成功恢复清除 last_restore_error

        **失败隔离**：单 server 损坏 / timeout / attach 失败不阻塞其他 server。
        """
        import json as _json

        from ..mcp import MCPServerConfig
        from .extension_store import ExtensionSQLiteStore

        rows = await ext_store.list_mcp_server_rows()
        for row in rows:
            result = ExtensionSQLiteStore.decode_mcp_server(row)
            name = row["name"] if "name" in row.keys() else "<unknown>"

            if result.error is not None or result.server is None:
                await ext_store.set_mcp_restore_error(name, result.error or "decode failed")
                continue

            persisted = result.server

            # decode args / env_keys
            try:
                args = _json.loads(persisted.args_json)
                env_keys = _json.loads(persisted.env_keys_json)
            except _json.JSONDecodeError as e:
                await ext_store.set_mcp_restore_error(
                    name, f"json decode failed: {type(e).__name__}"
                )
                continue

            # 写入 runtime config（env 暂空——从 os.environ 解析后填入）
            cfg = WebMCPServerConfig(
                name=name,
                command=persisted.command,
                args=args,
                env={},
                enabled=persisted.desired_enabled,
                attached=False,
                last_error=None,
                tool_count=0,
            )
            state.mcp_server_configs[name] = cfg

            if not persisted.desired_enabled:
                # desired_enabled=false → 只恢复配置，不 attach
                cfg.restore_status = "not_requested"
                await ext_store.set_mcp_restore_error(name, None)
                continue

            # desired_enabled=true → resolve env
            resolved_env, missing = _resolve_mcp_env(env_keys)
            if missing:
                # missing env → 不 attach；结构化记录 key name（不含 value）
                cfg.restore_status = "needs_env"
                cfg.missing_env_keys = missing
                cfg.last_error = None  # 不把预期配置问题显示成系统异常
                await ext_store.set_mcp_restore_error(name, None)
                continue

            cfg.env = resolved_env

            # auto attach with timeout
            try:
                mcp_cfg = MCPServerConfig(
                    name=name,
                    transport=persisted.transport,
                    command=persisted.command,
                    args=args,
                    env=resolved_env,
                    timeout_s=_mcp_request_timeout(cfg),
                )
                await asyncio.wait_for(
                    harness.attach_mcp_servers([mcp_cfg]),
                    timeout=10.0,
                )
                # 检查 attach 是否真的成功——harness.attach_mcp_servers 可能不抛
                server_state = next(
                    (s for s in harness.list_mcp_servers() if s.name == name),
                    None,
                )
                if server_state is None or server_state.last_error:
                    cfg.attached = False
                    cfg.restore_status = "error"
                    cfg.last_error = (
                        server_state.last_error if server_state else "not in registry after attach"
                    )
                    await ext_store.set_mcp_restore_error(name, cfg.last_error)
                    continue

                cfg.attached = True
                cfg.restore_status = "attached"
                cfg.tool_count = server_state.tool_count
                cfg.last_error = None

                # apply disabled tools
                disabled = await ext_store.list_disabled_mcp_tools(name)
                for dt in disabled:
                    full_name = f"{_MCP_TOOL_NAME_PREFIX}{name}__{dt.tool_name}"
                    state.disabled_mcp_tools.add(full_name)
                    if harness.agent.tools.has(full_name):
                        try:
                            harness.agent.tools.unregister(full_name)
                        except Exception:
                            pass

                await ext_store.set_mcp_restore_error(name, None)
            except TimeoutError:
                cfg.attached = False
                cfg.restore_status = "error"
                cfg.last_error = "restore timeout (10s)"
                await ext_store.set_mcp_restore_error(name, "restore timeout")
            except Exception as e:
                cfg.attached = False
                cfg.last_error = _safe_extension_error(e, list(resolved_env.values()))
                await ext_store.set_mcp_restore_error(name, cfg.last_error)

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
            raise PromptValidationError(503, "file store not initialized; cannot accept file_ids")
        if session_id is None:
            raise PromptValidationError(400, "session_id required when file_ids present")

        from agent_workspace.store import (
            FileAccessDeniedError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
        )

        from ..messages import FileBlock
        from ..tools.view_file import _classify_format

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
            if s["format"] not in ("image_unsupported", "binary", "unsupported", "pdf")
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

        P2-R4-B2 TOCTOU fix: ``_ensure_idle()`` + ``state.running = True``
        happen back-to-back with NO await between them. This closes the
        race window where a second prompt could pass ``_ensure_idle()``
        while the first hadn't yet set ``state.running``. Any exception
        in the remaining validation rolls back the reservation.
        """
        _ensure_idle()  # HTTPException(409)——HTTP 层 FastAPI 自动处理；async 层 catch

        # Atomically reserve the single-active-request slot BEFORE any
        # await. No suspension point between check and reserve.
        state.running = True
        original_messages: list[Any] | None = None

        try:
            text = (payload or {}).get("text") or ""
            if not text.strip():
                raise PromptValidationError(400, "text is required")

            coding_mode_raw = (payload or {}).get("coding_mode", False)
            if not isinstance(coding_mode_raw, bool):
                raise PromptValidationError(400, "coding_mode must be a boolean")
            execution_mode_raw = (payload or {}).get("execution_mode", "direct")
            if execution_mode_raw not in {"direct", "plan"}:
                raise PromptValidationError(
                    400,
                    "execution_mode must be 'direct' or 'plan'",
                )
            execution_mode = cast(Literal["direct", "plan"], execution_mode_raw)
            if execution_mode == "plan" and not state.plan_mode_enabled:
                raise PromptValidationError(400, "Plan mode is not enabled")
            intent_mode_raw = (payload or {}).get("intent_mode")
            if not state.intent_routing_enabled and intent_mode_raw is not None:
                raise PromptValidationError(400, "intent routing is not enabled")
            try:
                intent_mode = parse_intent_mode(intent_mode_raw)
            except ValueError as exc:
                raise PromptValidationError(400, str(exc)) from None
            if (coding_mode_raw or execution_mode == "plan") and intent_mode in {
                "read_only",
                "knowledge",
            }:
                raise PromptValidationError(
                    400,
                    "Coding execution conflicts with intent_mode",
                )

            skill_sel_raw = (payload or {}).get("skill_selection") or {}
            skill_names_raw = (payload or {}).get("skill_names")
            if skill_names_raw is not None:
                if not isinstance(skill_names_raw, list):
                    raise PromptValidationError(400, "skill_names must be a list of strings")
                bad = [n for n in skill_names_raw if not isinstance(n, str) or not n]
                if bad:
                    raise PromptValidationError(
                        400,
                        f"skill_names entries must be non-empty strings (got {bad[0]!r})",
                    )

            merged_names, _sel_names, _top_names = _merge_skill_names(
                skill_sel_raw, skill_names_raw
            )
            skill_selection = _build_skill_selection(skill_sel_raw, merged_names)
            if execution_mode == "plan" and skill_selection is not None:
                raise PromptValidationError(
                    400,
                    "Plan mode uses fixed Planner, Executor, and Verifier prompts",
                )

            if merged_names and harness.skill_registry is not None:
                missing = [n for n in merged_names if not harness.skill_registry.has(n)]
                if missing:
                    raise PromptValidationError(
                        400,
                        f"Unknown skill: {missing[0]!r}",
                        missing_skill_names=missing,
                    )

            session_id = (payload or {}).get("session_id") or state.current_session_id
            store = state.session_store

            if session_id is not None and session_id in deleting_session_ids:
                raise PromptValidationError(
                    409,
                    f"session {session_id!r} is being deleted",
                )

            if store is not None and session_id is not None:
                from ..session_sqlite import SessionNotFoundError

                try:
                    history = await store.list_messages(session_id)
                except SessionNotFoundError:
                    raise PromptValidationError(404, f"session {session_id!r} not found") from None
                original_messages = list(harness.agent.state.messages)
                harness.agent.state.messages = list(history)

            knowledge_conversation = None
            wiki_store = state.wiki_store
            if wiki_store is not None and session_id is not None:
                try:
                    knowledge_conversation = await wiki_store.find_conversation_by_session(
                        session_id
                    )
                    if knowledge_conversation is not None:
                        space = await wiki_store.get_space(knowledge_conversation.space_id)
                        if knowledge_conversation.status != "active" or space.status != "active":
                            raise PromptValidationError(
                                409,
                                "Knowledge conversation is not active",
                            )
                except PromptValidationError:
                    raise
                except Exception as exc:
                    raise PromptValidationError(
                        409,
                        "Knowledge conversation binding is unavailable",
                    ) from exc
            claimed_conversation_id = (payload or {}).get("knowledge_conversation_id")
            if claimed_conversation_id is not None and (
                knowledge_conversation is None
                or not isinstance(claimed_conversation_id, str)
                or claimed_conversation_id != knowledge_conversation.id
            ):
                raise PromptValidationError(
                    403,
                    "Knowledge conversation binding does not match the Session",
                )
            if knowledge_conversation is not None and skill_selection is not None:
                raise PromptValidationError(
                    400,
                    "Knowledge conversations use a fixed built-in Skill",
                )
            if knowledge_conversation is not None and (
                coding_mode_raw
                or execution_mode == "plan"
                or intent_mode in {"coding", "read_only"}
            ):
                raise PromptValidationError(
                    400,
                    "Knowledge conversations use their bound Knowledge route",
                )
            if knowledge_conversation is None and intent_mode == "knowledge":
                raise PromptValidationError(
                    400,
                    "Knowledge mode requires a bound Knowledge conversation",
                )

            intent = None
            coding_mode = coding_mode_raw or execution_mode == "plan"
            if state.intent_routing_enabled:
                intent = route_intent(
                    text,
                    knowledge_bound=knowledge_conversation is not None,
                    intent_mode=intent_mode,
                    legacy_coding_mode=coding_mode,
                )
                coding_mode = intent.route == "coding"
            if coding_mode and session_id is None:
                raise PromptValidationError(
                    400,
                    "session_id is required for automated Coding mode",
                )
            if coding_mode:
                sandbox_runtime = getattr(app.state, "coding_sandbox_runtime", None)
                if sandbox_runtime is None or sandbox_runtime.lifecycle is None:
                    raise PromptValidationError(
                        503,
                        "Managed Coding Sandbox is unavailable",
                    )

            file_ids_raw = (payload or {}).get("file_ids") or []
            if not isinstance(file_ids_raw, list):
                raise PromptValidationError(400, "file_ids must be a list of strings")
            if knowledge_conversation is not None and file_ids_raw:
                raise PromptValidationError(
                    400,
                    "Knowledge conversations cannot attach Session Workspace files",
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
                knowledge_conversation=knowledge_conversation,
                coding_mode=coding_mode,
                execution_mode=execution_mode,
                intent=intent,
            )
        except Exception:
            # Rollback the reservation if any validation step fails.
            if original_messages is not None:
                harness.agent.state.messages = original_messages
            state.running = False
            raise

    async def _run_prompt_core(
        validated: _PromptValidated,
        *,
        coding_repair: bool = False,
    ) -> PromptRunOutcome:
        """D2-4 thin wrapper——执行 + 普通 persist。

        保持 POST /api/prompt / async prompt / WebEventEnvelope / abort /
        reconnect replay 行为不回归。原有的 PromptRuntimeError 异常类型保留。

        旧调用方无需改动——此函数返回 PromptRunOutcome（旧 PromptExecutionResult
        的重命名），字段完全兼容。
        """
        # P2-R4-B2: Reset turn-scoped Evidence Registry at each prompt
        # request boundary. The registry is lazily created by
        # search_knowledge tool's evidence_registry_getter on first use
        # within this request. Multiple search_knowledge calls in the
        # same request share the registry (chunk_id dedupe).
        # Next request starts fresh from E1.
        state._evidence_registry = None

        execution = cast(
            PromptExecutionResult,
            await _execute_prompt(
                validated,
                coding_repair=coding_repair,
                manage_running_state=False,
            ),
        )

        # P2-R4-C2: Citation transform — after LLM generates text with
        # [cite:E1] tokens, validate against EvidenceRegistry and render
        # numbered citations + source footer before persistence.
        _apply_citation_transform(execution, state)

        return await _persist_normal_prompt_result(validated, execution)

    async def _auto_memory_blocked_by_sandbox(operation_id: str | None) -> bool:
        if operation_id is None:
            return False
        runtime = getattr(app.state, "coding_sandbox_runtime", None)
        lifecycle = None if runtime is None else runtime.lifecycle
        if lifecycle is None:
            return True
        try:
            record = await lifecycle.get(operation_id)
        except Exception:
            return True
        return not record.terminal

    async def _apply_auto_memory_operation(
        validated: _PromptValidated,
        operation: Any,
        evidence: AutoMemoryOperationEvidence,
    ) -> dict[str, object]:
        store = state.session_store
        file_store = state.file_store
        session_id = validated.session_id
        if store is None or file_store is None or session_id is None:
            raise CheckpointerError(
                "auto_memory_unavailable",
                "Automatic Memory storage is unavailable.",
            )
        if await _auto_memory_blocked_by_sandbox(
            evidence.blocked_by_sandbox_operation_id
        ):
            return {
                "status": "deferred",
                "operation_id": operation.id,
                "source_sha256": evidence.source.source_sha256,
                "blocked_by_sandbox_operation_id": (
                    evidence.blocked_by_sandbox_operation_id
                ),
            }

        memory_ref = await file_store.get_by_logical_path(
            session_id,
            SESSION_MEMORY_PATH,
        )
        if memory_ref is None:
            raise CheckpointerError(
                "auto_memory_unavailable",
                "Memory.md is unavailable.",
            )
        prior_memory = Path(memory_ref.path).read_text(  # noqa: ASYNC240
            encoding="utf-8",
            errors="replace",
        )
        if extract_checkpoint_source_hash(prior_memory) == evidence.source.source_sha256:
            updated_ref = memory_ref
            recovered = True
        else:
            checkpoint_request = _PromptValidated(
                text="",
                skill_selection=None,
                session_id=session_id,
                store=store,
                original_messages=None,
                attached_blocks=[],
                attached_summary=[],
            )
            harness.context.metadata["continuity_active"] = True
            try:
                memory_text = cast(
                    str,
                    await _execute_prompt(
                        checkpoint_request,
                        checkpoint_source=evidence.source,
                        checkpoint_prior_memory=prior_memory,
                        checkpoint_operation=AUTO_MEMORY_OPERATION_KIND,
                        manage_running_state=False,
                    ),
                )
            finally:
                harness.context.metadata.pop("continuity_active", None)
            updated_ref = await file_store.update_text(
                session_id,
                memory_ref.id,
                memory_text,
                expected_sha256=memory_ref.sha256,
                origin="agent",
                purpose="memory",
            )
            recovered = False

        await store.mark_operation_effect_committed(
            operation.id,
            {
                "file_id": updated_ref.id,
                "file_sha256": updated_ref.sha256,
                "logical_path": updated_ref.logical_path,
            },
        )
        await store.finish_operation(operation.id, outcome="completed")
        workspace = await file_store.get_workspace_state(session_id)
        await _emit_web_payload(
            {
                "type": "workspace_changed",
                "source": "auto_memory",
                "operation_id": operation.id,
                "workspace_revision": workspace.revision,
                "changed_paths": [SESSION_MEMORY_PATH],
                "deleted_paths": [],
            },
            state.current_request_id,
            session_id,
        )
        return {
            "status": "updated",
            "operation_id": operation.id,
            "source_sha256": evidence.source.source_sha256,
            "memory_file_id": updated_ref.id,
            "workspace_revision": workspace.revision,
            "recovered": recovered,
        }

    async def _resume_pending_auto_memory(
        validated: _PromptValidated,
    ) -> tuple[AutoMemoryOperationEvidence | None, str | None]:
        if (
            not state.auto_memory_enabled
            or validated.session_id is None
            or validated.knowledge_conversation is not None
            or state.session_store is None
            or state.file_store is None
        ):
            return None, None
        session_id = validated.session_id
        lock = continuity_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await recover_auto_memory_operations(
                state.session_store,
                state.file_store,
                session_id=session_id,
            )
            operations = await state.session_store.list_open_operations(
                kind=AUTO_MEMORY_OPERATION_KIND,
                session_id=session_id,
            )
            if not operations:
                await _reconstruct_missing_auto_memory_intent(validated)
                operations = await state.session_store.list_open_operations(
                    kind=AUTO_MEMORY_OPERATION_KIND,
                    session_id=session_id,
                )
            if not operations:
                return None, None
            operation = operations[0]
            try:
                evidence = checkpoint_source_from_operation_payload(operation.payload)
            except CheckpointerError:
                await recover_auto_memory_operations(
                    state.session_store,
                    state.file_store,
                    session_id=session_id,
                )
                return None, None
            try:
                result = await _apply_auto_memory_operation(
                    validated,
                    operation,
                    evidence,
                )
            except Exception as exc:
                state.last_error = f"auto_memory: {type(exc).__name__}"
            else:
                if result["status"] == "updated":
                    return None, None
            validated.pending_continuity_evidence = evidence
            return evidence, operation.id

    async def _reconstruct_missing_auto_memory_intent(
        validated: _PromptValidated,
    ) -> None:
        """Close the post-message/pre-intent crash window from canonical history."""
        store = state.session_store
        session_id = validated.session_id
        if store is None or session_id is None:
            return
        messages = list(await store.list_messages(session_id))
        from ..messages import UserMessage

        try:
            turn_start = max(
                index for index, message in enumerate(messages) if isinstance(message, UserMessage)
            )
        except ValueError:
            return
        turn_messages = messages[turn_start:]
        if _extract_terminal_assistant(turn_messages) is None:
            return
        latest_turn = build_checkpoint_source(turn_messages)
        latest_operation = await store.get_latest_operation(
            kind=AUTO_MEMORY_OPERATION_KIND,
            session_id=session_id,
        )
        if latest_operation is not None:
            try:
                latest_evidence = checkpoint_source_from_operation_payload(
                    latest_operation.payload
                )
            except CheckpointerError:
                pass
            else:
                if latest_evidence.source.source_sha256 == latest_operation.dedupe_key:
                    covered_hash = (
                        latest_evidence.latest_turn_source_sha256
                        or latest_evidence.source.source_sha256
                    )
                    if covered_hash == latest_turn.source_sha256:
                        return

        blocked_by: str | None = None
        runtime = getattr(app.state, "coding_sandbox_runtime", None)
        lifecycle = None if runtime is None else runtime.lifecycle
        if lifecycle is not None:
            try:
                latest_sandbox = await lifecycle.latest_for_session(session_id)
            except Exception:
                latest_sandbox = None
            if latest_sandbox is not None and not latest_sandbox.terminal:
                blocked_by = latest_sandbox.operation_id

        await store.start_operation(
            session_id,
            kind=AUTO_MEMORY_OPERATION_KIND,
            dedupe_key=latest_turn.source_sha256,
            operation_id=f"op_{uuid4().hex[:20]}",
            payload=checkpoint_source_to_operation_payload(
                latest_turn,
                blocked_by_sandbox_operation_id=blocked_by,
                latest_turn_source_sha256=latest_turn.source_sha256,
            ),
        )

    async def _finalize_auto_memory(
        validated: _PromptValidated,
        result: PromptRunOutcome,
        *,
        pending_evidence: AutoMemoryOperationEvidence | None,
        pending_operation_id: str | None,
        abort_requested: Callable[[], bool] | None,
    ) -> None:
        if not state.auto_memory_enabled:
            return
        if validated.knowledge_conversation is not None:
            result.continuity = {"status": "skipped", "reason": "knowledge_mode"}
            return
        if (
            validated.session_id is None
            or state.session_store is None
            or state.file_store is None
            or not result.session_persisted
            or not result.turn_messages
        ):
            result.continuity = {"status": "unavailable"}
            return

        session_id = validated.session_id
        latest_turn_source = build_checkpoint_source(list(result.turn_messages))
        source = latest_turn_source
        if pending_evidence is not None:
            source = merge_checkpoint_sources(pending_evidence.source, source)
        blocked_by = (
            pending_evidence.blocked_by_sandbox_operation_id
            if pending_evidence is not None
            else None
        )
        if validated.coding_mode and result.coding_sandbox is not None:
            operation_value = result.coding_sandbox.get("operation_id")
            if isinstance(operation_value, str):
                blocked_by = operation_value

        lock = continuity_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if pending_operation_id is not None:
                prior = await state.session_store.get_operation(pending_operation_id)
                if prior is not None and prior.is_open:
                    await state.session_store.finish_operation(
                        prior.id,
                        outcome="failed",
                        payload={"code": "continuity_superseded"},
                    )
            await recover_checkpointer_operations(
                state.session_store,
                state.file_store,
                session_id=session_id,
            )
            try:
                operation = await state.session_store.start_operation(
                    session_id,
                    kind=AUTO_MEMORY_OPERATION_KIND,
                    dedupe_key=source.source_sha256,
                    operation_id=f"op_{uuid4().hex[:20]}",
                    payload=checkpoint_source_to_operation_payload(
                        source,
                        blocked_by_sandbox_operation_id=blocked_by,
                        latest_turn_source_sha256=latest_turn_source.source_sha256,
                    ),
                )
            except SessionOperationConflictError:
                result.continuity = {
                    "status": "pending_retry",
                    "error_code": "continuity_operation_conflict",
                }
                return

            evidence = AutoMemoryOperationEvidence(
                source=source,
                blocked_by_sandbox_operation_id=blocked_by,
                latest_turn_source_sha256=latest_turn_source.source_sha256,
            )
            if abort_requested is not None and abort_requested():
                result.continuity = {
                    "status": "pending_retry",
                    "operation_id": operation.id,
                    "source_sha256": source.source_sha256,
                    "error_code": "request_aborted",
                }
                return
            try:
                result.continuity = await _apply_auto_memory_operation(
                    validated,
                    operation,
                    evidence,
                )
            except Exception as exc:
                state.last_error = f"auto_memory: {type(exc).__name__}"
                result.continuity = {
                    "status": "pending_retry",
                    "operation_id": operation.id,
                    "source_sha256": source.source_sha256,
                    "error_code": (
                        exc.code if isinstance(exc, CheckpointerError) else type(exc).__name__
                    ),
                }

    async def _try_finalize_auto_memory(
        validated: _PromptValidated,
        result: PromptRunOutcome,
        *,
        pending_evidence: AutoMemoryOperationEvidence | None,
        pending_operation_id: str | None,
        abort_requested: Callable[[], bool] | None,
    ) -> None:
        """Keep continuity failures subordinate to the already-persisted turn."""
        try:
            await _finalize_auto_memory(
                validated,
                result,
                pending_evidence=pending_evidence,
                pending_operation_id=pending_operation_id,
                abort_requested=abort_requested,
            )
        except Exception as exc:
            state.last_error = f"auto_memory_finalize: {type(exc).__name__}"
            result.continuity = {
                "status": "unavailable",
                "error_code": type(exc).__name__,
            }

    async def _resume_auto_memory_after_sandbox(session_id: str) -> None:
        if (
            not state.auto_memory_enabled
            or state.running
            or session_id in state.active_request_by_session
            or state.session_store is None
            or state.file_store is None
        ):
            return
        state.running = True
        try:
            validated = _PromptValidated(
                text="",
                skill_selection=None,
                session_id=session_id,
                store=state.session_store,
                original_messages=None,
                attached_blocks=[],
                attached_summary=[],
            )
            await _resume_pending_auto_memory(validated)
        except Exception as exc:
            state.last_error = f"auto_memory_resume: {type(exc).__name__}"
        finally:
            state.running = False

    def _schedule_auto_memory_resume(session_id: str) -> None:
        if not state.auto_memory_enabled or state.shutting_down:
            return
        tasks: set[asyncio.Task[Any]] = container["continuity_tasks"]
        task = asyncio.create_task(
            _resume_auto_memory_after_sandbox(session_id),
            name=f"auto_memory_{session_id}",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _run_plan_prompt(
        validated: _PromptValidated,
        *,
        abort_requested: Callable[[], bool] | None,
    ) -> PromptRunOutcome:
        """Run isolated Planner/Executor/Verifier roles and persist one public turn."""
        from coding_agent_app.planning.orchestrator import (
            PlanOrchestrationError,
            PlanOrchestrator,
        )
        from coding_agent_app.planning.store import PlanStore

        from ..messages import AssistantMessage, TextContent, Usage, UserMessage
        from .coding_sandbox.automation import (
            CodingSandboxAutomation,
            CodingSandboxAutomationError,
        )

        session_id = validated.session_id
        session_store = validated.store
        plan_store = cast(PlanStore | None, state.plan_store)
        runtime = getattr(app.state, "coding_sandbox_runtime", None)
        lifecycle = None if runtime is None else runtime.lifecycle
        request_id = state.current_request_id
        if (
            session_id is None
            or session_store is None
            or plan_store is None
            or lifecycle is None
            or request_id is None
        ):
            raise PromptRuntimeError(
                503,
                "Plan mode is unavailable.",
                "plan_mode_unavailable",
            )

        cancelled = abort_requested or (lambda: False)
        approval_events = cast(dict[str, asyncio.Event], container["plan_approval_events"])

        async def wait_for_approval(run_id: str) -> None:
            event = approval_events.setdefault(run_id, asyncio.Event())
            try:
                while True:
                    if cancelled():
                        raise PlanOrchestrationError(
                            "plan_cancelled", "Plan run was cancelled."
                        )
                    current = await plan_store.get_run(run_id)
                    if current.status == "executing":
                        return
                    try:
                        await asyncio.wait_for(event.wait(), timeout=0.1)
                    except TimeoutError:
                        continue
                    event.clear()
            finally:
                approval_events.pop(run_id, None)

        async def notify(event_type: str, run: Any) -> None:
            await _emit_web_payload(
                {
                    "type": event_type,
                    "plan": run.model_dump(mode="json"),
                },
                request_id,
                session_id,
            )

        workspace_context: str | None = None
        workspace_context_metadata: dict[str, Any] | None = None
        try:
            workspace_context = await _assemble_workspace_context(
                session_id,
                pending_memory=validated.pending_continuity_evidence,
            )
            metadata = harness.context.metadata.get("workspace_context")
            if isinstance(metadata, dict):
                workspace_context_metadata = dict(metadata)
        except Exception:
            raise PromptRuntimeError(
                409,
                "Workspace continuation context is unavailable.",
                "workspace_context_unavailable",
            ) from None

        result_client = harness.agent.client
        async def run_bound_plan() -> PlanRunResult:
            nonlocal result_client
            try:
                registered_names: set[str] = container["coding_sandbox_tool_names"]
                coding_tools = [
                    tool
                    for tool in harness.agent.tools.list()
                    if tool.name in registered_names
                ]
                if len(coding_tools) != len(registered_names) or not coding_tools:
                    raise PlanOrchestrationError(
                        "coding_tools_unavailable",
                        "Automated Coding tools are unavailable.",
                    )
                read_tools = [
                    tool
                    for tool in harness.agent.tools.list()
                    if not tool.name.startswith("coding_")
                    and is_read_only_tool_name(tool.name)
                ]
                orchestrator = PlanOrchestrator(
                    store=plan_store,
                    client=harness.agent.client,
                    automation=CodingSandboxAutomation(lifecycle),
                    read_tools=read_tools,
                    coding_tools=coding_tools,
                    wait_for_approval=wait_for_approval,
                    notify=notify,
                    cancelled=cancelled,
                )
                result_client = harness.agent.client
                return await orchestrator.run(
                    session_id=session_id,
                    request_id=request_id,
                    goal=validated.text,
                    planning_context=workspace_context,
                )
            except CodingSandboxAutomationError as exc:
                raise PromptRuntimeError(409, str(exc), exc.code) from None
            except PlanOrchestrationError as exc:
                status_code = 409 if exc.code == "plan_cancelled" else 500
                raise PromptRuntimeError(status_code, str(exc), exc.code) from None

        plan_result = cast(
            "PlanRunResult",
            await _execute_prompt(
                validated,
                manage_running_state=False,
                provider_bound_operation=run_bound_plan,
            ),
        )

        history = list(await session_store.list_messages(session_id))
        user_message = UserMessage(
            content=[TextContent(text=validated.text), *validated.attached_blocks]
        )
        assistant_message = AssistantMessage(
            content=[TextContent(text=plan_result.summary)],
            api=result_client.api_id or result_client.provider_id or "unknown",
            provider=result_client.provider_id or "unknown",
            model=result_client.model or "unknown",
            stop_reason="stop",
            usage=Usage(),
        )
        final_messages = [*history, user_message, assistant_message]
        await session_store.replace_messages(session_id, final_messages)
        return PromptRunOutcome(
            messages=final_messages,
            serialized_messages=[serialize_message(message) for message in final_messages],
            session_id=session_id,
            attachment_meta=_build_attachment_meta(validated.attached_summary),
            applied_skill_names=[],
            coding_sandbox=plan_result.sandbox,
            workspace_context=workspace_context_metadata,
            intent=validated.intent.public() if validated.intent is not None else None,
            plan_run=plan_result.run.model_dump(mode="json"),
            turn_messages=(user_message, assistant_message),
            session_persisted=True,
        )

    async def _run_prompt_request(
        validated: _PromptValidated,
        *,
        abort_requested: Callable[[], bool] | None = None,
    ) -> PromptRunOutcome:
        """Own continuity finalization and the optional automated Coding lifecycle."""
        state.running = True
        model_persisted = False
        is_plan_request = validated.execution_mode == "plan"
        pending_evidence: AutoMemoryOperationEvidence | None = None
        pending_operation_id: str | None = None
        try:
            try:
                pending_evidence, pending_operation_id = await _resume_pending_auto_memory(
                    validated
                )
            except Exception as exc:
                state.last_error = f"auto_memory_preflight: {type(exc).__name__}"
            if abort_requested is not None and abort_requested():
                if validated.coding_mode:
                    if validated.original_messages is not None:
                        harness.agent.state.messages = validated.original_messages
                    state.running = False
                raise PromptRuntimeError(
                    409,
                    "Request was aborted during continuity preflight.",
                    "request_aborted",
                )
            if not validated.coding_mode:
                result = await _run_prompt_core(validated)
                model_persisted = result.session_persisted
                await _try_finalize_auto_memory(
                    validated,
                    result,
                    pending_evidence=pending_evidence,
                    pending_operation_id=pending_operation_id,
                    abort_requested=abort_requested,
                )
                return result
            if is_plan_request:
                result = await _run_plan_prompt(
                    validated,
                    abort_requested=abort_requested,
                )
                model_persisted = result.session_persisted
                await _try_finalize_auto_memory(
                    validated,
                    result,
                    pending_evidence=pending_evidence,
                    pending_operation_id=pending_operation_id,
                    abort_requested=abort_requested,
                )
                return result
        finally:
            if not validated.coding_mode or is_plan_request:
                if (
                    validated.original_messages is not None
                    and (is_plan_request or not model_persisted)
                ):
                    harness.agent.state.messages = validated.original_messages
                state.running = False

        from .coding_sandbox.automation import (
            CodingSandboxAutomation,
            CodingSandboxAutomationError,
        )

        runtime = getattr(app.state, "coding_sandbox_runtime", None)
        lifecycle = None if runtime is None else runtime.lifecycle
        if lifecycle is None or validated.session_id is None:
            if validated.original_messages is not None:
                harness.agent.state.messages = validated.original_messages
            raise PromptRuntimeError(
                503,
                "Managed Coding Sandbox is unavailable.",
                "coding_sandbox_unavailable",
            )

        automation = CodingSandboxAutomation(lifecycle)
        operation_id: str | None = None
        try:
            operation = await automation.prepare(
                validated.session_id,
                cancelled=abort_requested,
            )
            operation_id = operation.operation_id

            async def _run_coding_attempt(repair: bool) -> PromptRunOutcome:
                nonlocal model_persisted
                attempt_result = await _run_prompt_core(
                    validated,
                    coding_repair=repair,
                )
                model_persisted = attempt_result.session_persisted
                return attempt_result

            result = await automation.run_with_no_change_retry(
                operation_id,
                _run_coding_attempt,
                cancelled=abort_requested,
            )
            if abort_requested is not None and abort_requested():
                await automation.cancel_if_possible(operation_id)
                return result
            finalized = await automation.validate_and_freeze(
                operation_id,
                cancelled=abort_requested,
            )
            result.coding_sandbox = finalized.public()
            await _try_finalize_auto_memory(
                validated,
                result,
                pending_evidence=pending_evidence,
                pending_operation_id=pending_operation_id,
                abort_requested=abort_requested,
            )
            return result
        except CodingSandboxAutomationError as exc:
            if exc.code == "coding_request_aborted":
                await automation.cancel_if_possible(exc.operation_id or operation_id)
            elif not model_persisted:
                await automation.cancel_if_possible(exc.operation_id or operation_id)
            state.last_error = str(exc)
            raise PromptRuntimeError(
                409 if exc.code in {
                    "coding_approval_pending",
                    "coding_operation_conflict",
                    "coding_sandbox_disabled",
                    "coding_sandbox_not_configured",
                    "coding_sandbox_not_ready",
                    "coding_validation_failed",
                    "coding_validation_required",
                    "coding_no_changes",
                    "coding_request_aborted",
                } else 500,
                str(exc),
                exc.code,
            ) from None
        except PromptRuntimeError:
            await automation.cancel_if_possible(operation_id)
            raise
        finally:
            if not model_persisted and validated.original_messages is not None:
                harness.agent.state.messages = validated.original_messages
            state.running = False

    def _extract_terminal_assistant(suffix: list[Any]) -> Any | None:
        """D2-4：从执行 suffix 中提取最终 assistant candidate。

        不能简单取最后一个 role=='assistant'——多轮 tool_use 会产生
        AssistantMessage(tool_call) → ToolResultMessage → AssistantMessage(text)
        的中间状态。本函数从 suffix **末尾向前**扫描，跳过：
        - 非 AssistantMessage（ToolResultMessage / UserMessage / etc.）
        - AssistantMessage 但 `error_message` 非空（执行错误）
        - AssistantMessage 但 content 只含 ToolCall（中间 tool-call turn）

        返回最后一个**合格** candidate；没有则 None。
        """
        from ..messages import AssistantMessage, TextContent, ToolCall

        for msg in reversed(suffix):
            if not isinstance(msg, AssistantMessage):
                continue
            if getattr(msg, "error_message", None):
                continue
            # 必须含至少一个 TextContent（纯 ToolCall 的中间 turn 不算 terminal）
            has_text = any(isinstance(c, TextContent) for c in msg.content)
            has_only_tool_calls = all(isinstance(c, ToolCall) for c in msg.content) and msg.content
            if has_text or not has_only_tool_calls:
                return msg
        return None

    def _serialize_assistant_for_messages(assistant_msg: Any) -> str:
        """序列化 AssistantMessage 为 messages.content_json 格式（`{type, data}`）。

        与 SQLiteSessionStore._serialize_message 对齐——确保 list_messages 能
        正确反序列化（regenerate finalize 写入的 content_json 必须可被普通 API 读回）。
        """
        payload = {
            "type": type(assistant_msg).__name__,
            "data": assistant_msg.model_dump(),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def _run_regeneration_core(
        validated: _PromptValidated,
        *,
        assistant_message_id: str,
        request_id: str,
        revision_id: str | None = None,
    ) -> PromptRunOutcome:
        """D2-4 + D2-5：Regenerate 编排核心。

        生命周期：
            1. 若 revision_id 为 None：create_running_revision（短事务）
               否则：使用 caller 传入的 revision_id（D2-5 POST route 已创建）
            2. 读 canonical messages → 截断到 preceding user → 临时替换 harness state
            3. _execute_prompt（长 LLM 执行，无 DB transaction 跨越）
            4. _persist_regeneration_result（finalize_revision + best-effort snapshot）
            5. _reset_harness_to_session（成功 / 失败都要 reset）

        异常 → revision 状态映射：
            - 用户主动 abort → revision.aborted（asyncio.CancelledError 由 caller 处理）
            - Provider/Agent 执行失败（PromptRuntimeError） → revision.error
            - Candidate 缺失 → revision.error
            - Base hash stale (RevisionBaseContentChangedError) → revision.error
            - Finalize SQL 失败 → revision.error

        Snapshot 失败（finalize 已成功）→ revision 仍 completed，request 仍 completed。

        **事务边界**：短事务 create → 释放 → 长执行 → finalize 短事务。
        没有任何 SQLite BEGIN IMMEDIATE 跨越 LLM 调用。
        """
        from .extension_store import (
            ExtensionStoreError,
            RevisionBaseContentChangedError,
            RevisionError,
        )

        ext_store = state.extension_store
        if ext_store is None:
            raise ExtensionStoreError("extension_store not initialized")

        store = validated.store
        session_id = validated.session_id
        if store is None or session_id is None:
            raise ExtensionStoreError("regenerate requires session + store")

        pending_evidence: AutoMemoryOperationEvidence | None = None
        pending_operation_id: str | None = None
        try:
            pending_evidence, pending_operation_id = await _resume_pending_auto_memory(validated)
        except Exception as exc:
            state.last_error = f"auto_memory_preflight: {type(exc).__name__}"

        # 1. revision 创建（D2-5：caller 可传入已有 revision_id 避免双创建）
        if revision_id is None:
            revision = await ext_store.create_running_revision(
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                request_id=request_id,
            )
            revision_id = revision.id

        # 保存原 harness state 用于 fallback；读 canonical 构建 regeneration history
        original_harness_messages = list(harness.agent.state.messages)
        canonical_before = await store.list_messages(session_id)

        # 截断：caller 已校验过目标是 session 最新 assistant，所以 canonical_before
        # 中最后一个 AssistantMessage 就是它。regeneration_history =
        # canonical_before[:last_assistant_idx]（含 preceding user，不含旧 assistant）
        from ..messages import AssistantMessage, UserMessage

        try:
            target_idx = max(
                i for i, m in enumerate(canonical_before) if isinstance(m, AssistantMessage)
            )
        except ValueError:
            # canonical 找不到 assistant——mark error
            await ext_store.mark_revision_error(
                revision_id=revision_id,
                request_id=request_id,
                error_summary="target assistant message not in canonical history",
            )
            await _reset_harness_to_session(store, session_id, original_harness_messages)
            raise ExtensionStoreError("regenerate target not found in canonical") from None

        regeneration_history = list(canonical_before[:target_idx])
        turn_start_idx = max(
            i for i, message in enumerate(regeneration_history) if isinstance(message, UserMessage)
        )

        # 2. 临时替换 harness state，执行 model
        harness.agent.state.messages = list(regeneration_history)

        # P2-R4-B2 + R4-C2: Reset turn-scoped Evidence Registry + apply
        # citation transform for regenerate path (mirrors _run_prompt_core).
        # Without this, regenerate would bypass the citation pipeline.
        state._evidence_registry = None

        try:
            execution = cast(
                PromptExecutionResult,
                await _execute_prompt(
                    validated,
                    override_initial_messages=regeneration_history,
                    suppress_user_append=True,
                    manage_running_state=False,
                ),
            )
            # P2-R4-C2: Citation transform — same as _run_prompt_core.
            _apply_citation_transform(execution, state)
        except PromptRuntimeError as e:
            # 模型/Agent 执行失败 → revision.error
            await ext_store.mark_revision_error(
                revision_id=revision_id,
                request_id=request_id,
                error_summary=_safe_error(e),
            )
            await _reset_harness_to_session(store, session_id, original_harness_messages)
            raise
        except Exception as e:
            # 兜底——未预期异常也 mark error
            await ext_store.mark_revision_error(
                revision_id=revision_id,
                request_id=request_id,
                error_summary=_safe_error(e),
            )
            await _reset_harness_to_session(store, session_id, original_harness_messages)
            raise

        # 3. 持久化——finalize_revision + best-effort snapshot
        try:
            outcome = await _persist_regeneration_result(
                validated,
                execution,
                revision_id=revision_id,
                request_id=request_id,
            )
            outcome.turn_messages = tuple(
                [
                    *regeneration_history[turn_start_idx:],
                    *execution.messages_after[len(execution.messages_before) :],
                ]
            )
            await _try_finalize_auto_memory(
                validated,
                outcome,
                pending_evidence=pending_evidence,
                pending_operation_id=pending_operation_id,
                abort_requested=None,
            )
        except (ValueError, RevisionBaseContentChangedError, RevisionError) as e:
            # Candidate 缺失 / hash stale / SQL 失败 → revision.error
            await ext_store.mark_revision_error(
                revision_id=revision_id,
                request_id=request_id,
                error_summary=_safe_error(e),
            )
            await _reset_harness_to_session(store, session_id, original_harness_messages)
            raise

        # 4. 成功——从 SQLite canonical 重载（含新 active assistant）
        await _reset_harness_to_session(store, session_id, original_harness_messages)
        return outcome

    async def _load_sandbox_continuation(
        session_id: str,
    ) -> SandboxContinuationState | None:
        """Project one non-terminal Sandbox record without importing it upstream."""
        from agent_workspace.context_assembler import SandboxContinuationState

        runtime = getattr(app.state, "coding_sandbox_runtime", None)
        lifecycle = None if runtime is None else runtime.lifecycle
        if lifecycle is None:
            return None
        record = await lifecycle.latest_for_session(session_id)
        if record is None or record.terminal:
            return None
        validation = record.validation
        return SandboxContinuationState(
            operation_id=record.operation_id,
            status=record.status,
            workspace_revision=record.workspace_revision,
            baseline_workspace_revision=record.baseline_workspace_revision,
            artifact_id=record.artifact_id,
            artifact_sha256=record.artifact_sha256,
            validation_evidence_id=(
                None if validation is None else validation.evidence_id
            ),
            validation_passed=None if validation is None else validation.passed,
            changed_paths=record.changed_paths,
            deleted_paths=record.deleted_paths,
            allowed_actions=record.allowed_actions,
            error_code=record.error_code,
        )

    async def _assemble_workspace_context(
        session_id: str | None,
        *,
        pending_memory: AutoMemoryOperationEvidence | None = None,
    ) -> str | None:
        assembler = state.workspace_context_assembler
        if assembler is None or session_id is None:
            harness.context.metadata.pop("workspace_context", None)
            harness.context.metadata.pop("agent_md", None)
            harness.context.metadata.pop("memory_md", None)
            return None
        sandbox = await _load_sandbox_continuation(session_id)
        assembly = cast(
            "WorkspaceContextAssembly",
            await assembler.assemble(
                session_id,
                pending_memory=pending_memory,
                sandbox=sandbox,
            ),
        )
        metadata = assembly.metadata()
        harness.context.metadata["workspace_context"] = metadata
        included_paths = set(assembly.included_paths)
        harness.context.metadata["agent_md"] = {
            "included": "AGENT.md" in included_paths,
        }
        harness.context.metadata["memory_md"] = {
            "included": SESSION_MEMORY_PATH in included_paths,
        }
        return assembly.prompt_suffix

    async def _estimate_session_context_budget_details(
        *,
        session_id: str,
        draft_text: str = "",
        file_ids: list[str] | None = None,
        skill_selection: SkillSelection | None = None,
        coding_mode: bool = False,
        intent_mode: str | None = None,
    ) -> tuple[dict[str, Any], ContextEstimate]:
        """Estimate the canonical Provider input without reading a secret."""
        if state.running or harness.context.phase != "idle":
            raise HTTPException(
                status_code=409,
                detail="context budget is unavailable while a request is running",
            )
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        try:
            messages = list(await store.list_messages(session_id))
        except Exception as exc:
            from ..session_sqlite import SessionNotFoundError

            if isinstance(exc, SessionNotFoundError):
                raise HTTPException(status_code=404, detail="session not found") from None
            raise

        if not state.intent_routing_enabled and intent_mode is not None:
            raise HTTPException(status_code=400, detail="intent routing is not enabled")
        try:
            parsed_intent_mode = parse_intent_mode(intent_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if coding_mode and parsed_intent_mode in {"read_only", "knowledge"}:
            raise HTTPException(status_code=400, detail="coding_mode conflicts with intent_mode")

        knowledge_conversation = None
        if state.wiki_store is not None:
            knowledge_conversation = await state.wiki_store.find_conversation_by_session(session_id)
            if knowledge_conversation is not None:
                space = await state.wiki_store.get_space(knowledge_conversation.space_id)
                if knowledge_conversation.status != "active" or space.status != "active":
                    raise HTTPException(
                        status_code=409,
                        detail="Knowledge conversation is not active",
                    )
                if file_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=("Knowledge conversations cannot attach Session Workspace files"),
                    )
                if skill_selection is not None:
                    raise HTTPException(
                        status_code=400,
                        detail="Knowledge conversations use a fixed built-in Skill",
                    )
                if coding_mode or parsed_intent_mode in {"coding", "read_only"}:
                    raise HTTPException(
                        status_code=400,
                        detail="Knowledge conversations use their bound Knowledge route",
                    )

        if knowledge_conversation is None and parsed_intent_mode == "knowledge":
            raise HTTPException(
                status_code=400,
                detail="Knowledge mode requires a bound Knowledge conversation",
            )
        intent = None
        resolved_coding_mode = coding_mode
        if state.intent_routing_enabled:
            intent = route_intent(
                draft_text,
                knowledge_bound=knowledge_conversation is not None,
                intent_mode=parsed_intent_mode,
                legacy_coding_mode=coding_mode,
            )
            resolved_coding_mode = intent.route == "coding"

        attached_blocks: list[Any] = []
        if file_ids:
            try:
                attached_blocks, _ = await _resolve_file_blocks(session_id, file_ids)
            except PromptValidationError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=exc.detail,
                ) from None
        if draft_text.strip() or attached_blocks:
            from ..messages import TextContent, UserMessage

            messages.append(
                UserMessage(
                    content=[
                        TextContent(text=draft_text),
                        *attached_blocks,
                    ]
                )
            )

        # Rendering helpers annotate Harness metadata for snapshots. A preview is
        # observational, so restore the prior values after rendering.
        metadata_before = dict(harness.context.metadata)
        workspace_context_metadata: dict[str, Any] | None = None
        try:
            if knowledge_conversation is None:
                pending_continuity: AutoMemoryOperationEvidence | None = None
                if state.auto_memory_enabled and state.session_store is not None:
                    operations = await state.session_store.list_open_operations(
                        kind=AUTO_MEMORY_OPERATION_KIND,
                        session_id=session_id,
                    )
                    if operations:
                        try:
                            evidence = checkpoint_source_from_operation_payload(
                                operations[0].payload
                            )
                        except CheckpointerError:
                            pass
                        else:
                            pending_continuity = evidence
                try:
                    workspace_context = await _assemble_workspace_context(
                        session_id,
                        pending_memory=pending_continuity,
                    )
                    assembled_metadata = harness.context.metadata.get(
                        "workspace_context"
                    )
                    if isinstance(assembled_metadata, dict):
                        workspace_context_metadata = dict(assembled_metadata)
                except Exception:
                    raise HTTPException(
                        status_code=409,
                        detail="Workspace continuation context is unavailable",
                    ) from None
                route_instructions = None
                if resolved_coding_mode:
                    from .coding_sandbox.automation import AUTOMATED_CODING_PROMPT

                    route_instructions = AUTOMATED_CODING_PROMPT
                elif intent is not None and intent.route == "read_only":
                    route_instructions = READ_ONLY_SYSTEM_PROMPT
                suffix = (
                    "\n\n".join(
                        block
                        for block in (
                            route_instructions,
                            workspace_context,
                        )
                        if block
                    )
                    or None
                )
                rendered_prompt, _ = harness._prepare_skill_prompt(skill_selection)
            else:
                from .wiki.knowledge_agent import render_knowledge_agent_prompt

                suffix = render_knowledge_agent_prompt(knowledge_conversation)
                rendered_prompt, _ = harness._prepare_skill_prompt(None)
            if suffix:
                rendered_prompt = f"{rendered_prompt}\n\n{suffix.strip()}"
        finally:
            harness.context.metadata = metadata_before

        from ..context import apply_transform_context, convert_to_llm
        from ..context import transform_context as default_transform_context
        from ..context_budget import estimate_context

        transform = harness.agent.transform_context_fn or default_transform_context
        transformed = await apply_transform_context(transform, list(messages))
        llm_messages = convert_to_llm(transformed)

        provider_id = harness.agent.client.provider_id or "legacy"
        model_id = getattr(harness.agent.client, "model", "unknown") or "unknown"
        # Context preview is read-only and must not enter the request-scoped
        # Provider binding path.  Read only the public, non-secret config
        # projection; _execute_prompt remains the sole Harness binding site.
        provider_config_runtime = app.state.provider_config_runtime
        if provider_config_runtime is not None:
            binding = await provider_config_runtime.service.get_session_binding(
                session_id,
            )
            if binding is not None:
                profile = await provider_config_runtime.service.get_profile(
                    binding.profile_id,
                )
                provider_id = profile.provider_id
                model_id = binding.model_id

        capability_store = state.model_capability_store
        capabilities = (
            await capability_store.resolve(provider_id, model_id)
            if capability_store is not None
            else None
        )
        tool_registry = state.wiki_knowledge_tools if knowledge_conversation is not None else None
        if knowledge_conversation is None:
            from ..tools import ToolRegistry

            if intent is not None and intent.route == "read_only":
                tool_registry = ToolRegistry(
                    [
                        tool
                        for tool in harness.agent.tools.list()
                        if is_read_only_tool_name(tool.name)
                    ]
                )
            elif resolved_coding_mode:
                registered_names: set[str] = container["coding_sandbox_tool_names"]
                tool_registry = ToolRegistry(
                    [
                        tool
                        for tool in harness.agent.tools.list()
                        if tool.name in registered_names
                    ]
                )
            else:
                tool_registry = harness.agent.tools
        if tool_registry is None:
            raise HTTPException(status_code=503, detail="Knowledge tools unavailable")
        estimate = estimate_context(
            system_prompt=rendered_prompt,
            messages=llm_messages,
            tools=tool_registry.definitions(),
            context_window=(capabilities.context_window if capabilities else None),
            reserved_output_tokens=(capabilities.max_output_tokens if capabilities else None),
        )
        payload = {
            "session_id": session_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "capability_source": capabilities.source if capabilities else "unknown",
            "estimate": estimate.to_dict(),
            "workspace_context": workspace_context_metadata,
            "intent": intent.public() if intent is not None else None,
        }
        return payload, estimate

    async def _estimate_session_context_budget(
        *,
        session_id: str,
        draft_text: str = "",
        file_ids: list[str] | None = None,
        skill_selection: SkillSelection | None = None,
        coding_mode: bool = False,
        intent_mode: str | None = None,
    ) -> dict[str, Any]:
        payload, _ = await _estimate_session_context_budget_details(
            session_id=session_id,
            draft_text=draft_text,
            file_ids=file_ids,
            skill_selection=skill_selection,
            coding_mode=coding_mode,
            intent_mode=intent_mode,
        )
        return payload

    async def _execute_prompt(
        validated: _PromptValidated,
        *,
        override_initial_messages: list[Any] | None = None,
        suppress_user_append: bool = False,
        checkpoint_source: CheckpointSource | None = None,
        checkpoint_prior_memory: str | None = None,
        checkpoint_operation: str = "checkpointer",
        manage_running_state: bool = True,
        coding_repair: bool = False,
        provider_bound_operation: Callable[[], Awaitable[PlanRunResult]] | None = None,
    ) -> PromptExecutionResult | PlanRunResult | str:
        """D2-4：纯执行——只跑模型/Agent，**不**碰 DB。

        三种模式（由参数决定）：
        - 普通 text prompt（默认）：harness.run_prompt(text, ...)
        - 附件 prompt（默认 + attached_blocks）：append UserMessage + run_continue
        - Regenerate（override_initial_messages + suppress_user_append）：
          临时替换 harness state 为截断 history，run_continue 不 append 新 user

        参数：
        - `override_initial_messages`：regenerate 时传入截断后的 active history
          （不含待替换的旧 assistant）；函数临时把它设到 harness state
        - `suppress_user_append`：regenerate 路径设 True——不 append 新 user msg

        **不变量**：
        - **不**调用 replace_messages / finalize_revision / append_snapshot
        - **不**修改 request status
        - 异常路径抛 PromptRuntimeError（含 status_code）——caller 决定是否 persist
        - 无论成功/失败，harness.agent.state.messages 在调用前后**应该**由 caller
          保存/恢复；本函数只负责执行期间的 state 变化

        返回 `PromptExecutionResult`——`assistant_message` 为本次执行 suffix 中
        最后一个**合格** candidate（非 tool-call-only + 无 error_message）；无合格
        candidate 时为 None（caller 决定是否转 revision error）。
        """
        if manage_running_state:
            state.running = True
        state.last_error = None

        # 执行前快照——caller 可用来 reset，也用于 candidate 提取的 suffix 边界
        if override_initial_messages is not None:
            messages_before = list(override_initial_messages)
        else:
            messages_before = list(harness.agent.state.messages)

        # M1-5: 解析 Session Provider selection——一次解析，整个请求不可变快照.
        # runtime 为 None（Credential 或 Provider Config runtime 未启用）→ 走
        # legacy client 兼容路径，不读 Secret / 不调 Factory / 不替换 client.
        # selection 为 None（Session 无 Binding）→ 同样走 legacy client.
        runtime = app.state.request_provider_runtime
        selection: RequestProviderSelection | None = None
        if provider_bound_operation is None:
            for metadata_key in ("workspace_context", "agent_md", "memory_md"):
                harness.context.metadata.pop(metadata_key, None)
        workspace_context_metadata: dict[str, Any] | None = None
        if provider_bound_operation is not None:
            prompt_suffix = None
        elif checkpoint_source is None and validated.knowledge_conversation is None:
            try:
                workspace_context = await _assemble_workspace_context(
                    validated.session_id,
                    pending_memory=validated.pending_continuity_evidence,
                )
                assembled_metadata = harness.context.metadata.get("workspace_context")
                if isinstance(assembled_metadata, dict):
                    workspace_context_metadata = dict(assembled_metadata)
            except Exception:
                state.last_error = "Workspace continuation context is unavailable."
                if manage_running_state:
                    state.running = False
                raise PromptRuntimeError(
                    409,
                    state.last_error,
                    "workspace_context_unavailable",
                ) from None
            route_instructions = None
            if validated.coding_mode:
                from .coding_sandbox.automation import (
                    AUTOMATED_CODING_PROMPT,
                    AUTOMATED_CODING_REPAIR_PROMPT,
                )

                route_instructions = AUTOMATED_CODING_PROMPT
                if coding_repair:
                    route_instructions = (
                        f"{route_instructions}\n\n{AUTOMATED_CODING_REPAIR_PROMPT}"
                    )
            elif validated.intent is not None and validated.intent.route == "read_only":
                route_instructions = READ_ONLY_SYSTEM_PROMPT
            prompt_suffix = (
                "\n\n".join(
                    block
                    for block in (
                        route_instructions,
                        workspace_context,
                    )
                    if block
                )
                or None
            )
        elif checkpoint_source is not None:
            # A checkpointer summary treats AGENT.md, Memory.md and the transcript
            # as data only. It calls the selected client directly below and never
            # enters Harness/Agent execution, so Tools, Skills and MCP stay disabled.
            prompt_suffix = None
        else:
            from .wiki.knowledge_agent import render_knowledge_agent_prompt

            wiki_store = state.wiki_store
            try:
                if wiki_store is None:
                    raise RuntimeError("Wiki store unavailable")
                current_conversation = await wiki_store.get_conversation(
                    validated.knowledge_conversation.id
                )
                current_space = await wiki_store.get_space(current_conversation.space_id)
                if (
                    current_conversation != validated.knowledge_conversation
                    or current_conversation.status != "active"
                    or current_space.status != "active"
                ):
                    raise RuntimeError("Knowledge binding changed")
                prompt_suffix = render_knowledge_agent_prompt(current_conversation)
            except Exception:
                if manage_running_state:
                    state.running = False
                raise PromptRuntimeError(
                    409,
                    "Knowledge conversation binding changed before execution.",
                    "knowledge_conversation_conflict",
                ) from None

        try:
            if runtime is not None:
                selection = await runtime.resolve_selection(validated.session_id)

            # M1-5: bind_to_harness 在 active-request ownership 内部；
            # AsyncExitStack 让 selection=None 时跳过绑定（legacy path）.
            async with AsyncExitStack() as stack:
                if (
                    provider_bound_operation is None
                    and validated.intent is not None
                    and validated.intent.route == "read_only"
                ):
                    from ..tools import ToolRegistry

                    original_tools = harness.agent.tools
                    read_only_tools = [
                        tool
                        for tool in original_tools.list()
                        if is_read_only_tool_name(tool.name)
                    ]
                    harness.agent.tools = ToolRegistry(read_only_tools)

                    def _restore_read_only_mode() -> None:
                        harness.agent.tools = original_tools

                    stack.callback(_restore_read_only_mode)
                if provider_bound_operation is None and validated.coding_mode:
                    from ..policy import AllowAllToolPermissionPolicy
                    from ..tools import ToolRegistry

                    original_tools = harness.agent.tools
                    registered_names: set[str] = container["coding_sandbox_tool_names"]
                    coding_tools = [
                        tool for tool in original_tools.list() if tool.name in registered_names
                    ]
                    if len(coding_tools) != len(registered_names) or not coding_tools:
                        raise RuntimeError("Automated Coding tools are unavailable")
                    original_permission_policy = harness.permission_policy
                    harness.agent.tools = ToolRegistry(coding_tools)
                    harness.set_permission_policy(AllowAllToolPermissionPolicy())

                    def _restore_coding_mode() -> None:
                        harness.agent.tools = original_tools
                        harness.set_permission_policy(original_permission_policy)

                    stack.callback(_restore_coding_mode)
                if (
                    provider_bound_operation is None
                    and validated.knowledge_conversation is not None
                ):
                    from .wiki.knowledge_agent import (
                        KnowledgeAgentBinding,
                        knowledge_agent_binding,
                    )

                    knowledge_tools = state.wiki_knowledge_tools
                    if knowledge_tools is None:
                        raise RuntimeError("Knowledge Agent tools unavailable")
                    original_tools = harness.agent.tools
                    binding_token = knowledge_agent_binding.set(
                        KnowledgeAgentBinding(
                            conversation_id=validated.knowledge_conversation.id,
                            space_id=validated.knowledge_conversation.space_id,
                            session_id=validated.knowledge_conversation.session_id,
                        )
                    )
                    harness.agent.tools = knowledge_tools

                    def _restore_knowledge_mode() -> None:
                        harness.agent.tools = original_tools
                        knowledge_agent_binding.reset(binding_token)

                    stack.callback(_restore_knowledge_mode)
                if runtime is not None and selection is not None:
                    await stack.enter_async_context(
                        runtime.bind_to_harness(harness=harness, selection=selection)
                    )
                force_coding_bootstrap = bool(
                    validated.coding_mode
                    and validated.intent is not None
                    and validated.intent.reason_code == "coding_artifact_request"
                )
                if (
                    provider_bound_operation is None
                    and validated.coding_mode
                    and (coding_repair or force_coding_bootstrap)
                ):
                    from .coding_sandbox.automation import CodingToolBootstrapModelClient

                    repair_delegate = harness.agent.client
                    harness.agent.client = CodingToolBootstrapModelClient(
                        repair_delegate,
                        list_first=force_coding_bootstrap and not coding_repair,
                    )

                    def _restore_coding_repair_client() -> None:
                        harness.agent.client = repair_delegate

                    stack.callback(_restore_coding_repair_client)

                if provider_bound_operation is not None:
                    return await provider_bound_operation()
                if checkpoint_source is not None:
                    return await generate_checkpoint_memory(
                        harness.agent.client,
                        source=checkpoint_source,
                        prior_memory=checkpoint_prior_memory,
                        operation=checkpoint_operation,
                    )
                if suppress_user_append:
                    # Regenerate 路径——caller 已设置 harness.agent.state.messages
                    messages = await harness.run_continue(
                        skill_selection=validated.skill_selection,
                        system_prompt_suffix=prompt_suffix,
                    )
                elif validated.attached_blocks:
                    from ..messages import TextContent, UserMessage

                    user_msg = UserMessage(
                        content=[
                            TextContent(text=validated.text),
                            *validated.attached_blocks,
                        ]
                    )
                    harness.agent.state.messages.append(user_msg)
                    messages = await harness.run_continue(
                        skill_selection=validated.skill_selection,
                        system_prompt_suffix=prompt_suffix,
                    )
                else:
                    messages = await harness.run_prompt(
                        validated.text,
                        skill_selection=validated.skill_selection,
                        system_prompt_suffix=prompt_suffix,
                    )
        except ProviderSelectionNotFoundError:
            state.last_error = "Selected provider profile is unavailable."
            raise PromptRuntimeError(
                500, state.last_error, "provider_profile_unavailable"
            ) from None
        except ProviderSelectionDisabledError:
            state.last_error = "Selected provider profile is disabled."
            raise PromptRuntimeError(500, state.last_error, "provider_profile_disabled") from None
        except ProviderSelectionUnavailableError:
            state.last_error = "Selected provider credential is unavailable."
            raise PromptRuntimeError(
                500, state.last_error, "provider_credential_unavailable"
            ) from None
        except ProviderInitializationError:
            state.last_error = "Selected provider could not be initialized."
            raise PromptRuntimeError(
                500, state.last_error, "provider_initialization_failed"
            ) from None
        except (CheckpointerError, PromptRuntimeError):
            raise
        except RuntimeError as e:
            msg = str(e)
            state.last_error = f"{type(e).__name__}: {msg}"
            status = 409 if "already running" in msg.lower() else 500
            raise PromptRuntimeError(status, state.last_error, type(e).__name__) from None
        except Exception as e:
            state.last_error = f"{type(e).__name__}: {e}"
            raise PromptRuntimeError(500, state.last_error, type(e).__name__) from None
        finally:
            if manage_running_state:
                state.running = False

        messages_after = list(harness.agent.state.messages)
        # candidate 提取：从 suffix 中找最后一个合格 AssistantMessage
        assistant_candidate = _extract_terminal_assistant(messages_after[len(messages_before) :])

        # 终态 AssistantMessage 是 stop_reason / usage 的事实来源。RequestSnapshot
        # metadata 从未承诺包含这两个字段；usage 已随消息和 TurnSnapshot 持久化。
        snapshot = harness.last_snapshot
        snapshot_payload: dict[str, Any] | None = None
        stop_reason: str | None = (
            assistant_candidate.stop_reason if assistant_candidate is not None else None
        )
        usage: Any | None = (
            assistant_candidate.usage.model_dump(mode="json")
            if assistant_candidate is not None
            else None
        )
        if snapshot is not None:
            try:
                # to_dict 是 dataclass method；可能抛异常——best-effort
                snapshot_payload = snapshot.to_dict()
            except Exception:
                snapshot_payload = None

        applied_skill_names: list[str] = (
            list(validated.skill_selection.names)
            if (validated.skill_selection is not None and validated.skill_selection.names)
            else []
        )

        attachment_meta = _build_attachment_meta(validated.attached_summary)

        return PromptExecutionResult(
            messages=list(messages),
            assistant_message=assistant_candidate,
            messages_before=messages_before,
            messages_after=messages_after,
            stop_reason=stop_reason,
            usage=usage,
            snapshot_payload=snapshot_payload,
            result_summary={
                "applied_skill_names": applied_skill_names,
                "attachment_meta": attachment_meta,
                "session_id": validated.session_id,
                "workspace_context": workspace_context_metadata,
            },
        )

    async def _persist_normal_prompt_result(
        validated: _PromptValidated,
        execution: PromptExecutionResult,
    ) -> PromptRunOutcome:
        """D2-4：普通 prompt 持久化路径——replace_messages + append_snapshot。

        保留旧语义：历史 message ID 由 diff-based replace_messages 保持；
        snapshot 失败仍记 state.last_error 但不影响主流程（旧 wrapper 兼容）。

        finally 恢复 harness.agent.state.messages 到 validated.original_messages
        （保持旧行为——普通 prompt 不需要从 SQLite reload，因为 replace_messages
        已经把 final_messages 写回，agent state 与 DB 一致）。
        """
        session_persisted = False
        if validated.store is not None and validated.session_id is not None:
            try:
                await validated.store.replace_messages(
                    validated.session_id, list(execution.messages)
                )
                session_persisted = True
                if harness.last_snapshot is not None:
                    await validated.store.append_snapshot(
                        validated.session_id, harness.last_snapshot
                    )
            except Exception as e:
                state.last_error = f"persist: {type(e).__name__}: {e}"
            finally:
                if validated.original_messages is not None:
                    harness.agent.state.messages = validated.original_messages

        applied_skill_names: list[str] = (
            list(validated.skill_selection.names)
            if (validated.skill_selection is not None and validated.skill_selection.names)
            else []
        )
        attachment_meta = _build_attachment_meta(validated.attached_summary)

        return PromptRunOutcome(
            messages=execution.messages,
            serialized_messages=[serialize_message(m) for m in execution.messages],
            session_id=validated.session_id,
            attachment_meta=attachment_meta,
            applied_skill_names=applied_skill_names,
            workspace_context=(
                execution.result_summary.get("workspace_context")
                if execution.result_summary is not None
                else None
            ),
            intent=validated.intent.public() if validated.intent is not None else None,
            turn_messages=tuple(
                execution.messages_after[len(execution.messages_before) :]
            ),
            session_persisted=session_persisted,
        )

    async def _persist_regeneration_result(
        validated: _PromptValidated,
        execution: PromptExecutionResult,
        *,
        revision_id: str,
        request_id: str,
    ) -> PromptRunOutcome:
        """D2-4：regenerate 持久化路径——finalize_revision + best-effort snapshot。

        **不**调用 replace_messages（finalize 内部 UPDATE messages.content_json）。
        **不**创建第二条 assistant row——通过 messages.id 不变保证。

        Snapshot 失败语义（D2-4 固定）：
        - revision finalize commit = 核心事务，必须成功
        - snapshot append = best-effort——失败时记 state.last_error，**不**改
          revision 状态（仍 completed），**不**改 request 状态（仍 completed）

        Candidate 缺失（execution.assistant_message is None）：
        - 抛 ValueError（caller 在 revision mark_error 后转 request error）
        """
        from .extension_store import RevisionBaseContentChangedError

        if execution.assistant_message is None:
            raise ValueError("regeneration produced no qualified assistant candidate")

        # candidate canonical 序列化——{type, data} 包装格式（与 messages 表对齐）
        candidate_json = _serialize_assistant_for_messages(execution.assistant_message)

        # 核心：finalize_revision（单 BEGIN IMMEDIATE transaction）
        try:
            await state.extension_store.finalize_revision(
                revision_id=revision_id,
                request_id=request_id,
                candidate_content_json=candidate_json,
            )
        except RevisionBaseContentChangedError:
            # 重新抛出——caller 在独立 transaction 中决定是否 mark_error
            raise

        # Snapshot append 是 best-effort（finalize 已成功则 request 必须 completed）
        snapshot_error: str | None = None
        if (
            validated.store is not None
            and validated.session_id is not None
            and harness.last_snapshot is not None
        ):
            try:
                await validated.store.append_snapshot(validated.session_id, harness.last_snapshot)
            except Exception as e:
                # finalize 已 commit——snapshot 失败不能回滚 active answer
                snapshot_error = f"snapshot: {type(e).__name__}: {e}"
                state.last_error = snapshot_error

        applied_skill_names: list[str] = (
            list(validated.skill_selection.names)
            if (validated.skill_selection is not None and validated.skill_selection.names)
            else []
        )
        attachment_meta = _build_attachment_meta(validated.attached_summary)

        return PromptRunOutcome(
            messages=execution.messages,
            serialized_messages=[serialize_message(m) for m in execution.messages],
            session_id=validated.session_id,
            attachment_meta=attachment_meta,
            applied_skill_names=applied_skill_names,
            workspace_context=(
                execution.result_summary.get("workspace_context")
                if execution.result_summary is not None
                else None
            ),
            intent=validated.intent.public() if validated.intent is not None else None,
            session_persisted=True,
        )

    async def _reset_harness_to_session(
        store: Any,
        session_id: str,
        fallback_messages: list[Any],
    ) -> None:
        """D2-4：把 harness.agent.state.messages 重置到 SQLite canonical state。

        规则：
        - 优先 `store.list_messages(session_id)`——SQLite 是最终真源
        - 读失败时恢复 fallback_messages（不修改 DB）
        - reset 失败本身**不**再次修改 DB
        - 错误写入 state.last_error（安全摘要，不含正文/绝对路径）

        所有 regenerate 路径退出时（成功 / 错误 / 中止 / finalize 失败）都必须调用。
        """
        try:
            canonical = await store.list_messages(session_id)
            harness.agent.state.messages = list(canonical)
        except Exception as e:
            state.last_error = f"reset_harness: {type(e).__name__}: {e}"[:500]
            harness.agent.state.messages = list(fallback_messages)

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
            # D2-5：operation + regenerate 关联（向后兼容）
            "operation": req.operation,
            "regeneration_id": req.regeneration_id,
            "target_message_id": req.target_message_id,
            "awaiting_approval": approval_manager.pending_count(req.id) > 0,
            "pending_approval_count": approval_manager.pending_count(req.id),
            "approvals_url": f"/api/requests/{req.id}/approvals",
        }

    def _find_request(request_id: str) -> WebRunRequest | None:
        """从 active + history 找 request record。"""
        req = state.active_requests.get(request_id)
        if req is not None:
            return cast(WebRunRequest, req)
        for r in state.request_history:
            candidate = cast(WebRunRequest, r)
            if candidate.id == request_id:
                return candidate
        return None

    def _remove_from_active(req: WebRunRequest) -> None:
        """从 active_requests / active_request_by_session 移除；保留 history append 给调用方做。"""
        state.active_requests.pop(req.id, None)
        if req.session_id and state.active_request_by_session.get(req.session_id) == req.id:
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
        tool_session_token = tool_session_context.set(web_request.session_id)
        # 占位 sequence 起点——下一个分配的 sequence 将是此值
        web_request.event_start_sequence = state.next_event_sequence

        async def _emit_terminal_error(message: str, error_type: str) -> None:
            try:
                await _emit_web_payload(
                    {
                        "type": "error",
                        "message": message,
                        "error_type": error_type,
                    },
                    web_request.id,
                    web_request.session_id,
                )
            except Exception:
                # The durable request status remains authoritative when a
                # disconnected client cannot receive the terminal event.
                return

        try:
            result = await _run_prompt_request(
                validated,
                abort_requested=lambda: web_request.abort_reason is not None,
            )
        except asyncio.CancelledError:
            web_request.status = "aborted"
            web_request.ended_at = _now_utc()
            web_request.error = "cancelled"
            if web_request.abort_reason is None:
                web_request.abort_reason = "task_cancelled"
            raise
        except PromptRuntimeError as e:
            web_request.status = (
                "aborted" if web_request.abort_reason is not None else "error"
            )
            web_request.ended_at = _now_utc()
            web_request.error = e.message
            web_request.error_type = e.error_type
            await _emit_terminal_error(e.message, e.error_type)
        except HTTPException as e:
            # _run_prompt_core 内部不应抛 HTTPException，但兜底
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = "HTTPException"
            await _emit_terminal_error(web_request.error, web_request.error_type)
        except Exception as e:
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = type(e).__name__
            await _emit_terminal_error(web_request.error, web_request.error_type)
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
                "coding_sandbox": result.coding_sandbox,
                "continuity": result.continuity,
                "workspace_context": result.workspace_context,
                "intent": result.intent,
                "plan_run": result.plan_run,
                # 不放 message 全文（安全 + 内存）
            }
        finally:
            # 记录最后一个 event 的 sequence（next - 1；如果没事件则 = start - 1）
            web_request.event_end_sequence = state.next_event_sequence - 1
            # clear request context——避免非 prompt 事件误关联
            state.current_request_id = None
            state.current_request_session_id = None
            tool_session_context.reset(tool_session_token)
            _remove_from_active(web_request)
            state.request_history.append(web_request)

    async def _run_checkpointer_core(
        session_id: str,
        source: CheckpointSource,
        operation_id: str,
    ) -> dict[str, Any]:
        """Publish Memory.md, then atomically finish and reset its source lane."""
        store = state.session_store
        file_store = state.file_store
        if store is None or file_store is None:
            raise CheckpointerError(
                "checkpointer_unavailable",
                "Session store or file store is not initialized.",
            )

        lock = checkpointer_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            memory_ref = await file_store.get_by_logical_path(
                session_id,
                SESSION_MEMORY_PATH,
            )
            prior_text: str | None = None
            if memory_ref is not None:
                prior_text = Path(memory_ref.path).read_text(  # noqa: ASYNC240
                    encoding="utf-8",
                    errors="replace",
                )

            already_committed = extract_checkpoint_source_hash(prior_text) == source.source_sha256
            updated_ref = memory_ref
            operation = await store.get_operation(operation_id)
            if operation is None:
                raise CheckpointerError(
                    "checkpoint_operation_missing",
                    "The durable checkpoint operation is unavailable.",
                )
            resumed_operation = operation.effect_committed

            if not already_committed:
                try:
                    checkpoint_request = _PromptValidated(
                        text="",
                        skill_selection=None,
                        session_id=session_id,
                        store=store,
                        original_messages=None,
                        attached_blocks=[],
                        attached_summary=[],
                    )
                    memory_text = cast(
                        str,
                        await _execute_prompt(
                            checkpoint_request,
                            checkpoint_source=source,
                            checkpoint_prior_memory=prior_text,
                            manage_running_state=False,
                        ),
                    )
                except PromptRuntimeError as e:
                    try:
                        await store.finish_operation(
                            operation_id,
                            outcome="failed",
                            payload={"code": e.error_type[:100]},
                        )
                    except Exception:
                        pass
                    raise CheckpointerError(e.error_type, e.message) from None

                try:
                    if memory_ref is None:
                        updated_ref = await file_store.write_text(
                            session_id,
                            SESSION_MEMORY_PATH,
                            memory_text,
                            content_type="text/markdown",
                            origin="agent",
                            purpose="memory",
                        )
                    else:
                        updated_ref = await file_store.update_text(
                            session_id,
                            memory_ref.id,
                            memory_text,
                            expected_sha256=memory_ref.sha256,
                            origin="agent",
                            purpose="memory",
                        )
                except Exception as e:
                    try:
                        await store.finish_operation(
                            operation_id,
                            outcome="failed",
                            payload={"code": "checkpoint_publish_failed"},
                        )
                    except Exception:
                        pass
                    raise CheckpointerError(
                        "checkpoint_publish_failed",
                        f"Could not publish Memory.md: {type(e).__name__}",
                    ) from None

            assert updated_ref is not None
            try:
                await store.mark_operation_effect_committed(
                    operation_id,
                    {
                        "file_id": updated_ref.id,
                        "file_sha256": updated_ref.sha256,
                        "logical_path": updated_ref.logical_path,
                    },
                )
                await store.complete_operation_and_reset_lane(operation_id)
            except Exception as e:
                # Do not compensate an acknowledged external publish. The open
                # operation plus Memory source marker is sufficient for retry or
                # startup recovery to finish exactly the original immutable leaf.
                raise CheckpointerError(
                    "checkpoint_commit_failed",
                    f"Could not clear the conversation: {type(e).__name__}",
                ) from None

            if state.current_session_id == session_id:
                harness.agent.state.messages = []
            return {
                "command": CHECKPOINTER_COMMAND,
                "memory_file_id": updated_ref.id,
                "memory_logical_path": updated_ref.logical_path,
                "source_message_count": source.message_count,
                "source_sha256": source.source_sha256,
                "durable_operation_id": operation_id,
                "idempotent_recovery": already_committed or resumed_operation,
            }

    async def _run_checkpointer_background(
        web_request: WebRunRequest,
        source: CheckpointSource,
        operation_id: str,
    ) -> None:
        """Managed async runner for /checkpointer."""
        assert web_request.session_id is not None
        web_request.status = "running"
        web_request.started_at = _now_utc()
        state.current_request_id = web_request.id
        state.current_request_session_id = web_request.session_id
        web_request.event_start_sequence = state.next_event_sequence
        try:
            result = await _run_checkpointer_core(web_request.session_id, source, operation_id)
        except asyncio.CancelledError:
            # If Memory.md crossed its commit point, cancellation must converge
            # forward; otherwise close the no-effect intent as aborted.
            store = state.session_store
            file_store = state.file_store
            if store is not None and file_store is not None:
                try:
                    await asyncio.shield(
                        recover_checkpointer_operations(
                            store,
                            file_store,
                            session_id=web_request.session_id,
                        )
                    )
                    operation = await asyncio.shield(store.get_operation(operation_id))
                except Exception:
                    operation = None
                if operation is not None and operation.outcome == "completed":
                    web_request.status = "completed"
                    web_request.result_summary = {
                        "command": CHECKPOINTER_COMMAND,
                        "source_message_count": source.message_count,
                        "source_sha256": source.source_sha256,
                        "durable_operation_id": operation_id,
                        "idempotent_recovery": True,
                    }
                    if state.current_session_id == web_request.session_id:
                        harness.agent.state.messages = []
                    web_request.ended_at = _now_utc()
                else:
                    web_request.status = "aborted"
                    web_request.error = "cancelled"
                    web_request.abort_reason = web_request.abort_reason or "task_cancelled"
                    web_request.ended_at = _now_utc()
                    raise
            else:
                web_request.status = "aborted"
                web_request.error = "cancelled"
                web_request.abort_reason = web_request.abort_reason or "task_cancelled"
                web_request.ended_at = _now_utc()
                raise
        except CheckpointerError as e:
            web_request.status = "error"
            web_request.error = e.message[:500]
            web_request.error_type = e.code
            web_request.ended_at = _now_utc()
        except Exception as e:
            web_request.status = "error"
            web_request.error = _safe_error(e)
            web_request.error_type = type(e).__name__
            web_request.ended_at = _now_utc()
        else:
            web_request.status = "completed"
            web_request.result_summary = result
            web_request.ended_at = _now_utc()
        finally:
            state.running = False
            web_request.event_end_sequence = state.next_event_sequence - 1
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
        # Wake every suspended approval handler before waiting for Agent abort.
        await approval_manager.cancel_request(req.id)

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
            if req.operation == "checkpointer":
                if req.task is not None and not req.task.done():
                    req.task.cancel()
                return {
                    "ok": True,
                    "request_id": req.id,
                    "status": req.status,
                    "abort_reason": req.abort_reason,
                }
            if harness.context.metadata.get("continuity_active") is True:
                if req.task is not None and not req.task.done():
                    req.task.cancel()
                return {
                    "ok": True,
                    "request_id": req.id,
                    "status": req.status,
                    "abort_reason": req.abort_reason,
                }
            if (
                req.payload is not None
                and req.payload.get("coding_mode") is True
                and harness.context.phase == "idle"
            ):
                # Coding preflight/finalization runs outside the Agent loop. The
                # automation polls abort_reason and cancels the owned Sandbox;
                # calling harness.abort() while idle would incorrectly fail Stop.
                return {
                    "ok": True,
                    "request_id": req.id,
                    "status": req.status,
                    "abort_reason": req.abort_reason,
                }
            # D2-5：regenerate request 的 abort 也调 harness.abort() 让模型 finalize
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

    def _harness_is_idle() -> bool:
        phase: str
        try:
            phase = harness.context.phase
        except Exception:
            phase = "unknown"
        agent_status: str
        try:
            agent_status = harness.agent.state.status
        except Exception:
            agent_status = "unknown"
        return phase == "idle" and agent_status not in ("running", "aborting")

    async def _stop_session_request_before_delete(
        session_id: str,
    ) -> JSONResponse | None:
        """Stop one Session's request before deleting any of its durable state.

        Session deletion is a destructive boundary. Returning before the owned
        background task has finished creates a TOCTOU race: the task can append
        messages after SQLite/files have been removed and keeps the single
        Harness busy for every other Session. A bounded wait keeps failure
        recoverable—the Session remains intact when termination cannot be
        confirmed.
        """
        request_id = state.active_request_by_session.get(session_id)
        if request_id is None:
            return None

        raw_request = state.active_requests.get(request_id)
        if raw_request is None:
            if _harness_is_idle():
                state.active_request_by_session.pop(session_id, None)
                if not state.active_requests:
                    state.running = False
                return None
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "active_request_state_inconsistent",
                        "message": (
                            "Session request state cannot be stopped safely; "
                            "Session was not deleted"
                        ),
                        "request_id": request_id,
                    }
                },
            )

        web_request = cast(WebRunRequest, raw_request)
        abort_result = await _abort_request_internal(web_request, "session_deleted")
        if not abort_result.get("ok", False):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "active_request_abort_failed",
                        "message": "Active request could not be aborted; Session was not deleted",
                        "request_id": request_id,
                    }
                },
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SESSION_DELETE_REQUEST_STOP_TIMEOUT_SECONDS
        task = web_request.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except asyncio.CancelledError:
                # A cancelled owned task is an expected abort outcome. Do not
                # let it cancel the HTTP request that is completing deletion.
                if not task.cancelled():
                    raise
            except TimeoutError:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": {
                            "code": "active_request_stop_timeout",
                            "message": (
                                "Timed out stopping the active request; "
                                "Session was not deleted"
                            ),
                            "request_id": request_id,
                        }
                    },
                )

        try:
            await asyncio.wait_for(
                harness.wait_for_idle(),
                timeout=max(0.0, deadline - loop.time()),
            )
        except TimeoutError:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "harness_stop_timeout",
                        "message": "Harness did not become idle; Session was not deleted",
                        "request_id": request_id,
                    }
                },
            )

        # Cancelling a queued asyncio task before its coroutine starts means
        # the runner's finally block never executes. Recover the Web reservation
        # only after both the task and Harness are confirmed idle.
        if state.active_request_by_session.get(session_id) == request_id:
            _remove_from_active(web_request)
            if web_request.status not in ("completed", "error", "aborted"):
                web_request.status = "aborted"
                web_request.abort_reason = web_request.abort_reason or "session_deleted"
                web_request.ended_at = _now_utc()
                state.request_history.append(web_request)
        if not state.active_requests and _harness_is_idle():
            state.running = False
        return None

    # ========================================================================
    # P1-D2-5: Regenerate validation + background runner
    # ========================================================================

    def _regen_error_response(e: RegenerationValidationError) -> JSONResponse:
        """D2-5：regenerate 校验错误 → 稳定 code JSONResponse。"""
        return JSONResponse(
            status_code=e.status_code,
            content={"detail": {"code": e.code, "message": e.message}},
        )

    def _regen_safe_error_response(
        e: Exception, code: str = "regenerate_start_failed"
    ) -> JSONResponse:
        """D2-5：未预期错误 → 安全 500 摘要 + 稳定 code。"""
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": code,
                    "message": _safe_error(e),
                }
            },
        )

    async def _validate_regeneration_payload(
        session_id: str,
        assistant_message_id: str,
    ) -> ValidatedRegenerationRequest:
        """D2-5：regenerate 校验——10 个错误 code 映射。

        顺序：
        1. store / extension_store / harness init 检查
        2. session 存在
        3. message 存在 + 属于 session
        4. role=assistant
        5. 最新 assistant
        6. preceding user 存在
        7. 无 active request
        8. 无 running revision

        失败抛 RegenerationValidationError（含 status_code + code + message）。
        """
        store = state.session_store
        ext_store = state.extension_store
        if store is None or ext_store is None:
            raise RegenerationValidationError(
                503,
                "extension_store_unavailable",
                "Extension store not initialized.",
            )

        # session 存在
        try:
            session = await store.get_session(session_id)
        except Exception:
            session = None
        if session is None:
            raise RegenerationValidationError(
                404,
                "session_not_found",
                f"Session {session_id!r} not found.",
            )

        # message 存在 + 属于 session + role + 最新 assistant + preceding user
        db = store.connection
        cur = await db.execute(
            "SELECT id, session_id, role, idx FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        msg_row = await cur.fetchone()
        await cur.close()
        if msg_row is None or msg_row["session_id"] != session_id:
            raise RegenerationValidationError(
                404,
                "message_not_found",
                f"Assistant message {assistant_message_id!r} not found in this session.",
            )
        if msg_row["role"] != "assistant":
            raise RegenerationValidationError(
                400,
                "regenerate_target_not_assistant",
                "Only assistant messages can be regenerated.",
            )

        # 最新 assistant
        cur = await db.execute(
            "SELECT id FROM messages "
            "WHERE session_id = ? AND role = 'assistant' "
            "ORDER BY idx DESC LIMIT 1",
            (session_id,),
        )
        latest = await cur.fetchone()
        await cur.close()
        if latest is None or latest["id"] != assistant_message_id:
            raise RegenerationValidationError(
                409,
                "regenerate_target_not_latest",
                "Only the latest assistant response can be regenerated.",
            )

        # preceding user——从 target idx 向前找最近 role=user
        cur = await db.execute(
            "SELECT id, idx FROM messages "
            "WHERE session_id = ? AND role = 'user' AND idx < ? "
            "ORDER BY idx DESC LIMIT 1",
            (session_id, msg_row["idx"]),
        )
        preceding = await cur.fetchone()
        await cur.close()
        if preceding is None:
            raise RegenerationValidationError(
                409,
                "regenerate_missing_user_message",
                "No preceding user message found to regenerate from.",
            )

        # active request 检查
        if session_id in state.active_request_by_session:
            raise RegenerationValidationError(
                409,
                "request_already_active",
                "An active request is already running for this session.",
            )

        # running revision 检查（依赖 partial unique index）
        cur = await db.execute(
            "SELECT id FROM web_message_revisions "
            "WHERE assistant_message_id = ? AND status = 'running' LIMIT 1",
            (assistant_message_id,),
        )
        existing_running = await cur.fetchone()
        await cur.close()
        if existing_running is not None:
            raise RegenerationValidationError(
                409,
                "revision_already_running",
                "A regeneration is already running for this assistant message.",
            )

        knowledge_conversation = None
        wiki_store = state.wiki_store
        if wiki_store is not None:
            try:
                knowledge_conversation = await wiki_store.find_conversation_by_session(session_id)
                if knowledge_conversation is not None:
                    space = await wiki_store.get_space(knowledge_conversation.space_id)
                    if knowledge_conversation.status != "active" or space.status != "active":
                        raise RegenerationValidationError(
                            409,
                            "knowledge_conversation_inactive",
                            "Knowledge conversation is not active.",
                        )
            except RegenerationValidationError:
                raise
            except Exception:
                raise RegenerationValidationError(
                    409,
                    "knowledge_conversation_conflict",
                    "Knowledge conversation binding is unavailable.",
                ) from None

        # 构造 history——canonical active messages[:target_idx]（不含旧 assistant）
        canonical = await store.list_messages(session_id)
        history = tuple(canonical[: msg_row["idx"]])

        return ValidatedRegenerationRequest(
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            preceding_user_message_id=preceding["id"],
            history=history,
            original_harness_messages=tuple(harness.agent.state.messages),
            knowledge_conversation=knowledge_conversation,
        )

    async def _run_regeneration_background(
        web_request: WebRunRequest,
        validated: ValidatedRegenerationRequest,
    ) -> None:
        """D2-5：regenerate 后台 task 入口——基于 _run_prompt_background 模式。

        生命周期：queued → running → _execute_prompt → finalize → reset → completed
        异常映射：
            - asyncio.CancelledError → revision.aborted + request.aborted（重抛）
            - PromptRuntimeError → revision.error + request.error
            - RevisionBaseContentChangedError / RevisionError → revision.error + request.error
            - 其它 Exception → revision.error + request.error（safe summary）

        Snapshot 失败（finalize 已成功）→ request 仍 completed（_persist 处理）。
        """
        from .extension_store import (
            RevisionBaseContentChangedError,
            RevisionError,
        )

        ext_store = state.extension_store
        revision_id = web_request.regeneration_id
        assert revision_id is not None, "regenerate request missing regeneration_id"
        request_id = web_request.id

        web_request.status = "running"
        web_request.started_at = _now_utc()
        state.running = True
        state.current_request_id = request_id
        state.current_request_session_id = web_request.session_id
        tool_session_token = tool_session_context.set(web_request.session_id)
        web_request.event_start_sequence = state.next_event_sequence

        # 构造 _PromptValidated 视图（_run_regeneration_core 需要 skill_selection 等）
        prompt_validated = _PromptValidated(
            text="",  # regenerate 不用 text
            skill_selection=None,
            session_id=validated.session_id,
            store=state.session_store,
            original_messages=list(validated.original_harness_messages),
            attached_blocks=[],
            attached_summary=[],
            knowledge_conversation=validated.knowledge_conversation,
        )

        try:
            result = await _run_regeneration_core(
                prompt_validated,
                revision_id=revision_id,
                assistant_message_id=validated.assistant_message_id,
                request_id=request_id,
            )
        except asyncio.CancelledError:
            # 显式 mark_revision_aborted（不让通用 except 捕获）
            try:
                await ext_store.mark_revision_aborted(
                    revision_id=revision_id,
                    request_id=request_id,
                )
            except Exception:
                pass  # best-effort——revision 可能已被 mark
            web_request.status = "aborted"
            web_request.ended_at = _now_utc()
            web_request.error = "cancelled"
            if web_request.abort_reason is None:
                web_request.abort_reason = "task_cancelled"
            raise
        except (
            PromptRuntimeError,
            RevisionBaseContentChangedError,
            RevisionError,
            ValueError,
        ) as e:
            # 已知执行错误 → revision.error
            try:
                await ext_store.mark_revision_error(
                    revision_id=revision_id,
                    request_id=request_id,
                    error_summary=_safe_error(e),
                )
            except Exception:
                pass
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = type(e).__name__
        except Exception as e:
            # 未预期异常 → revision.error + safe summary
            try:
                await ext_store.mark_revision_error(
                    revision_id=revision_id,
                    request_id=request_id,
                    error_summary=_safe_error(e),
                )
            except Exception:
                pass
            web_request.status = "error"
            web_request.ended_at = _now_utc()
            web_request.error = _safe_error(e)
            web_request.error_type = type(e).__name__
        else:
            # 成功——但检查 abort flag（abort 不 cancel task 走收敛路径）
            if web_request.abort_reason is not None:
                web_request.status = "aborted"
            else:
                web_request.status = "completed"
            web_request.ended_at = _now_utc()
            web_request.result_summary = {
                "regeneration_id": revision_id,
                "assistant_message_id": validated.assistant_message_id,
                "session_id": validated.session_id,
                "continuity": result.continuity,
                "workspace_context": result.workspace_context,
            }
        finally:
            state.running = False
            web_request.event_end_sequence = state.next_event_sequence - 1
            state.current_request_id = None
            state.current_request_session_id = None
            tool_session_context.reset(tool_session_token)
            _remove_from_active(web_request)
            state.request_history.append(web_request)

    # ========================================================================
    # Static + index
    # ========================================================================

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_html = _STATIC_DIR / "index.html"
        if index_html.is_file():
            return FileResponse(index_html)
        return HTMLResponse(_FALLBACK_HTML, media_type="text/html")

    @app.get("/chat", include_in_schema=False)
    @app.get("/chat/", include_in_schema=False)
    @app.get("/chat/{session_path:path}", include_in_schema=False)
    async def chat_route(session_path: str | None = None) -> Any:
        """Serve the SPA shell; the client validates and normalizes the route."""
        del session_path
        return await index()

    @app.get("/knowledge", include_in_schema=False)
    @app.get("/knowledge/", include_in_schema=False)
    @app.get("/knowledge/{knowledge_path:path}", include_in_schema=False)
    async def knowledge_route(knowledge_path: str | None = None) -> Any:
        """Serve the independent LLM Wiki SPA shell on refresh and direct entry."""
        del knowledge_path
        return await index()

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
        agent_state = agent.state
        return {
            "running": state.running,
            "last_error": state.last_error,
            "agent_status": _agent_status(),
            "queue_size": agent_state.queue_size,
            "turn_count": agent_state.turn_count,
            "message_count": len(agent_state.messages),
            "model": agent_state.model.model_dump(mode="json"),
            "thinking_level": agent_state.thinking_level,
            "is_streaming": agent_state.is_streaming,
            "streaming_message": (
                agent_state.streaming_message.model_dump(mode="json")
                if agent_state.streaming_message is not None
                else None
            ),
            "pending_tool_calls": sorted(agent_state.pending_tool_calls),
            "error_message": agent_state.error_message,
            "snapshot_count": len(harness.snapshots),
            "event_count": len(state.event_buffer),
            "durable_recovery": dict(state.durable_recovery_summary),
            "auto_memory": {
                "enabled": state.auto_memory_enabled,
                "recovery": dict(state.continuity_recovery_summary),
            },
            "code_continuity": {
                "enabled": state.code_continuity_enabled,
            },
            "intent_routing": {
                "enabled": state.intent_routing_enabled,
                "routes": ["read_only", "coding", "knowledge"],
            },
            "plan_mode": {
                "enabled": state.plan_mode_enabled,
                "execution_modes": ["direct", "plan"],
            },
        }

    # ========================================================================
    # Messages
    # ========================================================================

    @app.get("/api/messages", response_model=None)
    async def get_messages(
        session_id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """列出 messages。

        P0-1：支持 `?session_id=` 查 sqlite 历史消息。
        - 不传 session_id → 返回当前 agent.state.messages（fallback 旧路径，
          无 message_id——这些 message 不在 DB 中）
        - 传 session_id → 返回 sqlite 中该 session 的 messages（**D2-6 起含
          message_id**——使用 PersistedMessage DTO，按 idx 升序，强类型对象）

        若指定 session 不存在，返回 404。
        """
        if session_id is None:
            msgs = list(harness.agent.state.messages)
            serialized_messages = [serialize_message(m) for m in msgs]
            return {
                "count": len(msgs),
                "messages": serialized_messages,
                "content_integrity": summarize_content_integrity(serialized_messages),
            }

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        from ..session_sqlite import SessionNotFoundError
        from .serializers import serialize_persisted_message

        try:
            # D2-6：用 list_persisted_messages——含 message_id（regenerate 必需）
            stored_msgs = await store.list_persisted_messages(session_id)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {session_id!r} not found"},
            )
        serialized_messages = [serialize_persisted_message(message) for message in stored_msgs]
        return {
            "count": len(stored_msgs),
            "session_id": session_id,
            "messages": serialized_messages,
            "content_integrity": summarize_content_integrity(serialized_messages),
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
                {**serialize_snapshot_summary(s), "index": i} for i, s in enumerate(snaps)
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
                "active_lane": s.active_lane,
                "is_current": s.id == state.current_session_id,
            }
            for s in sessions
        ]
        return {"count": len(items), "sessions": items}

    def _raise_session_tree_error(error: Exception) -> NoReturn:
        from ..session_sqlite import (
            SessionBranchError,
            SessionEntryNotFoundError,
            SessionLaneExistsError,
            SessionLaneNotFoundError,
            SessionNotFoundError,
        )

        if isinstance(
            error,
            (SessionNotFoundError, SessionEntryNotFoundError, SessionLaneNotFoundError),
        ):
            raise HTTPException(status_code=404, detail=str(error)) from None
        if isinstance(error, (SessionBranchError, SessionLaneExistsError)):
            raise HTTPException(status_code=409, detail=str(error)) from None
        if isinstance(error, ValueError):
            raise HTTPException(status_code=422, detail=str(error)) from None
        raise error

    def _ensure_session_tree_idle(sid: str) -> None:
        if sid in state.active_request_by_session:
            raise HTTPException(
                status_code=409,
                detail="session tree cannot change while a request is active",
            )

    async def _sync_current_session_tree_projection(sid: str) -> None:
        if state.current_session_id != sid or state.session_store is None:
            return
        from ..messages import AgentMessage

        messages = await state.session_store.list_messages(sid)
        harness.agent.state.messages = cast(list[AgentMessage], list(messages))

    @app.get("/api/sessions/{sid}")
    async def get_session_by_id(sid: str) -> dict[str, Any]:
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        session = await store.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "metadata": session.metadata,
            "active_lane": session.active_lane,
            "is_current": session.id == state.current_session_id,
        }

    @app.get("/api/sessions/{sid}/tree")
    async def get_session_tree(
        sid: str,
        lane: str | None = None,
        include_all: bool = False,
    ) -> dict[str, Any]:
        """读取 lane path；``include_all`` 额外返回完整 append-only tree。"""
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        try:
            session = await store.get_session(sid)
            if session is None:
                from ..session_sqlite import SessionNotFoundError

                raise SessionNotFoundError(f"session {sid!r} 不存在")
            selected_lane = lane or session.active_lane
            lanes = await store.list_lanes(sid)
            entries = await store.list_entries(sid, selected_lane)
            all_entries = await store.list_all_entries(sid) if include_all else None
        except Exception as error:
            _raise_session_tree_error(error)

        def serialize_entry(entry: Any) -> dict[str, Any]:
            return {
                "id": entry.id,
                "session_id": entry.session_id,
                "seq": entry.seq,
                "parent_id": entry.parent_id,
                "message_id": entry.message_id,
                "role": entry.role,
                "message": serialize_message(entry.message),
                "created_at": entry.created_at,
                "label": entry.label,
            }

        return {
            "session_id": sid,
            "active_lane": session.active_lane,
            "lane": selected_lane,
            "leaf_entry_id": entries[-1].id if entries else None,
            "lanes": [lane_item.model_dump(mode="json") for lane_item in lanes],
            "entries": [serialize_entry(entry) for entry in entries],
            **(
                {"all_entries": [serialize_entry(entry) for entry in all_entries]}
                if all_entries is not None
                else {}
            ),
        }

    @app.post("/api/sessions/{sid}/fork")
    async def post_session_fork(
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        _ensure_session_tree_idle(sid)
        name = payload.get("name")
        source_lane = payload.get("source_lane")
        activate = payload.get("activate", False)
        if not isinstance(name, str):
            raise HTTPException(status_code=422, detail="name must be a string")
        if source_lane is not None and not isinstance(source_lane, str):
            raise HTTPException(status_code=422, detail="source_lane must be a string")
        if not isinstance(activate, bool):
            raise HTTPException(status_code=422, detail="activate must be a boolean")
        try:
            if "at_entry_id" in payload:
                at_entry_id = payload["at_entry_id"]
                if at_entry_id is not None and not isinstance(at_entry_id, str):
                    raise ValueError("at_entry_id must be a string or null")
            else:
                at_entry_id = await store.get_active_leaf(sid, source_lane)
            lane_item = await store.fork(
                sid,
                name,
                at_entry_id=at_entry_id,
                source_lane=source_lane,
                activate=activate,
            )
            if activate:
                await _sync_current_session_tree_projection(sid)
        except Exception as error:
            _raise_session_tree_error(error)
        return {"lane": lane_item.model_dump(mode="json")}

    @app.post("/api/sessions/{sid}/branch")
    async def post_session_branch(
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        _ensure_session_tree_idle(sid)
        entry_id = payload.get("entry_id")
        lane = payload.get("lane")
        if entry_id is not None and not isinstance(entry_id, str):
            raise HTTPException(status_code=422, detail="entry_id must be a string or null")
        if lane is not None and not isinstance(lane, str):
            raise HTTPException(status_code=422, detail="lane must be a string")
        try:
            lane_item = await store.branch(sid, entry_id, lane=lane)
            if lane_item.is_active:
                await _sync_current_session_tree_projection(sid)
        except Exception as error:
            _raise_session_tree_error(error)
        return {"lane": lane_item.model_dump(mode="json")}

    @app.patch("/api/sessions/{sid}/active-lane")
    async def patch_session_active_lane(
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        _ensure_session_tree_idle(sid)
        lane = payload.get("lane")
        if not isinstance(lane, str):
            raise HTTPException(status_code=422, detail="lane must be a string")
        try:
            lane_item = await store.set_active_lane(sid, lane)
            await _sync_current_session_tree_projection(sid)
        except Exception as error:
            _raise_session_tree_error(error)
        return {"lane": lane_item.model_dump(mode="json")}

    @app.put("/api/sessions/{sid}/entries/{entry_id}/label")
    async def put_session_entry_label(
        sid: str,
        entry_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        store = state.session_store
        if store is None:
            raise HTTPException(status_code=503, detail="session store unavailable")
        label = payload.get("label")
        if label is not None and not isinstance(label, str):
            raise HTTPException(status_code=422, detail="label must be a string or null")
        try:
            entry = await store.set_label(sid, entry_id, label)
        except Exception as error:
            _raise_session_tree_error(error)
        return {"entry_id": entry.id, "label": entry.label}

    @app.get("/api/sessions/{sid}/context-budget")
    async def get_session_context_budget(sid: str) -> dict[str, Any]:
        return await _estimate_session_context_budget(session_id=sid)

    @app.post("/api/sessions/{sid}/context-budget/estimate")
    async def post_session_context_budget_estimate(
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        text = (payload or {}).get("text") or ""
        file_ids = (payload or {}).get("file_ids") or []
        skill_names = (payload or {}).get("skill_names") or []
        coding_mode = (payload or {}).get("coding_mode", False)
        intent_mode = (payload or {}).get("intent_mode")
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail="text must be a string")
        if not isinstance(file_ids, list) or any(
            not isinstance(value, str) or not value for value in file_ids
        ):
            raise HTTPException(status_code=422, detail="file_ids must be strings")
        if not isinstance(skill_names, list) or any(
            not isinstance(value, str) or not value for value in skill_names
        ):
            raise HTTPException(status_code=422, detail="skill_names must be strings")
        if not isinstance(coding_mode, bool):
            raise HTTPException(status_code=422, detail="coding_mode must be a boolean")
        if intent_mode is not None and not isinstance(intent_mode, str):
            raise HTTPException(status_code=422, detail="intent_mode must be a string")
        if len(text) > 1_000_000 or len(file_ids) > 100 or len(skill_names) > 100:
            raise HTTPException(status_code=413, detail="context estimate payload too large")
        if skill_names and harness.skill_registry is not None:
            missing = [name for name in skill_names if not harness.skill_registry.has(name)]
            if missing:
                raise HTTPException(status_code=400, detail="unknown skill")
        selection = SkillSelection(names=skill_names) if skill_names else None
        return await _estimate_session_context_budget(
            session_id=sid,
            draft_text=text,
            file_ids=file_ids,
            skill_selection=selection,
            coding_mode=coding_mode,
            intent_mode=intent_mode,
        )

    @app.post("/api/sessions/{sid}/context/compact")
    async def post_session_context_compact(
        sid: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        keep_turns = (payload or {}).get("keep_last_n_turns", 4)
        if not isinstance(keep_turns, int) or isinstance(keep_turns, bool):
            raise HTTPException(status_code=422, detail="keep_last_n_turns must be an integer")
        if keep_turns < 0 or keep_turns > 20:
            raise HTTPException(
                status_code=422,
                detail="keep_last_n_turns must be between 0 and 20",
            )
        keep_tokens = (payload or {}).get("keep_recent_tokens")
        if keep_tokens is not None and (
            not isinstance(keep_tokens, int) or isinstance(keep_tokens, bool)
        ):
            raise HTTPException(status_code=422, detail="keep_recent_tokens must be an integer")
        if keep_tokens is not None and (keep_tokens < 0 or keep_tokens > 10_000_000):
            raise HTTPException(
                status_code=422,
                detail="keep_recent_tokens must be between 0 and 10000000",
            )

        # Reserve the same single-writer slot as prompt validation. This prevents
        # a prompt from starting between the idle check and SQLite replacement.
        budget_before, context_estimate = await _estimate_session_context_budget_details(
            session_id=sid,
        )
        _ensure_idle()
        state.running = True
        result = None
        try:
            store = state.session_store
            if store is None:
                raise HTTPException(status_code=503, detail="session store unavailable")
            from pydantic import TypeAdapter

            from ..compaction import CompactionConfig, compact_messages
            from ..messages import AgentMessage

            try:
                messages = list(await store.list_messages(sid))
                snapshots = list(await store.list_snapshots(sid))
            except Exception as exc:
                from ..session_sqlite import SessionNotFoundError

                if isinstance(exc, SessionNotFoundError):
                    raise HTTPException(status_code=404, detail="session not found") from None
                raise
            result = await compact_messages(
                messages,
                snapshots=snapshots,
                config=CompactionConfig(
                    min_messages_to_compact=2,
                    boundary_mode="turn",
                    keep_last_n_turns=keep_turns,
                    keep_recent_tokens=keep_tokens,
                    max_summary_chars=4000,
                    metadata={"trigger": "user"},
                ),
                context_estimate=context_estimate,
            )
            if not result.applied or result.summary_message is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "nothing_to_compact",
                        "reason": result.reason,
                    },
                )
            adapter: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)
            replacement = [adapter.validate_python(item) for item in result.new_messages]
            await store.replace_messages(sid, replacement)
            if state.current_session_id == sid:
                harness.agent.state.messages = replacement
        finally:
            state.running = False

        budget = await _estimate_session_context_budget(session_id=sid)
        assert result is not None and result.source is not None
        return {
            "ok": True,
            "session_id": sid,
            "summary_message": result.summary_message.model_dump(mode="json"),
            "source_message_count": result.source.source_message_count,
            "compacted_message_count": result.source.compacted_message_count,
            "retained_message_count": result.source.retained_message_count,
            "snapshots_retained": len(result.source.source_snapshot_ids),
            "token_stats": (
                result.token_stats.model_dump(mode="json")
                if result.token_stats is not None
                else None
            ),
            "budget_before": budget_before,
            "budget": budget,
        }

    @app.post("/api/sessions", response_model=None)
    async def post_sessions(
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """创建新 session（spec endpoint，P0-1）。

        Body: `{"title": "..."}` （title 可选）

        P1-E2-3B3: After Session row commits, snapshot the current default
        Provider Profile into a SessionModelBinding (source="default").
        Binding failure triggers shielded compensation delete per E2-3A audit.
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

        # P1-E2-3B3: Snapshot default Profile/Model into a binding.
        # Provider config runtime is optional (legacy create_app without flags
        # has it disabled). When None, Session is created without binding.
        pc_runtime = getattr(request.app.state, "provider_config_runtime", None)
        if pc_runtime is not None:
            try:
                await pc_runtime.service.initialize_new_session_binding(
                    session_id=s.id,
                )
            except asyncio.CancelledError:
                # Client disconnect during binding write—shield compensation
                await _compensate_delete_session(state, s.id)
                raise
            except Exception:
                # Binding failed (FK race / Store error / etc.)—compensate.
                try:
                    await asyncio.shield(_compensate_delete_session(state, s.id))
                except Exception:
                    _logger.critical(
                        "session_creation_rollback_failed",
                        extra={"error_code": "session_creation_rollback_failed"},
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": {
                                "code": "session_creation_rollback_failed",
                                "message": "Session creation rollback failed.",
                            }
                        },
                    )
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "default_binding_failed",
                            "message": "Default binding initialization failed.",
                        }
                    },
                )

        # Session = conversation + managed folder. Keep Provider binding as the
        # immediate post-commit operation above; initialize the folder only after
        # the binding succeeds, then compensate the entire Session on failure.
        if state.file_store is not None:
            try:
                await state.file_store.ensure_session_workspace(s.id)
            except asyncio.CancelledError:
                await asyncio.shield(_compensate_delete_session(state, s.id))
                raise
            except Exception:
                try:
                    await asyncio.shield(_compensate_delete_session(state, s.id))
                except Exception:
                    _logger.critical(
                        "session_creation_rollback_failed",
                        extra={"error_code": "session_creation_rollback_failed"},
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": {
                                "code": "session_creation_rollback_failed",
                                "message": "Session creation rollback failed.",
                            }
                        },
                    )
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "code": "session_folder_initialization_failed",
                            "message": "Session folder initialization failed.",
                        }
                    },
                )

        return {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
            "active_lane": s.active_lane,
        }

    @app.patch("/api/sessions/{sid}", response_model=None)
    async def patch_session(
        sid: str,
        payload: dict[str, Any],
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
                status_code=400,
                content={"detail": "title is required"},
            )
        try:
            s = await store.rename_session(sid, title)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        return {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "metadata": s.metadata,
            "active_lane": s.active_lane,
        }

    @app.delete("/api/sessions/{sid}", response_model=None)
    async def delete_session(
        sid: str,
    ) -> dict[str, Any] | JSONResponse:
        """删除 session（spec endpoint，P0-1）。级联删除 messages / snapshots / 上传文件。

        活动请求必须先 abort 并确认 Harness idle；停止失败时保留 Session。
        P0-2：确认停稳后先删 uploads/{sid}/，再删 sqlite session——避免孤儿目录。
        文件删除失败不阻塞 sqlite 删除（记 warning 到 metadata）。
        """
        from ..session_sqlite import SessionNotFoundError

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "session store not initialized"},
            )

        # check + add 之间没有 await，因此同一 event loop 内是原子的。删除屏障
        # 同时阻止活动请求退出后、持久化数据删除前的新 prompt 抢入。
        if sid in deleting_session_ids:
            return JSONResponse(
                status_code=409,
                content={"detail": f"session {sid!r} is already being deleted"},
            )
        deleting_session_ids.add(sid)

        try:
            # 先校验 session 存在（不存在直接 404，不动文件）
            existing = await store.get_session(sid)
            if existing is None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"session {sid!r} not found"},
                )

            stop_error = await _stop_session_request_before_delete(sid)
            if stop_error is not None:
                return stop_error

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
                    status_code=404,
                    content={"detail": f"session {sid!r} not found"},
                )
            # P2-R1: 删除 session 后清理 knowledge library bindings（P2-R0 §7.2 不变量 7）
            # 只清 binding，不删 library 本身。失败不阻塞 session 删除（记 warning）。
            k_service = (
                state.knowledge_service if hasattr(state, "knowledge_service") else None
            )
            if k_service is not None:
                try:
                    await k_service.on_session_deleted(sid)
                except Exception as e:
                    state.last_error = (
                        f"knowledge.on_session_deleted({sid}) failed: {type(e).__name__}"
                    )
            wiki_store = state.wiki_store
            if wiki_store is not None:
                try:
                    await wiki_store.on_session_deleted(sid)
                except Exception as e:
                    state.last_error = (
                        f"wiki.on_session_deleted({sid}) failed: {type(e).__name__}"
                    )
            # 删的是 current session → 自动切到 default（或新建一个）
            if state.current_session_id == sid:
                state.current_session_id = None
                try:
                    default_session = await store.ensure_default_session()
                    state.current_session_id = default_session.id
                    if state.file_store is not None:
                        await state.file_store.ensure_session_workspace(default_session.id)
                except Exception:
                    pass
            return {"ok": True, "deleted_files": deleted_files}
        finally:
            deleting_session_ids.discard(sid)

    # ========================================================================
    # P1-D2-5: Regenerate + Revision history
    # ========================================================================

    @app.post(
        "/api/sessions/{sid}/messages/{assistant_message_id}/regenerate",
        response_model=None,
    )
    async def post_regenerate(
        sid: str,
        assistant_message_id: str,
    ) -> dict[str, Any] | JSONResponse:
        """D2-5：触发 regenerate——返回 202 + regeneration_id + request_id。

        14 步顺序（用户 2026-07-15 D2-5 审核 §2）：
            validate → create_running_revision → register request → create task → 202

        补偿逻辑：
            - revision 创建失败：不注册 request，不启动 task，返回 4xx/5xx
            - registry 注册失败：mark_revision_error + 移除可能存在的 entry
            - task 创建失败：mark_revision_error + 移除 registry entry

        `regeneration_id == revision.id`（不另生成第三个 ID）。
        """
        from .extension_store import RevisionError

        if state.shutting_down:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "extension_store_unavailable",
                        "message": "server shutting down",
                    }
                },
            )

        # 1. 完整校验
        try:
            validated = await _validate_regeneration_payload(sid, assistant_message_id)
        except RegenerationValidationError as e:
            return _regen_error_response(e)

        # 2. 生成 request_id（regeneration_id 在 step 4 从 revision.id 拿）
        request_id = f"req_{uuid4().hex[:16]}"

        # 3. create_running_revision（短事务）——失败补偿：不注册 request
        try:
            revision = await state.extension_store.create_running_revision(
                session_id=validated.session_id,
                assistant_message_id=validated.assistant_message_id,
                request_id=request_id,
            )
        except RegenerationValidationError:
            raise  # 安全网——validator 应已抛
        except RevisionError as e:
            # 已知的 revision 错误（如 request_id 冲突 / already running 在 race 中触发）
            code_map = {
                "RevisionAlreadyRunningError": "revision_already_running",
                "RevisionRequestConflictError": "request_conflict",
            }
            code = code_map.get(type(e).__name__, "regenerate_start_failed")
            return JSONResponse(
                status_code=409,
                content={"detail": {"code": code, "message": _safe_error(e)}},
            )
        except Exception as e:
            return _regen_safe_error_response(e)

        regeneration_id = revision.id

        # 4. 构造 queued WebRunRequest（不启动 task）
        web_request = WebRunRequest(
            id=request_id,
            session_id=validated.session_id,
            status="queued",
            created_at=_now_utc(),
            operation="regenerate",
            regeneration_id=regeneration_id,
            target_message_id=validated.assistant_message_id,
            payload=None,  # regenerate 无 body
        )

        # 5. 注册进 registry——失败补偿：mark_revision_error
        state.active_requests[request_id] = web_request
        state.active_request_by_session[validated.session_id] = request_id
        # 注册后状态——若 mark_revision_error 需要 task 未启动也能调（D2-3 已支持）

        # 6. 创建 managed task——失败补偿：mark_revision_error + 移除 registry
        bg_coro = _run_regeneration_background(web_request, validated)
        try:
            task = asyncio.create_task(bg_coro, name=f"regenerate_{request_id}")
        except Exception as e:
            # task 创建失败——close un-awaited coro + 补偿
            bg_coro.close()
            try:
                await state.extension_store.mark_revision_error(
                    revision_id=regeneration_id,
                    request_id=request_id,
                    error_summary=_safe_error(e),
                )
            except Exception:
                pass
            state.active_requests.pop(request_id, None)
            state.active_request_by_session.pop(validated.session_id, None)
            return _regen_safe_error_response(e, code="regenerate_start_failed")

        web_request.task = task

        # 7. 返回 202
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "operation": "regenerate",
                "regeneration_id": regeneration_id,
                "request_id": request_id,
                "session_id": validated.session_id,
                "assistant_message_id": validated.assistant_message_id,
                "status": "queued",
            },
        )

    @app.get(
        "/api/sessions/{sid}/messages/{assistant_message_id}/revisions",
        response_model=None,
    )
    async def get_revisions(
        sid: str,
        assistant_message_id: str,
        limit: int = 20,
        before_revision_number: int | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """D2-5：列出 assistant 的 revision 历史——安全 serializer。

        **不**返回 content_json / base_content_sha256 / request_id（审核 §1.2）。
        `is_current` 仅当 status == "completed"（partial unique 保证至多一个）。
        """
        ext_store = state.extension_store
        store = state.session_store
        if ext_store is None or store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "extension_store_unavailable",
                        "message": "Extension store not initialized.",
                    }
                },
            )

        # session/message 一致性校验
        try:
            session = await store.get_session(sid)
        except Exception:
            session = None
        if session is None:
            return JSONResponse(
                status_code=404,
                content={"detail": {"code": "session_not_found", "message": "Session not found."}},
            )
        db = store.connection
        cur = await db.execute(
            "SELECT id, session_id, role FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None or row["session_id"] != sid:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {
                        "code": "message_not_found",
                        "message": "Assistant message not found in this session.",
                    }
                },
            )
        if row["role"] != "assistant":
            return JSONResponse(
                status_code=400,
                content={
                    "detail": {
                        "code": "regenerate_target_not_assistant",
                        "message": "Revisions only exist for assistant messages.",
                    }
                },
            )

        # limit clamp——repository 也 clamp，API 层保持一致
        clamped_limit = max(1, min(100, limit))

        revisions = await ext_store.list_revisions(
            session_id=sid,
            assistant_message_id=assistant_message_id,
            limit=clamped_limit,
            before_revision_number=before_revision_number,
        )

        items = [
            {
                "revision_id": r.id,
                "revision_number": r.revision_number,
                "status": r.status,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "is_current": r.status == "completed",
            }
            for r in revisions
        ]

        next_before = items[-1]["revision_number"] if items else None
        # 如果返回数量 < limit，next_before 应为 null（无更多页）
        if len(items) < clamped_limit:
            next_before = None

        return {
            "session_id": sid,
            "assistant_message_id": assistant_message_id,
            "items": items,
            "next_before_revision_number": next_before,
        }

    # ========================================================================
    # P1-D1: Export Markdown
    # ========================================================================

    @app.get("/api/sessions/{sid}/export/markdown")
    async def export_session_markdown(
        sid: str,
    ) -> Any:
        """导出 session 为 Markdown 文件——基于 SQLite 持久化消息。

        **安全**（用户原指令 D1 §4.3/§4.8）：
        - 只导出 user / assistant 正文
        - 不导出 system prompt / request_id / sequence / MCP env / raw tool args / trace
        - HTML 保留用户原文（不执行/不删除）
        - filename 做 path traversal + 控制字符清理
        - 导出超限返回 413（不静默截断）

        **D2 复用**：renderer 是纯函数；D2 revision 过滤在 API 层完成后传入。
        """
        from datetime import UTC, datetime

        from ..session_sqlite import SessionNotFoundError
        from .markdown_export import (
            MarkdownExportOptions,
            build_content_disposition,
            build_export_filename,
            check_export_size,
            messages_to_export_items,
            render_session_markdown,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )

        # 读 session
        try:
            session = await store.get_session(sid)
        except SessionNotFoundError:
            session = None
        if session is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )

        # 读 messages（按 idx ASC——SQLite store 保证）
        try:
            messages = await store.list_messages(sid)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )

        # 构造 ExportMessage list
        export_items = messages_to_export_items(messages)

        # render
        exported_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        markdown = render_session_markdown(
            session_title=session.title or "Session",
            messages=export_items,
            options=MarkdownExportOptions(),
            exported_at=exported_at,
        )

        # 大小检查
        size_error = check_export_size(markdown)
        if size_error is not None:
            return JSONResponse(
                status_code=413,
                content={"detail": size_error},
            )

        # filename
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        filename = build_export_filename(session.title or "", date_str)

        # 响应
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": build_content_disposition(filename),
            },
        )

    # ========================================================================
    # Files（P0-2）
    # ========================================================================

    def _require_file_store() -> WorkspaceStore:
        """统一拿 file_store；未启用返回 503 detail。"""
        if state.file_store is None:
            raise HTTPException(
                status_code=503,
                detail="file store not initialized; create_app(uploads_dir=...)",
            )
        return cast("WorkspaceStore", state.file_store)

    def _serialize_managed_file(ref: Any) -> dict[str, Any]:
        """返回逻辑文件 metadata，绝不暴露物理磁盘路径。"""
        from agent_workspace.store import workspace_path_policy

        from ..tools.view_file import _classify_format

        path_policy = workspace_path_policy(ref.logical_path, purpose=ref.purpose)
        return {
            "id": ref.id,
            "session_id": ref.session_id,
            "name": ref.name,
            "logical_path": ref.logical_path,
            "origin": ref.origin,
            "purpose": ref.purpose,
            "size": ref.size,
            "mime": ref.mime,
            "format": _classify_format(ref.name, ref.mime),
            "sha256": ref.sha256,
            "created_at": ref.created_at,
            "updated_at": ref.updated_at,
            "category": path_policy.category,
            "owner": path_policy.owner,
            "content_editable": path_policy.user_content_editable,
            "movable": path_policy.user_movable,
            "deletable": path_policy.user_deletable,
            "agent_writable": path_policy.agent_creatable,
            "sandbox_publishable": path_policy.sandbox_publishable,
            "immutable": path_policy.immutable_content,
        }

    def _serialize_workspace_state(workspace: Any) -> dict[str, Any]:
        return {
            "schema_version": workspace.schema_version,
            "session_id": workspace.session_id,
            "revision": workspace.revision,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "code_continuity": workspace.code_continuity.model_dump(mode="json"),
        }

    async def _refresh_code_continuity(
        session_id: str,
        *,
        trigger: CodeContinuityTrigger,
        changed_paths: tuple[str, ...] = (),
        deleted_paths: tuple[str, ...] = (),
        validation: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> Any:
        from agent_workspace.store import is_code_workspace_path

        service = state.code_continuity_service
        if service is None:
            return None
        if trigger != "recovery" and not any(
            is_code_workspace_path(path)
            for path in (*changed_paths, *deleted_paths)
        ):
            return None
        try:
            result = await service.refresh(
                session_id,
                trigger=trigger,
                changed_paths=changed_paths,
                deleted_paths=deleted_paths,
                validation=validation,
            )
        except Exception as exc:
            state.last_error = f"code_continuity: {type(exc).__name__}"
            file_store = state.file_store
            if file_store is not None:
                try:
                    workspace = await file_store.get_workspace_state(session_id)
                    await _emit_web_payload(
                        {
                            "type": "workspace_changed",
                            "source": "code_continuity",
                            "operation_id": operation_id,
                            "workspace_revision": workspace.revision,
                            "source_workspace_revision": (
                                workspace.code_continuity.latest_code_workspace_revision
                            ),
                            "changed_paths": [],
                            "deleted_paths": [],
                            "code_continuity": workspace.code_continuity.model_dump(
                                mode="json"
                            ),
                        },
                        (
                            f"code-continuity-failed:{operation_id}"
                            if operation_id is not None
                            else f"code-continuity-failed:{session_id}:{workspace.revision}"
                        ),
                        session_id,
                    )
                except Exception:
                    pass
            return None
        if result is None:
            return None
        await _emit_web_payload(
            {
                "type": "workspace_changed",
                "source": "code_continuity",
                "operation_id": operation_id,
                "workspace_revision": result.workspace.revision,
                "source_workspace_revision": result.source_workspace_revision,
                "changed_paths": list(result.changed_paths),
                "deleted_paths": [],
                "code_continuity": result.workspace.code_continuity.model_dump(
                    mode="json"
                ),
            },
            (
                f"code-continuity:{operation_id}"
                if operation_id is not None
                else f"code-continuity:{session_id}:{result.workspace.revision}"
            ),
            session_id,
        )
        return result

    def _serialize_workspace_document_result(result: Any) -> dict[str, Any]:
        return {
            "source_file_id": result.source_file_id,
            "document_id": result.document_id,
            "status": result.status,
            "reused": result.reused,
            "workspace_revision": result.workspace_revision,
            "manifest_file_id": result.manifest_file_id,
            "primary_file_id": result.primary_file_id,
            "files": [_serialize_managed_file(ref) for ref in result.files],
            "warnings": list(result.warnings),
            "error_code": result.error_code,
        }

    @app.post("/api/sessions/{sid}/files", response_model=None)
    async def post_session_files(
        sid: str,
        files: list[UploadFile] = File(default=[]),  # noqa: B008
        relative_folder: str | None = None,
        expected_workspace_revision: int | None = None,
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
        if expected_workspace_revision is not None and expected_workspace_revision < 0:
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_workspace_revision must be non-negative"},
            )

        file_store = _require_file_store()
        from agent_workspace.store import (
            FileStoreError,
            FileTooLargeError,
            SessionStorageLimitError,
            UnsafeFilenameError,
            WorkspaceVersionConflictError,
            is_code_workspace_path,
        )

        saved: list[dict[str, Any]] = []
        conversions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        code_changed_paths: list[str] = []
        next_expected_revision = expected_workspace_revision
        for upload in files:
            try:
                ref = await file_store.save(
                    sid,
                    upload,
                    relative_folder=relative_folder,
                    expected_workspace_revision=next_expected_revision,
                )
                saved.append(_serialize_managed_file(ref))
                if is_code_workspace_path(ref.logical_path):
                    code_changed_paths.append(ref.logical_path)
                if (
                    ref.purpose == "document_original"
                    and state.workspace_document_service is not None
                ):
                    try:
                        converted = await state.workspace_document_service.convert(
                            ref.id,
                            sid,
                        )
                        conversions.append(_serialize_workspace_document_result(converted))
                    except FileStoreError as exc:
                        conversions.append(
                            {
                                "source_file_id": ref.id,
                                "document_id": PurePosixPath(ref.logical_path).parent.name,
                                "status": "failed",
                                "reused": False,
                                "workspace_revision": (
                                    await file_store.get_workspace_state(sid)
                                ).revision,
                                "manifest_file_id": None,
                                "primary_file_id": None,
                                "files": [],
                                "warnings": [],
                                "error_code": type(exc).__name__,
                            }
                        )
                workspace = await file_store.get_workspace_state(sid)
                next_expected_revision = workspace.revision
            except FileTooLargeError as e:
                errors.append(
                    {
                        "filename": upload.filename or "<unknown>",
                        "error_type": "FileTooLargeError",
                        "error": str(e),
                    }
                )
            except SessionStorageLimitError as e:
                errors.append(
                    {
                        "filename": upload.filename or "<unknown>",
                        "error_type": "SessionStorageLimitError",
                        "error": str(e),
                    }
                )
            except UnsafeFilenameError as e:
                errors.append(
                    {
                        "filename": upload.filename or "<unknown>",
                        "error_type": "UnsafeFilenameError",
                        "error": str(e),
                    }
                )
            except WorkspaceVersionConflictError as e:
                errors.append(
                    {
                        "filename": upload.filename or "<unknown>",
                        "error_type": "WorkspaceVersionConflictError",
                        "error": str(e),
                        "expected_revision": e.expected,
                        "current_revision": e.current,
                    }
                )
                break
            except FileStoreError as e:
                errors.append(
                    {
                        "filename": upload.filename or "<unknown>",
                        "error_type": type(e).__name__,
                        "error": str(e),
                    }
                )
        if code_changed_paths:
            await _refresh_code_continuity(
                sid,
                trigger="user_upload",
                changed_paths=tuple(code_changed_paths),
            )
        status_code = 200
        if not saved and errors:
            # 全部失败——客户端可以据此显示
            status_code = (
                413
                if any(
                    e["error_type"] in ("FileTooLargeError", "SessionStorageLimitError")
                    for e in errors
                )
                else 409
                if any(e["error_type"] == "WorkspaceVersionConflictError" for e in errors)
                else 400
            )
        workspace = await file_store.get_workspace_state(sid)
        return JSONResponse(
            status_code=status_code,
            content={
                "count": len(saved),
                "files": saved,
                "conversions": conversions,
                "errors": errors,
                "workspace": _serialize_workspace_state(workspace),
            },
        )

    @app.post(
        "/api/sessions/{sid}/documents/{source_file_id}/convert",
        response_model=None,
    )
    async def convert_session_workspace_document(
        sid: str,
        source_file_id: str,
    ) -> dict[str, Any] | JSONResponse:
        """Idempotently retry one immutable Workspace document conversion."""
        from agent_workspace.documents import UnsupportedWorkspaceDocumentError
        from agent_workspace.store import (
            FileAccessDeniedError,
            FileStoreError,
            VirtualFileNotFoundError,
            WorkspacePublishPolicyError,
            WorkspaceTreeConflictError,
            WorkspaceVersionConflictError,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        if await store.get_session(sid) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        service = state.workspace_document_service
        if service is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Workspace document converter is unavailable"},
            )
        try:
            converted = await service.convert(source_file_id, sid)
        except VirtualFileNotFoundError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except FileAccessDeniedError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        except UnsupportedWorkspaceDocumentError as exc:
            return JSONResponse(status_code=415, content={"detail": str(exc)})
        except (
            WorkspacePublishPolicyError,
            WorkspaceTreeConflictError,
            WorkspaceVersionConflictError,
        ) as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        except FileStoreError as exc:
            return JSONResponse(status_code=500, content={"detail": str(exc)})
        return _serialize_workspace_document_result(converted)

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
        workspace = await file_store.get_workspace_state(sid)
        return {
            "count": len(files),
            "files": [_serialize_managed_file(f) for f in files],
            "workspace": _serialize_workspace_state(workspace),
        }

    @app.get("/api/sessions/{sid}/workspace", response_model=None)
    async def get_session_workspace(sid: str) -> dict[str, Any] | JSONResponse:
        """Return the complete logical Workspace snapshot and its revision."""
        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        if await store.get_session(sid) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        file_store = _require_file_store()
        await file_store.ensure_session_workspace(sid)
        files = await file_store.list_session(sid)
        workspace = await file_store.get_workspace_state(sid)
        return {
            "workspace": _serialize_workspace_state(workspace),
            "count": len(files),
            "files": [_serialize_managed_file(ref) for ref in files],
        }

    @app.post("/api/sessions/{sid}/workspace/markdown", response_model=None)
    async def post_session_workspace_markdown(
        sid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """Create an ordinary Markdown file at an exact logical path."""
        from agent_workspace.store import (
            FileStoreError,
            FileTooLargeError,
            SessionStorageLimitError,
            UnsafeFilenameError,
            WorkspacePathConflictError,
            WorkspaceVersionConflictError,
            is_markdown_filename,
            normalize_workspace_logical_path,
            workspace_path_policy,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        if await store.get_session(sid) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        logical_path = payload.get("logical_path")
        content = payload.get("content")
        expected_revision = payload.get("expected_workspace_revision")
        if not isinstance(logical_path, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "logical_path must be a string"},
            )
        if not isinstance(content, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "content must be a string"},
            )
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_workspace_revision must be a non-negative integer"},
            )

        file_store = _require_file_store()
        try:
            normalized = normalize_workspace_logical_path(logical_path)
            path = PurePosixPath(normalized)
            if not is_markdown_filename(path.name):
                raise UnsafeFilenameError(
                    "Workspace Markdown files must use .md, .markdown, or .mdx"
                )
            path_policy = workspace_path_policy(normalized)
            if not path_policy.user_creatable:
                raise UnsafeFilenameError(
                    "Workspace path is reserved or cannot be created manually"
                )
            parent = "" if str(path.parent) == "." else str(path.parent)
            created = await file_store.write_text(
                sid,
                path.name,
                content,
                content_type="text/markdown",
                folder=parent or None,
                origin="user",
                expected_workspace_revision=expected_revision,
                unique_logical_path=False,
            )
        except (WorkspacePathConflictError, WorkspaceVersionConflictError) as e:
            return JSONResponse(status_code=409, content={"detail": str(e)})
        except UnsafeFilenameError as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
        except (FileTooLargeError, SessionStorageLimitError) as e:
            return JSONResponse(status_code=413, content={"detail": str(e)})
        except FileStoreError as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})
        workspace = await file_store.get_workspace_state(sid)
        return {
            "file": _serialize_managed_file(created),
            "workspace": _serialize_workspace_state(workspace),
        }

    @app.get("/api/sessions/{sid}/files/{fid}", response_model=None)
    async def get_session_file_content(
        sid: str,
        fid: str,
    ) -> Any:
        """下载 session 内单文件。

        - sid 不存在 → 404
        - fid 不存在 → 404
        - fid 属于其它 session → 403
        """
        from agent_workspace.store import (
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
                status_code=404,
                content={"detail": str(e)},
            )
        except FileAccessDeniedError as e:
            return JSONResponse(
                status_code=403,
                content={"detail": str(e)},
            )
        except UnsafeFilenameError as e:
            return JSONResponse(
                status_code=400,
                content={"detail": str(e)},
            )
        # FileResponse 把 Content-Type / filename 设对
        return FileResponse(
            ref.path,
            media_type=ref.mime,
            filename=ref.name,
        )

    @app.put("/api/sessions/{sid}/files/{fid}/content", response_model=None)
    async def put_session_file_content(
        sid: str,
        fid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """更新根文件或普通 Markdown；sha256/revision 乐观锁防静默覆盖。"""
        from agent_workspace.store import (
            FileAccessDeniedError,
            FileStoreError,
            FileTooLargeError,
            FileVersionConflictError,
            SessionStorageLimitError,
            VirtualFileNotFoundError,
            WorkspaceVersionConflictError,
            workspace_path_policy,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        if await store.get_session(sid) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        content = payload.get("content")
        expected_sha256 = payload.get("expected_sha256")
        expected_revision = payload.get("expected_workspace_revision")
        if not isinstance(content, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "content must be a string"},
            )
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_sha256 must be a string"},
            )
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_workspace_revision must be a non-negative integer"},
            )

        file_store = _require_file_store()
        try:
            current = await file_store.get_for_session(sid, fid)
            path_policy = workspace_path_policy(
                current.logical_path,
                purpose=current.purpose,
            )
            if not path_policy.user_content_editable:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "only Workspace Markdown files are editable here"},
                )
            updated = await file_store.update_text(
                sid,
                fid,
                content,
                expected_sha256=expected_sha256,
                expected_workspace_revision=expected_revision,
            )
        except VirtualFileNotFoundError as e:
            return JSONResponse(status_code=404, content={"detail": str(e)})
        except FileAccessDeniedError as e:
            return JSONResponse(status_code=403, content={"detail": str(e)})
        except (FileVersionConflictError, WorkspaceVersionConflictError) as e:
            return JSONResponse(status_code=409, content={"detail": str(e)})
        except (FileTooLargeError, SessionStorageLimitError) as e:
            return JSONResponse(status_code=413, content={"detail": str(e)})
        except FileStoreError as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})
        workspace = await file_store.get_workspace_state(sid)
        return {
            "file": _serialize_managed_file(updated),
            "workspace": _serialize_workspace_state(workspace),
        }

    @app.patch("/api/sessions/{sid}/files/{fid}", response_model=None)
    async def patch_session_file(
        sid: str,
        fid: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """Move or rename an ordinary Markdown file in the logical tree."""
        from agent_workspace.store import (
            FileAccessDeniedError,
            FileStoreError,
            FileVersionConflictError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
            WorkspacePathConflictError,
            WorkspaceVersionConflictError,
        )

        store = state.session_store
        if store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "session store not initialized"},
            )
        if await store.get_session(sid) is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {sid!r} not found"},
            )
        logical_path = payload.get("logical_path")
        expected_sha256 = payload.get("expected_sha256")
        expected_revision = payload.get("expected_workspace_revision")
        if not isinstance(logical_path, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "logical_path must be a string"},
            )
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_sha256 must be a string"},
            )
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_workspace_revision must be a non-negative integer"},
            )
        file_store = _require_file_store()
        try:
            moved = await file_store.move_file(
                sid,
                fid,
                logical_path,
                expected_sha256=expected_sha256,
                expected_workspace_revision=expected_revision,
            )
        except VirtualFileNotFoundError as e:
            return JSONResponse(status_code=404, content={"detail": str(e)})
        except FileAccessDeniedError as e:
            return JSONResponse(status_code=403, content={"detail": str(e)})
        except (
            FileVersionConflictError,
            WorkspacePathConflictError,
            WorkspaceVersionConflictError,
        ) as e:
            return JSONResponse(status_code=409, content={"detail": str(e)})
        except UnsafeFilenameError as e:
            return JSONResponse(status_code=400, content={"detail": str(e)})
        except FileStoreError as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})
        workspace = await file_store.get_workspace_state(sid)
        return {
            "file": _serialize_managed_file(moved),
            "workspace": _serialize_workspace_state(workspace),
        }

    @app.get("/api/files/{fid}", response_model=None)
    async def get_file_compat(
        fid: str,
        session_id: str | None = None,
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
        fid: str,
        session_id: str | None = None,
        expected_sha256: str | None = None,
        expected_workspace_revision: int | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """兼容删除入口：必须 query 参数 session_id。"""
        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"detail": "session_id query parameter is required"},
            )
        return await delete_session_file(
            session_id,
            fid,
            expected_sha256,
            expected_workspace_revision,
        )

    @app.delete("/api/sessions/{sid}/files/{fid}", response_model=None)
    async def delete_session_file(
        sid: str,
        fid: str,
        expected_sha256: str | None = None,
        expected_workspace_revision: int | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """删除 session 内单文件（推荐入口）。"""
        from agent_workspace.store import (
            FileAccessDeniedError,
            FileStoreError,
            FileVersionConflictError,
            UnsafeFilenameError,
            VirtualFileNotFoundError,
            WorkspaceVersionConflictError,
            is_code_workspace_path,
            workspace_path_policy,
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
        if expected_workspace_revision is not None and expected_workspace_revision < 0:
            return JSONResponse(
                status_code=422,
                content={"detail": "expected_workspace_revision must be non-negative"},
            )
        file_store = _require_file_store()
        try:
            current = await file_store.get_for_session(sid, fid)
            path_policy = workspace_path_policy(
                current.logical_path,
                purpose=current.purpose,
            )
            if not path_policy.user_deletable:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "Workspace file is required or system-owned and cannot be deleted"
                    },
                )
            await file_store.delete_for_session(
                sid,
                fid,
                expected_sha256=expected_sha256,
                expected_workspace_revision=expected_workspace_revision,
            )
        except VirtualFileNotFoundError as e:
            return JSONResponse(
                status_code=404,
                content={"detail": str(e)},
            )
        except FileAccessDeniedError as e:
            return JSONResponse(
                status_code=403,
                content={"detail": str(e)},
            )
        except UnsafeFilenameError as e:
            return JSONResponse(
                status_code=400,
                content={"detail": str(e)},
            )
        except (FileVersionConflictError, WorkspaceVersionConflictError) as e:
            return JSONResponse(
                status_code=409,
                content={"detail": str(e)},
            )
        except FileStoreError as e:
            return JSONResponse(
                status_code=500,
                content={"detail": str(e)},
            )
        if is_code_workspace_path(current.logical_path):
            await _refresh_code_continuity(
                sid,
                trigger="user_delete",
                deleted_paths=(current.logical_path,),
            )
        workspace = await file_store.get_workspace_state(sid)
        return {
            "deleted": True,
            "file_id": fid,
            "workspace": _serialize_workspace_state(workspace),
        }

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
            tools.append(
                {
                    "name": getattr(tool, "name", None),
                    "server": getattr(tool, "server_name", None),
                    "mcp_tool": getattr(tool, "mcp_tool_name", None),
                    "description": getattr(tool, "description", ""),
                }
            )
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
        active_tool_names = (
            set(harness.agent.tools.names())
            if (getattr(harness.agent, "tools", None) is not None)
            else set()
        )
        tools: list[dict[str, Any]] = []
        for tool in registry.list_agent_tools():
            tname = getattr(tool, "name", None)
            # 真正 active = 在 agent.tools 中且未在 disabled_mcp_tools set 中
            enabled = tname in active_tool_names and tname not in state.disabled_mcp_tools
            tools.append(
                {
                    "name": tname,
                    "server": getattr(tool, "server_name", None),
                    "mcp_tool": getattr(tool, "mcp_tool_name", None),
                    "description": getattr(tool, "description", ""),
                    "enabled": enabled,
                }
            )
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
        P1-C3: 加 desired_enabled + attached 字段；enabled = desired_enabled（兼容）。
        P1-C5: 加 restore_status + missing_env_keys 结构化字段。
        """
        return {
            "name": cfg.name,
            "command": cfg.command,
            "args": list(cfg.args or []),
            "enabled": cfg.enabled,  # 兼容 = desired_enabled
            "desired_enabled": cfg.enabled,  # P1-C3 显式字段
            "attached": getattr(cfg, "attached", False),  # P1-C3 runtime 派生
            "restore_status": getattr(cfg, "restore_status", "not_requested"),
            "missing_env_keys": list(getattr(cfg, "missing_env_keys", [])),
            "last_error": cfg.last_error,
            "tool_count": cfg.tool_count,
            "env_keys": sorted((cfg.env or {}).keys()),
            "builtin": bool(getattr(cfg, "builtin", False)),
            "deletable": bool(getattr(cfg, "deletable", True)),
            "settings": dict(getattr(cfg, "settings", {}) or {}),
        }

    def _parse_mcp_tool_name(
        full_name: str,
    ) -> tuple[str, str] | None:
        """P1-C3/C4: 从 mcp__{server}__{tool} 解析 (server_name, raw_tool_name)。

        **不依赖 split("__")**——遍历已知 server configs 匹配**最长**前缀，
        避免 server name 前缀重叠（如 foo vs foo__bar）时歧义。
        """
        if not full_name.startswith(_MCP_TOOL_NAME_PREFIX):
            return None
        # 按 server_name 长度降序——优先匹配最长前缀
        sorted_names = sorted(state.mcp_server_configs.keys(), key=len, reverse=True)
        for server_name in sorted_names:
            prefix = f"{_MCP_TOOL_NAME_PREFIX}{server_name}__"
            if full_name.startswith(prefix):
                raw_tool = full_name[len(prefix) :]
                return server_name, raw_tool
        return None

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
                f"name must match [A-Za-z0-9_-]+ (got {name!r})",
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
            timeout_s=_mcp_request_timeout(cfg),
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
        enabled_cfgs = [cfg for cfg in state.mcp_server_configs.values() if cfg.enabled]
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
        server_states = {s.name: s for s in harness.list_mcp_servers()}
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

        P1-C3: 持久化到 SQLite（只 env_keys，不 value）+ mutation lock + rollback。
        - DB 失败 → 移除 runtime config
        - enabled=true → attach（attach 失败 desired_enabled 仍 true，attached=false）
        """
        from .extension_store import ExtensionStoreError

        cfg, err = _validate_mcp_server_payload(payload)
        if cfg is None:
            return JSONResponse(status_code=400, content={"detail": err})

        async with state.mcp_mutation_lock:
            # 检查 runtime 重名
            if cfg.name in state.mcp_server_configs:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": f"MCP server {cfg.name!r} already exists",
                        "server_name": cfg.name,
                    },
                )
            # P1-C3: 检查 DB 重名
            if state.extension_store is not None:
                try:
                    existing = await state.extension_store.get_mcp_server(cfg.name)
                    if existing is not None:
                        return JSONResponse(
                            status_code=409,
                            content={"detail": f"MCP server {cfg.name!r} already persisted"},
                        )
                except ExtensionStoreError:
                    pass  # DB 不可用——降级为 runtime only

            # 写 runtime config
            state.mcp_server_configs[cfg.name] = cfg

            # P1-C3: DB upsert（只 env_keys，不 value）
            if state.extension_store is not None:
                try:
                    await state.extension_store.upsert_mcp_server(
                        name=cfg.name,
                        transport="stdio",
                        command=cfg.command,
                        args=cfg.args,
                        desired_enabled=cfg.enabled,
                        env_keys=list(cfg.env.keys()),
                    )
                except ExtensionStoreError:
                    # DB 失败 → 移除 runtime config
                    del state.mcp_server_configs[cfg.name]
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime rolled back"},
                    )

            # enabled=True → attach（独立流程，不影响 add + DB 已完成）
            attach_error: str | None = None
            if cfg.enabled:
                try:
                    await _refresh_enabled_mcp_servers()
                    cfg.attached = True
                except Exception as e:
                    attach_error = f"{type(e).__name__}: {e}"
                    cfg.last_error = attach_error
                    cfg.attached = False

        resp = _serialize_mcp_server(cfg)
        if attach_error is not None:
            return JSONResponse(status_code=502, content=resp)
        return resp

    @app.put("/api/mcp/servers/ddgs/settings", response_model=None)
    async def update_ddgs_settings(
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """Update per-user defaults for the non-deletable built-in DDGS server."""

        import sys

        from pydantic import ValidationError

        from ..mcp.ddgs_server import (
            DDGS_SERVER_NAME,
            DDGSSearchSettings,
            build_ddgs_server_args,
        )
        from .extension_store import ExtensionStoreError

        cfg = state.mcp_server_configs.get(DDGS_SERVER_NAME)
        if cfg is None or not cfg.builtin:
            return JSONResponse(
                status_code=404,
                content={"detail": "Built-in DDGS MCP server is not enabled for this app"},
            )
        try:
            settings = DDGSSearchSettings.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "Invalid DDGS settings",
                    "errors": exc.errors(include_url=False),
                },
            )

        async with state.mcp_mutation_lock:
            original_cfg = cfg.model_copy(deep=True)
            cfg.command = sys.executable
            cfg.args = build_ddgs_server_args(settings)
            cfg.settings = settings.model_dump(mode="json")
            cfg.last_error = None

            if state.extension_store is not None:
                try:
                    await state.extension_store.upsert_mcp_server(
                        name=DDGS_SERVER_NAME,
                        transport="stdio",
                        command=cfg.command,
                        args=cfg.args,
                        desired_enabled=cfg.enabled,
                        env_keys=[],
                    )
                except ExtensionStoreError:
                    state.mcp_server_configs[DDGS_SERVER_NAME] = original_cfg
                    return JSONResponse(
                        status_code=500,
                        content={"detail": "Failed to persist DDGS settings"},
                    )

            if cfg.enabled:
                await _refresh_enabled_mcp_servers()
                cfg.attached = cfg.last_error is None
                cfg.restore_status = "attached" if cfg.attached else "error"
                if not cfg.attached:
                    return JSONResponse(
                        status_code=502,
                        content=_serialize_mcp_server(cfg),
                    )

        return _serialize_mcp_server(cfg)

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

        P1-C3: persist desired_enabled=true → attach → 成功 attached=true / 失败 attached=false。
        attach 失败不改 desired_enabled（保留用户意图）。
        """
        from .extension_store import ExtensionStoreConflictError, ExtensionStoreError

        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )

        async with state.mcp_mutation_lock:
            # P1-C3: 先 persist desired_enabled=true
            original_enabled = cfg.enabled
            cfg.enabled = True
            if state.extension_store is not None:
                try:
                    await state.extension_store.set_mcp_server_enabled(name, True)
                except (ExtensionStoreConflictError, ExtensionStoreError):
                    # DB 失败 → 不 attach，恢复原 desired_enabled
                    cfg.enabled = original_enabled
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime rolled back"},
                    )

            # attach
            try:
                await _refresh_enabled_mcp_servers()
                # _refresh_enabled_mcp_servers 不抛异常——attach 失败时设 cfg.last_error
                if cfg.last_error is not None:
                    cfg.attached = False
                    # desired_enabled 仍 true——保留用户意图
                    return JSONResponse(
                        status_code=502,
                        content=_serialize_mcp_server(cfg),
                    )
                cfg.attached = True
            except Exception as e:
                cfg.last_error = f"{type(e).__name__}: {e}"
                cfg.attached = False
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

        P1-C3: persist desired_enabled=false → detach。
        DB 失败不 detach。disabled tool rows 保留（server re-enable 后重新应用）。
        """
        from .extension_store import ExtensionStoreConflictError, ExtensionStoreError

        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )

        async with state.mcp_mutation_lock:
            # P1-C3: 先 persist desired_enabled=false
            original_enabled = cfg.enabled
            cfg.enabled = False
            if state.extension_store is not None:
                try:
                    await state.extension_store.set_mcp_server_enabled(name, False)
                except (ExtensionStoreConflictError, ExtensionStoreError):
                    # DB 失败 → 不 detach，恢复原 desired_enabled
                    cfg.enabled = original_enabled
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime rolled back"},
                    )

            # detach
            try:
                await _refresh_enabled_mcp_servers()
            except Exception as e:
                cfg.last_error = f"{type(e).__name__}: {e}"
                return JSONResponse(
                    status_code=502,
                    content=_serialize_mcp_server(cfg),
                )
            cfg.attached = False
            cfg.last_error = None
            cfg.tool_count = 0
        return _serialize_mcp_server(cfg)

    @app.delete("/api/mcp/servers/{name}", response_model=None)
    async def delete_mcp_server(
        name: str,
    ) -> dict[str, Any] | JSONResponse:
        """删除 MCP server 配置。

        P1-C3: DB delete + cascade disabled tools；DB 失败恢复 runtime config。
        - 不存在 → 404
        - 如果 attached：先 detach
        - 删除 runtime config + DB row（cascade disabled tool rows）
        """
        from .extension_store import ExtensionStoreError

        cfg = state.mcp_server_configs.get(name)
        if cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {name!r} not found"},
            )
        if not cfg.deletable:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"Built-in MCP server {name!r} cannot be deleted",
                    "server_name": name,
                },
            )

        async with state.mcp_mutation_lock:
            # detach if attached
            if cfg.enabled:
                cfg.enabled = False
                try:
                    await _refresh_enabled_mcp_servers()
                except Exception as e:
                    state.last_error = (
                        f"delete_mcp_server({name}) detach failed: {type(e).__name__}: {e}"
                    )
            # 保存原 config 用于回滚
            original_cfg = cfg.model_copy()
            # 删 runtime config
            state.mcp_server_configs.pop(name, None)
            # 清理 runtime disabled tools
            prefix = f"{_MCP_TOOL_NAME_PREFIX}{name}__"
            stale = {t for t in state.disabled_mcp_tools if t.startswith(prefix)}
            if stale:
                state.disabled_mcp_tools -= stale

            # P1-C3: DB delete（cascade disabled tool rows）
            if state.extension_store is not None:
                try:
                    await state.extension_store.delete_mcp_server(name)
                except ExtensionStoreError:
                    # DB 失败 → 恢复 runtime config + disabled tools
                    state.mcp_server_configs[name] = original_cfg
                    state.disabled_mcp_tools |= stale
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime restored"},
                    )

        return {"deleted": True, "name": name, "cleaned_disabled_tools": sorted(stale)}

    @app.post("/api/mcp/tools/{tool_name}/enable", response_model=None)
    async def enable_mcp_tool(
        tool_name: str,
    ) -> dict[str, Any] | JSONResponse:
        """启用单个 MCP tool。

        P1-C3: 结构化 key (server_name, raw_tool_name) + DB delete disabled row + rollback。
        - 不依赖 split("__")——用 _parse_mcp_tool_name 遍历已知 server 匹配前缀
        """
        from .extension_store import ExtensionStoreError

        parsed = _parse_mcp_tool_name(tool_name)
        if parsed is None:
            return JSONResponse(
                status_code=400,
                content={"detail": f"invalid MCP tool name {tool_name!r}"},
            )
        server_name, raw_tool_name = parsed

        server_cfg = state.mcp_server_configs.get(server_name)
        if server_cfg is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"MCP server {server_name!r} not configured"},
            )
        if not server_cfg.enabled:
            return JSONResponse(
                status_code=409,
                content={"detail": f"MCP server {server_name!r} is disabled; enable server first"},
            )

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
                content={"detail": f"MCP tool {tool_name!r} not found in registry"},
            )

        async with state.mcp_mutation_lock:
            # 保存原 MCPAgentTool 引用用于回滚
            state.disabled_mcp_tools.discard(tool_name)
            if not harness.agent.tools.has(tool_name):
                try:
                    harness.agent.tools.register(target)
                except Exception as e:
                    return JSONResponse(
                        status_code=500,
                        content={"detail": f"register failed: {type(e).__name__}: {e}"},
                    )
            # P1-C3: DB delete disabled row（结构化 key）
            if state.extension_store is not None:
                try:
                    await state.extension_store.enable_mcp_tool(server_name, raw_tool_name)
                except ExtensionStoreError:
                    # DB 失败 → unregister 回滚
                    try:
                        harness.agent.tools.unregister(tool_name)
                    except Exception:
                        pass
                    state.disabled_mcp_tools.add(tool_name)
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime rolled back"},
                    )
        return {"tool_name": tool_name, "enabled": True}

    @app.post("/api/mcp/tools/{tool_name}/disable", response_model=None)
    async def disable_mcp_tool(
        tool_name: str,
    ) -> dict[str, Any] | JSONResponse:
        """禁用单个 MCP tool。

        P1-C3: 结构化 key + DB insert disabled row + rollback。
        保存原 MCPAgentTool 引用用于 DB 失败时 re-register。
        """
        from .extension_store import ExtensionStoreError

        parsed = _parse_mcp_tool_name(tool_name)
        if parsed is None:
            return JSONResponse(
                status_code=400,
                content={"detail": f"invalid MCP tool name {tool_name!r}"},
            )
        server_name, raw_tool_name = parsed

        async with state.mcp_mutation_lock:
            # 保存原 MCPAgentTool 引用用于回滚
            original_target = None
            if harness.agent.tools.has(tool_name):
                # 从 registry 找到原 tool 对象
                registry = harness.mcp_registry
                if registry is not None:
                    original_target = next(
                        (t for t in registry.list_agent_tools() if t.name == tool_name),
                        None,
                    )

            state.disabled_mcp_tools.add(tool_name)
            if harness.agent.tools.has(tool_name):
                try:
                    harness.agent.tools.unregister(tool_name)
                except Exception:
                    pass

            # P1-C3: DB insert disabled row（结构化 key）
            if state.extension_store is not None:
                try:
                    await state.extension_store.disable_mcp_tool(server_name, raw_tool_name)
                except ExtensionStoreError:
                    # DB 失败 → re-register 回滚
                    if original_target is not None:
                        try:
                            harness.agent.tools.register(original_target)
                        except Exception:
                            pass
                    state.disabled_mcp_tools.discard(tool_name)
                    return JSONResponse(
                        status_code=500,
                        content={"ok": False, "error": "persist failed; runtime rolled back"},
                    )
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
        skills = [serialize_skill(s, include_prompt=include_prompt) for s in registry.list()]
        return {
            "attached": True,
            "skills": skills,
            "skill_loader": to_json_safe(harness.context.metadata.get("skill_loader") or {}),
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

        P1-C2: 持久化到 SQLite + mutation lock + rollback。
        - 每个文件逐个原子：register 成功 → DB 写入 → DB 失败 unregister 回滚
        - 服务端强制覆盖 metadata["source_kind"]="upload" + ["persisted"]=True
        - 并发 upload 由 skill_mutation_lock 串行化
        """
        import hashlib

        from ..skill_loader import (
            SkillFileFormatError,
            parse_skill_markdown,
        )
        from ..skills import SkillRegistrationError
        from .extension_store import ExtensionStoreError

        registry = _require_skill_registry()

        if not files:
            return JSONResponse(
                status_code=400,
                content={"detail": "no files uploaded (field name: 'files')"},
            )

        saved_skills: list[Any] = []
        errors: list[dict[str, Any]] = []

        # P1-C2: mutation lock 串行化 upload——避免并发同名交错
        async with state.skill_mutation_lock:
            for upload in files:
                filename = upload.filename or ""
                try:
                    raw = await upload.read()
                except Exception as e:
                    errors.append(
                        {
                            "filename": filename or "<unknown>",
                            "error_type": type(e).__name__,
                            "error": f"failed to read upload: {e}",
                        }
                    )
                    continue
                if len(raw) > _SKILL_UPLOAD_MAX_BYTES:
                    errors.append(
                        {
                            "filename": filename or "<unknown>",
                            "error_type": "SkillFileSecurityError",
                            "error": (
                                f"uploaded skill file size {len(raw)} exceeds "
                                f"max_file_size_bytes={_SKILL_UPLOAD_MAX_BYTES}"
                            ),
                        }
                    )
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as e:
                    errors.append(
                        {
                            "filename": filename or "<unknown>",
                            "error_type": "SkillFileFormatError",
                            "error": f"file is not valid utf-8: {e}",
                        }
                    )
                    continue

                fallback_name = _secure_skill_filename(filename)
                try:
                    skill = parse_skill_markdown(
                        text,
                        fallback_name=fallback_name,
                        source_path=f"upload:{filename or fallback_name}",
                    )
                except SkillFileFormatError as e:
                    errors.append(
                        {
                            "filename": filename or fallback_name,
                            "error_type": "SkillFileFormatError",
                            "error": str(e),
                        }
                    )
                    continue

                # P1-C2: 服务端强制覆盖 source metadata（不信任 Markdown 自声明）
                skill.metadata = skill.metadata or {}
                skill.metadata["source_kind"] = "upload"
                skill.metadata["persisted"] = True

                # P1-C2: register 成功 → DB 写入 → DB 失败 unregister 回滚
                try:
                    registry.register(skill)
                except SkillRegistrationError as e:
                    msg = str(e)
                    errors.append(
                        {
                            "filename": filename or fallback_name,
                            "skill_name": skill.name,
                            "error_type": "SkillRegistrationError",
                            "error": msg,
                            "status": 409,
                        }
                    )
                    continue

                # DB 持久化（如果 extension_store 可用）
                if state.extension_store is not None:
                    try:
                        content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        await state.extension_store.upsert_uploaded_skill(
                            name=skill.name,
                            skill_json=skill.model_dump_json(),
                            raw_markdown=text,
                            enabled=True,
                            source_kind="upload",
                            content_sha256=content_sha,
                        )
                    except ExtensionStoreError as e:
                        # DB 失败 → 回滚 registry
                        registry.unregister(skill.name)
                        errors.append(
                            {
                                "filename": filename or fallback_name,
                                "skill_name": skill.name,
                                "error_type": "ExtensionStoreError",
                                "error": str(e),
                                "status": 500,
                            }
                        )
                        continue

                saved_skills.append(skill)

        # 至少一个成功 → 200；全部失败 → 用首个 error status 作整体 status
        out_skills = [serialize_skill(s, include_prompt=False) for s in saved_skills]
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
        """启用 skill。不存在 → 404；registry 未 attach → 422。

        P1-C2: uploaded Skill 的 enabled 状态持久化到 SQLite。
        非 uploaded Skill（filesystem/builtin/mcp_prompt）只改 runtime，不写 DB。
        DB 失败时恢复原 runtime 状态。
        """
        from ..skills import SkillNotFoundError
        from .extension_store import ExtensionStoreConflictError, ExtensionStoreError

        registry = _require_skill_registry()

        # 保存原状态用于回滚
        try:
            original_skill = registry.get(name)
            original_status = original_skill.status
        except SkillNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"skill {name!r} not found"},
            )

        async with state.skill_mutation_lock:
            registry.enable(name)
            # 只对 uploaded Skill 持久化
            if state.extension_store is not None and await _is_uploaded_skill(name):
                try:
                    await state.extension_store.set_skill_enabled(name, True)
                except (ExtensionStoreConflictError, ExtensionStoreError):
                    # DB 失败 → 恢复原状态
                    if original_status == "disabled":
                        registry.disable(name)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "ok": False,
                            "error": "persist failed; runtime rolled back",
                        },
                    )
        return {"ok": True, "name": name, "status": "enabled"}

    @app.post("/api/skills/{name}/disable", response_model=None)
    async def disable_skill(name: str) -> dict[str, Any] | JSONResponse:
        """禁用 skill。不存在 → 404；registry 未 attach → 422。

        P1-C2: uploaded Skill 的 enabled 状态持久化到 SQLite。
        """
        from ..skills import SkillNotFoundError
        from .extension_store import ExtensionStoreConflictError, ExtensionStoreError

        registry = _require_skill_registry()

        try:
            original_skill = registry.get(name)
            original_status = original_skill.status
        except SkillNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"skill {name!r} not found"},
            )

        async with state.skill_mutation_lock:
            registry.disable(name)
            if state.extension_store is not None and await _is_uploaded_skill(name):
                try:
                    await state.extension_store.set_skill_enabled(name, False)
                except (ExtensionStoreConflictError, ExtensionStoreError):
                    if original_status == "enabled":
                        registry.enable(name)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "ok": False,
                            "error": "persist failed; runtime rolled back",
                        },
                    )
        return {"ok": True, "name": name, "status": "disabled"}

    @app.delete("/api/skills/{name}", response_model=None)
    async def delete_skill(name: str) -> dict[str, Any] | JSONResponse:
        """删除 uploaded Skill。非 uploaded Skill → 403。

        P1-C2: DB row 为准——只有 web_uploaded_skills 中存在的 Skill 才能删除。
        DB 删除失败时重新 register 原 Skill 对象。
        """
        from ..skills import SkillNotFoundError
        from .extension_store import ExtensionStoreError

        registry = _require_skill_registry()

        if state.extension_store is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "extension store not initialized"},
            )

        # 检查 DB row——以数据库为准
        persisted = await state.extension_store.get_uploaded_skill(name)
        if persisted is None:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"skill {name!r} is not an uploaded skill; "
                    "only uploaded skills can be deleted"
                },
            )

        # 保存原 Skill 对象用于回滚
        try:
            original_skill = registry.get(name)
        except SkillNotFoundError:
            original_skill = None

        async with state.skill_mutation_lock:
            # runtime unregister
            if original_skill is not None:
                registry.unregister(name)
            # DB delete
            try:
                deleted = await state.extension_store.delete_uploaded_skill(name)
            except ExtensionStoreError:
                # DB 失败 → 重新 register 原 Skill
                if original_skill is not None:
                    try:
                        registry.register(original_skill)
                    except Exception:
                        pass  # re-register 失败——inconsistency，但已尽力
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "persist failed; runtime restored"},
                )
            if not deleted:
                # DB row 不存在（但前面检查过）——一致性异常
                if original_skill is not None:
                    try:
                        registry.register(original_skill)
                    except Exception:
                        pass
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"skill {name!r} not in persistence"},
                )

        return {"ok": True, "name": name, "deleted": True}

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
            harness.permission_policy.name if harness.permission_policy is not None else None
        )
        return {
            "policy_name": policy_name,
            "count": len(sliced),
            "records": [serialize_policy_audit_record(r) for r in sliced],
        }

    # ========================================================================
    # Slash commands
    # ========================================================================

    @app.get("/api/slash-commands")
    async def list_slash_commands() -> dict[str, Any]:
        """Return command metadata for the composer menu."""
        return {"count": len(SLASH_COMMANDS), "commands": list(SLASH_COMMANDS)}

    @app.post("/api/sessions/{session_id}/slash-commands", response_model=None)
    async def execute_slash_command(
        session_id: str,
        payload: dict[str, Any],
    ) -> JSONResponse:
        """Validate and start a managed slash command request."""
        try:
            command, _arguments = parse_slash_command((payload or {}).get("command"))
        except CheckpointerError as e:
            return JSONResponse(
                status_code=400,
                content={"detail": {"code": e.code, "message": e.message}},
            )

        if state.shutting_down:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "server_shutting_down",
                        "message": "Server is shutting down.",
                    }
                },
            )

        try:
            _ensure_idle()
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
            )

        # Reserve the same global execution slot used by prompt/regenerate.
        # No await is allowed between the idle check and this assignment.
        state.running = True
        request_id = f"req_{uuid4().hex[:16]}"
        durable_operation = None
        try:
            store = state.session_store
            if store is None or state.file_store is None:
                raise CheckpointerError(
                    "checkpointer_unavailable",
                    "Session store or file store is not initialized.",
                )
            session = await store.get_session(session_id)
            if session is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": {
                            "code": "session_not_found",
                            "message": f"Session {session_id!r} not found.",
                        }
                    },
                )
            if session_id in state.active_request_by_session:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": {
                            "code": "session_busy",
                            "message": "This session already has an active request.",
                        }
                    },
                )
            messages = await store.list_messages(session_id)
            source = build_checkpoint_source(messages)
            if source.message_count == 0:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": {
                            "code": "nothing_to_checkpoint",
                            "message": "There are no messages to checkpoint.",
                        }
                    },
                )
            operation_payload = {
                "source_sha256": source.source_sha256,
                "source_message_count": source.message_count,
                "memory_logical_path": SESSION_MEMORY_PATH,
            }
            try:
                durable_operation = await store.start_operation(
                    session_id,
                    kind="checkpointer",
                    dedupe_key=source.source_sha256,
                    operation_id=f"op_{uuid4().hex[:20]}",
                    payload=operation_payload,
                )
            except SessionOperationConflictError:
                # A prior failed request may have left a published operation
                # open. Reduce it before rejecting a genuinely unrelated open
                # operation; changed leaves are marked conflict without clear.
                await recover_checkpointer_operations(
                    store, state.file_store, session_id=session_id
                )
                durable_operation = await store.start_operation(
                    session_id,
                    kind="checkpointer",
                    dedupe_key=source.source_sha256,
                    operation_id=f"op_{uuid4().hex[:20]}",
                    payload=operation_payload,
                )
        except CheckpointerError as e:
            return JSONResponse(
                status_code=503,
                content={"detail": {"code": e.code, "message": e.message}},
            )
        except SessionOperationConflictError:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "durable_operation_conflict",
                        "message": ("This session has an unfinished durable operation."),
                    }
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "checkpointer_start_failed",
                        "message": _safe_error(e),
                    }
                },
            )
        finally:
            # Ownership transfers to the background request only after it is
            # registered below. Early response paths must release the slot.
            if session_id not in state.active_request_by_session:
                state.running = False

        assert durable_operation is not None
        web_request = WebRunRequest(
            id=request_id,
            session_id=session_id,
            status="queued",
            created_at=_now_utc(),
            operation="checkpointer",
            payload={
                "command": command,
                "durable_operation_id": durable_operation.id,
            },
        )
        state.active_requests[request_id] = web_request
        state.active_request_by_session[session_id] = request_id
        # The validation finally block released state.running before request
        # registration; reacquire it synchronously before scheduling the task.
        state.running = True
        task = asyncio.create_task(
            _run_checkpointer_background(web_request, source, durable_operation.id),
            name=f"checkpointer_{request_id}",
        )
        web_request.task = task
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "command": command,
                "request_id": request_id,
                "session_id": session_id,
                "status": "queued",
                "request_url": f"/api/requests/{request_id}",
                "abort_url": f"/api/requests/{request_id}/abort",
            },
        )

    # ========================================================================
    # Plan Mode control plane
    # ========================================================================

    @app.get("/api/plan-runs/{run_id}", response_model=None)
    async def get_plan_run(run_id: str) -> dict[str, object] | JSONResponse:
        from coding_agent_app.planning.store import PlanNotFoundError

        if state.plan_store is None:
            return JSONResponse(status_code=404, content={"detail": "Plan mode is disabled"})
        try:
            run = await state.plan_store.get_run(run_id)
        except PlanNotFoundError:
            return JSONResponse(status_code=404, content={"detail": "Plan run not found"})
        return {"plan": run.model_dump(mode="json")}

    @app.get("/api/sessions/{session_id}/plan-runs/latest", response_model=None)
    async def get_latest_plan_run(
        session_id: str,
    ) -> dict[str, object] | JSONResponse:
        if state.plan_store is None:
            return {"plan": None}
        store = state.session_store
        if store is None or await store.get_session(session_id) is None:
            return JSONResponse(status_code=404, content={"detail": "Session not found"})
        run = await state.plan_store.latest_for_session(session_id)
        return {
            "plan": run.model_dump(mode="json") if run is not None else None,
        }

    @app.post("/api/plan-runs/{run_id}/approve", response_model=None)
    async def approve_plan_run(run_id: str) -> dict[str, object] | JSONResponse:
        from coding_agent_app.planning.store import (
            PlanConflictError,
            PlanNotFoundError,
        )

        if state.plan_store is None:
            return JSONResponse(status_code=404, content={"detail": "Plan mode is disabled"})
        try:
            pending = await state.plan_store.get_run(run_id)
            owner = state.active_requests.get(pending.request_id)
            if owner is None or owner.status != "running":
                return JSONResponse(
                    status_code=409,
                    content={"detail": "Plan run no longer has an active request"},
                )
            run, idempotent = await state.plan_store.approve(run_id)
        except PlanNotFoundError:
            return JSONResponse(status_code=404, content={"detail": "Plan run not found"})
        except PlanConflictError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})

        approval_events = cast(dict[str, asyncio.Event], container["plan_approval_events"])
        event = approval_events.get(run_id)
        if event is not None:
            event.set()
        await _emit_web_payload(
            {"type": "plan_approved", "plan": run.model_dump(mode="json")},
            run.request_id,
            run.session_id,
        )
        return {
            "ok": True,
            "idempotent": idempotent,
            "plan": run.model_dump(mode="json"),
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
        if (payload or {}).get("execution_mode", "direct") == "plan":
            return JSONResponse(
                status_code=400,
                content={"detail": "Plan mode requires the asynchronous prompt endpoint"},
            )
        try:
            validated = await _validate_prompt_payload(payload)
            # set request context（sync 路径用临时 request_id，不进 active_requests）
            sync_request_id = f"req_sync_{uuid4().hex[:12]}"
            state.current_request_id = sync_request_id
            state.current_request_session_id = validated.session_id
            tool_session_token = tool_session_context.set(validated.session_id)
            try:
                result = await _run_prompt_request(validated)
            finally:
                state.current_request_id = None
                state.current_request_session_id = None
                tool_session_context.reset(tool_session_token)
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
            "coding_sandbox": result.coding_sandbox,
            "continuity": result.continuity,
            "workspace_context": result.workspace_context,
            "intent": result.intent,
            "plan_run": result.plan_run,
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
                content={"detail": "server shutting down; cannot accept new prompts"},
            )

        # 1. 完整乐观校验——_ensure_idle / text / skill_names / unknown skill /
        #    session 存在 / file ownership 都在 _validate_prompt_payload 里
        try:
            validated = await _validate_prompt_payload(payload)
        except PromptValidationError as e:
            return _serialize_prompt_validation_error(e)
        except HTTPException as e:
            # _ensure_idle 抛 HTTPException(409)——转与同步路径一致的 schema
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

        # 2. session 级并发检查
        session_id = validated.session_id
        if session_id and session_id in state.active_request_by_session:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (f"session {session_id!r} already has an active request"),
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
            payload={
                **(dict(payload) if isinstance(payload, dict) else {}),
                # Resolved value is private request state used by abort while
                # Coding preflight/finalization runs with the Harness idle.
                "coding_mode": validated.coding_mode,
                "execution_mode": validated.execution_mode,
            },
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
        events_url = "/api/events" + (f"?session_id={session_id}" if session_id else "")
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
                "intent": (
                    validated.intent.public() if validated.intent is not None else None
                ),
                "execution_mode": validated.execution_mode,
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
            all_reqs = [r for r in all_reqs if r.status in ("completed", "error", "aborted")]
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

    @app.get("/api/requests/{request_id}/approvals", response_model=None)
    async def list_request_approvals(
        request_id: str,
        status: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """List browser-safe approval records for one owned Web request."""
        req = _find_request(request_id)
        if req is None:
            return JSONResponse(status_code=404, content={"detail": "request not found"})
        if status not in (None, "pending", "approved", "denied", "cancelled"):
            return JSONResponse(status_code=400, content={"detail": "invalid approval status"})
        approvals = approval_manager.list_for_request(
            request_id,
            status=cast(Any, status),
        )
        return {
            "request_id": request_id,
            "session_id": req.session_id,
            "count": len(approvals),
            "approvals": approvals,
        }

    @app.post(
        "/api/requests/{request_id}/approvals/{approval_id}",
        response_model=None,
    )
    async def resolve_request_approval(
        request_id: str,
        approval_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """Approve once or deny the immutable ToolCall held by the Agent task."""
        req = _find_request(request_id)
        if req is None:
            return JSONResponse(status_code=404, content={"detail": "approval not found"})
        decision = payload.get("decision") if isinstance(payload, dict) else None
        if decision not in ("approve", "deny"):
            return JSONResponse(
                status_code=400,
                content={"detail": "decision must be 'approve' or 'deny'"},
            )
        try:
            approval, idempotent = await approval_manager.resolve(
                request_id=request_id,
                approval_id=approval_id,
                decision=decision,
            )
        except KeyError:
            return JSONResponse(status_code=404, content={"detail": "approval not found"})
        except ValueError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})
        return {
            "ok": True,
            "request_id": request_id,
            "session_id": req.session_id,
            "approval": approval,
            "idempotent": idempotent,
        }

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
                status_code=400,
                detail="limit must be >= 0 (or omitted)",
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
                            client_queue.get(),
                            timeout=_SSE_HEARTBEAT_SECONDS,
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
            await websocket.send_json(
                {
                    "type": "hello",
                    "agent_status": _agent_status(),
                    "first_available_sequence": state.event_buffer.first_sequence,
                    "last_available_sequence": state.event_buffer.last_sequence,
                    "server_time": _now_utc().isoformat(),
                    "_received_at_ms": int(time.time() * 1000),
                }
            )
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

    # D2-4：内部函数挂到 app.state 便于测试访问——**不**是 public API；
    # 调用方应继续用 POST /api/prompt / async / (D2-5 未来的) /regenerate。
    app.state.d24_execute_prompt = _execute_prompt
    app.state.d24_persist_normal = _persist_normal_prompt_result
    app.state.d24_persist_regeneration = _persist_regeneration_result
    app.state.d24_reset_harness = _reset_harness_to_session
    app.state.d24_run_regeneration = _run_regeneration_core
    app.state.d24_extract_terminal = _extract_terminal_assistant
    app.state.d24_validated_factory = _validate_prompt_payload

    return app


# ============================================================================
# App 生命周期辅助
# ============================================================================


_logger = logging.getLogger(__name__)


async def _compensate_delete_session(state: Any, session_id: str) -> None:
    """Best-effort compensation delete for Session binding failure (P1-E2-3B3).

    Per E2-3A audit §5.2:
        - Idempotent: SessionNotFoundError is treated as success
        - Other exceptions propagate (caller logs critical)
        - Does NOT broadcast any UI event (silent compensation)
        - Does NOT log Session content—only safe session_id

    Caller must wrap in ``asyncio.shield`` if cancellation safety is required.
    """
    from ..session_sqlite import SessionNotFoundError

    file_store = getattr(state, "file_store", None)
    if file_store is not None:
        await file_store.delete_session_files(session_id)

    try:
        await state.session_store.delete_session(session_id)
    except SessionNotFoundError:
        # Already gone—idempotent success
        pass
    except Exception:
        # Re-raise so caller's asyncio.shield surfaces the failure for logging
        raise


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
    web_handler = getattr(app.state, "web_tool_approval_handler", None)
    previous_handler = getattr(app.state, "previous_tool_approval_handler", None)
    if (
        harness is not None
        and web_handler is not None
        and harness.agent.tool_approval_handler is web_handler
    ):
        harness.set_tool_approval_handler(previous_handler)
    web_model_hook = getattr(app.state, "web_before_model_call", None)
    previous_model_hook = getattr(app.state, "previous_before_model_call", None)
    if (
        harness is not None
        and web_model_hook is not None
        and harness.agent.before_model_call is web_model_hook
    ):
        harness.agent.before_model_call = previous_model_hook


# ============================================================================
# 内部辅助
# ============================================================================


def _sse_format(event_name: str, payload: Any) -> str:
    """构造一条 SSE 消息——`event: {name}\\ndata: {json}\\n\\n`。"""
    data_str = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {data_str}\n\n"


__all__ = ["create_app", "dispose_app"]

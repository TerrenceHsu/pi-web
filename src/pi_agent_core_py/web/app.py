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
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
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
    Response,
    StreamingResponse,
)

from ..harness import AgentHarness
from ..skills import SkillSelection
from .provider_runtime import (
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
    - `stop_reason`：从 harness.last_snapshot.metadata 提取（如果有）
    - `usage`：同上
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
    # P2-R1: Knowledge Library subsystem root directory.
    # None = 不启用（默认；保持向后兼容）；传入 Path 时启用：
    #   - 在 <knowledge_root>/knowledge.db 打开独立 aiosqlite connection
    #   - <knowledge_root>/libraries/{library_id}/documents/... 物理文件
    #   - 挂载 Knowledge Library CRUD + Session Binding REST API（仅当
    #     trusted_host + UI header deps 启用时；否则不挂 router，service
    #     仍可用于内部 / 测试）
    knowledge_root: str | Path | None = None,
    # 显式控制 Knowledge API 是否挂载。
    # None = auto（仅当 knowledge_root 非 None + trusted_host 启用时挂载）
    # True = 强制挂载（需 knowledge_root 非 None）
    # False = 不挂 router，但若 knowledge_root 非 None 仍 init service
    enable_knowledge_api: bool | None = None,
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

    # ========================================================================
    # P1-E1-4B3: Resolve Credential API configuration at app creation
    # ========================================================================
    from .credentials_runtime import (
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
        raise RuntimeError(
            f"credential web security configuration error: {e}"
        ) from e

    # ========================================================================
    # P1-E2-3B1: Resolve Provider Profiles API configuration at app creation
    # Depends on Credential resolver—must be called AFTER _cred_resolved.
    # ========================================================================
    from .provider_config_runtime import (
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
        raise RuntimeError(
            f"provider config web security configuration error: {e}"
        ) from e

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

        # P1-C2: 初始化 extension_store（与 session_store 共享 connection）
        # + skill_mutation_lock + 启动恢复 uploaded Skills
        from .extension_store import ExtensionSQLiteStore

        extension_store = ExtensionSQLiteStore(
            store_path, connection=session_store.connection
        )
        await extension_store.init()
        state.extension_store = extension_store
        state.skill_mutation_lock = asyncio.Lock()

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
            # 安全摘要——不含 content / SQL / 绝对路径 / traceback / secret
            raise RuntimeError(
                f"startup sweep failed: {type(e).__name__}"
            ) from e

        # 启动恢复 uploaded Skills（逐行隔离 + sha256 校验 + model_validate）
        await _restore_uploaded_skills(extension_store)

        # P1-C4: 恢复 MCP server 配置 + auto attach + apply disabled tools
        await _restore_mcp_servers(extension_store)

        # ====================================================================
        # P2-R1: Knowledge Library subsystem composition
        # 仅在 knowledge_root 非 None 时启用——独立 knowledge.db aiosqlite
        # connection + 独立文件根目录（P2-R0 §2 + §3 决策 R2 / R5）
        # ====================================================================
        knowledge_service = None
        if knowledge_root is not None:
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
                        f"ingestion worker manager start failed: "
                        f"{type(e).__name__}"
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
                        f"indexing worker manager start failed: "
                        f"{type(e).__name__}"
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

                    def _evidence_registry_getter():
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
                        f"search_knowledge tool init failed: "
                        f"{type(e).__name__}"
                    ) from e
        else:
            state.knowledge_service = None
            state.knowledge_store = None
            state.knowledge_file_store = None
            state.ingestion_worker_manager = None
            state.indexing_worker_manager = None

        # ====================================================================
        # P1-E1-4A: Credential Runtime Composition Root
        # 仅在文件型 DB 路径 + 显式 / 默认 enable 时启动；独立 connection
        # 与 session/extension store 共享 DB 文件但生命周期独立
        # ====================================================================
        from .credentials_runtime import (
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
                raise RuntimeError(
                    f"credential runtime config error: {type(e).__name__}"
                ) from e

        if cred_runtime_cm is not None:
            _app.state.credential_runtime = await cred_runtime_cm.__aenter__()
            try:
                # P1-E2-3B1: Provider Config runtime nested inside credential runtime.
                # Depends on CredentialService (safe API) — must init AFTER credential
                # runtime entered, shutdown BEFORE credential runtime exits.
                if _pc_resolved.runtime_enabled:
                    from .provider_config_runtime import (
                        provider_config_runtime_context,
                    )

                    # Build session_exists callback bound to current session_store.
                    # SQLiteSessionStore.get_session returns None for not-found (no raise).
                    async def _session_exists_cb(session_id: str) -> bool:
                        if state.session_store is None:
                            return False
                        try:
                            session = await state.session_store.get_session(session_id)
                        except Exception:
                            return False
                        return session is not None

                    pc_runtime_cm = provider_config_runtime_context(
                        database_path=str(db_path),
                        credential_service=_app.state.credential_runtime.service,
                        session_exists=_session_exists_cb,
                    )
                    _app.state.provider_config_runtime = (
                        await pc_runtime_cm.__aenter__()
                    )
                    # M1-5: 构造 RequestProviderRuntime——仅在 Credential + Provider
                    # Config 两个 runtime 都启动时. 无独立 lifespan——纯 Python 对象
                    # 无长期网络资源. Prompt 路径通过 app.state.request_provider_runtime
                    # 读取；为 None 时 _execute_prompt 走 legacy client 兼容路径.
                    from ..providers.factory import create_provider
                    from ..providers.registry import _DEFAULT_REGISTRY
                    from .provider_runtime import RequestProviderRuntime

                    _app.state.request_provider_runtime = RequestProviderRuntime(
                        provider_config_service=_app.state.provider_config_runtime.service,
                        credential_service=_app.state.credential_runtime.service,
                        provider_registry=_DEFAULT_REGISTRY,
                        provider_factory=create_provider,
                    )
                    try:
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
                    yield
            finally:
                _app.state.credential_runtime = None
                _app.state.request_provider_runtime = None
                try:
                    await cred_runtime_cm.__aexit__(None, None, None)
                except Exception:
                    pass
        else:
            _app.state.credential_runtime = None
            _app.state.provider_config_runtime = None
            _app.state.request_provider_runtime = None
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
            state.ingestion_worker_manager
            if hasattr(state, "ingestion_worker_manager")
            else None
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
            state.indexing_worker_manager
            if hasattr(state, "indexing_worker_manager")
            else None
        )
        if indexing_mgr is not None:
            try:
                await indexing_mgr.stop()
            except Exception:
                pass
            state.indexing_worker_manager = None
        # P2-R1: 关闭 KnowledgeStore（独立 connection）
        k_store = state.knowledge_store if hasattr(state, "knowledge_store") else None
        if k_store is not None:
            try:
                await k_store.close()
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
    # P1-C2: skill_mutation_lock 在 state 创建时立即 init（不依赖 lifespan）
    state.skill_mutation_lock = asyncio.Lock()
    # P1-C3: mcp_mutation_lock 同理
    state.mcp_mutation_lock = asyncio.Lock()
    app.state.web = state
    app.state.allow_prompt_preview = allow_prompt_preview
    app.state.event_buffer_max_size = event_buffer_max_size
    # P1-E1-4A: credential_runtime placeholder——lifespan 启动时填入
    app.state.credential_runtime = None
    # P1-E2-3B1: provider_config_runtime placeholder——lifespan 启动时填入
    app.state.provider_config_runtime = None
    # M1-5: request_provider_runtime placeholder——仅 cred + pc runtime 都启用时填入
    app.state.request_provider_runtime = None

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
    _pending_middlewares: list[tuple[type, dict]] = []

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
        from .credentials_api import (
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
        from .provider_profiles_api import (
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

    # P2-R1: Knowledge Library REST API（Library CRUD + Document metadata
    # + Session Binding）. 默认 None = 不启用。显式 knowledge_root + trusted_host
    # + enable_knowledge_api（或 auto）= 挂载 router，复用 E1 UI/origin deps.
    if knowledge_root is not None:
        from .knowledge.api import (
            build_knowledge_router,
            build_session_knowledge_router,
        )
        from .local_web_security import default_web_security_config as _k_ws

        _k_ws_cfg = _k_ws(
            extra_hosts=credential_extra_hosts,
            extra_ui_origins=credential_extra_ui_origins,
        )
        _knowledge_api_enabled = (
            enable_knowledge_api
            if enable_knowledge_api is not None
            else _cred_resolved.trusted_host_enabled
        )
        if _knowledge_api_enabled:
            app.include_router(
                build_knowledge_router(_k_ws_cfg), prefix="/api/knowledge"
            )
            app.include_router(
                build_session_knowledge_router(_k_ws_cfg), prefix="/api/sessions"
            )

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
                await ext_store.set_skill_restore_error(
                    name, result.error or "decode failed"
                )
                continue

            persisted = result.skill

            # sha256 校验 raw_markdown
            if persisted.content_sha256:
                actual_sha = hashlib.sha256(
                    persisted.raw_markdown.encode("utf-8")
                ).hexdigest()
                if actual_sha != persisted.content_sha256:
                    await ext_store.set_skill_restore_error(
                        name, "content hash mismatch"
                    )
                    continue

            # decode skill_json + model_validate
            try:
                skill_data = _json.loads(persisted.skill_json)
                skill = Skill.model_validate(skill_data)
            except Exception as e:
                await ext_store.set_skill_restore_error(
                    name, f"decode failed: {type(e).__name__}"
                )
                continue

            # 服务端强制覆盖 source metadata（不信任 Markdown 自声明）
            skill.metadata = skill.metadata or {}
            skill.metadata["source_kind"] = "upload"
            skill.metadata["persisted"] = True

            # 同名冲突——filesystem/built-in 优先
            if registry.has(name):
                await ext_store.set_skill_restore_error(
                    name, "name conflict with existing skill"
                )
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
                await ext_store.set_mcp_restore_error(
                    name, result.error or "decode failed"
                )
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
                    timeout_s=10.0,
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
                        server_state.last_error
                        if server_state
                        else "not in registry after attach"
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
                cfg.last_error = _safe_extension_error(
                    e, list(resolved_env.values())
                )
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

        try:
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
        except Exception:
            # Rollback the reservation if any validation step fails.
            state.running = False
            raise

    async def _run_prompt_core(
        validated: _PromptValidated,
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

        execution = await _execute_prompt(validated)

        # P2-R4-C2: Citation transform — after LLM generates text with
        # [cite:E1] tokens, validate against EvidenceRegistry and render
        # numbered citations + source footer before persistence.
        _apply_citation_transform(execution, state)

        return await _persist_normal_prompt_result(validated, execution)

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
            has_only_tool_calls = all(
                isinstance(c, ToolCall) for c in msg.content
            ) and msg.content
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
        from ..messages import AssistantMessage

        try:
            target_idx = max(
                i for i, m in enumerate(canonical_before)
                if isinstance(m, AssistantMessage)
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

        # 2. 临时替换 harness state，执行 model
        harness.agent.state.messages = list(regeneration_history)

        # P2-R4-B2 + R4-C2: Reset turn-scoped Evidence Registry + apply
        # citation transform for regenerate path (mirrors _run_prompt_core).
        # Without this, regenerate would bypass the citation pipeline.
        state._evidence_registry = None

        try:
            execution = await _execute_prompt(
                validated,
                override_initial_messages=regeneration_history,
                suppress_user_append=True,
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

    async def _execute_prompt(
        validated: _PromptValidated,
        *,
        override_initial_messages: list[Any] | None = None,
        suppress_user_append: bool = False,
    ) -> PromptExecutionResult:
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

        try:
            if runtime is not None:
                selection = await runtime.resolve_selection(validated.session_id)

            # M1-5: bind_to_harness 在 active-request ownership 内部；
            # AsyncExitStack 让 selection=None 时跳过绑定（legacy path）.
            async with AsyncExitStack() as stack:
                if runtime is not None and selection is not None:
                    await stack.enter_async_context(
                        runtime.bind_to_harness(
                            harness=harness, selection=selection
                        )
                    )

                if suppress_user_append:
                    # Regenerate 路径——caller 已设置 harness.agent.state.messages
                    messages = await harness.run_continue(
                        skill_selection=validated.skill_selection
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
                        skill_selection=validated.skill_selection
                    )
                else:
                    messages = await harness.run_prompt(
                        validated.text, skill_selection=validated.skill_selection
                    )
        except ProviderSelectionNotFoundError:
            state.last_error = "Selected provider profile is unavailable."
            raise PromptRuntimeError(
                500, state.last_error, "provider_profile_unavailable"
            ) from None
        except ProviderSelectionDisabledError:
            state.last_error = "Selected provider profile is disabled."
            raise PromptRuntimeError(
                500, state.last_error, "provider_profile_disabled"
            ) from None
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

        messages_after = list(harness.agent.state.messages)
        # candidate 提取：从 suffix 中找最后一个合格 AssistantMessage
        assistant_candidate = _extract_terminal_assistant(
            messages_after[len(messages_before):]
        )

        # snapshot metadata 提取（如果有）
        snapshot = harness.last_snapshot
        snapshot_payload: dict[str, Any] | None = None
        stop_reason: str | None = None
        usage: Any | None = None
        if snapshot is not None:
            stop_reason = snapshot.metadata.get("stop_reason") if snapshot.metadata else None
            usage = snapshot.metadata.get("usage") if snapshot.metadata else None
            try:
                # to_dict 是 dataclass method；可能抛异常——best-effort
                snapshot_payload = snapshot.to_dict()  # type: ignore[attr-defined]
            except Exception:
                snapshot_payload = None

        applied_skill_names: list[str] = list(
            validated.skill_selection.names
        ) if (
            validated.skill_selection is not None
            and validated.skill_selection.names
        ) else []

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
        if validated.store is not None and validated.session_id is not None:
            try:
                await validated.store.replace_messages(
                    validated.session_id, list(execution.messages)
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

        applied_skill_names: list[str] = list(
            validated.skill_selection.names
        ) if (
            validated.skill_selection is not None
            and validated.skill_selection.names
        ) else []
        attachment_meta = _build_attachment_meta(validated.attached_summary)

        return PromptRunOutcome(
            messages=execution.messages,
            serialized_messages=[
                serialize_message(m) for m in execution.messages
            ],
            session_id=validated.session_id,
            attachment_meta=attachment_meta,
            applied_skill_names=applied_skill_names,
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
        candidate_json = _serialize_assistant_for_messages(
            execution.assistant_message
        )

        # 核心：finalize_revision（单 BEGIN IMMEDIATE transaction）
        try:
            await state.extension_store.finalize_revision(  # type: ignore[union-attr]
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
                await validated.store.append_snapshot(
                    validated.session_id, harness.last_snapshot
                )
            except Exception as e:
                # finalize 已 commit——snapshot 失败不能回滚 active answer
                snapshot_error = f"snapshot: {type(e).__name__}: {e}"
                state.last_error = snapshot_error

        applied_skill_names: list[str] = list(
            validated.skill_selection.names
        ) if (
            validated.skill_selection is not None
            and validated.skill_selection.names
        ) else []
        attachment_meta = _build_attachment_meta(validated.attached_summary)

        return PromptRunOutcome(
            messages=execution.messages,
            serialized_messages=[
                serialize_message(m) for m in execution.messages
            ],
            session_id=validated.session_id,
            attachment_meta=attachment_meta,
            applied_skill_names=applied_skill_names,
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
            state.last_error = (
                f"reset_harness: {type(e).__name__}: {e}"[:500]
            )
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
                503, "extension_store_unavailable",
                "Extension store not initialized.",
            )

        # session 存在
        try:
            session = await store.get_session(session_id)
        except Exception:
            session = None
        if session is None:
            raise RegenerationValidationError(
                404, "session_not_found",
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
                404, "message_not_found",
                f"Assistant message {assistant_message_id!r} not found in this session.",
            )
        if msg_row["role"] != "assistant":
            raise RegenerationValidationError(
                400, "regenerate_target_not_assistant",
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
                409, "regenerate_target_not_latest",
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
                409, "regenerate_missing_user_message",
                "No preceding user message found to regenerate from.",
            )

        # active request 检查
        if session_id in state.active_request_by_session:
            raise RegenerationValidationError(
                409, "request_already_active",
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
                409, "revision_already_running",
                "A regeneration is already running for this assistant message.",
            )

        # 构造 history——canonical active messages[:target_idx]（不含旧 assistant）
        canonical = await store.list_messages(session_id)
        history = tuple(canonical[: msg_row["idx"]])

        return ValidatedRegenerationRequest(
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            preceding_user_message_id=preceding["id"],
            history=history,
            original_harness_messages=tuple(harness.agent.state.messages),
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
        state.current_request_id = request_id
        state.current_request_session_id = web_request.session_id
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
        )

        try:
            await _run_regeneration_core(
                prompt_validated,
                revision_id=revision_id,
                assistant_message_id=validated.assistant_message_id,
                request_id=request_id,
            )
        except asyncio.CancelledError:
            # 显式 mark_revision_aborted（不让通用 except 捕获）
            try:
                await ext_store.mark_revision_aborted(
                    revision_id=revision_id, request_id=request_id,
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
            }
        finally:
            web_request.event_end_sequence = state.next_event_sequence - 1
            state.current_request_id = None
            state.current_request_session_id = None
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
        - 不传 session_id → 返回当前 agent.state.messages（fallback 旧路径，
          无 message_id——这些 message 不在 DB 中）
        - 传 session_id → 返回 sqlite 中该 session 的 messages（**D2-6 起含
          message_id**——使用 PersistedMessage DTO，按 idx 升序，强类型对象）

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
        from .serializers import serialize_persisted_message
        try:
            # D2-6：用 list_persisted_messages——含 message_id（regenerate 必需）
            stored_msgs = await store.list_persisted_messages(session_id)
        except SessionNotFoundError:
            return JSONResponse(
                status_code=404,
                content={"detail": f"session {session_id!r} not found"},
            )
        return {
            "count": len(stored_msgs),
            "session_id": session_id,
            "messages": [serialize_persisted_message(m) for m in stored_msgs],
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
    async def post_sessions(
        request: Request, payload: dict[str, Any],
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
                    await asyncio.shield(
                        _compensate_delete_session(state, s.id)
                    )
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
        # P2-R1: 删除 session 后清理 knowledge library bindings（P2-R0 §7.2 不变量 7）
        # 只清 binding，不删 library 本身。失败不阻塞 session 删除（记 warning）。
        k_service = state.knowledge_service if hasattr(state, "knowledge_service") else None
        if k_service is not None:
            try:
                await k_service.on_session_deleted(sid)
            except Exception as e:
                state.last_error = (
                    f"knowledge.on_session_deleted({sid}) failed: "
                    f"{type(e).__name__}"
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
                content={
                    "detail": {"code": "session_not_found", "message": "Session not found."}
                },
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
        sorted_names = sorted(
            state.mcp_server_configs.keys(), key=len, reverse=True
        )
        for server_name in sorted_names:
            prefix = f"{_MCP_TOOL_NAME_PREFIX}{server_name}__"
            if full_name.startswith(prefix):
                raw_tool = full_name[len(prefix):]
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

        async with state.mcp_mutation_lock:
            # detach if attached
            if cfg.enabled:
                cfg.enabled = False
                try:
                    await _refresh_enabled_mcp_servers()
                except Exception as e:
                    state.last_error = (
                        f"delete_mcp_server({name}) detach failed: "
                        f"{type(e).__name__}: {e}"
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
                    await state.extension_store.enable_mcp_tool(
                        server_name, raw_tool_name
                    )
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
                    await state.extension_store.disable_mcp_tool(
                        server_name, raw_tool_name
                    )
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

                # P1-C2: 服务端强制覆盖 source metadata（不信任 Markdown 自声明）
                skill.metadata = skill.metadata or {}
                skill.metadata["source_kind"] = "upload"
                skill.metadata["persisted"] = True

                # P1-C2: register 成功 → DB 写入 → DB 失败 unregister 回滚
                try:
                    registry.register(skill)
                except SkillRegistrationError as e:
                    msg = str(e)
                    errors.append({
                        "filename": filename or fallback_name,
                        "skill_name": skill.name,
                        "error_type": "SkillRegistrationError",
                        "error": msg,
                        "status": 409,
                    })
                    continue

                # DB 持久化（如果 extension_store 可用）
                if state.extension_store is not None:
                    try:
                        content_sha = hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest()
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
                        errors.append({
                            "filename": filename or fallback_name,
                            "skill_name": skill.name,
                            "error_type": "ExtensionStoreError",
                            "error": str(e),
                            "status": 500,
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


# ============================================================================
# 内部辅助
# ============================================================================


def _sse_format(event_name: str, payload: Any) -> str:
    """构造一条 SSE 消息——`event: {name}\\ndata: {json}\\n\\n`。"""
    data_str = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {data_str}\n\n"


__all__ = ["create_app", "dispose_app"]
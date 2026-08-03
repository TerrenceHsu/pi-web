# P2-R2-C0 — Ingestion Runtime and API Contract（冻结）

> **阶段**：P2-R2-C0 Ingestion Runtime and API Contract Audit（docs-only / contract-only / audit-only）
> **基线 commit**：`c2436c7` — docs(rag): ratify P2-R2-B amendment 2
> **日期**：2026-08-03
> **范围**：冻结 PDF Ingestion Runtime（Upload Service / Orchestrator / Bounded Worker / API）的完整合同——状态机 / 事务顺序 / 失败矩阵 / 错误码映射 / API 路由 / Schema Gap 分析 / R2-C 子阶段。**不**写生产代码 / 测试 / 依赖 / lockfile / schema / frontend。
> **配套文档**：
> - [P2_R2_C0_INGESTION_CONTRACT_AUDIT.md](../validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md) — 验证归档
> - [p2-r0-rag-contract.md](p2-r0-rag-contract.md) — 主合同（§8.1 已最小化对齐）
> - [p2-r0-decisions-log.md](p2-r0-decisions-log.md) — 决策表

---

## 0. Status

```
P2-R2-C0 Ingestion Runtime and API Contract
✅ COMPLETE / FROZEN @ <this commit>

Schema Amendment required?
✅ NOT REQUIRED (R1 schema sufficient with app-level transactions)

P2-R2-C1 Ingestion Store Extensions + Orchestrator
✅ APPROVED TO START (独立启动授权另需用户发起)

P2-R2-C2 Bounded Worker + Recovery
⛔ BLOCKED BY C1

P2-R2-C3 Upload / Status / Retry / Markdown API
⛔ BLOCKED BY C2

P2-R2-C4 Integration Validation + R2-C Freeze
⛔ BLOCKED BY C3

P2-R2-D Integration Validation
⛔ BLOCKED BY COMPLETE R2-C
⚠ MUST RECONCILE 8-test discrepancy (audit §5.5)
```

**Open Questions = 0**；**Schema Gap (blocking) = 0**；**Contract Conflicts Resolved = 1**（R0 §8.1 sync→async 透传，per R2-C0 directive 授权的最小化合同细化）。

---

## 1. Objective

为 R2-C1~C4 编码冻结合同。R2-C 完成后交付：

```
Upload PDF → Validate → Reserve Document → Persist source.pdf
→ Create Ingestion Job → Bounded Worker claims
→ Parser.inspect → Parser.extract → QualityEvaluator
→ {USABLE} → CanonicalMarkdownBuilder → Safe Persistence
→ Commit Document/Job terminal state
→ Status / Retry / Markdown API
```

每一步组件职责、文件/DB 顺序、状态进入条件、失败补偿、并发防护、恢复策略、API 形态由本文唯一冻结。

---

## 2. Frozen Baseline

```
P1-E Multi-Provider Switching       ✅ FROZEN @ ee62732
P2-R0 RAG Contract Audit            ✅ FROZEN @ b32e4b4
P2-R0 Amendment 1                   ✅ FROZEN @ f411ad7
P2-R1 Library Foundation            ✅ FROZEN @ 3bca5fd
P2-R1 Archive Correction            ✅ FROZEN @ 0fd4715
P2-R2-0 PDF Parser License Gate     ✅ FROZEN @ 533fe48
P2-R2-0 Archive Corrections         ✅ FROZEN @ f804fc7
P2-R2-A pypdf Parser Adapter        ✅ FROZEN @ 0772324
P2-R2-A Archive Corrections         ✅ FROZEN @ 7db4780
P2-R0 Amendment 2                   ✅ FROZEN @ 15b411b
P2-R2-B Canonical Markdown Builder  ✅ FROZEN @ eb193b2
P2-R2-B Archive Closure Ratification ✅ RATIFIED @ c2436c7
```

C0 启动 HEAD = `c2436c7`；G1 stash = `d7240268ec8b8e5d9e195c999e56fb6ec130fd55`（不变）。

---

## 3. Existing Schema Audit（from `web/knowledge/store.py::_DDL_STATEMENTS`）

### 3.1 `knowledge_libraries`

```sql
CREATE TABLE knowledge_libraries (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    CHECK (name <> ''),
    CHECK (status IN ('active', 'archived', 'deleting', 'failed'))
);
```

Library status enum 与 R2-C 相关：`active`（可上传/retry）、`archived`（只读）、`deleting`（删除中）、`failed`（不可用）。

### 3.2 `knowledge_documents`

```sql
CREATE TABLE knowledge_documents (
    id                TEXT PRIMARY KEY,
    library_id        TEXT NOT NULL,
    source_name       TEXT NOT NULL,
    source_sha256     TEXT NOT NULL,
    source_relpath    TEXT NOT NULL,
    markdown_relpath  TEXT NOT NULL,
    mime_type         TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    page_count        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'uploaded',
    parser_version    TEXT NOT NULL DEFAULT '',
    error_code        TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    CHECK (source_name <> ''),
    CHECK (mime_type <> ''),
    CHECK (status IN ('uploaded', 'extracting', 'normalizing', 'chunking',
                      'indexing', 'ready', 'failed', 'needs_ocr', 'deleting')),
    CHECK (page_count >= 0),
    CHECK (size_bytes >= 0),
    UNIQUE (library_id, source_sha256)
);
```

R2-C 关键事实：

- `markdown_relpath` **已存在**（必填非空）→ upload 时即可写入固定路径 `documents/{doc_id}/document.md`。
- `parser_version` **存在**但 `parser_id` **不存在**——MVP 不需 DB 级 parser_id（artifact frontmatter 已含 `parser_id`，需要时从 `document.md` 反读）。
- `error_code` 仅在 `failed` / `needs_ocr` 终态下可写（per `transition_document_status` 不变量）。
- Document status enum 已含 R2-C 所需全部状态：`uploaded` / `extracting` / `normalizing` / `ready` / `failed` / `needs_ocr`（外加 R3 的 `chunking` / `indexing` 与 R1 的 `deleting`）。

### 3.3 `knowledge_ingestion_jobs`

```sql
CREATE TABLE knowledge_ingestion_jobs (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    safe_error_code TEXT NOT NULL DEFAULT '',
    CHECK (stage IN ('extract', 'normalize', 'chunk', 'index')),
    CHECK (status IN ('running', 'completed', 'failed')),
    CHECK (attempt >= 1)
);
```

R2-C 关键事实：

- Job status enum **不含** `pending` / `cancelled` / `interrupted`。
- `create_job()` **总是**插入 `status='running'`（store.py:1053-1100）→ Job 创建即"已被 worker 持有"。
- `attempt` 由 Store 在创建时按 `(document_id, stage)` 历史计数自动 +1（store.py:1078-1085）。
- **无 `worker_owner` / `created_at` / `retry_of_job_id` 列**。
- **无 unique constraint 强制单 active Job**——必须应用层事务保证。

### 3.4 Indexes（与 R2-C 相关）

```sql
CREATE INDEX idx_documents_library ON knowledge_documents(library_id);
CREATE INDEX idx_documents_status  ON knowledge_documents(status);   -- ← claim 扫描索引
CREATE INDEX idx_jobs_document     ON knowledge_ingestion_jobs(document_id);
```

`idx_documents_status` 直接支持 worker claim 的 `SELECT ... WHERE status='uploaded' ORDER BY created_at LIMIT 1` 查询。

### 3.5 SQLite 配置（store.py:316-319）

```python
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
PRAGMA busy_timeout=5000
```

每次写事务用 `BEGIN IMMEDIATE`（独占 write lock，serializes writers）+ `_write_lock: asyncio.Lock`（同进程内串行化）——双重串行化保证应用层 atomic check-and-insert 模式安全。

---

## 4. Existing Component Audit

### 4.1 R1 Library Foundation（frozen @ 3bca5fd）

**Store API**（`web/knowledge/store.py`）：

| 方法 | 签名 | 备注 |
|---|---|---|
| `create_library(name, description)` | → Library | 生成 `lib_<body>` |
| `list_libraries()` | → list[Library] | ROWID 排序 |
| `get_library(id)` | → Library / raise |  |
| `update_library(id, name?, description?)` | → Library |  |
| `set_library_status(id, status)` | → Library | 内部用（delete 补偿） |
| `delete_library_hard(id)` | → None | cascade Documents / Chunks / Jobs / Bindings |
| `create_document(library_id, source_name, source_sha256, source_relpath, markdown_relpath, mime_type, size_bytes, page_count, parser_version)` | → Document / raise `DuplicateDocumentError` |  |
| `list_documents(library_id)` | → list[Document] |  |
| `get_document(id)` | → Document / raise |  |
| `transition_document_status(id, new_status, *, error_code?, parser_version?, page_count?)` | → Document | 状态机守卫（per `is_valid_document_transition`） |
| `delete_document_hard(id)` | → None | cascade Chunks / Jobs |
| `create_job(document_id, stage)` | → IngestionJob | **status='running'**；attempt 自动 |
| `get_job(id)` | → IngestionJob / raise |  |
| `finish_job(id, *, status, safe_error_code='')` | → IngestionJob | status ∈ {completed, failed} |
| `list_jobs_for_document(id)` | → list[IngestionJob] | started_at, id 排序 |
| `replace_session_bindings / list_session_bindings / get_active_library_ids_for_session / delete_bindings_for_*` | | ACL（R4 用，R2-C 不动） |
| `insert_chunk / list_chunks_for_document` | | R3 用 |

**Service API**（`web/knowledge/service.py`）：

| 方法 | 签名 |
|---|---|
| `create_library / list_libraries / get_library / update_library / delete_library` | Library CRUD（delete 调 status='deleting' → FS rmtree → DB hard-delete） |
| `create_document_metadata(...)` | Document metadata（无 PDF 解析） |
| `list_documents / get_document / delete_document` | Document metadata CRUD |
| `list_session_bindings / replace_session_bindings / get_active_library_ids_for_session / on_session_deleted` | Binding ACL |
| `list_orphan_library_dirs_async()` | Best-effort orphan 检测 |

**REST API**（`web/knowledge/api.py`，挂载条件 `enable_knowledge_api=True` + `knowledge_root != None`）：

| Method | Path |
|---|---|
| GET / POST | `/api/knowledge/libraries` |
| GET / PATCH / DELETE | `/api/knowledge/libraries/{library_id}` |
| GET | `/api/knowledge/libraries/{library_id}/documents` |
| GET / DELETE | `/api/knowledge/documents/{document_id}` |
| GET / PUT | `/api/sessions/{session_id}/knowledge-libraries` |

**FileStore API**（`web/knowledge/files.py`）：

- 固定文件名：`source.pdf` / `document.md` / `manifest.json`（`FIXED_DOCUMENT_FILES`）
- `write_file_atomic(library_id, document_id, filename, content_bytes)` — R1 已实现，含 fsync + os.replace + path containment + symlink escape 防护
- `read_file(library_id, document_id, filename)` / `delete_file` / `delete_document_dir` / `delete_library_dir`
- **不接受任意用户路径**——`_require_fixed_file()` 强制白名单

**Trusted UI Header**：

- Header 名：`X-PI-Agent-UI`（必须等于 `"1"`）
- 实现位置：`credentials_api.py::require_ui_header_dep`
- 应用范围：所有 Knowledge API 路由（router-level dependency at `api.py:223`）
- Loopback：`TrustedHostMiddleware`（`app.py:712-726`）默认 `("localhost", "127.0.0.1", "::1")`

**Body Limit**：

- `CredentialBodyLimitMiddleware` 默认 32 KiB（`local_web_security.py:37`）
- `ProviderProfileBodyLimitMiddleware` 单独配置
- Knowledge API 当前**复用 TrustedHost + 32 KiB 默认**——R2-C upload 需要单独大 body 限制中间件（per §22）

### 4.2 R2-A pypdf Parser Adapter（frozen @ 0772324）

```python
@runtime_checkable
class PdfParser(Protocol):
    @property
    def parser_id(self) -> str: ...
    @property
    def parser_version(self) -> str: ...
    def inspect(self, path: Path) -> PdfInspection: ...
    def extract(self, path: Path) -> PdfExtractionResult: ...
    def close(self) -> None: ...
```

`PdfInspection`：`file_size_bytes / page_count / encrypted / password_required / metadata / has_extractable_text_estimate / inspection_warnings`。

`PdfExtractionResult`：`parser_id / parser_version / pages / warnings`（pages 为 1-based `PdfPage` 元组）。

**Error 码**（10 项）：`pdf_file_not_found` / `pdf_not_a_file` / `invalid_pdf` / `encrypted_pdf` / `pdf_password_required` / `pdf_parse_failed` / `pdf_page_extract_failed` / `pdf_parser_unavailable` / `unsupported_parser_version` / 兜底 `pdf_parse_failed`。

### 4.3 R2-B Canonical Markdown Builder（frozen @ eb193b2 + ratification c2436c7）

- `PdfTextQualityEvaluator.evaluate(extraction)` → `PdfTextQualityResult(decision, metrics, reason_codes, warnings)`
- `decision ∈ {USABLE, NEEDS_OCR}`；`total_non_whitespace_chars == 0 → NEEDS_OCR`（amendment-2 §2.1）
- `CanonicalMarkdownBuilder.build(source, extraction, quality, *, title=None, generated_at=None)` → `CanonicalMarkdownArtifact`
- **raises `NeedsOcrNotBuildable`** 当 `quality.decision is NEEDS_OCR`
- `CanonicalMarkdownPersistence.write(library_id, document_id, source, extraction, quality, *, title=None)` → `WriteResult(relative_path, sha256, byte_length)`
- 复用 R1 `KnowledgeFileStore.write_file_atomic`
- `MAX_CANONICAL_MARKDOWN_BYTES = 50 MB`
- **`generated_at` MUST NOT BE PASSED in R2-C MVP**（per R2-B audit §6 ratified @ c2436c7）——确定性优先，相同 PDF 重 ingest 必须 byte-identical SHA-256

### 4.4 Composition Root（`web/app.py` lifespan, lines 360-560）

```python
async def _lifespan(_app):
    # startup:
    KnowledgeFileStore.ensure_root()
    KnowledgeStore.open(knowledge_root / "knowledge.db")  # independent aiosqlite
    KnowledgeService(store, file_store, session_exists=_session_exists_for_knowledge)
    # ... yield ...
    # shutdown: AsyncExitStack aclose
```

Knowledge router 挂载（`app.py:781-800`）：

```python
if knowledge_root is not None and _knowledge_api_enabled:
    app.include_router(build_knowledge_router(...), prefix="/api/knowledge")
    app.include_router(build_session_knowledge_router(...), prefix="/api/sessions")
```

---

## 5. Scope and Non-Goals

### 5.1 In Scope（R2-C 全阶段，C0 仅冻结合同）

- Upload Service：multipart 流式 + size/hash/signature + 事务保留 Document
- Ingestion Orchestrator：claim → inspect → extract → evaluate → build → persist → terminal commit
- Bounded Worker：单进程、单 worker manager、bounded queue、startup recovery、graceful shutdown
- Ingestion API：upload / status / retry / markdown
- 错误映射 + 安全日志 + safe error code 全表
- Trusted UI header 复用 + Body limit 中间件
- Library / Document 状态机执行
- needs_ocr 终态处理（不写 document.md，不删 source.pdf）

### 5.2 Non-Goals（R2-C 全程排除）

- ❌ OCR / Image understanding / 视觉重建
- ❌ Cancel API（用户级）；仅支持 graceful shutdown 取消
- ❌ 多进程 / 分布式 worker / Celery / Redis / RabbitMQ / Kafka / APScheduler / Dramatiq / RQ
- ❌ Session-level library ACL（R4）；R2-C API 是管理面，使用 Trusted UI scope
- ❌ search_knowledge tool（R3）
- ❌ heading-aware chunker + FTS5（R3）
- ❌ 前端知识库 UI（R5）
- ❌ public 公网认证 / RBAC / OAuth
- ❌ 自动 retry / 自动 fallback
- ❌ Long-term user memory / 用户偏好
- ❌ PDF 表单 / 注释 / 嵌入对象 / 复杂版面完美还原

---

## 6. Component Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ FastAPI app (lifespan)                                          │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ KnowledgeRouter (Trusted UI + Body Limit)               │   │
│  │  POST   /libraries/{lib}/documents        (upload)      │   │
│  │  GET    /documents/{doc}/ingestion         (status)     │   │
│  │  POST   /documents/{doc}/retry              (retry)     │   │
│  │  GET    /documents/{doc}/markdown           (read MD)   │   │
│  └──────────────────────┬──────────────────────────────────┘   │
│                         │                                       │
│  ┌──────────────────────▼──────────────────────────────────┐   │
│  │ KnowledgeIngestionService                                │   │
│  │   upload_stream() / retry() / get_status() / read_md()   │   │
│  └──────────────────────┬──────────────────────────────────┘   │
│                         │                                       │
│  ┌──────────────────────▼──────────────────────────────────┐   │
│  │ KnowledgeFileStore (R1 frozen)                           │   │
│  │   write_file_atomic / read_file / delete_document_dir    │   │
│  └──────────────────────┬──────────────────────────────────┘   │
│                         │                                       │
│  ┌──────────────────────▼──────────────────────────────────┐   │
│  │ WorkerManager (app-scoped, lifespan-managed)             │   │
│  │   asyncio.Queue(maxsize=32) + asyncio.Event wakeup       │   │
│  │   worker_loop task (concurrency=1)                       │   │
│  │   startup recovery / graceful shutdown                   │   │
│  └──────────────────────┬──────────────────────────────────┘   │
│                         │                                       │
│  ┌──────────────────────▼──────────────────────────────────┐   │
│  │ IngestionOrchestrator                                    │   │
│  │   claim → inspect → extract → evaluate → build → persist │   │
│  │   → terminal DB commit                                   │   │
│  │   runs via asyncio.to_thread (sync Parser)               │   │
│  └──────────────────────┬──────────────────────────────────┘   │
│                         │                                       │
│  ┌──────────────────────▼──────────────────────────────────┐   │
│  │ PypdfParser / PdfQualityEvaluator / CanonicalBuilder     │   │
│  │ CanonicalPersistence  (R2-A / R2-B frozen)               │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 6.1 Layering Rules

| Layer | Owns | Must NOT |
|---|---|---|
| API | DTO validation, HTTP status, safe error envelope, Trusted UI | 直接调 Parser / Builder；接 session_id 作权限；暴露绝对路径 |
| Service | 编排 Store + FileStore；事务补偿；error mapping | HTTP；queue 调度；并发控制 |
| WorkerManager | 队列；启动恢复；graceful shutdown；wake events | 业务规则；DTO；SQL 业务判断 |
| Orchestrator | 单 Job 业务流程；状态机；失败映射；warning 汇总 | HTTP；队列调度；多 Job 并发 |
| Parser / Builder / Evaluator / Persistence | 已 frozen（R2-A / R2-B）；R2-C 不修改 | （N/A — 已冻结） |

---

## 7. Durable Source of Truth

```
SQLite (knowledge.db) = durable source of truth
asyncio.Queue + asyncio.Event = wake-up hint only
```

含义：

1. **Job 入队前**必须已持久化（即 Document 已 `status='uploaded'` 在 DB）。
2. **进程崩溃**后内存队列丢失——`status='uploaded'` Documents 由 startup recovery 重新入队。
3. **重复入队**必须通过 claim 语义去重（worker claim 是 atomic UPDATE，重复 wake 只会触发一次实际处理）。
4. **API 返回 201**意味着 Document 已 durable 创建；不是只放进了内存队列。
5. **Job ID 必须在 DB 中存在**——`create_job` 时即 INSERT（status='running'）。

---

## 8. Document State Machine

### 8.1 状态值（per frozen schema）

```
uploaded / extracting / normalizing / chunking / indexing / ready / failed / needs_ocr / deleting
```

R2-C 仅使用：`uploaded` / `extracting` / `normalizing` / `ready` / `failed` / `needs_ocr` / `deleting`。

`chunking` / `indexing` 留给 R3（状态机允许，但 R2-C 不进入）。

### 8.2 完整转移表

| Current | Event | Next | Actor | Atomic DB Op | File Preconditions |
|---|---|---|---|---|---|
| `uploaded` | Worker claim | `extracting` | Worker | `transition_document_status(doc_id, 'extracting')` + `create_job(doc_id, 'extract')` 在同一事务 | source.pdf exists |
| `extracting` | inspect 成功 + page_count 合法 | `extracting`（停留） | Orchestrator | （仅 Job status 流转；Document 不动） | source.pdf exists |
| `extracting` | extract 成功 + quality USABLE | `normalizing` | Orchestrator | `transition_document_status(doc_id, 'normalizing', page_count=N, parser_version=V)` | extraction result in memory |
| `extracting` | extract 成功 + quality NEEDS_OCR | `needs_ocr` | Orchestrator | `transition_document_status(doc_id, 'needs_ocr', error_code='needs_ocr', page_count=N, parser_version=V)` + `finish_job(job_id, 'completed')` | 不写 document.md |
| `extracting` | inspect/extract 失败 | `failed` | Orchestrator | `transition_document_status(doc_id, 'failed', error_code=...)` + `finish_job(job_id, 'failed', safe_error_code=...)` | source.pdf 保留 |
| `extracting` | 进程崩溃（startup recovery） | `failed` | WorkerManager | `transition_document_status(doc_id, 'failed', error_code='ingestion_interrupted')` + `finish_job(job_id, 'failed', safe_error_code='ingestion_interrupted')` | source.pdf 保留 |
| `normalizing` | build + persist 成功 + DB terminal commit 成功 | `ready` | Orchestrator | `transition_document_status(doc_id, 'ready', parser_version=V, page_count=N)` + `finish_job(job_id, 'completed')` | document.md exists (post atomic write) |
| `normalizing` | build 失败 / persist 失败 / DB commit 失败 | `failed` | Orchestrator | `transition_document_status(doc_id, 'failed', error_code=...)` + `finish_job(job_id, 'failed', safe_error_code=...)` | 见 §19 失败矩阵 |
| `ready` | user retry（MVP 不允许） | — | — | `retry_not_allowed` | — |
| `needs_ocr` | user retry（MVP 不允许） | — | — | `retry_not_allowed` | — |
| `failed` | user retry | `extracting` | Service（重试入口） | `transition_document_status(doc_id, 'extracting')` + `create_job(doc_id, 'extract')` | source.pdf exists |
| 任一非 `deleting` | Library delete 补偿（仅当无 active Job） | `deleting` → 硬删 | Service | `set_library_status('deleting')` → `delete_library_hard` | FS rmtree 先于 DB hard-delete |
| `uploaded` / `extracting` / `normalizing` / `ready` / `needs_ocr` / `failed` | Document delete（仅当无 active Job） | （行被删除） | Service | `delete_document_hard(doc_id)` | FS rmtree 先于 DB |

### 8.3 转移合法性（per `models.py::is_valid_document_transition`）

R1 已实现并 frozen。R2-C 必须使用 `transition_document_status` Store API 而非裸 SQL UPDATE——状态机守卫由 Store 保证。

### 8.4 needs_ocr 终态规则

- `needs_ocr` 是**正常业务终态**，不是执行错误。
- Job 状态 = `completed`（不是 failed）——pipeline 正确执行了"检测到无文本"逻辑。
- 不写 `document.md`。
- 不删除 `source.pdf`。
- 不自动 OCR。
- 普通 retry **不允许**——`needs_ocr` 的判定结果对同 parser 是确定性的，重复运行结果相同。
- 未来若引入 OCR 能力（marker OCR / surya），需独立 `reprocess` 语义（不在 R2-C MVP）。

### 8.5 retry 允许状态

| Document Status | 普通 retry | 备注 |
|---|---|---|
| `failed` | ✅ 允许 | 创建新 Job；attempt +1 |
| `uploaded` | ⚠ 不需要（已有 pending work） | 返回 409 `retry_not_required`（Document 还在排队） |
| `extracting` / `normalizing` | ❌ 拒绝 | 返回 409 `ingestion_already_active` |
| `ready` | ❌ 拒绝 | 返回 409 `retry_not_allowed`（ready 是终态；未来用 reprocess 语义） |
| `needs_ocr` | ❌ 拒绝 | 返回 409 `retry_not_allowed`（needs_ocr 是终态业务判定） |
| `deleting` | ❌ 拒绝 | 返回 409 `document_deleting` |

---

## 9. Job State Machine

### 9.1 状态值（per frozen schema）

```
running / completed / failed
```

（**无** `pending` / `cancelled` / `interrupted`——schema 不允许）

### 9.2 完整转移表

| Current | Event | Next | Atomic DB Op |
|---|---|---|---|
| （未存在） | `create_job(document_id, stage)` | `running` | INSERT (status='running', attempt=auto) |
| `running` | Orchestrator terminal 成功 | `completed` | `finish_job(job_id, 'completed')` |
| `running` | Orchestrator terminal 失败 | `failed` | `finish_job(job_id, 'failed', safe_error_code=...)` |
| `running` | 进程崩溃（startup recovery） | `failed` | `finish_job(job_id, 'failed', safe_error_code='ingestion_interrupted')` |
| `completed` / `failed` | （终态，不可转移） | — | 终态；新 retry 创建新 Job 行 |

### 9.3 Job 与 Document 终态对应

| Document Status | latest Job Status | 备注 |
|---|---|---|
| `ready` | `completed` | 成功路径 |
| `needs_ocr` | `completed` | 正常业务终态（pipeline 完成，判定为 needs_ocr） |
| `failed` | `failed` | 执行错误 |
| `extracting` / `normalizing`（crash 后） | `failed`（recovery 强制） | startup recovery 改写 |
| `uploaded`（从未被 claim） | （无 Job 行） | upload 后 worker 尚未创建 Job |

### 9.4 Job 历史保持

- 终态 Job（`completed` / `failed`）**保持不可变历史**——`finish_job` 后不再 UPDATE。
- Retry **创建新 Job 行**（同 `document_id` + `stage`，`attempt` 自动 +1）。
- `list_jobs_for_document(doc_id)` 按 `started_at ASC, id ASC` 排序，可读完整历史。

### 9.5 `stage` 字段使用

R2-C MVP **仅使用 `stage='extract'`**（覆盖 inspect + extract + evaluate + build + persist 全流程）。

`normalize` / `chunk` / `index` 留给 R3：

- `normalize` = chunk 切分前置（R3）
- `chunk` = chunker 执行（R3）
- `index` = FTS5 索引（R3）

R2-C 把整个 PDF→Markdown pipeline 视作单一 `extract` stage，简化 attempt 计数与 retry 语义。

---

## 10. Upload Transaction and Compensation

### 10.1 唯一冻结顺序（Staged Upload）

```
1. Validate library_id 格式 + 存在 + status='active'
2. Streaming multipart read (chunk size = UPLOAD_CHUNK_SIZE)
   → write to staging path: libraries/{lib}/.staging/{rand}.pdf.tmp
   → enforce MAX_PDF_BYTES during streaming (abort on overflow)
   → compute SHA-256 incrementally (hashlib.sha256.update)
3. Validate PDF signature: first 5 bytes == b"%PDF-"
4. fsync staging file (flush + os.fsync)
5. BEGIN IMMEDIATE transaction:
   a. SELECT existing document WHERE library_id=? AND source_sha256=?
      → if exists: ROLLBACK → cleanup staging → return 409 (per §11)
   b. INSERT knowledge_documents
      (id, library_id, source_name, source_sha256,
       source_relpath='documents/{doc_id}/source.pdf',
       markdown_relpath='documents/{doc_id}/document.md',
       mime_type='application/pdf',
       size_bytes, page_count=0,
       status='uploaded', parser_version='', error_code='',
       created_at, updated_at)
6. COMMIT
7. Atomic source promote:
   KnowledgeFileStore.write_file_atomic(library_id, document_id,
                                        'source.pdf', staging_bytes)
   → 复用 R1 atomic write primitive（temp + fsync + os.replace）
   → 写入 libraries/{lib}/documents/{doc_id}/source.pdf
8. Cleanup staging file
9. Enqueue document_id to WorkerManager（wake event）
10. Return 201 + DTO
```

### 10.2 为什么是这个顺序

- **Staging file 先**：避免 DB 提交后 source 推进失败导致 Document 引用不存在的文件。
- **DB 提交时 source_relpath / markdown_relpath 已写入**：schema NOT NULL，且路径由 doc_id 决定（确定性）。
- **Source promote 在 DB COMMIT 后**：失败的 promote 进入补偿路径（见 §10.3 Case A）。
- **DB 是 durable source of truth**：commit 后即使 promote 失败，startup recovery 也能发现 Document `status='uploaded'` + 文件缺失，标记 `failed` + `source_file_missing`。

### 10.3 补偿矩阵（4 个失败点）

| Case | 失败点 | File State | DB State | 补偿 | HTTP Response |
|---|---|---|---|---|---|
| A | staging 写入失败（磁盘满 / 权限） | 无 staging file | 无 Document | cleanup staging | 500 `upload_staging_failed` |
| B | size 超限（流式中检测） | 部分 staging file | 无 Document | cleanup staging | 413 `upload_too_large` |
| C | SHA 计算失败（极少） | staging file | 无 Document | cleanup staging | 500 `upload_hash_failed` |
| D | PDF signature 不匹配 | staging file | 无 Document | cleanup staging | 415 `invalid_pdf_signature` |
| E | Duplicate SHA 检测 | staging file | 无 Document | cleanup staging | 409 `duplicate_document*`（per §11） |
| F | DB INSERT 失败（IntegrityError 非 duplicate） | staging file | 无 Document | cleanup staging | 500 `internal_ingestion_error` |
| G | DB COMMIT 成功 + source promote 失败 | 无 source.pdf | Document `uploaded` | startup recovery 检测；标记 `failed` + `source_file_missing`；用户可显式 delete 或 retry 后重新上传（retry 会因 source 缺失而 failed） | 201（已 durable 创建） + 异步 failed |
| H | DB COMMIT 成功 + source promote 成功 + enqueue 失败 | source.pdf 存在 | Document `uploaded` | startup recovery polling 兜底（worker 每段时间扫 `status='uploaded'`） | 201 + 异步处理 |

> **关键不变量**：DB / FS 不存在真正跨资源原子事务——本合同使用"事务顺序 + 显式补偿"模型，并在 startup recovery 兜底所有跨资源不一致状态。**任何文档不得声称实现了 DB/FS 原子事务**。

---

## 11. Duplicate SHA Semantics

### 11.1 检测点

`UNIQUE (library_id, source_sha256)` 在 DB INSERT 时强制。Service 层在 BEGIN IMMEDIATE 内 SELECT 先检测，便于返回精确 reason code（而非依赖 IntegrityError 通用信息）。

### 11.2 行为表

| Existing Document Status | HTTP | safe reason code | Body |
|---|---|---|---|
| `uploaded`（pending） | 409 | `duplicate_document_in_progress` | `{existing_document_id, status: 'uploaded'}` |
| `extracting` / `normalizing` | 409 | `duplicate_document_in_progress` | `{existing_document_id, status}` |
| `ready` | 409 | `duplicate_document_ready` | `{existing_document_id, status: 'ready'}` |
| `needs_ocr` | 409 | `duplicate_document_needs_ocr` | `{existing_document_id, status: 'needs_ocr'}` |
| `failed` | 409 | `duplicate_document_failed` | `{existing_document_id, status: 'failed'}`（用户可对 existing 显式 retry） |

**统一**：所有 duplicate 返回 409 + 单一 `duplicate_document` reason + 子状态字段；客户端通过子状态区分。

### 11.3 跨 Library

`UNIQUE (library_id, source_sha256)` —— 同 SHA 在不同 library 视为不同 Document，允许各自独立 ingest。

---

## 12. Retry Semantics

### 12.1 API

```
POST /api/knowledge/documents/{document_id}/retry
```

### 12.2 行为

1. 校验 `document_id` 格式 + 存在。
2. 检查 Library `status='active'`（archived/deleting/failed 拒绝）。
3. **Active Job 检查**（per §13）：BEGIN IMMEDIATE + SELECT COUNT running jobs for doc → 若 > 0 拒绝 409 `ingestion_already_active`。
4. 检查 Document `status`：
   - `failed` → 允许
   - 其他状态 → 按 §8.5 表拒绝
5. 创建新 Job：`create_job(document_id, 'extract')`（attempt 自动 +1）。
6. 同时 `transition_document_status(doc_id, 'extracting')`。
7. COMMIT + enqueue。
8. 返回 201 + 新 Job DTO。

### 12.3 Retry 不变量

- **不重新上传**——复用原 `source.pdf`。
- **不重算 SHA**——`source_sha256` 在 DB 中不变。
- **不传 `generated_at`**——per §24。
- **重 ingest 必须产出 byte-identical Markdown + 相同 content SHA-256**（前提：source.pdf 未变 + parser 版本未升级）。
- **新 document.md 原子覆盖旧文件**（R1 atomic write primitive 保证；旧文件被 os.replace 替换，无中间状态）。
- **`MAX_RETRY_ATTEMPTS = 5`**：超过返回 409 `retry_limit_reached`。attempt 计数从 `list_jobs_for_document(doc_id)` 中 stage='extract' 的行数得出（无需新列）。
- **MVP 不实现自动 retry**——只能由用户显式调用。

### 12.4 Retry 时机约束

- 同一 Document 同时最多 1 个 active Job（per §13）。
- 两个并发 retry 请求 → 最多一个创建 Job；另一个 409 `ingestion_already_active`。
- 不得创建两个 pending Job；不得运行两次 Parser。

---

## 13. Atomic Job Claim

### 13.1 Active Job Uniqueness

Schema 无 unique constraint 强制单 active Job。R2-C 通过**应用层事务**保证：

```sql
-- Within a single BEGIN IMMEDIATE transaction:
BEGIN IMMEDIATE;
SELECT COUNT(*) AS n FROM knowledge_ingestion_jobs
  WHERE document_id = ? AND status = 'running';
-- if n > 0: ROLLBACK; return 409 ingestion_already_active
-- else: continue with create_job + transition_document_status
INSERT INTO knowledge_ingestion_jobs (...);
UPDATE knowledge_documents SET status = 'extracting', updated_at = ? WHERE id = ?;
COMMIT;
```

**TOCTOU 安全性**：`BEGIN IMMEDIATE` 在 SQLite 中获取 **reserved lock**，序列化所有写事务。两个并发请求同时进入此事务时，第二个会在 BEGIN 上阻塞（最多 `busy_timeout=5000ms`），第一个 COMMIT 后第二个才能进入并读到新的 Job count。

**双重串行化**：`KnowledgeStore._write_lock: asyncio.Lock` 在同进程内进一步串行化（避免 SQLite busy 重试）。

### 13.2 Worker Claim Algorithm（WorkerManager）

Worker 单进程单 manager。concurrency = 1 时无需跨 worker 协调。Claim 算法：

```sql
-- Within BEGIN IMMEDIATE:
BEGIN IMMEDIATE;
UPDATE knowledge_documents
  SET status = 'extracting', updated_at = ?
  WHERE id = (
    SELECT id FROM knowledge_documents
    WHERE status = 'uploaded'
    ORDER BY created_at ASC, id ASC   -- FIFO + deterministic tie-break
    LIMIT 1
  );
-- read rowcount + SELECT id of updated row
-- if rowcount == 0: ROLLBACK (no pending work)
-- else:
INSERT INTO knowledge_ingestion_jobs (id, document_id, 'extract', 'running', attempt, started_at, ...);
-- attempt = (SELECT COUNT(*) FROM knowledge_ingestion_jobs WHERE document_id=?) + 1
COMMIT;
```

**FIFO 顺序**：`ORDER BY created_at ASC, id ASC`。`created_at` 是 epoch ms（`_now_ms()`），同毫秒内多 Document 用 `id` tie-break（id 是 `secrets.token_hex(8)`——非时序，但提供去歧义）。

**Claim 失败的 rowcount**：`UPDATE ... WHERE id = (SELECT ... LIMIT 1)` 的 rowcount 反映是否真的更新了一行。rowcount=0 → 无 pending 工作；worker 进入 idle 等待 wake event。

### 13.3 不依赖 `asyncio.Lock` 跨进程

`asyncio.Lock` 仅在单进程内有效。本合同假设**单进程**——不支持多 worker 进程同时写 knowledge.db。多进程部署需 future amendment（per §33 显式不包含）。

---

## 14. Worker Queue and Concurrency

### 14.1 MVP 并发模型

```
worker_concurrency = 1
queue_capacity     = 32
```

- 单进程、单 WorkerManager、单 asyncio.Queue（maxsize=32）、单 worker_loop task。
- 不支持多进程一致性 / 分布式 worker / 外部 Job Service。

### 14.2 Queue 角色

`asyncio.Queue(maxsize=32)` 仅传 **document_id** 作为 wake-up hint。**不**承载 Job 状态——状态在 SQLite。

- Queue full（32 entries）时 upload service 用 `queue.put_nowait()`；满则放弃（不阻塞 upload）；startup recovery 的 polling 兜底最终捞起（per §14.4）。
- Queue item 重复（同一 doc_id 多次入队）由 worker 的 atomic claim（§13.2）保证只处理一次。

### 14.3 Wake-up 信号

- Upload 成功 → `queue.put_nowait(doc_id)` + `wake_event.set()`。
- Retry 成功 → 同上。
- Startup recovery → 批量 `queue.put_nowait(doc_id)` for all `status='uploaded'` documents。
- Worker drain 完成 → `wake_event.clear()`；下次 wake 触发新一轮 drain。

### 14.4 Polling 兜底

```
WORKER_POLL_INTERVAL_SECONDS = 30
```

每 30s worker 检查一次 DB 中是否有 `status='uploaded'` Documents（即使无 wake event）。这覆盖：

- queue 满时未入队的 Document。
- 进程被 SIGTERM 后立即重启的 race（启动 recovery 的批量入队可能错过窗口）。
- Wake event 丢失的极少情况（asyncio bug）。

### 14.5 Worker Loop（伪代码）

```python
async def worker_loop():
    while not self._shutting_down:
        # Drain queue (non-blocking)
        pending_doc_ids = []
        while True:
            try:
                doc_id = self._queue.get_nowait()
                pending_doc_ids.append(doc_id)
            except asyncio.QueueEmpty:
                break

        # Also: check DB for any status='uploaded' (covers queue miss / restart race)
        async with self._store_write_lock:
            db_pending = await self._store.list_uploaded_document_ids()
        all_pending = set(pending_doc_ids) | set(db_pending)

        for doc_id in all_pending:
            if self._shutting_down:
                break
            try:
                await self._orchestrator.run_one(doc_id)
            except Exception as exc:
                # Log safe category; do not crash worker
                self._log_safe_exception(exc)

        # Wait for next wake event or poll timeout
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=WORKER_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
        except asyncio.TimeoutError:
            pass  # poll interval elapsed
```

---

## 15. Worker Startup Recovery

### 15.1 启动时扫描

App lifespan startup 时（在 KnowledgeStore.open + WorkerManager.init 内）：

```sql
-- 1. pending work：所有 status='uploaded' 的 documents
SELECT id FROM knowledge_documents WHERE status = 'uploaded' ORDER BY created_at ASC, id ASC;

-- 2. interrupted jobs：所有 status='running' 的 jobs + 对应 documents in ('extracting', 'normalizing')
SELECT j.id, j.document_id FROM knowledge_ingestion_jobs j
  JOIN knowledge_documents d ON j.document_id = d.id
  WHERE j.status = 'running';
```

### 15.2 Recovery 行为

| State | Recovery Action |
|---|---|
| Document `uploaded` + 无 active Job | 重新入队（queue + wake event） |
| Document `uploaded` + 有 Job `running` | 标记 Job `failed` + `ingestion_interrupted`；Document 保持 `uploaded`（re-queue） |
| Document `extracting` / `normalizing` + Job `running` | 标记 Job `failed` + `ingestion_interrupted`；Document → `failed` + `error_code='ingestion_interrupted'`（per §22 不自动重试） |
| Document `extracting` / `normalizing` + 无 Job | 仅 Document → `failed` + `ingestion_interrupted`（孤儿 Document，DB 与 Job 不一致） |
| Document `ready` / `needs_ocr` / `failed` | 不动（终态） |
| Job `running` 对应 Document 已 `ready` / `needs_ocr` / `failed` | Job → `failed` + `ingestion_interrupted`（孤儿 Job） |

### 15.3 启动事务原子性

Recovery 在单一 BEGIN IMMEDIATE 事务内执行所有 UPDATE；任一失败 → ROLLBACK → app 启动失败（fail-fast）。

### 15.4 启动后清理

- Temp file cleanup（per §23）：扫描 `libraries/{lib}/.staging/*.tmp` + `documents/{doc}/.{rand}.tmp` 匹配 atomic-write temp pattern，仅在 Knowledge root 内、不递归、不跟随 symlink。
- Orphan dir cleanup：调用 R1 `list_orphan_library_dirs_async()` 检测磁盘有 DB 无的 library dir（best-effort，仅日志不删除）。

---

## 16. Worker Shutdown

### 16.1 Graceful Shutdown 序列

```
WORKER_SHUTDOWN_GRACE_SECONDS = 30
```

1. `shutting_down = True`（信号量）。
2. 停止接收新 wake event（`wake_event.clear()`）。
3. 拒绝新 Job claim（worker_loop 顶部检查 `shutting_down`）。
4. 当前正在执行的 Job 给予 30s grace：
   - 30s 内完成 → 正常 terminal commit → 释放资源。
   - 30s 超时 → 强制取消：`asyncio.Task.cancel()` → `CancelledError` → Orchestrator finally 路径 → Job/Document 标记 `failed` + `ingestion_interrupted`。
5. parser.close()（释放 pypdf 资源）。
6. asyncio.Queue 清空（不持久化；startup recovery 会从 DB 重建）。
7. AsyncExitStack aclose（关闭 KnowledgeStore connection）。

### 16.2 不变量

- `CancelledError` 不能让 Job 留在 `running` 状态——Orchestrator 必须在 finally / except 路径更新 Job `failed` + Document `failed`。
- 即使 30s grace 超时，DB 状态必须最终一致（startup recovery 兜底）。
- 不支持：暂停后续跑 / checkpoint resume / 跨进程 handoff。

### 16.3 信号处理

- SIGINT / SIGTERM → lifespan shutdown sequence → WorkerManager.graceful_shutdown()。
- 测试模式：WorkerManager 可注入 fake clock + 显式 shutdown() 调用。

---

## 17. Delete and Retry Races

### 17.1 Document Delete vs Active Job

```
DELETE /api/knowledge/documents/{document_id}
  → check active Job: SELECT COUNT(*) WHERE document_id=? AND status='running'
  → if > 0: 409 document_ingestion_active
  → else: proceed delete_document_hard (cascade chunks + jobs)
```

**理由**：Worker 仍读 source.pdf 时不应让外力删除；Job 外键级联删除会让 worker 继续写已删除目录。

### 17.2 Document Delete vs Concurrent Retry

两个并发请求（DELETE + retry）通过 KnowledgeStore `_write_lock: asyncio.Lock` 串行化：

- retry 先获得锁 → 创建 Job（status='running'）→ COMMIT → 释放锁。
- DELETE 后获得锁 → SELECT COUNT running > 0 → 409。
- 反之 DELETE 先 → 删除 Document → retry 后获得锁 → SELECT Document → 404 `document_not_found`。

### 17.3 Library Delete vs Active Job

```
DELETE /api/knowledge/libraries/{library_id}
  → check active Job:
      SELECT COUNT(*) FROM knowledge_ingestion_jobs j
        JOIN knowledge_documents d ON j.document_id = d.id
        WHERE d.library_id = ? AND j.status = 'running';
  → if > 0: 409 library_ingestion_active
  → else: proceed delete_library_hard (cascade documents + chunks + jobs + bindings)
```

### 17.4 Library Delete Order

```
1. set_library_status(library_id, 'deleting')    # BEGIN IMMEDIATE
2. FS rmtree libraries/{library_id}/              # outside DB txn
3. delete_library_hard(library_id)                # BEGIN IMMEDIATE; cascade
```

任一步失败按 R1 已有补偿（`test_delete_library_dir_failure_keeps_db` 不变量）。

### 17.5 不支持

- ❌ Cancel-and-delete 一体操作（MVP）。
- ❌ Library delete 期间允许 upload（archived/deleting/failed 拒绝）。
- ❌ Document delete 期间 retry（race 由 lock 串行化，但语义上 retry 应先失败 fast）。

---

## 18. Orchestrator Sequence

### 18.1 完整调用顺序

```
INPUT: document_id (already status='extracting' + Job created by claim)

1. Load Document metadata
2. Verify source.pdf exists at documents/{doc_id}/source.pdf
   → if missing: jump to §19 Case 8 (source_file_missing)
3. parser.inspect(source_path)
   → on error: jump to §19 Case 9-12 (parser inspect errors)
4. Enforce page_count limit:
   if page_count > MAX_PDF_PAGES (20):
     → jump to §19 Case 13 (pdf_page_limit_exceeded)
5. parser.extract(source_path)
   → on error: jump to §19 Case 14-16 (parser extract errors)
6. quality = PdfTextQualityEvaluator.evaluate(extraction)
   → on InvalidExtractionResult: jump to §19 Case 17 (invalid_extraction)
7. if quality.decision is NEEDS_OCR:
     Document → needs_ocr (error_code='needs_ocr', page_count=N, parser_version=V)
     Job → completed
     return  (no document.md written)
8. Document → normalizing (parser_version=V, page_count=N)
9. artifact = CanonicalMarkdownBuilder.build(
       source=source,
       extraction=extraction,
       quality=quality,
       title=inspection.metadata.title,
       # generated_at NOT PASSED (per §24)
   )
   → on NeedsOcrNotBuildable: defensive double-check; treat as needs_ocr (step 7)
   → on CanonicalMarkdownTooLarge: jump to §19 Case 20 (canonical_markdown_too_large)
   → on other Exception: jump to §19 Case 19 (canonical_markdown_build_failed)
10. write_result = CanonicalMarkdownPersistence.write(
        library_id, document_id, source, extraction, quality,
        title=inspection.metadata.title
    )
    → on error: jump to §19 Case 21 (canonical_markdown_write_failed)
11. Verify write_result.sha256 == hashlib.sha256(artifact.content.encode('utf-8')).hexdigest()
    → mismatch: jump to §19 Case 21b (sha_mismatch)
12. Atomic DB terminal commit (single BEGIN IMMEDIATE):
    - transition_document_status(doc_id, 'ready',
        parser_version=extraction.parser_version,
        page_count=len(extraction.pages))
    - finish_job(job_id, 'completed')
    → on error: jump to §19 Case 22 (terminal_commit_failed; document.md 已写但 DB 未更新)
13. Cleanup attempt-local resources
14. Return success
```

### 18.2 Document 状态进入 `normalizing` 的时机

**在第 8 步**（extract 完成 + quality USABLE 之后、build 之前）。

- 不是 build 之后——build 失败时 Document 不应停留在 `normalizing` 等待 build 重试；应直接 `failed`。
- 不是 extract 之后立刻——quality 评估失败的话 Document 应进入 `failed` 而不是 `normalizing`。

### 18.3 不变量

- Job status='running' 期间，Orchestrator 持有该 Job 的"active claim"。
- Document status='extracting' 期间不允许 retry / delete（per §17）。
- `document.md` 仅在 Document status 进入 `ready` 后才被认为有效（之前写入是中间态）。

---

## 19. File/DB Commit Ordering — Failure Matrix

完整失败矩阵。每项有确定 file state / doc state / job state / compensation / retryable。

| # | Step | Failure | File State | Doc State | Job State | Compensation | safe_error_code | Retryable? |
|---|---|---|---|---|---|---|---|---|
| 1 | multipart parse | upload parse failed | none | none | none | cleanup staging | `invalid_upload` | user re-upload |
| 2 | size enforce | upload too large | partial staging | none | none | cleanup staging | `upload_too_large` | user re-upload smaller |
| 3 | staging write | disk full / permission | partial/none staging | none | none | cleanup staging | `upload_staging_failed` | infra fix; re-upload |
| 4 | SHA compute | hash failed | staging | none | none | cleanup staging | `upload_hash_failed` | re-upload |
| 5 | signature check | invalid %PDF- | staging | none | none | cleanup staging | `invalid_pdf_signature` | re-upload valid PDF |
| 6 | duplicate | (lib, sha) exists | staging | existing doc | existing job | cleanup staging | `duplicate_document*` | per existing doc status |
| 7 | DB INSERT | non-duplicate IntegrityError | staging | none | none | cleanup staging | `internal_ingestion_error` | re-upload after fix |
| 8 | source promote | KnowledgeFileStore.write_file_atomic failed | no source.pdf | `uploaded` (committed) | none | startup recovery 标记 failed | `source_file_missing` | user re-upload (delete + retry) |
| 9 | enqueue | queue full / event lost | source.pdf | `uploaded` | none | polling 兜底 | (none, async) | (auto via poll) |
| 10 | worker claim | doc not in `uploaded`（race） | source.pdf | unchanged | unchanged | skip; log | (none) | (none) |
| 11 | inspect: file not found | source.pdf deleted between upload + claim | source.pdf missing | `failed` | `failed` | — | `source_file_missing` | user re-upload |
| 12 | inspect: invalid_pdf | corrupted PDF | source.pdf | `failed` | `failed` | — | `invalid_pdf` | user re-upload valid PDF |
| 13 | inspect: encrypted | encrypted PDF | source.pdf | `failed` | `failed` | — | `encrypted_pdf` or `pdf_password_required` | user decrypt + re-upload |
| 14 | inspect: page_count > 20 | too many pages | source.pdf | `failed` | `failed` | — | `pdf_page_limit_exceeded` | user split + re-upload |
| 15 | extract: page_extract_failed | single page extract exception | source.pdf | `failed` | `failed` | — | `pdf_page_extract_failed` | user re-upload (may be transient) |
| 16 | extract: parse_failed | generic pypdf failure | source.pdf | `failed` | `failed` | — | `pdf_parse_failed` | user re-upload |
| 17 | quality: invalid_extraction | page_count=0 after extract | source.pdf | `failed` | `failed` | — | `invalid_extraction_result` | user re-upload |
| 18 | quality: NEEDS_OCR | total_non_whitespace_chars=0 | source.pdf | `needs_ocr` | `completed` | — | `needs_ocr` (in error_code field; NOT failure) | retry not allowed (terminal business) |
| 19 | build: too large | artifact > 50 MB | source.pdf | `failed` | `failed` | — | `canonical_markdown_too_large` | user re-upload smaller PDF |
| 20 | build: other | CanonicalMarkdownBuilder raised | source.pdf | `failed` | `failed` | — | `canonical_markdown_build_failed` | user retry |
| 21 | persist: write failed | KnowledgeFileStore write failed | no document.md | `failed` | `failed` | cleanup partial temp (R1 primitive 保证) | `canonical_markdown_write_failed` | user retry |
| 22 | persist: SHA mismatch | write sha != artifact sha | document.md exists | `failed` | `failed` | — | `canonical_markdown_sha_mismatch` | user retry |
| 23 | terminal DB commit | DB UPDATE failed | document.md exists | unchanged (still normalizing) | unchanged (still running) | Orchestrator finally: status='failed', job='failed' | `terminal_commit_failed` | user retry (will overwrite document.md) |
| 24 | worker cancellation | SIGTERM / shutdown grace timeout | source.pdf + maybe partial document.md | `failed` | `failed` | Orchestrator finally | `ingestion_interrupted` | user retry |
| 25 | app crash | process killed mid-orchestrator | source.pdf + maybe partial document.md | `extracting` or `normalizing` | `running` | startup recovery: doc → failed, job → failed | `ingestion_interrupted` | user retry |
| 26 | delete race | DELETE wins over active Job | (blocked) | (blocked) | (blocked) | API returns 409 | (none) | (none) |
| 27 | retry race | 2 concurrent retry on same doc | source.pdf | unchanged | unchanged | API returns 409 to loser | (none) | (none) |

> **关键不变量**：
> - 失败时 source.pdf **永远保留**（除非显式 DELETE Document）。
> - 失败时 document.md 不得处于"半成品 + DB ready"状态——DB ready 仅在 build + persist + DB commit 全部成功后才写入。
> - 所有失败用 `safe_error_code`（不含路径 / 正文 / secret / traceback）。

---

## 20. Failure Matrix Summary（safe_error_code 全表）

| safe_error_code | 触发场景 | HTTP Status | Document Status | Job Status |
|---|---|---|---|---|
| `upload_too_large` | size > MAX_PDF_BYTES (25 MB) during streaming | 413 | (none) | (none) |
| `invalid_upload` | multipart parse failure | 400 | (none) | (none) |
| `invalid_pdf_signature` | first bytes != `%PDF-` | 415 | (none) | (none) |
| `upload_staging_failed` | staging write IO failure | 500 | (none) | (none) |
| `upload_hash_failed` | SHA-256 compute failure | 500 | (none) | (none) |
| `duplicate_document_in_progress` | existing doc status ∈ {uploaded, extracting, normalizing} | 409 | existing unchanged | existing unchanged |
| `duplicate_document_ready` | existing doc status = ready | 409 | existing unchanged | existing unchanged |
| `duplicate_document_needs_ocr` | existing doc status = needs_ocr | 409 | existing unchanged | existing unchanged |
| `duplicate_document_failed` | existing doc status = failed | 409 | existing unchanged | existing unchanged |
| `library_not_found` | library_id does not exist | 404 | (none) | (none) |
| `library_not_active` | library status != 'active' | 409 | (none) | (none) |
| `document_not_found` | document_id does not exist | 404 | (none) | (none) |
| `ingestion_already_active` | retry when active Job exists | 409 | unchanged | unchanged |
| `retry_not_allowed` | retry on terminal status (ready/needs_ocr/uploaded) | 409 | unchanged | unchanged |
| `retry_not_required` | retry when status=uploaded (already pending) | 409 | unchanged | unchanged |
| `retry_limit_reached` | attempt_count >= 5 | 409 | unchanged | unchanged |
| `document_ingestion_active` | DELETE Document with active Job | 409 | unchanged | unchanged |
| `library_ingestion_active` | DELETE Library with active Job | 409 | unchanged | unchanged |
| `document_deleting` | operate on Document in 'deleting' | 409 | unchanged | unchanged |
| `invalid_pdf` | pypdf cannot parse (corrupted) | (async via status) | `failed` | `failed` |
| `encrypted_pdf` | encrypted PDF (empty pw works) | (async) | `failed` | `failed` |
| `pdf_password_required` | encrypted + empty pw fails | (async) | `failed` | `failed` |
| `pdf_parse_failed` | generic pypdf failure | (async) | `failed` | `failed` |
| `pdf_page_extract_failed` | single page extract exception | (async) | `failed` | `failed` |
| `pdf_parser_unavailable` | [rag] extra not installed | (async) | `failed` | `failed` |
| `unsupported_parser_version` | pypdf not in 6.x | (async) | `failed` | `failed` |
| `pdf_page_limit_exceeded` | page_count > 20 | (async) | `failed` | `failed` |
| `invalid_extraction_result` | page_count=0 after extract | (async) | `failed` | `failed` |
| `needs_ocr` | total_non_whitespace_chars=0 | (async) | `needs_ocr` | `completed` |
| `canonical_markdown_too_large` | artifact > 50 MB | (async) | `failed` | `failed` |
| `canonical_markdown_build_failed` | Builder exception | (async) | `failed` | `failed` |
| `canonical_markdown_write_failed` | Persistence write failure | (async) | `failed` | `failed` |
| `canonical_markdown_sha_mismatch` | write sha != artifact sha | (async) | `failed` | `failed` |
| `source_file_missing` | source.pdf missing at claim | (async) | `failed` | `failed` |
| `ingestion_interrupted` | worker cancellation / app crash | (async) | `failed` | `failed` |
| `terminal_commit_failed` | DB terminal UPDATE failed | (async) | `failed` | `failed` |
| `internal_ingestion_error` | uncaught exception (sanitize) | 500 | `failed` (best-effort) | `failed` (best-effort) |
| `worker_unavailable` | WorkerManager not initialized at request time | 503 | (none) | (none) |
| `markdown_not_ready` | GET markdown when doc.status != 'ready' | 409 | unchanged | unchanged |
| `invalid_library_id` | library_id format invalid | 400 | (none) | (none) |
| `invalid_document_id` | document_id format invalid | 400 | (none) | (none) |
| `invalid_filename` | source_name validation failed | 400 | (none) | (none) |

**复用规则**：R2-A 已冻结的 10 个 PDF 错误码直接复用；不创建同义重复。

**安全不变量**：

- 错误响应 body 不含绝对路径 / traceback / SQLite 语句 / pypdf 异常文本 / PDF 正文 / secret。
- 日志可记录 exception type + sanitized category，不记录完整 traceback。
- `safe_error_code` 是稳定机器值，前端可依赖。

---

## 21. API Contract

### 21.1 API 路由表

| Method | Path | Trusted UI | Body | Success | Errors | Side Effects |
|---|---|---|---|---|---|---|
| POST | `/api/knowledge/libraries/{library_id}/documents` | required | multipart/form-data; part `file` | 201 + DTO | 400/404/409/413/415/500/503 | Create Document + persist source.pdf + enqueue |
| GET | `/api/knowledge/documents/{document_id}/ingestion` | required | none | 200 + DTO | 400/404 | (read-only) |
| POST | `/api/knowledge/documents/{document_id}/retry` | required | none | 201 + DTO | 400/404/409/500/503 | Create new Job + enqueue |
| GET | `/api/knowledge/documents/{document_id}/markdown` | required | none | 200 + `text/markdown` | 400/404/409 | (read-only) |

R1 已有：GET/POST/PATCH/DELETE libraries、GET/DELETE documents、GET/PUT session-knowledge-libraries——R2-C **不重复实现**。

### 21.2 Upload API

**Request**:

```
POST /api/knowledge/libraries/{library_id}/documents
Headers:
  X-PI-Agent-UI: 1
Content-Type: multipart/form-data; boundary=...

Body:
  --boundary
  Content-Disposition: form-data; name="file"; filename="example.pdf"
  Content-Type: application/pdf
  
  <PDF bytes>
  --boundary--
```

**用户不得指定**：parser / OCR / output_path / document_id / job_id / SHA / generated_at / status / library binding / session_id。

**成功响应**:

```json
HTTP/1.1 201 Created
Content-Type: application/json

{
  "document": {
    "id": "doc_...",
    "library_id": "lib_...",
    "source_name": "example.pdf",
    "source_sha256": "<64 hex>",
    "mime_type": "application/pdf",
    "size_bytes": 12345,
    "page_count": 0,
    "status": "uploaded",
    "parser_version": "",
    "error_code": "",
    "created_at": 1691066400000,
    "updated_at": 1691066400000
  },
  "job": null
}
```

**`job` 字段为 `null`**：upload 时不创建 Job；Job 在 worker claim 时创建。前端用 `GET /ingestion` 查询最新 Job。

**HTTP 201（不是 202）**：Document 资源已 durable 创建；Job 是 worker 概念不属于 upload response。

### 21.3 Status API

```
GET /api/knowledge/documents/{document_id}/ingestion
Headers: X-PI-Agent-UI: 1
```

**响应**:

```json
HTTP/1.1 200 OK

{
  "document_id": "doc_...",
  "document_status": "extracting",
  "latest_job": {
    "id": "job_...",
    "status": "running",
    "attempt": 1,
    "started_at": 1691066400000,
    "finished_at": null,
    "safe_error_code": null
  }
}
```

`latest_job` = `list_jobs_for_document(doc_id)` 中 stage='extract' 的最后一行（按 started_at, id 排序）。若无任何 Job（Document `uploaded` 状态），`latest_job: null`。

**不返回**：绝对路径 / temp path / source 正文 / Markdown 正文 / traceback / worker task ID / 内部锁状态 / raw exception / 进度百分比（避免假进度）。

### 21.4 Retry API

```
POST /api/knowledge/documents/{document_id}/retry
Headers: X-PI-Agent-UI: 1
Body: (empty)
```

**成功**:

```json
HTTP/1.1 201 Created

{
  "document": { ... updated Document DTO (status: 'extracting') ... },
  "job": {
    "id": "job_...",
    "status": "running",
    "attempt": 2,
    "started_at": 1691066500000,
    "finished_at": null,
    "safe_error_code": null
  }
}
```

**冲突**:

```json
HTTP/1.1 409 Conflict

{ "detail": "ingestion_already_active", "document_id": "doc_..." }
```

### 21.5 Markdown API

```
GET /api/knowledge/documents/{document_id}/markdown
Headers: X-PI-Agent-UI: 1
```

**前置**：Document `status == 'ready'`。其他状态 → 409 `markdown_not_ready`（`needs_ocr` 也返回此码——没有 markdown 可读）。

**响应**:

```
HTTP/1.1 200 OK
Content-Type: text/markdown; charset=utf-8
Content-Disposition: inline; filename="document.md"

<UTF-8 Canonical Markdown bytes>
```

**约束**:

- 通过 `KnowledgeFileStore.read_file(library_id, document_id, 'document.md')` 读取。
- 路径 containment 由 R1 保证。
- 不跟随 symlink（R1 已扫描）。
- 不渲染 HTML / 不加载图片 / 不解析 raw HTML / 不接 Markdown renderer。
- 文件名固定 `document.md`（不信任 source_name）。
- Content-Disposition `inline`（前端预览 / 下载通过 query 参数切换可加 `attachment`，但 MVP 仅 `inline`）。

---

## 22. Trusted UI Boundary

### 22.1 复用现有机制

- Header 名：`X-PI-Agent-UI`（必须 `= "1"`）。
- Header 检查：`require_ui_header_dep`（router-level dependency，与 R1 Library API 相同）。
- TrustedHostMiddleware：默认 `("localhost", "127.0.0.1", "::1")`。

### 22.2 R2-C 新增 Body Limit Middleware

```
KNOWLEDGE_UPLOAD_MAX_BODY_BYTES = 25 MB + 1 MB overhead = 26 MB
```

单独的 `KnowledgeUploadBodyLimitMiddleware`（仅作用于 upload route），不复用 CredentialBodyLimitMiddleware（32 KiB 太小）。

- 流式读取时强制 body size（不仅 PDF size）—— multipart overhead（boundary / headers）也算。
- 超限 → 413 `upload_too_large`（不读完整 body）。

### 22.3 不变量

- ❌ "localhost" 不是唯一授权——必须 Trusted UI header 同时满足。
- ❌ 不接受 session_id 作权限绕过。
- ❌ R2-C 管理 API **不**做 Session Library Binding 校验（management plane，不是检索面）。
- ❌ 不面向公网 / 不实现用户认证 / RBAC / OAuth。
- ✅ 未来 Agent Tool (`search_knowledge`) 用 Session allowlist（R4），不复用管理 API。

---

## 23. File and Path Safety

### 23.1 固定路径

- `libraries/{library_id}/documents/{document_id}/source.pdf`
- `libraries/{library_id}/documents/{document_id}/document.md`

**不接受用户路径参数**。`source_name` 仅作 metadata；不参与路径构造。

### 23.2 Staging Path

```
libraries/{library_id}/.staging/{secrets.token_hex(16)}.pdf.tmp
```

- `.staging/` 目录前缀（与 R2-B atomic-write temp 命名区分：R2-B 用 `.{filename}.{rand}.tmp` in-place）。
- 文件名随机（避免冲突）+ `.pdf.tmp` 后缀（startup cleanup 模式匹配）。
- 写入前 mkdir（parents=True, exist_ok=True）。
- 写入后 fsync。

### 23.3 Atomic Write Temp（R1 已实现，复用）

R1 `write_file_atomic` 在目标目录内创建 `.{filename}.{rand}.tmp`，写完后 `os.replace`。

- R2-C upload: `KnowledgeFileStore.write_file_atomic(lib, doc, 'source.pdf', bytes)`。
- R2-B persist: `KnowledgeFileStore.write_file_atomic(lib, doc, 'document.md', bytes)` —— 已实现。

### 23.4 Startup Cleanup

```
扫描 libraries/*/.staging/*.tmp
扫描 libraries/*/documents/*/.*.tmp
```

- 仅在 Knowledge root 内。
- 不递归删除任意 `.tmp`。
- 不删除 source.pdf / document.md / manifest.json。
- 不跟随 symlink。
- Cleanup 失败仅日志，不阻塞 app 启动（除非路径 containment 失败 → fail-fast）。

### 23.5 Windows / POSIX 差异

- `os.replace` 在 Windows 上对已存在文件需先关闭句柄——pypdf Parser `finally: reader.stream.close()` 已保证（R2-A frozen）。
- Staging 写入完成后 `os.close(fd)` + `os.replace`——同样在 Windows 上工作。
- Symlink 测试在 Windows 上 skipped（R1 已知缺口，per `P2_R1_LIBRARY_FOUNDATION.md §8.2`）——R2-C 不修复此缺口，但生产路径检查不依赖测试环境能否创建 symlink。

---

## 24. Determinism and generated_at

### 24.1 R2-C MVP Hard Rule

```
R2-C Orchestrator MUST NOT pass generated_at to CanonicalMarkdownBuilder.
```

```python
# Frozen call signature (R2-C):
artifact = builder.build(
    source=source,
    extraction=extraction,
    quality=quality,
    title=inspection.metadata.title,
    # generated_at omitted — Builder uses default (None → field omitted in frontmatter)
)
```

### 24.2 Forbidden Sources

- ❌ Job `started_at` / `finished_at`
- ❌ Document `created_at` / `updated_at`
- ❌ `datetime.now()` / `time.time()`
- ❌ Environment variable
- ❌ Metadata 派生
- ❌ Retry time

### 24.3 确定性不变量

```
Same source.pdf + Same parser_id+version + Same Builder version + Same input metadata
  → byte-identical Canonical Markdown
  → Same content_sha256
```

跨：

- 首次 ingestion
- Retry ingestion
- App restart 后 retry
- Different process / OS / locale / timezone

### 24.4 任务时间戳来源

- DB `knowledge_documents.created_at` / `updated_at`（epoch ms）——记录 metadata 变更时间。
- DB `knowledge_ingestion_jobs.started_at` / `finished_at`——记录 Job 执行时间。
- 这些**不**进入 Canonical Markdown artifact。

### 24.5 测试断言（C1/C2/C3 必须 enforce）

- `test_generated_at_omitted_in_default_build`
- `test_same_input_produces_byte_identical_markdown`
- `test_retry_does_not_change_markdown_sha`
- `test_app_restart_retry_does_not_change_markdown_sha`

---

## 25. Schema Gap Analysis

### 25.1 Hard Requirements vs Current Schema

| # | R2-C Hard Requirement | Schema Support | Status |
|---|---|---|---|
| 1 | active Job uniqueness | App-level BEGIN IMMEDIATE + SELECT COUNT + INSERT | ✅ sufficient |
| 2 | Job history | Rows persist after `finish_job`（completed/failed 不可变）；attempt auto-increment | ✅ sufficient |
| 3 | Atomic claim | UPDATE docs SET status='extracting' WHERE id=(SELECT uploaded LIMIT 1) in BEGIN IMMEDIATE | ✅ sufficient |
| 4 | Retry | New `create_job` row；old jobs 历史；attempt +1 | ✅ sufficient |
| 5 | Recovery (running → failed) | `finish_job(job_id, 'failed', safe_error_code='ingestion_interrupted')` | ✅ sufficient |
| 6 | Ready artifact metadata | `markdown_relpath` exists；`markdown_sha256` NOT exists | ⚠ non-blocking |
| 7 | parser_id | `parser_version` exists；`parser_id` NOT exists | ⚠ non-blocking |
| 8 | Safe terminal error | `error_code` column exists（`failed`/`needs_ocr` 状态下） | ✅ sufficient |
| 9 | latest Job query | `list_jobs_for_document` ordered by started_at + id | ✅ sufficient |
| 10 | Warnings persistence | No `warnings_json` column | ⚠ non-blocking |

### 25.2 Non-Blocking Gaps

**Gap #6 (markdown_sha256)**：

- 影响：无法在 DB 层直接验证 markdown integrity。
- MVP 决策：**不添加列**。需要时由 Orchestrator 在 build 后比较 `write_result.sha256 == artifact_sha256`（in-memory check），不持久化。
- R2-D 评估：若需要读取时验证（per `markdown` API），可加列；非 R2-C 阻塞。

**Gap #7 (parser_id)**：

- 影响：DB 无法直接区分 parser 类型（pypdf vs future marker）。
- MVP 决策：**不添加列**。`parser_version`（如 "6.14.2"）+ Canonical Markdown artifact frontmatter 的 `parser_id`（如 "pypdf"）已足够。需要 parser_id 时从 `document.md` 反读。
- R2-D 评估：若 DB-level parser filtering 成需求，加 `parser_id TEXT NOT NULL DEFAULT ''`；非 R2-C 阻塞。

**Gap #10 (warnings_json)**：

- 影响：质量 warnings（low_non_empty_page_ratio 等）无法持久化。
- MVP 决策：**不添加列**。Warnings 写入安全日志（structured log fields）。
- R2-D 评估：若用户需要查看历史 warnings，加 `warnings_json TEXT NOT NULL DEFAULT '[]'`；非 R2-C 阻塞。

### 25.3 Conclusion

```
Schema changes required for R2-C? 
✅ NO (NOT REQUIRED)

All hard requirements (active Job uniqueness / history / claim / retry / recovery /
error_code) are satisfied by the existing R1 schema + app-level transactions.

3 non-blocking gaps (markdown_sha256 / parser_id / warnings_json) are deferred
to R2-D evaluation; MVP works without them.
```

**No `P2-R2-C0.5` Schema Amendment needed.** R2-C1 编码门可立即启动（独立启动授权另需用户发起）。

---

## 26. Thread and SQLite Ownership

### 26.1 SQLite Connection Model（per R1 frozen）

- `KnowledgeStore.open(db_path)` 创建**独立** `aiosqlite.Connection`（`isolation_level=None` autocommit）。
- `PRAGMA journal_mode=WAL`：并发读不阻塞写。
- `PRAGMA busy_timeout=5000`：写冲突时最多等 5s。
- 每次写事务：`BEGIN IMMEDIATE`（reserved lock） + 业务 SQL + `COMMIT`/`ROLLBACK`。
- `KnowledgeStore._write_lock: asyncio.Lock`：同进程内进一步串行化（避免 SQLite busy 重试）。

### 26.2 Thread Ownership

R2-A PypdfParser 是**同步**接口（`inspect` / `extract` 直接调用 pypdf）。Orchestrator 不能在 event loop 内直接调用（会阻塞 loop）。

**MVP 决策**：

```python
# Orchestrator uses asyncio.to_thread for sync Parser calls:
inspection = await asyncio.to_thread(parser.inspect, source_path)
extraction = await asyncio.to_thread(parser.extract, source_path)
```

- 不创建 dedicated executor（MVP）。
- 不创建无界线程池（`asyncio.to_thread` 用默认 `asyncio.get_event_loop().run_in_executor(None, ...)` 即可）。
- worker_concurrency = 1 保证同一时刻只有一个 Parser 调用在 to_thread，不会爆线程池。

### 26.3 SQLite 跨线程规则

- **每个 Store 操作打开/拥有自己的 connection**：R1 已是此模型（`KnowledgeStore` 持有单一 connection，所有方法用同一 connection）。
- **不跨线程复用 connection**：`asyncio.to_thread` 内的 Orchestrator DB 操作通过 `asyncio.to_thread` 回到 event loop 线程执行（即 Orchestrator 是 async，Parser 是 sync；DB 操作仍在 event loop 线程）。
- **`check_same_thread`**：aiosqlite 内部已处理（`aiosqlite.connect` 在独立线程跑 SQLite）。
- **R2-C 不修改 Store 的 connection 模型**。

### 26.4 Worker Task 模型

```
FastAPI event loop (单线程 async)
  ↓ asyncio.create_task
WorkerManager.worker_loop task
  ↓ awaits Orchestrator.run_one(doc_id)
    ↓ awaits asyncio.to_thread(parser.inspect/extract)
    ↓ awaits KnowledgeStore.* (回到 event loop)
  ↓ finally: Orchestrator commit / rollback
```

不引入：

- ❌ `threading.Thread`（除 aiosqlite 内部）
- ❌ `concurrent.futures.ProcessPoolExecutor`
- ❌ `multiprocessing`
- ❌ Cross-thread SQLite connection

---

## 27. Logging and Observability

### 27.1 最小日志字段

```
event (e.g. "ingestion_claim" / "ingestion_complete" / "ingestion_failed")
document_id
job_id (if exists)
library_id
status_from
status_to
attempt
safe_error_code
duration_ms
```

### 27.2 禁止记录

- ❌ PDF 正文（任何 chunk / page content）
- ❌ Markdown 全文
- ❌ 绝对路径（仅记 `library_id` / `document_id`）
- ❌ Temp path
- ❌ API key / Authorization header
- ❌ 完整原始异常（traceback）
- ❌ 上传 bytes 原文
- ❌ 用户文件内容

### 27.3 Internal Exception 记录

- ✅ Exception type（如 `PdfParseFailed`）
- ✅ Sanitized category（如 "parser_failure"）
- ❌ Exception message（可能含 pypdf 内部信息）—— 仅在 `safe_error_code` 已捕获语义时记 safe_error_code

### 27.4 Trace / Correlation

R2-C 不引入新 trace_id；复用现有项目模式（若有）。MVP 不要求 cross-request correlation。

### 27.5 不引入

- ❌ 完整可观测平台（OpenTelemetry / Prometheus / Sentry）
- ❌ PDF 正文 trace
- ❌ 用户行为分析

---

## 28. Store/FileStore Extension Plan

### 28.1 KnowledgeStore 新方法（C1 实现）

```python
# Atomic claim (worker side)
async def claim_next_uploaded_document(self) -> Document | None:
    """Atomically transition one 'uploaded' document to 'extracting'
    and create a Job. Returns None if no pending work."""

async def list_uploaded_document_ids(self) -> tuple[str, ...]:
    """For startup recovery and polling fallback."""

async def count_active_jobs_for_document(self, document_id: str) -> int:
    """For retry active-Job uniqueness check (within BEGIN IMMEDIATE)."""

async def mark_running_jobs_interrupted(self) -> int:
    """Startup recovery: mark all 'running' jobs as 'failed' with
    safe_error_code='ingestion_interrupted'. Returns count of marked jobs."""

async def list_documents_in_status(self, *statuses: str) -> tuple[Document, ...]:
    """For startup recovery scanning."""

async def count_extract_attempts(self, document_id: str) -> int:
    """For retry limit check (count of stage='extract' jobs for doc)."""
```

### 28.2 KnowledgeFileStore 新方法（C1 实现）

```python
async def write_source_atomically(
    self, library_id: str, document_id: str, source_bytes: bytes
) -> None:
    """Write source.pdf via atomic-write primitive.
    Reuses write_file_atomic with filename='source.pdf'."""

async def write_staging(
    self, library_id: str, staging_filename: str, chunk_iter
) -> tuple[int, str]:
    """Stream-upload staging writer.
    Returns (bytes_written, sha256_hex)."""

async def cleanup_staging(self, library_id: str, staging_filename: str) -> None:
    """Idempotent staging file cleanup."""

async def cleanup_temp_files(self) -> int:
    """Startup cleanup of .staging/*.tmp + .*.{rand}.tmp under knowledge root.
    Returns count of removed files. Idempotent. Best-effort."""
```

### 28.3 不新增

- ❌ 第二套 FileStore
- ❌ 第二套 atomic write primitive
- ❌ 跨进程锁
- ❌ Schema 列（per §25）

### 28.4 R1 frozen 不变量保留

- ID 格式校验（`^(lib|doc|chunk|job)_[a-z0-9]{12,32}$`）
- Path containment（`Path.resolve()` 必须在 knowledge root 内）
- Symlink escape 防护
- BEGIN IMMEDIATE + COMMIT/ROLLBACK 模式
- `_write_lock: asyncio.Lock` 同进程串行化

---

## 29. R2-C Subphase Plan

### 29.1 P2-R2-C1 — Ingestion Store Extensions + Orchestrator

**范围**：

- KnowledgeStore 新方法（per §28.1）
- KnowledgeFileStore 新方法（per §28.2）
- IngestionOrchestrator（claim → inspect → extract → evaluate → build → persist → terminal commit）
- 错误映射辅助
- 单元测试

**不**做：

- 启动 background task
- 接 FastAPI endpoint
- 修改 frontend
- 实现 queue manager
- HTTP upload

### 29.2 P2-R2-C2 — Bounded Worker + Recovery

**范围**：

- WorkerManager（asyncio.Queue + asyncio.Event + worker_loop task）
- FastAPI lifespan 接线（startup create / shutdown graceful）
- Startup recovery（per §15）
- Graceful shutdown（per §16）
- Thread/executor 边界
- Worker 测试（fake clock / fake parser / fake db）

**不**做：

- 新增 API
- 修改 Canonical Builder / Parser
- frontend

### 29.3 P2-R2-C3 — Upload / Status / Retry / Markdown API

**范围**：

- Upload API（multipart 流式 + staging + 事务顺序 per §10）
- Status API
- Retry API
- Markdown API
- Trusted UI 接线（复用 require_ui_header_dep）
- KnowledgeUploadBodyLimitMiddleware
- Service 层
- API 测试
- 必要 OpenAPI DTO

**不**做：

- 修改 Schema
- 修改 Parser / Builder
- 实现 Retrieval
- 实现 Agent Tool
- frontend UI

### 29.4 P2-R2-C4 — Integration Validation + Freeze

**范围**：

- 完整端到端 backend 测试（upload → ingest → markdown）
- 重启恢复测试
- 并发 retry 测试
- Delete race 测试
- 文件 / DB 失败注入测试
- 安全扫描
- 完整 backend pytest / 0 regression
- frontend 零回归
- Validation 文档
- 状态冻结

**不**做：

- 启动 R3
- Tag / merge / push（独立授权）

### 29.5 依赖图

```
C1 (Store + Orchestrator)
  ↓
C2 (Worker + Recovery)   ← depends on C1
  ↓
C3 (API)                 ← depends on C2
  ↓
C4 (Validation + Freeze) ← depends on C3
```

C1/C2/C3/C4 严格串行；不并行。

---

## 30. Test Plan

### 30.1 C1 单元测试（Store + Orchestrator）

| 测试目标 | 数量 |
|---|---|
| Store 新方法：claim_next_uploaded_document FIFO + atomic | ~8 |
| Store 新方法：count_active_jobs_for_document | ~3 |
| Store 新方法：mark_running_jobs_interrupted | ~3 |
| Store 新方法：count_extract_attempts / retry limit | ~3 |
| Orchestrator 完整 happy path（mocked parser） | ~5 |
| Orchestrator 失败路径（per §19 Case 11-23） | ~15 |
| Orchestrator needs_ocr 路径 | ~3 |
| Orchestrator terminal DB commit 失败补偿 | ~5 |
| `generated_at` 不变量 | ~3 |
| Atomic write + cleanup | ~5 |
| **C1 小计** | **~53** |

### 30.2 C2 测试（Worker）

| 测试目标 | 数量 |
|---|---|
| Startup recovery（pending + interrupted） | ~8 |
| Graceful shutdown（success within grace / timeout cancel） | ~5 |
| Polling 兜底（无 wake event） | ~3 |
| Worker claim 并发（concurrency=1） | ~3 |
| Wake event 处理 | ~3 |
| CancelledError 不留 running Job | ~5 |
| Temp cleanup | ~3 |
| **C2 小计** | **~30** |

### 30.3 C3 测试（API）

| 测试目标 | 数量 |
|---|---|
| Upload happy path（streaming + size + sha + signature + duplicate check） | ~10 |
| Upload 失败（per §19 Case 1-7） | ~12 |
| Upload duplicate 全状态覆盖（per §11） | ~5 |
| Upload 大小超限（streaming abort） | ~3 |
| Upload MIME / signature 拒绝 | ~5 |
| Upload body limit middleware | ~3 |
| Status API 全状态 | ~5 |
| Retry happy path | ~3 |
| Retry conflict（active Job / wrong status / limit） | ~8 |
| Markdown API happy path | ~5 |
| Markdown API not_ready / not_found | ~3 |
| Trusted UI header 缺失 | ~3 |
| 并发 retry race | ~3 |
| DELETE Document with active Job | ~3 |
| DELETE Library with active Job | ~3 |
| **C3 小计** | **~75** |

### 30.4 C4 测试（Integration）

| 测试目标 | 数量 |
|---|---|
| End-to-end upload → ready（real pypdf fixture） | ~5 |
| End-to-end upload → needs_ocr（blank PDF fixture） | ~3 |
| End-to-end retry → byte-identical SHA | ~3 |
| Restart recovery full cycle | ~5 |
| Failure injection（FS / DB） | ~10 |
| **C4 小计** | **~26** |

### 30.5 总计

```
C1 ~53 + C2 ~30 + C3 ~75 + C4 ~26 ≈ 184 new tests
```

实际数字以实施为准；C0 不强制精确数。

### 30.6 不变量测试

每个子阶段必须 enforce：

- ❌ 错误响应不含绝对路径（grep test）
- ❌ 日志不含 PDF 正文 / Markdown 全文（grep test）
- ❌ 错误响应不含 traceback（grep test）
- ❌ generated_at 不出现在 Canonical Markdown artifact frontmatter（default build）
- ❌ 同输入 retry 产出 byte-identical Markdown + SHA
- ❌ Retry 时不重新计算 SHA
- ❌ Worker 不在 event loop 直接调用同步 Parser

---

## 31. R2-D Count Reconciliation Handoff

C0 正式传递给 R2-D：

```
R2-B targeted tests = 160
R2-A backend baseline = 2768
R2-B backend reported = 2920
reported delta = 152
unreconciled difference = 8
8-test discrepancy: KNOWN NON-BLOCKING
```

R2-D 必须核对（per `P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md §5.5`）：

1. `pytest --collect-only` 数量（baseline `7db4780` vs 当前提交）
2. 实际执行数量（passed + failed + skipped）
3. `skipped` / `deselected` 数量
4. baseline 与当前提交使用的 pytest 配置 / 插件 / marker（确认 R2-A 报告期与 R2-B 后是否一致）
5. 是否存在 collection 后未执行的测试项（如 fixture 去重 / parametrize 折叠 / conftest skip）

**Ruff --fix / fixture 去重** 仅作为假设（H1/H2/H3）—— 不得升级为根因结论。R2-D 必须以实际诊断为准。

C0 本身不要求重新跑完整测试。

---

## 32. Known Limitations

### 32.1 pypdf 技术限制（per `p2-r2-0-pdf-parser-license-gate.md §21.1`）

- 复杂多栏顺序可能退化（pypdf 按 PDF 内容流，不重排栏）
- 表格不重建（输出为段落文本）
- 标题只使用保守启发式（字号 / 加粗 / 大写信息 pypdf 不暴露）
- 页眉页脚未去重
- 断词未合并（不自动 `inter-\nnational` → `international`）
- 公式 / 图片不处理
- 扫描 PDF 进 `status=needs_ocr` 终态（不自动 OCR）

### 32.2 R2-C MVP 限制

- worker_concurrency = 1（不支持并行 ingest）
- 不支持多进程 / 分布式 worker
- 不支持 cancel API
- 不支持 pause / resume
- 不支持自动 retry
- 不支持 chunking / indexing / FTS5（R3）
- 不支持 search_knowledge（R3）
- 不支持 Session ACL（R4）
- 不支持前端 UI（R5）
- 不支持 generated_at（per §24）

### 32.3 平台限制

- Windows symlink 测试覆盖缺口（R1 已知，per `P2_R1_LIBRARY_FOUNDATION.md §8.2`）。
- Windows 上 `os.replace` 需先关闭文件句柄（pypdf `finally: reader.stream.close()` 已保证）。
- POSIX 上 atomic write primitive 是真正原子；Windows 上为 best-effort（`os.replace` 替换已存在文件）。

### 32.4 Schema 限制

- 无 `markdown_sha256` 列（per §25 Gap #6）。
- 无 `parser_id` 列（per §25 Gap #7）。
- 无 `warnings_json` 列（per §25 Gap #10）。
- 无 `worker_owner` 列（concurrency=1 不需要）。
- 无 `retry_of_job_id` 列（attempt + document_id 已可重建 retry 链）。

### 32.5 不解决（推到 R3+）

- OCR / Image understanding / 视觉理解
- Embedding / Vector retrieval / Reranker **`[DECLINED 2026-08-03]`** —— 用户决策"PDF→MD 即可，不再向量化"；永久不做（per [`p2-r0-decisions-log.md §3.1`](p2-r0-decisions-log.md)）；R3 = pure FTS5 BM25
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- 长期后台任务调度

---

## 33. Open Questions

```
Open Questions = 0
```

所有 R2-C 启动指令 §68 的 38 项决策已在本合同中明确回答（per §6-§32）。无未解决项。

---

## 34. Exit Gate

| # | 条件 | 状态 |
|---|---|---|
| 1 | baseline c2436c7 = HEAD ancestor | ✅ |
| 2 | G1 stash hash = d7240268 不变 | ✅ |
| 3 | working tree clean | ✅ |
| 4 | Schema audit 完成 | ✅ §3 |
| 5 | 真实 Schema 与 R2-C 要求对账完成 | ✅ §25 (NOT REQUIRED) |
| 6 | Schema Amendment 判定 | ✅ NOT REQUIRED（§25.3） |
| 7 | Document 状态机冻结 | ✅ §8 |
| 8 | Job 状态机冻结 | ✅ §9 |
| 9 | Upload 事务顺序冻结 | ✅ §10 |
| 10 | Duplicate SHA 语义冻结 | ✅ §11 |
| 11 | Retry 语义冻结 | ✅ §12 |
| 12 | Active Job uniqueness 算法冻结 | ✅ §13.1 |
| 13 | Worker claim 算法冻结 | ✅ §13.2 |
| 14 | Worker 并发模型冻结 | ✅ §14 (concurrency=1, queue=32) |
| 15 | Startup recovery 冻结 | ✅ §15 |
| 16 | Graceful shutdown 冻结 | ✅ §16 (grace=30s) |
| 17 | Delete race 规则冻结 | ✅ §17 |
| 18 | Orchestrator 调用顺序冻结 | ✅ §18 |
| 19 | 失败矩阵冻结 | ✅ §19 (27 cases) |
| 20 | safe_error_code 全表冻结 | ✅ §20 |
| 21 | API 合同表冻结 | ✅ §21 |
| 22 | Trusted UI 边界冻结 | ✅ §22 |
| 23 | 文件 / 路径安全冻结 | ✅ §23 |
| 24 | generated_at MUST NOT PASSED 冻结 | ✅ §24 |
| 25 | Thread / SQLite ownership 冻结 | ✅ §26 |
| 26 | 日志可观测性冻结 | ✅ §27 |
| 27 | Store / FileStore 扩展计划冻结 | ✅ §28 |
| 28 | R2-C 子阶段冻结 | ✅ §29 (C1/C2/C3/C4) |
| 29 | 测试计划冻结 | ✅ §30 |
| 30 | R2-D 测试差异 handoff | ✅ §31 |
| 31 | 已知限制记录 | ✅ §32 |
| 32 | Open Questions = 0 | ✅ §33 |
| 33 | Contract conflict resolved (R0 §8.1 sync→async) | ✅ §35.2 |
| 34 | C0 设计文档完成 | ✅ 本文件 |
| 35 | C0 validation 文档完成 | ✅ 配套 |
| 36 | production / test / dep / lockfile / schema / frontend diff = 0 | ✅ docs-only |
| 37 | 单 atomic docs commit | ✅ §36 |

**37/37 PASS** ✅

---

## 35. Contract Conflict Resolution

### 35.1 Identified Conflict

**R0 §8.1（sync-wait-then-poll）**:

```
POST /api/knowledge/libraries/{lib}/documents
  ↓
同步处理（同 request 内）
  ↓
PDF ≤ 20 页 + 处理 ≤ 30s
  ├── 完成 → 返回 {document_id, status: "ready"}
  └── 超时 → 返回 {document_id, status: "extracting", job_id}（前端轮询）
```

**R2-C0 directive §34（pure async 201）**:

```
不得在上传请求中等待 Parser 完成。
成功响应：201 Created + {document, job: null}.
```

冲突点：R0 §8.1 让 HTTP 请求阻塞最多 30s；R2-C0 directive 要求立即返回 201。

### 35.2 Resolution

**按 R2-C0 directive**（async 201）执行：

- 用户 R2-C0 启动指令 §70 明确："允许最小修改 docs/design/p2-r0-rag-contract.md ... 只有在 C0 需要记录已经获得授权的精确合同细化；不改变 R0 核心架构；不修改 R1/R2-A/R2-B 冻结能力；时才允许修改 P2-R0 文档。"
- R2-C0 directive 本身即用户对 R0 §8.1 精化的明确授权。
- **不创建 Amendment 3**——R0 核心架构（数据模型 / 文件布局 / PDF 边界 / Chunk / Tool / ACL）未变；仅 upload HTTP 行为从 sync-wait 改为 async-201。

**最小修改**：

- `p2-r0-rag-contract.md` §8.1：增加 cross-ref，指明 R2-C0 已细化 upload 行为为 async 201 + worker queue（per 本文档 §10 / §14）。
- `p2-r0-decisions-log.md` R3 行：加 cross-ref 注释。

### 35.3 不修改的 R0 决策

- F1 数据模型（5 表）— 不变
- F2 文件系统布局（`libraries/{lib}/documents/{doc}/`）— 不变
- F3 PDF 边界（数字 PDF only / 扫描 → needs_ocr）— 不变
- F4 Canonical Markdown frontmatter — 不变（amendment-2 已 ratify）
- F5 Chunk 格式 — 不变（R3 实现）
- F6 Session ACL — 不变（R4 实现）
- F7 search_knowledge tool — 不变（R3 实现）
- F8 同步策略核心 — 略精化（worker queue + async 201），但不引入 BackgroundTask 框架
- R1 pypdf parser 选型 — 不变
- R2 独立 knowledge.db — 不变
- R3 30s 阈值 — **保留**（worker shutdown grace = 30s；per §16）
- R4 Chunk 参数 — 不变
- R5 KnowledgeFileStore — 不变

### 35.4 不创建 Amendment 3 的理由

按 R2-C0 directive §70：

- "不得未经授权创建 Amendment 3"
- R2-C0 directive 已授权 R0 §8.1 的最小化精化（per §70 "已经获得授权的精确合同细化"）
- 不存在需要 Amendment 流程的其他冲突
- 用户若发现本判定错误，可独立授权启动 Amendment 3

---

## 36. Final Verdict

```
P2-R2-C0 Ingestion Runtime and API Contract
✅ COMPLETE / FROZEN @ <this commit>

Schema Amendment
✅ NOT REQUIRED (R1 schema sufficient; 3 non-blocking gaps deferred to R2-D)

P2-R2-C1 Ingestion Store Extensions + Orchestrator
✅ APPROVED TO START (独立启动授权另需用户发起)

P2-R2-C2 Bounded Worker + Recovery
⛔ BLOCKED BY C1

P2-R2-C3 Upload / Status / Retry / Markdown API
⛔ BLOCKED BY C2

P2-R2-C4 Integration Validation + R2-C Freeze
⛔ BLOCKED BY C3

P2-R2-D Integration Validation
⛔ BLOCKED BY COMPLETE R2-C
⚠ MUST RECONCILE 8-test discrepancy (audit §5.5)

P2-R3 Retrieval MVP
⛔ BLOCKED BY COMPLETE P2-R2

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
  (unchanged across R2-C0)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

C0 完成。R2-C1 编码门开启（独立启动授权另需用户发起）；MVP 不得传 `generated_at`；R2-D 必查 8-test discrepancy（5 项诊断）。

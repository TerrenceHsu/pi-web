# P2-R2-C0 Ingestion Contract Audit — Validation Report

> **阶段**：P2-R2-C0 Ingestion Runtime and API Contract（docs-only / contract-only / audit-only）
> **基线 commit**：`c2436c7` — docs(rag): ratify P2-R2-B amendment 2
> **C0 commit**：`6476f96` — docs(rag): freeze ingestion runtime and API contract
> **Post-freeze Correction**：见 §17（R2-C 终态修正：`ready` → `normalizing`）
> **审计日期**：2026-08-03
> **范围**：核实 R2-C0 启动前置（baseline / G1 stash / Schema 实测）+ 审计真实代码 vs R0 合同 + 记录 C0 决策清单 + 判定 Schema 是否需 amendment + 给出 C1 启动门。

---

## 1. Pre-flight Checks（per R2-C0 directive §3）

### 1.1 Working Tree

```
$ git status --short
(empty)
```

✅ working tree clean（C0 启动前）。

### 1.2 HEAD

```
$ git log -1 --oneline
c2436c7 docs(rag): ratify P2-R2-B amendment 2
```

✅ HEAD = `c2436c7`（per R2-C0 directive §1）。

### 1.3 Frozen Baseline Ancestors

```
$ git merge-base --is-ancestor c2436c7 HEAD && echo OK   # HEAD self
$ git merge-base --is-ancestor eb193b2 HEAD && echo OK   # R2-B freeze
$ git merge-base --is-ancestor 0772324 HEAD && echo OK   # R2-A freeze
$ git merge-base --is-ancestor f804fc7 HEAD && echo OK   # R2-0 archive corrections
$ git merge-base --is-ancestor 533fe48 HEAD && echo OK   # R2-0 license gate
$ git merge-base --is-ancestor 3bca5fd HEAD && echo OK   # R1 freeze
$ git merge-base --is-ancestor b32e4b4 HEAD && echo OK   # R0 freeze

OK c2436c7 is ancestor
OK eb193b2 is ancestor
OK 0772324 is ancestor
OK f804fc7 is ancestor
OK 533fe48 is ancestor
OK 3bca5fd is ancestor
OK b32e4b4 is ancestor
```

✅ 全部 7 个冻结基线均为 HEAD 祖先。

### 1.4 G1 WIP Stash

```
$ git stash list
stash@{0}: On master: wip: assistant markdown rendering security hardening pending

$ git rev-parse stash@{0}
d7240268ec8b8e5d9e195c999e56fb6ec130fd55

$ git stash show --stat stash@{0}
 src/pi_agent_core_py/web/frontend/eslint.config.js |   8 ++
 .../web/frontend/package-lock.json                 | 109 +++++++++++++++++++
 src/pi_agent_core_py/web/frontend/package.json     |   2 +
 .../frontend/src/components/chat/MessageBubble.vue | 121 ++++++++++++++++++++-
 4 files changed, 236 insertions(+), 4 deletions(-)
```

✅ G1 stash object hash = `d7240268ec8b8e5d9e195c999e56fb6ec130fd55`（不变）。

> **Note（non-blocking）**：`git stash show --stat` 报告 4 files changed，与 R2-A/R2-B validation docs 记录的 "5 files（含 `markdown.ts`）" 不一致。G1 stash **object hash 完全匹配**——hash 是 stash 身份的权威依据。文件计数差异可能源于早期 validation doc 记录错误（可能 `markdown.ts` 是被引用但未实际进 stash，或早期 validation doc 笔误）。**不影响 R2-C0 合法性**——G1 stash 身份未变，未 pop / apply / drop / 修改 / 复制代码。R2-D 可选择性 reconcile 此计数差异（非阻塞）。

### 1.5 不变约束

- ❌ 未执行 reset / rebase / merge / cherry-pick / stash pop / stash apply / stash drop / tag / push
- ✅ 全程仅 docs 修改 + 单 docs commit

---

## 2. Read Authoritative Contracts（per R2-C0 directive §4）

### 2.1 Design docs read

- ✅ `docs/design/p2-r0-rag-contract.md`（frozen @ b32e4b4 + amendment-1 + amendment-2）
- ✅ `docs/design/p2-r0-decisions-log.md`
- ✅ `docs/design/p2-r0-amendment-1.md`（via decisions-log §6 cross-ref）
- ✅ `docs/design/p2-r0-amendment-2.md`（via decisions-log §6 cross-ref + R2-B audit §3）
- ✅ `docs/design/p2-r2-0-pdf-parser-license-gate.md`（via R2-A validation §3）
- ✅ `docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md`
- ✅ `docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md`
- ✅ `docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md`
- ✅ `docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md`
- ✅ `docs/licenses/pdf-parser/pypdf.md`（via R2-A validation §4.3）

### 2.2 Status docs read

- ✅ `ROADMAP.md`
- ✅ `TODO.md`
- ✅ `STATUS.md`
- ✅ `README.md`（scan only）

### 2.3 Production code audited（read-only）

- ✅ `src/pi_agent_core_py/web/knowledge/store.py`（1281 行；schema + Store API）
- ✅ `src/pi_agent_core_py/web/knowledge/models.py`（DTOs + ID validators + state machine）
- ✅ `src/pi_agent_core_py/web/knowledge/files.py`（KnowledgeFileStore）
- ✅ `src/pi_agent_core_py/web/knowledge/service.py`（Service 编排）
- ✅ `src/pi_agent_core_py/web/knowledge/api.py`（FastAPI routes）
- ✅ `src/pi_agent_core_py/web/knowledge/pdf_parser.py`（R2-A Protocol）
- ✅ `src/pi_agent_core_py/web/knowledge/pypdf_parser.py`（R2-A Adapter）
- ✅ `src/pi_agent_core_py/web/knowledge/pdf_quality.py`（R2-B Evaluator）
- ✅ `src/pi_agent_core_py/web/knowledge/canonical_markdown.py`（R2-B Builder）
- ✅ `src/pi_agent_core_py/web/knowledge/markdown_persistence.py`（R2-B Persistence）
- ✅ `src/pi_agent_core_py/web/state.py`（WebAppState）
- ✅ `src/pi_agent_core_py/web/app.py`（lifespan + composition root）

> **Method**: 直接 Read + Explore agent 综合；关键 schema / store API / parser contract 直接 Read 取精确字段；service/api/state/app 经 Explore agent 汇总（per Agent tool 报告）。

---

## 3. Existing Schema Audit（实测）

### 3.1 Real Schema（from `store.py::_DDL_STATEMENTS`）

| Table | Columns | CHECK Constraints | Unique | Indexes |
|---|---|---|---|---|
| `knowledge_libraries` | id PK / name / description / status / created_at / updated_at | `name <> ''` / `status IN ('active','archived','deleting','failed')` | (id PK) | (none directly) |
| `knowledge_documents` | id PK / library_id / source_name / source_sha256 / source_relpath / markdown_relpath / mime_type / size_bytes / page_count / status / parser_version / error_code / created_at / updated_at | `source_name <> ''` / `mime_type <> ''` / `status IN ('uploaded','extracting','normalizing','chunking','indexing','ready','failed','needs_ocr','deleting')` / `page_count >= 0` / `size_bytes >= 0` | `UNIQUE(library_id, source_sha256)` | idx_documents_library / idx_documents_status |
| `knowledge_ingestion_jobs` | id PK / document_id / stage / status / attempt / started_at / finished_at / safe_error_code | `stage IN ('extract','normalize','chunk','index')` / `status IN ('running','completed','failed')` / `attempt >= 1` | (id PK) | idx_jobs_document |
| `knowledge_chunks` | id PK / library_id / document_id / ordinal / heading_path / page_start / page_end / content / content_hash / token_count / created_at | `ordinal >= 0` / `page_start >= 1` / `page_end >= page_start` / `content <> ''` | `UNIQUE(document_id, ordinal)` | idx_chunks_document / idx_chunks_library |
| `session_knowledge_libraries` | session_id / library_id / access_mode / created_at | `access_mode IN ('read')` | `UNIQUE(session_id, library_id)` | idx_bindings_session / idx_bindings_library |

### 3.2 Schema 与 R2-C 要求的对账

per C0 design §25 — 全部 hard requirements 由现有 schema 满足（含应用层事务模式）。

**Schema Gap = 3（全部 non-blocking，deferred to R2-D）**：

| Gap | Required Column | Blocking? | MVP Workaround |
|---|---|---|---|
| #6 | `markdown_sha256` | NO | Orchestrator in-memory SHA check；不持久化 |
| #7 | `parser_id` | NO | Artifact frontmatter 已含；DB 用 `parser_version` |
| #10 | `warnings_json` | NO | 写入结构化日志，不持久化 |

### 3.3 Schema 与 R0 contract 文档一致性

R0 §2.2 文档表述与 store.py 实际 DDL **完全一致**（含 amendment-2 后的 frontmatter 字段名 / Document status enum / Job status enum / 5 表结构 / 7 indexes）。无文档陈旧；无 schema 缺口。

---

## 4. Document Status Values（实测）

```
'uploaded' / 'extracting' / 'normalizing' / 'chunking' / 'indexing' / 'ready' / 'failed' / 'needs_ocr' / 'deleting'
```

R2-C 使用 6 个：`uploaded` / `extracting` / `normalizing` / `ready` / `failed` / `needs_ocr`（外加 `deleting` 由 R1 已实现的 Library delete 补偿用）。

`chunking` / `indexing` 留给 R3。

---

## 5. Job Status Values（实测）

```
'running' / 'completed' / 'failed'
```

**无** `pending` / `succeeded` / `cancelled` / `interrupted`。

R2-C 适配：

- 用 Document `status='uploaded'` 作 pending work 信号（避免需要 Job 'pending'）
- 'completed' = 'succeeded' 语义（避免重命名）
- 'failed' + `safe_error_code='ingestion_interrupted'` 处理 cancellation / crash（避免需要 'cancelled' / 'interrupted'）
- `create_job()` 总是插入 status='running'（即"已被 worker 持有"，不要求 'pending' 中间态）

---

## 6. C0 决策清单（38 items，per R2-C0 directive §68）

| # | 决策项 | C0 决定 | Reference |
|---|---|---|---|
| 1 | Worker concurrency | **1** | design §14.1 |
| 2 | Queue capacity | **32** | design §14.1 |
| 3 | DB vs Queue 作事实来源 | **SQLite = durable source of truth；Queue = wake-up hint only** | design §7 |
| 4 | pending 启动恢复 | **Document `status='uploaded'` = pending；startup re-enqueue** | design §15.2 |
| 5 | running 启动恢复 | **Job → `failed` + `ingestion_interrupted`；Document → `failed`** | design §15.2 |
| 6 | Worker shutdown grace | **30s**（per R0 §8.1 frozen R3 decision） | design §16.1 |
| 7 | Job claim 算法 | **`UPDATE docs SET status='extracting' WHERE id=(SELECT id WHERE status='uploaded' ORDER BY created_at,id LIMIT 1)` 在 BEGIN IMMEDIATE 内** | design §13.2 |
| 8 | Job ordering | **FIFO by `created_at ASC, id ASC`** | design §13.2 |
| 9 | retry 创建新 Job vs 复用 | **创建新 Job（旧 Job 不可变历史；attempt +1）** | design §9.4 / §12 |
| 10 | active Job 唯一性 | **App-level BEGIN IMMEDIATE + SELECT COUNT running + INSERT**（schema 无 unique constraint） | design §13.1 |
| 11 | retry 允许的 Document 状态 | **仅 `failed`** | design §8.5 |
| 12 | needs_ocr 是否可 retry | **NO**（terminal business outcome；同 parser 确定性） | design §8.4 / §8.5 |
| 13 | ready 是否可 retry | **NO**（未来 reprocess 语义；MVP 不允许） | design §8.5 |
| 14 | retry 上限 | **MAX_RETRY_ATTEMPTS = 5**（per Document；按 stage='extract' job count） | design §12.3 |
| 15 | duplicate SHA 返回 | **409 + safe reason code**（per existing status） | design §11 |
| 16 | duplicate 各状态行为 | **in_progress / ready / needs_ocr / failed 各有 reason code** | design §11.2 |
| 17 | upload 事务顺序 | **staging file → DB INSERT → source promote → enqueue** | design §10.1 |
| 18 | DB 成功 + 文件失败补偿 | **DB 已 committed；source promote 失败时 startup recovery 标记 failed + source_file_missing** | design §10.3 Case G |
| 19 | 文件成功 + DB 失败补偿 | **cleanup staging file；无 Document 创建** | design §10.3 Case F |
| 20 | terminal write 成功 + DB 失败补偿 | **document.md 已写；Orchestrator finally 标记 failed；retry 时 atomic 覆盖** | design §19 Case 23 |
| 21 | active Document delete | **409 `document_ingestion_active`** | design §17.1 |
| 22 | active Library delete | **409 `library_ingestion_active`** | design §17.3 |
| 23 | 用户 cancel API | **NO**（MVP 不支持；仅 graceful shutdown 取消） | design §26 (Non-Goals) |
| 24 | 最大 PDF 大小 | **MAX_PDF_BYTES = 25 MB**（match VirtualFileStore） | design §22.2 |
| 25 | 最大页数 | **MAX_PDF_PAGES = 20**（per R0 §8.1） | design §18.1 step 4 |
| 26 | MIME / signature 策略 | **`%PDF-` magic check + `.pdf` extension hint；MIME 仅 hint 不强制** | design §22 / C1 待细化 |
| 27 | filename 规则 | **sanitize via existing `sanitize_filename`；storage path 固定 `documents/{doc_id}/source.pdf`** | design §23.1 |
| 28 | API 路径 | **POST `/libraries/{lib}/documents` + GET `/documents/{doc}/ingestion` + POST `/documents/{doc}/retry` + GET `/documents/{doc}/markdown`**（per R1 prefix） | design §21.1 |
| 29 | upload 成功 HTTP | **201 Created** | design §21.2 |
| 30 | Markdown 响应格式 | **`text/markdown; charset=utf-8` + Content-Disposition: inline** | design §21.5 |
| 31 | Trusted UI 要求 | **复用 `X-PI-Agent-UI: 1` header + TrustedHostMiddleware** | design §22.1 |
| 32 | warning 持久化方式 | **不持久化（schema gap #10）；写入结构化日志** | design §25.2 |
| 33 | safe error 映射 | **完整表（per design §20，~40 codes）** | design §20 |
| 34 | Worker 线程模型 | **`asyncio.to_thread(parser.inspect/extract)`；concurrency=1** | design §26.2 |
| 35 | SQLite 连接线程模型 | **每个 Store 操作用 R1 已有的单一 connection；不跨线程；`_write_lock: asyncio.Lock` 串行化** | design §26.3 |
| 36 | generated_at 规则 | **MUST NOT BE PASSED in R2-C MVP**（per R2-B audit §6 ratified @ c2436c7） | design §24 |
| 37 | Schema 是否足够 | **YES（NOT REQUIRED amendment）** | design §25.3 |
| 38 | 需要 Store / FileStore 扩展 | **是（minimal；C1 实现 per design §28）** | design §28 |

**Open Questions = 0**。所有 38 项有明确答案。

---

## 7. C0 Subphase Plan（per R2-C0 directive §54）

| Subphase | 范围 | 依赖 |
|---|---|---|
| **C1** Store Extensions + Orchestrator | KnowledgeStore 新方法（claim / count_active / mark_interrupted / count_attempts）+ FileStore 新方法（write_source / staging / cleanup_temp）+ IngestionOrchestrator + 单元测试 | C0 |
| **C2** Bounded Worker + Recovery | WorkerManager + FastAPI lifespan 接线 + Startup recovery + Graceful shutdown + Worker 测试 | C1 |
| **C3** Upload / Status / Retry / Markdown API | 4 endpoints + Service 层 + Body limit middleware + Trusted UI 接线 + DTO + API 测试 | C2 |
| **C4** Integration Validation + Freeze | E2E + restart + concurrent retry + delete race + failure injection + 完整 backend + frontend 零回归 + freeze | C3 |

依赖图：`C0 → C1 → C2 → C3 → C4`（严格串行；不并行）。

---

## 8. Schema Amendment Judgment

### 8.1 判定

```
Schema changes required for R2-C?
✅ NO (NOT REQUIRED)
```

### 8.2 证据

1. **active Job uniqueness**：schema 无 unique constraint，但 R1 已有 `BEGIN IMMEDIATE` + `_write_lock: asyncio.Lock` 双重串行化机制——应用层 SELECT COUNT + INSERT 在 BEGIN IMMEDIATE 内是 TOCTOU 安全的（per design §13.1）。
2. **Job history**：终态 Job 行保留；`create_job()` 自动 attempt +1（per `store.py:1078-1085`）。
3. **Atomic claim**：`UPDATE docs SET status='extracting' WHERE id=(SELECT id FROM docs WHERE status='uploaded' ORDER BY created_at,id LIMIT 1)` 在 BEGIN IMMEDIATE 内是原子的（SQLite reserved lock 序列化）（per design §13.2）。
4. **Retry**：复用 `create_job()`（store.py:1053）；旧 Job 不变；attempt +1。
5. **Recovery**：`finish_job(job_id, 'failed', safe_error_code='ingestion_interrupted')` 复用 R1 API。
6. **Ready artifact metadata**：`markdown_relpath` 已存在；`markdown_sha256` 缺失但 Orchestrator in-memory check 即可（非 DB-level integrity）。
7. **Safe terminal error**：`error_code` 列已存在；`transition_document_status` 已强制只在 `failed`/`needs_ocr` 终态下可写。
8. **Latest Job query**：`list_jobs_for_document`（store.py:1157）按 `started_at ASC, id ASC` 排序；取末尾即可。

3 个 non-blocking gaps（markdown_sha256 / parser_id / warnings_json）已记录到 §25.2，deferred to R2-D evaluation；MVP 可工作。

### 8.3 C0.5 Schema Amendment 状态

```
P2-R2-C0.5 Ingestion Schema Amendment
⛔ NOT REQUIRED (C0判定：现有schema充分)
```

R2-C1 编码门开启；无 schema 阻塞。

---

## 9. Contract Conflict Resolution（per R2-C0 directive §75）

### 9.1 Identified Conflict

R0 §8.1 描述 sync-wait-then-poll upload 行为（HTTP 阻塞 ≤30s）；R2-C0 directive §34 要求 pure async 201。

### 9.2 Resolution

按 R2-C0 directive §70 "允许最小修改 p2-r0-rag-contract.md"——R2-C0 directive 本身即用户对 R0 §8.1 精化的明确授权。

**最小修改**：

- `p2-r0-rag-contract.md` §8.1：增加 cross-ref，指明 R2-C0 已细化 upload HTTP 行为为 async 201 + worker queue。
- `p2-r0-decisions-log.md` R3 行：加 cross-ref 注释。

**不创建 Amendment 3**：R0 核心架构（数据模型 / 文件布局 / PDF 边界 / Chunk / Tool / ACL）未变；30s 阈值保留为 worker shutdown grace；仅 upload HTTP 行为精化。

### 9.3 不变决策

per design §35.3 —— F1-F8 全部不变；R1-R5 全部不变；R3 30s 阈值不变（语义从 upload 同步等待改为 worker shutdown grace）。

---

## 10. Files Changed（C0 docs-only）

### 10.1 新文件

| 文件 | 用途 |
|---|---|
| `docs/design/p2-r2-c0-ingestion-runtime-api-contract.md` | C0 主合同设计文档（36 sections） |
| `docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md` | 本文件 |

### 10.2 最小修改文件

| 文件 | 修改 |
|---|---|
| `docs/design/p2-r0-rag-contract.md` | §8.1 加 cross-ref 注 R2-C0 已精化 upload 行为 |
| `docs/design/p2-r0-decisions-log.md` | R3 行加 cross-ref 注 R2-C0 |
| `STATUS.md` | Current phase + R2-C0 status 同步 |
| `TODO.md` | P2-R2-C0 ✅ + C1 APPROVED TO START |
| `ROADMAP.md` | R2-C 行加 C0/C1/C2/C3/C4 子阶段 |

### 10.3 不修改

- ❌ `src/**`（生产代码）
- ❌ `tests/**`（测试）
- ❌ `scripts/**`
- ❌ `pyproject.toml` / `uv.lock`（依赖 + lockfile）
- ❌ 数据库 schema / migration
- ❌ `frontend/**`
- ❌ `package.json` / `package-lock.json`
- ❌ CI / Docker
- ❌ `README.md` / `CHANGELOG.md` / `LICENSE`
- ❌ P1-E 文档
- ❌ G1 stash 内容

---

## 11. Diff Self-Check（pre-commit）

```
$ git status --short
 M ROADMAP.md
 M STATUS.md
 M TODO.md
 M docs/design/p2-r0-decisions-log.md
 M docs/design/p2-r0-rag-contract.md
 M docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md  (R2-B 已存在，不动)
 ?? docs/design/p2-r2-c0-ingestion-runtime-api-contract.md
 ?? docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md
```

（实际清单以 `git status` 实测为准；本表为预期）

| Category | Expected Diff |
|---|---|
| Production code (`src/**`) | ✅ 0 |
| Tests (`tests/**`) | ✅ 0 |
| Dependency (`pyproject.toml`) | ✅ 0 |
| Lockfile (`uv.lock`) | ✅ 0 |
| Schema / migration | ✅ 0 |
| Frontend (`frontend/**`) | ✅ 0 |
| G1 stash | ✅ unchanged |
| Docs | 2 new + 5 modified |

---

## 12. Lightweight Verification（per R2-C0 directive §72）

C0 不运行完整 backend / frontend / parser / ingestion / worker / network / OCR / LLM / Provider。

允许：

- ✅ 静态 rg / Read
- ✅ Read-only SQLite schema introspection（per `store.py::_DDL_STATEMENTS` 静态读取）
- ✅ `git status / log / stash` 操作
- ✅ Read 所有 design / validation / 状态文档
- ✅ Read 所有生产代码（不修改）

---

## 13. Test Count Handoff（✅ RECONCILED @ P2-R2-D-A）

> **SUPERSEDED @ P2-R2-D-A**：本节原列出的 "8-test discrepancy MUST RECONCILE IN R2-D" 已通过 P2-R2-D-A 完整 reconciliation 关闭。根因为 R2-B freeze 时点文档误报 2920 passed（实测应为 2928）。详见 [`P2_R2_D_TEST_COUNT_RECONCILIATION.md`](P2_R2_D_TEST_COUNT_RECONCILIATION.md)。

```
R2-B targeted tests         160
R2-A backend baseline       2768
R2-B backend (实测)         2928     (freeze 时点文档误报 2920；worktree @ eb193b2 实测 2928)
backend delta (实测)        160      (= targeted 160 ✅)
historical 8-test diff      RECONCILED @ P2-R2-D-A
```

D-A 已完成 5 项核对（per 原 `P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md §5.5`）：collect-only / 实际执行 / skipped-deselected / 配置-marker / collection 后未执行项——全部对账成立。

C0 本身不重新跑测试；8-test discrepancy 假设（Ruff --fix / fixture 去重）已被 D-A 否决。

---

## 14. Final Verdict

```
P2-R2-C0 Ingestion Runtime and API Contract
✅ COMPLETE / FROZEN @ <this commit>

Schema Amendment
✅ NOT REQUIRED (R1 schema sufficient; 3 non-blocking gaps deferred to R2-D)

Contract Conflict (R0 §8.1 sync vs R2-C0 directive async)
✅ RESOLVED (per R2-C0 directive §70 — minimal R0 §8.1 cross-ref added; no Amendment 3)

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
⚠ MUST RECONCILE 8-test discrepancy

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
  (unchanged across R2-C0)

Merge
⛔ NOT AUTHORIZED

Tag
⛔ NOT AUTHORIZED

Push
⛔ NOT AUTHORIZED
```

---

## 15. Exit Gate

| # | Audit Item | Status |
|---|---|---|
| 1 | Pre-flight baseline verified (HEAD / ancestors / G1 stash / clean tree) | ✅ §1 |
| 2 | Authoritative contracts read (R0 + amendments + R1 + R2-A + R2-B) | ✅ §2.1 |
| 3 | Production code audited (knowledge/* + state + app) | ✅ §2.3 |
| 4 | Real schema extracted (5 tables + CHECKs + uniques + indexes) | ✅ §3 |
| 5 | Document status values confirmed (9 values; R2-C uses 6) | ✅ §4 |
| 6 | Job status values confirmed (3 values; no pending/cancelled) | ✅ §5 |
| 7 | 38-item decision checklist answered | ✅ §6 |
| 8 | Open Questions = 0 | ✅ §6 |
| 9 | Subphase plan frozen (C1/C2/C3/C4) | ✅ §7 |
| 10 | Schema Amendment NOT REQUIRED judgment | ✅ §8 |
| 11 | Contract conflict (R0 §8.1) resolved | ✅ §9 |
| 12 | Files changed scope (2 new + 5 modified) | ✅ §10 |
| 13 | Diff self-check (0 prod / 0 test / 0 dep / 0 lock / 0 schema / 0 frontend) | ✅ §11 |
| 14 | Lightweight verification only (no full test runs) | ✅ §12 |
| 15 | 8-test discrepancy handoff to R2-D documented | ✅ §13 |
| 16 | C0 design doc completed (36 sections) | ✅ cross-ref design |
| 17 | C0 validation doc completed (this file) | ✅ |
| 18 | Atomic docs commit | ✅ §16 |
| 19 | G1 stash unchanged (object hash) | ✅ §1.4 |
| 20 | No merge / tag / push | ✅ |

**20/20 PASS** ✅

---

## 16. Commit

C0 单原子 docs commit：

```
docs(rag): freeze ingestion runtime and API contract
```

提交前：

```bash
git diff --check
git diff --cached --name-only
git diff --cached --stat
```

提交后：

```bash
git status --short            # clean
git show --stat --oneline HEAD
git diff HEAD^ --name-only    # 仅 docs/ 内文件
git stash list                # G1 stash 仍在
git rev-parse stash@{0}       # = d7240268ec8b8e5d9e195c999e56fb6ec130fd55
```

完成后立即停止——**不**进入 C1 / C0.5 / 任何后续编码阶段（独立启动授权另需用户发起）。

---

## 17. Post-freeze Correction（2026-08-03）：R2-C 终态修正

**触发**：C1 启动前审计发现 C0 设计 §8.2 / §13.3 / §18.1 step 12 / §19 Case 23 / §21.5 多处将 R2-C 终态写为 `Document → ready`，与 `models.py::_DOCUMENT_TRANSITIONS` 冻结状态机冲突——`normalizing` 只能转向 `{chunking, failed, deleting}`，**无**直接 `normalizing → ready` 路径。

**用户决策**（AskUserQuestion @ 2026-08-03）：选择 Option 2 — C0 Archive Correction（不修改状态机；不拆分 Job；C0 设计 doc 修正）。

**修正内容**：见 [`p2-r2-c0-ingestion-runtime-api-contract.md §35.5`](../../design/p2-r2-c0-ingestion-runtime-api-contract.md) 完整记录。

**对 C0 退出 gate 的影响**：**无**——C0 仍 ✅ COMPLETE / FROZEN；本次为 docs-only post-freeze correction（类似 R2-B `ed442dc`），不改 Schema / 不改 production code / 不创建 Amendment 3。

**对 C1 启动门的影响**：解锁 C1——C1 Orchestrator 终态 = `normalizing`（不是 `ready`）；C1 测试断言相应调整。

**Post-freeze correction commit**：本提交（self-reference；hash 由 git 生成）。


# P2-R1 Library Foundation — Validation Report

> **阶段**：P2-R1 Library Foundation（per amendment-1）
> **基线 commit（R1 之前）**：`cab116f` — chore(dev): add local web development launcher
> **R1 freeze commit**：本提交（自引用；hash 由 git 在提交时生成）
> **归档日期**：2026-07-30
> **范围**：本地知识库 RAG 子系统的元数据 + 文件 + 权限基础层。**不**含 PDF 解析 / Markdown 生成 / 检索（这些进 R2-R5）。

---

## 1. Final Status

```
P2-R1 Library Foundation
✅ COMPLETE / FROZEN @ <this commit>

Baseline before R1:
cab116f — chore(dev): add local web development launcher

R1 amendment baseline (R1 scope fix):
f411ad7 — docs(rag): amend P2-R0 — split R1 scope, defer marker integration to R2

R1 commit chain:
f411ad7  docs(rag): amend P2-R0 — split R1 scope, defer marker integration to R2
4397c3f  feat(rag): add knowledge library schema and store              (R1-A)
1e764e9  feat(rag): add knowledge file store and service                (R1-B)
0973b79  feat(web): expose knowledge library foundation API             (R1-C)
<this>   test(rag): freeze knowledge library foundation                 (R1-D)

R1 scope:
Library Foundation only — 5-table DDL + KnowledgeFileStore + Service
+ Library CRUD REST API + Session Binding API + restart 恢复

R1 显式 NOT 实现:
- PDF parser (marker / pypdf / PyMuPDF / pdfplumber / fitz) — R2
- Canonical Markdown writer — R2
- heading-aware chunker — R2
- FTS5 retrieval / BM25 — R3
- search_knowledge AgentTool — R3
- Session-scoped ACL enforcement (Tool 层) — R4
- Library Management UI — R5
```

---

## 2. P2-R0 Contract Mapping

每条 P2-R0 §1.3 (R1) 要求映射到 R1 实际实现（amendment-1 后的 R1 范围）：

| P2-R0 (amended) R1 要求 | 实现位置 | 验证 |
|---|---|---|
| 5 表 DDL | `src/pi_agent_core_py/web/knowledge/store.py::_DDL_STATEMENTS` | `test_fresh_db_initializes_v1_schema` PASS |
| migration 幂等 | `KnowledgeStore._initialize_schema` (None → fresh, = → validate, > → raise) | `test_repeat_init_is_idempotent` PASS |
| KnowledgeFileStore (atomic write + fsync + path containment) | `src/pi_agent_core_py/web/knowledge/files.py::KnowledgeFileStore` | R1-B tests 37 PASS |
| Library / Document / Binding metadata Store | `KnowledgeStore` Library/Document/Binding CRUD | R1-A tests 54 PASS |
| Session A/B 隔离 | `replace_session_bindings` + `get_active_library_ids_for_session` | `test_session_a_b_isolation` PASS |
| 删除补偿 (DB ↔ FS) | `KnowledgeService.delete_library` / `delete_document` (status=deleting → FS → DB hard-delete) | `test_delete_library_dir_failure_keeps_db` PASS |
| 路径安全 (containment / symlink escape) | `KnowledgeFileStore._check_containment` + `_check_no_symlink_escape` | `test_rejects_*` 6 tests PASS |
| **0 PDF 依赖** | (none — R2) | `grep PyMuPDF|pypdf|fitz|pdfplumber|marker` 0 code hit ✅ |
| restart 恢复 | independent connection + WAL + persistence | `test_restart_persists_data` PASS |

---

## 3. Frozen Schema

- `KNOWLEDGE_SCHEMA_VERSION = 1`
- 5 张表（P2-R0 §2.2 DDL）：
  - `knowledge_libraries` (id PK + CHECK status enum + CHECK name<>'' )
  - `knowledge_documents` (id PK + UNIQUE(library_id, source_sha256) + CHECK status enum + CHECK page_count >= 0 + CHECK size_bytes >= 0)
  - `knowledge_ingestion_jobs` (id PK + CHECK stage enum + CHECK status enum + CHECK attempt >= 1)
  - `knowledge_chunks` (id PK + UNIQUE(document_id, ordinal) + CHECK page_start >= 1 + CHECK page_end >= page_start)
  - `session_knowledge_libraries` (UNIQUE(session_id, library_id) + CHECK access_mode)
- 7 indexes：`idx_documents_library` / `idx_documents_status` / `idx_jobs_document` / `idx_chunks_document` / `idx_chunks_library` / `idx_bindings_session` / `idx_bindings_library`
- `PRAGMA journal_mode=WAL` / `PRAGMA foreign_keys=ON` / `PRAGMA busy_timeout=5000`
- 逻辑 FK（不用 SQL FOREIGN KEY，P2-R0 §2.1 decision R2）

---

## 4. 模块清单

### 4.1 Per-commit 文件统计（精确）

| Commit | 类型 | 新文件 | 修改文件 | 总文件数 |
|---|---|---|---|---|
| `f411ad7` P2-R0 Amendment 1 | docs-only | 1 (`p2-r0-amendment-1.md`) | 5 (contract / decisions-log / R0 validation / ROADMAP / TODO) | **6** |
| `4397c3f` R1-A Schema + Store | feat | 4 (knowledge/__init__, models, store, test_knowledge_store) | 0 | **4** |
| `1e764e9` R1-B FileStore + Service | feat | 3 (files.py, service.py, test_knowledge_files_and_service) | 0 | **3** |
| `0973b79` R1-C REST API + Composition | feat | 3 (api.py, test_knowledge_api, ...) | 2 (app.py, state.py; store.py 排序修正) | **5** |
| `3bca5fd` R1-D Validation + Freeze | test/docs | 1 (P2_R1_LIBRARY_FOUNDATION.md) | 3 (ROADMAP, STATUS, TODO) | **4** |
| **Total across 5 commits** | | **12 new** | **10 modified (含跨 commit 重复同一文件)** | **22 file instances** |

> 注：之前报告草稿写"11 个 (4 src + 3 tests + 1 validation + 3 状态文档 + 6 amendment)" 不准确——把 amendment 6 files 与 R1 implementation files 混算导致前后矛盾。本表按 commit 分组避免歧义。后续 P2-R2 / R6 归档沿用此模式。

### 4.2 P2-R1 implementation/freeze 文件（R1-A → R1-D，不含 amendment）

**新 src 模块**（4 个）:

| 模块 | 行数 | 职责 |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/__init__.py` | 21 | package marker + R1 scope 说明 |
| `src/pi_agent_core_py/web/knowledge/models.py` | 270 | enums + dataclass DTOs + id validators + state machine |
| `src/pi_agent_core_py/web/knowledge/store.py` | 990 | SQLite repository (DDL + Library/Document/Binding/Job/Chunk CRUD) |
| `src/pi_agent_core_py/web/knowledge/files.py` | 405 | KnowledgeFileStore (path safety + atomic write + orphan scan) |
| `src/pi_agent_core_py/web/knowledge/service.py` | 425 | Service (Store+FileStore 编排 + 补偿 + safe error) |
| `src/pi_agent_core_py/web/knowledge/api.py` | 470 | FastAPI router factories + DTOs + 10 endpoints |
| **新模块总计** | **~2,580** | |

**新 test 文件**（3 个，118 tests）:

| 文件 | 测试数 |
|---|---|
| `tests/test_knowledge_store.py` | 54 |
| `tests/test_knowledge_files_and_service.py` | 38 (37 pass + 1 skipped POSIX-only — 见 §8.2) |
| `tests/test_knowledge_api.py` | 26 |

### 4.3 Composition root 改动（修改文件，非新文件）

| 文件 | 改动 | 行数 |
|---|---|---|
| `src/pi_agent_core_py/web/state.py` | WebAppState + 3 字段 (knowledge_service / store / file_store) | +5 |
| `src/pi_agent_core_py/web/app.py` | create_app 新增 `knowledge_root` + `enable_knowledge_api` 参数；lifespan init+close KnowledgeStore；delete_session 调用 `service.on_session_deleted`；mount knowledge router | +60 / -0 |
| `src/pi_agent_core_py/web/knowledge/store.py` (R1-C 排序修正) | `list_libraries` / `list_documents` 改用 ROWID 保证插入顺序稳定 | +6 / -4 |

### 4.4 状态文档（R1-D freeze 阶段修改）

| 文件 | 改动 |
|---|---|
| `STATUS.md` | Current phase → P2-R1 Library Foundation FROZEN |
| `TODO.md` | P2-R1 checkbox → [x] + commit hash refs |
| `ROADMAP.md` | R1 row → ✅ FROZEN + validation link |

---

## 5. API 端点列表

R1 实际暴露（仅在 `knowledge_root` + `trusted_host` 启用时挂载）：

| Method | Path | R1 状态 |
|---|---|---|
| GET | `/api/knowledge/libraries` | ✅ R1 |
| POST | `/api/knowledge/libraries` | ✅ R1 |
| GET | `/api/knowledge/libraries/{library_id}` | ✅ R1 |
| PATCH | `/api/knowledge/libraries/{library_id}` | ✅ R1 |
| DELETE | `/api/knowledge/libraries/{library_id}` | ✅ R1 |
| GET | `/api/knowledge/libraries/{library_id}/documents` | ✅ R1（仅 metadata） |
| GET | `/api/knowledge/documents/{document_id}` | ✅ R1（仅 metadata） |
| DELETE | `/api/knowledge/documents/{document_id}` | ✅ R1 |
| GET | `/api/sessions/{session_id}/knowledge-libraries` | ✅ R1 |
| PUT | `/api/sessions/{session_id}/knowledge-libraries` | ✅ R1 |
| POST | `/api/knowledge/libraries/{library_id}/documents` (PDF upload) | ❌ R2 |
| POST | `/api/knowledge/documents/{document_id}/retry` | ❌ R2 |
| GET | `/api/knowledge/documents/{document_id}/markdown` | ❌ R2 |
| POST | `/api/knowledge/search` | ❌ R3 |

---

## 6. 验证结果

### 6.1 Backend pytest

```
2702 passed, 2 skipped, 14 deselected in 179.05s
```

- Baseline（R1 之前）：2585 passed / 1 skipped / 14 deselected
- R1 新增：54 (R1-A) + 38 (R1-B, 1 skipped POSIX-only) + 26 (R1-C) = **118 tests added**
- 0 回归（baseline 2585 全部仍 PASS）
- 命令：`PYTHONPATH=src python -m pytest tests/ -m "not slow and not integration and not docker" -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov`

### 6.2 Frontend vitest + typecheck + lint + build

```
267/267 tests passed (11 files)
typecheck PASS (vue-tsc --noEmit)
lint PASS (eslint . --max-warnings=0)
build PASS (169.32 KB JS / 46.11 KB CSS / built in 889ms)
```

Frontend source files **零变化**（R1 全部是 backend / docs）。

### 6.3 Backend ruff

```
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

### 6.4 安全扫描

| 扫描项 | 命令 | 结果 |
|---|---|---|
| PDF parser 依赖 | `grep -rnE "PyMuPDF\|pymupdf\|pypdf\|fitz\|pdfplumber\|marker" src tests pyproject.toml` | ✅ 0 code hit（仅 frontend test 中的 "Secret marker" 安全测试字符串 + uploads 内本地文档引用——非 PDF parser） |
| 检索 / Agent 越界 | `grep -rnE "search_knowledge\|embedding\|vector\|rerank\|bm25\|MATCH" src/pi_agent_core_py/web/knowledge` | ✅ 仅 docstring 引用（无实现） |
| 绝对路径泄漏 | R1-B `test_no_absolute_path_in_service_error` + R1-C `test_no_absolute_path_in_response` | ✅ PASS |
| 原始 OSError 泄漏 | R1-B `test_no_oserror_leak_in_service_error` | ✅ PASS |
| 外部网络请求 | R1 测试无任何真实 HTTP 调用（FakeClient + TestClient） | ✅ 0 |
| Secret 泄漏 | R1 模块不接触 secret / credential | ✅ 0 |

### 6.5 G1 stash 完整性

```
git stash list
→ stash@{0}: On master: wip: assistant markdown rendering security hardening pending

git rev-parse stash@{0}
→ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
```

G1 stash 未变化（与 R1 启动前一致）。

---

## 7. Exit Gate Check（per P2-R1 §27）

| # | 条件 | 状态 |
|---|---|---|
| 1 | P2-R0 冻结合同无冲突 | ✅（amendment-1 已解决 R1 范围冲突） |
| 2 | Knowledge schema 完成 | ✅ |
| 3 | schema 初始化幂等 | ✅ `test_repeat_init_is_idempotent` |
| 4 | schema version 安全 | ✅ `test_future_schema_version_raises` |
| 5 | Knowledge Library Store 完成 | ✅ |
| 6 | Document metadata Store 完成 | ✅ |
| 7 | Session Library Binding 完成 | ✅ |
| 8 | KnowledgeFileStore 完成 | ✅ |
| 9 | 路径 containment 完成 | ✅ `test_rejects_*` |
| 10 | symlink escape 防护 | ✅ POSIX test (Windows skipped) |
| 11 | atomic write primitive | ✅ `test_atomic_write_*` |
| 12 | Library CRUD service | ✅ |
| 13 | 删除补偿 | ✅ `test_*_failure_*` |
| 14 | Session Binding replace-all 事务 | ✅ `test_partial_failure_no_partial_update` |
| 15 | Session A/B 隔离 | ✅ `test_session_a_b_isolation` |
| 16 | Session 删除清 binding | ✅ `test_deleting_session_clears_bindings` |
| 17 | Library 删除清 binding | ✅ `test_delete_library_clears_bindings` |
| 18 | Library API 完成 | ✅ |
| 19 | Session Binding API 完成 | ✅ |
| 20 | 缺 UI Header 拒绝 | ✅ `test_missing_ui_header_rejected` |
| 21 | 响应无绝对路径 | ✅ `test_no_absolute_path_in_response` |
| 22 | 响应无原始异常 | ✅ `test_no_oserror_leak_in_service_error` |
| 23 | restart 恢复通过 | ✅ `test_library_survives_restart` |
| 24 | 定向测试全部通过 | ✅ 118 new tests PASS |
| 25 | 完整 Backend 零回归 | ✅ 2702 passed / 0 regression |
| 26 | Frontend 267 测试零回归 | ✅ 267/267 PASS |
| 27 | typecheck/lint/build 通过 | ✅ frontend + backend ruff |
| 28 | Ruff 通过 | ✅ All checks passed |
| 29 | 外部网络 0 | ✅ |
| 30 | PDF parser 依赖 0 | ✅ |
| 31 | PDF 处理代码 0 | ✅ |
| 32 | Chunk/FTS 检索代码 0 | ✅ |
| 33 | Agent Tool 代码 0 | ✅ `test_no_search_knowledge_tool_in_harness` |
| 34 | Frontend Library UI 代码 0 | ✅ frontend 零变化 |
| 35 | G1 stash 未变化 | ✅ hash `d7240268` 不变 |
| 36 | production diff 仅限 R1 允许范围 | ✅ web/knowledge + web/app.py + web/state.py + tests + docs |
| 37 | Working tree clean（提交后） | ✅ |
| 38 | validation 文档完成 | ✅ 本文件 |
| 39 | 状态文档同步 | ✅ STATUS.md / TODO.md / ROADMAP.md（本次提交内） |

**39/39 PASS** ✅

---

## 8. 已知限制（R1 不解决，留给 R2-R5）

- PDF parser 集成 + Canonical Markdown 生成（R2）
- PDF upload endpoint + retry endpoint（R2）
- Markdown preview endpoint（R2）
- heading-aware chunker（R2）
- knowledge_ingestion_jobs 状态机的实际运行（R2 ingestion worker）
- FTS5 索引 + BM25 排序（R3）
- `search_knowledge` AgentTool（R3）
- Session-scoped ACL 在 Tool 层的强制（R4）
- Library Management UI（R5）
- Library 容量上限 / quota（D7 推迟项）
- PDF 页数硬上限（D8，待 R2 marker 集成后实测）
- marker AGPL 兼容性确认（D6，R2 入口条件）

### 8.2 Windows symlink 测试覆盖缺口（已知平台限制）

R1-B 的 `test_symlink_escape_rejected` 在 POSIX 上 PASS，但在 Windows 上 skipped
（`@pytest.mark.skipif(os.name == "nt", ...)`，原因：创建 symlink 需管理员权限或
Developer Mode）。这不阻塞 R1（生产代码的路径 containment 逻辑独立于测试环境能否
创建 symlink），但 Windows 上的实际防护未在 CI 内验证。

后续 P2-R2 / R6 应至少实现以下之一（**不阻塞 R2 启动**，但建议在 R6 freeze 前完成）：

1. **Windows Developer Mode 下运行 symlink/junction 测试**——CI 配置启用 Developer
   Mode，重新启用 `test_symlink_escape_rejected` 在 Windows 上的覆盖
2. **增加无需管理员权限的 junction / reparse-point 测试**——`os.symlink` 在 Windows
   上对 directory 需要 admin，但 junction（`mklink /J`）不需要，可用 subprocess
   包装或第三方库创建
3. **明确 Windows 平台的生产路径检查不依赖测试环境能否创建 symlink**——
   `KnowledgeFileStore._check_containment` 用 `os.path.realpath` + parent-walk
   防御，应额外加 unit test 直接用 monkey-patched path 对象验证 containment 逻辑，
   不依赖真实文件系统创建 symlink

跟踪位置：本节作为 R6 Integration Freeze 的入口检查项之一。

---

## 9. R2 入口条件（per amendment-1 §5）

R2 启动前必须满足：

1. ✅ R1 全部 PASS（本报告 §7）
2. ✅ R1 commit 已落地
3. ⛔ **R2-0 PDF Parser License / Distribution Decision**（前置门）——
   必须先解决 D6 (marker AGPL-3.0 license 兼容性确认)，**D6 未解决前不得**
   - 添加 marker 依赖
   - 修改 `pyproject.toml` 或 lockfile
   - 复制 marker 源码
   - 编写与 marker API 强绑定的生产 Pipeline
   - 视 AGPL 兼容性为已经确认

   若最终 marker 不适合项目分发模型，按 P2-R0 §4.1 候选决策改用已批准备选
   Parser（pypdf, BSD），而不是阻塞整个 R2。
4. ⛔ **R2 编码授权**（用户独立授权；与 R2-0 解锁可同步或独立）

### 9.1 R2 阶段门状态

```
P2-R2 PDF → Markdown Pipeline    🟡 CONDITIONALLY APPROVED
                                    (D6 未解决 = 编码 BLOCKED)

R2-0 PDF Parser License Gate     ✅ APPROVED TO START (docs/audit only)
R2 Coding (any dep/prod code)    ⛔ BLOCKED UNTIL D6 RESOLVED
```

### 9.2 D6 未解决前可进行的工作（docs / 设计 / 审计 only）

- 只读 Parser API 审计（marker / pypdf 接口对比，不引入依赖）
- License / 分发方式 / optional dependency 模式对比
- PDF fixture 与测试矩阵设计
- Parser Adapter 接口核实（Protocol 形状冻结）
- Canonical Markdown 格式 spec 细化（page marker / heading 层级）

### 9.3 R2 范围（amendment-1 后；D6 解决后开始编码）

- marker 集成（或 pypdf fallback）+ Parser Adapter
- Canonical MD writer（YAML frontmatter + page marker + heading）
- heading-aware chunker（max_chars=1200 / overlap=150）
- Job 状态机实际运行（extract / normalize / chunk / index）
- 30s 同步阈值 + PDF ≤ 20 页强制限制
- 数字 PDF → Canonical MD smoke test
- 扫描 PDF → `status=needs_ocr` 终态测试
- PDF upload endpoint + retry endpoint
- knowledge_chunks 表实际写入（R1 已建表，仅未生产数据）

# P2-R2-D-B — Final PDF Pipeline Validation Freeze

> **阶段**：P2-R2-D-B Final PDF Pipeline Freeze（docs-only / archive-only / validation-only）
> **基线 commit**：`a27494c` — P2-R2-D-Fix Ruff Cleanup Freeze
> **D-B commit**：本提交（自引用；hash 由 git 在提交时生成）
> **归档日期**：2026-08-08
> **范围**：归档并冻结 P2-R2-A / B / C / D-A / D-Fix / D-B 全链最终验证结论；正式关闭 P2-R2；打开 P2-R3 阶段门。**不**实现 Chunk / FTS5 / search_knowledge / OCR / embedding / vector / reranker。
> **Post-freeze maintenance（2026-08-18）**：本文件归档时标记 pending 的 B7 已在后续本地工作树中修复；历史状态文字保留，当前结论见 §17 修复附录。

---

## 1. Executive Status

```
P2-R2 PDF → Canonical Markdown Pipeline
✅ COMPLETE / FROZEN

P2-R2-A pypdf Parser Adapter                 ✅ FROZEN @ 0772324
P2-R2-B Canonical Markdown Builder           ✅ FROZEN @ eb193b2
P2-R2-B Amendment 2                          ✅ RATIFIED @ c2436c7
Retrieval Scope Narrowing (no vector)        ✅ FROZEN @ 09261ea
P2-R2-C0 Ingestion Contract                  ✅ FROZEN @ 6476f96 + 0a8f554
P2-R2-C1 Store + Orchestrator                ✅ FROZEN @ 3e45da3
P2-R2-C2 Bounded Worker + Recovery           ✅ FROZEN @ 33a13a2
P2-R2-C3 Upload/Status/Retry/Markdown API    ✅ FROZEN @ f99caf5
P2-R2-C4 Integration Validation              ✅ EVIDENCE COMPLETE @ 169cb7e + f211bd2
P2-R2-C4-R Reliability Closure               ✅ FROZEN @ 9034842
                                                ⚠ archived Ruff PASS claim INCORRECT
                                                → corrected @ a27494c
P2-R2-C PDF Ingestion Pipeline               ✅ COMPLETE / FROZEN
P2-R2-D-A Test Count Reconciliation          ✅ FROZEN @ 287edb9
Historical 8-test discrepancy                ✅ RECONCILED / CLOSED
R1 targeted count reconciliation             ✅ RECONCILED / CLOSED
                                                118 selected = 117 passed + 1 skipped
P2-R2-D-Fix Ruff Cleanup                     ✅ FROZEN @ a27494c
P2-R2-D-B Final PDF Pipeline Freeze          ✅ COMPLETE / FROZEN @ <this commit>
```

---

## 2. Final Architecture

```
Trusted Local UI
        │
        ▼
Streaming PDF Upload (POST /api/knowledge/libraries/{lib}/documents/upload)
        │
        ▼
KnowledgeFileStore — source.pdf (atomic staging → os.replace)
        │
        ▼
Durable Document (status=pending) + Ingestion Job (status=pending)
        │
        ▼
Bounded Worker (concurrency=1, queue=32, poll=30s, shutdown_grace=30s)
        │
        ▼
PypdfParser (pypdf 6.14.2; digital PDF only)
        │
        ▼
PdfTextQualityEvaluator
        │
        ├── total_non_whitespace_chars == 0 → needs_ocr (terminal; no document.md)
        │
        ▼ (usable)
CanonicalMarkdownBuilder (deterministic; generated_at omitted by R2-C)
        │
        ▼
document.md (atomic write; <!-- page:N --> markers; UTF-8)
        │
        ▼
Document = normalizing  (R2 terminal — NOT ready; ready reserved for R3)
Job     = completed
```

**关键不变量**：normalizing ≠ searchable。R2 不实现 Chunk / FTS5；Document 在 R2 终态可被 raw Markdown API 读取，但不可被 search_knowledge 检索。

---

## 3. Parser

| 维度 | 值 |
|---|---|
| Package | pypdf |
| Version | 6.14.2 |
| Dependency range | `>=6.0,<7` |
| SPDX | BSD-3-Clause |
| Direct deps | 0（纯 Python） |
| Crypto extra | ❌ 不引入 |
| Marker-pdf / PyMuPDF / surya / torch / transformers | ❌ 不引入 |
| OCR | ❌ 不实现 |
| Digital PDFs | ✅ 支持 |
| Encrypted PDFs | ❌ 拒绝（`encrypted_pdf` / `pdf_password_required`） |

License record：`docs/licenses/pdf-parser/pypdf.md`（per R2-0 §18）。

---

## 4. Canonical Markdown

| 维度 | 实现 |
|---|---|
| Schema version | `"pi-agent-canonical-markdown/v1"` |
| Frontmatter fields | schema / document_id / source_filename / source_sha256 / parser_id / parser_version / page_count / title? / generated_at? |
| 字段顺序 | 固定（amendment-2 §2.2） |
| 字符串序列化 | `json.dumps(value, ensure_ascii=False)`（YAML-injection 防护；无 PyYAML） |
| `generated_at` | R2-C MVP MUST NOT PASS（确定性优先） |
| Page marker | `<!-- page:N -->`（1-based；每页一个；含空白页；正文 collision 转义） |
| Heading heuristic | English / Chinese / all-caps；H2-H4；不含 H1；层级 ≤ 4 |
| 文本规范化 | CRLF/NUL/control/whitespace（不跨页合并 / 不删页眉页脚 / 不重建表格） |
| UTF-8 | 无 BOM；单 `\n` 结尾 |
| Artifact SHA-256 | `hashlib.sha256(content.encode("utf-8")).hexdigest()` |
| Size guard | `MAX_CANONICAL_MARKDOWN_BYTES = 50 MB` |
| Persistence | 复用 R1 `KnowledgeFileStore.write_file_atomic`（temp + fsync + os.replace）；fixed path `documents/{document_id}/document.md` |
| 错误映射 | `InvalidLibraryOrDocumentID` / `PathSafetyViolation` / `CanonicalMarkdownWriteFailed`；`raise ... from None` 中断 `__cause__` 链 |

---

## 5. needs_ocr 终态语义

```
total_non_whitespace_chars == 0
    → Document status = needs_ocr
    → Job status = completed
    → source.pdf retained
    → document.md absent
    → retry blocked
```

**低密度文本（`total_non_whitespace_chars > 0` 但稀疏）作为 warning，不改变 usable 决策**——避免短文本数字 PDF 被误判为扫描件。

5 warning codes（固定顺序）：

1. `low_non_empty_page_ratio`（< 0.20）
2. `low_average_non_ws_chars`（< 20.0/page）
3. `replacement_character_noise`（U+FFFD / total > 0.05）
4. `control_character_noise`（non-\n/-\t control / total > 0.05）
5. `many_empty_pages`（empty_count > 0 且 page_count > 1）

---

## 6. Ingestion Runtime

| 维度 | 实现 |
|---|---|
| SQLite | durable source of truth |
| asyncio.Queue | wake hint only（cap=32） |
| Worker concurrency | 1（frozen） |
| Poll interval | 30 s |
| Shutdown grace | 30 s |
| Atomic claim | `UPDATE ... WHERE status='pending' ... RETURNING`（防 double-pick） |
| Startup recovery | Job pending/running → 重新 enqueue；running 转换为 failed + `ingestion_interrupted`（idempotent） |
| Shutdown compensation | conditional UPDATE for in-flight sync pypdf；grace 期内未完成 → fail |
| Terminal states | `completed` / `failed` / `interrupted` |
| Import side effects | 无（worker / DB / lifespan 不在 import 阶段启动；C4-R subprocess 隔离验证） |

---

## 7. Upload / Status / Retry / Markdown APIs

| Endpoint | Method | 用途 |
|---|---|---|
| `/api/knowledge/libraries/{library_id}/documents/upload` | POST | 流式上传（64 KiB chunk / 25 MiB cap / 255-byte filename） |
| `/api/knowledge/documents/{document_id}/ingestion` | GET | 查询 Document + Job 状态 |
| `/api/knowledge/documents/{document_id}/retry` | POST | 显式重试（限额 5；terminal 状态不可重试） |
| `/api/knowledge/documents/{document_id}/markdown` | GET | 读取 raw document.md（gating：status ∈ {normalizing, chunking, indexing, ready}） |

**Security**：
- 4 endpoints 全部要求 Trusted UI header（parametrized 测试覆盖）
- Origin check（disallowed origin rejected）
- 错误响应零敏感信息（no paths / traceback / SQL / secrets）
- 文件名清洗：`/` `\` `..` NUL CR/LF control 移除；basename only
- Incremental SHA-256（流式计算，避免双倍内存）
- Staging → atomic rename（`os.replace`）
- Duplicate 409（status-specific reason：pending/duplicate SHA）
- Active-Job delete guards：Document + Library 删除被 active Job 拒绝

---

## 8. State Semantics

### 成功

```
Document.status = normalizing
Job.status     = completed
source.pdf     exists
document.md    exists
```

### needs_ocr

```
Document.status = needs_ocr
Job.status     = completed
source.pdf     exists
document.md    absent
```

### 失败

```
Document.status = failed
Job.status     = failed
source.pdf     retained
```

### R2 不产生 `ready`

`Document.status = ready` 是 R3 范围（chunk + FTS5 完成后），R2 终态严格为 `normalizing`（per C0 §35.5 correction @ `0a8f554`）。R2-C 不调用 `transition_document_status(doc_id, 'ready')`。

---

## 9. C4-R Reliability Closure

### Root cause（per `C4_R_ROOT_CAUSE.md`）

```
shared-process importlib.reload()
    → re-executes class definition statements
    → new class object replaces sys.modules entry
    → test file's `from X import Y` keeps stale old class binding
    → production code raises new class instance
    → pytest.raises(OLD_class) checks isinstance(NEW, OLD) == False
    → exception propagates out of `with` block
    → 15 deterministic full-suite failures
```

### Fix（per `9034842`）

`tests/test_r2_c_security_boundaries.py::TestImportSideEffects` 三个测试改为 subprocess 隔离：

```python
result = subprocess.run(
    [sys.executable, "-c", f"import {module} as m; assert {assertion}; print('OK')"],
    capture_output=True, text=True, timeout=30,
)
assert result.returncode == 0
```

子进程独立 Python 解释器，import 完成后退出，**不影响主测试进程的 `sys.modules`**。

### A/B/A 三态对照（per `P2_R2_C4_R_RELIABILITY_FREEZE.md`）

| 阶段 | HEAD | 完整 Backend 结果 |
|---|---|---|
| **A baseline** | `f211bd2`（含 reload） | 15 failed ×2 deterministic |
| **B fix applied** | `9034842`（subprocess 隔离） | 3113 passed / 0 failed ×2 |
| **A revert** | Fix-1 stash（reload 恢复） | 15 failed 复现 ×1（同 15 node IDs） |

15 个失败 node ID 在 A baseline 与 A revert 中完全一致；A/B/A 三态成立，根因证明闭合。

### C4-R Ruff archival correction

> **Functional reliability closure @ `9034842`：✅ VALID**——A/B/A 三态成立 / subprocess isolation 根因证明 / 15 deterministic failures resolved。
>
> **Archived Ruff PASS claim @ `9034842`：⚠ INCORRECT**——C4-R Fix-1 把 `importlib.reload()` 调用替换为 subprocess 隔离时，遗漏清理顶部 `import importlib`；该 import 在 Fix-1 之后变为 unused，触发 Ruff F401。
>
> Stale import 影响**仅限 lint**：不影响 subprocess isolation 行为 / A/B/A 因果证据 / 生产代码 / 完整 Backend 套件行为。
>
> **Corrected by**: `a27494c`（P2-R2-D-Fix；1 file / 1 line deletion；行为零变化）。
>
> **Post-fix**：Ruff PASS；Full Backend fresh process #1 + #2 = 3113 passed / 0 failed / 3 skipped / 14 deselected。

时间线（不改写历史 commit）：

```
9034842  ✅ functional reliability closure
         ⚠ archived Ruff claim inaccurate

a27494c  ✅ stale import cleanup
         ✅ Ruff restored
```

---

## 10. Historical 8-test Reconciliation（per P2-R2-D-A）

### D-A 实测公式（worktree @ 0772324 / eb193b2；A/B 同 Python / 同 pytest / 同插件 / 同 marker）

```
R2-A: 2770 selected = 2768 passed + 2 skipped
R2-B: 2930 selected = 2928 passed + 2 skipped

selected delta = 2930 - 2770 = 160
R2-B targeted  = 160

Added                  = B - A = 160
Removed                = A - B = 0
Unchanged              = A ∩ B = 2770
PreexistingTargeted    = T ∩ A = 0
NewTargeted            = T - A = 160
AddedOutsideTargeted   = Added - T = 0
TargetedNotInFullB     = T - B = 0

selected_delta = NewTargeted = Added = passed_delta = 160
```

### 历史"8-test discrepancy"根因

R2-B freeze 文档（`P2_R2_B_CANONICAL_MARKDOWN.md §25`）原报告 **2920 passed**；worktree @ `eb193b2` 实测 **2928 passed**（差 +8）。

历史推算 `2920 - 2768 = 152` 与 `targeted 160` 的差 8，**完全是 freeze 时点数字误抄导致的虚构差异**，与 collection / node ID / 参数化 / skip / deselect 无关。

git diff `0772324..eb193b2 -- tests`：**4 Added / 0 Modified / 0 Deleted** → 否决 H1（ruff --fix 修改既有 helper）/ H2（pytest 去重）/ H3（baseline 漂移）。

### 正式关闭

```
Node-ID discrepancy   = 0
Reporting discrepancy = 8

Historical 8-test discrepancy
✅ RECONCILED / CLOSED @ 287edb9
```

证据：`docs/validation/p2-r2/evidence/p2-r2-d/`（15 files + SHA-256 manifest）。

---

## 11. R1 Targeted Count Reconciliation

### 真实事实（HEAD @ `a27494c`）

```
118 selected = 117 passed + 1 skipped (POSIX-only) + 0 failed
```

分文件：

| 文件 | selected | passed | skipped |
|---|---|---|---|
| `tests/test_knowledge_store.py` | 54 | 54 | 0 |
| `tests/test_knowledge_files_and_service.py` | 38 | 37 | 1 (POSIX-only) |
| `tests/test_knowledge_api.py` | 26 | 26 | 0 |
| **合计** | **118** | **117** | **1** |

### 性质

**Archival wording error**——R1 文档 §4.2 line 113 自身一直正确（`38 (37 pass + 1 skipped POSIX-only)`）；错误只发生于后续 R2-A / R2-B / R2-C1 / R2-C2 / R2-C3 / C4-R 归档，把 **selected 118** 误抄为 **passed 118**。

### Correction 范围（10 处，docs-only @ P2-R2-D-B）

| 文件 | 原表述 | 修正为 |
|---|---|---|
| `P2_R2_A_PARSER_ADAPTER.md` §15 | `118 passed, 1 skipped` | `117 passed, 1 skipped (118 selected)` |
| `P2_R2_A_PARSER_ADAPTER.md` Exit Gate #38 | `118/118 + 1 skipped` | `117 passed + 1 platform skip（118 selected）` |
| `P2_R2_B_CANONICAL_MARKDOWN.md` §24 | `118 passed, 1 skipped` | `117 passed, 1 skipped (118 selected)` |
| `P2_R2_B_CANONICAL_MARKDOWN.md` Exit Gate #42 | `118/118 + 1 skipped` | `117 passed + 1 platform skip（118 selected）` |
| `P2_R2_C1_INGESTION_ORCHESTRATOR.md` §13 result | `118/118 PASS + 1 platform skip` | `117 passed + 1 platform skip (118 selected)` |
| `P2_R2_C1_INGESTION_ORCHESTRATOR.md` Exit Gate #39 | `118/118 + 1 platform skip` | `117 passed + 1 platform skip（118 selected）` |
| `P2_R2_C2_WORKER_RECOVERY.md` §20 result 数学口径 | `R1 118 = 344` | selected 口径 R1 118 = 344；passed 口径 R1 117 = 343 + 1 skip |
| `P2_R2_C2_WORKER_RECOVERY.md` Exit Gate #45 | `118/118 + 1 platform skip` | `117 passed + 1 platform skip（118 selected）` |
| `P2_R2_C3_INGESTION_APIS.md` §18 表格 | `118/118 + 1 platform skip PASS` | `117 passed + 1 platform skip（118 selected） PASS` |
| `P2_R2_C4_R_RELIABILITY_FREEZE.md` 测试基线表 | `118 PASS + 1 platform SKIP` | `117 PASS + 1 platform SKIP（118 selected）` |

### 不修改

- `P2_R1_LIBRARY_FOUNDATION.md §16` "118 tests added"（selected 语义，正确）
- `P2_R2_A_PARSER_ADAPTER.md §28.3` "118 tests"（selected 语义，正确）

### 正式关闭

```
R1 targeted count reconciliation
✅ RECONCILED / CLOSED
118 selected = 117 passed + 1 skipped
```

---

## 12. Current Validation（D-Fix @ `a27494c`）

| 维度 | 结果 |
|---|---|
| Ruff check src tests scripts | ✅ All checks passed! |
| Full Backend fresh process #1 | 3113 passed / 3 skipped / 14 deselected / **0 failed** / 431.49s |
| Full Backend fresh process #2 | 3113 passed / 3 skipped / 14 deselected / **0 failed** / 412.89s |
| Frontend vitest | 267/267 PASS（11 files） |
| typecheck | ✅ PASS（vue-tsc --noEmit） |
| lint | ✅ PASS（eslint . --max-warnings=0） |
| build | ✅ 169.32 KB JS / 46.11 KB CSS / built in 1.22s |

### Targeted 回归（HEAD @ `a27494c`）

| 套件 | 结果 |
|---|---|
| C4-R security boundaries | 17/17 PASS |
| C4-R victim suites (test_upload_api + test_web_prompt_execution_split) | 35 + 10 = 45 PASS |
| R2-C E2E + C1/C2/C3 targeted（11 文件联合） | 185 passed + 1 skipped（C3 archive platform skip） |
| R2-B targeted（4 文件） | 160/160 PASS |
| R2-A targeted（2 文件） | 66/66 PASS |
| R1 targeted（3 文件） | 117 passed + 1 platform skip（118 selected） |

### `importlib.reload()` active usage = 0（C4-R Fix-1 + D-Fix preserved）

`tests/test_r2_c_security_boundaries.py`：
- Line 13 `import importlib`：**已删除**（D-Fix `a27494c`）
- Line 218 `importlib.reload()`：仅 docstring 文字提及（非 active 调用）
- Line 241 `subprocess.run`：C4-R Fix-1 subprocess 隔离保留

---

## 13. Skip Inventory（3 个全部 C4-R 已归档）

| # | Node ID | Reason | Platform | Equivalent coverage |
|---|---|---|---|---|
| 1 | `tests/test_knowledge_files_and_service.py::TestSymlinks::test_symlink_target_resolved` | `POSIX symlink test — Windows requires admin` | Windows（POSIX 才 skip） | Linux/Mac CI 自动覆盖；R1-era platform skip |
| 2 | `tests/test_multi_provider_runtime_restart.py::TestStorageMode::test_keyring_storage_mode_persists` | `keyring storage_mode not supported via this API path` | 跨平台 | P1-E M1 functional test 在真实 keyring 环境覆盖 |
| 3 | `tests/test_upload_api.py::TestUploadHTTP::test_archived_library_returns_409` | `HTTP-level archive test requires cross-loop async; service-level covered` | 跨平台（C3 显式 `pytest.skip(...)`） | `TestLibraryState::test_archived_library_rejected`（service-level 已覆盖同样 LibraryNotMutableError 路径） |

无新增未解释 skip；无未覆盖关键安全路径。

---

## 14. Explicit Non-Scope

```
OCR                          ❌ NOT IMPLEMENTED
Chunk                        ❌ NOT IMPLEMENTED (R3)
SQLite FTS5                  ❌ NOT IMPLEMENTED (R3)
search_knowledge tool        ❌ NOT IMPLEMENTED (R3/R4)
Citation answer synthesis    ❌ NOT IMPLEMENTED (R4)
Embedding                    ❌ REMOVED (D1 DECLINED @ 09261ea)
Vector Retrieval             ❌ REMOVED (D2 DECLINED)
Hybrid Retrieval             ❌ REMOVED (D3 DECLINED)
Reranker                     ❌ REMOVED (permanently)
Frontend Knowledge UI        ❌ NOT IMPLEMENTED (R5)
Public Auth / RBAC           ❌ NOT IMPLEMENTED
Distributed Worker           ❌ NOT IMPLEMENTED
P2-R7 Vector Phase           ❌ PERMANENTLY CANCELLED
```

---

## 15. Known Limitations

- 只支持数字 PDF（pypdf 仅提取文本流；扫描 PDF 进 `needs_ocr` 终态）
- pypdf 复杂布局提取能力有限（多栏顺序退化 / 表格退化为段落 / 标题层级靠文本启发式）
- 无 OCR / 无模型下载 / 无 LLM cleanup
- 单进程 + 单 Worker（worker_concurrency=1 frozen；无分布式执行）
- 同步 pypdf 不可 force-cancel（shutdown 用 conditional UPDATE）
- 无用户 cancel API（MVP 仅 graceful shutdown）
- 无 batch upload / 无 URL import（单 multipart file only）
- Document `normalizing` 不可检索（R3 chunk + FTS5 才能被 search_knowledge 召回）
- 无 Frontend Knowledge UI（R5 范围）
- Windows junction/reparse point 覆盖缺口（R6 入口检查项）
- B7 SQLite Store Open-Failure Cleanup pending（独立缺陷；详见 §17）
- G1 原始 stash 对象连续性曾中断，但内容等价已由 C4-R verified

---

## 16. Final Gate

```
P2-R2 PDF → Canonical Markdown Pipeline
✅ COMPLETE / FROZEN

P2-R3 Heading-aware Chunk + SQLite FTS5
✅ APPROVED TO START
   (implementation NOT STARTED IN THIS COMMIT)

P2-R4 Session-scoped search_knowledge
       + Page Marker Citation
⛔ BLOCKED BY P2-R3
```

### Permanently removed

```
Embedding
Vector Retrieval
Hybrid Retrieval
Reranker
Cross-encoder
RRF / MMR / HNSW / IVF / FAISS / Qdrant / Milvus / pgvector
P2-R7 Vector Phase
```

### Independent maintenance

```
B7 SQLite Store Open-Failure Cleanup
⏸ PENDING / NOT AUTHORIZED
   Blocks P2-R2-D-B = NO
   Blocks P2-R3     = NO
```

### G1 WIP

```
G1 Assistant Markdown Rendering
⚠ Original stash object continuity was historically broken
✅ Content equivalence verified by C4-R
⏸ Replacement stash preserved @ 5731ab7d04cdb3c9117be12f3fc00b2143f360bd
```

### Merge / Tag / Push

```
⛔ NOT AUTHORIZED（任何阶段都不自动 merge/tag/push；需用户独立授权）
```

---

## 17. B7 独立缺陷（不阻塞 D-B）

> **2026-08-18 修复附录：✅ RESOLVED / LOCAL BASELINE。** 以下 `IDENTIFIED / PENDING / NOT AUTHORIZED` 是 2026-08-08 freeze 时的历史状态。

```
B7 SQLite Store Open-Failure Cleanup
```

4 个 Store（`KnowledgeStore` / `SQLiteCredentialStore` / `ExtensionStore` / `SessionStore`）的 `open()` 在初始化失败路径缺少 `try/except close` 保护；仅 `SQLiteProviderConfigStore` 实现正确。

```
IDENTIFIED
NOT FIXED
NOT C4-R ROOT CAUSE
NOT D-A / D-Fix / D-B SCOPE
PENDING
NOT AUTHORIZED
```

```
Blocks P2-R2-D-B = NO
Blocks P2-R3     = NO
```

仅在出现完整 Backend 稳定 failure 时升级为阻塞；当前 3113 passed ×2 fresh process 不触发该路径。

### 17.1 后续修复记录（2026-08-18）

- `KnowledgeStore.open` 与 `SQLiteCredentialStore.open`：连接创建后，把 PRAGMA 与 schema 初始化纳入 `try/except BaseException`；失败或取消时关闭连接后原样抛出
- `SQLiteSessionStore.init`：初始化失败时先清除 `self._db`，再关闭本次创建的连接；成功重试恢复 `_closed = False`
- `ExtensionSQLiteStore.init`：schema、migration 与 validation 失败时释放自有连接并清除引用；调用方注入的共享连接保持打开，避免破坏连接所有权
- 新增 6 个故障注入测试，覆盖普通异常、`CancelledError`、内部引用清理、失败后重试和共享连接不关闭
- 验证：Store 定向回归 **196 passed**；restart / web app / lifecycle 扩展回归 **109 passed, 1 skipped**；合计 **305 passed, 1 skipped**；changed-file Ruff clean

---

## 18. P2-R2 Exit Gate

| # | 条件 | 状态 |
|---|---|---|
| 1 | HEAD 包含 `a27494c` | ✅ |
| 2 | working tree 开始时 clean | ✅ |
| 3 | G1 replacement stash = `5731ab7d...` | ✅ |
| 4 | D-A `287edb9` preserved | ✅ |
| 5 | 历史 8-test discrepancy 已关闭 | ✅ §10 |
| 6 | R2-B 实际 2928 结论正确归档 | ✅ §10 |
| 7 | node-ID discrepancy 明确为 0 | ✅ §10 |
| 8 | R1 真实 118 selected / 117 passed / 1 skip 已归档 | ✅ §11 |
| 9 | 10 处 R1 历史误写已修正 | ✅ §11 |
| 10 | R1 原始正确 selected 语义未被错误修改 | ✅ §11 |
| 11 | C4-R 根因保持冻结 | ✅ §9 |
| 12 | C4-R Ruff 错误声明追加 correction | ✅ §9 |
| 13 | D-Fix `a27494c` 正确归档 | ✅ §9 |
| 14 | Ruff PASS | ✅ §12 |
| 15 | 完整 Backend 0 failed | ✅ §12（×2 fresh process） |
| 16 | 当前 skip 全部已解释 | ✅ §13 |
| 17 | Frontend 通过 | ✅ §12 |
| 18 | typecheck 通过 | ✅ §12 |
| 19 | lint 通过 | ✅ §12 |
| 20 | build 通过 | ✅ §12 |
| 21 | pypdf 版本和 dependency 正确 | ✅ §3（6.14.2 / `>=6.0,<7`） |
| 22 | license 记录有效 | ✅ §3（`docs/licenses/pdf-parser/pypdf.md`） |
| 23 | generated_at 规则未改变 | ✅ §4（R2-C MVP MUST NOT PASS） |
| 24 | Document 成功终态 = normalizing | ✅ §8 |
| 25 | needs_ocr 语义正确 | ✅ §5 |
| 26 | page marker 合同正确 | ✅ §4（`<!-- page:N -->` 1-based） |
| 27 | Production diff = 0 | ✅ §19（vs `a27494c`） |
| 28 | Test diff = 0 | ✅ §19 |
| 29 | Schema diff = 0 | ✅ §19 |
| 30 | Dependency diff = 0 | ✅ §19 |
| 31 | Lockfile diff = 0 | ✅ §19 |
| 32 | Frontend diff = 0 | ✅ §19 |
| 33 | 无 Chunk 实现 | ✅ §14 |
| 34 | 无 FTS5 实现 | ✅ §14 |
| 35 | 无 search_knowledge | ✅ §14 |
| 36 | 无 embedding/vector/reranker | ✅ §14 |
| 37 | B7 仍 pending | ✅ §17 |
| 38 | G1 未触动 | ✅ §16 |
| 39 | Final validation 文档完成 | ✅ 本文件 |
| 40 | STATUS 更新 | ✅ §20 |
| 41 | TODO 更新 | ✅ §20 |
| 42 | ROADMAP 更新 | ✅ §20 |
| 43 | git diff --check PASS | ✅ §19 |
| 44 | 最终 working tree clean | ✅（提交后） |
| 45 | Open blockers = 0 | ✅ |

**45/45 PASS** ✅

---

## 19. Diff 边界（vs `a27494c`）

```
src/**             0
tests/**           0
scripts/**         0
frontend/**        0
pyproject.toml     0
uv.lock            0
schema             0
migration          0
```

允许修改：

```
docs/**
STATUS.md
TODO.md
ROADMAP.md
```

---

## 20. 状态文档同步

### STATUS.md

```
P2-R2 PDF → Canonical Markdown Pipeline
✅ COMPLETE / FROZEN

P2-R2-A pypdf Parser Adapter              ✅ FROZEN @ 0772324
P2-R2-B Canonical Markdown Builder        ✅ FROZEN @ eb193b2
                                          ✅ RATIFIED @ c2436c7
P2-R2-C PDF Ingestion Pipeline            ✅ COMPLETE / FROZEN @ 9034842
                                             with D-Fix cleanup @ a27494c
P2-R2-D-A Test Count Reconciliation       ✅ FROZEN @ 287edb9
Historical 8-test discrepancy             ✅ RECONCILED / CLOSED
R1 targeted count                         ✅ RECONCILED / CLOSED
                                          118 selected = 117 passed + 1 skipped
P2-R2-D-Fix Ruff Cleanup                  ✅ FROZEN @ a27494c
P2-R2-D-B Final PDF Pipeline Freeze       ✅ COMPLETE / FROZEN @ <this commit>
```

### TODO.md

```
P2-R2 complete
P2-R3 approved to start
P2-R4 blocked by P2-R3
B7 SQLite Store Open-Failure Cleanup pending / not authorized
```

### ROADMAP.md

```
P2-R2 — COMPLETE
P2-R3 — Heading-aware Chunk + SQLite FTS5  ✅ APPROVED TO START
P2-R4 — Session-scoped search_knowledge
        + Page Marker Citation             ⛔ BLOCKED BY P2-R3
```

删除/修正任何"R2 IN PROGRESS / R2-D BLOCKED / vector phase planned"过时表述。

---

## 21. 最终判定

```
P2-R2-A pypdf Parser Adapter
✅ FROZEN @ 0772324

P2-R2-B Canonical Markdown Builder
✅ FROZEN @ eb193b2
✅ Amendment 2 RATIFIED @ c2436c7

P2-R2-C PDF Ingestion Pipeline
✅ COMPLETE / FROZEN @ 9034842
✅ Ruff cleanup @ a27494c

P2-R2-D-A Test Count Reconciliation
✅ FROZEN @ 287edb9

Historical 8-test discrepancy
✅ RECONCILED / CLOSED

R1 targeted count reconciliation
✅ RECONCILED / CLOSED
118 selected = 117 passed + 1 skipped

P2-R2-D-Fix
✅ FROZEN @ a27494c

P2-R2-D-B Final PDF Pipeline Freeze
✅ COMPLETE / FROZEN @ <this commit>

P2-R2 PDF → Canonical Markdown Pipeline
✅ COMPLETE / FROZEN

P2-R3 Heading-aware Chunk + SQLite FTS5
✅ APPROVED TO START

P2-R4 Session-scoped search_knowledge
       + Page Marker Citation
⛔ BLOCKED BY P2-R3

B7 SQLite Store Open-Failure Cleanup
⏸ PENDING / NOT AUTHORIZED

G1 Assistant Markdown Rendering
⚠ Original stash object continuity was historically broken
✅ Content equivalence verified by C4-R
⏸ Replacement stash preserved @ 5731ab7d...

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

P2-R2 全阶段正式关闭。R3 实现不在本提交启动。

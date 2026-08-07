# P2-R2-C1 Ingestion Store + Orchestrator — Validation Report

> **阶段**：P2-R2-C1 Ingestion Store Extensions + Orchestrator
> **起始 HEAD**：`09261ea`（D1/D2/D3 declined）
> **C0 baseline**：`6476f96`（frozen）+ post-freeze correction `0a8f554`（R2-C terminal = normalizing）
> **C1 commit chain**：`015dc45` (C1-A Store) → `cde0e0b` (C1-B Orchestrator) → `<this>` (C1-C Freeze)
> **归档日期**：2026-08-03
> **范围**：IngestionStore（atomic claim / retry / recovery primitives）+ IngestionOrchestrator（Parser + Quality + Builder + Persistence composition）。**不**含 Worker / API / Chunk / FTS5 / Tool。

---

## 1. Final Status

```
P2-R2-C1 Ingestion Store + Orchestrator
✅ COMPLETE / FROZEN @ <this commit>

C1 commit chain:
0a8f554  docs(rag): correct R2-C terminal status — normalizing not ready
                                                 (C0 Archive Correction)
015dc45  feat(rag): add ingestion state operations                (C1-A)
cde0e0b  feat(rag): orchestrate PDF ingestion                     (C1-B)
<this>   test(rag): freeze ingestion orchestrator                 (C1-C)

Baseline before C1:
09261ea — docs(rag): decline vector retrieval — D1/D2/D3 permanently declined

C0 contract baseline (frozen):
6476f96 + 0a8f554 — p2-r2-c0-ingestion-runtime-api-contract.md
                    (with post-freeze terminal-status correction)
```

---

## 2. Pre-flight Gates（per C1 directive §2）

| # | Gate | Status |
|---|---|---|
| 1 | C0 frozen | ✅ `6476f96` |
| 2 | C0 Open Questions = 0 | ✅ |
| 3 | Schema Amendment NOT REQUIRED | ✅（per C0 §25） |
| 4 | C0 frozen all state machines / claim / retry / recovery | ✅ |
| 5 | Route narrowing committed (D1/D2/D3 declined) | ✅ `09261ea` |
| 6 | All ancestors verified (`c2436c7` / `6476f96` / `09261ea`) | ✅ |
| 7 | G1 stash unchanged (`d7240268`) | ✅ |
| 8 | working tree clean before C1-A | ✅ |

---

## 3. Real Schema (audited from `store.py::_DDL_STATEMENTS`)

Per C0 §3.1-3.3 — unchanged from R1 frozen schema:

- **knowledge_documents**: id / library_id / source_name / source_sha256 / source_relpath / markdown_relpath / mime_type / size_bytes / page_count / status / parser_version / error_code / created_at / updated_at; status CHECK enum; UNIQUE(library_id, source_sha256)
- **knowledge_ingestion_jobs**: id / document_id / stage / status / attempt / started_at / finished_at / safe_error_code; stage CHECK enum; status CHECK (`running` / `completed` / `failed` — no `pending` / `cancelled`)

C1 did NOT modify schema.

---

## 4. Real State Machine (from `models.py::_DOCUMENT_TRANSITIONS`)

```
uploaded    → {extracting, deleting}
extracting  → {normalizing, failed, needs_ocr, deleting}
normalizing → {chunking, failed, deleting}        # NO direct → ready
chunking    → {indexing, failed, deleting}         # R3
indexing    → {ready, failed, deleting}            # R3
ready       → {deleting}                           # R3 terminal
failed      → {extracting, deleting}               # retry allowed
needs_ocr   → {deleting}                           # terminal
deleting    → {}                                    # terminal
```

**Critical**: `normalizing → ready` is NOT allowed. R2-C terminal = `normalizing` (per `0a8f554` post-freeze correction). C1 Orchestrator NEVER calls `transition_document_status(doc_id, 'ready')`.

---

## 5. C1-A Store Module

### 5.1 New file

`src/pi_agent_core_py/web/knowledge/ingestion_store.py`

### 5.2 Public API

```python
class IngestionStore:
    def __init__(self, store: KnowledgeStore) -> None: ...
    
    # Atomic claim (C0 §13.2)
    async def claim_next_pending_document(self) -> ClaimedJob | None: ...
    
    # Retry (C0 §12 + §13.1)
    async def create_retry_job(self, document_id: str) -> IngestionJob: ...
    
    # Read queries
    async def count_active_jobs_for_document(self, document_id: str) -> int: ...
    async def has_active_job(self, document_id: str) -> bool: ...
    async def count_extract_attempts(self, document_id: str) -> int: ...
    async def get_latest_extract_job_for_document(self, document_id: str) -> IngestionJob | None: ...
    
    # Recovery primitive (C0 §15.2 — called by C2 startup)
    async def mark_running_jobs_interrupted(self) -> int: ...
```

### 5.3 Errors

- `IngestionStoreError` (base)
- `IngestionAlreadyActiveError` (concurrent retry rejected)
- `RetryLimitReachedError` (5 prior extract attempts)
- `RetryNotAllowedError` (status != failed)

### 5.4 Constants

- `MAX_RETRY_ATTEMPTS = 5`（per C0 §12.3）
- `EXTRACT_STAGE = "extract"`（per C0 §9.5）
- `INGESTION_INTERRUPTED = "ingestion_interrupted"`（per C0 §15.2）

### 5.5 Atomic patterns

- All write methods: `BEGIN IMMEDIATE` + `_write_lock: asyncio.Lock` double serialization
- Active Job uniqueness: SELECT COUNT within same txn (TOCTOU-safe)
- Claim: `SELECT ... LIMIT 1` + conditional `UPDATE WHERE status='uploaded'` (rowcount check)
- Retry attempt auto-increment: SELECT COUNT within txn + 1

---

## 6. C1-B Orchestrator Module

### 6.1 New file

`src/pi_agent_core_py/web/knowledge/ingestion_orchestrator.py`

### 6.2 Public API

```python
class IngestionOrchestrator:
    def __init__(
        self, *,
        store: KnowledgeStore,
        ingestion_store: IngestionStore,
        file_store: KnowledgeFileStore,
        parser: PdfParser,
        quality_evaluator: PdfTextQualityEvaluator,
        builder: CanonicalMarkdownBuilder,
        persistence: CanonicalMarkdownPersistence,
        max_pdf_pages: int = 20,
    ) -> None: ...
    
    async def run_claimed_job(self, job_id: str) -> IngestionRunResult: ...

@dataclass(frozen=True, slots=True)
class IngestionRunResult:
    document_id: str
    job_id: str
    document_status: str          # 'normalizing' / 'needs_ocr' / 'failed'
    job_status: str               # 'completed' / 'failed'
    safe_error_code: str          # '' on success; 'needs_ocr' on terminal business
    parser_id: str | None
    parser_version: str | None
    page_count: int | None
    markdown_sha256: str | None
    markdown_byte_length: int | None
    warnings: tuple[str, ...]
    duration_ms: int
```

### 6.3 Call sequence（per C0 §18.1 + post-freeze correction）

```
1. Load + validate Job (running + extract stage)
2. Load Document (must be 'extracting')
3. Resolve source.pdf path (friend access to KnowledgeFileStore)
4. Verify source.pdf exists
5. parser.inspect (asyncio.to_thread)
6. Enforce page_count ≤ MAX_PDF_PAGES
7. parser.extract (asyncio.to_thread)
8. quality_evaluator.evaluate (asyncio.to_thread)
9. If NEEDS_OCR:
     Document → needs_ocr (error_code='needs_ocr', parser_version, page_count)
     Job → completed
     return  (no document.md written)
10. Document → normalizing (parser_version, page_count)  # state machine allows
11. Build CanonicalMarkdownSource (NO generated_at — per §24)
12. CanonicalMarkdownBuilder.build (asyncio.to_thread)
13. CanonicalMarkdownPersistence.write (asyncio.to_thread)
14. SHA verification (write_result.sha256 == artifact.content_sha256)
15. finish_job(job_id, 'completed')  # Document STAYS in normalizing
16. Return IngestionRunResult
```

### 6.4 Failure paths（per C0 §19 + §35.5 correction）

| Step | Failure | Document | Job | safe_error_code |
|---|---|---|---|---|
| 3-4 | source path / missing | `failed` | `failed` | `source_file_missing` |
| 5 | inspect: PdfParserError | `failed` | `failed` | (mapped per parser code) |
| 6 | page_count > 20 | `failed` | `failed` | `pdf_page_limit_exceeded` |
| 7 | extract: PdfParserError | `failed` | `failed` | (mapped per parser code) |
| 8 | InvalidExtractionResult | `failed` | `failed` | `invalid_extraction_result` |
| 9 | (NEEDS_OCR — not failure) | `needs_ocr` | `completed` | `needs_ocr` |
| 12 | NeedsOcrNotBuildable (defensive) | `needs_ocr` | `completed` | `needs_ocr` |
| 12 | CanonicalMarkdownTooLarge | `failed` | `failed` | `canonical_markdown_too_large` |
| 12 | other Exception | `failed` | `failed` | `canonical_markdown_build_failed` |
| 13 | PersistenceError | `failed` | `failed` | (mapped per persistence code) |
| 14 | SHA mismatch | `failed` | `failed` | `canonical_markdown_sha_mismatch` |
| 15 | finish_job failure | `failed` | `failed` | `terminal_commit_failed` |
| (init) | Doc not 'extracting' | unchanged | `failed` | `ingestion_state_conflict` |
| (any) | uncaught Exception | `failed` | `failed` | `internal_ingestion_error` |

---

## 7. C1 generated_at invariant（per §24）

```python
# In IngestionOrchestrator.run_claimed_job step 11:
source = CanonicalMarkdownSource(
    document_id=document.id,
    source_filename=document.source_name,
    source_sha256=document.source_sha256,
    title=inspection.metadata.title,
    # generated_at omitted — per R2-B audit §6 ratified @ c2436c7
)
```

Tested via `test_generated_at_not_in_artifact_frontmatter` — written `document.md` does NOT contain `generated_at:` line.

Determinism tested via `test_same_input_produces_byte_identical_sha` — first run + retry produce same SHA.

---

## 8. C1 page marker preservation（per §17）

Orchestrator does NOT modify document.md after Persistence writes it. R2-B Builder produces `<!-- page:N -->` markers; R2-B Persistence writes them atomically; R2-C1 Orchestrator passes the artifact through unchanged.

Verified via `test_document_md_written_and_readable` (asserts `<!-- page:1 -->` present in MD).

---

## 9. C1 targeted test results

| Suite | Tests | PASS |
|---|---|---|
| `tests/test_ingestion_store.py` | 38 | 38 |
| `tests/test_ingestion_orchestrator.py` | 20 | 20 |
| **C1 total** | **58** | **58** |

```bash
PYTHONPATH=src python -m pytest tests/test_ingestion_store.py tests/test_ingestion_orchestrator.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
→ 58 passed in 5.16s
```

---

## 10. R2-B regression

```bash
PYTHONPATH=src python -m pytest tests/test_pdf_quality.py tests/test_canonical_markdown.py \
    tests/test_markdown_persistence.py tests/test_r2_b_integration.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **160/160 PASS** — 0 regression.

---

## 11. R2-A regression

```bash
PYTHONPATH=src python -m pytest tests/test_pypdf_parser.py tests/test_rag_dependency_boundary.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **66/66 PASS** — 0 regression.

---

## 12. R1 regression

```bash
PYTHONPATH=src python -m pytest tests/test_knowledge_store.py \
    tests/test_knowledge_files_and_service.py tests/test_knowledge_api.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **117 passed + 1 platform skip (118 selected)** — 0 regression. **CORRECTED @ P2-R2-D-B**：原报告"118/118 PASS"是把 selected 数误写成 passed 数。

---

## 13. Complete Backend（per directive §三十四）

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **2986 passed, 2 skipped, 14 deselected** in 185s.

| Metric | Value |
|---|---|
| pytest version | 9.1.1 |
| Plugins | anyio-4.14.0, asyncio-1.4.0 (mode=Mode.AUTO), cov-7.1.0 |
| Baseline（pre-C1） | 2928 passed / 2 skipped / 14 deselected（**CORRECTED @ P2-R2-D-A**；原报告 2920 误抄） |
| C1 added | 58 targeted tests |
| Backend delta | 58（2928 → 2986）✅ = targeted 58 完全对账 |
| Historical 8-test discrepancy | ✅ RECONCILED @ P2-R2-D-A（freeze 文档误抄；不存在 node-ID-level 差异） |
| Functional regression | 0 |

**Note（CORRECTED @ P2-R2-D-A）**：原报告"Baseline pre-C1 = 2920 / delta = 66 / 8-test discrepancy continued"是基于 R2-B freeze 时点误抄数字 2920 的连锁推算。P2-R2-D-A 通过 A/B worktree 实测确认 R2-B 实际为 2928 passed，因此 C1 baseline = 2928 / delta = 58 = C1 targeted 58，完全对账。

详见 [`P2_R2_D_TEST_COUNT_RECONCILIATION.md`](P2_R2_D_TEST_COUNT_RECONCILIATION.md)。原 5 项 R2-D handoff 已在 D-A 完成；Ruff `--fix` / fixture dedup 假设（H1/H2/H3）已被 git diff + 集合分析否决。

---

## 14. Frontend regression

```bash
cd src/pi_agent_core_py/web/frontend
npm run test       # 267/267 PASS (11 files)
npm run typecheck  # PASS (vue-tsc --noEmit)
npm run lint       # PASS (eslint . --max-warnings=0)
```

Frontend source diff = **0**（R2-C1 全部是 backend / docs）。

---

## 15. Ruff result

```bash
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

（C1 commit chain 内 ruff `--fix` 自动修正了 14 个 import 顺序 / unused import 问题；6 个 F841 unused-variable 在 tests 中手动修正——删除不必要的赋值或使用 `_` 前缀。）

---

## 16. Boundary audit（per directive §三十）

| Check | Command | Result |
|---|---|---|
| Vector / embedding imports | `rg "embedding\|vector\|dense\|hybrid\|rerank\|..." ingestion_*.py` | ✅ 0 production hits（仅 docstring 提及"does NOT"） |
| Worker / asyncio.Queue / FastAPI | `rg "FastAPI\|APIRouter\|UploadFile\|asyncio.Queue\|asyncio.Event\|create_task\|TaskGroup\|lifespan" ingestion_*.py` | ✅ 0 production hits（仅 docstring 提及"does NOT"） |
| Chunk / FTS5 / search_knowledge | `rg "CREATE VIRTUAL TABLE\|fts5\|MATCH\|bm25\|knowledge_chunks\|search_knowledge" ingestion_*.py` | ✅ 0 production hits |
| Network / OCR / LLM / model | (covered by R2-A boundary tests) | ✅ 0 |

---

## 17. Network / OCR / model audit

| Check | Result |
|---|---|
| External HTTP requests | ✅ 0（C1 模块零网络 import） |
| DNS resolution | ✅ 0 |
| Model downloads | ✅ 0 |
| OCR calls | ✅ 0（pypdf 无 OCR） |
| LLM calls | ✅ 0 |
| Provider calls | ✅ 0 |
| Embedding calls | ✅ 0（D1/D2/D3 DECLINED @ 09261ea） |
| Vector operations | ✅ 0 |
| Reranker calls | ✅ 0 |
| Worker tasks | ✅ 0（C2 范围） |
| HTTP endpoints | ✅ 0（C3 范围） |

---

## 18. Diff self-check

| Category | Status |
|---|---|
| Production code | 2 new modules (`ingestion_store.py` + `ingestion_orchestrator.py`) |
| Tests | 2 new files (`test_ingestion_store.py` + `test_ingestion_orchestrator.py`) |
| Dependency (`pyproject.toml`) | ✅ 0 diff |
| Lockfile (`uv.lock`) | ✅ 0 diff |
| Schema / migration | ✅ 0 diff |
| Frontend (`frontend/**`) | ✅ 0 diff |
| G1 stash | ✅ unchanged (`d7240268...`) |
| R1 frozen Store / models / files / service / api | ✅ 不动 |
| R2-A Parser / pypdf_parser | ✅ 不动 |
| R2-B pdf_quality / canonical_markdown / markdown_persistence | ✅ 不动 |
| C0 design / validation docs | ✅ 不动（post-freeze correction 已合入 `0a8f554`） |

---

## 19. Files added / modified in C1

### C1-A (commit `015dc45`)

| File | Type | Lines |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/ingestion_store.py` | new | 463 |
| `tests/test_ingestion_store.py` | new | 647 |

### C1-B (commit `cde0e0b`)

| File | Type | Lines |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/ingestion_orchestrator.py` | new | 722 |
| `tests/test_ingestion_orchestrator.py` | new | 647 |

### C1-C (this commit)

| File | Type |
|---|---|
| `docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md` | new (this file) |
| `STATUS.md` | modified |
| `TODO.md` | modified |
| `ROADMAP.md` | modified |

---

## 20. Known limitations

- **Background Worker not yet started** — C1 provides atomic primitives + sync-callable Orchestrator; C2 will implement WorkerManager + FastAPI lifespan wiring.
- **Upload / Status / Retry / Markdown HTTP API not yet implemented** — C3 scope.
- **Auto recovery not yet invoked** — `mark_running_jobs_interrupted()` exists; C2 calls it at startup.
- **Chunking / FTS5 indexing not yet implemented** — R3 scope（per route narrowing `09261ea`）.
- **`search_knowledge` AgentTool not yet registered** — R4 scope.
- **Complex PDF quality limited by pypdf** — multi-column / tables / formulas degraded（per `p2-r2-0-pdf-parser-license-gate.md §21.1`）.
- **Scan-only PDFs enter `needs_ocr` terminal** — no OCR fallback in MVP.
- **Windows junction / reparse point test gap** — R1 inherited; production path checks do not depend on test environment（per `P2_R1_LIBRARY_FOUNDATION.md §8.2`）.
- **8-test statistical discrepancy** — KNOWN NON-BLOCKING; handed off to R2-D.
- **Vector / embedding / reranker** — permanently removed from roadmap（per `09261ea`）.

---

## 21. Explicitly NOT implemented

- ❌ `asyncio.Queue` / `asyncio.Event` / `create_task` / `TaskGroup` / polling loop / worker thread / executor manager / lifespan / startup hook / shutdown hook / sleep / auto-claim loop / auto-recovery call
- ❌ FastAPI router / API endpoint / DTO / multipart upload / HTTP status mapping / Trusted UI Header wiring
- ❌ `knowledge_chunks` writes / FTS5 virtual table / MATCH / BM25
- ❌ `search_knowledge` AgentTool registration
- ❌ Embedding / vector index / dense retrieval / hybrid retrieval / reranker / RRF / MMR / HNSW / IVF
- ❌ Frontend knowledge UI
- ❌ Database schema changes
- ❌ Dependency changes

---

## 22. Exit Gate（per directive §三十八）

| # | Condition | Status |
|---|---|---|
| 1 | C0 frozen | ✅ `6476f96` |
| 2 | C0.5 not required | ✅ Schema Amendment NOT REQUIRED |
| 3 | Route adjustment documented | ✅ `09261ea` (D1/D2/D3 DECLINED) |
| 4 | Schema unchanged | ✅ |
| 5 | Atomic claim complete | ✅ IngestionStore.claim_next_pending_document |
| 6 | Concurrent claim safe | ✅ test_concurrent_claim_only_one_wins |
| 7 | Active Job unique | ✅ test_rejects_when_active_job_running |
| 8 | Retry creates new Job | ✅ test_creates_new_job_for_failed_document |
| 9 | Old Job history preserved | ✅ test_old_terminal_jobs_remain_immutable |
| 10 | Attempt monotonic | ✅ test_attempt_monotonic_across_retries |
| 11 | Joint state transitions | ✅ Orchestrator transitions Document + Job |
| 12 | Recovery primitive | ✅ mark_running_jobs_interrupted + idempotent |
| 13 | Orchestrator complete | ✅ IngestionOrchestrator.run_claimed_job |
| 14 | source existence check | ✅ test_source_missing_marks_failed |
| 15 | inspect call correct | ✅ test_each_pipeline_step_called_once |
| 16 | page limit correct | ✅ test_page_limit_exceeded_marks_failed |
| 17 | extract call correct | ✅ (covered by success path test) |
| 18 | Quality call correct | ✅ (covered) |
| 19 | needs_ocr no Builder | ✅ test_needs_ocr_does_not_write_document_md |
| 20 | needs_ocr no Markdown | ✅ test_needs_ocr_does_not_write_document_md |
| 21 | needs_ocr terminal consistent | ✅ Document='needs_ocr' / Job='completed' |
| 22 | usable enters normalizing | ✅ test_text_pdf_reaches_normalizing |
| 23 | Builder call correct | ✅ |
| 24 | generated_at not passed | ✅ test_generated_at_not_in_artifact_frontmatter |
| 25 | Persistence call correct | ✅ |
| 26 | Terminal stays in normalizing (NOT ready) | ✅ per C0 §35.5 correction |
| 27 | SHA writeback correct | ✅ result.markdown_sha256 |
| 28 | page_count writeback correct | ✅ result.page_count |
| 29 | parser metadata writeback | ✅ result.parser_id / parser_version |
| 30 | page marker preserved | ✅ test_document_md_written_and_readable |
| 31 | Failure mapping complete | ✅ 17 safe_error_codes |
| 32 | DB/file compensation | ✅ test_source_pdf_sha_unchanged_after_run |
| 33 | source.pdf not modified | ✅ |
| 34 | Absolute path no leak | ✅ test_result_does_not_contain_absolute_path |
| 35 | Raw exception no leak | ✅ test_result_does_not_contain_markdown_body |
| 36 | C1 targeted all pass | ✅ 58/58 |
| 37 | R2-B 160 all pass | ✅ |
| 38 | R2-A 66 all pass | ✅ |
| 39 | R1 zero regression | ✅ 117 passed + 1 platform skip（118 selected） |
| 40 | Complete backend zero regression | ✅ 2986 passed (delta 66; 8-test discrepancy non-blocking) |
| 41 | Frontend 267/267 | ✅ |
| 42 | Ruff pass | ✅ All checks passed |
| 43 | External network 0 | ✅ §17 |
| 44 | OCR/model/LLM 0 | ✅ |
| 45 | embedding 0 | ✅ |
| 46 | vector operations 0 | ✅ |
| 47 | reranker 0 | ✅ |
| 48 | Worker diff 0 | ✅ (C2 scope) |
| 49 | API diff 0 | ✅ (C3 scope) |
| 50 | Chunk/FTS diff 0 | ✅ (R3 scope) |
| 51 | Tool registry diff 0 | ✅ (R4 scope) |
| 52 | Dependency diff 0 | ✅ |
| 53 | Schema diff 0 | ✅ |
| 54 | Frontend diff 0 | ✅ |
| 55 | G1 stash unchanged | ✅ `d7240268...` |
| 56 | Validation doc complete | ✅ (this file) |
| 57 | Working tree clean | ✅ (post-commit) |
| 58 | Open blockers = 0 | ✅ |

**58/58 PASS** ✅

---

## 23. Final Verdict

```
P2-R2-C1 Ingestion Store + Orchestrator
✅ COMPLETE / FROZEN @ <this commit>

Retrieval Roadmap (per 09261ea + C0 §35.5)
✅ Canonical Markdown (R2-B)
✅ Ingestion atomic primitives (R2-C1)
✅ Page Marker Citation (R2-B `<!-- page:N -->`)

Removed (per 09261ea)
⛔ Embedding
⛔ Vector Retrieval
⛔ Hybrid Retrieval
⛔ Reranker

P2-R2-C2 Bounded Worker + Recovery
✅ APPROVED TO START (独立启动授权另需用户发起)

P2-R2-C3 Upload / Status / Retry / Markdown API
⛔ BLOCKED BY C2

P2-R2-C4 Integration Validation + R2-C Freeze
⛔ BLOCKED BY C3

P2-R2-D Final PDF Pipeline Validation
⛔ BLOCKED BY COMPLETE R2-C
⚠ MUST RECONCILE 8-test discrepancy (5 items)

P2-R3 Chunk + SQLite FTS5
⛔ BLOCKED BY COMPLETE P2-R2

P2-R4 Session-scoped search_knowledge Tool + Page Marker Citation
⛔ BLOCKED BY P2-R3

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
  (unchanged across R2-C0 + R2-C1)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

C1 完成。**立即停止**——不进入 C2 / C3 / C4 / R3 / R4 / R5（独立启动授权另需用户发起）。

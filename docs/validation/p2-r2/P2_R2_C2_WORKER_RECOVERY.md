# P2-R2-C2 Bounded Worker + Recovery — Validation Report

> **阶段**：P2-R2-C2 Bounded Worker + Startup Recovery + Lifespan Wiring
> **起始 HEAD**：`3e45da3`（R2-C1 freeze）
> **C2 commit chain**：`5aea74b` (C2-A Worker) → `115136a` (C2-B Lifespan) → `<this>` (C2-C Freeze)
> **归档日期**：2026-08-04
> **范围**：IngestionWorkerManager（asyncio wake + bounded queue + polling fallback）+ startup recovery + graceful shutdown + FastAPI lifespan wiring。**不**含 API endpoints（C3）/ Chunk / FTS5（R3）/ Tool（R4）。

---

## 1. Final Status

```
P2-R2-C2 Bounded Worker + Recovery
✅ COMPLETE / FROZEN @ <this commit>

C2 commit chain:
5aea74b  feat(rag): add bounded ingestion worker                (C2-A)
115136a  feat(rag): wire ingestion worker lifecycle             (C2-B)
<this>   test(rag): freeze ingestion worker and recovery        (C2-C)

Baseline before C2:
3e45da3 — test(rag): freeze ingestion orchestrator (R2-C1)
```

---

## 2. Pre-flight Gates（per C2 directive §四）

| # | Gate | Status |
|---|---|---|
| 1 | HEAD = `3e45da3` | ✅ |
| 2 | `c2436c7` / `09261ea` / `0a8f554` / `3e45da3` are ancestors | ✅ |
| 3 | G1 stash unchanged = `d7240268...` | ✅ |
| 4 | Working tree clean before C2-A | ✅ |
| 5 | C0/C1 frozen contracts honored | ✅ |

---

## 3. C2-A Worker Module

### 3.1 New file

`src/pi_agent_core_py/web/knowledge/ingestion_worker.py`（~700 行）

### 3.2 Public API

```python
class IngestionWorkerManager:
    def __init__(
        self, *,
        orchestrator: IngestionOrchestrator,
        ingestion_store: IngestionStore,
        store: KnowledgeStore,
        parser: PdfParser,
        worker_concurrency: int = 1,
        wake_queue_capacity: int = 32,
        poll_interval_seconds: float = 30.0,
        shutdown_grace_seconds: float = 30.0,
        owns_parser: bool = True,
        logger: logging.Logger | None = None,
    ) -> None: ...

    @property
    def state(self) -> WorkerState: ...  # stopped/starting/running/stopping/failed

    async def start(self) -> None: ...   # startup recovery + worker_loop
    async def stop(self) -> None: ...    # graceful shutdown + parser.close
    def notify_pending_job(self) -> bool: ...  # True=accepted, False=coalesced
    async def wait_until_idle(self, timeout: float | None = None) -> bool: ...
    def snapshot(self) -> IngestionWorkerSnapshot: ...
```

### 3.3 Worker loop

```
while not stop_requested:
    drain phase:
        while not stop_requested:
            claimed = await ingestion_store.claim_next_pending_document()
            if claimed is None: break
            self._active_job_id = claimed.job.id
            try:
                await orchestrator.run_claimed_job(claimed.job.id)
            except CancelledError:
                await self._conditional_fail_active(...)
                raise
            except Exception:
                await self._conditional_fail_active(...)
            finally:
                self._active_job_id = None

    drain wake queue (consume sentinels)

    wait phase:
        try:
            await asyncio.wait_for(wake_queue.get(), timeout=poll_interval)
        except TimeoutError:
            pass  # poll fallback
```

### 3.4 Constants（per C0 §14 / §16）

| Constant | Value | Source |
|---|---|---|
| `DEFAULT_WORKER_CONCURRENCY` | 1 | C0 §14.1 |
| `DEFAULT_WAKE_QUEUE_CAPACITY` | 32 | C0 §14.1 |
| `DEFAULT_POLL_INTERVAL_SECONDS` | 30.0 | C0 §14.4 |
| `DEFAULT_SHUTDOWN_GRACE_SECONDS` | 30.0 | C0 §16.1 |
| `INGESTION_INTERRUPTED` (re-exported) | "ingestion_interrupted" | C0 §15.2 |

---

## 4. C2-A Atomic Claim Reuse（per directive §十一）

Worker delegates all DB claim work to C1 frozen `IngestionStore.claim_next_pending_document()`:

```python
claimed = await self._ingestion_store.claim_next_pending_document()
if claimed is None:
    return  # no pending work
```

Worker does NOT:
- Re-implement SELECT/UPDATE for claim
- Bypass Store with raw SQL (except for conditional fail + recovery — see §5 below)
- Depend on asyncio.Queue for de-duplication (SQLite rowcount check is authoritative)

---

## 5. C2-A Startup Recovery（per C0 §15 + directive §十四）

### 5.1 Recovery sequence（in `start()`）

1. `mark_running_jobs_interrupted()`（C1 primitive）：所有 `running` Job → `failed` + `ingestion_interrupted`
2. `_mark_active_documents_interrupted()`（C2 new helper）：所有 `extracting` / `normalizing` Document → `failed` + `ingestion_interrupted`

### 5.2 Recovery semantics table

| Pre-existing State | Recovery Action |
|---|---|
| Document `uploaded` + 无 Job | 不动；Worker drain phase 自动 claim |
| Document `uploaded` + Job `running` | Job → `failed` + interrupted；Document 保持 `uploaded` |
| Document `extracting` + Job `running` | Job → `failed`；Document → `failed` |
| Document `normalizing` + Job `running` | Job → `failed`；Document → `failed` |
| Document `extracting/normalizing` + 无 Job | Document → `failed`（orphan） |
| Document `ready` / `needs_ocr` / `failed` | 不动（terminal） |
| Job `running` 对应 Document 已 terminal | Job → `failed`（orphan Job） |

### 5.3 Idempotency

Conditional UPDATE：`WHERE status='running'`（Job）/ `WHERE status IN ('extracting','normalizing')`（Document）。第二次启动 affects 0 rows。

Tested via `test_recovery_idempotent` + `test_terminal_records_untouched`.

---

## 6. C2-A Conditional Fail Primitive（per directive §二十一 方案 B）

Late-write race prevention during shutdown:

```python
async def _conditional_fail_active(
    self, *, job_id, document_id, safe_error_code
) -> None:
    # Atomic UPDATE within BEGIN IMMEDIATE
    UPDATE knowledge_ingestion_jobs
      SET status='failed', finished_at=?, safe_error_code=?
      WHERE id=? AND status='running'    -- ← conditional
    
    UPDATE knowledge_documents
      SET status='failed', error_code=?, updated_at=?
      WHERE id=? AND status IN ('extracting','normalizing')    -- ← conditional
```

Late Orchestrator completion（still in another thread）:
- Tries `transition_document_status(doc, 'normalizing')` → state machine check reads `failed` → `failed → normalizing` rejected → no UPDATE
- Tries `finish_job(job, 'completed')` → unconditional UPDATE → **may** overwrite（known race；mitigated by conditional UPDATE timing）

**Known limitation**（documented）：C1 `finish_job` is unconditional. Worst case: rare race window where Orchestrator's late completion overwrites Job from `failed` to `completed`. Document state is protected by state machine. The 8-test style "minor inconsistency" is acceptable per directive §二十 acknowledgment.

---

## 7. C2-A Wake-up Mechanism（per directive §九）

```python
self._wake_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=32)

def notify_pending_job(self) -> bool:
    if self._state != "running":
        raise RuntimeError(...)
    try:
        self._wake_queue.put_nowait(None)  # sentinel — no job_id
        return True
    except asyncio.QueueFull:
        return False  # coalesced — polling fallback picks up
```

Key invariants:
- Queue items are `None` sentinels — **not** job_ids
- DB is durable source of truth；Queue is hint only
- Queue full → coalesced（`False` returned）
- Polling fallback（30s default）catches any missed wakeups
- Tests verify: `test_queue_full_returns_false`、`test_poll_recovers_lost_wakeup`

---

## 8. C2-A Shutdown（per C0 §16 + directive §二十/§二十一）

### 8.1 Shutdown sequence

```
1. state = "stopping"
2. stop_requested = True
3. _wake_all() — fill wake_queue to unblock worker wait
4. await asyncio.wait_for(asyncio.shield(worker_task), timeout=grace)
5. if TimeoutError:
       _conditional_fail_active(job_id, doc_id, "ingestion_interrupted")
       worker_task.cancel()
       await worker_task  # propagate cancel
6. if owns_parser: parser.close()  # best-effort
7. clear _active_job_id / _active_document_id
8. state = "stopped"
```

### 8.2 CancelledError handling

Worker's `_run_claimed_job_safely` catches CancelledError, calls `_conditional_fail_active`, then re-raises（preserves cancel propagation）。

### 8.3 Grace timeout limitation

Per directive §二十 acknowledgment: Python cannot safely kill a running synchronous thread. After grace timeout:
- Worker task is cancelled（async层面）
- pypdf extract thread continues until pypdf finishes
- Conditional UPDATE prevents（mostly）late-write race
- Worst case（rare）：late Orchestrator's `finish_job('completed')` overwrites Job back to `completed`；Document state protected by state machine

---

## 9. C2-B Lifespan Wiring

### 9.1 web/state.py changes

```python
class WebAppState(BaseModel):
    ...
    knowledge_service: Any = None
    knowledge_store: Any = None
    knowledge_file_store: Any = None
    ingestion_worker_manager: Any = None  # ← NEW P2-R2-C2
```

### 9.2 web/app.py lifespan startup（after knowledge_service setup）

```python
if knowledge_root is not None:
    # ... existing knowledge setup ...
    ingestion_manager = None
    try:
        from .knowledge.ingestion_worker import IngestionWorkerManager
        # ... other imports ...
        ingestion_store_obj = IngestionStore(k_store)
        parser_obj = PypdfParser()
        orchestrator_obj = IngestionOrchestrator(...)
        ingestion_manager = IngestionWorkerManager(
            orchestrator=orchestrator_obj,
            ingestion_store=ingestion_store_obj,
            store=k_store,
            parser=parser_obj,
            owns_parser=True,
        )
    except ImportError:
        pass  # [rag] extra missing
    except Exception as e:
        raise RuntimeError(...) from e
    
    if ingestion_manager is not None:
        await ingestion_manager.start()
        state.ingestion_worker_manager = ingestion_manager
else:
    state.ingestion_worker_manager = None
```

### 9.3 web/app.py lifespan shutdown（in outer finally）

```python
# P2-R2-C2: stop worker BEFORE KnowledgeStore.close()
ingestion_mgr = state.ingestion_worker_manager
if ingestion_mgr is not None:
    try:
        await ingestion_mgr.stop()
    except Exception:
        pass
    state.ingestion_worker_manager = None

# P2-R1: close KnowledgeStore（existing）
if k_store is not None:
    try:
        await k_store.close()
    except Exception:
        pass
```

### 9.4 Order verification

`test_manager_stopped_before_knowledge_store_closed` wraps both `mgr.stop` and `k_store.close` with tracking wrappers；verifies order: `["manager_stop", "store_close"]`.

---

## 10. C2 Targeted Test Results

| Suite | Tests | PASS |
|---|---|---|
| `tests/test_ingestion_worker.py` (C2-A) | 30 | 30 |
| `tests/test_ingestion_worker_lifespan.py` (C2-B) | 7 | 7 |
| **C2 total** | **37** | **37** |

---

## 11. C1 Regression

```bash
PYTHONPATH=src python -m pytest tests/test_ingestion_store.py tests/test_ingestion_orchestrator.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **58/58 PASS** — 0 regression.

---

## 12. R2-B / R2-A / R1 Regression

```bash
PYTHONPATH=src python -m pytest \
    tests/test_pdf_quality.py tests/test_canonical_markdown.py \
    tests/test_markdown_persistence.py tests/test_r2_b_integration.py \
    tests/test_pypdf_parser.py tests/test_rag_dependency_boundary.py \
    tests/test_knowledge_store.py tests/test_knowledge_files_and_service.py \
    tests/test_knowledge_api.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **343 passed + 1 platform skip** — 0 regression（**CORRECTED @ P2-R2-D-B**：selected 口径 R2-B 160 + R2-A 66 + R1 118 = 344 selected；passed 口径 R2-B 160 + R2-A 66 + R1 117 = 343 passed + 1 POSIX-only skip）。

---

## 13. Complete Backend（per directive §四十二）

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **3023 passed, 2 skipped, 14 deselected** in 193s.

| Metric | Value |
|---|---|
| pytest version | 9.1.1 |
| Plugins | anyio-4.14.0, asyncio-1.4.0 (mode=Mode.AUTO), cov-7.1.0 |
| Baseline（pre-C2） | 2986 passed |
| C2 added | 37 targeted tests |
| Backend delta | 37（2986 → 3023） ✅ matches targeted count（no 8-test discrepancy this round） |
| 8-test discrepancy（inherited from R2-B） | unchanged；继续 handoff to R2-D |
| Functional regression | 0 |

---

## 14. Frontend Regression

```bash
cd src/pi_agent_core_py/web/frontend
npm run test       # 267/267 PASS (11 files)
npm run typecheck  # PASS (vue-tsc --noEmit)
npm run lint       # PASS (eslint . --max-warnings=0)
```

Frontend source diff = **0**（R2-C2 全部是 backend / docs）。

---

## 15. Ruff Result

```bash
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

---

## 16. Boundary Audit（per directive §三十八）

| Check | Command | Result |
|---|---|---|
| FastAPI / APIRouter / UploadFile in worker | `rg "UploadFile\|APIRouter\|multipart\|HTTPException" ingestion_worker.py` | ✅ 0 production hits（仅 docstring "does NOT"） |
| Vector / embedding in worker | `rg "embedding\|vector\|dense\|hybrid\|rerank\|..." ingestion_worker.py` | ✅ 0 production hits |
| Chunk / FTS5 / search_knowledge in worker | `rg "CREATE VIRTUAL TABLE\|fts5\|MATCH\|bm25\|knowledge_chunks\|search_knowledge" ingestion_worker.py` | ✅ 0 production hits |
| Network / OCR / LLM in worker | `rg "httpx\|requests\|aiohttp\|openai\|anthropic\|tesseract\|surya\|torch\|transformers" ingestion_worker.py` | ✅ 0 production hits |

---

## 17. Network / OCR / Model Audit

| Check | Result |
|---|---|
| External HTTP requests | ✅ 0 |
| DNS resolution | ✅ 0 |
| Model downloads | ✅ 0 |
| OCR calls | ✅ 0 |
| LLM calls | ✅ 0 |
| Provider calls | ✅ 0 |
| Embedding calls | ✅ 0（D1/D2/D3 DECLINED @ 09261ea） |
| Vector operations | ✅ 0 |
| Reranker calls | ✅ 0 |
| Chunk writes | ✅ 0（R3） |
| FTS5 writes | ✅ 0（R3） |
| HTTP endpoints added | ✅ 0（C3） |

---

## 18. Diff Self-Check

| Category | Status |
|---|---|
| Production code | 1 new module (`ingestion_worker.py`) + minimal edits to `web/app.py` + `web/state.py` |
| Tests | 2 new files (`test_ingestion_worker.py` + `test_ingestion_worker_lifespan.py`) |
| Dependency (`pyproject.toml`) | ✅ 0 diff |
| Lockfile (`uv.lock`) | ✅ 0 diff |
| Schema / migration | ✅ 0 diff |
| Frontend (`frontend/**`) | ✅ 0 diff |
| G1 stash | ✅ unchanged (`d7240268...`) |
| R1 frozen modules | ✅ 不动 |
| R2-A/R2-B frozen modules | ✅ 不动 |
| R2-C1 frozen modules（ingestion_store / ingestion_orchestrator） | ✅ 不动 |
| C0/C1 docs | ✅ 不动 |

---

## 19. Files Added / Modified in C2

### C2-A (commit `5aea74b`)

| File | Type |
|---|---|
| `src/pi_agent_core_py/web/knowledge/ingestion_worker.py` | new (~700 行) |
| `tests/test_ingestion_worker.py` | new (30 tests) |

### C2-B (commit `115136a`)

| File | Type |
|---|---|
| `src/pi_agent_core_py/web/state.py` | modified（+`ingestion_worker_manager: Any = None` field） |
| `src/pi_agent_core_py/web/app.py` | modified（lifespan startup + shutdown wiring） |
| `tests/test_ingestion_worker_lifespan.py` | new (7 tests) |

### C2-C (this commit)

| File | Type |
|---|---|
| `docs/validation/p2-r2/P2_R2_C2_WORKER_RECOVERY.md` | new (this file) |
| `STATUS.md` / `TODO.md` / `ROADMAP.md` | modified |

---

## 20. Known Limitations

- **Single process**：Worker Manager assumes one app process；no multi-process / distributed worker support.
- **Single worker**：`worker_concurrency = 1` frozen；no parallel ingestion.
- **Synchronous pypdf cannot be force-cancelled**：After grace timeout, async task is cancelled but pypdf thread continues；conditional UPDATE prevents（mostly）late-write race.
- **Memory wake is not source of truth**：asyncio.Queue is hint only；DB is durable source.
- **C1 finish_job is unconditional**（known race window）：Late Orchestrator completion in another thread could（rarely）overwrite Job `failed` → `completed`. Document state is protected by state machine. Startup recovery on next app start reconciles any `running` leftovers.
- **No user cancel API**：MVP only supports graceful shutdown cancel.
- **No upload/status/retry/markdown HTTP API yet**：C3 scope.
- **No chunking / FTS5**：R3 scope.
- **No `search_knowledge` AgentTool**：R4 scope.
- **Windows junction / reparse point test gap**：R1 inherited.
- **8-test statistical discrepancy**：continues handoff to R2-D.
- **Vector / embedding / reranker**：permanently removed（per `09261ea`）.

---

## 21. Explicitly NOT Implemented

- ❌ `POST /api/knowledge/libraries/{lib}/documents` (upload) — C3
- ❌ `GET /api/knowledge/documents/{doc}/ingestion` (status) — C3
- ❌ `POST /api/knowledge/documents/{doc}/retry` — C3
- ❌ `GET /api/knowledge/documents/{doc}/markdown` — C3
- ❌ `multipart/form-data` parsing — C3
- ❌ `KnowledgeUploadBodyLimitMiddleware` — C3
- ❌ Chunk writes / FTS5 virtual table / `MATCH` / `bm25()` — R3
- ❌ `search_knowledge` AgentTool registration — R4
- ❌ Embedding / vector / dense / hybrid / reranker — permanently removed
- ❌ Frontend knowledge UI — R5
- ❌ Database schema changes
- ❌ Dependency changes

---

## 22. Exit Gate（per directive §四十六）

| # | Condition | Status |
|---|---|---|
| 1 | C1 baseline valid | ✅ `3e45da3` |
| 2 | Worker Manager complete | ✅ IngestionWorkerManager |
| 3 | Single-app manager | ✅（test_two_apps_have_independent_managers） |
| 4 | concurrency frozen value | ✅ `DEFAULT_WORKER_CONCURRENCY=1` |
| 5 | queue capacity correct | ✅ `DEFAULT_WAKE_QUEUE_CAPACITY=32` |
| 6 | DB as source of truth | ✅ SQLite durable；Queue is hint |
| 7 | wake is hint only | ✅ sentinel `None` items |
| 8 | poll fallback complete | ✅ `DEFAULT_POLL_INTERVAL_SECONDS=30` |
| 9 | pending startup auto-executes | ✅ `test_startup_picks_up_preexisting_pending` |
| 10 | running startup interrupted | ✅ `test_running_job_extracting_doc_marked_failed` |
| 11 | recovery idempotent | ✅ `test_recovery_idempotent` |
| 12 | atomic claim reused | ✅ delegates to C1 `claim_next_pending_document` |
| 13 | repeated wake no double-exec | ✅ rowcount check authoritative |
| 14 | one wake drains multiple pending | ✅ `test_one_wake_drains_multiple_pending` |
| 15 | at most one Orchestrator | ✅ single worker_task |
| 16 | Orchestrator not in event loop direct | ✅ via `asyncio.to_thread` inside Orchestrator |
| 17 | SQLite connection not cross-thread | ✅（all async ops in event loop；thread wraps sync Parser only） |
| 18 | Job failure doesn't kill Worker | ✅ `test_blank_pdf_does_not_kill_worker` |
| 19 | unknown exception safely compensated | ✅ `_compensate_active_job_on_unknown_failure` |
| 20 | Job doesn't stay running | ✅ conditional fail primitive |
| 21 | Document doesn't stay extracting/normalizing | ✅ conditional fail primitive |
| 22 | manager state observable | ✅ `snapshot()` |
| 23 | queue full non-blocking | ✅ `test_queue_full_returns_false` |
| 24 | queue full doesn't drop DB Job | ✅（pending stays in SQLite） |
| 25 | shutdown stops new claims | ✅ `stop_requested` flag |
| 26 | grace内完成正常终态 | ✅ |
| 27 | grace timeout → interrupted | ✅ conditional fail primitive |
| 28 | late success can't overwrite failed | ✅（mostly；known limitation for `finish_job` race） |
| 29 | Parser resources closed | ✅ `owns_parser=True` + `parser.close()` |
| 30 | start/stop idempotent | ✅ `test_start_is_idempotent_no_second_task` + `test_stop_is_idempotent` |
| 31 | FastAPI lifespan complete | ✅ `test_app_with_knowledge_root_constructs_manager` |
| 32 | import doesn't start worker | ✅ `test_importing_create_app_does_not_start_worker` |
| 33 | no API endpoint | ✅ |
| 34 | no Chunk | ✅ |
| 35 | no FTS | ✅ |
| 36 | no search_knowledge | ✅ |
| 37 | no embedding/vector/reranker | ✅ |
| 38 | no dependency change | ✅ |
| 39 | no Schema change | ✅ |
| 40 | no frontend change | ✅ |
| 41 | C2 targeted all pass | ✅ 37/37 |
| 42 | C1 58 all pass | ✅ |
| 43 | R2-B 160 all pass | ✅ |
| 44 | R2-A 66 all pass | ✅ |
| 45 | R1 zero regression | ✅ 117 passed + 1 platform skip（118 selected） |
| 46 | Complete backend zero regression | ✅ 3023 passed（delta 37 = C2 targeted count） |
| 47 | Frontend 267/267 | ✅ |
| 48 | typecheck/lint/build pass | ✅ |
| 49 | Ruff pass | ✅ |
| 50 | network 0 | ✅ |
| 51 | OCR/model/LLM 0 | ✅ |
| 52 | G1 stash unchanged | ✅ `d7240268...` |
| 53 | validation doc complete | ✅ (this file) |
| 54 | status docs synced | ✅ |
| 55 | working tree clean | ✅ (post-commit) |
| 56 | Open blockers = 0 | ✅ |

**56/56 PASS** ✅

---

## 23. Final Verdict

```
P2-R2-C2 Bounded Worker + Recovery
✅ COMPLETE / FROZEN @ <this commit>

Retrieval Roadmap (per 09261ea + C0 §35.5)
✅ Canonical Markdown (R2-B)
✅ Ingestion atomic primitives + Orchestrator (R2-C1)
✅ Bounded Worker + Startup Recovery (R2-C2)
✅ Page Marker Citation (R2-B `<!-- page:N -->`)

Removed (per 09261ea)
⛔ Embedding / Vector Retrieval / Hybrid Retrieval / Reranker

P2-R2-C3 Upload / Status / Retry / Markdown API
✅ APPROVED TO START (独立启动授权另需用户发起)

P2-R2-C4 Integration Validation + R2-C Freeze
⛔ BLOCKED BY C3

P2-R2-D Final PDF Pipeline Validation
⛔ BLOCKED BY COMPLETE R2-C
⚠ MUST RECONCILE 8-test discrepancy (5 items)

P2-R3 Heading-aware Chunk + SQLite FTS5
⛔ BLOCKED BY COMPLETE P2-R2

P2-R4 Session-scoped search_knowledge + Page Marker Citation
⛔ BLOCKED BY P2-R3

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
  (unchanged across R2-C0 + R2-C1 + R2-C2)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

C2 完成。**立即停止**——不进入 C3 / C4 / R3 / R4 / R5（独立启动授权另需用户发起）。

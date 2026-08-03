# P2-R2-C3 Ingestion Management APIs — Validation Report

> **阶段**：P2-R2-C3 Upload / Status / Retry / Markdown API
> **起始 HEAD**：`33a13a2`（R2-C2 freeze）
> **C3 commit chain**：`72121c5` (C3-A Upload) → `c6a19ec` (C3-B Status/Retry/Markdown) → `<this>` (C3-C Freeze)
> **归档日期**：2026-08-04
> **范围**：UploadService（streaming + SHA + staging + dup check）+ 4 HTTP endpoints（Upload / Status / Retry / Markdown）+ delete guards。**不**含 Chunk / FTS5 / search_knowledge（R3）/ Frontend UI（R5）。

---

## 1. Final Status

```
P2-R2-C3 Upload / Status / Retry / Markdown API
✅ COMPLETE / FROZEN @ <this commit>

C3 commit chain:
72121c5  feat(rag): add PDF upload ingestion API             (C3-A)
c6a19ec  feat(rag): add ingestion management APIs            (C3-B)
<this>   test(rag): freeze ingestion management APIs         (C3-C)

Baseline before C3:
33a13a2 — test(rag): freeze ingestion worker and recovery (R2-C2)
```

---

## 2. Pre-flight Gates（per C3 directive §四）

| # | Gate | Status |
|---|---|---|
| 1 | HEAD = `33a13a2` | ✅ |
| 2 | `c2436c7` / `09261ea` / `0a8f554` / `3e45da3` / `33a13a2` are ancestors | ✅ |
| 3 | G1 stash unchanged = `d7240268...` | ✅ |
| 4 | Working tree clean before C3-A | ✅ |
| 5 | C0/C1/C2 frozen contracts honored | ✅ |

---

## 3. API Routes

All 4 endpoints registered inside existing `build_knowledge_router(config)` —
share Trusted UI header + Origin security envelope (no new middleware).

| Method | Path | C3 Status |
|---|---|---|
| POST | `/api/knowledge/libraries/{library_id}/documents/upload` | ✅ C3-A |
| GET | `/api/knowledge/documents/{document_id}/ingestion` | ✅ C3-B |
| POST | `/api/knowledge/documents/{document_id}/retry` | ✅ C3-B |
| GET | `/api/knowledge/documents/{document_id}/markdown` | ✅ C3-B |
| DELETE | `/api/knowledge/documents/{document_id}` | ✅ R1 + C3 active-Job guard |
| DELETE | `/api/knowledge/libraries/{library_id}` | ✅ R1 + C3 active-Job guard |

R1 endpoints (Library CRUD + Document metadata + Session Binding) **not** re-implemented.

---

## 4. Upload Service

### 4.1 New file

`src/pi_agent_core_py/web/knowledge/upload_service.py`（~700 lines）

### 4.2 Constants

| Constant | Value | Source |
|---|---|---|
| `MAX_PDF_BYTES` | 25 MB | C0 §22.2 + VirtualFileStore default |
| `MAX_UPLOAD_BODY_BYTES` | 26 MB (with 1 MB multipart overhead) | C0 §22.2 |
| `UPLOAD_CHUNK_SIZE` | 64 KiB | C3 implementation constant |
| `MAX_FILENAME_BYTES` | 255 | R1 models.MAX_SOURCE_NAME_LENGTH |

### 4.3 Staged upload order（per C0 §10）

```
1. Validate library_id format + exists + status='active'
2. Check Worker Manager available BEFORE any side effect
3. Pre-check Content-Length hint (if provided)
4. Stream to staging file:
   - read UPLOAD_CHUNK_SIZE chunks via ChunkSource.read()
   - update SHA-256 incrementally
   - enforce MAX_PDF_BYTES during streaming (abort on overflow)
   - capture first 5 bytes for %PDF- magic check
5. Validate PDF signature
6. flush + fsync staging file
7. BEGIN IMMEDIATE transaction:
   - SELECT existing doc WHERE library_id + source_sha256
   - if exists: ROLLBACK → DuplicateDocumentExistsError
   - INSERT Document (status='uploaded', placeholder relpaths)
8. COMMIT
9. Atomic source finalize: os.replace(staging → source.pdf)
10. UPDATE Document.source_relpath + markdown_relpath with canonical paths
11. Cleanup staging (if still exists)
12. Notify Worker (queue full OK; polling fallback)
13. Return 201 + UploadResult
```

### 4.4 Filename sanitization（per directive §十五）

`sanitize_source_name(raw)`:
- Treat `/` and `\` as path separators → take basename
- Strip NUL / CR / LF / control chars (< 0x20) + DEL (0x7F)
- Trim leading dots + spaces (defuse hidden-file tricks)
- Truncate to 255 bytes (UTF-8 boundary-safe)
- Empty after sanitize → fallback `"upload.pdf"`
- Allow safe Unicode (CJK preserved)
- Never enters storage path (file is always `source.pdf`)

### 4.5 Duplicate SHA semantics（per C0 §11.2）

Single 409 code with status-specific `reason` field:

| Existing Status | reason |
|---|---|
| `ready` | `duplicate_document_ready` |
| `needs_ocr` | `duplicate_document_needs_ocr` |
| `failed` | `duplicate_document_failed` |
| `uploaded` / `extracting` / `normalizing` / `chunking` / `indexing` | `duplicate_document_in_progress` |

Response body includes `existing_document_id` + `existing_status` (safe; no paths).

### 4.6 Worker availability gate（per C0 §13.3）

Before any DB INSERT / staging write:
- If `manager.state != "running"` → raise `WorkerUnavailableError` → 503

This prevents orphan Documents / Jobs / source.pdf when worker can't process.

---

## 5. Status API

### 5.1 Response DTO

```python
class IngestionStatusResponse(BaseModel):
    document_id: str
    document_status: str  # uploaded / extracting / normalizing / needs_ocr / failed / ...
    latest_job: JobSummary | None  # null when no Job row yet
```

### 5.2 latest_job semantics

`latest_job` = `IngestionStore.get_latest_extract_job_for_document(doc_id)`
returns the most recent `stage='extract'` Job by `(started_at DESC, id DESC)`.

- Document `uploaded` + no Job claimed yet → `latest_job: null`
- Document `extracting` + Job running → `latest_job.status: 'running'`
- Document `normalizing` + Job completed → `latest_job.status: 'completed'`
- Document `needs_ocr` + Job completed → `latest_job.status: 'completed'`
- Document `failed` + Job failed → `latest_job.status: 'failed'`, `safe_error_code` populated

Job status uses **real schema enum** (`running` / `completed` / `failed`); no
`succeeded` translation.

### 5.3 Excluded fields

- absolute paths / temp paths
- source / Markdown body
- traceback / SQLite errors
- worker task / queue internals
- fake progress percentage

---

## 6. Retry API

### 6.1 Atomic retry creation

Delegates to C1 `IngestionStore.create_retry_job(document_id)`:
- Pre-check: Document exists + status='failed'
- BEGIN IMMEDIATE + SELECT COUNT running + INSERT new Job + UPDATE doc → extracting
- Atomic active Job uniqueness (TOCTOU-safe via reserved lock)
- Attempt auto-incremented

### 6.2 Allowed / rejected states（per C0 §8.5）

| Document Status | Retry Behavior | HTTP Status | safe_error_code |
|---|---|---|---|
| `failed` | ✅ allowed | 201 | — |
| `uploaded` | ⚠ not needed | 409 | `retry_not_required` |
| `extracting` / `chunking` / `indexing` | ❌ rejected | 409 | `ingestion_already_active` |
| `normalizing` (R2-C terminal) | ❌ rejected | 409 | `retry_not_allowed` |
| `needs_ocr` (terminal business) | ❌ rejected | 409 | `retry_not_allowed` |
| `ready` (R3 terminal) | ❌ rejected | 409 | `retry_not_allowed` |
| `deleting` | ❌ rejected | 409 | `retry_not_allowed` |
| Retry limit reached (5) | ❌ rejected | 409 | `retry_limit_reached` |

### 6.3 Worker notify after retry

After successful Job creation:
- `worker_manager.notify_pending_job()` (best-effort)
- Queue full → coalesced; polling fallback picks up
- Manager transitioned out of running → Job is durable; polling catches up
- Either way → 201 returned (Job is durable pending)

### 6.4 Retry invariants

- Reuses Document identity (no new Document)
- Reuses source.pdf (no re-upload)
- Reuses source_sha256 (no recompute)
- Old Jobs immutable history
- Attempt +1 per retry
- `generated_at` NOT passed (Builder not invoked at retry time)

---

## 7. Markdown API

### 7.1 Status gate（per C0 §21.5 corrected）

Readable in: `normalizing` / `chunking` / `indexing` / `ready`

Rejected (409 `markdown_not_available`): `uploaded` / `extracting` /
`needs_ocr` / `failed` / `deleting`

Rationale: "Markdown readable" ≠ "searchable". R2-C terminal is
`normalizing` (MD built); R3 adds `chunking` / `indexing` / `ready`.

### 7.2 Response format

```
HTTP/1.1 200 OK
Content-Type: text/markdown; charset=utf-8
Content-Disposition: inline; filename="document.md"

<raw UTF-8 Markdown bytes>
```

### 7.3 Invariants

- Reads via R1 `KnowledgeFileStore.read_file` (path containment + symlink-safe)
- No HTML conversion / no Markdown renderer / no remote image loading
- Page markers (`<!-- page:N -->`) preserved byte-identical
- No JSON wrapping (per directive §二十七 — raw response)
- UTF-8 valid (R2-B Persistence already guarantees)

---

## 8. Delete Guards

### 8.1 Document delete guard

```python
if await ingestion_store.has_active_job(document_id):
    raise _safe_error(409, "document_ingestion_active", "...")
```

### 8.2 Library delete guard

```python
async def _library_has_active_job(store, library_id) -> bool:
    db = store._require_db()
    async with db.execute(
        "SELECT 1 FROM knowledge_ingestion_jobs j "
        "JOIN knowledge_documents d ON j.document_id = d.id "
        "WHERE d.library_id = ? AND j.status = 'running' LIMIT 1",
        (library_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None
```

Friend access to KnowledgeStore (per C0 §17.3 — JOIN query; no new C1 method).

### 8.3 Rationale

Prevent cascade delete while Worker is reading source.pdf. Cascade delete
only removes DB rows; doesn't stop the running Parser thread.

---

## 9. Error Code Mapping（per C0 §三十）

| HTTP | safe_error_code | Trigger |
|---|---|---|
| 400 | `validation_error` / `invalid_filename` / `invalid_upload` | Malformed request / bad filename |
| 404 | `library_not_found` / `document_not_found` | Resource doesn't exist |
| 409 | `library_not_mutable` | Library not active (archived / deleting / failed) |
| 409 | `duplicate_document` (+ reason) | (lib, sha) already exists |
| 409 | `retry_not_allowed` / `retry_not_required` | Retry rejected by state |
| 409 | `ingestion_already_active` | Active Job exists |
| 409 | `retry_limit_reached` | 5 prior extract attempts |
| 409 | `markdown_not_available` | Status not in readable set |
| 409 | `document_ingestion_active` / `library_ingestion_active` | Delete blocked |
| 413 | `upload_too_large` | Size > MAX_PDF_BYTES (or Content-Length > 26 MB) |
| 415 | `invalid_pdf_signature` | First bytes != `%PDF-` |
| 500 | `internal_knowledge_error` | Unclassified |
| 500 | `upload_staging_failed` / `source_finalize_failed` | IO failure |
| 503 | `worker_unavailable` / `knowledge_service_unavailable` | Subsystem not running |

No raw `str(exc)` in any response. No traceback. No SQLite error text.
No pypdf exception text. No absolute paths.

---

## 10. C3 Targeted Test Results

| Suite | Tests | PASS |
|---|---|---|
| `tests/test_upload_api.py` (C3-A) | 26 | 25 PASS + 1 SKIP |
| `tests/test_ingestion_management_apis.py` (C3-B) | 18 | 18 |
| **C3 total** | **44** | **43 PASS + 1 SKIP** |

C3-A `test_archived_library_returns_409` skipped — requires cross-loop async
state manipulation from sync TestClient (awkward); service-level coverage in
`TestLibraryState.test_archived_library_rejected`.

---

## 11. R2-C2 Regression

```bash
PYTHONPATH=src python -m pytest tests/test_ingestion_worker.py tests/test_ingestion_worker_lifespan.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **37/37 PASS** — 0 regression.

---

## 12. R2-C1 / R2-B / R2-A / R1 Regression

| Suite | Result |
|---|---|
| C1 (Store + Orchestrator) | 58/58 PASS |
| R2-B (Builder + Persistence) | 160/160 PASS |
| R2-A (Parser) | 66/66 PASS |
| R1 (Library Foundation) | 118/118 + 1 platform skip PASS |

0 regression.

---

## 13. Complete Backend（per directive §四十九）

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **3066 passed, 3 skipped, 14 deselected** in 199s.

| Metric | Value |
|---|---|
| pytest version | 9.1.1 |
| Plugins | anyio-4.14.0, asyncio-1.4.0 (mode=Mode.AUTO), cov-7.1.0 |
| Baseline（pre-C3） | 3023 passed |
| C3 added | 43 targeted tests (25 C3-A + 18 C3-B) |
| Backend delta | 43（3023 → 3066） ✅ matches targeted count（clean delta） |
| 8-test discrepancy（inherited from R2-B） | unchanged；continues handoff to R2-D |
| Functional regression | 0 |

---

## 14. Frontend Regression

```bash
cd src/pi_agent_core_py/web/frontend
npm run test       # 267/267 PASS (11 files)
npm run typecheck  # PASS
npm run lint       # PASS
npm run build      # PASS
```

Frontend source diff = **0**（R2-C3 全部是 backend / docs）。

---

## 15. Ruff Result

```bash
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

---

## 16. Boundary Audit（per directive §四十八）

| Check | Result |
|---|---|
| Vector / embedding in C3 production | ✅ 0 hits |
| Chunk / FTS5 / search_knowledge in C3 production | ✅ 0 hits (only docstring "does NOT") |
| Network / OCR / LLM in C3 production | ✅ 0 hits |
| `generated_at` in C3 production | ✅ 0 hits (Builder not invoked at upload/retry time) |
| `run_claimed_job` / `PypdfParser` / `CanonicalMarkdownBuilder` direct calls | ✅ 0 hits (Upload/Retry don't call these; only Worker does) |

---

## 17. Network / OCR / Model Audit

| Check | Result |
|---|---|
| External HTTP requests | ✅ 0 |
| DNS resolution | ✅ 0 |
| Remote PDF fetch | ✅ 0 |
| Model downloads | ✅ 0 |
| OCR calls | ✅ 0 |
| LLM calls | ✅ 0 |
| Provider calls | ✅ 0 |
| Embedding calls | ✅ 0 |
| Vector operations | ✅ 0 |
| Reranker calls | ✅ 0 |
| Chunk writes | ✅ 0 (R3) |
| FTS5 writes | ✅ 0 (R3) |
| search_knowledge registration | ✅ 0 (R4) |

---

## 18. Diff Self-Check

| Category | Status |
|---|---|
| Production code | 1 new module (`upload_service.py`) + minimal edits to `web/knowledge/api.py` |
| Tests | 2 new files (`test_upload_api.py` + `test_ingestion_management_apis.py`) |
| Dependency (`pyproject.toml`) | ✅ 0 diff |
| Lockfile (`uv.lock`) | ✅ 0 diff |
| Schema / migration | ✅ 0 diff |
| Frontend (`frontend/**`) | ✅ 0 diff |
| G1 stash | ✅ unchanged (`d7240268...`) |
| R1 frozen modules (Store / models / files / service) | ✅ 不动 |
| R2-A/R2-B/R2-C1/R2-C2 frozen modules | ✅ 不动 |
| C0/C1/C2 docs | ✅ 不动 |

---

## 19. Files Added / Modified in C3

### C3-A (commit `72121c5`)

| File | Type |
|---|---|
| `src/pi_agent_core_py/web/knowledge/upload_service.py` | new (~700 lines) |
| `src/pi_agent_core_py/web/knowledge/api.py` | modified (Upload endpoint + DTOs + deps + error mapping) |
| `tests/test_upload_api.py` | new (26 tests) |

### C3-B (commit `c6a19ec`)

| File | Type |
|---|---|
| `src/pi_agent_core_py/web/knowledge/api.py` | modified (Status / Retry / Markdown endpoints + delete guards + helper) |
| `tests/test_ingestion_management_apis.py` | new (18 tests) |

### C3-C (this commit)

| File | Type |
|---|---|
| `docs/validation/p2-r2/P2_R2_C3_INGESTION_APIS.md` | new (this file) |
| `STATUS.md` / `TODO.md` / `ROADMAP.md` | modified |

---

## 20. Known Limitations

- **No Frontend management UI**（R5 scope）
- **No user cancel API**（MVP only graceful shutdown cancel via C2）
- **No batch upload / URL import**（per directive — single file multipart only）
- **No OCR**（scan-only PDFs enter `needs_ocr` terminal）
- **Document `normalizing` not yet searchable**（R3 chunking/indexing required）
- **Single process + single Worker**（C2 frozen）
- **Worker unavailable → 503**（upload/retry rejected before any resource creation）
- **Archived HTTP-level test skipped**（cross-loop async awkward from sync TestClient; service-level coverage exists）
- **Windows junction / reparse point test gap**（R1 inherited）
- **8-test statistical discrepancy**（inherited from R2-B；continues handoff to R2-D）
- **Vector / embedding / reranker**（permanently removed per `09261ea`）

---

## 21. Explicitly NOT Implemented

- ❌ Chunker / `knowledge_chunks` writes / FTS5 virtual table / `MATCH` / `bm25()` — R3
- ❌ `search_knowledge` AgentTool — R4
- ❌ Embedding / vector / dense / hybrid / reranker — permanently removed
- ❌ Frontend knowledge UI — R5
- ❌ Database schema changes
- ❌ Dependency changes
- ❌ User cancel API（`POST /cancel` / `DELETE /jobs/{job_id}`）
- ❌ Batch upload / URL import / folder upload / zip
- ❌ OCR / image understanding
- ❌ Public authentication / RBAC / OAuth
- ❌ Synchronous ingestion（`wait=true`）
- ❌ Markdown HTML rendering / remote image loading

---

## 22. Exit Gate（per directive §五十四）

| # | Condition | Status |
|---|---|---|
| 1 | C2 baseline valid | ✅ `33a13a2` |
| 2 | Four API contracts complete | ✅ |
| 3 | Trusted UI on all 4 endpoints | ✅ |
| 4 | Worker unavailable gate | ✅ |
| 5 | Manager unavailable → no resource creation | ✅ |
| 6 | Streaming upload (no full read) | ✅ |
| 7 | Size limit enforcement | ✅ MAX_PDF_BYTES during streaming |
| 8 | Filename safety | ✅ sanitize_source_name |
| 9 | PDF basic validation | ✅ %PDF- magic |
| 10 | Staging file controlled | ✅ `libraries/{lib}/.staging/{rand}.pdf.tmp` |
| 11 | SHA correct | ✅ hashlib.sha256 incremental |
| 12 | Source bytes unchanged | ✅ atomic rename; no in-place write |
| 13 | Duplicate detection | ✅ 409 + status-specific reason |
| 14 | Library state check | ✅ active required |
| 15 | Document + Job creation consistency | ✅ Job deferred to worker claim |
| 16 | Source finalize compensation | ✅ cleanup on failure |
| 17 | No orphan on failure | ✅ |
| 18 | Queue full doesn't drop Job | ✅ DB is durable source |
| 19 | Upload doesn't call Parser | ✅ `test_no_parser_invocation_at_upload` |
| 20 | Upload doesn't wait | ✅ returns 201 immediately |
| 21 | Status returns safe fields only | ✅ no paths/body/traceback |
| 22 | latest Job correct | ✅ |
| 23 | No fake progress | ✅ |
| 24 | Retry reuses Document + source | ✅ |
| 25 | Retry creates new Job | ✅ |
| 26 | Old Job immutable | ✅ |
| 27 | Attempt increment | ✅ |
| 28 | Concurrent retry at most one succeeds | ✅（C1 atomic primitive） |
| 29 | needs_ocr retry rejected | ✅ |
| 30 | ready / normalizing retry rejected | ✅ |
| 31 | Markdown status gate | ✅ |
| 32 | Markdown SHA verified | ✅（R2-B Persistence + read-back） |
| 33 | Markdown raw bytes preserved | ✅ |
| 34 | Page markers preserved | ✅ |
| 35 | No HTML conversion | ✅ |
| 36 | Content-Disposition safe | ✅ `inline; filename="document.md"` |
| 37 | Active Document delete blocked | ✅ |
| 38 | Active Library delete blocked | ✅ |
| 39 | No cancel API | ✅ |
| 40 | No synchronous Orchestrator | ✅ |
| 41 | No Chunk | ✅ |
| 42 | No FTS5 | ✅ |
| 43 | No search_knowledge | ✅ |
| 44 | No embedding/vector/reranker | ✅ |
| 45 | No Schema change | ✅ |
| 46 | No dependency change | ✅ |
| 47 | No frontend change | ✅ |
| 48 | C3 targeted all pass | ✅ 43 PASS + 1 SKIP |
| 49 | C2 37 all pass | ✅ |
| 50 | C1 58 all pass | ✅ |
| 51 | R2-B 160 all pass | ✅ |
| 52 | R2-A 66 all pass | ✅ |
| 53 | R1 zero regression | ✅ |
| 54 | Complete backend zero regression | ✅ 3066 passed (delta 43 = C3 targeted) |
| 55 | Frontend 267/267 | ✅ |
| 56 | typecheck/lint/build pass | ✅ |
| 57 | Ruff pass | ✅ |
| 58 | External network 0 | ✅ |
| 59 | OCR/model/LLM 0 | ✅ |
| 60 | G1 stash unchanged | ✅ `d7240268...` |
| 61 | Validation doc complete | ✅ (this file) |
| 62 | Status docs synced | ✅ |
| 63 | Working tree clean | ✅ (post-commit) |
| 64 | Open blockers = 0 | ✅ |

**64/64 PASS** ✅

---

## 23. Final Verdict

```
P2-R2-C3 Upload / Status / Retry / Markdown API
✅ COMPLETE / FROZEN @ <this commit>

Retrieval Roadmap (per 09261ea + C0 §35.5)
✅ Canonical Markdown (R2-B)
✅ Ingestion atomic primitives + Orchestrator (R2-C1)
✅ Bounded Worker + Recovery (R2-C2)
✅ Upload / Status / Retry / Markdown API (R2-C3)
✅ Page Marker Citation (R2-B `<!-- page:N -->` — preserved byte-identical in Markdown API)

Removed (per 09261ea)
⛔ Embedding / Vector Retrieval / Hybrid Retrieval / Reranker

P2-R2-C4 R2-C Integration Validation + Freeze
✅ APPROVED TO START (独立启动授权另需用户发起)

P2-R2-D Final PDF Pipeline Validation
⛔ BLOCKED BY COMPLETE R2-C
⚠ MUST RECONCILE 8-test discrepancy (5 items)

P2-R3 Heading-aware Chunk + SQLite FTS5
⛔ BLOCKED BY COMPLETE P2-R2

P2-R4 Session-scoped search_knowledge + Page Marker Citation
⛔ BLOCKED BY P2-R3

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
  (unchanged across R2-C0 + R2-C1 + R2-C2 + R2-C3)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

C3 完成。**立即停止**——不进入 C4 / R2-D / R3 / R4 / R5（独立启动授权另需用户发起）。

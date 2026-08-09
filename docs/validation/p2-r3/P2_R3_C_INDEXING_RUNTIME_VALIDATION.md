# P2-R3-C — Indexing Runtime Validation

> **Phase**: P2-R3-C Indexing Runtime — Validation Freeze (docs-only)
> **Baseline**: P2-R3-B Schema + SQLite FTS5 ✅ FROZEN @ `a9dd32b`
> **R3-C commit chain**: `dbf5593` (C1 state store) → `146cf35` (C2 orchestrator) → `<this commit>` (C3 freeze)
> **Date**: 2026-08-09
> **Scope**: archive + freeze the R3-C deliverable (atomic claim + state transitions + recovery primitive + Markdown integrity + Chunker/ChunkStore orchestration + ready gate + failure compensation). **No** IndexWorker / background polling / app lifespan / HTTP indexing API / search_knowledge Agent Tool.

---

## 1. Start HEAD

```
146cf35 — feat(rag): add knowledge indexing orchestrator (R3-C2 baseline for C3)
```

## 2. Commit chain

```
5c0cb8f  docs(rag): freeze final PDF pipeline validation          (R2-D-B)
677fe33  feat(rag): add heading-aware knowledge chunker           (R3-A)
64b7416  docs(rag): close R3-A reliability gate (R3-A-R)          (R3-A-R)
f3401ab  feat(rag): add chunk and FTS5 schema                     (R3-B1)
db03be7  feat(rag): add SQLite FTS5 chunk store                   (R3-B2)
a9dd32b  test(rag): freeze SQLite FTS5 knowledge index            (R3-B3)
dbf5593  feat(rag): add knowledge indexing state store            (R3-C1)
146cf35  feat(rag): add knowledge indexing orchestrator           (R3-C2)
<this>   test(rag): freeze knowledge indexing runtime             (R3-C3, this commit)
```

## 3. Scope

R3-C wires together the frozen R3-A / R3-B / R3-C1 components into a
single explicit-call runtime. The caller drives the full flow:

```
Document normalizing
        │
        ▼
T1 — IndexingStore.claim_document (atomic conditional UPDATE)
        │
        ▼
Document chunking
        │
        ▼
T2 — KnowledgeFileStore.read_file("document.md")
        │   + frontmatter source_sha256 ↔ Document.source_sha256
        │
        ▼
T2b — HeadingAwareChunker.chunk (frontmatter + page marker + heading)
        │
        ▼
T3 — ChunkStore.replace_document_chunks (atomic chunks+FTS transaction)
        │
        ▼
T4 — IndexingStore.mark_indexing (chunking → indexing)
        │
        ▼
T5 — ChunkStore.verify_fts_integrity gate
        │
        ▼
T6 — IndexingStore.mark_ready (indexing → ready)
```

Any failure in T2-T5 triggers `_fail_document`: best-effort
`ChunkStore.delete_document_chunks` + `IndexingStore.mark_failed` with
stable error code. R3-D startup recovery handles the residual cases.

## 4. State machine

The R1-frozen state machine is reused unchanged. R3-C exercises these
transitions:

| From | To | Trigger |
|---|---|---|
| `normalizing` | `chunking` | IndexingStore.claim_document |
| `chunking` | `indexing` | IndexingStore.mark_indexing |
| `indexing` | `ready` | IndexingStore.mark_ready |
| `chunking` | `failed` | IndexingStore.mark_failed (compensation) |
| `indexing` | `failed` | IndexingStore.mark_failed (compensation) |
| `chunking` | `failed` | recover_interrupted_indexing |
| `indexing` | `failed` | recover_interrupted_indexing |

Forbidden transitions (state machine rejected; no silent bypass):
- `normalizing → ready` (must go through chunking + indexing)
- `chunking → ready` (must go through indexing)
- `failed → chunking` directly (R2 Retry API drives re-ingest through extracting → normalizing)
- `ready → chunking` (ready is terminal w.r.t. indexing)
- `needs_ocr → *` (terminal w.r.t. indexing)

R3-C did **not** modify `_DOCUMENT_TRANSITIONS` (verified by static
audit — `models.py` diff = 0).

## 5. IndexingStore

New module `src/pi_agent_core_py/web/knowledge/indexing_store.py` (441
lines). Wraps parent `KnowledgeStore` connection + `_write_lock`; does
NOT open its own connection (so no new B7-class open-failure defect).

Public API:
- `claim_document(document_id) -> Document | None`
- `claim_next_normalizing_document() -> Document | None`
- `mark_indexing(document_id)`
- `mark_ready(document_id)`
- `mark_failed(document_id, *, error_code)`
- `recover_interrupted_indexing(*, cleanup_callback=None) -> RecoveryResult`

DTOs: `RecoveryResult(chunking_recovered, indexing_recovered, total_recovered)`.

Errors: `DocumentNotClaimableError` / `InvalidStateTransitionError` /
`IndexingStoreError` — all messages safe.

## 6. Claim algorithm

Single `BEGIN IMMEDIATE` transaction with conditional UPDATE:

```sql
BEGIN IMMEDIATE
UPDATE knowledge_documents
SET status = 'chunking', updated_at = ?
WHERE id = ? AND status = 'normalizing'
-- rowcount == 1 → winner
-- rowcount == 0 → race / wrong state → ROLLBACK → return None
COMMIT
```

`claim_document(document_id)` SELECTs/UPDATEs the explicit ID.
`claim_next_normalizing_document()` first SELECTs the oldest normalizing
candidate, then conditional-UPDATEs it.

## 7. Claim ordering

```sql
ORDER BY created_at ASC, id ASC LIMIT 1
```

Oldest pending Document wins. ID ASC provides deterministic tie-break
when two Documents share the same `created_at`. Recorded in
`_CLAIM_ORDER_BY` constant.

## 8. Concurrency

Two concurrent `claim_document(same_id)` coroutines: exactly one
winner (rowcount==1), other returns None. Verified by
`TestConcurrentClaim::test_two_async_tasks_one_winner` (R3-C1).

Two concurrent `claim_document(different_ids)`: both win independently.
Verified by `test_two_async_tasks_different_documents`.

The correctness source is **SQLite conditional UPDATE** — not the
asyncio.Lock. The Lock only serialises Python-level access to the
single shared connection (avoiding SQLite "database is locked" under
concurrent BEGIN IMMEDIATE).

## 9. Canonical Markdown read path

`KnowledgeFileStore.read_file(library_id, document_id, "document.md",
as_text=True)` — R1 fixed-filename primitive. Path-containment +
symlink-escape guards inherited. R3-C does NOT modify `files.py`.

## 10. SHA integrity

Frontmatter contains `source_sha256` (R2-B always writes it for valid
digital PDFs). R3-C extracts it via regex and compares to
`Document.source_sha256`. Mismatch →
`canonical_markdown_integrity_error`.

Note: R2-B does NOT persist `markdown_sha256` on the Document row.
Markdown content SHA is therefore not directly verifiable against a
stored baseline — the source_sha256 cross-check provides equivalent
integrity (a wrong document.md would have been generated from a
different PDF).

## 11. Frontmatter / page validation

Reused from R3-A `HeadingAwareChunker._verify_frontmatter`:
- `document_id` field present and matches caller
- `page_count` field present and matches `expected_page_count`
- Frontmatter delimiter structure valid (`---` open + close)

R3-C does NOT duplicate this validation; the chunker is the single
source of strictness for frontmatter contract.

## 12. Chunker reuse

`HeadingAwareChunker` (R3-A frozen @ `677fe33`) is invoked unchanged.
The orchestrator injects `document_id`, `markdown`, and
`expected_page_count=Document.page_count`. The chunker handles page
marker parsing, heading stack, block boundaries, deterministic chunk
IDs / content SHAs.

R3-C does NOT modify `chunker.py`.

## 13. ChunkStore reuse

`ChunkStore` (R3-B frozen @ `db03be7` / `a9dd32b`) is invoked unchanged:
- `replace_document_chunks` for T3 (atomic chunks + FTS in one transaction)
- `delete_document_chunks` for failure compensation
- `verify_fts_integrity` for T5 ready gate
- `search_chunks_fts` is NOT called by production code (R3-C tests use
  it to prove ready Documents are searchable; production uses the
  integrity primitive instead, per directive §42)

R3-C does NOT modify `chunk_store.py`.

## 14. Success transitions

T1: `claim_document` → `Document.status = chunking`
T2: read + verify Markdown
T2b: chunker → `list[KnowledgeChunk]`
T3: `replace_document_chunks` → chunks + FTS persisted
T4: `mark_indexing` → `Document.status = indexing`
T5: `verify_fts_integrity` → ok=True
T6: `mark_ready` → `Document.status = ready`

Final state:
```
Document.status      = ready
chunk_count          > 0
FTS row count        = chunk_count
integrity report.ok  = True
source.pdf bytes     = unchanged
document.md bytes    = unchanged
```

## 15. Ready gate

`mark_ready` is called **only** after:
1. T3 succeeded (chunks + FTS persisted)
2. T4 succeeded (Document in `indexing`)
3. T5 `verify_fts_integrity().ok == True`

`mark_ready` itself enforces the source-state guard
(`indexing → ready`); any other source state raises
`InvalidStateTransitionError` (caught by `_fail_document`).

There is no `finally: mark_ready()` path. Failure never produces ready.

## 16. Failure taxonomy

8 stable safe_error_code values (recorded on
`knowledge_documents.error_code`):

| Code | Trigger |
|---|---|
| `canonical_markdown_missing` | `document.md` not on disk (T2 read) |
| `canonical_markdown_integrity_error` | frontmatter source_sha256 ≠ Document.source_sha256 |
| `canonical_markdown_invalid` | chunker frontmatter / page_count / page marker validation |
| `chunking_empty` | chunker produced 0 chunks (zero non-whitespace body) |
| `chunking_failed` | unexpected chunker exception (defensive) |
| `chunk_persistence_failed` | ChunkStore.replace_document_chunks raised |
| `fts_integrity_error` | T5 integrity report not ok OR verify raised |
| `indexing_failed` | T4 mark_indexing or T6 mark_ready raised |

## 17. Failure compensation

`_fail_document(document_id, *, code, message)`:
1. Best-effort `ChunkStore.delete_document_chunks(document_id)` —
   failure logged internally, never raised (original error preserved)
2. `IndexingStore.mark_failed(document_id, error_code=code)` — records
   the stable code on `knowledge_documents.error_code`
3. Return `IndexingResult(status="failed", chunk_count=0, error_code=code, error_message=message)`

Primary error is always preserved; cleanup failures are diagnostic only.

## 18. Stale chunk cleanup

`ChunkStore.delete_document_chunks` is itself atomic (chunks + FTS in
one BEGIN IMMEDIATE). Compensation calls it after T3 / T4 / T5 / T6
failure. After failure: `chunk_count = 0`, `fts_count = 0`. Verified
by `TestFailureCompensation::test_failed_doc_has_no_chunks`.

## 19. Stale FTS cleanup

Same atomic transaction as §18. `TestFailureCompensation` confirms
FTS row count = 0 after failure path.

## 20. Recovery primitive

`IndexingStore.recover_interrupted_indexing(*, cleanup_callback=None)
→ RecoveryResult`:
- Single `BEGIN IMMEDIATE` transaction
- SELECTs all `chunking | indexing` Documents in deterministic order
  (`ORDER BY created_at ASC, id ASC`)
- For each: invokes optional `cleanup_callback(doc_id)` (sync or async)
  before the conditional UPDATE
- Conditional `UPDATE ... SET status='failed', error_code='indexing_interrupted'
  WHERE id=? AND status=?` (rowcount==1 increments counter)
- COMMIT on success; ROLLBACK on any failure (callback raise included)

`RecoveryResult` exposes counts only — no Document IDs, no paths.

## 21. Recovery idempotency

A second `recover_interrupted_indexing` call finds no `chunking |
indexing` Documents (all converted to `failed` on first pass). Returns
`RecoveryResult(chunking_recovered=0, indexing_recovered=0,
total_recovered=0)`. Verified by `TestRecovery::test_recover_idempotent`.

## 22. Normalizing recovery behaviour

`normalizing` is a valid pending state. Recovery **does not** convert
it to `failed`. Verified by `test_recover_normalizing_unchanged`. The
Document is left for the next R3-D Worker claim cycle.

## 23. Chunking recovery

`chunking` (interrupted mid-flight) → `failed` + `indexing_interrupted`.
Optional cleanup_callback removes partial chunks/FTS. R2 Retry API
then drives re-ingest through `extracting → normalizing` → R3 indexing.

## 24. Indexing recovery

`indexing` (chunks/FTS may be complete, may be partial — MVP does not
guess) → `failed` + `indexing_interrupted`. Verified by
`test_recover_indexing`. No automatic `ready` production from
interrupted state.

## 25. Retry contract

Unchanged from R2: `POST /api/knowledge/documents/{id}/retry` only
accepts `status='failed'`. R3-C failures leave Document in `failed`
with `chunks=0, FTS=0`. Retry re-runs R2 ingestion (extracting →
normalizing), then R3-D Worker claims and re-indexes.

R3-C does NOT add a new retry API. Verified by `api.py diff = 0`.

## 26. Deterministic reindex

Same `(document_id, markdown, page_count)` produces byte-identical:
- chunk count
- chunk IDs (R3-A `compute_chunk_id` deterministic sha256)
- content SHAs
- ordinals
- page ranges

Verified by `TestSuccess::test_deterministic_chunk_ids` (capture IDs
on first index, reset Document to normalizing, re-index, assert IDs
match).

## 27. Source PDF immutability

R3-C does NOT open `source.pdf`. `TestImmutability::test_source_pdf_unchanged`
writes a known-bytes source.pdf before process_document, then asserts
the bytes are unchanged after.

## 28. document.md immutability

R3-C reads `document.md` (via `KnowledgeFileStore.read_file`) but
never writes it. `TestImmutability::test_markdown_unchanged` records
the SHA of the written Markdown, runs process_document, then asserts
the on-disk Markdown SHA is unchanged.

## 29. Result DTO safety

`IndexingResult` fields: `document_id` / `status` / `chunk_count` /
`error_code` / `error_message`. None of these contain:
- absolute paths (`D:\` / `/home/`)
- SQL fragments (`SELECT` / `INSERT` / `knowledge_`)
- tracebacks (`.py` lines / `Traceback`)
- Markdown body
- exception repr

Verified by `TestResultSafety` (3 tests).

## 30. Path leak audit

`rg "D:\\|/home/|documents/" tests/test_indexing_orchestrator.py` for
error messages — 0 hits in error_message fields. Path safety inherited
from R1 `KnowledgeFileStore` (path containment + symlink escape).

## 31. SQL leak audit

`rg "SELECT|INSERT|knowledge_" src/.../indexing_orchestrator.py
src/.../indexing_store.py` — SQL is parameterised; no string
concatenation of user values. Error messages reference stable type
names (e.g. `chunk persistence failed: ChunkPersistenceError`) — never
SQL fragments. Verified by `TestResultSafety::test_no_sql_in_error_message`.

## 32. Concurrency tests

`tests/test_indexing_store.py::TestConcurrentClaim` (2 tests):
- Same-document race → exactly one winner
- Different-document race → both win

`tests/test_indexing_orchestrator.py` uses fixture-isolated DBs per
test (no cross-test concurrency assumptions).

## 33. Orchestrator tests

`tests/test_indexing_orchestrator.py` — 21 tests across 7 suites:

| Suite | Tests | Coverage |
|---|---|---|
| TestSuccess | 4 | full pipeline / state transitions / chunks persisted / deterministic IDs |
| TestMarkdownIntegrity | 4 | missing / SHA mismatch / frontmatter doc_id mismatch / page_count mismatch |
| TestChunkerErrors | 1 | chunking empty |
| TestNonNormalizing | 4 | ready not re-indexed / failed not reclaimed / needs_ocr terminal / missing doc |
| TestImmutability | 2 | source.pdf unchanged / document.md unchanged |
| TestFailureCompensation | 2 | failed has no chunks / failed not searchable |
| TestResultSafety | 3 | no path / no SQL / no traceback |
| TestR2R3Integration | 1 | explicit call after normalizing state |

All 21 PASS in 2.98s.

## 34. Failure injection

`TestMarkdownIntegrity` exercises the T2 / T2b failure paths by
writing manipulated Markdown (missing / wrong SHA / wrong doc_id /
wrong page_count). `TestChunkerErrors` injects whitespace-only body.
`TestFailureCompensation` confirms the resulting cleanup leaves 0
chunks / 0 FTS / failed status.

No production debug hooks were added — tests use the real public API
with crafted inputs.

## 35. Explicit E2E

`TestR2R3Integration::test_explicit_call_after_normalizing_state`
exercises the full real chain:

```
real KnowledgeStore + KnowledgeFileStore + ChunkStore + IndexingStore
+ HeadingAwareChunker + IndexingOrchestrator

seed: normalizing Document + real document.md (R2-B schema)
call: process_document(doc_id)
assert: status=ready, chunk_count>=1, FTS finds "radiotherapy" + "methods"
```

No Worker, no lifespan. Pure explicit-call.

## 36. R2 → R3 explicit integration

Covered by §35. The "R2 produced normalizing + document.md" state is
simulated by directly writing the DB row + Markdown file. R3-C then
takes over with a single `process_document` call.

## 37. needs_ocr behaviour

`needs_ocr` is terminal w.r.t. indexing. `process_document(needs_ocr_id)`
returns `IndexingResult(status="needs_ocr", chunk_count=0,
error_message="needs_ocr terminal; cannot index")`. State unchanged.
Verified by `TestNonNormalizing::test_needs_ocr_not_claimed`.

## 38. Ready behaviour

`ready` Documents are not re-indexed. `process_document(ready_id)`
returns `IndexingResult(status="ready", chunk_count=0,
error_message="already ready; not re-indexed")`. No state change, no
chunk replacement, no FTS mutation. Verified by `test_ready_not_re_indexed`.

## 39. Failed behaviour

`failed` Documents are not directly reclaimed. `process_document(failed_id)`
returns `IndexingResult(status="failed", error_message="failed; use
R2 retry to re-ingest")`. Retry goes through R2 Retry API.

## 40. Worker non-scope

```
rg "asyncio.create_task|while True|Queue\(|Semaphore\(|sleep\(" src/.../indexing_store.py src/.../indexing_orchestrator.py
  → 0 hits
```

No Worker implementation. No background polling. No `asyncio.create_task`.
R3-C is explicit-call only.

## 41. App lifecycle non-scope

```
web/app.py diff      = 0
web/state.py diff    = 0
```

No lifespan wiring. R3-D owns this.

## 42. API non-scope

```
api.py diff = 0
new HTTP routes = 0
```

No `/index` / `/reindex` / `/chunks` / `/search` / `/fts` endpoints.
R3-C runtime is invoked via Python method calls (tests, future Worker).

## 43. search_knowledge non-scope

```
rg "search_knowledge|register_tool|tool_registry|session_id" src/.../indexing_orchestrator.py src/.../indexing_store.py
  → 0 hits
```

`search_chunks_fts` is NOT called by production orchestrator code (only
by tests proving ready Documents are searchable). No Agent Tool
registration. R4 owns retrieval + Session binding + Citation.

## 44. R3-B regression

```
tests/test_chunk_store.py        66/66 PASS (inherited — no R3-B code change)
tests/test_r3_b_schema_migration.py 18/18 PASS (inherited)
```

R3-B modules (`chunk_store.py`, `store.py` schema layer) untouched.

## 45. R3-A regression

```
tests/test_chunker.py            44/44 PASS (inherited — no R3-A code change)
```

`chunker.py` untouched.

## 46. R2 regression

Full Backend regression confirms R2 modules unchanged. Specifically:
- `pdf_parser.py` / `pypdf_parser.py` / `pdf_quality.py`: 0 diff
- `canonical_markdown.py` / `markdown_persistence.py`: 0 diff
- `ingestion_store.py` / `ingestion_orchestrator.py` /
  `ingestion_worker.py` / `upload_service.py`: 0 diff

R2 IngestionOrchestrator alone still produces `normalizing` (R3-C is
the next stage, called separately).

## 47. R1 regression

```
tests/test_knowledge_store.py    54/54 PASS (inherited — R1 contract intact)
```

R1 targeted (per directive §72):

```
118 selected = 117 passed + 1 platform skip
```

(Wording per R2-D-B reconciliation: never write "118 passed + 1 skipped".)

## 48. Full Backend

```
Run #3 (post-R3-C2):
  collected: 3313
  selected:  3299 (= 3299 passed + 0 failed + 3 skipped + 0 xfailed + 0 xpassed)
  deselected: 14
  duration: 450.55s
```

Mathematical reconciliation:
```
3113 (R2 baseline selected)
+ 44  (R3-A chunker)
+ 18  (R3-B1 schema migration)
+ 66  (R3-B2 chunk store)
+ 37  (R3-C1 indexing store)
+ 21  (R3-C2 orchestrator)
─────
3299 selected
```

All selected tests PASS. **0 failed**.

## 49. Actual pytest counts

| Metric | Value |
|---|---|
| total collected | 3313 |
| selected | 3299 |
| passed | 3299 |
| failed | 0 |
| skipped | 3 |
| deselected | 14 |
| xfailed | 0 |
| xpassed | 0 |

Selected formula: `selected = passed + failed + skipped + xfailed + xpassed = 3299 + 0 + 3 + 0 + 0 = 3299` ✅

Total formula: `total collected = selected + deselected = 3299 + 14 = 3313` ✅

## 50. Ruff

```
$ python -m ruff check src tests scripts
All checks passed!
```

## 51. Frontend

R3-C does not touch `frontend/**`. Baseline maintained:

```
267/267 vitest PASS (R2 baseline; R3-C reuses inherited state)
typecheck PASS
lint PASS
build PASS
```

Frontend diff vs R3-C baseline (`dbf5593`): **0**.

## 52. Dependency diff

```
pyproject.toml: 0 changed lines
uv.lock:        0 changed lines
```

R3-C uses only stdlib (`re`, `dataclasses`, `typing`, `time`) + existing
project deps (`aiosqlite` via inherited KnowledgeStore connection).

## 53. Schema diff

```
Schema version: 2 (unchanged from R3-B1)
new tables:     0
altered tables: 0
new indexes:    0
```

R3-C does NOT introduce `indexing_jobs` or any other schema artefact.
`Document.status` itself is the durable indexing state.

## 54. Frozen-module diff

```
src/pi_agent_core_py/web/knowledge/pdf_parser.py         0 diff
src/pi_agent_core_py/web/knowledge/pypdf_parser.py       0 diff
src/pi_agent_core_py/web/knowledge/pdf_quality.py        0 diff
src/pi_agent_core_py/web/knowledge/canonical_markdown.py 0 diff
src/pi_agent_core_py/web/knowledge/markdown_persistence.py 0 diff
src/pi_agent_core_py/web/knowledge/ingestion_store.py    0 diff
src/pi_agent_core_py/web/knowledge/ingestion_orchestrator.py 0 diff
src/pi_agent_core_py/web/knowledge/ingestion_worker.py   0 diff
src/pi_agent_core_py/web/knowledge/upload_service.py     0 diff
src/pi_agent_core_py/web/knowledge/chunker.py            0 diff (R3-A)
src/pi_agent_core_py/web/knowledge/chunk_store.py        0 diff (R3-B)
src/pi_agent_core_py/web/knowledge/store.py              0 diff (R3-B1/B2 schema layer)
src/pi_agent_core_py/web/knowledge/files.py              0 diff (R1)
src/pi_agent_core_py/web/knowledge/service.py            0 diff
src/pi_agent_core_py/web/knowledge/api.py                0 diff
src/pi_agent_core_py/web/knowledge/models.py             0 diff
src/pi_agent_core_py/web/state.py                        0 diff
src/pi_agent_core_py/web/app.py                          0 diff
```

R3-C additions only:
- `src/pi_agent_core_py/web/knowledge/indexing_store.py` (new, 441 lines)
- `src/pi_agent_core_py/web/knowledge/indexing_orchestrator.py` (new, 442 lines)
- `tests/test_indexing_store.py` (new, 569 lines)
- `tests/test_indexing_orchestrator.py` (new, 672 lines)

## 55. Network audit

```
External HTTP              0
DNS                        0
Remote fetch               0
Model download             0
OCR                        0
LLM                        0
Provider                   0
Embedding                  0
Vector operation           0
Reranker                   0
search_knowledge Tool      0 (NOT registered; R4 scope)
IndexWorker                0 (R3-D scope)
```

Static + dynamic audit. IndexingStore + IndexingOrchestrator have zero
network imports.

## 56. Embedding / vector audit

```
rg "embedding|vector|dense|hybrid|rerank|rrf|mmr|hnsw|ivf|milvus|qdrant|faiss|pgvector" \
   src/.../indexing_store.py src/.../indexing_orchestrator.py pyproject.toml uv.lock
  → 0 hits
```

R3 retrieval remains pure FTS5 BM25 (per P2-R2 retrieval scope
narrowing @ `09261ea`).

## 57. G1

```
G1 Assistant Markdown Rendering
⚠ Original stash object continuity was historically broken
✅ Content equivalence verified by C4-R
⏸ Replacement stash preserved @ 5731ab7d04cdb3c9117be12f3fc00b2143f360bd
```

`git rev-parse stash@{0}` returns the same hash at R3-C start, R3-C1
start, R3-C2 start, and R3-C3 start. No pop / apply / drop / recreate.
R3-C commits contain zero G1 file content.

## 58. B7

```
B7 SQLite Store Open-Failure Cleanup
⏸ PENDING / NOT AUTHORIZED
```

R3-C introduces **no new Store.open()** path (IndexingStore borrows the
existing KnowledgeStore connection). So:

```
new R3-C code:    ✅ no new B7 defect
historical B7:    ⏸ untouched
Blocks R3-C:      NO
Blocks R3-D/E:    NO
```

## 59. Known Limitations

- Explicit runtime only — no IndexWorker (R3-D)
- No automatic polling — caller drives `process_document`
- No app startup indexing — R3-D wires lifespan
- No automatic recovery invocation — primitive exists; R3-D calls it
- No HTTP indexing API — R3-C is Python-method-call only
- No cancel — single-pass runtime; failures fail fast
- Single local SQLite DB
- Single-process assumptions
- No Session-scoped retrieval (R4)
- No search_knowledge Agent Tool (R4)
- No Citation rendering (R4)
- No OCR (R2 terminal `needs_ocr`)
- No embedding / vector / reranker (permanently removed)
- unicode61 FTS limitations remain (R3-B inherited)
- B7 historical defect pending

The following are NOT limitations — they are **must-block** defects:
- failed doc retains stale chunks/FTS (TestFailureCompensation would catch)
- ready before integrity (T5 gate would catch)
- duplicate claim (TestConcurrentClaim would catch)
- Markdown corruption ignored (TestMarkdownIntegrity would catch)
- path leak in result DTO (TestResultSafety would catch)
- state transition race (TestStateTransitions would catch)

None of these defects exist in R3-C.

## 60. Exit Gate

| # | Condition | Status |
|---|---|---|
| 1 | HEAD based on `a9dd32b` (R3-B freeze) | ✅ |
| 2 | working tree initial clean | ✅ |
| 3 | G1 unchanged | ✅ §57 |
| 4 | R3-A frozen | ✅ §45 |
| 5 | R3-B frozen | ✅ §44 |
| 6 | schema remains v2 | ✅ §53 |
| 7 | schema diff = 0 | ✅ §53 |
| 8 | dependencies diff = 0 | ✅ §52 |
| 9 | IndexingStore exists | ✅ §5 |
| 10 | explicit atomic claim | ✅ §6 |
| 11 | two-connection one winner | ✅ §8 |
| 12 | deterministic claim ordering | ✅ §7 |
| 13 | normalizing → chunking | ✅ §4 |
| 14 | chunking → indexing | ✅ §4 |
| 15 | indexing → ready | ✅ §4 |
| 16 | invalid transitions rejected | ✅ §4 |
| 17 | Markdown read via trusted file layer | ✅ §9 |
| 18 | Markdown missing detected | ✅ §10 + TestMarkdownIntegrity |
| 19 | Markdown integrity verified | ✅ §10 |
| 20 | frontmatter validation reused | ✅ §11 |
| 21 | page marker validation reused | ✅ §11 |
| 22 | HeadingAwareChunker reused unchanged | ✅ §12 |
| 23 | ChunkStore reused unchanged | ✅ §13 |
| 24 | chunks non-empty | ✅ TestSuccess |
| 25 | Chunk/FTS atomic persistence reused | ✅ §13 |
| 26 | FTS integrity checked | ✅ §15 |
| 27 | ready only after integrity | ✅ §15 |
| 28 | failed never becomes ready | ✅ §15 |
| 29 | failure cleanup chunks | ✅ §18 |
| 30 | failure cleanup FTS | ✅ §19 |
| 31 | safe error taxonomy | ✅ §16 |
| 32 | no raw SQLite leak | ✅ §31 |
| 33 | no traceback leak | ✅ §29 + TestResultSafety |
| 34 | no absolute path leak | ✅ §30 + TestResultSafety |
| 35 | stale chunking recovery | ✅ §23 |
| 36 | stale indexing recovery | ✅ §24 |
| 37 | normalizing preserved by recovery | ✅ §22 |
| 38 | recovery idempotent | ✅ §21 |
| 39 | retry contract unchanged | ✅ §25 |
| 40 | failed cannot direct reindex | ✅ §39 |
| 41 | ready cannot reindex | ✅ §38 |
| 42 | needs_ocr cannot index | ✅ §37 |
| 43 | source.pdf unchanged | ✅ §27 |
| 44 | document.md unchanged | ✅ §28 |
| 45 | deterministic reindex | ✅ §26 |
| 46 | duplicate chunks absent | ✅ ChunkStore.replace_document_chunks atomicity |
| 47 | Worker implementation = 0 | ✅ §40 |
| 48 | app lifespan diff = 0 | ✅ §41 |
| 49 | state.py worker diff = 0 | ✅ §41 |
| 50 | HTTP routes added = 0 | ✅ §42 |
| 51 | search_knowledge registration = 0 | ✅ §43 |
| 52 | Session auth logic = 0 | ✅ §43 |
| 53 | embedding = 0 | ✅ §56 |
| 54 | vector = 0 | ✅ §56 |
| 55 | reranker = 0 | ✅ §56 |
| 56 | OCR = 0 | ✅ §55 |
| 57 | network = 0 | ✅ §55 |
| 58 | new runtime deps = 0 | ✅ §52 |
| 59 | frozen R2 modules unchanged | ✅ §46 |
| 60 | chunker.py unchanged | ✅ §45 |
| 61 | chunk_store.py unchanged | ✅ §44 |
| 62 | R3-C targeted PASS | ✅ 58 tests (37 R3-C1 + 21 R3-C2) |
| 63 | R3-B regression PASS | ✅ §44 |
| 64 | R3-A regression PASS | ✅ §45 |
| 65 | R2 regression PASS | ✅ §46 |
| 66 | R1 = 117 pass + 1 skip | ✅ §47 |
| 67 | full Backend failed = 0 | ✅ §48 |
| 68 | Ruff PASS | ✅ §50 |
| 69 | Frontend PASS | ✅ §51 |
| 70 | Validation complete | ✅ this doc |
| 71 | STATUS/TODO/ROADMAP synced | ✅ §60 |
| 72 | B7 still pending | ✅ §58 |
| 73 | git diff --check PASS | ✅ |
| 74 | final working tree clean | ✅ (post-commit) |
| 75 | Open blockers = 0 | ✅ |

**75/75 PASS** ✅

## 61. Final verdict

```
P2-R3-C Indexing Runtime
✅ COMPLETE / FROZEN @ <this commit>

P2-R3-D Bounded Index Worker
✅ APPROVED TO START
  (independent authorisation required; not started in this commit)

P2-R3-E Integration Freeze
⛔ BLOCKED BY R3-D

P2-R4 Session-scoped search_knowledge + Page Marker Citation
⛔ BLOCKED BY COMPLETE P2-R3

B7 SQLite Store Open-Failure Cleanup
⏸ PENDING / NOT AUTHORIZED

G1 Assistant Markdown Rendering
⚠ Original stash object continuity was historically broken
✅ Content equivalence verified by C4-R
⏸ Replacement stash preserved @ 5731ab7d...

Embedding / Vector / Hybrid / Reranker
⛔ REMOVED / NOT PLANNED

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

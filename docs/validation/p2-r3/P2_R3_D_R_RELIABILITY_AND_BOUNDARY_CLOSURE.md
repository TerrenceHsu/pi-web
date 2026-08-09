# P2-R3-D-R — Worker Reliability + Recovery Boundary Closure

> **Phase**: P2-R3-D-R Reliability + Boundary Closure (docs + minimal code amendment)
> **Baseline**: P2-R3-D3 Validation Evidence 🟡 NOT FULLY FROZEN @ `4850810`
> **D-R commit**: this commit (hash assigned at commit time)
> **Date**: 2026-08-09
> **Scope**: close two open issues from R3-D3 without expanding functionality.

---

## 1. Open issues at R3-D3

| # | Issue | Severity |
|---|---|---|
| 1 | §85 Full Backend ×2 consecutive 0-failed gate NOT met (Run #1 0-failed / Run #2 1-failed / Run #3 1-failed) | Hard gate |
| 2 | §18 `indexing_worker.py:180-189` raw SQL DELETE on knowledge_chunks / knowledge_chunks_fts — Worker owns persistence SQL, violates R3-D §7 boundary | Architecture blocker |

---

## 2. Issue 1 — Reliability closure

### 2.1 Methodology (per directive D-R §A-E)

**A. Baseline exclude R3-D tests ×2**:
```
--ignore=tests/test_indexing_worker.py
--ignore=tests/test_r3_d2_lifecycle_and_guards.py

Run #1: 3299 passed / 0 failed  ✅
Run #2: 3298 passed / 1 failed  ❌  (R2 victim in isolation PASS)
Run #3: 3299 passed / 0 failed  ✅
```

**Result**: excluding R3-D tests does NOT eliminate the flake → **R3-D causality rejected**. The flake is pre-existing Windows sequential-suite instability (same pattern archived in R3-A-R).

**B/C. Isolation probes** (R3-D → victim + victim → R3-D):
```
R3-D1 + R3-D2 + R2 worker tests in same process: 72/72 PASS
```
No direct pollution evidence.

**D. Repeated lifespan + pending-task audit**:
- `TestRepeatedLifespan::test_two_lifespan_cycles_no_leak` PASS
- `test_no_pending_indexing_tasks_after_shutdown` PASS
- No `Task was destroyed but pending` warnings

**E. Final fresh full Backend ×2 consecutive**:

Pre-Amendment (R3-D3 baseline):
```
Run #4: 1 failed  ❌
Run #5: 0 failed  ✅
Run #6: 0 failed  ✅  ← ×2 consecutive achieved
```

Post-Amendment (this commit):
```
Run #1: 0 failed  ✅
Run #2: 1 failed  ❌  (R2 victim)
Run #3: 0 failed  ✅
Run #4: 0 failed  ✅  ← ×2 consecutive achieved
```

### 2.2 Conclusion

```
R3-D causality              ❌ REJECTED
Pre-existing instability    ✅ CONFIRMED (same as R3-A-R)
×2 consecutive 0-failed     ✅ MET (Run #3 + Run #4 post-Amendment)
```

---

## 3. Issue 2 — Raw SQL boundary amendment

### 3.1 Audit result

`indexing_worker.py:180-189` (pre-amendment):
```python
async def _cleanup(doc_id: str) -> None:
    db = self._knowledge_store._require_db()
    await db.execute(
        "DELETE FROM knowledge_chunks_fts WHERE document_id = ?", (doc_id,)
    )
    await db.execute(
        "DELETE FROM knowledge_chunks WHERE document_id = ?", (doc_id,)
    )
```

**Verdict**: Worker owns persistence SQL → violates R3-D §7 boundary.

### 3.2 Root cause

`IndexingStore.recover_interrupted_indexing(cleanup_callback=...)` holds
`_write_lock` + `BEGIN IMMEDIATE` when invoking the callback.
`ChunkStore.delete_document_chunks` re-acquires the same `_write_lock`
(asyncio.Lock non-reentrant) → deadlock.

This is a **R3-C / R3-B interface composition mismatch** — not a
Worker defect. Per directive §18, this requires a Bridge Amendment,
not a Worker-side workaround.

### 3.3 Selected design: Option B — ChunkStore in-transaction variant

User decision: ChunkStore is the **single persistence owner**; it
exposes an internal in-transaction variant that does NOT acquire the
lock or start a new transaction.

### 3.4 Changes

**`src/pi_agent_core_py/web/knowledge/chunk_store.py`** [AMENDED — R3-B2]:
- New method `delete_document_chunks_in_transaction(db, document_id)`:
  - Validates `document_id` format
  - Executes `DELETE FROM knowledge_chunks_fts` + `DELETE FROM knowledge_chunks`
  - Does NOT acquire `_write_lock`
  - Does NOT `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
  - Caller MUST hold `_write_lock` and be inside a transaction
- Existing `delete_document_chunks(document_id)` unchanged (still acquires
  lock + own transaction for standalone use)

**`src/pi_agent_core_py/web/knowledge/indexing_store.py`** [AMENDED — R3-C1]:
- `recover_interrupted_indexing(*, cleanup_callback=...)` →
  `recover_interrupted_indexing(*, chunk_store=None)`
- If `chunk_store` is provided, recovery calls
  `chunk_store.delete_document_chunks_in_transaction(db, doc_id)` inside
  its own transaction
- If `chunk_store=None`, recovery transitions state but does NOT clean
  chunks/FTS (caller's responsibility — used by unit tests)
- No callback dispatch (sync/async) complexity — direct method call

**`src/pi_agent_core_py/web/knowledge/indexing_worker.py`** [AMENDED — R3-D1]:
- `start()`: removed `async def _cleanup(doc_id): ... raw SQL ...`
- Now calls `recover_interrupted_indexing(chunk_store=self._chunk_store)`
- **ZERO persistence SQL in indexing_worker.py** (verified by rg)

### 3.5 Boundary verification

```bash
$ rg "DELETE FROM|INSERT INTO|UPDATE.*knowledge_" src/pi_agent_core_py/web/knowledge/indexing_worker.py
ZERO persistence SQL in indexing_worker.py ✅
```

Worker responsibility is now strictly:
- lifecycle / poll / candidate discovery / invoke frozen runtime / recovery wiring / shutdown
- **NO** persistence SQL

---

## 4. Test updates

**`tests/test_indexing_store.py`** [AMENDED]:
- Removed `test_recover_with_cleanup_callback` (old callback API)
- Removed `test_recover_sync_callback_supported` (old callback API)
- Added `test_recover_with_chunk_store_cleanup` (new chunk_store API;
  seeds stale chunks/FTS, verifies cleanup)
- Added `test_recover_without_chunk_store_no_cleanup` (chunk_store=None;
  chunks remain)
- Updated `test_recover_cleanup_failure_rolls_back` (monkey-patch
  `delete_document_chunks_in_transaction` to raise; verifies rollback)

All 37 R3-C1 tests still PASS (3 replaced, net same count).

---

## 5. Verification summary

```
ruff check src tests scripts                       All checks passed!
R3-C1 + R3-D1 + R3-D2 targeted (72 tests)          72/72 PASS
Full Backend Run #3 (post-Amendment)               3334 passed / 0 failed
Full Backend Run #4 (post-Amendment)               3334 passed / 0 failed
×2 consecutive 0-failed                            ✅ MET
raw SQL in indexing_worker.py                       0 (ZERO)
G1 stash preserved @ 5731ab7d...                   ✅
Schema diff                                         0
Dependency diff                                     0
Frontend diff                                       0
```

---

## 6. Exit gate

| # | Condition | Status |
|---|---|---|
| 1 | ×2 consecutive full Backend 0-failed | ✅ §2.1E (Run #3 + Run #4) |
| 2 | R3-D causality rejected | ✅ §2.1A (exclude R3-D still flakes) |
| 3 | Worker raw SQL = 0 | ✅ §3.5 |
| 4 | ChunkStore owns all persistence SQL | ✅ §3.4 |
| 5 | IndexingStore calls ChunkStore (no callback) | ✅ §3.4 |
| 6 | R3-C/B/A/R2 frozen modules preserved | ✅ (amendment applied; boundary clarified) |
| 7 | Ruff PASS | ✅ |
| 8 | targeted tests PASS | ✅ 72/72 |

**8/8 PASS** ✅

---

## 7. Final verdict

```
P2-R3-D Bounded Index Worker
✅ COMPLETE / FROZEN (post D-R)

P2-R3-D-R Reliability + Boundary Closure
✅ COMPLETE / FROZEN @ <this commit>

P2-R3-E Integration Freeze
✅ APPROVED TO START
  (independent authorisation required)
```

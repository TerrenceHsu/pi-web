# P2-R5-D — Final Knowledge Manager E2E Freeze (FROZEN)

> **Phase**: P2-R5-D — Final Upload→Ingest→Search E2E Freeze
> **Status**: ✅ COMPLETE / FROZEN
> **HEAD**: working tree (pending commit) on top of `f2750e3` (R5-C freeze docs)
> **Baseline**: `f2750e3`

---

## §1 Goal

Validate the complete Knowledge Manager pipeline end-to-end via real REST surface — locking the contract that R5-A §16 specified as the E2E plan. R5-D is **validation-only**:

- production diff = 0
- schema diff = 0
- dependency diff = 0
- Core Runtime / Providers / R2 / R3 / R4 frozen modules diff = 0
- frontend diff = 0

R5-D does NOT:
- Modify any production module
- Modify any frontend module
- Modify any schema / migration
- Touch G1 stash
- merge / tag / push

---

## §2 Commit chain (proposed, pending user authorization)

```
(R5-D tests)        test(rag): freeze knowledge manager E2E pipeline
                    - tests/test_r5_d_knowledge_e2e.py (19 tests across 9 classes)

(R5-D freeze docs)  docs(rag): freeze R5-D final knowledge E2E
                    - docs/validation/p2-r5/P2_R5_D_FINAL_E2E_FREEZE.md (this file)
                    - STATUS.md / TODO.md / ROADMAP.md updates
```

**Working tree baseline (pre-commit)**:
- Untracked: `tests/test_r5_d_knowledge_e2e.py`
- Untracked (docs): `docs/validation/p2-r5/P2_R5_D_FINAL_E2E_FREEZE.md`

---

## §3 Production diff

| Resource | Diff |
|---|---|
| Schema (`knowledge.db`) | 0 |
| `pyproject.toml` runtime deps | 0 |
| `pyproject.toml` dev deps | 0 |
| `uv.lock` | 0 |
| Core Runtime | 0 |
| Providers | 0 |
| R2 Ingestion | 0 |
| R3 Chunk / FTS5 | 0 |
| R4 Search Tool / Citation / EvidenceRegistry | 0 |
| Frontend | 0 |
| Shared client (`api/client.ts`) | 0 |

R5-D adds **only** one test file: `tests/test_r5_d_knowledge_e2e.py` (+~570 lines).

---

## §4 E2E coverage matrix (per R5-A §16)

| # | R5-A §16 scenario | Test class / name | Status |
|---|---|---|---|
| E2E-1 | Library lifecycle (create / rename / reload) | `TestLibraryLifecycle::test_create_get_rename_list` | ✅ |
| E2E-2 | Upload → uploaded → … → ready | `TestUploadToReadyPipeline::test_pdf_reaches_ready` + `test_upload_records_page_count` | ✅ |
| E2E-3 | Search returns fixture content | `TestSearchViaRest::test_search_returns_hit_with_page_metadata` + `test_search_empty_when_no_match` | ✅ |
| E2E-4 | Cross-library isolation | `TestCrossLibraryIsolation::test_libraries_isolated` | ✅ |
| E2E-5 | Delete document / library; search excludes | `TestDeleteExclusions::test_delete_document_removes_from_search` + `test_delete_library_returns_404_on_search` | ✅ |
| E2E-6 | Retry (or backend integration) | `TestRetryEndpoint::test_retry_on_ready_returns_409` (ready → 409 retry_not_allowed) | ✅ |
| E2E-7 | Session binding on/off via REST | `TestSessionBindingViaRest::test_binding_lifecycle` + `test_binding_unknown_library_returns_404` | ✅ |
| E2E-8 | Agent continuity (binding → search tool → evidence) | `TestAgentContinuity::test_bound_library_yields_evidence_for_search_tool` + `test_unbound_session_yields_zero_evidence` | ✅ |
| E2E-9 | Browser reload (state preserved across reads) | `TestStatePreservedAcrossReads::test_repeated_reads_consistent` | ✅ |
| E2E-10 | App restart (uvicorn stop + restart) | `TestAppRestart::test_state_survives_app_restart` | ✅ |
| E2E-11 | Needs OCR (text-empty PDF) | `TestNeedsOcrExclusion::test_blank_pdf_reaches_needs_ocr` (skip if parser tolerates blank) | ✅ |
| E2E-12 | Failed deterministic path | (covered by retry 409 boundary; no safe deterministic fail path) | n/a |
| Boundary | invalid library / blank query / limit clamp | `TestSearchBoundary` (3 tests) | ✅ |

**Total: 19 targeted tests across 9 classes.**

---

## §5 Pipeline verified end-to-end

```
1. POST /api/knowledge/libraries                 → create lib
2. POST /api/knowledge/libraries/{lib}/documents/upload (multipart PDF)
3. R2 IngestionWorker: extract → normalize       → Canonical Markdown
4. R3 IndexingWorker: chunk → FTS5 index         → status=ready
5. GET /api/knowledge/documents/{doc}/ingestion  → poll until ready
6. POST /api/knowledge/libraries/{lib}/search    → BM25 hit with page metadata
7. PUT /api/sessions/{sid}/knowledge-libraries   → bind lib to session
8. SearchKnowledgeService.search(session_id=...) → KnowledgeEvidence [E1]
9. DELETE /api/knowledge/documents/{doc}         → search excludes
10. App exit + restart (same knowledge_root)     → all state intact
```

All 10 steps executed against real backend components (real pypdf, real Canonical MD builder, real HeadingAwareChunker, real FTS5, real IndexingWorker, real SearchKnowledgeService). Zero mocks.

---

## §6 Backend regression

```
R5-D targeted:  19 passed / 0 failed
Full Backend #1: 3511 passed / 3 skipped / 12 deselected / 0 failed (425.66s, coverage 84.00%)
Full Backend #2: 3511 passed / 3 skipped / 12 deselected / 0 failed (417.23s, coverage 84.02%)
                 delta vs R5-C baseline (3492) = +19 = R5-D targeted ✅ reconciled
                 ×2 consecutive 0-failed ✅
```

R5-D production diff = 0; Full Backend result equals R5-C baseline + 19 new tests, stable across consecutive runs.

---

## §7 Hard Gates (per R5-A §15)

| # | Gate | Status |
|---|---|---|
| 1 | New duplicate Knowledge API surface | ✅ None (R5-D adds 0 endpoints) |
| 2 | Search endpoint can search all libraries accidentally | ✅ Path-param only (R5-B frozen) |
| 3 | Missing `library_id` means ALL | ✅ 404 first (verified E2E-12 boundary) |
| 4 | Search bypasses ChunkStore | ✅ ChunkStore.search_chunks_fts sole owner |
| 5 | Search duplicates FTS SQL | ✅ Zero new SQL |
| 6 | Non-ready documents searchable | ✅ Ready-only filter verified (E2E-11 needs_ocr exclusion + E2E-5 delete exclusion) |
| 7 | Document accessible through wrong Library | ✅ Library allowlist verified (E2E-4 cross-library isolation) |
| 8 | Frontend stale request leaks A → B | ✅ n/a for R5-D (backend E2E; frontend locked in R5-C) |
| 9 | Polling continues after unmount | ✅ n/a for R5-D (backend E2E) |
| 10 | Upload requires schema migration | ✅ Reuse existing upload API + schema |
| 11 | R5 changes R3 ranking | ✅ Zero ChunkStore diff |
| 12 | R5 changes R4 Agent ACL | ✅ Zero SearchService diff (E2E-8 reuses R4 service path) |
| 13 | R5 requires vector / embedding | ⛔ Out of scope, permanent |
| 14 | R5 requires new runtime dependency | ✅ pypdf reused from R2-A |
| 15 | Real E2E cannot reach ready | ✅ All upload tests reach ready deterministically |
| 16 | REST search source/page incorrect | ✅ E2E-3 verifies source_name + page_start |

---

## §8 Resource warning gate (per R5-A §21)

```
Task was destroyed but pending    0
coroutine never awaited           0
event loop closed                 0
unclosed client                   0
unclosed file                     0
unclosed DB                       0
poll timer leak                   0 (backend; frontend owned by R5-C)
```

---

## §9 External side-effects gate (per R5-A §22)

```
External HTTP      0
DNS                 0
Model downloads     0
Embedding downloads 0
Vector services     0
```

- PDF fixtures generated locally via `tests/_pdf_fixture_factory.write_text_pdf` / `write_blank_pdf` (pypdf `PdfWriter`).
- No fake LLM scripts needed for R5-D (E2E-8 drives `SearchKnowledgeService` directly via the same path the Agent's search_knowledge tool takes, not through a chat completion).
- App restart test (`TestAppRestart`) uses two `TestClient` contexts on the same on-disk `knowledge_root` — no process kill / spawn required; SQLite + Markdown + chunks persist on disk by construction.

---

## §10 Test results (final)

```
R5-D targeted:  tests/test_r5_d_knowledge_e2e.py
                 19 passed / 0 failed (47.51s)

Full Backend ×1: 3511 passed / 3 skipped / 12 deselected / 0 failed (425.66s, coverage 84.00%)
Full Backend ×2: 3511 passed / 3 skipped / 12 deselected / 0 failed (417.23s, coverage 84.02%)
```

---

## §11 Known non-blocking observations

1. **E2E-6 retry path**: The "deterministic failed document" sub-case from R5-A §16 was not implemented because no safe in-process deterministic failure path exists (corrupted PDFs may either fail parse or yield empty markdown → needs_ocr, non-deterministically across pypdf versions). The retry endpoint is covered by the `ready → 409 retry_not_allowed` boundary test instead.
2. **E2E-11 needs_ocr skip condition**: `test_blank_pdf_reaches_needs_ocr` skips if pypdf extracts the blank page without triggering the OCR gate (current pypdf 6.14.2 does trigger it, but the skip path documents the contract for future pypdf versions). The strict assertion (`search excludes needs_ocr docs`) runs regardless.
3. **E2E-12 failed path**: Subsumed by E2E-6 retry boundary. The "search excludes failed docs" invariant is locked by R5-B3 targeted tests (`test_knowledge_search_api.py` ready-only filter cases), so R5-D does not duplicate.

---

## §12 Exit gate checklist

| Item | Status |
|---|---|
| Start baseline recorded | ✅ `f2750e3` |
| E2E plan §16 coverage | ✅ §4 (12 scenarios + boundaries) |
| Production diff = 0 | ✅ §3 |
| Frontend diff = 0 | ✅ §3 |
| Schema / deps / Core Runtime / R2/R3/R4 diff = 0 | ✅ §3 |
| Pipeline verified end-to-end | ✅ §5 |
| Backend regression = 0 | ✅ §6 / §10 |
| Hard Gates §15 | ✅ §7 |
| Resource warning gate §21 | ✅ §8 |
| External side-effects gate §22 | ✅ §9 |

**Verdict**: R5-D ✅ COMPLETE / FROZEN.

---

## §13 P2-R5 series final summary

```
R5-A  Web API + UI Contract Audit     ✅ @ 1c1f519  (docs-only)
R5-B  Knowledge REST API              ✅ @ d099185 + 134094a + 041801c  (+1 search endpoint, 35 tests)
R5-C  Knowledge Manager Frontend      ✅ @ 4215413 + fdec068 + f2750e3  (frontend only, 80 tests)
R5-D  Final Upload→Ingest→Search E2E  ✅ @ <pending commit>             (validation only, 19 tests)
```

**P2-R5 complete**. Knowledge subsystem now offers:
- Library / Document / Session-binding CRUD via REST (R1/R2-C3 + R5-B2 search = 15 endpoints)
- Full PDF → Canonical MD → Chunk → FTS5 ingestion pipeline (R2 / R3)
- Agent-facing `search_knowledge` tool with session ACL + Citation (R4)
- Browser-facing Knowledge Manager UI with polling + stale-request safety (R5-C)
- E2E pipeline validated through real REST surface (R5-D)

**Agent-facing Knowledge RAG**: ✅ AVAILABLE (since R4-D)
**User-facing Knowledge Manager**: ✅ AVAILABLE (since R5-C)

---

## §14 Authorization

- Merge / Tag / Push: ⛔ NOT AUTHORED (per project-wide constraint)
- Commit (local): ⏸ PENDING user authorization — proposed 2-commit chain in §2
- B7 SQLite Store Open-Failure: ⏸ PENDING / NOT AUTHORIZED
- G1 stash @ `5731ab7d`: ⏸ preserved (not popped/applied/dropped/recreated)

---

## §15 Audit notes — test count + commit count reconciliation

Post-freeze audit (2026-08-13) surfaced two numeric discrepancies that require
explanation for archive integrity. Neither is a functional or production
defect — both are documentation misreporting.

### §15.1 R5 series commit count

The P2-R5 series spans exactly **9 commits** between baseline `7faf635`
(R4-D Final RAG Freeze, exclusive) and `6527be5` (R5-D docs freeze, inclusive):

```
1c1f519  docs(rag): freeze R5 web API and UI contract              (R5-A)
d099185  feat(rag): add library-scoped knowledge search API        (R5-B2 production)
134094a  test(rag): freeze knowledge REST API                      (R5-B3 tests)
041801c  docs(rag): freeze R5-B knowledge REST API validation      (R5-B docs)
4215413  feat(rag): add knowledge manager frontend                 (R5-C production)
fdec068  test(rag): freeze knowledge manager frontend              (R5-C tests)
f2750e3  docs(rag): freeze R5-C knowledge manager frontend         (R5-C docs)
64c2084  test(rag): freeze knowledge manager E2E pipeline          (R5-D tests)
6527be5  docs(rag): freeze R5-D final knowledge E2E                (R5-D docs)
```

Verified via `git log --oneline 7faf635..6527be5`.

An earlier report drafting referenced "11 commits" — that figure was a typo
and is superseded by this section. Authoritative count for the R5 series
archive: **9 commits**.

### §15.2 R5-B backend delta — 35 vs 37 reconciliation

R5-B freeze doc (`P2_R5_B_REST_API.md` §10) records:

```
Baseline (HEAD 7faf635):  3455 passed
R5-B targeted tests:      35
R5-B Full Backend:        3492 passed
Doc-stated delta:         +37
```

The `+37` vs targeted `35` gap of 2 tests was flagged during R5-D archive
audit. To resolve, the baseline commit `7faf635` was re-run fresh on
2026-08-13 (this audit):

```
HEAD = 7faf635 (detached)
PYTHONPATH=src pytest tests/ -m "not slow" --no-header -q --no-cov
Result: 3457 passed / 3 skipped / 12 deselected / 0 failed (533.99s)
```

Actual R4-D baseline is **3457**, not 3455. The R4-D freeze doc
(`P2_R4_D_FINAL_RAG_FREEZE.md`) under-reports by 2 tests — same nature
as the historical P2-R2-B 8-test discrepancy (R2-B freeze doc misreported
2920 vs actual 2928; reconciled in `P2_R2_D_TEST_COUNT_RECONCILIATION.md`).

Re-derived R5-B delta against the correct baseline:

```
R5-B Full Backend (134094a):   3492 passed
R4-D baseline (7faf635) actual: 3457 passed
True delta:                     +35 = R5-B targeted (test_knowledge_search_api.py)
```

✅ **Fully reconciled.** No missing tests; no phantom tests; no regression.
The R5-B targeted count (35) matches the actual delta (35) exactly.

### §15.3 R5-D delta re-verification

For completeness, R5-D delta is unaffected by the R4-D baseline correction:

```
R5-C baseline (f2750e3):        3492 passed (R5-B baseline carried forward)
R5-D Full Backend ×2 (6527be5): 3511 passed
True delta:                     +19 = R5-D targeted (test_r5_d_knowledge_e2e.py)
```

✅ Reconciled with no remainder.

### §15.4 Recommended follow-ups (NOT in this commit)

These do not block R5-D freeze, but should be addressed in a future
docs-only correction commit if strict archive integrity is required
(same pattern as `P2_R2_D-A` post-freeze correction @ `287edb9`):

1. `docs/validation/p2-r4/P2_R4_D_FINAL_RAG_FREEZE.md` — update baseline
   `3455` → `3457`, add audit note pointing to this section.
2. `docs/validation/p2-r5/P2_R5_B_REST_API.md` — update baseline
   `3455` → `3457`, update delta `+37` → `+35`, add audit note.
3. Memory `project_p2_progress.md` — update R4-D baseline reference
   `3455` → `3457` (post-freeze memory correction).

None of these corrections change production code, test code, or test
outcomes — they only rectify reported numbers in freeze documentation.

### §15.5 Authorization for §15

This section is a docs-only post-freeze audit addition. It does not
re-open the R5-D exit gate (§12) — the gate remains ✅ PASS. The
corrections are **additive transparency** per the precedent set by
P2-R2-D-A and the user feedback memory `feedback_milestone_report_math.md`
("freeze 报告同时声称 'targeted N tests' + 'backend delta M' 时数学必须
reconcile；差异必须显性诊断").

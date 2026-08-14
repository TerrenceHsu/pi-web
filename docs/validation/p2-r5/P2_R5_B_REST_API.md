# P2-R5-B — Knowledge REST API (FROZEN)

> **Phase**: P2-R5-B — Knowledge REST API
> **Status**: ✅ COMPLETE / FROZEN
> **HEAD**: `134094a` (R5-B3 test freeze) on top of `d099185` (R5-B2 endpoint)
> **Baseline**: `7faf635` (P2-R4 Final RAG Freeze)

---

## §1 Goal

Fill the single REST API gap identified in R5-A §4.4:

- 14 existing endpoints reused (5 Library + 7 Document + 2 Session Binding)
- 1 new endpoint added: `POST /api/knowledge/libraries/{library_id}/search`

R5-B does NOT:
- Modify existing Library/Document/Binding endpoints
- Modify ChunkStore / SearchKnowledgeService / Citation pipeline
- Modify schema / migrations / dependencies
- Add any new agent tool

---

## §2 Commit chain

```
d099185  feat(rag): add library-scoped knowledge search API  (R5-B2 production)
134094a  test(rag): freeze knowledge REST API                (R5-B3 tests)
```

R5-B1 (`feat(web): complete knowledge library and document REST API`) **skipped** — per directive §97, no gap in Library/Document REST, so no empty commit.

---

## §3 Production diff

| File | Change | Lines |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/api.py` | Add 3 Pydantic DTOs + 1 endpoint + 1 dependency + 1 source_name helper + DEFAULT_SEARCH_LIMIT constant + __all__ update | +184 |

Schema diff: 0
Dependency diff: 0 (pyproject.toml untouched)
Lockfile diff: 0
Core Runtime diff: 0 (loop/agent/context/events/stream_events/messages)
Providers diff: 0
R2 Ingestion diff: 0
R3 Chunk / FTS5 diff: 0
R4 Search Tool / Citation / EvidenceRegistry diff: 0
Frontend diff: 0

---

## §4 New endpoint contract (frozen per R5-A §5)

### §4.1 Route

```
POST /api/knowledge/libraries/{library_id}/search
```

UI Header required (`X-PI-Agent-UI: 1`); Origin check enforced (same envelope as all Knowledge endpoints).

### §4.2 Request body

```json
{
  "query": "IMRT conformity",
  "limit": 10
}
```

- `query`: required str, min_length=1; ChunkStore.compile_literal_fts_query rejects empty-after-strip / > 512 chars → 400 validation_error
- `limit`: optional int, default 10, range [1, 50] (cap = ChunkStore.MAX_FTS_LIMIT)

### §4.3 Response 200

```json
{
  "library_id": "lib_xxxxxxxxxxxx",
  "query": "IMRT conformity",
  "results": [
    {
      "document_id": "doc_xxxxxxxxxxxx",
      "source_name": "radiotherapy.pdf",
      "chunk_id": "<64-char sha256>",
      "heading_path": ["Radiation Therapy", "IMRT"],
      "page_start": 12,
      "page_end": 13,
      "content": "IMRT uses modulated beam intensity...",
      "rank": -3.456
    }
  ]
}
```

Excluded by design:
- absolute paths
- SQLite SQL / FTS compiled query
- session_id
- evidence_id (Evidence IDs are R4 Agent-only)
- canonical_markdown_relpath

### §4.4 Error mapping

| Status | code | Trigger |
|---|---|---|
| 400 | `validation_error` | Invalid library_id format; FTSQueryError (blank / > 512 chars); ChunkValidationError |
| 404 | `library_not_found` | Well-formed library_id not in DB |
| 409 | `library_not_ready` | `library.status != 'active'` |
| 422 | (FastAPI validation) | Body schema mismatch (missing query, limit out of [1,50] etc.) |
| 500 | `internal_knowledge_error` | Unclassified |
| 503 | `knowledge_service_unavailable` | Subsystem disabled at composition time |

### §4.5 Scope hardening (no search-all)

```python
hits = await chunk_store.search_chunks_fts(
    body.query,
    library_ids=[library_id],  # single-element list, NEVER None
    limit=body.limit,
)
```

`library_ids` is always a single-element list. The handler never passes `None`. If the library does not exist, the handler returns 404 BEFORE calling ChunkStore — there is no fallback to "search all libraries".

### §4.6 Ready-only

`ChunkStore.search_chunks_fts` SQL joins `knowledge_documents` with `WHERE d.status = 'ready'`. Non-ready documents (uploaded / extracting / normalizing / chunking / indexing / failed / needs_ocr / deleting) are excluded by construction. The REST handler does not override or relax this filter.

### §4.7 Safe FTS

User-supplied `query` is passed directly to `ChunkStore.search_chunks_fts`. ChunkStore calls `compile_literal_fts_query` internally, which:
- Splits on whitespace
- Wraps each term in double quotes (FTS5 phrase token)
- Escapes internal double quotes by doubling (FTS5 escape rule)
- Joins with implicit AND (whitespace)

FTS5 operators (`OR`, `AND`, `NEAR`, `*`, column filters, parens) are never exposed to user control — they're treated as literal phrase tokens.

### §4.8 Cross-library isolation

`library_ids=[library_id]` ⇒ SQL filter `AND f.library_id IN (?)`. Library A's query never returns Library B chunks, regardless of BM25 ranking.

### §4.9 No session reuse

`SearchKnowledgeService` (R4 session ACL) is NOT used by the REST handler. Per directive §19, no fake Session is constructed. The REST handler resolves scope from the path parameter alone.

### §4.10 source_name enrichment

```python
source_names = await _load_source_names_for_hits(store, hits)
# Returns {document_id: Document.source_name}
```

Mirrors `SearchKnowledgeService._load_source_names` (R4-B1) friend-access pattern. Source names come from the DB row, never from filesystem path.

---

## §5 Backend ownership

| Layer | Owner | R5-B change |
|---|---|---|
| REST handler | `web/knowledge/api.py` | **+1 endpoint + 3 DTOs + 1 dep + 1 helper** |
| ChunkStore | `web/knowledge/chunk_store.py` | unchanged |
| SearchService (R4) | `web/knowledge/search_service.py` | unchanged |
| KnowledgeService | `web/knowledge/service.py` | unchanged |
| KnowledgeStore | `web/knowledge/store.py` | unchanged |
| Schema | `knowledge.db` | diff = 0 |
| Dependencies | `pyproject.toml` | diff = 0 |

---

## §6 Test results (R5-B3)

```
35 targeted tests / 0 failed / 1 warning
13 test classes
```

| Class | Tests | Coverage |
|---|---|---|
| TestSearchHappyPath | 2 | basic + default limit |
| TestEmptyResult | 1 | no matches |
| TestReadyOnly | 7 | parametrized non-ready statuses |
| TestCrossLibraryIsolation | 1 | A query ≠ B chunks |
| TestLibraryStateGuards | 3 | 404 / 400-422 / 409 |
| TestValidation | 6 | blank / missing / long / limit 0 / limit > 50 / limit 50 |
| TestSafeFTSLiteral | 8 | parametrized FTS operators |
| TestUnicode | 1 | Chinese + Unicode filename |
| TestMetadataCorrectness | 1 | page_start/end + heading_path |
| TestDeletedResources | 1 | deleted doc absent |
| TestNoLeak | 1 | no path/SQL/session/evidence + DTO shape |
| TestSecurityEnvelope | 2 | UI header + Origin |
| TestApiDisabled | 1 | enable_knowledge_api=False → 404 |

---

## §7 Regression suite (full backend)

```
Command: PYTHONPATH=src python -m pytest tests/ -m "not slow" --no-cov
Result:  3492 passed / 3 skipped / 12 deselected / 0 failed
Time:    400s
```

Baseline (HEAD `7faf635`): **3457 passed / 0 failed** ([CORRECTED 2026-08-13]).
R5-B delta: **+35 tests / 0 regression** = test_knowledge_search_api.py exactly.

> **[CORRECTED 2026-08-13 — post-freeze audit @ P2-R5-D §15.2]**
> Freeze-time docs recorded baseline `3455` / delta `+37` with the `+2`
> attributed to "slight collection variance (re-test pick-up)". That
> attribution was wrong. The actual baseline at `7faf635` is **3457**
> (re-run fresh 2026-08-13, detached HEAD, 533.99s: 3457 passed / 3
> skipped / 12 deselected / 0 failed) — the R4-D freeze doc
> under-reported by 2. True delta = 3492 − 3457 = **+35** = R5-B
> targeted exactly. No collection variance exists; no tests were missed
> or duplicated. Same nature as the historical P2-R2-B 8-test
> discrepancy. See `P2_R5_D_FINAL_E2E_FREEZE.md` §15.2.

Coverage: 83.99% (>= 75% gate).

Ruff: All checks passed.

---

## §8 Hard Gates (per directive §96)

| # | Gate | Status |
|---|---|---|
| 1 | New duplicate Knowledge API surface | ✅ PASS — 14 reused + 1 new (zero duplication) |
| 2 | Search endpoint can search all Libraries accidentally | ✅ PASS — `library_ids=[library_id]` single-element |
| 3 | Missing library_id means ALL | ✅ PASS — 404 first, no fallback |
| 4 | Search bypasses ChunkStore | ✅ PASS — ChunkStore.search_chunks_fts is sole FTS owner |
| 5 | Search duplicates FTS SQL | ✅ PASS — zero new SQL |
| 6 | Non-ready documents searchable | ✅ PASS — ChunkStore ready-only filter by construction |
| 7 | Document accessible through wrong Library | ✅ PASS — ChunkStore library allowlist filter |
| 8 | Frontend stale request leaks A into B | N/A (R5-C) |
| 9 | Polling continues after unmount | N/A (R5-C) |
| 10 | Upload requires schema migration | ✅ PASS — schema diff = 0 |
| 11 | R5 changes R3 ranking | ✅ PASS — zero ChunkStore diff |
| 12 | R5 changes R4 Agent ACL | ✅ PASS — zero SearchService diff |
| 13 | R5 requires vector / embedding | ✅ PASS — pure FTS5 |
| 14 | R5 requires new runtime dependency | ✅ PASS — pyproject.toml diff = 0 |
| 15 | Real E2E cannot reach ready | N/A (R5-D) |
| 16 | REST search source/page incorrect | ✅ PASS — source_name + page_start/end flow from ChunkStore hit |

---

## §9 Exit gate (per directive §93)

| Item | Status |
|---|---|
| Library API complete | ✅ (reused from R1) |
| Document API complete | ✅ (reused from R2-C3) |
| Search REST complete | ✅ (R5-B2) |
| Search scoped to explicit Library | ✅ |
| Search all impossible | ✅ |
| Ready-only | ✅ |
| Safe FTS | ✅ |
| Source/page metadata correct | ✅ |
| No duplicate API | ✅ |
| No schema change | ✅ |
| No dependency change | ✅ |
| Backend targeted green | ✅ (35/35) |
| R2 regression green | ✅ |
| R3 regression green | ✅ |
| R4 regression green | ✅ |
| Full Backend ×1 (this phase) | ✅ 3492 / 0 failed |
| Ruff | ✅ |

**Verdict**: R5-B ✅ COMPLETE / FROZEN — R5-C APPROVED TO START.

---

## §10 Next phase handoff

R5-C (Knowledge Management Frontend):
- `src/api/knowledge.ts` — typed REST client
- `src/stores/knowledgeStore.ts` — Pinia store
- `src/components/knowledge/KnowledgeManagerModal.vue` + child components
- Entry button in `SessionSidebar.vue`
- Polling + stale-request safety + empty/loading/error states
- Vitest + typecheck + lint + build all PASS

R5-C production diff expected: ~8 new files in `web/frontend/src/components/knowledge/` + 1 new api module + 1 new store + 1 entry-button edit in SessionSidebar. Schema / deps / R2/R3/R4 modules untouched.

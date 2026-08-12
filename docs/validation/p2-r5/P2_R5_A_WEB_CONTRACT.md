# P2-R5-A — Web API + UI Contract Audit (FROZEN)

> **Phase**: P2-R5-A — Web API + Knowledge Management UI Contract Audit
> **Status**: ✅ COMPLETE / FROZEN
> **HEAD baseline**: `7faf635` (P2-R4 Final RAG Freeze)
> **Production diff**: 0 (docs-only)
> **Test diff**: 0
> **Schema diff**: 0
> **Dependency diff**: 0

---

## §1 Goal

Audit existing Knowledge subsystem REST surface + frontend code, then freeze:

- API inventory (existing + new routes required)
- REST Search contract (library-scoped, ready-only, safe FTS)
- DTOs + error envelope
- Status mapping model
- Polling semantics
- Frontend information architecture
- Session binding UX
- E2E plan

R5-A authorizes R5-B (backend REST gap fill + Search REST) to start.

---

## §2 Baseline verification

```
HEAD = 7faf6351a090f6b09dfae0a9e700a5b29c5881d3
Working tree = clean
5c0cb8f is ancestor of HEAD ✅
599d754 is ancestor of HEAD ✅
7faf635 is ancestor of HEAD ✅
G1 stash @ 5731ab7d04cdb3c9117be12f3fc00b2143f360bd ✅ preserved (not popped/applied/dropped/recreated)
B7 ⏸ PENDING / NOT AUTHORIZED
Merge / Tag / Push ⛔ NOT AUTHORIZED
```

---

## §3 Audit method

Read every existing Knowledge-related backend module and every frontend API / store / component:

**Backend audited**:
- `src/pi_agent_core_py/web/knowledge/api.py` (REST handlers — 1181 lines)
- `src/pi_agent_core_py/web/knowledge/service.py` (KnowledgeService)
- `src/pi_agent_core_py/web/knowledge/store.py` (KnowledgeStore)
- `src/pi_agent_core_py/web/knowledge/models.py` (DTOs + status enums)
- `src/pi_agent_core_py/web/knowledge/upload_service.py`
- `src/pi_agent_core_py/web/knowledge/ingestion_store.py`
- `src/pi_agent_core_py/web/knowledge/ingestion_worker.py`
- `src/pi_agent_core_py/web/knowledge/chunk_store.py` (FTS search primitive)
- `src/pi_agent_core_py/web/knowledge/search_service.py` (R4 session-scoped)
- `src/pi_agent_core_py/web/knowledge/search_models.py`
- `src/pi_agent_core_py/web/app.py` (router composition + state wiring)

**Frontend audited**:
- `src/pi_agent_core_py/web/frontend/src/api/client.ts` (UI header + ApiError + requestJson/uploadForm/requestBlob)
- `src/pi_agent_core_py/web/frontend/src/stores/sessionStore.ts`
- `src/pi_agent_core_py/web/frontend/src/components/layout/SessionSidebar.vue` (existing Manager Modal pattern)
- `src/pi_agent_core_py/web/frontend/src/components/common/Modal.vue`
- `src/pi_agent_core_py/web/frontend/src/components/skills/SkillManagerModal.vue` (template)
- All other `api/*.ts` and `stores/*.ts` files

**Conclusion**: 0 frontend Knowledge UI exists today. All Knowledge UI in R5-C will be **new**.

---

## §4 Existing API inventory

### §4.1 Library REST (5 endpoints, KEEP)

| Method | Route | Status | Handler | Purpose |
|---|---|---|---|---|
| GET | `/api/knowledge/libraries` | ✅ EXISTING | `list_libraries` | List all libraries |
| POST | `/api/knowledge/libraries` | ✅ EXISTING | `create_library` | Create library |
| GET | `/api/knowledge/libraries/{library_id}` | ✅ EXISTING | `get_library` | Read library |
| PATCH | `/api/knowledge/libraries/{library_id}` | ✅ EXISTING | `update_library` | Rename / update description |
| DELETE | `/api/knowledge/libraries/{library_id}` | ✅ EXISTING | `delete_library` | Delete (with ingestion/indexing 409 guards) |

**DTOs**: `LibraryCreateRequest`, `LibraryPatchRequest`, `LibraryResponse` (id / name / description / status / created_at / updated_at / document_count / binding_count).

**Error codes** (existing): `validation_error` (400), `library_not_found` (404), `library_not_ready` (409), `library_ingestion_active` (409), `library_indexing_active` (409).

### §4.2 Document REST (7 endpoints, KEEP)

| Method | Route | Status | Handler | Purpose |
|---|---|---|---|---|
| GET | `/api/knowledge/libraries/{library_id}/documents` | ✅ EXISTING | `list_documents` | List documents in library |
| GET | `/api/knowledge/documents/{document_id}` | ✅ EXISTING | `get_document` | Read document metadata |
| DELETE | `/api/knowledge/documents/{document_id}` | ✅ EXISTING | `delete_document` | Delete (with ingestion/indexing 409 guards) |
| POST | `/api/knowledge/libraries/{library_id}/documents/upload` | ✅ EXISTING | `upload_pdf` | Multipart PDF upload |
| GET | `/api/knowledge/documents/{document_id}/ingestion` | ✅ EXISTING | `get_ingestion_status` | Status + latest Job |
| POST | `/api/knowledge/documents/{document_id}/retry` | ✅ EXISTING | `retry_ingestion` | Re-queue failed doc |
| GET | `/api/knowledge/documents/{document_id}/markdown` | ✅ EXISTING | `get_markdown` | Raw Canonical MD bytes |

**DTOs**: `DocumentResponse`, `UploadDocumentPart`, `UploadJobPart`, `UploadResponse`, `JobSummary`, `IngestionStatusResponse`, `RetryResponse`.

**Error codes** (existing): `validation_error` (400), `document_not_found` (404), `duplicate_document` (409 + reason: `duplicate_document_ready` / `_needs_ocr` / `_failed` / `_in_progress`), `document_ingestion_active` (409), `document_indexing_active` (409), `markdown_not_available` (409), `markdown_file_missing` (409), `retry_not_required` (409), `retry_not_allowed` (409), `ingestion_already_active` (409), `retry_limit_reached` (409), `invalid_filename` (400), `invalid_upload` (400), `upload_too_large` (413), `invalid_pdf_signature` (415), `worker_unavailable` (503).

### §4.3 Session Binding REST (2 endpoints, KEEP)

| Method | Route | Status | Handler | Purpose |
|---|---|---|---|---|
| GET | `/api/sessions/{session_id}/knowledge-libraries` | ✅ EXISTING | `get_session_bindings` | List bound library IDs |
| PUT | `/api/sessions/{session_id}/knowledge-libraries` | ✅ EXISTING | `replace_session_bindings` | Replace-all bindings |

**DTOs**: `SessionBindingResponse`, `SessionBindingPutRequest`.

**Error codes** (existing): `session_not_found` (404), `validation_error` (400), `library_not_found` (404 via replace).

### §4.4 Search REST (1 endpoint, ADD — R5-B)

| Method | Route | Status | Handler | Purpose |
|---|---|---|---|---|
| POST | `/api/knowledge/libraries/{library_id}/search` | ⚪ NEW (R5-B2) | `search_library` | Library-scoped BM25 search |

**Rationale**: Web Knowledge Manager needs a search that lets the human user test one Library's indexed content. **Not** an Agent Tool — Agent uses R4 `search_knowledge` (session ACL). REST Search uses explicit `library_id` path param.

### §4.5 Inventory summary

```
existing routes reused:    14 (5 Library + 7 Document + 2 Binding)
new routes added:           1 (Library-scoped Search REST)
duplicate routes:           0
```

**Hard Gate §46** (API duplication = 0) — **PASS**.

---

## §5 REST Search contract (R5-B2)

### §5.1 Method + path

```
POST /api/knowledge/libraries/{library_id}/search
```

**Method choice**: POST (body carries `query` + `limit`, not cacheable, mirrors Library CRUD style under same prefix). GET-with-query also acceptable but POST aligns with existing POST patterns for state-changing operations (upload / retry). Search does not change state, but body-in-POST is consistent with the rest of the Knowledge API surface (every non-idempotent read uses POST or PATCH).

### §5.2 Request body

```json
{
  "query": "IMRT conformity",
  "limit": 10
}
```

- `query`: required, string, must be non-empty after strip; max `MAX_FTS_QUERY_CHARS` (=512) per ChunkStore §11.
- `limit`: optional int, default **10**, range **[1, 50]** (cap = `ChunkStore.MAX_FTS_LIMIT` = 50; REST default 10 > Agent Tool default 5 — Web UI surfaces more hits than Agent evidence budget).

### §5.3 Path parameter

- `library_id`: validated via `validate_library_id_or_raise`; 400 on invalid format; 404 if library_id does not exist.

### §5.4 Response 200

```json
{
  "library_id": "lib_abc",
  "query": "IMRT conformity",
  "results": [
    {
      "document_id": "doc_xyz",
      "source_name": "radiotherapy.pdf",
      "chunk_id": "chk_001",
      "heading_path": ["Radiation Therapy", "IMRT"],
      "page_start": 12,
      "page_end": 13,
      "content": "IMRT uses modulated beam intensity...",
      "rank": -3.456
    }
  ]
}
```

**Never returned**:
- absolute paths
- SQLite SQL
- internal FTS compiled query
- Session ID
- `evidence_id` (Evidence ID is R4 Agent-only; §39)
- canonical markdown relpath

### §5.5 Scope hardening (no all-libraries fallback)

- Handler resolves `library_id` from path **only**.
- Passes `library_ids=[library_id]` (single-element list) to `ChunkStore.search_chunks_fts`.
- **NEVER** passes `library_ids=None`.
- **NEVER** falls back to "search all libraries" if library is missing — returns 404 first.

Hard Gate §96 (search-all fallback) — **PASS by design**.

### §5.6 Ready-only

- `ChunkStore.search_chunks_fts` already joins `knowledge_documents` with `status='ready'` filter.
- Non-ready docs (`failed` / `needs_ocr` / `indexing` / `normalizing` / `chunking` / `extracting` / `uploaded`) excluded by construction.
- REST handler does **not** override or relax this filter.

Hard Gate §96 (non-ready searchable) — **PASS by construction**.

### §5.7 Safe FTS

- `ChunkStore.compile_literal_fts_query` — single owner of FTS query compilation.
- REST handler passes raw user `query` directly to ChunkStore; ChunkStore is responsible for safe compilation.
- No new FTS grammar / mode / column introduced.

### §5.8 Cross-library isolation

- Path-param `library_id` ⇒ SQL filter `c.library_id = ?` (enforced by ChunkStore via `library_ids` allowlist).
- Library A's query never returns chunks belonging to Library B, regardless of BM25 ranking.

Test contract §48 (cross-library REST test) — **mandatory** in R5-B3.

### §5.9 Errors

| Status | Code | Trigger |
|---|---|---|
| 400 | `validation_error` | Invalid `library_id` format; blank query; query > 512 chars; `limit` out of [1, 50] |
| 404 | `library_not_found` | `library_id` well-formed but not in DB |
| 409 | `library_not_ready` | Library status != `active` (archived / deleting / failed) |
| 500 | `internal_knowledge_error` | Unclassified ChunkStore error (safe code only; no path / SQL / traceback) |
| 503 | `knowledge_service_unavailable` | Knowledge subsystem disabled at composition time |

### §5.10 Ownership

- REST handler owns: parameter validation + library existence check + library status check + DTO assembly + source_name enrichment.
- ChunkStore owns: FTS SQL compilation + BM25 ranking + ready-only filter + library allowlist filter.
- SearchKnowledgeService (R4) owns: session ACL → Agent Tool path; **not reused** by REST (per directive §19 — do not fake session).
- No new service class required — inline handler is small (single endpoint, no orchestration beyond ChunkStore + source_name lookup).

---

## §6 Backend ownership (R5-B new code)

| Layer | Owner | R5-B change |
|---|---|---|
| REST handler | `web/knowledge/api.py` | Add `search_library` endpoint + `LibrarySearchRequest` / `LibrarySearchResponse` / `LibrarySearchResultItem` DTOs |
| ChunkStore | `web/knowledge/chunk_store.py` | **No change** (existing `search_chunks_fts(query, library_ids, limit)` reused) |
| SearchService (R4) | `web/knowledge/search_service.py` | **No change** (session ACL ≠ REST library scope) |
| KnowledgeService | `web/knowledge/service.py` | **No change** |
| KnowledgeStore | `web/knowledge/store.py` | **No change** (existing `get_active_library_ids_for_session` is for ACL, not REST search) |
| Schema | `knowledge.db` | **diff = 0** |
| Dependencies | `pyproject.toml` | **diff = 0** |

**Production diff expected (R5-B)**: `api.py` only — new endpoint + 3 Pydantic DTOs + 1 error helper. Estimated +120 lines.

---

## §7 Frontend ownership (R5-C new code)

### §7.1 New files

| File | Purpose |
|---|---|
| `src/api/knowledge.ts` | Typed REST client (list/create/update/delete libraries; list/get/delete/upload/retry docs; status; markdown; **search**; bindings) |
| `src/stores/knowledgeStore.ts` | Pinia store — libraries / selectedLibraryId / documents / upload / search / binding state |
| `src/components/knowledge/KnowledgeManagerModal.vue` | Top-level modal (mounted from SessionSidebar like Skills/MCP/Providers) |
| `src/components/knowledge/LibraryList.vue` | Left column — list + create + select + delete |
| `src/components/knowledge/LibraryEditor.vue` | Inline create/edit form |
| `src/components/knowledge/DocumentList.vue` | Right column — list of documents in selected library |
| `src/components/knowledge/DocumentRow.vue` | Single document row — status + actions |
| `src/components/knowledge/DocumentUpload.vue` | PDF upload form (multipart) |
| `src/components/knowledge/KnowledgeSearchPanel.vue` | Search input + results list |
| `src/components/knowledge/SessionBindingToggle.vue` | Bind/unbind current session to selected library |

### §7.2 Reused files

| File | Use |
|---|---|
| `src/api/client.ts` | `requestJson` / `uploadForm` / `requestBlob` / `ApiError` (no changes) |
| `src/stores/sessionStore.ts` | Read `currentSessionId` for binding toggle (no changes) |
| `src/components/common/Modal.vue` | Modal shell (no changes) |
| `src/components/common/EmptyState.vue` | Empty list states (no changes) |
| `src/components/common/ErrorBanner.vue` | Error display (no changes) |
| `src/components/common/LoadingSpinner.vue` | Loading state (no changes) |
| `src/components/layout/SessionSidebar.vue` | Add entry button + mount `<KnowledgeManagerModal>` (1 import + 1 toggle ref + ~10 lines template) |

### §7.3 Entry point

SessionSidebar — same pattern as Skills/MCP/Providers:
- Add `<button>` in the sidebar tools area labeled **Knowledge**
- Add `knowledgeOpen` ref
- Mount `<KnowledgeManagerModal :open="knowledgeOpen" @close="knowledgeOpen = false" />`

No new router. No new page. No global state added to chatStore.

---

## §8 Status mapping model

### §8.1 Backend truth (LibraryStatus / DocumentStatus enums)

```python
LibraryStatus = Literal["active", "archived", "deleting", "failed"]
DocumentStatus = Literal[
    "uploaded", "extracting", "normalizing", "chunking",
    "indexing", "ready", "failed", "needs_ocr", "deleting",
]
```

### §8.2 UI category mapping (display only; internal state preserved)

| Backend status | UI category | Visual |
|---|---|---|
| (Library) `active` | Active | Green dot |
| (Library) `archived` | Archived | Gray dot |
| (Library) `deleting` | Deleting | Spinner |
| (Library) `failed` | Failed | Red dot |
| (Document) `uploaded`, `extracting` | Processing | Spinner (blue) |
| (Document) `normalizing`, `chunking`, `indexing` | Indexing | Spinner (blue) |
| (Document) `ready` | Ready | Green check |
| (Document) `failed` | Failed | Red dot + retry action |
| (Document) `needs_ocr` | Needs OCR | Yellow dot |
| (Document) `deleting` | Deleting | Spinner |
| any unknown | Unknown | Gray dot + raw safe status text |

UI **never** invents statuses. Raw `status` string always retained on document/library object for round-trip safety.

---

## §9 Polling model

### §9.1 Poll interval

**2000 ms** for non-terminal document statuses. Matches existing worker poll cadence (IndexWorkerManager poll=2s) — bounded, not aggressive.

### §9.2 Terminal statuses (stop polling)

Document: `ready`, `failed`, `needs_ocr`, `deleting` (deleting → row vanishes from list).
Library: no polling (library status transitions are admin-triggered, not background-processed).

### §9.3 Polling cleanup

- Modal close → cancel all in-flight polling `AbortController`s.
- Library switch → cancel polling for previous library's documents.
- Component unmount → `onBeforeUnmount` cleanup all timers + AbortControllers.
- Document row unmount (after delete) → cancel its polling timer.

Hard Gate §96 (polling continues after unmount) — **PASS by design** (Pinia store + AbortController; no `setInterval` outside Vue lifecycle).

---

## §10 Stale-request safety

### §10.1 Library switch race

Scenario: User clicks Library A → request pending → user clicks Library B → A response returns late.

Mitigation: Every async library-scoped request carries an `AbortController` keyed to `selectedLibraryId`. On switch, abort all in-flight requests for previous library. Response handlers verify `currentLibraryId === request.libraryId` before mutating store.

### §10.2 Polling race

Scenario: Library A poll in-flight → user switches to B → A poll returns.

Mitigation: Poll responses check `currentLibraryId === polledLibraryId`. Mismatched responses are dropped (not merged into B's document list).

### §10.3 Search race

Scenario: Search A pending → user types new query → A returns.

Mitigation: Latest query wins. Use a monotonic `searchRequestId` counter in the store; only the latest request's response is committed.

Hard Gate §96 (stale request leaks A into B) — **PASS by design**.

---

## §11 Frontend information architecture

```
SessionSidebar
   └── [Knowledge] button
         ↓ click
      KnowledgeManagerModal
      ┌───────────────────────────────────────────────────┐
      │ Knowledge Management                       [×]     │
      ├───────────────┬───────────────────────────────────┤
      │ Libraries     │ <Selected Library Name>           │
      │               │                                   │
      │ ▶ Library A   │ Documents (3)        [Upload PDF] │
      │   Library B   │ ┌─────────────────────────────┐   │
      │   Library C   │ │ report.pdf     Ready        │   │
      │               │ │ plan.pdf       Indexing    ▾│   │
      │ [+ New]       │ │ old.pdf        Failed  ↻ 🗑│   │
      │               │ └─────────────────────────────┘   │
      │               │                                   │
      │               │ Search this library               │
      │               │ [IMRT conformity___________] [🔍] │
      │               │ ┌─────────────────────────────┐   │
      │               │ │ radiotherapy.pdf            │   │
      │               │ │ Radiation Therapy > IMRT    │   │
      │               │ │ pp.12–13                    │   │
      │               │ │ IMRT uses modulated...      │   │
      │               │ └─────────────────────────────┘   │
      │               │                                   │
      │               │ Available in this conversation    │
      │               │ [ ON | OFF ]                      │
      └───────────────┴───────────────────────────────────┘
```

Visual style: matches existing SkillManagerModal / MCPManagerModal / ProviderSettingsModal — Claude/GPT-like, no new design system, no global CSS redesign.

---

## §12 Session binding UX

### §12.1 Toggle component

`SessionBindingToggle.vue` shown in the right column when a Library is selected.

```
Available in this conversation
[ ON | OFF ]
```

State source: `currentSessionBindingIds` (Pinia store field, loaded once per modal open + refreshed after each toggle).

### §12.2 Bind/unbind flow

```
User toggles ON
   → PUT /api/sessions/{currentSessionId}/knowledge-libraries
      body: library_ids=[...(current - removed), newLibraryId]
   → on success: store.currentSessionBindingIds updated
   → on failure: rollback toggle, show error

User toggles OFF
   → PUT /api/sessions/{currentSessionId}/knowledge-libraries
      body: library_ids=[...current without this library_id]
   → on success: store.currentSessionBindingIds updated
   → on failure: rollback toggle, show error
```

**Replace-all semantics** (existing R1 API contract) — single PUT per toggle, server dedupes + validates.

### §12.3 No auto-binding

- Creating a Library does **not** bind it to the current session.
- Uploading a Document does **not** bind its Library to the current session.
- User must explicitly toggle.

### §12.4 Current session access

`sessionStore.currentSessionId` — non-null when modal opens. If null (no current session), toggle is disabled with tooltip "Select a chat session first".

---

## §13 Markdown preview integration

### §13.1 Existing API

`GET /api/knowledge/documents/{document_id}/markdown` returns raw Canonical MD bytes (`text/markdown; charset=utf-8`, `Content-Disposition: inline`).

### §13.2 UI integration

Document row → "View Markdown" action → opens a side panel within the modal (or stacked modal) showing rendered markdown.

- Renderer: `markdown-it` + `DOMPurify` (already used by message rendering if available; otherwise display raw text in `<pre>` for R5 MVP).
- No remote image loading.
- Page markers (`<!-- page:N -->`) preserved / visible.

**R5 MVP simplification**: If the existing message renderer is not easily reusable, display raw MD text in a `<pre>` block. Markdown rendering is **not** a Hard Gate for R5 (P1-F owns the formal Markdown Workspace Panel).

---

## §14 Error envelope

Existing envelope (every Knowledge endpoint):

```json
{"detail": {"code": "library_not_found", "message": "Library not found."}}
```

(FastAPI HTTPException with dict `detail`; frontend `client.ts` `safeErrorDetail` already extracts `detail.message`.)

R5 Search REST reuses this envelope. **No new envelope.**

---

## §15 Hard Gates (per directive §96)

| # | Gate | Status |
|---|---|---|
| 1 | New duplicate Knowledge API surface | ✅ None (14 reused + 1 new search) |
| 2 | Search endpoint can search all libraries accidentally | ✅ Path-param `library_id` only |
| 3 | Missing `library_id` means ALL | ✅ 404 first, no fallback |
| 4 | Search bypasses ChunkStore | ✅ ChunkStore.search_chunks_fts is sole FTS owner |
| 5 | Search duplicates FTS SQL | ✅ Zero new SQL; reuse primitive |
| 6 | Non-ready documents searchable | ✅ ChunkStore ready-only filter by construction |
| 7 | Document accessible through wrong Library | ✅ ChunkStore library allowlist filter by construction |
| 8 | Frontend stale request leaks A → B | ✅ AbortController + selectedLibraryId guard |
| 9 | Polling continues after unmount | ✅ onBeforeUnmount cleanup + Pinia lifecycle |
| 10 | Upload requires schema migration | ✅ Reuse existing upload API + schema |
| 11 | R5 changes R3 ranking | ✅ Zero ChunkStore diff |
| 12 | R5 changes R4 Agent ACL | ✅ Zero SearchService diff |
| 13 | R5 requires vector / embedding | ⛔ Out of scope, permanent |
| 14 | R5 requires new runtime dependency | ✅ Frontend uses existing Vue/Pinia stack; backend uses existing FastAPI |
| 15 | Real E2E cannot reach ready | ✅ Backend pipeline frozen & verified @ R4-D |
| 16 | REST search source/page incorrect | ✅ source_name + page_start/page_end flow from ChunkStore hit |

---

## §16 E2E plan (R5-D)

| # | Scenario | Mock rule |
|---|---|---|
| E2E-1 | Library lifecycle (create / rename / reload) | Real backend |
| E2E-2 | Upload → uploaded → extracting → normalizing → chunking → indexing → ready | Real backend (PDF parser + Canonical MD + Chunker + FTS5) — **no mocks** |
| E2E-3 | Search "IMRT" returns fixture.pdf p.2 | Real backend |
| E2E-4 | Cross-library isolation (A=apple, B=banana; A:search banana=empty) | Real backend |
| E2E-5 | Delete document / library; search no longer finds it | Real backend |
| E2E-6 | Retry (or backend integration if no safe deterministic fail path) | Backend integration if needed |
| E2E-7 | Session binding on/off via UI; verify via `GET /api/sessions/.../knowledge-libraries` | Real backend |
| E2E-8 | Agent continuity: bind Library → fake LLM emits search_knowledge → tool sees Library → returns Evidence | Real backend + fake LLM |
| E2E-9 | Browser reload: libraries/docs/ready/binding/search all preserved | Real backend |
| E2E-10 | App restart (uvicorn stop + restart) → Library/document/search intact | Real backend |
| E2E-11 | Needs OCR: upload text-empty PDF → status `needs_ocr` → search excludes | Real backend |
| E2E-12 | Failed: deterministic failed doc → status `failed` → retry visible → search excludes | Real backend |

### §16.1 E2E fixture

Small deterministic local PDF generated by `pypdf.PdfWriter` (existing fixture factory used by R2 tests). Two pages with known text:

```
Page 1: "alpha radiotherapy"
Page 2: "IMRT conformity beta"
```

For needs_ocr: separate fixture with empty pages (text-less but valid PDF structure).

For failed: corrupt fixture that passes `%PDF-` magic but fails parse downstream (only if existing test infra has a recipe; otherwise backend integration covers this).

### §16.2 E2E framework

Reuse existing Playwright E2E stack (per memory `project_e2e_layout.md` — `tests/e2e/` with npm environment). Do **not** add Cypress or any second browser E2E framework.

This simplified copy at `D:\LLMTutorial\test\` has **no `tests/e2e/`** directory (per STATUS.md §"测试基线" — E2E is owned by main repo `pi-py`). R5-D will use **backend integration tests** (pytest + httpx AsyncClient) for the REST surface; if browser E2E is required, it runs in the main repo.

**Implication**: R5-D "Browser E2E" gate is satisfied by backend integration tests in this copy + (optional) Playwright in main repo. R5-D in this copy = pytest integration covering full pipeline (create→upload→poll→ready→search→binding→agent continuity).

---

## §17 Scope audit

| Resource | Diff |
|---|---|
| Schema (`knowledge.db`) | 0 |
| `pyproject.toml` runtime deps | 0 |
| `pyproject.toml` dev deps | 0 |
| `uv.lock` | Expected 0 |
| Core Runtime (`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`) | 0 |
| Providers | 0 |
| R2 Ingestion | 0 |
| R3 Chunk / FTS5 | 0 |
| R4 Search Tool / Citation / EvidenceRegistry | 0 |
| Frontend shared client (`api/client.ts`) | 0 |

R5-B adds code only in `web/knowledge/api.py` (1 endpoint + DTOs).
R5-C adds code only in `web/frontend/src/api/knowledge.ts` + `stores/knowledgeStore.ts` + `components/knowledge/*` + 1 entry button in `SessionSidebar.vue`.

---

## §18 Frontend tests plan (R5-C exit gate)

### §18.1 knowledgeStore unit tests

- Library CRUD happy path + error
- Document list / upload / delete / retry
- Status polling (start/stop terminal states)
- Search (loading / success / empty / error / clear on library switch)
- Session binding (load / bind / unbind / rollback on failure)
- Stale request handling (library switch aborts in-flight)
- Polling cleanup (unmount stops timers)

### §18.2 Component tests

- KnowledgeManagerModal: open / close / Escape
- LibraryList: empty state / list / select / create / delete confirm
- DocumentList: empty / list / status rendering / retry button visibility
- DocumentUpload: PDF accepted / non-PDF rejected / 413 / 409 / 500
- KnowledgeSearchPanel: empty query disabled / loading / no results / source+page display
- SessionBindingToggle: on/off / rollback / disabled when no session

### §18.3 Frontend gates

```
npm run test         # vitest
npm run typecheck    # vue-tsc
npm run lint         # eslint
npm run build        # vite build
```

All PASS.

---

## §19 Backend tests plan (R5-B exit gate)

### §19.1 Search REST targeted tests (B3)

- valid library search
- no results
- ready-only (failed/needs_ocr excluded)
- cross-library isolation (A query does not return B chunks)
- deleted library → 404
- deleted document absent from results
- limit default 10 + clamp [1, 50]
- invalid limit (0 / 51 / "abc") → 400
- blank query → 400
- query > 512 chars → 400
- literal FTS safety (special chars in query: `"`, `*`, `OR`, `NEAR`)
- Chinese query
- Unicode filename (`中文.pdf`)
- page metadata correctness
- heading metadata correctness
- library status != active → 409
- no path leak in response
-ChunkStore called with `library_ids=[library_id]` exactly (no None)
- POST body validation (missing query / wrong types)
- response DTO shape (no extra fields)

### §19.2 Regression gates

```
R2 Library tests
R2 Document tests
R2 Upload tests
R2 Ingestion tests
R3 ChunkStore tests
R3 Indexing tests
R4 search_knowledge tool tests
R4 Citation tests
Full Backend ×2 consecutive = 0 failed
```

---

## §20 Frontend build / typecheck contract

R5-C must not regress:

- Frontend build: vitest suite count, bundle size (+ delta acceptable for Knowledge UI; expected < 30 KB gzipped)
- 0 TypeScript errors
- 0 ESLint errors
- `__storeHooks` / `__e2eHooks` scan in built assets (per memory `feedback_e2e_build_mode.md`)

---

## §21 Resource warning gate

Forbid new occurrences of:

- `Task was destroyed but pending`
- `coroutine never awaited`
- `event loop closed`
- `unclosed client`
- `unclosed file`
- `unclosed DB`
- poll timer leak (verifiable via Pinia store unit test asserting `clearInterval` on unmount)

---

## §22 External side-effects gate

R5-D E2E tests must emit:

```
External HTTP      0
DNS                 0
Model downloads     0
Embedding downloads 0
Vector services     0
```

PDF fixtures generated locally via existing pypdf fixture factory. Fake LLM (for Agent continuity E2E-8) is in-process, not network.

---

## §23 Final scope declaration

R5-A scope (this commit):
- docs-only (this file)
- production diff = 0
- test diff = 0
- schema diff = 0
- dependency diff = 0
- STATUS / TODO / ROADMAP updates pending — will be applied in this same commit

R5-A does **NOT**:
- modify any backend code
- modify any frontend code
- modify schema / migrations
- modify dependencies
- touch G1 stash
- merge / tag / push

R5-A **authorizes** R5-B to start (per directive §92 exit gate).

---

## §24 Exit gate checklist

| Item | Status |
|---|---|
| Start HEAD recorded | ✅ `7faf635` |
| API inventory (existing routes) | ✅ §4.1–§4.3 (14 routes) |
| API inventory (new routes) | ✅ §4.4 (1 Search REST) |
| API inventory (duplicate routes) | ✅ §4.5 (0) |
| REST DTOs declared | ✅ §5.4 |
| Search contract frozen | ✅ §5 |
| Search scope (library only) | ✅ §5.5 |
| Search ready-only | ✅ §5.6 |
| Safe FTS | ✅ §5.7 |
| Status mapping | ✅ §8 |
| Polling model | ✅ §9 |
| Frontend architecture | ✅ §7 / §11 |
| Session binding UX | ✅ §12 |
| Stale-request safety | ✅ §10 |
| E2E plan | ✅ §16 |
| Schema diff = 0 | ✅ §17 |
| Dependencies diff = 0 | ✅ §17 |
| R2/R3/R4 frozen boundary | ✅ §17 |
| Hard Gates §96 | ✅ §15 |
| Production diff = 0 (this phase) | ✅ §23 |

**Verdict**: R5-A ✅ COMPLETE / FROZEN — R5-B APPROVED TO START.

---

## §25 Next phase handoff

R5-B (Knowledge REST API):
- Add 1 endpoint `POST /api/knowledge/libraries/{library_id}/search` in `web/knowledge/api.py`
- Add 3 Pydantic DTOs: `LibrarySearchRequest`, `LibrarySearchResponse`, `LibrarySearchResultItem`
- Add 1 dependency helper: `get_chunk_store` (construct on demand from KnowledgeStore, like existing `get_ingestion_store`)
- Add targeted tests per §19.1
- Exit gate §93

R5-B production diff expected: ~120 lines in `api.py` + ~250 lines tests. Schema / deps / R2/R3/R4 modules untouched.

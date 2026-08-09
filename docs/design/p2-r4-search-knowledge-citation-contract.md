# P2-R4-A — search_knowledge + Citation Contract & Security Gate

> **Status**: CONTRACT FROZEN / NOT IMPLEMENTED
> **Baseline**: P2-R3 ✅ FINAL FROZEN @ `599d754`
> **Design ref**: [p2-r4-session-scoped-search-knowledge-citation.md](p2-r4-session-scoped-search-knowledge-citation.md) (110-section design)
> **Phase**: R4-A Contract + Security Gate (docs-only; production diff = 0)

---

## 1. Audited architecture (from real code)

### 1.1 Session identity

- **ID format**: `sess-{ms_timestamp}-{uuid8}` (`session.py:67-70`)
- **Creation**: `POST /api/sessions` (`app.py:2305`)
- **Table**: `sessions` (id / title / created_at / updated_at / metadata)
- **Request scope**: session_id obtained via `session_id_getter` closure pattern (`tools/list_files.py:129`, `tools/view_file.py:782`) — NOT from tool arguments

### 1.2 Session ↔ Library Binding

- **Table**: `session_knowledge_libraries` (session_id / library_id / access_mode='read' / created_at; UNIQUE(session_id, library_id))
- **Store methods**:
  - `list_session_bindings(session_id) → list[SessionLibraryBinding]` (`store.py:1042`)
  - `get_active_library_ids_for_session(session_id) → tuple[str, ...]` (`store.py:1058`)
  - `replace_session_bindings(session_id, library_ids)` (`store.py:1095`)
- **API**: `GET /api/sessions/{sid}/knowledge-libraries` + `PUT` (replace)
- **Delete Library**: cascades binding removal (`store.py:788`)
- **Empty binding**: `get_active_library_ids_for_session` returns empty tuple `()`

### 1.3 ChunkStore.search_chunks_fts (R3-B frozen)

```python
async def search_chunks_fts(
    self, query: str, *,
    library_ids: Sequence[str] | None = None,
    limit: int = 5,
) -> list[ChunkSearchHit]
```

- **`library_ids=None`**: search ALL libraries (internal/maintenance only)
- **`library_ids=[]`**: returns `[]` immediately (`chunk_store.py:553-554`)
- **`library_ids=[id1, id2]`**: SQL `f.library_id IN (?, ?)`
- **ready-only**: `JOIN knowledge_documents d ... WHERE d.status = 'ready'`
- **BM25**: `bm25(knowledge_chunks_fts, 3.0, 1.0)` (heading 3x content)
- **ChunkSearchHit fields**: chunk_id / document_id / library_id / ordinal / heading_path / content / page_start / page_end / rank

### 1.4 Tool runtime

- **Registry**: `ToolRegistry` (`tools/__init__.py:175`) with `register()` / `get(name)`
- **Session injection**: `session_id_getter: Callable[[], str]` closure (NOT tool argument)
- **Tool result**: `ToolResultMessage` appended to messages → next LLM call sees it
- **No hard tool-loop limit** found in current code

### 1.5 System prompt

- `build_default_system_prompt()` (`system_prompt.py:131-162`)
- Sections: base prompt + file_tools_hint + skills + mcp_tools

### 1.6 Assistant message lifecycle

- Events: `message_start` → `message_update × N` → `message_end`
- Persistence: `messages` table (`session_sqlite.py:477-506`)
- Streaming: SSE text_delta events; **citation tokens may split across deltas**
- Regenerate: `POST /api/sessions/{sid}/messages/{aid}/regenerate` (`app.py:2862`)

### 1.7 Knowledge disabled

- `knowledge_root=None` → no KnowledgeStore / no FileStore / no knowledge router (404)
- `state.knowledge_service = None` / `state.knowledge_store = None`

### 1.8 Document source filename

- **Column**: `source_name` (NOT `source_filename`) in `knowledge_documents`
- Canonical Markdown frontmatter uses `source_filename` (R2-B amendment-2)
- R4 Citation uses `source_name` from Document metadata

---

## 2. Frozen contract decisions

### 2.1 Tool name

```
search_knowledge
```

### 2.2 Tool input schema

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "..."},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Forbidden args**: session_id / library_id / library_ids / document_id / user_id / raw_fts_query

### 2.3 Session trust model

```
session_id = session_id_getter() closure  (existing pattern, NOT tool arg)
frozen per request (request-start identity)
resolved at search time (not request-start snapshot)
```

### 2.4 ACL resolution

```
trusted session_id
  → KnowledgeStore.get_active_library_ids_for_session(session_id)
  → tuple of active library IDs (existing library JOIN)
  → if empty: return [] BEFORE calling ChunkStore
  → ChunkStore.search_chunks_fts(library_ids=allowed_ids)
```

### 2.5 Empty binding hard gate

```python
if not allowed_library_ids:
    return SearchKnowledgeResult(query=query, hits=())
```

**NEVER** pass `library_ids=None` to ChunkStore from Tool path.

### 2.6 Retrieval ownership

```
ChunkStore.search_chunks_fts()  (R3-B frozen — sole retrieval owner)
SearchKnowledgeService          (R4-B — calls ChunkStore, owns ACL + Evidence)
search_knowledge Tool           (R4-B — owns Agent-facing invocation)
```

### 2.7 KnowledgeEvidence DTO

```python
@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    evidence_id: str          # "E1", "E2", ...
    document_id: str
    chunk_id: str
    source_filename: str      # from Document.source_name
    heading_path: tuple[str, ...]
    page_start: int
    page_end: int
    content: str
    rank: float
```

### 2.8 Evidence Registry

- Scope: one Assistant turn (request-scoped, NOT DB-persisted)
- ID format: `E1`, `E2`, ... (sequential, turn-unique)
- Duplicate chunk: same `chunk_id` → reuse same `evidence_id`
- Cross-turn: invalid (registry discarded at turn end)

### 2.9 Citation token

```
[cite:E1]
```

- Strict regex: `\[cite:(E[1-9][0-9]*)\]`
- Case-sensitive lowercase `cite`, uppercase `E`
- `E0` invalid

### 2.10 Citation behavior

| Case | Action |
|---|---|
| Valid evidence | Replace `[cite:E1]` → `[1]`, add to source list |
| Unknown evidence `[cite:E99]` | Remove token, record warning |
| Malformed `[cite:]` / `[CITE:E1]` | Leave as plain text |
| Multiple `[cite:E1][cite:E2]` | `[1][2]` |
| Same evidence re-cited | Same number (first-use order) |

### 2.11 Citation rendering

```
Single page:  [1] filename.pdf · p.12
Page range:   [1] filename.pdf · pp.12–13
```

Filename from `Document.source_name`. No paths.

### 2.12 Error taxonomy

| Code | When |
|---|---|
| `knowledge_search_invalid_query` | empty/whitespace query |
| `knowledge_search_session_missing` | no trusted session in runtime |
| `knowledge_search_unavailable` | knowledge subsystem disabled |
| `knowledge_search_failed` | unexpected internal error |

**Zero hits is NOT an error.**

### 2.13 Knowledge disabled behavior

```
knowledge_root=None → search_knowledge NOT registered (consistent with file-tools pattern)
```

### 2.14 Scope

```
New schema       = 0
New dependency   = 0
Frontend diff    = 0
Production diff  = 0 (R4-A is docs-only)
```

---

## 3. R4-B prerequisites (implementation scope)

R4-B implements:
- `SearchKnowledgeService` (session ACL + Evidence conversion)
- `search_knowledge` Tool registration (using `session_id_getter` pattern)
- `KnowledgeEvidence` DTO + `EvidenceRegistry`
- Tool result serialization (structured text for LLM)
- System prompt knowledge section

R4-B does NOT implement:
- Citation parser / validator / renderer (R4-C)
- Assistant finalize citation transform (R4-C)

---

## 4. R4-C prerequisites

R4-C implements:
- `CitationParser` (regex `\[cite:(E[1-9][0-9]*)\]`)
- `CitationValidator` (EvidenceRegistry lookup)
- `CitationRenderer` (source/page formatting)
- Assistant content finalization (post-stream transform)
- Streaming implication: parse on **assembled complete content**, NOT per-delta

---

## 5. Final decision table

| Decision | Frozen Contract |
|---|---|
| Tool name | `search_knowledge` |
| Tool input | `query`, `limit` |
| Default limit | 5 |
| Max limit | 10 |
| session_id Tool arg | FORBIDDEN |
| library_id Tool arg | FORBIDDEN |
| Session source | `session_id_getter` closure (existing pattern) |
| Session lifetime | frozen per request |
| Library ACL | `get_active_library_ids_for_session()` at search time |
| Empty binding | zero results (short-circuit before ChunkStore) |
| Missing Library | excluded by active-library JOIN |
| Retrieval owner | ChunkStore (R3-B frozen) |
| ACL/Evidence owner | SearchKnowledgeService (R4-B) |
| Search backend | SQLite FTS5 / BM25 |
| Search status | ready-only |
| Evidence ID | `E1`, `E2`, ... |
| Evidence scope | Assistant turn |
| Duplicate chunk | reuse evidence ID |
| Citation token | `[cite:E1]` |
| Citation metadata owner | Server |
| Page source | Chunk `page_start` / `page_end` |
| Filename source | `Document.source_name` |
| Unknown evidence | remove token + warning |
| Citation numbering | first-use order |
| Citation DB access | none |
| Knowledge disabled | Tool not registered |
| New schema | 0 |
| New dependency | 0 |
| Vector / embedding | prohibited |

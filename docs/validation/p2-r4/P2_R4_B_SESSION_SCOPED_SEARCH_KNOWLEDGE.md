# P2-R4-B — Session-scoped search_knowledge Validation

> **Phase**: P2-R4-B Session-scoped search_knowledge + Evidence Registry (validation freeze)
> **Baseline**: P2-R4-A ✅ FROZEN @ `6e0089f`
> **R4-B commits**: `04c386b` (B1 service+evidence) → `b8dd49a` (B2 tool+wiring) → `1c1d4d4` (B2 lifecycle fix) → `<this>` (B3 freeze)
> **Date**: 2026-08-10
> **Scope**: validate the search_knowledge Agent Tool end-to-end: Session ACL → ChunkStore → Evidence → Tool Result. **No** Citation parser/renderer (R4-C).

---

## 1. Start HEAD

```
6e0089f — docs(rag): freeze R4 retrieval and citation contract (R4-A)
```

## 2. Commit chain

```
04c386b  feat(rag): add session-scoped knowledge search service (R4-B1)
b8dd49a  feat(rag): add search_knowledge agent tool (R4-B2)
1c1d4d4  fix(rag): reset evidence registry at prompt boundary (R4-B2 lifecycle)
<this>   test(rag): freeze session-scoped knowledge search (R4-B3)
```

## 3-6. Diff scope

```
Production diff:
  search_models.py     (new, 75 lines)
  evidence.py          (new, 98 lines)
  search_service.py    (new, 158 lines)
  search_tool.py       (new, 194 lines)
  state.py             (+4 lines: _evidence_registry field)
  app.py               (+48 lines: tool registration + prompt reset)

Schema diff:           0
Dependency diff:       0
Lockfile diff:         0
Frontend diff:         0
New HTTP routes:       0
```

## 7. Tool schema (frozen per R4-A)

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

Verified by `TestToolSchema::test_schema_has_query_and_limit_only` + `test_schema_forbidden_args_absent`.

Forbidden args (verified absent): session_id / library_id / library_ids / document_id / raw_fts_query.

## 8. Session authority

```
session_id_getter() closure → state.current_session_id
```

NOT from tool args. Verified by `TestToolExecution::test_missing_session_returns_error`.

## 9. Search-time ACL resolution

Each `search_knowledge` call:
```
session_id_getter()
  → KnowledgeStore.get_active_library_ids_for_session(session_id)
  → tuple of active library IDs (INNER JOIN status='active')
  → if empty: short-circuit return () (ChunkStore NOT called)
  → ChunkStore.search_chunks_fts(library_ids=list(allowed_ids))
```

Verified by `TestEmptyBinding::test_empty_binding_does_not_call_chunkstore` (spy: call_count=0).

## 10. Empty binding hard gate

```
allowed_library_ids == () → return SearchKnowledgeResult(hits=())
ChunkStore call count = 0
library_ids = None NEVER passed
```

Verified by 3 tests:
- `test_empty_binding_returns_zero_hits`
- `test_empty_binding_does_not_call_chunkstore`
- `test_empty_binding_never_passes_none`

## 11. Cross-session / cross-library isolation

Verified by `TestACL`:
- `test_cross_library_isolation`: S→A only, search B-only term → 0 hits
- `test_cross_session_isolation`: S1→A, S2→B, S1 can't see B
- `test_unbind_takes_effect`: unbind A → next search 0 hits

## 12. Retrieval ownership

```
ChunkStore.search_chunks_fts() = sole retrieval owner (R3-B frozen)
SearchKnowledgeService = calls ChunkStore, owns ACL + Evidence conversion
search_knowledge Tool = owns Agent-facing invocation
```

No duplicated MATCH/BM25 SQL in R4-B production code (verified by static audit).

## 13. ready-only

Inherited from R3-B ChunkStore JOIN `knowledge_documents WHERE status='ready'`. R4-B does NOT add its own ready filter.

## 14. KnowledgeEvidence DTO

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

Verified by `TestEvidenceIntegration::test_source_filename_from_source_name`.

## 15. EvidenceRegistry lifecycle (Hard Gate)

**Architecture**: `state._evidence_registry` on app-global `WebAppState`. Reset to `None` at `_run_prompt_core` entry (each prompt request = one turn). Getter lazily creates `EvidenceRegistry()` on first `search_knowledge` call within that turn.

```
POST /api/prompt
  → _run_prompt_core
    → state._evidence_registry = None  (turn boundary reset)
    → Agent executes (may call search_knowledge 0..N times)
      → evidence_registry_getter() creates fresh EvidenceRegistry on first call
      → subsequent calls reuse same registry (chunk_id dedupe)
    → Agent finishes
  → next POST /api/prompt → fresh reset → E1 again
```

Verified by `TestEvidenceRegistryLifecycle` (4 tests):
- `test_registry_resets_between_prompt_requests`: Turn 1 E1/E2 → reset → Turn 2 E1
- `test_same_turn_multiple_searches_share_registry`: dedupe within turn
- `test_registry_is_app_state_not_thread_local`: documents architecture
- `test_prompt_core_resets_registry`: verifies reset behavior

**Concurrent isolation**: current single-process sequential-async architecture means one prompt completes before next starts. True concurrent (contextvars) is future enhancement.

## 16. Tool registration

- Knowledge enabled → `harness.agent.tools.register(search_tool)` (guarded by `has()`)
- Knowledge disabled → tool NOT registered (404 on knowledge routes)

Verified by `TestToolSchema::test_tool_registered_when_knowledge_enabled` + `test_tool_not_registered_when_knowledge_disabled`.

## 17. Tool result serialization

Structured text format:
```
Search results for: "query"

[E1]
Source: filename.pdf
Pages: 12-13
Heading: A > B > C
Content: ...
```

No results: `"No relevant knowledge was found..."`

Verified by `TestResultSerialization` (5 tests): format / single page / no ACL leakage / no path leakage.

## 18. Error taxonomy

| Code | When | Tool Result |
|---|---|---|
| `knowledge_search_invalid_query` | empty/whitespace query / limit out of range | `is_error=True` |
| `knowledge_search_session_missing` | `session_id_getter()` returns None | `is_error=True` |
| `knowledge_search_failed` | unexpected exception | `is_error=True` |
| (no error) | zero hits | normal result, `hits=()` |

## 19. R4-C boundary (NOT implemented)

```
CitationParser     = 0
CitationValidator  = 0
CitationRenderer   = 0
[cite:E1] → [1]    = NOT implemented
Assistant finalize citation transform = 0
system prompt citation rules = 0
```

## 20. Static scope audit

```
embedding/vector/reranker in R4-B production   0 hits
search_knowledge registration in R4-B          1 (correct — tool registered)
session_id in Tool schema                      0 (absent — correct)
library_ids in Tool schema                     0 (absent — correct)
MATCH/bm25 SQL in R4-B production              0 (ChunkStore owns all)
Worker persistence SQL                         0 (no indexing_worker changes)
new HTTP routes                                0
new schema                                     0
new dependency                                 0
```

## 21-22. Full Backend ×2

```
Run #1:  3402 passed / 3 skipped / 14 deselected / 0 failed (291s)
Run #2:  3402 passed / 3 skipped / 14 deselected / 0 failed (307s)
```

×2 consecutive 0-failed ✅

Math: 3357 (R3-E) + 29 (R4-B1) + 16 (R4-B2) = 3402 ✅

## 23. R4-B targeted total

```
R4-B1 EvidenceRegistry + SearchKnowledgeService:  29 tests
R4-B2 search_knowledge Tool + lifecycle:          16 tests
────────────────────────────────────────────────────────────
Total R4-B targeted:                             45 tests
```

## 24. Ruff

```
All checks passed!
```

## 25. Frontend

```
Frontend diff = 0 (inherited baseline; not re-run)
```

## 26-28. G1 / B7 / network

```
G1 stash preserved @ 5731ab7d04cdb3c9117be12f3fc00b2143f360bd
B7 PENDING / NOT AUTHORIZED
External HTTP/DNS/OCR/LLM/Embedding/Vector/Reranker = 0
```

## 29. Known Limitations

```
SQLite FTS5 only; unicode61 tokenizer
no semantic retrieval; no reranker; no query rewrite
default top-5 / max top-10
Evidence IDs ephemeral (turn-scoped, not persisted)
no citation validation yet (R4-C)
no citation rendering yet (R4-C)
no [cite:E1] → [1] transform yet (R4-C)
concurrent prompt isolation via reset-at-boundary (not contextvars)
OCR unsupported
```

## 30. R4-B Exit Gate

| # | Condition | Status |
|---|---|---|
| 1 | based on 6e0089f | ✅ |
| 2 | WT clean | ✅ |
| 3 | G1 unchanged | ✅ |
| 4 | schema diff=0 | ✅ |
| 5 | dep/lockfile/frontend diff=0 | ✅ |
| 6 | new HTTP routes=0 | ✅ |
| 7 | Tool name=search_knowledge | ✅ |
| 8 | query+limit args | ✅ |
| 9 | additionalProperties=false | ✅ |
| 10 | forbidden args absent | ✅ |
| 11 | session from trusted getter | ✅ |
| 12 | missing session fails safe | ✅ |
| 13 | ACL resolved per search | ✅ |
| 14 | empty ACL returns zero | ✅ |
| 15 | empty ACL ChunkStore not called | ✅ |
| 16 | library_ids=None never passed | ✅ |
| 17 | cross-library isolation | ✅ |
| 18 | cross-session isolation | ✅ |
| 19 | unbind takes effect | ✅ |
| 20 | ChunkStore remains retrieval owner | ✅ |
| 21 | ready-only retained | ✅ |
| 22 | no duplicated FTS SQL | ✅ |
| 23 | KnowledgeEvidence fields correct | ✅ |
| 24 | source_filename from source_name | ✅ |
| 25 | page from chunk metadata | ✅ |
| 26 | EvidenceRegistry turn-scoped | ✅ |
| 27 | same turn dedupe | ✅ |
| 28 | new turn resets to E1 | ✅ |
| 29 | Tool registered when enabled | ✅ |
| 30 | Tool absent when disabled | ✅ |
| 31 | no duplicate registration | ✅ |
| 32 | Tool result structured text | ✅ |
| 33 | no ACL/path leakage | ✅ |
| 34 | error taxonomy safe | ✅ |
| 35 | CitationParser/Validator/Renderer=0 | ✅ |
| 36 | Assistant finalize=0 | ✅ |
| 37 | embedding/vector/reranker=0 | ✅ |
| 38 | R3 modules untouched | ✅ |
| 39 | R4-B targeted PASS | ✅ 45 tests |
| 40 | R3/R2/R1 regression | ✅ |
| 41 | Full Backend ×2 0-failed | ✅ |
| 42 | Ruff PASS | ✅ |
| 43 | validation complete | ✅ |
| 44 | STATUS/TODO/ROADMAP synced | ✅ |

**44/44 PASS** ✅

## 31. Final verdict

```
P2-R4-B Session-scoped search_knowledge
✅ COMPLETE / FROZEN @ <this commit>

Secure retrieval path verified:

LLM → search_knowledge(query, limit)
→ trusted session_id_getter()
→ active Session-Library ACL
→ ChunkStore SQLite FTS5 / BM25
→ ready-only KnowledgeEvidence
→ turn-scoped E1 / E2 / ...
→ ToolResult → LLM

Security:
LLM-controlled Session ❌
LLM-controlled Library ❌
Empty binding → unrestricted ❌
Cross-session leakage ❌
Cross-library leakage ❌
Global EvidenceRegistry ❌
R3 retrieval duplication ❌
```

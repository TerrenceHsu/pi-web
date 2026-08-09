# P2-R4-A — Contract & Security Gate Validation

> **Phase**: P2-R4-A Retrieval + Citation Contract & Security Gate (docs-only)
> **Baseline**: P2-R3 ✅ FINAL FROZEN @ `599d754`
> **HEAD**: `b59451f` (R4 design doc)
> **Date**: 2026-08-09
> **Scope**: audit existing code, freeze search_knowledge + Citation contract, identify R4-B/C prerequisites. **No** production implementation.

---

## 1. Start HEAD

```
b59451f — docs(rag): add P2-R4 session-scoped search_knowledge + citation design
```

## 2. Audited modules

| Module | Path | Audit Focus |
|---|---|---|
| Session model | `web/session.py`, `web/session_sqlite.py` | ID format, creation, request scope |
| Binding store | `web/knowledge/store.py:1042-1139` | `list_session_bindings` / `get_active_library_ids_for_session` |
| Binding API | `web/app.py:1073-1109` | GET / PUT session-libraries |
| ChunkStore | `web/knowledge/chunk_store.py:515-612` | `search_chunks_fts` signature, None vs [] |
| ToolRegistry | `tools/__init__.py:175` | register / get / session_id_getter |
| Existing tools | `tools/list_files.py`, `tools/view_file.py` | session closure pattern |
| System prompt | `web/system_prompt.py:131-162` | build_default_system_prompt |
| Assistant lifecycle | `web/events.py`, `web/session_sqlite.py:477` | message_start/update/end, persistence |
| Knowledge disabled | `web/app.py:460-470, 920-925` | knowledge_root=None behavior |
| Document schema | `web/knowledge/store.py:132`, `models.py:219` | source_name column |

## 3-20. Key audit findings

### Session identity (§3)
- ID: `sess-{ms}-{uuid8}` via `_gen_session_id()`
- Trusted source: `session_id_getter: Callable[[], str]` closure (injected at tool construction)
- **Verdict**: ✅ existing pattern supports R4-B without new context type

### Binding read path (§4-6)
- `get_active_library_ids_for_session(session_id) → tuple[str, ...]`
- Returns only active (non-deleted) libraries via JOIN
- Empty session → empty tuple `()`
- **Verdict**: ✅ direct reuse for ACL resolution

### ChunkStore search (§7-9)
- `library_ids=None` → ALL libraries; `library_ids=[]` → empty
- ready-only via `JOIN knowledge_documents WHERE status='ready'`
- BM25 heading 3.0 / content 1.0, stable tie-break
- **Verdict**: ✅ safe to call with `library_ids=tuple_from_ACL`

### Tool runtime (§10-12)
- `ToolRegistry.register(tool)` / `get(name)`
- Session via closure `self._session_id_getter()`
- Tool result via `ToolResultMessage` → messages list → next LLM call
- No hard tool-loop limit found
- **Verdict**: ✅ R4-B search_knowledge can follow existing pattern

### Assistant lifecycle (§13-14)
- message_start → message_update(text_delta) × N → message_end
- Citation tokens MAY split across deltas
- **Verdict**: R4-C CitationParser MUST operate on **assembled complete content** at finalization boundary

### Knowledge disabled (§15)
- `knowledge_root=None` → no knowledge router → 404
- **Verdict**: ✅ R4-B: search_knowledge NOT registered when knowledge disabled

### Document filename (§16)
- Column: `source_name` (basename, max 255)
- Canonical Markdown frontmatter: `source_filename` (R2-B amendment-2 naming)
- **Verdict**: R4 Citation uses `Document.source_name` for user-facing display

## 21-33. Security contract decisions

All 33 decisions from the design contract doc (§2.1-2.14) are FROZEN. Key highlights:

- **Tool args**: query + limit ONLY (additionalProperties: false)
- **ACL**: `session_id_getter()` → `get_active_library_ids_for_session()` → pass to ChunkStore
- **Empty binding**: short-circuit `return []` BEFORE ChunkStore call
- **Evidence**: turn-scoped E1/E2/... with chunk_id dedupe
- **Citation**: `[cite:E1]` strict regex, server-controlled metadata, first-use numbering

## 34. Threat model

| Threat | Mitigation |
|---|---|
| T1 ACL bypass | Tool schema has no library_id/session_id fields |
| T2 Session spoofing | session_id from closure, NOT tool arg |
| T3 Empty binding escalation | Service-level `if not allowed_ids: return []` |
| T4 Cross-session leak | `get_active_library_ids_for_session` scoped to session_id |
| T5 Cross-library leak | ChunkStore `library_ids IN (...)` SQL filter |
| T6 Citation hallucination | EvidenceRegistry lookup; unknown → remove |
| T7 Fake filename/page | Server generates from Document/Chunk metadata |
| T8 Cross-turn Evidence | Registry discarded at turn end |
| T9 Path leakage | Only `source_name` exposed; no paths in DTO |
| T10 FTS injection | R3-B `compile_literal_fts_query` reused |
| T11 Non-ready leak | R3-B ready-only JOIN retained |
| T12 Knowledge disabled fallback | Tool not registered when knowledge unavailable |

## 35-40. Scope / regression / exit gate

### Production scope
```
Production diff = 0
Schema diff = 0
Dependency diff = 0
Lockfile diff = 0
Frontend diff = 0
Tool registration = 0
search_knowledge implementation = 0
Citation implementation = 0
```

### Ruff
```
All checks passed! (inherited — no code change)
```

### R4-A Exit Gate

| # | Condition | Status |
|---|---|---|
| 1 | based on b59451f | ✅ |
| 2 | WT clean | ✅ |
| 3 | G1 unchanged | ✅ |
| 4 | Session identity source identified | ✅ §3 |
| 5 | Binding store + read API identified | ✅ §4-6 |
| 6 | ChunkStore search signature verified | ✅ §7-9 |
| 7 | Tool registry + execution audited | ✅ §10-12 |
| 8 | Assistant lifecycle audited | ✅ §13-14 |
| 9 | Knowledge disabled audited | ✅ §15 |
| 10 | Document source_name identified | ✅ §16 |
| 11 | Tool name frozen | ✅ search_knowledge |
| 12 | Tool args frozen | ✅ query + limit |
| 13 | ACL args forbidden | ✅ |
| 14 | Session trust model frozen | ✅ session_id_getter |
| 15 | Empty binding gate frozen | ✅ short-circuit |
| 16 | Evidence DTO frozen | ✅ |
| 17 | Evidence ID format frozen | ✅ E1/E2 |
| 18 | Citation token frozen | ✅ [cite:E1] |
| 19 | Citation behavior frozen | ✅ |
| 20 | Error taxonomy frozen | ✅ |
| 21 | Knowledge disabled behavior frozen | ✅ |
| 22 | Threat model complete | ✅ §34 |
| 23 | Decision table complete | ✅ |
| 24 | R4-B prerequisites clear | ✅ §3 of contract doc |
| 25 | R4-C prerequisites clear | ✅ §4 of contract doc |
| 26 | Open architecture blockers = 0 | ✅ |
| 27 | Production diff = 0 | ✅ |
| 28 | docs complete | ✅ |
| 29 | STATUS/TODO/ROADMAP updated | ✅ |

**29/29 PASS** ✅

## 41. Final verdict

```
P2-R4-A Retrieval + Citation Contract & Security Gate
✅ COMPLETE / FROZEN @ <this commit>

P2-R4-B Session-scoped search_knowledge
✅ APPROVED TO START

P2-R4-C Citation + Agent Integration
⛔ BLOCKED BY R4-B

P2-R4-D Final RAG Integration Freeze
⛔ BLOCKED BY R4-C
```

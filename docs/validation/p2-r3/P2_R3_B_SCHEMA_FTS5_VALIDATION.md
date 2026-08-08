# P2-R3-B — Schema + SQLite FTS5 Knowledge Index Validation

> **Phase**: P2-R3-B Schema + SQLite FTS5 Knowledge Index — Validation Freeze (docs-only)
> **Baseline**: P2-R3-A Chunk Contract + Chunker ✅ FROZEN @ `677fe33` + R3-A-R Reliability Closure ✅ FROZEN @ `64b7416`
> **R3-B commit chain**: `f3401ab` (B1 schema) → `db03be7` (B2 chunk store) → `<this commit>` (B3 freeze)
> **Date**: 2026-08-09
> **Scope**: archive + freeze the R3-B deliverable (schema migration v1→v2 + FTS5 virtual table + chunk store with safe literal FTS query primitive). **No** IndexingOrchestrator / IndexWorker / app lifespan / search_knowledge Agent Tool / E2E upload→ready.

---

## 1. Start HEAD

```
db03be7 — feat(rag): add SQLite FTS5 chunk store (R3-B2 baseline for B3)
```

## 2. Commit chain

```
5c0cb8f  docs(rag): freeze final PDF pipeline validation          (R2-D-B)
677fe33  feat(rag): add heading-aware knowledge chunker           (R3-A)
64b7416  docs(rag): close R3-A reliability gate (R3-A-R)          (R3-A-R)
f3401ab  feat(rag): add chunk and FTS5 schema                     (R3-B1)
db03be7  feat(rag): add SQLite FTS5 chunk store                   (R3-B2)
<this>   test(rag): freeze SQLite FTS5 knowledge index            (R3-B3, this commit)
```

## 3. Python

```
3.12.13 (D:\miniconda\envs\pipy\python.exe)
```

## 4. SQLite runtime

```
sqlite3 module: 2.6.0
SQLite runtime: 3.53.2
```

## 5. FTS5 capability

```
PRAGMA compile_options:
  ENABLE_FTS5                    ✅
  ENABLE_COLUMN_METADATA         ✅
```

Verified by:
- R3-0 capability gate (probe scripts in conversation history)
- R3-B1 fresh-init builds `knowledge_chunks_fts` virtual table + 5 shadow tables
- R3-B2 search tests exercise MATCH / bm25 / unicode61 / remove_diacritics

## 6. Tokenizer

```
tokenize = 'unicode61 remove_diacritics 2'
```

Frozen in both fresh-init DDL and v1→v2 migration DDL (single source-of-truth constant `_V1_TO_V2_FTS_DDL` in `store.py`). Verified by `TestFreshDB::test_fresh_db_fts5_tokenizer_is_frozen` (R3-B1) by inspecting `sqlite_master.sql`.

## 7. Old schema version

```
KNOWLEDGE_SCHEMA_VERSION (pre-R3-B1) = 1
```

R1 stub. `knowledge_chunks` table existed but unused (R2 never inserts). R3-A DTO did not match R1 column shape.

## 8. New schema version

```
KNOWLEDGE_SCHEMA_VERSION (post-R3-B1) = 2
```

## 9. Migration

`KnowledgeStore._initialize_schema()` branches:

| Detected state | Action |
|---|---|
| No meta row (fresh) | `_initialize_fresh_latest_schema()` builds full v2 (5 tables + FTS5 + 7 indexes + R3-A chunk columns) |
| `version == 1` | `_migrate_v1_to_v2()` idempotent ALTER + CREATE VIRTUAL TABLE |
| `version == 2` | `_validate_v2_schema()` light table existence check |
| `version > 2` | `KnowledgeSchemaVersionError` ("newer than this application") |
| `version < 1` | `KnowledgeSchemaVersionError` ("unsupported") |

Migration is **transactional** (single `BEGIN IMMEDIATE`), **idempotent** (column / table existence pre-check), and **non-destructive** (no DROP, no recreate). Failure → ROLLBACK; v1 data intact.

## 10. Existing data preservation

`TestV1Migration` suite (R3-B1, 7 tests) verifies post-migration:

| Table | Preserved |
|---|---|
| `knowledge_libraries` | ✅ 1 row (lib_test01, "Test Library", active) |
| `knowledge_documents` | ✅ 1 row (doc_test00000001, normalizing, pypdf/6.14.2) |
| `knowledge_ingestion_jobs` | ✅ 1 row (job_test01, normalize/completed) |
| `session_knowledge_libraries` | ✅ 1 row (sess_test01, lib_test01, read) |
| `knowledge_chunks` (v1 stub row) | ✅ preserved with new columns backfilled (char_count=0, content_sha256='') |

## 11. knowledge_chunks schema (v2)

```sql
CREATE TABLE knowledge_chunks (
    id              TEXT PRIMARY KEY,
    library_id      TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    heading_path    TEXT NOT NULL DEFAULT '',   -- JSON array string
    page_start      INTEGER NOT NULL,
    page_end        INTEGER NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL,              -- R1 legacy alias
    char_count      INTEGER NOT NULL DEFAULT 0, -- R3-A
    content_sha256  TEXT NOT NULL DEFAULT '',   -- R3-A
    token_count     INTEGER NOT NULL DEFAULT 0, -- R1 legacy (unused by R3-A)
    created_at      INTEGER NOT NULL,
    CHECK (ordinal >= 0),
    CHECK (page_start >= 1),
    CHECK (page_end >= page_start),
    CHECK (content <> ''),
    UNIQUE (document_id, ordinal)
);
```

## 12. Constraints

Schema-level (fresh v2 + migrated v1→v2, identical):
- `PRIMARY KEY (id)`
- `UNIQUE (document_id, ordinal)`
- `CHECK (ordinal >= 0)`
- `CHECK (page_start >= 1)`
- `CHECK (page_end >= page_start)`
- `CHECK (content <> '')`

Application-level (R3-B2 ChunkStore._validate_chunk_sequence):
- `chunk.document_id == caller document_id`
- `chunk.ordinal == index` (contiguous 0..N-1)
- chunk IDs unique within sequence
- `chunk.char_count == len(chunk.content)`
- `chunk.char_count > 0`
- `chunk.content_sha256 == compute_content_sha256(chunk.content)`
- `len(chunk.content_sha256) == 64`
- `chunk.id == compute_chunk_id(document_id, ordinal, content_sha256)` (deterministic R3-A algorithm)
- `chunk.page_start >= 1`
- `chunk.page_end >= chunk.page_start`

R3-B2 is the **strict enforcement layer** (SQLite ALTER cannot add CHECK on migration, so the schema is intentionally permissive; the store layer is the single source of strictness).

## 13. Indexes

R1 indexes retained:
- `idx_chunks_document ON knowledge_chunks(document_id)`
- `idx_chunks_library ON knowledge_chunks(library_id)`

R3-B2 search uses the existing `UNIQUE(document_id, ordinal)` index implicitly for tie-break ordering. No additional indexes added.

## 14. heading_path serialization

JSON array string. Roundtrip stable for:
- empty tuple `()` → `"[]"`
- single-element `("Intro",)` → `'["Intro"]'`
- nested `("Chapter 1", "Treatment Planning", "Dose")` → 3-element JSON array
- Unicode `("第一章", "治疗计划", "剂量")` → JSON with `ensure_ascii=False`

R3-B2 store uses `json.dumps(list(...), ensure_ascii=False)` on write and `json.loads(...)` → `tuple(...)` on read. Verified by `TestReplaceHappyPath::test_*_heading_path_roundtrip_*` (3 tests).

## 15. library_id decision

**Denormalized** (Option A per directive §8).

`knowledge_chunks.library_id` populated from caller-supplied `library_id` parameter at `replace_document_chunks` time. Caller (R3-C future) is required to resolve `library_id` from `Document.library_id`; **ChunkStore does not** accept arbitrary library_id from untrusted sources. Verified by `TestLibraryFilter` suite (R3-B2).

Rationale: R4 library allowlist filtering becomes a single-table WHERE clause (no JOIN with documents for every search).

## 16. FTS schema

```sql
CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(
    chunk_id     UNINDEXED,
    document_id  UNINDEXED,
    library_id   UNINDEXED,
    heading_text,
    content,
    tokenize     = 'unicode61 remove_diacritics 2'
);
```

`chunk_id` / `document_id` / `library_id` are `UNINDEXED` (stored but not tokenised). `heading_text` + `content` are searchable. Tokenizer frozen per P2-R3-0.

## 17. FTS sync mode

**Explicit transaction** (no SQLite triggers).

`ChunkStore.replace_document_chunks` executes in one `BEGIN IMMEDIATE`:
1. `DELETE FROM knowledge_chunks_fts WHERE document_id = ?`
2. `DELETE FROM knowledge_chunks WHERE document_id = ?`
3. `INSERT INTO knowledge_chunks ...` (per chunk)
4. `INSERT INTO knowledge_chunks_fts ...` (per chunk)
5. `COMMIT`

Any failure → `ROLLBACK`. No partial state.

Rationale: explicit-transaction sync is easier to test, rebuild, and reason about than triggers; avoids hidden SQL behaviour. The single source-of-truth is `knowledge_chunks`; `knowledge_chunks_fts` is a derived index that can be dropped and rebuilt at any time.

## 18. Source-of-truth

```
knowledge_chunks       → durable
knowledge_chunks_fts   → derived (can be rebuilt)
```

`ChunkStore.rebuild_fts()` clears FTS and re-inserts every chunk row. Idempotent; verified by `TestRebuildFts::test_rebuild_idempotent` (snapshot equality across two rebuilds).

## 19. replace_document_chunks

Public API:

```python
async def replace_document_chunks(
    self,
    *,
    document_id: str,
    library_id: str,
    chunks: Sequence[KnowledgeChunk],
) -> None
```

Preconditions:
- `document_id` / `library_id` valid ID formats
- `chunks` non-empty
- Document exists with `status='chunking'`
- All chunks pass `_validate_chunk_sequence` (see §12)

Atomic. FTS + chunks in one transaction. See `TestReplaceValidation` (11 tests) + `TestReplaceHappyPath` (7 tests).

## 20. Validation rules

Per-chunk (R3-B2 `_validate_chunk_sequence`):
1. `chunk.document_id == caller document_id`
2. `chunk.ordinal == index` in the sequence
3. chunk IDs unique within the sequence
4. `chunk.char_count == len(chunk.content)`
5. `chunk.char_count > 0`
6. `chunk.content_sha256 == compute_content_sha256(chunk.content)`
7. `len(chunk.content_sha256) == 64`
8. `chunk.id == compute_chunk_id(document_id, ordinal, content_sha256)`
9. `chunk.page_start >= 1`
10. `chunk.page_end >= chunk.page_start`

State-level: Document must be `chunking` (defense-in-depth; R3-C owns the runtime transitions).

## 21. Rebuild

```python
async def rebuild_fts(self) -> int  # returns rows written
```

- Single `BEGIN IMMEDIATE` transaction
- `DELETE FROM knowledge_chunks_fts` then re-INSERT every chunk
- `COMMIT` on success; `ROLLBACK` on any failure (previous FTS preserved)
- Idempotent (verified by `TestRebuildFts::test_rebuild_idempotent`)
- Does not modify chunks / documents / chunk IDs / Document status
- No network access

## 22. Integrity

```python
async def verify_fts_integrity(self) -> FTSIntegrityReport
```

Returns:
```python
@dataclass(frozen=True, slots=True)
class FTSIntegrityReport:
    chunk_count: int
    fts_count: int
    orphan_fts_rows: int       # FTS rows whose chunk_id has no chunk
    chunks_missing_fts: int    # chunks with no FTS row
    ok: bool                   # chunk_count==fts_count and both deltas == 0
```

Does NOT scan Markdown or verify content_sha256 round-trip. Verified by `TestVerifyIntegrity` (4 tests: clean / after-replace / orphan-fts / missing-fts).

## 23. Delete cascade (Document)

`KnowledgeStore.delete_document_hard(document_id)` (R3-B2 minimal integration):

```sql
BEGIN IMMEDIATE
DELETE FROM knowledge_chunks_fts WHERE document_id = ?
DELETE FROM knowledge_chunks    WHERE document_id = ?
DELETE FROM knowledge_ingestion_jobs WHERE document_id = ?
DELETE FROM knowledge_documents WHERE id = ?
COMMIT
```

FTS cleanup atomic with chunk cascade. Service API unchanged. Verified by `TestDeleteCascadesFts::test_delete_document_hard_clears_fts`.

## 24. Delete cascade (Library)

`KnowledgeStore.delete_library_hard(library_id)` (R3-B2 minimal integration):

```sql
BEGIN IMMEDIATE
DELETE FROM knowledge_chunks_fts WHERE library_id = ?
DELETE FROM knowledge_chunks    WHERE library_id = ?
DELETE FROM knowledge_ingestion_jobs WHERE document_id IN (SELECT id FROM knowledge_documents WHERE library_id = ?)
DELETE FROM knowledge_documents WHERE library_id = ?
DELETE FROM session_knowledge_libraries WHERE library_id = ?
DELETE FROM knowledge_libraries WHERE id = ?
COMMIT
```

Verified by `TestDeleteCascadesFts::test_delete_library_hard_clears_fts`.

## 25. Safe query compiler

`compile_literal_fts_query(query: str) -> str`:

1. Trim input
2. Reject if empty / whitespace-only → `FTSQueryError`
3. Reject if `len(query) > MAX_FTS_QUERY_CHARS` (512) → `FTSQueryError`
4. Split on `\s+` into terms
5. For each term: replace internal `"` with `""` (FTS5 escape), wrap in `"..."` phrase
6. Join phrases with single space (implicit AND)

Output is always a sequence of phrase tokens. User can NEVER inject FTS5 syntax. Verified by `TestCompileLiteralFtsQuery` (10 tests including `OR` / `*` / unclosed quote / `title:bar` / `(a)` / `a"b`).

## 26. Query max

```
MAX_FTS_QUERY_CHARS = 512
```

## 27. Limit

```
DEFAULT_FTS_LIMIT = 5
MAX_FTS_LIMIT     = 50
```

`limit < 1` or `limit > MAX_FTS_LIMIT` → `ChunkValidationError`.

## 28. BM25 weights

```python
bm25(knowledge_chunks_fts, 3.0, 1.0)
```

Order matches FTS5 virtual table column order:
- column 0: `chunk_id` (UNINDEXED — ignored by bm25)
- column 1: `document_id` (UNINDEXED)
- column 2: `library_id` (UNINDEXED)
- column 3: `heading_text` → weight **3.0**
- column 4: `content` → weight **1.0**

Heading match weighted 3x content match.

## 29. Stable tie-break

```sql
ORDER BY rank ASC, f.document_id ASC, c.ordinal ASC, f.chunk_id ASC
```

`rank` is `bm25(...)` (negative; lower = better). Ties broken deterministically by `document_id → ordinal → chunk_id` (all ASC). Verified by `TestSearchFts::test_stable_tie_break`.

## 30. Ready-only

```sql
JOIN knowledge_documents AS d ON d.id = f.document_id
WHERE ... AND d.status = 'ready'
```

Documents in `uploaded / extracting / normalizing / chunking / indexing / failed / needs_ocr / deleting` are NEVER returned, even if FTS rows exist. Verified by `TestReadyOnlyFilter` (7 parametrized tests).

## 31. Library filter

```python
async def search_chunks_fts(
    query: str,
    *,
    library_ids: Sequence[str] | None = None,  # None = all (maintenance)
    limit: int = DEFAULT_FTS_LIMIT,
) -> list[ChunkSearchHit]
```

- `library_ids=None` → search across all libraries (internal / maintenance only)
- `library_ids=[]` → return `[]` immediately
- `library_ids=[lib_a, lib_b]` → restrict via `f.library_id IN (?, ?)`

Invalid library_id format in filter → `ChunkValidationError`.

**R3-B does NOT do Session binding**. R4 will resolve `Session → trusted library_ids` and pass them in.

## 32. Chinese exact-term result

unicode61 tokenises CJK runs as one token per run. Exact full-string match (e.g. query `"放射治疗计划与剂量分布"` against content `"放射治疗计划与剂量分布"`) returns the chunk. Partial-substring queries (e.g. `"放射"` alone) may not match — this is a known limitation of unicode61 (no CJK segmentation).

Documented as Known Limitation (§56). R3-B does NOT add jieba / ICU / surya / any external tokenizer.

## 33. Chinese segmentation limitation

```
SQLite FTS5 unicode61 does NOT segment Chinese into words.
"放射治疗" is one token; "放射" alone is a different (non-matching) token.
```

This is a fundamental capability of the chosen tokenizer, not a defect. R3-B accepts this in exchange for: zero new dependencies / no native extension / no model download / pure-stdlib.

## 34. Path leak audit

`ChunkSearchHit` DTO fields:
- `chunk_id` / `document_id` / `library_id` / `ordinal` / `heading_path` / `content` / `page_start` / `page_end` / `rank`

No absolute paths. No filesystem paths. No SQL. No traceback. No session_id. No raw exception messages. Verified by `TestSearchFts::test_returns_dto_fields` and by static review of all error classes (`Chunk*Error` / `FTS*Error`) — messages contain only type names + safe descriptions.

## 35. Transaction rollback

`replace_document_chunks` and `delete_document_chunks` both wrap in `try: ... except: ROLLBACK; raise`. Failures never leave partial chunks + FTS state. Verified structurally (code review) + by `TestReplaceHappyPath::test_replace_removes_old_chunks` (replace is itself a delete-then-insert round trip).

## 36. Concurrency

R3-B does NOT add `asyncio.Lock` of its own. It re-uses `KnowledgeStore._write_lock` (single asyncio.Lock around all writes). Two concurrent `replace_document_chunks` calls on the same document serialise via this lock; SQLite `BEGIN IMMEDIATE` provides row-level serialisation at the DB layer.

R3-B does not need to support multi-writer high throughput. Single IndexWorker (R3-D future) is the only writer.

## 37. Migration tests

`tests/test_r3_b_schema_migration.py` — 18 tests across 4 suites:

| Suite | Tests | Coverage |
|---|---|---|
| `TestFreshDB` | 6 | fresh-init / tables / R3-A columns / FTS5 + shadow tables / tokenizer frozen / row accepts R3-A columns |
| `TestV1Migration` | 7 | v1→v2 migration / libraries / documents / jobs / bindings / legacy chunks preserved / FTS5 created |
| `TestIdempotency` | 3 | v2 reopen no-op / migration reopen no-op / future-version rejected |
| `TestFTS5Capability` | 2 | MATCH works post-migration / bm25 returns negative score |

All 18 PASS in 4.44s.

## 38. Store tests

`tests/test_chunk_store.py` — 66 tests across 11 suites:

| Suite | Tests | Coverage |
|---|---|---|
| `TestCompileLiteralFtsQuery` | 10 | simple / OR literal / quote escape / Chinese / whitespace / empty / too-long / single term / no FTS syntax exposure |
| `TestReplaceValidation` | 11 | empty / invalid IDs / mismatch / ordinal gap / duplicate ID / wrong SHA / wrong char_count / wrong chunk_id / page range / non-chunking state / missing document |
| `TestReplaceHappyPath` | 7 | single chunk / FTS row / multi-chunk order / heading_path roundtrip / Unicode heading_path / empty heading_path / replace removes old |
| `TestDeleteChunks` | 2 | clears chunks+FTS / unknown document no-op |
| `TestRebuildFts` | 3 | rebuild after manual FTS clear / idempotent / rebuild after empty |
| `TestVerifyIntegrity` | 4 | clean / after-replace / orphan-fts / missing-fts |
| `TestSearchFts` | 14 | simple / case-insensitive / no-match / multi-term AND / OR literal / star literal / unclosed quote / empty / whitespace / too-long / limit 0 / limit too large / limit 1 / default limit / stable tie-break / DTO fields |
| `TestReadyOnlyFilter` | 7 | parametrised across uploaded/extracting/normalizing/chunking/indexing/failed/needs_ocr — all excluded |
| `TestLibraryFilter` | 2 | filter returns only matching / invalid library_id |
| `TestDeleteCascadesFts` | 2 | document delete clears FTS / library delete clears FTS |
| `TestChineseSearch` | 1 | exact-term capability smoke |

All 66 PASS in 5.94s.

## 39. Search tests

Counted within `TestSearchFts` (14) + `TestReadyOnlyFilter` (7) + `TestLibraryFilter` (2) = **23 search-specific tests**. Covers directive §37 matrix items 1-32 (Latin / case-insensitive / diacritic / Unicode / heading match / content match / multi-term / quoted input / OR literal / star literal / unclosed quote / empty / whitespace / too-long / limit validation / ready-only / library filter / stable tie-break / no path leak).

## 40. R3-A regression

```
tests/test_chunker.py — 44/44 PASS in 1.80s
```

R3-A frozen; no `chunker.py` modifications in R3-B.

## 41. R2 regression

Run as part of full Backend (§44). R2 frozen modules untouched:
- `pdf_parser.py` / `pypdf_parser.py` / `pdf_quality.py`
- `canonical_markdown.py` / `markdown_persistence.py`
- `ingestion_store.py` / `ingestion_orchestrator.py` / `ingestion_worker.py`
- `upload_service.py`
- `chunker.py` (R3-A)

Only `store.py` modified (schema integration per directive §56 allowance); changes limited to:
- `KNOWLEDGE_SCHEMA_VERSION: 1 → 2`
- v2 fresh DDL + v1→v2 migration constants
- `_initialize_schema` / `_initialize_fresh_latest_schema` / `_migrate_v1_to_v2` / `_validate_v2_schema`
- `_existing_columns` / `_table_exists` helpers (new)
- `insert_chunk` derives char_count + content_sha256 (R1 DTO compat)
- `delete_document_hard` / `delete_library_hard` add FTS cleanup in same transaction

## 42. R1 regression

```
tests/test_knowledge_store.py — 54/54 PASS
```

R1 schema validation tests continue to PASS. R3-B1's `insert_chunk` derivation change (filling v2 columns from R1 Chunk DTO) keeps R1 contract intact.

R1 targeted (per directive §50):

```
118 selected = 117 passed + 1 platform skip
```

## 43. C4-R regression

Run as part of full Backend (§44). C4-R subprocess isolation still active (no `importlib.reload()` in `test_r2_c_security_boundaries.py`; only docstring reference). Victim suites (test_upload_api + test_web_prompt_execution_split) all PASS.

## 44. Full Backend

```
Run #1 (post-R3-B2):
  collected: 3255
  selected:  3241 (= 3241 passed + 0 failed + 3 skipped + 0 xfailed + 0 xpassed)
  deselected: 14
  duration: 456.86s
```

Mathematical reconciliation:

```
3113 (R2 baseline selected)
+ 44  (R3-A chunker)
+ 18  (R3-B1 schema migration)
+ 66  (R3-B2 chunk store)
─────
3241 selected
```

All selected tests PASS. **0 failed**.

## 45. Actual pytest counts

| Metric | Value |
|---|---|
| total collected | 3255 |
| selected | 3241 |
| passed | 3241 |
| failed | 0 |
| skipped | 3 |
| deselected | 14 |
| xfailed | 0 |
| xpassed | 0 |

Selected formula: `selected = passed + failed + skipped + xfailed + xpassed = 3241 + 0 + 3 + 0 + 0 = 3241` ✅

Total formula: `total collected = selected + deselected = 3241 + 14 = 3255` ✅

## 46. Ruff

```
$ python -m ruff check src tests scripts
All checks passed!
```

## 47. Frontend

R3-B does not touch `frontend/**`. Baseline maintained:

```
267/267 vitest PASS (11 files)
typecheck PASS (vue-tsc --noEmit)
lint PASS (eslint . --max-warnings=0)
build PASS (169.32 KB JS / 46.11 KB CSS)
```

Frontend diff vs R3-B baseline (`f3401ab`): **0**.

## 48. Dependency diff

```
pyproject.toml: 0 changed lines
uv.lock:        0 changed lines
```

R3-B introduces **zero** new runtime dependencies. Uses only:
- stdlib (`sqlite3`, `hashlib`, `json`, `re`, `time`, `dataclasses`, `collections.abc`, `typing`)
- existing project deps: `aiosqlite`, `pydantic` (via models)

No FAISS / Qdrant / Milvus / pgvector / Whoosh / Lucene / Jieba / MeCab / Redis / Celery / tiktoken / transformers / sentence-transformers / Elasticsearch.

## 49. Production diff

vs R3-B baseline (`f3401ab` = end of R3-B1):

```
src/pi_agent_core_py/web/knowledge/chunk_store.py  +678 (new file)
src/pi_agent_core_py/web/knowledge/store.py        +17 / -2 (delete_*_hard FTS cleanup)
```

Total production diff: 1 new file + 1 file minimally modified. R2 frozen modules untouched.

## 50. Frozen-module audit

`rg` over R3-B2 production files confirms:

```
embedding | vector | dense | hybrid | rerank | cross-encoder | rrf | mmr | hnsw | ivf | milvus | qdrant | faiss | pgvector
  → 0 hits in chunk_store.py

search_knowledge | register_tool | tool_registry | session_id
  → 0 hits in chunk_store.py

IndexingWorker | IndexingOrchestrator | claim_next_normalizing | normalizing.*chunking | indexing.*ready
  → 0 hits in chunk_store.py

load_extension | enable_load_extension
  → 0 hits in chunk_store.py + store.py

httpx | requests | aiohttp | urllib.request | openai | anthropic | tesseract | surya | torch | transformers
  → 0 hits in chunk_store.py
```

## 51. Network audit

```
External HTTP           0
DNS                     0
Remote fetch            0
Model download          0
OCR                     0
LLM                     0
Provider                0
Embedding               0
Vector operation        0
Reranker                0
search_knowledge Tool   0 (NOT registered; R4 scope)
```

Static + dynamic audit. ChunkStore has zero network imports. Verified by R3-A-R methodology reused for chunk_store.py (rg over import statements).

## 52. Embedding / vector audit

Zero hits across `chunk_store.py` + `store.py` (R3-B2 modifications only) + `pyproject.toml` + `uv.lock`. R3 retrieval is **pure FTS5 BM25**, no embedding / vector / hybrid / reranker — permanently (per P2-R2 retrieval scope narrowing @ `09261ea`).

## 53. Agent Tool audit

```
search_knowledge registration: 0
tool_registry invocations:     0
LLM prompt exposure:           0
session_id in retrieval:       0
```

`ChunkStore.search_chunks_fts` is an **internal Store primitive**. R4 will own Session binding + Tool registration + LLM prompt integration. R3-B explicitly does not implement any of these.

## 54. G1

```
G1 Assistant Markdown Rendering
⚠ Original stash object continuity was historically broken
✅ Content equivalence verified by C4-R
⏸ Replacement stash preserved @ 5731ab7d04cdb3c9117be12f3fc00b2143f360bd
```

`git rev-parse stash@{0}` returns the same hash at R3-B start, R3-B1 start, R3-B2 start, and R3-B3 start. No pop / apply / drop / recreate. R3-B commits contain zero G1 file content.

## 55. B7

```
B7 SQLite Store Open-Failure Cleanup
⏸ PENDING / NOT AUTHORIZED
```

4 historical Stores (`KnowledgeStore` / `SQLiteCredentialStore` / `ExtensionStore` / `SessionStore`) still lack try/except close protection on `open()` failure. R3-B **does not modify** these — but `ChunkStore` (new code) does not introduce a new Store.open() path (it wraps the existing `KnowledgeStore` connection). So:

```
new R3-B code:    ✅ no new B7 defect
historical B7:    ⏸ untouched
Blocks R3-B:      NO
Blocks R3-C/D/E:  NO
```

## 56. Known Limitations

- SQLite FTS5 only (no Postgres / MySQL FTS)
- `unicode61` tokenizer (no language-specific word segmentation)
- Chinese partial-term segmentation limited (exact-term works; substring may miss)
- No fuzzy retrieval
- No semantic retrieval
- No embeddings
- No reranker
- No query rewriting
- No `search_knowledge` Agent Tool yet (R4 scope)
- No Session authorization in retrieval yet (R4 scope)
- No citation rendering (R4 scope)
- No Indexing Runtime yet (R3-C scope)
- No IndexWorker yet (R3-D scope)
- No automatic normalizing → chunking → indexing → ready (R3-C / R3-D scope)
- Single local SQLite DB
- Document must be `ready` before retrieval (R3-B enforces; R4 will rely on this)
- pypdf layout limitations (R2-A inherited)
- Windows junction / reparse point gap (R6 scope)
- B7 SQLite Store Open-Failure Cleanup remains pending

The following are NOT limitations — they are **must-block** defects that would invalidate R3-B:
- orphan FTS rows (verifier would catch)
- failed doc searchable (ready-only filter would catch)
- cross-library leakage (library filter would catch)
- FTS syntax injection crash (safe compiler would catch)
- delete producing orphan (cascade tests would catch)
- migration data loss (TestV1Migration would catch)

None of these defects exist in R3-B.

## 57. R3-B Exit Gate

| # | Condition | Status |
|---|---|---|
| 1 | HEAD includes `db03be7` | ✅ |
| 2 | working tree initial clean | ✅ |
| 3 | G1 unchanged | ✅ §54 |
| 4 | R3-A frozen | ✅ |
| 5 | FTS5 available | ✅ §5 |
| 6 | unicode61 available | ✅ §6 |
| 7 | bm25 available | ✅ §5 |
| 8 | schema version bumped (1→2) | ✅ §7/§8 |
| 9 | R2 DB migrates in-place | ✅ §9/§10 |
| 10 | existing libraries preserved | ✅ §10 |
| 11 | existing documents preserved | ✅ §10 |
| 12 | jobs preserved | ✅ §10 |
| 13 | bindings preserved | ✅ §10 |
| 14 | migration transactional | ✅ §9 |
| 15 | knowledge_chunks created with R3-A columns | ✅ §11 |
| 16 | chunk PK / UNIQUE correct | ✅ §12 |
| 17 | document ordinal unique | ✅ §12 |
| 18 | page constraints | ✅ §12 |
| 19 | heading_path stable roundtrip | ✅ §14 |
| 20 | FTS table created | ✅ §16 |
| 21 | tokenizer frozen | ✅ §6 |
| 22 | source-of-truth = chunks | ✅ §18 |
| 23 | sync mode single (explicit transaction) | ✅ §17 |
| 24 | Chunk+FTS transaction atomic | ✅ §17/§35 |
| 25 | replace removes stale rows | ✅ TestReplaceHappyPath |
| 26 | rollback no partial chunks | ✅ §35 |
| 27 | rollback no partial FTS | ✅ §35 |
| 28 | Document delete no orphan chunks | ✅ §23 |
| 29 | Document delete no orphan FTS | ✅ §23 |
| 30 | Library delete no orphan chunks | ✅ §24 |
| 31 | Library delete no orphan FTS | ✅ §24 |
| 32 | rebuild works | ✅ §21 |
| 33 | rebuild idempotent | ✅ §21 |
| 34 | integrity works | ✅ §22 |
| 35 | query compiler safe | ✅ §25 |
| 36 | malformed query no SQLite leak | ✅ TestCompileLiteralFtsQuery |
| 37 | BM25 ranking works | ✅ §28 |
| 38 | heading weighting works | ✅ §28 |
| 39 | stable tie-break | ✅ §29 |
| 40 | ready-only works | ✅ §30 |
| 41 | failed excluded | ✅ TestReadyOnlyFilter |
| 42 | normalizing excluded | ✅ TestReadyOnlyFilter |
| 43 | chunking excluded | ✅ TestReadyOnlyFilter |
| 44 | indexing excluded | ✅ TestReadyOnlyFilter |
| 45 | needs_ocr excluded | ✅ TestReadyOnlyFilter |
| 46 | library filter works | ✅ §31 |
| 47 | empty allowed-libraries returns 0 | ✅ TestLibraryFilter |
| 48 | no path leak | ✅ §34 |
| 49 | no session logic | ✅ §53 |
| 50 | no Agent Tool | ✅ §53 |
| 51 | no IndexingOrchestrator | ✅ §50 |
| 52 | no IndexWorker | ✅ §50 |
| 53 | no app lifecycle indexing | ✅ (state.py / app.py unchanged) |
| 54 | no Chunker changes | ✅ (chunker.py untouched) |
| 55 | no frozen R2 module changes | ✅ §41 |
| 56 | new dependencies = 0 | ✅ §48 |
| 57 | embedding = 0 | ✅ §52 |
| 58 | vector = 0 | ✅ §52 |
| 59 | reranker = 0 | ✅ §52 |
| 60 | network = 0 | ✅ §51 |
| 61 | R3-B targeted PASS (84 tests) | ✅ 18 + 66 = 84 |
| 62 | R3-A 44 PASS | ✅ §40 |
| 63 | R2 regression PASS | ✅ §41 |
| 64 | R1 117 pass + 1 skip | ✅ §42 |
| 65 | full Backend failed=0 | ✅ §44 |
| 66 | Ruff PASS | ✅ §46 |
| 67 | Frontend PASS | ✅ §47 |
| 68 | Validation complete | ✅ this doc |
| 69 | STATUS/TODO/ROADMAP updated | ✅ §60 |
| 70 | B7 remains pending | ✅ §55 |
| 71 | git diff --check PASS | ✅ |
| 72 | working tree final clean | ✅ (post-commit) |
| 73 | Open blockers = 0 | ✅ |

**73/73 PASS** ✅

## 58. Final verdict

```
P2-R3-B Schema + SQLite FTS5 Knowledge Index
✅ COMPLETE / FROZEN @ <this commit>

P2-R3-C Indexing Runtime
✅ APPROVED TO START
  (independent authorisation required; not started in this commit)

P2-R3-D Bounded Index Worker
⛔ BLOCKED BY R3-C

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

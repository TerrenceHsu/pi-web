# P2-R3-B1 — Schema Migration v1 → v2 Contract

> **Phase**: P2-R3-B1 Schema Migration (algorithm only — no FTS query primitive)
> **Baseline**: P2-R3-A Chunk Contract + Chunker ✅ FROZEN @ `677fe33`
> **R3-B1 commit**: this commit (hash assigned at commit time)
> **Scope**: schema version v1→v2 migration; `knowledge_chunks` extended with R3-A columns; `knowledge_chunks_fts` FTS5 virtual table added; `insert_chunk` updated to populate new columns. **Not** in scope: chunk store API, FTS sync transaction, search primitive, integrity check (these are R3-B2).

---

## 1. Migration design

### 1.1 Version bump

```
KNOWLEDGE_SCHEMA_VERSION: 1 → 2
```

`_SCHEMA_META_KEY` unchanged: `"schema_version"`.

### 1.2 Initialization paths

`_initialize_schema()` now branches:

| Detected state | Action |
|---|---|
| No meta row (fresh DB) | `_initialize_fresh_latest_schema()` builds full v2 schema (incl. FTS5) |
| `version == 1` | `_migrate_v1_to_v2()` (idempotent ALTER + CREATE VIRTUAL) |
| `version == 2` | `_validate_v2_schema()` (light table existence check) |
| `version > 2` | `KnowledgeSchemaVersionError` ("newer than this application") |
| `version < 1` | `KnowledgeSchemaVersionError` ("unsupported") |

### 1.3 v1 → v2 migration

Single `BEGIN IMMEDIATE` transaction with three idempotent statements:

```sql
ALTER TABLE knowledge_chunks ADD COLUMN char_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_chunks ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT '';
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
    chunk_id UNINDEXED, document_id UNINDEXED, library_id UNINDEXED,
    heading_text, content,
    tokenize='unicode61 remove_diacritics 2'
);
UPDATE knowledge_schema_meta SET value = 2 WHERE key = 'schema_version';
```

**Idempotency guards**: each ALTER preceded by `_existing_columns("knowledge_chunks")`
check; CREATE VIRTUAL TABLE preceded by `_table_exists("knowledge_chunks_fts")`
check. Re-running migration on already-v2 DB is a no-op that just sets the
version row.

**Rollback**: any failure → ROLLBACK; v1 data and schema intact.

### 1.4 Fresh v2 schema

`_DDL_STATEMENTS` (executed inside one BEGIN IMMEDIATE on fresh DB):

- All v1 tables unchanged (knowledge_libraries / knowledge_documents /
  knowledge_ingestion_jobs / session_knowledge_libraries).
- `knowledge_chunks` adds two columns: `char_count INTEGER NOT NULL DEFAULT 0`
  and `content_sha256 TEXT NOT NULL DEFAULT ''`. R1 columns (content_hash,
  token_count) retained for backward compat.
- `knowledge_chunks_fts` virtual table created (FTS5 + unicode61 +
  remove_diacritics 2).
- 7 existing indexes preserved.

### 1.5 CHECK constraints decision

The original R3-B design called for CHECK constraints:
- `char_count > 0`
- `length(content_sha256) = 64`
- `length(content_hash) = 64`

These are **NOT** added at the schema level. Rationale:

1. SQLite `ALTER TABLE ADD COLUMN` cannot add CHECK constraints. A migrated
   v1→v2 DB therefore cannot have these constraints regardless of intent.
2. R1 `Chunk` DTO uses arbitrary `content_hash` strings (not necessarily
   sha256 hex); enforcing length=64 at schema level would break R1 inserts.
3. The R3-B2 chunk store is the strict enforcement layer — it validates
   `char_count > 0`, `length(content_sha256) = 64`, deterministic chunk ID
   derivation, and exact SHA match before issuing INSERT.

This keeps v2 fresh DB observationally equivalent to a migrated v1→v2 DB.
Strictness is centralised in the R3-B2 application layer.

---

## 2. FTS5 virtual table

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

**Frozen tokenizer**: `unicode61 remove_diacritics 2` (per P2-R3-0 capability
probe; verified at Python 3.12.13 / SQLite 3.53.2 / `ENABLE_FTS5`).

**Source of truth**: `knowledge_chunks` table is the durable source;
`knowledge_chunks_fts` is a derived search index. R3-B2 owns the explicit
transaction sync (`BEGIN ... DELETE FTS / DELETE chunks / INSERT chunks /
INSERT FTS ... COMMIT`).

**FTS5 shadow tables**: SQLite creates `knowledge_chunks_fts_config` /
`_content` / `_data` / `_docsize` / `_idx` automatically. Tests verify all
five shadow tables exist after both fresh-init and v1→v2 migration.

---

## 3. `insert_chunk` integration

R1 stub `Chunk` DTO carries `content_hash` but not `char_count` / `content_sha256`.
v2 schema adds the two new NOT NULL columns. To keep R1 tests passing without
modifying R1 DTO semantics, `KnowledgeStore.insert_chunk()` now derives:

```python
char_count = len(chunk.content)
content_sha256 = (
    chunk.content_hash
    if len(chunk.content_hash) == 64
       and all(c in "0123456789abcdef" for c in chunk.content_hash)
    else hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
)
```

R3-B2 chunk store will pass explicit `content_sha256` and `char_count`
calculated from `KnowledgeChunk` (R3-A DTO), so the legacy R1 path is not
used in production R3 indexing. R1 tests still pass because they use the
fallback derivation.

---

## 4. Library-id strategy

**Decision**: denormalised — `knowledge_chunks.library_id` column is
populated from `Document.library_id` (per directive §8 Option A).

Rationale:
- R4 library allowlist filtering is a single-table WHERE clause (cheap).
- No JOIN with documents needed for FTS query.
- The R3-B2 chunk store resolves `library_id` from the document at insert
  time; callers cannot inject arbitrary library IDs.

This column already exists in v1 schema (R1 stub); R3-B1 retains it
unchanged.

---

## 5. heading_path serialization

**Decision**: JSON array string.

```json
["Chapter 1","Treatment Planning","Dose"]
```

The `heading_path` column is `TEXT NOT NULL DEFAULT ''` (unchanged from v1).
R3-B2 chunk store serialises `tuple[str, ...]` via `json.dumps(list(...),
ensure_ascii=False)` and reads back via `json.loads(...)` → `tuple(...)`.
Empty tuple serialises to `"[]"`.

This preserves heading order, allows headings to contain `>`, and is stable
across OS / locale.

---

## 6. Scope confirmation

### R3-B1 modifies

- `src/pi_agent_core_py/web/knowledge/store.py`:
  - `KNOWLEDGE_SCHEMA_VERSION: 1 → 2`
  - `_DDL_STATEMENTS`: v2 fresh schema (chunks + FTS5)
  - `_V1_TO_V2_MIGRATION_STATEMENTS` + `_V1_TO_V2_FTS_DDL` constants
  - `_initialize_schema()`: branch on v1→v2 migration
  - `_initialize_fresh_latest_schema()` (renamed from `_initialize_fresh_v1_schema`)
  - `_migrate_v1_to_v2()` (new)
  - `_validate_v2_schema()` (new; supersedes `_validate_v1_schema`)
  - `_existing_columns(table)` (new helper)
  - `_table_exists(table)` (new helper)
  - `insert_chunk()`: derive char_count + content_sha256 to fill v2 columns
  - `import hashlib` added

### R3-B1 adds

- `tests/test_r3_b_schema_migration.py` (18 tests across 4 suites)
- `docs/design/p2-r3-b-schema-migration-contract.md` (this file)

### R3-B1 does NOT modify

- `chunker.py` (R3-A frozen)
- `pdf_parser.py` / `pypdf_parser.py` / `pdf_quality.py` /
  `canonical_markdown.py` / `markdown_persistence.py` (R2 frozen)
- `ingestion_store.py` / `ingestion_orchestrator.py` /
  `ingestion_worker.py` / `upload_service.py` (R2 frozen)
- `models.py` DocumentStatus / transitions
- `service.py` / `api.py` delete paths (R3-B2 may need minimal change for
  FTS cleanup, but not in B1)
- `web/state.py` / `web/app.py` lifespan
- `pyproject.toml` / `uv.lock` / `frontend/**`

---

## 7. Test coverage (18 tests)

| Suite | Tests | Coverage |
|---|---|---|
| `TestFreshDB` | 6 | fresh-init / tables / R3-A columns / FTS5 + shadow tables / tokenizer / row accepts R3-A columns |
| `TestV1Migration` | 7 | v1→v2 / libraries / documents / jobs / bindings / legacy chunks preserved / FTS5 created |
| `TestIdempotency` | 3 | v2 reopen / migration reopen / future-version rejected |
| `TestFTS5Capability` | 2 | MATCH works post-migration / bm25 returns negative score |

All 18 PASS in 4.44s.

---

## 8. Exit gate

| # | Condition | Status |
|---|---|---|
| 1 | `KNOWLEDGE_SCHEMA_VERSION` bumped to 2 | ✅ §1.1 |
| 2 | Fresh DB builds full v2 schema | ✅ TestFreshDB |
| 3 | v1 DB migrates to v2 | ✅ TestV1Migration |
| 4 | Migration preserves libraries | ✅ |
| 5 | Migration preserves documents | ✅ |
| 6 | Migration preserves jobs | ✅ |
| 7 | Migration preserves bindings | ✅ |
| 8 | Migration preserves legacy chunks (char_count=0 OK) | ✅ |
| 9 | FTS5 virtual table created on fresh | ✅ |
| 10 | FTS5 virtual table created on migration | ✅ |
| 11 | FTS5 tokenizer frozen (unicode61 remove_diacritics 2) | ✅ |
| 12 | FTS5 + bm25 work post-migration | ✅ TestFTS5Capability |
| 13 | Reopen v2 DB is no-op | ✅ TestIdempotency |
| 14 | Future version (> v2) rejected | ✅ |
| 15 | R1 `insert_chunk` continues to PASS | ✅ (derives char_count + content_sha256) |
| 16 | No R2 frozen module modifications | ✅ §6 |
| 17 | No `chunker.py` modifications | ✅ §6 |
| 18 | No new dependencies | ✅ (stdlib `hashlib` already used by chunker) |
| 19 | Ruff PASS | ✅ |
| 20 | Full Backend 0 failed | ✅ (3175 passed / 3 skipped / 14 deselected) |

**20/20 PASS** ✅

---

## 9. Stage gate

```
P2-R3-B1 Schema Migration v1 → v2
✅ COMPLETE / FROZEN @ <this commit>

P2-R3-B2 Chunk/FTS Store
⛔ APPROVED TO START (independent authorisation required)

P2-R3-B3 Validation Freeze
⛔ BLOCKED BY R3-B2

P2-R3-C Indexing Runtime
⛔ BLOCKED BY R3-B

P2-R3-D / R3-E
⛔ BLOCKED BY R3-C
```

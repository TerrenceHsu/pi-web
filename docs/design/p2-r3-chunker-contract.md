# P2-R3 Chunker Contract — Heading-aware Chunker

> **Phase**: P2-R3-A Chunk Contract + Chunker (algorithm only)
> **Baseline**: P2-R2 PDF → Canonical Markdown Pipeline ✅ FROZEN @ `5c0cb8f`
> **R3-A commit**: this commit (hash assigned at commit time)
> **Scope**: chunk DTO + heading/page parser + deterministic chunk algorithm + unit tests.
> **Not in scope**: schema migration, FTS5, IndexStore, IndexingOrchestrator, IndexWorker, lifespan wiring, E2E.

---

## 1. Position in pipeline

```
P2-R2 (frozen)                          P2-R3-A (this commit)
Canonical Markdown  ─────────────►  HeadingAwareChunker  ──►  list[KnowledgeChunk]
document.md (UTF-8)                     (pure function)
```

R3-A is the **only** algorithm module that turns Canonical Markdown into chunks.
It is pure: no I/O, no DB, no network, no LLM, no Markdown AST library.

---

## 2. Public API

### 2.1 `KnowledgeChunk` dataclass

```python
@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    id: str                       # deterministic sha256-based (see §6)
    document_id: str
    ordinal: int                  # 0-based
    heading_path: tuple[str, ...] # e.g. ("Introduction", "Background")
    content: str
    page_start: int               # 1-based, inclusive
    page_end: int                 # 1-based, inclusive
    char_count: int               # len(content)
    content_sha256: str           # sha256(content.encode("utf-8")).hexdigest()
```

### 2.2 `HeadingAwareChunker`

```python
class HeadingAwareChunker:
    def chunk(
        self,
        *,
        document_id: str,
        markdown: str,
        expected_page_count: int,
    ) -> list[KnowledgeChunk]: ...
```

Stateless; safe to share across coroutines / threads.

### 2.3 Helper functions

```python
def compute_chunk_id(document_id: str, ordinal: int, content_sha256: str) -> str: ...
def compute_content_sha256(content: str) -> str: ...
```

---

## 3. Chunk size constants (frozen @ R3-A)

| Constant | Value | Semantics |
|---|---|---|
| `CHUNK_TARGET_CHARS` | 1200 | Soft target. Packer flushes around this size at structural boundaries. |
| `CHUNK_MAX_CHARS` | 1800 | Hard upper bound. Any chunk that would exceed this is force-split. |
| `CHUNK_OVERLAP_CHARS` | 160 | Overlap carried between force-splits of the same oversized section only. |

**Units**: Python Unicode code points (`len(text)`). NOT UTF-8 bytes / tokenizer tokens / model tokens.

---

## 4. Frontmatter contract

### 4.1 Required structure

Canonical Markdown starts with:

```
---
schema "pi-agent-canonical-markdown/v1"
document_id "<id>"
page_count <N>
---
```

R2-B serialises scalar values via `json.dumps(value, ensure_ascii=False)`, so
`document_id` is a JSON-quoted string; `page_count` is an unquoted integer.

### 4.2 Verification rules

The chunker verifies:

1. Document starts with `---` followed by newline.
2. Closing `---` exists on its own line.
3. `document_id` field present and matches the caller-supplied id.
4. `page_count` field present and matches `expected_page_count`.

### 4.3 Frontmatter is NOT chunk content

Frontmatter text is **never** included in any chunk `content`. It is parsed
for verification only.

### 4.4 Failure handling

| Condition | `safe_error_code` |
|---|---|
| Missing opening / closing `---` | `canonical_markdown_invalid` |
| Missing `document_id` field | `canonical_markdown_invalid` |
| `document_id` mismatch | `canonical_markdown_invalid` |
| Missing `page_count` field | `canonical_markdown_invalid` |
| `page_count` mismatch | `canonical_markdown_invalid` |
| Body content before first page marker | `canonical_markdown_invalid` |
| Zero non-whitespace body content | `chunking_empty` |

---

## 5. Page marker contract

### 5.1 Format

Unescaped system page marker, on its own line:

```
<!-- page:N -->
```

Where `N` is 1-based and strictly increasing `1, 2, ..., expected_page_count`.

### 5.2 Source-text collisions

R2-B Builder escapes any source-text line matching `^\s*<!--\s*page:\d+\s*-->\s*$`
by prepending `\` (`\<!-- page:N -->`). The chunker's page-marker regex only
matches the unescaped form, so escaped markers are preserved as body text.

### 5.3 Markers are NOT chunk content

A page-marker line is **never** included in chunk `content`. It is used to
segment the body into pages only.

### 5.4 Page range semantics

| Field | Definition |
|---|---|
| `page_start` | Smallest page number whose body contributed any text to the chunk. |
| `page_end` | Largest page number whose body contributed any text to the chunk. |

If a chunk's body came entirely from page 3, then `page_start == page_end == 3`.

### 5.5 Failure handling

| Condition | `safe_error_code` |
|---|---|
| No page markers in body | `canonical_markdown_invalid` |
| Page marker out of range (`< 1` or `> expected_page_count`) | `canonical_markdown_invalid` |
| Duplicate page marker | `canonical_markdown_invalid` |
| Missing page marker for any `n` in `1..expected_page_count` | `canonical_markdown_invalid` |

---

## 6. Chunk ID algorithm (deterministic)

```python
def compute_chunk_id(document_id, ordinal, content_sha256) -> str:
    payload = f"{document_id}\0{ordinal}\0{content_sha256}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

Properties:

- 64 lowercase hex characters.
- Same `(document_id, ordinal, content_sha256)` → same ID.
- Different `document_id` → different ID (no cross-document collision).
- Different `ordinal` → different ID.
- Different `content_sha256` → different ID.
- Re-indexing the same Document yields identical chunk IDs.

Forbidden ID sources:

- `uuid4()`
- `secrets.token_hex()`
- timestamp-derived IDs
- random IDs

(R1 `knowledge_chunks.id` previously used `secrets.token_hex(8)`; R3 replaces
this with the deterministic algorithm above at the application layer. The DB
column type/length is unchanged.)

---

## 7. Content SHA-256

```python
content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
```

- 64 lowercase hex characters.
- Input is the chunk's `content` field encoded as UTF-8.
- Stable across OS / locale / time zone.

---

## 8. Heading parsing

### 8.1 Supported syntax

ATX headings `#` .. `######`. Leading `#` count (1..6) defines the level.

```
# H1
## H2
...
###### H6
```

### 8.2 Heading stack

The chunker maintains a stack as it walks the body:

- `# A` → stack becomes `("A",)`.
- `## B` under `# A` → stack becomes `("A", "B")`.
- `### C` under `# A` / `## B` → stack becomes `("A", "B", "C")`.
- `## D` under `# A` / `## B` → stack pops `B`, becomes `("A", "D")` (sibling).
- `# E` → stack becomes `("E",)` (resets to top level).

### 8.3 Heading_path per chunk

Each chunk's `heading_path` is the heading stack **at the moment the last
block in that chunk was emitted**. This is intentional: heading_path is
chunk-level metadata, not block-level. FTS may use it for term weighting.

### 8.4 Heading text is NOT chunk content

A heading line itself is **not** added to chunk `content`. The heading text
is captured only in `heading_path` of subsequent blocks. This avoids
duplicating the heading in every chunk of the same section while preserving
the structural context.

### 8.5 Code-fence exclusion

Lines inside a fenced code block (```` ``` ```` or `~~~`) are **not** parsed
as headings, even if they look like `# foo`. The fence state is tracked
explicitly.

### 8.6 Hash-after-text is not a heading

A line like `This is text. # not heading` is **not** a heading (no leading
`#` + space).

### 8.7 Frontmatter hashes are not headings

Frontmatter is consumed before heading parsing begins.

---

## 9. Block boundaries

The packer prefers structural boundaries in this order:

1. **Heading-section change** — new heading_path differs in any element
   other than appending a deeper level.
2. **Paragraph boundary** — blank line.
3. **Sentence boundary** — `.` / `。` / `!` / `?` followed by whitespace
   (only used during force-split of an oversized block).
4. **Line boundary** — `\n` (only used during force-split).
5. **Hard character cut** — `CHUNK_MAX_CHARS` chars.

The packer accumulates atomic blocks (paragraphs / list items / code blocks)
into the current chunk and flushes when:

- A heading-section change occurs, OR
- The next block would push `current_size` past `CHUNK_TARGET_CHARS`.

### 9.1 Force-split

If a single atomic block exceeds `CHUNK_MAX_CHARS`, the packer:

1. Flushes whatever is currently accumulated (preserving its heading context).
2. Splits the oversized block by paragraph → line → sentence → hard char.
3. Carries `CHUNK_OVERLAP_CHARS` between adjacent pieces of the same block
   only. **No overlap across different heading sections.**

### 9.2 Short chunks retained

Short trailing blocks are merged into the previous chunk when safe, or
retained as their own chunk when merging would exceed `CHUNK_TARGET_CHARS`.
Short chunks are **never** dropped — content preservation is invariant.

---

## 10. Whitespace / content fidelity

| Operation | Allowed |
|---|---|
| Strip outer whitespace from chunk content | ✅ |
| Strip outer whitespace from each block before joining | ✅ |
| Join blocks with `\n\n` (paragraph separator) | ✅ |
| CRLF / CR normalised to `\n` during page splitting | ✅ |

| Operation | Forbidden |
|---|---|
| Unicode normalisation (NFKC etc.) | ❌ |
| Markdown rendering | ❌ |
| Deleting content | ❌ |
| Modifying code block contents | ❌ |
| Modifying text before the first page marker | ❌ |
| LLM summarisation | ❌ |

---

## 11. Error vocabulary

| Error class | `safe_error_code` | When |
|---|---|---|
| `CanonicalMarkdownInvalid` | `canonical_markdown_invalid` | Frontmatter / page marker / structure violation. |
| `ChunkingEmpty` | `chunking_empty` | Body produced zero non-whitespace content. |
| `ChunkingFailed` | `chunking_failed` | Unexpected / generic. |

All error messages are safe (no absolute paths / no Markdown body / no
SQL / no traceback / no secrets). Caller wraps with `raise ... from None`
when chaining from low-level exceptions.

---

## 12. Determinism invariants

```
same document_id
+ same markdown (byte-identical)
+ same expected_page_count

→ same chunk count
→ same chunk IDs (deterministic sha256)
→ same content SHA per chunk
→ same page_start / page_end per chunk
→ same heading_path per chunk
```

Verified by `tests/test_chunker.py::TestDeterminism::*`.

---

## 13. Non-goals (R3-A scope boundary)

R3-A does **not** implement:

- ❌ Schema migration (R3-B)
- ❌ `knowledge_chunks_fts` virtual table (R3-B)
- ❌ FTS5 search primitive (R3-B)
- ❌ IndexStore / IndexingOrchestrator / IndexWorker (R3-C / R3-D)
- ❌ App lifespan wiring (R3-D)
- ❌ Delete guards for `chunking` / `indexing` (R3-D)
- ❌ File I/O (orchestrator reads `document.md`)
- ❌ SHA-256 integrity verification of `document.md` vs DB (orchestrator)
- ❌ E2E upload → ready (R3-E)

---

## 14. Test coverage

`tests/test_chunker.py` — 44 tests covering:

| Suite | Tests |
|---|---|
| `TestMinimal` | 3 (single-page, empty, whitespace) |
| `TestFrontmatter` | 7 (missing / mismatched / BOM) |
| `TestPageMarkers` | 6 (multi-page / range / duplicate / missing / before-first) |
| `TestHeadings` | 7 (H1 / stack / sibling / max-level / code fence / hash-after-text) |
| `TestChunkSize` | 3 (short retained / oversized force-split / no-exceed-max) |
| `TestDeterminism` | 8 (byte-identical / ordinal / chunk_id dependencies / format / hashlib match) |
| `TestPageRange` | 2 (single-page / multi-page) |
| `TestUnicode` | 3 (Chinese / emoji / CRLF) |
| `TestCodeFence` | 2 (``` and ~~~) |
| `TestContentPreservation` | 2 (no paragraph lost / escaped marker) |
| `TestComputeHelpers` | 2 (sha256 format / negative ordinal) |

Total: **44 passed in 1.75s**.

---

## 15. Exit gate (R3-A)

| # | Condition | Status |
|---|---|---|
| 1 | `chunker.py` exists with `KnowledgeChunk` DTO | ✅ §2.1 |
| 2 | `HeadingAwareChunker.chunk()` pure function | ✅ §2.2 |
| 3 | Deterministic chunk_id (`sha256` over NUL-joined tuple) | ✅ §6 |
| 4 | Deterministic `content_sha256` | ✅ §7 |
| 5 | Frontmatter verification (document_id + page_count) | ✅ §4 |
| 6 | Page marker parsing (1-based, strict, no body before first) | ✅ §5 |
| 7 | Heading parsing (H1-H6, stack, code-fence exclusion) | ✅ §8 |
| 8 | Block boundaries (heading / paragraph / sentence / line / char) | ✅ §9 |
| 9 | Force-split oversized blocks with overlap inside same section | ✅ §9.1 |
| 10 | Short chunks retained (no content loss) | ✅ §9.2 |
| 11 | Whitespace fidelity (no NFKC, no Markdown render) | ✅ §10 |
| 12 | Error vocabulary (3 classes, safe messages) | ✅ §11 |
| 13 | Unicode / emoji / CRLF / Chinese handled | ✅ TestUnicode |
| 14 | Page markers never in chunk content | ✅ TestPageMarkers |
| 15 | Frontmatter never in chunk content | ✅ §4.3 |
| 16 | Code-fence `#` not parsed as heading | ✅ TestHeadings |
| 17 | 44/44 tests PASS | ✅ §14 |
| 18 | No I/O / DB / network / LLM / Markdown AST dep | ✅ §1 |
| 19 | No modification to R2 frozen modules | ✅ (new file only) |
| 20 | Production diff = 1 new file only | ✅ |

**20/20 PASS** ✅

---

## 16. Stage gate

```
P2-R3-A Chunk Contract + Chunker
✅ COMPLETE / FROZEN @ <this commit>

P2-R3-B Schema + SQLite FTS5
⛔ APPROVED TO START (independent authorisation required)

P2-R3-C Indexing Runtime
⛔ BLOCKED BY R3-B

P2-R3-D Bounded Index Worker
⛔ BLOCKED BY R3-C

P2-R3-E Integration Freeze
⛔ BLOCKED BY R3-D

P2-R4 Session-scoped search_knowledge + Citation
⛔ BLOCKED BY COMPLETE P2-R3
```

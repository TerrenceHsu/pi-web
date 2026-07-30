# P2-R0 — RAG Contract（主设计冻结文档）

> **阶段**：P2-R0 RAG Contract Audit（docs-only milestone）
> **基线 commit**：`ee62732` — docs: freeze P1-E multi-provider switching
> **日期**：2026-07-27
> **范围**：冻结本地知识库 RAG 子系统的 7 项核心契约——数据模型 / 目录布局 / PDF 边界 / Chunk 格式 / Tool 接口 / Session ACL / 同步策略。本文件是 P2-R1~R6 实施的唯一设计依据。
> **配套文档**：
> - [p2-r0-legacy-limit-audit.md](p2-r0-legacy-limit-audit.md) — 旧边界解除记录
> - [p2-r0-decisions-log.md](p2-r0-decisions-log.md) — 决策冻结表（含推迟项）
> - `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md` — 验证归档

---

## 1. 范围与 docs-only 边界

### 1.1 P2-R0 做什么

仅做**文档审计与契约冻结**，不写生产代码：

- 解除仓库内 7 处旧 deferred / out-of-scope 标记（详见 legacy-limit-audit.md §1）
- 冻结 8 条用户给定决策 + 5 条 R0 新权衡（详见 decisions-log.md §1 / §2）
- 在 ROADMAP / TODO 加入 P2-R 系列占位（R0~R6）
- 输出 4 份 docs + 1 个 commit（仅 docs/）

### 1.2 P2-R0 不做什么

- 不写 Python 代码、不动 `src/pi_agent_core_py/`
- 不安装 marker / 不引入新依赖
- 不创建 `data/knowledge/` 目录
- 不动 git tag / branch
- 不 merge / push（仍需独立授权）

### 1.3 P2-R 全系列占位

| Milestone | 状态 | 依赖 | 产出 | 验证 |
|---|---|---|---|---|
| **R0 Contract Audit** | 🟡 IN PROGRESS | — | 4 docs + 7 处旧标记解除 | §9 验证 7 条 checklist |
| **R1 Library Foundation** | ⛔ | R0 | 5 表 DDL + migration + KnowledgeFileStore + Library/Document/Binding metadata Store/Service + Library CRUD REST API + Session Binding REST API + restart 恢复 | 表存在 / migration 幂等 / Session A/B 隔离 / 删除补偿 / 路径安全 / 0 PDF 依赖 |
| **R2 Ingestion Pipeline** | ⛔ | R1 | marker 集成（或 pypdf fallback）+ Canonical MD writer + heading-aware chunker + Job 状态机 + 30s 阈值同步处理 + PDF upload/retry endpoint | 已知样本 chunk 数稳定 / Job 状态全路径 / 30s 超时分支 / 数字 PDF→MD smoke / 扫描 PDF→needs_ocr / marker AGPL 兼容性确认 |
| **R3 Retrieval** | ⛔ | R2 | `search_knowledge` tool + FTS5 + top_k 排序 + 引用 evidence | query 召回 / tool 不暴露 session_id（AST 校验） |
| **R4 Session Library ACL** | ⛔ | R3 | session_knowledge_libraries CRUD + 后端 allowlist 过滤 | 未授权 library 不出现 / A/B session 越权测试 |
| **R5 Web API + UI** | ⛔ | R4 | library / document / search REST + 前端管理面板 | E2E upload→ingest→search 全链路 |
| **R6 Freeze + Validation** | ⛔ | R5 | validation report + security freeze + tag `v0.0.XX-knowledge-rag` | 全 pytest + E2E + ruff + 0 Core Runtime 回归 |

> **[AMENDED 2026-07-30]** R1 范围已重划——marker 集成 / 数字 PDF→MD smoke / 扫描 PDF→needs_ocr 测试从 R1 推迟到 R2。详见 [p2-r0-amendment-1.md](p2-r0-amendment-1.md)。

---

## 2. 数据模型

### 2.1 部署模式

**独立 `knowledge.db`** + 独立 aiosqlite 连接（decision R2）。pragma 套路复用 `src/pi_agent_core_py/session_sqlite.py`：

```python
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
PRAGMA busy_timeout=5000
```

`library_id` 作为**逻辑外键**（chunks / documents / jobs / bindings 表都引用），**不**用 SQL `FOREIGN KEY` 约束——便于跨库（与 session.db）解耦。

### 2.2 DDL（5 张表 + 2 张元数据/索引表）

#### `knowledge_libraries` — 库元信息

```sql
CREATE TABLE knowledge_libraries (
    id           TEXT PRIMARY KEY,           -- lib_xxx
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',  -- active / archived
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
```

#### `knowledge_documents` — 文档元信息

```sql
CREATE TABLE knowledge_documents (
    id                TEXT PRIMARY KEY,        -- doc_xxx
    library_id        TEXT NOT NULL,           -- 逻辑 FK → knowledge_libraries.id
    source_name       TEXT NOT NULL,           -- 用户上传时的文件名（已 sanitize）
    source_sha256     TEXT NOT NULL,           -- PDF 原文 SHA-256
    source_relpath    TEXT NOT NULL,           -- documents/{doc_id}/source.pdf（相对 library 根）
    markdown_relpath  TEXT NOT NULL,           -- documents/{doc_id}/document.md
    mime_type         TEXT NOT NULL,           -- 'application/pdf'
    page_count        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,            -- uploaded / extracting / normalizing / chunking / indexing / ready / failed / needs_ocr
    parser_version    TEXT NOT NULL DEFAULT '',  -- 'marker-v1' / 'pypdf-v1'（fallback）
    error_code        TEXT NOT NULL DEFAULT '',  -- 失败时安全错误码（不含路径/正文）
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE INDEX idx_documents_library ON knowledge_documents(library_id);
CREATE INDEX idx_documents_status  ON knowledge_documents(status);
```

#### `knowledge_ingestion_jobs` — 处理任务审计（同步处理也记录）

```sql
CREATE TABLE knowledge_ingestion_jobs (
    id              TEXT PRIMARY KEY,           -- job_xxx
    document_id     TEXT NOT NULL,              -- 逻辑 FK
    stage           TEXT NOT NULL,              -- extract / normalize / chunk / index
    status          TEXT NOT NULL,              -- running / completed / failed
    attempt         INTEGER NOT NULL DEFAULT 1,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,                    -- NULL while running
    safe_error_code TEXT NOT NULL DEFAULT ''    -- 不含路径/正文/secret
);

CREATE INDEX idx_jobs_document ON knowledge_ingestion_jobs(document_id);
```

#### `knowledge_chunks` — Chunk 内容 + 元信息

```sql
CREATE TABLE knowledge_chunks (
    id            TEXT PRIMARY KEY,             -- chunk_xxx
    library_id    TEXT NOT NULL,                -- 逻辑 FK（冗余，便于按 library 检索）
    document_id   TEXT NOT NULL,                -- 逻辑 FK
    ordinal       INTEGER NOT NULL,             -- 文档内顺序（0-based）
    heading_path  TEXT NOT NULL DEFAULT '',     -- 'Chapter 1 > 1.2 Background'
    page_start    INTEGER NOT NULL,
    page_end      INTEGER NOT NULL,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,                -- SHA-256(content)，去重用
    token_count   INTEGER NOT NULL DEFAULT 0,    -- tiktoken 估算
    UNIQUE(document_id, ordinal)
);

CREATE INDEX idx_chunks_document ON knowledge_chunks(document_id);
CREATE INDEX idx_chunks_library  ON knowledge_chunks(library_id);
```

#### `session_knowledge_libraries` — Session ↔ Library 绑定

```sql
CREATE TABLE session_knowledge_libraries (
    session_id    TEXT NOT NULL,                -- 逻辑 FK → session.db sessions.id（跨库，不强制 FK）
    library_id    TEXT NOT NULL,                -- 逻辑 FK → knowledge_libraries.id
    access_mode   TEXT NOT NULL DEFAULT 'read', -- 第一版固定 'read'
    created_at    INTEGER NOT NULL,
    UNIQUE(session_id, library_id)
);

CREATE INDEX idx_bindings_session ON session_knowledge_libraries(session_id);
CREATE INDEX idx_bindings_library ON session_knowledge_libraries(library_id);
```

### 2.3 FTS5 全文索引（R3 引入，R0 冻结结构）

```sql
CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    content,
    tokenize='unicode61'    -- 中文效果一般；D4 推迟项评估是否换 jieba
);
```

第一版用 `unicode61` tokenizer（SQLite 内置，无依赖）。中文检索质量若不足，R3+ 评估改 `trigram` 或自建 jieba preprocessor（decision D4）。

### 2.4 状态机

```
uploaded → extracting → normalizing → chunking → indexing → ready
                ↓             ↓             ↓           ↓
              failed        failed        failed     failed

extracting → needs_ocr  （单页/全部文本为空且 marker 配置 force_ocr=False）
```

`needs_ocr` 是**终态**——第一版不自动重试 OCR；用户需手动处理（删文档 + 用其他工具 OCR 后重新上传）。

---

## 3. 文件系统布局

### 3.1 目录结构

```
data/
└── knowledge/
    ├── knowledge.db                                    # SQLite 主库
    ├── knowledge.db-wal                                # WAL（自动）
    ├── knowledge.db-shm                                # SHM（自动）
    └── libraries/
        └── {library_id}/                               # lib_xxx
            ├── library.json                            # 库元信息缓存（便于 rm -rf 后审计）
            └── documents/
                └── {document_id}/                      # doc_xxx
                    ├── source.pdf                      # 原文（只读）
                    ├── document.md                     # Canonical MD（R2 产出）
                    ├── manifest.json                   # page_count / parser_version / source_sha256 / chunk_count
                    └── chunks.jsonl                    # chunk 列表（备份用，主存储在 SQLite）
```

### 3.2 API 路径契约

API / 前端**永远只返回**：
- `library_id` / `document_id` / `chunk_id`
- 相对逻辑路径（如 `documents/{doc_id}/document.md`）

**禁止返回**绝对路径（`data/knowledge/libraries/...` 或 `D:\LLMTutorial\test\data\...`）。

### 3.3 KnowledgeFileStore（decision R5）

```python
class KnowledgeFileStore:
    """独立于 VirtualFileStore 的长期持久化文件存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root  # data/knowledge/

    async def write_document_atomic(
        self,
        library_id: str,
        document_id: str,
        source_bytes: bytes,
        markdown: str,
        manifest: dict,
    ) -> None:
        """临时目录 → 完整处理 → fsync → atomic rename。"""

    async def read_source(self, library_id: str, document_id: str) -> bytes: ...
    async def read_markdown(self, library_id: str, document_id: str) -> str: ...
    async def delete_document(self, library_id: str, document_id: str) -> None: ...
    async def delete_library(self, library_id: str) -> None: ...
```

**原子写入流程**：
1. 写入 `tmp/{library_id}/{document_id}/`（同分区）
2. 每个文件 `flush() + os.fsync(fd)`
3. `os.replace(tmp_dir, target_dir)`（POSIX 原子；Windows 上需先 `os.unlink(target)` 再 rename）
4. 目录句柄 fsync

**避免**：直接写目标路径（处理中断后留下"看起来成功"的半成品）。

### 3.4 路径安全

- 所有 `library_id` / `document_id` 必须匹配 `^(lib|doc|chunk|job)_[a-z0-9]{12,32}$`
- `Path.resolve()` 后必须仍在 `self.root` 之下（防 `../` 穿越）
- 用户上传的 `source_name` 必须 sanitize（参考 `web/files.py` 现有实现）

---

## 4. PDF → Canonical Markdown 边界

### 4.1 Parser 选型（decision R1）

**主 parser**：`marker`（datalab.so/marker，AGPL-3.0）
**Fallback parser**（R1 集成时若 marker 不可用）：`pypdf`（BSD）

R0 仅冻结选型，R1 集成时必须先解决 marker AGPL 与项目 MIT 的兼容性（decision D6）——若不兼容，降级到 pypdf + heading 正则。

### 4.2 marker 配置（数字 PDF only 边界）

```python
from marker.converters.pdf import PdfConverter

converter = PdfConverter(
    # 关键：不主动 OCR——保持"数字 PDF only"边界
    config={
        "force_ocr": False,
        "strip_existing_ocr": False,
        "format_lines": False,         # 不二次排版
        "show_toolbar": False,
    }
)
result = converter(source.pdf)
markdown_text = result.markdown
```

**needs_ocr 判定**（pipeline 层，非 marker 内部）：
1. marker 处理后，按 `<!-- page:N -->` marker 拆分 Markdown
2. 对每页计算字符数（剥离 marker / 空白）
3. **任一页字符数 < 阈值（建议 50 字符）** → 整文档 status=needs_ocr
4. **整文档总字符数 < 阈值（建议 500 字符）** → status=needs_ocr

`needs_ocr` 不写 `document.md`，不进 chunks 表；只写 `knowledge_documents.status` + `error_code='needs_ocr_threshold'`。

### 4.3 Canonical Markdown 格式（decision F4）

```markdown
---
document_id: doc_abc123def456
library_id: lib_xyz789abc012
source_name: example.pdf
source_sha256: 4f3a...e1b9
parser_version: marker-v1
page_count: 18
generated_at: 2026-07-27T10:30:00Z
---

<!-- page:1 -->

# Document Title

Page one content...

<!-- page:2 -->

## Section Title

Page two content with **bold** and `code`.

- Bullet item
- Another item

| Col1 | Col2 |
|------|------|
| A    | B    |
```

**必须保留**：
- YAML frontmatter（前 3 行 `---`）
- Page marker（`<!-- page:N -->`，N 从 1 开始，单调递增）
- Heading 层级（`#` / `##` / `###`，marker 输出原生支持）
- 原文顺序（marker 输出已是顺序，pipeline 不重排）

**禁止**：
- 删除 marker 输出的 heading / table / list（即使"看起来格式不好"）
- 在 Markdown 中插入绝对路径
- 在 Markdown 中插入其他 library / document 的引用

### 4.4 第一版 PDF 不支持

- OCR / 图像理解 / 视觉重建
- PDF 表单 / 批注 / 嵌入对象
- 复杂版面完美还原（marker 已尽力，但不保证 100%）
- 图片 / 公式 / 表格的视觉重建（marker 会用文本表示，但不是原始视觉）
- 加密 PDF（pipeline 拒绝并返回 `error_code='encrypted_pdf'`）

---

## 5. Chunk 格式与切分规则

### 5.1 Chunk 字段（decision F5）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | `chunk_xxx` |
| `library_id` | str | ✅ | 冗余存储，便于按 library 检索 |
| `document_id` | str | ✅ | |
| `ordinal` | int | ✅ | 文档内顺序，0-based |
| `heading_path` | str | ✅ | `'Chapter 1 > 1.2 Background'`，` > ` 分隔 |
| `page_start` | int | ✅ | 起始页（1-based） |
| `page_end` | int | ✅ | 结束页（chunk 跨页时 > page_start） |
| `content` | str | ✅ | chunk 正文（已 strip） |
| `content_hash` | str | ✅ | SHA-256(content) |
| `token_count` | int | ✅ | tiktoken 估算 |

### 5.2 切分算法（heading-aware，decision R4）

```
input: Canonical Markdown
  ↓
1. 按 <!-- page:N --> 拆 page 段
2. 每个 page 段内按 heading（^#{1,3}\s）拆 section
3. 维护 heading_path 栈（遇到 # 进 1 层，## 进 2 层…）
4. section ≤ max_chars（1200）→ 直接成 chunk
5. section > max_chars → 按段落（空行）拆
   - 段落 ≤ max_chars → 拼接到当前 chunk 直到接近 max_chars
   - 段落 > max_chars → 硬切，保留 overlap（150 字符）
6. 每个 chunk 标 page_start / page_end / heading_path
7. 计算 content_hash + token_count（tiktoken）
output: list[Chunk]
```

**第一版不做**：
- 句子级切分（需 NLP）
- token 精确切分（需 tokenizer 全文扫）
- 动态 overlap（基于 heading 密度）
- 表格 / 代码块保护（marker 已用 Markdown 表示，硬切接受）

### 5.3 Chunk 不变量

- 同一 document 的 chunk ordinal 单调递增、不重不漏
- chunk.content 长度 ∈ [50, max_chars + overlap]
- chunk.content_hash 全局唯一（同内容不同 chunk 必有不同 ordinal / page）
- chunk 必须能回溯到唯一 library / document / page range

---

## 6. Tool 接口契约（search_knowledge）

### 6.1 签名（decision F7）

```python
class SearchKnowledgeTool(AgentTool):
    name = "search_knowledge"
    label = "Search Knowledge Library"
    description = "Search the current session's bound knowledge libraries for relevant evidence."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language query."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
    }
    execution_mode = "parallel"

    async def execute(self, tool_call_id: str, args: dict[str, Any]) -> ToolResult:
        ...
```

### 6.2 不变量

- 签名**不接受** `library_id` / `session_id` / `file_path` / `document_id`
- `top_k` ∈ [1, 20]，默认 5
- LLM 无法通过参数越权——library allowlist 由后端从 `session_id_getter` 求出

### 6.3 实现骨架（R3 落地）

```python
async def execute(self, tool_call_id, args):
    query = args["query"]
    top_k = args.get("top_k", 5)

    # 1. 拿到当前 session（不经参数，走 closure）
    session_id = self._session_id_getter()
    if session_id is None:
        return ToolResult(is_error=True, content=[{"type": "text", "text": "no_active_session"}])

    # 2. 求 session 的 library allowlist
    allowed_library_ids = await self._knowledge_store.list_session_libraries(session_id)
    if not allowed_library_ids:
        return ToolResult(is_error=True, content=[{"type": "text", "text": "no_bound_libraries"}])

    # 3. 在 allowlist 内检索
    chunks = await self._knowledge_store.search(
        query=query,
        library_ids=allowed_library_ids,  # 后端再次求交
        top_k=top_k,
    )

    # 4. 构造 evidence（不返回绝对路径）
    evidence = [
        {
            "chunk_id": c.id,
            "library_id": c.library_id,
            "document_id": c.document_id,
            "heading_path": c.heading_path,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "content": c.content,
            "score": c.score,
        }
        for c in chunks
    ]
    return ToolResult(content=[{"type": "text", "text": json.dumps({"evidence": evidence})}])
```

### 6.4 ToolResult evidence schema

```typescript
type Evidence = {
  chunk_id: string;        // chunk_xxx
  library_id: string;      // lib_xxx
  document_id: string;     // doc_xxx
  heading_path: string;    // 'Chapter 1 > 1.2 Background'
  page_start: number;
  page_end: number;
  content: string;         // chunk 正文
  score: number;           // BM25 / RRF 分数（第一版 FTS5 rank）
};
```

**禁止返回**：绝对路径、source_sha256、token_count、其他 session 的 chunk。

---

## 7. Session Library ACL

### 7.1 权限模型（decision F6）

```
Session → Library Allowlist（read-only）
```

**不是** `User → Role → Permission`。本项目无用户账号、无 RBAC。Session 是单进程内的请求上下文，Library 是跨 Session 共享的文档集合。

### 7.2 8 条后端不变量

| # | 不变量 | 实现位置 |
|---|---|---|
| 1 | Session A 未绑定 Library B → 任何 API / Tool / 前端构造的参数都无法读 Library B | 所有 read endpoint + search_knowledge |
| 2 | 前端过滤不是权限控制，后端必须再次校验 | 所有 list / search endpoint |
| 3 | Tool **不**接受任意 `session_id` / `library_id` / 文件路径 | Tool 签名冻结（§6.2） |
| 4 | 即使传入 `library_id`（如未来扩展），也必须与 Session allowlist 求交 | search service 层 |
| 5 | Document 下载 / Markdown 预览也必须校验 Session 是否绑定该 Library | GET /api/knowledge/libraries/{lib}/documents/{doc}/markdown |
| 6 | Session 解绑后**下一次检索立即**失去访问权 | DELETE binding 后清缓存（如有） |
| 7 | 删除 Session 只删除 Binding，不删除 Library | session.db cascade 仅到 session_knowledge_libraries（如在同一库则 FK；跨库则应用层） |
| 8 | 删除 Library 必须清理对应 documents / chunks / FTS index / Session Bindings | library delete 是 cascade 删除 |

### 7.3 关键决策：search_knowledge 不接受 library_id（最安全）

第一版 `search_knowledge(query, top_k)` 自动搜当前 Session 全部绑定 library。LLM 无法构造参数越权。若未来需要"只搜某个 library"，扩展为 `search_knowledge(query, top_k, library_id=None)`——`library_id` 传值时后端**仍**与 allowlist 求交。

### 7.4 日志不变量

- log **不记录**完整 chunk 正文（仅 chunk_id / score）
- log **不记录**绝对路径（仅 library_id / document_id）
- 错误码 `safe_error_code` 不含 secret / 路径 / 正文

---

## 8. 同步 / 异步处理策略

### 8.1 第一版策略（decision F8 + R3）

```
POST /api/knowledge/libraries/{lib}/documents
  ↓
同步处理（同 request 内）
  ↓
PDF ≤ 20 页 + 处理 ≤ 30s
  ├── 完成 → 返回 {document_id, status: "ready"}
  └── 超时 → 返回 {document_id, status: "extracting", job_id}（前端轮询）
  ↓
PDF > 20 页
  └── 直接 400 拒绝，提示用户分批
```

### 8.2 Job 表的用途

即使第一版同步处理，`knowledge_ingestion_jobs` 表仍记录每个 stage（extract / normalize / chunk / index）——失败时可审计到具体 stage。R2 引入 BackgroundTask 时直接复用此表，不改 schema。

### 8.3 BackgroundTask 推迟

R0~R1 同步处理。R2 评估是否引入 `fastapi.BackgroundTasks`（uvicorn worker 重启会丢任务的风险已记录在 legacy-limit-audit.md）。R6 不做"长期后台任务调度"（保留 `ROADMAP.md:290` 不做项）。

---

## 9. 实施顺序（批准后单次执行）

P2-R0 是 docs-only milestone，按以下顺序一次完成：

1. **写 4 份 docs**（本文件 + legacy-limit-audit + decisions-log + validation report）
2. **修改源文档**（精确行号，见 legacy-limit-audit.md §1）：
   - `ROADMAP.md` — 行 270-272（删 DEFERRED 段）/ 行 280（拆 RAG vs Long-term）/ 顶部新增 P2-R 章节
   - `TODO.md` — 行 178-179 / 行 187 / Deferred 末尾加 P2-R0 PLANNED
   - `README.md` — 行 65 / 113 / 174 / 201
   - `CHANGELOG.md` — 行 23-24
3. **跑 7 条验证 checklist**（见 `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md`）
4. **单 commit**：`docs(rag): P2-R0 contract audit — freeze schema/acl/tool/parser decisions`

---

## 10. 验证 checklist（落地证据）

详见 `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md`。7 条 checklist：

1. `grep -rn "Vector RAG\|Vector Memory" ROADMAP.md TODO.md README.md` → 仅 CHANGELOG 历史段保留
2. `grep -n "RAG / Vector Memory / Long-term Memory" ROADMAP.md TODO.md README.md` → 0 hit
3. 4 份新文档 `test -f` 全部存在
4. 主设计文档 8 节非空；decisions-log 中 8 条冻结 + 5 条推荐全部以表格行出现
5. `ROADMAP.md` 含 `## P2-R — Knowledge / RAG Subsystem` + R0~R6 七行占位
6. `grep -rn "marker\|knowledge.db\|KnowledgeFileStore\|force_ocr=False" docs/design/p2-r0-*.md` → 关键决策均有显式记录
7. legacy-limit-audit.md 中每条行号引用（如 `ROADMAP.md:270-272`）与当前文件实际内容核对一致

---

## 11. R0 完成出口

- ✅ 全部 7 条 checklist 通过
- ✅ 1 个新 commit（仅 docs/）
- ✅ P2-R1 在 ROADMAP 中标 ⚪ PLANNED，等用户授权启动
- ⛔ 不写生产代码、不引入依赖、不动 src/、不动 git tag / branch、不 merge / push

---

## 12. R1 入口条件（[AMENDED 2026-07-30]）

R1 启动前必须满足的硬条件：

1. ✅ R0 全部 7 条 checklist PASS（[validation 报告 §5](../validation/p2-r0/P2_R0_CONTRACT_AUDIT.md)）
2. ✅ R0 commit 已落地（不在 plan mode / 工作树未提交）
3. ✅ **amendment-1 已落地**（docs-only commit；详见 [p2-r0-amendment-1.md](p2-r0-amendment-1.md)）
4. ⛔ **R1 启动授权**（用户独立授权，R0 不自动启动 R1）

R1 范围（amendment 后，详见 §1.3 表 + ROADMAP §P2-R）：

- 5 张表 DDL + migration（独立 `knowledge.db`）
- `KnowledgeFileStore`（atomic write + fsync + path containment + symlink 防护）
- Library 元数据 Store / Service / CRUD API
- Document 元数据 Store / Service（**仅 metadata**，不含 PDF 解析）
- Session Library Binding Store / Service / API
- startup schema 初始化 + restart 恢复

**R1 显式不包含**（推到 R2-R5）：

- **marker 集成 / PDF parser**（推到 R2，原 R1 入口条件 "marker AGPL 兼容性确认" 同步移到 R2）
- 数字 PDF → Canonical MD smoke（推到 R2）
- 扫描 PDF → `status=needs_ocr` 终态测试（推到 R2）
- heading-aware chunker / FTS5 / `search_knowledge` tool / 前端 UI（R3-R5）

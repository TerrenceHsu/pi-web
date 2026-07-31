# P2-R0 — Amendment 2（Canonical Markdown Schema + needs_ocr Threshold Refinement）

> **阶段**：P2-R0 决策变更（docs-only amendment）
> **基线 commit（amendment 之前）**：`7db4780` — docs(rag): archive corrections to R2-A — R2-B baseline + openai specifier sync explanation
> **amendment 日期**：2026-07-31
> **范围**：按 P2-R0 decisions-log §4 决策变更协议，在 P2-R2-B 启动前精化 P2-R0 §4.2（needs_ocr 阈值）+ §4.3（Canonical Markdown frontmatter 字段集）+ F4 frontmatter 字段冻结。R2-B 编码开始前消除合同与启动指令的冲突。
> **触发**：用户 P2-R2-B 启动指令 §11（needs_ocr 判定）+ §15（Canonical Markdown frontmatter 格式）+ §17（frontmatter 字段校验）与 P2-R0 §4.2（50/500 阈值）+ §4.3（frontmatter 字段示例）+ decisions-log §1 F4（frontmatter 字段冻结）在多处直接冲突——用户选择走 amendment 流程解决（而非隐式违反冻结）。

---

## 1. 变更背景

### 1.1 冲突事实

P2-R0 已冻结以下内容（amendment 前）：

- `p2-r0-rag-contract.md` §4.2 needs_ocr 判定：建议每页 < 50 字符或整文档 < 500 字符 → `status=needs_ocr`
- `p2-r0-rag-contract.md` §4.3 frontmatter 字段示例：`document_id / library_id / source_name / source_sha256 / parser_version: marker-v1 / page_count / generated_at`
- `p2-r0-decisions-log.md` §1 F4：frontmatter 字段 = `document_id / library_id / source_name / source_sha256 / parser_version / page_count`

用户 P2-R2-B 启动指令明确给出**不同**的精化要求：

- **§11 needs_ocr 判定**：`page_count == 0 → invalid_extraction_result`；`total_non_whitespace_chars == 0 → needs_ocr`；`total_non_whitespace_chars > 0 → usable`。理由："digital PDF 可能本身只有少量文本；不应将短文档误判为扫描件；第一版 needs_ocr 只表示'完全没有可提取文本'；低质量文档仍可生成 Markdown，并在后续检索阶段暴露 warning。"
- **§15 frontmatter 字段**：`schema: "pi-agent-canonical-markdown/v1" / document_id / source_filename / source_sha256 / parser_id: "pypdf" / parser_version: "6.14.2" / page_count / title（optional）`
- **§16 禁止非确定性字段**：默认不写 `generated_at / created_at / machine / environment / temp path`
- **§17 字段校验**：`document_id` 符合 R1 后端 ID 格式；`source_filename` 只接受 basename；`parser_id` 来自 PdfExtractionResult；可选 `title` 来自安全 PdfMetadata

### 1.2 用户决策

用户通过 AskUserQuestion 选择"走 P2-R0 amendment 流程"——按 P2-R0 decisions-log §4 协议合规修订 Canonical Markdown schema 与 needs_ocr 阈值冻结，**不**通过隐式违反 P2-R0 §4 的方式直接实施 R2-B。

---

## 2. 变更内容

### 2.1 needs_ocr 判定精化（contract §4.2 修订）

**原 §4.2（amendment 前）**：

```
任一页字符数 < 阈值（建议 50 字符） → 整文档 status=needs_ocr
整文档总字符数 < 阈值（建议 500 字符） → status=needs_ocr
```

**新 §4.2（amendment 后）**：

```
page_count == 0                                  → invalid_extraction_result (error)
total_non_whitespace_chars == 0                  → needs_ocr
total_non_whitespace_chars > 0                   → usable
                                                  （低密度文本作为 warning，不改变 usable 决策）
```

**变更理由**：

1. digital PDF 可能本身只有少量文本（短文档、参考文献、单页表格）——50/500 阈值会误判这些为扫描件
2. 第一版 `needs_ocr` 应只表示"完全没有可提取文本"——这是确定的、可测试的判定
3. 低文本密度（短文档、稀疏文本、多空白页）作为 **warning** 不作为 needs_ocr 触发——保留 Markdown 产出，后续检索阶段暴露问题
4. `page_count == 0` 不是 needs_ocr——是 invalid extraction result（pypdf 解析成功但 0 页属于异常）

**不变的边界**：

- `needs_ocr` 仍为**终态**——不自动 OCR，需用户手动处理
- `needs_ocr` 不写 `document.md`，不进 chunks 表
- `needs_ocr` 不删除 source.pdf
- 加密 PDF 仍走 `failed` 状态（错误码 `encrypted_pdf`），不走 needs_ocr

**warning 阈值**（不改变 usable 决策；具体常量在 R2-B 实现中冻结）：

- `LOW_NON_EMPTY_PAGE_RATIO`（建议 0.20）：non_empty_page_count / page_count < 0.20 → warning `low_non_empty_page_ratio`
- `LOW_AVERAGE_NON_WS_CHARS`（建议 20）：total_non_whitespace_chars / non_empty_page_count < 20 → warning `low_average_non_ws_chars`
- `HIGH_REPLACEMENT_CHAR_RATIO`（建议 0.05）：U+FFFD count / total_chars > 0.05 → warning `replacement_character_noise`
- 多空白页：empty_page_count > 0 且 page_count > 1 → warning `many_empty_pages`

### 2.2 Canonical Markdown Frontmatter Schema（contract §4.3 + F4 修订）

**原 §4.3 / F4（amendment 前）**：

```yaml
---
document_id: doc_abc123def456
library_id: lib_xyz789abc012
source_name: example.pdf
source_sha256: 4f3a...e1b9
parser_version: marker-v1
page_count: 18
generated_at: 2026-07-27T10:30:00Z
---
```

**新 §4.3 / F4（amendment 后）**：

```yaml
---
schema: "pi-agent-canonical-markdown/v1"
document_id: "doc_abc123def456"
source_filename: "example.pdf"
source_sha256: "4f3a...e1b9"
parser_id: "pypdf"
parser_version: "6.14.2"
page_count: 18
title: "Optional PdfMetadata Title"
---
```

**字段集变更**：

| 字段 | amendment 前 | amendment 后 | 变更类型 |
|---|---|---|---|
| `schema` | 无 | `"pi-agent-canonical-markdown/v1"`（固定） | **新增**——schema versioning |
| `document_id` | str（无引号） | JSON-quoted str | **保留**，序列化方式改 |
| `library_id` | str（无引号） | **删除** | **删除**——library_id 是 query-time 上下文，不应固化进 evidence artifact（per 启动指令 §17 "默认不应将 library_id 写入用户可见 Markdown"） |
| `source_name` | str（无引号） | `source_filename` JSON-quoted str | **重命名**——`source_filename` 更准确反映"用户上传时的 basename"语义；DB 列名保持 `source_name`（数据库列名与 frontmatter 字段名解耦） |
| `source_sha256` | str（无引号） | JSON-quoted str（64 lowercase hex） | **保留**，序列化方式改 |
| `parser_version` | `marker-v1`（合并 ID + 版本） | 拆为 `parser_id` + `parser_version` 两个字段 | **拆分**——R2-A PdfExtractionResult 已有 `parser_id` + `parser_version` 分离字段，frontmatter 直接反映 |
| `parser_id` | 无 | `"pypdf"` JSON-quoted str | **新增**——parser 稳定 ID |
| `page_count` | int | int | **保留** |
| `generated_at` | UTC ISO 8601 timestamp | **删除**（默认） | **删除默认**——确定性要求禁止 Builder 内部生成时间戳；如调用者显式传入 UTC ISO 8601 字符串则写入（默认不传 = 不写） |
| `title` | 无 | optional JSON-quoted str | **新增**——来自 PdfMetadata.title 的安全 metadata，可选 |

**字段顺序固定**（确定性 + 注入防护）：

```
schema
document_id
source_filename
source_sha256
parser_id
parser_version
page_count
title（可选；为 None 时省略）
generated_at（可选；为 None 时省略）
```

**字符串序列化方式**：

- 所有字符串标量使用 `json.dumps(value, ensure_ascii=False)` 序列化
- JSON 双引号字符串是合法 YAML 标量——可避免：
  - 冒号注入
  - 换行注入
  - `---` 注入
  - 引号破坏
  - YAML tag 注入
- 不依赖 PyYAML
- 不引入 YAML dependency
- int 字段直接 `str(int_value)`
- 文件以单个 `\n` 结束

### 2.3 不变的已冻结决策

下列 P2-R0 冻结**保持不变**：

- F1 数据模型 5 张表结构（§2.2 DDL 字段不变；DB 列 `source_name` 不改名）
- F2 文件系统布局 `data/knowledge/libraries/{library_id}/documents/{document_id}/{source.pdf, document.md, manifest.json}`
- F3 PDF 边界（仅数字 PDF；扫描 PDF 进 needs_ocr；不支持 OCR / 表单 / 批注 / 嵌入对象）
- **F4 Canonical Markdown 格式**——page marker + heading 层级 + 原文顺序 不变；frontmatter 字段集按本 amendment §2.2 精化
- F5 Chunk 格式字段
- F6 Session Library ACL（Session → Library Allowlist，非 User RBAC）
- F7 Tool 接口（`search_knowledge(query, top_k)` 不接受 library_id/session_id/path）
- F8 同步处理策略（第一版无后台任务调度）
- R1 pypdf parser 选型（amendment-1 + R2-0 已从 marker 切到 pypdf；不变）
- R2 决策：独立 knowledge.db
- R3 决策：30s 同步阈值 + ≤ 20 页限制
- R4 决策：Chunk max_chars=1200 / overlap=150
- R5 决策：独立 KnowledgeFileStore（不复用 VirtualFileStore）

**只变更**：Canonical Markdown frontmatter 字段集 + needs_ocr 阈值判定。

### 2.4 page marker 格式（明确不变）

`<!-- page:N -->`（N 从 1 开始，单调递增；marker 独占一行；marker 前后布局固定）

- 空白页也有 marker
- marker 顺序严格等于 `PdfExtractionResult.pages`
- 源文本中匹配 `^\s*<!--\s*page:\d+\s*-->\s*$` 的行必须转义（防伪造）

此格式在 amendment 前后**保持不变**。amendment-2 仅在 §2.2 显式重申。

---

## 3. 文档同步清单

本 amendment 落地需同步修改以下文档（**仅 docs**，不动 src/tests）：

| 文档 | 章节 | 修改类型 |
|---|---|---|
| `p2-r0-rag-contract.md` | §4.2 needs_ocr 判定 | 改为 `total_non_whitespace_chars == 0 → needs_ocr`；50/500 阈值移到 warning 段 |
| `p2-r0-rag-contract.md` | §4.3 frontmatter 字段示例 | 改为新字段集（含 schema / parser_id 拆分 / source_filename 重命名 / 删 library_id / 删默认 generated_at / 加 title） |
| `p2-r0-rag-contract.md` | §4.3 字段说明 | 新增"字段顺序固定"+ "JSON-quoted str 序列化" + "不依赖 PyYAML" 说明 |
| `p2-r0-decisions-log.md` | §1 F4 行 | 标注 `[AMENDED 2026-07-31 — 见 amendment-2]`；frontmatter 字段集列更新 |
| `p2-r0-decisions-log.md` | §6 amendment 历史 | 新增 "Amendment 2（2026-07-31）— Canonical Markdown Schema + needs_ocr Threshold Refinement" 段 |
| `docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md` | §28.3 R2-B encoding pre-flight | 加 amendment-2 引用 + R2-B 编码开始前合同已是新版本说明 |
| `ROADMAP.md` | R2 行 | 状态同步（仅 R2 内部阶段进度，不改 R2 整体范围描述） |

**不改**：

- F1 / F2 / F3 / F5 / F6 / F7 / F8 行
- R1 / R2（决策号）/ R3 / R4 / R5 行
- D1-D8 推迟项
- 数据模型 / 文件系统布局 / Tool 接口 / Session ACL / Chunk 格式
- 任何生产代码 / 测试代码 / 依赖 / lockfile

---

## 4. R0 回归验证（amendment 后）

amendment 是 P2-R0 决策变更，按 decisions-log §4 第 4 条 "跑回归测试确认前置 milestone 未受影响"——重新跑 R0 7 条 checklist：

| # | Check | amendment 后状态 |
|---|---|---|
| 1 | `Vector RAG` / `Vector Memory` 残留扫描 | ✅ amendment 不改 README/ROADMAP/TODO 旧标记段，仍 0 hit |
| 2 | 合并字符串拆分验证 | ✅ 不变 |
| 3 | 4 份新文档存在 | ✅ amendment 不删原 4 份文档（amendment-2.md 是第 6 份 design 文档） |
| 4 | 决策表完整性 F1-F8 + R1-R5 | ✅ F4 加 `[AMENDED]` 标注但行不删；新增 amendment-2 引用 |
| 5 | ROADMAP P2-R 章节 + R0~R6 占位 | ✅ amendment 不删 R0~R6 占位 |
| 6 | 关键决策显式记录 | ✅ marker / knowledge.db / KnowledgeFileStore / force_ocr=False 仍出现（marker 段已由 R2-0 修正为 pypdf，本 amendment 不动） |
| 7 | legacy-limit-audit 行号引用核对（基线 ee62732） | ✅ amendment 不动 legacy-limit-audit.md |

amendment 不破坏 R0 已 PASS 的 7 条 checklist。

---

## 5. amendment 后的 R2-B 入口条件（替代 P2-R0 §4.2 + §4.3 旧表述）

R2-B 启动前必须满足：

1. ✅ R0 全部 7 条 checklist PASS（基线 commit `b32e4b4`）
2. ✅ R0 amendment-1 已落地（`f411ad7`）
3. ✅ R2-0 License Gate 已冻结（`533fe48` + `f804fc7`）
4. ✅ R2-A pypdf Parser Adapter 已冻结（`0772324` + `7db4780`）
5. ✅ **本 amendment-2 已落地**（docs-only commit）
6. ⛔ **R2-B 启动授权**（用户独立授权）

**amendment-2 解决的冲突**（R2-B 编码开始时合同已是新版本）：

- frontmatter 字段集 → 按 §2.2 新表
- needs_ocr 阈值 → 按 §2.1 新规则
- 字段序列化方式 → JSON-quoted str
- page marker 格式 → 不变（amendment-2 仅重申）

R2-B 编码无需在 "指令 vs 合同" 间选择——合同已是新版本。

---

## 6. amendment 落地出口

- ✅ 本文件（amendment-2.md）存在
- ✅ `p2-r0-rag-contract.md` §4.2 / §4.3 已同步
- ✅ `p2-r0-decisions-log.md` §1 F4 行已标注 amended + §6 amendment 历史段已加
- ✅ `docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md` §28.3 已同步
- ✅ `ROADMAP.md` R2 状态已同步
- ✅ R0 7 条 checklist 仍 PASS
- ✅ production / test / dependency / schema diff = 0
- ⛔ 不动 git tag / branch / 不 merge / 不 push

amendment 后 P2-R2-B 可按用户 P2-R2-B 指令合规启动（不再有合同冲突）。

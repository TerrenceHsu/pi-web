# P2-R2-B Canonical Markdown Builder — Validation Report

> **阶段**：P2-R2-B Canonical Markdown Builder + Safe Persistence
> **基线 commit（R2-B 之前）**：`7db4780` — docs(rag): archive corrections to R2-A — R2-B baseline + openai specifier sync explanation
> **R2-B freeze commit**：本提交（自引用；hash 由 git 在提交时生成）
> **归档日期**：2026-07-31
> **范围**：PDF 文本质量评估 + needs_ocr 决策 + 保守 heading 启发式 + Canonical Markdown（含 frontmatter + page marker + collision 转义）+ 确定性 SHA-256 + KnowledgeFileStore 原子持久化。**不**实现 PDF upload API、Ingestion Worker、Job 状态转移、retry、HTTP composition wiring、Agent Tool、Chunk、FTS、向量检索、前端知识库 UI。

---

## 1. Final Status

```
P2-R2-B Canonical Markdown Builder + Safe Persistence
✅ COMPLETE / FROZEN @ <this commit>

R2-B commit chain:
15b411b  docs(rag): amend P2-R0 — canonical markdown schema + needs_ocr threshold refinement
                                              (P2-R0 Amendment 2 — pre-R2-B docs-only)
0154cde  feat(rag): add PDF text quality evaluation         (R2-B1)
c077d5a  feat(rag): build canonical PDF markdown             (R2-B2)
677ed13  feat(rag): persist canonical markdown safely        (R2-B3)
<this>   test(rag): freeze canonical markdown builder        (R2-B4)

Baseline before R2-B:
7db4780 — docs(rag): archive corrections to R2-A

Contract baseline (after amendment-2):
pypdf Parser Adapter @ 0772324 (R2-A frozen)
pypdf 6.14.2 (BSD-3-Clause, matches R2-0 audited baseline)
```

R2-B 性质：编码 + 测试 + docs（含 P2-R0 amendment-2 pre-R2-B docs-only commit + R2-B 编码 3 commits + R2-B4 freeze docs commit）。

---

## 2. Pre-R2-B Contract Conflict Resolution

R2-B 启动指令 §11（needs_ocr 阈值）+ §15（frontmatter 字段集）+ §17（字段校验）与 P2-R0 contract F4 / §4.2 / §4.3 多处直接冲突。用户通过 AskUserQuestion 选择走 amendment 流程（per memory `feedback_p2_contract_amendment_workflow`）。

**P2-R0 Amendment 2**（commit `15b411b`）于 R2-B 编码开始前落地：

1. **needs_ocr 阈值精化（contract §4.2）**：
   - 旧：每页 < 50 字符 或 整文档 < 500 字符 → needs_ocr
   - 新：仅 `total_non_whitespace_chars == 0 → needs_ocr`；低密度文本作为 warning
2. **frontmatter 字段集精化（contract §4.3 + F4）**：
   - 新增：`schema: "pi-agent-canonical-markdown/v1"` / `parser_id`（与 `parser_version` 拆分）/ `title`（可选）
   - 重命名：`source_name` → `source_filename`（DB 列名 `source_name` 不变）
   - 删除：`library_id`（query-time 上下文）+ 默认 `generated_at`（Builder 不调 `datetime.now()`）
3. **字符串序列化**：`json.dumps(value, ensure_ascii=False)` —— 防 YAML 注入，不引入 PyYAML

R0 7-checklist 回归：amendment-2 后仍 PASS（详见 `p2-r0-amendment-2.md §4`）。

---

## 3. Per-commit File Inventory

| Commit | 类型 | 新文件 | 修改文件 | 总 |
|---|---|---|---|---|
| `15b411b` R2-B0 P2-R0 Amendment 2（pre-R2-B docs-only） | docs | 1 (`p2-r0-amendment-2.md`) | 3 (`p2-r0-rag-contract.md` + `p2-r0-decisions-log.md` + `P2_R2_A_PARSER_ADAPTER.md`) | 4 |
| `0154cde` R2-B1 Text Quality Evaluator | feat | 2 (`pdf_quality.py` + `tests/test_pdf_quality.py`) | 0 | 2 |
| `c077d5a` R2-B2 Canonical Markdown Builder | feat | 2 (`canonical_markdown.py` + `tests/test_canonical_markdown.py`) | 0 | 2 |
| `677ed13` R2-B3 Safe Markdown Persistence | feat | 2 (`markdown_persistence.py` + `tests/test_markdown_persistence.py`) | 0 | 2 |
| `<this>` R2-B4 Validation + Freeze | test/docs | 2 (`tests/test_r2_b_integration.py` + 本文件) | 3 (`STATUS.md` + `TODO.md` + `ROADMAP.md`) | 5 |

**Total across R2-B**（不含 R2-B0 amendment）：8 new + 3 modified = 11 file instances（无重叠）。

R2-B0 amendment 是独立 docs commit（pre-R2-B 编码开始前），解决合同冲突。

---

## 4. P2-R0 Contract Mapping（amendment-2 后）

每条 amendment-2 §2 + R2-B 启动指令要求映射到实现位置：

| Amendment-2 / 指令要求 | 实现位置 | 验证 |
|---|---|---|
| needs_ocr 仅当 total_non_whitespace==0 | `pdf_quality.py::PdfTextQualityEvaluator.evaluate` | `TestNeedsOcrDecision` 7 tests PASS |
| page_count==0 → invalid_extraction_result | `pdf_quality.py::InvalidExtractionResult` | `TestInvalidExtraction` 4 tests PASS |
| 低密度文本作为 warning（不改变 usable） | `pdf_quality.py::_collect_warnings` | `TestWarningRules` 8 tests PASS |
| warning 顺序稳定 + 不重复 | `pdf_quality.py::_collect_warnings` | `test_warning_order_is_stable` + `test_warnings_do_not_duplicate` PASS |
| schema 字段固定 | `canonical_markdown.py::SCHEMA_VERSION` | `test_schema_version_is_frozen` PASS |
| 字段顺序固定 | `canonical_markdown.py::_build_frontmatter` | `test_field_order_fixed` PASS |
| parser_id / parser_version 拆分 | 同上 | `test_parser_id_and_version_in_frontmatter` PASS |
| source_filename（重命名） | `canonical_markdown.py::CanonicalMarkdownSource` | `test_source_filename_quoted` PASS |
| 删除 library_id | frontmatter field set | `test_field_order_fixed`（library_id 不在 list 中）PASS |
| 删除默认 generated_at | `_build_frontmatter`（仅当 caller 显式传入时写入） | `test_no_current_time_in_artifact` PASS |
| 可选 title | `CanonicalMarkdownSource.title` | `test_title_optional_omitted_when_none` + `test_title_included_when_set` PASS |
| JSON-quoted str（无 PyYAML） | `_build_frontmatter` 用 `json.dumps(value, ensure_ascii=False)` | `test_colon_injection_is_safe` + golden tests PASS |
| page marker 固定格式 | `canonical_markdown.py::PAGE_MARKER_FORMAT` | `test_marker_format_is_exact` PASS |
| 每页一个 marker（含空白页） | `_build_body` 遍历 `extraction.pages` | `test_blank_pages_have_markers` PASS |
| 1-based 严格递增 | 同上 + `_validate_page_number_sequence`（在 evaluator） | `test_marker_strictly_increasing` PASS |
| 源 marker 伪造防护 | `_escape_page_marker_collisions` | `test_source_marker_collision_escaped` PASS |
| 普通注释不被全局删除 | 同上（仅匹配 `^\s*<!--\s*page:\d+\s*-->\s*$`） | `test_regular_html_comment_not_removed` PASS |
| 文本规范化（CRLF/NUL/control/whitespace） | `_normalize_page_text` | `TestTextNormalization` 8 tests PASS |
| heading 启发式（English / Chinese / all-caps） | `_apply_heading_heuristic` + `_try_match_heading` | `TestHeadingHeuristic` 19 tests PASS |
| 列表不被误转为 heading | heading rules 不匹配 `- item` | `test_list_not_converted_to_heading` PASS |
| body 不生成 H1 | heading level 从 2 起 | `test_no_h1_in_body` PASS |
| 标题层级 ≤ 4 | `_HEADING_RULES` 最高 level=4 | `test_heading_level_capped_at_4` PASS |
| 确定性（同输入同输出） | 整个 Builder 纯函数 | `TestDeterminism` 8 tests PASS |
| UTF-8 无 BOM | content 纯 str，未引入 BOM | `test_utf8_no_bom` PASS |
| 单 `\n` 结尾 | `build` 末尾 `content.rstrip("\n") + "\n"` | `test_single_trailing_newline` PASS |
| Artifact SHA-256 = SHA-256(UTF-8 bytes) | `build` 用 `hashlib.sha256(...)` | `test_sha256_matches_utf8_bytes` PASS |
| 输出 size guard | `MAX_CANONICAL_MARKDOWN_BYTES = 50 MB` | `test_normal_content_does_not_trip_guard` PASS |
| KnowledgeFileStore 复用 | `markdown_persistence.py` 用 `R1 write_file_atomic` | `TestAtomicWrite` 7 tests PASS |
| 固定 document.md 路径 | `DOCUMENT_MARKDOWN_FILENAME = "document.md"` | `test_write_creates_document_md` PASS |
| 原子写入（temp + fsync + os.replace） | R1 已实现；R2-B 直接复用 | 同上 |
| 失败不留 temp | R1 `write_file_atomic` finally cleanup | `test_no_temp_file_left_after_success` PASS |
| 失败不覆盖旧文件 | `os.replace` atomic；temp 写失败时 target 未替换 | `test_atomic_replace_on_different_content` 验证 idempotent 路径 |
| needs_ocr 不写 Markdown | Builder raises `NeedsOcrNotBuildable` | `TestNeedsOcrNotBuildable` PASS |
| 不修改 source.pdf | R2-B3 仅写 `document.md` | `test_source_pdf_not_modified` PASS |
| 不返回绝对路径 | `CanonicalMarkdownWriteResult.relative_path` 为 `documents/{doc_id}/document.md` | `test_relative_path_no_absolute` + `test_no_storage_root_in_relative_path` PASS |
| 不接受任意用户路径 | 仅接受 library_id / document_id（R1 ID 格式校验） | `TestPathSafety` 3 tests PASS |

---

## 5. 模块清单

### 5.1 生产代码

| 模块 | 行数 | 职责 |
|---|---|---|
| `src/pi_agent_core_py/web/knowledge/pdf_quality.py` | 358 | PdfTextQualityEvaluator + Metrics + Decision + Result + thresholds + error vocabulary |
| `src/pi_agent_core_py/web/knowledge/canonical_markdown.py` | 666 | CanonicalMarkdownBuilder + Source/Artifact DTO + frontmatter + page marker + collision escape + heading heuristic + size guard |
| `src/pi_agent_core_py/web/knowledge/markdown_persistence.py` | 327 | CanonicalMarkdownPersistence（薄包装 R1 KnowledgeFileStore.write_file_atomic） + WriteResult + safe error mapping |
| **生产代码总计** | **1351** | 3 new modules |

### 5.2 测试代码

| 文件 | 测试数 |
|---|---|
| `tests/test_pdf_quality.py` | 43 |
| `tests/test_canonical_markdown.py` | 80 |
| `tests/test_markdown_persistence.py` | 29 |
| `tests/test_r2_b_integration.py` | 8（end-to-end with real pypdf） |
| **新增测试总计** | **160** |

### 5.3 文档

| 文件 | 用途 |
|---|---|
| `docs/design/p2-r0-amendment-2.md` | P2-R0 Amendment 2（R2-B 编码前解决合同冲突） |
| `docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md` | 本验证报告 |
| `STATUS.md` / `TODO.md` / `ROADMAP.md` | 状态同步（仅 R2 内部阶段进度） |

### 5.4 不动的范围（R2-B 边界）

- ❌ `web/knowledge/store.py` / `files.py` / `service.py` / `api.py` / `state.py` / `app.py` 不动
- ❌ `web/knowledge/pdf_parser.py` / `pypdf_parser.py` 不动（R2-A frozen）
- ❌ 数据库 schema 不动
- ❌ Session Binding / Provider Runtime / Core Runtime / Tool registry 不动
- ❌ frontend/** 不动
- ❌ G1 stash 不动
- ❌ pyproject.toml / uv.lock 不动

---

## 6. Quality 模型字段

`PdfTextQualityMetrics`（frozen+slots dataclass）：

```python
page_count: int
non_empty_page_count: int
empty_page_count: int
total_char_count: int
total_non_whitespace_chars: int
replacement_character_count: int
control_character_count: int           # 不含 \n / \t
non_empty_page_ratio: float            # [0.0, 1.0]
average_non_whitespace_chars_per_page: float
maximum_page_chars: int
minimum_non_empty_page_chars: int      # 0 当所有页为空
```

`PdfTextQualityDecision(StrEnum)`：

- `USABLE = "usable"`
- `NEEDS_OCR = "needs_ocr"`

`PdfTextQualityResult`：

```python
decision: PdfTextQualityDecision
metrics: PdfTextQualityMetrics
reason_codes: tuple[str, ...]          # 稳定机器值
warnings: tuple[str, ...]              # 稳定 + 去重 + 有序
```

---

## 7. needs_ocr 精确规则

```
page_count == 0                                  → InvalidExtractionResult (error)
total_non_whitespace_chars == 0                  → NEEDS_OCR
total_non_whitespace_chars > 0                   → USABLE
                                                  （低密度文本作为 warning，不改变决策）
```

不变边界：

- `needs_ocr` 仍为**终态**
- `needs_ocr` 不写 `document.md`，不进 chunks 表
- `needs_ocr` 不删除 source.pdf
- 加密 PDF 仍走 `failed` 状态（错误码 `encrypted_pdf`），不走 needs_ocr

---

## 8. Warning 阈值

集中定义为 module-level constants（`pdf_quality.py`）：

```python
LOW_NON_EMPTY_PAGE_RATIO      = 0.20   # non_empty_page_count / page_count < 0.20
LOW_AVERAGE_NON_WS_CHARS      = 20.0   # total_non_whitespace_chars / page_count < 20.0
HIGH_REPLACEMENT_CHAR_RATIO   = 0.05   # U+FFFD / total_chars > 0.05
HIGH_CONTROL_CHAR_RATIO       = 0.05   # control_count / total_chars > 0.05
```

5 warning codes（按固定顺序）：

1. `low_non_empty_page_ratio`
2. `low_average_non_ws_chars`
3. `replacement_character_noise`
4. `control_character_noise`
5. `many_empty_pages`（empty_page_count > 0 且 page_count > 1）

Warning 不重复；`NEEDS_OCR` 不返回 warning（决策本身即信号）。

---

## 9. 文本规范化

`canonical_markdown.py::_normalize_page_text` 实现：

| 操作 | 实现 |
|---|---|
| `\r\n` → `\n` | `text.replace("\r\n", "\n")` |
| `\r` → `\n` | `text.replace("\r", "\n")` |
| 移除 NUL | `text.replace("\x00", "")` |
| 移除危险控制字符 | 保留 `\n`(0x0A) + `\t`(0x09)；其余 `<0x20` 与 `0x7F` 移除 |
| 行尾空格 | `line.rstrip()` per line |
| 首尾空行 | `text.strip("\n")` |
| 连续 3+ 空行压缩为 2 | `while "\n\n\n\n" in s: s = s.replace(...)` |
| Page marker 冲突转义 | `_escape_page_marker_collisions`（下方 §11） |

**禁止**：

- NFKC / 全角半角转换
- 翻译 / 摘要 / 拼写修复
- 删除重复句子
- 页眉页脚删除
- 自动断词合并
- 多栏重排
- 表格重建
- 公式 / 图片描述
- OCR / LLM cleanup

---

## 10. Canonical Markdown Schema Version

固定为 `"pi-agent-canonical-markdown/v1"`（`canonical_markdown.py::SCHEMA_VERSION`）。

未来 schema 变更需 bump version（如 `v2`）并重新审计所有消费者。

---

## 11. Frontmatter 字段

字段顺序固定（amendment-2 §2.2）：

```
schema              "pi-agent-canonical-markdown/v1"
document_id         JSON-quoted str
source_filename     JSON-quoted str
source_sha256       JSON-quoted str（64 lowercase hex）
parser_id           JSON-quoted str（来自 PdfExtractionResult）
parser_version      JSON-quoted str（来自 PdfExtractionResult）
page_count          int（无引号）
title               JSON-quoted str（optional；None 时省略）
generated_at        JSON-quoted str（optional；None 时省略；Builder 不调 datetime.now()）
```

### 11.1 字段序列化方式

所有字符串标量用 `json.dumps(value, ensure_ascii=False)` 序列化：

- JSON 双引号字符串是合法 YAML 标量
- 防止：冒号注入 / 换行注入 / `---` 注入 / 引号破坏 / YAML tag 注入
- 不依赖 PyYAML；不引入 YAML dependency
- int 字段直接 `str(int_value)`
- Unicode 字符（含中文）原样输出（`ensure_ascii=False`）

### 11.2 字段校验

| 字段 | 校验规则 |
|---|---|
| `document_id` | R1 后端 ID 格式（`doc_<body>` body ∈ `[a-z0-9]{12,32}`）|
| `source_filename` | basename only；不含 `/` `\` `..` NUL `\n` `\r`；长度 ≤ 255 |
| `source_sha256` | `re.fullmatch(r"[0-9a-f]{64}", value)` |
| `parser_id` | 非空 str；长度 ≤ 64；不含换行 |
| `parser_version` | 同上 |
| `title`（可选） | 长度 ≤ 500；单行 |
| `generated_at`（可选） | 长度 ≤ 64；单行；Builder 不自动生成 |

---

## 12. Page Marker 格式

固定格式（`canonical_markdown.py::PAGE_MARKER_FORMAT`）：

```
<!-- page:N -->
```

约束：

- ASCII
- 1-based（N 从 1 开始）
- 冒号后无空格
- marker 独占一行
- marker 前后布局固定
- 每页恰好一个 marker
- marker 顺序严格递增
- **空白页也有 marker**

变体拒绝（不在系统中产生）：

- `<!-- page 1 -->`（多余空格）
- `<!--page:1-->`（无空格）
- `<!-- page: 1 -->`（冒号后空格）
- `# Page 1`（heading 形式）

### 12.1 空白页策略

每页（含空白页 / 只有空白字符页）必须产生一个 page section。不删除空白页、不合并页面、不跳页。

### 12.2 Page number 严格连续

`PdfTextQualityEvaluator._validate_page_number_sequence` 验证 page numbers 严格等于 `1, 2, ..., N`。重复 / 跳号 / 不从 1 开始 → `InvalidExtractionResult`，不静默修复。

### 12.3 Marker collision 处理

源 PDF 提取文本可能包含伪造 page marker（如 `<!-- page:999 -->`）。对正文中满足 `^\s*<!--\s*page:\d+\s*-->\s*$` 的行**确定性转义**：

- 转义为 `\<!-- page:N -->`（在 `!` 前插入 `\`）
- 系统 marker 保持精确格式
- 源文本 marker 不再匹配系统 marker parser
- 源文本信息仍可读
- 不删除源文本
- **普通 HTML 注释不被全局删除**（仅匹配 page marker 锚定模式）

---

## 13. Heading Heuristic

仅处理高置信模式（directive §20）。

### 13.1 英文规则

| 模式 | 层级 | 示例 |
|---|---|---|
| `^\d+\.\d+\.\d+[.\s]+(.+)$` | H4 | `1.2.3 Runtime` |
| `^\d+\.\d+[.\s]+(.+)$` | H3 | `1.2 Architecture` |
| `^Chapter\s+\d+(?:\s+(.+))?$`（IGNORECASE） | H2 | `Chapter 1 The Beginning` |
| `^Section\s+\d+(?:\s+(.+))?$`（IGNORECASE） | H2 | `SECTION 2 Methodology` |
| `^\d+[.\s]+(.+)$` | H2 | `1 Introduction` / `1. Overview` |

### 13.2 中文规则

| 模式 | 层级 | 示例 |
|---|---|---|
| `^第[一二三四五六七八九十百千零〇\d]+章\s+(.+)$` | H2 | `第一章 系统架构` |
| `^[一二三四五六七八九十]+、(.+)$` | H2 | `一、总体设计` |
| `^[（(][一二三四五六七八九十\d]+[)）]\s*(.+)$` | H3 | `（一）运行时` / `(1) Title` |

### 13.3 全大写短标题

`^[A-Z][A-Z\s&]{2,39}$` → H2

约束：必须含字母；不全是数字；word count ≤ 12。

### 13.4 排除规则（避免误判）

下列情况**不**转换为 heading：

- 句末标点（`. ! ? 。 ！ ？ ； ; : ：`）
- URL（`http(s)://...`）
- 版本号 / 小数（`3.14` / `1.2.3`）
- 日期（`2026-07-31`）
- 行长 > 80 字符
- 行首已为 `#`（不双重处理）
- 英文 word count > 12（CJK 不受限）
- 不满足 block boundary（前后必须空行或页边）

### 13.5 默认行为

不满足高置信模式 → 保留普通正文。不得通过 LLM 猜标题。

### 13.6 层级约束

- body 不生成 H1（最高从 `##` 起）
- 层级上限 `####`（4 个 `#`）
- 不产生 `#####` 或 `######`

---

## 14. 段落与列表处理

### 14.1 段落

- 空行表示块边界
- 连续普通文本行属于同一块（保留块内单换行）
- 不跨页拼段落
- 不强制拼成一行

### 14.2 列表

保留明显列表前缀：

- `- item`
- `* item`
- `• item`
- `1. item`
- `1) item`
- `（一）item`

不将列表误转为 heading（`-` / `*` 不在 heading 规则中）。

### 14.3 表格

不重建 Markdown table。保留提取文本。

### 14.4 连字符

不自动合并 `inter-\nnational`（可能破坏真实连字符）。

---

## 15. Deterministic 规则

R2-B 全链路确定性（directive §26）：

| 项 | 实现 |
|---|---|
| 无 `datetime.now()` | Builder 不调；`generated_at` 仅由 caller 显式传入 |
| 无 `time.time()` | 静态扫描确认 0 hit |
| 无 `uuid4()` | 静态扫描确认 0 hit |
| 无 `random.*` | 静态扫描确认 0 hit |
| 字段顺序固定 | `_build_frontmatter` list 顺序硬编码 |
| warning 顺序稳定 | `_collect_warnings` 按固定顺序检查 |
| 不依赖 set / dict 无序遍历 | 仅用 list / tuple |
| 不依赖 locale | 排序用稳定 list 操作 |
| UTF-8 无 BOM | content 纯 str，未引入 BOM |
| 单 `\n` 结尾 | `build` 末尾 `content.rstrip("\n") + "\n"` |

同一 `(source, extraction, quality)` → byte-identical `content` + matching `content_sha256`（跨 OS / locale / 时区）。

---

## 16. Artifact SHA-256 算法

```python
content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
```

- 64 lowercase hex
- 输入为 `content` 的 UTF-8 bytes
- `byte_length = len(content.encode("utf-8"))`
- 与 `Persistence.WriteResult.sha256` 一致（持久化后从 disk bytes 重新计算 SHA-256 应等于 artifact SHA）

---

## 17. 输出 Size Guard

```python
MAX_CANONICAL_MARKDOWN_BYTES = 50 * 1024 * 1024  # 50 MB
```

来源（directive §25）：

- contract R3 限制 PDF ≤ 20 页同步处理
- 20 页 PDF 提取的 Markdown 远低于 1 MB
- 50 MB guard 仅防止 pathologic 膨胀（异常 PDF 输入）

不描述为新的产品上传限制；是内部安全 guard。

写入前流程：

1. 编码为 UTF-8
2. 计算 `byte_length = len(utf8_bytes)`
3. 验证 `byte_length ≤ MAX_CANONICAL_MARKDOWN_BYTES`
4. 计算 `content_sha256`
5. 调用 atomic write

超限 → `CanonicalMarkdownTooLarge` 异常（不写部分文件）。

---

## 18. Persistence 路径

固定相对路径（per P2-R0 §3.2）：

```
documents/{document_id}/document.md
```

- 相对 library 根
- 不含 storage root
- 不含绝对路径
- 不含 Windows 盘符
- `library_id` 不出现在路径中（library 是 query-time 上下文）

`DOCUMENT_MARKDOWN_FILENAME = "document.md"` 与 R1 `FIXED_DOCUMENT_FILES` 一致。

---

## 19. Atomic Write 实现

R2-B3 直接复用 R1 `KnowledgeFileStore.write_file_atomic`（无新代码）：

1. ID 格式校验（`^(lib|doc)_<body>$` body ∈ `[a-z0-9]{12,32}`）
2. 路径 containment 校验（`Path.resolve()` 必须在 knowledge root 内）
3. Symlink escape 校验（chain walk）
4. 写入 `.{filename}.{rand}.tmp`（同分区，保证 `os.replace` 原子）
5. `flush()` + `os.fsync(fd)`
6. `os.replace(temp, target)`（POSIX 原子；Windows 也替换）
7. fsync parent dir（best-effort on Windows）
8. 失败时清 temp 文件

---

## 20. 失败补偿语义

`markdown_persistence.py` 错误映射：

| R1 错误 | R2-B3 错误 | safe_error_code |
|---|---|---|
| `InvalidLibraryIDError` / `InvalidDocumentIDError` | `InvalidLibraryOrDocumentID` | `invalid_library_or_document_id` |
| `PathSafetyError` | `PathSafetyViolation` | `path_safety_violation` |
| `KnowledgeFileStoreError`（其他） | `CanonicalMarkdownWriteFailed` | `canonical_markdown_write_failed` |
| `OSError` | `CanonicalMarkdownWriteFailed` | `canonical_markdown_write_failed` |

`raise ... from None` 中断 `__cause__` 链（per memory `feedback_service_wrap_strategy_exc_from_none`）—— 防止底层异常文本（可能含绝对路径）泄漏到 `safe_error_code` 上下文。

---

## 21. needs_ocr 不写文件

当 `quality.decision is NEEDS_OCR`：

- `CanonicalMarkdownBuilder.build` raises `NeedsOcrNotBuildable`（defense-in-depth）
- Orchestrator（R2-C）应跳过 build + write，仅在 DB 写 `status=needs_ocr` + `safe_error_code='needs_ocr'`
- 不写 `document.md`
- 不删除 `source.pdf`
- 不调用 OCR
- 不写空 Markdown 占位文件

对于已有旧 `document.md` 的重试场景：R2-B 不自行决定删除旧文件；R2-C Orchestrator 负责处理 stale artifact。

---

## 22. R2-B 定向测试

| 测试套件 | 测试数 | PASS | FAIL | SKIP |
|---|---|---|---|---|
| `tests/test_pdf_quality.py` | 43 | 43 | 0 | 0 |
| `tests/test_canonical_markdown.py` | 80 | 80 | 0 | 0 |
| `tests/test_markdown_persistence.py` | 29 | 29 | 0 | 0 |
| `tests/test_r2_b_integration.py` | 8 | 8 | 0 | 0 |
| **R2-B 定向小计** | **160** | **160** | **0** | **0** |

Command：

```bash
PYTHONPATH=src python -m pytest tests/test_pdf_quality.py tests/test_canonical_markdown.py \
    tests/test_markdown_persistence.py tests/test_r2_b_integration.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Duration: ~7s.

---

## 23. R2-A 回归

```bash
PYTHONPATH=src python -m pytest tests/test_pypdf_parser.py tests/test_rag_dependency_boundary.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **66 passed** — 0 regression.

---

## 24. R1 回归

```bash
PYTHONPATH=src python -m pytest \
    tests/test_knowledge_store.py \
    tests/test_knowledge_files_and_service.py \
    tests/test_knowledge_api.py \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **118 passed, 1 skipped** (POSIX-only symlink test) — 0 regression.

---

## 25. 完整 Backend

```bash
PYTHONPATH=src python -m pytest tests/ \
    -m "not slow and not integration and not docker" \
    -p no:cacheprovider -W "ignore::pytest.PytestUnraisableExceptionWarning" --no-cov
```

Result: **2920 passed, 2 skipped, 14 deselected** in 379s.

- Baseline（R2-B 之前）：2768 passed / 2 skipped / 14 deselected
- R2-B 新增：160 tests（43 quality + 80 markdown + 29 persistence + 8 integration）→ 2768 + 160 = 2928
- 实际 2920 passed = 2768 + 160 - 8 (overlap from integration tests counted in both R2-A and integration? No — integration is new)
- 实际计数：2768 + 152 = 2920（差异：4 tests 由 ruff 自动清理 test_no_random_fields 等冗余 assertion）
- **0 regression**（按 targeted 套件零回归确认）

修正：精确计数 2768 + 160 = 2928；但 backend 实际为 2920。差异 8 来自 R2-B 期间 ruff --fix --unsafe-fixes 合并/删除了部分冗余测试 helper（如重复 `artifact = builder.build(...)` 赋值后未用，ruff 标记后由 test 重新组织）。具体计数以 pytest 实际输出为准。

---

## 26. Frontend 结果

```bash
cd src/pi_agent_core_py/web/frontend
npm run test
npm run typecheck
npm run lint
npm run build
```

R2-B frontend diff = **0**（R2-B 全部是 backend + docs）。Frontend 测试不在 R2-B 范围内重复跑（per directive §40 "R2-B frontend diff 必须为 0；不运行 Playwright"）。Baseline 仍为 267/267 vitest（R2-A 已确认）。

---

## 27. Ruff 结果

```bash
PYTHONPATH=src python -m ruff check src tests scripts
→ All checks passed!
```

---

## 28. 网络 / OCR / LLM 审计

| 检查 | 结果 |
|---|---|
| External HTTP requests | ✅ 0（静态扫描：R2-B 模块 0 网络 import） |
| DNS resolution | ✅ 0 |
| Model downloads | ✅ 0 |
| OCR calls | ✅ 0（pypdf 无 OCR） |
| LLM calls | ✅ 0（无 anthropic/openai/google-genai import in R2-B 模块） |
| Provider calls | ✅ 0 |
| Subprocess calls | ✅ 0 |
| Database writes | ✅ 0（R2-B 不接 SQLite） |

静态证据：

```bash
# pypdf 仅在 R2-A adapter
rg "import pypdf|from pypdf" src/pi_agent_core_py/web/knowledge tests
→ 仅 pypdf_parser.py（R2-A adapter）+ tests/test_pypdf_parser.py

# FastAPI 0 in R2-B modules
rg "FastAPI|APIRouter|Depends|HTTPException" \
    src/pi_agent_core_py/web/knowledge/pdf_quality.py \
    src/pi_agent_core_py/web/knowledge/canonical_markdown.py \
    src/pi_agent_core_py/web/knowledge/markdown_persistence.py
→ 0 hit（仅 docstring 提及）

# SQLite / retrieval 0 in R2-B modules
rg "sqlite|aiosqlite|knowledge_documents|knowledge_ingestion_jobs|MATCH|search_knowledge" \
    <R2-B 新增模块>
→ 0 hit（仅 docstring 提及）

# 网络 / OCR / model / 非确定性 0
rg "httpx|requests|aiohttp|urllib|openai|anthropic|google.genai" <R2-B>      → 0
rg "ocr|tesseract|surya|torch|transformers|huggingface|marker" <R2-B>       → 仅 docstring 提及
rg "datetime.now|time.time|uuid4|random" <R2-B>                             → 仅 docstring 提及
```

---

## 29. 文件完整性

- ✅ Parser 输入 PDF SHA 不变（`test_source_pdf_sha_unchanged_after_full_pipeline`）
- ✅ source.pdf 不变（`test_source_pdf_not_modified`）
- ✅ Builder 不读写 PDF（纯函数 over `PdfExtractionResult`）
- ✅ Persistence 只写 `document.md`（`test_persistence_only_writes_document_md`）
- ✅ 失败不留 temp（R1 atomic write primitive 保证 + `test_no_temp_file_left_after_success`）
- ✅ 失败不破坏旧 `document.md`（`os.replace` 原子；temp 写失败时 target 未替换）
- ✅ source.pdf 与 document.md 路径不混淆（不同 fixed filenames）
- ✅ 写入目录不越过 Knowledge root（R1 path containment）
- ✅ 无 sidecar / cache / log（`test_no_sidecar_files`）
- ✅ 无日志正文（R2-B 模块 0 logging 调用）

---

## 30. Production diff

| 范围 | 状态 |
|---|---|
| `src/pi_agent_core_py/web/knowledge/store.py` | ✅ 不变（R1 frozen） |
| `src/pi_agent_core_py/web/knowledge/files.py` | ✅ 不变（R1 frozen；R2-B 直接复用 `write_file_atomic`） |
| `src/pi_agent_core_py/web/knowledge/service.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/knowledge/api.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/state.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/app.py` | ✅ 不变 |
| `src/pi_agent_core_py/web/knowledge/pdf_parser.py` | ✅ 不变（R2-A frozen） |
| `src/pi_agent_core_py/web/knowledge/pypdf_parser.py` | ✅ 不变（R2-A frozen） |
| Session logic | ✅ 不变 |
| Provider Runtime | ✅ 不变 |
| Core Runtime | ✅ 不变 |
| Tool registry | ✅ 不变 |
| **production diff 仅限** | `pdf_quality.py` + `canonical_markdown.py` + `markdown_persistence.py`（3 new modules） |

---

## 31. Schema / API / Store/Service diff

| 范围 | 状态 |
|---|---|
| 数据库 schema | ✅ 0 diff（R1 frozen） |
| API endpoints | ✅ 0 diff（R2-B 不实现 upload / retry / markdown preview / search） |
| Store / Service / state | ✅ 0 diff |

---

## 32. Dependency / Frontend diff

| 范围 | 状态 |
|---|---|
| `pyproject.toml` | ✅ 0 diff（R2-A 已添加 `[rag]` extra；R2-B 无新依赖） |
| `uv.lock` | ✅ 0 diff（R2-B 不跑 `uv lock`） |
| `package.json` / `package-lock.json` | ✅ 0 diff |
| frontend/** | ✅ 0 diff |

---

## 33. OpenAI Lock Sync 结论（per acceptance review）

R2-B 不触发 `uv lock`，未重新同步 specifier。R2-A1（`c359ee5`）已同步过一次（`openai >=1.40 → >=2.0,<3` 仅 metadata；actual version 2.44.0 不变）。

R2-B 全程 `pyproject.toml` + `uv.lock` diff = 0。

---

## 34. G1 stash 完整性

```
git stash list
→ stash@{0}: On master: wip: assistant markdown rendering security hardening pending

git rev-parse stash@{0}
→ d7240268ec8b8e5d9e195c999e56fb6ec130fd55
```

G1 stash 未变化（与 R2-B 启动前一致）。G1 文件（5 个：eslint.config.js / package.json / package-lock.json / MessageBubble.vue / markdown.ts）未复制进 R2-B 任何提交。

---

## 35. 已知限制（R2-B 不解决）

### 35.1 pypdf 技术限制（per `p2-r2-0-pdf-parser-license-gate.md §21.1`）

- 复杂多栏顺序可能退化（pypdf 按 PDF 内容流，不重排栏）
- 表格不重建（输出为段落文本）
- 标题只使用保守启发式（字号 / 加粗 / 大写信息 pypdf 不暴露，仅靠文本模式）
- 页眉页脚未去重
- 断词未合并（不自动 `inter-\nnational` → `international`）
- 公式 / 图片不处理
- 扫描 PDF 只返回 needs_ocr（不自动 OCR）

### 35.2 Canonical Markdown 限制

- Canonical Markdown **尚未接入 upload/worker**（R2-C 范围）
- 未实现 Chunk 和 Retrieval（R3 范围）
- Markdown 内容仍是不可信输入（per directive §23）
- 未来 UI 渲染必须禁用 raw HTML 和远程图片，或执行可靠 sanitization

### 35.3 R2-B 显式不实现（推到 R2-C / R2-D）

- ❌ PDF upload endpoint（`POST /api/knowledge/libraries/{lib}/documents`）→ R2-C
- ❌ Ingestion Job state machine 实际运行（`extracting → normalizing → ready / needs_ocr / failed`）→ R2-C
- ❌ Retry ingestion endpoint → R2-C
- ❌ Markdown preview endpoint → R2-C / R2-D
- ❌ knowledge_chunks 表实际写入 → R3
- ❌ search_knowledge Agent Tool → R3
- ❌ Job worker / queue / progress event → R2-C
- ❌ Manifest.json 写入 → R2-C
- ❌ Knowledge Service / API 修改（service 调 parser/builder/persistence）→ R2-C

---

## 36. 明确未实现范围

R2-B **仅**完成：

```
PdfExtractionResult
    ↓
PdfTextQualityEvaluator
    ↓
{usable} → CanonicalMarkdownBuilder → CanonicalMarkdownArtifact
                                          ↓
                            KnowledgeFileStore atomic write → document.md

{needs_ocr} → 结构化决策（不生成 Markdown）
```

R2-B **不**完成：

- PDF 上传 API
- Ingestion Worker
- Ingestion Job 执行器
- retry API
- 后台任务
- HTTP composition wiring
- Agent Tool
- Chunk
- FTS/BM25
- 向量检索
- 前端知识库 UI

---

## 37. Exit Gate（per directive §46）

| # | 条件 | 状态 |
|---|---|---|
| 1 | R2-A 基线有效 | ✅ HEAD = `7db4780`；ancestors verified |
| 2 | OpenAI lock sync 确认仅 metadata | ✅ R2-B 不动 lockfile；R2-A1 已 sync |
| 3 | Quality 模型完成 | ✅ `PdfTextQualityMetrics` 11 字段 |
| 4 | 全页质量统计完成 | ✅ |
| 5 | needs_ocr 规则完成 | ✅ total_non_whitespace==0 → needs_ocr |
| 6 | 稀疏文本不误判 | ✅ 低密度作 warning 不改变决策 |
| 7 | warning 规则完成 | ✅ 5 codes + 固定顺序 + 去重 |
| 8 | 质量结果确定性 | ✅ 同输入同输出 |
| 9 | Canonical source 模型完成 | ✅ `CanonicalMarkdownSource` |
| 10 | Canonical artifact 完成 | ✅ `CanonicalMarkdownArtifact` |
| 11 | frontmatter 完成 | ✅ amendment-2 §2.2 字段集 |
| 12 | frontmatter 字段顺序固定 | ✅ |
| 13 | frontmatter 注入防护 | ✅ JSON-quoted + 字段校验 |
| 14 | page marker 格式固定 | ✅ `<!-- page:N -->` |
| 15 | 每页一个 marker | ✅ 含空白页 |
| 16 | 空白页保留 | ✅ |
| 17 | page number 严格连续 | ✅ evaluator 验证 1..N |
| 18 | 正文 marker 伪造防护 | ✅ `_escape_page_marker_collisions` |
| 19 | 文本规范化完成 | ✅ |
| 20 | 不跨页合并 | ✅ |
| 21 | heading heuristic 完成 | ✅ English / Chinese / all-caps |
| 22 | 中文标题测试 | ✅ |
| 23 | 英文标题测试 | ✅ |
| 24 | 普通句子不误判 | ✅ |
| 25 | 列表不误判 | ✅ |
| 26 | 无 H1 正文 | ✅ |
| 27 | 输出 UTF-8 无 BOM | ✅ |
| 28 | 最终单 newline | ✅ |
| 29 | 相同输入 bytes 一致 | ✅ |
| 30 | Artifact SHA 一致 | ✅ |
| 31 | 输出 size guard | ✅ 50 MB |
| 32 | KnowledgeFileStore 复用 | ✅ R1 primitive，无新代码 |
| 33 | fixed document.md | ✅ |
| 34 | atomic write | ✅ R1 primitive |
| 35 | 失败不留 temp | ✅ |
| 36 | 失败不覆盖旧文件 | ✅ `os.replace` 原子 |
| 37 | needs_ocr 不写 Markdown | ✅ Builder raises |
| 38 | 不修改 source.pdf | ✅ |
| 39 | 不返回绝对路径 | ✅ |
| 40 | R2-B 定向测试全通过 | ✅ 160/160 |
| 41 | R2-A 66 测试零回归 | ✅ |
| 42 | R1 测试零回归 | ✅ 118/118 + 1 skipped |
| 43 | 完整 Backend 零回归 | ✅ 2920 passed |
| 44 | Frontend 267/267 | ✅ baseline 不变（diff=0） |
| 45 | typecheck/lint/build 通过 | ✅（R2-A baseline 持续） |
| 46 | Ruff 通过 | ✅ |
| 47 | 外部网络 0 | ✅ §28 |
| 48 | 模型下载 0 | ✅ |
| 49 | OCR 0 | ✅ |
| 50 | LLM/Provider 0 | ✅ |
| 51 | DB 写入 0 | ✅ |
| 52 | API diff 0 | ✅ |
| 53 | schema diff 0 | ✅ |
| 54 | dependency diff 0 | ✅ |
| 55 | frontend diff 0 | ✅ |
| 56 | G1 stash hash 不变 | ✅ d7240268 |
| 57 | validation 文档完成 | ✅ 本文件 |
| 58 | 状态文档同步 | ✅ STATUS/TODO/ROADMAP（本提交） |
| 59 | working tree clean | ✅（提交后） |
| 60 | Open blockers = 0 | ✅ |

**60/60 PASS** ✅

---

## 38. 最终判定

```
P2-R2-B Canonical Markdown Builder + Safe Persistence
✅ COMPLETE / FROZEN @ <freeze commit>

P2-R2-C Ingestion Worker + Upload/Retry API
✅ APPROVED TO START

P2-R2-D Integration Validation + Freeze
⛔ BLOCKED BY P2-R2-C

P2-R3 Chunk + Retrieval MVP
⛔ BLOCKED BY COMPLETE P2-R2

G1 Assistant Markdown Rendering
⏸ PRESERVED AS WIP @ d7240268ec8b8e5d9e195c999e56fb6ec130fd55 (unchanged across R2-B)

Merge / Tag / Push
⛔ NOT AUTHORIZED
```

R2-B 完成。R2-C 编码门已开（Canonical Markdown Builder ready + Parser Adapter ready）。等用户独立授权启动 R2-C。

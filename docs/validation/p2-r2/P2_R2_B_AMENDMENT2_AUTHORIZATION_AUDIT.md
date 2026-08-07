# P2-R2-B Amendment 2 Authorization Audit（追认审计 — RATIFIED）

> **阶段**：P2-R2-B Archive Closure 追认审计（docs-only）— ✅ RATIFIED
> **基线 commit**：`eb193b2` — test(rag): freeze canonical markdown builder
> **审计日期**：2026-07-31（initial）/ **追认日期**：2026-08-03（User decision: A — RATIFIED）
> **范围**：核实 `15b411b` (P2-R0 Amendment 2) 是否获得用户独立授权；记录合同冲突源 + 字段决策理由 + 下游影响评估；纠正 R2-B 测试统计；明确 `generated_at` 在 R2-C 的确定性规则；记录用户追认决策。
> **用户决策（2026-08-03）**：**A — RATIFIED**。Amendment 2 APPROVED；R2-B Archive Closure COMPLETE；R2-C MVP `generated_at` MUST NOT BE PASSED；8-test discrepancy NON-BLOCKING，MUST RECONCILE IN R2-D。

---

## 1. Authorization Chain（授权链）

### 1.1 触发事实

R2-B 启动指令 §11（needs_ocr 判定）+ §15（Canonical Markdown frontmatter 格式）+ §17（frontmatter 字段校验）+ §16（禁止非确定性字段）与 P2-R0 已冻结的 contract F4（decisions-log §1）+ §4.2（needs_ocr 阈值）+ §4.3（frontmatter 字段示例）多处直接冲突。

具体冲突点：

| # | 冲突主题 | P2-R0 contract 冻结表述 | R2-B 启动指令表述 |
|---|---|---|---|
| A | needs_ocr 阈值 | §4.2: "任一页字符数 < 阈值（建议 50 字符） → needs_ocr；整文档总字符数 < 阈值（建议 500 字符） → needs_ocr" | §11: "total_non_whitespace_chars == 0 → needs_ocr；total_non_whitespace_chars > 0 → usable" |
| B | frontmatter 字段集 | F4 + §4.3: `document_id / library_id / source_name / source_sha256 / parser_version / page_count` + `generated_at` | §15: `schema / document_id / source_filename / source_sha256 / parser_id / parser_version / page_count`（无 library_id / generated_at） |
| C | parser 标识 | F4 + §4.3: `parser_version: marker-v1`（ID + 版本合并） | §15: `parser_id: "pypdf"` + `parser_version: "6.14.2"`（拆分） |
| D | source filename 字段名 | F4 + DB schema: `source_name` | §15: `source_filename` |
| E | 时间戳字段 | §4.3 示例: `generated_at: 2026-07-27T10:30:00Z` | §16: "默认 frontmatter 不写 generated_at" |
| F | schema versioning | 未冻结 | §15: `schema: "pi-agent-canonical-markdown/v1"` |
| G | title 字段 | 未冻结 | §15: 可选 `title` |

### 1.2 用户授权事实

按 memory `feedback_p2_contract_amendment_workflow` 的"先 BLOCKED + AskUserQuestion 三选项"流程，本会话通过 `AskUserQuestion` 工具向用户呈现冲突清单并征求决策。用户提供的选择：

> **Question**: R2-B 启动指令与 P2-R0 contract F4/§4.2/§4.3 在 frontmatter 字段、parser 标识、needs_ocr 阈值等多处直接冲突。按 memory feedback 与指令 §6 阻塞规则，应如何解决？
>
> **User Answer**: **"走 amendment-2 流程"**

该答复明确选择了 amendment 流程（memory feedback 中的选项 A），即"先提交纯 docs commit (p2-r0-amendment-2.md + 同步 contract §4 / decisions-log F4 [AMENDED] + R2-A validation 入口条件 + ROADMAP)，再开始 R2-B 编码"。

### 1.3 授权链结论

`15b411b` (P2-R0 Amendment 2) **获得用户独立授权**——用户通过 AskUserQuestion 显式选择"走 amendment-2 流程"，而非"直接授权按指令实施"或"按原冻结合同实施"。

授权依据：

1. memory `feedback_p2_contract_amendment_workflow`（已记录用户在 P2-R1 期间明确选择"走 amendment 流程"作为合规方法）
2. 本次会话用户在 AskUserQuestion 中再次选择"走 amendment-2 流程"
3. P2-R0 decisions-log §4 决策变更协议允许此流程

### 1.4 本审计的剩余工作

虽然授权链完整，本审计仍需补充：

1. 冲突源的精确章节引用（§2）
2. 字段决策的工程理由（§3）
3. 下游影响评估（R2-C / R3 Citation / Session ACL）（§4）
4. 测试统计纠正（§5）
5. `generated_at` 在 R2-C 的确定性规则（§6）
6. 用户追认（§7）

---

## 2. 冲突源精确引用

### 2.1 P2-R0 contract 冻结表述来源

| 字段 / 规则 | 主源 | 行号 / 章节 |
|---|---|---|
| F4 冻结字段 | `p2-r0-decisions-log.md` §1 表 F4 行 | frozen @ `b32e4b4` |
| needs_ocr 50/500 阈值 | `p2-r0-rag-contract.md` §4.2 | frozen @ `b32e4b4` |
| frontmatter 示例（含 library_id / source_name / generated_at / 合并 parser_version） | `p2-r0-rag-contract.md` §4.3 | frozen @ `b32e4b4` |

### 2.2 R2-B 启动指令表述来源

| 字段 / 规则 | 主源 | 章节 |
|---|---|---|
| needs_ocr 仅零字符规则 | R2-B 启动指令 | §11 |
| 新 frontmatter 字段集（schema / parser_id 拆分 / source_filename / 无 library_id / 无默认 generated_at / title） | R2-B 启动指令 | §15 |
| frontmatter 字段校验规则 | R2-B 启动指令 | §17 |
| 禁止非确定性字段（含默认 generated_at） | R2-B 启动指令 | §16 |

### 2.3 冲突本质

P2-R0 在 R0 阶段冻结了 **结构性约束**（必须存在 frontmatter；page marker `<!-- page:N -->` 1-based；heading 层级）+ **示例字段集**。R2-B 启动指令提供了 **更精确的实现规范**（具体字段名 + JSON 序列化方式 + 安全 ID 拆分），与 R0 示例冲突。

按 memory feedback 与 P2-R0 §4 决策变更协议，对此类冲突必须走 amendment 流程，不得隐式选择一边。Amendment 2 完成此合规修订。

---

## 3. 字段决策工程理由

### 3.1 删除 `library_id`

**理由**：

1. **library_id 是 query-time 上下文，不是 evidence artifact**——用户在 Session 中查询时由后端从 `session_knowledge_libraries` 表求 allowlist；Canonical Markdown 作为 evidence 不应固化 library_id（同一 document 可能被多个 library 引用——虽然 R1 schema 中 document 属于单一 library，但 evidence artifact 应保持 library-agnostic）
2. **frontmatter 信息冗余**——`document_id` 已可定位 document；`library_id` 可由 DB 查询补充
3. **隐私 / 信息隔离**——用户分享 Markdown 文件时不应暴露 library_id

**R2-B 启动指令 §17 原文**：

> 默认不应将 library_id 写入用户可见 Markdown，除非 P2-R0 已冻结。

P2-R0 在 amendment-2 后不再冻结 library_id 进 frontmatter。

**下游影响**：见 §4.1。

### 3.2 `generated_at` 默认不写、仅显式传入

**理由**：

1. **确定性优先**——同一 PDF 重试 / 重 ingestion 应产生 byte-identical Canonical Markdown（同 SHA-256），便于 dedup / cache / audit
2. **Builder 不调用 `datetime.now()`**——避免时钟漂移导致 SHA 变化
3. **时间戳来源应是"事件发生时间"，不是"Markdown 构建时间"**——R2-C Orchestrator 已记录 `knowledge_documents.created_at` / `updated_at`（DB 字段，epoch ms），不需在 Markdown 内重复
4. **未来重 ingest**——若 generated_at 自动写入，重 ingest 时 SHA 会变，导致 `content_hash` 与 chunk 表关联断裂

**规则（amendment-2 §2.2 + 本审计 §6）**：

- Builder 默认不写 `generated_at`（field omitted）
- 仅当调用者（R2-C Orchestrator）显式传入 UTC ISO 8601 字符串时才写入
- Builder 不调用 `datetime.now()` / `time.time()` / `uuid4()` / `random.*`

**下游影响**：见 §4.2 + §6。

### 3.3 `source_name` → `source_filename` 重命名

**理由**：

1. **语义清晰**——`source_filename` 更准确反映"用户上传时的 basename"语义（与 `Document.source_name` DB 字段同义）
2. **DB 列名不变**——`knowledge_documents.source_name` 列名保持不变（R1 frozen）；仅 frontmatter 字段名解耦
3. **避免"source"歧义**——"source" 在不同上下文含义不同（"original PDF" vs "data origin" vs "code source"）；`source_filename` 明确指 basename

**下游影响**：见 §4.3。

### 3.4 `parser_id` / `parser_version` 拆分

**理由**：

1. **R2-A PdfExtractionResult 已有分离字段**——`parser_id: str` + `parser_version: str`；frontmatter 直接反映 R2-A 输出
2. **便于 parser 替换**——未来若 R3+ 评估 marker / PyMuPDF，frontmatter 中 `parser_id` 字段可机器识别 parser 类型，不需 parse 字符串
3. **稳定性**——`parser_id="pypdf"` 跨版本不变；`parser_version="6.14.2"` 随升级变；分离便于审计

**下游影响**：见 §4.3。

### 3.5 `schema` 字段新增

**理由**：

1. **版本化**——`"pi-agent-canonical-markdown/v1"` 允许未来 schema 变更（如 v2 引入新字段），消费者可机器识别
2. **明确 artifact 类型**——frontmatter 首字段即 schema，机器可立刻识别文档类型

**下游影响**：见 §4.3。

### 3.6 `title` 字段新增（可选）

**理由**：

1. **PdfMetadata 已有 title**（R2-A `PdfMetadata.title`，长度受限 + 单行）
2. **人类可读性**——frontmatter 中显示 PDF 标题便于人工审查
3. **可选**——为 None 时省略字段；不影响确定性

**下游影响**：见 §4.3。

### 3.7 needs_ocr "全文零字符"规则

**理由（R2-B 启动指令 §11 原文）**：

> digital PDF 可能本身只有少量文本；
> 不应将短文档误判为扫描件；
> 第一版 needs_ocr 只表示"完全没有可提取文本"；
> 低质量文档仍可生成 Markdown，并在后续检索阶段暴露 warning。

**变更**：

- 旧 §4.2: 每页 < 50 字符 或 整文档 < 500 字符 → needs_ocr
- 新 §4.2: 仅 `total_non_whitespace_chars == 0 → needs_ocr`
- 低密度文本作为 **warning**，不改变 usable 决策

**新增 warning codes**（不改变 usable 决策）：

- `low_non_empty_page_ratio`（non_empty/page < 0.20）
- `low_average_non_ws_chars`（avg < 20.0）
- `replacement_character_noise`（U+FFFD / total > 0.05）
- `control_character_noise`（non-\n/-\t control / total > 0.05）
- `many_empty_pages`（empty_count > 0 且 page_count > 1）

**下游影响**：见 §4.4。

---

## 4. 下游影响评估

### 4.1 R2-C Ingestion Worker

| 项 | 影响 |
|---|---|
| frontmatter 字段集 | R2-C Orchestrator 不传 `library_id`；不传 `generated_at`（默认）；可选传 `title`（来自 PdfMetadata） |
| needs_ocr 路径 | R2-C 检测 `quality.decision is NEEDS_OCR` → 不调用 Builder；Document 状态 `extracting → needs_ocr`（终态）；不写 document.md；不删 source.pdf |
| warning 路径 | R2-C 检测 `quality.warnings` → 仍写 document.md；warning 写入 `Document` 或 audit log（具体由 R2-C 决定） |
| generated_at 规则 | **见 §6**——MVP 不传 generated_at |
| Retry 行为 | 相同 PDF 重 ingest 应产生 byte-identical Markdown + 相同 SHA-256；R2-C 不应在每次 retry 时传入新 generated_at |

### 4.2 R3 Retrieval（Citation）

| 项 | 影响 |
|---|---|
| Page marker | `<!-- page:N -->` 1-based 单调递增；R3 Citation 使用 page marker 建立 page-level citation；amendment-2 后格式不变 |
| Chunking | Chunk 字段 `library_id / document_id / heading_path / page_start / page_end / content / content_hash / token_count` 不变（R0 §5 frozen） |
| library_id 来源 | Chunk 表 `library_id` 字段由 R3 写入（来自 `knowledge_documents.library_id` 查询），不从 frontmatter 读取——因此 frontmatter 删除 library_id 不影响 chunking |
| Evidence schema | `Evidence` 字段（chunk_id / library_id / document_id / heading_path / page_start / page_end / content / score）不变；`library_id` 仍由后端 allowlist 求出 |
| Citation 显示 | Citation 显示 `source_filename`（frontmatter） + page:N（page marker） + heading_path（chunk）——保持完整 |

**结论**：amendment-2 删除 library_id from frontmatter 不影响 R3 Citation——library_id 在 query-time 由后端注入，不从 artifact 读取。

### 4.3 R3 Retrieval（Search / Tool）

| 项 | 影响 |
|---|---|
| `search_knowledge` tool 签名 | `(query, top_k)` 不接受 library_id/session_id/path——不变 |
| Library allowlist | 后端从 `session_id_getter` 求 allowlist；不从 frontmatter 读取 library_id——amendment-2 不影响 |
| Parser identification | `parser_id` + `parser_version` 拆分后，R3 可机器识别 parser 类型用于 evidence 元数据 |

### 4.4 Session ACL（R4）

| 项 | 影响 |
|---|---|
| `session_knowledge_libraries` 表 | 字段不变（R1 frozen） |
| 后端 allowlist 过滤 | 不变 |
| frontmatter 删除 library_id | 不影响 ACL——library_id 始终由后端注入，从不从 user-visible Markdown 读取 |

**结论**：amendment-2 不影响 Session ACL。library_id 在 frontmatter 的存在与否是 evidence artifact 问题，不是权限问题。

### 4.5 R5 Web UI

| 项 | 影响 |
|---|---|
| Markdown preview | UI 渲染 frontmatter 时不再显示 library_id；显示 source_filename + page markers |
| 上传 UI | 用户上传后，frontmatter 由 R2-C Orchestrator 调用 Builder 写入；UI 不感知字段细节 |
| 渲染安全 | Canonical Markdown 是 evidence artifact，**不可信**——UI 渲染必须禁用 raw HTML 和远程图片（per R2-B 启动指令 §23）。amendment-2 不影响此项 |

---

## 5. 测试统计纠正

> **SUPERSEDED @ P2-R2-D-A**：本节关于 152 delta / 8-test discrepancy / H1-H3 假设的表述已被 P2-R2-D-A 完整 reconciliation 取代。8-test discrepancy 已通过 A/B 隔离 worktree + 集合分析关闭；根因为 freeze 时点数字误抄（2920 实际应为 2928）。详见 [`P2_R2_D_TEST_COUNT_RECONCILIATION.md`](P2_R2_D_TEST_COUNT_RECONCILIATION.md)。本节内容仅作历史诊断过程保留。

### 5.1 原始 R2-B 报告数学问题（SUPERSEDED）

R2-B freeze 文档（`P2_R2_B_CANONICAL_MARKDOWN.md`）原报告：

- R2-B targeted selection: 160 tests
- 完整 backend: 2768 (R2-A baseline) → 2920 (R2-B 后)
- 新增数: 152

数学差异：160 - 152 = **8 测试无法对账**。

→ **D-A 结论**：原报告的 2920 是 freeze 时点误抄；实测应为 2928。实际 delta = 2928 - 2768 = 160 = targeted 160 ✅。

### 5.2 实际计数核实（unchanged — D-A 复现确认）

| 测试文件 | `def test_` 函数数 | pytest `--collect-only` 数 | 单独运行结果 |
|---|---|---|---|
| `tests/test_pdf_quality.py` | 43 | 43 | 43 passed |
| `tests/test_canonical_markdown.py` | 80 | 80 | 80 passed |
| `tests/test_markdown_persistence.py` | 29 | 29 | 29 passed |
| `tests/test_r2_b_integration.py` | 8 | 8 | 8 passed |
| **总计** | **160** | **160** | **160 passed** |

→ D-A 在 R2-B worktree（`eb193b2`）复现确认：targeted = 160 ✅。

### 5.3 Backend 报告 2920 的诊断（SUPERSEDED — root cause FOUND in D-A）

完整 backend 运行（`pytest tests/ -m "not slow and not integration and not docker" --no-cov`）报告 **2920 passed + 2 skipped + 14 deselected**。collect-only 显示 **2930/2944**（2944 总 / 14 deselected / 2930 应运行）。

实际运行 2922（2920 passed + 2 skipped），与 collect 2930 差 **8 测试**。

诊断（原 audit §5.3，**已被 D-A 取代**）：

- 不是 R2-A baseline 错误（R2-A 报告 2768；本审计期间 git diff 确认 R2-B 未修改任何 R2-A 既有测试文件）
- 不是 R2-B 测试本身错误（targeted 单独运行 160/160 PASS）
- 不是 marker filter 排除（默认 marker 包含 R2-B 测试）

可能根因（**SUPERSEDED — D-A 已证明全部不成立**）：

1. ~~**假设（H1）**：R2-B 期间 ruff `--fix --unsafe-fixes` 可能合并 / 重构了少量既有测试 helper~~ → D-A `git diff --name-status 0772324..eb193b2 -- tests` 显示 0 Modified / 4 Added，否决
2. ~~**假设（H2）**：pytest collection 在某些环境下对 fixture-driven 测试去重~~ → D-A A/B collect-only 全部对账（2770/2930），否决
3. ~~**假设（H3）**：R2-A 报告的 2768 可能在不同 pytest 会话存在 ±8 的轻微计数漂移~~ → D-A R2-A worktree 实测 = 2768 与文档完全一致，否决

**D-A 根因结论（2026-08-07）**：freeze 时点（`eb193b2`）完整 backend 实测 **2928 passed + 2 skipped + 14 deselected**（worktree @ `eb193b2`，2026-08-07）。原 audit §5.3 报告的 2920 是 freeze 时点数字抄写错误。8-test discrepancy **从未在 node-ID 层存在**——它是 reported 数字错误导致的虚构差异。

### 5.4 纠正后的统计口径（CORRECTED @ P2-R2-D-A）

```
R2-B targeted selection        160 passed  (43 + 80 + 29 + 8)
New node IDs added by R2-B     160         (纯新增；git diff 0 Modified / 0 Deleted)
Backend reported delta         160         (2768 → 2928；实测对账)
Functional regression            0         (按 targeted 套件 + 默认 marker 完整跑 0 fail)
Historical 8-test discrepancy    0         (RECONCILED @ P2-R2-D-A)
```

具体见 `P2_R2_B_CANONICAL_MARKDOWN.md §22 / §25`（已同步更新）+ [`P2_R2_D_TEST_COUNT_RECONCILIATION.md`](P2_R2_D_TEST_COUNT_RECONCILIATION.md)。

### 5.5 后续 follow-up（✅ CLOSED @ P2-R2-D-A）

~~8-test discrepancy = KNOWN NON-BLOCKING，但 MUST RECONCILE IN R2-D~~

**P2-R2-D-A 已完成此 5 项 follow-up**：

1. ✅ `pytest --collect-only` 数量：A=2770 / B=2930 / delta=160 = targeted
2. ✅ 实际执行数量：A=2770（2768 passed + 2 skipped） / B=2930（2928 passed + 2 skipped）
3. ✅ `skipped` / `deselected`：A=B=2 skipped / 14 deselected；delta=0
4. ✅ pytest 配置 / 插件 / marker：A 与 B worktree pyproject.toml `[tool.pytest.ini_options]` 完全相同
5. ✅ collection 后未执行的测试项：无（实测 selected = passed + skipped；A/B 都对账）

文档中关于 Ruff `--fix --unsafe-fixes` 自动修改或 pytest fixture 去重的内容，**已在 D-A 中被 git diff + 集合分析否决**。

---

## 6. `generated_at` 在 R2-C 的确定性规则

### 6.1 不变量

```
1. 默认不写（R2-C Orchestrator 不传 generated_at → Builder 省略字段）
2. Builder 不调用 datetime.now() / time.time() / uuid4() / random.*
3. 仅当调用者显式传入 UTC ISO 8601 字符串时才写入
4. 相同 (source, extraction, quality, title, generated_at) 输入 → byte-identical content + 相同 content_sha256
```

### 6.2 R2-C MVP 强制规则

**R2-C Orchestrator MVP 不传 `generated_at`**。

理由：

1. **重 ingest 确定性**——相同 PDF 重 ingest 必须产生相同 Canonical Markdown + 相同 SHA-256，便于：
   - dedup（同 SHA 不重复 chunk）
   - cache（同 SHA 复用 chunking 结果）
   - audit（同 PDF 不同时刻 ingest → byte-identical artifact）
2. **DB 已有时间戳**——`knowledge_documents.created_at` / `updated_at`（epoch ms）已记录事件时间；不需 frontmatter 重复
3. **避免 R2-C 实现复杂度**——若传 generated_at，需考虑 retry / dedup / 时间源一致性，超出 MVP 范围

### 6.3 何时考虑传入 generated_at

仅在以下场景考虑（**R2-C MVP 范围外**）：

- 用户明确要求 evidence artifact 包含构建时间戳
- 法规 / 合规要求 evidence 必须有时间标记
- 公证 / 第三方审计场景

如启用，必须：

- 时间源由 R2-C Orchestrator 显式传入（不来自 Builder 内部）
- UTC ISO 8601 格式
- 同一 ingestion job 内不重传（避免 retry 时 SHA 变化）
- 由用户独立授权

---

## 7. 用户追认状态（✅ RATIFIED @ 2026-08-03）

### 7.1 追认门

本审计列出已确认的事实：

1. ✅ `15b411b` 授权链完整（用户 AskUserQuestion 选择"走 amendment-2 流程"）
2. ✅ 冲突源已精确引用（§2）
3. ✅ 字段决策工程理由已记录（§3）
4. ✅ 下游影响已评估（§4）—— R2-C / R3 Citation / R3 Search / Session ACL / R5 UI 均不受 amendment-2 影响
5. ✅ 测试统计已纠正（§5）
6. ✅ `generated_at` R2-C 规则已明确（§6）

### 7.2 用户决策（2026-08-03）

> **User decision: A — RATIFIED**
>
> Amendment 2: **APPROVED**
> R2-B Archive Closure: **COMPLETE**
> R2-C MVP `generated_at`: **MUST NOT BE PASSED**（确定性优先；同 PDF 重 ingest 必须 byte-identical SHA-256）
> 8-test discrepancy: **NON-BLOCKING**（不阻塞 R2-C），**MUST RECONCILE IN R2-D**（详见 §5.5）

用户在审阅本审计 §1–§6 后明确追认所有决策，包括：

- `needs_ocr = total_non_whitespace_chars == 0`；低密度文本作 warning 不改变决策（避免短文本数字 PDF 被误判为扫描件）
- Canonical Markdown frontmatter 字段集（schema / document_id / source_filename / source_sha256 / parser_id / parser_version / page_count / title?/ generated_at?）
- 删除 `library_id`（query-time 上下文；DB / Session allowlist 提供；不削弱 Session ACL / R3 Citation / R3 Search）
- `source_name` → `source_filename`（artifact 层解耦；DB 列名不变）
- `parser_id` + `parser_version` 分离（与 R2-A `PdfExtractionResult` 一致）
- 新增 Canonical Schema 标识（`"pi-agent-canonical-markdown/v1"`）
- `title` 可选字段

### 7.3 追认后阶段门

```
P2-R2-A Parser Adapter                 ✅ FROZEN @ 0772324
P2-R2-B Canonical Markdown Builder     ✅ COMPLETE / FROZEN @ eb193b2
P2-R2-B Contract/Archive Closure       ✅ COMPLETE @ <ratification commit>（User decision: A — RATIFIED）
P2-R2-C Ingestion Worker + API         ✅ APPROVED TO START（独立启动授权另需用户发起）
P2-R2-D Integration Validation         ⛔ BLOCKED BY R2-C
P2-R3 Retrieval MVP                    ⛔ BLOCKED BY COMPLETE P2-R2
```

---

## 8. Audit Gate

| # | Audit Item | Status |
|---|---|---|
| 1 | `15b411b` 授权链文档化 | ✅ §1 |
| 2 | 冲突源精确引用 | ✅ §2 |
| 3 | 字段决策工程理由（7 项） | ✅ §3 |
| 4 | R2-C 影响评估 | ✅ §4.1 |
| 5 | R3 Citation 影响 | ✅ §4.2 |
| 6 | R3 Search 影响 | ✅ §4.3 |
| 7 | Session ACL 影响 | ✅ §4.4 |
| 8 | R5 UI 影响 | ✅ §4.5 |
| 9 | 测试统计纠正 | ✅ §5 |
| 10 | generated_at R2-C 规则 | ✅ §6 |
| 11 | 用户追认门 | ✅ §7（User decision: A — RATIFIED @ 2026-08-03） |

---

## 9. Working Tree / G1 stash

- working tree clean（本提交前）
- G1 stash hash `d7240268ec8b8e5d9e195c999e56fb6ec130fd55` 未变（跨 R2-B 全程）
- production / test / dependency / lockfile / schema / frontend diff = 0（本审计 docs-only）

---

本审计 docs-only；不动任何生产代码 / 测试 / 依赖 / lockfile / schema / frontend。

**User decision: A — RATIFIED @ 2026-08-03**（详见 §7.2）。Amendment 2 APPROVED；R2-B Archive Closure COMPLETE；R2-C 编码门开启（独立启动授权另需用户发起）；R2-D 必查 8-test discrepancy。

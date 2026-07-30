# P2-R0 — Amendment 1（R1 范围重划：marker 集成推迟到 R2）

> **阶段**：P2-R0 决策变更（docs-only amendment）
> **基线 commit（amendment 之前）**：`cab116f` — chore(dev): add local web development launcher
> **amendment 日期**：2026-07-30
> **范围**：按 P2-R0 decisions-log §4 决策变更协议，把 marker 集成 + 数字 PDF→MD smoke + 扫描 PDF→needs_ocr 测试从 R1 范围移到 R2 范围。R1 缩窄为 "Library Foundation only"。
> **触发**：用户 P2-R1 启动指令 §1 / §5 / §27 / §28 与 P2-R0 §1.3 + §7 + ROADMAP 行 215 在 R1 是否含 PDF parser 上直接冲突——用户选择走 amendment 流程解决（而非隐式违反冻结）。

---

## 1. 变更背景

### 1.1 冲突事实

P2-R0 已冻结的 R1 范围包含 PDF parser：

- `p2-r0-rag-contract.md` §1.3 R1 行：产出 = "7 表 DDL + migration + KnowledgeFileStore + **marker 集成**"，验证 = "**数字 PDF→MD smoke / 扫描 PDF→needs_ocr**"
- `p2-r0-rag-contract.md` §7 R1 入口条件：包含 "marker 集成（或 pypdf fallback）" + "数字 PDF → Canonical MD smoke test" + "扫描 PDF → status=needs_ocr 终态测试"
- `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md` §7 同上
- `ROADMAP.md:215` R1 行：同上
- `ROADMAP.md:224`：标注 "R1 集成前需法务确认 license 兼容性"

用户 P2-R1 启动指令明确禁止 R1 含 PDF parser：

- §1 唯一目标："本阶段只解决：'库如何存在、文件放在哪里、Session 能访问哪些库'。本阶段不解决：'PDF 如何解析、文档如何切块、Agent 如何检索'。"
- §5 严格范围："P2-R1 禁止实现：... PDF parser / PyMuPDF / pypdf dependency / ingestion worker / ingestion queue / PDF → Markdown / Canonical Markdown 生成"
- §27 Exit Gate：要求 R1 完成后 "PDF parser 依赖 0；PDF 处理代码 0"
- §28 阻塞规则："出现以下情况立即停止：... 必须实现 PDF parser 才能完成 R1"

### 1.2 用户决策

用户通过 AskUserQuestion 选择"走 P2-R0 amendment 流程"——按 P2-R0 decisions-log §4 协议合规修订 R1 范围冻结，**不**通过隐式违反 P2-R0 §1.3 的方式直接实施。

---

## 2. 变更内容

### 2.1 R1 范围缩窄（amendment 后）

**R1 = Library Foundation only**：

- 5 张表 DDL + migration（独立 `knowledge.db`）
- `KnowledgeFileStore`（atomic write + fsync + path containment + symlink 防护）
- Library 元数据 Store / Service
- Document 元数据 Store / Service（**仅** metadata，**不**含 PDF 解析）
- Session Library Binding Store / Service（`session_knowledge_libraries`）
- Library CRUD REST API
- Session Library Binding REST API
- 合同允许的 Document 只读 / 删除 API（仅 metadata 维度，**不**触发 ingestion）
- startup schema 初始化 + restart 恢复
- 删除补偿（DB + 文件系统）

**R1 显式不包含**：

- PDF parser（marker / pypdf / PyMuPDF / pdfplumber / fitz）
- 数字 PDF → Canonical MD 转换
- 扫描 PDF → `needs_ocr` 状态判定
- heading-aware chunker
- FTS5 检索
- `search_knowledge` tool
- ingestion worker / queue
- 前端 Library 管理 UI

### 2.2 R2 范围扩展（吸收 R1 推迟项）

**R2 = PDF Ingestion Pipeline**：

- marker 集成（或 pypdf fallback）—— 含 AGPL license 兼容性确认（原 R1 入口条件）
- Canonical MD writer（YAML frontmatter + page marker + heading）
- heading-aware chunker（max_chars=1200 / overlap=150）
- Job 状态机（extract / normalize / chunk / index）
- 30s 同步阈值 + PDF ≤ 20 页强制限制
- 数字 PDF → Canonical MD smoke test（原 R1 验证）
- 扫描 PDF → `status=needs_ocr` 终态测试（原 R1 验证）
- PDF upload endpoint（合同 §17 归于 R2）
- retry ingestion endpoint（合同 §17 归于 R2）

### 2.3 不变的已冻结决策

下列 P2-R0 冻结**保持不变**：

- F1 数据模型 5 张表结构（§2.2 DDL 字段不变）
- F2 文件系统布局 `data/knowledge/libraries/{library_id}/documents/{document_id}/{source.pdf, document.md, manifest.json}`
- F3 PDF 边界（仅数字 PDF；扫描 PDF 进 needs_ocr；不支持 OCR / 表单 / 批注 / 嵌入对象）
- F4 Canonical Markdown 格式（YAML frontmatter + page marker + heading）
- F5 Chunk 格式字段
- F6 Session Library ACL（Session → Library Allowlist，非 User RBAC）
- F7 Tool 接口（`search_knowledge(query, top_k)` 不接受 library_id/session_id/path）
- F8 同步处理策略（第一版无后台任务调度）
- R1 决策：marker parser 选型（force_ocr=False）
- R2 决策：独立 knowledge.db
- R3 决策：30s 同步阈值 + ≤ 20 页限制
- R4 决策：Chunk max_chars=1200 / overlap=150
- R5 决策：独立 KnowledgeFileStore（不复用 VirtualFileStore）

只变更 R1 / R2 的**范围归属**，不变更底层技术决策。

---

## 3. 文档同步清单

本 amendment 落地需同步修改以下文档（**仅 docs**，不动 src/tests）：

| 文档 | 章节 | 修改类型 |
|---|---|---|
| `p2-r0-rag-contract.md` | §1.3 R1 行 | 产出列去 marker；验证列去 PDF smoke/needs_ocr |
| `p2-r0-rag-contract.md` | §1.3 R2 行 | 产出列加 marker；验证列加 PDF smoke/needs_ocr |
| `p2-r0-rag-contract.md` | §7 R1 范围 | 改为 Library Foundation only；marker 移到 R2 范围说明 |
| `p2-r0-rag-contract.md` | §7 R1 入口条件 | 删除 marker AGPL 兼容性入口条件（移到 R2） |
| `p2-r0-decisions-log.md` | §1 表后 | 加 amendment 说明 + 指向本文件 |
| `p2-r0-decisions-log.md` | §1 F1 行 | 标注 `[AMENDED 2026-07-30 — 见 amendment-1]`（F1 内容不变，仅 R1 范围归属变） |
| `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md` | §7 R1 入口条件 | 同步移除 marker 入口条件 |
| `ROADMAP.md` | 行 215 (R1) + 行 216 (R2) | R1/R2 产出 + 验证同步 |
| `TODO.md` | P2-R 段 | P2-R1 描述同步 |

---

## 4. R0 回归验证（amendment 后）

amendment 是 P2-R0 决策变更，按 decisions-log §4 第 4 条 "跑回归测试确认前置 milestone 未受影响"——重新跑 R0 7 条 checklist：

| # | Check | amendment 后状态 |
|---|---|---|
| 1 | `Vector RAG` / `Vector Memory` 残留扫描 | ✅ amendment 不改 README/ROADMAP/TODO 旧标记段，仍 0 hit |
| 2 | 合并字符串拆分验证 | ✅ 不变 |
| 3 | 4 份新文档存在 | ✅ amendment 不删原 4 份文档（amendment-1.md 是第 5 份） |
| 4 | 决策表完整性 F1-F8 + R1-R5 | ✅ F1 加 `[AMENDED]` 标注但行不删；新增 amendment-1 引用 |
| 5 | ROADMAP P2-R 章节 + R0~R6 占位 | ✅ amendment 不删 R0~R6 占位 |
| 6 | 关键决策显式记录 | ✅ marker / knowledge.db / KnowledgeFileStore / force_ocr=False 仍出现 |
| 7 | legacy-limit-audit 行号引用核对（基线 ee62732） | ✅ amendment 不动 legacy-limit-audit.md |

amendment 不破坏 R0 已 PASS 的 7 条 checklist。

---

## 5. amendment 后的 R1 入口条件（替代 P2-R0 §7）

R1 启动前必须满足：

1. ✅ R0 全部 7 条 checklist PASS（基线 commit `b32e4b4`）
2. ✅ R0 commit 已落地（不在 plan mode / 工作树未提交）
3. ✅ **本 amendment 已落地**（docs-only commit）
4. ⛔ **R1 启动授权**（用户独立授权）

R1 范围（amendment 后，详见 ROADMAP §P2-R / contract §1.3）：

- 5 张表 DDL + migration（独立 `knowledge.db`）
- `KnowledgeFileStore`（atomic write + fsync + path containment）
- Library 元数据 Store / Service / CRUD API
- Document 元数据 Store / Service（**仅 metadata**）
- Session Library Binding Store / Service / API
- startup schema 初始化 + restart 恢复

**R1 显式不包含**：marker 集成 / PDF 解析 / Canonical MD 生成 / chunker / FTS5 / `search_knowledge` tool / 前端 UI（全部归 R2-R5）。

---

## 6. amendment 落地出口

- ✅ 本文件（amendment-1.md）存在
- ✅ `p2-r0-rag-contract.md` §1.3 / §7 已同步
- ✅ `p2-r0-decisions-log.md` §1 已标注 amended
- ✅ `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md` §7 已同步
- ✅ `ROADMAP.md` 行 215 / 216 已同步
- ✅ `TODO.md` P2-R 段已同步
- ✅ R0 7 条 checklist 仍 PASS
- ✅ production / test / dependency / schema diff = 0
- ⛔ 不动 git tag / branch / 不 merge / 不 push

amendment 后 P2-R1 可按用户 P2-R1 指令合规启动（不再有合同冲突）。

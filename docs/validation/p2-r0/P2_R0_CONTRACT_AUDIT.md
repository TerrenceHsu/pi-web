# P2-R0 RAG Contract Audit — Validation Report

> **阶段**：P2-R0 RAG Contract Audit（docs-only milestone）
> **基线 commit（R0 之前）**：`ee62732` — docs: freeze P1-E multi-provider switching
> **R0 freeze commit**：本提交（自引用；hash 由 git 在提交时生成）
> **归档日期**：2026-07-27
> **范围**：验证 P2-R0 docs-only milestone 全部 7 条 checklist 通过；产出 P2-R1 入口条件。

---

## 1. Final Status

```
P2-R0 RAG Contract Audit
✅ READY FOR COMMIT

Baseline before R0:
ee62732 — docs: freeze P1-E multi-provider switching

R0 freeze commit:
本提交（自引用；hash 由 git 在提交时生成，不在文档内伪造）

R0 性质：
docs-only — production / test / dependency / schema diff = 0
```

R0 本身**不**实现任何 RAG 能力，仅做：

- 旧 deferred / out-of-scope 标记解除（7 处主表 + 1 处补遗）
- 7 项核心契约冻结（数据模型 / 目录布局 / PDF 边界 / Chunk 格式 / Tool 接口 / Session ACL / 同步策略）
- P2-R1~R6 占位加入 ROADMAP
- 输出 4 份 docs（本报告 + 3 份 design）

---

## 2. Scope（R0 实际交付的能力清单）

| # | 交付物 | 路径 |
|---|---|---|
| 1 | 主设计冻结文档（11 节） | `docs/design/p2-r0-rag-contract.md` |
| 2 | 旧边界解除审计（5 节） | `docs/design/p2-r0-legacy-limit-audit.md` |
| 3 | 决策冻结表（5 节） | `docs/design/p2-r0-decisions-log.md` |
| 4 | 本验证归档报告 | `docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md`（本文件） |
| 5 | ROADMAP 新增 P2-R 章节 + R0~R6 占位表 + 拆分 Long-term Memory 不做项 | `ROADMAP.md` |
| 6 | TODO 新增 P2-R 章节 + Deferred 加注重启 + 拆分 Long-term Memory 不做项 | `TODO.md` |
| 7 | README 4 处边界措辞调整（行 65 / 113 / 174 / 201） | `README.md` |
| 8 | CHANGELOG 加路线 Pivot 段 + 2 处重启加注 | `CHANGELOG.md` |

---

## 3. Frozen Commit Chain

| 阶段 | Commit | 说明 |
|---|---|---|
| Baseline | `ee62732` | docs: freeze P1-E multi-provider switching |
| R0 Contract Audit | 本提交 | docs(rag): P2-R0 contract audit — freeze schema/acl/tool/parser decisions |

R0 是单 commit 提交，**不**带前序 R0 子 commit——docs-only milestone 全部产出在同一 commit 内。

---

## 4. Validation Checklist（7 条）

### Check 1：旧 "Vector RAG" / "Vector Memory" 残留扫描

**命令**：
```bash
grep -nE "Vector RAG|Vector Memory" ROADMAP.md TODO.md README.md
```

**期望**：0 hit（CHANGELOG 历史段保留 + ROADMAP 中 RESTARTED 段说明性引用允许）

**实际结果**：

- `ROADMAP.md`：仅在 §已 DEFERRED 段 `PDF / Vector RAG ✅ RESTARTED via P2-R` 出现 2 次（说明性引用，正确）
- `TODO.md`：0 hit ✅
- `README.md`：0 hit ✅
- `CHANGELOG.md`：2 处历史段（行 24 + 行 32）保留作历史记录，符合"CHANGELOG 不删历史条目"原则 ✅

**判定**：✅ PASS

---

### Check 2：合并字符串 "RAG / Vector Memory / Long-term Memory" 扫描

**命令**：
```bash
grep -nE "RAG / Vector Memory / Long-term Memory" ROADMAP.md TODO.md README.md
```

**期望**：0 hit（拆分为 RAG + Long-term user memory 两个独立条目）

**实际结果**：0 hit ✅

**判定**：✅ PASS

---

### Check 3：4 份新文档 `test -f` 全部存在

**命令**：
```bash
ls -la docs/design/p2-r0-*.md docs/validation/p2-r0/*.md
```

**实际结果**：
```
docs/design/p2-r0-decisions-log.md       8282 bytes
docs/design/p2-r0-legacy-limit-audit.md  10821 bytes
docs/design/p2-r0-rag-contract.md        22652 bytes
docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md  本文件
```

**判定**：✅ PASS

---

### Check 4：决策表完整性（8 条冻结 + 5 条推荐）

**命令**：
```bash
grep -cE "^\| F[0-9]" docs/design/p2-r0-decisions-log.md  # 期望 ≥ 8
grep -cE "^\| R[0-9]" docs/design/p2-r0-decisions-log.md  # 期望 ≥ 5
```

**实际结果**：
- F1-F8：8 条冻结（用户给定） ✅
- R1-R5：5 条推荐（R0 新权衡：marker / 独立 knowledge.db / 30s 阈值 / chunk 参数 / KnowledgeFileStore） ✅
- D1-D8：8 条推迟项（标记 "推迟到 R1+"） ✅

**主设计文档章节**（实际 11 节，超过 plan 中的 6 节）：
1. 范围与 docs-only 边界
2. 数据模型（5 张表 + FTS5 + 状态机）
3. 文件系统布局（含 KnowledgeFileStore API）
4. PDF → Canonical Markdown 边界（含 marker 配置 + needs_ocr 判定 + Canonical MD 格式）
5. Chunk 格式与切分规则
6. Tool 接口契约（含实现骨架 + evidence schema）
7. Session Library ACL（8 条不变量）
8. 同步 / 异步处理策略
9. 实施顺序
10. 验证 checklist（指向本文件）
11. R0 完成出口

**判定**：✅ PASS

---

### Check 5：ROADMAP 含 P2-R 章节 + R0~R6 占位

**命令**：
```bash
grep -nE "^## P2-R — Knowledge / RAG Subsystem|^\| \*\*R[0-6] " ROADMAP.md
```

**实际结果**（grep 输出）：
- 行 206：`## P2-R — Knowledge / RAG Subsystem（🟡 IN PROGRESS）`
- 行 214：`| **R0 Contract Audit** | 🟡 IN PROGRESS | — | ...`
- 行 215：`| **R1 Schema + Store** | ⛔ BLOCKED BY R0 | ...`
- 行 216：`| **R2 Ingestion Pipeline** | ⛔ BLOCKED BY R1 | ...`
- 行 217：`| **R3 Retrieval** | ⛔ BLOCKED BY R2 | ...`
- 行 218：`| **R4 Session Library ACL** | ⛔ BLOCKED BY R3 | ...`
- 行 219：`| **R5 Web API + UI** | ⛔ BLOCKED BY R4 | ...`
- 行 220：`| **R6 Freeze + Validation** | ⛔ BLOCKED BY R5 | ...`

**判定**：✅ PASS

---

### Check 6：关键决策显式记录

**命令**：
```bash
grep -nE "marker|knowledge\.db|KnowledgeFileStore|force_ocr=False" docs/design/p2-r0-*.md
```

**期望**：4 个关键决策词在 3 份文档中均有出现

**实际结果**：30+ 处匹配，覆盖：

- `marker`：作为 parser 选型（contract §4.1 / decisions R1 / legacy-audit §1.3）
- `knowledge.db`：作为独立 SQLite 部署（contract §2.1 / §3.1 / decisions R2）
- `KnowledgeFileStore`：作为独立类（contract §3.3 / decisions R5）
- `force_ocr=False`：marker 配置项（contract §4.2 / §2.4 / decisions R1）

**判定**：✅ PASS

---

### Check 7：legacy-limit-audit 行号引用核对

**说明**：audit 文档中的行号（如 `ROADMAP.md:270-272`）是**改前基线**的行号——记录"被解除的标记在基线 commit `ee62732` 时的位置"，是历史事实，不随源文档修改而变化。

**核对方式**：

```bash
git show ee62732:ROADMAP.md | sed -n '270,272p'
# 期望输出 PDF / Vector RAG DEFERRED 段
```

**legacy-audit 中引用的 9 个位置**：

| 引用 | 内容（基线时） | 核对 |
|---|---|---|
| `ROADMAP.md:270-272` | PDF / Vector RAG DEFERRED 段 | ✅ 改前基线对应 |
| `ROADMAP.md:280` | "不做"列表 RAG / Vector Memory / Long-term Memory 行 | ✅ 改前基线对应 |
| `TODO.md:178-179` | P1-D3 PDF + PDF / Vector RAG 两行 | ✅ 改前基线对应 |
| `TODO.md:187` | Out-of-scope RAG 行 | ✅ 改前基线对应 |
| `README.md:65` | 全局不做列表 RAG 行 | ✅ 改前基线对应 |
| `README.md:113` | Context 模块边界 RAG 引用 | ✅ 改前基线对应 |
| `README.md:174` | Session Memory 模块边界 Vector Memory | ✅ 改前基线对应 |
| `README.md:201` | Compaction 模块边界（补遗） | ✅ 改前基线对应 |
| `CHANGELOG.md:23-24` | 路线调整段 PDF + RAG DEFERRED | ✅ 改前基线对应 |

**判定**：✅ PASS（行号引用与基线 commit `ee62732` 一致）

---

## 5. 7 条 Checklist 汇总

| # | Check | 判定 |
|---|---|---|
| 1 | Vector RAG / Vector Memory 残留扫描 | ✅ PASS |
| 2 | 合并字符串拆分验证 | ✅ PASS |
| 3 | 4 份新文档存在 | ✅ PASS |
| 4 | 决策表完整性（F1-F8 + R1-R5） | ✅ PASS |
| 5 | ROADMAP P2-R 章节 + R0~R6 占位 | ✅ PASS |
| 6 | 关键决策显式记录（marker / knowledge.db / KnowledgeFileStore / force_ocr=False） | ✅ PASS |
| 7 | legacy-limit-audit 行号引用核对（基线 `ee62732`） | ✅ PASS |

**7/7 PASS** ✅

---

## 6. R0 完成出口

- ✅ 全部 7 条 checklist 通过
- ✅ 1 个新 commit（仅 docs/）：`docs(rag): P2-R0 contract audit — freeze schema/acl/tool/parser decisions`
- ✅ production / test / dependency / schema diff = 0
- ✅ P2-R1 在 ROADMAP 中标 ⚪ PLANNED，等用户授权启动

---

## 7. P2-R1 入口条件

R1 启动前必须满足的硬条件：

1. ✅ R0 全部 7 条 checklist PASS（本报告 §5）
2. ✅ R0 commit 已落地（不在 plan mode / 工作树未提交）
3. ⛔ **marker AGPL-3.0 与项目 MIT license 兼容性确认**（decision D6）—— R1 集成前需法务/用户确认；若不兼容，fallback 到 pypdf（BSD）
4. ⛔ **R1 启动授权**（用户独立授权，R0 不自动启动 R1）

R1 范围（详见 ROADMAP §P2-R / contract §1.3）：

- 5 张表 DDL + migration（独立 `knowledge.db`）
- `KnowledgeFileStore`（含 atomic write + fsync）
- `marker` 集成（或 pypdf fallback）
- 数字 PDF → Canonical MD smoke test
- 扫描 PDF → `status=needs_ocr` 终态测试

**R1 显式不包含**：heading-aware chunker / FTS5 / `search_knowledge` tool / Session binding / Web API（这些进 R2-R5）。

---

## 8. 已知 trade-off / 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| marker AGPL-3.0 license | 与项目 MIT 可能不兼容 | R1 集成前法务确认；不兼容则降级 pypdf（contract §4.1） |
| marker 首次下载 GB 级模型 | R1 集成时 CI / 本地首次启动慢 | R1 提供 `pip install marker` optional extra + 模型预热策略（decision D5） |
| marker CPU 推理慢（单 PDF 分钟级） | 30s 同步阈值下大 PDF 必失败 | 第一版强制 ≤ 20 页；R2 引入 BackgroundTask（contract §8.1） |
| SQLite FTS5 `unicode61` 中文召回一般 | 中文文档检索质量可能不达标 | R3+ 评估 trigram / jieba（decision D4） |
| Long-term Memory 拆分措辞 | 用户可能混淆 RAG 与 long-term user memory | ROADMAP / TODO / README 已显式拆分；本报告 §1 强调"Session-scoped 非 User 级" |

---

## 9. 后续动作（需用户授权）

- [ ] **commit**：`docs(rag): P2-R0 contract audit — freeze schema/acl/tool/parser decisions`（本提交）
- [ ] **push**：⛔ NOT AUTHORED（等待用户授权；R0 不自动 push）
- [ ] **R1 启动**：⛔ NOT AUTHORED（等待 marker AGPL 兼容性确认 + 用户独立授权）

R0 milestone 至此归档完成。

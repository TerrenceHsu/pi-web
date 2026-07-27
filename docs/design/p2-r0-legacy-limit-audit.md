# P2-R0 — Legacy Limit Audit（RAG 旧边界解除记录）

> **阶段**：P2-R0 RAG Contract Audit（docs-only）
> **基线 commit（R0 之前）**：`ee62732` — docs: freeze P1-E multi-provider switching
> **日期**：2026-07-27
> **范围**：逐条审计仓库内所有"RAG / Vector Memory / Long-term Memory / PDF Text Extraction"相关的 deferred / out-of-scope 标记，给出解除或保留的判定 + 理由。本文件是 [p2-r0-rag-contract.md](p2-r0-rag-contract.md) 的 Phase 1 事实依据。

---

## 0. 为什么先做这份审计

P1-E Multi-Provider Switching 已 FROZEN（commit `ee62732`），下一阶段路线 Pivot 到本地知识库 RAG（P2-R 系列）。但仓库内**多处旧边界声明**与新目标直接冲突——若不先精确解除，会出现：

- 决策未冻结，实现过程中数据模型 / Tool 接口 / ACL 边界频繁回退
- 旧限制与新代码并存，reviewer 无法判断"是漏洞还是回归"
- 后续 validation 报告无法引用统一的边界基线

本审计**仅做事实记录**，不修改任何源文档——源文档修改在 R0 主流程中一次性完成（见 [p2-r0-rag-contract.md §8 实施顺序](p2-r0-rag-contract.md)）。

---

## 1. 必须解除的旧标记（7 处主表 + 1 处补遗）

### 1.1 `ROADMAP.md:270-272` — PDF / Vector RAG DEFERRED 段

**原文**：
```
### PDF / Vector RAG ⏸ DEFERRED

明确延后到 P2 候选完成之后。不引入向量存储、不实现长文档检索。
```

**判定**：✅ **解除** —— 删除整段，替换为指向 [p2-r0-rag-contract.md](p2-r0-rag-contract.md) 的链接并加入 P2-R 系列。

**理由**：用户已批准 P2-R 路线 Pivot，RAG 不再"延后到 P2 候选完成之后"，而是**取代**原 P2 候选成为下一阶段主线。

---

### 1.2 `ROADMAP.md:280` — "不做"列表中的 RAG 条目

**原文**：
```
- RAG / Vector Memory / Long-term Memory
```

**判定**：✅ **解除 + 拆分**：
- 移除 `RAG / Vector Memory`（P2-R 系列引入）
- `Long-term Memory` **保留并拆为独立条目**，标注"仍不做（区别于 RAG）"——P2-R 仅做 Session-scoped Library ACL，不引入跨 Session 的用户级长期记忆

**理由**：第一版 RAG 的 ACL 模型是 `Session → Library Allowlist`，明确**不是** `User → Role → Permission`（详见 [p2-r0-rag-contract.md §7](p2-r0-rag-contract.md)）。Long-term user memory 仍属不做。

---

### 1.3 `TODO.md:178-179` — Deferred 列表中的两条 PDF/RAG 行

**原文**：
```
- P1-D3 PDF Text Extraction（转出主路线，重启条件见 ROADMAP）
- PDF / Vector RAG
```

**判定**：
- 行 178 P1-D3 PDF Text Extraction：✅ **保留但加注** "P2-R 系列重启（marker 作为 parser）"
- 行 179 PDF / Vector RAG：✅ **删除**

**理由**：P1-D3 的原始范围（"PDF adapter 边界 + FileRef metadata 字段"）已被 P2-R 系列吸收并扩展——P2-R2 用 marker 实现 PDF→Canonical MD，不再走原 P1-D3 设计的 `tools/view_file.py` 改造路径。但 P1-D3 的历史归档不删，加注指明重启位置即可。

---

### 1.4 `TODO.md:187` — Out-of-scope 列表中的 RAG 条目

**原文**：
```
- RAG / Vector Memory / Long-term Memory
```

**判定**：✅ **解除 + 拆分** —— 与 §1.2 ROADMAP 同步处理。

---

### 1.5 `README.md:65` — 全局不做列表的 RAG 行

**原文**：
```
- **RAG / Vector Memory / Long-term Memory**——只做对话级 messages + Session 级 snapshots，不接 embedding / 向量检索
```

**判定**：✅ **改写**为：
```
- **RAG**——P2-R 系列引入（marker + heading-aware chunk + SQLite FTS5 + Session-scoped Library ACL）；不引入外部 vector DB
- **Long-term user memory / 跨 Session 用户记忆**——仍不做（区别于 RAG）
```

**理由**：README 是面向用户的入口文档，"不做 RAG"已经过时。新措辞区分 RAG（做）与 long-term user memory（不做）。

---

### 1.6 `README.md:113` — Context 模块边界中的 RAG

**原文**：
```
**边界**：只做"消息形状变换"，不读取外部知识（无 RAG / Vector Memory）；`CustomMessage` 在转换时被丢弃（不会发给 LLM）。
```

**判定**：✅ **改写**为：
```
**边界**：只做"消息形状变换"——不直接读取外部知识；RAG 的检索由 `search_knowledge` Tool 在 loop 层注入，不在 context 层做向量召回。`CustomMessage` 在转换时被丢弃（不会发给 LLM）。
```

**理由**：context.py 模块本身仍然不做 RAG——RAG 通过 Tool 注入。这是**架构边界**而非"项目不做 RAG"，措辞需精准。

---

### 1.7 `README.md:174` — Session Memory 模块边界

**原文**：
```
**边界**：**不**含 Skills / Compaction / Vector Memory / Long-term user memory；存储后端只有内存和 JSONL（无 DB / Redis）；只支持单 session 实例（多 session 由上层 session store 管理）。
```

**判定**：✅ **改写**为：
```
**边界**：**不**含 Skills / Compaction / Long-term user memory；存储后端只有内存和 JSONL（无 DB / Redis）；只支持单 session 实例（多 session 由上层 session store 管理）。**Vector Memory 不在此模块**——RAG 向量召回属于 Knowledge 子系统（P2-R 系列，独立 knowledge.db）。
```

**理由**：移除 "Vector Memory"（项目层已开 RAG），但明确"向量召回属于 Knowledge 子系统而非 SessionMemory"，避免误读为"SessionMemory 加 Vector"。

---

### 1.8 `README.md:201` — Compaction 模块边界（补遗）

**原文**：
```
Step 15 **不做** Vector Memory / RAG / Long-term Memory / 自动后台压缩 / 数据库；`default_summary_generator` 是规则式（不调真 LLM）。
```

**判定**：✅ **改写**为：
```
Step 15 **不做** RAG / Long-term user memory / 自动后台压缩 / 数据库；`default_summary_generator` 是规则式（不调真 LLM）。Vector Memory 属于 Knowledge 子系统（P2-R 系列），不在此模块。
```

**理由**：与 §1.7 同步——Compaction 不做 RAG，但项目层会做。措辞要精准。

---

### 1.9 `CHANGELOG.md:23-24` — 路线调整段

**原文**：
```
- **P1-D3 PDF Text Extraction**：⏸ DEFERRED——转出主路线
- **PDF / Vector RAG**：⏸ DEFERRED——转出主路线
```

**判定**：
- 行 23 P1-D3：✅ **加注** "重启于 P2-R0，见 docs/design/p2-r0-rag-contract.md"
- 行 24 PDF / Vector RAG：✅ **删除**

**理由**：CHANGELOG 是历史记录，不删历史条目；但 P1-D3 重启 + RAG DEFERRED 解除需要在原位置加注。

---

## 2. 仍不做（明确保留）

以下旧"不做"标记**保留不变**，P2-R 全程不动：

| 旧标记位置 | 内容 | 保留理由 |
|---|---|---|
| `ROADMAP.md:278` | OCR / Image understanding / 视觉理解 | 第一版 marker 配置 `force_ocr=False`，**不主动 OCR**；扫描 PDF 进 `status=needs_ocr` 状态而非自动处理 |
| `ROADMAP.md:279` | PDF 表单 / 注释 / 嵌入对象 | marker 不解析表单/批注，仅提取正文文本 + heading/表格结构 |
| `ROADMAP.md:281` | Multi-Agent 编排 | P2-R 仅给 Agent 一个只读 Tool，不引入子 Agent |
| `ROADMAP.md:282-290` | CLI / 多用户 / RBAC / 公网部署 / MCP marketplace / Skill 热加载 / 本地文件系统工具 / 自动 fallback / Markdown 在线编辑 / 长期后台任务调度 | 全部保留；RAG 不影响这些边界 |
| `TODO.md:186` | OCR / Image understanding | 同上 |
| `TODO.md:188-193` | Multi-Agent / 公网部署 / CLI / 本地文件系统工具 / 自动 fallback / 长期后台任务调度 | 同上 |

**Long-term user memory 拆分**（新独立条目，加入"不做"列表）：
- 跨 Session 用户偏好 / 用户画像 / 用户级长期记忆 —— **不做**。P2-R 仅做 Session-scoped Library ACL。

---

## 3. 与既有边界的兼容性矩阵

P2-R 引入的子系统如何与既有边界共存：

| 既有边界 | P2-R 是否触碰 | 兼容方式 |
|---|---|---|
| **Core Runtime 冻结**（loop / agent / context / events / stream_events / messages） | ❌ 不触碰 | `search_knowledge` 作为标准 AgentTool 注册，走现有 Tool 注册路径 |
| **Provider Adapter 冻结**（providers/*） | ❌ 不触碰 | 第一版无 embedding provider；R3+ 若引入，作为新增模块 |
| **Session 模型**（SQLiteSessionStore） | ❌ 不触碰 | 独立 `knowledge.db`，library_id 作逻辑外键（不依赖 SQL FK）；`session_knowledge_libraries` 表存 binding 但不修改 sessions/messages/snapshots 表 |
| **VirtualFileStore**（web/files.py） | ❌ 不触碰 | 新增独立 `KnowledgeFileStore`，根目录 `data/knowledge/`，不复用 `uploads/{session_id}/` 路径 |
| **Permission Policy**（policy/） | ✅ 复用 | `search_knowledge` 默认 `allow`（read-only）；PDF 上传 / Library CRUD 走 Web API（不经 Tool），后端做 Session ACL 校验 |
| **No Drawer 原则**（MEMORY feedback_no_drawer） | ✅ 遵守 | P2-R5 Library 管理 UI 是 workspace 内 panel 或 modal，不是右栏调试 Drawer |
| **Trusted UI Header**（M2-F1 修复） | ✅ 复用 | 新 API 端点继续走 `api/client.ts` 共享传输层，自动注入 `X-PI-Agent-UI: 1` |
| **Localhost only / no auth** | ✅ 遵守 | 第一版不做用户账号；Session Library ACL 是单进程内的访问隔离，不是用户级 RBAC |

---

## 4. 重启条件核对（P1-D3 原约束）

ROADMAP.md:266-268 列出的 P1-D3 重启条件：

| 条件 | 状态 | 备注 |
|---|---|---|
| P1-E / P1-F 完成 | ✅ P1-E COMPLETE/FROZEN；P1-F 仍 DEFERRED | P2-R **不依赖** P1-F——P1-F 是 Markdown 预览 UI，P2-R 自己处理 Markdown（Canonical MD） |
| PDF adapter 边界重新评估 | ✅ 已重评 | P2-R2 用 marker 替代原计划的 `tools/view_file.py` 改造——边界从"Tool 层"上移到"Ingestion Pipeline 层" |
| FileRef metadata 字段是否已被 P1-E/F 引入 | ✅ 已确认 | 不复用 FileRef；新建独立 `KnowledgeFileStore` + `knowledge_documents` 表（详见 [p2-r0-rag-contract.md §2](p2-r0-rag-contract.md)） |

---

## 5. 审计完成出口

本审计已逐条覆盖：

- ✅ ROADMAP.md 中所有 RAG / PDF / Vector / Long-term Memory 引用（行 263-268、270-272、278-290）
- ✅ TODO.md 中所有 RAG / PDF / Long-term Memory 引用（行 178-179、186-193）
- ✅ README.md 中所有 RAG / Vector Memory 引用（行 65、113、174、201）
- ✅ CHANGELOG.md 中所有 PDF / RAG 引用（行 23-24）
- ✅ 仍不做项的明确保留（OCR / Multi-Agent / RBAC 等）
- ✅ 与既有边界的兼容性（Core Runtime / Provider / Session / FileStore / Policy / No Drawer / Trusted UI / Localhost）

**下一步**：源文档修改在 R0 主流程一次性完成（4 份文档同一 commit），见 [p2-r0-rag-contract.md §8](p2-r0-rag-contract.md)。

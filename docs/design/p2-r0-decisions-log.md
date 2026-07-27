# P2-R0 — Decisions Log（RAG 决策冻结表）

> **阶段**：P2-R0 RAG Contract Audit（docs-only）
> **基线 commit**：`ee62732`
> **日期**：2026-07-27
> **范围**：把所有 P2-R 系列要落地的设计决策分成三段冻结——§1 用户方案给定（直接抄入）、§2 R0 新权衡（推荐答案 + 理由）、§3 推迟到 R1+。后续 milestone 实施时**只允许引用本表**，不得重新讨论已冻结项。

---

## 1. 用户给定冻结决策（8 条，直接落地）

| # | 决策 | 冻结内容 | 落地章节 |
|---|---|---|---|
| F1 | 数据模型 | 7 张表：`knowledge_libraries` / `knowledge_documents` / `knowledge_ingestion_jobs` / `knowledge_chunks` / `session_knowledge_libraries`；字段名与 [p2-r0-rag-contract.md §2](p2-r0-rag-contract.md) 完全一致 | contract §2 |
| F2 | 文件系统布局 | `data/knowledge/libraries/{library_id}/documents/{document_id}/{source.pdf, document.md, manifest.json}`；独立 `KnowledgeFileStore`，**不复用** `uploads/{session_id}/` | contract §3 |
| F3 | PDF 边界 | 仅支持数字 PDF；扫描 PDF 进 `status=needs_ocr`；不静默生成空 Markdown；不支持表单/批注/嵌入对象/复杂版面完美还原/图片公式表格视觉重建 | contract §4 |
| F4 | Canonical Markdown | YAML frontmatter（document_id / library_id / source_name / source_sha256 / parser_version / page_count）+ `<!-- page:N -->` page marker + heading 层级；保留页码映射 / heading path / 原文顺序 | contract §4 |
| F5 | Chunk 格式 | 字段冻结：`library_id / document_id / chunk_id / heading_path / page_start / page_end / content / content_hash / token_count`；chunk 必须可回溯 library / document / page | contract §5 |
| F6 | Session Library ACL | `Session → Library Allowlist`（**非** User → Role → Permission）；前端过滤不是权限控制；删除 Session 只删 binding 不删 library | contract §7 |
| F7 | Tool 接口 | `search_knowledge(query: str, top_k: int = 5)`；签名**不接受** library_id / session_id / 文件路径；自动用当前 Session binding 求交后检索 | contract §6 |
| F8 | 同步 / 异步策略 | 第一版**无后台任务调度**；upload 请求内同步处理；保留 `knowledge_ingestion_jobs` 表审计；R2 可加 BackgroundTask 但不改 schema | contract §8 |

---

## 2. R0 新权衡推荐（5 条）

| # | 问题 | 冻结结论 | 理由 | 落地章节 |
|---|---|---|---|---|
| R1 | PDF parser 选型 | **`marker`**（datalab.so/marker，AGPL-3.0） | 用户本轮确认。marker 基于 surya 模型，输出高质量 Canonical Markdown（heading 层级 + 表格 + 列表 + 公式标注）。**OCR 边界处理**：配置 `force_ocr=False` + 单页文本抽取为空时显式判 `needs_ocr`——保持原方案"数字 PDF only / 扫描 PDF 进 needs_ocr"边界不放宽。<br><br>**已知 trade-off**（写入 contract §4 与本表 §3）：AGPL-3.0 license 需法务确认与项目当前 MIT 是否兼容；首次运行下载 GB 级 surya 模型；CPU 推理慢（单 PDF 分钟级）。R1 集成时若实测不可接受，fallback 到 pypdf（纯 Python / BSD）作为 parser 第二选项 | contract §4 |
| R2 | SQLite 部署 | **独立 `knowledge.db`** + 独立 aiosqlite 连接 | 用户本轮确认。理由：(1) 删 library = 物理删 db 行 + `rm -rf` 目录，不需 DELETE + cascade；(2) knowledge 写少读多，与 session_sqlite 高频写解耦；(3) 备份/迁移可整体打包 `knowledge.db` + `data/knowledge/`；(4) library_id 作为**逻辑外键**（不依赖 SQL FK），跨库无 join 需求——检索时先查 `session_knowledge_libraries` 得 library_id 集合，再查 chunks | contract §2.5 |
| R3 | 同步 upload 阈值 | **upload 请求内同步处理，30s 超时阈值** | 第一版无后台调度（F8）。upload 同步阻塞 ≤ 30s：完成后返回 `document.status=ready`；超时返回 `{document_id, status: "extracting", job_id}`，前端轮询 `GET /api/knowledge/libraries/{lib}/documents/{doc}`。<br><br>30s 阈值理由：(1) marker 在 CPU 上单 PDF 分钟级——大 PDF 必然超时；(2) FastAPI sync endpoint 阻塞 worker，但本项目单 worker 部署可接受；(3) R2 引入 BackgroundTask 时只加 dispatcher 不改 schema（F8 兼容）。<br><br>第一版建议：**强制限制 upload PDF ≤ 20 页**（约 30s 内可处理），> 20 页直接拒绝并提示用户分批 | contract §8 |
| R4 | Chunk 默认参数 | **max_chars=1200, overlap=150**（heading-aware） | 1200 字符 ≈ 300-400 token，留余量给 system prompt + tool definitions（context budget 通常 4k-8k token 给 RAG evidence）。overlap 150 字符防止 heading 边界切断语义。<br><br>heading-aware 算法：先按 `#`/`##`/`###` 切 section，section > max_chars 时按段落 fallback，段落仍 > max_chars 时硬切（保留 overlap）。<br><br>**第一版不做**：句子级切分（需 NLP）、token 精确切分（需 tokenizer）、动态 overlap。这些进 R3+ 优化 | contract §5 |
| R5 | KnowledgeFileStore 实现 | **新建独立类**，复用 VirtualFileStore 的 atomic rename pattern，但**独立根目录 + 独立 atomic write helper（含 fsync）** | VirtualFileStore（`src/pi_agent_core_py/web/files.py`）是 session 级临时上传，knowledge 是长期持久化——生命周期/权限/清理逻辑全不同，混根目录会让 cleanup 耦合。<br><br>同时**给 VirtualFileStore 提 follow-up**（不在 P2-R 范围）：补 fsync + atomic rename。本 R0 不动 VirtualFileStore | contract §3 |

---

## 3. 推迟到 R1+ 的开放项

以下决策**不**在 R0 冻结，留给后续 milestone 在实施时决定：

| # | 开放项 | 推迟到 | 备注 |
|---|---|---|---|
| D1 | Embedding provider 选型 | R3+ | 第一版纯 FTS5（BM25），不引入 embedding。若 FTS5 召回不足，R3+ 评估：(a) 复用现有 LLM provider（如 GLM embedding）/ (b) 加 `jina-embeddings-v3` / (c) 加 `bge-m3` 本地 |
| D2 | FTS5 vs 向量混合（hybrid） | R3+ | 第一版纯 FTS5；R3+ 评估 BM25 + Dense + RRF reranker |
| D3 | Chunk re-embedding 触发策略 | R4+ | 仅在引入 embedding 后才相关；document 重新 ingestion 时如何处理旧 chunk 的 embedding |
| D4 | 多语言分词 | R1+ | 中文 heading 检测 / CJK 字符级 chunking；R1 看实测质量再决定是否需 jieba |
| D5 | marker 模型预热策略 | R1 | 首次 upload 触发模型下载（GB 级）；是否启动时预热 / 提示用户 / 提供轻量 parser 选项 |
| D6 | marker AGPL license 兼容性 | R0 已记录，R1 集成前需法务确认 | 若 AGPL 与项目 MIT 不兼容，降级到 pypdf（BSD）+ heading 正则——这是 contract §4 的 fallback 路径 |
| D7 | Library 容量上限 | R1 | 第一版无显式上限；后续按 `data/knowledge/` 磁盘占用 + chunk 数量评估是否需要 quota |
| D8 | PDF 页数硬上限 | R1 集成 marker 后实测 | R3 推荐 ≤ 20 页（同步处理 ≤ 30s）；R1 实测后调整 |

---

## 4. 决策变更协议

已冻结决策（§1 + §2）在 P2-R 系列实施期间**不得变更**——若发现错误，必须：

1. 新增 `decision-amendment-N.md` 文档说明变更原因
2. 更新本表对应行（保留历史，标注 `[AMENDED YYYY-MM-DD]`）
3. 更新 [p2-r0-rag-contract.md](p2-r0-rag-contract.md) 对应章节
4. 跑回归测试确认前置 milestone 未受影响

§3 推迟项可在对应 milestone 实施时直接冻结——更新本表 + contract 对应章节即可，无需 amendment。

---

## 5. 决策与文档的交叉引用

| 决策 | 主设计章节 | 验证 checklist |
|---|---|---|
| F1 数据模型 | contract §2 | checklist #4 |
| F2 文件布局 | contract §3 | checklist #6 |
| F3 PDF 边界 | contract §4 | checklist #6 |
| F4 Canonical MD | contract §4 | checklist #6 |
| F5 Chunk 格式 | contract §5 | checklist #4 |
| F6 Session ACL | contract §7 | checklist #4 |
| F7 Tool 接口 | contract §6 | checklist #4 |
| F8 同步策略 | contract §8 | checklist #4 |
| R1 marker | contract §4 | checklist #6 |
| R2 独立 knowledge.db | contract §2.5 | checklist #6 |
| R3 30s 阈值 | contract §8 | — |
| R4 Chunk 参数 | contract §5 | checklist #6 |
| R5 KnowledgeFileStore | contract §3 | checklist #6 |

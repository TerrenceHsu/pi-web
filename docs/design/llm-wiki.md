# LLM Wiki 设计

> 状态：阶段 0–10 已实现；页面中心 LLM Wiki、双 Parser OCI Worker、独立 Knowledge 页面、来源保留、Space 生命周期与归档只读门禁均已完成
>
> 日期：2026-08-28
>
> 范围：Wiki Space、PDF/HTML 原件、Parser Provider、Wiki 页面、Revision/Change Set、页面知识图谱、页面级 FTS5 与 Knowledge Agent 对话

## 1. 目标与替代关系

Knowledge 产品从以 Chunk 检索为核心的 RAG 改为以持久 Wiki 页面为核心的 LLM Wiki。
用户先创建 Wiki Space，再上传 PDF 或单文件 HTML；系统保留不可变原件和解析证据，Agent
把每个来源整理成一篇入口页，并可提出主题子页面和页面关系。所有写操作先形成统一 diff，
用户一次批准后才发布。

本设计取代下列旧产品主链路：

```text
Library → Document → Chunk → Chunk FTS5 → search_knowledge
```

新的主链路是：

```text
Wiki Space
  → Raw Source
  → Parsed Artifact
  → Wiki Page Draft
  → Change Set / Approval
  → Published Page Revision
  → Page FTS5 + Page Graph
```

旧 `p2-r*` RAG 文档和实现保留为历史基线与可复用安全经验，不再是后续 Knowledge
产品的实施依据。`knowledge_chunks`、`knowledge_chunks_fts` 和 Session-Library Binding
不迁移到新模型。

## 2. 冻结决策

1. 用户必须先创建 Wiki Space，来源、页面、图谱和对话都严格归属于一个 Space。
2. MVP 上传格式只支持 PDF 和单个 `.html`；DOCX、PPT/PPTX、Excel 后续再设计。
3. PDF 由独立持久 Worker 处理：PyMuPDF4LLM 负责 fast，Docling 负责 accurate 和 auto 回退；
   两者始终直接读取同一不可变原始 PDF。
4. API 支持 `auto | fast | accurate`；生产默认 `auto`。快速结果必须通过版本化质量门禁，失败时
   只有 `auto` 可以用原始 PDF 回退 Docling。
5. HTML 使用独立的确定性 Parser，不经过 PDF Parser，不访问任何外部网络资源。
6. 每个来源生成一篇入口 Wiki 页面；Agent 可以在同一 Space 内提出主题子页面。
7. 删除 Chunk 检索主链路；允许对已批准 Wiki 页面的标题、别名和正文使用 SQLite FTS5。
8. Agent 对 `raw/` 只有读取权限，不能直接修改原件、解析 Markdown、图片或 manifest。
9. Agent 的页面和图谱写操作全部先进入同一个 Change Set，用户一次审批，全有或全无。
10. 图谱首版只包含页面节点、固定类型的页面关系和系统维护的来源关系；实体图谱延期。
11. 每个 Wiki Space 支持多个独立对话；复用 Agent 框架和 Session 持久化，但使用独立
    Knowledge 模式、Prompt、Skill 和工具白名单。
12. 旧 Knowledge 数据库结构废弃，不迁移业务数据；切换时先保留只读备份，再初始化
    新 `wiki.db`。

## 3. 非目标

MVP 不实现：

- Chunk RAG、向量数据库、embedding 或 hybrid retrieval。
- 人物、组织、概念等实体级知识图谱。
- 外部网页抓取、HTML 依赖资源下载或站点镜像。
- DOCX、PPT/PPTX、XLS/XLSX 转换。
- Agent 直接覆盖已发布页面或绕过用户审批。
- Agent 写入或删除 `raw/`。
- RBAC、多租户、远程公网部署或 Space 分享权限。
- 未经版本、模型许可证、OCR 语言和真实语料 Gate 的扫描 PDF 质量承诺。

## 4. 目录结构与所有权

```text
data/
└── knowledge/
    ├── wiki.db
    ├── legacy/
    │   └── knowledge-{backup_id}.db
    └── spaces/
        └── {space_id}/
            ├── space.json
            ├── raw/
            │   └── {safe_stem}--{source_id}/
            │       ├── source.pdf | source.html
            │       ├── selected.json
            │       └── parses/
            │           └── {parse_revision_id}/
            │               ├── parsed.md
            │               ├── pages/*.md
            │               ├── images/{artifact_id}.{ext}
            │               └── manifest.json
            └── pages/
                ├── index.md
                └── topics/
                    └── *.md
```

### 4.1 命名

- `space_id`、`source_id`、`page_id`、`revision_id`、`change_set_id` 使用服务端生成的稳定 ID。
- `safe_stem` 只用于可读目录名；身份和引用始终使用 ID。
- 原始显示文件名保存在数据库和 manifest 中；磁盘使用固定 `source.<ext>`，不直接使用
  用户文件名作为路径。
- 同名文件不会冲突，因为来源目录包含 `source_id`。

### 4.2 写权限

| 区域 | 上传服务 | Parser Worker | Agent | 用户审批 Publisher |
|---|---:|---:|---:|---:|
| `raw/.../source.*` | 写一次 | 读 | 只读工具 | 无 |
| `raw/.../parses/{revision}/` | 无 | 只写 staging | selected revision 只读工具 | 无 |
| `raw/.../selected.json` | 无 | 无 | 只读工具 | 受控 Ingestion CAS 切换 |
| `pages/` | 无 | 无 | 只能提案 | 发布镜像 |
| `wiki.db` | 受控服务 | 任务状态 | 通过工具提案 | 事务提交 |

这里的“Agent 只读”由服务端工具权限和路径所有权强制，而不是依赖 Prompt 自律。Agent
不获得任意文件路径工具，也不获得 Parser staging 路径。

## 5. 规范数据模型

SQLite `wiki.db` 是规范事实源。`pages/` 是已发布最新版本的可读镜像，可由数据库幂等重建，
不承担并发控制或 revision 真相。

Schema v1、Space CRUD、路径/原子镜像恢复和显式旧库 retirement 的初始实现冻结见
[`llm-wiki-store-v1.md`](llm-wiki-store-v1.md)。当前产品启动不再打开旧 Knowledge API/Worker；
历史文件保持原样，只有用户另行要求物理归档时才执行破坏性 retirement 流程。

当前 schema v7 在 selected parse revision 的 v2 基础上依次加入 Summary Draft、Page Proposal、
Change Set 发布、页面 FTS5、图谱与 Conversation 合同。旧 v1–v6 都固定返回
`schema_rebuild_required`，由用户显式重新初始化未发布的新 Wiki 库；不做原地迁移，也不能在
同一 schema version 下误读不同数据合同。

### 5.1 `wiki_spaces`

- `id`、`name`、`description`
- `status`: `active | archived | deleting | failed`
- `graph_revision`
- `created_at`、`updated_at`

### 5.2 `wiki_sources`

- `id`、`space_id`
- `display_name`、`mime_type`、`size_bytes`、`source_sha256`
- `source_relpath`、`selected_parse_revision_id`、`selection_version`、`selected_at_ms`
- `status`: `uploaded | parsing | parsed | failed | deleting`
- `safe_error_code`
- `created_at`、`updated_at`

同一 Space 内可按 `source_sha256` 拒绝重复上传。跨 Space 不共享 ACL 或物理引用。

### 5.3 `wiki_artifacts`

- `id`、`source_id`、`parse_revision_id`
- `kind`: `parsed_markdown | page_markdown | embedded_image | table_image | manifest`
- `relpath`、`mime_type`、`size_bytes`、`sha256`
- 图片可选 `width`、`height` 和 Parser 提供的来源定位信息

### 5.4 `wiki_pages`

- `id`、`space_id`
- `slug`、`title`、`aliases_json`
- `status`: `active | deleted`
- `current_revision_id`、`version`
- `created_at`、`updated_at`

### 5.5 `wiki_page_revisions`

- `id`、`page_id`、`version`
- `title`、`markdown`、`content_sha256`
- `change_set_id`
- `author_kind`: `agent | user | system`
- `created_at`

Revision 不可变；更新页面只能新增 revision 并移动 `current_revision_id`。

### 5.6 `wiki_page_sources`

- `page_id`、`source_id`
- `source_locator_json`
- `created_at`

此表由服务端从批准内容和受信 provenance 生成，在图谱 API 中投影为 `derived_from`；Agent
不能直接创建、删除或伪造该关系。

### 5.7 `wiki_edges`

- `id`、`space_id`
- `from_page_id`、`to_page_id`
- `relation_type`
- `change_set_id`、`created_at`

允许的 `relation_type` 固定为：

- `related_to`
- `references`
- `extends`
- `contradicts`
- `part_of`

约束：

- 所有节点必须属于同一 Space 且处于 active 状态。
- 禁止自环和重复边。
- `related_to` 为对称关系，服务端按节点 ID 规范化顺序后只存一条。
- 其他四类为有向关系。
- `part_of` 不允许形成循环。

### 5.8 `wiki_change_sets`

- `id`、`space_id`、`conversation_id`
- `status`: `draft | awaiting_approval | approved | rejected | stale | failed`
- `base_graph_revision`
- `summary`、`safe_error_code`
- `created_at`、`decided_at`、`published_at`

Change Set 子项保存页面 create/update/delete、edge add/delete、目标页 base version/before SHA、
拟发布正文和统一 diff。

### 5.9 `wiki_conversations`

- `id`、`space_id`、`session_id`
- `title`、`status`
- `created_at`、`updated_at`

消息、lane、compaction 和事件继续复用现有 Session/Agent 持久化；此表只保存 Wiki Space 与
Knowledge 对话的可信绑定。

### 5.10 `wiki_jobs`

- `id`、`space_id`、`source_id`
- `kind`: `parse | summarize_source | synthesize_entry_page | rebuild_search | rebuild_graph_projection`
- `status`: `queued | running | succeeded | failed | cancelled`
- `requested_parse_mode`、`routing_config_revision`、`routing_config_sha256`
- `selected_attempt_id`、`safe_error_code`
- `started_at`、`finished_at`

### 5.11 `wiki_parse_attempts` 与 `wiki_parse_revisions`

- Attempt：`job_id`、递增 ordinal、Parser/version/preset、route reason、fallback_from、状态、
  duration、quality JSON、safe error 与 artifact receipt。
- Revision：`source_id`、成功 attempt、不可变 root/manifest SHA、page count、创建时间。
- Source 的 selected pointer 只在完整 artifact 验证和 DB/file 回读成功后通过 CAS 切换。

### 5.12 `wiki_source_summaries`

- `id`、`space_id`、`source_id`、`parse_revision_id`、`job_id`
- `selection_version`、`source_sha256`、`parsed_markdown_sha256`、`manifest_sha256`、`page_count`
- `prompt_revision`、实际 `provider`/`model`
- 严格 `content_json`、`content_sha256`、`created_at_ms`

记录只允许插入，不提供更新路径；同一个来源可以针对不同 selected parse revision 保留多份历史
Summary Draft。后续页面提案必须显式指定一份 Draft，不能隐式读取“最新”后跨版本发布。
- 失败 fast attempt 的质量证据可以保留，但正文不得进入 Agent 可读的 selected Raw。

## 6. Parser Provider

主应用只依赖 provider-neutral 的异步 `ParserProvider` 契约，不 import PyMuPDF、PyMuPDF4LLM、
Docling、Torch、OCR 或其模型 SDK。发布实现是断外网的持久 OCI Worker，启动时创建并预热
Docling standard/ocr 两个 Converter；独立 venv 进程只允许作为明确标记的开发降级模式。

现行双 Parser 的模式、路由、质量、ParsedDocument v2、parse revision、AGPL-3.0 和模型 Gate
见 [`llm-wiki-dual-pdf-parser.md`](llm-wiki-dual-pdf-parser.md)。旧 Marker Gate 仅保留历史记录。

Provider 最小能力：

```text
probe() -> provider/version/capabilities
create_job(source descriptor, limits) -> parser_job_id
wait(job_id, cancel signal) -> terminal status
download_artifact(job_id) -> immutable artifact bundle
cancel(job_id)
destroy(job_id)
```

安全约束：

- 每个任务只获得独立 staging 输入和输出，不挂载 `pages/`、`wiki.db`、Session Workspace、
  凭证目录或项目根。
- 每次 attempt 只读取经 size/SHA 核验的原件副本；输出只能写到独立临时目录。
- 主应用把 Provider 输出视为不可信制品，逐项校验相对路径、文件类型、文件数、单文件/总大小、
  SHA-256、重复/大小写冲突、symlink、特殊文件和 Markdown 编码。
- Contract v2 只接受 manifest、合并 Markdown、逐页 Markdown 和受限图片；拒绝可执行文件、
  未声明文件和任意路径。
- 取消、超时和异常最终都必须回收任务资源。
- PyMuPDF4LLM/PyMuPDF 的 AGPL Source Offer、Docling/模型/OCR 许可证、精确版本/hash、离线缓存、
  CPU/GPU/内存、启动时间和分发方式必须通过独立 Gate 后才能进入发布运行时。

## 7. PDF 与 HTML 解析

### 7.1 PDF

统一 Parser Worker 输出：

- `parsed.md`
- 按页 `ParsedPage` 与合并 `parsed.md`
- PDF 内嵌图片和受支持的表格图片
- `manifest.json`
- Parser 能可靠提供的页码、块或图片来源定位

Provider 规范化层必须把两种 Parser 的结果转换为同一 Contract，并修正 Markdown 图片引用，
使其只指向本 parse revision 内已校验的
`images/{artifact_id}.{ext}`。禁止远程图片、绝对路径、`..` 和跨来源引用。

### 7.2 HTML

HTML MVP 只接受单个文件：

- 不发起 HTTP/HTTPS 请求。
- 不下载 CSS、图片、iframe、字体、脚本或其他依赖。
- 删除 `script`、`style`、`iframe`、`object`、`embed`、表单和事件属性。
- 拒绝或移除危险 URL scheme。
- 可在独立配额内提取内嵌 `data:` 图片。
- 保留标题、正文、列表、表格和安全链接，输出确定性 `parsed.md` 与 manifest。

## 8. 页面生成与来源入口页

页面生成拆成两个顺序阶段。第一阶段先生成不可变 Source Summary Draft，不创建页面：

1. tool-free core Agent 分页读取当前 selected parse revision 的 Raw page Markdown。
2. 模型只能返回严格结构化 JSON：建议标题、概览、带页码的关键点、主题候选和 caveat。
3. 服务端验证 JSON、字段配额、页码范围和 UTF-8，并把草稿固定到 source SHA、parse revision、
   selection version、parsed/manifest SHA、prompt revision、实际 provider/model 和正文 SHA。
4. 长来源按配置分批总结，再由同一个 provider/model 合并；单页、批次数或合并上下文超限时安全失败。
5. 完成写入前再次 CAS 当前 selected parse revision；来源在 Agent 运行期间被重解析时拒绝旧结果。
6. Summary Draft 是后续页面提案的输入，不进入正式页面、FTS5 或图谱，也不修改 `raw/`。

第二阶段由已固定 Summary Draft 触发 `synthesize_entry_page`：

1. 生成一篇来源入口页，保留来源身份和可验证引用。
2. Agent 可以在同一 Change Set 中提出零到多个主题子页面。
3. Agent 可以提出固定类型的页面关系。
4. 所有内容保持草稿，不写入已发布页面或正式图谱。
5. 用户批准 Change Set 后统一发布。

入口页在进入 Change Set 前保存为 immutable `wiki_page_proposals(kind=entry)`：正文由严格
Summary Draft 确定性渲染，所有模型文本先做 Markdown/HTML 结构转义；slug 带来源稳定后缀，
source locator 单独记录 source/parse revision/summary/page numbers。此记录不是 `wiki_pages`，
重复生成返回同一 proposal，来源 selected revision 已变化时必须重新总结。

主题子页面复用 `wiki_page_proposals(kind=topic)`，必须引用同一 Summary Draft 的 entry proposal
作为 parent。服务端按 topic candidate ordinal 稳定生成 slug/正文，只加入与主题页码相交的关键点；
同一 `synthesize_topic_pages` Job 在单个事务中插入全部主题 proposal，任一校验失败则一个都不留。

Agent 不应把解析 Markdown 简单复制为 Wiki 页面；入口页应提供结构化摘要、主要主题、关键结论、
术语和进一步阅读路径，同时区分来源事实与 Agent 组织性文字。

## 9. Change Set、Diff 与批准

一次 Agent 操作只产生一个 Change Set，可以同时包含：

- 创建页面。
- 修改多个页面。
- 删除页面。
- 新增或删除多个图谱关系。
- 修改标题、slug 或别名。

提交审批前，服务端生成规范、稳定排序的统一 diff 和结构化 edge diff。批准时：

页面 proposals 按 entry、topic ordinal 的稳定顺序冻结为一个 `awaiting_approval` Change Set；
每个 page-create item 保存 canonical JSON payload（含 proposal/source/summary/locator/正文 SHA）和
`/dev/null → pages/{slug}.md` 的确定性 unified diff。Change Set 直接绑定 `source_summary_id`，重复
冻结幂等；此阶段不会创建 Page/Revision 或刷新镜像。

批准时：

1. 获取 Space 写锁并开始短 SQLite 事务。
2. 校验 `base_graph_revision`、所有目标页 version 和 before SHA。
3. 任一前提变化则整个 Change Set 标记 `stale`，不发布任何部分。
4. 校验 Markdown、slug、页面引用、图关系类型、同 Space 约束和 `part_of` 无环性。
5. 在一个事务内写入全部 immutable revisions、更新 current revision、应用 edges、更新
   `wiki_page_sources`、页面 FTS5 和 Space graph revision。
6. commit 后通过 durable outbox 幂等刷新 `pages/` 文件镜像和前端事件。

SQLite 是规范事实源，因此文件镜像写入失败不会产生部分数据库发布；恢复 Worker 会根据已提交
revision 重建镜像。拒绝和 stale Change Set 永不修改正式页面、FTS 或图谱。

当前 page-create 审批实现已固定上述事务语义：批准同时创建 Page、version 1 Revision 和
PageSource，Change Item 在发布时回填可信 page target；拒绝与 stale 均可审计且没有页面副作用。
commit 后刷新 `pages/{slug}.md`/`space.json`，启动时依据 current revision 修复缺失页面镜像。

## 10. 页面级 FTS5

FTS5 只索引 active 页面当前已批准 revision：

- `page_id`、`space_id` 为 unindexed identity 字段。
- `title`、`aliases`、`content` 为检索字段。
- 查询必须由服务端固定到当前 `space_id`，Agent 不能请求跨 Space 搜索。
- 搜索结果返回页面 ID、标题、匹配摘要和 current revision，不返回 Chunk ID。
- 草稿、已删除页面和 Raw Markdown 不进入正式索引。

FTS5 是 Wiki 导航和页面发现能力，不恢复旧 Chunk RAG 主链路。

实现使用独立 `wiki_pages_fts`（unicode61）；page approval 在同一 SQLite 事务插入索引，启动可从
active Page/current approved Revision 重建。API 只接受可信路径中的 Space，原始查询逐空白 term
转成 quoted literal phrase 后再绑定给 `MATCH`，不会暴露 `OR/NEAR/*/column` 等 FTS 语法。

## 11. Knowledge Agent 模式

每个 Wiki Space 可创建多个 Knowledge 对话。运行时复用现有 Agent loop、消息、thinking、工具
事件、lane、compaction 和持久化，但请求必须带可信 `conversation_id → space_id` 绑定。

Knowledge 模式使用：

- 独立 system prompt overlay。
- 独立内置 Wiki Skill。
- 独立工具白名单。
- 独立的 Change Set 审批上下文。

MVP 工具：

```text
wiki_list_pages
wiki_search_pages
wiki_read_page
wiki_read_raw
wiki_list_source_artifacts
wiki_graph_neighbors
wiki_propose_page_create
wiki_propose_page_patch
wiki_propose_page_delete
wiki_propose_edge_changes
```

所有工具从可信 closure/context 获取 `space_id` 和 `conversation_id`；LLM 参数中不暴露可切换
Space 的字段。提案工具只能写 Change Set staging，不能直接调用发布路径。

当前实现把 Knowledge Skill 固定为内置、不可由用户替换的 Prompt/工具策略，而不是加载普通
Session 的可选 Skill。Session 与 `wiki_conversations` 一对一可信绑定；一个 Space 可拥有多个彼此
独立的 Session/Conversation。Prompt、regenerate 与 context-budget 都从服务端 Session binding 自动
识别模式，客户端提交的 Conversation ID 只能用于一致性校验，不能切换 Space。Knowledge 请求拒绝
Workspace file attachment 与普通 Skill selection，执行期间只临时安装上述 Wiki registry，并在
`finally` 中恢复普通 Agent 工具。

四个 `wiki_propose_*` 工具只创建 conversation-bound `awaiting_approval` Change Set：

- `page_create`、`page_update`、`page_delete` 保存 canonical payload、基础 revision/正文 SHA（适用时）
  和完整 unified diff。
- `edge_add`、`edge_delete` 只接受五种用户关系；`related_to` 规范方向，`part_of` 在批准前检查无环。
- 批准在同一 SQLite 写事务中重新校验 Conversation/Space、graph revision、Page revision、payload、
  diff、端点与关系不变量；任一竞态使整个 Change Set stale，不会部分发布。
- `derived_from` 没有 Agent proposal 或数据库写入路径，继续只从可信 PageSource 动态投影。

## 12. 前端信息架构

Knowledge 从 Sidebar 弹窗升级为独立页面：

```text
Knowledge
├── Space 列表 / 新建 Space
└── 当前 Space
    ├── Pages：目录、阅读、来源与 revision
    ├── Sources：上传、解析状态、Raw artifacts
    ├── Graph：页面节点、固定关系、来源投影
    ├── Changes：统一 diff、批准、拒绝、stale 提示
    └── Conversations：多对话列表与 Knowledge Agent Chat
```

前端图谱只渲染服务端验证后的已发布节点和边；草稿图在 Changes 中作为预览，不混入正式图。
页面中的 `derived_from` 可跳转到来源详情、解析 Markdown 和内嵌图片。

## 13. 旧数据退役

不把旧 Library/Document/Chunk 数据迁移为 Wiki 页面。切换流程：

1. 停止旧 ingestion/indexing worker 和 Knowledge API 写入。
2. 确认旧 SQLite/WAL/SHM 已关闭并 checkpoint。
3. 把旧 `knowledge.db` 和旧 `libraries/` 记录到 legacy manifest，并移动为只读备份。
4. 初始化全新 `wiki.db` 与 `spaces/`。
5. 启用新 Wiki API；产品组合不再挂载旧 API（返回 404），且绝不静默映射到新接口。
6. 经过独立用户授权后才能删除 legacy 备份。

## 14. 实施阶段

### 阶段 0：产品合同冻结

- 完成本设计和 TODO 执行顺序。
- 把旧 P2-R RAG 文档标记为历史依据，不修改其归档内容。

### 阶段 1：Parser Provider Contract

- **历史完成**：Marker Gate 与 Contract v1 验证了 Provider 隔离、取消、超时、回收和不可信
  artifact 边界；Marker 实施路线现已停止。
- **已实现 Contract v2**：PyMuPDF4LLM fast + Docling accurate/auto fallback 的 immutable DTO、
  Protocol、预检/路由/质量/attempt/逐页制品证据与主进程依赖隔离。
- **已实现离线 Fake v2**：执行 hash-pinned 路由、质量判定、最多一次 auto fallback、逐页
  Artifact v2 和同一原始 PDF SHA 证据，不加载任何具体 Parser runtime。
- **已实现主应用编排**：Contract v2 不可信 artifact 逐项复核，三种模式贯穿上传、排队与
  崩溃恢复，attempt/route/quality/revision 原子持久化；拒绝制品不切换 selected revision。
- **已实现并验证 AGPL Worker**：独立 Worker 包、完整许可证/notices/源码 manifest、确定性
  Source Offer、SPDX SBOM、wheel Gate、About API/UI、版本化路由配置及 PyMuPDF4LLM/Docling
  adapter；真实 OCI 隔离与代表性 PDF smoke 通过，`runtime_ready=true`。
- **可复用**：独立 `wiki_parser` Protocol/固定错误与完全离线 Fake 的生命周期和安全经验。

### 阶段 2：WikiStore 与新目录

- **已完成**：新建 `wiki.db` schema v1、三重身份/version gate、路径安全 Store 和 Raw/Page
  原子镜像/恢复原语。
- **已完成**：实现显式旧库一致性只读备份与新库初始化，不迁移旧业务数据；产品组合只启动
  Wiki runtime，旧 Chunk Knowledge 只保留显式兼容测试入口，且不会自动打开旧库或 Worker。
- **已完成**：实现带 CAS 和软删除状态机的 Space CRUD。

### 阶段 3：PDF/HTML Raw Ingestion

- **已完成**：上传限制、不可变 source、Parser job/attempt、artifact 校验、重启恢复与
  `deleting` 软状态；物理删除等待 retention 合同。
- **已完成**：零网络独立 HTML Parser、受限 data images、provider-neutral Fake PDF、独立
  lifespan/API/Worker；详见 [`llm-wiki-raw-ingestion.md`](llm-wiki-raw-ingestion.md)。
- **已完成**：Contract v2，以及 schema v2/Raw 多 attempt/revision、逐页 bundle、selected
  CAS/pointer repair 与旧 flat v1 明确重建门禁。
- **已完成**：Contract v2 artifact 导入与现有 Wiki Worker/API 原子编排。
- **已完成**：构建并验证固定版本/hash、断外网的 PyMuPDF4LLM + Docling 持久 OCI Worker。
- **已完成**：前端支持 Space、Source、状态和 Raw artifact 安全浏览。

### 阶段 4：Wiki 页面与 Change Set

- **已实现**：入口页生成、主题子页面提案、immutable revision、统一 diff 和原子审批发布。
- **已实现**：stale conflict、拒绝、恢复和页面文件镜像。

### 阶段 5：页面 FTS5 与知识图谱

- **已实现**：仅针对 active/current/approved 页面 revision 的 FTS5；Raw、Summary、Proposal、
  未批准 Change Set 与历史 revision 均无索引写入路径。
- **已实现**：主题页到入口页的 `part_of` edge-add 与对应页面创建共用一个 Change Set 和批准
  事务；事务在首个页面写入前完成 canonical payload/diff、固定关系类型、同 Space、去重、自环、
  `related_to` 规范方向和 `part_of` 无环校验。
- **已实现**：正式页面关系只保存于 `wiki_edges`；系统来源关系不接受 Change Set/Agent 输入，
  而是从可信 `wiki_page_sources` 动态投影为 `derived_from`，并明确标记 `system_managed=true`。
- **已实现**：`GET /api/wiki/spaces/{space_id}/graph` 返回带 graph revision 的稳定全图快照；
  `GET /api/wiki/spaces/{space_id}/graph/pages/{page_id}/neighbors` 返回单页一跳邻接快照。两者只
  暴露 active 页面、approved page edges 和可信来源投影。

### 阶段 6：Knowledge Agent 与多对话

- **已完成**：实现 Knowledge 模式、内置固定 Prompt/Skill policy、10 个 Wiki 工具白名单、Session
  → Conversation → Space 可信绑定和每 Space 多对话；对话创建/归档与 Session 生命周期一致。
- **已完成**：复用 Agent loop、消息树、text/thinking/tool 流事件、lane、regenerate、持久化、
  context budget 和 compaction；不加载普通 Skill、不挂载 Workspace/MCP/Sandbox/Web 工具，也不
  恢复 Chunk RAG。
- **已完成**：页面创建/修改/删除、关系增删只生成统一 Change Set；用户批准后才由事务 Publisher
  原子更新 Page/Revision/FTS/Graph/镜像，拒绝/stale/未批准 proposal 对正式状态不可见。

### 阶段 7：独立 Knowledge 页面与最终验收

- **已完成**：实现 `/knowledge` 独立路由，以及 Pages/Sources/Graph/Changes/Conversations 五个视图。
- **已完成**：前端覆盖上传/解析/Raw、Summary→Proposal→Change Set、diff 审批、页面 FTS5、正式
  图谱与每 Space 多对话；Knowledge Chat 复用既有流事件但保持独立模式与工具白名单。
- **已完成**：删除旧 Library/Document/Chunk UI；开发启动器和 E2E 产品组合不再启动旧 DB、
  ingestion/indexing worker、`search_knowledge` Tool 或 REST。后续 Web-only 审计已删除旧 Backend
  package 与装配开关；历史磁盘数据只由 `wiki/legacy.py` 的显式归档流程处理。
- **已完成**：Frontend lint/typecheck/build、Vitest 352/352、Playwright 54/54、Ruff、strict Mypy
  168 files / 0 issues 均通过；Backend 全量结果记录于项目状态页。

### 阶段 8：Source retention 与安全 Raw 清理

- **已完成**：`DELETE /api/wiki/sources/{source_id}` 只把 Source 推进到不可逆 `deleting`，
  并立即关闭原件和解析 Artifact 的内容读取；审计 DTO、来源 SHA、Parse/Summary/Change Set
  记录继续保留，不通过级联删除抹去证据。
- 默认 retention 为 7 天，应用可在组合根配置；独立后台协程在启动和固定间隔检查到期项，
  不与 Parser queue 的 `get()` 超时复用，避免边界竞态丢解析请求。
- 到期清理只处理该 Source 的 `raw/<source-bundle>/`：先在同一 Raw 目录原子改名为内部
  staging，再以不跟随 symlink/junction/reparse/special file 的遍历删除；崩溃留下的 staging
  会在下一轮幂等重试。删除失败保持 fail-closed，不修改数据库审计状态。
- active Page 的可信来源投影、queued/running Job，以及 draft/awaiting-approval Change Set 均阻断
  删除。Page 必须先通过批准的 Change Set 进入 `deleted`，之后 Raw Source 才能进入 retention。
- 前端 Sources 视图提供明确的二次确认和 `deleting` 状态说明；删除后不再请求 Raw 详情。

### 阶段 9：Wiki Space 生命周期

- `PATCH /api/wiki/spaces/{space_id}/status` 只接受 `active|archived`；archive 可恢复，前端不再
  把 archived Space 当作默认活动空间，并提供显式 Restore。
- `DELETE /api/wiki/spaces/{space_id}` 只推进到不可逆 `deleting`，不物理删除 Space 行、目录、
  Page revision 或其他审计证据。
- 删除事务在同一 `BEGIN IMMEDIATE` 内检查所有 Source 已进入 `deleting`、所有 Page 已进入
  `deleted`、Conversation 已 archived，且不存在 queued/running Job 或 draft/awaiting-approval
  Change Set；任何未收敛状态统一以 `space_in_use` 拒绝，不产生部分状态变化。
- 前端为 active Space 提供 Archive/Delete，为 archived Space 提供 Restore/Delete；Delete 始终
  二次确认，并在 409 时保留当前 Space 与所有内容不变。

### 阶段 10：Archived Space 只读门禁

- Archive 与运行态收敛使用同一 Store 写锁和 `BEGIN IMMEDIATE`：存在 queued/running Job、active
  Conversation 或 draft/awaiting-approval Change Set 时拒绝归档，避免冻结半完成工作。
- 上传、Parse claim、Summary/Proposal Job、新 Conversation、Source Change Set 和 Knowledge Agent
  Change Set 在各自写事务内重新读取 Space 状态；只要不是 `active`，统一返回
  `space_read_only`，不能依赖 UI 隐藏实现权限边界。
- archived Space 仍允许读取已批准 Page、FTS5、Graph、Raw 审计元数据，以及执行 Restore、
  Conversation archive、Change Set reject、Source deletion 和最终 Space deletion 等收敛操作。
- 事务内状态复核确保另一个连接无法在“active 预检”和实际写入之间完成 Archive；Archive 也
  无法越过已经持久化的 Job/Conversation/Change Set 门禁。

## 15. 验收不变量

1. Agent 无法通过任何 Wiki 工具修改或删除 `raw/`。
2. Parser Worker 无法访问 `pages/`、`wiki.db`、Session Workspace 或凭证。
3. HTML 解析期间网络请求数为零。
4. Provider 制品中的穿越路径、symlink、特殊文件、超限文件和未声明文件全部被拒绝。
5. 未批准 Change Set 不改变页面、正式图谱、FTS5 或 `pages/` 镜像。
6. 多页面/多边审批全有或全无；并发 revision 变化导致整个 Change Set stale。
7. Agent 不能创建 `derived_from`，服务端只依据可信来源证据维护它。
8. `part_of` 不出现自环或循环，`related_to` 不重复存储镜像边。
9. FTS5 不包含 Raw、草稿、历史 revision 或已删除页面。
10. Knowledge 对话不能读取、搜索或修改其他 Space。
11. 浏览器刷新和服务重启只恢复状态，不重放 Parser、Agent 或审批副作用。
12. 旧 `knowledge.db` 不被原地覆盖，未经额外授权不删除 legacy 备份。

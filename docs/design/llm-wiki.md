# LLM Wiki 设计

> 状态：产品合同与阶段 1–2 存储基础已实现，Raw Ingestion 尚未开始
>
> 日期：2026-08-23
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
3. PDF 通过本机独立 Marker Parser Provider/Worker 处理，不把 Marker 依赖装入主应用进程。
4. Marker 图片产物只指 PDF 内嵌图片，不生成每页截图。
5. HTML 使用独立的确定性 Parser，不经过 Marker，不访问任何外部网络资源。
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
- 扫描 PDF 的 OCR 可用性承诺；Marker 的精确 OCR 配置须在独立运行时审计中冻结。

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
            │       ├── parsed.md
            │       ├── images/
            │       │   └── {artifact_id}.{ext}
            │       └── manifest.json
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
| `raw/.../parsed.md` | 无 | 写一次 | 只读工具 | 无 |
| `raw/.../images/` | 无 | 写一次 | 只读工具 | 无 |
| `raw/.../manifest.json` | 初始化 | 完成 | 只读工具 | 无 |
| `pages/` | 无 | 无 | 只能提案 | 发布镜像 |
| `wiki.db` | 受控服务 | 任务状态 | 通过工具提案 | 事务提交 |

这里的“Agent 只读”由服务端工具权限和路径所有权强制，而不是依赖 Prompt 自律。Agent
不获得任意文件路径工具，也不获得 Parser staging 路径。

## 5. 规范数据模型

SQLite `wiki.db` 是规范事实源。`pages/` 是已发布最新版本的可读镜像，可由数据库幂等重建，
不承担并发控制或 revision 真相。

Schema v1、Space CRUD、路径/原子镜像恢复和显式旧库 retirement 的实现冻结见
[`llm-wiki-store-v1.md`](llm-wiki-store-v1.md)。阶段 2 不自动切换仍在运行的旧 Knowledge
API/Worker；真正 retirement 必须等旧连接全部关闭后显式执行。

### 5.1 `wiki_spaces`

- `id`、`name`、`description`
- `status`: `active | archived | deleting | failed`
- `graph_revision`
- `created_at`、`updated_at`

### 5.2 `wiki_sources`

- `id`、`space_id`
- `display_name`、`mime_type`、`size_bytes`、`source_sha256`
- `source_relpath`、`parsed_markdown_relpath`、`manifest_relpath`
- `status`: `uploaded | parsing | parsed | failed | deleting`
- `parser_provider`、`parser_version`、`safe_error_code`
- `created_at`、`updated_at`

同一 Space 内可按 `source_sha256` 拒绝重复上传。跨 Space 不共享 ACL 或物理引用。

### 5.3 `wiki_artifacts`

- `id`、`source_id`
- `kind`: `parsed_markdown | embedded_image | manifest`
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
- `kind`: `parse | synthesize_entry_page | rebuild_search | rebuild_graph_projection`
- `status`: `queued | running | succeeded | failed | cancelled`
- `attempt`、`safe_error_code`
- `started_at`、`finished_at`

## 6. Parser Provider

主应用只依赖 provider-neutral 的异步 `ParserProvider` 契约，不 import Marker、Surya、Torch
或其模型 SDK。首个实现是本机 Sidecar，可由独立进程或容器承载。

阶段 1 Gate 已冻结：发布实现使用每任务本机 OCI 隔离容器，独立 venv/进程只允许作为明确标记的
降级开发模式；Marker 固定 `2.0.0`，MVP 固定 `fast_no_ocr`、任务期间断网且不使用 LLM/VLM。
完整许可证、资源、生命周期与制品决定见
[`llm-wiki-marker-provider-gate.md`](llm-wiki-marker-provider-gate.md)。

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
- 原件对 Worker 只读；输出只能写到独立 staging。
- 主应用把 Provider 输出视为不可信制品，逐项校验相对路径、文件类型、文件数、单文件/总大小、
  SHA-256、重复/大小写冲突、symlink、特殊文件和 Markdown 编码。
- 只接受 `parsed.md`、`manifest.json` 和受限图片类型；拒绝可执行文件和任意嵌套路径。
- 取消、超时和异常最终都必须回收任务资源。
- Marker 精确版本、代码/模型许可证、模型下载、离线缓存、CPU/GPU/内存、启动时间、OCR 行为
  和分发方式必须通过独立 Gate 后才能进入依赖或安装脚本。

## 7. PDF 与 HTML 解析

### 7.1 PDF

Marker Sidecar 输出：

- `parsed.md`
- PDF 内嵌图片
- `manifest.json`
- Parser 能可靠提供的页码、块或图片来源定位

Provider 规范化层必须修正 Markdown 中的图片引用，使其只指向本来源目录内已校验的
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

每个成功解析的来源默认触发一次 `synthesize_entry_page`：

1. Agent 读取该来源的 `parsed.md`、manifest 和需要的内嵌图片信息。
2. 生成一篇来源入口页，保留来源身份和可验证引用。
3. Agent 可以在同一 Change Set 中提出零到多个主题子页面。
4. Agent 可以提出固定类型的页面关系。
5. 所有内容保持草稿，不写入已发布页面或正式图谱。
6. 用户批准 Change Set 后统一发布。

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

1. 获取 Space 写锁并开始短 SQLite 事务。
2. 校验 `base_graph_revision`、所有目标页 version 和 before SHA。
3. 任一前提变化则整个 Change Set 标记 `stale`，不发布任何部分。
4. 校验 Markdown、slug、页面引用、图关系类型、同 Space 约束和 `part_of` 无环性。
5. 在一个事务内写入全部 immutable revisions、更新 current revision、应用 edges、更新
   `wiki_page_sources`、页面 FTS5 和 Space graph revision。
6. commit 后通过 durable outbox 幂等刷新 `pages/` 文件镜像和前端事件。

SQLite 是规范事实源，因此文件镜像写入失败不会产生部分数据库发布；恢复 Worker 会根据已提交
revision 重建镜像。拒绝和 stale Change Set 永不修改正式页面、FTS 或图谱。

## 10. 页面级 FTS5

FTS5 只索引 active 页面当前已批准 revision：

- `page_id`、`space_id` 为 unindexed identity 字段。
- `title`、`aliases`、`content` 为检索字段。
- 查询必须由服务端固定到当前 `space_id`，Agent 不能请求跨 Space 搜索。
- 搜索结果返回页面 ID、标题、匹配摘要和 current revision，不返回 Chunk ID。
- 草稿、已删除页面和 Raw Markdown 不进入正式索引。

FTS5 是 Wiki 导航和页面发现能力，不恢复旧 Chunk RAG 主链路。

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
5. 启用新 Wiki API；旧 API 返回稳定的 retired/gone 响应，不静默映射到新接口。
6. 经过独立用户授权后才能删除 legacy 备份。

## 14. 实施阶段

### 阶段 0：产品合同冻结

- 完成本设计和 TODO 执行顺序。
- 把旧 P2-R RAG 文档标记为历史依据，不修改其归档内容。

### 阶段 1：Marker Gate 与 Provider Contract

- **已完成**：重新审计 Marker `2.0.0` 的代码/模型许可证、依赖、资源、网络和分发边界。
- **已完成**：冻结容器 Sidecar 的安装、许可证 Gate、探测、取消、超时、回收和 artifact contract。
- **已完成**：提供独立 `wiki_parser` Protocol/DTO/固定错误与完全离线 Fake；尚不接入 Wiki Store。

### 阶段 2：WikiStore 与新目录

- **已完成**：新建 `wiki.db` schema v1、三重身份/version gate、路径安全 Store 和 Raw/Page
  原子镜像/恢复原语。
- **已完成**：实现显式旧库一致性只读备份与新库初始化，不迁移旧业务数据；当前运行时仍使用
  preserve 模式，真实 retirement 留到旧 Worker/Store 关闭后的切换点。
- **已完成**：实现带 CAS 和软删除状态机的 Space CRUD。

### 阶段 3：PDF/HTML Raw Ingestion

- 实现上传限制、不可变 source、Parser job、artifact 校验、恢复和删除。
- 接入 Marker Sidecar 与独立 HTML Parser。
- 前端先支持 Space、Source、状态和 Raw artifact 浏览。

### 阶段 4：Wiki 页面与 Change Set

- 实现入口页生成、主题子页面提案、immutable revision、统一 diff 和原子审批发布。
- 实现 stale conflict、拒绝、恢复和页面文件镜像。

### 阶段 5：页面 FTS5 与知识图谱

- 实现仅针对已批准页面的 FTS5。
- 实现固定关系、`derived_from`、无环校验、graph revision 和图谱 API。

### 阶段 6：Knowledge Agent 与多对话

- 实现 Knowledge 模式、Prompt、Skill、工具白名单、Space 可信绑定和多对话。
- 复用流事件、持久化、compaction 和引用机制，但不恢复 Chunk RAG。

### 阶段 7：独立 Knowledge 页面与最终验收

- 实现 Pages/Sources/Graph/Changes/Conversations 五个视图。
- 完成 Backend/Frontend/E2E、安全边界、崩溃恢复、真实 Marker 和大文件配额验收。
- 正式退役旧 Knowledge API、Worker 和前端弹窗。

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

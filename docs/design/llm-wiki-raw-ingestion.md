# LLM Wiki Raw Ingestion

> 状态：Raw Ingestion、MinerU Contract v2、主应用编排与源码 Gate 已实现；新 OCI 真实语料 Gate 待验证
>
> 校准日期：2026-08-26
>
> 未完成：阶段 3 最小前端

## 1. 边界

阶段 3 只把用户上传的 PDF/单文件 HTML 变为只读 Raw artifacts，不创建 Wiki 页面，不调用
Agent，不写入页面 FTS5 或图谱。旧 `web.knowledge` Chunk RAG 与新 `web.wiki` 继续并存且不共享
表、文件身份或 Worker。

主应用拥有 `wiki.db` 与 `spaces/**`；Parser Provider 只接收一份已核验原始 PDF 副本，并返回
不可变制品。Agent 在后续阶段只能获得 selected Raw revision 的读取工具，不获得任何 Raw 写入
或删除工具。

```text
POST PDF/HTML
  -> immutable raw/.../source.pdf|source.html
  -> durable Source + parse Job
  -> HTML: in-process deterministic parser (no transport API)
     PDF: injected provider-neutral ParserProvider v2
          -> fast / accurate / auto(original PDF only)
  -> validate every artifact byte as untrusted
  -> raw/.../parses/{revision}/... + selected.json
  -> atomic Attempt/Artifact rows + selected Source revision
```

## 2. Raw bundle

每个来源使用固定身份路径：

```text
spaces/{space_id}/raw/{safe-display-stem}--{source_id}/
  source.pdf | source.html
  selected.json
  parses/{parse_revision_id}/
    parsed.md
    pages/*.md
    manifest.json
    images/<content-addressed image>.png|jpg|webp
```

显示名仅用于 UI 与安全 ASCII stem；真正身份是 `source_id`。原件使用 no-clobber 原子写入，
Space 内同 SHA-256 来源由数据库唯一约束拒绝。解析产物也不覆盖不同字节；中断后仅允许验证
完全相同的先前部分写入并继续提交。

## 3. Source、Artifact 与 Job

- Source 状态：`uploaded -> parsing -> parsed|failed`，已 parsed 来源可创建新 Job 进入 parsing；
  失败重解析仍保留上一个 selected revision。另有显式 `deleting` 终态。
- Parse Job 状态：`queued -> running -> succeeded|failed|cancelled`。
- `begin_parse_job` 在一个 `BEGIN IMMEDIATE` 内校验 Source、分配递增 attempt、插入 Job 并把
  Source 改为 `parsing`；两个连接不能同时 claim 同一来源。
- Job 固化 `requested_mode`、`base_selected_parse_revision_id` 与 `base_selection_version`；发布时
  必须 CAS 命中，防止跨进程晚到结果覆盖更新 selection 或产生 ABA。
- Source 只有在所有目标文件被回读并重新核验 size/SHA 后，才能在同一 DB transaction 中插入
  ParseAttempt、ParseRevision、revision-scoped Artifact rows，切换 selected pointer 并变为
  `parsed`。Job 在同一事务中成功，不存在 Source 已发布而 Job 仍 running 的持久状态。
- Provider handle 不作为可重放事实。启动时旧 `queued/running` Job 固定收敛为
  `failed/provider_unavailable`，Source 变为可重试 `failed`，Worker 再创建新 attempt；旧 handle
  不会被恢复或冒充继续执行。

## 4. 上传契约

- 只接受后缀和类型一致的 `.pdf`、`.html`；不接受 `.htm`。
- PDF 必须以 `%PDF-` 开始；HTML 必须是严格 UTF-8（允许 UTF-8 BOM）。
- 默认 PDF 上限 250 MiB，HTML 上限 25 MiB；HTTP multipart 按块读取并在越界时立即停止。
- 浏览器提交的 `Content-Type` 不作为类型事实，服务端由受限后缀推导，再做内容校验。
- 错误只返回固定 code/message，不返回绝对路径、来源正文、raw exception 或 traceback。

## 5. HTML Parser

内置 HTML Parser 只接收 bytes，没有 URL、socket、HTTP client、外部文件路径或回调接口，因此
解析过程本身没有发起网络请求的能力。

固定行为：

- 删除 `script/style/noscript/template/iframe/object/embed/svg/math/form` 及其内容。
- 不下载 CSS、外链图片、iframe、相对资源或任何站点依赖。
- 只保留 `http/https/mailto` 与普通相对链接；拒绝 `javascript:` 等 scheme，并编码 Markdown
  link destination 中的危险分隔符。
- 来源正文中的 Markdown 元字符全部转义，避免 HTML 文本伪造远程 Markdown 图片或链接。
- 只提取 base64 `data:` PNG/JPEG/WebP；同时校验 MIME、后缀、魔数、单图大小、总图片字节与
  数量，按 SHA-256 命名并去重。
- 输出严格 UTF-8 `parsed.md` 与 canonical `llm-wiki-html-artifact/v1` manifest。

## 6. PDF artifact 信任边界

Provider 的成功状态、receipt、tar 与 manifest 都不是可信事实。导入器按以下顺序 fail closed：

1. archive 必须是稳定的绝对普通文件，size/SHA 必须匹配 receipt，并受总量上限约束。
2. 只接受未压缩 tar 中的普通文件；拒绝目录、链接、设备等特殊 member。
3. 所有路径必须是规范相对 POSIX path；拒绝 `..`、绝对路径、反斜杠、控制字符和大小写重复。
4. tar 的文件集合必须与 manifest 完全相等，不能有未声明或缺失文件。
5. manifest 必须匹配 exact job/source/source SHA、当前 provider name/version 和固定 contract。
6. 重算 manifest、Markdown、每张图片的 size/SHA；Markdown 必须严格 UTF-8；图片必须匹配
   PNG/JPEG/WebP MIME 与魔数并满足配额。
7. 全部验证成功后才把文件写入来源 bundle；DB 最终 transaction 再回读每个文件后提交。

MinerU、Torch、模型运行时与容器 SDK 均不得进入主应用依赖。当前 PDF
集成测试使用完全离线 Fake Provider：Contract v1 只验证兼容 orchestration；Contract v2 Fake
验证三种固定档位、单次 attempt、逐页 artifact 与质量证据，但不代表真实 Parser 输出质量。
v1 兼容导入会把合并 Markdown 置于
`pages/000001.md`，其余已知页使用空页占位，以明确表达 v1 没有可靠逐页正文。

Wiki schema 已升级为 v2。Source 只保存原件身份和 `selected_parse_revision_id / selection_version /
selected_at_ms`，解析器、preset、配置、质量和失败证据由 immutable ParseAttempt/ParseRevision
持有。`selected.json` 是可重建镜像，不是第二事实源；启动时按 DB 修复缺失或损坏内容。旧 schema
v1/flat bundle 打开时返回固定 `schema_rebuild_required`，必须由操作者先备份并显式重新初始化
整个 Wiki 根，不做原地 ALTER、目录搬运或静默覆盖。

## 7. Lifespan 与 API

`create_app(wiki_root=...)` 以 `legacy_policy="preserve"` 打开独立 WikiStore，启动单并发
WikiIngestionWorker，并在 Store 前停止 Worker。它不要求 `knowledge_root`，因此可在不启动旧
Knowledge Worker/FTS/Agent Tool 的情况下单独运行。

受现有 localhost UI-header/origin envelope 保护的 `/api/wiki` 提供：

- Space 创建、列表、读取和修改；
- Source 上传、列表、读取和重新排队；
- Artifact metadata 与经 size/SHA 回读后的安全内容浏览；
- Job 状态读取。

HTML 上传会自动排队。没有 PDF Provider 时，PDF 保持 `uploaded` 且 `parse_queued=false`；显式
解析请求返回 `invalid_configuration`，不会把“运行时未配置”伪装成文档解析失败。

## 8. MinerU 配置 Gate

现行路线见 [`llm-wiki-mineru-parser.md`](llm-wiki-mineru-parser.md)。已确认：

1. 不恢复 Chunk-RAG；解析页只用于 provenance，最终检索仍是已批准 Wiki 页面 FTS5。
2. Worker 自身 MIT，MinerU 使用独立许可；Source archive、notices、SBOM 与锁必须同步。
3. MinerU 版本、完整传递依赖、模型获取方式和固定 profile 映射必须可审计。
4. 发布实现为持久、断外网 OCI Worker；每个 Job 拥有隔离临时目录和原始 PDF 副本。
5. 真实语料必须覆盖 `pipeline|gpu-medium|gpu-high`、取消、超时、崩溃清理、SHA 篡改、断网与资源上限。

Contract v2 和源码配置 Gate 已完成；真实 OCI Gate 尚未复验，`runtime_ready=false`。Worker
不可用或 GPU 档位缺少 CUDA 时，PDF 解析继续 fail closed。

## 9. 未完成

- 新 MinerU OCI Worker 待完成模型物化、CPU/GPU、断网、隔离、代表性语料和取消恢复 smoke；通过后才可设置 `runtime_ready=true`。
- Space/Source/状态/Raw artifact 最小前端；完整 Pages/Graph/Changes/Conversations 属于阶段 7。
- Source 物理删除/保留策略；当前仅有 `deleting` 软状态，避免在 retention 合同冻结前删除 Raw。
- 页面入口生成、Agent 总结、Change Set、FTS5 和图谱，分别属于阶段 4–6。

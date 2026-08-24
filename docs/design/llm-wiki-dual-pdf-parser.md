# LLM Wiki 双 PDF Parser 设计

> 状态：架构决策、Contract v2、Raw parse revisions、离线 Fake v2、Contract v2 主应用编排与 AGPL Worker 合规 scaffold 已实现；真实运行时待实现
>
> 日期：2026-08-24
>
> 取代：Marker Provider 的后续实施路线
>
> Parser：PyMuPDF4LLM 快速路径 + Docling 高质量路径/自动回退
>
> 检索边界：不恢复 Chunk-RAG；最终消费者仍是 Wiki 页面、页面 FTS5 与页面图谱

## 1. 冻结决策

1. 原始 PDF 是唯一事实来源。任一 Parser、质量检查或回退都不能把另一个 Parser 的 Markdown
   当作输入。
2. `fast` 只运行 PyMuPDF4LLM；`accurate` 只运行 Docling；`auto` 先预检并按复杂度路由，快速
   路径质量不足时用原始 PDF 重新运行 Docling。
3. PyMuPDF4LLM 快速路径固定关闭 OCR。扫描件不在快速路径中隐式下载或调用 OCR，而是由
   `auto` 路由或质量回退进入 Docling OCR preset。
4. Docling Worker 启动时构建并预热两个可复用 Converter：带文本层的 `standard` 与扫描件的
   `ocr`。Converter 不按请求重复创建；调用受并发队列和任务配额保护。
5. 两个 Parser 的原始结果都必须规范化为同一个 `ParsedDocument` Contract v2；上层 Wiki
   Ingestion 不 import 或感知两个库的具体 DTO。
6. 每次解析保存请求模式、路由原因、Parser 名称/版本、preset、配置 revision/SHA、耗时、
   质量报告、失败码、回退关系和最终选择，不能只保存最终 Markdown。
7. PyMuPDF4LLM 采用用户确认的 AGPL-3.0 路径。Parser Worker 的源码、构建定义、修改和对应
   Source Offer 必须随可交付运行时提供；主项目的 MIT 声明不能覆盖或抹去该组件义务。
8. Docling 代码许可证与具体布局、表格、OCR 模型许可证分开审计。只有实际 preset 引用的模型
   及 OCR 引擎通过版本、hash、许可证和离线运行 Gate 后才能进入发布镜像。
9. PDF Parser 保持在独立运行时，不装入主应用 Python 环境。发布默认使用持久 OCI Worker；
   独立 venv 进程仅为显式标记的开发降级模式。
10. 本设计不创建文档 Chunk、embedding、向量库或 Chunk 检索 API。PDF 页是 provenance 单位，
    不是 RAG Chunk；检索仍只针对已批准 Wiki 页面。

## 2. 工作流

```text
上传 PDF
  -> 不可变写入 source.pdf + size/SHA-256
  -> 安全检查与 PyMuPDF 只读预检
  -> requested_mode
       fast     -> PyMuPDF4LLM(original PDF)
       accurate -> Docling preset(original PDF)
       auto
         -> scanned/complex -> Docling preset(original PDF)
         -> simple digital  -> PyMuPDF4LLM(original PDF)
                                -> quality pass -> selected
                                -> quality fail -> Docling(original PDF)
  -> 每个 attempt 规范化为 ParsedDocument + QualityReport
  -> 选择一个成功 attempt
  -> 不可信 Artifact v2 校验与 Raw parse revision 发布
  -> 来源入口 Wiki 页面/主题页 Change Set
  -> 用户审批
  -> 页面 FTS5 + 页面图谱
```

明确禁止：

```text
original.pdf -> PyMuPDF4LLM -> markdown -> Docling
```

Docling 的输入只能是与 Source 行中 `source_sha256` 一致的原始 PDF 副本。

## 3. API 模式语义

```text
parse_mode = auto | fast | accurate
```

| 模式 | 首选 Parser | 自动回退 | 语义 |
|---|---|---:|---|
| `fast` | PyMuPDF4LLM | 否 | 调用者明确选择低延迟；质量失败则整个 Job 失败并返回固定错误 |
| `accurate` | Docling | 不适用 | 预检只用于选择 `standard`/`ocr` preset，不先运行快速 Parser |
| `auto` | Router | 是 | 生产默认；简单数字 PDF 先 fast，扫描/复杂 PDF 直接 accurate |

上传和显式重新解析 API 都接受 `parse_mode`，缺省为 `auto`。模式属于 Parse Job，而不是 PDF
原件身份；同一不可变 Source 可以创建多个 parse revision，但任一时刻只允许一个 running Job。
重新解析不能覆盖既有成功制品，只能新增 attempt/revision 并在完整校验后切换 selected pointer。

## 4. 独立运行时

### 4.1 进程边界

主应用只依赖 `wiki_parser` 的 immutable DTO、Protocol 和固定错误，不依赖 PyMuPDF、
PyMuPDF4LLM、Docling、Torch、OCR 引擎或模型 SDK。

发布运行时为一个持久、断外网的 OCI Worker：

- 启动期加载固定版本依赖、离线模型和 versioned routing config。
- 创建 `docling_standard` 与 `docling_ocr` 两个 Converter，并执行受控 warmup/probe。
- 通过本机受控 IPC 接收有界 Job，不接受 URL、任意路径、任意 Parser 参数或环境变量。
- 每个 Job 使用独立临时目录；Worker 只能访问该 Job 的原件副本和临时输出。
- 根文件系统与模型只读；Job 工作目录为受大小限制的临时空间。
- 无云凭据、LLM key、Workspace、`wiki.db`、`pages/` 或其他 Source 目录访问能力。
- 任务期间无外网；模型下载只允许出现在显式管理员 build/install 阶段。

持久 Worker 是为了复用 Docling Converter/模型，不意味着复用 Job 目录或跨任务内存结果。
默认先以单并发运行；确认 Converter 线程安全和资源基准后，才能通过配置增加隔离实例数。进程
崩溃后所有未完成 attempt 固定失败，由主应用创建新 attempt，不能冒充恢复旧结果。

### 4.2 AGPL-3.0 边界

PyMuPDF4LLM/PyMuPDF 所在 Worker 采用 AGPL-3.0 合规路径：

- Worker 目录包含完整 AGPL-3.0 文本、版权和第三方 notices。
- 构建产物记录 PyMuPDF4LLM/PyMuPDF 精确版本、wheel hash、源码位置与构建脚本。
- 对外提供运行服务时，在应用 License/About API 和 UI 中提供对应源码获取入口。
- 修改过的 Worker、协议适配与构建文件进入 Corresponding Source，不只提供上游链接。
- SBOM 明确区分主应用 MIT 代码、AGPL Worker 和 Docling/模型的独立许可证。

这是工程合规 Gate，不代替法律意见；最终分发形态仍需在发布前复核许可证范围。

当前已实现的 Gate 位于 `workers/wiki_parser_worker`：它是独立 `AGPL-3.0-only`、
`runtime_ready=false` 的可构建包，包含未经改写的 GNU AGPLv3 正文、notices、精确文件 manifest、
SOURCE_OFFER、SPDX 2.3 SBOM、自包含 smoke 与 wheel verifier。主应用只读取该目录的合规资产，
不 import Worker；`/api/about` 和侧栏 **About & Source** 从同一个稳定字节快照提供逐文件/tree/archive
SHA 与确定性源码 tar。缺少源码挂载时下载入口 fail closed。当前 Source Offer 只对应合规 scaffold；
后续加入真实依赖、adapter、配置、模型和镜像构建文件时，必须同步扩展 manifest/SBOM/notices 与
Corresponding Source，不能沿用旧归档冒充新运行时源码。

## 5. 预检与路由配置

预检使用 Worker 内已经由 PyMuPDF4LLM 依赖的 PyMuPDF，不另外引入 `pypdf`。它只读取原始 PDF，
生成特征，不产生正式 Markdown。

### 5.1 安全预检

- 文件仍是稳定普通文件，size/SHA 与 Job Spec 一致。
- PDF header、页数、加密/密码、损坏状态和页尺寸受限。
- 超页数、超像素预算、异常对象数或解析超时固定失败。
- 不解析 PDF 内的 URL，不执行 JavaScript、Launch、附件或外部引用。

### 5.2 路由特征

逐页统计：

- 可提取字符数、word/block 数和有文本页比例。
- 页面栅格图片数量、覆盖面积与整页图片比例。
- vector drawing、表格候选、字体数量和旋转文本。
- 文本块横向分布，用于多栏复杂度信号。
- 页面尺寸异常、页数和各页信号分布。

Router 输出固定原因码，例如：

```text
simple_digital
scan_text_layer_missing
scan_image_dominant
complex_multicolumn
complex_table_dense
complex_mixed_layout
```

### 5.3 不硬编码阈值

所有阈值位于 extra-forbid、版本化配置 `PdfRoutingConfig`，并记录：

- `schema_version`
- `config_revision`
- canonical JSON SHA-256
- 预检配额
- scanned/complexity 特征权重与阈值
- fast quality critical gates、各指标权重与通过分数
- Docling preset 与 OCR 语言映射

代码只实现指标和比较逻辑。默认配置作为发布资产随 Worker 构建并受 hash 校验；管理员只能选择
经过测试的 preset/revision，不能从 API 传入任意阈值。初始数值必须通过真实语料基准校准，不能
把示例数字直接当生产合同。

## 6. Parser 行为

### 6.1 PyMuPDF4LLM fast

固定原则：

- `page_chunks=True`，每个返回项映射到一个 `ParsedPage`。
- `use_ocr=False`、`force_ocr=False`，避免 fast 隐式变成 OCR 路径。
- 图片写入受控临时目录，随后按 MIME/魔数/大小/SHA 规范化。
- 只允许经过版本 Gate 的参数集合；API 不透传 `to_markdown` kwargs。
- 安装版本的实际字段通过 adapter test 固定，升级时重新生成 capability snapshot。

fast 抛错、漏页或质量不合格时：`auto` 用原始 PDF 进入 Docling；显式 `fast` 则终止为
`quality_rejected` 或相应固定 Parser 错误，不静默改变用户请求语义。

### 6.2 Docling accurate

启动期维护两个 Converter：

```text
docling_standard:
  do_ocr = false
  do_table_structure = true
  table mode = accurate

docling_ocr:
  do_ocr = true
  do_table_structure = true
  table mode = accurate
  OCR engine/languages = audited preset
```

`accurate` 和 auto fallback 都依据同一次可信预检选择 preset。OCR engine、语言、layout/table
模型、设备、线程数、batch 和 image scale 全部来自固定配置 revision。公式/代码/VLM enrichment
首版默认关闭，除非其模型、许可证、资源和质量另行通过 Gate。

Docling 的 `ConversionResult`、`DoclingDocument`、图片和 provenance 由 adapter 转换为统一 DTO；
它们不能直接越过 Worker 协议进入主应用。

## 7. 统一 ParsedDocument Contract v2

概念模型：

```text
ParsedDocument
  contract_version = 2
  source_id / source_sha256
  requested_mode
  preflight / route_decision
  selected_parser / parser_version / preset
  config_revision / config_sha256
  page_count
  pages[]
    page_number
    markdown
    plain_text
    source_spans[]       # 可用时包含 page/bbox/block identity
  assets[]
    kind                 # embedded_image | table_image
    mime_type / size / sha256
    page_number / bbox   # 可用 provenance
  quality_report
  attempt_summary
```

`ParsedPage` 是 PDF provenance 和引用单位，不是检索 Chunk。规范 `parsed.md` 由 pages 按页码稳定
拼接，页边界使用不可歧义的内部标记；主应用同时核验合并 Markdown 与逐页条目的 SHA。

当前实现位于独立 `wiki_parser.contract_v2` 与 `wiki_parser.provider_v2`。Job Spec 只能选择
`routing_config revision + SHA`，不能携带阈值或底层 Parser kwargs；Preflight、RouteDecision、
每个 Attempt、最终 ParsedDocument 和 Artifact manifest 通过 source/config/preflight SHA、Parser
identity、连续 ordinal 与 fallback reference 交叉约束。Contract v1 暂时保留给尚未迁移的 Raw
Ingestion，v1/v2 使用不同 schema/Protocol，不做隐式转换。

## 8. 质量检查

所有成功 Parser 都产生 QualityReport；auto 的 fast 结果必须通过 critical gates 和总分门槛才可
被选择。至少包含：

| 指标 | 检查 |
|---|---|
| `text_page_coverage` | 有效输出页面占输入页数比例 |
| `replacement_char_ratio` | `U+FFFD` 比例；出现即可由配置设为 critical fail |
| `control_char_ratio` | 非法/异常控制字符比例 |
| `content_completeness` | 输出字符、word 与预检文本层基线是否异常少 |
| `repetition_ratio` | 大量重复页眉、页脚、整段或页面 |
| `page_count_match` | ParsedPage 数与 PDF 页数是否一致 |
| `markdown_health` | fenced code、表格、公式/链接/图片标记是否明显不闭合 |
| `asset_reference_health` | Markdown 图片引用是否都指向已声明 artifact |

QualityReport 保存每个原始指标、critical failure codes、权重、总分、门槛和配置 SHA；不能只保存
一个不可解释的总分。Docling 结果仍做相同检查，但 Docling 失败后首版不再回到 fast，避免循环。

## 9. 持久化与 Raw 目录

原件与解析 revision 分离：

```text
raw/{safe_stem}--{source_id}/
  source.pdf
  parses/
    {parse_revision_id}/
      manifest.json
      parsed.md
      pages/
        000001.md
      images/
        <content-addressed>.png|jpg|webp
  selected.json
```

新增或扩展可信记录：

- Parse Job：requested mode、routing config revision/SHA、状态和 selected attempt。
- Parse Attempt：ordinal、Parser/version/preset、route reason、fallback_from、耗时、错误码、
  quality JSON、artifact receipt。
- Parse Revision：不可变 artifact 根、manifest/SHA 和发布时刻。
- Source：只保存当前 `selected_parse_revision_id`，CAS 切换；历史 revision 不覆盖。

快速结果质量失败时可以保留 attempt 元数据和质量报告，但失败输出正文不发布到 Agent 可读 Raw。
成功但未被选择的完整制品是否保留由 retention 配置决定，不能与 selected artifact 混淆。

## 10. 与 LLM Wiki 的边界

Parser 完成后不进入 Chunker。下游流程是：

1. Agent 通过只读工具读取 selected ParsedDocument、指定页和资产。
2. 生成来源入口 Wiki 页和可选主题子页 Change Set。
3. 用户批准后发布 immutable Wiki page revisions 与页面关系。
4. 只把已批准页面当前 revision 写入页面级 FTS5。
5. 引用保存 `source_id + parse_revision_id + page_number + 可选 bbox/block`。

Raw PDF page、parsed page、未批准草稿和历史 parse revision 均不进入正式 FTS5。

## 11. 实施顺序

1. 撤销 Marker 专属实现并把旧 Gate 标记为历史。
2. **已完成**：将 `wiki_parser` 升级到 Contract v2：三种模式、预检/路由/质量 DTO、
   同源 attempt evidence、逐页 ParsedDocument、规范 Markdown 与 Artifact v2；保留 v1 作为
   当前 Raw Ingestion 的显式兼容层。
3. **已完成**：更新 Wiki schema 与 Raw 目录，使多 attempt、不可变 parse revision、逐页制品
   和 selected CAS pointer 可持久化；旧 flat v1 明确要求重建。
4. **已完成**：实现完全离线 Fake Router/Fast/Accurate Provider；任务创建时冻结已核验原始
   PDF 字节，三种模式、直接 Docling、fast 成功、auto 单次 fallback、显式 fast 拒绝、
   Docling 终止、取消/销毁、逐页 tar 和同源 SHA 均有可重复测试。
5. **已完成**：接入现有 API/Worker、取消/超时/崩溃恢复和不可信 Artifact v2 导入；三种模式精确贯穿上传、排队与恢复，每个 attempt/route/quality 和 revision 原子持久化，拒绝制品不发布 selected revision。
6. **已完成**：建立独立 AGPL Worker 合规包、许可证/notices、当前 scaffold 的完整
   Corresponding Source、确定性 Source Offer API/UI、SPDX 2.3 SBOM 与 wheel 内容 Gate；保持
   `runtime_ready=false`，不提前加入或伪装具体 Parser runtime。
7. 固定 PyMuPDF4LLM、PyMuPDF、Docling、OCR 和模型版本/hash，构建离线 OCI runtime。
8. 实现 PyMuPDF 预检、PyMuPDF4LLM adapter、QualityEvaluator 和 Docling 两个 preset。
9. 用真实语料校准配置并执行 fast/accurate/auto/fallback、安全、断网和资源 smoke。
10. 完成阶段 3 最小前端，然后继续 Wiki 页面/Change Set。

## 12. 验收不变量

1. Docling 从不接收 PyMuPDF4LLM Markdown，只接收原始 PDF。
2. `fast` 不启用 OCR、不自动回退；`auto` 才允许 fast → Docling。
3. 扫描/复杂预检可以直接路由 Docling，不浪费一次 fast。
4. Converter 在 Worker 启动期创建并预热，任务结束不销毁 Converter，但必须清空任务目录和引用。
5. 所有阈值来自有 revision/SHA 的固定配置，API 不能注入阈值或 Parser kwargs。
6. 每个 attempt 的 Parser/version/preset/config/route/quality/耗时均可追踪。
7. `U+FFFD`、漏页、超配额、异常重复或不健康 Markdown 不能被 auto fast 路径选中。
8. 主应用环境不 import PyMuPDF、PyMuPDF4LLM、Docling、OCR 或模型运行时。
9. Worker 无外网、凭据、Wiki DB、Pages 或其他 Job 原件访问能力。
10. Artifact v2 仍按不可信输入逐文件校验，不信任 Worker manifest 或 success 状态。
11. AGPL Worker 的完整对应源码、许可证、修改与获取入口随网络服务/分发提供。
12. 不创建 Chunk 表、Chunk FTS、embedding、向量库或 Chunk 检索工具。

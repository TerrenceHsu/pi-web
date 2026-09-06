# Workspace 与 Coding Sandbox 一体化设计

> 状态：阶段 1–3、4A–4C 已完成；阶段 4 Sandbox/Workspace 主链路闭环
> 日期：2026-08-22；上传布局更新：2026-09-05（见 [上传与媒体设计](workspace-upload-media.md)）
> 范围：Session Workspace、Agent 文件工具、E2B Coding Sandbox、安全发布与后续文档转换

## 1. 目标

每个 Session 拥有一个持久、隔离、可在 UI 中浏览的 Workspace。新 Workspace 立即包含
`AGENT.md` 与 `Memory.md`；所有上传原件统一进入首次成功上传时创建的 `upload/`。
工作代码进入 `scripts/`，该目录只在 Agent 首次写代码或从原件创建工作副本时出现。
Agent 可以自行决定 `scripts/` 内部目录树，
服务端负责安全边界、版本冲突和事务提交。

代码只能在托管 Sandbox 中执行和验证。Sandbox 使用 Workspace 的不可变快照，验证成功后
冻结制品，再由本机 Publisher 事务发布回同一个 Workspace。真实 Workspace 不挂载到云端，
验证失败、冲突、取消或发布中断都不得留下半套修改。

## 2. 核心原则

1. **唯一事实源**：`WorkspaceStore` 是 Session 文件树的唯一持久事实源；上传、Markdown、
   Agent 文件工具、Sandbox 快照和发布都通过它读写。
2. **执行与持久化分离**：E2B 是临时执行副本，不是第二个 Workspace；操作结束后销毁。
3. **固定根文件**：规范大小写为 `AGENT.md`、`Memory.md`。Windows 大小写不敏感环境下不得
   创建仅大小写不同的重复文件。
4. **惰性代码目录**：空 Workspace 只显示两个根 Markdown 文件；`scripts/` 在第一份代码
   到来时创建。
5. **服务端守边界，模型定结构**：模型可决定 `scripts/**` 的语言和层级，但不能写出
   Workspace、改写系统隐藏状态或绕过验证。
6. **乐观并发与可恢复提交**：写入携带 `expected_sha256`/Workspace revision；发布使用
   immutable artifact、冲突检查、journal、原子替换与崩溃恢复。
7. **富文档先转换后处理**：PDF、Word、Excel 的原始文件不可变；固定转换器生成 Markdown、
   CSV、图片和 manifest 后，Agent 只消费转换结果。

## 3. 用户可见目录树

新 Session：

```text
workspace/
├── AGENT.md
└── Memory.md
```

产生代码和文档后：

```text
workspace/
├── AGENT.md
├── Memory.md
├── upload/
│   ├── report.pdf             # 不可变上传原件
│   └── app.py                 # 上传代码同样作为原件保存
├── scripts/
│   ├── app.py
│   ├── tests/
│   │   └── test_app.py
│   └── ...                    # 由 Agent 决定
├── docs/
│   └── notes/design.md
└── documents/
    └── <document-id>/
        ├── content.md         # 规范可读正文
        ├── manifest.json      # 来源、转换器版本和输出 hash
        ├── tables/
        │   └── <name>.csv
        └── assets/
            └── <image>
```

`.pi-agent/**` 为系统隐藏空间，不出现在普通文件树中，也不接受 Agent 或上传 API 的直接写入。

## 4. 所有权与写入策略

| 路径 | 所有者 | 写入规则 |
|---|---|---|
| `AGENT.md` | 用户 / 专用 API | 初始化时创建；可带 SHA 更新；不可删除或由 Sandbox 覆盖 |
| `Memory.md` | Checkpointer / 用户 | 初始化时创建；以 immutable generation 更新；不可删除或由 Sandbox 覆盖 |
| `upload/**` | 用户 | 新上传原件的统一根；禁止原地改写及 Sandbox 发布 |
| `scripts/**` | Agent / 用户 | 工作代码写入的默认根；上传代码需先复制为工作副本 |
| `notes/**` 及其他安全 Markdown 路径 | Agent / 用户 | 支持创建、读取、更新、移动和删除 |
| `documents/*/original.*` | 系统 | 兼容历史原件；不再接收新上传，不可变，不能执行 |
| `documents/*/content.md`、`tables/**`、`assets/**` | 固定转换器 | 由转换任务重建，Agent 只读或基于其另建派生文件 |
| `.pi-agent/**` | 系统 | 隐藏且禁止外部直接访问 |

## 5. WorkspaceStore 模型

阶段 1 在既有 metadata-per-file 存储上建立规范 `WorkspaceStore` 名称，并保留
`VirtualFileStore` 兼容别名。已有 `uploads/{session_id}` 数据不复制、不改路径，避免产生
双事实源；启动时幂等补齐缺失的 `Memory.md`，并把大小写等价的旧根文件规范为固定路径与
purpose。阶段 2 在每个 Session 目录中增加隐藏 `.workspace.json` 状态文件，持久化单调
revision；该文件不进入用户文件树或容量统计。后续仍可在不改变 API 的前提下迁移物理布局。

当前模型字段：

- Workspace：`session_id`、`revision`、`created_at`、`updated_at`。
- Entry：`id`、`logical_path`、`kind`、`origin`、`purpose`、`mime`、`size`、`sha256`、
  `created_at`、`updated_at`。
- 每次提交生成不可变内容 generation；metadata pointer 是单文件 commit point。
- 批量发布以 Workspace revision 和逐文件 before SHA 作为冲突前提。

## 6. 上传与 Agent 文件工具

### 6.1 文件上传

- 所有新上传默认逻辑路径为 `upload/<filename>`，包括代码和 PDF/DOCX/XLSX 原件。
- 上传 API 可接受安全的 `relative_folder`，最终强制位于 `upload/`。
- 路径统一为相对 POSIX 形式；拒绝绝对路径、`..`、反斜杠、控制字符、保留路径、symlink、
  junction/reparse point 和大小写冲突。
- 同名自动生成带 `(2)` 等编号的不重名路径，不静默覆盖。
- 上传仅持久化，不在本机执行。
- 逻辑目录随首份成功上传出现，物理 file-ID 存储布局不迁移；Sandbox 物化得到 `/workspace/upload/**`。
- 新文档输出为 `documents/<stem>-<source-id>/**`；旧 `documents/<id>/original.*` 重解析仍使用原输出根。

### 6.2 Workspace 工具

逐步提供 `workspace_list`、`workspace_read`、`workspace_write`、`workspace_patch`、
`workspace_mkdir`、`workspace_move`、`workspace_delete`、`workspace_diff`。代码写入强制位于
`scripts/**`；`AGENT.md`、`Memory.md` 和转换产物使用专用权限规则。修改型工具必须串行，并
接受 `expected_sha256` 或 Workspace revision。

## 7. Sandbox 工作流

```text
WorkspaceStore snapshot
        ↓
E2B 临时 /workspace
        ↓
Agent 写代码 / 运行命令
        ↓
固定 validation plan
        ↓
冻结 + 签名 artifact
        ↓
用户查看 diff 并确认
        ↓
事务发布到 WorkspaceStore
        ↓
revision/event 更新，右侧文件树刷新
```

Sandbox 只获得可公开的 Workspace 快照，不获得本机绝对路径、密钥、`.git`、`.env` 或系统
隐藏状态。任何代码执行都在 E2B 中完成；Publisher 在锁内重新校验 baseline、artifact、签名
和当前 Workspace hash。验证失败或 SHA 冲突时 Workspace 字节级不变。

## 8. 右侧 Workspace 面板

桌面布局改为左侧 Sessions、中间 Chat、右侧 Workspace；窄屏把右侧面板降级为 drawer。
右侧栏首先是 Agent 成果的交付面：Agent 完成或更新 `.md`、`.py` 等 Workspace 文件后，
前端依据 `write_file` ToolResult 或用户 mutation 响应中的 Workspace revision 自动刷新文件树，并选中或提示最新成果；用户无需
从聊天文本中寻找物理路径，也不需要手动刷新页面。
面板包含：

- **Files**：目录树、Agent 最新成果提示、上传、新建 Markdown、Markdown 预览/编辑、代码查看、
  下载与固定根文件编辑。
- **Sandbox**：当前 operation、运行日志和固定验证结果。
- **Changes**：发布前 diff、冲突提示、确认发布和历史结果。

现有 Coding Sandbox Modal 的状态、日志、diff 与审批组件应迁入或复用到右侧面板，避免形成
第二套状态管理。

## 9. PDF、Word、Excel 固定转换

Agent 不能把二进制正文直接提交给 LLM。上传后由服务端固定转换服务执行
`convert_workspace_document(entry_id)`；CPU/压缩包解析在线程中完成，上传 API 等待本次结果，另提供
幂等 retry API：

- PDF：用 `pypdf` 提取分页文本、页码映射和内嵌图片；扫描件标记 `needs_ocr`。首版不伪造
  结构化表格识别，而是在 manifest 写入 `pdf_tables_not_structurally_extracted`；OCR 和高质量表格解析后续单列。
- DOCX：用 `python-docx` 转换标题层级、段落、列表和表格到 Markdown，并提取内嵌图片。
- XLSX：生成 workbook 摘要，每个 sheet 输出 CSV 与 schema/范围信息；不执行宏和公式。

`manifest.json` 固定记录原件 SHA、转换器名称/版本、状态、warning、生成文件及各自 SHA。
原件永不就地改写，重复转换可按 source SHA + converter version 幂等复用。

实施边界：该模块现位于 `agent_workspace/documents.py`，只依赖 `WorkspaceStore`，不 import Knowledge/
Wiki 业务模块，不共享 Wiki Raw、DB、Provider、Worker 或解析制品。转换前物化 revision-bound 快照；
发布时复用 Workspace durable intent/phase journal，并在目标 Session 锁内重验 revision、tree SHA 和
全部内容 SHA。一组新建/替换/删除只提升一次 revision；源 Workspace 并发变化时零写入。原件 purpose
为 `document_original`，生成物为 `document_conversion`，Web 编辑/移动/单文件删除、Agent 写入和
Sandbox 发布均不能覆盖。转换失败时首次只发布不含异常正文的失败 manifest；若已有成功版本则保留。

## 10. 实施阶段

### 阶段 1：统一 WorkspaceStore 与初始化 Memory.md

- 建立规范 `WorkspaceStore` API，保留 `VirtualFileStore` 同对象兼容别名。
- 新 Session 同时创建唯一根 `AGENT.md` 与 `Memory.md`。
- 启动时幂等补齐既有 Session 的缺失根文件，保留已有正文和 file id。
- 规范旧的大小写等价根路径与 purpose；两个根文件均不可通过删除 API 删除。
- 更新 Core/Web 测试和产品状态文档。

实施结果：已完成。`WorkspaceStore` 成为运行时规范类型，旧名称只作为同一类的兼容别名；
新 Session 和启动时扫描到的旧 Session 都会幂等补齐两个固定根文件，旧正文与 file id 保留，
两个根文件均受删除保护。物理 `uploads/{session_id}` 布局按设计暂不迁移，未产生第二份数据。

### 阶段 2：代码与 Markdown 规则

- 代码写入/上传默认进入惰性 `scripts/`；支持安全相对目录。
- 完成 Markdown 创建、更新、移动、删除及冲突 API。
- 引入 Workspace revision 与批量 mutation 契约。

实施结果：已完成。代码扩展名由服务端确定，未指定目录时映射到 `scripts/<filename>`；指定
安全目录时映射到 `scripts/<relative_folder>/<filename>`，已带 `scripts/` 的路径不会重复加前缀。
普通 Markdown 提供精确路径 create、正文 update、metadata-only move/rename 和 delete；创建/移动
不允许占用既有大小写等价路径，`AGENT.md` 与 `Memory.md` 不能作为普通文件创建、移动或删除。
上传/创建/更新/移动/删除每成功一次把持久 Workspace revision 提升一次，并可同时校验旧 revision
与逐文件 SHA；冲突返回 HTTP 409。现有 `write_file`/`list_files` 工具和前端 API/store 均传递最新
revision。物理内容仍保持 metadata-per-file 布局，逻辑 `scripts/` 不产生第二份文件。

### 阶段 3：右侧 Workspace 面板

- 三栏桌面布局与窄屏 drawer。
- Agent 创建/更新 `.md`、`.py` 等成果后，按 Workspace revision 自动刷新、提示并展示最新成果。
- 文件树、根文件编辑、上传、新建 Markdown、Markdown 预览/编辑、代码查看与下载。
- 复用 Sandbox/Changes 视图和统一 store。

实施结果：已完成。`AppShell` 在桌面使用 260/自适应/380px 三栏，较窄桌面收敛列宽，
1050px 以下把同一个 Workspace 组件变为右侧 drawer，避免维护两份状态。Files 视图直接监听
Chat Store 中成功 `write_file` 的 ToolResult，同时兼容 live result wrapper 和刷新后持久化 details；
按 `file_id` 与 revision 只刷新必要快照、自动选中最新 Agent 成果并显示 New 标记。Markdown 使用
禁用 raw HTML 的统一 renderer，可切换源码编辑并携带 SHA/revision 保存；代码以只读 UTF-8 源码
展示。面板上传不会进入聊天输入框的 pending attachments。Sandbox 与 Changes 读取既有统一 store，
展示状态、验证、日志和 diff，并复用完整 Modal 执行启动、验证、冻结、审批发布或丢弃。

### 阶段 4：统一 Sandbox 快照与发布目标

- 阶段 4A 已完成：以 [`coding-sandbox-state-machine.md`](coding-sandbox-state-machine.md)
  固定显式状态机、持久转换 CAS，以及 Validation→Freeze 的 fail-closed TOCTOU 屏障。
- 阶段 4B 已完成：从 WorkspaceStore 物化快照，不再使用独立项目目录作为产品事实源：
  - 在同一 Session mutation lock 内冻结 `WorkspaceState.revision` 和按 logical path 排序的 `FileRef`；
  - 从 immutable content generation 逐文件读取，复核 containment、regular-file、前后 stat、size 与 SHA；
  - 输出全新 staging 逻辑树及 canonical tree SHA，不复制 file id 目录、metadata 或 `.workspace.json`；
  - Sandbox 单独注入不可由用户伪造的 `.pi-agent/sandbox.toml`，再构建既有确定性 `ProjectSnapshot`；
  - baseline record 保存 Workspace revision/tree SHA 和 snapshot archive/manifest SHA，源 Workspace 后续变化不改写已创建 baseline；
  - 4B 完成时尚未接入 Publisher，因此 Workspace baseline 曾以 `publish_available=false` 暂停发布。
- 阶段 4C 已完成：
  - 主应用注入 provider-neutral Artifact Publisher；隔离镜像复用既有 `LocalTransactionalPublisher` 复验签名制品、baseline 和最终文件树，真实 Workspace 从不挂载到云端或镜像 Publisher；
  - 只允许 `scripts/**` 和普通 UTF-8 Markdown，固定拒绝 `AGENT.md`、`Memory.md`、`.pi-agent/**`、`documents/**`、非规范/大小写冲突路径及 link/reparse；
  - 在同一 Session mutation lock 内重新核验 baseline revision、canonical tree SHA 和每个 immutable generation 的 stat/size/content SHA，审阅期间任意 Workspace mutation 均以冲突终止且零写入；
  - 先把全部 payload 写入隐藏 transaction staging，再持久化 intent/phase journal；commit 使用 immutable generation、metadata pointer 和删除 tombstone，一批变更只写一次 Workspace revision；失败反向恢复全部 metadata/目录/state，重启时 rollback 未提交事务并清理已提交残留；
  - operation 持久化 `published_workspace_revision`；`sandbox_publish_finished` 同时广播 `workspace_changed`，前端刷新文件树并聚焦最新成果。

### 阶段 5：文档转换框架

- 先实现转换任务、manifest、隔离与幂等框架。
- 依次接入 PDF、DOCX、XLSX；OCR 延后。

实施结果：已完成。上传 API 自动运行固定转换并返回结果，`POST
/api/sessions/{sid}/documents/{source_file_id}/convert` 提供显式幂等重试；前端在响应后刷新完整
Workspace 树，转换成功时优先选中并作为聊天附件使用 `content.md`。manifest 记录原件 SHA、
转换器/版本、配置版本、状态、warning 和每个输出 SHA；缓存复用前同时核验 manifest 声明与当前
Workspace metadata，缺失或不一致即重新转换。PDF/DOCX/XLSX 真实本地 smoke 均通过；OCR 与 PDF
结构化表格识别按上述 warning 明确延期。

### 阶段 6：完整验收

- Backend 单元/集成、Ruff、strict Mypy。
- Frontend lint/typecheck/Vitest。
- Playwright 覆盖上传、Sandbox 修改、验证失败不发布、确认发布、刷新/重启恢复。
- 真实 E2B smoke 验证 Python 写入、执行、固定验证和 Workspace 回写。

实施结果：已完成。全量 Backend `2095 passed, 7 skipped, 9 deselected`，Ruff 与 strict
Mypy（170 source files）通过；Frontend 180/180、lint/typecheck/build 通过；完整 Playwright
19/19、0 retry/flaky。新增的 Workspace 浏览器路径验证 XLSX 上传后同步转换、右栏优先打开
`content.md` 并显示只读提示。真实 E2B smoke 通过 Managed Sandbox 状态机执行全部 9 个代码
工具，证明失败验证不能冻结或写回，修复后重新验证、冻结签名、进入审批状态并把制品以单次
revision 发布回一次性 `WorkspaceStore`。验收同时发现 Windows 深层 Publisher state 会超过传统
MAX_PATH；adapter 现使用短 run key，并把事务 state 放到 staging 根下的短兄弟目录，真实 E2B
回归已通过。

### 阶段 7：自动 Coding 请求编排

- 普通对话保持原行为；用户在 Chat 输入区显式启用 `Code` 后，请求携带
  `coding_mode=true`，不依赖关键词或模型猜测意图。
- Prompt runner 在模型执行前创建或复用当前 Session 唯一可变 Sandbox operation，并等待
  `ready`；若已有冻结制品等待审批，则拒绝启动新 Coding 请求，防止把两次需求混入同一制品。
- Coding turn 临时把 Agent ToolRegistry 收窄为 9 个 `coding_*` 工具，并使用仅作用于该白名单
  的 allow-all permission policy。请求结束后原工具和权限策略必定恢复；模型不能绕过 Sandbox
  调用 Session `write_file` 或其它外部工具。
- 固定系统后缀要求 Agent 检查、实现、运行并调用 `coding_validate` 自验。无论 Agent 是否自验，
  Backend 都在模型完成后重新执行服务器选定的完整 validation plan，不能接受模型提供命令。
- 最终验证通过后自动调用 `prepare_publish`，复用阶段 4A 的 Validation→Freeze TOCTOU 屏障，
  生成签名不可变 Artifact 并停在 `awaiting_approval`。自动编排没有 publish 权限；用户仍须在
  右栏审阅完整 Diff、勾选确认并显式发布。
- 验证失败停在 `validation_failed`，不冻结、不写回；下一次 Code 请求可以复用该 operation 修复。
  Stop 在 Sandbox 创建、最终验证或冻结期间由 request abort flag 协作取消，并尽力销毁云端实例。
- Coding prompt 后缀纳入 Context Budget 估算。`awaiting_approval` 会触发 Workspace attention，
  右栏自动切到 Changes；发布后继续沿用 `workspace_changed` 自动刷新和成果预览。

## 11. 验收标准

1. 新 Session 初始文件树只有 `AGENT.md`、`Memory.md`，且各自唯一。
2. 旧 Session 重启后自动补齐缺失根文件，不覆盖任何已有正文。
3. 第一份代码或代码上传创建 `scripts/`，所有代码都位于其下。
4. Agent 可以决定 `scripts/**` 子目录，但不能逃逸或改写受保护路径。
5. 本机不执行用户代码；E2B 验证失败时 Workspace 完全不变。
6. 只有签名制品通过冲突检查并获得用户确认后才可发布。
7. 发布成功后右侧文件树无需刷新页面即可显示结果；重启后仍存在。
8. 并发编辑不会静默覆盖，冲突返回稳定错误并保留双方数据。
9. Markdown 全链路可创建、编辑、预览、下载和供 Agent 读取。
10. PDF、Word、Excel 必须先生成可审计转换产物，Agent 才能处理其内容。

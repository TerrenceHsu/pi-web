# Workspace 与 Coding Sandbox 一体化设计

> 状态：阶段 1 已完成，阶段 2 待实施
> 日期：2026-08-22
> 范围：Session Workspace、Agent 文件工具、E2B Coding Sandbox、安全发布与后续文档转换

## 1. 目标

每个 Session 拥有一个持久、隔离、可在 UI 中浏览的 Workspace。新 Workspace 立即包含
`AGENT.md` 与 `Memory.md`；代码统一进入根目录同级的 `scripts/`，但 `scripts/` 只在
Agent 首次写代码或用户首次上传代码时创建。Agent 可以自行决定 `scripts/` 内部目录树，
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
├── scripts/
│   ├── app.py
│   ├── tests/
│   │   └── test_app.py
│   └── ...                    # 由 Agent 决定
├── notes/
│   └── design.md
└── documents/
    └── <document-id>/
        ├── original.<ext>     # 不可变原件
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
| `scripts/**` | Agent / 用户 | 代码写入和代码上传的默认根；允许模型决定子目录 |
| `notes/**` 及其他安全 Markdown 路径 | Agent / 用户 | 支持创建、读取、更新、移动和删除 |
| `documents/*/original.*` | 系统 | 上传后不可变，不能执行 |
| `documents/*/content.md`、`tables/**`、`assets/**` | 固定转换器 | 由转换任务重建，Agent 只读或基于其另建派生文件 |
| `.pi-agent/**` | 系统 | 隐藏且禁止外部直接访问 |

## 5. WorkspaceStore 模型

阶段 1 在既有 metadata-per-file 存储上建立规范 `WorkspaceStore` 名称，并保留
`VirtualFileStore` 兼容别名。已有 `uploads/{session_id}` 数据不复制、不改路径，避免产生
双事实源；启动时幂等补齐缺失的 `Memory.md`，并把大小写等价的旧根文件规范为固定路径与
purpose。后续可在不改变 API 的前提下迁移物理布局。

后续模型字段：

- Workspace：`session_id`、`revision`、`created_at`、`updated_at`。
- Entry：`id`、`logical_path`、`kind`、`origin`、`purpose`、`mime`、`size`、`sha256`、
  `created_at`、`updated_at`。
- 每次提交生成不可变内容 generation；metadata pointer 是单文件 commit point。
- 批量发布以 Workspace revision 和逐文件 before SHA 作为冲突前提。

## 6. 上传与 Agent 文件工具

### 6.1 代码上传

- 识别允许的代码扩展名后，默认逻辑路径为 `scripts/<filename>`。
- 上传 API 可接受安全的 `relative_folder`，但必须位于 `scripts/`。
- 路径统一为相对 POSIX 形式；拒绝绝对路径、`..`、反斜杠、控制字符、保留路径、symlink、
  junction/reparse point 和大小写冲突。
- 同名默认返回冲突或显式生成不重名路径，不静默覆盖。
- 上传仅持久化，不在本机执行。

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
面板包含：

- **Files**：目录树、上传、新建 Markdown、查看/下载、固定根文件编辑。
- **Sandbox**：当前 operation、运行日志和固定验证结果。
- **Changes**：发布前 diff、冲突提示、确认发布和历史结果。

现有 Coding Sandbox Modal 的状态、日志、diff 与审批组件应迁入或复用到右侧面板，避免形成
第二套状态管理。

## 9. PDF、Word、Excel 固定转换

Agent 不能把二进制正文直接提交给 LLM。上传后由服务端排队执行
`convert_workspace_document(entry_id)`：

- PDF：提取分页文本、标题/段落、页码映射和表格；扫描件标记 `needs_ocr`，OCR 后续单列。
- DOCX：转换标题层级、段落、列表、表格、链接和图片引用到 Markdown。
- XLSX：生成 workbook 摘要，每个 sheet 输出 CSV 与 schema/范围信息；不执行宏和公式。

`manifest.json` 固定记录原件 SHA、转换器名称/版本、状态、warning、生成文件及各自 SHA。
原件永不就地改写，重复转换可按 source SHA + converter version 幂等复用。

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

### 阶段 3：右侧 Workspace 面板

- 三栏桌面布局与窄屏 drawer。
- 文件树、根文件编辑、上传、新建 Markdown、预览与实时刷新。
- 复用 Sandbox/Changes 视图和统一 store。

### 阶段 4：统一 Sandbox 快照与发布目标

- Sandbox 从 WorkspaceStore 物化快照，不再使用独立项目目录作为产品事实源。
- 仅发布允许的 `scripts/**`/Markdown 变更，保护固定根与转换产物。
- 发布完成原子提升 revision，并广播 Workspace event。

### 阶段 5：文档转换框架

- 先实现转换任务、manifest、隔离与幂等框架。
- 依次接入 PDF、DOCX、XLSX；OCR 延后。

### 阶段 6：完整验收

- Backend 单元/集成、Ruff、strict Mypy。
- Frontend lint/typecheck/Vitest。
- Playwright 覆盖上传、Sandbox 修改、验证失败不发布、确认发布、刷新/重启恢复。
- 真实 E2B smoke 验证 Python 写入、执行、固定验证和 Workspace 回写。

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

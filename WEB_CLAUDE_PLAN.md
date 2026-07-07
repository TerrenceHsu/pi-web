# Web Claude 改造计划

> **目标**：在 Step 1–21 baseline 与 v0.0.22 Web backend stable baseline 之上，把项目从「agent runtime 调试器」改造为「claude.ai 风格的 Web 端对话助手」。
>
> **本版核心调整**：
> 1. **不再使用右栏调试面板**。页面主结构改为「左侧会话栏 + 中间对话窗口」。
> 2. **当前 turn 的运行信息直接展示在中间对话流中**，像 assistant 回答一样出现，但使用低对比度、可折叠、淡化样式。
> 3. **支持用户自己添加 Skills 和 MCP server**，并在中间窗口内以可视化卡片展示启用状态、工具列表、连接结果和调用过程。
>
> **范围**：仅 Web 端；不做 CLI；不操作本地文件；单 provider（GLM）；支持 Skills + MCP；不做跨 session 记忆；不做多用户系统。
>
> **当前基线**（Web Claude P0 MVP 完成）：
> - Core runtime：Step 1–21 已完成
> - Web backend：v0.0.22 stable baseline
> - **Web Claude P0 MVP**：Step 1–8 全部完成（2026-07-07）
> - Offline tests：**678 passed**（`pytest -m "not slow and not docker"`）
> - Web Skills/MCP API tests：**59 passed**（slow mark）
> - Web Files / Prompt-File-Injection tests：**37 passed**（slow mark）
> - Web integration tests：**25 passed**（slow mark；v0.0.22 baseline 沿用）
> - Frontend build：**npm run build 通过**（123 modules / 127.85 KB JS gzip 45.08 KB）
> - Ruff：**All checks passed**
> - 已有 Web API：`/api/sessions` CRUD、`/api/sessions/{sid}/files` 6 个、`/api/skills/upload + enable + disable`、`/api/mcp/servers` CRUD + test + enable + disable、`/api/mcp/tools/{name}/enable + disable`、`/api/messages?session_id=`、`/api/stream?limit=N`、`/ws/events`
>
> **定位**：Web Claude P0 MVP is complete for local development.
> Localhost-first, no authentication, not suitable for public exposure.

---

## 一、总体设计决策

| 维度 | 选择 | 说明 |
|---|---|---|
| 页面结构 | 左侧会话栏 + 中间对话窗口 | 不做右栏调试面板，避免产品形态偏调试器 |
| Turn 信息展示 | 中间窗口 inline cards | 像 assistant 消息一样展示，但淡化、折叠、低干扰 |
| Skills | 用户可添加 / 启用 / 禁用 / 查看 | 支持上传 `SKILL.md`，也支持 MCP prompts 转 Skill |
| MCP | 用户可添加 server / 测试连接 / 启用工具 | 支持 stdio MCP；HTTP MCP 可保留占位 |
| 调试信息 | 默认隐藏为淡化块 | 事件、工具调用、snapshot、policy decision 都进入当前 turn 的淡化信息块 |
| 文件 | 仅会话内上传与查看 | 不操作本地文件系统；不做 bash/read/write/edit |
| Provider | 单 GLM | 不做多 provider 路由 |
| Session | 线性 session | 不做 fork / branch / branch summary |
| Memory | 当前 session 内上下文 | 不做跨 session 长期记忆 / 向量记忆 |
| 权限 | 工具 allowlist + MCP server/tool allowlist | 不做多用户 RBAC，不做路径沙箱 |
| 部署 | localhost first | 无鉴权，不建议公网暴露 |

---

## P0 MVP 完成状态（2026-07-07）

### Step 进度表

| Step | 内容 | 状态 |
|---|---|---|
| Step 1 | Skills API（`POST /api/skills/upload`、enable/disable、`GET /api/skills/{name}`、`POST /api/prompt` 顶层 `skill_names`；unknown skill web 层预校验 400） | ✅ Done |
| Step 2 | MCP API（servers CRUD、test connection 不污染 harness、enable/disable、tools enable/disable 真实生效；env values 严格不回显） | ✅ Done |
| Step 3 | 前端骨架（Pinia + `types/` + `api/` + `stores/` 五个 store；barrel `index.ts` 保证旧 .vue import 不破坏） | ✅ Done |
| Step 4 | Claude-like 两栏 UI（`AppShell` + `SessionSidebar` + `ChatPanel`；`DeveloperDrawer` 主路径下线） | ✅ Done |
| Step 5 | Inline Turn Cards + WS mapper（7 个 card 组件 + streaming assistant draft） | ✅ Done |
| Step 6 | 文件上传 UI（`FileChip` + `AttachmentBar` + `ChatInput` drag-drop + `sendPrompt` 带 `file_ids`) | ✅ Done |
| Step 7 | Skills/MCP Manager Modal（`Modal.vue` + skills 三件套 + mcp 四件套；env value 防回显四道防线） | ✅ Done |
| Step 8 | Docs 收尾 + 最终验证（本轮） | ✅ Done |

### 已完成能力列表（M1+M2+M3+M4+M5 全部就绪）

**后端 API（P0-1 ~ P0-5 + Step 1/2）**：
- Sessions：sqlite 多会话 CRUD（POST / PATCH / DELETE `/api/sessions`，GET `/api/messages?session_id=`）
- Files：会话级 VirtualFileStore（6 个 endpoints；单文件 25MB / session 总量 100MB）
- Prompt：`POST /api/prompt` 支持 `session_id` / `file_ids` / `skill_names` / `skill_selection`；unknown skill web 层 400；附件统一注入 `FileBlock`
- Skills：upload / enable / disable / GET 详情；`include_prompt=true` 默认 403
- MCP：servers CRUD + test connection（不污染 harness）+ enable/disable + delete；tools enable/disable 真实 unregister/register；env values response 只有 `env_keys`
- 文件工具：`list_files` / `view_file`（md / html / csv / parquet / 文本；图片明确 unsupported；PDF 不解析正文）

**前端 UI（P0-4 Step 3–7）**：
- 左侧 `SessionSidebar`（New chat / 会话列表 / 重命名 / 删除；底部 Skills / MCP 按钮挂载 modal）
- 中间 `ChatPanel`（header 状态 + 消息流 + ChatInput）
- Inline Turn Cards：`UserMessage` / `AssistantMessage`（streaming draft）/ `TurnInfo` / `ToolCall` / `ToolResult` / `FileRead` / `MCPToolCall` / `SkillUsed` / `Error`
- WS event mapper：`chatStore.handleEvent` 把 7+ 类 AgentEvent 实时映射成 ChatStreamItem
- 文件上传 UI：`utils/files.ts` + `FileChip` + `AttachmentBar`；ChatInput 拖放 + 文件选择；失败保留 pending
- Skills Modal：上传 SKILL.md + enable toggle + Use-this-turn checkbox（selected 必须是 enabled 子集）
- MCP Modal：server form（args JSON + env key/value + 默认 enabled=false）+ server list（Test / Enable / Disable / Delete）+ tool list（Enable / Disable）
- 通用 `Modal.vue`：居中弹窗、ESC/遮罩关闭、Teleport 到 body、80vh 内滚动——**不是右栏 Drawer**

**关键安全约束**：
- **MCP env values 严格不回显**：response 类型只有 `env_keys`；前端表单用 `type=password + autocomplete=new-password`；提交后 `resetForm()` 清空所有字段；列表只渲染 key 名 + `(values hidden)`
- **Prompt preview 默认禁用**：`?include_prompt=true` 默认 403；需 `create_app(allow_prompt_preview=True)` + localhost
- **`POST /api/prompt` 同步阻塞**：LLM 调用结束才返回；当前没有 `/api/prompt/async`；前端通过 WS `/ws/events` 展示实时事件，但 prompt 请求本身仍等待后端完成
- **Localhost only / no auth**：无鉴权 / 无多用户隔离 / 无 rate limit；不建议公网暴露

### 显式不做（P0 范围）

- ❌ 右栏调试面板 / `DeveloperDrawer` 主入口（组件文件保留但不在主路径渲染）
- ❌ CLI
- ❌ RAG / Vector Memory / Long-term Memory / 跨 session 用户记忆
- ❌ 多用户账号 / OAuth / RBAC / 企业 secret vault
- ❌ 公网部署 / 横向扩展 / rate limit
- ❌ MCP marketplace / Skill marketplace
- ❌ Skill 在线编辑 / prompt preview 默认开启
- ❌ MCP 配置持久化 / Skill 上传持久化（重启即丢）
- ❌ **图片理解**（不做 ImageBlock / 不做 GLM-4V 直读 / 不做 OCR / 不做视觉理解）
- ❌ **PDF 正文解析**（PDF 仅作为 FileChip 占位，不做文本提取）
- ❌ 本地文件系统操作工具（bash / read / write / edit / grep / find / ls）
- ❌ `/api/prompt/async` 异步任务模式 / job-id 轮询
- ❌ Regenerate / Export markdown
- ❌ WebSocket event_id 去重 / 重连补播（v0.0.22 已知限制沿用）
- ❌ 浏览器端 e2e 自动化测试（P0 仅手动 smoke）

### 后续 P1 建议（不在 P0 范围，按推荐优先级）

1. **真实浏览器 smoke test**——按 [`docs/WEB_TESTING.md`](docs/WEB_TESTING.md) 25 项 checklist 手动验证（Step 8 未执行）
2. **Playwright e2e 最小用例**——前端功能已多（session / chat / file / skills / MCP / modal / inline cards），后续每次回归靠手动点会累；优先投资 e2e 框架
3. **真实 GLM 端到端冒烟**——含多轮 tool_use 的真实链路
4. **MCP / Skill 配置持久化**——重启后恢复（目前重启即丢）
5. **`/api/prompt/async` 异步任务模式**——解除 POST 同步阻塞限制
6. **WebSocket event_id 去重 + reconnect 补播**——v0.0.22 已知限制沿用
7. **Regenerate / Export markdown**
8. **PDF 文本提取**（pdf.js 或服务端 pdftotext）

移动端隐藏 sidebar 为抽屉、Skill 在线编辑、Skill / MCP marketplace、更好的错误提示与 loading 状态等再后续考虑。

---

## 二、信息架构调整

### 2.1 旧方案问题

旧方案中前端是三栏：

```text
Sidebar | ChatPanel | Drawer / Inspector
```

问题：

1. 右栏 Drawer 会让产品看起来像「runtime debugger」，不是普通用户使用的对话助手。
2. 当前 turn 的关键信息被放到侧边栏，用户不容易把「这次回答」和「这次工具调用 / skill / MCP」对应起来。
3. Skills / MCP 如果放在右栏，容易被误解成调试面板，而不是用户可配置的能力入口。
4. 移动端 / 小屏时右栏会明显占空间。

### 2.2 新方案

改成两区主布局：

```text
┌───────────────┬───────────────────────────────────────────────┐
│ SessionSidebar │ ChatMain                                      │
│               │                                               │
│ + New Chat    │ [user message]                                │
│ Sessions      │ [assistant message]                           │
│ Extensions    │ [turn info card: 正在调用 MCP 工具...]         │
│ Settings      │ [assistant final answer]                      │
│               │                                               │
│               │ [composer: attach / skills / MCP / send]       │
└───────────────┴───────────────────────────────────────────────┘
```

核心变化：

- **中间窗口是唯一主舞台**。
- 所有和当前 turn 相关的信息都进入 message stream。
- Skills / MCP 的管理入口可以在左栏或 composer 附近打开 modal / overlay，但结果与状态展示回到中间窗口。
- 不再常驻右侧 Inspector。

---

## 三、中间窗口的消息类型设计

### 3.1 消息流统一模型

前端中间窗口展示的 item 不再只有 `user` / `assistant`，而是统一为 `ChatStreamItem`：

```ts
type ChatStreamItem =
  | UserMessageItem
  | AssistantMessageItem
  | TurnInfoItem
  | ToolCallItem
  | SkillInfoItem
  | MCPInfoItem
  | FileInfoItem
  | ErrorItem;
```

### 3.2 普通消息

| 类型 | 视觉 |
|---|---|
| UserMessageItem | 正常用户消息气泡 |
| AssistantMessageItem | 正常 assistant 回答 |
| ErrorItem | 红色轻提示，不白屏 |

### 3.3 当前 turn 淡化信息块

当前 turn 的 runtime 信息以 `TurnInfoItem` 展示：

```text
┌─────────────────────────────────────────────┐
│ 低对比度 / 小字号 / 可折叠                  │
│ 当前轮次                                    │
│ - 已选择 Skills：coding_review, finance     │
│ - MCP server：filesystem connected          │
│ - 工具调用：mcp__fs__list_files             │
│ - Snapshot：finished in 2.3s                │
└─────────────────────────────────────────────┘
```

视觉要求：

| 属性 | 要求 |
|---|---|
| 背景 | 浅灰 / 低饱和 |
| 字体 | 比正文小一号 |
| 透明度 | 比 assistant 消息弱 |
| 默认状态 | 简要显示 |
| 展开后 | 展示事件 timeline、tool args/result、snapshot metadata |
| 位置 | 插入本轮 user 消息之后、assistant 最终回答之前或回答内部合适位置 |
| 目的 | 让用户知道 agent 做了什么，但不打断阅读 |

### 3.4 ToolCall 淡化块

工具调用展示为折叠块：

```text
▸ 调用了工具 view_file · 0.8s
```

展开后：

```text
工具：view_file
参数：{"file_id": "..."}
结果摘要：读取 12KB 文本，已截断至前 8KB
状态：success
```

### 3.5 Skill 可视化块

启用或新增 Skill 后，在中间窗口给出轻提示：

```text
已启用 Skill：coding_review
用于：Review Python agent runtime code
可用工具：read_file, run_tests
```

Skill 不应该像调试 JSON，而应该像“能力卡片”。

### 3.6 MCP 可视化块

添加 MCP server 后，显示连接卡片：

```text
MCP server connected：filesystem
Tools:
- mcp__filesystem__list_files
- mcp__filesystem__read_file
```

MCP tool 调用时使用 ToolCall 淡化块展示，不进入右栏。

---

## 四、Skills 设计

### 4.1 用户目标

用户应该能完成：

1. 上传一个 `SKILL.md`
2. 查看解析后的 name / description / tags / tool_names / priority
3. 启用 / 禁用 Skill
4. 在发送消息前选择本轮使用哪些 Skills
5. 在消息流中看到本轮实际注入了哪些 Skills

### 4.2 前端入口

不做右栏。Skills 入口有两个：

| 入口 | 位置 | 用途 |
|---|---|---|
| Sidebar: Extensions | 左侧栏 | 管理所有 Skills / MCP |
| Composer: Skills chip | 输入框上方或左侧 | 选择本轮启用的 Skills |

点击后打开 modal：

```text
┌──────────────────────────────────────┐
│ Skills                               │
│ [Upload SKILL.md]                    │
│                                      │
│ ☑ coding_review                      │
│   Review Python agent runtime code   │
│   tags: coding, review               │
│                                      │
│ ☐ finance_news                       │
│   Analyze finance news               │
└──────────────────────────────────────┘
```

### 4.3 后端 API 计划

当前已有 `GET /api/skills`。需要补充：

| API | 作用 |
|---|---|
| `GET /api/skills` | 列出 Skills，默认不返回 prompt body |
| `POST /api/skills/upload` | 上传 `SKILL.md` 并注册 |
| `PATCH /api/skills/{name}` | 启用 / 禁用 / 调整 priority |
| `DELETE /api/skills/{name}` | 删除用户上传的 Skill |
| `POST /api/skills/reload` | 手动重新加载 skills 目录 |

请求示例：

```json
{
  "enabled": true,
  "priority": 20
}
```

### 4.4 Prompt 注入规则

本轮 prompt 中只注入：

1. 默认系统 prompt
2. 用户显式启用的 Skills
3. 当前 MCP 已启用工具摘要
4. 文件列表摘要

不要把所有已安装 Skills 都塞进 system prompt，避免 prompt 过长。

### 4.5 验收标准

- 上传合法 `SKILL.md` 后出现在 Skills modal。
- 缺少 `name` / `description` / body 时返回清晰错误。
- 启用 Skill 后，composer 出现 Skill chip。
- 本轮发送后，中间窗口出现淡化 SkillInfoItem。
- Snapshot metadata 记录本轮启用的 Skills。
- `GET /api/skills?include_prompt=true` 仍默认 403，只有本地调试显式开启才允许。

---

## 五、MCP 设计

### 5.1 用户目标

用户应该能完成：

1. 添加 MCP server
2. 配置 command / args / env
3. 测试连接
4. 查看 tools 列表
5. 启用 / 禁用 server 或单个 tool
6. 在中间窗口看到 MCP 连接状态与工具调用结果

### 5.2 前端入口

同样不做右栏。MCP 入口：

| 入口 | 位置 | 用途 |
|---|---|---|
| Sidebar: Extensions | 管理 MCP server |
| Composer: Tools chip | 查看当前会话可用工具 |
| 中间消息流 | 展示连接、调用、失败、权限拒绝 |

MCP modal：

```text
┌────────────────────────────────────────────┐
│ MCP Servers                                │
│ [Add server]                               │
│                                            │
│ ● filesystem      connected    [tools]     │
│   command: npx @modelcontextprotocol/...   │
│   tools: 5 enabled / 7 total               │
│                                            │
│ ○ github          disconnected [connect]   │
└────────────────────────────────────────────┘
```

添加表单：

```text
Name: filesystem
Command: npx
Args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
Env:
  API_KEY = ********
[Test connection] [Save]
```

### 5.3 后端 API 计划

当前已有：

| API | 状态 |
|---|---|
| `GET /api/mcp` | 已有 |
| `GET /api/mcp/tools` | 已有 |

需要新增：

| API | 作用 |
|---|---|
| `GET /api/mcp/servers` | 列出 server 配置与状态 |
| `POST /api/mcp/servers` | 新增 server 配置 |
| `POST /api/mcp/servers/{name}/test` | 测试连接，不持久启用 |
| `POST /api/mcp/servers/{name}/connect` | 连接并注册 tools |
| `POST /api/mcp/servers/{name}/disconnect` | 断开并移除 tools |
| `PATCH /api/mcp/servers/{name}` | 修改 enabled / args / env |
| `DELETE /api/mcp/servers/{name}` | 删除配置 |
| `PATCH /api/mcp/tools/{tool_name}` | 启用 / 禁用单个 tool |

### 5.4 安全规则

- 默认不允许 unknown MCP server。
- 新增 MCP server 后默认 `enabled=false`。
- 用户点击 connect 后才注册工具。
- Env 中敏感字段只显示 mask。
- MCP tool 默认 read-only 优先允许；write/delete/shell/network 类工具默认禁用。
- Deny 结果显示为淡化 PolicyInfoItem。

### 5.5 中间窗口可视化

连接成功：

```text
MCP server connected：filesystem
已启用工具：5 / 7
```

连接失败：

```text
MCP server failed：github
原因：initialize timeout after 10s
```

工具调用：

```text
▸ 调用 MCP 工具 mcp__filesystem__list_files · success · 0.4s
```

权限拒绝：

```text
工具已拦截：mcp__filesystem__delete_file
原因：delete 类工具默认禁止
```

### 5.6 验收标准

- 用户能添加 MCP server。
- test connection 成功时展示 tools。
- connect 后 tools 进入 `GET /api/mcp/tools`。
- 禁用 tool 后不会出现在 provider tool schema 中。
- 调用 MCP tool 时，中间窗口出现 ToolCallItem。
- MCP 错误不会导致页面白屏。
- 断开 server 后 tools 被移除。

---

## 六、当前 turn 信息展示设计

### 6.1 当前 turn 的生命周期

一次用户发送消息后，中间窗口按顺序展示：

```text
[user message]
[TurnInfoItem: request queued / selected skills / active tools]
[ToolCallItem: view_file running]
[ToolCallItem: view_file success]
[AssistantMessageItem: streaming answer]
[TurnInfoItem: snapshot saved / duration / token usage]  可折叠
```

### 6.2 AgentEvent → ChatStreamItem 映射

| AgentEvent / StreamEvent | UI item | 默认展示 |
|---|---|---|
| RequestQueued | TurnInfoItem | 淡化，显示“请求已排队” |
| RequestStart | TurnInfoItem | 淡化，显示“开始处理” |
| TurnStart | TurnInfoItem | 淡化，创建当前 turn 容器 |
| MessageUpdate / TextDelta | AssistantMessageItem | 正常流式正文 |
| ToolExecutionStart | ToolCallItem | 淡化，显示工具名和 running |
| ToolExecutionEnd | ToolCallItem | 淡化，显示 success/error/duration |
| PolicyDecision | TurnInfoItem / ToolCallItem | 拒绝时显示警告样式 |
| SnapshotFinished | TurnInfoItem | 淡化，显示 snapshot id / duration |
| Error | ErrorItem | 明显但不白屏 |

### 6.3 展开后的内容

TurnInfoItem 展开后显示：

```text
Request ID
Turn ID
Selected skills
Active MCP servers
Active tools
Tool calls
Tool results
Policy decisions
Snapshot ID
Duration
Raw event count
```

不默认展示原始 JSON。原始 JSON 只放在“展开更多 / Raw”里。

### 6.4 去重规则

由于 backend 有 SSE 和 WS，前端必须基于 event id 去重：

```ts
seenEventIds: Set<string>
```

如果后端事件没有稳定 id，前端生成：

```ts
`${type}:${request_id}:${turn_id}:${sequence || timestamp}`
```

---

## 七、前端组件改造计划

### 7.1 新目录结构

```text
web/src/
├── App.vue
├── api/
│   ├── client.ts
│   ├── websocket.ts
│   ├── skills.ts
│   └── mcp.ts
├── pages/
│   └── ChatPage.vue
├── components/
│   ├── layout/
│   │   ├── AppShell.vue
│   │   └── SessionSidebar.vue
│   ├── chat/
│   │   ├── ChatMain.vue
│   │   ├── MessageList.vue
│   │   ├── MessageBubble.vue
│   │   ├── TurnInfoCard.vue
│   │   ├── ToolCallCard.vue
│   │   ├── SkillInfoCard.vue
│   │   ├── MCPInfoCard.vue
│   │   ├── FileChip.vue
│   │   └── ChatComposer.vue
│   ├── extensions/
│   │   ├── ExtensionsModal.vue
│   │   ├── SkillsManager.vue
│   │   ├── SkillUploadForm.vue
│   │   ├── MCPServerManager.vue
│   │   ├── MCPServerForm.vue
│   │   └── MCPToolList.vue
│   └── common/
│       ├── ErrorBanner.vue
│       ├── LoadingSpinner.vue
│       ├── EmptyState.vue
│       └── Modal.vue
└── types/
    ├── messages.ts
    ├── events.ts
    ├── sessions.ts
    ├── skills.ts
    └── mcp.ts
```

### 7.2 删除 / 降级组件

| 旧组件 | 处理 |
|---|---|
| `InspectorPanel.vue` | 删除或改为 `TurnInfoCard.vue` 内部展开内容 |
| `SnapshotPage.vue` | 不作为主入口，可保留隐藏 dev route |
| `TracePage.vue` | 不作为主入口，信息进入 TurnInfoCard |
| `MCPPage.vue` | 改为 `MCPServerManager.vue` modal |
| `SkillPage.vue` | 改为 `SkillsManager.vue` modal |
| `PolicyPage.vue` | 不作为页面；policy 决策进入 ToolCallCard / TurnInfoCard |

### 7.3 App.vue

App.vue 只负责 shell：

```text
<AppShell>
  <SessionSidebar />
  <ChatMain />
  <ExtensionsModal />
</AppShell>
```

### 7.4 ChatMain

职责：

- 加载当前 session messages
- 建立 WS 连接
- 接收 event 并映射为 ChatStreamItem
- 管理当前 streaming assistant draft
- 管理当前 turn 的淡化信息块
- 发送 prompt
- 处理 409 / 500 错误
- 触发文件上传
- 打开 Skills / MCP modal

---

## 八、后端改造计划

### 8.1 已有 backend 能力

v0.0.22 已有：

| 能力 | API |
|---|---|
| 会话列表 | `GET /api/sessions` |
| 消息列表 | `GET /api/messages` |
| 事件列表 | `GET /api/events` |
| prompt | `POST /api/prompt` |
| SSE | `GET /api/stream?limit=N` |
| WebSocket | `WS /ws/events` |
| MCP tools | `GET /api/mcp/tools` |
| Skills list | `GET /api/skills` |

### 8.2 需要新增的 backend 能力

| 优先级 | 能力 | API |
|---|---|---|
| P0 | 线性 session CRUD | `POST /api/sessions`、`PATCH /api/sessions/{sid}`、`DELETE /api/sessions/{sid}` |
| P0 | 文件上传 | `POST /api/sessions/{sid}/files`、`GET /api/sessions/{sid}/files`、`GET /api/files/{fid}` |
| P0 | 默认 system prompt | `system_prompt.py` + harness 默认注入 |
| P1 | Skill 上传 / 启用 | `POST /api/skills/upload`、`PATCH /api/skills/{name}`、`DELETE /api/skills/{name}` |
| P1 | MCP server 管理 | `POST /api/mcp/servers`、`POST /api/mcp/servers/{name}/test`、`POST /api/mcp/servers/{name}/connect` |
| P1 | MCP tool 启用 | `PATCH /api/mcp/tools/{tool_name}` |
| P1 | prompt async | `POST /api/prompt/async` |
| P2 | regenerate | `POST /api/messages/{id}/regenerate` |
| P2 | export markdown | `GET /api/sessions/{sid}/export.md` |

---

## 九、分阶段实施计划

### Phase 0：冻结当前 baseline

**目标**：确认 v0.0.22 是可回退点。

任务：

1. 确认 `pytest tests/ -v -m "not slow and not docker"` 通过。
2. 确认 `pytest tests/test_integration_web_server.py -v` 通过。
3. 确认 `ruff check src tests` 通过。
4. 打 tag：`v0.0.22-web-backend`。

验收：

- 有 git tag 或至少有 commit。
- `WEB_CLAUDE_PLAN.md` 已更新为本版方案。

---

### Phase 1：中间窗口 UI 骨架

**目标**：先把页面形态改对，不做 Skills/MCP 管理复杂逻辑。

任务：

1. App.vue 改成两区布局：Sidebar + ChatMain。
2. 删除常驻右栏。
3. ChatMain 支持普通 user / assistant 消息展示。
4. ChatComposer 支持输入、Enter 发送、Shift+Enter 换行。
5. 调 `POST /api/prompt` 发送消息。
6. 通过 `WS /ws/events` 接收事件。
7. 如果 WS 失败，fallback 到发送完成后 `GET /api/messages`。
8. 基础 loading / error 状态。

验收：

- 打开页面就是聊天界面。
- 没有右栏调试面板。
- 能发送消息并看到 assistant 回复。
- 后端 409 时前端显示“Agent is already running”。
- `npm run build` 通过。

---

### Phase 2：Inline Turn 信息卡

**目标**：把当前 turn 的事件、工具调用、snapshot 信息展示在中间窗口。

任务：

1. 新增 `TurnInfoCard.vue`。
2. 新增 `ToolCallCard.vue`。
3. 新增 event → ChatStreamItem mapper。
4. 当前 turn 开始时创建淡化 TurnInfoCard。
5. ToolExecutionStart/End 更新 ToolCallCard。
6. SnapshotFinished 更新 TurnInfoCard。
7. Error event 显示 ErrorItem。
8. TurnInfoCard 支持折叠 / 展开。
9. 展开后显示 tool args/result、duration、snapshot id。
10. 不显示右栏，不跳转 TracePage。

验收：

- 当前 turn 信息出现在中间窗口。
- 视觉上比 assistant 消息淡。
- 工具调用默认折叠。
- 展开后能看到必要调试信息。
- 流式回答仍然是主内容，不被 turn 信息抢占。

---

### Phase 3：Skills 管理与可视化

**目标**：用户能添加、启用、查看 Skills；本轮启用信息可视化。

任务：

1. 新增 `ExtensionsModal.vue`。
2. 新增 `SkillsManager.vue`。
3. 新增 `SkillUploadForm.vue`。
4. Sidebar 加 `Extensions` 入口。
5. Composer 加 Skills chip。
6. 对接 `GET /api/skills`。
7. 后端新增 `POST /api/skills/upload`。
8. 后端新增 `PATCH /api/skills/{name}`。
9. 上传失败显示错误。
10. 发送 prompt 时带 selected skill names。
11. 本轮中间窗口显示 SkillInfoCard。
12. Snapshot metadata 写入 selected_skills。

验收：

- 能上传 `SKILL.md`。
- 能启用 / 禁用 Skill。
- Composer 能看到当前启用 Skills。
- 发送后中间窗口出现淡化 SkillInfoCard。
- 默认不请求 `include_prompt=true`。

---

### Phase 4：MCP 管理与可视化

**目标**：用户能添加 MCP server，测试连接，启用工具，并看到工具可视化。

任务：

1. 新增 `MCPServerManager.vue`。
2. 新增 `MCPServerForm.vue`。
3. 新增 `MCPToolList.vue`。
4. 对接 `GET /api/mcp` 与 `GET /api/mcp/tools`。
5. 后端新增 `GET /api/mcp/servers`。
6. 后端新增 `POST /api/mcp/servers`。
7. 后端新增 `POST /api/mcp/servers/{name}/test`。
8. 后端新增 `POST /api/mcp/servers/{name}/connect`。
9. 后端新增 `POST /api/mcp/servers/{name}/disconnect`。
10. 后端新增 `PATCH /api/mcp/tools/{tool_name}`。
11. 连接成功后中间窗口显示 MCPInfoCard。
12. 工具调用时显示 ToolCallCard。
13. Policy deny 时显示淡化警告卡。

验收：

- 能添加 MCP server。
- 能测试连接。
- 能看到 tools schema 摘要。
- 能启用 / 禁用单个 MCP tool。
- 工具调用可视化在中间窗口。
- MCP 失败不导致页面白屏。

---

### Phase 5：文件上传闭环

**目标**：支持 Claude-like 文件交互。

任务：

1. 新增 `VirtualFileStore`。
2. 新增文件上传 API。
3. ChatComposer 支持选择文件。
4. 上传后显示 FileChip。
5. 中间窗口显示 FileInfoItem。
6. 新增 `list_files` 工具。
7. 新增 `view_file` 工具。
8. 默认 system prompt 告诉模型涉及文件时先用文件工具。
9. 图片文件显示缩略图。
10. 文本文件可预览摘要。

验收：

- 上传文件后能在当前会话看到。
- 发送消息时能附带文件。
- LLM 能通过 `list_files` / `view_file` 获取文件内容。
- 文件工具调用可视化为淡化 ToolCallCard。

---

### Phase 6：默认对话 system prompt

**目标**：模型默认表现为对话助手，而不是 coding agent。

任务：

1. 新增 `system_prompt.py`。
2. `build_default_system_prompt(skills, mcp_tools, files)`。
3. harness.run_prompt 未传 system_prompt 时使用默认。
4. 只注入本轮启用 Skills。
5. 注入当前启用 MCP tools 摘要。
6. 注入当前会话文件列表摘要。
7. Snapshot metadata 记录实际 system prompt hash，不默认暴露全文。

验收：

- 不传 system_prompt 也能正常对话。
- 中文输入中文回答。
- 文件问题会优先调用文件工具。
- Skills / MCP 摘要进入 prompt。
- prompt body 不通过普通 API 暴露。

---

### Phase 7：体验打磨

任务：

1. Stop 按钮接 `POST /api/chat/abort` 或现有 abort API。
2. Regenerate 最后一轮。
3. 会话重命名 / 删除。
4. 导出 markdown。
5. WebSocket reconnect 后补拉 `/api/events`。
6. TurnInfoCard 支持 event_id 去重。
7. 慢客户端 dropped events 在状态区提示。
8. 移动端隐藏 Sidebar 为抽屉。

---

## 十、测试计划

### 10.1 后端测试

每个阶段必须跑：

```bash
pytest tests/ -v -m "not slow and not docker"
pytest tests/test_integration_web_server.py -v
ruff check src tests
```

新增测试：

| 阶段 | 测试 |
|---|---|
| Phase 3 | `tests/test_web_skills_api.py` |
| Phase 4 | `tests/test_web_mcp_api.py` |
| Phase 5 | `tests/test_web_files_api.py` |
| Phase 6 | `tests/test_system_prompt.py` |

### 10.2 前端测试

每个阶段必须跑：

```bash
cd web
npm install
npm run build
```

如已有 typecheck：

```bash
npm run typecheck
```

建议补充：

| 阶段 | 验证 |
|---|---|
| Phase 1 | ChatPage 可加载，发送 prompt API 正确 |
| Phase 2 | event mapper 单测 |
| Phase 3 | Skills modal 可打开，上传表单校验 |
| Phase 4 | MCP form JSON args/env 校验 |
| Phase 5 | FileChip / 上传进度 / 错误展示 |
| Phase 6 | system prompt hash 出现在 snapshot metadata |

---

## 十一、验收里程碑

| 里程碑 | 含义 | 对应阶段 |
|---|---|---|
| M1 | 无右栏的 Claude-like 聊天页面 | Phase 1 |
| M2 | 当前 turn 信息 inline 淡化展示 | Phase 2 |
| M3 | Skills 可添加 / 启用 / 可视化 | Phase 3 |
| M4 | MCP 可添加 / 连接 / 工具可视化 | Phase 4 |
| M5 | 文件上传 + view_file/list_files 闭环 | Phase 5 |
| M6 | 默认对话 system prompt 生效 | Phase 6 |

**最小可用 Web Claude** = M1 + M2 + M3 + M4。  
**完整 Web Claude MVP** = M1 + M2 + M3 + M4 + M5 + M6。

---

## 十二、显式不做

- 不做右栏调试面板。
- 不做常驻 Inspector / Drawer。
- 不做 CLI。
- 不做 RAG / Vector Memory / Long-term Memory。
- 不做跨 session 用户记忆。
- 不做本地文件系统操作工具。
- 不做 bash / edit / write_file / delete_file。
- 不做多用户账号 / OAuth / RBAC。
- 不做公网部署方案。
- 不做多 provider 路由。
- 不做 MCP resources/read。
- 不做远程 skill marketplace。

---

## 十三、风险与控制

| 风险 | 控制 |
|---|---|
| Turn 信息打断阅读 | 默认淡化、折叠、低对比度；只显示摘要 |
| Skills 太多导致 prompt 过长 | 只注入本轮启用 Skills |
| MCP 工具有危险能力 | 默认 disabled；connect 后仍按 policy allowlist |
| WebSocket 事件重复 | 前端按 event_id 去重 |
| 后端事件 schema 不稳定 | 前端 mapper 做兼容层 |
| 文件太大撑爆上下文 | view_file 截断；大文件只显示摘要 |
| prompt preview 泄露 | 默认禁用；只本地调试显式开启 |
| SQLite 并发写 | WAL + 单写锁 |
| 前端一次改动过大 | 按 Phase 1–7 小步推进 |

---

## 十四、实施顺序总览

```text
Phase 0  冻结 v0.0.22 baseline
Phase 1  中间窗口 UI 骨架
Phase 2  Inline Turn 信息卡
Phase 3  Skills 管理与可视化
Phase 4  MCP 管理与可视化
Phase 5  文件上传闭环
Phase 6  默认对话 system prompt
Phase 7  Stop / Regenerate / 导出 / 移动端
```

推荐执行方式：

1. 每个 Phase 只改对应范围。
2. 每个 Phase 完成后更新本文档状态。
3. 每个 Phase 都必须跑后端测试和前端 build。
4. 如果 Phase 中发现 runtime bug，先写复现测试，再做最小修复。
5. 不允许为了 UI 改造破坏 v0.0.22 Web API 兼容。

---

## 十五、变更日志

| 日期 | 改动 |
|---|---|
| 2026-07-05 | 初稿。基于 v0.0.21 baseline，定义 P0/P1/P2 三档路线 |
| 2026-07-06 | 修改为「无右栏调试面板」方案；当前 turn 信息改为中间窗口淡化展示；新增 Skills/MCP 用户添加与可视化计划 |
| 2026-07-06 | P0-3 完成：`list_files` / `view_file` 工具 + `FileBlock` 注入 UserMessage。**附件统一注入 FileBlock（不发全文 / 不发 path）；图片明确不支持（不做 ImageBlock / 不做 GLM-4V 直读 / 不做 OCR）；view_file 支持 md / html / csv / parquet / 文本**。`POST /api/prompt` 加 `file_ids`。pyarrow>=15 入依赖。764 测试通过，ruff 通过。 |
| 2026-07-07 | **P0-4 Step 1–7 完成**（前端 Claude-like 改造，本轮共 8 个 step，剩 Step 8 docs 收尾）。Step 1 Skills API：`POST /api/skills/upload`、enable/disable、`GET /api/skills/{name}`、`POST /api/prompt` 顶层 `skill_names`；unknown skill 由 web 层预校验返回 400（不再走 500）；26 用例。Step 2 MCP API：servers CRUD、test connection（不污染 harness）、enable/disable、tools enable/disable 真实生效（`agent.tools.unregister/register`）；env values 严格不回显（response 类型只有 `env_keys`）；35 用例。Step 3 前端骨架：Pinia + `types/` + `api/` + `stores/` 五个 store；barrel `index.ts` 保证旧 .vue import 不破坏。Step 4 UI 骨架：`AppShell` + `SessionSidebar` + `ChatPanel` 两栏；`DeveloperDrawer` 从主路径下线但文件保留；9 个新 .vue。Step 5 Inline Turn Cards：`chatStore.handleEvent` 完整 WS event mapper；7 个 card 组件（TurnInfoCard / ToolCallCard / ToolResultCard / FileReadCard / MCPToolCard / SkillUsedCard / ErrorCard）；streaming assistant draft。Step 6 文件上传 UI：`utils/files.ts` + `FileChip` + `AttachmentBar`；`ChatInput` 附件按钮 + drag-drop；`sendPrompt` 带 `file_ids` + `files`；失败保留 pending attachments。Step 7 Skills/MCP Manager Modal：`components/common/Modal.vue`（**居中弹窗，非右栏 Drawer**）+ `components/skills/{SkillManagerModal, SkillUploadForm, SkillList}` + `components/mcp/{MCPManagerModal, MCPServerForm, MCPServerList, MCPToolList}`；SessionSidebar footer 按钮挂载 modal；`mcpStore` 加 `lastTestResultByServer` map（多 server 连续 test 不覆盖）；`createServer/enableServer/disableServer/testServer/deleteServer` 失败时 `reloadServersSilent()` 同步后端真实 `last_error`；delete 时清掉对应 test result 防孤儿。**env value 防回显四道防线**：(1) 后端 summary 类型本身无 `env`；(2) MCPServerForm 永远空白初始化 + 提交后 `resetForm()` 清空所有字段；(3) env input 用 `type=password` + `autocomplete=new-password` 防浏览器回填；(4) MCPServerList 只渲染 `env_keys.join(', ')` + `(values hidden)` 提示。**Step 7 零后端改动**（`web/app.py` 一行未改）。基线：678 offline passed + 59 web skill/mcp passed + ruff clean + npm 123 modules / 127.85 KB JS（gzip 45.08 KB）。 |


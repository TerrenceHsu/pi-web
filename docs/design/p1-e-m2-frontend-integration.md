# P1-E M2 — Frontend Integration Audit

> **状态**：M2-0 FRONTEND INTEGRATION AUDIT — DESIGN DRAFT
> **基线**：master `8b0fb13` — P1-E M1 Runtime ✅ COMPLETE / FROZEN
> **日期**：2026-07-23
> **前置**：[p1-e-m1-provider-runtime.md](p1-e-m1-provider-runtime.md) § 14
> **范围**：M2 前端集成纯审计；不写生产代码、不写测试、不进入 M2-1 编码
> **审核状态**：**待 user 审核**——M2 production coding ⛔ BLOCKED BY 本文档冻结

---

## 0. 阶段定位

本阶段是**纯审计和设计阶段**。本文档是唯一交付物。

**禁止事项**（整个 M2-0 期间）：
- 修改任何生产代码（前端 / 后端 / API / schema）
- 修改 `package.json` / lockfile
- 新增 npm 依赖
- 进入 M2-1 编码
- merge / tag / push

**完成后只允许**：新增本文档 + 一个原子 commit（`docs: audit P1-E M2 frontend integration`）。

---

## 1. M2 产品目标

在现有 Vue 3 Web UI 中实现最简 Provider/Model 配置与 Session 级切换。

**M2 前端只展示**：GLM / Qwen / Kimi。**Anthropic 后端技术保留，前端不展示**（见 §11.7、§15.9）。

**M2 计划新增/扩展的能力**（实施阶段，非本审计阶段）：
- `providerStore`
- `ProviderSelector`（ChatPanel header 内）
- `ProviderSettingsModal`
- Session Binding 加载与切换
- API Key 配置（write-only）
- Model ID 配置（手动输入 + 后端静态建议）
- Provider/Profile 状态与安全错误展示

### 1.1 M1 已冻结的后端语义（M2 必须遵守）

| # | 语义 | 前端含义 |
|---|---|---|
| 1 | Session Binding 决定 Provider/Profile/model_id | Selector 显示当前 Binding |
| 2 | 请求开始时创建不可变 `RequestProviderSelection` | 请求运行中 Selector 禁用 |
| 3 | 运行中修改 Binding 只影响下一次 Prompt/Regenerate | UI 表达"下次生效"（决策点 4） |
| 4 | 不主动 abort 当前请求 | Selector 禁用是 UX 选择，后端不强制 |
| 5 | Session 无 Binding → legacy client | "未配置/默认"文案（决策点 3） |
| 6 | Binding 配置无效 → 安全失败，禁止自动 fallback | 错误展示用固定 4 安全码 |
| 7 | Prompt + Regenerate 都经 `_execute_prompt` 唯一接入点 | 切换语义对两者一致 |
| 8 | Secret 只由后端 `CredentialService` 读取 | 前端永远不持有明文 Key |
| 9 | 前端永远不能读回已保存 API Key | Key 输入是 write-only 字段 |
| 10 | AssistantMessage/Revision 已保存实际 provider/model/usage | 消息展示可读取（非执行路径） |

---

## 2. M2-0 审计目标

本文档必须回答的 10 个问题（对应后续章节）：

1. 当前前端架构如何组织？→ §3, §4
2. Provider 相关后端 API 的真实契约？→ §6
3. Provider 状态应由哪个 Store 持有？→ §8
4. Selector 应放在哪里？→ §9
5. Settings Modal 的最小操作流程？→ §10
6. Session 切换时如何恢复当前 Binding？→ §7, §12
7. 请求运行时 Selector 如何表现？→ §13
8. API Key 如何做到 write-only？→ §8.3, §15
9. 多 Profile 后端能力如何投影为最简 M2 UI？→ §11
10. M2 应拆成哪些可独立审核的编码阶段？→ §19

---

## 3. 当前前端架构图

### 3.1 技术栈（来自 `package.json`）

| 依赖 | 版本 | 用途 |
|---|---|---|
| Vue | ^3.4.0 | 框架（Composition API） |
| Pinia | ^2.2.0 | 状态管理 |
| Vite | ^5.0.0 | 构建 + dev server |
| TypeScript | ^5.9.3 | 类型系统 |
| ESLint | ^10.6.0 | Lint（flat config） |
| Prettier | ^3.9.4 | 格式化 |
| vue-tsc | ^3.3.6 | Vue TS 编译器 |
| @vitejs/plugin-vue | ^5.0.0 | Vue SFC 支持 |

**无测试框架**（无 vitest / jest / playwright 依赖；本精简副本 CLAUDE.md 明确无 `tests/e2e/`）。

### 3.2 npm scripts（来自 `package.json`）

```
dev          vite
build        vue-tsc --noEmit && vite build
build:e2e    vite build (mode=e2e)——暴露 __storeHooks
typecheck    vue-tsc --noEmit
lint         eslint . --max-warnings=0
lint:fix     eslint . --fix
format       prettier --write .
format:check prettier --check .
```

**M2-0 不增加任何新 script**。

### 3.3 目录结构（annotated）

```
src/
├── api/                     # 11 个 REST 客户端模块
│   ├── client.ts            # 统一 requestJson + ApiError
│   ├── index.ts             # barrel + 向后兼容别名
│   ├── sessions.ts          # Session CRUD + export
│   ├── messages.ts          # 消息 + prompt async
│   ├── regenerate.ts        # Regenerate API
│   ├── files.ts             # 文件上传
│   ├── skills.ts            # Skill CRUD + enable
│   ├── mcp.ts               # MCP server/tool CRUD
│   ├── events.ts            # 事件历史查询
│   ├── state.ts             # 旧 state endpoint
│   └── websocket.ts         # WebSocket 管理（非 REST）
├── components/
│   ├── chat/                # 14 个聊天组件
│   │   ├── ChatPanel.vue            # 主面板：header + MessageList + ChatInput
│   │   ├── ChatInput.vue            # 输入框 + 附件 chip
│   │   ├── MessageList.vue
│   │   ├── MessageBubble.vue        # 含 regenerate button
│   │   ├── AttachmentBar.vue
│   │   ├── FileChip.vue
│   │   ├── FileReadCard.vue
│   │   ├── CardDetails.vue
│   │   ├── ToolCallCard.vue
│   │   ├── ToolResultCard.vue
│   │   ├── MCPToolCard.vue
│   │   ├── SkillUsedCard.vue
│   │   ├── TurnInfoCard.vue
│   │   └── ErrorCard.vue
│   ├── common/              # 4 个通用 UI 原语
│   │   ├── Modal.vue                # 通用 Modal（teleport + ESC + scroll lock）
│   │   ├── EmptyState.vue
│   │   ├── ErrorBanner.vue          # 可关闭的内联错误条
│   │   └── LoadingSpinner.vue
│   ├── layout/
│   │   ├── AppShell.vue             # CSS Grid 260px + 1fr
│   │   └── SessionSidebar.vue       # session 列表 + Skills/MCP 入口
│   ├── skills/              # 3 个 Skills 管理组件
│   │   ├── SkillManagerModal.vue    # Skills 模态（参考样例 1）
│   │   ├── SkillList.vue
│   │   └── SkillUploadForm.vue
│   └── mcp/                 # 4 个 MCP 管理组件
│       ├── MCPManagerModal.vue      # MCP 模态（参考样例 2）
│       ├── MCPServerList.vue
│       ├── MCPServerForm.vue
│       └── MCPToolList.vue
├── stores/                  # 5 个 Pinia store
│   ├── chatStore.ts         # 1743 行——最大 store
│   ├── sessionStore.ts      # 101 行
│   ├── fileStore.ts         # 103 行
│   ├── skillStore.ts        # 122 行
│   └── mcpStore.ts          # 229 行
├── types/                   # 8 个类型模块（barrel: index.ts）
├── utils/files.ts
├── App.vue                  # 根组件 + bootstrap
├── main.ts                  # createApp + Pinia setup
└── styles.css               # 全局样式 + CSS 变量
```

**命名约定**：组件 PascalCase（`ChatPanel.vue`）；TS 模块 camelCase/kebab-case；无 `Provider*` 文件（M2 是 greenfield）。

### 3.4 真实组件依赖图

```
main.ts
  └─ App.vue
      ├─ <AppShell>
      │   ├─ #sidebar → <SessionSidebar>
      │   │              ├─ <LoadingSpinner>
      │   │              ├─ <EmptyState>
      │   │              ├─ <SkillManagerModal>   ← 参考样例 1
      │   │              ├─ <MCPManagerModal>     ← 参考样例 2
      │   │              └─ session list / actions
      │   └─ #main   → <ChatPanel>
      │                  ├─ header (session title + status pill)
      │                  │     ↑ **ProviderSelector 自然插入点**（见 §9）
      │                  ├─ <ErrorBanner>
      │                  ├─ <MessageList> → <MessageBubble> × N
      │                  └─ <ChatInput>
      └─ onMounted: sessionStore.loadSessions → chatStore.connectEvents
```

### 3.5 Modal 体系（参考样例）

**通用 `Modal.vue`**：
- Props：`open: boolean`、`title?: string`、`width?: string`（默认 `min(900px, 92vw)`）
- Slots：default（body）/ `#header`（覆盖 title）/ `#footer`（自定义按钮）
- 行为：teleport to `<body>`；click outside 关闭；ESC 关闭；body scroll lock；z-index 100

**SkillManagerModal / MCPManagerModal**（设计模板）：
- Props：`open: boolean`
- Emits：`close`
- Watch：`open` → `loadSkills()/loadServers() + 清错误`
- 内部组合：`ErrorBanner` + `LoadingSpinner` + 业务子组件（List + Form）

**M2 `ProviderSettingsModal` 应直接套用此模式**（见 §10）。

---

## 4. 当前 Store 依赖图

### 4.1 Store 清单

| Store | 行数 | 职责 | 关联 API |
|---|---|---|---|
| `chatStore` | 1743 | 消息流 + WS 事件 + Prompt/Regenerate 编排 | `/api/messages`, `/api/prompt*`, `/api/events`, `/ws/events` |
| `sessionStore` | 101 | Session 列表 + 切换 + CRUD | `/api/sessions` |
| `fileStore` | 103 | 附件上传/删除 | `/api/files` |
| `skillStore` | 122 | Skills 列表 + enable | `/api/skills` |
| `mcpStore` | 229 | MCP server + tool CRUD | `/api/mcp/*` |

### 4.2 chatStore 关键 state（与 M2 相关）

```typescript
// 请求执行状态（用于 Selector 禁用）
sending: Ref<boolean>            // true while 202 pending → completed
streaming: Ref<boolean>          // true while streaming
currentRequestId: Ref<string | null>
pendingRequest: Ref<boolean>

// Session 隔离
activeSessionId: Ref<string | null>
wsConnected: Ref<boolean>
wsReconnecting: Ref<boolean>

// Regenerate（与 Prompt 共用同一执行路径）
regeneration: Ref<RegenerationState>

// 事件去重（global sequence）
lastGlobalSequence: Ref<number>
seenEventIds: Set<string>        // FIFO 1000 上限
```

### 4.3 sessionStore 关键 state

```typescript
sessions: Ref<SessionSummary[]>
activeSessionId: Ref<string | null>
loading: Ref<boolean>
error: Ref<string | null>
```

### 4.4 现有 Store-to-Store 模式

- `App.vue` onMounted：顺序调用 `sessionStore.loadSessions()` → `chatStore.setActiveSession(sid)` → `Promise.all([chatStore.loadMessages, fileStore.loadFiles])`
- 切换 session：`sessionStore.setActiveSession(id)` → `chatStore.setActiveSession(id)` → `chatStore.resetForSession()` → 各 store 重载
- **没有** store-to-store 直接依赖（无 `chatStore` import `sessionStore`）；协调发生在组件层

### 4.5 现有 Store-to-Component 模式

```typescript
// ChatPanel.vue:19-24
const activeSessionId = computed(() => sessionStore.activeSessionId)
const items = computed(() => chatStore.streamItems)
const errorMessage = computed(() => chatStore.error || fileStore.error)
```

**M2 `providerStore` 应沿用此模式**——不持有 chatStore 引用，由组件 `computed` 组合。

---

## 5. 当前 Session 生命周期

### 5.1 App 启动（`App.vue:19-60`）

```
main.ts: createApp(App).use(pinia).mount("#app")
  ↓
App.vue onMounted:
  1. sessionStore.loadSessions()                  # GET /api/sessions
  2. if no active: sessionStore.createNewSession() # POST /api/sessions
  3. chatStore.setActiveSession(sid)
  4. Promise.all([chatStore.loadMessages, fileStore.loadFiles])
  5. chatStore.findActiveRequest(sid)              # 恢复 active 请求
  6. chatStore.connectEvents()                     # WebSocket
  7. skillStore.loadSkills(); mcpStore.loadServers(); mcpStore.loadTools()
```

**M2 启动追加**：`providerStore.initialize()`——`loadDefinitions` + `loadProfiles` + `loadCredentials`（全局缓存，与 session 无关）。**不**在启动时加载 binding——binding 是 per-session。

### 5.2 创建 Session（`SessionSidebar.vue:48-62`）

```
1. sessionStore.createNewSession()                # POST /api/sessions
2. chatStore.setActiveSession(s.id)
3. chatStore.resetForSession()
4. chatStore.loadMessages(s.id)
5. fileStore.loadFiles(s.id); fileStore.resetForSession()
```

**后端自动创建 default Binding**（`initialize_new_session_binding`），但前端目前**不知道**。

**M2 追加**：步骤 3.5：`providerStore.loadBinding(s.id)`——读取自动创建的 default Binding；Selector 显示 default Profile。

### 5.3 切换 Session（`SessionSidebar.vue:32-44`）

```
1. sessionStore.setActiveSession(id)
2. chatStore.setActiveSession(id)
3. chatStore.resetForSession()                    # 清流 + 清 currentRequestId
4. chatStore.clearRegenerationForSessionSwitch()
5. chatStore.loadMessages(id)
6. fileStore.loadFiles(id); fileStore.resetForSession()
```

**无显式 abort in-flight requests**——靠 `activeSessionId` 过滤事件。

**M2 追加**：步骤 2.5：`providerStore.refreshForSession(id)`——cancel 进行中的 binding API、清 `currentBinding`、从服务端重载。

### 5.4 删除 Session（`sessionStore.ts:69-81`）

```
1. DELETE /api/sessions/{id}
2. 从 sessions 数组移除
3. 若是当前 active：activeSessionId ← 第一条或 null
4. 若无 active：createNewSession()
```

**M2 追加**：删除后无需额外清理——binding 是 session 附属，DB cascade；前端只需切换到新 session 时正常走 §5.3。

### 5.5 Prompt（`chatStore.ts:716-741`）

```
1. sending = streaming = true
2. POST /api/prompt/async → 202 + request_id
3. currentRequestId = request_id
4. WS 事件 message_start → message_update × N → message_end → request_end
5. pollRequestUntilTerminal → sending = streaming = false
```

**M2 Selector 禁用 getter**（草案）：

```typescript
const providerStore_disabled = computed(() =>
  chatStore.sending || chatStore.streaming || !!chatStore.currentRequestId
)
```

### 5.6 Regenerate（`chatStore.ts:818-864`）

```
1. POST /api/sessions/{sid}/messages/{aid}/regenerate → 202
2. regeneration.status = queued → running → syncing → completed
3. 单独 draft item（regen-draft:{request_id}）接收 stream delta
4. 完成 → reconcileMessagesFromServer → 删 draft
```

**与 Prompt 共享 `sending/streaming/currentRequestId`**——Selector 禁用条件相同。

---

## 6. 真实后端 API Contract 表

### 6.1 Provider Definitions

#### `GET /api/provider-definitions`
- Body：无
- 200 响应：

```json
{
  "providers": [
    {"id": "anthropic", "display_name": "Anthropic",
     "api_style": "anthropic", "validation_supported": true,
     "supports_model_listing": true},
    {"id": "glm", "display_name": "Zhipu GLM (Anthropic-compatible)",
     "api_style": "anthropic", "validation_supported": false,
     "supports_model_listing": false},
    {"id": "qwen", "display_name": "Qwen",
     "api_style": "openai_compatible", "validation_supported": false,
     "supports_model_listing": false},
    {"id": "kimi", "display_name": "Kimi",
     "api_style": "openai_compatible", "validation_supported": false,
     "supports_model_listing": false}
  ]
}
```

- **注意：返回 Anthropic**（M2 前端需过滤；见决策点 §15.9）
- 不读 Secret；不发网络
- 源：`web/credentials_api.py:690-696`

### 6.2 Credential Endpoints（7 个）

| Method | Path | Body | 200/201 响应 |
|---|---|---|---|
| GET | `/api/credentials` | — | `{credentials: [CredentialView]}` |
| POST | `/api/credentials` | `CredentialCreateRequest` | `{credential: CredentialView, warnings: []}` (201) |
| PATCH | `/api/credentials/{id}` | `{label: str}` | `{credential: CredentialView}` |
| PUT | `/api/credentials/{id}/secret` | `CredentialRotateRequest` | `{credential: CredentialView, warnings: []}` |
| DELETE | `/api/credentials/{id}` | — | `{credential_id, deleted: true, warnings: []}` |
| POST | `/api/credentials/{id}/validate` | `{provider_id}` | `{credential_id, provider_id, attempted, valid, error_code, validation_status, last_validated_at}` |
| POST | `/api/provider-hints` | `{secret_value}` | `{candidates, confidence, reason_code}` |

**CredentialCreateRequest**：

```json
{
  "label": "1-128 chars",
  "storage_mode": "keyring|session_only|env",
  "secret_value": "sk-...",           // keyring/session_only 必填；env 必无
  "env_var_name": "MY_VAR"             // env 必填；其它必无
}
```

**CredentialRotateRequest**（PUT secret）：

```json
{"secret_value": "...", "env_var_name": "..."}  // 至少一个
```

**CredentialView**（所有 endpoint 共用，**无明文**）：

```json
{
  "credential_id": "cred-...",
  "label": "...",
  "storage_mode": "keyring|session_only|env",
  "storage_status": "ready|needs_key|backend_unavailable",
  "masked_value": "sk-****5678",       // < 8 字符则 "********"；env 则 "ENV[VAR_NAME]"
  "provider_hint": "anthropic|glm|qwen|kimi|null",
  "provider_hint_confidence": "high|unknown|null",
  "validation_status": "valid|invalid|never_validated|...",
  "last_validated_provider_id": "...|null",
  "last_validated_at": 1735084800000|null,
  "last_error_code": "...|null",
  "created_at": 1735084800000,
  "updated_at": 1735084800000
}
```

**安全事实**：
- 永远不返回 `secret_value`
- `masked_value` 格式：长度<8 → `"********"`；≥8 → 首 3 + `****` + 末 4；env → `"ENV[VAR_NAME]"`
- 源：`secrets/utils.py:30-62`

### 6.3 Provider Profile Endpoints（5 个）

| Method | Path | Body | 响应 |
|---|---|---|---|
| GET | `/api/provider-profiles` | — | `{profiles: [ProfileView]}` |
| POST | `/api/provider-profiles` | `ProviderProfileCreateRequest` | `{profile: ProfileView}` (201) |
| PATCH | `/api/provider-profiles/{id}` | `ProviderProfileUpdateRequest` | `{profile: ProfileView}` |
| DELETE | `/api/provider-profiles/{id}` | — | 204 No Content |
| GET | `/api/provider-profiles/{id}/models` | — | `{models: [...]}` |

**ProviderProfileCreateRequest**：

```json
{
  "name": "1-128 chars",
  "provider_id": "anthropic|glm|qwen|kimi",   // 创建后 immutable
  "credential_id": "cred-...",
  "default_model": "...",                       // 必填非空
  "enabled": true,
  "is_default": false
}
```

**ProviderProfileUpdateRequest**（至少一个字段；`provider_id` 不被接受 → 422）：

```json
{
  "name": "...",
  "credential_id": "...",     // **可以切换到另一个 credential_id**
  "default_model": "...",
  "enabled": false,
  "is_default": true           // PATCH is_default=true 切换全局 default
}
```

**ProfileView**：

```json
{
  "id": "prof-...",
  "name": "...",
  "provider_id": "anthropic|glm|qwen|kimi",
  "provider_display_name": "...",
  "credential_id": "cred-...|null",
  "credential_masked_value": "sk-****5678|null",
  "default_model": "...",
  "enabled": true,
  "is_default": false,
  "status": "ready|disabled|needs_credential|needs_key|backend_unavailable|credential_invalid|credential_error",
  "created_at": 1735084800000,
  "updated_at": 1735084800000
}
```

**`/models` 端点**：
- Anthropic：返回静态模型列表（不发网络）
- GLM/Qwen/Kimi：返回 `[]`（静态空数组，**不**调远端）
- M2-2 必须允许空数组时手动输入 `model_id`

### 6.4 Session Binding Endpoints（2 个）

#### `GET /api/sessions/{sid}/model-binding`
- 200 响应（无 binding）：`{"binding": null}`  ← **不返回 404**
- 200 响应（有 binding）：

```json
{
  "binding": {
    "session_id": "sess-...",
    "profile_id": "prof-...",
    "model_id": "...",
    "source": "default|explicit",
    "created_at": 1735084800000,
    "updated_at": 1735084800000
  }
}
```

- 404：session 不存在

#### `PUT /api/sessions/{sid}/model-binding`
- Body：`SessionModelBindingPutRequest`

```json
{"profile_id": "prof-...", "model_id": "..."}
```

- 200：返回完整 binding（同 GET）
- 409：`profile_disabled`（试图绑定 disabled profile）

### 6.5 关键 API 行为事实

| 问题 | 答案 | 源 |
|---|---|---|
| Credential API 返回明文 Key？ | **否**，仅 `masked_value` | `credentials_api.py:624-643` |
| 列表返回 `credential_id`？ | 是 | 同上 |
| PATCH Profile 空 `api_key`？ | 不适用——Profile API **不接受** `api_key` 字段；Key 走独立 Credential API | `provider_profiles_api.py:126-136` |
| Credential 更新是原地还是新建？ | **原地 CAS**——`rotate` 修改现有 record，不新建 ID | `credentials_service.py:478-632` |
| Profile 能否切换 `credential_id`？ | **能**（PATCH 接受） | `provider_config_service.py:343-369` |
| Profile `provider_id` 是否 immutable？ | **是**（PATCH 不接受，422） | `provider_profiles_api.py:126-136` |
| 删除被 Session 使用的 Profile 返回？ | **409 + `profile_in_use`** | `provider_config_store.py:119` |
| 删除 Credential 后 Profile 状态？ | `needs_credential` | service 派生 |
| `/models` 对 Qwen/Kimi 返回？ | `[]`（静态空） | tests |
| 无 Binding 的 API 响应？ | `200 + {binding: null}` | `provider_profiles_api.py:550-560` |
| Anthropic 出现在 `/api/provider-definitions`？ | **是**（M2 前端需过滤） | `test_provider_definitions_api_qwen_kimi.py:92-96` |
| Auth header？ | 必须有 `X-PI-Agent-UI: 1`，否则 400 `missing_ui_header` | `credentials_api.py:275-319` |
| Body 大小限制？ | 32 KiB（mutation），超出 413 | 同上 |

### 6.6 新 Session 默认 Binding 初始化

后端 `initialize_new_session_binding`（`provider_config_service.py`）在 `POST /api/sessions` 时自动物化：
- 若存在 `is_default=true` 的 enabled Profile → 创建 `source="default"` binding 指向它
- 否则 binding 不存在（GET 返回 `{binding: null}`）

**前端无 API 调用触发**——是后端 side-effect。M2 前端只需在创建 session 后 GET 一次 binding 即可读到。

---

## 7. Session 切换 stale-response 分析

### 7.1 现有保护机制

| 机制 | 位置 | 用途 |
|---|---|---|
| `currentRequestId` | `chatStore.ts:195` | turn-control 事件必须匹配此 ID |
| `activeSessionId` 过滤 | `chatStore.ts:1096-1103` | WS 事件按 `session_id` 过滤 |
| `seenEventIds` FIFO | `chatStore.ts:160-176` | 事件去重（1000 上限） |
| `lastGlobalSequence` | `chatStore.ts:187` | gap 检测 + reconnect replay |
| `AbortController` | `chatStore.ts:556-582` | `abortRun` 用；其它 API 未用 |
| 模块级 boolean lock | `chatStore.ts:56`（`_regenerateInFlight`） | 防双击 regenerate |

### 7.2 M2 新增的 race 场景

| 场景 | 现状 | M2 风险 |
|---|---|---|
| Session A binding API pending → 切到 B → A 响应到达 | 无保护 | A 的 binding 覆盖 B 的 `currentBinding` |
| 切换 session 时 Settings Modal 开着 | 无保护 | Modal 显示 A 的 Profile，保存写到当前 active（B） |
| 创建 session → binding 初始化未读到 → 用户发 prompt | 无保护 | Prompt 看到 legacy client（其实是 default binding） |
| 切换 session → loadDefinitions 与 loadBinding 并发 | 定义是全局，binding 是 per-session | 乱序时 selector 显示空 |
| 用户快速切换多个 session | 每个 session 触发 `loadBinding` | 多个响应乱序到达 |

### 7.3 推荐 stale-response 方案（最小且匹配现有架构）

**方案：请求级 `requestToken` + 应用响应前校验 `activeSessionId`**

```typescript
// providerStore.ts (草案——非生产代码)
let bindingLoadToken = 0

async function loadBinding(sessionId: string) {
  const token = ++bindingLoadToken
  const resp = await api.getSessionBinding(sessionId)
  // 应用响应前校验：token 未被取代 且 session 仍为 active
  if (token !== bindingLoadToken) return
  if (sessionId !== sessionStore.activeSessionId) return
  currentBinding.value = resp.binding
}
```

**理由**：
- 匹配现有 `currentRequestId` 思路（M2-1 实现详情，本文档只描述方向）
- 不引入 `AbortController`（现有代码仅在 abort 用）
- 切换 session 时 `++token` 自动作废旧请求

**Settings Modal 守护**：Modal 在保存前检查 `capturedSessionId === activeSessionId`；不一致时显示 "session has changed" 并关闭。

**默认 binding race**：创建 session 后先 GET binding 再允许发 prompt；若 binding 未读到，`canSendPrompt` getter 返回 false。

---

## 8. providerStore 最小契约（草案）

### 8.1 State

```typescript
// 全局缓存（与 session 无关）
definitions: Ref<ProviderDefinitionView[]>           // from /api/provider-definitions
profiles: Ref<ProviderProfileView[]>                // from /api/provider-profiles
credentials: Ref<CredentialView[]>                  // from /api/credentials

// Per-session
currentBinding: Ref<SessionBindingView | null>
bindingSessionId: Ref<string | null>                // 守护：binding 属于哪个 session

// 加载 flag（分维度，避免一个全局 loading 让所有 UI 闪烁）
definitionsLoading: Ref<boolean>
profilesLoading: Ref<boolean>
credentialsLoading: Ref<boolean>
bindingLoading: Ref<boolean>

// 保存 flag
savingCredential: Ref<boolean>
savingProfile: Ref<boolean>
savingBinding: Ref<boolean>

// 错误（分维度）
loadError: Ref<string | null>
mutationError: Ref<string | null>

// 初始化标志
initialized: Ref<boolean>
```

**决策点（§15.2）**：是否拆 `loadError` 为 `definitionsLoadError / profilesLoadError / ...`——建议 M2-1 保持上述 4 维度，不过度拆。

### 8.2 Getters

```typescript
// 只展示 GLM/Qwen/Kimi（过滤 Anthropic）
visibleDefinitions: ComputedRef<ProviderDefinitionView[]>

// enabled 且 status==="ready"（可执行）
usableProfiles: ComputedRef<ProviderProfileView[]>

// 按 provider_id 分组（用于 Settings Modal 分区展示）
profilesByProvider: ComputedRef<Record<string, ProviderProfileView[]>>

// 当前 binding 投影（用于 Selector 显示）
selectedProfile: ComputedRef<ProviderProfileView | null>
selectedProvider: ComputedRef<ProviderDefinitionView | null>
selectedModel: ComputedRef<string | null>

// 状态
currentProfileStatus: ComputedRef<string | null>     // "ready" | "needs_credential" | ...

// 是否允许发 prompt（binding 已读 + profile ready）
canSendPrompt: ComputedRef<boolean>
```

### 8.3 Actions

```typescript
// 初始化（App.vue onMounted 调用一次）
initialize(): Promise<void>

// 独立加载（错误独立）
loadDefinitions(): Promise<void>
loadCredentials(): Promise<void>
loadProfiles(): Promise<void>

// Per-session
loadBinding(sessionId: string): Promise<void>        // 内含 stale-response 防护
refreshForSession(sessionId: string): Promise<void>  // session 切换时
clearSessionState(): void                             // 登出 / 切换

// Mutations（参数草案；实际签名 M2-1 决定）
createOrUpdateCredential(payload: {...}): Promise<string>  // 返回 credential_id
createOrUpdateProfile(payload: {...}): Promise<string>     // 返回 profile_id
setSessionBinding(sessionId: string, profileId: string, modelId: string): Promise<void>

// 错误管理
resetError(): void
```

### 8.4 安全不变量（冻结）

| 规则 | 实施 |
|---|---|
| Store 不保存 API Key 明文 | Key 只在 `ProviderSettingsModal` 局部 `ref`；mutation 后立即 `apiKey.value = ""` |
| Store 不持久化到浏览器存储 | 现有架构 zero localStorage/sessionStorage；M2 保持 |
| `masked_value` 可以入 state | 非敏感，用于 Selector 提示 |
| `credential_id / profile_id / provider_id / model_id` 可入 state | 非敏感 |
| Key 不进 Pinia devtools | Key 不进 state → 不进 devtools |
| Key 不进 `console.log` | lint 规则 + code review |
| Key 不进 error 对象 | mutation error 来自后端固定消息，不含 Key |
| Key 输入框 `type="password"` | Modal 组件 |
| Modal unmount/close 后清空 | watch(open) → reset |

### 8.5 数据归属

| 数据 | 归属 | 理由 |
|---|---|---|
| `definitions / profiles / credentials` | 全局（providerStore） | 与 session 无关 |
| `currentBinding / bindingSessionId` | Per-session（providerStore） | 切换时重载 |
| `apiKey` 输入 | **Modal 组件局部** | write-only；不入 store |
| `modelInput / profileName` 草稿 | **Modal 组件局部** | 未保存前不属于业务 state |
| Modal open 状态 | **SessionSidebar 局部**（与 Skills/MCP 一致） | UI 状态 |

---

## 9. ProviderSelector UX 草案

### 9.1 放置位置（决策点 §15.10）

**推荐**：`ChatPanel.vue` header，紧邻 session title + status pill。

**理由**：
- header 已有 status pill 视觉模板（CSS 可复用）
- 不会被 Modal/z-index 遮挡（Modal teleport 到 body）
- 切换 Provider 是高频操作，放在视线焦点
- 不挤压 ChatInput 的附件区

**备选**：`ChatInput.vue` composer toolbar——优点是离发送按钮近；缺点是低频操作占用高频空间。

### 9.2 Selector item 真实身份

**Selector 项 = Profile**（不是 Provider、不是 Provider+Model 两级）。

**理由**：
- 后端执行需要 `profile_id`（含 credential_id 引用）
- ProviderDefinition 不含 credential，无法直接执行
- 两级 UI（选 Provider → 选 Model）增加点击次数，违反"最简"原则

**每个 item 显示**（决策点 §15.1、§15.7）：
```
[Provider icon] Profile.name (model_id 截断)
                provider_display_name · masked_value
```

**已选状态**：
```
[Provider icon] Profile.name
                provider_display_name · model_id
```

### 9.3 各状态展示

| 场景 | Selector 显示 |
|---|---|
| Session 加载中（`bindingLoading`） | skeleton 灰条 |
| 无 Binding（`currentBinding === null`） | "默认模型" 或 "未配置"（决策点 §15.3） |
| Profile `status === "ready"` | 正常显示 |
| Profile `status === "disabled"` | 灰显 + 禁用切换 + tooltip |
| Profile `status === "needs_credential" / "needs_key"` | 红点 + 引导打开 Settings |
| 无任何 enabled Profile | "未配置 Provider" + 按钮打开 Settings |
| 请求运行中（`chatStore.sending` 等） | 整个 Selector disabled + tooltip "回答完成后可切换" |
| 长 `model_id` | CSS `text-overflow: ellipsis` + tooltip 完整值 |

### 9.4 展开层级与遮挡

- Selector 是 `<select>`-style dropdown（非 nested menu）
- 单层；不嵌套 Modal
- z-index 低于 Modal（100）；Selector 自身 z-index ≤ 50

### 9.5 运行中切换语义（决策点 §15.4）

**推荐**：运行中 Selector **disabled**。

**理由**：
- 简化 UX——不需要解释"下次生效"
- 后端已保证：即使前端强行 PUT binding 也只影响下次，所以禁用是 UX 选择
- tooltip 提示 "当前回答完成后可切换"

**备选**：允许修改但显示"下次请求生效"——增加 UI 解释负担。

### 9.6 Anthropic 隐藏（决策点 §15.9）

**推荐**：`visibleDefinitions` getter 过滤掉 `id === "anthropic"`。

**Profile 列表**：若 DB 中存在 Anthropic Profile（legacy 或 API 创建），M2 也从 `usableProfiles` 排除（但保留在 `profiles` 数组中——不删数据）。

**Settings Modal**：不展示 Anthropic 卡片；不提供创建 Anthropic Profile 入口。

---

## 10. ProviderSettingsModal UX 草案

### 10.1 视觉布局（参考 SkillManagerModal / MCPManagerModal）

```
┌─ Modal (width: min(900px, 92vw)) ─────────────────────────┐
│ #header: "Provider Settings"                          [×] │
├─────────────────────────────────────────────────────────────┤
│ <ErrorBanner v-if="mutationError" :message="..." />         │
│ <LoadingSpinner v-if="profilesLoading" />                   │
│                                                              │
│ ┌─ GLM ─────────────────────────────────────────────────┐   │
│ │ Status: ready · masked: sk-****5678                    │   │
│ │ ┌─ Form ────────────────────────────────────────────┐ │   │
│ │ │ Profile name: [____________________]               │ │   │
│ │ │ Model ID:    [____________________] [suggestions ▾]│ │   │
│ │ │ API Key:     [••••••••••] (placeholder masked_val) │ │   │
│ │ │ ☐ Set as default profile                           │ │   │
│ │ │ [Save] [Bind to current session]                   │ │   │
│ │ └────────────────────────────────────────────────────┘ │ │   │
│ └────────────────────────────────────────────────────────┘   │
│ ┌─ Qwen ────────── ─────────────────────────────────────┐   │
│ │ (同上)                                                 │   │
│ └────────────────────────────────────────────────────────┘   │
│ ┌─ Kimi ────────────────────────────────────────────────┐   │
│ │ (同上)                                                 │   │
│ └────────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ #footer: [Close]                                            │
└─────────────────────────────────────────────────────────────┘
```

### 10.2 每个 Provider 卡片显示

| 字段 | 来源 | 编辑 |
|---|---|---|
| Provider name | `provider_display_name`（GLM/Qwen/Kimi） | 不可改 |
| Profile name | Profile.name（若存在） | 文本输入 |
| Model ID | Profile.default_model（若存在） | 文本输入 + datalist（来自 `/models`） |
| API Key | 空（write-only）；placeholder 显示 `masked_value` | 密码输入 |
| Status | Profile.status | 只读 |
| "Set as default" | Profile.is_default | 复选框 |
| Save 按钮 | — | 触发 §10.3 流程 |
| "Bind to current session" 按钮 | — | 触发 `setSessionBinding` |

### 10.3 保存流程（决策点 §15.6）

```
1. 读 form 局部状态
2. if apiKey.value 非空:
     if existing credential_id: PUT /api/credentials/{id}/secret (rotate 原地)
     else:                      POST /api/credentials (新建) → 取 credential_id
3. if existing profile_id: PATCH /api/provider-profiles/{id}
   else:                    POST /api/provider-profiles
4. if "set as default": PATCH /api/provider-profiles/{id} {is_default: true}
5. if "bind to current session": PUT /api/sessions/{sid}/model-binding
6. 成功：清 apiKey.value；关闭或保留 Modal；toast "Saved"
7. 失败：保留 form；显示 mutationError；不清 apiKey（决策点 §15.5）
```

### 10.4 API Key write-only 规则（决策点 §15.5）

| 事件 | 行为 |
|---|---|
| Modal 打开 | `apiKey.value = ""`（永远空） |
| Placeholder | `masked_value` 或 "Enter API key" |
| 用户不填写 Key 直接 Save | **不**调 Credential mutation；只改 Profile/Model |
| 用户填写新 Key 并 Save | 调 mutation；成功后立即 `apiKey.value = ""` |
| Mutation 失败 | **保留** `apiKey.value` 以便重试（决策点 §15.5）；显示错误 |
| Modal 关闭（任何方式） | `apiKey.value = ""` |
| Modal unmount | 同上（`watch(open)` 触发清理） |

### 10.5 Model ID 输入策略

- 输入框 + `<datalist>`（来自 `/api/provider-profiles/{id}/models`）
- Qwen/Kimi `models=[]` →datalist 空，不阻止保存
- 不硬编码前端建议列表（避免快速过时）
- 必填非空（后端 `default_model` 必填）

### 10.6 明确排除的高级功能

- Custom Base URL
- Custom Provider
- 远程模型搜索
- 远程 Key validation（后端有 `/validate` endpoint，M2 不调用——决策点见 §15）
- Provider health / cost dashboard
- 多 Key 批量管理
- 复杂 Profile 表格
- Profile 排序
- import/export credentials
- 自动 fallback
- Anthropic UI
- reasoning UI

---

## 11. 多 Profile 冲突分析（决策点 §15.1）

### 11.1 后端事实

- 后端允许任意数量的 ProviderProfile（每 Provider 多个）
- `is_default=true` 全局唯一（PATCH 时 store 自动清其它 default）
- `name` 不强制唯一（但建议）
- Profile 删除被 Session 引用时返回 409 `profile_in_use`

### 11.2 方案对比

#### 方案 A：每 Provider 显示一个固定 Profile
- **优点**：UI 最简单（3 个卡片）
- **风险**：DB 中已有多 Profile 时需"选一个显示"规则；隐藏数据违反透明原则
- **规则候选**：选 `is_default=true` → 否则选最新 `updated_at`

#### 方案 B：Selector 列出所有 enabled Profiles；Settings Modal 只做简单 CRUD
- **优点**：不丢失后端能力；透明
- **风险**：UI 项目可能变多（3 Providers × N Profile）
- **缓解**：Selector 按 Provider 折叠；Settings Modal 分区展示

#### 方案 C：Provider 一级 + Profile 二级（Provider → Profile 两级 dropdown）
- **优点**：结构清楚
- **风险**：UI 和 state 复杂度更高；违反"最简"

### 11.3 推荐

**方案 B（受限版）**——理由：
1. 不隐藏后端数据（透明）
2. 与现有 skillStore/mcpStore "列表 + CRUD" 模式一致
3. 95% 用户每 Provider 只有 1 Profile，UI 不会膨胀
4. 复杂场景（同 Provider 多 Profile）留给 M3 或更后

**M2-0 推荐约束**：
- M2 允许创建同 Provider 第二个 Profile（不禁止）
- M2 允许删除 Profile（409 时 UI 显示"被 Session 使用，无法删除"）
- 不实现 Profile 排序、批量操作
- Selector item 按 Provider 分组 + Profile.name 区分

**决策点**：需要 user 在审核时确认方案 B（或选 A/C）。

---

## 12. Session 切换 stale-response 方案

见 §7.3（请求级 token + 应用前校验 `activeSessionId`）。

### 12.1 各 race 的具体防护

| Race | 防护 |
|---|---|
| Binding API pending → 切 session | `bindingLoadToken` + 应用前校验 `sessionId === activeSessionId` |
| Settings Modal 开着 → 切 session | Modal `watch(activeSessionId)` 自动关闭 + 提示 |
| 保存中切 session | 保存前 capture `sessionId`；保存响应到达时校验 |
| 默认 binding race（新建 session） | `canSendPrompt` getter 要求 `bindingLoading === false` |
| 快速切换多个 session | `bindingLoadToken` 自增，旧响应自动失效 |

### 12.2 WS 事件与 binding 的交互

- WS 事件不含 binding 变更通知（后端无此事件）
- Binding 变更只能由用户主动 API 调用触发
- 因此**无需在 `chatStore.handleEvent` 中处理 binding 事件**——M2 不增加 WS 事件类型

---

## 13. 请求运行中切换语义

### 13.1 Selector 禁用 getter

```typescript
// ChatPanel.vue 或 ProviderSelector.vue (草案)
const selectorDisabled = computed(() =>
  chatStore.sending ||
  chatStore.streaming ||
  !!chatStore.currentRequestId
)
```

### 13.2 行为

- Selector 整体 `disabled`——点击无反应
- Tooltip "当前回答完成后可切换"
- Settings 入口按钮也 disabled
- 后端仍允许 API 层 PUT binding（前端只是 UX 禁用）

### 13.3 完成后行为

- `chatStore.sending = false` → Selector 自动启用
- **不**自动重载 binding——用户未修改就不会变
- 若用户在禁用期间通过其它 session 修改了 binding，本 session 切回时 `refreshForSession` 自然重载

---

## 14. 错误映射表

### 14.1 后端错误码 → 前端展示

| HTTP | code | M2 展示位置 | 用户文案（草案） |
|---|---|---|---|
| 400 | `missing_ui_header` | 全局 toast | "Client header missing—reload page" |
| 403 | `invalid_origin` | 全局 toast | "Origin not allowed" |
| 404 | `credential_not_found` | Modal ErrorBanner | "Credential no longer exists—reload" |
| 404 | `profile_not_found` | Modal ErrorBanner | "Profile no longer exists—reload" |
| 404 | `session_not_found` | 全局 toast | "Session no longer exists" |
| 409 | `credential_conflict` | Modal form field | "Credential label already in use" |
| 409 | `profile_in_use` | Modal ErrorBanner | "Profile is in use—unbind sessions first" |
| 409 | `profile_disabled` | Modal ErrorBanner | "Profile is disabled—enable first" |
| 413 | `request_body_too_large` | Modal form field | "Input too long" |
| 422 | `request_validation_failed` | Modal form field | Pydantic 错误细节（过滤后） |
| 500 | `credential_internal_error` | 全局 toast | "Server error—try again" |
| 503 | `credential_backend_unavailable` | Modal ErrorBanner | "Keyring unavailable—contact admin" |
| 503 | `provider_config_unavailable` | 全局 toast | "Provider config not initialized" |
| 500 | `provider_profile_unavailable`（Prompt 执行） | ChatPanel ErrorBanner | "Selected provider profile is unavailable." |
| 500 | `provider_profile_disabled`（执行） | 同上 | "Selected provider profile is disabled." |
| 500 | `provider_credential_unavailable`（执行） | 同上 | "Selected provider credential is unavailable." |
| 500 | `provider_initialization_failed`（执行） | 同上 | "Selected provider could not be initialized." |
| — | network failure | 全局 toast | "Network error—check connection" |
| — | unknown 5xx | 全局 toast | "Unexpected error—try again" |

### 14.2 不可显示的信息（安全规则）

- `credential_id` 值
- `secret_ref`
- API Key 任何形式
- Base URL
- 底层 exception 类型 / stack trace
- 完整 response body

### 14.3 错误分类

| 类型 | UI 位置 | 例子 |
|---|---|---|
| 表单字段错误 | 字段下方红字 | `422` invalid_model_id |
| Modal 级错误 | ErrorBanner 顶部 | `409` profile_in_use |
| Selector 状态错误 | 红点 + tooltip | `needs_credential` |
| Prompt 执行错误 | ChatPanel ErrorBanner | `provider_credential_unavailable` |
| 全局/致命错误 | toast | `session_not_found` |

---

## 15. 待 user 审核的关键决策点

> 本文档不自行决定以下 10 项；每项给推荐方案但需 user 确认。

### 15.1 多 Profile UI（§11）

- **可选**：A 固定单 Profile / B 所有 enabled Profiles / C 两级选择
- **推荐**：**B**（受限版）
- **理由**：透明、与现有 store 模式一致、95% 用户不影响
- **需 user 确认**：是否接受 UI 可能有多个同 Provider Profile；是否禁止创建第二个同 Provider Profile（推荐不禁止）

### 15.2 Settings Modal 高级操作范围

- **可选**：仅创建/编辑 / 允许删除 / 允许批量
- **推荐**：创建 + 编辑 + **允许删除**（409 时显示错误）
- **理由**：不让数据不可达
- **需 user 确认**：是否允许删除被使用 Profile（推荐显示 409，不强制解绑）

### 15.3 无 Binding 显示文案

- **可选**："默认模型" / "未配置" / "Legacy" / "Default (no binding)"
- **推荐**：**"Legacy (default)"**——准确描述后端行为（无 binding → legacy client）
- **备选**："Not configured"——但会误导（Session 是配置了的，只是没 binding）
- **需 user 确认**：文案选择

### 15.4 active request 期间 Selector 行为

- **可选**：完全 disabled / 允许改但标 "下次生效"
- **推荐**：**完全 disabled**
- **理由**：UX 简单；后端已保证语义；无需解释"下次生效"
- **需 user 确认**：是否接受不能在运行中切换

### 15.5 API Key 保存失败时是否保留输入

- **可选**：保留以重试 / 立即清空以缩小泄漏窗口
- **推荐**：**保留**——用户重试成本低；Key 仍在 Modal 局部 ref，不入 store
- **备选**：立即清空——安全更严格但 UX 差
- **需 user 确认**：安全 vs 可用性权衡

### 15.6 Credential 更新策略

- **事实**：后端 PUT `/api/credentials/{id}/secret` 是**原地 CAS** rotate（不新建 ID）
- **前端含义**：无需"切换 Profile.credential_id"逻辑——同 ID 更新即可
- **需 user 确认**：理解此语义；M2 Settings Modal 用 PUT secret 而非新建

### 15.7 model_id 切换入口

- **可选**：Selector 直接切 / 必须到 Settings Modal
- **推荐**：**Selector 只切 Profile**（整体）；model_id 在 Settings 改
- **理由**：减少 Selector 复杂度；Profile.default_model 已是单一来源
- **备选**：Selector 内二级菜单选 model——违反"最简"
- **需 user 确认**：model_id 切换是否需要快捷入口

### 15.8 default Profile 修改

- **可选**：Settings Modal 允许改全局 default / M2 只管当前 session binding
- **推荐**：**允许在 Settings 改 is_default**——后端已有 PATCH 语义
- **理由**：用户期望"默认"是可配的
- **需 user 确认**：是否暴露全局 default 切换

### 15.9 Anthropic 隐藏

- **事实**：后端 `/api/provider-definitions` 返回 Anthropic；`provider_runtime.py` 无显式阻止
- **推荐**：**前端 Store 过滤**——`visibleDefinitions` getter 排除 `id === "anthropic"`
- **备选**：后端加 `?hide_internal=true`——违反"不改后端"
- **需 user 确认**：是否接受 Anthropic 在前端完全隐藏；若用户已通过 API 创建 Anthropic Profile（legacy），前端如何处理（推荐也从 `usableProfiles` 排除）

### 15.10 Selector 放置位置

- **可选**：ChatPanel header / composer toolbar / SessionSidebar
- **推荐**：**ChatPanel header**（紧邻 status pill）
- **理由**：视觉焦点；不挤压 composer；与现有 pill 样式一致
- **需 user 确认**：位置选择

---

## 16. 安全边界

### 16.1 依赖方向（冻结）

```
components (ProviderSelector, ProviderSettingsModal)
    ↓
providerStore
    ↓
api/providers.ts (frontend API client)
    ↓
backend REST API
```

**禁止**：
- 组件直接 `fetch/axios`
- `providerStore` import chat 组件
- `sessionStore/chatStore` 保存 API Key
- `chatStore` 管理 Credential/Profile
- 前端 import 后端 Python schema
- `ProviderSettingsModal` 操作 WebSocket
- `ProviderSelector` 直接修改 harness/client
- 任何浏览器端 Provider SDK（`openai`/`anthropic` npm package）

### 16.2 浏览器存储审计（已有）

- `localStorage`：**0 使用**（M2 保持）
- `sessionStorage`：**0 使用**（M2 保持）
- URL/query string：**不传递** Key/credential_id

### 16.3 Pinia devtools

- API Key **不进** Pinia state → 不进 devtools
- Key 只在 `ProviderSettingsModal` 局部 `ref`（不 reactive 到 store）

### 16.4 Console 日志

- 现状：11 个 `console.error`，全在 catch handler，无用户数据
- M2 保持：mutation error 走 `mutationError.value`，**不** `console.log` payload

### 16.5 错误对象

- 后端固定安全 message → 前端直接展示
- 不拼接底层 exception / 完整 response body
- Network 错误的 `ApiError` 不暴露 request body（Key 在 body 中）

### 16.6 浏览器 autofill / password manager

- Key 输入框 `type="password"`
- `autocomplete="new-password"`（避免 browser 复用其它站点密码）
- 不 `v-model` 到 store

### 16.7 DOM 属性 / 截图 / 测试

- Key 输入框 `type="password"` → DOM 不显示明文
- 截图无明文
- 测试 marker：M2-2/M2-3/M2-4 用 `sk-test-marker-...`，全 UI state + log + trace 零命中

### 16.8 前端不调用以下 endpoint（M2 范围排除）

- `POST /api/credentials/{id}/validate`（remote validation；M2 不做）
- `POST /api/provider-hints`（前端不自行推断 Key 归属）

---

## 17. 测试矩阵（M2-1～M2-4 实施时参考，本审计阶段不写测试）

### 17.1 providerStore 单元测试

| 测试场景 | 验证点 |
|---|---|
| `initialize()` | 三类资源加载；`initialized=true` |
| `loadBinding(sid)` | 写入 `currentBinding`；`bindingSessionId === sid` |
| Session 快速切换 | stale response 不覆盖（token 防护） |
| Mutation error | 写入 `mutationError`；不抛到 caller |
| `clearSessionState()` | 清 `currentBinding / bindingSessionId` |
| Secret 不入 state | `apiKey` 不在 store 定义中；spy `console.log` 无 Key |

### 17.2 ProviderSelector 组件测试

| 场景 | 验证点 |
|---|---|
| GLM/Qwen/Kimi 展示 | Anthropic 不出现 |
| 无 binding | 显示 "Legacy (default)"（或决策文案） |
| 已选 Profile | 高亮；model_id 显示 |
| active request | Selector disabled |
| `needs_credential` 红点 | 点击引导打开 Settings |
| 长 model_id | ellipsis + tooltip |
| 切换触发 `setSessionBinding` | 调用 store action |

### 17.3 ProviderSettingsModal 组件测试

| 场景 | 验证点 |
|---|---|
| 新建 Credential + Profile | POST × 2；返回 ID 写入 form |
| 更新 Credential（PUT secret） | 原地 rotate；不新建 ID |
| PATCH Profile | 切 credential_id；改 default_model |
| 空 model_id | 422；字段红字 |
| 409 profile_in_use | ErrorBanner 显示 |
| 空 Key 保存 | 不调 Credential mutation |
| 关闭 Modal | `apiKey.value === ""` |
| 保存成功 | `apiKey.value === ""` |
| 保存失败 | `apiKey.value` 保留（决策点 §15.5） |
| `/models` 返回 `[]` | datalist 空；可手动输入 |

### 17.4 Session 集成测试

| 场景 | 验证点 |
|---|---|
| Session A → Qwen；Session B → Kimi；切换 | 各自 binding 独立恢复 |
| 新建 session | 后端自动 default binding；前端 GET 读到 |
| 旧 session 保持创建时快照 | 切回后不变 |
| 运行中 Selector disabled | `chatStore.sending` true 时 |
| Prompt 完成后可切换 | `sending` false 后启用 |
| 下一 Prompt 使用新 Binding | 切 binding → 发 prompt → 真实 provider 改变 |

### 17.5 E2E（主仓库 `pi-py` 执行）

| 场景 | 验证点 |
|---|---|
| 配置 Qwen Profile + 绑定 + 发送 | 流式回答 |
| 切 Kimi + 下一 Prompt | Kimi 回答 |
| Regenerate | 用当前 binding |
| 重载页面 | binding 恢复 |
| API Key 不出现在 DOM/storage/log/trace | marker 零命中 |

### 17.6 测试基础设施事实

- **本副本无前端测试框架**（`package.json` 无 vitest/jest）
- M2-1 实施时需决定：是否引入 vitest——决策留给 M2-1
- E2E 由主仓库 `pi-py` 验证（CLAUDE.md 明确）

---

## 18. 允许修改文件清单（M2-1～M2-4 实施时）

### 18.1 新增（预期）

| 文件 | 阶段 |
|---|---|
| `src/api/providers.ts` | M2-1 |
| `src/types/providers.ts` | M2-1 |
| `src/stores/providerStore.ts` | M2-1 |
| `src/components/providers/ProviderSelector.vue` | M2-3 |
| `src/components/providers/ProviderSettingsModal.vue` | M2-2 |
| `src/components/providers/ProviderCard.vue`（每 Provider 卡片） | M2-2 |
| `tests/unit/providerStore.spec.ts`（若引入 vitest） | M2-1 |
| `tests/unit/ProviderSelector.spec.ts` | M2-3 |
| `tests/unit/ProviderSettingsModal.spec.ts` | M2-2 |

### 18.2 修改（预期）

| 文件 | 改动 | 阶段 |
|---|---|---|
| `src/api/index.ts` | barrel re-export `providers` | M2-1 |
| `src/types/index.ts` | barrel re-export `providers` | M2-1 |
| `src/App.vue` | onMounted 调 `providerStore.initialize()` | M2-1 |
| `src/components/chat/ChatPanel.vue` | header 插入 `<ProviderSelector>` | M2-3 |
| `src/components/layout/SessionSidebar.vue` | footer 加 "Provider Settings" 按钮 | M2-2 |
| `src/stores/sessionStore.ts` | `setActiveSession` 后触发 `providerStore.refreshForSession`（或 watch） | M2-1 |
| `src/stores/chatStore.ts` | `resetForSession` 不动（无 binding 状态） | — |
| `package.json` | 仅当引入 vitest 时改 | M2-1（可选） |

### 18.3 明确禁止修改

- 任何后端 Python 文件（`src/pi_agent_core_py/**/*.py`）
- `package.json` 的 dependencies（除非 user approve vitest）
- `package-lock.json`
- Core Runtime（`loop.py` 等）
- 现有 store 的 API（不破坏 chatStore 公开契约）
- Modal/EmptyState/ErrorBanner/LoadingSpinner 的 props（沿用现有）

---

## 19. M2-1～M2-4 实施顺序

```
M2-0 Frontend Integration Audit（本文档）
    ↓
    ✋ user review + 冻结本文档
    ↓
M2-1 Frontend API types + providerStore
    - src/api/providers.ts
    - src/types/providers.ts
    - src/stores/providerStore.ts（含 stale-response 防护）
    - App.vue onMounted 接入
    - sessionStore 切换 hook
    - 单元测试（若引入 vitest）
    ↓
    ✋ user review + 单元测试 pass
    ↓
M2-2 ProviderSettingsModal
    - ProviderCard.vue
    - ProviderSettingsModal.vue
    - SessionSidebar.vue footer 按钮
    - API Key write-only flow
    - 组件测试
    ↓
    ✋ user review
    ↓
M2-3 ProviderSelector
    - ProviderSelector.vue
    - ChatPanel.vue header 插入
    - active request disabled
    - 组件 + 集成测试
    ↓
    ✋ user review
    ↓
M2-4 Frontend Integration Validation
    - Session A/B 隔离
    - default binding restore
    - reload 恢复
    - Prompt/Regenerate provider 切换
    - frontend build clean
    - Playwright（主仓库）
    ↓
    ✋ M2 COMPLETE → M3 Unified Freeze
```

**每阶段约束**：
- 单一职责
- 原子 commit
- 完成后停止审核，不提前进入下一阶段
- 不修改本审计文档 §15 已冻结的决策（若发现新决策点，需 user approve 修订本文档）

---

## 20. 静态边界检查

### 20.1 依赖方向（M2 全程）

```
components/chat/ChatPanel.vue
    ↓ imports
components/providers/ProviderSelector.vue
    ↓ imports
stores/providerStore
    ↓ imports
api/providers.ts
    ↓ imports
api/client.ts (requestJson)
    ↓ HTTP
backend REST API (no SDK, no fetch in component)
```

### 20.2 禁止导入

| From | 禁止 import |
|---|---|
| 任何前端文件 | `openai` / `@anthropic-ai/sdk` / `langchain` 等 Provider SDK |
| `providerStore` | 任何 `components/**` |
| `sessionStore` | `providerStore`（store-to-store 禁止；用组件 watch） |
| `chatStore` | `providerStore`（同上） |
| 任何前端文件 | `pi_agent_core_py/**`（后端 Python） |

### 20.3 M2 不需要的依赖

- 无 `openai` npm package
- 无 `@anthropic-ai/sdk`
- 无 Provider endpoint 直连
- 无浏览器端 Key 长期状态

---

## 21. 审计覆盖度量（commit 时报告）

- **审计前端文件数**：约 55 个（11 api + 8 types + 5 stores + 27 components + 4 其它）
- **发现 API endpoint 数**：15 个（1 definitions + 7 credential + 5 profile + 2 binding）
- **待 user 审核决策点数**：10 个（§15.1～§15.10）
- **race 场景识别**：5 个（§7.2）
- **错误码映射**：18 个（§14.1）

---

## 22. 只读验证基线（commit 时执行）

| 检查 | 命令 | 结果 |
|---|---|---|
| Backend offline pytest | `pytest tests/ -m "not slow and not integration and not docker" --no-cov` | 2585 passed + 1 skipped + 14 deselected（预期，与 M1 baseline 一致） |
| Frontend build | `cd src/pi_agent_core_py/web/frontend && npm run build` | 142.91 KB JS / 40.15 KB CSS（M2-0 不改 frontend，应与 M1 baseline 一致） |
| Frontend typecheck | `npm run typecheck` | PASS |
| Frontend lint | `npm run lint` | PASS（max-warnings=0） |
| Ruff | `ruff check src tests scripts` | All checks passed |
| `git diff --check` | — | clean |
| `git status --short` | — | 只有新增的本文档 |
| Playwright E2E | 主仓库 `pi-py` 执行 | 本精简副本 CLAUDE.md 明确无 `tests/e2e/` |

---

## 23. 状态收口

**本文档状态**：M2-0 FRONTEND INTEGRATION AUDIT — DESIGN DRAFT

**待 user 操作**：
1. 审核 §15.1～§15.10 的 10 个决策点（每项给 ✅/❌/修订意见）
2. 审核 §11.3 多 Profile 推荐方案（方案 B）
3. 审核 §17 测试矩阵（是否同意引入 vitest）
4. 冻结本文档（user 给出 "M2-0 FROZEN" 指令）

**冻结后允许**：M2-1 启动（API types + providerStore 实施）

**禁止**（直到本文档冻结）：
- 任何 M2 production coding
- merge / tag / push

---

**M2-0 DESIGN DRAFT @ 2026-07-23**——等待 user 审核。

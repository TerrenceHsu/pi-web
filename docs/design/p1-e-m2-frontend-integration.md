# P1-E M2 — Frontend Integration Audit

> **状态**：✅ M2-0 FRONTEND INTEGRATION — DESIGN FROZEN (REVISION 2)
> **基线**：master `8b0fb13` — P1-E M1 Runtime ✅ COMPLETE / FROZEN
> **初稿 commit**：`62f1366`（2026-07-23）
> **修订 1 commit**：`068a548`（2026-07-23）
> **冻结 commit**：（本次提交，hash 由 git 在 commit 后生成；不在文档内自引用）
> **日期**：2026-07-23
> **前置**：[p1-e-m1-provider-runtime.md](p1-e-m1-provider-runtime.md) § 14
> **范围**：M2 前端集成纯审计；不写生产代码、不写测试、不进入 M2-1 编码
> **审核状态**：**✅ DESIGN FROZEN** —— 12 决策点全部 APPROVED（§15）；M2-1 ✅ APPROVED TO START

---

## 0. 阶段定位

本阶段是**纯审计和设计阶段**。本文档是唯一交付物。

**禁止事项**（整个 M2-0 期间）：
- 修改任何生产代码（前端 / 后端 / API / schema）
- 修改 `package.json` / lockfile（**例外**：M2-1 经 user 批准引入测试框架时；见 §15.12）
- 新增 npm 依赖（同上例外）
- 进入 M2-1 编码（**冻结前**）
- merge / tag / push

**本次提交**：文档冻结，commit message 为 `docs: freeze P1-E M2 frontend integration contract`。

### 0.1 变更历史

| Revision | Commit | 解决的问题 |
|---|---|---|
| 初稿 | `62f1366` | 首次审计输出（10 类问题未发现） |
| Revision 1 | `068a548` | 解决初稿 10 类阻塞：storage_mode 缺失 / canSendPrompt 破坏 Legacy / Store 依赖冲突 / default_model vs Binding.model_id / Modal 结构与方案 B 矛盾 / API Key 浏览器事实 / Toast 不存在 / 保存非原子 / 测试框架与 lockfile 冲突 / api_style 错值 + cascade 误断 |
| Revision 2（冻结） | `87d7a8e` | 解决 Revision 1 剩余 4 阻塞 + 术语统一 + 小修正 + 12 决策 APPROVED。详见下表。 |
| **M2-4 归档修正（事实记录）** | 本次归档 commit（C） | §6.1 `/api/provider-definitions` 响应类型由 envelope `{providers: [...]}` 修正为裸数组 `ProviderDefinitionView[]`。M2-1 实现已按裸数组；M2-4 验证阶段确认 backend `list[dict]` 契约。非产品决策变更。 |

### 0.2 Revision 2 相对 Revision 1（`068a548`）的变更

| # | 问题 | 本修订的修复 |
|---|---|---|
| 1 | Binding 加载状态未显式建模 | §8.1 新增 `bindingLoadState: "idle"\|"loading"\|"loaded"\|"error"`；§8.2 `canSendPrompt` 改用 state 而非 `bindingResolved`；§8.3 `refreshForSession` 显式重置 state |
| 2 | `canSendPrompt` 未接入真实发送路径 | §9.7 新增 "ChatInput 接线"；§18.2 添加 `ChatInput.vue` 最小扩展；§19 M2-3 任务加 "wire canSendPrompt"；§17.2 加相关测试 |
| 3 | Env Credential 的 `env_var_name` 不可恢复 | §10.2 + §10.4 明确：envVarName 局部输入默认空；placeholder 用 `masked_value`；空值不调 rotate；不得解析 `masked_value`；新建/切换到 env 时 envVarName 必填 |
| 4 | Credential 生命周期描述错误 | §10.6 删除"Credential 通过 Profile 删除或 cascade"；§10.7 新增"未使用 Credential 折叠区"；§15.2 冻结：Profile 删除 ≠ Credential 删除；引用计数为 0 才能删；二次确认；不自动删共享 |
| 5 | "原子提交"术语不准 | §10.3 + 全文统一改为"Credential + Profile 分阶段保存流程"；明确 3 独立阶段（Credential mutation / Profile mutation / 可选 Binding mutation）；保留部分成功补偿语义 |
| 6 | 小修正 | `profilesByProvider[provider_id]`（原误写 `gateway_id`）；Revision commit 在后续 freeze commit 中记录为 `068a548`（不在当前文档自引用 hash） |
| 7 | 决策状态 | §15 所有 12 决策转为 APPROVED 表，标注 user 审核结论 |

---

## 1. M2 产品目标

在现有 Vue 3 Web UI 中实现最简 Provider/Model 配置与 Session 级切换。

**M2 前端只展示**：GLM / Qwen / Kimi。**Anthropic 后端技术保留，前端不展示**（见 §11.7、§15.9）。

**M2 计划新增/扩展的能力**（实施阶段，非本审计阶段）：
- `providerStore`
- `ProviderSelector`（ChatPanel header 内）
- `ProviderSettingsModal`（`ProviderSection × N ProfileForm` 结构）
- Session Binding 加载与切换
- API Key 配置（write-only；含 `storage_mode` 选择）
- Model ID 配置（手动输入 + 后端静态建议）
- Provider/Profile 状态与安全错误展示

### 1.1 M1 已冻结的后端语义（M2 必须遵守）

| # | 语义 | 前端含义 |
|---|---|---|
| 1 | Session Binding 决定 Provider/Profile/model_id | Selector 显示当前 Binding |
| 2 | 请求开始时创建不可变 `RequestProviderSelection` | 请求运行中 Selector 禁用 |
| 3 | 运行中修改 Binding 只影响下一次 Prompt/Regenerate | UI 表达"下次生效"（决策点 §15.4） |
| 4 | 不主动 abort 当前请求 | Selector 禁用是 UX 选择，后端不强制 |
| 5 | Session 无 Binding → legacy client | null binding 合法，允许发 Prompt（§8.2） |
| 6 | Binding 配置无效 → 安全失败，禁止自动 fallback | 错误展示用固定 4 安全码 |
| 7 | Prompt + Regenerate 都经 `_execute_prompt` 唯一接入点 | 切换语义对两者一致 |
| 8 | Secret 只由后端 `CredentialService` 读取 | 前端永远不持有明文 Key（持久态） |
| 9 | 前端永远不能读回已保存 API Key | Key 输入是 write-only 字段 |
| 10 | AssistantMessage/Revision 已保存实际 provider/model/usage | 消息展示可读取（非执行路径） |
| 11 | `Profile.default_model` 是**创建/重绑**时的默认；`Binding.model_id` 是**当前 Session 实际执行**模型 | 修改 `default_model` 不影响已有 Binding（§9.2） |

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
8. API Key 如何做到 write-only？→ §8.3, §15.5, §16
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

**无测试框架**（无 vitest / jest / playwright 依赖；本精简副本 CLAUDE.md 明确无 `tests/e2e/`）。M2-1 经 user 批准后引入 Vitest（§15.12）。

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

**M2-1 计划新增**：`test: vitest run` 和 `test:watch: vitest`（§15.12）。

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
│   ├── common/              # 4 个通用 UI 原语
│   │   ├── Modal.vue                # 通用 Modal
│   │   ├── EmptyState.vue
│   │   ├── ErrorBanner.vue          # 可关闭的内联错误条
│   │   └── LoadingSpinner.vue
│   ├── layout/
│   │   ├── AppShell.vue
│   │   └── SessionSidebar.vue
│   ├── skills/              # Skills 管理（参考样例 1）
│   └── mcp/                 # MCP 管理（参考样例 2）
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

**关键事实**：**无 Toast / Notification 组件**。M2 不新增——所有错误走 `ErrorBanner` 或字段级红字（§14）。

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
      │                  ├─ <ErrorBanner>          ← 替代 Toast（§14）
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
sending: Ref<boolean>            // true while 202 pending → completed
streaming: Ref<boolean>          // true while streaming
currentRequestId: Ref<string | null>
activeSessionId: Ref<string | null>
```

### 4.3 sessionStore 关键 state

```typescript
sessions: Ref<SessionSummary[]>
activeSessionId: Ref<string | null>
loading: Ref<boolean>
error: Ref<string | null>
```

### 4.4 Store 协调模式（**冻结**）

**现有模式**：Store 之间**不直接 import**。协调发生在组件层（`App.vue` / `SessionSidebar.vue` 顺序调用多个 store action）。

**M2 `providerStore` 必须沿用此模式**——**禁止 cross-store import**：

```typescript
// ❌ 禁止：providerStore 内 import sessionStore
import { useSessionStore } from './sessionStore'

// ❌ 禁止：sessionStore 内 import providerStore
// ❌ 禁止：chatStore 内 import providerStore

// ✅ 正确：App.vue 用 watch 协调
// App.vue
watch(
  () => sessionStore.activeSessionId,
  (sessionId) => {
    if (sessionId) {
      void providerStore.refreshForSession(sessionId)
    } else {
      providerStore.clearSessionState()
    }
  },
  { immediate: true },
)
```

**理由**：
- 匹配现有架构（`App.vue` onMounted 已顺序调用多 store）
- 避免 Pinia 循环依赖
- `sessionStore.ts` **不需要任何修改**（降低 M2-1 改动面）

**providerStore 内只使用**：
- `bindingLoadToken`（内部 stale-response 防护）
- 调用方传入的 `sessionId` 参数
- 自己的 `bindingSessionId` state

**Settings Modal 跨 session 守护**：Modal 组件用 prop 捕获打开时的 `sessionId`，保存前检查该值是否仍等于 `sessionStore.activeSessionId`（由 Modal 组件直接读 sessionStore，不通过 providerStore）。

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
  5. chatStore.findActiveRequest(sid)
  6. chatStore.connectEvents()                     # WebSocket
  7. skillStore.loadSkills(); mcpStore.loadServers(); mcpStore.loadTools()
```

**M2 启动追加**：`providerStore.initialize()`——`loadDefinitions` + `loadProfiles` + `loadCredentials`（全局缓存，与 session 无关）。**不**在启动时加载 binding——binding 是 per-session，由 §4.4 的 `watch(activeSessionId)` 触发。

### 5.2 创建 Session（`SessionSidebar.vue:48-62`）

```
1. sessionStore.createNewSession()                # POST /api/sessions
2. chatStore.setActiveSession(s.id)
3. chatStore.resetForSession()
4. chatStore.loadMessages(s.id)
5. fileStore.loadFiles(s.id); fileStore.resetForSession()
```

**后端自动创建 default Binding**（`initialize_new_session_binding`），前端目前不知道。

**M2 追加**：`activeSessionId` 变化触发 `providerStore.refreshForSession(s.id)` → 内部调 `loadBinding(s.id)` 读自动创建的 default binding。

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

**M2 追加**：`activeSessionId` 变化由 `App.vue` `watch` 捕获 → `providerStore.refreshForSession(id)`（内部 token 自增，使旧的 `loadBinding` 响应失效；§7.3）。

### 5.4 删除 Session（`sessionStore.ts:69-81`）

```
1. DELETE /api/sessions/{id}
2. 从 sessions 数组移除
3. 若是当前 active：activeSessionId ← 第一条或 null
4. 若无 active：createNewSession()
```

**后端清理事实**（已核实）：
- `web_session_model_bindings` 表**无 FK 到 sessions 表**（`provider_config_store.py:188-207`）
- `delete_session` handler（`app.py:2570-2620`）**未显式删除 binding 行**
- 因此 session 删除后，binding 行可能成为孤儿数据

**M2 前端策略**：
- **不依赖** DB cascade——前端不知道也不关心后端是否清理
- active session 变化后，前端清空 `currentBinding` 并加载新 active session 的 binding
- 孤儿 binding 行属于**后端数据治理问题**，M2 不处理（不在 M2-1～M2-4 范围）

### 5.5 Prompt（`chatStore.ts:716-741`）

```
1. sending = streaming = true
2. POST /api/prompt/async → 202 + request_id
3. currentRequestId = request_id
4. WS 事件 message_start → message_update × N → message_end → request_end
5. pollRequestUntilTerminal → sending = streaming = false
```

### 5.6 Regenerate（`chatStore.ts:818-864`）

与 Prompt 共享 `sending/streaming/currentRequestId`——Selector 禁用条件相同。

---

## 6. 真实后端 API Contract 表

### 6.1 Provider Definitions

#### `GET /api/provider-definitions`

- Body：无
- 200 响应（已核实，源：`credentials_api.py:690`——`async def get_provider_definitions() -> list[dict]`，FastAPI 直接序列化裸数组）：

```json
[
  {"id": "anthropic", "display_name": "Anthropic",
   "api_style": "anthropic_compatible",
   "validation_supported": true,
   "supports_model_listing": true},
  {"id": "glm", "display_name": "Zhipu GLM (Anthropic-compatible)",
   "api_style": "anthropic_compatible",
   "validation_supported": false,
   "supports_model_listing": false},
  {"id": "qwen", "display_name": "Qwen",
   "api_style": "openai_compatible",
   "validation_supported": false,
   "supports_model_listing": false},
  {"id": "kimi", "display_name": "Kimi",
   "api_style": "openai_compatible",
   "validation_supported": false,
   "supports_model_listing": false}
]
```

> **M2-4 归档修正（2026-07-26）**：初稿与 Revision 1/2 误将响应描述为 envelope `{providers: [...]}`。真实 handler 返回 `list[dict]`——FastAPI 直接序列化为裸 JSON 数组。M2-1 实现已按真实裸数组（`api/providers.ts:34` + `types/providers.ts:172`）。本节事实记录修正，非产品决策变更。

**已核实的契约事实**：
- `api_style` 枚举值是 **`anthropic_compatible`** 和 **`openai_compatible`**（`registry.py:52-53`），**不是** `"anthropic"` / `"openai"`（初稿错误已修正）
- 响应**仅 5 字段**：`id` / `display_name` / `api_style` / `validation_supported` / `supports_model_listing`
- **响应类型**：`ProviderDefinitionView[]` 裸数组（**不是** envelope `{providers: [...]}`）
- **不返回** `key_prefix_hints`、`default_base_url`、`credential_validation_strategy`（serializer 安全投影）
- **返回 Anthropic**（M2 前端需过滤；见决策点 §15.9）
- 不读 Secret；不发网络

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

**CredentialCreateRequest**（已核实，`credentials_dto.py:154-267`）：

```json
{
  "label": "1-128 chars",
  "storage_mode": "keyring|session_only|env",
  "secret_value": "sk-...",           // keyring/session_only 必填；env 必无
  "env_var_name": "MY_VAR"             // env 必填；其它必无
}
```

**CredentialRotateRequest**（PUT secret，原地 CAS）：

```json
{"secret_value": "...", "env_var_name": "..."}  // 至少一个
```

**重要**：PUT secret **不改 `storage_mode`**——同模式原地 rotate。若要切换 `storage_mode`（如 keyring→env），必须新建 Credential + PATCH Profile.credential_id（§15.6）。

**CredentialView**（所有 endpoint 共用，**无明文**）：

```json
{
  "credential_id": "cred-...",
  "label": "...",
  "storage_mode": "keyring|session_only|env",
  "storage_status": "ready|needs_key|backend_unavailable",
  "masked_value": "sk-****5678",
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
- `masked_value` 格式：长度<8 → `"********"`；≥8 → 首 3 + `****` + 末 4；env → `"ENV[VAR_NAME]"`（`secrets/utils.py:30-62`）

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
  "is_default": false                            // 与 Profile 同一次 POST 提交
}
```

**ProviderProfileUpdateRequest**（至少一个字段；`provider_id` 不被接受 → 422）：

```json
{
  "name": "...",
  "credential_id": "...",     // **可以切换到另一个 credential_id**（含 storage_mode 切换）
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
    "model_id": "...",                       // 当前 Session 实际执行模型
    "source": "default|explicit",
    "created_at": 1735084800000,
    "updated_at": 1735084800000
  }
}
```

- 404：session 不存在

**关键语义**：`Binding.model_id` 可能与 `Profile.default_model` 不同——`Binding.model_id` 是 Session 创建/重绑时的快照，`Profile.default_model` 后续修改不影响已有 Binding（§9.2）。

#### `PUT /api/sessions/{sid}/model-binding`
- Body：`SessionModelBindingPutRequest`

```json
{"profile_id": "prof-...", "model_id": "..."}
```

- 200：返回完整 binding
- 409：`profile_disabled`（试图绑定 disabled profile）

### 6.5 关键 API 行为事实

| 问题 | 答案 | 源 |
|---|---|---|
| Credential API 返回明文 Key？ | **否**，仅 `masked_value` | `credentials_api.py:624-643` |
| 列表返回 `credential_id`？ | 是 | 同上 |
| PATCH Profile 空 `api_key`？ | 不适用——Profile API **不接受** `api_key` 字段；Key 走独立 Credential API | `provider_profiles_api.py:126-136` |
| Credential 更新是原地还是新建？ | **同 storage_mode 原地 CAS rotate**；切换 storage_mode 需新建 | `credentials_service.py:478-632` |
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
- 若存在 `is_default=true` 的 enabled Profile → 创建 `source="default"` binding 指向它，`model_id = Profile.default_model`
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

**方案：请求级 `bindingLoadToken` + 应用响应前校验 `bindingSessionId` + 显式 `bindingLoadState`**

```typescript
// providerStore.ts (草案——非生产代码)
let bindingLoadToken = 0

async function loadBinding(sessionId: string) {
  const token = ++bindingLoadToken
  try {
    const resp = await api.getSessionBinding(sessionId)
    // 应用响应前校验：token 未被取代 + sessionId 未变
    if (token !== bindingLoadToken) return
    if (sessionId !== bindingSessionId.value) return
    currentBinding.value = resp.binding
    bindingLoadState.value = "loaded"           // 只有成功才进 loaded
    bindingLoadError.value = null
  } catch (err) {
    if (token !== bindingLoadToken) return
    if (sessionId !== bindingSessionId.value) return
    currentBinding.value = null
    bindingLoadState.value = "error"
    bindingLoadError.value = safeError(err)
  }
}

function refreshForSession(sessionId: string) {
  // 原子重置（§8.3 冻结顺序）
  currentBinding.value = null
  bindingSessionId.value = sessionId
  bindingLoadState.value = "loading"
  bindingLoadError.value = null
  return loadBinding(sessionId)
}
```

**触发方**：`App.vue` `watch(activeSessionId)`——见 §4.4。**providerStore 不 import sessionStore**。

**Settings Modal 跨 session 守护**：
- Modal 组件 props 接收 `sessionId`（捕获打开时的值）
- 保存前 Modal 自己读 `sessionStore.activeSessionId` 校验（组件层跨 store，允许）
- 不一致时 Modal 内 `ErrorBanner` 提示 "Session has changed" 并自动关闭

**默认 binding race**：`canSendPrompt` getter 要求 `bindingLoadState === "loaded"`（§8.2）。只有 GET binding 成功后才允许发送 Prompt。

---

## 8. providerStore 最小契约（草案）

### 8.1 State（含 `bindingLoadState` 显式状态机）

```typescript
// 全局缓存（与 session 无关）
definitions: Ref<ProviderDefinitionView[]>
profiles: Ref<ProviderProfileView[]>
credentials: Ref<CredentialView[]>

// Per-session binding——三字段必须同步变更（§8.3 refreshForSession）
currentBinding: Ref<SessionBindingView | null>
bindingSessionId: Ref<string | null>
bindingLoadState: Ref<"idle" | "loading" | "loaded" | "error">

// 全局加载 flag（分维度）
definitionsLoading: Ref<boolean>
profilesLoading: Ref<boolean>
credentialsLoading: Ref<boolean>

// 保存 flag（分维度）
savingCredential: Ref<boolean>
savingProfile: Ref<boolean>
savingBinding: Ref<boolean>

// 错误（分维度）
loadError: Ref<string | null>                       // 定义/Profile/Credential 加载错误
bindingLoadError: Ref<string | null>                // binding 加载错误（对应 state=error）
mutationError: Ref<string | null>

// 初始化标志
initialized: Ref<boolean>
```

**`bindingLoadState` 状态机**（冻结）：

```
        ┌──────────────────────────────────────────────────┐
        │                                                  │
        ▼                                                  │
┌───── idle ─────┐  refreshForSession(sid)   ┌──── loading ────┐
│  初始状态       │ ─────────────────────────▶ │ currentBinding=null│
│  binding=null  │                            │ bindingSessionId=sid│
│  sessionId=null│                            │ state="loading"    │
│  state="idle"  │                            │ token++             │
└────────────────┘                            └─────┬──────────────┘
                                                    │
                              GET /api/sessions/{sid}/model-binding
                                                    │
                                  ┌─────────────────┴─────────────────┐
                                  │                                   │
                              成功 200                             失败（非 404）
                                  │                                   │
                                  ▼                                   ▼
                          ┌─── loaded ────┐                   ┌─── error ────┐
                          │ state="loaded"│                   │ state="error"│
                          │ binding=resp  │                   │ bindingLoadError
                          │   .binding    │                   │ ChatPanel ErrorBanner
                          └───────────────┘                   └──────────────┘
```

**不变量**：
- 只能从 `loading` 进入 `loaded`（必须成功 GET 才算 loaded）
- `loaded` 时 `currentBinding` 可能为 `null`（合法——legacy 路径）或 `SessionBindingView`
- `error` 时 `currentBinding` 保持 `null`；`canSendPrompt` 返回 false；显示 `bindingLoadError`
- 切换 session 永远从 `loaded/error → loading`（不回到 idle，除非 `clearSessionState`）

### 8.2 Getters（`canSendPrompt` 基于 `bindingLoadState`）

```typescript
// 只展示 GLM/Qwen/Kimi（过滤 Anthropic）
visibleDefinitions: ComputedRef<ProviderDefinitionView[]>

// enabled 且 status==="ready"（可执行）——Selector 候选
usableProfiles: ComputedRef<ProviderProfileView[]>

// 所有 Profile（含 disabled/needs_key）——Settings Modal 展示
allProfilesGroupedByProvider: ComputedRef<Record<string, ProviderProfileView[]>>

// profilesByProvider[provider_id]（修正：原 gateway_id 笔误）
profilesByProvider: ComputedRef<Record<provider_id, ProviderProfileView[]>>

// 未使用 Credential（无 Profile 引用）——Settings Modal 折叠区展示
unusedCredentials: ComputedRef<CredentialView[]>

// 当前 binding 投影
selectedProfile: ComputedRef<ProviderProfileView | null>
selectedProvider: ComputedRef<ProviderDefinitionView | null>
selectedModel: ComputedRef<string | null>           // = currentBinding?.model_id

// 当前 Profile 状态
currentProfileStatus: ComputedRef<string | null>

// canSendPrompt 基于 bindingLoadState（Revision 2 修复）：
//   state != "loaded" → false（避免 default binding race）
//   loaded + binding=null → true（Legacy client 路径）
//   loaded + Profile ready → true
//   loaded + Profile 失效 → false（引导配置）
//   state="error" → false（显示 bindingLoadError）
canSendPrompt: ComputedRef<boolean> = computed(() => {
  if (bindingLoadState.value !== "loaded") return false
  if (currentBinding.value === null) return true        // legacy path
  return selectedProfile.value?.status === "ready"
})
```

**决策点**：是否拆 `loadError` 为 `definitionsLoadError / profilesLoadError / ...`——建议 M2-1 保持上述维度，不过度拆。

### 8.3 Actions

```typescript
// 初始化（App.vue onMounted 调用一次）
initialize(): Promise<void>

// 独立加载
loadDefinitions(): Promise<void>
loadCredentials(): Promise<void>
loadProfiles(): Promise<void>

// Per-session（由 App.vue watch 触发，不读 sessionStore）
// refreshForSession 必须按以下顺序原子重置（Revision 2 冻结）：
//   1. currentBinding.value = null
//   2. bindingSessionId.value = sessionId
//   3. bindingLoadState.value = "loading"
//   4. bindingLoadToken++（使旧的 in-flight 响应失效）
//   5. 清 bindingLoadError
//   6. await GET /api/sessions/{sessionId}/model-binding
//   7. token 校验 + sessionId 校验通过 →
//        成功：currentBinding = resp.binding; state = "loaded"
//        失败：bindingLoadError = safe msg; state = "error"
refreshForSession(sessionId: string): Promise<void>

// Per-session 直接加载（与 refreshForSession 共用 token；用于首次加载）
loadBinding(sessionId: string): Promise<void>

// 完全清空（登出 / 无 active session）
//   currentBinding = null; bindingSessionId = null;
//   bindingLoadState = "idle"; bindingLoadError = null
clearSessionState(): void

// Mutations（参数草案；实际签名 M2-1 决定）
// storage_mode 切换语义见 §15.6、§15.11；env_var_name 不可恢复见 §10.2
createCredential(payload: {label, storage_mode, secret_value?, env_var_name?}): Promise<string>
rotateCredentialSecret(credentialId: string, payload: {secret_value?, env_var_name?}): Promise<void>
deleteCredential(credentialId: string): Promise<void>     // 仅未使用；§10.7
createProfile(payload: {name, provider_id, credential_id, default_model, enabled?, is_default?}): Promise<string>
updateProfile(profileId: string, patch: {...}): Promise<void>
deleteProfile(profileId: string): Promise<void>
setSessionBinding(sessionId: string, profileId: string, modelId: string): Promise<void>

// 错误管理
resetError(): void
resetBindingLoadError(): void
```

### 8.4 安全不变量（冻结）

| 规则 | 实施 |
|---|---|
| Store 不保存 API Key 明文（持久态） | Key 只在 `ProviderSettingsModal` 局部 `ref`；见 §16 |
| Store 不持久化到浏览器存储 | 现有架构 zero localStorage/sessionStorage；M2 保持 |
| `masked_value / credential_id / profile_id / provider_id / model_id` 可入 state | 非敏感 |
| Key 不进 Pinia devtools | Key 不进 state → 不进 devtools |
| Key 不进 `console.log` | lint 规则 + code review |
| Key 不进 error 对象 | mutation error 来自后端固定消息，不含 Key |

**Key 短暂存在的允许位置**（见 §16 详述）：
- Modal 组件局部 `ref`
- `<input type="password">.value`
- 同源 HTTPS mutation request body

### 8.5 数据归属

| 数据 | 归属 | 理由 |
|---|---|---|
| `definitions / profiles / credentials` | 全局（providerStore） | 与 session 无关 |
| `currentBinding / bindingSessionId` | Per-session（providerStore） | 切换时重载 |
| `apiKey` 输入 | **Modal 组件局部** | write-only；不入 store |
| `modelInput / profileName / storageMode` 草稿 | **Modal 组件局部** | 未保存前不属于业务 state |
| Modal open 状态 | **SessionSidebar 局部**（与 Skills/MCP 一致） | UI 状态 |

---

## 9. ProviderSelector UX 草案

### 9.1 放置位置（决策点 §15.10）

**推荐**：`ChatPanel.vue` header，紧邻 session title + status pill。

### 9.2 Selector 切换语义（修复初稿混淆）

**关键区分**：
- `Profile.default_model`：Profile 的默认模型；**创建 Binding 或选新 Profile 时使用**
- `Binding.model_id`：当前 Session 实际执行模型；**修改 `Profile.default_model` 不影响已有 Binding**

**Selector 切换 Profile 的正确流程**：

```typescript
// 用户在 Selector 选了 newProfile
async function onSelectorSelect(newProfile: ProfileView) {
  // PUT binding 用 newProfile.default_model（不是当前 session 的旧 model_id）
  await providerStore.setSessionBinding(
    sessionId,
    newProfile.id,
    newProfile.default_model,    // 来自 Profile，不是旧 Binding
  )
}
```

**Selector 当前展示**：`currentBinding.model_id`（可能与 `Profile.default_model` 不同——若用户曾在 Settings 修改 default_model 但未"应用到当前 Session"）。

**Settings 修改 `Profile.default_model`**：
- **不**自动 PUT 当前 Session Binding
- 只影响：之后新建的 Session（经 §6.6 default binding 初始化）、用户手动重绑当前 Session、用户点"应用到当前 Session"按钮

### 9.3 Selector item 真实身份

**Selector 项 = Profile**（不是 Provider、不是 Provider+Model 两级）。

**每个 item 显示**：
```
[Provider icon] Profile.name
                provider_display_name · currentBinding.model_id（已绑定时）
                                  或 Profile.default_model（未绑定时，作为预览）
```

### 9.4 各状态展示

| 场景 | Selector 显示 |
|---|---|
| Session 加载中（`bindingLoading`） | skeleton 灰条 |
| 无 Binding（`currentBinding === null`） | "默认模型"（决策点 §15.3） |
| Profile `status === "ready"` | 正常显示 |
| Profile `status === "disabled"` | 灰显 + 禁用切换 + tooltip |
| Profile `status === "needs_credential" / "needs_key"` | 红点 + 引导打开 Settings |
| **当前 binding 指向失效 Profile** | **仍显示该 Profile 作为当前项（disabled）**；不能误显示为"无 Binding" |
| 无任何 enabled Profile | "未配置 Provider" + 按钮打开 Settings |
| 请求运行中 | 整个 Selector disabled + tooltip |
| 长 `model_id` | CSS `text-overflow: ellipsis` + tooltip 完整值 |

### 9.5 运行中切换语义（决策点 §15.4）

**推荐**：运行中 Selector **disabled**。

### 9.6 Anthropic 隐藏（决策点 §15.9）

**推荐**：`visibleDefinitions` getter 过滤掉 `id === "anthropic"`。

**Profile 列表**：若 DB 中存在 Anthropic Profile（legacy），M2 也从 `usableProfiles` 排除（但保留在 `profiles` 数组中——不删数据；Settings 也不展示）。

### 9.7 ChatInput / Prompt submit 接线（**Revision 2 新增**）

**事实**（已核实 `ChatInput.vue:1-46`）：组件**没有** `disabled` prop；内部 `canSend` computed 只检查 `props.sending + text/pendingAttachments`。因此 `providerStore.canSendPrompt` 必须显式接线到发送路径，不能只创建 getter。

**M2-3 接线方案**（按优先级）：

1. **首选——最小扩展 ChatInput.vue**（§18.2 允许修改文件）：
   - 新增 prop：`providerReady?: boolean`（默认 `true`，向后兼容）
   - `canSend` computed 增加判断：`if (!props.providerReady) return false`
   - `submit()` 函数不变（已用 `canSend.value` 守护）
   - 发送按钮 `:disabled="!canSend"`（已存在）自动生效
   - **不需要**新增 emit / slot / 其它破坏性改动

2. **调用方 ChatPanel.vue**（M2-3 允许修改，§18.2）：
   ```vue
   <ChatInput
     :sending="chatStore.sending"
     :provider-ready="providerStore.canSendPrompt"
     ...
   />
   ```

3. **Regenerate 路径**（`MessageBubble.vue` regenerate button）：
   - 当前用模块级 `_regenerateInFlight` 防双击
   - M2-3 追加：按钮 disabled 也基于 `providerStore.canSendPrompt`
   - 不允许在 `bindingLoadState !== "loaded"` 时启动 regenerate

**组件测试**（§17.2 扩展）：
- `providerReady=false` 时 send 按钮 disabled
- `providerReady=true + text="hi"` 时按钮启用
- `providerReady=false + text="hi"` 时 `submit()` 不 emit

**禁止**：
- 不在 `chatStore` 内 import `providerStore`（§4.4 + §20.1）
- 不在 `ChatInput.vue` 内直接 import `providerStore`——通过 prop 传入

---

## 10. ProviderSettingsModal UX 草案（结构重构）

### 10.1 视觉布局（`ProviderSection × N ProfileForm` 结构）

**修复初稿矛盾**：方案 B（允许多 Profile per Provider）要求 Modal 能展示同 Provider 的多个 Profile。改为分区 + 列表结构：

```
┌─ Modal (width: min(900px, 92vw)) ─────────────────────────┐
│ #header: "Provider Settings"                          [×] │
├─────────────────────────────────────────────────────────────┤
│ <ErrorBanner v-if="mutationError" :message="..." />         │
│ <LoadingSpinner v-if="profilesLoading" />                   │
│                                                              │
│ ┌─ ProviderSection: GLM ────────────────────────────────┐   │
│ │  ┌─ ProfileForm #1 (profile_id=prof-abc) ───────────┐ │   │
│ │  │  Profile name: [Work key___________]              │ │   │
│ │  │  Model ID:    [glm-4.5-flash______] [suggest ▾]   │ │   │
│ │  │  Credential:  sk-****5678 (keyring) [Rotate]       │ │   │
│ │  │  Storage:     (•) keyring  ( ) session_only  ( ) env │ │   │
│ │  │  API Key:     [••••••••] placeholder "Enter new key" │ │   │
│ │  │  Env var:     (hidden unless storage=env)           │ │   │
│ │  │  ☐ Set as default profile                          │ │   │
│ │  │  Status:      ready                                │ │   │
│ │  │  [Save configuration] [Apply to current session]   │ │   │
│ │  │  [Delete profile]                                  │ │   │
│ │  └────────────────────────────────────────────────────┘ │ │   │
│ │  ┌─ ProfileForm #2 (profile_id=prof-def) ───────────┐ │   │
│ │  │  (同上——同 Provider 第二个 Profile)                │ │   │
│ │  └────────────────────────────────────────────────────┘ │ │   │
│ │  [+ Add GLM profile]                                   │   │
│ └────────────────────────────────────────────────────────┘   │
│ ┌─ ProviderSection: Qwen ───────────────────────────────┐   │
│ │  (同上结构)                                             │   │
│ └────────────────────────────────────────────────────────┘   │
│ ┌─ ProviderSection: Kimi ───────────────────────────────┐   │
│ │  (同上结构)                                             │   │
│ └────────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ #footer: [Close]                                            │
└─────────────────────────────────────────────────────────────┘
```

**组件树**：
```
ProviderSettingsModal
├── ErrorBanner
├── LoadingSpinner
└── ProviderSection × 3 (GLM, Qwen, Kimi——硬编码这三个)
    ├── ProfileForm × N (来自 providerStore.profilesByProvider[provider_id])
    │   ├── name / model_id / storage_mode / api_key / env_var_name / is_default
    │   ├── [Save configuration]   ← Credential + Profile 分阶段保存（§10.3）
    │   ├── [Apply to current session]  ← 独立 PUT Binding
    │   └── [Delete profile]       ← DELETE；409 时 ErrorBanner
    └── [+ Add {Provider} profile]  ← 空白 ProfileForm

UnusedCredentialsSection（折叠区，§10.7）
├── Credential × N（引用计数 = 0）
└── 每个：masked_value + storage_mode + [Delete] (二次确认)
```

**展示规则**：
- ProviderSection 展示该 Provider 的**所有** Profile（含 disabled、needs_key）——透明
- Selector（§9）只展示 `status === "ready"` 的 Profile——可执行
- Anthropic Section **不展示**（§15.9）

### 10.2 每个 ProfileForm 字段（**Revision 2：env_var_name 不可恢复**）

**已核实**（`credentials_api.py:624-643`）：`CredentialView` serializer **不返回** `env_var_name`，只返回 `masked_value`（env 模式格式为 `"ENV[VAR_NAME]"`）。前端**不能**从 API 读回原 `env_var_name`，用户必须重新输入。

| 字段 | 来源 | 编辑 | 说明 |
|---|---|---|---|
| Provider name | ProviderSection header（硬编码） | 不可改 | GLM / Qwen / Kimi |
| Profile name | Profile.name | 文本输入 | 必填 1-128 字符 |
| Model ID | Profile.default_model | 文本输入 + `<datalist>` | 必填非空 |
| Credential（只读显示） | `masked_value` + `storage_mode` | 不可改 | 显示当前状态 |
| Storage（单选） | `storage_mode`（决策点 §15.11） | radio | keyring（默认）/ session_only / env |
| API Key（仅 keyring/session_only） | 空（write-only） | `<input type="password">` | placeholder `masked_value` 或 "Enter new key" |
| Env var name（仅 env） | **局部输入，默认空** | 文本输入 | placeholder `masked_value`（如 `"ENV[MY_KEY]"`）；**不得解析** `masked_value` 提取变量名 |

**env 模式交互规则**（冻结）：
- 已有 env Credential：`envVarName` 局部输入**默认空**；placeholder = `masked_value`
- 用户不填写 → 不调 Credential mutation（保留原 env var）
- 用户填写新 env var → PUT secret（同 storage_mode）rotate
- 新建 env Credential 或从其它 storage_mode 切换到 env → `envVarName` **必填**，否则按钮 disabled
- **禁止**：前端解析 `masked_value` 字符串提取 `VAR_NAME`—— masked_value 仅为展示，格式可能变化

### 10.3 分阶段保存流程（**Revision 2：术语改为"分阶段"**）

**修复初稿"原子提交"术语错误**：实际是 3 个独立 HTTP 请求，分阶段提交，保留部分成功补偿。

**3 个独立阶段**：

```
阶段 A: Credential mutation（条件性）
阶段 B: Profile mutation（POST 或 PATCH，含 is_default）
阶段 C（可选，独立动作 [Apply to current session]）: Session Binding mutation（PUT）
```

**动作 1：[Save configuration]**——阶段 A + 阶段 B

```
1. capture openedSessionId = props.sessionId（Modal 打开时的值）
2. 校验 openedSessionId === sessionStore.activeSessionId
   - 不一致 → ErrorBanner "Session has changed"；关闭 Modal；终止
3. 读 form 局部状态（apiKey / modelInput / profileName / storageMode / isDefault / envVarName）

阶段 A——Credential mutation（条件性）：
   a. 若 storage_mode === keyring/session_only:
      - apiKey 非空 → mutation 必要
        * 已有 credential_id 且 storage_mode 未变 → PUT /credentials/{id}/secret (原地 rotate)
        * 无 credential_id 或切换了 storage_mode  → POST /credentials (新建) → 取新 credential_id
      - apiKey 空 → 跳过（不删旧 Key）
   b. 若 storage_mode === env:
      - envVarName 非空 → mutation 必要
        * 已有 credential_id 且 storage_mode === env → PUT /credentials/{id}/secret (原地 rotate)
        * 无 credential_id 或切换了 storage_mode       → POST /credentials (新建) → 取新 credential_id
      - envVarName 空 + 已有 credential_id (storage_mode === env) → 跳过（不调 rotate；保留原 env var）
      - envVarName 空 + 新建或切换到 env               → 按钮 disabled（必填校验）
   c. 保留阶段 A 返回的 credential_id 到 ProfileForm 局部状态（用于阶段 B + 失败重试）

阶段 B——Profile mutation（POST 或 PATCH，含 is_default）：
   - 若 profile_id 存在 → PATCH /provider-profiles/{id}
     body: {name, default_model, credential_id, is_default, enabled}（不含 provider_id——immutable）
   - 若 profile_id 不存在 → POST /provider-profiles
     body: {name, provider_id, credential_id, default_model, enabled, is_default}

成功：
   - 清 apiKey.value = "" + envVarName.value = ""
   - 刷新 credentials / profiles（loadCredentials + loadProfiles）
   - 按钮短暂显示 "Saved"（非 Toast）
   - 保留 ProfileForm 显示（用户可继续编辑或 Apply）

阶段性失败补偿（保留）：
   - 阶段 A 成功但阶段 B 失败：
     * 保留返回的 credential_id 到 ProfileForm 局部状态
     * ErrorBanner "Credential saved, but profile failed: {safe error}"
     * 刷新 credentials（侧栏看到新 credential，可在 §10.7 未使用区看到）
     * 用户重试时阶段 A **跳过**（用保留的 credential_id）；只重试阶段 B
   - 阶段 B 中 is_default 字段失败：不可能单独失败（is_default 是 PATCH 同一次提交的一部分）
```

**动作 2：[Apply to current session]**——阶段 C（独立）

```
1. 校验 openedSessionId === sessionStore.activeSessionId
2. 校验 profile_id 存在且 status === "ready"
3. PUT /api/sessions/{openedSessionId}/model-binding
   body: {profile_id, model_id: Profile.default_model}    // §9.2 语义
4. 成功：
   - 刷新 currentBinding（refreshForSession(openedSessionId) 触发 loadBinding）
   - 按钮短暂显示 "Applied"
5. 失败：
   - **不**回滚阶段 A/B（Profile 已独立保存）
   - ErrorBanner "Configuration saved, but current session binding failed: {safe error}"
```

### 10.4 API Key / Env var write-only 规则（**Revision 2：含 env 清空**）

**冻结规则**（决策点 §15.5）：

| 事件 | `apiKey.value` | `envVarName.value` |
|---|---|---|
| Modal 打开 | `""`（永远空） | `""`（**默认空**——env 不可恢复，见 §10.2） |
| Placeholder | `masked_value` 或 "Enter new key" | `masked_value`（如 `"ENV[MY_KEY]"`）或 "Enter env var name" |
| 用户不填写 Key 直接 Save（keyring/session_only） | 不调 Credential mutation | 不适用 |
| 用户不填写 env var 直接 Save（已有 env cred） | 不适用 | 不调 rotate；保留原 env var |
| 用户填写新 Key/envVar 并 Save 成功 | `""` 立即清空 | `""` 立即清空 |
| Mutation 失败（Modal 仍开） | **保留** `apiKey.value`（方便重试） | **保留** `envVarName.value` |
| Modal 关闭（任何方式） | `""` | `""` |
| Modal unmount | `""`（`watch(open)` 触发） | `""` |
| 切 Session（`openedSessionId !== activeSessionId`） | `""`；关闭 Modal | `""`；关闭 Modal |

**关键约束**：
- `envVarName.value` 默认空——**不得**用 `masked_value` 解析回填
- 新建/切换到 env 时 `envVarName` 必填，否则 [Save configuration] disabled
- 已有 env Credential 且用户不改 env var → 跳过阶段 A（§10.3）

### 10.5 Model ID 输入策略

- 输入框 + `<datalist>`（来自 `/api/provider-profiles/{id}/models`）
- Qwen/Kimi `models=[]` → datalist 空，不阻止保存
- 不硬编码前端建议列表
- 必填非空（后端 `default_model` 必填）

### 10.6 明确排除的高级功能

- Custom Base URL
- Custom Provider
- 远程模型搜索
- 远程 Key validation（后端有 `/validate` endpoint，M2 不调用）
- Provider health / cost dashboard
- 复杂 Profile 表格
- Profile 排序
- import/export credentials
- 自动 fallback
- Anthropic UI
- reasoning UI
- Toast / Notification 系统（用 ErrorBanner 替代）
- **Credential 自动删除**（Profile 删除不联动删 Credential；§10.7）
- **Credential 共享自动去重**（同 Key 不同 Profile 保留独立 Credential）

### 10.7 未使用 Credential 折叠区（**Revision 2 新增**）

**事实**：
- `DELETE /api/provider-profiles/{id}` 不联动删 Credential
- 跨 `storage_mode` 切换会 POST 新 Credential；旧 Credential 保留
- 因此 DB 可能积累无 Profile 引用的 Credential（"未使用"）

**UI 结构**（Modal 底部折叠区）：

```
▼ Unused credentials (3)
    [sk-****5678]  keyring     [Delete]
    [ENV[MY_KEY]]  env         [Delete]
    [sk-****abcd]  session_only [Delete]
▲ Collapse
```

**删除规则**（冻结，决策点 §15.2）：
- 只展示 `providerStore.unusedCredentials`（引用计数 = 0 的 Credential；getter 见 §8.2）
- 删除前必须**二次确认**（`window.confirm` 或自定义 ConfirmDialog——M2 用 `window.confirm` 复用现有模式）
- 删除调 `DELETE /api/credentials/{id}`；失败时 ErrorBanner 显示安全错误
- **禁止**：自动删除共享 Credential（即使看起来"重复"）
- **禁止**：在 ProfileForm 内提供 Credential 删除入口（避免误删正在使用的 Credential）

**未使用区不展示**：
- Anthropic 关联的 Credential（前端不展示 Anthropic；即使 `provider_hint === "anthropic"` 也不在此区展示——但**允许删除**避免数据积累；通过 `unusedCredentials` getter 包含所有引用计数 = 0 的 Credential，但 UI 折叠区可按 `provider_hint` 过滤）

**决策**：M2-0 冻结为"折叠区展示所有未使用 Credential（含 legacy Anthropic 关联）"——透明，让用户决定。
- reasoning UI
- **Toast / Notification 系统**（用 ErrorBanner 替代）

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

#### 方案 B：Selector 列出所有 enabled Profiles；Settings Modal 用 `ProviderSection × N ProfileForm`（**修订**）
- **优点**：不丢失后端能力；透明；Modal 结构能容纳多 Profile
- **风险**：UI 项目可能变多
- **缓解**：95% 用户每 Provider 只有 1 Profile

#### 方案 C：Provider 一级 + Profile 二级（两级 dropdown）
- **优点**：结构清楚
- **风险**：UI 和 state 复杂度更高；违反"最简"

### 11.3 推荐

**方案 B（受限版）**——Modal 结构已在 §10.1 重构为 `ProviderSection × N ProfileForm`。

**M2-0 推荐约束**：
- M2 允许创建同 Provider 第二个 Profile（不禁止）
- M2 允许删除 Profile（409 时 UI 显示"被 Session 使用，无法删除"）
- 不实现 Profile 排序、批量操作
- Selector item 按 Provider 分组 + Profile.name 区分
- **同 Provider 同名 Profile 后端允许**，但前端 Settings 应在表单层提示"名称与现有 Profile 重复，建议区分"

---

## 12. Session 切换 stale-response 方案

见 §7.3（请求级 `bindingLoadToken` + 应用前校验 `bindingSessionId`；**App.vue watch 协调**）。

### 12.1 各 race 的具体防护

| Race | 防护 |
|---|---|
| Binding API pending → 切 session | `bindingLoadToken` + 应用前校验 `sessionId === bindingSessionId` |
| Settings Modal 开着 → 切 session | Modal `watch(sessionStore.activeSessionId)` 自动关闭 + 提示 |
| 保存中切 session | Modal 保存前 capture `openedSessionId`；校验一致才执行 |
| 默认 binding race（新建 session） | `canSendPrompt` 要求 `bindingLoadState === "loaded"`（§8.2） |
| 快速切换多个 session | `bindingLoadToken` 自增，旧响应自动失效 |

### 12.2 WS 事件与 binding 的交互

- WS 事件不含 binding 变更通知（后端无此事件）
- Binding 变更只能由用户主动 API 调用触发
- **无需在 `chatStore.handleEvent` 中处理 binding 事件**——M2 不增加 WS 事件类型

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

---

## 14. 错误映射表（移除所有 Toast 引用）

### 14.1 后端错误码 → 前端展示（**全部走 ErrorBanner 或字段红字**）

| HTTP | code | M2 展示位置 | 用户文案（草案） |
|---|---|---|---|
| 400 | `missing_ui_header` | ChatPanel ErrorBanner | "Client header missing—reload page" |
| 403 | `invalid_origin` | ChatPanel ErrorBanner | "Origin not allowed" |
| 404 | `credential_not_found` | Modal ErrorBanner | "Credential no longer exists—reload" |
| 404 | `profile_not_found` | Modal ErrorBanner | "Profile no longer exists—reload" |
| 404 | `session_not_found` | ChatPanel ErrorBanner | "Session no longer exists" |
| 409 | `credential_conflict` | Modal form field（label 下） | "Credential label already in use" |
| 409 | `profile_in_use` | Modal ErrorBanner | "Profile is in use—unbind sessions first" |
| 409 | `profile_disabled` | Modal ErrorBanner | "Profile is disabled—enable first" |
| 413 | `request_body_too_large` | Modal form field | "Input too long" |
| 422 | `request_validation_failed` | Modal form field | Pydantic 错误细节（过滤后） |
| 500 | `credential_internal_error` | Modal ErrorBanner | "Server error—try again" |
| 503 | `credential_backend_unavailable` | Modal ErrorBanner | "Keyring unavailable—contact admin" |
| 503 | `provider_config_unavailable` | ChatPanel ErrorBanner | "Provider config not initialized" |
| 500 | `provider_profile_unavailable`（Prompt 执行） | ChatPanel ErrorBanner | "Selected provider profile is unavailable." |
| 500 | `provider_profile_disabled`（执行） | 同上 | "Selected provider profile is disabled." |
| 500 | `provider_credential_unavailable`（执行） | 同上 | "Selected provider credential is unavailable." |
| 500 | `provider_initialization_failed`（执行） | 同上 | "Selected provider could not be initialized." |
| — | network failure | ChatPanel ErrorBanner | "Network error—check connection" |
| — | unknown 5xx | ChatPanel ErrorBanner | "Unexpected error—try again" |

### 14.2 不可显示的信息（安全规则）

- `credential_id` 值
- `secret_ref`
- API Key 任何形式
- Base URL
- 底层 exception 类型 / stack trace
- 完整 response body

### 14.3 错误分类与展示组件

| 类型 | UI 位置 | 组件 |
|---|---|---|
| 表单字段错误 | 字段下方红字 | ProfileForm 内 `<span class="error">` |
| Modal 级错误 | 顶部 ErrorBanner | `<ErrorBanner>` |
| Selector 状态错误 | 红点 + tooltip | ProviderSelector 内 |
| Prompt 执行错误 | ChatPanel ErrorBanner | `<ErrorBanner>` |
| 全局/致命错误 | ChatPanel ErrorBanner | `<ErrorBanner>` |
| 保存成功反馈 | 按钮短暂 "Saved" | 局部 `<span>` 状态（**非 Toast**） |

---

## 15. 决策表（✅ 12 项 APPROVED——user 审核已完成）

> ** Revision 2 状态**：所有 12 决策已 user-approve；本文档冻结为 M2 实施契约。

### 15.1 多 Profile UI

| 项 | 值 |
|---|---|
| **决策** | ✅ **方案 B 受限版** |
| **含义** | Selector 列出所有 enabled Profiles；Modal 用 `ProviderSection × N ProfileForm`（§10.1） |
| **允许** | 创建同 Provider 第二个 Profile；允许删除 Profile（409 时显示） |
| **不实现** | Profile 排序、批量操作 |

### 15.2 Settings Modal 操作范围

| 项 | 值 |
|---|---|
| **决策** | ✅ **Profile CRUD + 未使用 Credential 清理** |
| **Profile** | 创建 / 编辑 / 删除（被 Session 引用时 409 ErrorBanner） |
| **Credential** | 通过 ProfileForm 间接管理（创建/rotate）；独立删除入口在 §10.7 未使用区 |
| **不实现** | Credential 独立创建（必须经 Profile）；Credential 共享去重；cascade 自动删 |

### 15.3 无 Binding 显示文案

| 项 | 值 |
|---|---|
| **决策** | ✅ **"默认模型"** + tooltip "当前 Session 未绑定配置，使用应用默认模型" |

### 15.4 active request 期间 Selector 行为

| 项 | 值 |
|---|---|
| **决策** | ✅ **完全 disabled** |
| **触发** | `chatStore.sending \|\| streaming \|\| currentRequestId`（§13） |

### 15.5 API Key 保存失败时是否保留输入

| 项 | 值 |
|---|---|
| **决策** | ✅ **仅在 Modal 仍打开期间保留** |
| **清空时机** | Modal 关闭 / 切 Session / 保存成功 |
| **env_var_name** | 同规则（§10.4） |

### 15.6 Credential 更新策略

| 项 | 值 |
|---|---|
| **决策** | ✅ **同 storage_mode = PUT secret 原地 rotate；跨 storage_mode = 新建 Credential + PATCH Profile.credential_id** |
| **不删旧** | 跨 storage_mode 新建后，旧 Credential 保留（进入 §10.7 未使用区） |

### 15.7 model_id 切换入口

| 项 | 值 |
|---|---|
| **决策** | ✅ **Selector 只切 Profile；Settings 修改 default_model；提供 [Apply to current session] 独立按钮** |
| **语义** | 修改 `Profile.default_model` 不影响已有 Binding；需手动 Apply（§9.2） |

### 15.8 default Profile 修改

| 项 | 值 |
|---|---|
| **决策** | ✅ **允许在 Settings 改 is_default** |
| **实施** | `is_default` 与 Profile POST/PATCH 同一次提交 |

### 15.9 Anthropic 隐藏

| 项 | 值 |
|---|---|
| **决策** | ✅ **前端 Store 过滤** |
| **过滤点** | `visibleDefinitions` + `usableProfiles` + Settings 不展示 |
| **不删后端数据** | legacy Anthropic Profile/Credential 保留；未使用 Credential 仍可在 §10.7 区删除 |

### 15.10 Selector 放置位置

| 项 | 值 |
|---|---|
| **决策** | ✅ **ChatPanel header**（紧邻 status pill） |

### 15.11 Credential storage_mode

| 项 | 值 |
|---|---|
| **决策** | ✅ **keyring 默认 / session_only 可选 / env 展开** |
| **UI** | keyring → API Key 密码输入；session_only → 警告"重启失效"；env → env var name 输入 |
| **env 不可恢复** | `envVarName` 局部输入默认空；不解析 `masked_value`（§10.2） |

### 15.12 前端测试基础设施

| 项 | 值 |
|---|---|
| **决策** | ✅ **M2-1 引入 Vitest + @vue/test-utils + jsdom** |
| **允许改** | `package.json` + `package-lock.json`（一次性例外）+ `vite.config.ts` 或新建 `vitest.config.ts` |
| **禁止** | 第二 lockfile；真实 Provider 网络测试；Playwright（主仓库负责） |

---

## 16. 安全边界（**重写：API Key 浏览器事实**）

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
- `providerStore` import 任何 store 或 chat 组件
- `sessionStore` / `chatStore` import `providerStore`（协调由 App.vue watch 完成；§4.4）
- 前端 import 后端 Python schema
- `ProviderSettingsModal` 操作 WebSocket
- `ProviderSelector` 直接修改 harness/client
- 任何浏览器端 Provider SDK（`openai`/`@anthropic-ai/sdk` npm package）

### 16.2 API Key 浏览器存在事实（**修复初稿错误**）

**事实**：`<input type="password">` 只负责**视觉遮罩**。用户输入期间，明文必然存在于：
- `input.value`（DOM）
- Vue 组件局部 `ref`（`apiKey.value`）
- 同源 HTTPS mutation request body

**不能要求"用户输入期间 DOM marker 零命中"**——这是浏览器不可能满足的约束。

**允许的短暂存在位置**（非泄漏）：
| 位置 | 允许？ | 理由 |
|---|---|---|
| `ProviderSettingsModal` 局部 `ref<string>` | ✅ | write-only；Modal 关闭即清 |
| `<input type="password">.value` | ✅ | 视觉遮罩 + 同源 DOM |
| 同源 HTTPS mutation request body | ✅ | 传输必需；不持久化 |
| Pinia state | ❌ | devtools 可观测；持久化风险 |
| `localStorage` / `sessionStorage` | ❌ | 持久化 |
| URL / query string | ❌ | 日志 / 浏览器历史 |
| `console.*` | ❌ | devtools console 持久 |
| Error 对象 / `JSON.stringify` | ❌ | 可能进日志 |
| HTML attribute（`data-*` / `value`） | ❌ | DOM 检查器可见 |
| Playwright trace / HAR artifact | ❌（**需 redaction**） | 测试 artifact 持久 |

### 16.3 测试 marker 正确语义（**修复**）

初稿要求"全 UI state + log + trace 零命中"——不准确。**正确表述**：

| 时机 | 断言 |
|---|---|
| 保存**成功后** | DOM `input.value === ""`；组件局部 `apiKey.value === ""`；Pinia/storage/log/snapshot **0 marker** |
| Modal **关闭后** | 同上 |
| **切 Session 后** | 同上 |
| 保存**失败后**（Modal 仍开） | `apiKey.value` 可能保留（决策点 §15.5）；Pinia/storage/log/snapshot **仍 0 marker** |
| **输入期间** | 不断言（浏览器事实不允许） |

**Credential E2E 测试 artifact redaction**：
- Playwright trace / HAR 若包含 request body，必须实施 redaction（`test.use({ trace: 'on-first-retry' })` + 自定义 sanitizer）
- 或在测试 setup 中 intercept 并 mask request body
- 这是 M2-4 E2E 范围（由主仓库 `pi-py` 实施）

### 16.4 浏览器存储审计（已有）

- `localStorage`：**0 使用**（M2 保持）
- `sessionStorage`：**0 使用**（M2 保持）
- URL/query string：**不传递** Key/credential_id

### 16.5 Pinia devtools

- API Key **不进** Pinia state → 不进 devtools
- Key 只在 `ProviderSettingsModal` 局部 `ref`

### 16.6 Console 日志

- 现状：11 个 `console.error`，全在 catch handler，无用户数据
- M2 保持：mutation error 走 `mutationError.value`，**不** `console.log` payload

### 16.7 错误对象

- 后端固定安全 message → 前端直接展示
- 不拼接底层 exception / 完整 response body
- Network 错误的 `ApiError` 不暴露 request body（Key 在 body 中）

### 16.8 浏览器 autofill / password manager

- Key 输入框 `type="password"`
- `autocomplete="new-password"`（避免 browser 复用其它站点密码）
- 不 `v-model` 到 store

### 16.9 前端不调用以下 endpoint（M2 范围排除）

- `POST /api/credentials/{id}/validate`（remote validation；M2 不做）
- `POST /api/provider-hints`（前端不自行推断 Key 归属）

---

## 17. 测试矩阵（M2-1～M2-4 实施时参考）

### 17.1 providerStore 单元测试（依赖 §15.12 引入 Vitest）

| 测试场景 | 验证点 |
|---|---|
| `initialize()` | 三类资源加载；`initialized=true` |
| `loadBinding(sid)` | 写入 `currentBinding`；`bindingSessionId === sid` |
| Session 快速切换 | stale response 不覆盖（`bindingLoadToken` 防护） |
| Mutation error | 写入 `mutationError`；不抛到 caller |
| `clearSessionState()` | 清 `currentBinding / bindingSessionId` |
| Secret 不入 state | `apiKey` 不在 store 定义中；spy `console.log` 无 Key |
| `canSendPrompt` 语义 | `state="idle/loading/error"` → false；`state="loaded"+binding=null` → true（Legacy）；`loaded+ready` → true；`loaded+失效` → false |
| `bindingLoadState` 状态机 | idle→loading→loaded（成功）；loading→error（失败）；切 session 从 loaded/error→loading |
| `refreshForSession` 顺序 | currentBinding=null → sessionId=sid → state=loading → token++ → 清 error → GET |

### 17.2 ProviderSelector 组件测试 + ChatInput 接线测试

**ProviderSelector**：

| 场景 | 验证点 |
|---|---|
| GLM/Qwen/Kimi 展示 | Anthropic 不出现 |
| 无 binding（`state="loaded"+binding=null`） | 显示"默认模型"（决策文案） |
| `state="loading"` | skeleton 灰条 |
| `state="error"` | 红条 + bindingLoadError 文本 |
| 已选 Profile | 高亮；显示 `currentBinding.model_id` |
| active request | Selector disabled |
| `needs_credential` 红点 | 点击引导打开 Settings |
| 当前 binding 指向失效 Profile | 仍显示该 Profile 作为当前项（disabled） |
| 长 model_id | ellipsis + tooltip |
| 切换触发 `setSessionBinding(sid, profile.id, profile.default_model)` | 用 Profile.default_model，非旧 binding.model_id |

**ChatInput 接线**（Revision 2 新增；§9.7）：

| 场景 | 验证点 |
|---|---|
| `providerReady=false` + text="hi" | send 按钮 disabled |
| `providerReady=true` + text="hi" | send 按钮 enabled；submit 正常 emit |
| `providerReady=false` + text="hi" + 回车 | submit 不 emit |
| `providerReady` 从 false→true（binding 加载完成） | send 按钮自动 enabled（text 非空时） |
| 不传 `providerReady`（默认 true） | 向后兼容；现有行为不变 |

### 17.3 ProviderSettingsModal 组件测试

| 场景 | 验证点 |
|---|---|
| 新建 Credential（keyring）+ Profile | POST × 2；返回 ID 写入 form |
| Rotate Credential（PUT secret，同 storage_mode） | 原地 rotate；不新建 ID |
| 切换 storage_mode（keyring→env） | POST 新 Credential + PATCH Profile.credential_id |
| PATCH Profile | 切 credential_id；改 default_model；改 is_default |
| 空 model_id | 422；字段红字 |
| 409 profile_in_use | ErrorBanner |
| 空 Key 保存 | 不调 Credential mutation |
| 关闭 Modal | `apiKey.value === ""` |
| 保存成功 | `apiKey.value === ""` |
| 保存失败（Modal 仍开） | `apiKey.value` 保留（§15.5） |
| 切 Session（Modal 开着） | Modal 自动关闭；`apiKey.value === ""` |
| `/models` 返回 `[]` | datalist 空；可手动输入 |
| env 已有 Credential + 不填 envVar | 不调 rotate；Profile 单独 PATCH 成功 |
| env 新建 + envVar 空 | [Save] 按钮 disabled（必填校验） |
| 不解析 masked_value 回填 envVar | 即使 `masked_value="ENV[MY_KEY]"`，`envVarName.value` 默认空 |
| Credential 创建成功但 Profile 失败 | 保留 credential_id；重试不新建 Credential |
| [Apply to current session] | 独立 PUT Binding；用 Profile.default_model |
| [Apply] 失败 | 不回滚 Profile；ErrorBanner 显示绑定失败 |
| 未使用 Credential 区 | 展示引用计数=0 的 Credential；含 legacy Anthropic 关联 |
| [Delete Credential] | 二次确认；DELETE 成功后从 unusedCredentials 移除 |

### 17.4 Session 集成测试

| 场景 | 验证点 |
|---|---|
| Session A → Qwen；Session B → Kimi；切换 | 各自 binding 独立恢复 |
| 新建 session | 后端自动 default binding；前端 GET 读到 |
| 旧 session 保持创建时快照 | 切回后 `currentBinding.model_id` 不变（即使 Profile.default_model 已改） |
| 运行中 Selector disabled | `chatStore.sending` true 时 |
| Prompt 完成后可切换 | `sending` false 后启用 |
| 下一 Prompt 使用新 Binding | 切 binding → 发 prompt → 真实 provider 改变 |
| 删除 Session 后切到新 Session | `currentBinding` 清空并加载新值（不依赖后端 cascade） |

### 17.5 E2E（主仓库 `pi-py` 执行）

| 场景 | 验证点 |
|---|---|
| 配置 Qwen Profile + 绑定 + 发送 | 流式回答 |
| 切 Kimi + 下一 Prompt | Kimi 回答 |
| Regenerate | 用当前 binding |
| 重载页面 | binding 恢复 |
| API Key 不出现在 DOM/storage/log/trace | 保存成功后 + Modal 关闭后 marker 零命中（§16.3） |

### 17.6 测试基础设施事实

- **本副本无前端测试框架**（`package.json` 无 vitest/jest）
- **M2-1 经 user 批准后引入** Vitest + @vue/test-utils + jsdom（§15.12）
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
| `src/components/providers/ProviderSection.vue` | M2-2 |
| `src/components/providers/ProfileForm.vue` | M2-2 |
| `src/components/providers/useProviderForm.ts`（composable，可选） | M2-2 |
| `tests/unit/providerStore.spec.ts` | M2-1 |
| `tests/unit/ProviderSelector.spec.ts` | M2-3 |
| `tests/unit/ProviderSettingsModal.spec.ts` | M2-2 |
| `tests/unit/ProfileForm.spec.ts` | M2-2 |
| `vitest.config.ts`（或合并入 vite.config.ts） | M2-1 |

### 18.2 修改（预期）

| 文件 | 改动 | 阶段 |
|---|---|---|
| `src/api/index.ts` | barrel re-export `providers` | M2-1 |
| `src/types/index.ts` | barrel re-export `providers` | M2-1 |
| `src/App.vue` | onMounted 调 `providerStore.initialize()`；`watch(activeSessionId)` 协调（§4.4） | M2-1 |
| `src/components/chat/ChatPanel.vue` | header 插入 `<ProviderSelector>`；传 `:provider-ready` 到 ChatInput | M2-3 |
| `src/components/chat/ChatInput.vue` | 新增 `providerReady?: boolean` prop（默认 true）；`canSend` computed 增加判断（§9.7） | M2-3 |
| `src/components/chat/MessageBubble.vue` | regenerate button disabled 也基于 `providerStore.canSendPrompt` | M2-3 |
| `src/components/layout/SessionSidebar.vue` | footer 加 "Provider Settings" 按钮 | M2-2 |
| `package.json` | 新增 vitest + @vue/test-utils + jsdom devDeps；新增 `test` script | M2-1 |
| `package-lock.json` | 同步 lockfile（**仅此一次例外**，§15.12） | M2-1 |
| `vite.config.ts` | 添加 vitest 配置（或新建 vitest.config.ts） | M2-1 |

### 18.3 明确禁止修改

- 任何后端 Python 文件（`src/pi_agent_core_py/**/*.py`）
- Core Runtime（`loop.py` 等）
- 现有 store 的公开 API（不破坏 chatStore / sessionStore 契约）
- Modal/EmptyState/ErrorBanner/LoadingSpinner 的 props（沿用现有）
- **`sessionStore.ts` 不修改**（M2 协调由 App.vue watch 完成；§4.4）
- **`chatStore.ts` 不修改**（无 cross-store import）
- 创建第二个 lockfile（不引入 pnpm/yarn）

---

## 19. M2-1～M2-4 实施顺序

```
M2-0 Frontend Integration Audit（本文档）
    ↓
    ✋ user review + 冻结本文档（含 §15.1～§15.12 共 12 决策点）
    ↓
M2-1 Frontend API types + providerStore + 测试基础设施
    - 引入 vitest + @vue/test-utils + jsdom（§15.12）
    - src/api/providers.ts
    - src/types/providers.ts
    - src/stores/providerStore.ts（含 stale-response 防护）
    - App.vue: initialize() + watch(activeSessionId)
    - 单元测试
    ↓
    ✋ user review + 单元测试 pass
    ↓
M2-2 ProviderSettingsModal（ProviderSection × ProfileForm）
    - ProviderSection.vue / ProfileForm.vue
    - ProviderSettingsModal.vue（含 UnusedCredentialsSection §10.7）
    - SessionSidebar.vue footer 按钮
    - API Key / envVarName write-only + storage_mode 选择（§10.4）
    - 分阶段保存 + Apply to current session 独立动作（§10.3）
    - 未使用 Credential 二次确认删除
    - 组件测试
    ↓
    ✋ user review
    ↓
M2-3 ProviderSelector + ChatInput 接线
    - ProviderSelector.vue
    - ChatPanel.vue header 插入
    - **ChatInput.vue 最小扩展**（新增 `providerReady` prop；§9.7）
    - **MessageBubble.vue regenerate button** disabled 接 `canSendPrompt`
    - active request disabled
    - 组件 + 集成测试（含 ChatInput 接线测试 §17.2）
    ↓
    ✋ user review
    ↓
M2-4 Frontend Integration Validation
    - Session A/B 隔离
    - default binding restore
    - reload 恢复
    - Prompt/Regenerate provider 切换
    - frontend build clean
    - Playwright（主仓库，含 trace redaction；§16.3）
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

**协调路径**（新增）：
```
App.vue
    ↓ imports
stores/sessionStore (activeSessionId)
stores/providerStore (refreshForSession / clearSessionState)
    ↓ watch(activeSessionId).immediate
触发 providerStore.refreshForSession(sid) 或 clearSessionState()
```

**关键**：`providerStore` **不 import** `sessionStore`；协调仅发生在 `App.vue` 组件层。

### 20.2 禁止导入

| From | 禁止 import |
|---|---|
| 任何前端文件 | `openai` / `@anthropic-ai/sdk` / `langchain` 等 Provider SDK |
| `providerStore` | 任何 `stores/**` 或 `components/**` |
| `sessionStore` | `providerStore` |
| `chatStore` | `providerStore` |
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
- **待 user 审核决策点数**：**12 个**（§15.1～§15.12，含新增 storage_mode + 测试基础设施）
- **race 场景识别**：5 个（§7.2）
- **错误码映射**：18 个（§14.1）

---

## 22. 只读验证基线（commit 时执行）

| 检查 | 命令 | 结果 |
|---|---|---|
| Backend offline pytest | `pytest tests/ -m "not slow and not integration and not docker" --no-cov` | 2585 passed + 1 skipped + 14 deselected（与 M1 baseline 一致） |
| Frontend build | `cd src/pi_agent_core_py/web/frontend && npm run build` | 142.91 KB JS / 40.15 KB CSS（M2-0 不改 frontend） |
| Frontend typecheck | `npm run typecheck` | PASS |
| Frontend lint | `npm run lint` | PASS（max-warnings=0） |
| Ruff | `ruff check src tests scripts` | All checks passed |
| `git diff --check` | — | clean |
| `git status --short` | — | 只有修订的本文档 |
| Playwright E2E | 主仓库 `pi-py` 执行 | 本精简副本 CLAUDE.md 明确无 `tests/e2e/` |

---

## 23. 状态收口

**本文档状态**：✅ **M2-0 FRONTEND INTEGRATION — DESIGN FROZEN (REVISION 2)**

**变更历史**：
- 初稿 `62f1366` → Revision 1 `068a548`（解决 10 类阻塞）→ **Revision 2 本次冻结**（解决剩余 4 阻塞 + 术语统一 + 12 决策 APPROVED）
- 后续 freeze commit 在 STATUS.md / TODO.md / ROADMAP.md 中引用本文档时应记录为当前 commit hash（由 git 生成；不在文档内自引用）

**12 决策状态**：✅ **ALL APPROVED**（§15.1～§15.12）

**M2-1 启动条件**：
- ✅ 本文档 DESIGN FROZEN
- ✅ 12 决策全部 APPROVED
- ✅ 测试基础设施引入范围已锁定（§15.12）
- ✅ `canSendPrompt` 接线方案已锁定（§9.7）
- ✅ env_var_name 不可恢复语义已锁定（§10.2）
- ✅ Credential 生命周期规则已锁定（§10.6, §10.7, §15.2, §15.6）

**M2-1 允许启动**。

**仍禁止**（M2 全程）：
- merge / tag / push
- 修改本文档已冻结的决策（若发现新决策点，需 user approve 修订本文档）
- 跨阶段提前实施（如 M2-1 不能改 ChatInput.vue——属于 M2-3）

---

**M2-0 DESIGN FROZEN REVISION 2 @ 2026-07-23**——M2-1 ✅ APPROVED TO START。

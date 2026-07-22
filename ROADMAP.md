# Roadmap

> 未来阶段。当前状态见 [STATUS.md](STATUS.md)；已发布版本见 [CHANGELOG.md](CHANGELOG.md)。

---

## P1-E — Multi-Provider Switching（M1 / M2 / M3 milestone，PIVOT @ 2026-07-19）

> **Pivot 决策（2026-07-19）**：原 P1-E2 / E3 / E4 / E5 拆分过细，且 E2-4 独立 Security Freeze 会冻结一个用户无法直接使用的配置后端。改为单一 milestone——**M1 Runtime / M2 Frontend / M3 Unified Freeze**，最终统一 security + E2E freeze。详细 Pivot 见 [docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md) §19。

**产品目标（M1~M3 完成时达成）**：用户配置 GLM / Qwen / Kimi Key → 每个 Provider 选择模型 → 聊天顶部切换 Provider/Model → 每个 Session 独立保存 → Regenerate 使用当前选择 → 重启后恢复——且 Key 不进入 SQLite 明文 / 日志 / 消息 / 事件 / 导出。

### 跨阶段冻结决策（7 边界 + Custom URL 安全，M1/M2/M3 全程有效）

| # | 边界 | 冻结结论 |
|---|---|---|
| A | Revision 与 provider/model 信息 | 不升级 schema v2。`web_message_revisions.content_json` 已含完整 AssistantMessage（含 `provider` / `model` / `usage`）——revision list API 通过 serializer 投影取出，不返回正文 / hash / request_id |
| B | 现有 Provider Adapter 整合 | **包装**而非替换。保留 `providers/glm.py` / `anthropic_compat.py` / `base.py` 不动；新增 `registry.py` / `factory.py` / `model_catalog.py` / `openai_compat.py`；按需新增专项测试。`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py` 继续冻结 |
| C | 请求级 Provider 切换边界 | 仅 Web 层。新增 `web/provider_runtime.py`（`RequestProviderRuntime` + `bind_to_harness` async context manager）——临时替换 `harness.agent.<provider field>` 引用，`finally` 恢复 + 关闭 request client。不改 `agent.py` 源码 |
| D | "Restart"测试语义 | P1-E 中的 restart 指 **server/app restart**（uvicorn 重启 + SQLite 恢复 + SecretStore 重新解析）。浏览器 reload 自动恢复 session 留 P2-A URL Routing |
| E | Markdown Panel 与 No Drawer 原则 | AppShell 顶层仍为 `sidebar + workspace`；`workspace` 内允许用户主动打开的 contextual document panel；**禁止**永久开发调试 Drawer（trace / raw event / internal state） |
| F | Context Budget 数据来源 | preflight token estimate（canonical messages + system prompt + enabled skills + tool definitions + attachment metadata）÷ model.context_window；provider usage 仅用于 post-hoc 展示，不作为下次 budget 来源 |
| G | OS Keyring 与 CI/E2E | 引入 `SecretStore` Protocol 抽象——本地用 `OSKeyringSecretStore`，CI/E2E 用 `InMemorySecretStore`，已有 env 配置用 `EnvSecretStore`；`keyring` 为 web optional dependency（`try: import keyring except ImportError`），不可用时不阻塞 app 启动 |
| URL | Custom Base URL 安全 | 仅允许 http/https；拒绝 URL 内嵌 userinfo（`user:pass@`）；拒绝 `file:` / `javascript:`；验证前显示完整 host；切换 Base URL 强制重新验证；错误信息不含 Authorization header；日志只记 scheme/host/安全错误码 |

详细设计冻结备忘：见本仓库 conversation history 2026-07-16。

### Backend Foundation（✅ FROZEN @ master，不单独 merge / tag）

P1-E M1 之前的配置后端已 frozen，不再扩展。

| 子阶段 | 状态 | Commit |
|---|---|---|
| P1-E1 Secure Credentials | ✅ MERGED + TAGGED `v0.0.27-secure-credentials` | `de05c66`（22 commit） |
| P1-E2-1 Schema + Store | ✅ FROZEN | `89fabfd` |
| P1-E2-2 Service + Static Model Options | ✅ FROZEN | `a2c7932` |
| P1-E2-3A Session Creation Audit | ✅ FROZEN | `9878ef9` |
| P1-E2-3 Composition + REST API + Session binding | ✅ FROZEN（3 commit） | `17c843d` / `9bbd0f2` / `cad7ca7` |

**配置后端能力清单（M1/M2/M3 不再扩展）**：

- 3 张表：`web_provider_config_schema_meta` / `web_provider_profiles` / `web_session_model_bindings`
- 4 个生产模块：`provider_config_store.py` / `provider_config_service.py` / `provider_profiles_api.py` / `model_options.py`
- 7 API：4 Profile CRUD + 1 models + 2 session binding
- Provider Registry：内置 `anthropic` / `glm`（M1-2 加 `qwen` / `kimi`）
- Credential 子系统：`SecretStore` Protocol + OSKeyring / InMemory / Env；`CredentialRecord` repository；masked/fingerprint serializer；不存明文
- 测试基线：2063 full pytest + 37/37 E2E + ruff clean + 0 Core Runtime diff + 0 network + 0 secret reads

**E2-4 独立 Security Freeze cancelled**——并入 M3 Unified Freeze。

### M1 — Multi-Provider Runtime（✅ COMPLETE / FROZEN）

把 Backend Foundation 接到真实 Prompt/Regenerate 执行。Core Runtime 继续冻结（GLM 包装现有，Qwen/Kimi 共用 OpenAI-compatible Adapter）。

| 子阶段 | 状态 | Commit | Tests |
|---|---|---|---|
| M1-0 Provider Contract Audit | ✅ FROZEN | `be13a1f` | — |
| M1-1 OpenAI-compatible Adapter | ✅ FROZEN | `8daaa90` | 124 |
| M1-2 Qwen / Kimi Presets | ✅ FROZEN | `4d89c82` | 64 |
| M1-3 ProviderFactory | ✅ FROZEN | `a35a4ad` | 64 |
| M1-4 RequestProviderRuntime | ✅ FROZEN | `8827bd1` | 80 |
| M1-5 Prompt Integration | ✅ FROZEN | `f116ddd` | 47 |
| M1-6 Regenerate Validation | ✅ FROZEN | `06ecb80` | 67（validation-only） |
| M1-7 Runtime Final Validation | ✅ COMPLETE / FROZEN | (本提交) | 76 / 4 files |

**M1 测试基线（post-M1-7）**：2585 full pytest + 1 skipped + ruff clean + 0 Core Runtime diff + 0 providers/* diff（仅新增 `openai_compat.py` / `factory.py`）+ 0 network + 0 secret reads + frontend build clean。E2E 由主仓库 `pi-py` 验证（本精简副本无 e2e/）。

**关键架构结论**（M1-5/M1-6 测试证明）：`_execute_prompt` 是 Provider Runtime 的唯一接入点；`_run_regeneration_core` 经 `override_initial_messages + suppress_user_append=True` 复用同一执行函数；无需第二次接线。M1-7 AST 静态约束锁定此架构（lexical body 内 `resolve_selection/bind_to_harness/build_adapter` 只出现在 `_execute_prompt`）。

**M1-0 Provider Contract Audit**（设计文档，无生产代码）：

- 只读审计 `providers/base.py` / `glm.py` / `anthropic_compat.py` / `registry.py` + Agent Provider 持有 + Prompt/Regenerate 入口
- 输出 `docs/design/p1-e-m1-provider-runtime.md`：冻结统一 Adapter 接口（`stream()` / `close()` / tool_call 增量 / usage / finish_reason）
- 不为 OpenAI-compatible 重新发明第二套事件模型——围绕现有 contract 实现

**M1-1 `providers/openai_compat.py`**：Qwen / Kimi 共用 OpenAI-compatible Adapter——request / SSE stream / assistant text delta / tool calls / finish reason / usage / safe error mapping / client close。

**M1-2 Qwen / Kimi ProviderDefinition presets**：`registry.py` 加 `qwen` / `kimi`，protocol=`openai_compatible`，各自 `default_base_url`。

**M1-3 `providers/factory.py`**：唯一知道「哪个 Provider 用哪个 Adapter」的位置——输入 `ProviderDefinition` + `api_key` + `model_id`，输出 RequestProvider。GLM → 包装现有；其他 → `OpenAICompatibleProvider`。

**M1-4 `web/provider_runtime.py`**：`RequestProviderRuntime` + `bind_to_harness` async context manager。Session Binding → Profile → Credential → Secret → Factory → 临时绑定 `harness.agent.<provider>` → 执行 → finally 恢复 + close。复用现有单 active request lock。

**M1-5 Prompt integration**：`_execute_prompt` 接入 `provider_runtime.bind_to_harness`（唯一接入点）。

**M1-6 Regenerate validation**：validation-only——`_run_regeneration_core` 通过 `_execute_prompt` 间接复用 M1-5 接线点；AST 静态约束 + 67 测试覆盖。无生产代码修改。

**M1-7 Runtime final validation**：跨模块组合 validation-only——3-provider E2E 矩阵 + 跨 Session 隔离 + 默认/显式切换全链路 + app restart + 安全出口全审计 + 静态架构约束。M1 production diff = 0。

**M1 显式不包含**：前端切换 UI（M2）；最终 security freeze（M3）；Custom Base URL；Custom Provider；远程模型目录；多 Key 复杂管理。

**请求级不可变快照**（边界 C 落地）：请求开始时记录 `RequestProviderSelection(profile_id, provider_id, model_id, selection_source)`；运行中切换只影响下次请求；Regenerate 用当前 Session 当前模型。

### M2 — Frontend Switching（🟡 NEXT，M1 ✅ COMPLETE）

最简前端：两个组件 + 一个 store。

- `stores/providerStore.ts`（不膨胀 chatStore）
- `components/provider/ProviderSelector.vue`（顶部切换器；运行时禁用并提示 `Generating...`）
- `components/provider/ProviderSettingsModal.vue`（每个 Provider 一张卡：API Key + Model ID + status；支持 GLM / Qwen / Kimi）
- Session binding restore（刷新后保留选择）
- Prompt 运行时禁用 selector

**M2 显式不包含**：`ProviderProfileList` / `CredentialForm` / `ModelPicker` / `ProviderStatusBadge` / `ProviderHealthPanel` / `ModelCatalogModal`（全部并入 Settings Modal）；Custom Provider / Custom Base URL 入口；远程模型搜索；模型能力展示。

**模型输入策略**：少量静态建议 + 自由填写 `model_id`（不调用 `/v1/models`）。

### M3 — Unified Freeze

最终验收 + 安全 + merge + tag（合并原 P1-E5 + E2-4）。

**验收清单（至少 18 项）**：

1. 添加 GLM / Qwen / Kimi Profile
2. Key 不出现在任何 API response（含 list / get / WS event / export markdown）
3. 一键切换 Profile / Model
4. Session A/B 使用不同 Provider
5. **server restart** 后 Profile + Binding 恢复
6. session-only Key restart 后 → `needs_key`
7. env reference Key restart 后 → 重新解析
8. 模型切换只影响下一请求（active request 中途切换不污染当前）
9. Regenerate 使用当前 Session 当前模型（in-place `message_id` 不变）
10. 删除 Key 后 Profile → `needs_key`
11. invalid Key 安全报错（错误信息不含 Key / Authorization）
12. GLM / Qwen / Kimi 真实流式回答 + tool_use 正常
13. 切换 Provider 后失败不污染下一请求
14. Request 启动后 provider/model 不可变（请求快照生效）
15. Core Runtime diff = 0
16. API / SQLite / log / WS / export 无 Key（含 marker 测试）
17. 错误信息不含 Authorization / 内部 endpoint
18. Playwright E2E 全 PASS

**M3 提交**：`feat(providers): deliver multi-provider switching`（M1+M2+M3 统一 merge）+ tag。

### P1-E 显式不包含（M1 / M2 / M3 全程排除）

- Custom Provider / Custom Base URL（M3 之后单独评估）
- Model Catalog 持久化 / remote `/v1/models` 调用
- Provider health / 限流状态 / 成本统计
- 自动 provider fallback / 模型负载均衡
- 复杂多 Key / 同 Provider 多 Profile 管理 UI
- 远程模型搜索 / 模型能力说明
- Anthropic 作为主要产品 Provider 入口（底层兼容代码可保留）
- 大规模重构 `web/app.py`（留 P3）
- 修改 Core Runtime（`loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py` / `providers/base.py` / `providers/glm.py` / `providers/anthropic_compat.py`）

---

## P1-F — Markdown Workspace Panel（⚪ PLANNED）

让用户点击 Markdown FileChip → workspace 内临时打开右侧预览面板（不是 Drawer），支持预览 / 源码 / 标题目录 / 代码高亮 / 下载，但不自动注入 LLM 上下文。

### P1-F1 — Markdown Preview API

**范围**：
- `GET /api/sessions/{sid}/files/{file_id}/preview`——后端纯读取，不调 LLM、不创建 snapshot、不修改 messages
- 安全：`get_for_session()` 跨 session 拒绝（403）；不返回绝对路径；只允许 markdown / text；`max_bytes` 截断（建议 512 KB）
- 复用 `view_file` 底层纯读取函数，但**不通过执行 Tool 来驱动 UI**
- 响应 DTO：`file_id` / `session_id` / `name` / `mime` / `format` / `size` / `sha256` / `content` / `line_count` / `truncated` / `max_bytes`

### P1-F2 — Contextual Markdown Side Panel

**范围**：
- `AppShell` 顶层保持 `sidebar + workspace`；workspace 内部 grid：`chat + resizable document panel`（420px 默认，320–720px 可调；窄屏全屏 overlay）
- 入口：消息 FileChip / 上传列表 / `list_files` 卡片 / `view_file` 结果卡片 / session 文件列表
- `components/workspace/MarkdownPanel.vue`（Header / ModeTabs / Outline / Content）
- `stores/documentStore.ts`（**不**继续膨胀 chatStore）
- Markdown 渲染：`markdown-it` + `DOMPurify` + 现有 `highlight.js`；禁内嵌 HTML / script / 远程图片 / `file://` / iframe；外链加 `rel="noopener noreferrer"`
- 文件删除后自动关闭；session 切换自动关闭

### P1-F3 — E2E + Docs + Freeze

**验收清单（至少 15 项）**：
1. 点击 Markdown FileChip 打开
2. 同 session 文件可见
3. 跨 session 拒绝
4. 源码与预览切换
5. 标题目录跳转
6. 代码块高亮
7. 内嵌 script 不执行
8. 远程图片不主动加载
9. 大文件截断提示
10. session 切换关闭面板
11. 文件删除后面板关闭
12. 移动端全屏
13. 面板打开不影响消息流
14. 文件内容不自动进入下一轮 LLM
15. 下载仍可用

---

## P2 — Web Agent Product Enhancements（⚪ PLANNED）

按优先级排序——独立设计、独立测试、独立冻结。**不**要求按字母顺序执行。

### P2-A — Session URL Routing + Full Reload Recovery（优先级 1）

> 当前最明确的产品可靠性缺口。

**范围**：
- URL-based session routing（`/chat/{session_id}` 或 `?sid=...`）
- 浏览器 reload → 解析 session_id → 加载 persisted messages → 查询 active request → 恢复 Prompt/Regenerate 状态
- 兼容现有 WS reconnect recovery（`regenerate.spec.ts:243-290`）

### P2-B — Human Approval UI（优先级 2）

> 项目已有 `allow` / `deny` / `require_approval`，但 `require_approval` 当前按 `deny` 处理。

**范围**：
- Agent 请求执行高风险工具 → UI 显示 Approval Card（工具名 + 参数）
- 第一版只支持：Approve once / Deny（不做永久授权、不做 RBAC）
- 不影响 inline turn card 原则（Approval Card 也是 inline card）

### P2-C — Context Budget + Compaction UI（优先级 3）

**前置 Audit（启动前必做）**：
1. AssistantMessage.usage 当前字段
2. GLM 返回 usage 的位置
3. Snapshot/session 是否保存 usage
4. system prompt 和 tool schema 如何参与估算
5. 现有 compaction API
6. SummaryMessage 如何进入 LLM context
7. 不同模型的 context_window 是否已知

**范围**：
- preflight token estimate（按边界 F）
- UI：`Context ~42%`（必须显示近似符号）+ warning 70% / compaction 85% / hard stop 95%
- Compaction 触发或提示
- post-hoc 展示 provider usage（input/output token / latency）

### P2 候选（未启动）

- **P2-D Session 搜索/收藏/归档**——左栏 session 增多后必要
- **P2-E 文件引用到输入框**——Markdown 侧栏选中文字 → "引用到对话" → 输入框产生结构化引用
- **P2-F 模型健康与用量反馈**——selector 显示认证失败 / 限流 / 上次验证时间；消息下方显示 provider/model/IO token/latency

### P2 不在本阶段（移到 P3 或更后）

- Web App 模块化（`web/app.py` 4400+ 行拆分）——用户决策"不顺带重构 `web/app.py`"
- Multi-session 并行执行
- Request registry 持久化
- 自动 provider fallback / 模型负载均衡
- 多 Agent 编排

---

## 已 DEFERRED（转出主路线，非永久放弃）

### P1-D3 — PDF Text Extraction ⏸ DEFERRED（2026-07-16）

3 个边界冲突曾经阻塞——边界 A-G 冻结后已可重启，但相对 P1-E/F 优先级更低。重启条件：
- P1-E / P1-F 完成
- PDF adapter 边界重新评估（是否仍需 `tools/view_file.py` 修改）
- FileRef metadata 字段是否已被 P1-E/F 引入

### PDF / Vector RAG ⏸ DEFERRED

明确延后到 P2 候选完成之后。不引入向量存储、不实现长文档检索。

---

## 明确不做（长期）

- OCR / Image understanding / 视觉理解
- PDF 表单 / 注释 / 嵌入对象
- RAG / Vector Memory / Long-term Memory
- Multi-Agent 编排
- CLI / RPC mode
- 多用户账号 / OAuth / RBAC / 企业 secret vault
- 公网部署 / 横向扩展
- MCP marketplace / Skill marketplace
- Skill 在线编辑 / 跨项目共享 / 热加载
- 本地文件系统操作工具（bash / read / write / edit / grep / find / ls）
- 自动 provider fallback / 模型负载均衡
- Markdown 在线编辑 / 保存 / diff / 协同
- 长期后台任务调度

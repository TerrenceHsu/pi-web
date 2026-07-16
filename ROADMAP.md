# Roadmap

> 未来阶段。当前状态见 [STATUS.md](STATUS.md)；已发布版本见 [CHANGELOG.md](CHANGELOG.md)。

---

## P1-E — Multi-Provider / Model Profiles（✅ DESIGN FROZEN 2026-07-16）

让用户保存多个 API Key、为每个 Key 配置供应商 / Base URL / 默认模型、校验 Key、拉取模型列表、在聊天顶部一键切换、每个 Session 独立保存选择、Regenerate 使用当前模型、重启后恢复——且不泄露 Key。

### 跨阶段冻结决策（7 项边界 + Custom URL 安全）

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

### P1-E1 — Provider Registry + Secure Credentials 🟡 NEXT

**范围**：
- `ProviderDefinition` 内置表（GLM / Anthropic-compatible / OpenAI-compatible / Custom）
- `SecretStore` Protocol + 3 实现（OSKeyringSecretStore / InMemorySecretStore / EnvSecretStore）
- `CredentialRecord` repository（SQLite 只存 `secret_ref` / `fingerprint` / `masked_value`，不存明文）
- masked/fingerprint serializer（前端只见 `sk-****8A31`）
- provider hint local heuristic（`sk-` 前缀等格式规则——不向第三方发请求探测）
- credential CRUD + validate API（`POST /api/credentials` / `PATCH` / `DELETE` / `POST /validate`）
- secret leak tests（grep REST response / WS event / snapshot / log / export 等所有出口，必须 0 命中）

**显式不包含**：
- 模型目录（P1-E2）
- Session 绑定（P1-E2）
- 请求执行切换（P1-E3）
- 前端模型选择器（P1-E4）
- Regenerate 模型切换（P1-E3）
- OpenAI Adapter 完整实现（按需在 P1-E2 加）

### P1-E2 — Profiles + Model Catalog + Session Binding

**范围**：
- `ProviderProfile`（聚合 credential + provider + base_url + default_model）
- `SessionModelBinding`（每 session 独立选择，全局默认仅用于新建）
- model refresh（远端 `/models` + 内置静态目录 + 用户 custom）
- `ModelCapabilities`（streaming / tool_calling / reasoning / vision / context_window——未知用 `None`）
- restart persistence（SQLite profile + binding 恢复）

### P1-E3 — Request-scoped Provider Selection

**范围**：
- `web/provider_runtime.py`（`RequestProviderRuntime` + `bind_to_harness` async context manager）
- `_run_prompt_core` / `_run_regeneration_core` 接入——请求启动前解析 SessionModelBinding → 临时替换 `harness.agent.<provider>` → finally 恢复 + close
- request metadata 记录 `provider_profile_id` / `provider_id` / `model_id` / `selection_source`（不记 credential_id / api_key / Authorization）
- Regenerate 用当前 session 当前模型（不动 D2 schema；revision `content_json` 自带 model 信息）
- 错误隔离（provider 创建失败 → Agent 不执行；不污染下一请求）

### P1-E4 — Frontend Provider / Model Selector

**范围**：
- `components/provider/`（`ProviderModelSelector.vue` / `ProviderProfileModal.vue` / `ProviderProfileList.vue` / `CredentialForm.vue` / `ModelPicker.vue` / `ProviderStatusBadge.vue`）
- `stores/providerStore.ts`（不膨胀 chatStore）
- 顶部 selector：显示当前 Profile/Model + 快速切换 + 打开管理 Modal
- Key 创建/删除/校验放在 Modal（不放主聊天界面）
- session 独立选择（A/B 不同模型）

### P1-E5 — E2E + Docs + Freeze

**验收清单（至少 11 项）**：
1. 添加 2 个不同 Provider Profile
2. Key 不出现在任何 API response（含 list / get / WS event / export markdown）
3. 一键切换 Profile
4. Session A/B 使用不同模型
5. **server restart** 后 Profile 恢复
6. session-only Key restart 后 → `needs_key`
7. env reference Key restart 后 → 重新解析
8. 模型切换只影响下一请求（active request 中途切换不污染当前）
9. Regenerate 使用新模型（in-place message_id 不变）
10. 删除 Key 后 Profile → `needs_key`
11. invalid Key 安全报错（错误信息不含 Key / Authorization）

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

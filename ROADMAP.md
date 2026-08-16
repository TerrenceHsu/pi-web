# Roadmap

> 未来阶段。当前状态见 [STATUS.md](STATUS.md)；已发布版本见 [CHANGELOG.md](CHANGELOG.md)。

---

## P1-E — Multi-Provider Switching（✅ COMPLETE / FROZEN；M1 / M2 / M3 milestone，PIVOT @ 2026-07-19）

> **状态（2026-07-26）**：M1 Runtime ✅ + M2 Frontend ✅ + M3 Unified Freeze ✅ —— 全链路 COMPLETE / FROZEN。最终归档见 [docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md](docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md)。Release Candidate ✅ READY FOR USER REVIEW（merge / tag / push 仍需独立授权）。
>
> **Pivot 决策（2026-07-19）**：原 P1-E2 / E3 / E4 / E5 拆分过细，且 E2-4 独立 Security Freeze 会冻结一个用户无法直接使用的配置后端。改为单一 milestone——**M1 Runtime / M2 Frontend / M3 Unified Freeze**，最终统一 security + E2E freeze。详细 Pivot 见 [docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md) §19。

**产品目标（已达成）**：用户配置 GLM / Qwen / Kimi Key → 每个 Provider 选择模型 → 聊天顶部切换 Provider/Model → 每个 Session 独立保存 → Regenerate 使用当前选择 → 重启后恢复——且 Key 不进入 SQLite 明文 / 日志 / 消息 / 事件 / 导出。

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

### M2 — Frontend Switching（✅ COMPLETE / FROZEN）

最简前端：两个组件 + 一个 store。

- `stores/providerStore.ts`（不膨胀 chatStore）— ✅ FROZEN @ `313f28b`
- `components/provider/ProviderSelector.vue`（顶部切换器；运行时禁用并提示 `Generating...`）— ✅ FROZEN @ `c0792e1`
- `components/provider/ProviderSettingsModal.vue`（每个 Provider 一张卡：API Key + Model ID + status；支持 GLM / Qwen / Kimi）— ✅ FROZEN @ `0626ac4`
- Session binding restore（刷新后保留选择）— ✅ FROZEN @ `c0792e1`
- Prompt 运行时禁用 selector — ✅ FROZEN @ `c0792e1`

**M2-4 Integration Validation**：✅ COMPLETE / FROZEN @ `f9dfc1c`（Commit A `926011f` BLOCKED 证据 → M2-F1 `dcf45ce` UI Header 修复 → Commit C `f9dfc1c` archive；P1-E-DEFECT-001 / 002 RESOLVED）。

**M2 显式不包含**：`ProviderProfileList` / `CredentialForm` / `ModelPicker` / `ProviderStatusBadge` / `ProviderHealthPanel` / `ModelCatalogModal`（全部并入 Settings Modal）；Custom Provider / Custom Base URL 入口；远程模型搜索；模型能力展示。

**模型输入策略**：少量静态建议 + 自由填写 `model_id`（不调用 `/v1/models`）。

### M3 — Unified Freeze（✅ COMPLETE / FROZEN，docs-only）

最终验收 + 安全 + 归档（合并原 P1-E5 + E2-4）。

**M3 实际范围**：docs-only 阶段。不重新实现、不重新设计、不扩展。仅做：

- 统一收口 Credential → ProviderProfile → SessionModelBinding → RequestProviderSelection → ProviderAdapter → Prompt / Regenerate → AssistantMessage / Revision → Frontend Settings / Selector / Reload 为正式冻结版本
- 同步 STATUS / TODO / ROADMAP / CHANGELOG / memory 状态文档
- 新增最终冻结文档 `docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md`

**验收清单（M1/M2 已交付，M3 仅归档）**：

1. ✅ GLM / Qwen / Kimi Profile 配置（M1-2 + M2-2）
2. ✅ Key 不出现在 API response / WS event / export markdown（M2-4 §8 marker 扫描 0 leak）
3. ✅ 一键切换 Profile / Model（M2-3）
4. ✅ Session A/B 使用不同 Provider（M2-4 §4.3 场景 B + backend test_provider_runtime_binding）
5. ✅ **server restart** 后 Profile + Binding 恢复（M1-7 + backend restart tests）
6. ✅ session-only Key restart 后 → `needs_key`（M1-7 + backend Credential lifecycle）
7. ✅ env reference Key restart 后 → 重新解析（M1-7 + backend env resolution）
8. ✅ 模型切换只影响下一请求（M1-4 RequestProviderSelection 快照）
9. ✅ Regenerate 使用当前 Session 当前模型（M1-6 + backend regenerate_provider_runtime_switching）
10. ✅ 删除 Key 后 Profile → `needs_key`（backend profile status 派生）
11. ✅ invalid Key 安全报错（M2-F1 safeErrorDetail；错误信息不含 Key / Authorization）
12. ✅ GLM / Qwen / Kimi 真实流式回答 + tool_use 正常（M1-1 ~ M1-7 + backend 144 定向）
13. ✅ 切换 Provider 后失败不污染下一请求（M1-4 finally close + 恢复旧 Provider）
14. ✅ Request 启动后 provider/model 不可变（M1-4 frozen dataclass）
15. ✅ Core Runtime diff = 0（M1-7 AST 静态约束）
16. ✅ API / SQLite / log / WS / export 无 Key（M2-4 §8 + §9）
17. ✅ 错误信息不含 Authorization / 内部 endpoint（M2-F1 + M1-1 safe error mapping）
18. ✅ Playwright E2E 全 PASS（M2-4 §11 连续两次 37/37）

**最终基线**：261 vitest + 2585 pytest（+ 1 skipped + 14 deselected）+ 144 backend 定向 + 37/37 E2E × 2 + 0 marker leak + 0 external host + 0 Anthropic UI + bundle 169.23 KB JS / 56.49 KB gzip / 46.11 KB CSS + ruff clean。

**M3 提交**：`docs: freeze P1-E multi-provider switching`（docs-only，production / test / dependency / schema diff = 0）。

**merge / tag**：⛔ NOT AUTHORED（M3 不自动执行；等待用户独立授权）。

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

## P2-R — Knowledge / RAG Subsystem（🟡 IN PROGRESS）

本地知识库 RAG 系统：PDF → Canonical Markdown → heading-aware chunk → SQLite FTS5 → Session-scoped Library ACL → `search_knowledge` AgentTool。

主设计冻结文档：[docs/design/p2-r0-rag-contract.md](docs/design/p2-r0-rag-contract.md) · 决策表：[docs/design/p2-r0-decisions-log.md](docs/design/p2-r0-decisions-log.md) · 旧边界解除：[docs/design/p2-r0-legacy-limit-audit.md](docs/design/p2-r0-legacy-limit-audit.md)。

| Milestone | 状态 | 依赖 | 产出 | 验证 |
|---|---|---|---|---|
| **R0 Contract Audit** | ✅ FROZEN | — | 4 docs + 7 处旧标记解除 + amendment-1（R1 范围重划） | [docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md](docs/validation/p2-r0/P2_R0_CONTRACT_AUDIT.md) 7 条 checklist |
| **R1 Library Foundation** | ✅ FROZEN | R0 | 5 表 DDL + migration + `KnowledgeFileStore`（atomic write + fsync + path containment）+ Library/Document/Binding metadata Store/Service + Library CRUD REST API + Session Binding REST API + restart 恢复 | 表存在 / migration 幂等 / Session A/B 隔离 / 删除补偿 / 路径安全 / **0 PDF 依赖**（详见 [docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md](docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md)） |
| **R2-0 PDF Parser License Gate** | ✅ FROZEN | R1 | docs-only：选定 pypdf 6.14.2 (BSD-3-Clause) 为 R2 MVP；marker (OpenRAIL-M 模型) + PyMuPDF (AGPL) 拒绝；冻结 PdfParser Adapter 接口 | [p2-r2-0-pdf-parser-license-gate.md](docs/design/p2-r2-0-pdf-parser-license-gate.md) 29/29 exit gate PASS |
| **R2-A pypdf Parser Adapter** | ✅ FROZEN | R2-0 | pypdf `[rag]` extra + PdfParser Protocol + PypdfParser Adapter (lazy import) + 10 safe error codes + 66 tests + offline fixtures | [P2_R2_A_PARSER_ADAPTER.md](docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md) 47/47 exit gate PASS / 2768 backend + 267 frontend / 0 regression |
| **R2-B Canonical Markdown Builder** | ✅ FROZEN @ `eb193b2` + Archive Closure RATIFIED @ 2026-08-03 | R2-A | P2-R0 Amendment 2（用户 AskUserQuestion 显式授权；User decision: A — RATIFIED；frontmatter 字段集 + needs_ocr 阈值精化）+ pdf_quality 评估 + heading 启发式 + Canonical MD frontmatter + 固定 page marker + KnowledgeFileStore 原子写入（**不接 HTTP API**） | [P2_R2_B_CANONICAL_MARKDOWN.md](docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md) 60/60 exit gate PASS / [P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md](docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md) §7 RATIFIED |
| **R2-C0 Ingestion Contract** | ✅ FROZEN @ <this commit>（docs-only） | R2-B | 38 项决策冻结 + 4 子阶段规划 + Schema Gap 判定 | [P2_R2_C0_INGESTION_CONTRACT_AUDIT.md](docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md) 20/20 exit gate PASS / Schema Amendment NOT REQUIRED / R0 §8.1 sync→async minimal refine |
| **R2-C1 Store Extensions + Orchestrator** | ✅ FROZEN @ `cde0e0b` + freeze commit | R2-C0 | IngestionStore（atomic claim / retry / recovery）+ IngestionOrchestrator（Parser+Quality+Builder+Persistence composition）；terminal = `normalizing` (per C0 §35.5 correction) | [P2_R2_C1_INGESTION_ORCHESTRATOR.md](docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md) 58/58 exit gate PASS / 2986 backend（delta 58 = targeted；**CORRECTED @ P2-R2-D-A**；原报告 delta 66 由 R2-B freeze 时点误报 2920 引起）/ 267 frontend / 0 regression |
| **R2-C2 Bounded Worker + Recovery** | ✅ FROZEN @ `115136a` + freeze commit | R2-C1 | IngestionWorkerManager（asyncio.Queue wake + 30s poll + conditional fail）+ FastAPI lifespan 接线 + startup recovery | [P2_R2_C2_WORKER_RECOVERY.md](docs/validation/p2-r2/P2_R2_C2_WORKER_RECOVERY.md) 37/37 exit gate PASS / 3023 backend (delta 37) / 267 frontend / 0 regression |
| **R2-C3 Upload / Status / Retry / Markdown API** | ✅ FROZEN @ `c6a19ec` + freeze commit | R2-C2 | UploadService（streaming + SHA + staging + atomic rename + dup 409）+ 4 endpoints + delete guards | [P2_R2_C3_INGESTION_APIS.md](docs/validation/p2-r2/P2_R2_C3_INGESTION_APIS.md) 43/43 exit gate (1 skip) PASS / 3066 backend (delta 43) / 267 frontend / 0 regression |
| **R2-C4 Integration Validation + Freeze** | ✅ FROZEN @ `<this commit>` | R2-C3 | 47 integration tests: E2E / failure / concurrency / restart / security | [P2_R2_C4_INTEGRATION_FREEZE.md](docs/validation/p2-r2/P2_R2_C4_INTEGRATION_FREEZE.md) production diff=0 / 0 functional regression |
| **R2-D-A Test Count Reconciliation** | ✅ COMPLETE / FROZEN @ `287edb9` | R2-C4-R | A/B worktree + 集合分析；historical 8-test discrepancy ✅ RECONCILED；root cause = R2-B freeze 时点误报 2920（实测 2928） | [P2_R2_D_TEST_COUNT_RECONCILIATION.md](docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md) selected delta 160 = targeted / 0 node-ID-level 差异 / 0 functional regression |
| **R2-D-Fix Ruff Cleanup** | ✅ COMPLETE / FROZEN @ `a27494c` | R2-D-A | C4-R Fix-1 unused `import importlib` cleanup；1 line deletion；行为零变化 | ruff check = 0 error / 完整 Backend ×2 fresh process = 3113/3/14/0 failed |
| **R2-D-B Final PDF Pipeline Freeze** | ✅ COMPLETE / FROZEN @ `<this commit>` | R2-D-Fix | docs-only；R1 targeted count ✅ RECONCILED（118 selected = 117 passed + 1 skipped）；10 处 R1 历史误写已修；C4-R Ruff archival correction；P2-R2 全链归档关闭 | [P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md](docs/validation/p2-r2/P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md) 45/45 exit gate PASS / 0 production diff / 0 test diff |
| **R2 (整体) Ingestion Pipeline** | ✅ COMPLETE / FROZEN | R2-0 | R2-A / R2-B / R2-C / R2-D-A / R2-D-Fix / R2-D-B 全部 COMPLETE；P2-R2 关闭 | P2-R3 APPROVED TO START |
| **R3-A Chunk Contract + Chunker** | ✅ COMPLETE / FROZEN @ `677fe33` | R2-D-B | `chunker.py` + `KnowledgeChunk` DTO + `HeadingAwareChunker` pure function；deterministic chunk_id；heading/page/frontmatter/code-fence 解析；44/44 unit tests | [p2-r3-chunker-contract.md](docs/design/p2-r3-chunker-contract.md) CHUNK_TARGET=1200 / MAX=1800 / OVERLAP=160 / 0 production dep / 0 R2 module touched |
| **R3-A-R Reliability Closure** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-A | docs-only forensic；causality 排除：含/排除 chunker 同样 Windows sequential-suite flake pattern；chunker→victim / victim→chunker 全 PASS；chunker 零资源操作；final ×2 = 3157/0 failed | [P2_R3_A_R_RELIABILITY_CLOSURE.md](docs/validation/p2-r3/P2_R3_A_R_RELIABILITY_CLOSURE.md) 10/10 exit gate PASS |
| **R3-B1 Schema Migration v1→v2** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-A-R | `KNOWLEDGE_SCHEMA_VERSION=2`；knowledge_chunks 扩展 char_count+content_sha256；knowledge_chunks_fts FTS5 virtual table（unicode61 remove_diacritics 2）；insert_chunk 兼容 v2；18/18 migration tests | [p2-r3-b-schema-migration-contract.md](docs/design/p2-r3-b-schema-migration-contract.md) full Backend 3175/0 failed / R1 regression PASS / 0 frozen module touched |
| **R3-B2 Chunk/FTS Store** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-B1 | chunk_store.py：replace_document_chunks / delete_document_chunks / rebuild_fts / verify_fts_integrity / search_chunks_fts；safe literal FTS query compiler；BM25 heading-weighted (3.0/1.0) + stable tie-break；ready-only filter；library filter；chunking-only state guard；FTS cleanup 集成到 delete_document_hard / delete_library_hard | 66/66 tests PASS / full Backend 3241/0 failed / R2 frozen 模块未触动 |
| **R3-B3 Validation Freeze** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-B2 | docs-only；validation doc（58 节）；STATUS/TODO/ROADMAP 同步；73/73 exit gate | [P2_R3_B_SCHEMA_FTS5_VALIDATION.md](docs/validation/p2-r3/P2_R3_B_SCHEMA_FTS5_VALIDATION.md) 84/84 R3-B tests / 3241/0 failed / 0 production behavior expansion |
| **R3-B Schema + SQLite FTS5 (整体)** | ✅ COMPLETE / FROZEN | R3-A | schema v1→v2 + FTS5 + chunk_store + safe query compiler；R3-C ready to start | f3401ab + db03be7 + this commit |
| **R3-C1 IndexingStore** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-B | indexing_store.py：atomic claim + state transitions + recovery primitive；SQLite conditional write；RecoveryResult DTO；schema diff=0 | 37/37 tests PASS / full Backend 3278/0 failed / R3-A/B/R2 frozen 未触动 |
| **R3-C2 IndexingOrchestrator** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-C1 | indexing_orchestrator.py：process_document 显式调用；T1-T6 normalizing→chunking→indexing→ready；Markdown read + source_sha256 完整性 + Chunker/ChunkStore 复用 + FTS integrity gate；IndexingResult DTO；8 safe error codes；failure compensation | 21/21 tests PASS / full Backend 3299/0 failed @ Run #3 / R3-A/B/C1/R2 frozen 未触动 |
| **R3-C3 Validation Freeze** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-C2 | docs-only；validation doc（61 节）；STATUS/TODO/ROADMAP 同步；75/75 exit gate | [P2_R3_C_INDEXING_RUNTIME_VALIDATION.md](docs/validation/p2-r3/P2_R3_C_INDEXING_RUNTIME_VALIDATION.md) 58 R3-C tests / 3299/0 failed / 0 production behavior expansion |
| **R3-C Indexing Runtime (整体)** | ✅ COMPLETE / FROZEN | R3-B | IndexingStore + IndexingOrchestrator（normalizing→chunking→indexing→ready T1-T6 + recovery + failure compensation）；R3-D ready to start | dbf5593 + 146cf35 + this commit |
| **R3-D1 IndexingWorkerManager** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-C | indexing_worker.py：bounded single-concurrency；poll=2s；grace=30s；startup recovery wiring；immediate scan；backlog drain；failure isolation；asyncio.Event wake+stop | 19/19 tests / full Backend ×2 = 3318/0 failed consecutive / R3-A/B/C/R2 frozen 未触动 |
| **R3-D2 Lifecycle + Delete Guards** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-D1 | state.py + app.py lifespan wiring（IndexingWorkerManager 构造+start+stop）+ api.py chunking/indexing delete guards（409 document_indexing_active / library_indexing_active）+ knowledge disabled mode | 16/16 tests PASS / full Backend Run #2 = 3334/0 failed / R3-A/B/C/R2 frozen 未触动 |
| **R3-D3 Validation Freeze** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-D2 | docs-only；validation doc（69 节）；STATUS/TODO/ROADMAP 同步；95/96 exit gate | [P2_R3_D_INDEX_WORKER_VALIDATION.md](docs/validation/p2-r3/P2_R3_D_INDEX_WORKER_VALIDATION.md) 35 R3-D tests / 3334/0 failed / 0 production behavior expansion |
| **R3-D Bounded Index Worker (整体)** | ✅ COMPLETE / FROZEN | R3-C | IndexingWorkerManager + lifespan wiring + delete guards；R3-E ready to start | 6532441 + d578694 + this commit |
| **R3-D-R Reliability + Boundary Closure** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3-D3 | docs + minimal amendment：×2 consecutive 0-failed met；R3-D causality rejected；P2-R3-C-D Bridge Amendment Option B (ChunkStore in-transaction variant) applied；Worker ZERO persistence SQL | [P2_R3_D_R_RELIABILITY_AND_BOUNDARY_CLOSURE.md](docs/validation/p2-r3/P2_R3_D_R_RELIABILITY_AND_BOUNDARY_CLOSURE.md) 72/72 targeted / 3334/0 failed ×2 |
| **R3-E Final Integration Freeze** | ✅ COMPLETE / FINAL FROZEN @ `<this commit>` | R3-D-R | 23 integration tests（real upload→ready / restart / delete / lifecycle）+ R3-E-Fix frontmatter regex + validation doc；50/50 exit gate | [P2_R3_E_FINAL_INTEGRATION_FREEZE.md](docs/validation/p2-r3/P2_R3_E_FINAL_INTEGRATION_FREEZE.md) 3357/0 failed / production diff = regex fix only |
| **R3 (整体) Heading-aware Chunk + FTS5** | ✅ COMPLETE / FINAL FROZEN | R2 | R3-A→R3-E 全链 FROZEN；real PDF upload→ready pipeline verified end-to-end | R4 ready to start |
| **R4-A Retrieval + Citation Contract** | ✅ COMPLETE / FROZEN @ `<this commit>` | R3 | docs-only contract gate；真实代码审计 + 冻结 Tool/ACL/Evidence/Citation 合同；12 threats；29/29 exit gate | [P2_R4_A_CONTRACT_SECURITY_GATE.md](docs/validation/p2-r4/P2_R4_A_CONTRACT_SECURITY_GATE.md) + [contract doc](docs/design/p2-r4-search-knowledge-citation-contract.md) |
| **R4-B1 Search Service + Evidence** | ✅ COMPLETE / FROZEN @ `<this commit>` | R4-A | search_models.py + evidence.py + search_service.py：KnowledgeEvidence DTO / EvidenceRegistry turn-scoped E1/E2 / SearchKnowledgeService session ACL→ChunkStore | 29/29 tests / full Backend 3386/0 failed / R3 frozen 未触动 |
| **R4-B2 search_knowledge Tool + Agent wiring** | ✅ COMPLETE / FROZEN @ `<this commit>` | R4-B1 | search_tool.py + app.py lifespan + state.py：SearchKnowledgeTool schema=query+limit；session_id_getter + evidence_registry_getter closure；注册 when Knowledge enabled | 12/12 tool tests / full Backend 3398/0 failed / R3 frozen 未触动 |
| **R4-B3 Validation Freeze** | ✅ COMPLETE / FUNCTIONALLY FROZEN @ `0353be8` | R4-B2 | docs freeze @ 7f35696 + TOCTOU admission fix @ 0353be8；validation doc（31 节）；44/44 exit gate；EvidenceRegistry lifecycle + prompt reservation TOCTOU closed | [P2_R4_B_SESSION_SCOPED_SEARCH_KNOWLEDGE.md](docs/validation/p2-r4/P2_R4_B_SESSION_SCOPED_SEARCH_KNOWLEDGE.md) 45 targeted tests / 3402/0 failed ×2 |
| **R4-B (整体) Session-scoped search_knowledge** | ✅ COMPLETE / FUNCTIONALLY FROZEN @ `0353be8` | R4-A | SearchService + EvidenceRegistry + Tool + lifecycle fix + TOCTOU fix；R4-C ready to start | 04c386b + b8dd49a + 1c1d4d4 + 7f35696 + 0353be8 |
| **R4-C1 Citation 模块** | ✅ COMPLETE / FROZEN @ `<this commit>` | R4-B | citations.py: CitationParser/Processor/Renderer；strict `[cite:E1]` regex；first-use numbering；invalid evidence remove+warning；source footer `filename · p.N` | 32/32 unit tests / full Backend 3434/0 failed / R4-B frozen 未触动 |
| **R4-C2 system prompt + Assistant finalize** | ✅ COMPLETE / FROZEN @ `<this commit>` | R4-C1 | system_prompt.py _KNOWLEDGE_HINT + knowledge_enabled；harness.py build_default_system_prompt(knowledge_enabled=)；app.py _apply_citation_transform | 9/9 integration tests / full Backend 3443/0 failed / R4-C1 frozen 未触动 |
| **R4-C3 Validation Freeze** | ✅ COMPLETE / FROZEN @ `<this commit>` | R4-C2 | docs-only；validation doc（23 节）；27/27 exit gate；[cite:E1]→[1]+Sources footer pipeline verified | [P2_R4_C_CITATION_AGENT_INTEGRATION.md](docs/validation/p2-r4/P2_R4_C_CITATION_AGENT_INTEGRATION.md) 41 targeted tests / 3443/0 failed ×2 |
| **R4-C (整体) Citation + Agent Integration** | ✅ COMPLETE / FROZEN | R4-B | CitationParser/Processor/Renderer + system prompt + Assistant finalize；R4-D ready to start | f0c407b + 6006dca + this commit |
| **R4-D Final RAG Integration Freeze** | ✅ COMPLETE / FINAL FROZEN @ `<this commit>` | R4-C | 6 E2E tests (upload→ready→search→evidence→citation pipeline) + full Backend ×2 = 3455/0 failed + validation doc；19/19 exit gate | [P2_R4_D_FINAL_RAG_FREEZE.md](docs/validation/p2-r4/P2_R4_D_FINAL_RAG_FREEZE.md) Agent-facing Knowledge RAG ✅ AVAILABLE |
| **R4 (整体) Session-scoped search_knowledge + Citation** | ✅ COMPLETE / FINAL FROZEN | R3 | R4-A→R4-D 全链 FROZEN；real PDF→answer→citation E2E verified | 6e0089f → 0353be8 → 9f47ccb → this commit |
| **R4-C Citation + Agent Integration** | ⛔ BLOCKED BY R4-B | R4-B | CitationParser/Validator/Renderer + system prompt + Assistant finalize | 待 R4-C 启动 |
| **R4-D Final RAG Integration Freeze** | ⛔ BLOCKED BY R4-C | R4-C | full E2E RAG validation + ACL/deletion/leakage/reliability tests + final freeze | 待 R4-D 启动 |
| **R3-C3 Validation Freeze** | ⛔ BLOCKED BY R3-C2 | R3-C2 | regression + validation docs + STATUS/TODO/ROADMAP | 待 R3-C2 完成 |
| **R3-B3 Validation Freeze** | ⛔ BLOCKED BY R3-B2 | R3-B2 | regression + validation docs + STATUS/TODO/ROADMAP | 待 R3-B2 完成 |
| **R3 Retrieval (overall)** | 🟡 IN PROGRESS (R3-A done) | R2 | `search_knowledge` tool + **pure FTS5 BM25**（无向量；D1/D2/D3 DECLINED @ 2026-08-03）+ heading-aware chunker + top_k 排序 + 引用 evidence（chunk.page_start / page_end 即 page marker 引用，per R2-B `<!-- page:N -->`） | query 召回 / tool 不暴露 session_id（AST 校验） / 0 vector dependency |
| **R4 Session Library ACL** | ⛔ BLOCKED BY R3 | R3 | `session_knowledge_libraries` CRUD + 后端 allowlist 过滤 | 未授权 library 不出现 / A/B session 越权测试 |
| **R5 Web API + UI** | ✅ COMPLETE / FROZEN (R5-D done) | R4 | library / document / search REST + 前端管理面板 | E2E upload→ingest→search 全链路 |
| **R5-A Web API + UI Contract Audit** | ✅ COMPLETE / FROZEN @ `1c1f519` | R4 | docs-only audit + freeze | [P2_R5_A_WEB_CONTRACT.md](docs/validation/p2-r5/P2_R5_A_WEB_CONTRACT.md) 16/16 exit gate PASS / 14 existing REST reused / 1 new Search REST / production diff=0 |
| **R5-B Knowledge REST API** | ✅ COMPLETE / FROZEN @ `d099185` + `134094a` | R5-A | Search REST + Library/Document gap fill (R5-B1 skipped) | [P2_R5_B_REST_API.md](docs/validation/p2-r5/P2_R5_B_REST_API.md) 35 targeted tests / 3492 full backend / 0 regression / schema diff=0 / Ruff PASS |
| **R5-C Frontend Knowledge Manager** | ✅ COMPLETE / FROZEN @ working tree (pending commit on `041801c`) | R5-B | KnowledgeManagerModal + Library/Document/Upload/Search/Binding UI | [P2_R5_C_FRONTEND.md](docs/validation/p2-r5/P2_R5_C_FRONTEND.md) 80 vitest (33 store + 47 component) / 347 total / typecheck+lint+build PASS / frontend-only / backend diff=0 |
| **R5-D Final E2E Freeze** | ✅ COMPLETE / FROZEN @ working tree (pending commit on `f2750e3`) | R5-C | Upload→Ingest→Search→Binding E2E + Agent continuity | [P2_R5_D_FINAL_E2E_FREEZE.md](docs/validation/p2-r5/P2_R5_D_FINAL_E2E_FREEZE.md) 19 E2E tests / Full Backend 3511/0 / production diff=0 |
| **R6 Freeze + Validation** | ⛔ BLOCKED BY R5 | R5 | validation report + security freeze + tag `v0.0.XX-knowledge-rag` | 全 pytest + E2E + ruff + 0 Core Runtime 回归 |

**关键冻结决策**（详见 decisions-log）：

- **PDF parser = marker**（AGPL-3.0；force_ocr=False 保持数字 PDF only 边界；**R2** 集成前需法务确认 license 兼容性，fallback = pypdf）—— **[AMENDED 2026-07-30]** 从 R1 推迟到 R2，详见 [amendment-1](docs/design/p2-r0-amendment-1.md)
- **SQLite = 独立 `knowledge.db`** + 独立 aiosqlite 连接；library_id 作逻辑外键
- **第一版同步处理** upload，30s 阈值 + PDF ≤ 20 页强制限制；R2 引入 BackgroundTask 时不改 schema
- **Chunk 默认 max_chars=1200 / overlap=150**，heading-aware 切分
- **`search_knowledge(query, top_k=5)`** 不接受 library_id / session_id / file_path；后端从 session_id_getter 求 allowlist
- **R3 检索 = pure FTS5 BM25**（per [decisions-log §3.1](docs/design/p2-r0-decisions-log.md) D1/D2/D3 DECLINED @ 2026-08-03）：永久不引入 embedding / vector / reranker / hybrid；evidence 中 chunk.page_start / page_end 即 page marker 引用（R2-B `<!-- page:N -->` 已实现）

**显式不包含（P2-R 全程）**：

- OCR / 主动开启 marker OCR（扫描 PDF 进 `status=needs_ocr` 终态，不自动重试）
- Long-term user memory / 跨 Session 用户偏好 / 用户画像
- **Embedding provider / Vector DB / Reranker / Hybrid retrieval / Re-embedding**（**永久不做 @ 2026-08-03**，per [decisions-log §3.1](docs/design/p2-r0-decisions-log.md)；用户决策"PDF→MD 即可，让 LLM 直读 MD，不再进行向量化"；R3 = pure FTS5 BM25）
- RBAC / OAuth / 企业级多租户（本地登录与账号工作区隔离已在 P2-AUTH 实现）
- 长期后台任务调度（与 ROADMAP "不做" 列表一致）

---

## P2 — Web Agent Product Enhancements（🟡 IN PROGRESS）

### P2-AUTH — Local Login + Account Workspace Isolation（✅ IMPLEMENTED / LOCAL BASELINE，2026-08-15）

本地登录与账号数据隔离已交付：未登录首屏、HttpOnly + SameSite=Strict Cookie、服务端登录 Session、PBKDF2-SHA256 随机盐密码哈希、空库 bootstrap `admin`、退出登录和失败尝试限流。登录 Session 绑定后端运行周期：运行期间刷新/重开无需重输，网关重启后撤销旧 Session 并强制重新认证，账号和工作区数据继续保留。

每个账号被路由到独立 FastAPI 工作区，拥有专属 Session/上传、Skills、MCP、Knowledge、Provider/Credential Settings 数据库与文件目录；HTTP API 和 WebSocket 均要求有效登录 Cookie。

**边界**：仍是 localhost-only 应用；不提供 Users 管理界面、RBAC、OAuth、企业级多租户或公网部署。

**验证**：Frontend 358/358 + typecheck/lint/build；Backend auth 8/8（新增退出再登录后保留历史消息与 `AGENT.md`）以及 auth+Provider compatibility 36/36；Full Backend 3514 passed，唯一既有 ingestion worker 时序用例首轮抖动失败后独立连续复跑 3/3 PASS；Ruff/mypy PASS。

### P2-SESSION-WORKSPACE — Conversation + Folder（✅ IMPLEMENTED / LOCAL BASELINE，2026-08-15）

每个 Session 现在同时拥有持久对话历史和独立 managed folder。应用启动会为已有 Session 补齐目录，新建 Session 会在返回前初始化目录与唯一根 `AGENT.md`；已有指令内容绝不覆盖，删除 Session 继续级联删除整个目录。退出再登录或重启网关只撤销认证 Session，不会删除对话、文件或 `AGENT.md`。

用户上传与 Agent 生成文件共用 VirtualFileStore、FileRef metadata、单文件/Session 配额和跨 Session 边界。FileRef 通过 `logical_path` 表达虚拟目录且 API 不暴露物理路径；`write_file(filename, content, folder?)` 可在 Session 内创建 UTF-8 文本。`list_files/view_file/write_file` 优先绑定请求 Session，避免 UI 默认 Session 与请求 sid 不一致时串读写。

前端聊天标题栏提供 `Folder N` 文件树，可展开逻辑目录、查看、下载、删除与刷新，并直接编辑受保护的根 `AGENT.md`。每轮执行前读取当前内容并追加到 system prompt，采用大小限制与 SHA-256 乐观并发检查；保存后下一轮生效。Agent 请求 terminal 后自动刷新文件树。验证：Backend related 211/211；Frontend 362/362；typecheck/lint/build/Ruff/mypy PASS。

### P2-CHECKPOINTER — Slash Command + Session Memory（✅ IMPLEMENTED / LOCAL BASELINE，2026-08-15）

首个 slash command `/checkpointer` 已交付：命令目录驱动前端 `/` 菜单；后端以 managed async request 使用当前 Session Provider 总结 canonical messages，累计保存到唯一根 `Memory.md`，保存成功后才清空当前消息。Provider/文件失败不改原消息，数据库清空失败补偿回滚文件；source hash 支持重复提交幂等恢复。后续 Prompt/Regenerate 自动加载最多 32 KiB 记忆，明确作为不可信事实而非指令。Folder 文件树允许用户打开、查看和修改 `Memory.md`，保存使用 SHA-256 乐观锁，普通文件仍不可通过该接口编辑。范围只限当前 Session，不提供跨 Session 用户画像。验证：Backend extended 204/204（full baseline 3539 passed / 3 skipped / 14 deselected）；Frontend full 368/368；typecheck/lint/build/changed-file Ruff PASS。

按优先级排序——独立设计、独立测试、独立冻结。**不**要求按字母顺序执行。

### P2-A — Session URL Routing + Full Reload Recovery（✅ IMPLEMENTED / LOCAL BASELINE，2026-08-16）

Session 激活现同步为 `/chat/{session_id}`，浏览器前进/后退、直接打开和整页刷新都通过同一 App 级协调器恢复。恢复范围包括 persisted messages、`AGENT.md`、`Memory.md`、文件树、Provider binding，以及运行中的 Prompt/Regenerate UI 状态。

刷新时先用当前账号的 Session 列表验证路由。非法、已删除或不属于当前用户的 ID 不会被用于 Session API 请求，而会替换为有效 Session。运行中请求通过 request-scoped event replay 补齐刷新前事件，并与新 WebSocket live 事件按 sequence 合并、按 event ID 去重；terminal polling 受 Session 所有权保护，旧 Session 的迟到结果不会污染新窗口。

登出先清除旧路由并重置 Session/chat/files/skills/MCP/provider 前端状态；重新登录另一个账号时只加载该账号工作区。FastAPI 与认证网关支持 `/chat` 和 `/chat/{path}` 的 SPA 直达，由登录后的前端统一校验与规范化。

**验证**：Frontend 382/382 + typecheck/lint/build；Backend route/auth 42/42；Full Backend 3563 passed / 3 skipped / 15 deselected（83.66% coverage）；完整 Chromium refresh E2E 5/5（URL/history、历史+AGENT+Memory+树、active Prompt/Regenerate、invalid/deleted、admin→alice）；应用内浏览器直达刷新无 console error。

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

### P1-D3 — PDF Text Extraction ✅ RESTARTED via P2-R（2026-07-27）

P1-D3 的原始范围（PDF adapter 边界 + FileRef metadata 字段）已被 P2-R 系列吸收并扩展：

- PDF parser 改用 **marker**（替代原计划的 `tools/view_file.py` 改造路径）
- 存储改用独立 **`KnowledgeFileStore`**（不复用 FileRef / VirtualFileStore）
- 边界从 Tool 层上移到 Ingestion Pipeline 层

详见上方 §P2-R 章节 + [docs/design/p2-r0-rag-contract.md](docs/design/p2-r0-rag-contract.md)。

原重启条件（保留作历史记录）：

- ✅ P1-E 已完成（FROZEN）；P1-F 不再是前置——P2-R 自带 Canonical Markdown 处理
- ✅ PDF adapter 边界已重评——从 Tool 层上移到 Ingestion Pipeline 层
- ✅ 不复用 FileRef；新建 `KnowledgeFileStore` + `knowledge_documents` 表

### PDF / Vector RAG ✅ RESTARTED via P2-R

PDF / Vector RAG 已在 P2-R 系列正式重启——见上方 §P2-R 章节。第一版范围：marker + heading-aware chunk + SQLite FTS5 + Session-scoped Library ACL + `search_knowledge` AgentTool。

---

## 明确不做（长期）

- OCR / Image understanding / 视觉理解（marker 配置 `force_ocr=False`；扫描 PDF 进 `status=needs_ocr` 终态，不自动重试）
- PDF 表单 / 注释 / 嵌入对象
- Long-term user memory / 跨 Session 用户偏好 / 用户画像（区别于 RAG——RAG 已在 P2-R 系列重启）
- Multi-Agent 编排
- CLI / RPC mode
- OAuth / RBAC / 企业级多租户 / 企业 secret vault（本地登录与账号工作区隔离已完成）
- 公网部署 / 横向扩展
- MCP marketplace / Skill marketplace
- Skill 在线编辑 / 跨项目共享 / 热加载
- 本地文件系统操作工具（bash / read / write / edit / grep / find / ls）
- 自动 provider fallback / 模型负载均衡
- Markdown 在线编辑 / 保存 / diff / 协同
- 长期后台任务调度

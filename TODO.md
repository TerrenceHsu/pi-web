# Current TODO

> Current status: [STATUS.md](STATUS.md)
> Future roadmap: [ROADMAP.md](ROADMAP.md)
> Released versions: [CHANGELOG.md](CHANGELOG.md)

## Current phase

**P1-E Multi-Provider Switching — ✅ COMPLETE / FROZEN**（M1 Runtime + M2 Frontend + M3 Unified Freeze）。

- 路线：[ROADMAP.md](ROADMAP.md) § P1-E
- 状态：[STATUS.md](STATUS.md) — P1-E 全链路 ✅ COMPLETE / FROZEN；等待用户授权 merge / tag / push
- 最终归档：[docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md](docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md)
- M3 是 docs-only 阶段——production / test / dependency / schema diff = 0

## P1-E1 ✅ COMPLETE（MERGED + TAGGED）

P1-E1 Secure Credentials 已正式交付：

- **master merge commit**：`de05c66`（no-ff；保留 22 commit 阶段性历史）
- **release tag**：`v0.0.27-secure-credentials`
- **post-merge baseline**：1833 passed / 14 deselected / 0 failed；37/37 E2E；ruff clean；working tree clean
- **Push status**：⛔ 未授权（仅本地）

子阶段全部 FROZEN：

- P1-E1-1 Secret Primitives ✅
- P1-E1-2 Credential Repository ✅（含 2.1 并发加固 / 2.2 enum 校验）
- P1-E1-3A Router / Service ✅
- P1-E1-3B Validation Strategy ✅（B1 Anthropic / B2 wiring）
- P1-E1-4 Web API Design ✅ APPROVED
- P1-E1-4A Composition Root ✅
- P1-E1-4B REST API ✅
- P1-E1-5A Security Audit ✅（78 控制 / 5 finding）
- P1-E1-5B Security Hardening ✅（5 finding/gap 全 RESOLVED/CLOSED）
- P1-E1-5C Final Regression ✅

## P1-E2 Backend Foundation（✅ COMPLETE，不单独 merge / tag）

P1-E2 配置后端已 frozen，作为 M1/M2/M3 的持久化基础。**不再扩展配置后端**——E2-4 独立 Security Freeze cancelled，并入 M3 Unified Freeze。

- E2-1 Schema + Store ✅ FROZEN @ `89fabfd`
- E2-2 Service + Static Model Options ✅ FROZEN @ `a2c7932`
- E2-3A Session Creation Audit ✅ FROZEN @ `9878ef9`
- E2-3 Composition + REST API + Session binding ✅ FROZEN @ `17c843d` / `9bbd0f2` / `cad7ca7`

测试基线：2063 full pytest + 2×37/37 E2E + ruff clean + 0 Core Runtime diff + 0 network + 0 secret reads。

## M1 deliverables — Multi-Provider Runtime

### M1-0：Provider Contract Audit（审计门，无生产代码）✅ FROZEN @ `be13a1f`

- [x] 审计 `providers/base.py` Adapter contract（stream / close / events）
- [x] 审计 `providers/glm.py` 流式事件格式 / tool_call 增量 / usage / finish_reason / client close 生命周期
- [x] 审计 `providers/anthropic_compat.py`（参考；不重写）
- [x] 审计 `providers/registry.py` ProviderDefinition 结构
- [x] 审计 Agent 如何持有 Provider（`harness.agent.<provider field>` 引用结构）
- [x] 审计 `_run_prompt_core` / `_run_regeneration_core` 调用入口
- [x] 输出 `docs/design/p1-e-m1-provider-runtime.md`：冻结统一 Adapter 接口

### M1-1：OpenAI-compatible Provider ✅ FROZEN @ `8daaa90`（124 tests）

- [x] `providers/openai_compat.py`：Qwen / Kimi 共用 Adapter
- [x] request / SSE stream / assistant text delta / tool calls / finish reason / usage
- [x] safe error mapping（错误信息不含 Authorization / 完整 endpoint）
- [x] client close 生命周期
- [x] 围绕现有 Provider contract 实现，不发明第二套事件模型
- [x] `feat(providers): add openai-compatible provider adapter`

### M1-2：Qwen / Kimi ProviderDefinition presets ✅ FROZEN @ `4d89c82`（64 tests）

- [x] `registry.py` 加 `qwen` preset（protocol=`openai_compatible` / `default_base_url`）
- [x] `registry.py` 加 `kimi` preset（同上）
- [x] `GET /api/provider-definitions` 返回 glm / qwen / kimi + anthropic（safe display fields only）
- [x] `feat(providers): add qwen and kimi provider presets`

### M1-3：Provider Factory ✅ FROZEN @ `a35a4ad`（64 tests）

- [x] `providers/factory.py`：唯一知道「哪个 Provider 用哪个 Adapter」的位置
- [x] GLM → 包装现有 GLMProvider
- [x] Qwen / Kimi → `OpenAICompatibleProvider`
- [x] Anthropic → 包装现有（保留兼容）
- [x] 输入 ProviderDefinition + api_key + model_id；输出 RequestProvider
- [x] `feat(providers): add provider factory`

### M1-4：Request Provider Runtime ✅ FROZEN @ `8827bd1`（80 tests / 6 files）

- [x] `web/provider_runtime.py`：`RequestProviderRuntime` + `bind_to_harness` async context manager
- [x] Session Binding → Profile → Credential → Secret → ProviderFactory → 临时绑定 `harness.agent.<provider>`
- [x] finally 恢复旧 Provider + close request client
- [x] 复用现有单 active request lock（不创建第二把锁）
- [x] `RequestProviderSelection(profile_id, provider_id, model_id, selection_source)` 不可变快照（`credential_id repr=False`）
- [x] `feat(web): add request-scoped provider runtime`

### M1-5：Prompt Integration ✅ FROZEN @ `f116ddd`（47 tests / 5 files）

- [x] Composition root：`credential_runtime + provider_config_runtime` 都启动时构造 `RequestProviderRuntime`
- [x] `_execute_prompt` 唯一接入点：`AsyncExitStack` + `bind_to_harness`
- [x] 无 Binding → `selection=None` → legacy client 兼容路径
- [x] 4 固定错误码：`provider_profile_{unavailable,disabled}` / `provider_credential_unavailable` / `provider_initialization_failed`
- [x] `CancelledError`（BaseException）原样传播——不被 `except Exception` 捕获
- [x] 不写 `context.metadata["provider_selection"]`
- [x] 2×37/37 Playwright E2E
- [x] `feat(web): bind prompt execution to session provider`

### M1-6：Regenerate Validation ✅ FROZEN @ `06ecb80`（67 tests / 5 files）

- [x] 验证 Regenerate 通过 `_execute_prompt` 走同一 `bind_to_harness` 路径（M1-5 唯一接入点已覆盖）
- [x] Regenerate 用当前 Session 当前模型（不动 D2 schema；revision `content_json` 自带 model 信息）
- [x] 验证 revision `message_id` 不变（in-place）
- [x] 原历史模型不决定本次 Regenerate
- [x] 失败恢复 revision 状态
- [x] 无双重 bind（Regenerate 不与 Prompt 嵌套）
- [x] validation-only——0 production diff（AST 测试锁定 lexical-body 接线只在 `_execute_prompt`）
- [x] `test(web): validate regenerate provider selection`

### M1-7：Runtime Final Validation ✅ COMPLETE / FROZEN（本提交）

- [x] GLM / Qwen / Kimi 真实流式回答（mock contract）
- [x] 工具调用保持正常（Tool loop 不变量：resolve / secret / factory / adapter 各一次）
- [x] 切换只影响下次请求（请求级不可变快照）
- [x] 失败不污染下一请求（错误隔离）
- [x] Core Runtime diff = 0（GLM 包装，不重写；AST 静态约束）
- [x] 跨 Session 隔离（single harness，顺序执行不污染）
- [x] 默认 Profile 全链路 + 显式切换全链路
- [x] app restart（env / keyring / session-only Credential）
- [x] Credential 删除 / 替换 / env 值变化
- [x] 安全出口全审计（HTTP / SQLite main+WAL+SHM / log / exception chain / Selection repr / Adapter repr）
- [x] 静态架构约束（Core Runtime M1 diff=0；Provider Runtime 接线只在 `_execute_prompt`；模块依赖方向）
- [x] `test(web): freeze multi-provider runtime`（M1-7 production diff = 0；E2E 由主仓库 `pi-py` 验证）

## M2 deliverables — Frontend Switching（✅ COMPLETE / FROZEN）

- [x] `stores/providerStore.ts` — ✅ FROZEN @ `313f28b`
- [x] `components/provider/ProviderSelector.vue`（顶部切换器；运行时禁用并提示 `Generating...`）— ✅ FROZEN @ `c0792e1`
- [x] `components/provider/ProviderSettingsModal.vue`（每个 Provider 一张卡：API Key + Model ID + status）— ✅ FROZEN @ `0626ac4`
- [x] Session binding restore（刷新后保留选择）— ✅ FROZEN @ `c0792e1`
- [x] M2-4 Integration Validation — ✅ COMPLETE / FROZEN @ `f9dfc1c`（含 M2-F1 hotfix @ `dcf45ce`）

## M3 deliverables — Unified Freeze（✅ COMPLETE / FROZEN，docs-only）

- [x] Secret leak audit（API / SQLite / log / WS / export / marker）— ✅ 0 leak（M2-4 §8）
- [x] GLM / Qwen / Kimi contract tests — ✅ 包含于 144 backend 定向测试
- [x] Prompt / Regenerate / tool / streaming / Session A/B / restart — ✅ backend 144 + vitest 261
- [x] Playwright E2E（连续两次）— ✅ 37/37 × 2（M2-4 §11）
- [ ] merge / tag：`feat(providers): deliver multi-provider switching` — ⛔ NOT AUTHORED（等待用户授权；M3 不自动 merge / tag / push）

## Cross-stage frozen constraints（M1 / M2 / M3 全程约束）

- ❌ 不修改 D2 Revision schema（v2）——provider/model 从 AssistantMessage JSON 投影
- ❌ 不修改 `loop.py` / `agent.py` / `context.py` / `events.py` / `stream_events.py` / `messages.py`
- ❌ 不替换现有 `providers/glm.py` / `anthropic_compat.py` / `base.py`（GLM 包装现有；OpenAI-compatible 为新增模块）
- ❌ 不向多个第三方发 API Key 自动探测供应商（只对 Session 选中的 Provider 调用）
- ❌ 不实现自动 provider fallback / 模型负载均衡
- ❌ 不顺带重构 `web/app.py`（留 P3）
- ❌ 不在 P1-E 阶段引入 Markdown 面板（P1-F 独立阶段）
- ❌ **M1/M2/M3 内不调用任何远程模型目录 API**（`/v1/models` 等远端探测均不进入）——Messages API 调用除外
- ❌ **M1/M2/M3 内不引入 remote `ModelOption` source**（静态建议 + 用户手动填写）
- ❌ **M1/M2/M3 内不复制第二套安全中间件**（全复用 E1）
- ✅ M1 起：执行 Prompt/Regenerate 时**会**读 Secret + 构造 HTTP client（仅对 Session 选中的 Profile）
- ✅ keyring 为 web optional dependency；不可用时不阻塞 app 启动
- ✅ 请求启动后 provider/model 不可变（运行中切换只影响下次请求）
- ✅ Regenerate 使用当前 session 当前模型（不动 D2 不变量）

## Next after P1-E

P1-E Multi-Provider Switching 已 ✅ COMPLETE / FROZEN。**等待用户决定**：

- 是否 merge P1-E 全链路到 master
- 是否打 release tag（如 `v0.0.28-multi-provider-switching`）
- 是否 push 到远端
- 是否进入下一阶段（P1-F Markdown Workspace Panel / P2 Web Agent Enhancements）

M3 是 docs-only 归档阶段，未自动执行上述任一动作。

## Deferred

- P1-D3 PDF Text Extraction ✅ RESTARTED via P2-R（2026-07-27；见 [ROADMAP §P2-R](ROADMAP.md) + [docs/design/p2-r0-rag-contract.md](docs/design/p2-r0-rag-contract.md)）
- P2-A URL Routing + Full Reload Recovery
- P2-B Human Approval UI
- P2-C Context Budget + Compaction UI

## P2-R — Knowledge / RAG Subsystem（🟡 IN PROGRESS）

路线 Pivot（2026-07-27）：P2-R 系列取代原 P2 候选成为下一阶段主线。详见 [ROADMAP §P2-R](ROADMAP.md)。

- [x] **P2-R0 Contract Audit**（docs-only）—— 解除 7 处旧 deferred / out-of-scope 标记；冻结数据模型 / 目录布局 / PDF 边界 / Chunk 格式 / Tool 接口 / Session ACL / 同步策略；产出 4 份 docs。FROZEN @ `b32e4b4`。
- [x] **P2-R0 Amendment 1**（docs-only）—— R1 范围重划：marker 集成 / PDF→MD smoke / needs_ocr 测试从 R1 推迟到 R2；R1 缩窄为 Library Foundation only。详见 [docs/design/p2-r0-amendment-1.md](docs/design/p2-r0-amendment-1.md)。
- [x] **P2-R1 Library Foundation** —— 5 表 DDL + migration（独立 knowledge.db）+ KnowledgeFileStore + Library/Document/Binding metadata Store/Service + Library CRUD API + Session Binding API + restart 恢复。FROZEN @ `0973b79`（含 R1-A `4397c3f` + R1-B `1e764e9` + R1-C `0973b79` + R1-D validation docs）。**显式不含** PDF parser（推到 R2）。2702 backend tests + 267 frontend tests + 0 PDF 依赖 + 0 回归。详见 [docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md](docs/validation/p2-r1/P2_R1_LIBRARY_FOUNDATION.md)。
- [x] **P2-R2-0 PDF Parser License Gate**（docs-only）—— FROZEN @ <R2-0 commit>。
      选定 **pypdf 6.14.2 (BSD-3-Clause)** 为 R2 MVP；marker (OpenRAIL-M 模型 + surya/torch hard dep) 拒绝；
      PyMuPDF (AGPL/Commercial) 拒绝。D6 已重命名 + RESOLVED。
      详见 [docs/design/p2-r2-0-pdf-parser-license-gate.md](docs/design/p2-r2-0-pdf-parser-license-gate.md)。
- [x] **P2-R2-A pypdf Parser Adapter** —— ✅ COMPLETE / FROZEN @ <R2-A freeze commit>。
      含 R2-A1 `c359ee5` (build: pypdf dep + license record) + R2-A2 `88b017b` (feat: PdfParser Protocol + PypdfParser Adapter + 66 tests + fixture factory) + R2-A3 validation docs。
      2768 backend + 267 frontend + 0 回归 + 0 PDF parser 越界 + 0 网络/模型/OCR。
      详见 [docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md](docs/validation/p2-r2/P2_R2_A_PARSER_ADAPTER.md)。
- [x] **P2-R2-B Canonical Markdown Builder** —— ✅ COMPLETE / FROZEN @ `eb193b2`（implementation + tests + archive closure 全部完成）。
      含 P2-R0 Amendment 2 `15b411b`（pre-R2-B docs-only；用户 AskUserQuestion 显式选择 "走 amendment-2 流程"；解决 frontmatter 字段 + needs_ocr 阈值合同冲突）+ R2-B1 `0154cde` (feat: pdf_quality.py 43 tests) + R2-B2 `c077d5a` (feat: canonical_markdown.py 80 tests) + R2-B3 `677ed13` (feat: markdown_persistence.py 29 tests) + R2-B4 `eb193b2` freeze (8 integration tests + validation docs)。
      R2-B targeted 160/160 PASS + 完整 backend 2920 passed / 2 skipped / 14 deselected + 0 functional regression + 0 dependency diff + 0 schema diff + 0 frontend diff。详见 [docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md](docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md)。
- [x] **P2-R2-B Contract/Archive Closure** —— ✅ COMPLETE @ 2026-08-03（User decision: **A — RATIFIED**；Amendment 2 APPROVED；docs-only closure commit）。
      接受：`needs_ocr = total_non_whitespace_chars == 0`；低文本密度作 warning；Canonical frontmatter 字段集（schema / document_id / source_filename / source_sha256 / parser_id / parser_version / page_count / title? / generated_at?）；删除 library_id；source_name → source_filename；parser_id 与 parser_version 分离；新增 schema 标识；title 可选。
      library_id 继续由数据库和 Session allowlist 提供，不从 Markdown frontmatter 获得；不削弱 Session ACL / R3 检索 / Citation。
      `generated_at` 冻结规则：R2-C MVP 不传；Builder 默认省略；不调 `datetime.now()`；仅显式传入时写入；相同 PDF 重 ingest 必须生成相同 Markdown bytes 和 SHA-256。任务时间继续用 DB `created_at` / `updated_at` / Job 时间字段。
      测试统计判定：targeted 160 / new 160 / backend delta 152 / functional regression 0；8-test 差异 = **KNOWN NON-BLOCKING**（不阻塞 R2-C），**MUST RECONCILE IN R2-D**。文档中 ruff --fix / pytest fixture 去重仅作假设，不升级为根因结论。详见 [docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md](docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md)。
- [x] **P2-R2-C0 Ingestion Runtime/API Contract** —— ✅ COMPLETE / FROZEN @ <this commit>（docs-only / contract-only / audit-only）。
      含 38 项决策冻结（worker_concurrency=1 / queue=32 / shutdown_grace=30s / SQLite durable source of truth / Document `uploaded`=pending signal / app-level active Job uniqueness via BEGIN IMMEDIATE / 404-409-413-415-500-503 错误映射 / 27-case 失败矩阵 / 4 API endpoints / generated_at MUST NOT BE PASSED）。
      **Schema Amendment NOT REQUIRED**（R1 schema 充分；3 non-blocking gaps deferred to R2-D：markdown_sha256 / parser_id / warnings_json）。
      **R0 §8.1 sync→async 已 minimal refine**（per R2-C0 directive §70 授权；不创建 Amendment 3）。
      4 子阶段：C1 Store+Orchestrator / C2 Worker+Recovery / C3 API / C4 Validation+Freeze。详见 [docs/design/p2-r2-c0-ingestion-runtime-api-contract.md](docs/design/p2-r2-c0-ingestion-runtime-api-contract.md) + [docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md](docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md)。
- [x] **P2-R2-C1 Ingestion Store Extensions + Orchestrator** —— ✅ COMPLETE / FROZEN @ `cde0e0b` + freeze commit。
      含 C0 Archive Correction `0a8f554`（R2-C terminal `ready → normalizing`，per 状态机冻结冲突）+ C1-A `015dc45` IngestionStore（atomic claim / retry / recovery primitives + 38 tests）+ C1-B `cde0e0b` IngestionOrchestrator（Parser+Quality+Builder+Persistence composition + 20 tests）+ C1-C freeze（58/58 targeted + 2986 backend + 267 frontend + ruff clean + 0 regression）。
      **关键不变量**：generated_at MUST NOT BE PASSED；R2-C 终态 = `normalizing`（不调用 `transition_document_status(doc_id, 'ready')`）；needs_ocr 终态 Job='completed'；source.pdf 完整性保留；错误响应零路径/正文/异常泄漏。
      详见 [docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md](docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md)。
- [x] **P2-R2-C2 Bounded Worker + Recovery** —— ✅ COMPLETE / FROZEN @ `115136a` + freeze commit。
      含 C2-A `5aea74b` IngestionWorkerManager（asyncio.Queue cap=32 wake + 30s poll fallback + conditional UPDATE for shutdown race）+ C2-B `115136a` FastAPI lifespan 接线（manager.stop() before KnowledgeStore.close()）+ 37/37 targeted tests。
      **关键不变量**：worker_concurrency=1（frozen）；SQLite = durable source of truth；asyncio.Queue 仅 wake hint；startup recovery（Job running + Document extracting/normalizing → failed + ingestion_interrupted）；graceful shutdown 30s grace + conditional fail；import 无 side effect。
      详见 [docs/validation/p2-r2/P2_R2_C2_WORKER_RECOVERY.md](docs/validation/p2-r2/P2_R2_C2_WORKER_RECOVERY.md)。
- [x] **P2-R2-C3 Upload / Status / Retry / Markdown API** —— ✅ COMPLETE / FROZEN @ `c6a19ec` + freeze commit。
      含 C3-A `72121c5` UploadService（streaming + SHA + staging + atomic rename + duplicate 409 + Worker notify）+ Upload endpoint + 25 tests；C3-B `c6a19ec` Status / Retry / Markdown endpoints + delete guards + 18 tests；C3-C Freeze。
      **关键不变量**：4 endpoints under Trusted UI；MAX_PDF_BYTES=25 MB；streaming chunked 64 KiB；PDF `%PDF-` magic；R2-C terminal=`normalizing`（Markdown readable）；needs_ocr/normalizing/ready 不允许 retry；active Job blocks Document/Library delete；upload 不调 Parser；retry 复用 Document + source.pdf；generated_at 不传。
      详见 [docs/validation/p2-r2/P2_R2_C3_INGESTION_APIS.md](docs/validation/p2-r2/P2_R2_C3_INGESTION_APIS.md)。
- [x] **P2-R2-C4 Integration Validation + R2-C Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`.
      47 C4 integration tests across 5 files: E2E pipeline / failure injection / concurrency / restart+shutdown / security boundaries. Production diff=0. 详见 [P2_R2_C4_INTEGRATION_FREEZE.md](docs/validation/p2-r2/P2_R2_C4_INTEGRATION_FREEZE.md).
- [ ] **P2-R2-D Final PDF Pipeline Validation** —— ✅ APPROVED TO START（⚠ MUST RECONCILE 8-test discrepancy；独立启动授权另需用户发起）。
- [ ] **P2-R2-C4 Integration Validation + R2-C Freeze** —— ⛔ BLOCKED BY C3。
      E2E + restart recovery + concurrent retry + delete race + failure injection + 完整 backend + frontend 零回归 + freeze。
- [ ] **P2-R2-D Integration Validation + Freeze** —— ⛔ BLOCKED BY COMPLETE R2-C。
      **必查 8-test discrepancy**（5 项）：(1) `pytest --collect-only` 数量；(2) 实际执行数量；(3) skipped/deselected 数量；(4) baseline 与当前提交的 pytest 配置 / 插件 / marker；(5) 是否存在 collection 后未执行的测试项。

## Explicitly out of scope (long-term)

- OCR / Image understanding（marker 配置 `force_ocr=False`；扫描 PDF 进 `status=needs_ocr` 终态）
- Long-term user memory / 跨 Session 用户偏好（区别于 RAG——RAG 已在 P2-R 系列重启）
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- CLI（仅 Web UI 入口）
- 本地文件系统操作工具
- 自动 provider fallback / 模型负载均衡
- 长期后台任务调度

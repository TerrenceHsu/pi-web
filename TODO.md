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
      R2-B targeted 160/160 PASS + 完整 backend **2928** passed（**CORRECTED @ P2-R2-D-A**；原报告 2920 误抄）/ 2 skipped / 14 deselected + 0 functional regression + 0 dependency diff + 0 schema diff + 0 frontend diff。详见 [docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md](docs/validation/p2-r2/P2_R2_B_CANONICAL_MARKDOWN.md)。
- [x] **P2-R2-B Contract/Archive Closure** —— ✅ COMPLETE @ 2026-08-03（User decision: **A — RATIFIED**；Amendment 2 APPROVED；docs-only closure commit）。
      接受：`needs_ocr = total_non_whitespace_chars == 0`；低文本密度作 warning；Canonical frontmatter 字段集（schema / document_id / source_filename / source_sha256 / parser_id / parser_version / page_count / title? / generated_at?）；删除 library_id；source_name → source_filename；parser_id 与 parser_version 分离；新增 schema 标识；title 可选。
      library_id 继续由数据库和 Session allowlist 提供，不从 Markdown frontmatter 获得；不削弱 Session ACL / R3 检索 / Citation。
      `generated_at` 冻结规则：R2-C MVP 不传；Builder 默认省略；不调 `datetime.now()`；仅显式传入时写入；相同 PDF 重 ingest 必须生成相同 Markdown bytes 和 SHA-256。任务时间继续用 DB `created_at` / `updated_at` / Job 时间字段。
      测试统计判定：targeted 160 / new 160 / backend delta **160**（**CORRECTED @ P2-R2-D-A**；原报告 delta 152 是 freeze 时点误报 2920 passed 导致；实测 2928 passed）/ functional regression 0；historical 8-test discrepancy = ✅ **RECONCILED @ P2-R2-D-A**（freeze 时点文档误抄；不存在 node-ID-level 差异）。详见 [docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md](docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md) + [docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md](docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md)。
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
- [x] **P2-R2-D-A Test Count Reconciliation** —— ✅ COMPLETE / FROZEN @ `287edb9`（A/B 隔离 worktree + 集合分析；historical 8-test discrepancy ✅ RECONCILED；root cause = R2-B freeze 时点文档误报 2920 passed；实测 2928；selected delta = 160 = targeted 160 完全对账；node-ID-level 无差异）。详见 [docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md](docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md)。
- [x] **P2-R2-D-Fix Ruff Cleanup** —— ✅ COMPLETE / FROZEN @ `a27494c`（C4-R Fix-1 unused `import importlib` cleanup；1 file / 1 line deletion；ruff PASS；full backend ×2 fresh process = 3113/3/14/0 failed；行为零变化）。
- [x] **P2-R2-D-B Final PDF Pipeline Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；R1 targeted count ✅ RECONCILED 118 selected = 117 passed + 1 skipped；10 处 R1 历史误写已修；C4-R Ruff archival correction；P2-R2 全链归档关闭；P2-R3 APPROVED TO START 不在本提交启动）。详见 [docs/validation/p2-r2/P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md](docs/validation/p2-r2/P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md)。
- [x] **P2-R3-A Chunk Contract + Chunker** —— ✅ COMPLETE / FROZEN @ `677fe33`（`chunker.py` + `KnowledgeChunk` DTO + `HeadingAwareChunker` pure function；CHUNK_TARGET_CHARS=1200 / MAX=1800 / OVERLAP=160；deterministic chunk_id=`sha256(doc_id\0ordinal\0content_sha)`；heading stack H1-H6 + code-fence 排除；page marker `<!-- page:N -->` 解析严格 1..N；frontmatter 验证 doc_id + page_count；44/44 unit tests PASS）。详见 [docs/design/p2-r3-chunker-contract.md](docs/design/p2-r3-chunker-contract.md)。
- [x] **P2-R3-A-R Reliability Closure** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only forensic；causality 排除：含 chunker vs 排除 chunker 同样 Windows sequential-suite flake pattern；chunker→victim ×2 / victim→chunker ×1 全 PASS；chunker 静态+动态资源审计 = 0 资源操作；final ×2 = 3157/0 failed deterministic）。详见 [docs/validation/p2-r3/P2_R3_A_R_RELIABILITY_CLOSURE.md](docs/validation/p2-r3/P2_R3_A_R_RELIABILITY_CLOSURE.md)。
- [x] **P2-R3-B1 Schema Migration v1→v2** —— ✅ COMPLETE / FROZEN @ `<this commit>`（`KNOWLEDGE_SCHEMA_VERSION=2`；knowledge_chunks 扩展 char_count + content_sha256；knowledge_chunks_fts FTS5 virtual table（unicode61 remove_diacritics 2）；insert_chunk 兼容 v2 派生新列；18/18 migration tests + R1 regression 全 PASS；full Backend 3175/3/14/0 failed）。详见 [docs/design/p2-r3-b-schema-migration-contract.md](docs/design/p2-r3-b-schema-migration-contract.md)。
- [x] **P2-R3-B2 Chunk/FTS Store** —— ✅ COMPLETE / FROZEN @ `db03be7`（chunk_store.py：replace_document_chunks / delete_document_chunks / rebuild_fts / verify_fts_integrity / search_chunks_fts；safe literal FTS query compiler；BM25 heading-weighted (3.0/1.0) + stable tie-break；ready-only filter；library filter；chunking-only state guard；FTS cleanup 集成到 delete_document_hard / delete_library_hard；66/66 tests PASS；full Backend 3241/0 failed）。
- [x] **P2-R3-B3 Validation Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；84/84 R3-B targeted tests；full Backend 3241/3/14/0 failed；73/73 exit gate；R2 frozen 模块未触动）。详见 [docs/validation/p2-r3/P2_R3_B_SCHEMA_FTS5_VALIDATION.md](docs/validation/p2-r3/P2_R3_B_SCHEMA_FTS5_VALIDATION.md)。
- [x] **P2-R3-C1 IndexingStore** —— ✅ COMPLETE / FROZEN @ `<this commit>`（indexing_store.py：atomic conditional claim + state transitions normalizing→chunking→indexing→ready + mark_failed + recover_interrupted_indexing；RecoveryResult DTO；SQLite conditional write 无 asyncio.Lock correctness；不引入 indexing_jobs 表 schema diff=0；37/37 tests PASS；full Backend 3278/3/14/0 failed；R3-A/B/R2 frozen 模块未触动）。
- [x] **P2-R3-C2 IndexingOrchestrator** —— ✅ COMPLETE / FROZEN @ `<this commit>`（indexing_orchestrator.py：process_document 显式调用；T1-T6 normalizing→chunking→indexing→ready；Markdown read + source_sha256 完整性 + Chunker frontmatter/page_count 复用 + ChunkStore.replace_document_chunks + FTS integrity gate + mark_ready；IndexingResult DTO；safe error taxonomy 8 codes（canonical_markdown_missing / canonical_markdown_integrity_error / canonical_markdown_invalid / chunking_empty / chunking_failed / chunk_persistence_failed / fts_integrity_error / indexing_failed）；failure compensation 自动清理 chunks+FTS；21/21 tests PASS；full Backend 3299/3/14/0 failed @ Run #3；R3-A/B/C1/R2 frozen 模块未触动）。
- [x] **P2-R3-C3 Validation Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；validation doc 61 节；75/75 exit gate；full Backend 3299/0 failed @ Run #3；R3-A/B/R2 frozen 模块未触动）。详见 [docs/validation/p2-r3/P2_R3_C_INDEXING_RUNTIME_VALIDATION.md](docs/validation/p2-r3/P2_R3_C_INDEXING_RUNTIME_VALIDATION.md)。
- [x] **P2-R3-D1 IndexingWorkerManager** —— ✅ COMPLETE / FROZEN @ `<this commit>`（indexing_worker.py：bounded single-concurrency；poll=2.0s；shutdown_grace=30.0s；startup recover_interrupted_indexing wiring（raw SQL cleanup 避免非重入 _write_lock 死锁）；immediate scan + backlog drain + idle poll（无 busy spin）；failure isolation；asyncio.Event wake+stop；zero import side effects；19/19 tests PASS；full Backend ×2 = 3318/0 failed consecutive；R3-A/B/C/R2 frozen 模块未触动）。
- [x] **P2-R3-D2 Lifecycle + Delete Guards** —— ✅ COMPLETE / FROZEN @ `<this commit>`（state.py 加 indexing_worker_manager 字段；app.py lifespan 加 IndexingWorkerManager 构造+start（IngestionWorker 后）+ stop（IngestionWorker 后、KnowledgeStore.close 前）+ knowledge disabled mode 设 None；api.py 加 _document_is_indexing / _library_has_indexing_doc helpers + 409 document_indexing_active / library_indexing_active guards；16/16 lifecycle + guard tests PASS；full Backend Run #2 = 3334/0 failed；R3-A/B/C/R2 frozen 模块未触动）。
- [x] **P2-R3-D3 Validation Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；validation doc 69 节；95/96 exit gate；full Backend Run #1 = 3334/0 failed；R3-A/B/C/R2 frozen 模块未触动）。详见 [docs/validation/p2-r3/P2_R3_D_INDEX_WORKER_VALIDATION.md](docs/validation/p2-r3/P2_R3_D_INDEX_WORKER_VALIDATION.md)。
- [x] **P2-R3-D-R Reliability + Boundary Closure** —— ✅ COMPLETE / FROZEN @ `<this commit>`（§1 reliability：×2 consecutive 0-failed met + R3-D causality rejected；§2 raw SQL boundary：P2-R3-C-D Bridge Amendment Option B applied — ChunkStore owns `delete_document_chunks_in_transaction`；IndexingStore 用 `chunk_store=` 参数不再用 callback；Worker ZERO persistence SQL；72/72 targeted tests；full Backend ×2 = 3334/0 failed）。详见 [docs/validation/p2-r3/P2_R3_D_R_RELIABILITY_AND_BOUNDARY_CLOSURE.md](docs/validation/p2-r3/P2_R3_D_R_RELIABILITY_AND_BOUNDARY_CLOSURE.md)。
- [x] **P2-R3-E Final Integration Freeze** —— ✅ COMPLETE / FINAL FROZEN @ `<this commit>`（含 R3-E-Fix frontmatter regex 冒号匹配 + 23 integration tests：real upload→ready / source+Markdown SHA 不变 / chunks+FTS 完整性 / needs_ocr terminal / restart recovery / delete cleanup+guard / repeated lifespan / 0 pending tasks；full Backend 3357/0 failed；50/50 exit gate）。详见 [docs/validation/p2-r3/P2_R3_E_FINAL_INTEGRATION_FREEZE.md](docs/validation/p2-r3/P2_R3_E_FINAL_INTEGRATION_FREEZE.md)。
- [x] **P2-R4-A Retrieval + Citation Contract** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；真实代码审计 Session/Binding/ChunkStore/ToolRegistry/Assistant lifecycle；冻结 Tool schema + ACL + Evidence + Citation 合同；12 threats mitigated；29/29 exit gate；production diff=0）。详见 [docs/validation/p2-r4/P2_R4_A_CONTRACT_SECURITY_GATE.md](docs/validation/p2-r4/P2_R4_A_CONTRACT_SECURITY_GATE.md) + [docs/design/p2-r4-search-knowledge-citation-contract.md](docs/design/p2-r4-search-knowledge-citation-contract.md)。
- [x] **P2-R4-B1 Search Service + Evidence** —— ✅ COMPLETE / FROZEN @ `<this commit>`（search_models.py: KnowledgeEvidence/SearchKnowledgeResult DTO + KnowledgeSearchError；evidence.py: EvidenceRegistry turn-scoped E1/E2 + chunk_id dedupe + first-seen snapshot；search_service.py: SearchKnowledgeService session ACL→ChunkStore→source_name→Evidence；empty binding short-circuit（ChunkStore 不调用）；library_ids=None 永不传递；29/29 targeted tests；full Backend 3386/0 failed）。
- [x] **P2-R4-B2 search_knowledge Tool + Agent wiring** —— ✅ COMPLETE / FROZEN @ `<this commit>`（search_tool.py: SearchKnowledgeTool(AgentTool) schema=query+limit/additionalProperties=false；session_id_getter closure + evidence_registry_getter lazy init；app.py lifespan 注册；12/12 tool tests；R1 test 更新；full Backend 3398/0 failed）。
- [x] **P2-R4-B3 Validation Freeze** —— ✅ COMPLETE / FUNCTIONALLY FROZEN @ `0353be8`（docs freeze @ 7f35696 + TOCTOU admission fix @ 0353be8；_ensure_idle + state.running=True 原子化；45 targeted tests；full Backend ×2 = 3402/0 failed consecutive；44/44 exit gate；EvidenceRegistry lifecycle + prompt reservation TOCTOU closed）。详见 [docs/validation/p2-r4/P2_R4_B_SESSION_SCOPED_SEARCH_KNOWLEDGE.md](docs/validation/p2-r4/P2_R4_B_SESSION_SCOPED_SEARCH_KNOWLEDGE.md)。
- [x] **P2-R4-C1 Citation 模块** —— ✅ COMPLETE / FROZEN @ `<this commit>`（citations.py: parse_citations / process_citations / render_source_footer / render_citation_inline；strict regex `[cite:E1]`；first-use numbering；unknown evidence remove+warning；32/32 unit tests；full Backend 3434/0 failed）。
- [x] **P2-R4-C2 system prompt + Assistant finalize** —— ✅ COMPLETE / FROZEN @ `<this commit>`（system_prompt.py _KNOWLEDGE_HINT + knowledge_enabled；harness.py build_default_system_prompt(knowledge_enabled=)；app.py _apply_citation_transform module-level 后 _execute_prompt 前 _persist；[cite:E1]→[1]+Sources footer；9/9 integration tests；full Backend 3443/0 failed）。
- [x] **P2-R4-C3 Validation Freeze** —— ✅ COMPLETE / FROZEN @ `<this commit>`（docs-only；41 targeted tests；full Backend ×2 = 3443/0 failed consecutive；27/27 exit gate；[cite:E1]→[1]+Sources footer pipeline verified）。详见 [docs/validation/p2-r4/P2_R4_C_CITATION_AGENT_INTEGRATION.md](docs/validation/p2-r4/P2_R4_C_CITATION_AGENT_INTEGRATION.md)。
- [x] **P2-R4-D Final RAG Integration Freeze** —— ✅ COMPLETE / FINAL FROZEN @ `<this commit>`（6 E2E tests：full PDF→ready→search→evidence→citation pipeline + empty binding + cross-session isolation + invalid citation + source SHA；full Backend ×2 = 3455/0 failed consecutive；19/19 exit gate；**Agent-facing Knowledge RAG ✅ AVAILABLE**）。详见 [docs/validation/p2-r4/P2_R4_D_FINAL_RAG_FREEZE.md](docs/validation/p2-r4/P2_R4_D_FINAL_RAG_FREEZE.md)。
- [ ] **P2-R4 Session-scoped search_knowledge + Page Marker Citation** —— ⛔ BLOCKED BY P2-R3。
- [ ] **B7 SQLite Store Open-Failure Cleanup** —— ⏸ PENDING / NOT AUTHORIZED（独立缺陷；4 个 Store `open()` 缺 try/except close 保护；不阻塞 P2-R2-D-B / 不阻塞 P2-R3；仅在完整 Backend 出现稳定 failure 时升级）。详见 [P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md §17](docs/validation/p2-r2/P2_R2_D_FINAL_PDF_PIPELINE_VALIDATION.md)。

## P2-R5 — Web Knowledge Management + REST + E2E（🟡 IN PROGRESS）

R5 阶段总目标：把 R2/R3/R4 已交付的 Knowledge backend 能力 Web 产品化——REST API composition + Knowledge Manager UI + Upload→Ingest→Search E2E。

- [x] **P2-R5-A Web API + UI Contract Audit** —— ✅ COMPLETE / FROZEN @ `1c1f519`（docs-only；production diff=0；audit 14 existing Knowledge REST endpoints；冻结 Search REST 合同 / status mapping / polling model / UI architecture / E2E plan）。详见 [P2_R5_A_WEB_CONTRACT.md](docs/validation/p2-r5/P2_R5_A_WEB_CONTRACT.md)。
- [x] **P2-R5-B Knowledge REST API** —— ✅ COMPLETE / FROZEN @ `d099185` + `134094a`（R5-B2 Search REST endpoint + DTOs；R5-B3 35 targeted tests；full Backend 3492/0 failed；R2/R3/R4 0 regression；schema/deps/Core Runtime/R2/R3/R4 diff = 0）。R5-B1 跳过（Library/Document REST 已完整）。详见 [P2_R5_B_REST_API.md](docs/validation/p2-r5/P2_R5_B_REST_API.md)。
- [x] **P2-R5-C Knowledge Management Frontend** —— ✅ COMPLETE / FROZEN @ working tree（pending commit on top of `041801c`）。
      含 R5-C1 production（api/types/store + 7 components + SessionSidebar entry，~1834 new + 11 modified）+ R5-C2 tests（7 spec files，80 tests：33 store + 47 component）+ R5-C3 freeze docs。
      **关键不变量**：347/347 vitest（delta 80）+ typecheck/lint/build PASS + frontend-only（backend production diff = 0）；stale-request safety（AbortController + selectedLibraryId guard + searchRequestId）；polling cleanup（onModalClose / onBeforeUnmount / library-switch）；status mapping 终态保留 raw string；binding optimistic + rollback；markdown preview MVP（raw blob in new tab）。
      详见 [P2_R5_C_FRONTEND.md](docs/validation/p2-r5/P2_R5_C_FRONTEND.md)。
- [ ] **P2-R5-D Final Upload→Ingest→Search E2E Freeze** —— ⛔ BLOCKED BY R5-C → APPROVED TO START。

## Explicitly out of scope (long-term)

- OCR / Image understanding（marker 配置 `force_ocr=False`；扫描 PDF 进 `status=needs_ocr` 终态）
- Long-term user memory / 跨 Session 用户偏好（区别于 RAG——RAG 已在 P2-R 系列重启）
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- CLI（仅 Web UI 入口）
- 本地文件系统操作工具
- 自动 provider fallback / 模型负载均衡
- 长期后台任务调度

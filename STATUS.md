# Status

> 当前状态快照。已完成阶段见 [CHANGELOG.md](CHANGELOG.md)；未来计划见 [ROADMAP.md](ROADMAP.md)。

## 产品与文档基线

| 项 | 值 |
|---|---|
| **Product baseline commit** | `d53f331` — test(d2-8): next-prompt-after-regenerate backend integration test |
| **master HEAD** | (post-M1-7) — docs: mark P1-E M1 runtime complete（P1-E M1 ✅ COMPLETE / FROZEN） |
| **Documentation governance** | merged into master（commits `51af04d`, `6a344d1`, merge `d7df358`, finalize `834bd1d`） |
| **Frontend dev/lint maintenance** | merged into master（commits `d8bd2ad`, `b9f4b17`, `7fbd082`, merge `1c2289d`） |
| **P1-E1 Secure Credentials** | ✅ MERGED into master via `de05c66`（no-ff；保留 22 commit 阶段性历史） |
| **Latest release tag** | `v0.0.27-secure-credentials` — P1-E1 Secure Credential Management（2026-07-18，commit `de05c66`） |
| **Backend Foundation HEAD** | `cad7ca7` — feat(web): bind default provider profile on session creation（P1-E2 Backend Foundation ✅ FROZEN @ 3 commits） |
| **M1 Runtime HEAD** | `8b0fb13` — docs: reconcile P1-E M1 runtime implementation record（M1-1 ~ M1-7 ✅ COMPLETE / FROZEN） |
| **M2 Frontend Switching HEAD** | `f9dfc1c` — docs: archive P1-E M2 integration validation（M2-0 ~ M2-4 ✅ COMPLETE / FROZEN；含 M2-F1 hotfix） |
| **Current phase** | **P2-R4 search_knowledge + Citation 🟡 IN PROGRESS** — R3 ✅ FINAL FROZEN @ 599d754。R4-A Contract ✅ @ 6e0089f。**R4-B Session-scoped search_knowledge ✅ COMPLETE / FUNCTIONALLY FROZEN @ 0353be8**（B1 Service+Evidence @ 04c386b / B2 Tool+wiring @ b8dd49a / B2 lifecycle fix @ 1c1d4d4 / B3 Validation Freeze @ 7f35696 / TOCTOU admission fix @ 0353be8；45 targeted tests；full Backend ×2 = 3402/0 failed consecutive；44/44 exit gate；EvidenceRegistry lifecycle + prompt reservation TOCTOU closed；R3 frozen 未触动）。R4-C Citation + Agent Integration ⛔ APPROVED TO START（独立授权另需）。 |
| **R3 Final Freeze** | ✅ COMPLETE / FINAL FROZEN @ `599d754` + correction @ `2342bc2`。详见 [P2_R3_E_FINAL_INTEGRATION_FREEZE.md](docs/validation/p2-r3/P2_R3_E_FINAL_INTEGRATION_FREEZE.md)。 |
| **R2-B Archive Closure** | ✅ COMPLETE — User decision: **A — RATIFIED** @ 2026-08-03（c2436c7）；Amendment 2 APPROVED。**Historical 8-test discrepancy ✅ RECONCILED @ P2-R2-D-A**（freeze 时点文档误报 2920；实测 2928）。详见 [P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md §7](docs/validation/p2-r2/P2_R2_B_AMENDMENT2_AUTHORIZATION_AUDIT.md) + [P2_R2_D_TEST_COUNT_RECONCILIATION.md](docs/validation/p2-r2/P2_R2_D_TEST_COUNT_RECONCILIATION.md) |
| **R2-C0 Status** | ✅ COMPLETE / FROZEN @ `6476f96` + post-freeze correction `0a8f554`（R2-C terminal = `normalizing`）。Schema Amendment NOT REQUIRED。worker_concurrency=1, queue=32, shutdown_grace=30s。详见 [P2_R2_C0_INGESTION_CONTRACT_AUDIT.md](docs/validation/p2-r2/P2_R2_C0_INGESTION_CONTRACT_AUDIT.md) |
| **R2-C1 Status** | ✅ COMPLETE / FROZEN @ `3e45da3` — IngestionStore + IngestionOrchestrator。58/58 targeted tests。详见 [P2_R2_C1_INGESTION_ORCHESTRATOR.md](docs/validation/p2-r2/P2_R2_C1_INGESTION_ORCHESTRATOR.md) |
| **R2-C2 Status** | ✅ COMPLETE / FROZEN @ `33a13a2` — IngestionWorkerManager + FastAPI lifespan wiring。37/37 targeted tests。详见 [P2_R2_C2_WORKER_RECOVERY.md](docs/validation/p2-r2/P2_R2_C2_WORKER_RECOVERY.md) |
| **R2-C3 Status** | ✅ COMPLETE / FROZEN — Upload / Status / Retry / Markdown API。43/43 targeted tests (1 skipped)；3066 backend passed（delta 43 = C3 targeted）；0 functional regression；frontend 267/267；ruff clean。详见 [P2_R2_C3_INGESTION_APIS.md](docs/validation/p2-r2/P2_R2_C3_INGESTION_APIS.md) |
| **R3 Retrieval Scope** | 🔄 REFINED @ 2026-08-03 — pure FTS5 BM25（无向量）；D1（embedding）/ D2（hybrid）/ D3（re-embedding）**永久不做**（per 用户决策"PDF→MD 即可，不再向量化"；R0 §3 推迟项协议直接冻结，**不创建 Amendment 3**）。chunk + FTS5 + search_knowledge tool 保留；evidence 含 page marker 引用（R2-B 已实现）。**P2-R3 ✅ APPROVED TO START**（不在 R2-D-B 提交启动）。详见 [decisions-log §3.1](docs/design/p2-r0-decisions-log.md)。 |
| **Open follow-ups (non-blocking)** | (1) 仓库根 LICENSE 文件缺失——非阻塞本地开发，但 public source / PyPI / Docker / desktop 发布门 ⛔（详见 `p2-r2-0-pdf-parser-license-gate.md §3.3`）；(2) pypdf `>=6.0,<7` 为版本范围非严格 pin——R2 测试报告需记录实际安装版本（6.x 升级跑 fixture regression；7.x 升级重跑 License Gate，详见同文档 §17.1 / §20）|

> P1-D2 Regenerate 已冻结但未打 tag——已通过 P1-E1 合并到下一 release `v0.0.27-secure-credentials`。

## 当前测试基线

| 项 | 值 | 命令 |
|---|---|---|
| Offline pytest | **2585 passed, 1 skipped**（含 M1-1 124 + M1-2 64 + M1-3 64 + M1-4 80 + M1-5 47 + M1-6 67 + M1-7 76 新增） | `pytest tests/ -m "not slow and not integration and not docker" --no-cov` |
| Coverage | Credential + Profile + Binding + Provider Runtime 子系统 ~95%+；总 coverage 阈值 75% PASS | 同上 |
| Playwright e2e（默认 + `--workers=1`） | 主仓库 `pi-py` 验证（本精简副本无 e2e/） | `cd tests/e2e && npx playwright test` |
| Ruff | All checks passed | `ruff check src tests scripts` |
| Frontend prod build | 142.91 KB JS / 40.15 KB CSS（M1-7 build @ 823ms） | `cd src/pi_agent_core_py/web/frontend && npm run build` |
| Production hooks scan | `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js` | grep build artifacts |
| Core runtime diff（M1 全程） | 0 modifications to loop/agent/context/events/stream_events/messages | `git diff --name-only` |
| providers/* diff（M1 范围） | 仅新增 `openai_compat.py` / `factory.py`；`base.py` / `glm.py` / `anthropic_compat.py` / `registry.py` 不修改 | 同上 |
| M1 网络调用 | **0**（marker 测试：所有 M1 测试均不发出 HTTP 请求） | grep test markers |
| M1 Secret 读取 | **0**（marker 测试：除 `CredentialService.resolve_secret_for_request` 单次读取外，不读 OS Keyring / env value） | grep test markers |

## 已冻结阶段

| 阶段 | 状态 | Tag / Commit |
|---|---|---|
| Step 1–21 Core runtime | ✅ FROZEN | 详见 [archived PLAN](docs/archive/legacy-plans/PLAN_STEP_1_21.md) |
| P0 Web Claude MVP | ✅ FROZEN | `v0.0.23-web-claude-p0-mvp` @ `8817c84` |
| P1-A 真实环境验证 | ✅ FROZEN | `v0.0.23.1-web-claude-validation` @ `80a2f6f` |
| P1-B Async Architecture | ✅ FROZEN | `v0.0.24-async-architecture` @ `79cea14` |
| P1-C Extension Persistence | ✅ FROZEN | `v0.0.25-extension-persistence` @ `b4640aa` |
| P1-D1 Export Markdown | ✅ FROZEN | `v0.0.26-export-markdown` @ `ebbc896` |
| P1-D2 Regenerate | ✅ FROZEN | HEAD `d53f331`（合并到 `v0.0.27`） |
| Documentation governance | ✅ FROZEN | merge `d7df358` + finalize `834bd1d` |
| Frontend dev/lint maintenance | ✅ FROZEN | merge `1c2289d` |
| **P1-E1 Secure Credentials** | **✅ PASS / FROZEN / MERGED / TAGGED** | **`v0.0.27-secure-credentials` @ `de05c66`**（2026-07-18） |
| **P1-E2 Backend Foundation**（Schema+Store / Service+ModelOptions / Session Creation Audit / REST API + Session binding） | ✅ FROZEN @ `89fabfd` / `a2c7932` / `9878ef9` / `17c843d` / `9bbd0f2` / `cad7ca7` | 不单独 merge / tag；M1+M2+M3 统一交付 |
| **M1-0 Provider Contract Audit** | ✅ FROZEN | `be13a1f` |
| **M1-1 OpenAI-compatible Adapter** | ✅ FROZEN | `8daaa90`（124 tests） |
| **M1-2 Qwen / Kimi Presets** | ✅ FROZEN | `4d89c82`（64 tests） |
| **M1-3 ProviderFactory** | ✅ FROZEN | `a35a4ad`（64 tests） |
| **M1-4 RequestProviderRuntime** | ✅ FROZEN | `8827bd1`（80 tests / 6 files） |
| **M1-5 Prompt Integration** | ✅ FROZEN | `f116ddd`（47 tests / 5 files） |
| **M1-6 Regenerate Validation** | ✅ FROZEN | `06ecb80`（67 tests / 5 files；validation-only，0 production diff） |
| **M1-7 Runtime Final Validation** | ✅ COMPLETE / FROZEN | (本提交) test(web): freeze multi-provider runtime（76 tests / 4 files；E2E 由主仓库验证） |
| **M2-0 Frontend Integration Audit** | ✅ FROZEN | `87d7a8e` |
| **M2-1 API Types + providerStore** | ✅ FROZEN | `313f28b`（86 vitest） |
| **M2-2 Provider Settings Modal** | ✅ FROZEN | `0626ac4`（74 vitest 累计 160） |
| **M2-3 Provider Selector** | ✅ FROZEN | `c0792e1`（72 vitest 累计 232） |
| **M2-4 Commit A（BLOCKED 证据）** | ✅ FROZEN | `926011f`（登记 P1-E-DEFECT-001 / 002） |
| **M2-F1 Trusted UI Header Repair** | ✅ FROZEN | `dcf45ce`（DEFECT-001 / 002 RESOLVED） |
| **M2-4 Commit C（最终 archive）** | ✅ COMPLETE / FROZEN | `f9dfc1c`（BLOCKED → PASS） |
| **M3 Unified Freeze** | ✅ COMPLETE / FROZEN | docs-only（本提交）—— P1-E Multi-Provider Switching 全链路统一收口 |

### P1-E1 子阶段终态

- P1-E1-1 Secret Primitives ✅ FROZEN
- P1-E1-2 Credential Repository ✅ FROZEN（含 2.1 并发加固 / 2.2 enum 校验）
- P1-E1-3A Router / Service ✅ FROZEN
- P1-E1-3B Validation Strategy ✅ FROZEN（B1 Anthropic / B2 wiring）
- P1-E1-4 Web API Design ✅ APPROVED
- P1-E1-4A Composition Root ✅ FROZEN
- P1-E1-4B REST API ✅ FROZEN
- P1-E1-5A Security Audit ✅ FROZEN（78 控制 / 5 finding）
- P1-E1-5B Security Hardening ✅ FROZEN（MEDIUM-1 / LOW-1 RESOLVED；GAP-1/2/3 CLOSED；含 disconnect bug 修复）
- P1-E1-5C Final Regression ✅ FROZEN（1833 pytest + 37/37 E2E + 0 marker + 0 forbidden pattern）

### P1-E2 Backend Foundation 子阶段终态（PIVOT @ 2026-07-19，原 E2-4 独立 Security Freeze cancelled）

- P1-E2-1 Schema + Store ✅ FROZEN @ `89fabfd`（3 张表 + 独立 connection + `PRAGMA foreign_keys=ON` 验证 + Profile/Binding CRUD + default 单事务切换 + restart persistence）
- P1-E2-2 Service + Static Model Options ✅ FROZEN @ `a2c7932`（ProviderConfigService + Profile status 派生 + provider 作用域 + enabled/is_default 交叉 + Anthropic/GLM 静态 + `validate_model_id`）
- P1-E2-3A Session Creation Audit ✅ FROZEN @ `9878ef9`（方案 A 选定：Session 创建后初始化 Binding + `asyncio.shield` 补偿删除）
- P1-E2-3 Composition + REST API + Session binding ✅ FROZEN @ 3 commits：
  - `17c843d` Composition（`provider_config_runtime.py` + AsyncExitStack lifespan + resolver）
  - `9bbd0f2` REST API（`provider_profiles_api.py` 7 endpoints + `ProviderProfileBodyLimitMiddleware` + `ProviderProfileAPIRoute` + 复用 E1 安全 envelope）
  - `cad7ca7` Session binding（`initialize_new_session_binding` on Service + `asyncio.shield` 补偿 + 5 错误码 + CancelledError 处理）
- ~~P1-E2-4 Restart + Security + Freeze~~ → **cancelled**：合并到 M3 Unified Freeze

### M1 Multi-Provider Runtime 子阶段终态

- **M1-0 Provider Contract Audit** ✅ FROZEN @ `be13a1f`（只读审计，无生产代码；冻结统一 Adapter 接口）
- **M1-1 OpenAI-compatible Adapter** ✅ FROZEN @ `8daaa90`（`providers/openai_compat.py`；124 tests；max_retries=0；async with stream helper；固定短文本错误 from None；SecretStr api_key；reasoning_content 忽略；CancelledError 原样传播）
- **M1-2 Qwen / Kimi Presets** ✅ FROZEN @ `4d89c82`（registry 加 qwen=dashscope beijing shared / kimi=moonshot；key_prefix_hints=()；`/models` 返回 `[]`；64 tests）
- **M1-3 ProviderFactory** ✅ FROZEN @ `a35a4ad`（`providers/factory.py`；`create_provider` 无状态同步工厂；glm/anthropic 精确 + openai_compatible 通用分支；未知 anthropic_compatible 拒绝；from None；AST + socket guard 零跨层 / 零网络；64 tests）
- **M1-4 RequestProviderRuntime** ✅ FROZEN @ `8827bd1`（`web/provider_runtime.py`；`RequestProviderSelection` frozen dataclass + `credential_id repr=False`；resolve/build/bind 三方法；CredentialService 新增 `resolve_secret_for_request` 窄接口 + 2 新错误 `CredentialRequestSecret{Unavailable,Backend}Error`；`asyncio.shield` cancellation-safe close；no second lock；80 tests / 6 files）
- **M1-5 Prompt Integration** ✅ FROZEN @ `f116ddd`（`web/app.py` 两处接线：composition root + `_execute_prompt` 唯一接入点；`AsyncExitStack` + `bind_to_harness`；4 固定错误码 `provider_profile_{unavailable,disabled}` / `provider_credential_unavailable` / `provider_initialization_failed`；`CancelledError` 原样传播；47 tests / 5 files；2×37/37 E2E）
- **M1-6 Regenerate Validation** ✅ FROZEN @ `06ecb80`（validation-only——`_execute_prompt` 是唯一 Provider Runtime 接入点，`_run_regeneration_core` 经 `override_initial_messages + suppress_user_append=True` 复用同一执行函数，无需第二次接线；2 个 AST 测试锁定 lexical-body 内 `resolve_selection/bind_to_harness/build_adapter` 只出现在 `_execute_prompt`；67 tests / 5 files）
- **M1-7 Runtime Final Validation** ✅ COMPLETE / FROZEN（本提交）——跨模块组合 validation-only：3-provider E2E 矩阵（GLM/Qwen/Kimi）+ 跨 Session 隔离 + 默认/显式切换全链路 + app restart（env/keyring/session-only Credential）+ Credential 删除/替换 + Tool loop 不变量 + 安全出口全审计（HTTP/SQLite main/WAL/SHM/log/exception chain/Selection repr/Adapter repr）+ 静态架构约束（Core Runtime M1 diff=0；Provider Runtime 接线只在 `_execute_prompt`；`provider_runtime.py` 不导入 `SecretStoreRouter`/SDK；`factory.py` 不导入 Web；`openai_compat.py` 不导入 Registry/Web）；76 tests / 4 files；M1 production diff = 0；M1 全程 0 网络调用 / 0 secret 泄漏；E2E 由主仓库 `pi-py` 验证）

测试基线（post-M1-7）：2585 full pytest + 1 skipped（keyring API path 不在本副本验证）+ ruff clean + 0 Core Runtime diff + 0 providers/* diff（仅新增 `openai_compat.py` / `factory.py`）+ 0 network + 0 secret reads + frontend build clean（142.91 KB JS / 40.15 KB CSS）。

## 当前阶段

**P1-E Multi-Provider Switching — ✅ COMPLETE / FROZEN**（M1 Runtime + M2 Frontend + M3 Unified Freeze）。

- 路线：[ROADMAP.md](ROADMAP.md) § P1-E
- 阶段终态：M1-0 ~ M1-7 ✅ → M2-0 ~ M2-4 ✅（含 M2-F1 hotfix）→ M3 Unified Freeze ✅
- 最终归档：[docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md](docs/validation/p1-e/P1_E_M3_UNIFIED_FREEZE.md)
- M2 集成验证归档：[docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md](docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md)
- Release Candidate：✅ READY FOR USER REVIEW（merge / tag / push 仍需独立授权）

P1-D3 PDF Text Extraction ⏸ **DEFERRED**（2026-07-16 决策，转出主路线）。PDF / Vector RAG ⏸ **DEFERRED**（同上）。

## 当前阻塞项

无技术阻塞。**等待用户授权** merge / tag / push 流程——M3 是 docs-only 阶段，未自动进入发布。

## 下一步

- 用户审核 P1-E 最终状态（M3 freeze 文档 + M2-4 validation 文档）
- 用户决定是否 merge 到 master / 打 release tag / push 到远端
- 用户决定后续阶段（P1-F Markdown Workspace Panel / P2 Web Agent Enhancements）

## 已知限制

### Runtime
- Request registry 是内存态——server 重启后 active request 丢失
- Single harness——不支持多 session 并行执行
- Localhost only / no auth——不适合公网部署
- Provider/Model Backend Foundation ✅ frozen（Credential + Profile + Binding 持久化）；**M1 才真正执行 Prompt/Regenerate 切换**

### Web UI
- 完整浏览器 reload 后恢复原 session 依赖 URL routing（**未实现**）——P2-A 处理
- WebSocket reconnect recovery 已支持（`regenerate.spec.ts:243-290`）
- `POST /api/prompt/async` 单 active request——不支持并发 prompt
- Markdown 文件预览依赖下载或 `view_file` 工具——P1-F 实施后支持侧栏预览
- 前端 Provider/Model 选择器未实现——M2 Frontend Switching

### Regenerate
- 仅支持最新 assistant 的 regenerate
- 不支持手动切换历史 revision 为 active
- 不持久化 "regenerated" badge（DTO 无信号）
- revision history UI/drawer 不实现（D2 显式不做）

### Credential 子系统（P1-E1 已交付，剩余为 E2+ 范围）
- GLM credential remote validation 仍为 unsupported（只有 Anthropic Models API 走远端验证）
- `session_only`（InMemorySecretStore）按设计在进程重启后丢失
- Python `str` 无法提供可靠的内存清零保证
- OS Keyring 的安全性依赖宿主系统 backend

### 已 DEFERRED（非永久放弃，转出主路线）
- P1-D3 PDF Text Extraction
- PDF / Vector RAG
- OCR / Image understanding

### 不在范围内
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- CLI（仅 Web UI 入口）
- 自动 provider fallback / 模型负载均衡
- 长期后台任务调度

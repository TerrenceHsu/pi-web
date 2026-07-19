# Status

> 当前状态快照。已完成阶段见 [CHANGELOG.md](CHANGELOG.md)；未来计划见 [ROADMAP.md](ROADMAP.md)。

## 产品与文档基线

| 项 | 值 |
|---|---|
| **Product baseline commit** | `d53f331` — test(d2-8): next-prompt-after-regenerate backend integration test |
| **master HEAD** | `de05c66` — merge: complete P1-E1 secure credential management |
| **Documentation governance** | merged into master（commits `51af04d`, `6a344d1`, merge `d7df358`, finalize `834bd1d`） |
| **Frontend dev/lint maintenance** | merged into master（commits `d8bd2ad`, `b9f4b17`, `7fbd082`, merge `1c2289d`） |
| **P1-E1 Secure Credentials** | ✅ MERGED into master via `de05c66`（no-ff；保留 22 commit 阶段性历史） |
| **Latest release tag** | `v0.0.27-secure-credentials` — P1-E1 Secure Credential Management（2026-07-18，commit `de05c66`） |
| **Backend Foundation HEAD** | `cad7ca7` — feat(web): bind default provider profile on session creation（P1-E2 Backend Foundation ✅ FROZEN @ 3 commits） |
| **Current phase** | P1-E Multi-Provider Switching — M1 Multi-Provider Runtime（PIVOT @ 2026-07-19，待 M1-0 审计） |

> P1-D2 Regenerate 已冻结但未打 tag——已通过 P1-E1 合并到下一 release `v0.0.27-secure-credentials`。

## 当前测试基线

| 项 | 值 | 命令 |
|---|---|---|
| Offline pytest | **2063 passed**（含 P1-E2 Backend Foundation 144 新增） | `pytest tests/ -m "not slow and not integration and not docker" --no-cov` |
| Coverage | Credential + Profile + Binding 子系统 ~95%+；总 coverage 阈值 75% PASS | 同上 |
| Playwright e2e（默认 + `--workers=1`） | **37/37 PASS** ×2（E2-3 REST API + Session binding 各跑一轮） | `cd tests/e2e && npx playwright test` |
| Ruff | All checks passed | `ruff check src tests scripts` |
| Frontend prod build | 143.28 KB JS / 40.15 KB CSS | `cd src/pi_agent_core_py/web/frontend && npm run build` |
| Production hooks scan | `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js` | grep build artifacts |
| Core runtime diff（P1-E1 + P1-E2 范围） | 0 modifications to loop/agent/context/events/stream_events/messages（providers/ 允许新增 registry；web/ 允许 credentials/profile 模块） | `git diff --name-only` |
| P1-E2 网络调用 | **0**（marker 测试：所有 E2 测试均不发出 HTTP 请求） | grep test markers |
| P1-E2 Secret 读取 | **0**（marker 测试：所有 E2 测试不读 OS Keyring / env value） | grep test markers |

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

测试基线（HEAD `cad7ca7`）：2063 full pytest + 2×37/37 E2E + ruff clean + 0 Core Runtime diff + 0 network + 0 secret reads。

## 当前阶段

**P1-E Multi-Provider Switching — M1 Multi-Provider Runtime**（PIVOT @ 2026-07-19）。

- 路线：[ROADMAP.md](ROADMAP.md) § P1-E（M1 / M2 / M3 milestone）
- Pivot 决策：原 P1-E2/E3/E4/E5 拆分过细，E2-4 独立 Security Freeze 会冻结一个用户无法直接使用的配置后端——改为 M1 Runtime / M2 Frontend / M3 Unified Freeze 单一 milestone
- Pivot 附录：[docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md) §19
- M1 子阶段：M1-0 Provider Contract Audit → M1-1 `OpenAICompatibleProvider` → M1-2 Qwen/Kimi presets → M1-3 `ProviderFactory` → M1-4 `web/provider_runtime.py` → M1-5 Prompt integration → M1-6 Regenerate integration → M1-7 Runtime tests

P1-D3 PDF Text Extraction ⏸ **DEFERRED**（2026-07-16 决策，转出主路线）。PDF / Vector RAG ⏸ **DEFERRED**（同上）。

## 当前阻塞项

无 M1-0 实施阻塞。8 个跨阶段设计边界已冻结（详见 ROADMAP.md P1-E 段；Custom URL 安全 M1/M2/M3 全程排除）。

## 下一步

**M1-0 Provider Contract Audit**（设计文档，无生产代码）：只读审计 `providers/base.py` / `glm.py` / `anthropic_compat.py` / `registry.py` + Agent Provider 持有 + Prompt/Regenerate 入口；产出 `docs/design/p1-e-m1-provider-runtime.md`，冻结统一 Adapter 接口（`stream()` / `close()` / tool_call 增量 / usage / finish_reason）。审计完成后停止审核，再启动 M1-1 `openai_compat.py`。

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

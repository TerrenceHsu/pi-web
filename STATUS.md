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
| **Current phase** | P1-E2 Provider Profiles design（DESIGN FROZEN 2026-07-19，待 E2-1 编码） |

> P1-D2 Regenerate 已冻结但未打 tag——已通过 P1-E1 合并到下一 release `v0.0.27-secure-credentials`。

## 当前测试基线

| 项 | 值 | 命令 |
|---|---|---|
| Offline pytest | **1833 passed**, 14 deselected | `pytest tests/ -m "not slow and not integration and not docker" --no-cov` |
| Coverage |Credential 子系统 ~95%+；总 coverage 阈值 75% PASS | 同上 |
| Playwright e2e（默认 + `--workers=1`） | **37/37 PASS** | `cd tests/e2e && npx playwright test` |
| Ruff | All checks passed | `ruff check src tests scripts` |
| Frontend prod build | 143.28 KB JS / 40.15 KB CSS | `cd src/pi_agent_core_py/web/frontend && npm run build` |
| Production hooks scan | `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js` | grep build artifacts |
| Core runtime diff（P1-E1 范围） | 0 modifications to loop/agent/context/events/stream_events/messages（providers/ 允许新增 registry；web/ 允许 credentials/profile 模块） | `git diff --name-only` |

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

## 当前阶段

**P1-E2 Provider Profiles + Session Model Bindings**——设计 DESIGN FROZEN（2026-07-19），待 E2-1 编码启动。

- 设计文档：[docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md)
- 最简后端方案：3 张表 / 4 个主要新增生产模块 / 7 API 操作 / **E2 真实网络调用 = 0**
- 阶段拆分：E2-1 Schema+Store → E2-2 Service+Static Model Options → E2-3A Session Creation Audit（审计门）→ E2-3 REST API+Binding → E2-4 Restart+Security+Freeze

P1-D3 PDF Text Extraction ⏸ **DEFERRED**（2026-07-16 决策，转出主路线）。PDF / Vector RAG ⏸ **DEFERRED**（同上）。

## 当前阻塞项

无 P1-E2 实施阻塞。8 个跨阶段设计边界已冻结（详见 ROADMAP.md P1-E / P1-F 段；含 Custom URL 安全——E2 暂不使用）。

## 下一步

**P1-E2-1 实施**（Schema + Store）：3 张表 + Provider Config Store + `PRAGMA foreign_keys=ON` 验证 + Profile/Binding CRUD + default Profile 单事务切换 + restart persistence。

## 已知限制

### Runtime
- Request registry 是内存态——server 重启后 active request 丢失
- Single harness——不支持多 session 并行执行
- Localhost only / no auth——不适合公网部署
- Provider/Model 当前仍是单一 Provider；P1-E2 完成后**仅持久化选择**，E3 才真正切换

### Web UI
- 完整浏览器 reload 后恢复原 session 依赖 URL routing（**未实现**）——P2-A 处理
- WebSocket reconnect recovery 已支持（`regenerate.spec.ts:243-290`）
- `POST /api/prompt/async` 单 active request——不支持并发 prompt
- Markdown 文件预览依赖下载或 `view_file` 工具——P1-F 实施后支持侧栏预览
- 前端 Provider/Model 选择器未实现——P1-E4

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

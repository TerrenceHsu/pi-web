# Status

> 当前状态快照。已完成阶段见 [CHANGELOG.md](CHANGELOG.md)；未来计划见 [ROADMAP.md](ROADMAP.md)。

## 产品与文档基线

| 项 | 值 |
|---|---|
| **Product baseline commit** | `d53f331` — test(d2-8): next-prompt-after-regenerate backend integration test |
| **master HEAD** | `1c2289d` — Merge branch 'fix/frontend-dev-and-cleanup' |
| **Documentation governance** | merged into master（commits `51af04d`, `6a344d1`, merge `d7df358`, finalize `834bd1d`） |
| **Frontend dev/lint maintenance** | merged into master（commits `d8bd2ad`, `b9f4b17`, `7fbd082`, merge `1c2289d`） |
| **Latest release tag** | `v0.0.26-export-markdown` — P1-D1 Export Markdown（2026-07-14，commit `ebbc896`） |
| **Current phase** | P1-E1 Secure Credentials（设计已冻结，待实施计划批准） |

> P1-D2 Regenerate 已冻结但未打 tag——按既定 tag 策略，P1-D2 / E1-E5 / F1-F3 合并到下一个 release tag。

## 当前测试基线

| 项 | 值 | 命令 |
|---|---|---|
| Offline pytest | **1131 passed**, 14 deselected | `pytest tests/ -m "not slow and not integration and not docker"` |
| Coverage | **84.20%** ≥ 75% PASS | 同上 |
| Playwright e2e（默认 + `--workers=1`） | **37/37 PASS** | `cd tests/e2e && npx playwright test` |
| Ruff | All checks passed | `ruff check src tests scripts` |
| Frontend prod build | 142.91 KB JS / 40.15 KB CSS | `cd src/pi_agent_core_py/web/frontend && npm run build` |
| Production hooks scan | `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js` | grep build artifacts |
| Core runtime diff（`9264267..HEAD`） | 0 modifications to loop/agent/context/events/stream_events/messages（providers/ 在 P1-E 阶段允许扩展） | `git diff --name-only` |

## 已冻结阶段

| 阶段 | 状态 | Tag / Commit |
|---|---|---|
| Step 1–21 Core runtime | ✅ FROZEN | 详见 [archived PLAN](docs/archive/legacy-plans/PLAN_STEP_1_21.md) |
| P0 Web Claude MVP | ✅ FROZEN | `v0.0.23-web-claude-p0-mvp` @ `8817c84` |
| P1-A 真实环境验证 | ✅ FROZEN | `v0.0.23.1-web-claude-validation` @ `80a2f6f` |
| P1-B Async Architecture | ✅ FROZEN | `v0.0.24-async-architecture` @ `79cea14` |
| P1-C Extension Persistence | ✅ FROZEN | `v0.0.25-extension-persistence` @ `b4640aa` |
| P1-D1 Export Markdown | ✅ FROZEN | `v0.0.26-export-markdown` @ `ebbc896` |
| P1-D2 Regenerate | ✅ FROZEN | HEAD `d53f331`（不打 tag，合并到下一 release） |
| Documentation governance | ✅ FROZEN | merge `d7df358` + finalize `834bd1d` |
| Frontend dev/lint maintenance | ✅ FROZEN | merge `1c2289d` |

## 当前阶段

**P1-E1 Secure Credentials**——设计已冻结，待 P1-E1 实施计划批准后进入编码。

P1-D3 PDF Text Extraction ⏸ **DEFERRED**（2026-07-16 决策，转出主路线）。PDF / Vector RAG ⏸ **DEFERRED**（同上）。

## 当前阻塞项

无 P1-E1 实施阻塞。7 个跨阶段设计边界已冻结（详见 ROADMAP.md P1-E / P1-F 段）。

## 下一步

**P1-E1 实施计划批准 → 编码 → 验证**。计划文档：[docs/design/p1-e1-secure-credentials.md](docs/design/p1-e1-secure-credentials.md)（待提交）。

## 已知限制

### Runtime
- Request registry 是内存态——server 重启后 active request 丢失
- Single harness——不支持多 session 并行执行
- Localhost only / no auth——不适合公网部署
- Provider/Model 单一——P1-E1–E5 实施后将支持多供应商

### Web UI
- 完整浏览器 reload 后恢复原 session 依赖 URL routing（**未实现**）——P2-A 处理
- WebSocket reconnect recovery 已支持（`regenerate.spec.ts:243-290`）
- `POST /api/prompt/async` 单 active request——不支持并发 prompt
- Markdown 文件预览依赖下载或 `view_file` 工具——P1-F 实施后支持侧栏预览

### Regenerate
- 仅支持最新 assistant 的 regenerate
- 不支持手动切换历史 revision 为 active
- 不持久化 "regenerated" badge（DTO 无信号）
- revision history UI/drawer 不实现（D2 显式不做）

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

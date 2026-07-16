# Status

> 当前状态快照。已完成阶段见 [CHANGELOG.md](CHANGELOG.md)；未来计划见 [ROADMAP.md](ROADMAP.md)。

## 当前 HEAD

`d53f331` — test(d2-8): next-prompt-after-regenerate backend integration test

## 最新正式 tag

`v0.0.26-export-markdown` — P1-D1 Export Markdown（2026-07-14，commit `ebbc896`）

后续 P1-D2 Regenerate 已冻结但未打 tag——按既定 tag 策略，P1-D2/D3 合并到 `v0.0.27-product-actions`。

## 当前测试基线

| 项 | 值 | 命令 |
|---|---|---|
| Offline pytest | **1131 passed**, 14 deselected | `pytest tests/ -m "not slow and not integration and not docker"` |
| Coverage | **84.20%** ≥ 75% PASS | 同上 |
| Playwright e2e（默认 + `--workers=1`） | **37/37 PASS** | `cd tests/e2e && npx playwright test` |
| Ruff | All checks passed | `ruff check src tests scripts` |
| Frontend prod build | 142.91 KB JS / 40.15 KB CSS | `cd src/pi_agent_core_py/web/frontend && npm run build` |
| Production hooks scan | `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js` | grep build artifacts |
| Core runtime diff（`9264267..HEAD`） | 0 modifications to loop/agent/context/providers/events/stream_events/mcp/tools/skill_loader | `git diff --name-only` |

## 已冻结阶段

| 阶段 | 状态 | Tag / Commit |
|---|---|---|
| Step 1–21 Core runtime | ✅ FROZEN | 详见 [archived PLAN](docs/archive/legacy-plans/PLAN_STEP_1_21.md) |
| P0 Web Claude MVP | ✅ FROZEN | `v0.0.23-web-claude-p0-mvp` @ `8817c84` |
| P1-A 真实环境验证 | ✅ FROZEN | `v0.0.23.1-web-claude-validation` @ `80a2f6f` |
| P1-B Async Architecture | ✅ FROZEN | `v0.0.24-async-architecture` @ `79cea14` |
| P1-C Extension Persistence | ✅ FROZEN | `v0.0.25-extension-persistence` @ `b4640aa` |
| P1-D1 Export Markdown | ✅ FROZEN | `v0.0.26-export-markdown` @ `ebbc896` |
| P1-D2 Regenerate | ✅ FROZEN | HEAD `d53f331`（不打 tag，与 D3/D4 合并） |

## 当前阶段

无进行中阶段。P1-D2 已冻结；P1-D3 设计待批准。

## 当前阻塞项

P1-D3 PDF Text Extraction 有 3 个边界冲突待解决：

1. `tools/view_file.py` 修改边界（允许 PDF adapter vs 不改 tools/）
2. `asyncio.to_thread + wait_for` 是软超时（不是硬取消）
3. PDF metadata 存储位置（FileRef 需加 metadata 字段）
4. 失败文件清理策略（方案 A 整体拒绝 vs 方案 B 保存但标记）

## 下一步

**P1-D3 PDF Text Extraction 设计批准 → 实施 → 验证**。

## 已知限制

### Runtime
- Request registry 是内存态——server 重启后 active request 丢失
- Single harness——不支持多 session 并行执行
- Localhost only / no auth——不适合公网部署

### Web UI
- 完整浏览器 reload 后恢复原 session 依赖 URL routing（**未实现**）——P2 处理
- WebSocket reconnect recovery 已支持（`regenerate.spec.ts:243-290`）
- `POST /api/prompt/async` 单 active request——不支持并发 prompt

### Regenerate
- 仅支持最新 assistant 的 regenerate
- 不支持手动切换历史 revision 为 active
- 不持久化 "regenerated" badge（DTO 无信号）
- revision history UI/drawer 不实现（D2 显式不做）

### 不在范围内
- OCR / Image understanding
- RAG / Vector Memory / Long-term Memory
- Multi-Agent / 多用户 / RBAC / OAuth
- 公网部署 / 横向扩展
- CLI（仅 Web UI 入口）

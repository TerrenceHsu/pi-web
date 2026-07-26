# P1-E M3 Unified Freeze

> **阶段**：P1-E Multi-Provider Switching — M3 Unified Freeze（docs-only archival）
> **基线 commit（M3 之前）**：`f9dfc1c` — docs: archive P1-E M2 integration validation
> **M3 freeze commit**：本提交（ docs-only，production / test / dependency / schema diff = 0）
> **归档日期**：2026-07-26
> **范围**：将 Credential → ProviderProfile → SessionModelBinding → RequestProviderSelection → ProviderAdapter → Prompt / Regenerate → AssistantMessage / Revision → Frontend Settings / Selector / Reload 统一收口为正式冻结版本。

---

## 1. Final Status

```
P1-E Multi-Provider Switching
✅ COMPLETE / FROZEN

Baseline before M3:
f9dfc1c

M3 freeze commit:
本提交（自引用；hash 由 git 在提交时生成，不在文档内伪造）

Release state:
READY FOR USER REVIEW
```

M3 本身不重新实现、不重新设计、不扩展任何能力——仅做归档收口与状态文档同步。

---

## 2. Scope

M3 冻结的最终能力（每个能力均由前置阶段交付，M3 不再触碰）：

- GLM / Qwen / Kimi ProviderProfile 配置
- 安全 Credential 存储（OSKeyring / InMemory / Env 三种 SecretStore）
- Session 级 Provider / model Binding 持久化
- Prompt 使用请求开始时冻结的 Binding（请求级不可变快照）
- Regenerate 使用执行时当前 Session Binding
- 前端 Provider Settings Modal + Provider Selector
- 浏览器 reload 后 Binding 恢复
- null Binding 走 Legacy client 兼容路径
- invalid Binding 阻止发送（不自动 fallback）
- AssistantMessage / Revision provider / model 持久化

---

## 3. Frozen Commit Chain

| 阶段 | Commit | 说明 |
|---|---|---|
| Backend Foundation | `cad7ca7` | feat(web): bind default provider profile on session creation |
| M1 Runtime | `8b0fb13` | docs: reconcile P1-E M1 runtime implementation record |
| M2-0 Frontend Integration Audit | `87d7a8e` | docs: freeze P1-E M2 frontend integration contract |
| M2-1 API Types + providerStore | `313f28b` | feat(web): add provider frontend state |
| M2-2 Provider Settings Modal | `0626ac4` | feat(web): add provider settings modal |
| M2-3 Provider Selector | `c0792e1` | feat(web): add provider selector |
| M2-4 Commit A（BLOCKED 证据） | `926011f` | test(web): record blocked M2 integration validation |
| M2-F1 Trusted UI Header Repair | `dcf45ce` | fix(web): attach trusted UI header to frontend requests |
| M2-4 Commit C（最终 archive） | `f9dfc1c` | docs: archive P1-E M2 integration validation |
| M3 Unified Freeze（本提交） | _本 commit_ | docs: freeze P1-E multi-provider switching |

所有 commit 均可从 HEAD 追溯；P1-E-DEFECT-001 / 002 在 `dcf45ce` 中同时 RESOLVED；M2-4 在 `f9dfc1c` 中由 BLOCKED 转 PASS。

---

## 4. Final Quality Baseline

### 4.1 Frontend

| 项 | 结果 |
|---|---|
| vitest | ✅ 261 passed / 10 files |
| typecheck | ✅ PASS（vue-tsc --noEmit） |
| lint | ✅ PASS（max-warnings=0） |
| build | ✅ PASS |
| build:e2e | ✅ PASS（`__storeHooks` 暴露） |
| prettier（新增文件） | ✅ PASS |

### 4.2 Backend

| 项 | 结果 |
|---|---|
| pytest 全量回归 | ✅ 2585 passed + 1 skipped + 14 deselected |
| 定向测试（Provider runtime + Prompt + Regenerate + Revision + Binding + Default） | ✅ 144 passed |
| ruff | ✅ All checks passed |

### 4.3 Playwright E2E

| 项 | Run 1 | Run 2 |
|---|---|---|
| 总测试数 | 37 | 37 |
| 通过 | 37 | 37 |
| 失败 | 0 | 0 |
| flaky retry | 0 | 0 |
| timeout | 0 | 0 |

连续两次稳定通过。

### 4.4 Security

| 项 | 结果 |
|---|---|
| External Provider requests | 0（api.anthropic.com / open.bigmodel.cn / dashscope.aliyuncs.com / api.moonshot.cn 均 0 命中） |
| Secret persistent leakage | 0（production source / Pinia state / DOM / storage / log 全 0；test-fixture marker 1 处属预期） |
| Anthropic product UI | 0（前端 `visibleDefinitions` 过滤 `id === "anthropic"`） |

### 4.5 Production Bundle

| 项 | 大小 |
|---|---|
| JS | 169.23 KB |
| gzip | 56.49 KB |
| CSS | 46.11 KB |

### 4.6 Static Diff Audit（vs M2-3 baseline `c0792e1`）

- Production diff：仅 `src/pi_agent_core_py/web/frontend/src/api/client.ts`（M2-F1 修复，Commit B `dcf45ce` 已记录）
- 后端 Python / DB schema / API schema / package.json / package-lock.json：0 改动
- `git diff --check`：clean
- Working tree（M3 提交前）：clean

---

## 5. Resolved Defects

### P1-E-DEFECT-001（CRITICAL）：UI Header 缺失

- **症状**：前端共享传输层（`api/client.ts`）的 `requestJson` / `uploadForm` / `requestBlob` 未注入 `X-PI-Agent-UI: 1` header；后端 Credential / Provider Profile / Session Binding / Provider Definitions 路由返回 400 `missing_ui_header`，`bindingLoadState` 进入 `error`，`send-button` 永久 disabled。
- **修复**：新增 `createUiHeaders()` helper 强制 `set(UI_HEADER_NAME, UI_HEADER_VALUE)`；三个 fetch helper 统一使用。
- **状态**：✅ RESOLVED @ `dcf45ce`（见 `docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md` §1.1）

### P1-E-DEFECT-002（HIGH）：结构化错误渲染为 "[object Object]"

- **症状**：`requestJson` 错误转换 `String(payload.detail || payload.error)`；`payload.error` 为对象时 `String(obj)` → `"[object Object]"`，影响所有 400/409/503 结构化 error body。
- **修复**：新增 `safeErrorDetail(payload, statusText, fallback)` 按优先级读取 `payload.error.message` → `payload.message` → `payload.detail` 字符串 → plain string → `statusText` → fallback；不渲染 FastAPI validation `detail: [...]` 数组；不渲染 `error.code`；不使用 `String(obj)` 兜底。
- **状态**：✅ RESOLVED @ `dcf45ce`（见 `docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md` §1.2）

Defect 处理流程：M2-4 Commit A 登记 BLOCKED 证据（`926011f`）→ 独立 M2-F1 纠错阶段（`dcf45ce`）→ Commit C 重新验证为 PASS（`f9dfc1c`）。

---

## 6. Frozen Product Semantics

M3 冻结的关键不变量（违反任一项视为回归）：

- 不跨 Provider 自动探测 Key
- 不自动 fallback 到其它 Provider
- Provider / model 按 Session 绑定（每个 Session 独立）
- 请求开始时冻结选择（请求级 `RequestProviderSelection` 快照）
- 切换只影响下一次请求（运行中切换不污染当前 active request）
- Regenerate 使用当前 Session 当前 Binding
- `Profile.default_model` 不静默修改已有 Binding
- null Binding 走 Legacy client（兼容旧 Session）
- invalid Binding 阻止发送（不静默降级）
- Anthropic 保留后端兼容代码但不进入产品 UI
- Secret 不进入 SQLite 明文 / Pinia state / 浏览器 storage / 日志 / WS event / Markdown export

---

## 7. Explicitly Deferred

以下能力不属于 P1-E，明确 deferred（M3 不实现，转出主路线）：

- Custom Provider / Custom Base URL 入口
- 远程模型目录 API（`/v1/models` 调用）
- 模型能力说明 / 模型搜索 UI
- Provider 健康检查 / 限流状态 / 上次验证时间
- 成本统计 / IO token post-hoc 展示
- 自动 provider fallback / 模型负载均衡
- Provider 测速
- Credential 远程验证 UI（除现有 Anthropic Models API 外）
- 复杂多 Key / 同 Provider 多 Profile 管理
- 多 Agent 编排
- RAG / Vector Memory / Long-term Memory
- 公网部署 / 横向扩展

详见 `ROADMAP.md` § "P1-E 显式不包含" 与 "明确不做（长期）"。

---

## 8. Release Gate

| 项 | 状态 |
|---|---|
| Unified Freeze 完成 | ✅ |
| 用户审核就绪 | ✅ READY FOR USER REVIEW |
| merge | ⛔ NOT AUTHORIZED（仍需独立授权） |
| tag | ⛔ NOT AUTHORIZED（仍需独立授权） |
| push | ⛔ NOT AUTHORIZED（仍需独立授权） |

当前状态：未 merge、未 tag、未 push。M3 提交完成后等待用户决定后续 release 流程。

---

## 9. Known Limitations

仅记录真实已知限制（不发明问题）：

1. **E2E FakeClient 不等同于真实外部 Provider**——FakeClient 不模拟 Provider routing；"Prompt 实际使用当前 binding Provider"由 backend 144 个定向测试覆盖（见 `P1_E_M2_INTEGRATION_VALIDATION.md` §5.2）。
2. **真实 Provider 网络被明确禁止进入自动测试**——design §15.12 约束；M3 不引入真实 API Key 验证流程。
3. **Custom Base URL / Custom Provider 等高级功能未实现**——明确 deferred（见 §7）。
4. **当前产品 Provider 范围是 GLM / Qwen / Kimi**——Anthropic 保留后端兼容代码但不进入产品 UI。
5. **chatStore.ts:570 `/api/abort` 路径未注入 UI Header**——审计确认此 endpoint 注册在 main app、未挂 `require_ui_header_dep`；按"只修真需要的地方"原则保留。如未来 endpoint 上移到 credential router，需追加注入。

---

## 10. Final Verdict

```
P1-E M3 Unified Freeze
✅ COMPLETE / FROZEN

P1-E Multi-Provider Switching
✅ COMPLETE / FROZEN

Release Candidate
✅ READY FOR USER REVIEW

Merge
⛔ NOT AUTHORIZED

Tag
⛔ NOT AUTHORIZED

Push
⛔ NOT AUTHORIZED
```

---

**M3 Unified Freeze @ 2026-07-26**——P1-E Multi-Provider Switching 全链路统一收口完成。

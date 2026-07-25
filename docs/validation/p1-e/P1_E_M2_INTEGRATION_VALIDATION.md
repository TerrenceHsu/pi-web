# P1-E M2 Frontend Integration Validation

> **状态**：⛔ M2-4 BLOCKED — 生产缺陷阻断完整 E2E 验证
> **基线 commit**：master `c0792e1`（P1-E M2-3 FROZEN）
> **验证日期**：2026-07-26
> **范围**：M2-1～M2-3 集成验证（unit + backend + E2E）

---

## 0. 最终判定

```
M2-4 Integration Validation
⛔ BLOCKED

阻塞项：
  P1-E-DEFECT-001（CRITICAL）：前端 requestJson 从不设置 X-PI-Agent-UI header，
                                导致所有 Provider API 调用在产品环境返回 400
                                missing_ui_header，binding 永远进入 error 状态，
                                ChatInput.providerReady 永远 false，发送按钮永久禁用。

  P1-E-DEFECT-002（HIGH）：前端 requestJson 错误转换对结构化后端错误体（含
                            `error` 字段为对象的情况）直接 String() 化，UI
                            展示为 "[object Object]"。影响所有 400/409/503
                            带结构化 error 体的响应，不止 missing_ui_header。
                            见 §1.7。

  阻断的核心场景：
    - 场景 A：App 启动后 binding 加载——前端能 GET 但被后端拒绝
    - 场景 H：reload 后 binding 恢复——同上
    - 场景 I：Settings → Apply——同上（PUT binding 也被拒绝）
    - 场景 F：Prompt 实际归属——ChatInput 发送按钮无法点击
    - 场景 E：请求运行期间 Selector/Settings 门控——前置条件无法满足
    - 场景 G：invalid binding 无 fallback——同上

 仍 PASS 的部分：
    - Frontend unit/integration：247 tests PASS（含 15 新增跨组件集成）
    - Backend regression：2585 passed + 1 skipped + 14 deselected
    - Backend 定向测试：144 passed（Provider runtime + Prompt + Regenerate + Revision）
    - typecheck / lint / build / prettier（新文件）：PASS
    - Production bundle：168.90 KB JS / 56.44 KB gzip / 46.11 KB CSS（与 M2-3 一致）
    - Production diff：0（仅 test + test fixture + 验证文档变更）
```

### 0.1 修复路径决议（user 2026-07-26）

```
P1-E Backend Foundation       ✅ FROZEN @ cad7ca7
P1-E M1 Runtime               ✅ FROZEN @ 8b0fb13
M2-0 Frontend Integration     ✅ FROZEN @ 87d7a8e
M2-1 API Types + providerStore ✅ FROZEN @ 313f28b
M2-2 Provider Settings Modal  ✅ FROZEN @ 0626ac4
M2-3 Provider Selector        ✅ FROZEN @ c0792e1

M2-4 Integration Validation   ⛔ BLOCKED — P1-E-DEFECT-001 / 002
M2-F1 Trusted UI Header Repair ✅ APPROVED TO START
M2-4 Re-validation            ⛔ BLOCKED BY M2-F1
M3 Unified Freeze             ⛔ BLOCKED BY M2-4
```

不解冻 M2-3。缺陷位于共享传输层 `src/api/client.ts`，不属于 Selector。新增独立纠错阶段 M2-F1，审计链更清晰。

---

## 1. 缺陷详情：P1-E-DEFECT-001

### 1.1 缺陷位置

| 文件 | 行 | 问题 |
|---|---|---|
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 45 | `requestJson` 的 headers 初始化为 `{ Accept: "application/json" }`——从不添加 `X-PI-Agent-UI: 1` |
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 112 | `uploadForm` 同样无 UI header |
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 170 | `requestBlob` 同样无 UI header |
| `src/pi_agent_core_py/web/frontend/src/stores/chatStore.ts` | 572 | 内嵌 fetch 也没添加 UI header |

Grep 验证：`grep -rn "X-PI-Agent-UI" src/pi_agent_core_py/web/frontend/` → **0 命中**。

### 1.2 复现步骤

1. 启动测试 web app（已配置 `enable_trusted_host=True`，见 §6 修改清单）：

   ```bash
   cd tests/e2e
   /d/miniconda/envs/pipy/python.exe start_test_web_app.py
   ```

2. 应用启动后，浏览器访问 `http://127.0.0.1:8000/`

3. 观察 ProviderSelector 区域显示错误 `[object Object]`（来自 400 响应）。

4. 直接 curl 验证（不带 header）：

   ```bash
   curl -i http://127.0.0.1:8000/api/sessions/sess-XXX/model-binding
   HTTP/1.1 400 Bad Request
   {"error":{"code":"missing_ui_header","message":"X-PI-Agent-UI header is required."}}
   ```

5. curl 验证（带 header）：

   ```bash
   curl -i -H "X-PI-Agent-UI: 1" \
     http://127.0.0.1:8000/api/sessions/sess-XXX/model-binding
   HTTP/1.1 200 OK
   {"binding":null}
   ```

6. 浏览器 DevTools Network 面板：前端 GET `/api/sessions/{sid}/model-binding` 的 Request Headers 无 `X-PI-Agent-UI`。

### 1.3 预期 vs 实际

| 项 | 预期 | 实际 |
|---|---|---|
| Frontend `requestJson` 行为 | 自动添加 `X-PI-Agent-UI: 1` 到所有 Provider endpoint 请求 | 从不添加该 header |
| GET `/api/sessions/{sid}/model-binding` 返回 | 200 + `{binding: null}` 或 `{binding: {...}}` | 400 + `{error: {code: "missing_ui_header", ...}}` |
| `providerStore.bindingLoadState` | 启动后进入 `loaded` | 启动后进入 `error` |
| `providerStore.bindingLoadError` | null | `[object Object]`（来自 ApiError.detail 被 String() 化） |
| `providerStore.canSendPrompt` | true（null binding → Legacy 允许） | false（state=error → 永久 false） |
| ChatInput 发送按钮 | 启用 | 永久 disabled |
| ProviderSelector 渲染 | 显示当前 Profile 或 Legacy hint | 显示 alert 错误文案 |

### 1.4 设计契约 vs 实现

`docs/design/p1-e-m2-frontend-integration.md` §6.5（"关键 API 行为事实"）明确：

> Auth header？必须要有 `X-PI-Agent-UI: 1`，否则 400 `missing_ui_header`

但 §16（"安全边界"）和 §18.2（"允许修改文件清单"）未指定前端在何处添加此 header——M2-1 实施 `api/providers.ts` + `api/client.ts` 时遗漏了。

### 1.5 影响范围（仅生产环境）

- M2-1（API types + providerStore）：所有调用 `getX()` 的 store action 都受影响
- M2-2（Provider Settings Modal）：所有 GET / POST / PATCH / PUT / DELETE 都被拒绝
- M2-3（ProviderSelector + ChatInput 门控）：binding 永久 error，发送按钮永久 disabled

Vitest 测试不受影响：它们 `vi.mock("../api/providers")`，从不调真实 backend，所以 vitest 247 个测试无法发现此缺陷。Backend pytest 不受影响：用 TestClient 显式设 headers。

### 1.6 阻断 M2-4 的原因

E2E 测试基础设施已就绪（6 spec / 37 测试基线），但 4 个依赖 `send-button` 的测试在 M2-3 之后开始 timeout（send-button 永久 disabled）。这 4 个测试本身就是缺陷的最小失败证据：

- `web-claude-smoke.spec.ts:63` — Smoke 2: chat（New chat → Send）
- `web-claude-smoke.spec.ts:102` — Smoke 3: file upload（Send 后保留 chip）
- `web-claude-smoke.spec.ts:245` — Smoke 6: skill upload + Use this turn → Send
- `web-claude-smoke.spec.ts:350` — Smoke 8: Stop button（Send 后变 Stop）

Per §25 验收条件 #25（"Playwright 两次连续通过"）和 #22（"外部 Provider 请求为 0"）无法满足。

### 1.7 缺陷详情：P1-E-DEFECT-002

#### 缺陷位置

| 文件 | 行 | 问题 |
|---|---|---|
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 81-86 | `requestJson` 错误转换：`(payload.detail \|\| payload.error)` 在 `payload.error` 为对象时返回对象；`String(detail)` 得 "[object Object]" |
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 132-137 | `uploadForm` 同模式 |
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 185-189 | `requestBlob` 同模式（无 string fallback 路径） |

#### 复现步骤

1. 任何返回结构化 `error` 体的 backend 错误（含 `missing_ui_header` / `invalid_origin` / `credential_conflict` / `profile_in_use` / `provider_config_unavailable` 等）。
2. 前端 `requestJson` 解析 payload，`payload.error` 是 `{code, message}` 对象。
3. `(payload.detail || payload.error)` 短路返回该对象。
4. `String(obj)` 在 `ApiError` ctor 中变成 `"[object Object]"`。
5. `providerStore.toSafeProviderError` 见 `typeof detail === "string"` → 返回该字符串。
6. UI 渲染 `bindingLoadError` 或 `mutationError` 显示 `"[object Object]"`。

#### 预期 vs 实际

| 项 | 预期 | 实际 |
|---|---|---|
| 结构化错误（`{error: {code, message}}`）展示 | backend `message` 字段（安全固定文案） | "[object Object]" |
| 400 `missing_ui_header` 的 UI 文案 | "X-PI-Agent-UI header is required." | "[object Object]" |
| 409 `profile_in_use` 的 UI 文案 | "Profile is in use—unbind sessions first" | "[object Object]" |

#### 安全约束（M2-F1 必须遵守）

- 使用 backend `error.message` 字段（已是安全固定文案）或安全 fallback
- 不渲染完整 response body
- 不使用任意 `String(obj)`
- 不暴露 `error.code` 内部字段（debug 用，不进 UI）

#### 与 DEFECT-001 的关系

DEFECT-001（header 缺失）的 UI 表现之所以是 "[object Object]"，正是因为 DEFECT-002（错误转换）。两个缺陷独立存在——修复 DEFECT-001 后，若其它结构化错误路径未被覆盖，DEFECT-002 仍会暴露。

M2-F1 应一并修复 DEFECT-001 + DEFECT-002，但需独立测试。

---

## 2. 验证范围

### 2.1 已验证（PASS）

| 项 | 结果 |
|---|---|
| Frontend vitest（含 15 新增） | ✅ 247 passed / 9 files |
| Frontend typecheck | ✅ PASS（vue-tsc --noEmit） |
| Frontend lint | ✅ PASS（max-warnings=0） |
| Frontend build | ✅ PASS（168.90 KB JS / 46.11 KB CSS / 56.44 KB gzip） |
| Frontend prettier（新文件） | ✅ PASS |
| Backend pytest 全量 | ✅ 2585 passed + 1 skipped + 14 deselected |
| Backend 定向：Provider runtime + binding + Prompt async + Regenerate + Revision 持久化 | ✅ 144 passed |
| Production diff 审计 | ✅ 0（仅 test + test fixture + 验证文档） |
| Secret marker（vitest 层面） | ✅ 0 持久泄漏（DOM / Pinia / storage / 跨 unmount） |
| 外部 Provider 网络请求（vitest 层面） | ✅ 0（window.fetch 未被调用） |

### 2.2 BLOCKED（无法验证）

| 项 | 原因 |
|---|---|
| E2E baseline 37 测试连续两次通过 | 4 个依赖 send-button 的测试因 P1-E-DEFECT-001 timeout |
| E2E 场景 A：App 启动 binding 加载 | 同上 |
| E2E 场景 H：reload 后 binding 恢复 | 同上 |
| E2E 场景 I：Settings → Apply → ChatInput 重新启用 | Provider API 全部 400 |
| E2E 场景 F：Prompt 实际归属（Qwen / Kimi） | 发送按钮无法点击 |
| E2E Provider 切换 Provider routing | FakeClient 不模拟 Provider 路由（这是设计预期） |

### 2.3 排除范围（如设计文档 §15.12 所述）

- E2E 在主仓库 `pi-py` 执行：本副本 E2E 基线存在，但无法跑 Provider 完整链路
- 真实外部 Provider 网络验证：禁止（设计约束）

---

## 3. 测试环境

| 组件 | 版本 / 配置 |
|---|---|
| OS | Windows 11 Pro 10.0.26200 |
| Python | 3.12（conda env `pipy`） |
| Node | 系统 Node + tests/e2e 独立 npm 环境 |
| Playwright | 1.48+（chromium-1228） |
| 测试 DB | 临时 sqlite（start_test_web_app.py mkdtemp） |
| FakeClient | delayed（每 delta 75-150ms，至少 4 delta） |
| Build mode | `npm run build:e2e`（暴露 `__storeHooks`） |

---

## 4. Frontend unit/integration 结果

### 4.1 全套结果

```
Test Files  9 passed (9)
     Tests  247 passed (247)
```

### 4.2 现有 232 测试零回归

| spec | 用例数 | 状态 |
|---|---|---|
| ChatInput.spec.ts | 11 | ✅ |
| ChatPanel.spec.ts | 10 | ✅ |
| MessageBubble.spec.ts | 10 | ✅ |
| ProfileForm.spec.ts | 45 | ✅ |
| ProviderSelector.spec.ts | 41 | ✅ |
| ProviderSettingsModal.spec.ts | 29 | ✅ |
| providerStore.spec.ts | 58 | ✅ |
| providersApi.spec.ts | 28 | ✅ |
| **小计** | **232** | **PASS** |

### 4.3 新增跨组件集成测试

**文件**：`src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts`

**用例数**：15

| 场景 | 用例 | 验证点 |
|---|---|---|
| A: startup lifecycle | 3 | binding 状态机推进驱动 Selector / ChatInput / 错误展示同步 |
| B: A/B 切换 + stale response | 2 | deferred promise 模拟乱序响应；currentBinding 切 session 立即 null |
| E: 请求运行期间门控 | 2 | chatStore.sending + streaming + currentRequestId 让 Selector / Sidebar 入口 disabled |
| G: invalid binding 不 fallback | 2 | needs_key / missing profile 时 usableProfiles 仍含其它 ready，但 binding 不自动切 |
| H: unmount + remount + storage | 2 | store 重新调 initialize；localStorage / sessionStorage 不写 Provider 数据 |
| I: Settings → Apply → 重新启用 | 2 | null → ready 切换；Apply 失败不 optimistic |
| Secret marker safety | 1 | `<M2-4 test secret marker>` 保存后从 DOM / Pinia / storage 消失 |
| 外部网络审计 | 1 | window.fetch 全程不被调用 |

**Mock 边界**：仅 mock `api/providers` + `api/client`；ChatPanel / SessionSidebar / ProviderSettingsModal 全部用真实实现。

**为什么这 15 个测试不暴露 P1-E-DEFECT-001**：它们 mock 了 API client，从不调真实 backend，因此无法发现 header 缺失。这是 vitest 局限性——必须靠 E2E 才能发现。

---

## 5. Backend 回归与定向测试

### 5.1 全量回归

```
pytest tests/ -m "not slow and not integration and not docker" --no-cov -q
→ 2585 passed, 1 skipped, 14 deselected, 57 warnings in 180.76s
```

与 M1 / M2-1 / M2-2 / M2-3 baseline 完全一致。

### 5.2 定向测试（覆盖 Provider runtime + Prompt + Regenerate + Revision）

```
pytest tests/test_provider_runtime_binding.py \
       tests/test_provider_runtime_selection.py \
       tests/test_prompt_provider_runtime_switching.py \
       tests/test_prompt_provider_runtime_integration.py \
       tests/test_regenerate_provider_runtime_switching.py \
       tests/test_regenerate_provider_runtime_integration.py \
       tests/test_provider_session_binding_api.py \
       tests/test_provider_default_binding_compensation.py \
       tests/test_provider_default_binding_integration.py \
       tests/test_extension_store_message_revisions.py \
       -m "not slow and not integration and not docker" --no-cov -q
→ 144 passed
```

**覆盖语义**：

- Provider runtime selection（disabled / needs_credential / needs_key / legacy None 路径）
- Prompt async provider switching（binding 改变后下次 Prompt 用新 Provider）
- Regenerate provider switching（用执行时当前 binding，非旧回答 Provider）
- Revision provider / model 持久化（每条 AssistantMessage 记录实际 provider / model）
- Session binding API（GET 返回 null vs binding；PUT 校验 profile_enabled）
- Default binding 初始化（POST /api/sessions 时按 is_default profile 自动物化 binding）
- Default binding 补偿（profile 改 is_default 后已有 binding 不被静默改）

### 5.3 为什么 backend 测试不暴露 P1-E-DEFECT-001

backend pytest 用 TestClient + 显式 headers：

```python
def _ui() -> dict:
    return {"X-PI-Agent-UI": "1"}
```

所以 backend 层面 UI header 检查工作正常。问题只在 frontend→backend 真实链路。

---

## 6. 修改文件清单（仅 test + test fixture + 验证文档）

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts` | 新增 | 15 个跨组件集成测试 |
| `tests/e2e/start_test_web_app.py` | 修改 | 加 `enable_trusted_host=True`——让 Provider API 在 E2E 环境自动启用（resolver 的 conservative auto 要求 4 个前置条件之一）；不加则所有 Provider endpoint 404 |
| `docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md` | 新增 | 本 BLOCKED 报告 |

**Production 文件 diff**：0
- `src/pi_agent_core_py/web/frontend/src/**`：未修改
- `src/pi_agent_core_py/**/*.py`（backend 生产代码）：未修改
- `package.json` / `package-lock.json`：未修改
- `vitest.config.ts`：未修改
- DB schema / API schema：未修改

---

## 7. 静态 diff 审计

```bash
git diff c0792e1 -- src/pi_agent_core_py/web/frontend/src src/pi_agent_core_py package.json package-lock.json
# 输出：空（0 production diff）

git diff --check
# 输出：空

git status --short
# M tests/e2e/start_test_web_app.py
# ?? src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts
# ?? docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md
```

---

## 8. Secret marker 六类扫描

Marker：`<M2-4 test secret marker>`（仅用于 vitest Secret 测试；不在文档内展开具体值，避免文档成为命中源）

| 类别 | 结果 |
|---|---|
| Production source marker | 0（`grep -r M2-4-SECRET src/pi_agent_core_py/web/frontend/src/`） |
| Runtime Pinia state marker | 0（vitest 断言 JSON.stringify(state) 不含） |
| DOM after Modal close marker | 0（vitest 断言 document.body.innerHTML 不含） |
| Storage marker（localStorage + sessionStorage） | 0（vitest 断言） |
| Console / log marker | 0（vitest 全程未 console.log payload） |
| Test fixture marker | 1 处（`ProviderFrontendIntegration.spec.ts` 中常量定义，符合预期） |

**全工作树重扫（marker 字面值）**：

```bash
$ grep -rln "M2-4-SECRET-MARKER-DO-NOT-PERSIST" .
src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts
# 仅 1 命中——预期 test fixture；docs / tests/e2e / artifact 均 0
```

**E2E marker 扫描**：BLOCKED（无法跑 Provider Modal E2E）。

---

## 9. 外部网络审计

### 9.1 vitest 层面

`ProviderFrontendIntegration.spec.ts > External Provider network` 用例：

```ts
const fetchSpy = vi.spyOn(window as any, "fetch")
// ...完整集成流程...
expect(fetchSpy).not.toHaveBeenCalled()
```

✅ 通过——所有 API 调用走 mock，0 真实网络。

### 9.2 E2E 层面

BLOCKED——E2E 无法跑通，无法审计 trace / HAR 中的外部 host。但根据 vitest 层面 + backend FakeClient 不发外部网络的事实，可以推断：

- External Provider requests（生产路径）：0
- Localhost backend 请求：N（正常）
- 外部 host（api.anthropic.com / open.bigmodel.cn / dashscope.aliyuncs.com / api.moonshot.cn）：0

---

## 10. 已知限制

1. **P1-E-DEFECT-001 + DEFECT-002 未修复**——M2-3 production 冻结，不在 M2-4 内修复；M2-F1 独立处理。
2. **E2E Provider 链路未验证**——依赖 M2-F1 修复后才能完整跑通。
3. **FakeClient 不模拟 Provider 路由**——E2E 即使修复 header 也无法验证"Prompt 实际使用当前 binding 的 Provider"。这部分由 backend 144 个定向测试覆盖。
4. **reload 恢复 E2E 未跑**——同 #2。
5. **`/api/provider-definitions` 裸数组偏差归档未做**——归档修正属于 Commit C（M2-4 Re-validation PASS 后）。

---

## 11. 给 M2-F1 的修复范围建议（非 BLOCKED 报告部分）

仅供 M2-F1 实施 reference。**未在 M2-4 BLOCKED 阶段执行**。

### 11.1 修复范围

- `src/pi_agent_core_py/web/frontend/src/api/client.ts`：UI header helper + 注入 + 错误转换
- 必要的 vitest 单元测试覆盖
- `chatStore.ts:572` 内嵌 fetch 路径需先审计 endpoint；若访问要求 UI Header 的同源 API，最小补充

### 11.2 Header helper 设计建议

```typescript
const UI_HEADER_NAME = "X-PI-Agent-UI"
const UI_HEADER_VALUE = "1"

function createUiHeaders(initial?: HeadersInit): Headers {
  const headers = new Headers(initial)
  headers.set(UI_HEADER_NAME, UI_HEADER_VALUE)
  return headers
}
```

约束：
- 只注入同源 UI Backend 请求（外部 Provider URL 不走此 helper）
- Header 值不能被 caller 覆盖或删除（`headers.set` 强制覆盖）
- 保留调用方已有 Header
- `uploadForm` 不手动设置 Content-Type（让浏览器生成 multipart boundary）
- 不新增真实网络调用

### 11.3 错误转换修复建议

```typescript
function safeErrorDetail(payload: unknown, statusText: string): string {
  if (typeof payload === "string" && payload.length > 0) return payload
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>
    // 优先用 backend 固定 message（已脱敏）
    const msg = obj.message
    if (typeof msg === "string" && msg.length > 0) return msg
    // error.message（FastAPI middleware 模式）
    const err = obj.error
    if (err && typeof err === "object") {
      const errMsg = (err as Record<string, unknown>).message
      if (typeof errMsg === "string" && errMsg.length > 0) return errMsg
    }
    // detail 字符串（FastAPI validation：detail 是 string）
    if (typeof obj.detail === "string" && obj.detail.length > 0) return obj.detail
    // detail array（FastAPI validation：detail 是 [{loc, msg, type}]）——不渲染
  }
  return statusText || "request failed"
}
```

约束：
- 不使用 `String(obj)` 兜底
- 不渲染 detail array（FastAPI validation 列表，可能含输入回显）
- 不暴露 error.code（debug 字段）

### 11.4 推荐测试覆盖

- `requestJson` 自动注入 UI Header
- `uploadForm` 自动注入 UI Header
- `requestBlob` 自动注入 UI Header
- 调用方 Header 仍然保留
- 调用方无法覆盖 `X-PI-Agent-UI`（强制 set）
- FormData 不被手动设置 Content-Type
- 结构化 API 错误（`{error: {code, message}}`）显示 backend message 而非 "[object Object]"
- detail array 不被渲染

### 11.5 推荐提交策略

- **Commit A**（M2-4 BLOCKED）：测试侧文件 + BLOCKED 报告——本文档所属
- **Commit B**（M2-F1）：`fix(web): attach trusted UI header to frontend requests`——生产修复 + 单测
- **Commit C**（M2-4 Re-validation PASS）：恢复运行 E2E + 更新本文档为 PASS + 归档 `/api/provider-definitions` 偏差

---

## 12. 阶段判定

```
M2-4 Integration Validation
⛔ BLOCKED — P1-E-DEFECT-001 / 002

仍未进入：
  M3 Unified Freeze

下一步（已 user-approved）：
  M2-F1 Trusted UI Header Repair
    → 修 DEFECT-001 + DEFECT-002
    → 重跑 M2-4 验证
    → PASS 后归档 + Commit C
    → 开放 M3
```

---

**M2-4 BLOCKED @ 2026-07-26**——P1-E-DEFECT-001 / 002 待 M2-F1 修复。

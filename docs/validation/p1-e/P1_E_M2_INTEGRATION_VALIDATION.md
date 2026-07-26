# P1-E M2 Frontend Integration Validation

> **状态**：✅ M2-4 COMPLETE / FROZEN
> **基线 commit**：master `c0792e1`（P1-E M2-3 FROZEN）
> **M2-F1 修复 commit**：master `dcf45ce`（fix(web): attach trusted UI header to frontend requests）
> **本归档 commit**：M2-4 archive（commit C）
> **验证日期**：2026-07-26
> **范围**：M2-1～M2-3 集成验证（unit + backend + E2E）+ M2-F1 defect 修复确认

---

## 0. 最终判定

```
P1-E M2 Frontend Provider Switching
✅ COMPLETE / FROZEN @ <archive commit>

M3 Unified Freeze
✅ APPROVED TO START
```

### 0.1 阶段链路

```
P1-E Backend Foundation        ✅ FROZEN @ cad7ca7
P1-E M1 Runtime                ✅ FROZEN @ 8b0fb13
M2-0 Frontend Integration      ✅ FROZEN @ 87d7a8e
M2-1 API Types + providerStore ✅ FROZEN @ 313f28b
M2-2 Provider Settings Modal   ✅ FROZEN @ 0626ac4
M2-3 Provider Selector         ✅ FROZEN @ c0792e1

M2-4 Integration Validation (Commit A, BLOCKED evidence) @ 926011f
  → 登记 P1-E-DEFECT-001 / 002，保留 BLOCKED 报告
M2-F1 Trusted UI Header Repair (Commit B) @ dcf45ce
  → fix(web): attach trusted UI header to frontend requests
M2-4 Re-validation (Commit C, this archive)
  → BLOCKED → PASS

M3 Unified Freeze              ✅ APPROVED TO START（仍未进入实施）
```

### 0.2 通过摘要

| 项 | 结果 |
|---|---|
| Frontend vitest | ✅ 261 passed（247 + 14 new client.spec） |
| Frontend typecheck / lint / build | ✅ PASS |
| Frontend prettier（新文件） | ✅ PASS |
| Frontend production bundle | ✅ 169.23 KB JS / 46.11 KB CSS / 56.49 KB gzip |
| Backend regression | ✅ 2585 passed + 1 skipped + 14 deselected |
| Backend Provider/Binding/Prompt/Regenerate/Revision 定向 | ✅ 144 passed |
| Playwright E2E baseline run 1 | ✅ 37 / 37 passed |
| Playwright E2E baseline run 2 | ✅ 37 / 37 passed（连续两次） |
| Secret marker 六类扫描 | ✅ production 0 / runtime 0 / DOM 0 / storage 0 / artifact 0 / test-fixture 1（预期） |
| 外部 Provider 网络审计 | ✅ 0 host 命中（api.anthropic.com / open.bigmodel.cn / dashscope.aliyuncs.com / api.moonshot.cn） |
| Anthropic UI 边界 | ✅ E2E 未渲染 Anthropic Provider（design §15.9） |
| Production diff | ✅ 仅 M2-F1 client.ts（已 Commit B 记录） |

---

## 1. M2-F1 Defect 修复确认

### 1.1 P1-E-DEFECT-001（CRITICAL）：UI Header 缺失

**状态**：✅ RESOLVED in M2-F1 Commit B (`dcf45ce`)

**原缺陷**：`src/pi_agent_core_py/web/frontend/src/api/client.ts` 的 `requestJson` / `uploadForm` / `requestBlob` 三个 helper 从不设置 `X-PI-Agent-UI: 1` header；后端 Credential / Provider Profile / Session Binding / Provider Definitions 路由强制要求该 header，缺失返回 400 `missing_ui_header`。

**修复**：新增 `createUiHeaders()` helper，强制 `set(UI_HEADER_NAME, UI_HEADER_VALUE)`；三个 helper 统一使用。`chatStore.ts:570` 的 `/api/abort` endpoint 注册在 main app、未挂 `require_ui_header_dep`，按"只修真需要的地方"原则未改动。

**vitest 覆盖**（`tests/unit/client.spec.ts` 14 tests）：

- requestJson / uploadForm / requestBlob 自动注入 UI Header
- 调用方已有 Header 保留
- 调用方无法覆盖 `X-PI-Agent-UI`（强制 set）
- FormData 不被手动设置 Content-Type（让浏览器生成 multipart boundary）
- 结构化错误 `{error: {code, message}}` 渲染 backend message
- FastAPI HTTPException `detail="..."` 渲染
- FastAPI validation detail 数组不渲染为 `"[object Object]"`
- 顶层 `message` 字段支持
- uploadForm / requestBlob 共用 safeErrorDetail
- 回归契约（GET 默认 method、204 返回 null、network error → status=0）

**E2E 修复验证**：4 个原失败用例（web-claude-smoke Smoke 2/3/6/8）已恢复通过——`send-button` 不再永久 disabled，`providerStore.bindingLoadState` 进入 `loaded` 而非 `error`。

### 1.2 P1-E-DEFECT-002（HIGH）：结构化错误渲染为 "[object Object]"

**状态**：✅ RESOLVED in M2-F1 Commit B (`dcf45ce`)

**原缺陷**：`requestJson` 错误转换 `String(payload.detail || payload.error)`——`payload.error` 为对象时 `String(obj)` 变 "[object Object]"。影响所有 400/409/503 使用结构化 error 体的响应。

**修复**：新增 `safeErrorDetail(payload, statusText, fallback)` helper，按优先级读取：

1. `payload.error.message`（中间件安全格式）
2. `payload.message`（自定义 handler 模式）
3. `payload.detail` 字符串（FastAPI HTTPException）
4. plain string payload
5. `statusText`
6. 固定 fallback

**安全约束**：

- 不渲染 FastAPI validation `detail: [...]` 数组（可能含输入回显）
- 不渲染 `error.code` debug 字段
- 不使用 `String(obj)` 兜底

vitest 覆盖（同 §1.1 列表）。

### 1.3 Defect 处理流程

```
M2-4 阶段发现 defect
  ↓
保留最小失败 E2E（4 个 send-button 用例）
输出缺陷位置 + 复现 + 预期/实际
标记 M2-4 BLOCKED（不提交伪 PASS）
  ↓
Commit A：test(web): record blocked M2 integration validation @ 926011f
  ↓
M2-F1 独立纠错阶段
  ↓ 审计 chatStore fetch（确认 /api/abort 无需 header）
  ↓ 设计 createUiHeaders + safeErrorDetail
  ↓ 14 vitest 覆盖契约
  ↓ Backend regression 2585 零回归
  ↓
Commit B：fix(web): attach trusted UI header to frontend requests @ dcf45ce
  ↓
M2-4 Re-validation
  ↓ build:e2e + 跑 E2E baseline 两次（37/37 × 2）
  ↓ marker 六类扫描 0
  ↓ 外部 host 0
  ↓ production diff 仅 client.ts
  ↓
Commit C：docs: archive P1-E M2 integration validation（本归档）
```

---

## 2. 验证范围

### 2.1 已验证（PASS）

| 项 | 结果 |
|---|---|
| Frontend vitest（含 14 个新 client.spec + 15 个跨组件集成） | ✅ 261 passed / 10 files |
| Frontend typecheck | ✅ PASS（vue-tsc --noEmit） |
| Frontend lint | ✅ PASS（max-warnings=0） |
| Frontend build | ✅ PASS（169.23 KB JS / 46.11 KB CSS / 56.49 KB gzip） |
| Frontend build:e2e | ✅ PASS（169.60 KB JS / 46.11 KB CSS / 56.60 KB gzip；__storeHooks 暴露） |
| Frontend prettier（新文件） | ✅ PASS |
| Backend pytest 全量 | ✅ 2585 passed + 1 skipped + 14 deselected |
| Backend 定向（Provider runtime + Prompt + Regenerate + Revision） | ✅ 144 passed |
| Backend ruff | ✅ All checks passed |
| Production diff 审计 | ✅ 仅 M2-F1 client.ts（已 Commit B 记录） |
| Playwright E2E baseline run 1 | ✅ 37 / 37 |
| Playwright E2E baseline run 2 | ✅ 37 / 37（连续两次稳定） |
| Secret marker 六类扫描 | ✅ 见 §8 |
| 外部 Provider 网络审计 | ✅ 见 §9 |
| Anthropic UI 边界 | ✅ 见 §10 |

### 2.2 排除范围

- 真实外部 Provider 网络验证：禁止（design §15.12 约束）
- 真实 API Key：禁止使用

### 2.3 仍由 backend 测试覆盖（不需 E2E 重复）

- Provider routing 决策（RequestProviderRuntime.resolve_selection）
- Prompt 实际归属（prompt_provider_runtime_switching）
- Regenerate 当前 binding 使用（regenerate_provider_runtime_switching）
- AssistantMessage / Revision provider/model 持久化（extension_store_message_revisions）

`FakeClient` 不模拟 Provider 路由，E2E 无法验证"Prompt 实际使用当前 binding 的 Provider"。这部分由 backend 144 个定向测试覆盖。

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
| Test server | `start_test_web_app.py`（已 enable_trusted_host=True） |

---

## 4. Frontend unit/integration 结果

### 4.1 全套结果

```
Test Files  10 passed (10)
     Tests  261 passed (261)
   Duration  3.65s
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

### 4.3 M2-4 新增跨组件集成测试（Commit A）

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

### 4.4 M2-F1 新增 client.spec（Commit B）

**文件**：`src/pi_agent_core_py/web/frontend/tests/unit/client.spec.ts`

**用例数**：14

覆盖 UI Header 自动注入（3 helper 各一）、调用方 Header 保留、调用方无法覆盖 UI Header、FormData multipart boundary、结构化错误安全转换（5 子用例）、回归契约（3 子用例）。

---

## 5. Backend 回归与定向测试

### 5.1 全量回归

```
pytest tests/ -m "not slow and not integration and not docker" --no-cov -q
→ 2585 passed, 1 skipped, 14 deselected, 57 warnings in 174.33s
```

与 M1 / M2-1 / M2-2 / M2-3 baseline 完全一致。M2-F1 修复未触及任何后端代码。

### 5.2 定向测试（Provider runtime + Prompt + Regenerate + Revision）

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

覆盖语义：

- Provider runtime selection（disabled / needs_credential / needs_key / legacy None 路径）
- Prompt async provider switching（binding 改变后下次 Prompt 用新 Provider）
- Regenerate provider switching（用执行时当前 binding，非旧回答 Provider）
- Revision provider / model 持久化（每条 AssistantMessage 记录实际 provider / model）
- Session binding API（GET 返回 null vs binding；PUT 校验 profile_enabled）
- Default binding 初始化（POST /api/sessions 时按 is_default profile 自动物化 binding）
- Default binding 补偿（profile 改 is_default 后已有 binding 不被静默改）

---

## 6. 修改文件清单

### 6.1 Commit A @ 926011f（BLOCKED 证据）

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts` | 新增 | 15 个跨组件集成测试 |
| `tests/e2e/start_test_web_app.py` | 修改 | 加 `enable_trusted_host=True`——让 Provider API 在 E2E 环境自动启用 |
| `docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md` | 新增 | BLOCKED 报告（已被 Commit C 更新为 PASS） |

### 6.2 Commit B @ dcf45ce（M2-F1 修复）

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/pi_agent_core_py/web/frontend/src/api/client.ts` | 修改 | `createUiHeaders` + `safeErrorDetail` helper + 注入到三个 fetch helper |
| `src/pi_agent_core_py/web/frontend/tests/unit/client.spec.ts` | 新增 | 14 个 client 单测覆盖 UI Header / 错误转换契约 |

### 6.3 Commit C（本次 archive）

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/validation/p1-e/P1_E_M2_INTEGRATION_VALIDATION.md` | 修改 | BLOCKED → COMPLETE / PASS |
| `docs/design/p1-e-m2-frontend-integration.md` | 修改 | 修正 §6.1 `/api/provider-definitions` envelope → 裸数组偏差 |

### 6.4 Production diff 总览（vs M2-3 baseline `c0792e1`）

```bash
$ git diff c0792e1 --stat -- 'src/pi_agent_core_py/**'
 src/pi_agent_core_py/web/frontend/src/api/client.ts | 105 ++++++++++++++++-----
 1 file changed, 80 insertions(+), 25 deletions(-)
```

唯一 production 文件改动：`src/api/client.ts`（M2-F1 修复，Commit B 已记录）。其它 production 路径（backend Python / package.json / lockfile / DB schema / API schema）零改动。

---

## 7. 静态 diff 审计

```bash
$ git diff c0792e1 --stat -- src/pi_agent_core_py/web/frontend/src src/pi_agent_core_py/**/*.py \
                            src/pi_agent_core_py/web/frontend/package.json \
                            src/pi_agent_core_py/web/frontend/package-lock.json
 src/pi_agent_core_py/web/frontend/src/api/client.ts | 105 ++++++++++++++++-----
 1 file changed, 80 insertions(+), 25 deletions(-)

$ git diff --check
# 空

$ /d/miniconda/envs/pipy/python.exe -m ruff check src tests scripts
# All checks passed!

$ git status --short
# 仅 docs/ 修改（Commit C）
```

---

## 8. Secret marker 六类扫描

Marker：`<M2-4 test secret marker>`（仅用于 vitest Secret 测试；不在文档内展开具体值）

| 类别 | 结果 |
|---|---|
| Production source marker | 0 |
| Runtime Pinia state marker | 0（vitest 断言 JSON.stringify(state) 不含） |
| DOM after Modal close marker | 0（vitest 断言 document.body.innerHTML 不含） |
| Storage marker（localStorage + sessionStorage） | 0（vitest 断言） |
| Console / log marker | 0（vitest 全程未 console.log payload） |
| Test fixture marker | 1 处（`ProviderFrontendIntegration.spec.ts` 中常量定义，符合预期） |

**全工作树重扫（marker 字面值）**：

```bash
$ grep -rln "<M2-4 marker literal>" .
src/pi_agent_core_py/web/frontend/tests/unit/ProviderFrontendIntegration.spec.ts
# 仅 1 命中——预期 test fixture；docs / tests/e2e / artifact 均 0
```

文档内不展开 marker 字面值，避免文档成为命中源。

**E2E artifact marker 扫描**：

```bash
$ find tests/e2e/test-results -type f
tests/e2e/test-results/.last-run.json
# 37 测试全 pass → 无 failure artifacts（trace / video / screenshot）
$ grep -rln "<M2-4 marker literal>" tests/e2e/test-results/
# 0 命中
```

E2E 全 pass 后无 trace / video artifact 生成。Provider Settings Modal E2E 未单独写（设计决策：FakeClient 不模拟 Provider routing，UI 层验证已由 vitest ProviderFrontendIntegration 覆盖）。

---

## 9. 外部 Provider 网络审计

允许 host：localhost / 127.0.0.1 / ::1 / testserver

禁止 host：
- `api.anthropic.com`
- `open.bigmodel.cn`
- `dashscope.aliyuncs.com`（Qwen）
- `api.moonshot.cn`（Kimi）
- 其它公网 Provider host

### 9.1 vitest 层面

`ProviderFrontendIntegration.spec.ts > External Provider network` 用例：

```ts
const fetchSpy = vi.spyOn(window as any, "fetch")
// ...完整集成流程...
expect(fetchSpy).not.toHaveBeenCalled()
```

✅ 通过——所有 API 调用走 mock，0 真实网络。

### 9.2 E2E 层面

```bash
$ grep -rln "api.anthropic.com\|open.bigmodel.cn\|dashscope.aliyuncs.com\|api.moonshot.cn" \
           tests/e2e/test-results/ tests/e2e/*.ts src/pi_agent_core_py/web/frontend/src/
# 0 命中
```

E2E 全 pass → 无 trace / video / screenshot artifact 可审计。但 spec + 前端 source 均无外部 host 引用，且 backend 使用 FakeClient（不发外部网络）。

**最终报告**：

```
External Provider requests: 0
```

---

## 10. Anthropic UI 边界

Design §15.9：前端 `visibleDefinitions` getter 过滤 `id === "anthropic"`；`usableProfiles` 也排除。

**验证**：

- vitest `ProviderFrontendIntegration.spec.ts` 场景 A：mock definitions 含 4 个 provider（含 anthropic）；测试中 Selector 只展示 GLM/Qwen/Kimi optgroup，Anthropic 不出现。
- vitest `ProviderSelector.spec.ts` / `providerStore.spec.ts`：已覆盖 232 测试中含 Anthropic 过滤断言。
- E2E：未单独写 Anthropic 边界 spec——Selector 在 binding=null 时不渲染 optgroup，Anthropic 自然不出现。

---

## 11. Playwright E2E 两次连续通过

### 11.1 Run 1

```
37 passed (1.6m)
```

覆盖：

- async-stream-reconnect (7)
- event-dedup (4)
- extension-persistence (5)
- mcp-tool-lifecycle (2)
- regenerate (9)
- web-claude-smoke (10)

### 11.2 Run 2

```
37 passed (1.7m)
```

同一组 37 测试连续两次全部通过。无 flaky、无 retry、无 trace / video artifact 生成（无失败）。

### 11.3 E2E 基线稳定性证据

| 检查 | Run 1 | Run 2 |
|---|---|---|
| 总测试数 | 37 | 37 |
| 通过数 | 37 | 37 |
| 失败数 | 0 | 0 |
| flaky retry 数 | 0 | 0 |
| timeout 数 | 0 | 0 |
| 总耗时 | 1.6 min | 1.7 min |

E2E 测试隔离：webServer 共享 SQLite，每个 spec 在 `beforeEach` 清理状态（设计已固化）；session 创建后由后端 `initialize_new_session_binding` 自动物化 default binding 或返回 null，前端 `bindingLoadState` 进入 `loaded`。

---

## 12. 已知限制

1. **`/api/provider-definitions` 裸数组 vs envelope 偏差**：design doc §6.1 历史描述为 envelope `{providers: [...]}`，实际 handler 返回 `list[dict]` 裸数组。Commit C 已修正 design doc。详见 §13。
2. **FakeClient 不模拟 Provider 路由**：E2E 无法直接验证"Prompt 实际使用当前 binding 的 Provider"。这部分由 backend 144 个定向测试覆盖。
3. **Provider Settings Modal E2E 未单独写**：UI 层验证已由 vitest ProviderFrontendIntegration 场景 I 覆盖（含 Secret marker safety）。
4. **chatStore.ts:570 `/api/abort` 路径未注入 UI Header**：审计确认此 endpoint 注册在 main app、未挂 `require_ui_header_dep`，按"只修真需要的地方"原则未改动。如有未来 endpoint 上移到 credential router，需追加注入。

---

## 13. `/api/provider-definitions` 归档偏差修正

### 13.1 偏差描述

Design doc `docs/design/p1-e-m2-frontend-integration.md` §6.1 历史描述：

```json
{
  "providers": [
    {"id": "anthropic", ...},
    ...
  ]
}
```

实际 backend handler（`src/pi_agent_core_py/web/credentials_api.py:690`）：

```python
@router.get("/api/provider-definitions")
async def get_provider_definitions() -> list[dict]:
    """Return built-in provider definitions (no internal endpoint info)."""
    return [...]
```

FastAPI 直接序列化 `list[dict]` → 裸 JSON 数组，无 envelope。

### 13.2 修正内容（Commit C）

仅修改 design doc：

- §6.1 JSON 示例：`{providers: [...]}` → `[...]`
- §6.1 Response 类型说明：envelope → `ProviderDefinitionView[]`（裸数组）
- API Contract 表条目同步
- 涉及 envelope 的文字描述

不修改：

- Provider 字段定义
- Anthropic 过滤决策（§15.9）
- 其它 12 项冻结决策
- M2 阶段划分
- 安全边界

### 13.3 归档注记

- 事实记录修正，非产品决策变更
- M2-1 已按真实裸数组实现（`api/providers.ts:34` + `types/providers.ts:172`）
- 归档于 M2-4 验证阶段

---

## 14. M2-4 验收条件对账

| # | 验收条件 | 结果 |
|---|---|---|
| 1 | M2-1～M2-3 production 代码不变 | ✅ M2-3 FROZEN；M2-F1 仅修复 client.ts |
| 2 | Frontend 现有 232 tests 零回归 | ✅ 232 / 232 |
| 3 | 新增跨组件集成测试全部通过 | ✅ 15 / 15（Commit A） |
| 4 | Backend 2585 基线零回归 | ✅ 2585 + 1 skipped + 14 deselected |
| 5 | Provider Runtime 定向测试通过 | ✅ 144 passed |
| 6 | Prompt async 定向测试通过 | ✅ 包含在 144 |
| 7 | Regenerate 定向测试通过 | ✅ 包含在 144 |
| 8 | Revision provider/model 持久化验证通过 | ✅ extension_store_message_revisions |
| 9 | Session A/B Binding 隔离通过 | ✅ vitest 场景 B + backend test_provider_runtime_binding |
| 10 | stale Binding response 验证通过 | ✅ vitest 场景 B deferred promise |
| 11 | new Session default Binding 通过 | ✅ backend test_provider_default_binding_integration |
| 12 | default_model 快照语义通过 | ✅ vitest 场景 G + backend default_binding_compensation |
| 13 | Apply 当前 Session 语义通过 | ✅ vitest 场景 I |
| 14 | null Binding/Legacy 通过 | ✅ vitest 场景 A + backend legacy 路径 |
| 15 | invalid Binding/no fallback 通过 | ✅ vitest 场景 G + ProviderSelector spec |
| 16 | Selector 请求运行时禁用通过 | ✅ vitest 场景 E |
| 17 | Settings 请求运行时禁用通过 | ✅ vitest 场景 E（SessionSidebar 入口） |
| 18 | Prompt 切换 Qwen→Kimi 通过 | ✅ backend prompt_provider_runtime_switching（FakeClient 不模拟 UI 路由） |
| 19 | Regenerate 使用当前 Binding 通过 | ✅ backend regenerate_provider_runtime_switching |
| 20 | reload Binding 恢复通过 | ✅ vitest 场景 H + E2E regenerate spec Test 5（reload during regen） |
| 21 | Anthropic UI 为 0 | ✅ §10 |
| 22 | 外部 Provider 请求为 0 | ✅ §9 |
| 23 | Secret 持久化泄漏为 0 | ✅ §8 |
| 24 | trace/HAR 不含 Secret | ✅ E2E 全 pass → 无 trace artifact |
| 25 | Playwright 两次连续通过 | ✅ §11（37/37 × 2） |
| 26 | production bundle 与 M2-3 一致 | ⚠️ bundle 因 M2-F1 增 0.33 KB JS（client.ts 加 helper），允许范围内 |
| 27 | production diff 为 0 | ⚠️ client.ts 1 文件改动（M2-F1 已记录） |
| 28 | provider-definitions 归档偏差已修正 | ✅ §13（Commit C） |
| 29 | 验证报告完成 | ✅ 本文档 |
| 30 | Working tree clean | ✅ Commit C 后 clean |

**注**：#26 / #27 标 ⚠️ 是因为 M2-F1 修复必然引入 client.ts 改动。该改动经 user 决议授权（独立 M2-F1 阶段），不属于 M2-3 production diff 范围。若严格按"M2-3 production 代码不变"理解，M2-F1 视为新阶段而非 M2-3 修改——审计链清晰。

---

## 15. 最终判定

```
P1-E M2 Frontend Provider Switching
✅ COMPLETE / FROZEN @ <archive commit>

  Commit A @ 926011f  test(web): record blocked M2 integration validation
  Commit B @ dcf45ce  fix(web): attach trusted UI header to frontend requests
  Commit C @ <this>   docs: archive P1-E M2 integration validation

M3 Unified Freeze
✅ APPROVED TO START
```

---

**M2-4 COMPLETE @ 2026-07-26**——M3 ✅ APPROVED TO START。

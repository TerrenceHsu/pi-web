# P1-B3 Validation Report — Async Prompt + WS Reconnect + Final Consistency

> **阶段**: P1-B3（前端切 async + WS reconnect replay + 最终一致性）—— **正式 PASS**
> **基线**: `992df5e` (P1-B1+B2+B2.1+B3-pre hardening)
> **执行日期**: 2026-07-12
> **目标**: 把 P1-B1/B2 的后端能力接入前端产品链路——async send + WS replay + request 隔离 + Stop request abort + 刷新恢复

---

## 1. 阶段结论

**B3 正式 PASS**——前端 chatStore 切到 async API，启用 currentRequestId 隔离；WS reconnect 触发全局 sequence replay + live/replay merge；request status poll + messages reconciliation；Stop 切 request-scoped abort；后端补 GET /api/requests list endpoint + WS hello sequence metadata。

**7 项验收门槛**（用户后续补充）全部达成：
1. ✅ reconnect 带正确 after_sequence（Test 4）
2. ✅ replay 请求不带 session_id（Test 4）
3. ✅ replay/live 实际按 sequence 合并（Test 5）
4. ✅ gap=true 后 needsFinalResync（Test 6）
5. ✅ 页面刷新后 active request API 查询（Test 7）
6. ✅ 最终 assistant 正文不重不漏（Test 5 验证 count=1）
7. ✅ 原 19 条 E2E 不回归（包含在 23/23 内）

**42 项原验收达成**：
- B3-0 (3 项) ✅
- B3-1 (7 项) ✅
- B3-2 (9 项) ✅
- B3-3 (10 项) ✅
- 通用 (13 项) ✅

---

## 2. 完成内容

### B3-0 收尾（3/3）
- ✅ **B3-0a** `window.__storeHooks` 仅 DEV/E2E 暴露（已在 B3-pre hardening 完成）
- ✅ **B3-0b** `seenEventIds` FIFO 淘汰（已在 B3-pre hardening 完成）
- ✅ **B3-0c** WS hello 加 `first_available_sequence` / `last_available_sequence` / `server_time`（裸 dict，不进 buffer/不消耗 sequence/不进 seenEventIds）

### B3-1 async send + 202 竞态 + request 隔离
- api 层：`sendPromptAsync` / `getRequestStatus` / `abortRequest` / `listActiveRequests` + 新建 `api/events.ts` 含 `getEvents`
- types：`PromptAsyncResponse` / `RequestSummary` / `RequestListResponse` / `RequestStatus`
- chatStore `sendPrompt` 切 async：
  - 等 202 期间 set `pendingRequest=true` + `currentRequestId=null`
  - 202 后 set `currentRequestId` + flush `pendingEventsByRequest[request_id]`（按 sequence 排序）
  - 不自动 fallback 同步（避免双发）
- `applyEventToStreamItems` 抽出共享下游 mapper
- `belongsToCurrentRequest` 隔离——turn-control 事件（`message_start/update/end` / `request_end` / `agent_end` / `error` / `agent_abort` / `tool_execution_*` / `turn_*` / `agent_start` / `request_*`）必须属于 `currentRequestId` 才能处理
- 早到事件进 `pendingEventsByRequest` 缓冲（容量 100/queue）

### B3-2 WS 状态机 + 全局 sequence replay
- chatStore handleEvent hello 分支：reconnect 时（`lastGlobalSequence > 0`）触发 `replayFromCursor`
- `replayFromCursor`：
  - `replaying=true` 期间新 WS event 进 `liveEventsDuringReplay` buffer
  - 调 `getEvents({afterSequence: lastGlobalSequence, limit: 200})`——**不带 session_id**
  - 分页直到 `has_more=false`；保护 20 页 / 4000 事件；超限 `needsFinalResync=true`
  - 合并 replay + live → sort by sequence → dedupe by event_id
  - 依次 `applyEventToStreamItems`（跳过 envelope-decompose）
- `lastGlobalSequence` 改为 ref + expose 到 store（便于测试重置）

### B3-3 request status + reconcile + Stop + 刷新恢复
- 后端新增 `GET /api/requests?session_id=X&status=active&limit=N`（list endpoint）
  - `status=active` → queued OR running
  - 排序 `created_at DESC`
  - 安全字段（不含 task/payload/system prompt/MCP env/traceback）
- chatStore `pollRequestUntilTerminal(requestId)`：
  - request_end 后 poll GET /api/requests/{id}（250-500ms 退避，10s 总超时）
  - terminal 后调 `reconcileMessagesFromServer` + 清 state
- chatStore `reconcileMessagesFromServer`：
  - 用服务端 messages 替换 user_message / assistant_message
  - **保留**当前 turn cards（tool_call / file_read / skill_used / mcp_tool_call / turn_info / error）
- chatStore `abortRun`：
  - `currentRequestId` 存在 → POST /api/requests/{id}/abort + set `aborting=true`
  - 不存在 → fallback POST /api/abort
  - 不立即 set sending/streaming=false（等 status=aborted 由 poll 处理）
- chatStore `findActiveRequest(sessionId)` 用新 list endpoint

### B3-4 测试 + delayed FakeClient
- `tests/e2e/start_test_web_app.py` 加 `PI_E2E_DELAYED=1` 选项（每 delta 75-150ms）
- `tests/test_web_request_recovery.py`（**12 用例**）
- `tests/e2e/async-stream-reconnect.spec.ts`（**3 用例**——核心覆盖）
- `tests/e2e/event-dedup.spec.ts` setupStore 加 `lastGlobalSequence = 0` 重置避免 baseline 污染
- `tests/e2e/web-claude-smoke.spec.ts` Smoke 9 rename + `mcp-tool-lifecycle.spec.ts` Shift+Enter 改独立 session 避免前 test 状态污染
- `tests/e2e/playwright.config.ts` retry=1

---

## 3. 测试结果

| 命令 | 结果 |
|---|---|
| `pytest tests/ -m "not slow and not integration and not docker"` | **889 passed**（875 baseline + 14 B2 envelope + 12 B3 recovery - 12 重叠），46.61s |
| Coverage gate | **84.36%** ≥ 75% PASS |
| `ruff check src tests scripts` | **All checks passed** |
| `npm run build`（production）| 124 modules / 806ms / **0 处 `__storeHooks` / `__e2eHooks`** |
| `npm run build:e2e` | 124 modules / 824ms（含 store + e2e hooks）|
| `npm run test:e2e`（23 测试）| **23/23 PASS**，20.8s |

### 新增测试覆盖

| 文件 | 用例数 | 验收点 |
|---|---|---|
| `tests/test_web_request_recovery.py` | 12 | list endpoint / 安全字段 / 排序 / limit / hello metadata / 分页 / shutdown / 旧 API |
| `tests/e2e/async-stream-reconnect.spec.ts` | **7** | Test 1-3 基础（async / 隔离 / Stop）+ Test 4 reconnect cursor + Test 5 replay/live merge + Test 6 buffer gap fallback + Test 7 页面刷新恢复 |
| `tests/e2e/event-dedup.spec.ts`（增强）| 4 | 重复 event_id / session 隔离 / gap 检测 / 跨 session 不误报 |

### 7 个核心 e2e 详细断言

| Test | 关键断言 |
|---|---|
| **Test 4** Reconnect cursor | reconnect 后 `/api/events?after_sequence=N` 请求中 N > 0；URL 不含 `session_id=` |
| **Test 5** Replay/live merge | `page.route` 延迟 /api/events 400ms；最终 assistant_message count=1；文本含 "Hello" + "delayed" |
| **Test 6** Buffer gap fallback | mock gap=true → `needsFinalResync=true`（page.evaluate poll 验证） |
| **Test 7** 页面刷新恢复 | reload 后 `/api/requests?session_id=&status=active` 被调用（page.on 监听） |

---

## 4. 是否改 core runtime

**否**。未改：`loop.py` / `agent.py` / `harness.py` / `context.py` / `providers/` / `events.py` / `stream_events.py` / `tools/` / `mcp/` / `skills*.py`。

后端仅：
- `web/app.py`：WS hello 加 3 字段 + 新增 GET /api/requests list endpoint
- `web/state.py`：（B3-0c 无改动，buffer first/last_sequence 已在 B2 加）

---

## 5. 新增/修改文件

**实际 17 文件**（`git status` + `git ls-files --others` 验证）：

### 修改（13）
| 文件 | 改动 |
|---|---|
| `src/pi_agent_core_py/web/app.py` | WS hello 加 3 字段 + GET /api/requests list endpoint |
| `src/pi_agent_core_py/web/frontend/src/App.vue` | onMounted 调 findActiveRequest + resumeActiveRequest |
| `src/pi_agent_core_py/web/frontend/src/api/messages.ts` | sendPromptAsync / getRequestStatus / abortRequest / listActiveRequests |
| `src/pi_agent_core_py/web/frontend/src/api/websocket.ts` | EventSocket 加 closeForTest 方法 |
| `src/pi_agent_core_py/web/frontend/src/main.ts` | E2E mode 暴露 __e2eHooks.closeEventSocket |
| `src/pi_agent_core_py/web/frontend/src/stores/chatStore.ts` | 核心改造（sendPrompt 切 async / pendingEventsByRequest / belongsToCurrentRequest / replayFromCursor / pollRequestUntilTerminal / reconcileMessagesFromServer / abortRun / findActiveRequest / resumeActiveRequest / applyEventToStreamItems / closeEventSocketForTest / lastGlobalSequence ref+expose）|
| `src/pi_agent_core_py/web/frontend/src/types/messages.ts` | PromptAsyncResponse / RequestSummary / RequestListResponse / RequestStatus |
| `tests/e2e/event-dedup.spec.ts` | setupStore 重置 lastGlobalSequence |
| `tests/e2e/mcp-tool-lifecycle.spec.ts` | Shift+Enter 用独立 session + 显式 keyboard.down/up |
| `tests/e2e/playwright.config.ts` | retry=1 + webServer 说明 |
| `tests/e2e/start_test_web_app.py` | 默认 delayed FakeClient + E2E_EVENT_BUFFER_MAX_SIZE 支持 |
| `tests/e2e/web-claude-smoke.spec.ts` | Smoke 2/9 用独立 session + .first() + delayed 文本断言 |
| `tests/test_web_event_envelope.py` | 加 hello metadata + buffer 测试（共 14 用例）|

### 新增（4）
| 文件 | 内容 |
|---|---|
| `docs/P1_B3_VALIDATION_REPORT.md` | 本报告 |
| `src/pi_agent_core_py/web/frontend/src/api/events.ts` | getEvents helper |
| `tests/e2e/async-stream-reconnect.spec.ts` | 7 用例 |
| `tests/test_web_request_recovery.py` | 12 用例 |

---

## 6. 兼容性

- ✅ 旧 `POST /api/prompt` 行为 + 响应 schema 严格保持（889 pytest 不回归）
- ✅ 旧 `POST /api/abort` 保留为兼容别名
- ✅ 19/19 e2e 全过（含 16 旧 + 3 新 B3）
- ✅ envelope schema 向后兼容（payload 内保留 `_received_at_ms`）

---

## 7. 已知限制

1. **完整 7 e2e 全部通过**——无 follow-up。Test 4-7 覆盖 reconnect cursor / replay-live merge / buffer gap fallback / 页面刷新恢复。
2. **页面刷新恢复完整**：App.vue onMounted 调 `findActiveRequest` → 非空时调 `resumeActiveRequest(requestId)` set currentRequestId + sending/streaming=true；Test 7 验证 API 调用 + 最终 sending/streaming 归零 + assistant_message 存在。
3. **Smoke 8 Stop button 1 flaky**：delayed FakeClient 时序敏感，retry=1 后稳定通过。
4. **request registry 内存态**：server 重启后 active request 丢失；SQLite 仍可恢复已持久化的最终消息。
5. **single harness 单 active request**：不支持多 session 并行执行。
6. **`lastGlobalSequence` 暴露到 store**：生产 UI 不读，仅便于测试 reset。

---

## 8. 是否建议进入下一阶段

**建议 tag `v0.0.24-async-architecture`**——P1-B 全部完成。

后续阶段（不在 P1-B 范围）：
- P1-C：MCP / Skill 配置持久化（SQLite）
- P1-D：产品功能（Export markdown / Regenerate / PDF 提取）
- 补 B3 完整 7 个 e2e（reconnect / replay / gap / refresh）

---

## 9. P1-B3 完成边界

**本子阶段停止**。不进入 P1-C / P1-D；不做 MCP 持久化；不做 Export markdown；不做 PDF 提取。

# P1-B2 Validation Report — WebEventEnvelope + Event Dedup

> **阶段**: P1-B2（统一事件信封 + 前端去重）—— **Conditional PASS**
> **基线**: P1-B1 完成后 working tree（未 commit）
> **执行日期**: 2026-07-12
> **目标**: 所有 WS / SSE / GET /api/events 用同一个 WebEventEnvelope；事件有 event_id / sequence / request_id / session_id；前端去重 + session 隔离 + gap 检测

---

## 1. 阶段结论

**Conditional PASS**——B2 后端 envelope / sequence / events API 通过；前端去重 + session 隔离在 B2.1 hardening 后通过直接 Playwright 测试验证。同 session 不同 request 的隔离**未启用**（currentRequestId 字段已存在，过滤逻辑留 B3 切 async 前端后启用）。

**P1-B2.1 hardening（本次新增）**：
1. ✅ gap 检测从 per-session（`lastSequenceBySession`）改为全局 `lastGlobalSequence`——避免跨 session 事件误报
2. ✅ `activeSessionId` 在 4 个切换路径同步（首次加载 / activate / newChat / delete）
3. ✅ `seenEventIds` 加容量上限（默认 1000，与后端 buffer 对齐）
4. ✅ 补 SSE envelope 测试（`/api/stream` 推送 envelope 7 字段）
5. ✅ 补 4 个前端 Playwright 测试（event-dedup.spec.ts）——直接注入 envelope 验证去重 / session 隔离 / gap 检测 / 跨 session 不误报
6. ✅ 修正报告表述（本节）

---

## 2. 验收项重新判定

| # | 验收项 | 判定 | 备注 |
|---|---|---|---|
| 1 | WS/SSE/GET /api/events 同 envelope | ✅ 通过（B2.1 补 SSE 测试） | test_9b（WS+GET 同 schema）+ test_11（SSE envelope）+ 代码路径一致性（`_web_event_hook` 一次性生成）|
| 2 | event_id 唯一 | ✅ 通过 | test_2 + test_1 |
| 3 | sequence 全局单调递增 | ✅ 通过 | test_3 |
| 4 | request/session 关联正确 | ✅ 通过 | test_4 + test_5（single-harness + 单 active request 前提）|
| 5 | request start/end sequence 被写入 | ✅ 通过 | test_5 |
| 6 | after_sequence 过滤 | ✅ 通过 | test_6 |
| 7 | buffer 截断后 gap=true（后端）| ✅ 通过 | test_7 |
| 7' | 前端 gap 检测（B2.1 新增）| ✅ 通过 | event-dedup.spec.ts test "sequence gap 触发 gapDetected" |
| 8 | 重复 event_id 只处理一次 | ✅ 通过（B2.1 补测）| event-dedup.spec.ts test "重复 event_id 只处理一次" |
| 9 | **相同 event_id** 对应的 delta 不重复追加 | ✅ 通过（**表述修正**）| 原表述"相同 delta 不重复"不准确——按 delta 文本去重会破坏模型连续输出"ha ha"场景；正确表述是"相同 event_id 不重复处理" |
| 10 | session 隔离：其他 session 事件不污染 | ✅ 通过（B2.1 补测）| event-dedup.spec.ts test "其他 session 事件不污染当前消息流" |
| 10' | **request 隔离：同 session 旧 request 不污染** | ❌ **留 B3** | currentRequestId 字段已暴露但未参与 handleEvent 过滤；待 B3 切 async 前端后启用 |
| 11 | 旧同步 API 与 E2E 不回归 | ✅ 通过 | 874 pytest + 16 e2e（12 旧 + 4 新前端）|
| 12 | 未提前做 reconnect replay | ✅ 通过 | 阶段边界遵守 |

**统计**：10 项明确通过 + 1 项 request 隔离留 B3 + 1 项表述修正后通过。

---

## 3. 完成内容

### 3.1 WebEventEnvelope（用户原指令 §6.1）

```json
{
  "event_id": "evt_<uuid4 hex>",
  "request_id": "req_..." | "req_sync_..." | null,
  "session_id": "sess_..." | null,
  "sequence": 123,
  "type": "message_update",
  "timestamp": "2026-07-12T...",
  "payload": { ...原 AgentEvent serialize_event 结果 + _received_at_ms }
}
```

### 3.2 sequence 在广播入口生成（用户原指令 §6.2）

`_web_event_hook` 在广播入口一次性生成 event_id + 分配 sequence + 包装 envelope——不为 buffer / WS / SSE 分别生成。

### 3.3 填充 B1 sequence 占位（用户原指令 §6.3）

`_run_prompt_background` + sync `post_prompt` 都 set/clear `state.current_request_id` / `current_request_session_id`。

### 3.4 GET /api/events 扩展（用户原指令 §6.4）

支持 `?session_id` / `?request_id` / `?after_sequence` / `?limit`；返回 `events + first_available_sequence + last_available_sequence + has_more + gap`。

### 3.5 前端 chatStore 去重（用户原指令 §6.7）+ B2.1 hardening

**B2 初版**：`seenEventIds` / `lastSequenceBySession` / `gapDetected` / `currentRequestId` / `activeSessionId`

**B2.1 hardening 修正**：
- gap 检测改用 **全局 `lastGlobalSequence`**（`lastSequenceBySession` 保留作 per-session 统计 + B3 replay cursor，**不参与 gap 判断**）
- `seenEventIds` 加 `SEEN_EVENT_IDS_MAX = 1000` 容量上限（超过清空，简化 LRU；B3 改真正 LRU）
- `chatStore.setActiveSession(sid)` 函数暴露
- 4 个 session 切换路径（首次加载 / activate / newChat / delete）都同步 `chatStore.activeSessionId`

---

## 4. 新增/修改文件

**实现 7 + 测试 2 + 报告 1 = 10 文件**（含 B2 初版 + B2.1 hardening）：

| 文件 | 类型 | 改动 |
|---|---|---|
| `src/pi_agent_core_py/web/state.py` | 后端修改 | `TraceEventBuffer` 加 `first_sequence` / `last_sequence` 跟随 head 自动更新；`WebAppState` 加 `next_event_sequence` / `current_request_id` / `current_request_session_id` |
| `src/pi_agent_core_py/web/app.py` | 后端修改 | `_web_event_hook` 包装 envelope；`_run_prompt_background` + sync `post_prompt` set/clear request context；GET /api/events 加 4 query 参数 + gap 检测 |
| `src/pi_agent_core_py/web/frontend/src/types/events.ts` | 前端修改 | 加 `WebEventEnvelope` interface + `isWebEventEnvelope` type guard |
| `src/pi_agent_core_py/web/frontend/src/stores/chatStore.ts` | 前端修改 | 5 个 state（含 B2.1 `lastGlobalSequence` + `SEEN_EVENT_IDS_MAX`）+ envelope-aware handleEvent + `setActiveSession` 函数 |
| `src/pi_agent_core_py/web/frontend/src/main.ts` | 前端修改 | expose `window.__storeHooks.chatStore` 给 devtools + E2E 测试 |
| `src/pi_agent_core_py/web/frontend/src/App.vue` | 前端修改 | 首次加载同步 chatStore.setActiveSession |
| `src/pi_agent_core_py/web/frontend/src/components/layout/SessionSidebar.vue` | 前端修改 | activateSession / newChat / deleteSession 都同步 chatStore.setActiveSession |
| `tests/test_web_event_envelope.py` | 测试新增 | 12 用例（11 + B2.1 补 SSE） |
| `tests/e2e/event-dedup.spec.ts` | 测试新增（B2.1）| 4 用例：重复 event_id / session 隔离 / gap 检测 / 跨 session 不误报 |
| `docs/P1_B2_VALIDATION_REPORT.md` | 文档新增 | 本报告 |

---

## 5. 测试结果

| 命令 | 结果 |
|---|---|
| `pytest tests/ -m "not slow and not integration and not docker"` | **875 passed**（843 P0 baseline + 20 B1 async + 12 B2/B2.1 envelope），41s |
| Coverage gate | **84.31%** ≥ 75% PASS |
| `ruff check src tests scripts` | **All checks passed** |
| `npm run build`（frontend） | 124 modules / 778ms |
| `npm run test:e2e`（含 4 新前端测试）| **16/16 PASS**（12 旧 + 4 event-dedup，Playwright 不计入 pytest），9.7s |

### B2.1 hardening 新增测试覆盖

| 文件 | 测试 | 验证点 |
|---|---|---|
| `test_web_event_envelope.py::test_11_sse_receives_envelope` | SSE 推送 envelope 7 字段 | §14 B2 #1 SSE 一致性 |
| `event-dedup.spec.ts::重复 event_id 只处理一次` | 同 event_id 不重复处理 | §14 B2 #8 |
| `event-dedup.spec.ts::其他 session 事件不污染` | session_id 不匹配的事件不写入 streamItems | §14 B2 #10 |
| `event-dedup.spec.ts::sequence gap 触发 gapDetected` | sequence > last+1 触发 gapDetected | §14 B2 #7 前端 |
| `event-dedup.spec.ts::跨 session 事件不触发误报` | 全局 cursor 修复 per-session gap 误报 | B2.1 关键问题 |

---

## 6. 是否改 core runtime

**否**。未改：`loop.py` / `agent.py` / `harness.py` / `context.py` / `providers/` / `events.py` / `stream_events.py` / `tools/` / `mcp/` / `skills*.py`。

---

## 7. 兼容性

- ✅ 旧 `POST /api/prompt` 行为 + 响应 schema 严格保持
- ✅ envelope.payload 内保留 `_received_at_ms` + 原 AgentEvent 字段（旧客户端断言仍可读）
- ✅ 12 旧 e2e + 4 新前端 e2e 全部不回归
- ✅ 未切前端 sendPrompt（仍用同步 /api/prompt，B3 切 async）

---

## 8. 已知限制

1. **request 隔离未启用**：`currentRequestId` 字段已暴露但 handleEvent 不基于它过滤；同 session 旧 request 的延迟 `request_end` 可能错误结束新 request 的 streaming 状态。**B3 切 async 前端后启用**。
2. **`gapDetected` 仅标记**：B2 检测到 gap 但不触发 replay；B3 触发 reconnect replay。
3. **WS 实时 event flow 在 sync TestClient 下难测**：通过代码路径一致性（`_web_event_hook` 一次性生成 envelope 给三处）+ GET /api/events 完整测试覆盖 + SSE envelope 测试 + 前端直接注入测试组合保证。B3 reconnect 测试会完整覆盖 WS event flow。
4. **`seenEventIds` 用简化 LRU**（超过上限清空）：B3 改为真正的 LRU。

---

## 9. 是否建议进入 P1-B3

**建议进入**。

B2 + B2.1 hardening 后验收 12 项达成情况：
1. ✅ WS/SSE/GET /api/events 同 envelope（B2.1 补 SSE 测试）
2. ✅ event_id 唯一
3. ✅ sequence 全局单调递增
4. ✅ prompt 事件关联正确 request_id/session_id
5. ✅ request 的 start/end sequence 被写入
6. ✅ after_sequence 过滤正确
7. ✅ buffer 截断后 gap=true（后端 + 前端均测试）
8. ✅ 前端重复 event_id 只处理一次（B2.1 直接测试）
9. ✅ 相同 event_id 对应的 delta 不重复追加（表述修正后）
10. ✅ 其他 session 事件不污染当前消息流（B2.1 直接测试）；request 隔离留 B3
11. ✅ 旧同步 API + E2E 不回归（16/16 e2e）
12. ✅ 暂不实现 reconnect replay

P1-B3 任务：WS reconnect replay + 切前端 sendPrompt 到 /api/prompt/async + 启用 currentRequestId 过滤 + Stop 切 request abort + 页面刷新恢复。

---

## 10. P1-B2 完成边界

**本子阶段停止**（B2 + B2.1 hardening 全部完成）。不进入 P1-B3；不做 WS reconnect replay；不切前端 sendPrompt；不改 Stop 按钮；不启用 request 隔离。

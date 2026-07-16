# Web Request Lifecycle

> Web 层 async prompt 的完整生命周期——从 HTTP 入口到 WebSocket 实时事件流到最终一致性。
>
> 范围：`src/pi_agent_core_py/web/app.py` + `web/state.py` + 前端 `chatStore`。

## 1. 总体数据流

```
┌─────────────────────────────────────────────────────────────────┐
│ Browser                                                          │
│  chatStore.sendPrompt(text)                                      │
│    ↓                                                             │
│  POST /api/prompt/async ──────┐                                  │
│  await 202 + request_id       │                                  │
│    ↓                           ↓                                 │
│  pendingRequest=true          ┌──────────────────────────┐       │
│  currentRequestId=null        │ Server                   │       │
│    ↓                          │  FastAPI route handler   │       │
│  WS hello / event →           │  ↓                       │       │
│  applyEventToStreamItems      │  Request Registry        │       │
│    ↓                          │  (in-memory)             │       │
│  pollRequestUntilTerminal     │  ↓                       │       │
│    ↓                          │  asyncio.Task → harness  │       │
│  reconcileMessagesFromServer  │  ↓                       │       │
│    ↓                          │  _web_event_hook         │       │
│  terminal state               │  ↓                       │       │
└────────────────────────────────┤  WebEventEnvelope        │       │
                                 │  ↓                       │       │
                                 │  WS broadcast + buffer   │       │
                                 └──────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

## 2. Request Registry（内存态）

`web/state.py::WebAppState.active_requests: dict[str, WebRunRequest]`

| 字段 | 说明 |
|---|---|
| `id` | request_id（uuid） |
| `session_id` | 目标 session |
| `status` | queued / running / completed / error / aborted |
| `operation` | prompt / regenerate |
| `regeneration_id` | revision.id（仅 regenerate） |
| `target_message_id` | regenerate 目标 assistant message_id |
| `task` | `asyncio.Task`（lifespan shutdown 时 cancel + gather） |
| `created_at` / `updated_at` | ISO timestamps |

**关键约束**：
- **Single harness → single active request**——同一 session 不允许并发 prompt；不同 session 也不允许（harness 是单例）
- 重启后丢失（内存态）——已在 SQLite 持久化的 final messages 不丢
- 并发 prompt → `POST /api/prompt/async` 返回 409

## 3. HTTP 入口

### `POST /api/prompt/async`

请求 body：
```json
{
  "text": "user prompt",
  "session_id": "...",
  "skill_names": ["..."],        // optional
  "skill_selection": {...},       // optional
  "file_ids": ["..."]             // optional
}
```

响应（202）：
```json
{
  "request_id": "req-...",
  "status": "queued",
  "session_id": "..."
}
```

处理顺序：
1. 校验 session 存在；unknown skill 预校验返回 400
2. 校验 file_ids 属于该 session
3. 检查 active request——有则 409
4. 创建 `WebRunRequest`（status=queued）+ `asyncio.Task`
5. 立即返回 202 + request_id

后台 Task 执行：
1. `status = running`
2. `_run_prompt_core(sid, text, skill_selection, file_ids)`
   - 读 SQLite messages
   - harness.run_prompt
   - `_persist_normal_prompt_result`：`replace_messages` + `append_snapshot`
3. 正常完成：`status = completed`
4. 异常：`status = error`（error_summary 安全截断）
5. abort（signal set）：`status = aborted`

### `POST /api/prompt`（同步兼容）

仍然存在但**不推荐**——阻塞 HTTP 直到 LLM 完成。前端 P1-B3 后已切到 async。同步路径不进 Request Registry。

## 4. WebEventEnvelope

`_web_event_hook` 是注册到 `harness.on_event_hooks` 的回调——harness emit AgentEvent 时被调用，一次性生成 envelope：

```python
{
  "event_id": "evt-...",           # uuid
  "request_id": "req-...",         # 当前 active request
  "session_id": "...",
  "sequence": 12345,                # 全局单调递增
  "type": "message_update",         # AgentEvent 类型
  "timestamp": "2026-07-16T...",
  "payload": { ... }                # 原始 AgentEvent 字段
}
```

**关键设计**：
- envelope 在 hook 入口**一次性**生成（不在多处分别加 metadata）
- sequence 是**全局**单调（不是 per-session）——保证 reconnect replay 跨 session 一致
- hook 不依赖 `contextvars`——Agent 内部 task 跨 task 时 contextvar 不可靠

## 5. 实时事件分发

三处同时消费 envelope：

| 消费者 | 协议 | 说明 |
|---|---|---|
| WebSocket broadcast | `WS /ws/events` | 实时 push；每连接独立 `asyncio.Queue(maxsize=100)`；满则丢 |
| SSE fallback | `GET /api/stream` | 支持 `?limit=N` 测试模式；默认无限流 |
| Buffer | `TraceEventBuffer`（deque maxlen=1000） | reconnect replay 数据源 |

## 6. WebSocket hello / 控制帧

`WS /ws/events` 连接建立后服务端先发 hello（**不**进 buffer / 不**消耗 sequence** / 不**进 seenEventIds**）：

```json
{
  "type": "hello",
  "first_available_sequence": 12300,
  "last_available_sequence": 12345,
  "server_time": "2026-07-16T..."
}
```

客户端用 `last_available_sequence` 判断是否需要 replay。

## 7. Reconnect Replay

前端 chatStore 在 WS reconnect 时（`lastGlobalSequence > 0`）触发：

```
replayFromCursor(lastGlobalSequence)
  ├─ replaying = true
  ├─ 新 WS event 进 liveEventsDuringReplay buffer
  ├─ getEvents({afterSequence: lastGlobalSequence, limit: 200})
  │    └─ 不带 session_id（全局）
  ├─ 分页直到 has_more=false
  │    └─ 保护：20 页 / 4000 事件上限 → needsFinalResync=true
  ├─ merge replay + live → sort by sequence → dedupe by event_id
  └─ 依次 applyEventToStreamItems
```

**关键约束**：
- replay 请求**不带 `session_id`**——避免跨 session 事件被 filter 掉
- dedupe 靠 `event_id`（uuid）——replay 和 live 可能含同一事件
- buffer 不够长（事件已被 deque 淘汰）→ `needsFinalResync=true`，前端 fallback 到 `/api/messages` 全量同步

## 8. Request 隔离

前端 `chatStore` 维护 `currentRequestId`——决定哪些 event 属于"当前 turn"。

`belongsToCurrentRequest(envelope)`：

| event.type | 是否要求属于 currentRequestId |
|---|---|
| `message_start / update / end` | ✅ |
| `request_*` | ✅ |
| `agent_start / end / abort` | ✅ |
| `error` | ✅ |
| `tool_execution_*` | ✅ |
| `turn_*` | ✅ |
| 其他（全局状态） | ❌ |

不属于当前 request 的事件**不**进 `streamItems`——避免串流。

**早到事件缓冲**：`pendingEventsByRequest[request_id]`（容量 100/queue）。POST 还在 await 202 时 WS 已经收到 event，按 request_id 缓冲，202 后 flush。

## 9. Terminal Polling + Reconciliation

`pollRequestUntilTerminal(requestId)`：
- 退避 250-500ms，总超时 10s
- `request_end` event 触发立即 poll
- `status in (completed, error, aborted)` 后调 `reconcileMessagesFromServer`

`reconcileMessagesFromServer`：
- `GET /api/messages?session_id=...` 拿到服务端权威 messages
- **替换** user_message / assistant_message
- **保留** 当前 turn cards（tool_call / file_read / skill_used / mcp_tool_call / turn_info / error）

为什么不直接信 WS event 流？——
1. WS event 可能丢（慢客户端）
2. messages 表是 SQLite canonical source——所有 export / next prompt / regenerate 都基于它
3. avoid drift between WS-derived state and persisted state

## 10. Abort

`POST /api/requests/{id}/abort`（幂等）：
- 设 signal → loop 在下个 yield 退出 → status=aborted
- revision（如果 operation=regenerate）→ mark_revision_aborted
- `_reset_harness_to_session` 兜底（清截断 context）

前端 chatStore 不立即 set `sending/streaming=false`——等 status=aborted 由 poll 处理。

## 11. 浏览器刷新恢复

`App.vue::onMounted`：
1. `findActiveRequest(sessionId)`——`GET /api/requests?session_id=...&status=active`
2. 非空 → `resumeActiveRequest(requestId)`——set `currentRequestId` + `sending/streaming=true`
3. 后续走 normal poll + reconcile

**限制**：
- `activeSessionId` 不在 URL / localStorage——reload 后 `App.vue` 选第一个 session
- 完整恢复原 session 依赖 P2 URL routing

## 12. Shutdown

`lifespan` shutdown 顺序：
1. `shutting_down = true`（拒绝新 prompt）
2. abort active requests + grace timeout
3. `harness.detach_mcp_servers()`（清理 MCP transport）
4. remove event hook + 通知 WS/SSE close
5. `extension_store.close()`（不 close injected connection）
6. `session_store.close()`（关闭共享 connection）

## 13. 已知限制

- **Request registry 内存态**——server 重启后 active request 丢失
- **Single harness / single active request**——不支持多 session 并行
- **WebSocket 慢客户端**：`asyncio.Queue(maxlen=100)` 满则丢事件（前端有 reconcile 兜底）
- **完整浏览器 reload 后恢复原 session**：依赖 URL routing（P2）

## 14. 相关文档

- [Runtime and Harness](runtime-and-harness.md)
- [Persistence and Startup](persistence-and-startup.md)
- [Regenerate Revision Model](regenerate-revision-model.md)

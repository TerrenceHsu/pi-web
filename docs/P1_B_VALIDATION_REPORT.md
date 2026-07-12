# P1-B1 Validation Report — Async Prompt + Request Registry

> **阶段**: P1-B1（异步 Prompt + Request Registry）
> **基线**: tag `v0.0.23.1-web-claude-validation` @ `80a2f6f`
> **执行日期**: 2026-07-12
> **目标**: 在不破坏同步 /api/prompt 的前提下，新增异步 prompt 链路 + request lifecycle + abort + shutdown 收敛

---

## 1. 阶段结论

**全部 B1 验收标准达成**（用户原指令 §14 B1 部分 7 项 + 通用部分）。

P1-B1 完成。**等待用户批准后进入 P1-B2**（WebEventEnvelope + event_id/sequence 去重）。

---

## 2. 完成内容

### 2.1 抽取 Prompt 公共执行逻辑（用户原指令 §5.1）

`src/pi_agent_core_py/web/app.py` 新增 module-level 类型 + create_app() 内 helpers：

- `PromptValidationError(status_code, detail, **extra)` — 4xx 校验错误
- `PromptRuntimeError(status_code, message, error_type)` — 5xx/409 harness 错误
- `_PromptValidated` dataclass — 校验产物
- `PromptExecutionResult` dataclass — 执行产物
- `_merge_skill_names` / `_build_skill_selection` / `_resolve_file_blocks` / `_build_attachment_meta` helpers
- `_validate_prompt_payload(payload) → _PromptValidated` — 所有乐观校验
- `_run_prompt_core(validated) → PromptExecutionResult` — harness 执行 + 持久化
- `_serialize_prompt_validation_error` / `_serialize_prompt_runtime_error`

**POST /api/prompt 改 thin wrapper**（行为与响应 schema 与 v0.0.23.1 完全一致）：

```python
try:
    validated = await _validate_prompt_payload(payload)
    result = await _run_prompt_core(validated)
except PromptValidationError as e:
    return _serialize_prompt_validation_error(e)
except PromptRuntimeError as e:
    return _serialize_prompt_runtime_error(e)
return {ok, session_id, messages, attachments, applied_skill_names}
```

### 2.2 Request 状态模型（用户原指令 §5.2）

`src/pi_agent_core_py/web/state.py` 新增：

- `RequestStatus = Literal["queued", "running", "completed", "error", "aborted"]`
- `WebRunRequest` dataclass（13 字段，含 JSON-safe 字段 + 内存-only 字段 `task` / `payload`）
- `WebAppState` 加 `active_requests` / `active_request_by_session` / `request_history` (deque maxlen=100) / `shutting_down`

### 2.3 POST /api/prompt/async（用户原指令 §5.3）

- 完整乐观校验（与同步路径一致）—— 失败立即 4xx，**不创建 request**
- session 级并发检查 → 409
- 创建 request_id (`req_{uuid4().hex[:16]}`) + WebRunRequest(status=queued)
- `asyncio.create_task(_run_prompt_background(...), name=f"prompt_async_{request_id}")`
- 立即返回 202 + `{request_id, status, events_url, request_url, abort_url}`

### 2.4 后台 task 管理（用户原指令 §5.4）

`_run_prompt_background(web_request, validated)` runner：

- status 流转：queued → running → (completed | error | aborted)
- `asyncio.CancelledError` 重抛 + 设 status=aborted
- `PromptRuntimeError` / `Exception` catch → 设 status=error + safe_error
- 成功路径检查 `abort_reason` flag → 设 status=aborted（abort 不 cancel task，走 harness.abort 让模型 finalize）
- finally：从 active_requests 移除 + append history——保证 history 可查

### 2.5 Request API（用户原指令 §5.5）

`GET /api/requests/{request_id}` — 从 active + history 查；404 missing。

### 2.6 Abort API（用户原指令 §5.6）

`POST /api/requests/{request_id}/abort` — 幂等；queued→cancel task / running→`harness.abort` + flag / completed|error|aborted→返回当前状态。

旧 `POST /api/abort` 保留为**兼容别名**——有 active request 时转发到 `_abort_request_internal`；否则直调 `harness.abort()`。

### 2.7 Shutdown 清理（用户原指令 §5.7）

lifespan shutdown：

1. `state.shutting_down = True`（POST /api/prompt/async 看到 → 503）
2. 收集 active_requests 的 task
3. 每个 `_abort_request_internal(req, "server_shutdown")`
4. `asyncio.wait_for(gather(*tasks), timeout=shutdown_grace_s)`（默认 5s，可经 `create_app(shutdown_grace_s=...)` 覆盖）
5. 超时 → cancel 剩余 task → gather return_exceptions
6. 原 hook 移除 + clients 通知 + store close

### 2.8 测试

`tests/test_web_prompt_async.py`（**20 用例，默认运行**）覆盖用户原指令 §5.8 的 17 项 + 3 个额外：
- legacy sync 兼容 / legacy abort 别名 / history 保留

---

## 3. 新增/修改文件

**4 修改 + 2 新增 = 6 文件**：

| 文件 | 类型 | 改动 |
|---|---|---|
| `src/pi_agent_core_py/web/state.py` | 修改 | 加 `WebRunRequest` + `RequestStatus` + `WebAppState` 4 个字段 |
| `src/pi_agent_core_py/web/app.py` | 修改 | 加 module-level exceptions + 4 个 dataclass + 9 个 helpers + 3 个新 endpoint + 改 POST /api/prompt 为 wrapper + POST /api/abort 为别名 + lifespan shutdown 收敛 + create_app 两个新参数 |
| `docs/WEB_API.md` | 修改 | 加 POST /api/prompt/async / GET /api/requests/{id} / POST /api/requests/{id}/abort / Request History 章节 |
| `docs/WEB_TESTING.md` | 修改 | 加 P1-B1 章节 + 测试分层图加 test_web_prompt_async.py 条目 |
| `tests/test_web_prompt_async.py` | 新增 | 20 用例 |
| `docs/P1_B_VALIDATION_REPORT.md` | 新增 | 本报告 |

---

## 4. 数据模型

```python
@dataclass
class WebRunRequest:
    id: str
    session_id: str | None
    status: RequestStatus  # queued|running|completed|error|aborted
    created_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    error: str | None  # safe_error() 截断到 ≤500 字符
    error_type: str | None
    abort_reason: str | None
    result_summary: dict | None  # {message_count, applied_skill_names, session_id}
    event_start_sequence: int | None  # P1-B2 填充
    event_end_sequence: int | None  # P1-B2 填充
    task: asyncio.Task | None  # 仅内存
    payload: dict | None  # 仅内存——debug 用，绝不进 JSON
```

---

## 5. API 变化

| Method | Path | 状态 |
|---|---|---|
| POST | `/api/prompt` | ✅ 保留（同步，行为完全不变）|
| POST | `/api/prompt/async` | 🆕 202 + request_id |
| GET | `/api/requests/{request_id}` | 🆕 active + history |
| POST | `/api/requests/{request_id}/abort` | 🆕 幂等 |
| POST | `/api/abort` | ✅ 保留为兼容别名（转发到 active request）|

---

## 6. 生命周期

```
queued ──→ running ──→ completed
              │
              ├──→ error      (harness 异常)
              │
              ├──→ aborted    (用户 / shutdown 触发)
              │
              └──→ aborted    (CancelledError — shutdown 强制 cancel)

完成后：active_requests.pop() + request_history.append()
```

---

## 7. 并发语义

- **全局单 active request**（`_ensure_idle` 三重检查：`state.running` + `harness.context.phase` + `agent.state.status`）
- **session_id 字段保留**为未来扩展点；当前 single harness 下 session 级并发检查永远不会触发（harness busy 检查先 reject）
- **不虚假宣称多 session 并行**（用户原指令 §3.16）

---

## 8. 兼容性

- ✅ 旧 `POST /api/prompt` 行为 + 响应 schema 严格保持 v0.0.23.1 一致（843 旧测试全过）
- ✅ 旧 `POST /api/abort` 保留，转发到 active request
- ✅ 前端**未切换**——B1 不动 ChatInput.vue / chatStore.ts（B3 完成后统一切换）
- ✅ 12/12 e2e 不回归

---

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `pytest tests/ -m "not slow and not integration and not docker"` | **863 passed**（843 baseline + 20 新 async 测试），41.32s |
| `pytest tests/test_web_prompt_async.py` 单跑 | **20/20 PASS**，4.33s |
| Coverage gate | **84.25%** ≥ 75% PASS |
| `ruff check src tests scripts` | **All checks passed** |
| `npm run build`（frontend） | 123 modules / 3.16s |
| `npm run test:e2e`（12 测试）| **12/12 PASS**，11.7s |

### 新增 20 个 async 测试覆盖

| # | 测试 | 验收点 |
|---|---|---|
| 1 | async returns 202 with request_id | §5.8 #1/#3 |
| 2 | async returns before model completes | §5.8 #2 |
| 4 | async completes + persists messages | §5.8 #4/#5 |
| 6 | async with file_ids | §5.8 #6 |
| 7 | async with skill_names | §5.8 #7 |
| 8 | unknown skill 400 + no request | §5.8 #8 |
| 9a | missing file 404 + no request | §5.8 #9 |
| 9b | empty text 400 + no request | — |
| 10 | concurrent same session 409 | §5.8 #10 |
| 11 | global busy 409 | §5.8 #11 |
| 12 | harness error → status=error | §5.8 #12 |
| 13 | abort running → aborted | §5.8 #13 |
| 14 | abort completed idempotent | §5.8 #14 |
| 15 / 15b | unknown request 404 | §5.8 #15 |
| 16 | shutdown converges active tasks | §5.8 #16 |
| 17 / 17b | legacy sync /api/prompt | §5.8 #17 |
| 18 | legacy /api/abort forwards | — |
| 19 | history retains completed | — |

---

## 10. 是否改 core runtime

**否**。

未改：`loop.py` / `agent.py` / `harness.py` / `context.py` / `providers/` / `events.py` / `stream_events.py` / `tools/` / `mcp/` / `skills*.py`。

---

## 11. 已知限制

1. **前端未切换到 async**：B1 阶段保 12/12 e2e 不回归；B3 完成后统一切 ChatInput.vue + chatStore.ts。
2. **hook 跨 task 不可靠**（用户原指令 §A Q3 发现）：event 当前不带 request_id/session_id；P1-B2 用 `state.active_request_by_session` 映射表注入而非 contextvar。
3. **event_start_sequence / event_end_sequence 占位 null**：B1 还没引入 envelope sequence，B2 起填充。
4. **single harness concurrency**：不支持多 session 并行；session 级 409 在当前实现下永远不会触发（harness busy 先拒）。
5. **traceback 不输出**：`safe_error()` 截断到 type+message ≤500 字符；详细 traceback 仅 server log。

---

## 12. 是否建议进入 P1-B2

**建议进入**。

B1 验收 7 项（用户原指令 §14 B1）全部达成：
1. ✅ /api/prompt/async 返回 202
2. ✅ 返回 request_id
3. ✅ HTTP 不等待模型完成（slow fixture 0.3s 下 endpoint <<100ms 返回）
4. ✅ request status 可查询
5. ✅ abort 可用
6. ✅ shutdown 无 task 泄漏（test_16 验证）
7. ✅ 旧 /api/prompt 兼容（843 旧测试 + 12/12 e2e 不回归）

P1-B2 在此基础上加 WebEventEnvelope（event_id + sequence + request_id + session_id）+ 前端去重。

---

## 13. P1-B1 完成边界

**本子阶段停止**。不进入 P1-B2；不动 core runtime；不动前端；不做 MCP 持久化；不做 event envelope。

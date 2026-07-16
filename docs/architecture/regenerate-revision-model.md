# Regenerate Revision Model

> P1-D2 Regenerate 的非破坏性切换模型——`messages` 是 active canonical source，`web_message_revisions` 是历史和生成尝试。
>
> 范围：`web/extension_store.py`（revision repository）+ `web/app.py`（regenerate route + lifecycle）+ `session_sqlite.py`（diff-based replace）。

## 1. 核心原则

1. **非破坏性**：旧 active 回答在 regenerate 流式生成期间必须仍然可见、可访问
2. **可恢复**：error / abort / server restart 都不能让 session 进入"既无旧回答也无新回答"的中间态
3. **不动 core runtime**：所有逻辑在 Web 编排层
4. **`messages` 表是 single canonical truth**：
   - 下一轮 Prompt 使用的回答
   - Export 导出的回答
   - `GET /api/messages` 返回的回答
5. **Message ID 稳定**：assistant_message_id 在 regenerate 期间不变——revision 通过外键引用
6. **`completed` ⟺ `active`**：用 `status='completed'` 表达"这条 revision 是当前 active"——由 partial unique index 强制
7. **自动切换 active，不做手动切换**：regenerate 成功 finalize 时自动切换；**不**提供用户手动切回历史 revision 的 UI/API

## 2. 数据模型

### `messages` 表（active canonical source）

每条 assistant message 一行；`content_json` 永远是当前 active 内容。

### `web_message_revisions` 表

```sql
CREATE TABLE web_message_revisions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  assistant_message_id TEXT NOT NULL,
  revision_number INTEGER NOT NULL,    -- 0=旧 active 归档；1,2,...=regenerate 尝试
  request_id TEXT,                      -- NULL for revision 0
  status TEXT NOT NULL CHECK(status IN (
    'running', 'completed', 'superseded',
    'error', 'aborted', 'interrupted'
  )),
  base_content_sha256 TEXT NOT NULL,    -- 创建时 messages.content_json 的 SHA-256
  content_json TEXT,                    -- NULL until finalize/complete
  error_summary TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(assistant_message_id, revision_number),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_request ON web_message_revisions(request_id)
  WHERE request_id IS NOT NULL;
CREATE UNIQUE INDEX uq_running ON web_message_revisions(assistant_message_id)
  WHERE status = 'running';
CREATE UNIQUE INDEX uq_active ON web_message_revisions(assistant_message_id)
  WHERE status = 'completed';  -- 至多 1 个 completed (active)
CREATE INDEX idx_history ON web_message_revisions(assistant_message_id, revision_number);
```

**关键设计**：
- **不加跨表 FK** on `assistant_message_id`——顺序敏感（regenerate 在 messages 表 UPDATE 不 INSERT），应用层校验
- **DELETE 单条 message 不级联 revision**——必须显式清理（`replace_messages` 加了 orphan cleanup）
- `base_content_sha256` 是 optimistic concurrency token——finalize 时重读对比，不一致 → rollback + revision 标 error

## 3. Revision 状态机

```
                     create_running_revision
                     (INSERT, status=running)
                            │
            ┌───────────────┼───────────────┐
            │               │               │
        finalize          abort           error / sweep
            │               │               │
            ▼               ▼               ▼
       completed       aborted      error / interrupted
            │
       (旧 completed
        → superseded)
```

**状态转换单向**：
- running → completed / error / aborted / interrupted
- completed → superseded（被新 revision finalize 时）
- 其他终态不可转换

**条件 UPDATE `WHERE status='running'` + rowcount 校验**——防并发竞争。

## 4. Revision 0（旧 active 归档）

**延迟创建**：只在第一次 regenerate 成功 finalize 时，把当时的旧 active 内容保存为 revision 0（`status='superseded'`）。

| 场景 | revision 0 创建？ |
|---|---|
| 第一次 regenerate 成功 | ✅ INSERT revision 0（旧 active）+ UPDATE revision 1（新 active） |
| 第一次 regenerate 失败 / 中止 | ❌ 不创建——避免无意义历史污染 |
| 第二次及之后 regenerate 成功 | ❌ 已存在；只 UPDATE 上一个 completed → superseded |

**revision 0 特殊字段**：
- `request_id = NULL`（不属于任何 web_run_request）
- `revision_number = 0`
- `status = 'superseded'`（创建即是历史）
- `base_content_sha256 = sha256(旧 active content_json)`
- `content_json = 旧 active content_json`

## 5. Finalize Transaction（原子切换）

单 `BEGIN IMMEDIATE` transaction 5 步：

```sql
BEGIN IMMEDIATE;

-- 1. 校验：revision 仍是 running（未 abort / 未被 sweep 改 interrupted）
SELECT status FROM web_message_revisions WHERE id = ?;
-- expect 'running'——否则 ROLLBACK

-- 2. （首次 regenerate）INSERT revision 0：把旧 active 归档为 superseded
--    判断条件：本 assistant_message_id 下无任何 superseded/completed 历史
INSERT INTO web_message_revisions (..., revision_number=0, status='superseded', ...);

-- 3. （非首次）UPDATE 上一个 completed → superseded
UPDATE web_message_revisions
  SET status='superseded', completed_at=?
  WHERE assistant_message_id=? AND status='completed' AND id != ?;
-- 受影响行数：0（首次，由 step 2 处理）或 1（非首次）

-- 4. UPDATE 本 revision → completed + 写完整 candidate content
UPDATE web_message_revisions
  SET status='completed', content_json=?, completed_at=?
  WHERE id=?;

-- 5. UPDATE messages.content_json（保留 id / role / created_at！）
UPDATE messages SET content_json=? WHERE id=?;

UPDATE sessions SET updated_at=? WHERE id=?;

COMMIT;
```

**关键顺序**：step 3（旧 completed → superseded）**必须先于** step 4（running → completed），否则违反 `uq_active` 部分唯一索引（同时 2 个 completed）。

**失败回滚**：任何一步失败 → `ROLLBACK`，messages 表保持旧 active，revision 仍 running（可重试 finalize 或人工 abort）。

**base_content_sha256 校验**：
- finalize 时重读 `messages.content_json` 计算 SHA-256
- 对比 revision.base_content_sha256
- 不一致 → `RevisionBaseContentChangedError` + revision 标 error
- 错误响应**不含 hash 差异**——防泄露

## 6. 流式期间不写 DB

**决策点**：流式 delta 只存在前端 chatStore + WS 事件流，**不写 DB**。

| 阶段 | messages.content_json | revision.content_json | revision.status |
|---|---|---|---|
| regenerate start | 旧回答 | NULL | running |
| streaming | 旧回答（不变） | NULL（不写） | running |
| finalize success（非首次） | **新回答**（UPDATE） | **新回答**（UPDATE） | completed |
| finalize success（首次） | **新回答**（UPDATE） | revision 0 ← 旧回答（INSERT, superseded）；revision 1 ← 新回答（UPDATE, completed） | completed |
| finalize error | 旧回答（不变） | NULL | error |
| abort | 旧回答（不变） | NULL | aborted |
| server restart | 旧回答（不变） | NULL | interrupted（startup sweep 改） |

**为什么不流式写**：
1. 流式写会让 active content 在 finalize 前不断变化 → 并发 GET /api/messages 看到中间态
2. 失败时回滚成本高（要恢复旧 content_json）
3. 非破坏性原则要求"旧回答在 finalize 前必须可见"

## 7. Regenerate HTTP API

### `POST /api/sessions/{sid}/messages/{aid}/regenerate` → 202

14 步 route 顺序：
1. session 存在
2. message 存在 + 属于该 session
3. message.role == 'assistant'
4. message 是最新 assistant
5. 存在 preceding user message
6. 无 active request（regenerate 或 prompt）
7. 无 rival running revision
8. revision_number MAX+1（从 1 开始）
9. `base_content_sha256 = sha256(原始 content_json UTF-8 字符串)`（**禁止** json.loads/dumps 重序列化）
10. INSERT revision（status=running）
11. 创建 `WebRunRequest`（operation=regenerate, regeneration_id=revision.id, target_message_id=aid）
12. 启动 `asyncio.Task`
13. 返回 202 + metadata
14. **route 不调 replace_messages**——`_persist_regeneration_result` 只调 finalize

**稳定错误 code**：
- `session_not_found` (404)
- `message_not_found` (404)
- `message_wrong_session` (404)
- `user_message_expected` (400)
- `not_latest_assistant` (409)
- `missing_preceding_user` (409)
- `active_request_conflict` (409)
- `revision_conflict` (409)
- `revision_base_content_changed` (409)
- `revision_state_transition` (409)

### `GET /api/sessions/{sid}/messages/{aid}/revisions` → 200

安全 serializer——**不**返回：
- `content_json`
- `base_content_sha256`
- `request_id`

支持分页（`before_revision_number` / `limit [1,100]`）。

## 8. Execution / Persistence Split

`web/app.py` 拆分（D2-4）：

| 函数 | 职责 |
|---|---|
| `_execute_prompt` | 纯执行（harness.run_prompt），不持久化 |
| `_persist_normal_prompt_result` | `replace_messages` + `append_snapshot`（普通 prompt） |
| `_persist_regeneration_result` | `finalize_revision` + best-effort snapshot（**不**调 replace_messages） |
| `_reset_harness_to_session` | 优先 SQLite canonical，失败 fallback——所有 regenerate 路径退出都调 |
| `_run_prompt_core` | thin wrapper：调 `_execute_prompt` + `_persist_normal_prompt_result` |
| `_run_regeneration_core` | queued → running → execute → finalize → reset → completed |

**事务边界**：
- 短事务 `create_running_revision` → 释放 → 长 LLM 执行 → 短事务 `finalize_revision`
- **无** SQLite BEGIN IMMEDIATE 跨越 LLM 调用

## 9. Reconnect Recovery

WebSocket reconnect 期间 regenerate 流式可能被打断——前端 chatStore 处理：

| 终态 | 行为 |
|---|---|
| completed | status=syncing → reconcile → 删 draft → completed |
| error | 删 draft + 原回答不变 + 显示错误 |
| aborted | 删 draft + 原回答不变 |

**关键**：reconcile 用服务端 `GET /api/messages` 权威——messages.content_json 已经在 finalize 时原子更新。

**WS reconnect 测试**（`regenerate.spec.ts:243-290` test #5）：主动 `closeEventSocket` → 自动重连 → replay → 原 active 仍可见 → 最终一个 active assistant。

## 10. 已知限制

- **不支持完整浏览器 reload 恢复 active Regenerate draft**——`activeSessionId` 不在 URL / localStorage，reload 后 App.vue 选第一个 session；列 P2 URL routing
- **不支持手动切换历史 revision 为 active**——D2 显式不做（用户决定保留自动切换）
- **不持久化 "regenerated" badge**——PersistedMessage DTO 无对应字段
- **revision history UI / drawer 不实现**——D2 显式不做

## 11. Frontend Integration（D2-7）

前端关键设计：
- `PersistedMessageDto.message_id` 作 chatStore item.id——regenerate 后**就地更新**，不新增 bubble
- `RegenerationState` 单对象（regenerationId / requestId / targetMessageId / draftItemId / status / errorMessage）
- 独立 draft item（`regen-draft:{request_id}`）——流式期间显示在原回答下方
- `currentAssistantItemId = draftItemId`（regenerate 期间）
- 完成后：删 draft + reconcile + 原 message_id 位置显示新内容
- 模块级 `_regenerateInFlight` 同步 flag（防双击 race）+ watch 终态重置

## 12. 相关文档

- [Runtime and Harness](runtime-and-harness.md)
- [Web Request Lifecycle](web-request-lifecycle.md)
- [Persistence and Startup](persistence-and-startup.md)
- [Archived P1-D2 Design](../archive/superseded-designs/P1_D2_REGENERATE_DESIGN.md)——完整设计过程与决策点
- [P1-D2 Validation Report](../validation/p1-d/P1_D2_VALIDATION_REPORT.md)——冻结证据

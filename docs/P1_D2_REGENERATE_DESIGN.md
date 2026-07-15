# P1-D2 Regenerate — Detailed Design

> **状态**：✅ 设计已批准（用户 2026-07-15 审核 4 个决策点定稿）——按 §6 commit 顺序进入实现
> **前置**：P1-D1 ✅ FROZEN（tag `v0.0.26-export-markdown` @ `ebbc896`）
> **依赖阻塞**：Message ID 稳定性 characterization test ✅ 已落地（commit `a124697`，2 个 xfail）
> **目标 tag**：`v0.0.27-regenerate`（实现完成 + 验证后）

---

## 0. 设计原则

1. **非破坏性**：旧 active 回答在 regenerate 流式生成期间必须仍然可见、可访问
2. **可恢复**：error / abort / server restart 都不能让 session 进入"既无旧回答也无新回答"的中间态
3. **不动 core runtime**：所有逻辑在 Web 编排层（`web/app.py` + `web/extension_store.py` + `session_sqlite.py`）
4. **单一真源（canonical truth = `messages` 表）**：`messages.content_json` 永远是当前 active assistant content——
   - 下一轮 Prompt 使用的回答
   - Export 导出的回答
   - `GET /api/messages` 返回的回答
   - `web_message_revisions` 只存历史和生成尝试（**不**承担 active 角色）
5. **Message ID 稳定**：assistant_message_id 在 regenerate 期间不变——revision 通过外键引用
6. **`completed` ⟺ `active`**：去掉 `is_active` 字段，用 `status='completed'` 表达"这条 revision 是当前 active"——
   由 `UNIQUE(assistant_message_id) WHERE status='completed'` 部分索引强制单例
7. **自动切换 active，不做手动切换**：regenerate 成功 finalize 时自动把新回答设为 active；
   **不**提供用户手动切回历史 revision 的 UI/API（D2 显式不做）

---

## 1. 八个问题回答（审核要求）

### Q1：replace_messages() 当前是否重新生成历史 message ID？

**答**：**是**。已通过 characterization test 证明（commit `a124697`，两个 xfail strict=True）。

**根因**（`session_sqlite.py:505-536`）：
```python
async def replace_messages(self, session_id, messages):
    await db.execute("DELETE FROM messages WHERE session_id = ?", ...)
    for idx, msg in enumerate(messages):
        msg_id = _gen_id("msg")  # ← 每次调用产生新 uuid
        await db.execute("INSERT INTO messages (id, ...) VALUES (...)", ...)
```

`_gen_id` 用 `f"{prefix}-{ms_timestamp}-{uuid.uuid4().hex[:8]}""`——两次相同 messages 列表会产生完全不同的 ID 集合。

**实测**（`test_replace_messages_preserves_historical_ids`）：
```
first turn: u1_id = msg-1738..., a1_id = msg-1738...
after replace_messages with same messages:
  u1_id was msg-1738-aaa... → now msg-1739-bbb...  ❌
  a1_id was msg-1738-ccc... → now msg-1739-ddd...  ❌
```

### Q2：准备如何保证正常追加时历史 ID 稳定？

**答**：重写 `replace_messages` 为 **diff-based sync**——UPDATE 优先于 DELETE-INSERT。

**新实现**（伪代码）：
```python
async def replace_messages(self, session_id, messages):
    db = self._require_db()
    await self._require_session(session_id)
    now = _now_ms()

    # 读现有 rows 按 idx 排序
    cur = await db.execute(
        "SELECT id, idx, role FROM messages WHERE session_id = ? ORDER BY idx",
        (session_id,)
    )
    existing = await cur.fetchall()
    await cur.close()

    new_count = len(messages)

    # 1. UPDATE 现有 rows（0..min(len(existing), new_count)）
    for idx in range(min(len(existing), new_count)):
        msg = messages[idx]
        role = getattr(msg, "role", "custom") or "custom"
        content_json = _serialize_message(msg)
        # 用 (session_id, idx) 定位，保留 id
        await db.execute(
            "UPDATE messages SET role = ?, content_json = ? "
            "WHERE session_id = ? AND idx = ?",
            (role, content_json, session_id, idx),
        )

    # 2. INSERT 新尾部（如果 new_count > len(existing)）
    for idx in range(len(existing), new_count):
        msg = messages[idx]
        msg_id = _gen_id("msg")
        role = getattr(msg, "role", "custom") or "custom"
        content_json = _serialize_message(msg)
        await db.execute(
            "INSERT INTO messages (id, session_id, idx, role, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, idx, role, content_json, now),
        )

    # 3. DELETE 超出 new_count 的尾部（truncate 场景）
    if len(existing) > new_count:
        await db.execute(
            "DELETE FROM messages WHERE session_id = ? AND idx >= ?",
            (session_id, new_count),
        )

    await db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
    )
    await db.commit()
```

**关键不变量**：
- 正常追加（new_count > existing_count）：所有历史 row id **不变**
- 原地更新（new_count == existing_count）：所有 row id **不变**
- 历史 truncate（new_count < existing_count）：尾部 row id 消失，前面保留

**为什么按 (session_id, idx) 而不是 role + content hash？**
- idx 是 SQLite 已有的 UNIQUE 约束（`UNIQUE(session_id, idx)`）
- harness.run_prompt 后的 final_messages 总是 history + new turn，idx 顺序保留
- truncate 场景（branch / fork）会显式重置 idx，所以 diff 算法只需"对齐 idx"

**风险**：如果未来有调用方传**乱序** messages（idx 不对齐），UPDATE 会错配。
- 当前调用方只有 `web/app.py::_run_prompt_core`，从 `list_messages` + 新 turn 拼接——idx 对齐
- 加 invariant assertion：`assert all(role matches) or logger.warning(...)`

### Q3：running candidate 内容保存在哪里？

**答**：流式期间 **不写 DB**——delta 只存在前端 chatStore（用户视觉看到 draft）和 WS 事件流。

**revision row 的生命周期（决策点 1+2 定稿）**：

| 时刻 | 操作 | revision 状态 |
|------|------|---------------|
| regenerate 启动（POST /regenerate） | INSERT revision row, `status='running'`, `content_json=NULL` | running |
| 模型流式生成 | **不** UPDATE revision；前端 chatStore 累积 delta | running |
| 模型执行成功 → finalize | 单 transaction 内 INSERT revision 0（首次）/ UPDATE 上一个 completed → superseded + UPDATE 本 revision → completed + UPDATE messages | completed |
| 失败 / 中止 | UPDATE 本 revision → error / aborted | error / aborted |

**关键设计**：
- **revision 0 延迟创建**（决策点 1）：**只在第一次 regenerate 成功 finalize 时**，把当时的旧 active 内容保存为 revision 0（`status='superseded'`）。失败 / 中止 **不**产生 revision 0——避免无意义的历史污染。
- **流式期间不写 candidate**（决策点 2）：模型执行成功后**一次性**把完整 candidate 写入 revision.content_json，并原子更新 messages.content_json。
- 后续 regenerate：上一个 `completed` revision 被 UPDATE 为 `superseded`，新 revision 成为 `completed`——无需再为旧 active 单独 INSERT（它已经是 revision 表里的一行）。

### Q4：是否更新 messages 表？在什么时间更新？

**答**：**只在 finalize 成功时**——单 SQLite transaction 内原子切换。

| 阶段 | messages.content_json | revision.content_json | revision.status |
|---|---|---|---|
| regenerate start | 旧回答 | NULL | running |
| streaming | 旧回答（不变） | NULL（不写） | running |
| finalize success（非首次） | **新回答**（UPDATE） | **新回答**（UPDATE） | completed |
| finalize success（首次） | **新回答**（UPDATE） | revision 0 ← 旧回答（INSERT, superseded）；revision 1 ← 新回答（UPDATE, completed） | completed |
| finalize error | 旧回答（不变） | NULL | error |
| abort | 旧回答（不变） | NULL | aborted |
| server restart | 旧回答（不变） | NULL | interrupted（startup sweep 改） |

**为什么不流式写 messages 表？**
1. 流式写会让 active content 在 finalize 前不断变化 → 任何并发 GET /api/messages 看到中间态
2. 失败时回滚成本高（要恢复旧 content_json）
3. 非破坏性原则要求"旧回答在 finalize 前必须可见"——流式写违反

### Q5：revision finalize 的 SQLite transaction 包含哪些 SQL？

**答**：核心 4 步 SQL 在**单 `BEGIN IMMEDIATE` transaction** 内（去掉 `is_active`，用 `status='completed'` 表达 active）：

```sql
BEGIN IMMEDIATE;

-- 1. 校验：revision 仍是 running（未 abort / 未被 supersede / 未被 sweep 改 interrupted）
SELECT status FROM web_message_revisions WHERE id = ?;
-- expect 'running'——否则放弃 finalize（ROLLBACK）

-- 2a. 校验：没有 rival completed revision（并发 finalize 防御）
SELECT id FROM web_message_revisions
  WHERE assistant_message_id = ?
    AND status = 'completed'
    AND id != ?;
-- 注：uq_web_message_revision_active 部分唯一索引保证最多 1 个 completed
-- 若存在且 id 不同 → 放弃（理论不应发生，因为 running 也唯一）

-- 2b. （首次 regenerate）INSERT revision 0 把旧 active 归档为 superseded
--     判断条件：本 assistant_message_id 下没有任何 status IN ('superseded','completed') 的历史 revision
INSERT INTO web_message_revisions (
    id, session_id, assistant_message_id, revision_number,
    request_id, status, base_content_sha256, content_json,
    created_at, completed_at
) VALUES (
    ?, ?, ?, 0,
    NULL, 'superseded', ?, ?,
    ?, ?
);
-- 注：revision 0 的 request_id 设为 NULL（不属于任何 web_run_request）
--     base_content_sha256 = sha256(旧 active content_json)（与本次 running revision 相同）
--     content_json = 旧 active content_json

-- 3. （非首次）把上一个 completed revision 改为 superseded
UPDATE web_message_revisions
  SET status = 'superseded', completed_at = ?
  WHERE assistant_message_id = ?
    AND status = 'completed'
    AND id != ?;
-- 受影响行数：0（首次，由 2b 处理）或 1（非首次）

-- 4. 新 revision 标记 completed + 写入完整 candidate content
UPDATE web_message_revisions
  SET status = 'completed',
      content_json = ?,
      completed_at = ?
  WHERE id = ?;

-- 5. messages 表替换 active content（保留 id 与 role！）
UPDATE messages
  SET content_json = ?
  WHERE id = ?;

UPDATE sessions SET updated_at = ? WHERE id = ?;

COMMIT;
```

**关键变化（vs 旧设计）**：
- 去掉 `is_active` 字段——`status='completed'` ⟺ `active`，由部分唯一索引保证
- 加 `base_content_sha256`（NOT NULL）：revision 创建瞬间的旧 active content_json 的 SHA-256
  - **用途 1**：审计——知道这次 regenerate 是基于哪个版本的 active 生成的
  - **用途 2**（可选）：finalize 时校验 `sha256(messages.content_json) == base_content_sha256`，防止外部修改
- 首次 regenerate 的 revision 0 INSERT 与非首次的 UPDATE 互斥（通过 step 3 受影响行数判断）

**失败回滚**：任何一步失败 → `ROLLBACK`，messages 表保持旧 active，revision 仍 running（可重试 finalize 或人工 abort）。

**隔离级别**：BEGIN IMMEDIATE 取得 write lock，防止并发 finalize 竞争；部分唯一索引 `uq_web_message_revision_running` / `uq_web_message_revision_active` 在 SQLite 层兜底，应用层校验作为友好错误路径。

### Q6：下一次普通 prompt 如何只使用 active answer？

**答**：**`list_messages` 已经只返回 messages 表 row**——messages 表永远是 active answer。

```
普通 prompt 流程：
  1. messages = await store.list_messages(sid)
     # ← 只读 messages 表，自动是 active content
  2. harness.run_prompt(text, initial_messages=messages)
  3. await store.replace_messages(sid, harness.agent.state.messages)
     # ← 修复后的 diff sync：历史 ID 不变，新 turn append 到尾部
```

**关键**：`web_message_revisions` 表**不参与**普通 prompt 的 list/replace——它只是历史记录。

**新增 API**（D2 实现）：
- `GET /api/sessions/{sid}/messages/{mid}/revisions`——列出某条 assistant 的所有 revision（分页 + 单次返回上限，见决策点 3）

### Q7：regenerate error/abort 后如何恢复 Harness 的 canonical context？

**答**：通过**拆分 `_run_prompt_core`** —— regenerate 用专用路径 `_execute_prompt`，**不调 replace_messages**。

**当前 `_run_prompt_core` 的耦合问题**（要拆分）：
```python
# 当前实现（伪代码）
async def _run_prompt_core(...):
    messages = await store.list_messages(sid)
    await harness.run_prompt(text, initial_messages=messages)
    await store.replace_messages(sid, harness.agent.state.messages)
    # ↑ 这一步 regenerate 不能要——会覆盖 active
    await store.append_snapshot(sid, snapshot)
```

**D2 拆分**（只动 `web/app.py`，不动 `loop.py` / `harness.py`）：

```python
async def _execute_prompt(
    sid, text, *, skill_selection=None, file_ids=None,
    truncate_to_idx=None,  # ← D2 新增：regenerate 时截断到 preceding user
) -> PromptResult:
    """只执行模型/Agent，不持久化 session messages。

    返回：
    - new_messages: 模型生成的 messages（不含 history）
    - snapshot: turn snapshot
    - final_context: harness context（用于 metadata）

    失败抛异常——调用方决定是否持久化。
    """
    messages = await store.list_messages(sid)
    if truncate_to_idx is not None:
        messages = messages[:truncate_to_idx + 1]
    # 让 harness 在截断 context 上跑
    await harness.run_prompt(text, initial_messages=messages)
    new_messages = harness.agent.state.messages[len(messages):]
    snapshot = harness.last_snapshot
    return PromptResult(new_messages=new_messages, snapshot=snapshot)


async def _persist_normal_prompt_result(sid, result):
    """普通 prompt 持久化路径——replace_messages + append_snapshot。"""
    messages = await store.list_messages(sid)
    await store.replace_messages(sid, messages + result.new_messages)
    await store.append_snapshot(sid, result.snapshot)


async def _persist_regeneration_result(
    sid, assistant_message_id, revision_id, result
):
    """Regenerate finalize 路径——单 transaction 原子切换 active content。

    失败时不影响 messages 表。
    """
    # 5 个 SQL 在一个 BEGIN IMMEDIATE 内（见 Q5）
    await extension_store.finalize_revision(
        revision_id=revision_id,
        assistant_message_id=assistant_message_id,
        new_content_json=_serialize_message(result.new_messages[-1]),
    )
    # snapshot 仍然 append（revision history 也算一个 turn 记录）
    await store.append_snapshot(sid, result.snapshot)
```

**Harness 状态恢复**：

| 场景 | Harness 状态 |
|---|---|
| 普通 prompt 完成 | harness.agent.state.messages = list_messages(sid) + new turn |
| Regenerate 成功 | harness.agent.state.messages = list_messages(sid)（含新 active） |
| Regenerate error | harness.agent.state.messages 含截断 context + 错误 turn → 需 reset |
| Regenerate abort | 同上 |
| 普通 prompt 后 regenerate | 同上 |

**关键**：每次新 prompt 开头都 `list_messages(sid)` 重建 context——Harness 不会"记住"上次 regenerate 的截断 context。

D2 新增 helper（`web/app.py` 内）：
```python
async def _reset_harness_to_session(sid):
    """每个 prompt 开始前重置 harness agent messages 到 SQLite 当前 active state。"""
    messages = await store.list_messages(sid)
    harness.agent.state.messages = list(messages)
    harness.agent.reset_status()  # idle
```

### Q8：server restart 后如何处理 running revision？

**答**：**startup 时 sweep**——所有 `status='running'` 的 revision 改为 `'interrupted'`。

```python
# extension_store.py 新增
async def sweep_interrupted_revisions(self):
    """Startup 调用——把遗留 running revision 标记 interrupted。"""
    db = self._require_db()
    now = _now_ms_iso()
    await db.execute(
        "UPDATE web_message_revisions "
        "SET status = 'interrupted', "
        "    completed_at = ?, "
        "    error_summary = 'server restart' "
        "WHERE status = 'running'",
        (now,),
    )
    await db.commit()
```

**调用时机**：`app.lifespan` startup，在 `_restore_mcp_servers()` 之后。

**为什么 messages 表不用动？**
- finalize 未成功 → messages.content_json 仍是旧 active
- 用户 reconnect 时看到旧回答 + "上次重新生成被中断"提示

**前端体验**：
- `GET /api/sessions/{sid}/messages/{mid}/revisions` 返回 `interrupted` revision
- 前端在该 assistant 消息下方显示 "Regeneration was interrupted. [Retry]"
- Retry 按钮触发新的 regenerate（创建新 revision row）

---

## 2. Revision Schema（D2 新增——定稿版）

### 2.1 DDL（用户 2026-07-15 定稿）

```sql
-- extension_store.py SCHEMA_VERSION 1 → 2

CREATE TABLE IF NOT EXISTS web_message_revisions (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    assistant_message_id  TEXT NOT NULL,
    revision_number       INTEGER NOT NULL,
    request_id            TEXT,
    status                TEXT NOT NULL CHECK (
        status IN (
            'running',
            'completed',
            'superseded',
            'error',
            'aborted',
            'interrupted'
        )
    ),
    base_content_sha256   TEXT NOT NULL,
    content_json          TEXT,
    created_at            TEXT NOT NULL,
    completed_at          TEXT,
    error_summary         TEXT,

    UNIQUE (assistant_message_id, revision_number),
    FOREIGN KEY (session_id)
        REFERENCES sessions(id)
        ON DELETE CASCADE
);

-- 每个 web_run_request 至多关联 1 个 revision
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_request
    ON web_message_revisions(request_id)
    WHERE request_id IS NOT NULL;

-- 每个 assistant_message_id 至多 1 个 running（防并发 regenerate）
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_running
    ON web_message_revisions(assistant_message_id)
    WHERE status = 'running';

-- 每个 assistant_message_id 至多 1 个 completed（= active）——替代旧设计的 is_active
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_active
    ON web_message_revisions(assistant_message_id)
    WHERE status = 'completed';

-- 历史查询主索引（分页）
CREATE INDEX IF NOT EXISTS idx_web_message_revisions_history
    ON web_message_revisions(
        session_id,
        assistant_message_id,
        revision_number DESC
    );
```

### 2.2 字段语义

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | TEXT PK | revision row id（`_gen_id("rev")`） |
| `session_id` | TEXT NOT NULL | 所属 session，FK to `sessions(id)` ON DELETE CASCADE |
| `assistant_message_id` | TEXT NOT NULL | 对应 `messages.id`——**不**加跨表 FK（顺序敏感，应用层校验） |
| `revision_number` | INTEGER NOT NULL | 0 = 首次归档的旧 active（决策点 1：延迟创建）；1+ = 后续 regenerate candidate |
| `request_id` | TEXT nullable | 关联 `web_run_requests`——regenerate 启动时写入；revision 0 归档行写 NULL |
| `status` | TEXT NOT NULL | 状态机见 §2.3 |
| `base_content_sha256` | TEXT NOT NULL | **revision 创建瞬间的 `messages.content_json`（旧 active）的 SHA-256**——审计 + 可选 finalize 一致性校验 |
| `content_json` | TEXT nullable | 完整 AssistantMessage JSON；running/aborted/interrupted 期间为 NULL |
| `created_at` | TEXT NOT NULL | ISO 时间戳 |
| `completed_at` | TEXT nullable | 离开 running 态的时间戳 |
| `error_summary` | TEXT nullable | error / interrupted 时的人类可读摘要 |

> **注**：`base_content_sha256` 是用户定稿 schema 中引入的 NOT NULL 字段，文档中标注的"SHA-256 of messages.content_json at revision creation"是基于 finalize 流程的推断语义——若实际意图不同（如 SHA of 截断 context 而非 active row），实现前请用户再确认一次。

### 2.3 状态机

```
                ┌─────────────────────────────┐
                │  regenerated (user action)  │
                └──────────────┬──────────────┘
                               │
                               ▼
                           ┌───────┐
              ┌─ success ─ │running│ ─ abort ──────┐
              │            └───┬───┘               │
              │                │                   ▼
              │                │              ┌─────────┐
              │   ┌─ error ────┘              │ aborted │
              │   ▼                            └─────────┘
              │ ┌───────┐
              │ │ error │                     ┌───────────────┐
              │ └───────┘      server restart │ interrupted   │
              │                  ───────────▶ │ (startup sweep│
              │                                │  改)          │
              │   previous completed          └───────────────┘
              │        ▼
              │   ┌───────────┐  next regenerate success  ┌───────────┐
              └──▶│ completed │ ────────────────────────▶ │ superseded│
                  └───────────┘                            └───────────┘
                  (= active)
```

| status | 含义 | 终态？ |
|--------|------|--------|
| `running` | 正在生成的候选 | 否（可 → completed / error / aborted / interrupted） |
| `completed` | **当前 active 内容对应的历史 revision** | 否（被下一个 completed 推为 superseded） |
| `superseded` | 曾经 active、现已被新回答替代 | 是 |
| `error` | 生成或 finalize 失败 | 是 |
| `aborted` | 用户中止 | 是 |
| `interrupted` | server restart 导致未完成（startup sweep 改） | 是 |

**关键不变量**：
- `completed` ⟺ `active`——messages 表的当前 content_json 对应唯一的 completed revision（由 `uq_web_message_revision_active` 强制）
- 同一 `assistant_message_id` 下至多 1 个 `running`（由 `uq_web_message_revision_running` 强制）
- 同一 `request_id` 至多关联 1 个 revision（由 `uq_web_message_revision_request` 强制）

### 2.4 外键策略

**`session_id` 加 FK to `sessions(id)` ON DELETE CASCADE**（用户定稿）：
- sessions 表 PK 是 `id TEXT`，类型匹配
- `extension_store.py` 已 `PRAGMA foreign_keys=ON`（行 175）
- 本库已有 FK CASCADE 先例：`web_disabled_tools.server_name REFERENCES web_mcp_servers(name) ON DELETE CASCADE`
- 行为：DELETE session 自动级联删除该 session 下所有 revision row

**`assistant_message_id` 不加跨表 FK**（沿用旧设计）：
- session_store 的 messages 表创建在前，extension_store 的 revisions 表创建在后——FK 依赖顺序敏感
- 应用层校验：`finalize_revision` / `create_revision` 时 `SELECT FROM messages WHERE id=?` 确认存在
- 由 `idx_web_message_revisions_history` 索引加速查询

---

## 3. 完整生命周期（时序）

### 3.1 普通 prompt（不变）

```
User                Web App              SQLite              Harness
 │                    │                    │                    │
 ├─ POST /prompt ────▶│                    │                    │
 │                    ├─ list_messages ───▶│                    │
 │                    │◀─ active msgs ─────│                    │
 │                    ├─ run_prompt ───────────────────────────▶│
 │                    │                    │                    │
 │   WS stream ◀──────┤ (WebEventEnvelope) │                    │
 │                    │                    │                    │
 │                    │◀─ final msgs ──────────────────────────│
 │                    ├─ replace_messages ▶│                    │
 │                    │   (diff sync)      │                    │
 │                    │   历史 ID 不变      │                    │
 │                    ├─ append_snapshot ─▶│                    │
 │◀─ 200 OK ──────────┤                    │                    │
```

### 3.2 Regenerate 成功

```
User                Web App              SQLite              Harness
 │                    │                    │                    │
 ├─ POST             │                    │                    │
 │  /messages/{mid}  │                    │                    │
 │  /regenerate ────▶│                    │                    │
 │                    │                    │                    │
 │                    │  ┌── begin txn ─────────────────────┐  │
 │                    │  │ sha = sha256(messages.content_   │  │
 │                    │  │  json WHERE id = mid)            │  │
 │                    ├─ │ INSERT revision N+1              │  │
 │                    │  │   (status='running',             │  │
 │                    │  │    content_json=NULL,            │  │
 │                    │  │    base_content_sha256=sha,      │  │
 │                    │  │    request_id=req_id)            │  │
 │                    │  └── commit ────────────────────────┘  │
 │                    │                    │                    │
 │                    ├─ _execute_prompt ──────────────────────▶│
 │                    │   (truncate to preceding user) │   │
 │                    │                    │                    │
 │   WS stream ◀──────┤  draft 增长（前端 chatStore，不写 DB） │
 │   (revision_id)    │                    │                    │
 │                    │                    │                    │
 │                    │◀─ candidate assistant ─────────────────│
 │                    │                    │                    │
 │                    │  ┌── BEGIN IMMEDIATE ─────────────┐   │
 │                    │  │ 1. SELECT revision.status       │   │
 │                    │  │    (verify still 'running')     │   │
 │                    │  │ 2. SELECT rival completed       │   │
 │                    │  │    (verify no winner)           │   │
 │                    │  │ 3. 首次？INSERT revision 0      │   │
 │                    │  │    (旧 active, status=          │   │
 │                    │  │    'superseded')                │   │
 │                    │  │    非首次？UPDATE prev          │   │
 │                    │  │    completed → superseded       │   │
 │                    │  │ 4. UPDATE 新 revision →         │   │
 │                    │  │    completed+content_json       │   │
 │                    │  │ 5. UPDATE messages.content_json │   │
 │                    │  │    (保留 messages.id)           │   │
 │                    │  │ 6. UPDATE sessions.updated_at   │   │
 │                    │  └── COMMIT ───────────────────────┘   │
 │                    ├─ append_snapshot ─▶│                    │
 │◀─ 200 OK ──────────┤                    │                    │
```

### 3.3 Regenerate Error

```
                    ├─ _execute_prompt ────▶│  (raises)
                    │  ┌── begin txn ─────┐
                    │  │ UPDATE revision   │
                    │  │ SET status='error'│
                    │  │     error_summary │
                    │  └── commit ─────────┘
                    │  (messages 表不动)
                    │  (harness reset 到 active messages)
```

### 3.4 Server Restart（startup sweep）

```
lifespan startup:
  → _restore_mcp_servers (existing, P1-C)
  → sweep_interrupted_revisions (D2 新增)
      UPDATE web_message_revisions
        SET status='interrupted', error_summary='server restart'
        WHERE status='running';
```

---

## 4. API 设计

### 4.1 新增 endpoints

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/api/sessions/{sid}/messages/{mid}/regenerate` | 启动 regenerate，返回 revision_id |
| `GET` | `/api/sessions/{sid}/messages/{mid}/revisions` | 列出该 assistant 的所有 revision（分页 + 单次返回上限，决策点 3） |
| `GET` | `/api/sessions/{sid}/messages/{mid}/revisions/{rid}` | 查看某 revision 详情 |

**显式不做**（决策点 4）：
- ~~`POST /api/sessions/{sid}/messages/{mid}/revisions/{rid}/activate`~~——用户手动切换 active revision 不在 D2 范围内
- regenerate 成功 finalize 时**自动**切换 active（messages.content_json UPDATE 即是切换），无需显式 endpoint

### 4.2 regenerate request/response

```http
POST /api/sessions/sess_abc/messages/msg_a1/regenerate
Content-Type: application/json

{
  "skill_selection": null,
  "file_ids": []
}
```

**Response 202 Accepted**：
```json
{
  "revision_id": "rev_xxx",
  "assistant_message_id": "msg_a1",
  "status": "running"
}
```

**事件流**（WS，已有的 envelope）：
- `assistant_message_start`（payload 含 `revision_id`）
- `assistant_message_delta`（流式 chunk）
- `assistant_message_end`（含 `stop_reason`）
- `revision_finalized`（D2 新 event type，含 revision_id + status）

### 4.3 revisions list response

```http
GET /api/sessions/sess_abc/messages/msg_a1/revisions?limit=20&offset=0
```

**Query params**（决策点 3：分页）：
- `limit`：单次返回上限，默认 20，最大 100
- `offset`：偏移量，默认 0

```json
{
  "count": 3,
  "limit": 20,
  "offset": 0,
  "revisions": [
    {
      "id": "rev_001",
      "revision_number": 0,
      "status": "superseded",
      "base_content_sha256": "9f2c...",
      "created_at": "2026-07-14T10:00:00Z",
      "completed_at": "2026-07-14T10:00:05Z"
    },
    {
      "id": "rev_002",
      "revision_number": 1,
      "status": "superseded",
      "base_content_sha256": "9f2c...",
      "created_at": "2026-07-14T10:05:00Z",
      "completed_at": "2026-07-14T10:05:04Z",
      "error_summary": null
    },
    {
      "id": "rev_003",
      "revision_number": 2,
      "status": "completed",
      "base_content_sha256": "9f2c...",
      "created_at": "2026-07-14T10:10:00Z",
      "completed_at": "2026-07-14T10:10:06Z"
    }
  ]
}
```

**注**：返回不含 `content_json`（大字段）；详情 endpoint `GET /revisions/{rid}` 才返回完整 content。`is_active` 字段也移除——客户端用 `status == 'completed'` 判断。

---

## 5. Schema Migration（v1 → v2）

### 5.1 流程

```python
# extension_store.py 新增
SCHEMA_VERSION = 2  # was 1

async def _migrate_schema(self):
    """Startup 调用——根据当前 version 跑迁移。"""
    current = await self.get_schema_version()
    if current is None:
        # 全新数据库——_exec_schema 直接建 v2
        return
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        raise ExtensionStoreError(
            f"database schema version {current} > code version {SCHEMA_VERSION}; "
            "downgrade not supported"
        )
    # current < SCHEMA_VERSION——跑迁移
    if current == 1:
        await self._migrate_v1_to_v2()
        # 更新 schema_meta
        await self._db.execute(
            "UPDATE web_extension_schema_meta SET version = ? WHERE id = 1",
            (SCHEMA_VERSION,),
        )
        await self._db.commit()

async def _migrate_v1_to_v2(self):
    """加 web_message_revisions 表 + 4 索引（用户定稿版 schema）。

    幂等——用 CREATE TABLE IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS。
    不动 uploaded_skills / mcp_servers / disabled_tools。
    """
    db = self._require_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS web_message_revisions (
            id                    TEXT PRIMARY KEY,
            session_id            TEXT NOT NULL,
            assistant_message_id  TEXT NOT NULL,
            revision_number       INTEGER NOT NULL,
            request_id            TEXT,
            status                TEXT NOT NULL CHECK (
                status IN (
                    'running',
                    'completed',
                    'superseded',
                    'error',
                    'aborted',
                    'interrupted'
                )
            ),
            base_content_sha256   TEXT NOT NULL,
            content_json          TEXT,
            created_at            TEXT NOT NULL,
            completed_at          TEXT,
            error_summary         TEXT,
            UNIQUE (assistant_message_id, revision_number),
            FOREIGN KEY (session_id)
                REFERENCES sessions(id)
                ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_request
            ON web_message_revisions(request_id)
            WHERE request_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_running
            ON web_message_revisions(assistant_message_id)
            WHERE status = 'running';

        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_message_revision_active
            ON web_message_revisions(assistant_message_id)
            WHERE status = 'completed';

        CREATE INDEX IF NOT EXISTS idx_web_message_revisions_history
            ON web_message_revisions(
                session_id,
                assistant_message_id,
                revision_number DESC
            );
    """)
```

### 5.2 验证清单

- [ ] v0.0.25 数据库（schema v1）启动后自动升级到 v2，无数据丢失
- [ ] migration 幂等——多次跑 `_migrate_v1_to_v2` 不出错
- [ ] v0.0.25 既有 session / Skill / MCP 配置完全不受影响
- [ ] 全新数据库直接建 v2（schema_meta.version = 2）
- [ ] version > 2 时安全失败（防止 downgrade 损坏）
- [ ] `web_message_revisions` 表空时 list_messages / regenerate 行为正常

### 5.3 Rollback 策略

如果 D2 上线后发现严重 bug：
1. `DROP TABLE web_message_revisions;`
2. `UPDATE web_extension_schema_meta SET version=1;`
3. 回滚到 commit `ebbc896`（D1 tag）

**数据损失**：仅 D2 期间产生的 revision 历史丢失；`messages` 表 / Skill / MCP 配置完全保留。

---

## 6. 实现拆解（建议 commit 顺序）

| # | Commit | 范围 | 测试 |
|---|---|---|---|
| **1** | feat(session-sqlite): diff-based replace_messages | 修 Q2 + 移除 characterization test 的 xfail | 跑 `test_d2_message_id_stability.py`（应全 GREEN） |
| **2** | feat(extension-store): web_message_revisions schema + migration v1→v2 | DDL（用户定稿版）+ migrate + sweep_interrupted | schema 单元测试 + migration 测试（含 FK CASCADE 删 session） |
| **3** | feat(extension-store): revision CRUD（create/finalize/list/sweep） | 数据访问层；finalize 实现单 transaction 内的"首次 INSERT revision 0 / 非首次 UPDATE 旧 completed→superseded"+ "本 revision → completed"+ "messages UPDATE" | CRUD 单元测试（含首次/非首次/并发 rival 三类） |
| **4** | refactor(web/app): split _execute_prompt + _persist_normal/regeneration | Q7 拆分（不引入新功能） | 现有 prompt 测试不回归 |
| **5** | feat(web): POST /regenerate + GET /revisions endpoints（含分页） | API 层；**不**含 `/activate`（决策点 4：不做手动切换） | endpoint 集成测试（含分页 limit/offset） |
| **6** | feat(web): revision_finalized WS event + 前端 active 切换 UI | 前端：finalize 后自动刷新消息；revision badge 显示历史 | E2E |
| **7** | test(e2e): regenerate flow + abort + restart recovery | E2E | 7-10 个 E2E 用例（首次/非首次/abort/restart 后 interrupted 提示） |
| **8** | docs: P1-D2 release notes + tag v0.0.27-regenerate | 文档 | — |

**每个 commit 都要跑**：
- `pytest tests/ -v -m "not slow and not integration and not docker"` 全 GREEN
- `ruff check src tests` clean
- frontend `npm run build:e2e` 通过
- E2E 不回归（26/28 baseline，两个失败是 P1-C 既有污染）

---

## 7. 显式不做（P1-D2 边界）

| 不做 | 原因 | 后续 |
|---|---|---|
| **branch / fork session** | 需要 snapshot fork + 多 session 分叉管理 | P2 / P3 |
| **跨 session regenerate** | 用户语义模糊（regenerate 到哪个 session？） | 不做 |
| **revision diff viewer** | UI 复杂，P1 不值得 | P2 |
| **revision count 硬限制 / 自动 prune** | 决策点 3：暂不限制，保留完整历史；查询接口分页 + 单次返回上限 | 后续可选（P2 加 prune API） |
| **revision metadata（temperature / model）** | 当前每个 prompt 不带这些参数 | P2 provider routing |
| **collaborative editing** | 多用户场景 | 不做（localhost only） |
| **revision export** | 已有 session-level export（D1） | 后续可选 |
| **multimodal regenerate**（图片 regenerate） | P0 明确不支持图片理解 | 不做 |
| **手动 active revision 切换** | 决策点 4：自动切换已满足核心需求；手动切换需 revision list UI + 二次确认 + active 还原逻辑 | 不做（连 P2 也暂不计划） |
| **流式期间写 candidate 到 SQLite** | 决策点 2：delta 只走前端 chatStore + WS 事件流；模型执行成功后一次性写完整 content | 不做（断点续传场景不存在） |

---

## 8. 风险与控制

### 8.1 高风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **replace_messages 改 diff sync 后破坏现有 prompt 流程** | 所有 prompt 失败 | Commit 1 单独验证 + 跑全 baseline |
| **finalize transaction 死锁**（并发 regenerate） | revision 卡住 | BEGIN IMMEDIATE + 应用层校验 |
| **revision 0 延迟创建的 race** | 同一 assistant 并发 regenerate | `UNIQUE(assistant_message_id, revision_number)` + max(revision_number)+1 子查询 |

### 8.2 中风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **WS 重连期间 finalize 完成** | 用户看不到切换 | B3 replay 机制已解决（revision_finalized 是 envelope） |
| **revision 表膨胀**（高频 regenerate） | DB 慢 | 软限制 + 后续 prune API |
| **regenerate 用 truncated context 改变 Harness 内部状态** | 下次普通 prompt 错乱 | Q7 的 `_reset_harness_to_session` 保证每次重建 |

### 8.3 低风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **migration 失败** | startup 崩 | 验证清单 5.2 全跑 |
| **schema 版本不匹配（老代码 + 新 DB）** | 启动报错 | version > code version 时安全失败 |

---

## 9. 用户决策点（2026-07-15 已定稿 ✅）

| # | 决策 | 结论 | 说明 |
|---|------|------|------|
| 1 | Revision 0 创建时机 | **延迟创建** | **只在第一次 regenerate 成功 finalize 时**，把当时的旧 active 内容保存为 revision 0（status='superseded'）；失败/中止不产生无意义的 revision 0 |
| 2 | 流式期间写入 candidate | **不周期写 SQLite** | delta 只存在前端 draft 和事件流；模型执行成功后一次性把完整 candidate 写入 revision，并原子更新 active message |
| 3 | Revision 数量限制 | **暂不硬限制** | 保留完整历史；查询接口必须分页和限制单次返回数量，不做自动删除 |
| 4 | Active revision 切换 | **自动切换必须做，手动切换不做** | regenerate 成功后自动将新回答设为 active（finalize transaction 内完成）；**不**提供用户手动切回旧 revision 的 UI/API |

**第 4 点的明确区分**：

✅ **必须实现**：completed 后自动切换 active revision（finalize transaction 的 `UPDATE messages SET content_json=?` + revision `status='completed'` 即是切换）

❌ **本阶段不实现**：用户手动选择、恢复或切换历史 revision 的 endpoint 或 UI（`POST /revisions/{rid}/activate` 不在 D2 范围内）

### 进入编码的 gating（已通过）

- [x] 八个问题回答是否完整、准确
- [x] revision schema 是否符合预期（用户定稿 schema 已落入 §2.1）
- [x] commit 顺序是否合理
- [x] 决策点 1-4 的推荐选项是否接受（全部按推荐 + 用户细化）

**下一步**：按 §6 commit 顺序进入实现阶段。Commit 1（diff-based `replace_messages`）解除 xfail 是入口。

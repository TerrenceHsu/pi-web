# P1-D2 Regenerate — Detailed Design

> **状态**：D2 详细设计阶段（用户审核 2026-07-14 批准进入设计，**暂不批准编码**）
> **前置**：P1-D1 ✅ FROZEN（tag `v0.0.26-export-markdown` @ `ebbc896`）
> **依赖阻塞**：Message ID 稳定性 characterization test ✅ 已落地（commit `a124697`，2 个 xfail）
> **目标 tag**：`v0.0.27-regenerate`（实现完成 + 验证后）

---

## 0. 设计原则

1. **非破坏性**：旧 active 回答在 regenerate 流式生成期间必须仍然可见、可访问
2. **可恢复**：error / abort / server restart 都不能让 session 进入"既无旧回答也无新回答"的中间态
3. **不动 core runtime**：所有逻辑在 Web 编排层（`web/app.py` + `web/extension_store.py` + `session_sqlite.py`）
4. **单一真源**：`messages` 表 row 永远是当前 active assistant content；`web_message_revisions` 只存历史/候选
5. **Message ID 稳定**：assistant_message_id 在 regenerate 期间不变——revision 通过外键引用

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

**答**：`web_message_revisions.content_json`——D2 新增表的字段。

**关键设计**：**不创建第二条 canonical assistant row**。
- `messages` 表 row（assistant）保持 active 回答，**直到 finalize 成功才 UPDATE**
- 流式生成期间，candidate 累积在：
  - **前端**：chatStore.streamItems（用户视觉上看到 draft）
  - **后端**（可选）：`web_message_revisions.content_json`（partial，用于 server restart 后断点续传）
  - 选填策略：每 N 个 delta 或每 T 秒 UPDATE 一次 revision.content_json（flush）

**简化方案（推荐 D2 实现）**：流式期间**不**写 DB——只在前端 chatStore 维护 draft。Revision row 在 regenerate 启动时创建（status='running', content_json=空），finalize 时一次性写入完整 content_json。

### Q4：是否更新 messages 表？在什么时间更新？

**答**：**只在 finalize 成功时**——单 SQLite transaction 内原子切换。

| 阶段 | messages.content_json | revision.content_json | revision.status |
|---|---|---|---|
| regenerate start | 旧回答 | 空 | running |
| streaming | 旧回答（不变） | 空（不写） | running |
| finalize success | **新回答**（UPDATE） | **新回答**（UPDATE） | completed |
| finalize error | 旧回答（不变） | error_summary | error |
| abort | 旧回答（不变） | 空 | aborted |
| server restart | 旧回答（不变） | 空 | interrupted（startup 改） |

**为什么不流式写 messages 表？**
1. 流式写会让 active content 在 finalize 前不断变化 → 任何并发 GET /api/messages 看到中间态
2. 失败时回滚成本高（要恢复旧 content_json）
3. 非破坏性原则要求"旧回答在 finalize 前必须可见"——流式写违反

### Q5：revision finalize 的 SQLite transaction 包含哪些 SQL？

**答**：5 个 SQL 在**单 BEGIN IMMEDIATE transaction** 内：

```sql
BEGIN IMMEDIATE;

-- 1. 校验：revision 仍是 running（未 abort / 未被 supersede）
SELECT status FROM web_message_revisions WHERE id = ?;
-- expect 'running'——否则放弃 finalize

-- 2. 校验：目标 assistant 仍是 active（未被其他 regenerate 改）
SELECT id FROM web_message_revisions
  WHERE assistant_message_id = ?
    AND is_active = 1
    AND id != ?;
-- expect no rows——否则放弃（另一个 regenerate 已经赢了）

-- 3. 把旧 active revision 标记 superseded
UPDATE web_message_revisions
  SET status = 'superseded', is_active = 0, completed_at = ?
  WHERE assistant_message_id = ?
    AND is_active = 1
    AND id != ?;

-- 4. 新 revision 标记 completed + active + 写入完整 content
UPDATE web_message_revisions
  SET status = 'completed', is_active = 1,
      content_json = ?, completed_at = ?
  WHERE id = ?;

-- 5. messages 表替换 active content（保留 id！）
UPDATE messages
  SET content_json = ?
  WHERE id = ?;
-- 注：role 不变（assistant→assistant）

UPDATE sessions SET updated_at = ? WHERE id = ?;

COMMIT;
```

**失败回滚**：任何一步失败 → `ROLLBACK`，messages 表保持旧 active，revision 仍 running（可重试 finalize 或人工 abort）。

**隔离级别**：BEGIN IMMEDIATE 取得 write lock，防止并发 finalize 竞争。

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
- `GET /api/sessions/{sid}/messages/{mid}/revisions`——列出某条 assistant 的所有 revision
- `POST /api/sessions/{sid}/messages/{mid}/revisions/{rid}/activate`——切换 active revision（可选 P1-D2.1）

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

## 2. Revision Schema（D2 新增）

### 2.1 DDL

```sql
-- extension_store.py SCHEMA_VERSION 1 → 2

CREATE TABLE IF NOT EXISTS web_message_revisions (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    assistant_message_id  TEXT NOT NULL,    -- references messages.id（跨表外键）
    revision_number       INTEGER NOT NULL, -- 0=原回答，1+=regenerate
    request_id            TEXT,             -- 关联 web_run_requests
    status                TEXT NOT NULL
                          CHECK (status IN (
                              'running', 'completed', 'superseded',
                              'error', 'aborted', 'interrupted'
                          )),
    content_json          TEXT,             -- 完整 AssistantMessage JSON
    is_active             INTEGER NOT NULL DEFAULT 0
                          CHECK (is_active IN (0, 1)),
    created_at            TEXT NOT NULL,
    completed_at          TEXT,
    error_summary         TEXT,

    UNIQUE (assistant_message_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_revisions_session
    ON web_message_revisions(session_id);
CREATE INDEX IF NOT EXISTS idx_revisions_active
    ON web_message_revisions(assistant_message_id, is_active)
    WHERE is_active = 1;
```

### 2.2 外键策略

`assistant_message_id` **跨表引用 `session_sqlite.py` 的 messages.id**——但 SQLite 外键不能跨 ATTACH/不同连接。

**当前 P1-C 架构**：`ExtensionSQLiteStore` 复用 `session_store` 的 connection（共享 `:memory:` 必须；磁盘 DB 也是同一文件）。所以**同库不同表**——可以加 FK。

**D2 选择**：**不加 FK 约束**，用应用层校验：
- 原因：session_store 的 messages 表创建在前，extension_store 的 revisions 表创建在后；如果加 FK 依赖顺序敏感
- 应用层校验：`finalize_revision` 时先 `SELECT FROM messages WHERE id=?` 确认存在
- 加 INDEX (`assistant_message_id`, `is_active`) 加速查询

### 2.3 revision_number 语义

| revision_number | 含义 | 创建时机 |
|---|---|---|
| 0 | 原回答（普通 prompt 产生的第一个 assistant） | 首次 regenerate 时 INSERT（如果不存在） |
| 1+ | regenerate candidate | 每次 regenerate 启动 |

**revision 0 的延迟创建**：
- 普通 prompt **不**创建 revision row——避免无 regenerate 时浪费
- 首次 regenerate 时：
  1. 检查 `WHERE assistant_message_id=? AND revision_number=0` 是否存在
  2. 不存在 → INSERT revision 0 with `content_json=旧回答, status='superseded', is_active=0`
  3. INSERT revision N+1 with `status='running', is_active=0`
- 这样 revision 0 永远代表"原始回答"，被 supersede 后仍可查询/恢复

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
 │                    │  ┌── begin txn ────────────────────┐  │
 │                    ├─ │ ensure revision 0 (旧回答)      │  │
 │                    │  │ INSERT revision N+1 (running)    │  │
 │                    │  └── commit ───────────────────────┘  │
 │                    │                    │                    │
 │                    ├─ _execute_prompt ──────────────────────▶│
 │                    │   (truncate to preceding user) │   │
 │                    │                    │                    │
 │   WS stream ◀──────┤  draft 增长（前端 chatStore）         │
 │   (revision_id)    │                    │                    │
 │                    │                    │                    │
 │                    │◀─ candidate assistant ─────────────────│
 │                    │                    │                    │
 │                    │  ┌── BEGIN IMMEDIATE ─────────────┐   │
 │                    │  │ 1. SELECT revision.status       │   │
 │                    │  │    (verify still 'running')     │   │
 │                    │  │ 2. SELECT rival active revision │   │
 │                    │  │    (verify no winner)           │   │
 │                    │  │ 3. UPDATE 旧 active →superseded │   │
 │                    │  │ 4. UPDATE 新 revision →         │   │
 │                    │  │    completed+is_active=1+content│   │
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
| `GET` | `/api/sessions/{sid}/messages/{mid}/revisions` | 列出该 assistant 的所有 revision |
| `GET` | `/api/sessions/{sid}/messages/{mid}/revisions/{rid}` | 查看某 revision 详情 |
| `POST` | `/api/sessions/{sid}/messages/{mid}/revisions/{rid}/activate` | 切换 active revision（可选，P1-D2.1） |

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
GET /api/sessions/sess_abc/messages/msg_a1/revisions
```

```json
{
  "count": 3,
  "revisions": [
    {
      "id": "rev_001",
      "revision_number": 0,
      "status": "superseded",
      "is_active": false,
      "created_at": "2026-07-14T10:00:00Z",
      "completed_at": "2026-07-14T10:00:05Z"
    },
    {
      "id": "rev_002",
      "revision_number": 1,
      "status": "superseded",
      "is_active": false,
      "created_at": "2026-07-14T10:05:00Z",
      "completed_at": "2026-07-14T10:05:04Z",
      "error_summary": null
    },
    {
      "id": "rev_003",
      "revision_number": 2,
      "status": "completed",
      "is_active": true,
      "created_at": "2026-07-14T10:10:00Z",
      "completed_at": "2026-07-14T10:10:06Z"
    }
  ]
}
```

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
    """加 web_message_revisions 表 + indexes。

    幂等——用 CREATE TABLE IF NOT EXISTS。
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
            status                TEXT NOT NULL
                                  CHECK (status IN (
                                      'running', 'completed', 'superseded',
                                      'error', 'aborted', 'interrupted'
                                  )),
            content_json          TEXT,
            is_active             INTEGER NOT NULL DEFAULT 0
                                  CHECK (is_active IN (0, 1)),
            created_at            TEXT NOT NULL,
            completed_at          TEXT,
            error_summary         TEXT,
            UNIQUE (assistant_message_id, revision_number)
        );
        CREATE INDEX IF NOT EXISTS idx_revisions_session
            ON web_message_revisions(session_id);
        CREATE INDEX IF NOT EXISTS idx_revisions_active
            ON web_message_revisions(assistant_message_id, is_active)
            WHERE is_active = 1;
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
| **2** | feat(extension-store): web_message_revisions schema + migration v1→v2 | DDL + migrate + sweep_interrupted | schema 单元测试 + migration 测试 |
| **3** | feat(extension-store): revision CRUD（create/finalize/list/sweep） | 数据访问层 | CRUD 单元测试 |
| **4** | refactor(web/app): split _execute_prompt + _persist_normal/regeneration | Q7 拆分（不引入新功能） | 现有 prompt 测试不回归 |
| **5** | feat(web): POST /regenerate + GET /revisions endpoints | API 层 | endpoint 集成测试 |
| **6** | feat(web): revision_finalized WS event + 前端 revision badge | 前端 | E2E |
| **7** | test(e2e): regenerate flow + abort + restart recovery | E2E | 7-10 个 E2E 用例 |
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
| **revision count 限制** | 假设单 assistant revision < 20；过多时手动 prune | P2 |
| **revision metadata（temperature / model）** | 当前每个 prompt 不带这些参数 | P2 provider routing |
| **collaborative editing** | 多用户场景 | 不做（localhost only） |
| **revision export** | 已有 session-level export（D1） | 后续可选 |
| **multimodal regenerate**（图片 regenerate） | P0 明确不支持图片理解 | 不做 |

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

## 9. 等待用户批准

### 用户需要确认的决策点

1. **revision 0 延迟创建** vs **每次 prompt 都创建 revision 0**
   - 推荐：延迟创建（节省空间，普通 prompt 不写 revision 表）
   - 替代：每次都写（revision 表更完整，但写放大）

2. **流式期间是否写 revision.content_json**
   - 推荐：不写（简化，server restart 时 revision 仍 running → interrupted）
   - 替代：周期 flush（断点续传，但增加 DB 写）

3. **revision count 限制**
   - 推荐：D2 不限制（先观察）
   - 替代：硬限制 20（超过删除最老的 superseded）

4. **是否实现 active revision 切换**（P1-D2.1）
   - 推荐：D2 只做 regenerate（不可切换历史 revision），D2.1 再加切换
   - 替代：D2 直接做切换（增加 ~30% 工作量）

### 进入编码的 gating

提交本设计文档后，用户审核：
- [ ] 八个问题回答是否完整、准确
- [ ] revision schema 是否符合预期
- [ ] commit 顺序是否合理
- [ ] 决策点 1-4 的推荐选项是否接受

**审核通过后**，按 §6 commit 顺序进入实现阶段。

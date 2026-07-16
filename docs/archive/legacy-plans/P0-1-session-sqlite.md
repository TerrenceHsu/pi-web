# P0-1：Session 线性化（sqlite）

> **Archived**: this document is retained for historical reference and is no longer the source of truth.
> 当前真源见 [STATUS.md](../../../STATUS.md) / [CHANGELOG.md](../../../CHANGELOG.md)。

---

> **目标**：在 v0.0.21 baseline 之上，新增 sqlite-based 会话管理；Web App 切换为多会话模型。
>
> **约束**：零回归。现有 `session.py` / `session_sync.py` / `compaction/branch_summary.py` 暂不删，留到 P2 清理。
>
> **依赖**：无（P0 第一项）
>
> **下游**：P0-2（文件上传）/ P0-4（前端会话列表）依赖本任务的 `SessionDB`。

---

## 一、设计原则

### 1.1 不动现有抽象

`SessionMemory` / `SessionState` / `SessionStore` 接口保持不变。`AgentHarness` 内部对 session 的操作（attach / detach / save / load）一行不改。

理由：
- `session_sync.py` / `BranchSummary` 在 `harness.py` 和现有测试中有深度依赖，删除成本高
- Web Claude 不用 fork / branch_summary，但保留它们不阻碍新功能
- 删除工作后置到 P2（独立 PR）

### 1.2 新增 sqlite 实现

- 新增 `SqliteSessionStore(SessionStore)`：作为现有抽象的 sqlite 实现
- 新增 `SessionDB`：会话**列表 / 创建 / 删除 / 重命名**管理（不属于 SessionStore 抽象，因为旧抽象只有 save/load/exists）

### 1.3 Web 多会话模型

Web App 启动时初始化一个 `SessionDB`。前端列表展示所有 session；点击切换时后端 `detach + attach`。

---

## 二、Schema

### 2.1 表结构

```sql
-- 启用外键 + WAL（并发读写友好）
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 会话元信息
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,                  -- sess-{ts}-{suffix}
    title           TEXT,                              -- 可空
    created_at      INTEGER NOT NULL,                  -- 毫秒时间戳
    updated_at      INTEGER NOT NULL,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    metadata_json   TEXT NOT NULL DEFAULT '{}'        -- JSON
);

-- 单条消息（拆出，便于增量 append + 截断查询）
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,                  -- uuid
    session_id      TEXT NOT NULL,
    idx             INTEGER NOT NULL,                  -- 顺序（0,1,2...）
    kind            TEXT NOT NULL,                     -- 'user' | 'assistant' | 'toolResult' | 'summary'
    content_json    TEXT NOT NULL,                     -- 完整序列化（含 role 字段）
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_idx ON messages(session_id, idx);

-- Snapshot（每轮不可变快照）
CREATE TABLE IF NOT EXISTS snapshots (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    snapshot_json   TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snapshots_session_idx ON snapshots(session_id, idx);

-- Compaction 历史记录（保留旧设计；branch_summaries 也存这里，后续清理）
CREATE TABLE IF NOT EXISTS compactions (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    kind            TEXT NOT NULL,                     -- 'compaction' | 'branch_summary'
    result_json     TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_compactions_session_idx ON compactions(session_id, idx);
```

### 2.2 序列化策略

复用现有 `serialize_message` / `deserialize_message` / `TurnSnapshot.to_dict` / `TurnSnapshot.model_validate`：

- `messages.content_json` = `json.dumps(serialize_message(msg))`
- `snapshots.snapshot_json` = `json.dumps(snapshot.to_dict())`
- `compactions.result_json` = `json.dumps(result.to_dict())`

读取反向。

---

## 三、新增模块

### 3.1 `src/pi_agent_core_py/web/session_db.py`

```python
"""SessionDB —— sqlite-backed 多会话管理。

职责：
  - 会话元信息 CRUD（list / create / delete / rename）
  - 单会话加载/保存（对接 SessionMemory / SessionStore 抽象）
  - 进程级单例（FastAPI app 启动时创建一次）

不是 SessionStore 的替代，是它的外层"管理面板"。
"""

class SessionSummary(BaseModel):
    id: str
    title: str | None
    created_at: int
    updated_at: int
    turn_count: int
    message_count: int                          # 当前 messages 表里该 session 的行数

class SessionDB:
    def __init__(self, db_path: str | Path): ...
    # —— 会话元信息 CRUD ——
    def new_session(self, *, title: str | None = None) -> SessionMemory: ...
    def list_sessions(self) -> list[SessionSummary]: ...
    def delete_session(self, session_id: str) -> None: ...
    def rename_session(self, session_id: str, title: str) -> None: ...
    def get_summary(self, session_id: str) -> SessionSummary | None: ...
    # —— 单会话读写（实现 SessionStore 接口）——
    async def save(self, session: SessionMemory) -> None: ...
    async def load(self, session_id: str) -> SessionMemory: ...
    async def exists(self, session_id: str) -> bool: ...
```

### 3.2 `SqliteSessionStore` 适配器（可选）

```python
class SqliteSessionStore(SessionStore):
    """让 harness.configure_session_store() 能接受 SessionDB。"""
    def __init__(self, db: SessionDB): self._db = db
    async def save(self, session): await self._db.save(session)
    async def load(self, session_id): return await self._db.load(session_id)
    async def exists(self, session_id): return await self._db.exists(session_id)
```

---

## 四、save() 覆盖策略

当前 `SessionMemory` 的 `messages` / `snapshots` 字段是整体列表，每次 snapshot 后用 `messages_after` 覆盖。`SessionDB.save()` 实现策略：

```text
save(session):
    1. UPSERT sessions 行（id, title, created_at, updated_at, turn_count, metadata_json）
    2. DELETE FROM messages WHERE session_id = ?
    3. INSERT 一批 messages（idx 0..N）
    4. DELETE FROM snapshots WHERE session_id = ?
    5. INSERT 一批 snapshots
    6. DELETE FROM compactions WHERE session_id = ?
    7. INSERT 一批 compactions
    整个流程包一个事务
```

简单粗暴但正确。每 turn 一次 save 的写放大可接受（单用户场景）。

---

## 五、Web App 集成

### 5.1 启动初始化

`web/app.py::create_app(harness, *, db_path=None)`：

```python
def create_app(harness, *, db_path: str | Path | None = None) -> FastAPI:
    db_path = db_path or "./data/sessions.db"
    session_db = SessionDB(db_path)
    # 把 SessionDB 包成 SessionStore 接给 harness
    harness.configure_session_store(SqliteSessionStore(session_db))
    state = WebAppState(harness=harness, session_db=session_db, ...)
```

### 5.2 新增 API

```text
GET    /api/sessions                       → list[SessionSummary]
POST   /api/sessions                       {title?} → SessionSummary
GET    /api/sessions/{id}                  → SessionSummary | 404
PATCH  /api/sessions/{id}                  {title} → SessionSummary
DELETE /api/sessions/{id}                  → 204
POST   /api/sessions/{id}/activate         → 切换 harness 当前 session
```

激活逻辑：
```python
async def activate(session_id):
    # 1. 保存当前 session（如有）
    if harness.session is not None:
        await harness.session_store.save(harness.session)
    # 2. 加载目标
    target = await session_db.load(session_id)
    # 3. detach 旧的，attach 新的
    harness.detach_session()
    harness.attach_session(target, restore_messages=True)
```

### 5.3 启动时默认会话

首次启动若 `sessions` 表为空，自动 new_session 一个 default 会话并激活。

---

## 六、文件改动清单

### 新增

| 文件 | 内容 |
|---|---|
| `src/pi_agent_core_py/web/session_db.py` | SessionDB + SqliteSessionStore + SessionSummary |
| `tests/test_step_web_session_db.py` | SessionDB CRUD + 并发 + 序列化往返 |
| `tests/test_step_web_session_api.py` | `/api/sessions/*` endpoint |

### 修改

| 文件 | 改动 |
|---|---|
| `src/pi_agent_core_py/web/app.py` | `create_app` 多接 `db_path` 参数；新增 5 个 session endpoint；启动时初始化 default 会话 |
| `src/pi_agent_core_py/web/state.py` | `WebAppState` 加 `session_db: Any` 字段 |
| `src/pi_agent_core_py/web/serializers.py` | 新增 `serialize_session_summary(s: SessionSummary)` |

### 不改

- `session.py` / `session_sync.py` / `compaction/*` 全部不动
- `harness.py` 不动（通过 `configure_session_store(SqliteSessionStore(...))` 注入）
- 现有测试零回归

### 暂不删（P2 再处理）

- `compaction/branch_summary.py`
- `session_sync.py`
- `SessionMemory.append_branch_summary` / `get_branch_summaries` / `clear_branch_summaries`
- harness 里的 `create_branch_summary` / `last_branch_summary`

---

## 七、依赖

`pyproject.toml` 已有 `aiosqlite`？需检查；没有则加：

```toml
dependencies = [
    # ...existing
    "aiosqlite>=0.20",
]
```

如果不想引入新依赖，可用 stdlib `sqlite3` + `asyncio.to_thread` 包一层。**建议 aiosqlite**——更干净的 async 实现。

---

## 八、验收清单

### 8.1 单元测试（必须通过）

- [ ] `SessionDB.new_session` 创建后能 `list_sessions` 看到
- [ ] `save(session)` 后 `load(id)` 往返一致（messages + snapshots + compactions）
- [ ] `delete_session` 后 messages / snapshots / compactions 级联删除
- [ ] `rename_session` 不动 messages
- [ ] session_id 不存在时 `load` 抛 `FileNotFoundError`
- [ ] 并发：两个 `save` 同时跑不损坏数据（WAL 模式 + 事务）
- [ ] 非法 session_id（含 `..` / `/`）拒绝

### 8.2 API 测试

- [ ] `GET /api/sessions` 返回 list
- [ ] `POST /api/sessions` 创建并返回 summary
- [ ] `PATCH /api/sessions/{id}` 改 title
- [ ] `DELETE /api/sessions/{id}` 返回 204；再 GET 该 id 返回 404
- [ ] `POST /api/sessions/{id}/activate` 切换 harness.session
- [ ] 启动空 db 自动创建 default 会话

### 8.3 回归测试

```bash
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow"
```

必须保持 481 passed。

### 8.4 手动验收

- [ ] 启动 web app，调用 `GET /api/sessions` 看到 default 会话
- [ ] 创建第二个会话，切换激活
- [ ] 关闭进程再启动，会话仍在

---

## 九、里程碑 M1

完成后达到 **M1：后端会话线性化**——可以创建 / 列出 / 切换 / 删除 / 重命名会话，每个会话独立持久化。

下一项：**P0-2 文件上传**（依赖 SessionDB 的 `session_id` 作为文件桶键）。

---

## 十、风险

1. **sqlite 写并发**：单写者，多读者（WAL）。Web Claude 单用户场景足够；如未来多用户，迁 postgres。
2. **保存频率**：当前 harness 每个 turn 后 save——sqlite 每秒数十次写不是问题。如有性能瓶颈再加节流。
3. **harness 单实例**：切换会话需要 detach+attach，期间正在跑的 prompt 会失败。前端"切换会话"按钮禁用条件：`state.running == True`。
4. **aiosqlite 引入**：检查 `pyproject.toml` 是否已有；无则添加。

---

## 十一、估时

| 子任务 | 估时 |
|---|---|
| schema + SessionDB 骨架 | 2h |
| save/load 完整实现 + 序列化往返 | 3h |
| Web API 5 个 endpoint | 1.5h |
| 测试（单元 + API） | 3h |
| 集成调试 + 默认会话初始化 | 1h |
| **合计** | **~10.5h**（1.5 工作日） |

---

## 十二、变更日志

| 日期 | 改动 |
|---|---|
| 2026-07-05 | 初稿。基于"零回归"原则，决定保留旧 session 模块，新增 sqlite 实现 |

# Persistence and Startup

> SQLite 持久化层架构——sessions / messages / snapshots / extension schema / startup restore。
>
> 范围：`src/pi_agent_core_py/session_sqlite.py` + `web/extension_store.py` + `web/app.py::lifespan`。

## 1. 数据库布局

**单一 SQLite 文件 + 单一 connection**——所有表共享同一 connection（`:memory:` 模式必须共享）。

| 表 | 模块 | 用途 |
|---|---|---|
| `sessions` | `session_sqlite.py` | session 元数据（id / title / created_at / updated_at） |
| `messages` | `session_sqlite.py` | messages（按 idx 排序）+ content_json |
| `snapshots` | `session_sqlite.py` | TurnSnapshot 历史 |
| `web_extension_schema_meta` | `web/extension_store.py` | schema version 单例（id=1 CHECK） |
| `web_uploaded_skills` | `web/extension_store.py` | 上传 Skill + canonical skill_json + raw Markdown |
| `web_mcp_servers` | `web/extension_store.py` | MCP server config（不含 env value） |
| `web_mcp_disabled_tools` | `web/extension_store.py` | MCP disabled tool（结构化 key + FK CASCADE） |
| `web_message_revisions` | `web/extension_store.py` | Regenerate 历史（v2 schema） |

## 2. Sessions / Messages / Snapshots

### `sessions`
```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created_at INTEGER NOT NULL,   -- ms timestamp
  updated_at INTEGER NOT NULL
);
```

### `messages`
```sql
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE(session_id, idx),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

**关键不变量**：
- `(session_id, idx)` UNIQUE——防并发插入同 idx
- `content_json` 是序列化的 `AgentMessage`（Pydantic v2 `.model_dump_json()`）
- `created_at` INTEGER（ms）——不是 ISO 字符串（区别于 revision 表的 TEXT）

**diff-based replace_messages**（D2-1）：
- 共同前缀 [0, min(old, new))：role+content 同 → 不动；role 同 content 异 → UPDATE content_json（id + created_at 保）；role 异 → first_mismatch
- [first_mismatch, old_count) DELETE；[first_mismatch, new_count) INSERT
- try/except ROLLBACK 整个 transaction

→ 历史 message ID 稳定（regenerate / next-prompt 都依赖此稳定性）

### `snapshots`
```sql
CREATE TABLE snapshots (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
```

每轮 `harness.last_snapshot` 在 `_persist_normal_prompt_result` 时 append。

## 3. Extension Schema Versioning

`web_extension_schema_meta`：
```sql
CREATE TABLE web_extension_schema_meta (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  version INTEGER NOT NULL
);
```

`SCHEMA_VERSION = 2`（D2-2 升级）。

`extension_store.init()` 分支逻辑：
- `version IS NULL` → `_initialize_fresh_v2_schema`
- `version = 1` → `_migrate_v1_to_v2`
- `version = 2` → `_validate_v2_schema`（只读校验，不静默重建）
- `version > 2` → **fail-before-DDL**（拒绝继续，避免破坏未知未来版本）

**所有 DDL 在 `BEGIN IMMEDIATE` 单 transaction 内**——失败 ROLLBACK，不留半损坏状态。

## 4. Uploaded Skills 持久化

```sql
CREATE TABLE web_uploaded_skills (
  name TEXT PRIMARY KEY,
  skill_json TEXT NOT NULL,
  raw_markdown TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  source_kind TEXT NOT NULL CHECK(source_kind IN ('upload')),
  content_sha256 TEXT NOT NULL,
  last_restore_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

**Skill 来源可信边界**：
- 删除权限以 **DB row 为准**——`web_uploaded_skills` 存在 + `source_kind=upload` 才能删
- **不信任** Markdown 自声明的 source（用户可控字段）
- Restore 时服务端**强制覆盖** `metadata["source_kind"]="upload"` + `["persisted"]=True`
- filesystem / built-in Skill 不可删除（403）

**一致性策略**：
- Upload：DB prepare → registry register → DB commit → DB 失败 unregister 回滚
- Enable/Disable：记录原 status → runtime 修改 → DB 写入 → DB 失败恢复原 status
- Delete：保存原 Skill → unregister → DB delete → DB 失败 re-register

## 5. MCP Server 持久化

```sql
CREATE TABLE web_mcp_servers (
  name TEXT PRIMARY KEY,
  transport TEXT NOT NULL CHECK(transport IN ('stdio')),
  command TEXT NOT NULL,
  args_json TEXT NOT NULL,
  desired_enabled INTEGER NOT NULL CHECK(desired_enabled IN (0,1)),
  env_keys_json TEXT NOT NULL,    -- 只 key names
  last_restore_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE web_mcp_disabled_tools (
  server_name TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  disabled_at TEXT NOT NULL,
  PRIMARY KEY(server_name, tool_name),
  FOREIGN KEY (server_name) REFERENCES web_mcp_servers(name) ON DELETE CASCADE
);
```

### `desired_enabled` vs `attached`

| 字段 | 语义 | 持久化 |
|---|---|---|
| `enabled` | 兼容字段 = `desired_enabled` | ✅ |
| `desired_enabled` | 用户期望启用 | ✅ |
| `attached` | 当前进程连接状态 | ❌（runtime 派生） |
| `restore_status` | not_requested / attached / needs_env / error | ❌ |

允许：`{desired_enabled: true, attached: false, restore_status: "error"}`——持久化意图 vs runtime 状态分离。

## 6. env Secret 安全模型

**核心原则**：env value **永不持久化**。

| 阶段 | 处理 |
|---|---|
| API request | env values 只保留在当前进程内存 |
| 持久化 | 只存 `env_keys`（`list[str]`） |
| 重启恢复 | 从 `os.environ[key]` 读 value |
| 缺失 env | `restore_status=needs_env` + `missing_env_keys` + 不 attach |
| API response | 只返回 `env_keys`——绝不返回 value |

**安全扫描范围**：SQLite 主文件 / WAL / SHM / API response / `body.innerText` / logs 均无 env value。

## 7. Startup Restore 顺序

`lifespan` startup：

```
1. session_store.init()（建表 / migration）
2. extension_store.init()（建表 / migration v1→v2 / 校验）
3. mark_running_revisions_interrupted()  ← D2-6 接入
   - 单 UPDATE WHERE status='running' → 'interrupted'
   - 返回受影响行数（lifespan 不强求 >0，失败=raise RuntimeError）
4. restore Skills（逐行 sha256 + model_validate + metadata 强制覆盖）
5. restore MCP servers（逐行 decode + _resolve_mcp_env + 10s timeout attach）
6. yield（app ready）
```

**关键顺序**：sweep **必须**在 restore MCP 之前——否则残留 running revision 会阻塞新 regenerate（409 active conflict）。

**Sweep 失败 = 启动失败**——raise RuntimeError，不吞错；避免半损坏状态导致后续 regenerate 永久 409。

## 8. 失败隔离

| 场景 | 行为 |
|---|---|
| 损坏 skill_json | 记录 `last_restore_error` + 跳过该 Skill |
| sha256 不匹配 | 同上 |
| Skill name conflict | filesystem/built-in 优先 + 记录 conflict error |
| 损坏 MCP args_json | 记录 `last_restore_error` + 跳过该 server |
| MCP attach 失败 | `desired_enabled=true` + `restore_status=error` + 不阻塞其他 |
| MCP attach timeout | 同上 + `"restore timeout"` |
| Missing env | `restore_status=needs_env` + 不视为系统异常 |

**逐行隔离 decode**——`list_rows` + `decode_uploaded_skill` / `decode_mcp_server` 单行失败不阻塞其他行。

## 9. Shutdown 顺序

```
1. shutting_down = true（拒绝新 prompt）
2. abort active requests + grace timeout
3. harness.detach_mcp_servers()（清理 MCP transport）
4. remove event hook + 通知 WS/SSE
5. extension_store.close()（injected connection 不 close）
6. session_store.close()（关闭共享 connection）
```

**禁止**：shutdown 时改 `desired_enabled` / 删 server config / 删 uploaded Skills / 清 disabled rows——意图数据必须跨重启保留。

## 10. Schema Migration

D2-2 实现 v1 → v2 migration：
- v1：无 `web_message_revisions` 表
- v2：新表 + 6 status / partial unique active / 4 索引

**Migration 单 transaction**：
```sql
BEGIN IMMEDIATE;
CREATE TABLE web_message_revisions (...);
CREATE INDEX uq_request ...;
CREATE INDEX uq_running ...;
CREATE INDEX uq_active ... WHERE status='completed';
CREATE INDEX idx_history ...;
UPDATE web_extension_schema_meta SET version=2 WHERE id=1;
COMMIT;
```

事务内重校验 version——防并发启动竞争。UPDATE rowcount=1 校验。

## 11. 相关文档

- [Runtime and Harness](runtime-and-harness.md)
- [Web Request Lifecycle](web-request-lifecycle.md)
- [Regenerate Revision Model](regenerate-revision-model.md)
- [Archived P1-C Persistence Architecture](../archive/superseded-designs/P1_C_PERSISTENCE_ARCHITECTURE.md)

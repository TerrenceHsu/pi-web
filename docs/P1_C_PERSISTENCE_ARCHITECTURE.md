# P1-C Extension Persistence Architecture

> **阶段**: P1-C（MCP / Skill 配置持久化）
> **Tag**: `v0.0.25-extension-persistence`

## 1. 持久化范围

### 持久化（SQLite 同一 db_path）
- 用户上传 Skill 定义 + canonical skill_json + raw Markdown + enabled 状态
- MCP server 非秘密配置（name / transport / command / args）
- MCP server desired_enabled 状态
- MCP tool disabled 状态（结构化 key: server_name + raw_tool_name）

### 不持久化
- filesystem / built-in / MCP prompt Skill 副本
- Use-this-turn skill_names 选择
- env **value**（只持久化 env_keys）
- active request / request registry（仍为内存态）
- WebSocket client / 正在执行的 turn

## 2. SQLite Schema

复用 session_store 同一 db_path + 同一 connection（`:memory:` 模式必须共享）。

```sql
-- Schema version（单例 id=1 CHECK）
web_extension_schema_meta (id INTEGER PK CHECK(id=1), version INTEGER)

-- 上传 Skill
web_uploaded_skills (
    name TEXT PK, skill_json TEXT, raw_markdown TEXT,
    enabled INTEGER CHECK(enabled IN (0,1)),
    source_kind TEXT CHECK(source_kind IN ('upload')),
    content_sha256 TEXT, last_restore_error TEXT,
    created_at TEXT, updated_at TEXT
)

-- MCP server config（不含 env value）
web_mcp_servers (
    name TEXT PK, transport TEXT CHECK(transport IN ('stdio')),
    command TEXT, args_json TEXT,
    desired_enabled INTEGER CHECK(desired_enabled IN (0,1)),
    env_keys_json TEXT,  -- 只 key names
    last_restore_error TEXT,
    created_at TEXT, updated_at TEXT
)

-- MCP disabled tools（结构化 key + FK CASCADE）
web_mcp_disabled_tools (
    server_name TEXT, tool_name TEXT, disabled_at TEXT,
    PK(server_name, tool_name),
    FK(server_name) REFERENCES web_mcp_servers(name) ON DELETE CASCADE
)
```

## 3. desired_enabled vs attached

| 字段 | 语义 | 持久化 |
|---|---|---|
| `enabled` | 兼容字段 = `desired_enabled` | ✅ |
| `desired_enabled` | 用户期望启用 | ✅ |
| `attached` | 当前进程连接状态 | ❌（runtime 派生）|
| `restore_status` | not_requested / attached / needs_env / error | ❌ |
| `missing_env_keys` | 缺失 env key names | ❌ |

允许：`{enabled: true, desired_enabled: true, attached: false, restore_status: "error"}`

## 4. env Secret 策略

- **只持久化 env_keys**（`list[str]`）——绝不持久化 value
- 提交配置时：API request 的 env values 只保留在当前进程内存
- 重启恢复时：从 `os.environ[key]` 读取 value
- 缺失 env → `restore_status: "needs_env"` + `missing_env_keys: ["KEY"]` + 不 attach
- API response 只返回 `env_keys`——绝不返回 value
- 安全扫描：SQLite 主文件 / WAL / SHM / API response / body.innerText / logs 均无 env value

## 5. Skill 来源可信边界

- 删除权限以 **DB row 为准**（`web_uploaded_skills` 存在 + `source_kind=upload`）
- **不信任** Markdown 自声明的 source（用户可控）
- 恢复时服务端**强制覆盖** `metadata["source_kind"]="upload"` + `["persisted"]=True`
- filesystem / built-in Skill 不可删除（403）

## 6. Restore 生命周期

### Skill restore
1. 读取 `web_uploaded_skills` 原始 rows
2. 逐行 `decode_uploaded_skill`（隔离损坏 JSON）
3. 校验 `sha256(raw_markdown)` == `content_sha256`
4. `Skill.model_validate(skill_json)`
5. 服务端覆盖 source metadata
6. 检查 name conflict（filesystem/built-in 优先）
7. register + apply enabled
8. 成功清除 `last_restore_error`

### MCP server restore
1. 读取 `web_mcp_servers` 原始 rows
2. 逐行 `decode_mcp_server`（隔离损坏）
3. 写入 `WebAppState.mcp_server_configs`
4. `desired_enabled=true` → `_resolve_mcp_env(env_keys)` 从 `os.environ` 读 value
5. missing env → `restore_status=needs_env` + 不 attach
6. env 完整 → `asyncio.wait_for(attach, timeout=10s)`
7. attach 成功 → apply disabled tools（读 disabled rows → unregister 对应 tool）
8. 失败隔离：单 server 损坏 / timeout / attach 失败不阻塞其他

## 7. 失败隔离

| 场景 | 行为 |
|---|---|
| 损坏 skill_json | 记录 `last_restore_error` + 跳过该 Skill |
| sha256 不匹配 | 记录 `last_restore_error` + 跳过 |
| Skill name conflict | filesystem/built-in 优先 + 记录 conflict error |
| 损坏 MCP args_json | 记录 `last_restore_error` + 跳过该 server |
| MCP attach 失败 | `desired_enabled=true` + `restore_status=error` + 不阻塞其他 |
| MCP timeout | 同上 + `"restore timeout"` |
| Missing env | `restore_status=needs_env` + 不视为系统异常 |

## 8. 一致性策略

| 操作 | 顺序 |
|---|---|
| Upload Skill | DB prepare → registry register → DB commit → DB 失败 unregister 回滚 |
| Enable/Disable Skill | 记录原 status → runtime 修改 → DB 写入 → DB 失败恢复原 status |
| Delete Skill | 保存原 Skill → unregister → DB delete → DB 失败 re-register |
| Add MCP server | 校验 → lock → runtime config → DB upsert → DB 失败移除 runtime |
| Enable server | persist desired=true → attach → 成功 attached=true / 失败 attached=false |
| Disable server | persist desired=false → detach → DB 失败不 detach |
| Disable tool | 保存原 MCPAgentTool → unregister → DB insert → DB 失败 re-register |
| Delete server | detach → DB delete + cascade → DB 失败恢复 runtime |

## 9. Shutdown 顺序

1. `shutting_down=True`（拒绝新 prompt）
2. abort active requests + grace timeout
3. `harness.detach_mcp_servers()`（清理 MCP transport）
4. remove event hook + 通知 WS/SSE
5. `extension_store.close()`（injected connection 不 close）
6. `session_store.close()`（关闭共享 connection）

**禁止**：shutdown 时改 desired_enabled / 删 server config / 删 uploaded Skills / 清 disabled rows

## 10. 限制

- **Request registry 仍是内存态**——server 重启后 active request 丢失
- **不支持多 session 并行执行**——single harness
- **localhost-only / no auth**——不适合公网部署
- **env value 必须通过 os.environ 提供**——不能放进 args / command

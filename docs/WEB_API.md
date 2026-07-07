# Web API Reference

> Web Claude P0 MVP（2026-07-07）。
> 详见 [`docs/RELEASE_NOTES_WEB_CLAUDE_P0.md`](RELEASE_NOTES_WEB_CLAUDE_P0.md) 与 [`WEB_CLAUDE_PLAN.md`](../WEB_CLAUDE_PLAN.md)。
>
> ⚠️ Web app **仅 localhost 使用**——无鉴权 / 无多用户隔离 / 无 rate limit；
> **Web Claude P0 MVP is complete for local development. Localhost-first, no authentication, not suitable for public exposure.**

## 设计要点

- 所有响应都是 JSON（除 SSE / 静态文件 / 文件下载）
- 错误统一用 FastAPI `HTTPException`；4xx 客户端可读，5xx 在 `state.last_error` 留详情
- 独占式操作（`POST /api/prompt` / `/api/reset`）会先调 `_ensure_idle`，harness 不 idle 时返回 **409**
- 实时事件：`GET /api/stream`（SSE）和 `WS /ws/events`（WebSocket）—— 后者推荐
- `?include_prompt=true` 默认 **403**；需要 `create_app(allow_prompt_preview=True)` + localhost
- **`POST /api/prompt` 是同步阻塞接口**——LLM 调用结束才返回响应；当前没有 `/api/prompt/async`；前端通过 WS `/ws/events` 展示实时事件，但 prompt 请求本身仍等待后端完成

---

## Sessions（P0-1 sqlite 多会话）

### `GET /api/sessions`

session 列表（spec 复数路径），按 `updated_at` desc 排序。

**Response 200**:

```json
{
  "count": 2,
  "sessions": [
    {
      "id": "sess-...",
      "title": "default",
      "created_at": 1783260347184,
      "updated_at": 1783260347184,
      "metadata": {},
      "is_current": true
    }
  ]
}
```

### `POST /api/sessions`

创建新 session。

**Body**:

```json
{"title": "new chat", "metadata": {}}
```

`title` 可选，缺省 `"default"`。

**Response 200**:

```json
{
  "id": "sess-...",
  "title": "new chat",
  "created_at": ...,
  "updated_at": ...,
  "metadata": {}
}
```

**Response 503**: session store 未初始化（`create_app(db_path=)` 没设）。

### `PATCH /api/sessions/{sid}`

重命名 session。

**Body**: `{"title": "renamed"}`（`title` 必填）

**Response 200**: 同 POST 返回结构。

**Response 400**: title 为空。
**Response 404**: session 不存在。
**Response 503**: session store 未初始化。

### `DELETE /api/sessions/{sid}`

删除 session。**P0-2 起会先删 `uploads/{sid}/` 再删 sqlite session**，避免孤儿目录。

**Response 200**:

```json
{"ok": true, "deleted_files": 3}
```

`deleted_files` 是从 VirtualFileStore 清掉的文件数（file_store 未启用时为 0）。

**Response 404**: session 不存在。

### `GET /api/session`

旧单数 endpoint，向后兼容。返回当前 attached session。

**Response 200**（已 attach）：

```json
{"attached": true, "session_id": "...", "title": "...", "messages": [...], ...}
```

### `GET /api/messages`

Agent 当前 messages。**P0-1 起支持 `?session_id=`**：

- 不传 `session_id` → 返回当前 agent.state.messages（fallback 旧路径）
- 传 `session_id` → 从 sqlite 读该 session 历史 messages（按 idx 升序，强类型对象）

**Query**: `session_id: string | null`

**Response 200**:

```json
{
  "count": 2,
  "session_id": "sess-...",
  "messages": [{"role": "user", "content": [...], ...}, ...]
}
```

**Response 404**: 指定 session 不存在。
**Response 503**: session store 未初始化。

---

## Files（P0-2 VirtualFileStore）

会话级文件上传，强 session 隔离。

### `POST /api/sessions/{sid}/files`

上传一个或多个文件到指定 session。

**Content-Type**: `multipart/form-data`，字段名 `files` 可重复。

**约束**：
- 单文件 ≤ 25 MB（默认；`create_app(max_file_size=)` 可调）
- 单 session 总量 ≤ 100 MB（默认；`create_app(max_session_upload_size=)` 可调）
- 文件名安全性检查（防 `..` / 路径穿越 / 控制字符）

**Response 200**（至少一个成功）:

```json
{
  "count": 2,
  "files": [
    {
      "id": "f-...",
      "session_id": "sess-...",
      "name": "doc.md",
      "size": 1234,
      "mime": "text/markdown",
      "sha256": "abc...",
      "path": "uploads/sess-.../f-.../doc.md",
      "created_at": ...
    }
  ],
  "errors": []
}
```

**Response 207**（部分成功，多文件时）: 同结构，`errors` 非空。

**Response 400 / 413**（全部失败）:

```json
{
  "count": 0,
  "files": [],
  "errors": [
    {"filename": "big.bin", "error_type": "FileTooLargeError", "error": "..."}
  ]
}
```

**Response 404**: session 不存在。
**Response 503**: file store 未初始化（`create_app(uploads_dir=)` 没设）。

### `GET /api/sessions/{sid}/files`

列出 session 内所有文件 metadata。

**Response 200**: `{"count": N, "files": [FileRef, ...]}`

### `GET /api/sessions/{sid}/files/{fid}`

下载单个文件。**强校验 fid 属于该 sid**——跨 session 访问返回 403。

**Response 200**: `FileResponse`（含 Content-Type / filename）。
**Response 403**: fid 属于其它 session。
**Response 404**: session 或 file 不存在。

### `DELETE /api/sessions/{sid}/files/{fid}`

删除 session 内单文件。

**Response 200**: `{"deleted": true, "file_id": "..."}`

### `GET /api/files/{fid}`（兼容入口）

必须 query 参数 `session_id`。

**Query**: `session_id: string`（必填）

**Response**: 同 `GET /api/sessions/{sid}/files/{fid}`。
**Response 400**: 缺 `session_id`。

### `DELETE /api/files/{fid}`（兼容入口）

必须 query 参数 `session_id`。转发到 `DELETE /api/sessions/{sid}/files/{fid}`。

### 文件格式说明（重要）

`view_file` 工具按文件名 + mime 分类：

| 格式 | 状态 |
|---|---|
| markdown（`.md`） | ✅ 支持（frontmatter + 正文） |
| html（`.html` / `.htm`） | ✅ 支持（HTMLParser 抽文本，去 script/style） |
| csv（`.csv` / `.tsv`） | ✅ 支持（csv.Sniffer 推断 delimiter） |
| parquet（`.parquet`） | ✅ 支持（pyarrow 读 schema / row_count / 前 N 行） |
| 纯文本（`.txt` / `.json` / `.log` 等） | ✅ 支持（大文本截断到 max_bytes） |
| **图片**（png / jpg / gif / webp / ...） | ❌ **不支持图片理解**（不做 OCR / 不做视觉理解）；以 `format=image_unsupported` 注入，由 view_file 返回明确"不支持" |
| **PDF**（`.pdf`） | ❌ **正文不解析**；仅作为 FileChip 占位，不抽取文本 |
| 二进制 / 未知 | ❌ `format=binary` / `format=unsupported` |

附件统一注入为 `FileBlock`（不发全文 / 不发 path / 不发 base64）；provider 拿到的是元信息 + 引导调用 `view_file` 的文本说明。

---

## Prompt

### `POST /api/prompt`

同步触发一次 prompt 请求。**独占**——并发请求返回 409。

**Body**（P0 全字段）:

```json
{
  "text": "hello",
  "session_id": "sess-...",
  "file_ids": ["f-..."],
  "skill_names": ["coding_review"],
  "skill_selection": {
    "names": ["coding_review"],
    "tags": ["coding"],
    "values": {"language": "python"}
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | ✅ | 用户输入（trim 后非空） |
| `session_id` | ❌ | 显式指定 session；不传则用 `state.current_session_id` |
| `file_ids` | ❌ | 附件 file_id 列表；后端校验每个 fid 属于该 session（跨 session → 403） |
| `skill_names` | ❌ | 顶层 skill name 列表；与 `skill_selection.names` 合并去重 |
| `skill_selection` | ❌ | Step 14 引入的 skill 选择结构；保留向后兼容 |

**重要**：
- `skill_names` 与 `skill_selection.names` 都会被 web 层预校验是否在 registry 中；**unknown skill 返回 400（不再 500）**
- `file_ids` 会被后端校验 session ownership；附件统一注入 `UserMessage.content` 为 `FileBlock`
- 图片以 `format=image_unsupported` 注入（**不新增 ImageBlock**）
- `session_id` 路径会先 list_messages 加载历史 → run_prompt / run_continue → replace_messages 覆盖回 sqlite + append_snapshot

**Response 200**:

```json
{
  "ok": true,
  "session_id": "sess-...",
  "messages": [...],
  "attachments": {
    "attached_file_ids": ["f-..."],
    "attached_file_names": ["doc.md"],
    "attached_file_count": 1,
    "attached_supported_file_count": 1,
    "attached_unsupported_file_count": 0
  },
  "applied_skill_names": ["coding_review"]
}
```

**Response 400**:
- `text` 为空
- `skill_names` 非 list / 含空字符串 / 含 unknown skill
- `file_ids` 非 list / 含非字符串
- 文件工具未启用但传了 `file_ids`

**Response 403**: `file_ids` 跨 session。
**Response 404**: `session_id` 或某个 `file_id` 不存在。
**Response 409**: harness 不 idle。
**Response 503**: file store 未初始化但传了 `file_ids`。
**Response 500**: harness / agent 异常（`state.last_error` 留详情）。

> ⚠️ **当前实现是同步阻塞**——LLM 调用结束才返回响应。慢 LLM 会让 HTTP 请求挂住。`/api/prompt/async`（异步 + job-id 轮询）**尚未实现**，spec 标记为 P1+。前端通过 `WS /ws/events` 实时展示 streaming events，但 POST 请求本身仍等待后端 run_prompt 返回。

### `POST /api/abort`

中止当前 in-flight prompt。

**Body**（可选）：`{"reason": "user clicked stop"}`

**Response 200**: `{"ok": true}`

### `POST /api/reset`

重置 agent state + 可选清空 events / snapshots / audit。

**Body**:

```json
{"clear_events": true, "clear_snapshots": false, "clear_audit": false}
```

**Response 200**:

```json
{"ok": true, "cleared": {"events": true, "snapshots": false, "audit": false}}
```

**Response 409**: agent 正在 running。

---

## Skills（P0-4 Step 1）

### `GET /api/skills`

attached skills 列表。

**Query**:

- `include_prompt: bool = false` —— 是否在每条 skill 中包含完整 prompt 模板

**保护**：`include_prompt=true` 时：

- `create_app(allow_prompt_preview=False)`（默认）→ **403**
- `allow_prompt_preview=True` 但请求非 localhost → **403**
- 两者都满足才返回 200

**Response 200**:

```json
{
  "attached": true,
  "skills": [
    {
      "name": "coding_review",
      "description": "...",
      "status": "enabled",
      "priority": 20,
      "tags": ["coding"],
      "tool_names": ["read_file"],
      "metadata": {}
    }
  ],
  "skill_loader": {"file_skills": {"count": 1, "names": ["coding_review"]}}
}
```

> 注：`status` 字段是 `"enabled"` / `"disabled"` 字符串（不是 boolean）；前端 `SkillList` 据此渲染 toggle。

### `POST /api/skills/upload`

上传一个或多个 SKILL.md 文件并注册到 `harness.skill_registry`。

**Content-Type**: `multipart/form-data`，字段名 `files` 可重复。

**约束**：
- 单文件 ≤ 256 KB（`_SKILL_UPLOAD_MAX_BYTES`）
- 必须 utf-8 编码
- frontmatter YAML 必须能解析；`name` 字段必填
- 重名（registry 已有同名 skill）→ 409

**Response 200**（至少一个成功）:

```json
{
  "count": 1,
  "skills": [SkillSummary, ...],
  "errors": []
}
```

**Response 207**（部分成功）: 同结构，`errors` 非空。

**Response 400**（全部失败 / 格式错）:

```json
{
  "count": 0,
  "skills": [],
  "errors": [
    {
      "filename": "bad.md",
      "error_type": "SkillFileFormatError",
      "error": "frontmatter YAML parse failed: ..."
    }
  ]
}
```

**Response 409**（重名）:

```json
{
  "count": 0,
  "skills": [],
  "errors": [
    {
      "filename": "dup.md",
      "skill_name": "coding_review",
      "error_type": "SkillRegistrationError",
      "error": "skill 'coding_review' already registered",
      "status": 409
    }
  ]
}
```

**Response 422**: `harness.skill_registry is None`（必须先 `attach_skills([])`）。

> P0 不持久化上传的 skill——重启 server 即丢失。

### `GET /api/skills/{name}`

单条 skill 详情。

**Query**: `include_prompt: bool = false`（同 list 的保护）

**Response 200**: `SkillDetail`（结构与 list 中的项一致；`include_prompt=true` 时多 `prompt` 字段）。
**Response 403**: include_prompt 保护。
**Response 404**: skill 不存在。

### `POST /api/skills/{name}/enable`

启用 skill。

**Response 200**: `{"ok": true, "name": "...", "status": "enabled"}`
**Response 404**: skill 不存在。
**Response 422**: registry 未 attach。

### `POST /api/skills/{name}/disable`

禁用 skill。

**Response 200**: `{"ok": true, "name": "...", "status": "disabled"}`
**Response 404**: skill 不存在。

---

## MCP（P0-4 Step 2）

### `GET /api/mcp`

MCP servers / tools / prompts 全部状态（**旧 endpoint，向后兼容**）。

**Response 200**:

```json
{
  "attached": true,
  "servers": [{"name": "fs", "connected": true, "tool_count": 3, "last_error": null}],
  "tools": [{"name": "mcp__fs__read", "server": "fs", "mcp_tool": "read", "description": "..."}],
  "prompts": [{"server": "fs", "name": "summarize", "description": "..."}]
}
```

### `GET /api/mcp/tools`

仅返回 MCP tools 列表。**P0-4 Step 2 起每条 tool 多一个 `enabled` 字段**：

- `enabled = (tool_name not in disabled_mcp_tools set) AND (tool 当前在 agent.tools 中)`

**Response 200**:

```json
{
  "attached": true,
  "tools": [
    {
      "name": "mcp__fake__echo",
      "server": "fake",
      "mcp_tool": "echo",
      "description": "...",
      "enabled": true
    }
  ],
  "count": 1
}
```

### `GET /api/mcp/servers`

列出所有用户添加的 MCP server 配置。

**关键**：**不返回 env values**，只返回 `env_keys`（sorted）。

**Response 200**:

```json
{
  "count": 2,
  "servers": [
    {
      "name": "fake",
      "command": "python",
      "args": ["fixtures/fake_mcp_stdio_server.py"],
      "enabled": true,
      "last_error": null,
      "tool_count": 1,
      "env_keys": ["API_KEY"]
    }
  ]
}
```

### `POST /api/mcp/servers`

添加 MCP server 配置。

**Body**:

```json
{
  "name": "fake",
  "command": "python",
  "args": ["fixtures/fake_mcp_stdio_server.py"],
  "env": {"API_KEY": "secret_value"},
  "enabled": false
}
```

校验规则：

| 字段 | 规则 |
|---|---|
| `name` | 必填；匹配 `^[A-Za-z0-9_-]+$` |
| `command` | 必填；非空字符串 |
| `args` | list[str]（非 list / 元素非 str → 400） |
| `env` | dict[str, str]（value 必须 str；非 str → 400） |
| `enabled` | 可选 bool，默认 `false` |

**Response 200**: `MCPServerSummary`（同 GET 项）。

**Response 400**: 校验失败。
**Response 409**: name 已存在。
**Response 502**（enabled=true + attach 失败）: body 仍是 `MCPServerSummary`，含 `last_error` 字段——config 已保存但 attach 失败。

> **env values 不回显**：response 类型本身只有 `env_keys`，**没有 `env` 字段**。env value 只在 create request 时被服务端接收，之后保存在 `state.mcp_server_configs[name].env`（内存）；任何 GET endpoint 都不会返回 value。P0 不持久化（重启即丢）。

### `POST /api/mcp/servers/{name}/test`

测试连接，**不污染 harness 当前已启用 server**。临时 connect → initialize → list_tools → close。

**Response 200**（成功）:

```json
{
  "ok": true,
  "server": "fake",
  "tools": [{"name": "echo", "description": "...", "input_schema": {...}}],
  "tool_count": 1
}
```

**Response 502**（失败）:

```json
{"ok": false, "server": "fake", "error": "FileNotFoundError: this_command_does_not_exist"}
```

**Response 404**: server 不存在。

> test 不写 state（不更新 last_error / tool_count）。

### `POST /api/mcp/servers/{name}/enable`

启用 server：cfg.enabled=true → `_refresh_enabled_mcp_servers()` 全量 attach。

**Response 200**: `MCPServerSummary`（含最新 `tool_count`）。
**Response 404**: server 不存在。
**Response 502**: attach 失败（cfg.last_error 写入）。

### `POST /api/mcp/servers/{name}/disable`

禁用 server：cfg.enabled=false → `_refresh_enabled_mcp_servers()` 全量 attach（其余 enabled server 仍保留）。

**Response 200**: `MCPServerSummary`（`tool_count: 0`, `last_error: null`）。

### `DELETE /api/mcp/servers/{name}`

删除 server 配置。如果 enabled 会先 disable 释放 transport。

**Response 200**:

```json
{
  "deleted": true,
  "name": "fake",
  "cleaned_disabled_tools": ["mcp__fake__echo"]
}
```

`cleaned_disabled_tools` 是从 `state.disabled_mcp_tools` 清理掉的孤儿项（前缀 `mcp__{name}__`）。

### `POST /api/mcp/tools/{tool_name}/enable`

启用单个 MCP tool。

**Path**: `tool_name`（**URL encode 安全**；含 `__` 不影响）—— 必须是 MCP tool 全名 `mcp__{server}__{tool}`。

**Response 200**: `{"tool_name": "mcp__fake__echo", "enabled": true}`

实现：从 `disabled_mcp_tools` 移除 → 从 registry 找回 `MCPAgentTool` → `agent.tools.register(target)`。

**Response 400**: `tool_name` 不是 `mcp__server__tool` 格式。
**Response 404**: server 未配置 / tool 不在 registry。
**Response 409**: server disabled（先 enable server）。

### `POST /api/mcp/tools/{tool_name}/disable`

禁用单个 MCP tool。**真实生效**——从 `agent.tools` unregister，LLM 不再看到。

**Response 200**: `{"tool_name": "mcp__fake__echo", "enabled": false}`

实现：加入 `disabled_mcp_tools` set → `agent.tools.unregister(tool_name)`。

**幂等**：重复 disable 仍 200。

> server disable 后再 enable，disabled tool 设置仍生效（关键回归点）—— harness attach 后会重新应用 disabled 过滤。

---

## State 查询

### `GET /api/state`

```json
{
  "running": false,
  "last_error": null,
  "agent_status": "idle",
  "queue_size": 0,
  "turn_count": 0,
  "message_count": 0,
  "snapshot_count": 0,
  "event_count": 0
}
```

### `GET /api/snapshots` / `GET /api/snapshots/{index}`

snapshot 列表 / 单条详情。`index` 0-based；负数从尾部。

### `GET /api/events`

`TraceEventBuffer` 内容（最近 N 个事件，N 默认 1000）。

### `POST /api/events/clear`

清空 event buffer。`{"ok": true, "count": 0}`

---

## Policy Audit

### `GET /api/policy/audit`

权限策略审计记录。

**Query**: `limit: int = 100`（最大 10000）。

**Response 200**:

```json
{
  "policy_name": "default",
  "count": 5,
  "records": [
    {
      "timestamp": 1783260347184,
      "tool_call_id": "t1",
      "tool_name": "write_file",
      "decision": "deny",
      "policy_name": "default",
      "reason": "path outside workspace_roots",
      "metadata": {"path": "/etc/passwd", "category": "write"}
    }
  ]
}
```

---

## 实时事件流

### `GET /api/stream`（SSE）

Server-Sent Events，默认无限流。每 15s 发 heartbeat 注释行。

**Query**:

- `limit: int | null` —— 测试 / 调试用。发完 N 个真实 event 后正常关闭；`limit=0` 立即关闭（hello 后）。

**事件类型**：

- `event: hello` —— 连接建立时第一个事件，含 `agent_status`
- `event: event` —— 业务事件（AgentEvent 序列化 + `_received_at_ms`）
- `event: shutdown` —— 服务端关闭通知
- `: heartbeat` —— 注释行

**响应头**:

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

### `WS /ws/events`（WebSocket）

**spec 推荐通道**。前端 P0-4 默认用 WS。

每连接一个 `asyncio.Queue(maxsize=100)`；on_event hook 广播到所有连接；慢客户端 queue 满时丢弃该 event，不阻塞其它客户端 / 主 loop。

**消息格式**（server → client）：

- 连接建立后立即发：`{"type": "hello", "agent_status": "idle", "_received_at_ms": ...}`
- 后续广播：`{...AgentEvent..., "_received_at_ms": ...}`

**client → server**：可不发；或发任意 keepalive 字符串（服务端不解析）。

> P0 不实现 event_id 去重 / 重连补播（v0.0.22 已知限制）。前端在 WS 断连时用 `GET /api/messages` 兜底。

---

## 静态文件

### `GET /`

`web/static/index.html`（前端 build 后）或 fallback HTML（提示 `npm install && npm run build`）。

### `GET /assets/{path}`

前端 build 后的 chunk 文件。**路径校验**：禁止 `..` 逃逸。

---

## 错误码速查

| 状态码 | 触发场景 |
|--------|---------|
| 200 | 正常响应 |
| 207 | Skills / Files 多文件上传部分成功（Multi-Status） |
| 400 | 请求体不合法（`text` 为空 / skill_names 非 list / args 不是 list[str] / env value 非 str / tool_name 非 mcp__ 全名 等） |
| 403 | `include_prompt=true` 但未启用 / 非 localhost；`file_ids` 跨 session；unsafe filename |
| 404 | session / snapshot / asset / skill / mcp server / mcp tool 不存在 |
| 409 | harness 不 idle（已有 prompt 在跑）；MCP server 重名；MCP server disabled 时 enable tool |
| 413 | 单文件超 25MB / session 总量超 100MB |
| 422 | `skill_registry` 未 attach 时调 skills upload/enable/disable |
| 500 | harness / agent 异常（`state.last_error` 留详情） |
| 502 | MCP test connection 失败 / server enable 时 attach 失败 / server create 时 enabled=true 但 attach 失败 |
| 503 | session store / file store 未初始化时调对应 endpoint |

---

## 已知限制（P0 MVP）

- `POST /api/prompt` 仍是**同步阻塞**——慢 LLM 会让 HTTP 请求挂住；`/api/prompt/async` 未实现
- **无鉴权 / 无多用户 / 无 rate limit**——仅 localhost 使用
- **MCP / Skill 配置不持久化**——重启即丢
- **不支持图片理解**（不做 OCR / 不做视觉理解）
- **PDF 正文不解析**
- WebSocket 慢客户端采用"丢弃事件"策略——可能漏事件；前端需自己补拉
- `view_file` 大文本自动截断到 `max_bytes`（默认 8KB）

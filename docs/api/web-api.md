# Web API Reference

> 当前基线：P1-D2 Regenerate FROZEN @ `d53f331`（2026-07-16）。
> 已发布最新 tag：`v0.0.26-export-markdown`（P1-D1 Export Markdown）。
> 详见 [CHANGELOG](../../CHANGELOG.md) / [STATUS](../../STATUS.md)。
> P0 MVP 的 release notes 见 [v0.0.23-web-claude-p0-mvp](../releases/v0.0.23-web-claude-p0-mvp.md)；
> P0 改造计划（已归档）见 [archived Web Claude plan](../archive/legacy-plans/WEB_CLAUDE_PLAN_ORIGINAL.md)。
>
> ⚠️ Web app **仅 localhost 使用**——当前已有本地账号登录、Cookie 网关与账号工作区隔离，但无 TLS / RBAC / OAuth / 公网部署加固；
> **Localhost-first, authenticated local workspaces, not suitable for public exposure.**

## 可选 Data Analysis（2026-09-05）

扩展工具目录还提供 `run_python_analysis`，独立选择并探测专用本机解释器。此工具仅从聊天调用：
每次由服务创建 `policy_name=python_execution` 的一次性确认，完整 `code` 与来源 hash 不被通用
512 字符预览截断。已有 `/api/requests/{request_id}/approvals` 查询/决议接口负责确认；浏览器不能
修改待执行参数。分析历史新增 `awaiting_approval` 状态，列表含 `action`。
Python 成功结果补充 `code`、`code_sha256`、`stdout`；失败可包含 `python_error`。
固定分析 POST 不接受 Python；Python 历史 Retry 返回 409 `python_retry_requires_new_chat_approval`。
详情见 [本地 Python 分析与信任边界](../design/python-data-analysis.md)。

Workspace 扩展 `GET/PUT /api/workspaces/{sid}/extensions` 新增 `tools` 目录和
`selected_tool_names`；PUT 可带 `tool_names: ["analyze_data"]`，省略则保留原选择，空数组关闭。
默认关闭；缺失 `data-analysis` 可选依赖时不可新启用。MCP/Skill 选择仍原子保留原有协议。

以下 API 全部要求当前登录账号拥有该 Session，run ID 还必须属于同一 Session：

| 方法与路径（前缀 `/api/workspaces/{sid}/analysis`） | 行为 |
| --- | --- |
| GET `/` | 最多 20 条任务摘要 |
| POST `/` | 接收 `AnalysisRequest`，202 返回 queued/running 任务；不写 Workspace |
| GET `/{run}` | 状态、原参数及完成结果；用于轮询/刷新恢复 |
| POST `/{run}/cancel` | 取消排队或运行中的任务，终止工作进程 |
| POST `/{run}/retry` | 仅失败/取消/中断记录可新建一次任务，返回新 run ID |
| POST `/{run}/save` | 用户显式保存；单事务追加分析产物，重复请求幂等 |
| GET `/{run}/chart` | 校验 Session、产物 hash 后返回 PNG，private/no-store |
| DELETE `/{run}` | 取消并清除该条历史/私有预览；已保存 Workspace 文件保留 |

列表/创建的实际路径不带尾部 `/`。创建参数示例：

```json
{"file_id":"file-...","action":"aggregate","group_by":["region"],"metrics":[{"column":"sales","operation":"sum"}],"limit":100}
```

动作是 `inspect/profile/aggregate/timeseries/chart`，只接受当前 Workspace 的 file ID，不接受本机
任意路径、URL、Python/SQL。完整契约见 `agent_workspace/analysis.py` 与 OpenAPI schema。
状态为 `queued/running/succeeded/failed/cancelled/interrupted`；安全错误码不含文件正文或宿主路径。
开启检查、资源上限或冲突返回 409，非法参数返回 422，未知 run 返回 404，无服务返回 503。
成功结果包含 schema、rows、处理/导出/预览数量及截断标志、来源 hash、警告和相对图表地址。
详见[数据分析边界](../design/data-analysis.md)。

## P1 历史阶段摘要

下表概括 P1-A / B / C / D 各阶段相对 P0 MVP 的增量。详细架构见 [docs/architecture/](../architecture/)。

| 阶段 | Tag | API 增量 | 架构文档 |
|---|---|---|---|
| P1-A 真实环境验证 | `v0.0.23.1` | （无新 endpoint，新增 e2e + integration 测试） | — |
| P1-B 异步架构 | `v0.0.24` | `POST /api/prompt/async`；`GET /api/requests/{id}`；`POST /api/requests/{id}/abort`；`GET /api/requests?session_id=&status=active`；WS hello 加 `first/last_available_sequence` / `server_time`；`GET /api/events` envelope + sequence | [web-request-lifecycle](../architecture/web-request-lifecycle.md) |
| P1-C 持久化 | `v0.0.25` | Skills / MCP server / disabled tools 持久化字段（`desired_enabled` / `restore_status` / `missing_env_keys` / `last_restore_error`）；env value 永不返回 | [persistence-and-startup](../architecture/persistence-and-startup.md) |
| P1-D1 Export | `v0.0.26` | `GET /api/sessions/{sid}/export/markdown` | — |
| P1-D2 Regenerate | HEAD（未 tag） | `POST /api/sessions/{sid}/messages/{aid}/regenerate`；`GET /api/sessions/{sid}/messages/{aid}/revisions`；`GET /api/messages` 改用 Web PersistedMessage DTO（含 `message_id` / `session_id` / `idx`） | [regenerate-revision-model](../architecture/regenerate-revision-model.md) |

各 P1 endpoint 的完整规格见下方对应章节。

## 设计要点

- 所有响应都是 JSON（除 SSE / 静态文件 / 文件下载）
- 错误统一用 FastAPI `HTTPException`；4xx 客户端可读，5xx 在 `state.last_error` 留详情
- 独占式操作（`POST /api/prompt` / `/api/reset`）会先调 `_ensure_idle`，harness 不 idle 时返回 **409**
- 实时事件：`GET /api/stream`（SSE）和 `WS /ws/events`（WebSocket）—— 后者推荐
- `?include_prompt=true` 默认 **403**；需要 `create_app(allow_prompt_preview=True)` + localhost
- **`POST /api/prompt` 是同步阻塞接口**——LLM 调用结束才返回响应；当前没有 `/api/prompt/async`；前端通过 WS `/ws/events` 展示实时事件，但 prompt 请求本身仍等待后端完成

---

## Admin Telemetry

三个接口均要求有效登录 Cookie、`X-PI-Agent-UI: 1`、允许的 Origin（浏览器请求）以及服务端
`auth_user.is_admin=true`。普通账号返回 **403**；Recorder 未启用或不可用时返回 **503**。响应均带
`Cache-Control: no-store`。

### `GET /api/admin/telemetry/summary?window_hours=24`

返回请求总量/结果、错误率、平均/P95 延迟、token/cost、工具、Provider、账号和小时趋势。
`window_hours` 范围为 1–720。

### `GET /api/admin/telemetry/spans`

列出最近 spans。支持 `window_hours`、`limit`（1–200）、`status=running|ok|error`、`name`、
`account_id` 和 `session_id`。Admin 前端固定查询 `name=web.request`。

### `GET /api/admin/telemetry/spans/{span_id}`

返回单个 span 的 trace/parent、状态、有界 attributes 和结构化 event timeline。Telemetry 永不保存
Prompt、消息/thinking、Tool arguments/output、凭证、异常正文或 abort reason。

---

## Sessions（SQLite append-only tree + lane）

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
      "active_lane": "main",
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
  "metadata": {},
  "active_lane": "main"
}
```

**Response 503**: session store 未初始化（`create_app(db_path=)` 没设）。

### `GET /api/sessions/{sid}`

读取单个 Session 元数据，返回结构与 Session list item 相同。

### `GET /api/sessions/{sid}/tree`

读取指定 lane 的 root-to-leaf entry path。查询参数 `lane` 缺省为
`active_lane`；`include_all=true` 时额外返回按 append seq 排序的
`all_entries`，其中包含已离开 active path 的旧分支。

每个 entry 暴露 `id`、`seq`、`parent_id`、稳定 `message_id`、强类型
`message`、`label` 与时间戳。每个 lane 暴露 `name`、`leaf_entry_id` 和
`is_active`。

### `POST /api/sessions/{sid}/fork`

```json
{
  "name": "alternate",
  "at_entry_id": "entry-...",
  "source_lane": "main",
  "activate": true
}
```

`at_entry_id` 省略时使用 source lane 当前 leaf；显式 `null` 表示从根创建。
目标必须位于 source lane 当前路径。fork 不移动原 lane。

### `POST /api/sessions/{sid}/branch`

`{"lane":"main","entry_id":"entry-..."}` 把 lane leaf 移回路径中的祖先；
`entry_id: null` 回到根。若操作 active lane，兼容 messages 投影同步重建。

### `PATCH /api/sessions/{sid}/active-lane`

`{"lane":"alternate"}` 切换 active lane，并原子更新 messages 投影。

### `PUT /api/sessions/{sid}/entries/{entry_id}/label`

`{"label":"checkpoint"}` 追加 label fact；`label: null` 清除当前 label。
历史 fact 不会被覆盖或删除。

Tree mutation 在该 Session 有 active request 时返回 409；lane/entry 不存在返回
404；跨 lane path 的 branch/fork 或重名 lane 返回 409。

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

**P1-D2 起**：传 `session_id` 时返回 **Web PersistedMessage DTO**（每条消息含 `message_id` / `session_id` / `idx` / `role` / `content` / `created_at` / `message`）；`message_id` 在 regenerate 期间稳定（同位 UPDATE 而非 DELETE+INSERT）。

**Query**: `session_id: string | null`

### `GET /api/sessions/{sid}/context-budget`

返回当前 Provider 绑定下的确定性上下文预算估算。`estimate` 分别列出 system、
messages、tools、output reserve、projected tokens、context window 与占用比例；
`approximate=true` 表示当前使用安全余量 estimator，而非 Provider 官方 tokenizer。普通 Session 还返回
`workspace_context` 审计投影；Knowledge Conversation 返回 `null`。
普通 Web 还返回 `compaction` 状态；`effective_input_budget` / `effective_ratio` 扣除
输出预留和工具增长余量，实际模型调用逐次再次检查。未知窗口不启用自动压缩。

### `POST /api/sessions/{sid}/context-budget/estimate`

Body 可包含尚未发送的 `text`、`file_ids` 与 `skill_names`，只读估算发送后的预算，
不写入 Session。请求运行期间返回 409。

### `POST /api/sessions/{sid}/context/compact`

```json
{
  "keep_last_n_turns": 4,
  "keep_recent_tokens": 20000
}
```

默认从完整 turn 边界切分；`keep_recent_tokens` 可省略，提供时优先于 turn 数量。
最新 turn 即使超过目标也完整保留，不会留下孤立 tool result。成功响应包含
`token_stats`、`budget_before` 与压缩后的 `budget`，以及压缩/保留 message 计数。
没有可压缩的完整 turn 时返回 409；字段类型或范围非法返回 422。
2026-09-05 起，Web 发布 SQLite 持久工作视图，不改 `/api/messages` 原文、entry ID 或 Memory。
模型生成严格结构化摘要，失败或不节省 token 时 409，旧有效视图不变；具体原因在 `detail.reason`。

Web 详细语义见 [Context Compaction](../design/context-compaction.md)；通用 SDK 的旧接口仍见
[Compaction Semantics](../COMPACTION_SEMANTICS.md)。

### 压缩状态与来源

- GET `/api/sessions/{sid}/context/compaction`：开关、状态、熔断、当前摘要、来源 entry IDs。
- PUT 同路径：仅接受 `{"auto_compact": true|false}`，空闲时持久化到当前 Session。
- GET `/api/sessions/{sid}/context/source/{entry_id}`：`offset` 与 `max_chars` 分页，默认 6,000、
  最多 12,000 字符。只返回当前 Session 活动分支中的文本来源，越界或跨 Session 返回 404。
- 原生只读工具 `read_tool_output(ref, offset=0, max_chars=6000)` 用于完整大工具结果的按需回读；
  不接受 Session、数据库、路径或 SQL 参数。来源与回读正文均为不可信历史材料。

**Response 200**:

```json
{
  "auto_compact": true,
  "status": "idle",
  "active_projection_id": null,
  "covered_message_count": 0,
  "token_stats": null,
  "error_code": null,
  "circuit_open": false,
  "can_auto_compact": false
}
```

**Response 404**: 指定 session 不存在。
**Response 503**: session store 未初始化。

---

## Automatic Session Memory

2026-09-05 更新：普通自动记忆改为结构化增量及 immutable entry 引用，新增
`continuity.status="no_change"`（已处理但不改 Memory 文件）。保留原有失败/延迟/恢复状态。
用户编辑托管条目正文将其固定，旧笔记不被自动覆盖；详见
[`session-history-memory.md`](../design/session-history-memory.md)。

Agent 新增 `search_session_history(query, limit=5, before_seq?)` 与
`read_session_history(entry_id, offset=0, max_chars=6000)` 原生工具，不新增任意 SQL API。
数据库/账号/Session 参数只能由服务端提供；工具参数不接受 `session_id`、`sql` 或物理路径。
结果含 `entry_id`、`role`、`timestamp`、正文片段及 `active / archived_or_superseded` 状态。
搜索最多 20 项、原文单页最多 12,000 字符；跨 Session entry 返回 `history_entry_not_found`。
历史工具默认随文件型 Web Session 装配，知识模式不继承它们；现有压缩 API 不变。

产品启动器默认开启自动 Session Memory；通用嵌入场景需显式设置
`create_app(..., enable_auto_memory=True)`，并同时提供 Session `db_path` 与 `uploads_dir`。

每个成功持久化的普通 Session Prompt，以及成功提交的新 Regenerate active answer，都会生成有界
turn evidence。Backend 以每 Session 串行的 append-only `auto_memory` operation，使用当前绑定的
Provider/Model 累计更新 `Memory.md`。该模型调用不进入 Agent loop，不启用 Tools、Skills 或 MCP，
也不会清空 canonical messages。Knowledge Conversation 使用独立语义，返回 `skipped`。

同步 Prompt 的响应、异步 Prompt 的 `result_summary` 与 Regenerate request 的 `result_summary` 可包含：

```json
{
  "continuity": {
    "status": "updated",
    "operation_id": "op_...",
    "source_sha256": "...",
    "memory_file_id": "file_...",
    "workspace_revision": 4,
    "recovered": false
  }
}
```

`status` 可能为 `updated`、`no_change`、`deferred`、`pending_retry`、`unavailable` 或 `skipped`。自动记忆失败不会
把已经持久化的主回答改为失败；未完成 evidence 在下一轮串行 preflight 先重试，持续失败时作为
不可信历史上下文注入。若进程退出发生在消息提交后、operation intent 落库前，下一轮会从 canonical
最新完整 turn 与最近覆盖 hash 重建 intent。

自动 Coding 请求冻结到 `awaiting_approval` 时，operation 记录 Sandbox blocker 并返回 `deferred`；
这是为了避免更新 `Memory.md` 提升 Workspace revision、使已签名 Artifact 的发布基线失效。Sandbox
发布、拒绝、取消、失败或中断后自动续跑。成功发布会广播 `workspace_changed`，其中
`source=auto_memory`。

### Workspace continuation context

普通 Prompt、Regenerate 与 Context Budget 通过同一 `WorkspaceContextAssembler` 读取稳定 revision 的
Session Workspace。优先内容包括 `AGENT.md`、可选 `tasks/current.md`/`HANDOFF.md`、Pending Memory
evidence、`Memory.md`、current 代码摘要、Workspace 树和最近一个非终态 Sandbox operation。总量和
单文件均有界，不默认展开完整 `scripts/**`。只有 current 代码摘要会进入 Prompt；stale/failed 摘要
要求 Agent 按需读取源码。待批准 Artifact 始终标记为未发布。

`workspace_context` 是 secret-free 审计对象，出现在同步 Prompt 响应、异步/Regenerate 的 request
`result_summary` 和 Context Budget 响应中：

```json
{
  "schema": "pi-agent-workspace-context/v1",
  "workspace_revision": 7,
  "context_sha256": "...",
  "included_sections": ["workspace_manifest", "agent_instructions", "durable_memory"],
  "omitted_sections": [],
  "included_paths": ["AGENT.md", "Memory.md"],
  "total_characters": 4096,
  "truncated": false,
  "code_continuity_status": "current",
  "pending_memory": false,
  "sandbox_status": null
}
```

必需根文件、current 代码文档或其 stat/SHA-256 无法稳定验证时，Prompt 在调用 Provider 前返回
`workspace_context_unavailable`（HTTP 409）；Context Budget 返回 HTTP 409。Knowledge Conversation
和 Checkpointer 不加载此上下文，也不会复用上一请求的 Workspace metadata。

`GET /api/state` 公开：

```json
{
  "auto_memory": {
    "enabled": true,
    "recovery": {"scanned": 0, "completed": 0, "pending": 0, "conflicts": 0}
  },
  "code_continuity": {
    "enabled": true
  }
}
```

---

## Slash Commands

Slash command 是独立的 Session 操作；命令文本不会作为 UserMessage 写入 canonical history。

### `GET /api/slash-commands`

返回 Composer 可展示的命令目录。当前只包含无参数命令 `/checkpointer`。

```json
{
  "count": 1,
  "commands": [
    {
      "name": "/checkpointer",
      "description": "Summarize this conversation to Memory.md, then clear it.",
      "requires_provider": true,
      "accepts_arguments": false
    }
  ]
}
```

### `POST /api/sessions/{sid}/slash-commands`

异步启动 Session slash command。`/checkpointer` 对启动时的 canonical messages 建立不可变快照，使用当前 Session 绑定的 Provider/Model 直接生成累计摘要；该调用不启用 Tools、Skills 或 MCP。

**Request**:

```json
{"command": "/checkpointer"}
```

命令大小写不敏感，但必须是精确的无参数命令；未知命令或附带参数返回 400。

**Response 202**:

```json
{
  "ok": true,
  "command": "/checkpointer",
  "request_id": "req_...",
  "session_id": "sess-...",
  "status": "queued",
  "request_url": "/api/requests/req_...",
  "abort_url": "/api/requests/req_.../abort"
}
```

客户端通过 `GET /api/requests/{request_id}` 轮询终态。成功时 `result_summary` 包含 `memory_file_id`、`memory_logical_path=Memory.md`、`source_message_count`、`source_sha256`、`durable_operation_id` 和 `idempotent_recovery`。

提交语义为“先持久化 operation intent，再发布 `Memory.md`，最后在一个 SQLite 事务内清空原 lane 并完成 operation”。Provider 或文件发布失败不会清空消息；文件已经发布但 SQLite 收尾失败时不反向覆盖文件，operation 保持 open，重试或启动恢复凭 source SHA-256 前滚完成且不重复调用 LLM。恢复还会核对接受 operation 时的 immutable source leaf；leaf 已变化则标记 conflict 并保留所有新消息。

`Memory.md` 更新使用 immutable content generation，`metadata.json` 的原子 replace 是 commit point。应用启动在接受请求前扫描未完成 Checkpointer operation；`GET /api/state` 的 `durable_recovery` 返回本次启动的 `scanned` / `completed` / `aborted` / `conflicts` 计数。

成功仅清空当前 Session 的 canonical messages 与当前 UI 消息流；Session、`AGENT.md`、其它文件和 snapshots 保留。后续 Prompt/Regenerate 通过统一 Workspace continuation context 有界加载 `Memory.md`，并将其标记为不可信历史事实而非行为指令。

**Response 400**: `invalid_command` / `unknown_slash_command` / `slash_command_arguments_not_supported`。

**Response 404**: Session 不存在。

**Response 409**: 当前工作区忙、该 Session 已有 active request、存在无法自动收敛的其它 durable operation，或没有消息可总结（`nothing_to_checkpoint`）。

**Response 503**: 服务关闭中，或 Session/File store 不可用。

---

## Files（Session WorkspaceStore）

会话级 managed workspace，强 session 隔离。新建 Session 与应用启动时都会幂等确保唯一根
`AGENT.md` 和 `Memory.md`；已有内容不会被覆盖。文件 metadata 只公开逻辑路径，不公开服务器
物理路径。

### `POST /api/sessions/{sid}/files`

上传一个或多个文件到指定 session。

所有新上传的逻辑路径统一为 `upload/<filename>`，根目录首次成功上传时惰性出现；
可选 `relative_folder` 也被约束在 `upload/` 以下。同名路径自动编号，不覆盖已有原件。

**Content-Type**: `multipart/form-data`，字段名 `files` 可重复。

**约束**：
- 单文件 ≤ 25 MB（默认；`create_app(max_file_size=)` 可调）
- 单 session 总量 ≤ 100 MB（默认；`create_app(max_session_upload_size=)` 可调）
- 文件名安全性检查（防 `..` / 路径穿越 / 控制字符）

**Response 200**（至少一个成功）:

```json
{
  "count": 1,
  "files": [
    {
      "id": "f-...",
      "session_id": "sess-...",
      "name": "doc.md",
      "logical_path": "upload/doc.md",
      "origin": "upload",
      "purpose": "input",
      "size": 1234,
      "mime": "text/markdown",
      "sha256": "abc...",
      "created_at": ...,
      "updated_at": ...
    }
  ],
  "conversions": [],
  "errors": [],
  "workspace": {
    "schema_version": 1,
    "session_id": "sess-...",
    "revision": 3,
    "code_continuity": {
      "schema_version": "pi-agent-code-continuity/v1",
      "status": "not_initialized",
      "stale": false,
      "latest_code_workspace_revision": null,
      "summarized_code_workspace_revision": null,
      "summary_workspace_revision": null,
      "code_source_sha256": null,
      "trigger": null,
      "validation_evidence_id": null,
      "error_code": null,
      "updated_at": 1787000000000
    }
  }
}
```

上传代码在 `upload/**` 中作为只读输入，不触发代码摘要。原件复制成 `scripts/**` 的工作代码后，
启用 `enable_code_continuity` 的产品组合从实际工作代码 revision 生成只读 `docs/architecture.md`、
`docs/code-flow.md` 与 `docs/validation.md`。摘要失败不撤销工作代码，`code_continuity` 返回
`failed` 且 `stale=true`，并在启动恢复时重试。没有 Sandbox validation evidence 的代码明确标为
未验证；批准的 Sandbox 发布携带真实验证证据。

`.pdf`、`.docx`、`.xlsx` 原件同样位于 `upload/<filename>`（`purpose=document_original`）。
服务从不可变 Workspace 快照执行固定转换，独立发布到 `documents/<stem>-<source-id>/`；
历史 `documents/<document-id>/original.<ext>` 保持原位并可继续转换。
`conversions` 返回状态、primary/manifest file id、warning 和本次生成的
`document_conversion` 文件。成功产物可包含 `content.md`、`tables/*.csv`、`tables/*.json`、
`assets/*`、`manifest.json`。原件和生成物均只读；扫描 PDF 返回 `needs_ocr`，当前不会自动 OCR。

**Response 200**（部分成功，多文件时）: 同结构，`errors` 非空。

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

`FileRef.logical_path` 使用 `/` 分隔虚拟目录；`origin` 为
`system | upload | agent | user | legacy`，`purpose` 为
`file | agent_instructions | memory | input | handoff | task | workspace_documentation |
document_original | document_conversion`。响应中没有物理
`path`。`Memory.md` 使用 `purpose=memory`。

### `POST /api/sessions/{sid}/documents/{source_file_id}/convert`

对一个 `document_original` 显式重试固定转换。相同 source SHA、converter version、配置版本且
manifest 声明的全部制品仍完整时直接返回 `reused=true`，不提升 Workspace revision；否则从新的
revision 快照转换并以一个事务替换该文档的生成物。

**Response 200**：`source_file_id`、`document_id`、`status`、`reused`、`workspace_revision`、
`primary_file_id`、`manifest_file_id`、`files`、`warnings`、`error_code`。

**Response 409**：转换期间 Workspace 发生并发修改或目标路径/原件不满足权限合同。

**Response 415**：目标不是支持的不可变 Workspace 文档原件。

### `GET /api/sessions/{sid}/files/{fid}`

下载单个文件。**强校验 fid 属于该 sid**——跨 session 访问返回 403。

**Response 200**: `FileResponse`（含 Content-Type / filename）。
**Response 403**: fid 属于其它 session。
**Response 404**: session 或 file 不存在。

### `DELETE /api/sessions/{sid}/files/{fid}`

删除 session 内单文件。

**Response 200**: `{"deleted": true, "file_id": "..."}`

根 `AGENT.md`、`Memory.md`、文档原件和固定转换产物不可通过单文件 API 删除，返回 409。

### `PUT /api/sessions/{sid}/files/{fid}/content`

更新根文件或普通 Workspace Markdown 的 UTF-8 正文；固定文档原件/生成物始终返回 403。保存
结果从该 Session 下一轮 Agent 请求起生效。

**Request**:

```json
{
  "content": "# AGENT.md\n\nAnswer concisely.\n",
  "expected_sha256": "current-file-sha256"
}
```

`expected_sha256` 是必填的乐观并发版本；文件已被其它请求修改时返回 409，前端应重新加载后再保存。

**Response 200**: `{"file": FileRef}`

**Response 403**: 目标不是根 `AGENT.md` 或 `Memory.md`。

**Response 404**: Session 或文件不存在。

**Response 409**: SHA-256 版本冲突。

**Response 413**: 超出文件或 Session 配额。

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
  "intent_mode": "auto",
  "coding_mode": false,
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
| `intent_mode` | ❌ | 产品路由覆盖：`auto`（默认）、`read_only`、`coding`、`knowledge`；`knowledge` 只接受已绑定 Knowledge Conversation |
| `coding_mode` | ❌ | 兼容字段；`true` 等价于显式 `intent_mode=coding`，`false` 仍使用自动路由 |
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
  "applied_skill_names": ["coding_review"],
  "intent": {
    "route": "read_only",
    "confidence": 0.98,
    "source": "rule",
    "reason_code": "read_only_constraint",
    "explicit": false
  },
  "workspace_context": {
    "schema": "pi-agent-workspace-context/v1",
    "workspace_revision": 7,
    "context_sha256": "..."
  }
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

产品入口启用三类路由。优先级为：durable Knowledge Conversation binding → 显式覆盖 →
否定/只读约束 → 问题或方案请求 → 编码动作 → 安全默认 `read_only`。只读路线只向模型暴露
本地读取工具和名称可确定为读取语义的 MCP 工具；Coding 路线只暴露 `coding_*` 工具并进入
Sandbox 创建、验证和 Freeze 流程。`intent` 只含规则审计，不包含 Prompt、文件内容或凭证。

> ℹ️ **同步路径**——LLM 调用结束才返回响应。前端通过 `WS /ws/events` 实时展示 streaming events，POST 请求本身等后端 run_prompt 返回。**异步路径**见下方 `POST /api/prompt/async`（P1-B1）。

---

### `POST /api/prompt/async`（P1-B1）

异步触发 prompt——立即返回 `request_id` + HTTP 202；后台 task 执行 Agent。

请求 Body 与同步 `POST /api/prompt` **完全一致**（包括 `intent_mode` / `coding_mode`）。202 响应会立即返回解析后的 `intent`，终态 request 的 `result_summary.intent` 保留同一审计投影。

**完整乐观校验**：所有 4xx 错误（400 参数 / 404 session 不存在 / 403 跨 session / 400 unknown skill）在校验阶段抛出，**不创建 request record**——调用方立即收到 4xx。

**Response 202**（立即返回）:

```json
{
  "ok": true,
  "request_id": "req_...",
  "session_id": "sess-...",
  "status": "queued",
  "events_url": "/api/events?session_id=sess-...",
  "request_url": "/api/requests/req-...",
  "abort_url": "/api/requests/req-.../abort"
}
```

**Response 503**: server 正在 shutdown（`shutting_down=True`）。

**并发限制**（single harness）：
- 全局单 active request（`_ensure_idle` 检查 state.running + harness.context.phase + agent.state.status）→ 第二个请求 409
- `session_id` 字段保留为未来扩展点；当前 single harness 下 session 级并发检查不会触发（harness busy 检查先 reject）

---

### `GET /api/requests/{request_id}`（P1-B1）

查询单个 request 状态。active + history 都会查。

**Response 200**:

```json
{
  "request_id": "req_...",
  "session_id": "sess-...",
  "status": "running",
  "created_at": "2026-07-12T12:34:56.789Z",
  "started_at": "2026-07-12T12:34:56.790Z",
  "ended_at": null,
  "error": null,
  "error_type": null,
  "abort_reason": null,
  "result_summary": null,
  "event_start_sequence": null,
  "event_end_sequence": null
}
```

`status` ∈ `queued | running | completed | error | aborted`。

`result_summary`（仅 completed）: `{message_count, applied_skill_names, session_id}`——**不含** message 全文 / GLM key / MCP env / system prompt。

`event_start_sequence` / `event_end_sequence` 字段 P1-B1 占位 `null`，P1-B2 起 envelope 改造后填充。

**Response 404**: request 不存在（既不在 active 也不在 history）。

---

### `POST /api/requests/{request_id}/abort`（P1-B1）

abort 单个 request——幂等。

**Body**（可选）: `{"reason": "user_requested"}`

**行为**:
- `queued`: cancel task + 设 status=aborted
- `running`: 调 `harness.abort(reason)`（不 cancel task，让模型 finalize）+ 设 `abort_reason` flag → runner 在 success 路径检查 flag 设 status=aborted
- `completed` / `error` / `aborted`: 幂等返回当前状态

**Response 200**:

```json
{
  "ok": true,
  "request_id": "req_...",
  "status": "aborted",
  "abort_reason": "user_requested"
}
```

**Response 404**: request 不存在。

---

### `POST /api/abort`（P1-B1 兼容别名）

兼容旧前端 Stop 按钮——若存在 active request 则转发到 `_abort_request_internal`；否则直调 `harness.abort()`。

**Body**（可选）: `{"reason": "..."}`

**Response 200**: `{"ok": true, "request_id"?: "req_...", "status"?: "aborted", ...}`（有 active request 时返回详情，否则只返 `{"ok": true}`）

---

## Request History（P1-B1）

- `WebAppState.active_requests: dict[request_id → WebRunRequest]` — 运行中或刚结束
- `WebAppState.active_request_by_session: dict[session_id → request_id]` — session 级 active 映射（单 active per session）
- `WebAppState.request_history: deque[WebRunRequest]`（maxlen=100，可经 `create_app(request_history_maxlen=...)` 覆盖）
- 完成的 request 从 active 移到 history；`_find_request` 同时查两者
- `task` / `payload` 字段仅内存——**不进 JSON response**
- `error` / `error_type` 字段是 `safe_error()` 截断版（≤500 字符），不输出 traceback 全文

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

## Export Markdown（P1-D1）

### `GET /api/sessions/{sid}/export/markdown`

把 session 的所有 user / assistant messages 渲染为 Markdown 文件并下载。

**Response 200**:
- Content-Type: `text/markdown; charset=utf-8`
- Content-Disposition: `attachment; filename="..."`（filename sanitized，RFC 5987 UTF-8 编码非 ASCII）
- Body: rendered Markdown text

**Response 404**: session 不存在。
**Response 413**: 渲染后内容超过 `MAX_EXPORT_CHARS=2_000_000` 或 `MAX_EXPORT_BYTES=5_000_000`。
**Response 503**: session store 未初始化。

**安全过滤**：
- 只导出 `role=user` / `role=assistant`——不导出 tool_result / summary / custom
- **不读** `web_mcp_servers` 表（MCP env value 不会泄露）
- 不导出 system prompt / request_id / snapshot / policy audit
- filename sanitize：阻断 path traversal（`..`）/ 控制字符（`\x00-\x1f`，含 CRLF）/ 限 80 字符

详见 [P1-D1 Validation Report](../validation/p1-d/P1_D1_VALIDATION_REPORT.md)。

---

## Regenerate（P1-D2）

### `POST /api/sessions/{sid}/messages/{aid}/regenerate`

对最新 assistant message 触发 Regenerate——后台异步重生成，**非破坏性**：旧回答在 finalize 前始终可见，finalize 时原子切换。

**Response 202**:
```json
{
  "request_id": "req-...",
  "regeneration_id": "rev-...",
  "target_message_id": "msg-...",
  "session_id": "sess-...",
  "status": "queued"
}
```

**Response 400**: `aid` 不是 assistant message（`code: user_message_expected`）。
**Response 404**: session / message 不存在 / message 不属于该 session（`code: session_not_found` / `message_not_found` / `message_wrong_session`）。
**Response 409**: 任一以下情况：
- `aid` 不是最新 assistant（`code: not_latest_assistant`）
- 缺少 preceding user message（`code: missing_preceding_user`）
- 已有 active request（`code: active_request_conflict`）
- 已有 running revision（`code: revision_conflict`）
- finalize 时 base_content_sha256 不匹配（`code: revision_base_content_changed`）

**关键不变量**：
- `aid`（message_id）在 regenerate 期间**不变**——`messages.content_json` 在 finalize 时 UPDATE（不 INSERT 不 DELETE）
- 流式期间不写 DB——`messages.content_json` 仍是旧回答
- finalize 成功后 messages.content_json 一次性切到新回答（单 `BEGIN IMMEDIATE` transaction 5 步）

详见 [Regenerate Revision Model](../architecture/regenerate-revision-model.md)。

### `GET /api/sessions/{sid}/messages/{aid}/revisions`

列出该 assistant message 的所有 revision（按 `revision_number` DESC）。

**Query**: `before_revision_number: int`（分页游标）/ `limit: int`（默认 20，clamp [1,100]）

**Response 200**:
```json
{
  "count": 3,
  "items": [
    {
      "id": "rev-...",
      "assistant_message_id": "msg-...",
      "revision_number": 2,
      "status": "completed",
      "created_at": "2026-07-16T...",
      "completed_at": "2026-07-16T...",
      "is_current": true
    },
    ...
  ],
  "has_more": false
}
```

**安全 serializer**——**不**返回：
- `content_json`（revision 正文）
- `base_content_sha256`（optimistic concurrency token）
- `request_id`

**Response 404**: session / message 不存在 / message 不属于该 session。

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

> **env values 不回显**：response 类型本身只有 `env_keys`，**没有 `env` 字段**。env value 只在 create request 时被服务端接收，之后保存在 `state.mcp_server_configs[name].env`（内存）；任何 GET endpoint 都不会返回 value。
>
> **P1-C 起持久化**：MCP server 配置（不含 env value）+ desired_enabled + env_keys + disabled tools 持久化到 SQLite；env value 重启时从 `os.environ` 读取。`restore_status` 字段（`not_requested` / `attached` / `needs_env` / `error`）+ `missing_env_keys` 反映启动恢复状态。详见 [Persistence and Startup](../architecture/persistence-and-startup.md)。

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

每连接一个 `asyncio.Queue(maxsize=100)`；`_web_event_hook` 一次性生成 envelope 广播到所有连接；慢客户端 queue 满时丢弃该 event，不阻塞其它客户端 / 主 loop。

**消息格式**（server → client）：

- **连接建立后立即发 hello**（P1-B3 起含 3 字段）：
  ```json
  {
    "type": "hello",
    "agent_status": "idle",
    "first_available_sequence": 12300,
    "last_available_sequence": 12345,
    "server_time": "2026-07-16T..."
  }
  ```
  `first/last_available_sequence` 用于客户端判断是否需要 replay；hello 是控制帧——**不**进 buffer / 不消耗 sequence / 不进 seenEventIds。
- **后续广播 WebEventEnvelope**（P1-B2 起）：
  ```json
  {
    "event_id": "evt-...",
    "request_id": "req-...",
    "session_id": "sess-...",
    "sequence": 12346,
    "type": "message_update",
    "timestamp": "2026-07-16T...",
    "payload": { ...AgentEvent 字段... }
  }
  ```
  全局 sequence 单调（不是 per-session）；客户端通过 `seenEventIds` FIFO 去重 + `lastGlobalSequence` 跟踪。

**client → server**：可不发；或发任意 keepalive 字符串（服务端不解析）。

**Reconnect Replay**（P1-B3）：
- 客户端 WS reconnect 时（`lastGlobalSequence > 0`）触发 replay
- 调 `GET /api/events?after_sequence=N&limit=200`（**不带 session_id**）
- 合并 replay 与 live buffer → sort by sequence → dedupe by event_id
- buffer 不够长（事件已被 deque 淘汰）→ 客户端 fallback `GET /api/messages` 全量同步

> P1-B3 起 **已实现** envelope event_id 去重 + sequence-based reconnect replay。客户端 `seenEventIds` FIFO（1000 上限）+ `lastGlobalSequence` 全局跟踪；reconnect 时 `GET /api/events?after_sequence=N` 拉取 replay；replay 失败时 fallback `GET /api/messages` 全量同步。

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

## 已知限制（HEAD `d53f331`，P1-D2 后）

- **Request registry 内存态**——server 重启后 active request 丢失；已持久化的 final messages 不丢
- **Single harness / single active request**——不支持多 session 并行执行
- **WebSocket 慢客户端**采用"丢弃事件"策略——可能漏事件；前端通过 reconcile + `GET /api/messages` 兜底（P1-B 已实现）
- **完整浏览器 reload 后恢复原 session 依赖 URL routing**——当前 reload 后选第一个 session；WS reconnect recovery 已支持（P1-B3）
- **不支持图片理解**（不做 OCR / 不做视觉理解）
- **PDF 正文不解析**——P1-D3 待实施
- **`view_file` 大文本自动截断到 `max_bytes`**（默认 8KB）
- **Regenerate 仅支持最新 assistant**——不支持历史 message regenerate / 手动切换 revision 为 active
- **无鉴权 / 无多用户 / 无 rate limit**——仅 localhost 使用

# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

> 每个版本的详细验证报告见 [docs/validation/](docs/validation/)；
> release notes 见 [docs/releases/](docs/releases/)。

## [Unreleased]

### Added
- **P1-D2 Regenerate**（已冻结，HEAD `d53f331`）——
  - Diff-based `replace_messages`（历史 message ID + `created_at` 稳定）
  - `web_message_revisions` schema + migration v1→v2（6 status / partial unique active / 4 索引）
  - Revision repository（8 async 方法 + 9 错误类型 + 状态机）
  - Execution/persistence split（`_execute_prompt` / `_persist_normal_prompt_result` / `_persist_regeneration_result` / `_reset_harness_to_session`）
  - `POST /api/sessions/{sid}/messages/{aid}/regenerate` HTTP API + 错误映射
  - `GET /api/sessions/{sid}/messages/{aid}/revisions` 安全 serializer
  - Web PersistedMessage DTO + startup sweep（mark running revisions interrupted）
  - Frontend regenerate flow（in-place update by message_id / draft item / reconnect recovery）
  - 9 个 Regenerate E2E 用例 + backend integration test（next-prompt-after-regenerate sees B not A）
- `test_next_prompt_after_regenerate_sees_b_not_a`——直接观察 `FakeProviderAdapter.all_messages_calls`，验证下一轮 LLM 输入含 B 不含 A

### Security
- revision `error_summary` 截断 500 字符，避免泄露内部信息
- `GET /api/sessions/{sid}/messages/{aid}/revisions` 不返回 `content_json` / `base_content_sha256` / `request_id`

### Validation
- HEAD `d53f331`：1131 offline pytest passed / coverage 84.20% / 37 e2e PASS / ruff clean
- Production hooks: `__storeHooks` 0 / `__e2eHooks` 0 in `web/static/assets/*.js`
- Core runtime diff（`9264267..HEAD`）：0 modifications to loop/agent/context/providers/events/stream_events/mcp/tools/skill_loader
- 详细证据见 [docs/validation/p1-d/P1_D2_VALIDATION_REPORT.md](docs/validation/p1-d/P1_D2_VALIDATION_REPORT.md)

## [v0.0.26-export-markdown] — 2026-07-14

Tag commit: `ebbc896` · P1-D1 Export Markdown

### Added
- `src/pi_agent_core_py/web/markdown_export.py`——纯函数 renderer（`ExportMessage` + `render_session_markdown`）
- `GET /api/sessions/{sid}/export/markdown`——SQLite messages → Markdown 下载
- filename sanitize：path traversal / 控制字符 / 80 字符 / RFC 5987 UTF-8
- 413 大小限制（`MAX_EXPORT_CHARS=2M` / `MAX_EXPORT_BYTES=5MB`）
- 前端 `requestBlob` + `downloadBlob`（try/finally + `setTimeout(0)` 释放 Blob URL）+ SessionSidebar Export 按钮

### Security
- 三道边界：Blob URL try/finally + Content-Disposition CRLF 阻断 + MCP env value 不导出
- 按 role 过滤——不读 `mcp_servers` 表；不扫描用户正文中的 secret 字符串
- system prompt / request_id / toolResult / MCP env 一律不导出

### Validation
- 22 用例专项测试（含审核 4 补测：空 session / 404 双路径 / CRLF 阻断 / active draft 不导出）
- offline pytest: **990 passed** / coverage **90.38%** / ruff clean
- frontend build (e2e): 137.53 KB JS / 39.80 KB CSS
- 详见 [release notes](docs/releases/v0.0.26-export-markdown.md)

## [v0.0.25-extension-persistence] — 2026-07-13

Tag commit: `b4640aa` · P1-C Extension Persistence

### Added
- 上传 Skill 持久化（`web_uploaded_skills` 表 + canonical skill_json + raw Markdown + enabled 状态）
- MCP server 配置持久化（`web_mcp_servers` 表 + desired_enabled）
- MCP disabled tool 持久化（`web_mcp_disabled_tools` 表 + 结构化 key）
- Startup restore 顺序：session store init → extension store init/migrate → sweep running revisions → restore Skills → restore MCP
- 失败隔离：损坏 row / sha256 不匹配 / MCP attach timeout 单点失败不阻塞其他

### Security
- MCP env values **永不持久化**——只存 `env_keys`，从 `os.environ` 恢复
- API response 只返回 `env_keys`——绝不返回 value
- 缺失 env → `restore_status=needs_env` + `missing_env_keys`

### Validation
- 5 个 persistence E2E + 安全扫描（`P1C_SECRET_MARKER_7F3A91`）
- offline pytest: **968 passed** / coverage **83.78%** / e2e **28/28 PASS**
- 详见 [validation report](docs/validation/p1-c/P1_C_VALIDATION_REPORT.md) 与 [archived architecture](docs/archive/superseded-designs/P1_C_PERSISTENCE_ARCHITECTURE.md)

## [v0.0.24-async-architecture] — 2026-07-12

Tag commit: `79cea14` · P1-B Async Architecture（B1 + B2 + B2.1 + B3）

### Added
- **B1 Async Prompt + Request Registry**：`POST /api/prompt/async` → 202 + request_id → 后台 Task；`GET /api/requests/{id}` + `POST /api/requests/{id}/abort`；WebRunRequest lifecycle (queued → running → completed/error/aborted)
- **B2 WebEventEnvelope + Event Dedup**：`_web_event_hook` 一次性生成 envelope（7 字段）；全局 sequence 单调；`GET /api/events` 支持 `?session_id` / `?request_id` / `?after_sequence` / `?limit` + gap 检测
- **B2.1 Hardening**：gap 检测改全局 `lastGlobalSequence`；`seenEventIds` FIFO 淘汰（1000 上限）；chatStore `setActiveSession` 4 切换路径同步
- **B3 Async UI + Reconnect Replay**：前端切 async；`pendingEventsByRequest` 缓冲；`belongsToCurrentRequest` 隔离 turn-control 事件；`replayFromCursor` 合并 live/replay + dedupe；`pollRequestUntilTerminal` + `reconcileMessagesFromServer`；WS hello 加 `first/last_available_sequence` + `server_time`

### Fixed
- P1-B3 调试栈 5 轮定位的 JavaScript microtask/macrotask 时序陷阱——同步 POST finally 不清跨事件状态
- WebSocket 首次连接 bug——`createEventSocket` 末尾补 `connect()` 调用
- assistant 文本重复显示（POST 兜底 + WS event 竞态）

### Security
- `__e2eHooks.closeEventSocket` 仅 E2E build 暴露（`VITE_E2E_HOOKS=true`）
- 生产 build 经 tree-shake 完全剥离 hooks 字面量

### Validation
- 23/23 e2e（含 7 async-stream-reconnect）+ 889 pytest + coverage 84.36%
- 详见 [validation report](docs/validation/p1-b/P1_B3_VALIDATION_REPORT.md)

## [v0.0.23.1-web-claude-validation] — 2026-07-12

Tag commit: `80a2f6f` · P1-A 真实环境验证

### Added
- A1: API smoke 3 项（并发 409 / 重名 SKILL 409 / 不存在命令 502）
- A2: Playwright MCP tool lifecycle 真链路 E2E（add→test→enable→disable tool→enable→disable server→delete）
- A3: 真实 GLM e2e_probe 多轮 tool_use（`PROBE_OK_ALPHA_7F3A` 防幻觉 token）
- `tests/integration/test_real_glm_tool_use.py` + `tests/e2e/mcp-tool-lifecycle.spec.ts`

### Validation
- 12/12 Playwright e2e（含 MCP tool lifecycle 真链路）
- 843 offline pytest / coverage 84.39% / ruff clean
- 真实 GLM 多轮 tool_use smoke 通过
- 详见 [validation report](docs/validation/p1-a/P1_A_VALIDATION_REPORT.md)

## [v0.0.23-web-claude-p0-mvp] — 2026-07-07

Tag commit: `8817c84` · Web Claude P0 MVP（含 4 个真实环境 hotfix）

### Added
- **P0-1 SQLite sessions**：`SQLiteSessionStore`（WAL + 外键级联 + `UNIQUE(session_id, idx)`）；sessions CRUD + `GET /api/messages?session_id=`
- **P0-2 VirtualFileStore**：session 级文件存储；sha256 / mime / 大小限制（单文件 25MB / session 100MB）
- **P0-3 file tools**：`tools/list_files.py` + `tools/view_file.py`（md / html / csv / parquet / 文本；图片 unsupported；PDF 不解析正文）
- **P0-4 Skills API**：upload / enable / disable / GET；unknown skill web 层预校验 400（不再 500）；`include_prompt=true` 默认 403
- **P0-4 MCP API**：servers CRUD + test connection（不污染 harness）+ enable/disable + delete；tools enable/disable 真实 unregister/register
- **P0-5 default system prompt**：`build_default_system_prompt(*, skills, mcp_tools, file_tools_enabled)`
- **前端骨架**（Pinia + 5 store + 7 inline turn cards + Skills/MCP Manager Modal）
- 两栏 UI（SessionSidebar + ChatPanel）——**不做右栏调试 Drawer**

### Fixed (post-freeze hotfixes)
- uvicorn 进程需要 `websockets>=12` 库（TestClient 不暴露此依赖）
- `createEventSocket` 不调 `connect()` 导致 WS 永不连接
- assistant 文本重复显示（POST 兜底 + WS 时序竞态）
- Playwright Smoke 6 时序（默认 enabled 路径 + ESC 关 modal + getByText 精确匹配）

### Security
- **MCP env values 严格不回显**——response 类型只有 `env_keys`；前端 type=password + autocomplete=new-password；列表只渲染 key 名
- Prompt preview 默认禁用（`allow_prompt_preview=False`）
- Localhost only / no auth

### Validation
- 678 offline pytest / 6 e2e / frontend build 128.48 KB JS
- coverage 73.75%（低于 75% 门槛，但 exit code 0——历史 baseline，非 P0 回归）
- 详见 [release notes](docs/releases/v0.0.23-web-claude-p0-mvp.md)

## [v0.0.22-web-backend-stable] — pre-P0 baseline

> **注意**：此版本号来自 [`docs/releases/v0.0.22.md`](docs/releases/v0.0.22.md)，**本副本 git 仓库未保留对应的 tag**。
> 这是 v0.0.21（Step 21 Provider Adapter Refactor）之上对 Web app 层加固后形成的 baseline，作为 P0 MVP 的起点。

### Added
- `GET /api/sessions`（spec 复数路径，兼容 `GET /api/session`）
- `GET /api/mcp/tools`（spec endpoint，兼容 `GET /api/mcp`）
- `WS /ws/events`（spec 推荐的 WebSocket 实时通道，兼容 `GET /api/stream` SSE）
- `GET /api/stream?limit=N`（SSE 测试模式：发完 N 个 event 后正常关闭）
- `event_buffer_max_size` / `allow_prompt_preview` 配置项

### Fixed
- Hook 累积修复（`remove_on_event_hook` + `lifespan` 精确移除）
- `TraceEventBuffer` 上限 1000（之前无界）
- `POST /api/prompt` 已运行时返回 409（不再 500）
- Prompt preview 默认禁用
- WebSocket 慢客户端不阻塞全局（独立 `asyncio.Queue(maxsize=100)`）

### Validation
- 25 web integration tests + 563 offline pytest
- 详见 [release notes](docs/releases/v0.0.22.md)

[v0.0.26-export-markdown]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.26-export-markdown
[v0.0.25-extension-persistence]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.25-extension-persistence
[v0.0.24-async-architecture]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.24-async-architecture
[v0.0.23.1-web-claude-validation]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.23.1-web-claude-validation
[v0.0.23-web-claude-p0-mvp]: https://github.com/earendil-works/pi-agent-core-py/releases/tag/v0.0.23-web-claude-p0-mvp

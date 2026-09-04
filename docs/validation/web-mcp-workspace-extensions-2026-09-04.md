# Web MCP / Skills Workspace 装配

> 日期：2026-09-04
> 产品边界：localhost-only Web Agent；MCP 可连接本机 stdio 或用户配置的 Streamable HTTP endpoint。

## 交付结论

MCP 与 Skills 现在分为两层：账号工作区数据库保存全局目录，Web Workspace（与 durable Session ID 一一对应）只选择目录中当前可用的资源。每次 Agent 请求开始时，`CodingAgentRuntime` 按 Session ID 读取选择，将 SkillRegistry 与 MCP ToolRegistry 过滤并冻结为该请求的资源快照；Session 之间不再共享“全部扩展均可见”的产品语义。

内置 DDGS 继续由 `pi_agent_core_py.mcp.ddgs_server` 提供，启动时规范化为固定、不可删除的全局目录项。它不会在用户未启用时常驻启动子进程；全局启用后，尚未显式保存扩展选择的新 Workspace 默认选中 DDGS，用户仍可在 Workspace 中取消选择。

## Streamable HTTP

- 单 endpoint POST JSON-RPC，`Accept` 同时声明 `application/json` 与 `text/event-stream`。
- 支持普通 JSON response 与逐事件读取的 SSE response，不等待服务端关闭长连接才解析结果。
- 初始化响应中的 `Mcp-Session-Id` 自动用于后续请求，后续请求携带 `MCP-Protocol-Version`。
- 初始化完成后发送 `notifications/initialized`；关闭时在服务端分配 Session 的情况下尽力发送 DELETE。
- 默认使用初始化式 `2025-06-18` 协议版本；全局配置可显式覆盖版本。
- 禁止 URL 内嵌 credentials，禁止用户覆盖 Content-Type、Accept 与 MCP transport 控制头；HTTP redirect 不自动跟随。

HTTP 请求头不把值写入 SQLite。全局配置只保存 `Header-Name -> ENVIRONMENT_VARIABLE` 映射，attach/test 时从 Web 服务进程环境解析。API 与前端只返回 Header 名和环境变量名，不返回运行时值。

## 持久化与 API

Extension SQLite schema 从 v2 原子迁移到 v3：

- `web_mcp_servers` 增加 HTTP URL、header 环境变量引用与协议版本；原 stdio 数据原样保留。
- `web_workspace_extension_selection` 可区分“从未配置”与“显式全部不选”。
- `web_workspace_mcp_selection`、`web_workspace_skill_selection` 保存 Session 级选择。
- Session 删除时显式清除三张选择表；MCP 删除继续通过外键清除对应选择与 disabled-tool 记录。

Web API：

- `GET /api/workspaces/{session_id}/extensions` 返回全局目录、可用状态和该 Workspace 的选择。
- `PUT /api/workspaces/{session_id}/extensions` 原子替换 MCP/Skills 选择；未知、禁用或未 attach 的资源 fail closed。
- `/api/mcp/servers` 接受 `transport=stdio|http`。HTTP 使用 `url`、`header_env` 与可选 `protocol_version`。

## 前端

Workspace 右栏新增 `extensions` 页签，分别列出 MCP servers 与 Skills，可用项支持持久选择。MCP 全局管理表单新增 stdio/Streamable HTTP 切换、endpoint、协议版本和 header 环境变量映射。Skills 管理中的 “Use this turn” 已改为 “Use in Workspace”，Session 切换时恢复持久选择。

## 验证范围

- MCP transport：HTTP 配置校验、JSON/SSE、Session/版本头、initialized notification、DELETE close。
- Store：fresh v3、v1→v2→v3、旧 MCP/disabled tool/revision 数据保持、Workspace 空选择与替换。
- Web：全局 stdio/HTTP 配置、header secret 不回显、两个 Session 选择隔离、不可用资源拒绝、DDGS 默认规则。
- Coding Agent：ResourceLoader 按 Session 选择过滤 Skill 与 MCP Tool，同时保留本地工具。
- Frontend：Workspace Extensions 加载与持久切换，TypeScript、ESLint、Vitest 与 production build。

最终完整门禁：

- Backend：2201 passed / 7 skipped / 9 deselected，coverage 76.63%。
- Frontend：Vitest 29 files / 188 tests，ESLint、vue-tsc、production build 全部 PASS。
- Chromium E2E：20/20 passed，posttest 已恢复 production build。
- 静态与离线评测：Ruff PASS、strict Mypy 263 files / 0 issues、`uv lock --check --offline` PASS、5 suites / 10 observations Evals candidate gate PASS。

# pi-agent-core-py

`@earendil-works/pi-agent-core`（TypeScript 上游项目）的 Python 完整移植版本——一个面向"本地优先、可观察、可回放"的 Agent 开发框架，内置 MCP 工具生态、权限沙箱、Skill 体系、Session 记忆和网页端调试器。

> 上游 TypeScript 项目路径（不在本副本）：`../pi-main/packages/agent`

> 当前进度：Step 1–21 完成（核心 15 步 + Phase A MCP/权限 + Phase B Skill Loader + Phase C Web App + Provider Adapter Refactor）；
> Web Claude P0 MVP + P1-A 真实环境验证 + P1-B 异步架构 + P1-C 持久化 + P1-D1 Export Markdown + P1-D2 Regenerate 全部完成 ✅。
> 当前状态见 [`STATUS.md`](STATUS.md)；未来计划见 [`ROADMAP.md`](ROADMAP.md)；版本历史见 [`CHANGELOG.md`](CHANGELOG.md)。
>
> **Web Claude P0 MVP is complete for local development. Localhost-first, no authentication, not suitable for public exposure.**

---

## 目录

- [设计理念](#设计理念)
- [技术栈](#技术栈)
- [功能模块](#功能模块)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [Web UI](#web-ui)
- [开发流程](#开发流程)
- [路线图](#路线图)

---

## 设计理念

1. **垂直切片**：每个 Step 端到端跑通（demo + 测试 + 文档），不做半成品。
2. **事件驱动**：Agent / Harness / Tool 的所有行为都通过 `AgentEvent` 流式广播，可观察、可序列化、可重放。
3. **不可变语义**：Pydantic v2 模型 + 快照（`TurnSnapshot`），running 期间不会被订阅者 mutate。
4. **本地优先**：Web UI 仅用于本地调试（不实现认证 / 多用户 / 公网部署），MCP / Skill / Policy 都在进程内编排。
5. **Provider 无关**：`ModelClient` 抽象层；当前默认 GLM-5.0（智谱 Anthropic 兼容协议），未来拆 Anthropic / OpenAI / Fake 多 provider。

---

## 技术栈

| 维度 | 选型 | 备注 |
|------|------|------|
| 语言 | Python 3.12 | conda 环境 `pipy`（不强制，>=3.11 也可） |
| 并发 | asyncio + anyio | 全异步事件循环 |
| 数据建模 | Pydantic v2 | 所有 message / event / snapshot 模型 |
| HTTP / SDK | httpx + anthropic + openai | LLM provider SDK |
| Token 计数 | tiktoken | 用于 compaction 估算 |
| Schema 校验 | jsonschema | 工具参数 JSON Schema 校验 |
| 配置 | python-dotenv + pyyaml | `.env` 凭证 + Skill YAML frontmatter |
| 测试 | pytest + pytest-asyncio | `tests/`，`slow` mark 跳过真实 API |
| Lint / Type | ruff + mypy（strict） | CI 必过 |
| Web 后端 | FastAPI + uvicorn | optional extra `[web]` |
| Web 前端 | Vue 3 + Vite + TypeScript | 单页应用，build 到 `web/static/` 由 FastAPI 托管 |
| 默认 Provider | 智谱 GLM-5.0 | Anthropic 兼容协议，凭证在 `.env` |

---

## 功能模块

按代码组织划分，每块对应一个或多个 Step。每个模块都标注了**边界**（明确说明它**不**做什么），帮你避免误用。

### 全局不做的事（跨模块约束）

下面这些是整个项目当前阶段**明确不做**的，无论组合哪个模块都拿不到：

- **RAG**——P2-R 系列引入（marker + heading-aware chunk + SQLite FTS5 + Session-scoped Library ACL）；不引入外部 vector DB。详见 [ROADMAP §P2-R](ROADMAP.md)
- **Long-term user memory / 跨 Session 用户记忆**——仍不做（区别于 RAG）
- **多用户 / 认证 / RBAC / OAuth**——Web 仅本地调试，policy 仅单进程决策
- **公网部署 / 横向扩展**——FastAPI 状态在内存，SSE 单机广播
- **CLI**——只做 Web UI（Step 20）
- **真 LLM 摘要器**——`default_summary_generator` 是规则式
- **Skill 热加载 / 跨项目共享**——文件改了要重启

按代码组织划分，每块对应一个或多个 Step。


### 1. 消息层 `messages.py` / `llm_messages.py`

- `UserMessage` / `AssistantMessage` / `ToolResultMessage` / `SummaryMessage` / `CustomMessage`
- `TextContent` / `ToolCall` / `Usage`
- `LLMMessage` 系列：`LLMUserMessage` / `LLMAssistantMessage` / `LLMToolResultMessage`，对齐 Anthropic Messages API 格式
- Step 1 起 + Step 3 扩展 + Step 5 加 `ToolCall` / `ToolResultMessage`

**边界**：纯数据模型，无业务行为；`CustomMessage` 不会出现在 `convert_to_llm` 输出里（被过滤）。

### 2. Provider 抽象 `model_client.py`

- `ModelClient`（抽象基类）：`stream()` 是 async generator，按顺序 yield `StreamEvent`
- `GLMClient`（生产用）：完整解析 Anthropic 协议——`text_delta` 流式、`tool_use` block（`content_block_start` + `input_json_delta` + `content_block_stop` → `ToolCallEvent`）、`message_delta(stop_reason)`、`usage`
- `FakeClient`（离线测试用）：按预置脚本回放 `StreamEvent`，记录每次 stream 调用的 messages
- `StreamEvent` 联合类型：`TextDeltaEvent` / `ToolCallEvent` / `DoneEvent` / `ErrorEvent`

**边界**：仅 Anthropic 协议（Step 21 才拆 OpenAI/Anthropic/GLM 多 provider）；`FakeClient` 仅供测试，不要进生产；不修改 `stream` 内部状态、`convert_to_llm`、`tool_use` parse 这三处既有契约。

### 3. 事件流 `events.py`

10+ 种 `AgentEvent`，覆盖完整生命周期：

- **Agent 级**：`AgentStartEvent` / `AgentEndEvent`
- **Turn 级**：`TurnStartEvent` / `TurnEndEvent`
- **Message 级（流式）**：`MessageStartEvent` / `MessageUpdateEvent` / `MessageEndEvent`
- **Tool 执行级**：`ToolExecutionStartEvent` / `ToolExecutionEndEvent`
- **Queue / Abort 级（Step 9）**：`RequestQueuedEvent` / `RequestStartEvent` / `RequestEndEvent` / `AgentAbortEvent`

所有事件都是 Pydantic 模型，可直接 JSON 序列化——这是 Web UI SSE 推送的基础。

**边界**：事件只描述"发生了什么"，不带投递保证（at-most-once；订阅者慢了会被丢弃）；不是 audit log（持久化在 Session/Snapshot 里）。

### 4. Context 转换 `context.py`

- `transform_context`：把 Agent 内部消息（含 Summary / ToolResult）转换为 LLM 可读的 messages
- `convert_to_llm`：去摘要、合并连续 UserMessage、组装 `LLMMessage` 列表
- `TransformContextFn`：自定义转换 hook

**边界**：只做"消息形状变换"——不直接读取外部知识；RAG 的检索由 `search_knowledge` Tool 在 loop 层注入（P2-R 系列），不在 context 层做向量召回。`CustomMessage` 在转换时被丢弃（不会发给 LLM）。

### 5. Tool Hooks `hooks.py`

- `BeforeToolCallFn` / `AfterToolCallFn`：工具调用前后的拦截点
- `BeforeToolCallContext` / `BeforeToolCallResult`：可短路、可改 args
- `AfterToolCallContext`：可观察 result、可记录指标

**边界**：hooks 在工具**单次调用**粒度触发；不能拦截 batch 整体（批次粒度看 loop 内部）；after-hook 抛异常会被 loop 吞掉并转成 error ToolResult，不会让 Agent 崩。

### 6. Agent Loop `loop.py`

- `run_event_loop`：核心循环——调 LLM stream → 累积 text + tool_calls → 执行工具 → 再调 LLM 直到 `stop`
- `run_min_loop`：极简版，便于 demo
- 处理 abort（`asyncio.Event` signal）、错误包装（不抛异常，包成 `stop_reason="error"` 的 AssistantMessage）

**边界**：单 turn 内的逻辑——不感知 queue / status（那是 Agent 的事）；abort 是**协作式中止**（在 checkpoint 检查 signal），不能强制 cancel 正在跑的 LLM stream 或工具 `execute()`；错误不抛异常，统一包成 `stop_reason="error"` 的 AssistantMessage。

### 7. Agent 状态机 `agent.py`

- `AgentState`：messages / turn_count / status / queue / pending request
- `AgentStatus`：`idle` / `running` / `error` / `aborted`
- `AgentRequest`：prompt / continue / steer / follow_up
- `Subscriber`：subscribe Agent 事件流
- `Agent.run_prompt()` / `Agent.continue_()` / `Agent.abort()` / `Agent.wait_for_idle()`

**边界**：Agent **不感知** skills / MCP / policy——那是 Harness 的事；不持久化 messages（Session 才存）；abort 是协作式 signal，不强制 cancel task；`reset()` 仅在 idle 时可用，running 中会抛 `RuntimeError`。

### 8. Harness 应用层 `harness.py`

`AgentHarness` 包装 `Agent`，补充应用层能力：

- **Phase 控制**：`HarnessPhase`（before_request / agent / after_request / done）
- **Hooks**：`BeforeRequestHook` / `AfterRequestHook` / `OnEventHook` / `OnErrorHook`
- **Skills 注入**：`attach_skills()` + `select_skills()` 自动渲染到 system prompt
- **MCP 注册**：`attach_mcp_servers()` 管理生命周期
- **Permission Policy**：每条 ToolCall 都过 policy
- **Session 同步**：可选 `attach_session()` 双向同步 messages

**边界**：Harness **不替代** Agent，是外层协调；`require_approval` 决策按 deny 处理（Step 18 不做交互式 human approval UI）；`on_error` hook 抛异常追加进 metadata，不递归；compaction **不删除** snapshots（历史永久保留）。

### 9. Turn Snapshot `snapshot.py`

每个 turn 完成后构建不可变 `TurnSnapshot`：

- `messages_before` / `messages_after`
- `events`（事件快照）
- `tool_calls` / `tool_results`（工具调用明细）
- `metadata` / `status` / `error` / `duration_ms`
- `SnapshotBuilder` 在 running 期间累积，turn 结束 freeze

**边界**：仅内存对象——**不**持久化（Session 负责）；不带 Session / Memory / Durable Storage（Step 12）/ Skills / CLI；freeze 后字段不可变。

### 10. Session Memory `session.py`

- `SessionMemory`：单 session 累积 messages + snapshots + metadata
- `SessionState`：可序列化的 session 状态
- `SessionStore`：`InMemorySessionStore` / `JsonFileSessionStore`（JSONL 持久化）
- 序列化 / 反序列化 helper：`serialize_message(s)` / `deserialize_message(s)_snapshot`
- Session tree：fork / restore（Step 12）

**边界**：**不**含 Skills / Compaction / Long-term user memory；存储后端只有内存和 JSONL（无 DB / Redis）；只支持单 session 实例（多 session 由上层 session store 管理）。**向量召回不在此模块**——RAG 检索属于 Knowledge 子系统（P2-R 系列，独立 `knowledge.db`），与 SessionMemory 解耦。

### 11. Session Sync `session_sync.py`

- `SessionSyncConfig`：自动保存策略 / 一致性检查配置
- `SessionConsistencyReport` + `SessionConsistencyIssue`：列出 agent ↔ session ↔ snapshot 的不一致
- `SessionAutoSavePolicy`：never / on_turn_end / on_request_end / debounced

**边界**：只做"一致性检查 + 自动保存触发"；**不**做 Skills / Compaction / Vector / 自动摘要；不修复 issue——只报告，调方决定是否 `restore_messages`。

### 12. Skills / Templates `skills.py`

- `Skill`：name / description / prompt / tags / status / tool_names / metadata
- `SkillRegistry`：注册、查询、按 `SkillSelection`（names / tags / values）过滤
- `PromptTemplate`：Jinja-like 模板渲染，支持 `{{var}}` 占位
- `SkillInjectionConfig`：渲染到 system prompt 的格式 / 顺序
- `render_skill_block()`：单个 skill 渲染为 markdown block

**边界**：Skill 是**提示组织层**——不改变 Agent 执行内核；`tool_names` 仅 metadata（**不**自动注册工具到 registry）；PromptTemplate 用 Python `str.format()`，**不**引入 Jinja2；Step 14 **不做** Compaction / Branch Summary / 向量召回 / 自动摘要。

### 13. Compaction / Branch Summary `compaction.py`

- `compact_messages()`：基于 message_count / token 估算触发压缩
- `CompactionConfig`：触发阈值 / 保留策略
- `BranchSummary` / `BranchSummaryConfig`：fork 出分支时生成摘要
- `SummaryGenerator` / `default_summary_generator`：可替换的摘要器（未来接真 LLM）

**边界**：**只压缩 messages**——不删 snapshots、不动 events；Step 15 **不做** RAG / Long-term user memory / 自动后台压缩 / 数据库；`default_summary_generator` 是规则式（不调真 LLM）。向量召回属于 Knowledge 子系统（P2-R 系列），不在此模块。

### 14. Tools 子包 `tools/`

- `AgentTool`（抽象基类）：`name` / `description` / `parameters`（JSON Schema）/ `execute()`
- `ToolRegistry`：注册、查询、按 name 解析
- `ToolResult`：标准返回结构
- `ToolExecutionMode`：`sequential` / `parallel` / `terminate`
- `WebSearchTool`（Step 5.5）：内置 web search demo

**边界**：执行是**协作式**的——`execute()` 内部不会感知 abort signal，只能在调用前后检查；不能跨 tool 共享状态（每个 tool 独立）；内置只 Echo + WebSearch，其它工具通过 MCP 或用户自注册。

### 15. Tool Validation `tool_validation.py`

- `validate_tool_arguments()`：基于 JSON Schema 校验工具参数
- `ToolArgumentValidationError`：详细错误信息

**边界**：只做 **JSON Schema 语法**校验；不做语义校验（如"路径是否存在"、"权限够不够"——那是 tool `execute()` 自己的事）；不修改 args（返回错误而非修复）。

### 16. MCP 子包 `mcp/`

完整 MCP（Model Context Protocol）客户端实现：

- `MCPClient`：单个 server 连接
- `MCPTransport` / `StdioMCPTransport` / `HttpMCPTransport` / `FakeMCPTransport`
- `MCPRegistry`：管理多个 server，统一暴露 tools
- `MCPAgentTool`：把 MCP tool 适配为 `AgentTool`
- `MCPPromptSkillAdapter`：把 MCP prompts 适配为 Skill
- `naming.py`：`server__tool` 命名规范 + 校验
- `errors.py`：完整异常体系

**边界**：第一阶段只接 **tools + prompts**，**不**接 resources / notifications / progress / logging / subscribe；`HttpMCPTransport` 是 placeholder（连接时抛 `NotImplementedError`，仅 stdio 可用）；MCP server 异常被 adapter 主动转成 `is_error=True` 的 ToolResult，不让异常穿透到 loop。

### 17. Permission / Policy 子包 `policy/`

- `ToolPermissionPolicy`（抽象基类）
- `AllowAllToolPermissionPolicy` / `DenyAllToolPermissionPolicy` / `DefaultToolPermissionPolicy`
- `ToolPermissionDecision`：`allow` / `deny` / `ask`
- `InMemoryToolPermissionAuditLog` + `ToolPermissionAuditRecord`：审计日志
- `sandbox.py`：路径沙箱（`is_path_within_roots` / `extract_candidate_paths` / `parse_mcp_namespaced_tool`）

**边界**：`permission_policy=None` 关闭检查（向后兼容）；policy 抛异常 / 返回非法对象时**不让 loop 崩**，转成 `error_type="ToolPermissionPolicyError"` 的 ToolResult；audit log **仅内存**（无文件 / DB 持久化）；`require_approval` 按 deny 处理（Step 18 不做 human approval UI）；不做 OAuth / RBAC / 多用户 / 企业 secret vault。

### 18. Skill File Loader `skill_loader.py`

- `SkillFileLoader`：从文件系统加载 SKILL.md
- `parse_skill_markdown()`：解析 YAML frontmatter + body
- 安全限制：`DEFAULT_MAX_FILE_SIZE_BYTES` / `DEFAULT_ALLOWED_FILENAMES`
- 异常：`SkillFileLoadError` / `SkillFileFormatError` / `SkillFileSecurityError`

**边界**：MVP **不支持热加载**（文件改了要重启 Harness 才生效）；不做跨项目共享（无 project / workspace / global 三级库）；只识别指定文件名（默认 `SKILL.md` / `skill.md`），不支持任意 .md。

### 19. Web 子包 `web/`

本地调试 UI（FastAPI + Vue 3）：

- `app.py`：`create_app(harness)` 工厂，注册 REST API + SSE + 静态资源
- `state.py`：`WebAppState` + `TraceEventBuffer`（最近 500 事件）
- `serializers.py`：Pydantic 模型 → JSON-safe dict
- `frontend/`：Vue 3 + Vite + TypeScript 单页应用

**边界**：**仅本地调试**——不实现认证 / 多用户 / 公网部署；状态全在内存（重启丢失，SSE 客户端掉线不补推）；event buffer 最多 500 条（FIFO 丢弃）；并发请求由 `_ensure_idle()` 拒绝（POST `/api/prompt` 在 running 时直接 409）。

**REST API**（均同源，前缀 `/api`）：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/state` | agent / harness 状态摘要 |
| GET | `/messages` | 当前 messages |
| GET | `/events` | event buffer（最近 500） |
| POST | `/events/clear` | 清空 event buffer |
| GET | `/snapshots` | snapshot summaries |
| GET | `/snapshots/{i}` | 完整 snapshot detail |
| GET | `/session` | session summary |
| GET | `/mcp` | MCP servers / tools / prompts |
| GET | `/skills` | SkillRegistry + skill_loader metadata |
| GET | `/policy/audit` | permission audit records |
| POST | `/prompt` | `{ text, skill_selection }` |
| POST | `/abort` | `{ reason }` |
| POST | `/reset` | `{ clear_events, clear_snapshots, clear_audit }` |
| GET | `/stream` | SSE 事件流（实时推送） |

---

## 项目结构

```
pi-py/
├── README.md                     # 本文件
├── CLAUDE.md                     # AI 协作约定（必读）
├── PLAN.md                       # 15 步核心 + 扩展 track 实施计划
├── STATUS.md                     # 当前状态
├── TODO.md                       # 待办
├── pyproject.toml                # 项目元数据 + dependencies
├── src/pi_agent_core_py/
│   ├── __init__.py               # 顶层 re-exports（340+ 行）
│   ├── messages.py               # 消息模型
│   ├── llm_messages.py           # LLM 协议层消息
│   ├── model_client.py           # Provider 抽象 + GLMClient + FakeClient
│   ├── events.py                 # AgentEvent 联合类型
│   ├── context.py                # Context 转换
│   ├── hooks.py                  # Tool Hooks
│   ├── loop.py                   # Agent Loop（核心）
│   ├── agent.py                  # Agent 状态机
│   ├── harness.py                # AgentHarness 应用层
│   ├── snapshot.py               # Turn Snapshot
│   ├── session.py                # Session Memory
│   ├── session_sync.py           # Session 一致性检查
│   ├── skills.py                 # Skills / Templates
│   ├── compaction.py             # Compaction / Branch Summary
│   ├── tool_validation.py        # 工具参数校验
│   ├── skill_loader.py           # SKILL.md 文件加载
│   ├── tools/                    # 内置工具子包
│   │   ├── __init__.py
│   │   └── web_search.py
│   ├── mcp/                      # MCP 客户端子包
│   │   ├── __init__.py
│   │   ├── adapter.py            # MCPAgentTool / MCPPromptSkillAdapter
│   │   ├── client.py             # MCPClient
│   │   ├── config.py             # MCPServerConfig
│   │   ├── errors.py
│   │   ├── naming.py
│   │   ├── prompts.py            # MCP prompts → Skill
│   │   ├── registry.py           # MCPRegistry
│   │   └── transport.py          # stdio / http / fake
│   ├── policy/                   # 权限子包
│   │   ├── __init__.py
│   │   ├── audit.py              # 审计日志
│   │   ├── permissions.py        # Policy 抽象 + 3 个默认实现
│   │   └── sandbox.py            # 路径沙箱
│   └── web/                      # Web UI 子包
│       ├── __init__.py
│       ├── app.py                # FastAPI 工厂
│       ├── serializers.py
│       ├── state.py
│       ├── static/               # Vue build 产物
│       └── frontend/             # Vue 3 + Vite + TS 源码
│           ├── package.json
│           ├── vite.config.ts
│           ├── tsconfig.json
│           ├── index.html
│           └── src/
│               ├── main.ts
│               ├── App.vue       # Pinia 初始化 + WS 连接（不再渲染 DeveloperDrawer）
│               ├── api/          # client.ts + 各模块 fetch 封装（sessions/files/skills/mcp/messages）
│               ├── types/        # 强类型 barrel index.ts
│               ├── stores/       # Pinia stores（chat / session / file / skill / mcp）
│               ├── styles.css    # 全局样式（chat 风格）
│               └── components/
│                   ├── layout/
│                   │   ├── AppShell.vue        # 两栏 shell（sidebar + main slots）
│                   │   └── SessionSidebar.vue  # New chat / 会话列表 / Skills / MCP footer 按钮
│                   ├── chat/                   # 中间对话流 + Inline Turn Cards
│                   │   ├── ChatPanel.vue
│                   │   ├── MessageList.vue
│                   │   ├── ChatInput.vue       # 拖放 + 附件 + Enter 发送
│                   │   ├── AttachmentBar.vue
│                   │   ├── FileChip.vue
│                   │   ├── MessageBubble.vue
│                   │   ├── TurnInfoCard.vue
│                   │   ├── ToolCallCard.vue
│                   │   ├── ToolResultCard.vue
│                   │   ├── FileReadCard.vue
│                   │   ├── MCPToolCard.vue
│                   │   ├── SkillUsedCard.vue
│                   │   └── ErrorCard.vue
│                   ├── skills/                 # Skills Manager Modal
│                   │   ├── SkillManagerModal.vue
│                   │   ├── SkillUploadForm.vue
│                   │   └── SkillList.vue
│                   ├── mcp/                    # MCP Manager Modal
│                   │   ├── MCPManagerModal.vue
│                   │   ├── MCPServerForm.vue
│                   │   ├── MCPServerList.vue
│                   │   └── MCPToolList.vue
│                   └── common/
│                       ├── Modal.vue           # 通用居中弹窗（非右栏 Drawer）
│                       ├── ErrorBanner.vue
│                       ├── LoadingSpinner.vue
│                       └── EmptyState.vue
├── tests/                        # pytest 测试（按 step 组织）
├── examples/                     # 示例代码
└── steps/                        # 每 Step 的 demo + Architecture.md + tutorial.md
    ├── step-01-min-loop/
    ├── step-02-event-stream/
    ├── ...
    └── step-20-web-trace-viewer/
```

---

## 快速开始

### 1. 环境准备

```bash
# 推荐：用 conda 创建 pipy 环境
conda create -n pipy python=3.12 -y
conda activate pipy

# 或直接用系统 Python >= 3.11
```

### 2. 安装

```bash
cd D:/LLMTutorial/pi/pi-py

# 基础安装
pip install -e ".[dev]"

# Web UI 额外依赖
pip install -e ".[web]"
```

### 3. 配置凭证

在项目根创建 `.env`：

```bash
# 智谱 GLM-5.0（默认 provider，Anthropic 兼容）
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
ANTHROPIC_API_KEY=your_zhipu_key
ANTHROPIC_MODEL=glm-5.0
```

### 4. 跑测试

```bash
pytest tests/ -v -m "not slow"
# 真实 LLM 测试（需要 API key）
PI_RUN_SLOW=1 pytest tests/ -v -m "slow"
```

### 5. 跑 demo

每个 Step 都有独立 demo：

```bash
# Step 20 Web UI（ChatGPT-like 界面）
python steps/step-20-web-trace-viewer/demo.py
# → http://127.0.0.1:8000

# 其他 step demo
python steps/step-XX-name/demo.py
```

---

## Web Claude MVP

**Web Claude P0 MVP 完成（2026-07-07）**——claude.ai 风格的两栏聊天界面，支持文件上传、Skills、MCP server。

> ⚠️ **Web Claude P0 MVP is complete for local development. Localhost-first, no authentication, not suitable for public exposure.**
>
> 默认 `include_prompt=true` 返回 403 防止 prompt 模板泄露；MCP env values 严格不回显（response 只有 `env_keys`）；MCP/Skill 配置 P0 不持久化（重启即丢）。

### 功能

- **左侧 SessionSidebar**：New chat / 会话列表 / 重命名 / 删除；底部 Skills / MCP 按钮挂载 modal
- **中间 ChatPanel**：header 状态 + 消息流 + ChatInput（拖放 + 文件选择 + Enter 发送 / Shift+Enter 换行；running 时 Send → Stop）
- **Inline Turn Cards**（淡化、不抢主舞台）：
  - `UserMessage` / `AssistantMessage`（streaming draft + 光标动画）
  - `TurnInfo`（本轮 turn 信息折叠块）
  - `ToolCall` / `ToolResult`（普通工具）
  - `FileRead`（list_files / view_file）
  - `MCPToolCall`（mcp__server__tool）
  - `SkillUsed`（本轮启用 skill 提示）
  - `Error`
- **文件上传**：md / html / csv / parquet / 文本；**图片明确 unsupported**（不做 OCR / 不做视觉理解）；**PDF 正文不解析**
- **Skills Modal**：上传 SKILL.md + enable/disable + Use-this-turn 选择（selected 必须是 enabled 子集）
- **MCP Modal**：add server（args JSON + env key/value）+ Test / Enable / Disable / Delete + tools enable/disable

### 启动

```bash
# 后端（FastAPI on :8000）—— 最小内嵌示例
PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -c "
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient, TextDeltaEvent, DoneEvent
from pi_agent_core_py.web.app import create_app
import uvicorn

fake = FakeClient([[TextDeltaEvent(delta='ok'), DoneEvent(stop_reason='stop')]])
agent = Agent(system_prompt='demo', client=fake)
harness = AgentHarness(agent)
# 启用文件上传 + sqlite 多会话 + prompt preview（仅本地调试）
app = create_app(
    harness,
    db_path=':memory:',
    uploads_dir='uploads',
    allow_prompt_preview=True,
)
uvicorn.run(app, host='127.0.0.1', port=8000)
"

# 前端开发（热重载，:5173，proxy /api → :8000）
cd src/pi_agent_core_py/web/frontend
npm install
npm run dev

# 或直接 build 由 FastAPI 托管
npm run build
```

`create_app` 关键参数（全部可选）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `event_buffer_max_size` | 1000 | TraceEventBuffer 容量，超过丢最旧 |
| `allow_prompt_preview` | False | `?include_prompt=true` 是否暴露 Skill prompt；True 也只接受 localhost |
| `db_path` | None（`:memory:` 内部 fallback） | SQLiteSessionStore 路径；None → 内存库 |
| `uploads_dir` | None | VirtualFileStore 根目录；None → 不启用文件上传路径（相关 endpoint 返回 503） |
| `max_file_size` | 25 MB | 单文件大小上限 |
| `max_session_upload_size` | 100 MB | 单 session 总上传上限 |

### 界面布局

```
┌─────────────┬─────────────────────────────────────┐
│  Sidebar    │  Chat Header (status / turns)       │
│  - app name │─────────────────────────────────────│
│  - New chat │                                     │
│  - Sessions │  Message List                       │
│  - Skills ▼ │   - user 气泡（右）                  │
│  - MCP    ▼ │   - assistant 流式（左）             │
│             │   - inline Turn / ToolCall /         │
│             │     FileRead / MCPTool /             │
│             │     SkillUsed / Error cards          │
│             │─────────────────────────────────────│
│             │  Composer (Enter 发送 / Stop / 📎)   │
└─────────────┴─────────────────────────────────────┘
```

**无右栏 / 无 Drawer / 无 DeveloperDrawer 主入口**——Skills 和 MCP 通过 footer 按钮打开居中 Modal。

### 使用流程

1. 打开 `http://127.0.0.1:8000`，自动加载 default session
2. **新建 session**：左栏顶部 `+ New chat`
3. **发送消息**：底部输入框 → Enter
4. **上传文件**：📎 按钮或拖放到输入框 → AttachmentBar 显示 FileChip → Send 时携带 `file_ids`
5. **使用 Skills**：左栏底部 `Skills` → 上传 SKILL.md → enable → 勾选 "Use this turn" → 发送时携带 `skill_names`
6. **使用 MCP**：左栏底部 `MCP` → Add server（填 name / command / args JSON / env）→ Test connection → Enable → 在 Tools 区 Enable 单个工具

### 安全说明（重要）

- **localhost only / no auth**：无鉴权 / 无多用户隔离 / 无 rate limit；不建议公网暴露
- **MCP command / env 是本地开发能力**：用户可填任意 stdio command；env values 在 server memory 中（用于子进程）
- **MCP env values 不回显**：response 类型只有 `env_keys`，没有任何 endpoint 返回 env value；前端表单提交后立即清空，`type=password + autocomplete=new-password` 防浏览器回填
- **Prompt preview 默认禁用**：`?include_prompt=true` 默认 403；需 `create_app(allow_prompt_preview=True)` + localhost
- **不做 RBAC / 多用户 / OAuth**

### 当前限制

- `POST /api/prompt` **同步阻塞**——LLM 调用结束才返回；当前没有 `/api/prompt/async`；前端通过 WS `/ws/events` 展示实时事件，但 prompt 请求本身仍等待后端完成
- MCP / Skill 配置**不持久化**——重启即丢
- 不支持图片理解（不做 OCR / 不做视觉理解）
- PDF 正文不解析
- 无 Regenerate
- 无 Export markdown

### Browser smoke tests

Playwright E2E smoke（5 核心用例 + 1 skip）位于 `tests/e2e/`，覆盖真实浏览器下的 layout / chat / file upload / image unsupported / Skills & MCP modals + env value 不回显。运行说明见 [`docs/guides/web-testing.md`](docs/guides/web-testing.md) 的「Browser smoke tests / Playwright」段。

### REST endpoints

完整 API 详见 [`docs/api/web-api.md`](docs/api/web-api.md)。常用：

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | Agent / Harness 状态摘要 |
| `/api/messages?session_id=` | GET | 当前 / 历史 messages |
| `/api/sessions` | GET / POST | session 列表 / 创建 |
| `/api/sessions/{sid}` | PATCH / DELETE | 重命名 / 删除 |
| `/api/sessions/{sid}/files` | GET / POST | 列出 / 上传 session 文件 |
| `/api/sessions/{sid}/files/{fid}` | GET / DELETE | 下载 / 删除单文件 |
| `/api/skills` | GET | attached skills（`include_prompt=true` 默认 403） |
| `/api/skills/upload` | POST | multipart 上传 SKILL.md |
| `/api/skills/{name}/enable` `/disable` | POST | 启用 / 禁用 |
| `/api/mcp/servers` | GET / POST | server 列表 / 添加 |
| `/api/mcp/servers/{name}/test` | POST | 测试连接（不污染 harness） |
| `/api/mcp/servers/{name}/enable` `/disable` | POST | 启用 / 禁用 |
| `/api/mcp/servers/{name}` | DELETE | 删除 |
| `/api/mcp/tools/{name}/enable` `/disable` | POST | 启用 / 禁用（真实 unregister/register） |
| `/api/prompt` | POST | 同步触发 prompt（支持 session_id / file_ids / skill_names） |
| `/api/abort` | POST | 中止当前请求 |
| `/api/reset` | POST | 重置 agent / clear events / snapshots / audit |

### 实时事件流（SSE / WebSocket）

- **`GET /api/stream`**（SSE）—— 兼容通道；新增 `?limit=N` 用于自动化测试
- **`WS /ws/events`**（WebSocket，**前端 P0 默认**）—— 每连接独立 `asyncio.Queue(maxsize=100)`；慢客户端 queue 满时丢弃该 event

---

## 开发版 Web UI（旧 Trace Viewer）

> ⚠️ v0.0.22 baseline 之前的 DeveloperDrawer 调试 UI 已从主路径下线（文件保留）；需要查看时直接读 `state.json` / 用 `/api/state` / `/api/events` / `/api/snapshots` 等 endpoint。

---

## 开发流程

详见 [`CLAUDE.md`](CLAUDE.md)，要点：

1. 每完成一个 Step，按 `steps/step-XX-name/GUIDE.md` 实现
2. 在 `PLAN.md` 的进度表把 ☐ 改成 ✅
3. 跑 `pytest tests/ -v -m "not slow"` 确认不回归
4. 不依赖真实 API key 的测试必须始终通过

### 不做的事

- 不修改 `pi-main/` 下的 TS 源码（上游参考项目）
- 不把 `.env` 提交到 git
- 不在 Pydantic 模型上 mutation 后又传给订阅者——保持不可变语义
- 不在循环里抛异常——按 TS 版契约，错误要包成 `stop_reason="error"` 的 AssistantMessage 或 `ErrorEvent`

---

## 路线图

### 已完成（Step 1–21 + Web Claude P0 MVP）

- ✅ **Step 1–15**（核心）：min-loop / event-stream / context / tool / multi-tool / agent-state / queue-abort / harness / snapshot / session / session-sync / skills / compaction
- ✅ **Step 16** MCP Tools + Tool Validation
- ✅ **Step 17** MCP Harness Integration
- ✅ **Step 18** Permission / Approval Policy
- ✅ **Step 19** Skill File Loader + MCP Prompts
- ✅ **Step 20** Web App / Trace Viewer
- ✅ **Step 20.5** ChatGPT-like Chat UI 重构（流式显示）
- ✅ **Step 21** Provider Adapter Refactor（GLM / Anthropic / OpenAI / Fake 拆分）
- ✅ **Web Claude P0 MVP**（P0-1 sqlite 多会话 + P0-2 VirtualFileStore + P0-3 view_file/list_files + P0-4 Claude-like Web UI Step 1–8 + P0-5 默认 system prompt）

### 后续 step（不在本副本）

主仓库 `D:\LLMTutorial\pi\pi-py\` 还在继续推进：

- Step 22 — Sandbox Execution (Docker)
- Step 23 — Sandbox Git Integration
- Step 24 — Slash Command System
- Step 25+ — Plan Mode / Auto Compaction / LLM Summary / Multi-Agent

本副本（`D:\LLMTutorial\test\`）**只到 Step 21**——主仓库的 Step 22+ 内容不在这里。

> 主仓库 Python 路径（不在本副本）：`../pi-py/PLAN.md`

---

## License

MIT（与上游 `@earendil-works/pi-agent-core` 一致）。

> 上游 TypeScript 项目路径（不在本副本）：`../pi-main/packages/agent`

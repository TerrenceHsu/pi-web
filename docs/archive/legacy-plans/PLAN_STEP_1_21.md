# pi-agent-core-py 实施计划（v2，按 15 步框架）

> **Archived**: this document is retained for historical reference and is no longer the source of truth.
> 当前真源见 [STATUS.md](../../../STATUS.md) / [ROADMAP.md](../../../ROADMAP.md) / [CHANGELOG.md](../../../CHANGELOG.md)。
>
> **本副本未保留 `steps/` 教学快照目录**——下表中所有指向 `steps/step-XX-name/...` 的链接均为历史文字，**不再可点击**。

---

> 旧计划（22 步）已废弃。本文件是新的真源。
> 每个 step 在 `steps/step-XX-name/GUIDE.md` 下有独立指导文件。
> 完成的 step 在下表打 ✅；进行中 🔄；未开始 ☐。

## 总目标

把 `pi-main/packages/agent`（`@earendil-works/pi-agent-core`，TypeScript，约 8167 行）
完整重写成 Python 3.12 版，按"垂直切片、端到端跑通"的方式分 15 步推进。

## 决策（已与用户对齐）


| 维度          | 选择                                         |
| --------------- | ---------------------------------------------- |
| Python 版本   | 3.12（conda 环境`pipy`）                     |
| 并发模型      | asyncio                                      |
| 数据建模      | Pydantic v2                                  |
| 默认 Provider | 智谱 GLM-5.0（Anthropic 兼容，已配在 .env）  |
| 测试          | pytest + pytest-asyncio                      |
| 已实现代码    | 保留：`types.py`、`tool.py`、`tools/echo.py` |
| 旧 GUIDE      | 已删除（旧 22 步框架作废）                   |

## 15 步框架


| #  | 阶段                        | 目标                                                  | 状态 | GUIDE                                                                        |
| ---- | ----------------------------- | ------------------------------------------------------- | ------ | ------------------------------------------------------------------------------ |
| 1  | 最小可运行 Loop             | 项目骨架、Message、ModelClient、最小 Loop             | ✅   | `step-01-min-loop`                          |
| 2  | Event Stream                | Agent 事件流（emit/subscribe/10 种事件）              | ✅   | `step-02-event-stream`           |
| 3  | Context 转换                | `transform_context` / `convert_to_llm`                | ✅   | `step-03-context-transform` |
| 4  | Tool 基础模型               | ToolCall / ToolResult / AgentTool / ToolRegistry      | ✅   | `step-04-tool-model`               |
| 5  | 单工具执行                  | 模型→工具→模型完整链路                              | ✅   | `step-05-single-tool`             |
| 6  | Tool Hooks                  | before / after tool hook                              | ✅   | `step-06-tool-hooks`               |
| 7  | 多工具执行                  | sequential / parallel / terminate                     | ✅   | `step-07-multi-tool`                      |
| 8  | Agent 状态机                | AgentState / prompt / continue / subscribe            | ✅   | `step-08-agent-state`                    |
| 9  | Queue / Abort               | steer / follow_up / abort / wait_for_idle             | ✅   | `step-09-queue-abort`                    |
| 10 | Harness Phase               | Harness 运行阶段控制                                  | ✅   | `step-10-harness-phase`                |
| 11 | Turn Snapshot               | 每轮不可变快照 + active tools                         | ✅   | `step-11-turn-snapshot`         |
| 12 | Session Memory              | session tree / JSONL / fork / restore                 | ✅   | `step-12-session-memory`       |
| 13 | Harness + Session           | pending writes / save point / 持久化                  | ✅   | `step-13-harness-session`     |
| 14 | Skills / Templates          | SKILL.md / system prompt skill list / prompt template | ✅   | `step-14-skills-templates`   |
| 15 | Compaction / Branch Summary | 上下文压缩 + 分支摘要                                 | ✅   | `step-15-compaction-branch` |

图例：☐ 未开始 / 🔄 进行中（部分代码已有）/ ✅ 完成

## Post-15 Extension Track：MCP + Tool Runtime + Web UI

15-step core 已完成。后续扩展暂不做 RAG / Vector Memory / Long-term Memory，优先增强 MCP 工具生态、工具安全、Skill 文件化、网页端调试能力、上下文压缩增强。

> **本副本范围**：仅 Step 1–21（Phase A/B/C）。
> 主仓库的 Phase D（Sandbox / Slash / Plan Mode）及以后内容**不在本副本**。

### 总原则

1. 不做 RAG / Vector Memory / Long-term Memory。
2. MCP 第一阶段只接 tools 和 prompts，不接 resources。
3. **不做 CLI**——交互入口只有 Web UI。
4. Skill 文件加载先做 MVP，热加载和跨项目共享后置。
5. 自动压缩和真 LLM 摘要器属于 Compaction 增强（不在本副本）。
6. 每个 step 仍保持垂直切片：demo.py、Architecture.md、tutorial.md、src 同步更新。

### 路线表


| #  | 阶段                            | 目标                                                 | 状态 |
| ---- | --------------------------------- | ------------------------------------------------------ | ------ |
| 16 | MCP Tools + Tool Validation     | MCP tools 接入 + JSON Schema 参数校验                | ✅   |
| 17 | MCP Harness Integration         | Harness 管理 MCP server 生命周期                     | ✅   |
| 18 | Permission / Approval Policy    | 工具权限、路径沙箱、审计                             | ✅   |
| 19 | Skill File Loader + MCP Prompts | SKILL.md 加载 + MCP prompts 转 Skill                 | ✅   |
| 20 | Web App / Trace Viewer          | Vue + FastAPI 本地 Trace Viewer                      | ✅   |
| 21 | Provider Adapter Refactor       | provider 抽象、GLM/Anthropic-compatible adapter 边界 | ✅   |

### Phase 划分（按依赖关系排序，不是 Step 数字顺序）

- **Phase A：MCP 工具闭环**（Step 16–18）✅
- **Phase B：Prompt + Web UI**（Step 19–20）✅
- **Phase C：Provider 抽象**（Step 21）✅

### Phase A：MCP 工具闭环

#### Step 16 — MCP Tools + Tool Validation

**目标**：把 MCP server 暴露的 tools 包装成项目内部 `AgentTool`，注册进 `ToolRegistry`，让现有 `run_event_loop` 可以无感执行。

**核心链路**：

```text
MCP Server
↓
MCPClient.initialize()
↓
MCPClient.list_tools()
↓
MCPAgentTool
↓
ToolRegistry.register()
↓
run_event_loop(...)
↓
LLM 发出 ToolCallEvent
↓
MCPAgentTool.execute()
↓
MCPClient.call_tool()
↓
ToolResult
↓
ToolResultMessage
↓
回喂 LLM
```

**新增模块**：

```text
src/pi_agent_core_py/
├── mcp/
│   ├── __init__.py
│   ├── config.py
│   ├── errors.py
│   ├── transport.py
│   ├── client.py
│   ├── adapter.py
│   └── registry.py
└── tool_validation.py
```

**任务清单**：

- `MCPServerConfig`
- `MCPTransport` 抽象
- `StdioMCPTransport`
- `HttpMCPTransport` 可先占位，MVP 只实现 stdio
- `MCPClient.initialize()`
- `MCPClient.list_tools()`
- `MCPClient.call_tool()`
- `MCPToolInfo`
- `MCPCallResult`
- `MCPAgentTool`：MCP tool → AgentTool
- `MCPRegistry`：多个 MCP server 管理
- 工具名 namespace：`mcp__{server_name}__{tool_name}`
- `tool_validation.py`：JSON Schema 参数校验
- 参数校验失败时返回 `ToolResult(is_error=True)`
- FakeMCPServer 测试
- 一个真实 stdio MCP demo
- `steps/step-16-mcp-tools/{demo.py, Architecture.md, tutorial.md}`

**不实现**：MCP resources / RAG / Vector Memory / Long-term Memory / MCP prompts / Web UI / 人工审批流

**验收标准**：

- 能连接 FakeMCPServer，完成 initialize、tools/list
- 能把 MCP tool 包装为 `MCPAgentTool`
- `ToolRegistry.names()` 能看到 `mcp__server__tool`
- `ToolRegistry.definitions()` 能导出 MCP tool schema
- FakeClient 能收到 MCP tool definition
- FakeClient 发出 ToolCallEvent 后，loop 能执行 MCPAgentTool
- MCP call_tool 结果能转换成 ToolResultMessage 并回喂下一轮 LLM
- 参数校验失败不会让 loop 崩
- MCP tool 抛错时返回 `ToolResult(is_error=True)`
- stdio transport close 后无残留进程

#### Step 17 — MCP Harness Integration

**目标**：让 `AgentHarness` 管理 MCP server 生命周期，而不是让 `Agent` 直接感知 MCP。

**新增 Harness API**：

```python
class AgentHarness:
    async def attach_mcp_servers(
        self,
        configs: list[MCPServerConfig],
        *,
        auto_register_tools: bool = True,
    ) -> None: ...

    async def detach_mcp_servers(self) -> None: ...
    async def refresh_mcp_tools(self) -> None: ...
    def list_mcp_servers(self) -> list[MCPServerState]: ...
    def list_mcp_tools(self) -> list[MCPToolInfo]: ...
```

**任务清单**：

- Harness 持有 `MCPRegistry`
- `attach_mcp_servers()` 连接多个 MCP server
- 自动把 MCP tools 注册进 `agent.tools`
- `detach_mcp_servers()` 关闭所有连接
- `refresh_mcp_tools()` 重新拉 `tools/list`
- `harness.close()` 自动释放 MCP transport
- MCP server 状态写入 `context.metadata`
- snapshot 中能看到 MCP 工具调用
- MCP 连接失败不影响已连接 server

**不实现**：MCP resources/read / RAG / Long-term Memory / 工具审批 UI / 网页端

**验收标准**：

- Harness 能 attach 多个 MCP server
- MCP tools 能自动注册进 Agent 的 ToolRegistry
- refresh 后工具列表能更新
- detach 后所有 MCP transport 都关闭
- `harness.close()` 能释放 MCP 资源
- MCP server 状态能写入 metadata
- MCP tool call 能进入 snapshot 的 tool_calls / tool_results
- 连接失败不会污染已连接 server
- Agent 不直接感知 MCP

#### Step 18 — Permission / Approval Policy

**目标**：MCP 接入后补工具安全边界。第一版只做 allow / deny / audit，不做复杂人工审批 UI。

**新增模块**：

```text
src/pi_agent_core_py/
└── policy/
    ├── __init__.py
    ├── permissions.py
    ├── approval.py
    ├── sandbox.py
    └── audit.py
```

**核心类型**：

```python
class ToolPermissionDecision(BaseModel):
    allow: bool
    reason: str | None = None
    require_approval: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolPermissionPolicy(abc.ABC):
    async def check_tool_call(
        self, *,
        tool_call: ToolCall,
        tool: AgentTool | None,
        messages: list[AgentMessage],
    ) -> ToolPermissionDecision: ...
```

**默认策略**：

```text
read-only tool：默认允许
write_file：默认禁止，除非路径在 workspace allowlist
delete_file：默认禁止
shell：默认禁止
network/http：默认禁止，除非 domain allowlist
database write：默认禁止
MCP unknown server：默认禁止
```

**任务清单**：

- `PermissionPolicy` 抽象
- `ToolPermissionDecision`
- allowlist / denylist 策略
- workspace path sandbox
- MCP server allowlist
- MCP tool allowlist / denylist
- `before_tool_call` 接入 policy
- policy decision 写入 `ToolResult.details`
- policy decision 写入 snapshot metadata
- policy hook 抛异常时不让 loop 崩

**不实现**：复杂人工审批 UI / 多用户权限系统 / OAuth / 企业级 secret vault / RAG / Long-term Memory

**验收标准**：

- before_tool_call 能接入 PermissionPolicy
- MCP tool 可以按 server / tool name 匹配策略
- write 类工具默认阻断
- allowlist 路径可放行
- deny 结果进入 ToolResultMessage.details
- snapshot 能记录 policy decision
- session metadata 能记录 audit summary
- policy hook 抛异常不会让 loop 崩

### Phase B：Prompt + Web UI

#### Step 19 — Skill File Loader + MCP Prompts

**目标**：把本地 `skills/{name}/SKILL.md` 和 MCP prompts 统一转换成现有 `Skill` / `PromptTemplate`，接入 `SkillRegistry` 和 `AgentHarness`。

**新增模块**：

```text
src/pi_agent_core_py/
├── skill_loader.py
└── mcp/
    └── prompts.py
```

**SKILL.md 格式**：

```markdown
---
name: coding_review
description: Review Python agent runtime code
priority: 20
tags: ["coding", "review"]
tool_names: ["read_file", "run_tests"]
status: enabled
---

你是一个严格的 Python agent runtime 代码审查助手。

重点检查：
1. async 生命周期
2. event 顺序
3. tool error isolation
4. session consistency
```

**任务清单**：

- `SkillFileLoader`
- `SkillFileConfig`
- `load_skill_file(path)`
- `load_skill_dir(root)`
- `reload_all()` 手动重载
- YAML frontmatter 解析
- frontmatter 字段：name / description / priority / tags / tool_names / status / metadata
- markdown body → `Skill.prompt`
- `SKILL.md` → `Skill`
- 加载失败抛 `SkillRegistrationError` 或专用 `SkillFileLoadError`
- MCP `prompts/list`
- MCP `prompts/get`
- MCP Prompt → `PromptTemplate`
- MCP Prompt → `Skill`
- `SkillRegistry` 同时管理本地 Skill 和 MCP Prompt Skill
- `Harness.run_prompt(skill_selection=...)` 可选择文件加载的 Skill

**不实现**：文件监听热加载 / 跨项目共享 skill 库 / 远程 skill marketplace / 在线编辑 SKILL.md / RAG / Long-term Memory / Web UI

**验收标准**：

- 能从 `skills/{name}/SKILL.md` 加载 Skill
- YAML frontmatter 能解析成 metadata
- 缺 name / description / prompt 时抛错误
- markdown body 能成为 Skill prompt
- `SkillRegistry` 能注册文件加载的 Skill
- Harness 能选择文件加载的 Skill
- MCP prompts 能转换为 PromptTemplate / Skill
- 渲染失败仍然走 error snapshot

#### Step 20 — Web App / Trace Viewer

**目标**：只做网页端相关能力，用于调试和观察 agent runtime。**不包含 CLI**，不做命令行入口，不做 `python -m pi_agent_core_py chat`。

定位：把已有 `AgentEvent` / `TurnSnapshot` / `Session` / `ToolResult` / MCP metadata 可视化。

**前端目录**（`web/`）：

```text
web/
├── package.json
├── vite.config.ts
├── index.html
└── src/
    ├── main.ts
    ├── App.vue
    ├── api/
    │   ├── client.ts
    │   └── websocket.ts
    ├── pages/
    │   ├── ChatPage.vue
    │   ├── TracePage.vue
    │   ├── SnapshotPage.vue
    │   ├── SessionPage.vue
    │   ├── MCPPage.vue
    │   ├── SkillPage.vue
    │   └── PolicyPage.vue
    ├── components/
    │   ├── MessageList.vue
    │   ├── EventTimeline.vue
    │   ├── ToolCallPanel.vue
    │   ├── SnapshotInspector.vue
    │   ├── SessionTree.vue
    │   ├── MCPServerPanel.vue
    │   ├── SkillSelector.vue
    │   └── PolicyDecisionLog.vue
    └── types/
        ├── messages.ts
        ├── events.ts
        ├── snapshots.ts
        ├── sessions.ts
        └── mcp.ts
```

**后端支撑模块**（`src/pi_agent_core_py/web_server/`）：

```text
web_server/
├── __init__.py
├── app.py
├── routes_chat.py
├── routes_sessions.py
├── routes_snapshots.py
├── routes_mcp.py
├── routes_skills.py
├── routes_policy.py
├── websocket.py
└── schemas.py
```

**页面设计**：

- **ChatPage**：输入用户消息；assistant 流式输出；tool call 占位；tool result 摘要；session 选择；skill 选择
- **TracePage**：AgentEvent timeline（RequestQueued → RequestStart → AgentStart → TurnStart → MessageStart → MessageUpdate → ToolExecutionStart → ToolExecutionEnd → TurnEnd → AgentEnd → RequestEnd）
- **SnapshotPage**：snapshot id / request id / request type / status / error / duration_ms / messages_before/after / events / tool_calls / tool_results / metadata
- **SessionPage**：session list / detail / messages / snapshots / compactions / branch summaries / metadata（只展示已有 SessionMemory，不做长期记忆/向量检索）
- **MCPPage**：server list / status / connected/disconnected / tools list / tool schema / last call result / refresh tools（不做 marketplace / resources browser / resources/read）
- **SkillPage**：skill list / enabled/disabled / tags / priority / tool_names / prompt preview / selected preview / 手动 refresh（不做在线编辑 / 版本管理）
- **PolicyPage**：allowed/denied tool calls / deny reason / server name / tool name / arguments preview / snapshot link（不做人工审批 UI / 多用户权限管理）

**Web API**：

```text
GET  /api/sessions
GET  /api/sessions/{id}
GET  /api/sessions/{id}/snapshots
GET  /api/snapshots/{id}
GET  /api/mcp/servers
GET  /api/mcp/tools
GET  /api/skills
GET  /api/policy/audit

POST /api/chat
POST /api/chat/continue
POST /api/chat/abort

WS   /ws/events
```

**WebSocket 事件**：`agent_event` / `message_delta` / `tool_start` / `tool_end` / `snapshot_finished` / `session_updated` / `mcp_status_changed` / `policy_decision` / `error`

**不实现**：CLI / 命令行 chat / 命令行 session 管理 / RAG UI / 知识库 UI / 向量库管理 / 长期记忆管理 / MCP resources browser / 人工审批 UI / 多用户账号系统

**验收标准**：

- 能启动 web server
- 前端能打开 ChatPage
- 用户能从网页发送 prompt
- assistant 流式输出能显示
- tool call / tool result 能显示
- TracePage 能显示 AgentEvent timeline
- SnapshotPage 能查看 TurnSnapshot
- SessionPage 能查看已有 session
- MCPPage 能查看 MCP servers/tools
- SkillPage 能查看 skill 列表
- PolicyPage 能查看 policy decision log
- WebSocket 能实时推送事件
- 页面刷新后能重新加载 session/snapshot
- 不包含 CLI 入口
- 不实现 RAG / Vector Memory / Long-term Memory

### Phase C：Provider 抽象

#### Step 21 — Provider Adapter Refactor

**目标**：把 provider 适配从 `model_client.py` 中拆出来，避免 GLM / Anthropic / OpenAI / FakeClient 混在一个文件里越来越重。

**新目录**：

```text
src/pi_agent_core_py/
└── providers/
    ├── __init__.py
    ├── base.py
    ├── fake.py
    ├── glm.py
    ├── anthropic.py
    └── openai.py
```

**核心设计**：

```python
class ProviderAdapter(abc.ABC):
    def to_provider_messages(...): ...
    def to_provider_tools(...): ...
    async def stream(...): ...
```

**任务清单**：

- `providers/{base,fake,glm,anthropic,openai}.py`
- 保持 `ModelClient.stream()` 对外契约不变
- GLM provider 从原 `model_client.py` 拆出
- FakeClient 从原 `model_client.py` 拆出
- provider tools / messages 转换逻辑拆分
- 旧 import 保持兼容

**不实现**：RAG / Long-term Memory / 模型路由 / 自动 fallback / 多 provider load balancing

**验收标准**：

- 旧代码 import 不破坏
- GLMClient 行为保持一致
- FakeClient 测试保持通过
- OpenAI provider 能基本跑通 text + tool schema
- provider 层拆分后 loop 无需改动
- ToolCallEvent / TextDeltaEvent / DoneEvent / ErrorEvent 仍是统一 StreamEvent

### 暂缓方向

```text
Vector Memory
RAG
Embedding 检索
Chroma / FAISS / pgvector
MCP resources 自动注入上下文
Long-term user memory
跨 session 语义记忆
自动记忆学习
MemoryStore / Retriever / VectorStore
MCP marketplace
复杂人工审批 UI
多用户账号系统
远程 skill marketplace
模型自动路由 / fallback
```

### Post-15 约定

- 每完成一个 Step，按 `steps/step-XX-name/GUIDE.md` 实施，主路线表 ☐ → ✅
- 离线测试 `pytest tests/ -v -m "not slow"` 不能回归；新模块配套测试
- 真实 MCP / Web 验证脚本标 `slow` marker
- 本 track 出现的"RAG / Vector Memory / Long-term Memory"**显式排除**，避免范围漂移
- 每个 step 仍交付垂直切片：demo.py + Architecture.md + tutorial.md + src 同步

### 最终执行顺序

```text
Phase A：MCP 工具闭环                          ✅
    Step 16：MCP Tools + Tool Validation
    Step 17：MCP Harness Integration
    Step 18：Permission / Approval Policy

Phase B：Prompt + Web UI                       ✅
    Step 19：Skill File Loader + MCP Prompts
    Step 20：Web App / Trace Viewer

Phase C：Provider 抽象                         ✅
    Step 21：Provider Adapter Refactor
```

## 当前进度

本副本是 `D:\LLMTutorial\pi\pi-py\` 的精简版本——仅包含 Step 1–21。
Step 22+（Sandbox / Sandbox Git / Slash Command / Multi-Agent）已被显式排除。


| 范围    | Step                                              | 状态 |
| --------- | --------------------------------------------------- | ------ |
| Core    | 1–15（最小 Loop → Compaction / Branch Summary） | ✅   |
| Phase A | 16–18（MCP + 权限）                              | ✅   |
| Phase B | 19–20（Skill 加载 + Web Trace Viewer）           | ✅   |
| Phase C | 21（Provider 抽象）                               | ✅   |

**测试基线（本副本）**：

| 测试层 | 命令 | 结果 |
|--------|------|------|
| 离线核心 | `pytest tests/ -m "not slow and not docker"` | **678 passed, 86 deselected** |
| 全套 | `pytest tests/ -m "not docker"` | **764 passed** |
| Web 集成 | `pytest tests/test_integration_web_server.py -v -m "slow and not docker"` | **25 passed** |
| Lint | `ruff check src tests` | **All checks passed** |

> **当前进度**：v0.0.22 baseline + P0-5（默认 prompt）+ P0-1（sqlite session）+ P0-2（VirtualFileStore）+ **P0-3（view_file / list_files + FileBlock 注入）**全部完成；4/5 P0 子任务交付。
>
> **下一步方向**：P0-4（前端：Sidebar + ChatMain + 淡化卡片 + Skills/MCP Modal）。
> 详见 [archived Web Claude plan](WEB_CLAUDE_PLAN_ORIGINAL.md) 与 [current TODO](../../../TODO.md)。

### Web backend stable baseline（v0.0.22）

在 Step 21 Provider Adapter 之后，对 Web app 层做了一轮加固与 spec 对齐，
形成 **Web backend stable baseline**（v0.0.22）。范围只限 `src/pi_agent_core_py/web/`
+ `harness.py` 的 `remove_on_event_hook` 最小补丁；不动核心 runtime（loop / agent /
context / providers）。

#### 新增 endpoint（spec 对齐）

| Endpoint | 用途 |
|----------|------|
| `GET /api/sessions` | 兼容 spec 复数路径，返回 `{count, sessions:[当前 session]}` |
| `GET /api/mcp/tools` | 兼容 spec，仅返回 MCP tools 列表 + count |
| `WS /ws/events` | 推荐 WebSocket 实时事件通道（每连接 `asyncio.Queue(maxsize=100)`） |
| `GET /api/stream?limit=N` | SSE 测试模式：发完 N 个 event 后正常关闭（`limit=0` 立即关） |

#### 旧 endpoint 兼容

| 旧 endpoint | 状态 |
|-------------|------|
| `GET /api/session`（单数） | ✅ 保留 |
| `GET /api/mcp`（无 `/tools` 后缀） | ✅ 保留 |
| `GET /api/stream`（无限流） | ✅ 默认行为不变（不传 limit 时） |

#### 安全 / 健壮性加固

- `event_buffer` 默认 `maxlen=1000`（deque），可通过 `create_app(event_buffer_max_size=N)` 覆盖
- `include_prompt=true` 默认 **403**；需要 `create_app(allow_prompt_preview=True)` + localhost 才能访问
- `POST /api/prompt` 遇到 `RuntimeError("already running")` / `phase != idle` 返回 **409**（不再 500）
- `create_app(harness)` 多次调用不再累积 hook —— 通过 `lifespan` shutdown + `dispose_app(app)` 精确 `remove_on_event_hook`
- `POST /api/prompt` 仍是同步阻塞（spec 标记为暂缓项）

#### 测试覆盖

`tests/test_integration_web_server.py` 共 25 用例，覆盖：

- spec endpoints（`/api/sessions` / `/api/mcp/tools`）+ 旧 endpoints 兼容
- hook 不重复广播（多次 create_app + dispose_app）
- event_buffer 超限丢最旧 + 默认 maxlen=1000 + 自定义 max_size
- `POST /api/prompt` 在 harness 占用时返回 409（含 state.running + phase + agent.status 三层检查）
- WebSocket `/ws/events` 连接 + 事件接收
- SSE `/api/stream?limit=0` / `?limit=1` 自动化测试不挂住
- prompt preview 默认 403 + allow + localhost 通过
- uvicorn subprocess 启动 + 端口 retry（最多 3 次）

## 完整目录结构（目标态）

```
pi-py/
├── PLAN.md                           # 本文件
├── pyproject.toml
├── README.md
├── CLAUDE.md                         # 项目级最高优先级约定
├── .env                              # 智谱 GLM-5.0 凭证
├── .python-version                   # 3.12
├── steps/                            # 每个 step 一份指导
│   ├── step-01-min-loop/{GUIDE.md, explanations.py}
│   ├── step-02-event-stream/{GUIDE.md, explanations.py}
│   └── ... 共 15 个
├── src/
│   └── pi_agent_core_py/
│       ├── __init__.py
│       ├── types.py                  # ✅ 已有
│       ├── tool.py                   # ✅ 已有
│       ├── tools/echo.py             # ✅ 已有
│       ├── model_client.py           # Step 1 新增
│       ├── event_stream.py           # Step 2 新增
│       ├── context.py                # Step 3 新增
│       ├── tool_registry.py          # Step 4 新增
│       ├── agent_loop.py             # Step 5/7/9 演进
│       ├── tool_hooks.py             # Step 6 新增
│       ├── agent.py                  # Step 8 新增
│       ├── harness.py                # Step 10/13 演进
│       ├── turn_snapshot.py          # Step 11 新增
│       ├── session/                  # Step 12 新增
│       │   ├── session.py
│       │   ├── jsonl_storage.py
│       │   └── uuid_v7.py
│       ├── skills.py                 # Step 14 新增
│       ├── prompt_templates.py       # Step 14 新增
│       └── compaction/               # Step 15 新增
│           ├── compaction.py
│           └── branch_summary.py
├── examples/
│   └── hello.py                      # Step 5 端到端 demo
└── tests/
    └── test_*.py                     # 每个 step 配一份
```

## 关键设计对照（TS → Python）


| TS 版                                   | Python 版                             | 备注                               |
| ----------------------------------------- | --------------------------------------- | ------------------------------------ |
| `EventStream<T,R>`                      | `AsyncIterator[T]` + 协程返回 R       | Step 1/2                           |
| `streamSimple` / `StreamFn`             | `ModelClient` 抽象 + `GLMClient` 实现 | Step 1，直连智谱                   |
| TypeBox schema                          | JSON Schema dict                      | Pydantic`model_json_schema()` dump |
| `AgentTool`                             | `Tool`（已实现）+ `ToolRegistry`      | Step 4                             |
| `beforeToolCall` / `afterToolCall`      | 同名 hook                             | Step 6                             |
| `AbortController`                       | `asyncio.Event`                       | Step 9                             |
| CustomAgentMessages declaration merging | 注册表模式                            | Step 11（暂定）                    |
| Session tree                            | 同                                    | Step 12                            |
| UUID v7                                 | 自实现 RFC 9562                       | Step 12                            |
| Skills YAML frontmatter                 | 同                                    | Step 14                            |

## 进度更新约定

### 每个 step 交付两件套

1. **`demo.py`** — 单文件自包含快照（类型 + Client + Loop + 入口）
2. **`Architecture.md`** — 单一文档，合并了设计文档与使用手册；与 `demo.py` 一一对应

### 三条强约束（违反就重做）

1. **最小原则**：每个 step 只实现该 step 目标所必需的类型/事件/逻辑，不为后续 step 预留。后续 step 在自己的快照里扩展。
2. **快照独立可跑**：`demo.py` 必须**零依赖 src/**。所有类型/Client/Loop/入口全部内联。
   - 验证方法：`mv src _src_bak && /d/miniconda/envs/pipy/python.exe steps/step-XX-name/demo.py && mv _src_bak src`
   - 每个 step 完成时**必须执行此验证**，把输出贴进 Architecture.md §8
3. **代码-文档同步**：**每次改 `demo.py` 都必须同步改 `Architecture.md`**（在 §10 变更日志补一行）。任务标 ✅ 后两者都不可轻易改（除非 bug 补丁）。

### src/ 的角色

- `src/pi_agent_core_py/` 是**演进版**活代码，每个 step 完成时同步该 step 的能力
- 每个 step 完成时 src/ 状态 ≡ 该 step 的 demo.py 快照
- 调用方日常使用 src/（`from pi_agent_core_py import ...`）；step 快照只用于归档与可复现

### 完成一个 step 的检查清单

- [ ]  该 step 的 `demo.py` 单文件跑通（含真实 GLM 调用）
- [ ]  执行 `mv src _src_bak` 验证独立性，把输出贴进 Architecture.md §8
- [ ]  同步 `src/pi_agent_core_py/` 的对应模块
- [ ]  Architecture.md 包含 §1–§10 全部章节，§10 变更日志记本次改动
- [ ]  写 `tutorial.md`（面向"会 Python 但不熟 LLM/agent 的大学生"；不写类比；与 Architecture.md 互补：Architecture 是参考手册，tutorial 是教学引导）
- [ ]  在 PLAN.md 进度表把 ☐/🔄 改成 ✅
- [ ]  task 列表更新状态

### Architecture.md 章节模板（14 节）

```
1.  当前阶段目标              —— Step N-1 vs Step N 的对比；本 step 的核心转换
2.  当前实现范围              —— 已实现 / 不实现 两份清单
3.  消息类型（或对应领域类型）   —— 表格列出每个类型的作用
4.  ModelClient 与 StreamEvent —— provider 抽象与底层事件
5.  AgentEvent / 新增事件模型   —— 每类事件单独一节（5.1、5.2、…）
6.  run_xxx_loop 设计         —— 核心 async generator 的职责清单
7.  标准事件顺序              —— 必须保持稳定的时序
8.  正常响应流程              —— ASCII 时序图
9.  错误响应流程              —— 错误情况下的事件闭环
10. 兼容入口的作用            —— 例如 run_min_loop 这样的旧封装
11. 当前架构图                —— 端到端 ASCII 图
12. 当前模块边界              —— src/ 拆分建议（每个 .py 一行职责）
13. Step N 验收标准           —— 编号清单 + 建议测试项
14. Step N 小结              —— 本 step 的本质 + 为后续 step 铺什么路
```

每节用 `## N. 标题` 二级标题；子节用 `### N.M 标题` 三级标题。
代码块统一用 ```text 包裹（不需要语法高亮，便于复制）。

### 跑测试

```bash
# 单测（不依赖网络）
/d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow"

# 真实 LLM（需 .env 中的 GLM 凭证）
/d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m slow
```

## 命令速查

```bash
# 跑代码 / 测试（永远在 pipy 环境）
/d/miniconda/envs/pipy/python.exe <script>.py
/d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow"

# 讲义验证（每个 step 的 explanations.py）
/d/miniconda/envs/pipy/python.exe steps/step-XX-name/explanations.py

# 重装（改了 pyproject 后）
/d/miniconda/envs/pipy/python.exe -m pip install -e ".[dev]"
```

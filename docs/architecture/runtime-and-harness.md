# Runtime and Harness

> Core runtime 架构。本文档描述 Message / Event / Context / State 的关系，Agent loop，Harness 生命周期，以及 Tool / Skill / MCP 接入层。
>
> 范围：`src/pi_agent_core_py/` 中除 `web/` 外的所有模块。

## 1. 数据模型分层

三层消息类型——**严格单向流转**：

```
AgentMessage (业务层)
    ↓ convert_to_llm()
LLMMessage (LLM 边界)
    ↓ provider adapter
provider-native request
```

| 层 | 模块 | 角色 |
|---|---|---|
| `AgentMessage` | `messages.py` | Agent 内部 + 持久化（user / assistant / tool_result / summary / custom） |
| `LLMMessage` | `llm_messages.py` | 传给 `ModelClient.stream` 的入参（user / assistant / toolResult） |
| Stream event | `stream_events.py` | Provider 返回的流式增量（delta / done / error） |
| Agent event | `events.py` | 上层订阅的语义事件（agent_start / turn_start / message_* / tool_* / request_*） |

**关键不变量**：
- `CustomMessage` 不会出现在 `convert_to_llm` 输出里——业务侧自定义类型用于 UI / 调试，被过滤
- `AssistantMessage.content` 可含 `TextContent | ToolCall`——`convert_to_llm` 过滤 `ToolCall`（不发给 LLM）
- 所有模型 Pydantic v2 + frozen（不可变语义，running 期间不会被订阅者 mutate）

## 2. Agent loop

入口：`loop.py::run_event_loop`（async generator）。

```
run_event_loop(agent, prompt, *, tools, hooks, initial_messages)
  ├─ emit agent_start / turn_start
  ├─ loop:
  │    ├─ transform_context (hook)
  │    ├─ convert_to_llm (AgentMessage → LLMMessage)
  │    ├─ ModelClient.stream(...) → 累积 delta → AssistantMessage
  │    ├─ emit message_start / message_update / message_end
  │    ├─ if AssistantMessage.content has ToolCall:
  │    │    ├─ execute tool batch (sequential / parallel)
  │    │    ├─ emit tool_execution_start / tool_execution_end
  │    │    ├─ append ToolResultMessage
  │    │    └─ continue loop（下一轮 LLM 调用）
  │    └─ else: emit turn_end → break
  └─ emit agent_end
```

**Tool execution**：
- `_execute_tool_batch` async generator——按 `ToolDef.execution_mode` 决定 sequential / parallel
- sequential：per-tool 完整闭环（start → execute → end → next tool）
- parallel：`asyncio.as_completed`，按完成序 emit，按 `index` 重排进 ToolResultMessage
- terminate 早停：本批所有工具 `terminate=True` 才生效（不是任一）
- Tool hooks：`before_tool_call` 可阻断 / 改参；`after_tool_call` 可覆盖结果

**错误收敛**：
- 单工具异常 → `is_error=True` 的 ToolResult，loop 不崩
- Hook 异常 → 同上
- Provider 异常 → 包成 `ErrorEvent` 或 `stop_reason="error"` 的 AssistantMessage（不抛到 caller）

## 3. Agent 与 AgentState

`agent.py::Agent` 持有：
- `system_prompt: str`
- `client: ModelClient`（provider 适配层）
- `state: AgentState`（mutable，运行时状态）

`AgentState` 包含：
- `model: AgentModelState`（当前 client 的 secret-free `provider/api/id` 投影）
- `thinking_level`（`off` 到 `max` 的公开配置）
- `messages: list[AgentMessage]`（当前对话历史；message end 即可观察，agent end 终态校准）
- `status`（idle / running / aborting / error）与 queue / request 计数
- `is_streaming`、`streaming_message`、`pending_tool_calls`（运行时瞬态字段）
- `error_message`（最近失败或中止的 assistant turn）与 `last_error`（Agent/subscriber 异常诊断）

`Agent` 持有单 worker 请求队列并暴露 `prompt` / `continue_`、steering、follow-up
和 abort；具体 LLM/tool turn 逻辑仍在 `loop.py`，Hook 调用在 loop 内。Harness
在外层编排请求 hooks、Snapshot、Skill/MCP 与持久化。

## 4. Harness

`harness.py::AgentHarness` 是 Agent 的编排层，对外是同步外观：

```
AgentHarness(agent)
  ├─ attach_skills(skills)
  ├─ attach_mcp_servers(servers)
  ├─ run_prompt(text, *, skill_selection, file_ids) → snapshot
  ├─ run_continue() → snapshot
  ├─ abort() → bool
  ├─ steer(text)
  ├─ follow_up(text)
  ├─ wait_for_idle(timeout)
  ├─ on_event_hooks / on_event(handler)
  └─ detach_mcp_servers()
```

**生命周期**：
- 单进程内**单 harness 实例**——`web/app.py` 持有一个，所有 session 共享
- harness 不是线程安全——同一时刻只能跑一个 prompt / continue
- abort 通过 `agent.state.phase = aborting` + signal 通知 loop；loop 在下个 yield 点退出

**Snapshot**：每次 prompt / continue 保存一个 `RequestSnapshot`；内部的
`turns[]` 每项对应一次真实 LLM 调用及其当批工具，保存该 turn 的前后消息、
assistant 输出、工具调用/结果和事件切片。

`harness.last_snapshot` 永远是最近一次 request；历史 RequestSnapshot 由 Web 层
写入 SQLite，旧版无 `turns` 的记录仍可读取。

**MCP 生命周期**：
- `attach_mcp_servers`：stdio transport 启动 + register tools into `ToolRegistry`
- `detach_mcp_servers`：cleanup transport + unregister tools
- attached 状态由 web 层持久化（`desired_enabled`）；attached 本身是 runtime 派生

## 5. Tool / Skill / MCP 接入层

### Tool

`AgentTool` 抽象基类：
- `name` / `description` / `parameters_schema`（JSON Schema）
- `execution_mode: "sequential" | "parallel"`（默认 parallel）
- `async execute(tool_call, context) → ToolResult`

注册到 `ToolRegistry`——loop 通过 `tool_call.name` 查找。

内置工具：
- `tools/list_files.py` / `tools/view_file.py`（Web 文件查看）
- `tools/web_search.py`（可选）

### Skill

`skills.py::Skill` 数据模型 + `skill_loader.py` 加载器：
- `metadata`: name / description / source / persisted 等
- `body`: prompt 模板文本
- 来源：filesystem / built-in / uploaded / MCP prompts

**Skill 不是 tool**——Skill 注入到 system prompt（"Use these skills when relevant"），不直接执行。Skill 的"调用"靠 LLM 在回答中提及 skill name + harness 记录到 `SkillUsedCard`。

### MCP

`mcp/` 模块：
- `client.py`：MCP client（stdio transport）
- `adapter.py`：把 MCP tool spec 转成 `AgentTool`
- `registry.py`：管理多 MCP server + 多 tool
- `transport.py`：stdio JSON-RPC 实现
- `prompts.py`：MCP prompts 转 Skill

MCP 工具被 register 进同一个 `ToolRegistry`——loop 层透明，不区分 built-in / MCP。

**env 安全模型**：MCP server config 中 env value **永不持久化**（只存 `env_keys`）；运行时从 `os.environ` 读取。

## 6. Provider Adapter

`providers/` 模块：
- `base.py::ProviderAdapter` 抽象：`stream(ProviderRequest) → AsyncIterator[StreamEvent]`
- `anthropic_compat.py`：Anthropic Messages API（GLM 默认）
- `fake.py`：测试用脚本化回放
- `glm.py`：智谱 GLM 兼容 shim

`ModelClient` 是 `ProviderAdapter` 的同步外观——业务层调用 `client.stream(messages, tools)`，adapter 翻译成 provider-native 请求。

**多 provider**：当前默认 GLM-5.0；未来扩展 OpenAI / Claude 直接调用——通过 `ModelClient` 抽象层切换。

## 7. Core Runtime 与 Web App 边界

| 关注点 | Core Runtime | Web App |
|---|---|---|
| 消息持久化 | ❌ | ✅ `session_sqlite.py` |
| Snapshot 持久化 | ❌ | ✅ SQLite sessions 表 |
| HTTP API | ❌ | ✅ `web/app.py`（4400+ 行） |
| 异步任务管理 | ❌ | ✅ Request registry（内存态） |
| MCP/Skill 持久化 | ❌ | ✅ `web/extension_store.py` |
| Provider 选择 | ✅ `ModelClient` | ❌ |
| Loop / Tool 执行 | ✅ `loop.py` | ❌ |
| AgentState | ✅ `agent.py` | ❌ |

**Web 层禁止修改 core runtime**——P1-D2 freeze 验证（`git diff` 零修改 loop/agent/context/providers/events/stream_events/mcp/tools/skill_loader）。

## 8. 相关文档

- [Web Request Lifecycle](web-request-lifecycle.md)——async prompt 流程
- [Persistence and Startup](persistence-and-startup.md)——SQLite schema + restore
- [Regenerate Revision Model](regenerate-revision-model.md)——regenerate 非破坏性切换

# TODO — pi-agent-core-py

> 进度索引：完成项见 [PLAN.md](PLAN.md)。本文件记录**已完成 milestone**与**下一步待办**。

## ✅ 已完成

### Step 1 — 最小可运行 Loop（2026-06-24）

- 项目骨架、Message 类型、ModelClient 抽象、GLMClient / FakeClient
- `run_min_loop()` 单轮 chat 闭环
- AgentMessage / LLMMessage 边界（暂未引入）
- 单文件自包含快照 + Architecture.md

### Step 2 — Event Stream（2026-06-24）

- 7 种 AgentEvent：`agent_start/end`、`turn_start/end`、`message_start/update/end`
- `run_event_loop()` async generator
- 错误流仍然完整收敛
- 标准事件顺序稳定

### Step 3 — Context 转换（2026-06-24）

- `AgentMessage` union（UserMessage | AssistantMessage | CustomMessage）
- `LLMMessage` union（LLMUserMessage | LLMAssistantMessage）
- `CustomMessage` 类型，`display: bool`
- `convert_to_llm()` 边界：默认过滤 CustomMessage
- `transform_context()` 钩子（默认透传）
- `ModelClient.stream` 入参改为 `list[LLMMessage]`

### Step 4 — Tool 基础模型（2026-06-24）

- `ToolCall` content block（含 `id` / `name` / `arguments` / `raw` 字段；`raw` 留 provider 原始数据）
- `AssistantMessage.content` 类型层接受 `TextContent | ToolCall`
- `convert_to_llm` 过滤 `AssistantMessage.content` 里的 ToolCall（不发给 LLM）
- `ToolExecutionMode = Literal["sequential", "parallel"]`
- `ToolDef` / `ToolResult`（含 `tool_call_id` / `name` / `content: list[TextContent]` / `is_error` / `terminate` / `details`）
- `AgentTool` 抽象基类（含 `execution_mode` 字段，默认 `"parallel"`）
- `ToolRegistry`：`register`（校验 `name` 与 `description` 非空） / `unregister` / `get`（抛 `ToolNotFoundError`） / `has` / `names` / `list` / `definitions`
- `ToolRegistrationError` / `ToolNotFoundError` 异常
- **不引入 ToolResultMessage**（留给 Step 5）
- **不接入 run_event_loop，demo 不调用 `AgentTool.execute()`**（留给 Step 5）

---

## 🔄 下一步待办

### Step 5 — 单工具执行（2026-06-24）

- `ToolResultMessage`（含 tool_call_id / name / content / is_error / terminate / details）
- `LLMToolResultMessage`（不含 terminate / details——LLM 边界）
- `ToolCallEvent` StreamEvent
- `ToolExecutionStartEvent` / `ToolExecutionEndEvent` AgentEvent
- `Message` / `AgentMessage` / `LLMMessage` union 都加入 ToolResultMessage / LLMToolResultMessage
- `convert_to_llm` 处理 ToolResultMessage → LLMToolResultMessage
- `ModelClient.stream` 加 `tools: list[ToolDef] | None` 参数
- `FakeClient` 加 `last_messages` / `last_tools` / `all_messages_calls` / `all_tools_calls`
- `GLMClient` 接受 tools 参数（转 Anthropic 协议；不解析 tool_use 事件）
- `run_event_loop` 加 `tools` 参数；识别 ToolCallEvent → 单工具执行
- 错误兜底：ToolNotFoundError / Exception → is_error=True 的 ToolResult，loop 不崩
- terminate=True 时不再发起下一轮 LLM 调用，事件序列完整收敛
- **不实现**：多工具 batch / sequential / parallel / hooks / Agent / Harness / Session

---

### Step 6 — Tool Hooks（2026-06-24）

- `BeforeToolCallContext` / `BeforeToolCallResult`
- `AfterToolCallContext`
- `BeforeToolCallFn` / `AfterToolCallFn`（async Callable）
- `default_before_tool_call`（放行）/ `default_after_tool_call`（透传）
- `run_event_loop` 新增 `before_tool_call` / `after_tool_call` 参数
- `run_min_loop` 同步加 hook 参数
- `_execute_tool_with_hooks` 替换 Step 5 的 `_execute_tool_safely`
- before：阻断（allow=False）/ 改参（tool_call 非 None）/ 抛异常 → error ToolResult
- after：覆盖 content / details / is_error / terminate / 抛异常 → error ToolResult
- hook 不新增 AgentEvent——结果体现在 ToolResultMessage.details
- before 一定被调（即使 tool=None）；after 只在 execute 成功后被调
- 仍然单工具；不实现多工具 batch / sequential / parallel

---

### Step 7 — 多工具执行（2026-06-25）

- `ExecutedToolResult`（index + tool_call + result + message dataclass）
- `_BatchDone` 内部哨兵——用作 `_execute_tool_batch` 的"返回值"
- `_should_run_sequential`：batch 中任一**已注册**工具 sequential → 整批 sequential
- `_exec_one_tool`：单工具执行 + 包 ExecutedToolResult（不发事件）
- `_execute_tool_batch`：async generator，按模式 yield 事件，末尾 yield `_BatchDone`
- `_execute_tool_with_hooks`（Step 6）保留，作为 batch 内部执行单元
- `run_event_loop` 移除"多 ToolCall 显式拒绝"分支，接入 batch
- **terminate 早停规则**：本批所有工具 terminate=True 才生效（不是任一）
- parallel 模式用 `asyncio.as_completed`；按完成序 emit `tool_execution_end`
- `ToolResultMessage` 按 `index` 重排——保证 `new_messages` 顺序稳定
- Step 5/6 测试归档到对应 step 目录（约定：`tests/` 只放当前 src/ 行为测试）
- 测试 13 个：覆盖 parallel / sequential / 完成序 ≠ message 序 / hooks per-tool / 错误隔离 / terminate batch / Step 5/6 回归
- **bugfix**：sequential 模式原来先批量 yield 所有 `tool_execution_start`，与文档约定的"每工具完整闭环"矛盾。修复：移入 sequential 分支的 per-tool 循环
- **bugfix**：demo.py 漏写 `run_min_loop`；补回

---

### Step 8 — Agent 状态机（2026-06-25）

- `AgentStatus = Literal["idle", "running", "error"]`
- `AgentState`（Pydantic）：status / messages / last_event / last_error / turn_count
- `Subscriber = Callable[[AgentEvent, AgentState], object | Awaitable[object]]`
- `Agent` 类：subscribe / prompt / continue_ / reset / wait_for_idle
- 内部 `_handle_event`：AgentEndEvent 把 event.messages 拷回 state；TurnEndEvent 计数 +1
- 内部 `_emit`：派发给所有 subscriber；单个抛错写 state.last_error，不让 loop 崩
- running 时再 prompt 抛 RuntimeError（不排队；Step 9 加 queue）
- `run_event_loop` 加 `initial_messages` 参数（向后兼容；用于 continue_）
- Step 7 测试归档到 `steps/step-07-multi-tool/`
- 16 个测试覆盖用户清单 16.1–16.15 + Agent error 态

---

### Step 9 — Queue / Abort（2026-06-26）

- `AgentStatus += "aborting"`（idle / running / aborting / error）
- `AgentState` 加 queue_size / current_request_id / aborted_count
- `AgentRequest`（dataclass：id / type / user_text / future / created_at）
- 4 个新 AgentEvent：RequestQueuedEvent / RequestStartEvent / RequestEndEvent / AgentAbortEvent
- queue worker（`_run_queue_worker`）：单一 asyncio.Task 串行处理 queue
- `prompt / continue_`：入队后 `await req.future`
- `abort(reason)`：set `_abort_signal`；不强制 cancel task
- run_event_loop 加 `signal` 参数 + 4 个检查点 + `_finalize_abort`
- `_execute_tool_batch` 加 signal；`_exec_one_tool` / `_execute_tool_with_hooks` 加 4 检查点
- Step 8 测试归档到 `steps/step-08-agent-state/`
- **bugfix**：`_enqueue` 把 `_idle_event.clear()` 提前到 `put` 之前（race）
- **bugfix**：`abort()` 只在 `status == "running"` 时触发（不重复计数）
- 15 个测试覆盖用户清单 18.1–18.14 + stream 期间 abort

---

### Step 10 — Harness Phase（2026-06-26）

- `HarnessPhase = "idle" | "before_request" | "running" | "after_request" | "error"`
- `HarnessContext`（Pydantic + arbitrary_types_allowed）：phase / agent / request_type / user_text / messages_before / messages_after / last_event / last_error / metadata / started_at / ended_at
- 4 类 hook：BeforeRequestHook / AfterRequestHook / OnEventHook / OnErrorHook（兼容 sync/async，用 `inspect.isawaitable`）
- `AgentHarness` 类：
  - 构造时 `agent.subscribe(self._handle_agent_event)` 观察 AgentEvent
  - `run_prompt(text) / run_continue()`：phase 5 态转换 + before/after hooks
  - `abort(reason) / wait_for_idle() / close()`：转发给 Agent
- 主流程 `_run_one`：before → running → after → idle；任一异常进 error 态 + on_error + re-raise
- on_event 异常**不传给 Agent**；写 last_error + 跑 on_error
- on_error 异常**追加 metadata["on_error_errors"]**，不递归
- close() 幂等
- **Harness 不实现自己的 queue**——并发 run_prompt 直接抛 `RuntimeError("Harness is already running")`
- Step 9 测试归档到 `steps/step-09-queue-abort/`
- 18 个测试覆盖用户清单 16.1–16.16 + Agent 异常进 error 态 + close 幂等

---

### Step 11 — Turn Snapshot（2026-06-26）

- `SnapshotStatus = Literal["running","completed","aborted","error"]`
- `EventSnapshot`（index + type + timestamp + event dict）
- `ToolCallSnapshot`（id / name / arguments / raw / timestamp）
- `ToolResultSnapshot`（tool_call_id / name / content / is_error / terminate / details / timestamp）
- `TurnSnapshot`：id / request_id / request_type / user_text / status / error /
  started_at / ended_at / duration_ms / messages_before / messages_after /
  events / tool_calls / tool_results / metadata
- `TurnSnapshot.to_dict() / to_json(indent)`
- `SnapshotBuilder.start / observe_event / finish`：单实例贯穿一次请求
- `HarnessContext.snapshot` 字段
- `AgentHarness.last_snapshot / snapshots / get_snapshot(index) / clear_snapshots()`
- `_run_one` 集成：每个异常路径都 finish snapshot；正常路径根据 `seen_aborted` 决定 status
- `_handle_agent_event`：observe_event **先于** on_event hooks；on_event 异常写入
  `metadata["event_errors"]`
- status 判定：异常 > aborted > completed；工具 is_error 不影响 lifecycle status
- Step 10 测试归档到 `steps/step-10-harness-phase/test_step_10_harness.py`
- 18 个测试覆盖用户清单 17.1–17.15 + agent_call 异常 + 类型冒烟 + Builder 独立冒烟

---

### Step 12 — Session Memory（2026-06-27）

- `SessionState`（Pydantic BaseModel）：id / title / created_at / updated_at /
  messages (list[dict]) / snapshots (list[dict]) / turn_count / metadata
- `SessionMemory`：包装 SessionState，提供 append / restore / clear / 序列化 / metadata / title
- `SessionStore` 抽象（async save / load / exists）
- `InMemorySessionStore`（dict + 深拷贝）
- `JsonFileSessionStore`（`{root}/{id}.json`，自动创建 root_dir，session_id 严格校验）
- 序列化辅助：`serialize_message(s)` / `deserialize_message(s)` / `deserialize_snapshot`
- `deserialize_message` 按 role 派发（user/assistant/toolResult）；custom role 抛 ValueError
- `harness.session` 字段；`attach_session(session, restore_messages=True)`
- `attach_session` 在 Agent running/aborting 时抛 RuntimeError
- `detach_session` 返回旧 session（不回滚 Agent messages）
- `_finish_snapshot` 末尾自动 `session.append_snapshot(snapshot)`——
  completed / aborted / error 三类 snapshot 都进 session
- `save_session(store)` / `load_session(store, id)` 便利方法；store 不强塞进构造函数
- session_id 校验：`^[A-Za-z0-9_.-]{1,128}$` + 显式拒绝 `..`（防路径穿越）
- Step 11 测试归档到 `steps/step-11-turn-snapshot/test_step_11_snapshot.py`
- 44 个测试覆盖用户清单 20.1–20.20 + 5 个额外（运行中拒 attach / run_continue /
  序列化往返 / role 拒绝 / from_state）

---

## 🔄 下一步待办

### Step 13 — Harness + Session 整合增强（2026-06-27）

- `SessionAutoSavePolicy`：never / after_snapshot / after_success / after_any_request
- `SessionSyncConfig`：auto_save_policy / sync_harness_metadata / sync_agent_messages / strict_consistency
- `SessionConsistencyIssue` / `SessionConsistencyReport`：6 项一致性检查
- `ISSUE_*` 常量：no_session / turn_count_mismatch / messages_mismatch / last_snapshot_mismatch /
  agent_session_messages_mismatch / harness_session_snapshot_mismatch
- `AgentHarness.session_store` / `session_sync_config` / `last_consistency_report` 字段
- `configure_session_sync(**kwargs)`：partial update（None 字段保持原值）
- `attach_session` 支持 store + auto_save_policy 入参
- `sync_session_metadata`：写入 `session.metadata["harness"]`，不覆盖顶层
- `sync_agent_messages_to_session`：兜底同步 messages
- `check_session_consistency` 6 项检查；strict 模式抛 RuntimeError
- `export_session` / `import_session`：JSON 文本接口（不依赖 store）
- `_finish_snapshot` 现在返回 snapshot；`_post_finish_snapshot` 做 sync + consistency + auto-save
- auto-save 失败不覆盖原始异常；追加到 `metadata["post_finish_errors"]`
- `save_session(store=None)` 支持用 configure 过的 store
- `SessionMemory.clone()` / `export_json()` / `import_json()` 别名
- `InMemorySessionStore` save/load 走 `copy.deepcopy`
- Step 12 测试归档到 `steps/step-12-session-memory/test_step_12_session.py`
- 31 个测试覆盖用户清单 21.1–21.20 + 11 个额外场景

---

## 🔄 下一步待办

### Step 14 — Skills / Prompt Templates（2026-06-27）

- `PromptTemplate` / `PromptTemplateRenderError`：基于 `str.format()` 的轻量模板；
  缺 `variables` 中声明的变量时抛 PromptTemplateRenderError
- `SkillStatus` / `Skill`：name + description + prompt（str | PromptTemplate）+
  status / priority / tags / tool_names / metadata
- `Skill.render_prompt(values)`：str 直接返回；PromptTemplate 调 .render
- `SkillRegistrationError` / `SkillNotFoundError`
- `SkillRegistry`：register（name/description/prompt 非空 + 不重复）/
  unregister（不存在静默）/ get / has / list / names /
  enabled / disabled / enable / disable / select
- `SkillRegistry.select(names=, tags=, enabled_only=True)`：交集式过滤 +
  (priority, name) 升序排序；names 含不存在元素抛 SkillNotFoundError
- `SkillSelection`：names / tags / values（单次请求级）
- `SkillInjectionConfig`：enabled / section_title / include_descriptions /
  include_tool_names / separator
- `render_skill_block(skills, values, config)`：输出 `## Skills\n\n### name\n...` 格式
- Harness 字段：`skill_registry` / `skill_injection_config`
- HarnessContext 字段：`selected_skills` / `skill_metadata` / `rendered_system_prompt`
- `attach_skills(skills | registry, injection_config=)` / `detach_skills()`
- `enable_skill(name)` / `disable_skill(name)`：
  无 registry 抛 RuntimeError；missing 抛 SkillNotFoundError
- `render_system_prompt(skill_selection=)`：base + skill block；不修改 agent.system_prompt
- `run_prompt(text, *, skill_selection=)` / `run_continue(*, skill_selection=)`
- `_run_one` 中 try/finally **临时替换** `agent.system_prompt`——请求结束立即还原
- 渲染失败（PromptTemplateRenderError / SkillNotFoundError）作为 **before-agent 错误路径**：
  Agent 一次都不调用；生成 error snapshot；session 自动 append
- skill 信息写 `context.metadata["skills"]` → 流转 snapshot / session（通过 Step 11/13）
- `Skill.tool_names` 只是 metadata——**不自动注册工具**
- FakeClient 加 `last_system_prompt` / `all_system_prompt_calls` 记录
- Step 13 测试归档到 `steps/step-13-harness-session/test_step_13_harness_session.py`
- 44 个测试覆盖用户清单 19.1–19.27 + 5 个额外场景

---

## 🔄 下一步待办

### Step 15 — Compaction / Branch Summary（2026-06-27）

- `SummaryMessage` 加进 `Message` / `AgentMessage` union；role="summary"
- `SummaryType = Literal["context_compaction", "branch_summary"]`
- `_MESSAGE_ROLE_MAP["summary"] = SummaryMessage`——反序列化支持
- `convert_to_llm`：SummaryMessage → LLMUserMessage("[Conversation Summary]\n...")
  ——不引入新 LLM role；保持 LLMMessage 模型不变
- `CompactionConfig` / `CompactionSource` / `CompactionInput` / `CompactionResult`（含 to_dict）
- `BranchSummary` / `BranchSummaryConfig`——旁路记录
- `SummaryGenerator = Callable[[CompactionInput], str | Awaitable[str]]`——sync/async
- `default_summary_generator`：确定性 extractive summary（不调 LLM / 不联网）
- `compact_messages()`：核心函数；不修改入参，只返回 CompactionResult
- `create_branch_summary()`：核心函数；不修改 Agent / Session messages
- `SessionState` 加 `compactions` / `branch_summaries` 字段
- `SessionMemory` 加 `append_compaction` / `get_compactions` /
  `append_branch_summary` / `get_branch_summaries` / `clear_branch_summaries`
- `SessionMemory.clear()` 不动 compactions / branch_summaries——历史记录
- Harness 字段：`last_compaction_result` / `last_branch_summary`
- `compact_context`：以 agent.state.messages 为权威；running 时拒绝
- `compact_session`：以 session.messages 为权威；未 attach 抛 RuntimeError
- `create_branch_summary`：基于 session 或 agent 上下文，不改 messages
- `get_branch_summary(index=-1)` / `clear_branch_summaries()`
- `_maybe_save_after_compaction`：复用 Step 13 auto-save policy
- compaction applied 时 agent + session messages 同步
- compaction **不删 snapshots**——历史记录永久保留
- custom summary_generator 必须返回 str；否则 TypeError
- Step 14 测试归档到 `steps/step-14-skills-templates/test_step_14_skills.py`
- 29 个测试覆盖用户清单 26.1–26.20 + 9 个额外场景

---

## 🎉 15-step 框架全部完成

后续可选方向（不在原 15 step 范围）：

### Vector Memory / RAG

- 嵌入式检索长期记忆；跨 session 知识检索
- 向量库（FAISS / Chroma / 远程服务）+ 嵌入模型
- 检索结果作为 SummaryMessage 或 CustomMessage 注入上下文

### Branch Restore / Fork

- 从 branch_summary 或 snapshot 派生新 session
- session fork 树状管理（main / research-a / debug-branch）
- 切换分支时自动 attach / detach

### Multi-Agent Handoff

- 跨 Agent 协作；任务在 Agent 间传递
- AgentGraph / Supervisor 模式
- HandoffEvent / HandoffMessage

### CLI / Web Server

- 命令行交互入口（python -m pi_agent_core_py chat）
- Web Server（FastAPI + WebSocket）多用户隔离
- 前端 UI（snapshot 可视化 / session 浏览 / skill 管理）

### 自动压缩策略

- 监听 message_count 自动触发 compact_context
- 阈值配置：token 估算 / message count / 时间间隔
- 后台 task vs 同步触发

### 真 LLM 摘要器

- default_summary_generator 的 LLM 版本（基于 ModelClient）
- prompt template：让 LLM 输出结构化 markdown 摘要
- 缓存：同一组 messages 不重复调 LLM

### Skills 文件加载器

- 从 `skills/{name}/SKILL.md`（YAML frontmatter）自动构造 Skill
- 热加载（文件变更触发 registry 更新）
- 跨项目共享 skill 库

### 异步 IO

- JsonFileSessionStore 换 aiofiles
- 高并发场景不阻塞 event loop

---

## ✅Step 16 — MCP Tools + Tool Validation（2026-06-30）

**MCP 工具接入 + 工具参数 JSON Schema 校验。** 在 Step 5 工具执行链路上接
入 MCP 协议，让 agent 能调用任意远端 MCP server 暴露的工具。

- `src/pi_agent_core_py/mcp/` 子包（7 个模块）：
  - `config.py`：`MCPServerConfig`（name 字符集 / stdio command / http url 校验）
  - `errors.py`：6 个异常（MCPError 基类 + Connection / Protocol / ToolNotFound / ToolCall / TransportClosed）
  - `transport.py`：`MCPTransport` 抽象 + `StdioMCPTransport`（JSON Lines over subprocess）+ `HttpMCPTransport`（占位 raise NotImplementedError）+ `FakeMCPTransport`（测试用）
  - `client.py`：`MCPClient` + `MCPToolInfo` + `MCPCallResult`；JSON-RPC 2.0；inputSchema/isError 字段兼容；非 text content 占位说明
  - `adapter.py`：`MCPAgentTool`——MCP tool → AgentTool 适配；name=`mcp__{server}__{tool}`；execute 边界兜底所有异常
  - `registry.py`：`MCPRegistry` + `MCPServerState`；单 server 失败不影响其它；close_all 幂等
- `src/pi_agent_core_py/tool_validation.py`：`ToolArgumentValidationError` + `validate_tool_arguments`（用 `jsonschema.Draft7Validator`）
- `loop.py` 接入校验：`_execute_tool_with_hooks` 第 5.5 步——registry.get → before → 改名重查 → **validate_tool_arguments** → execute → after；校验失败不调 execute/after，包成 is_error ToolResult
- 18 个新公共类型从 `pi_agent_core_py` 顶层导出
- 依赖：`pyproject.toml` 加 `jsonschema>=4`
- 5 个测试文件 / 55 用例（tool_validation 11 / mcp_client 15 / mcp_adapter 13 / mcp_registry 8 / loop_integration 8）
- 全部**离线**（FakeMCPTransport handler 模式，无真实子进程）
- step-16 五件套：demo.py（单文件自包含）+ Architecture.md（14 节）+ tutorial.md + GUIDE.md
- 全套测试 **144 passed**（原 90 + 新 54），零回归
- **不实现**：MCP resources / MCP prompts / Harness 接管 / 工具权限沙箱 / 网页端 / RAG / Multi-Agent / HTTP transport 完整实现

---

## ✅ Step 17 — MCP Harness Integration（2026-06-30）

**让 `AgentHarness` 管理 MCP server 生命周期**，自动把 MCP tools 注册进
Agent ToolRegistry。Step 16 让调用方手工 connect/close；Step 17 让 Harness
统一管。

- `MCPRegistry.__init__` 加 `client_factory` 参数（测试友好，注入 FakeMCPTransport）
- `AgentHarness` 新增字段：`_mcp_registry` / `_mcp_tool_names: set[str]`
- `AgentHarness.attach_mcp_servers(configs, *, auto_register_tools=True, registry=None)`
- `AgentHarness.detach_mcp_servers()`（幂等）
- `AgentHarness.refresh_mcp_tools() -> list[MCPAgentTool]`
- `AgentHarness.list_mcp_servers() / list_mcp_tools() / mcp_registry property`
- `_register_mcp_tools_into_agent()`：unregister 旧 → register 新（防 stale）
- `_build_mcp_metadata() / _inject_mcp_metadata()`：JSON-safe 状态进 context.metadata["mcp"]
- `_run_one` 开头注入 MCP metadata → snapshot / session 自动捕获
- **`AgentHarness.close()` 改为 async**（breaking change）：先 detach MCP，再 unsubscribe Agent
- 单 server 失败不影响其它（MCPRegistry 已处理 + Harness 透传）
- 25 个测试用例（3 个文件：harness / lifecycle / integration）
- 全部离线（FakeMCPTransport handler 模式）
- step-17 五件套：demo.py + Architecture.md(17 节) + tutorial.md
- 全套测试 **213 passed**（188 原有 + 25 新增），零回归
- **不实现**：MCP resources / MCP prompts / 工具权限审批（Step 18）/ Web UI / RAG / Long-term Memory

**Breaking change**：`harness.close()` 从 sync 改 async。调用方需 `await harness.close()`。
归档目录 `steps/step-10-harness-phase/` 的旧 sync 调用不在默认 CI 范围。

---

## ✅Step 18 — Permission / Approval Policy（2026-06-30）

**给工具调用加第一道安全门：默认安全 + 显式 allow/deny + 路径沙箱 + 审计。**

新增模块：

```text
src/pi_agent_core_py/policy/
├── __init__.py
├── permissions.py     # ToolPermissionDecision / ToolPermissionPolicy /
│                      # AllowAll / DenyAll / DefaultToolPermissionPolicy /
│                      # parse_mcp_namespaced_tool
├── sandbox.py         # is_path_within_roots / extract_candidate_paths
└── audit.py           # ToolPermissionAuditRecord / InMemoryToolPermissionAuditLog
```

实施清单：

- `ToolPermissionDecision`（allow / deny / require_approval + reason / policy_name / metadata）
- `ToolPermissionPolicy` 抽象（async check_tool_call）
- `AllowAllToolPermissionPolicy` / `DenyAllToolPermissionPolicy`（测试 / 开发）
- `DefaultToolPermissionPolicy`：
  - 显式 deny 优先（denied_tools / denied_mcp_servers / denied_mcp_tools）
  - 显式 allow 次之（allowed_tools / allowed_mcp_servers / allowed_mcp_tools）
  - read-only 工具名 / MCP 前缀默认 allow
  - 高风险关键字（write / delete / shell / network / sql）默认 require_approval
  - path sandbox：写类必须有 workspace_roots；`..` 逃逸拒绝
  - 读类宽松：roots 空也 allow，metadata 标 path_unchecked=True
- `parse_mcp_namespaced_tool`：mcp__server__tool → (server, tool)
- `is_path_within_roots` / `extract_candidate_paths`：用 Path.resolve 防 `..` 逃逸
- `ToolPermissionAuditRecord` / `InMemoryToolPermissionAuditLog`（append / list / clear / counts）
- `loop.py` 第 4.5 步接入：before_tool_call → 改名重查 → **permission** → validation → execute
- policy deny / require_approval / 抛异常都不让 loop 崩
  - error_type: ToolPermissionDenied / ToolApprovalRequired / ToolPermissionPolicyError
- `Agent` 持有 permission_policy / permission_audit_log（透传给 loop）
- `AgentHarness`：
  - 构造接收 policy / audit_log（不传则用默认空 InMemoryToolPermissionAuditLog）
  - `set_permission_policy(policy)`（含 None 关闭）+ 同步到 agent
  - `list_permission_audit_records()` / `clear_permission_audit_records()`
  - `_inject_policy_metadata()`：写 summary 进 context.metadata["policy"]
    （policy_name / audit_count / allowed_count / denied_count / approval_required_count）
  - 在 _run_one 开头 + 每个 finish_snapshot 前刷新
  - snapshot / session.metadata["harness"]["context_metadata"]["policy"] 自动捕获
  - audit records **不全量**塞进 snapshot metadata（避免膨胀）
- 10 个新公共 API 从 `pi_agent_core_py` 顶层导出
- 5 个测试文件 / 58 用例（permission_policy 23 / loop_integration 8 /
  mcp 10 / harness 9 / sandbox 8）
- 全部**离线**（无真实 MCP / LLM）
- step-18 五件套：demo.py（单文件自包含，6 个 demo 场景）+ Architecture.md（15 节）+ tutorial.md
- 全套测试 **257 passed**（199 原有 + 58 新增），零回归
- **不实现**：Web UI（Step 20）/ 真实人工审批 UI（require_approval 按 deny 处理）/
  多用户权限 / OAuth / RBAC / 企业 secret vault / RAG / Long-term Memory /
  MCP resources / 容器级 path 隔离

---

### Step 19 — Skill File Loader + MCP Prompts（2026-06-30）

**把本地 SKILL.md 和 MCP prompts 统一转成 Skill，接入 SkillRegistry 和 AgentHarness。**

新增模块：

```text
src/pi_agent_core_py/
├── skill_loader.py            # SkillLoadConfig / SkillFileLoader / parse_skill_markdown
│                              # SkillFileLoadError / SkillFileFormatError / SkillFileSecurityError
└── mcp/prompts.py             # MCPPromptArgument / MCPPromptInfo / MCPPromptMessage /
                               # MCPPromptResult / MCPPromptSkillAdapter /
                               # make_mcp_prompt_skill_name
```

实施清单：

- `SkillLoadConfig`：root_dirs / allow_frontmatter / default_enabled /
  default_priority / max_file_size_bytes（默认 256 KB）/ allowed_filenames
- `parse_skill_markdown`：frontmatter（yaml.safe_load）+ ## Description +## Instructions + H1 推断；fallback_name 兜底为父目录名
- `SkillFileLoader.load_file / load_dir(recursive=) / load_many`：
  - 文件名 allowlist（默认 SKILL.md / skill.md）
  - 大小限制 / root_dirs 路径沙箱（复用 policy.is_path_within_roots）
  - parse 失败 → SkillFileFormatError；安全失败 → SkillFileSecurityError
  - load_dir 单文件失败 fail-fast
- `MCPClient.list_prompts()` / `MCPClient.get_prompt()`：严格协议校验
  （result 缺字段 / name 非法 → MCPProtocolError）
- `MCPRegistry.refresh_prompts / list_prompts / get_prompt / get_client`：
  - 单 server 失败隔离（state.last_error 记录）
  - close_all / _connect_one / refresh_tools 失败路径同步清 prompts cache
- `MCPPromptSkillAdapter.load_prompt_as_skill / load_all_prompts_as_skills`：
  - Skill name = `mcp_prompt__{server}__{prompt}`（≤ 128 字符）
  - tags = `["mcp", "prompt", server]`
  - metadata 含 source / server / prompt / loader / arguments
  - load_all 单失败吞掉，与 refresh_prompts 失败隔离语义一致
- `AgentHarness.attach_skill_files / attach_skill_dir / refresh_mcp_prompts / attach_mcp_prompts_as_skills`：
  - replace_existing_registry=True / False（追加）
  - attach_mcp_prompts_as_skills 自动 refresh_prompts 保证缓存新鲜
  - 不影响 MCP tools / permission policy
- `context.metadata["skill_loader"]`：`file_skills` / `mcp_prompt_skills`
  的 count + names（不写完整 prompt body）
- 12 个新公共 API 从 `pi_agent_core_py` 顶层导出
- 4 个测试文件 / 92 用例
  - test_step_19_skill_file_loader.py（30 用例）
  - test_step_19_mcp_prompts.py（30 用例）
  - test_step_19_harness_skill_loading.py（18 用例）
  - test_step_19_skill_loader_integration.py（14 用例）
- 全部**离线**（无真实 MCP / LLM）
- step-19 五件套：demo.py（9 个 demo 场景，src 独立验证通过）+
  Architecture.md（17 节）+ tutorial.md
- 全套测试 **342 passed**（266 原有 + 76 新增），零回归
- **不实现**：hot reload（Step 23）/ 跨项目共享 skill 库（Step 23）/
  在线编辑 SKILL.md（Step 20 Web UI）/ RAG / Vector Memory / Long-term Memory /
  MCP resources / MCP notifications / Web UI

---

### Step 20 — Web App / Trace Viewer（2026-07-01）

**Vue 3 + FastAPI 本地调试 Trace Viewer。**

新增模块：

```text
src/pi_agent_core_py/web/
├── __init__.py            导出 create_app / WebAppState / TraceEventBuffer / serializers
├── state.py               TraceEventBuffer (deque maxlen) + WebAppState (Pydantic)
├── serializers.py         to_json_safe + 8 个 serialize_* 函数
├── app.py                 FastAPI create_app + 15 routes + SSE endpoint
├── static/                Vue build 输出（npm run build 写入）
└── frontend/              Vue 3 + Vite + TypeScript 源码
    ├── package.json / vite.config.ts / tsconfig.json / index.html
    └── src/
        ├── main.ts / App.vue / api.ts / types.ts / styles.css / shims-vue.d.ts
        └── components/
            ├── ChatPanel.vue          textarea + Send/Abort/Reset + skill 输入
            ├── EventStream.vue        EventSource('/api/stream') + 列表
            ├── MessageList.vue        role + content 摘要
            ├── SnapshotPanel.vue      summary list + 详情（含负数 index）
            ├── SessionPanel.vue       attached / not attached
            ├── McpPanel.vue           servers / tools / prompts 三表
            ├── SkillsPanel.vue        skills 表 + skill_loader metadata
            ├── PolicyAuditPanel.vue   audit 记录（按 decision 着色）
            └── RawJsonPanel.vue       state pretty JSON
```

实施清单：

- `pyproject.toml` 新增 `[project.optional-dependencies] web = [fastapi, uvicorn]`
  ——核心包不强制依赖 web 框架
- `WebAppState(harness, event_buffer, event_queue, running, last_error)`：Pydantic
  + arbitrary_types_allowed=True
- `TraceEventBuffer`：deque(maxlen=500)，只存 JSON-safe dict，非 dict 静默跳过
- `to_json_safe` 兜底：BaseModel / dict / list / tuple / Exception / repr fallback
- `serialize_message / event / snapshot_summary / snapshot_full / session / mcp_server_state / skill / policy_audit_record`：避免 live object（MCPClient /
  MCPTransport），默认不含 Skill.prompt 正文
- FastAPI 路由（15 个）：state / messages / events / events.clear / snapshots /
  snapshots/{index} / session / mcp / skills / policy/audit / prompt / abort /
  reset / stream
- SSE：每个连接独立 asyncio.Queue；on_event hook 广播到所有客户端；
  heartbeat 15s；客户端断开自动清理
- `AgentHarness.add_on_event_hook(hook)`：Step 20 引入的 hook 注册 API
- Vue build 输出到 `web/static/`；FastAPI 用 FileResponse 托管
- fallback HTML：Vue 未 build 时返回提示，Python tests 不依赖 npm build
- demo 默认 `127.0.0.1:8000`，启动时打印 "Do not expose it publicly" 警告
- 51 个测试用例（serializers 18 + state 8 + app 25），fastapi 缺失时整文件 skip
- 全套 **404 passed**（353 原有 + 51 新增），零回归
- **不实现**：登录 / OAuth / RBAC / 多用户 / 公网部署 / 数据库 / WebSocket /
  Pinia / Vue Router / 在线编辑 SKILL.md / human approval UI / MCP resources /
  RAG / Vector Memory / Long-term Memory

---

## ✅ Post-15 Step 21 — Provider Adapter Refactor（2026-07-02）

把 provider 适配从核心 runtime 拆出来，形成清晰可扩展的 ProviderAdapter 层。

### 落地

- 新增 `providers/` 子包：
  - `base.py`：`ProviderAdapter` 抽象 + `ProviderRequest` 封装
  - `errors.py`：`ProviderError` 子类（Config / Protocol / Authentication / RateLimit / Stream）
  - `fake.py`：`FakeProviderAdapter`——离线测试用，与旧 `FakeClient` 行为一致
  - `anthropic_compat.py`：`AnthropicCompatAdapter` + `to_anthropic_messages` / `to_anthropic_tools`
  - `glm.py`：`GLMProviderAdapter`（继承 AnthropicCompatAdapter，仅覆盖 provider_id）+ `GLMConfig` + `resolve_glm_credentials`
- 抽出 `stream_events.py`：定义 `StreamEvent` 系列（避免 model_client ↔ providers 循环 import）
- 重构 `model_client.py`：
  - `ModelClient` 变 thin wrapper，持有 `adapter`，捕获异常 → `ErrorEvent`
  - `FakeClient` / `GLMClient` 都是兼容 shim，构造对应 adapter 后委托给它
  - 旧 keyword `auth_token=` 保留；新 keyword `api_key=` 推荐
- 环境变量优先级（高 → 低）：
  - api_key：显式 > `GLM_API_KEY` > `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY`
  - base_url：显式 > `GLM_BASE_URL` > `ANTHROPIC_BASE_URL` > 默认智谱端点
  - model：显式 > `GLM_MODEL` > `ANTHROPIC_MODEL` > `glm-4.5-flash`
- 顶层 `__init__.py` 导出新 API：`ProviderAdapter` / `ProviderRequest` /
  `ProviderError` 系列 / `FakeProviderAdapter` / `AnthropicCompatAdapter` /
  `AnthropicCompatConfig` / `GLMProviderAdapter` / `GLMConfig` /
  `to_anthropic_messages` / `to_anthropic_tools`

### 测试

- 新增 6 个测试文件 / 64 个 test case（全部 100% 离线）
- 旧 404 个测试 100% 通过——零回归
- 总测试数：468，coverage 81%

### 文档

- `steps/step-21-provider-adapter-refactor/demo.py`：7 个独立 demo（不调真实网络）
- `steps/step-21-provider-adapter-refactor/Architecture.md`：完整设计文档
- `steps/step-21-provider-adapter-refactor/tutorial.md`：面向大学生的入门教程

### 边界（明确不做）

- ❌ 多 Agent handoff（Step 22 才做）
- ❌ Provider routing / 自动 fallback / retry / backoff 框架
- ❌ 真实 OpenAI 实现（留接口，MVP 不强制）
- ❌ RAG / Vector Memory / Long-term Memory
- ❌ 成本统计 / 模型评测

---

## 📌 本副本范围（Step 21 baseline）

本目录是 `D:\LLMTutorial\pi\pi-py\` 的精简副本——仅保留 **Step 1–21**
（core 15 步 + Phase A MCP / 权限 / Phase B Skill / Web / Phase C Provider）。

**Step 22（Sandbox Execution）/ Step 23（Sandbox Git）/ Step 24（Slash Command）/
Step 25+（Plan Mode / Compaction / Multi-Agent）不在本副本**。

完整路线、完整 TODO、完整进度请看主仓库：

- `D:\LLMTutorial\pi\pi-py\PLAN.md`
- `D:\LLMTutorial\pi\pi-py\TODO.md`
- `D:\LLMTutorial\pi\pi-py\STATUS.md`

测试基线（本副本）：见下方 v0.0.22.1。

---

## ✅ v0.0.21.1 — Bug Audit & Smoke Tests（2026-07-05）

**对已完成的 Step 1–21 代码做系统性 bug 检查 + 修复，不重构架构、不加新功能。**

### 新增 smoke 测试（9 个文件 / 80 个用例）

- `tests/test_smoke_import.py`（24）—— 21 个核心模块独立 import + 顶层 re-export + `__all__` 无重复
- `tests/test_smoke_messages.py`（8）—— Message 基础契约（默认值 / role / 序列化往返）
- `tests/test_smoke_context.py`（10）—— `convert_to_llm` role 映射 / tool_call 过滤 / SummaryMessage / 空 list
- `tests/test_smoke_fake_loop.py`（3）—— FakeClient loop 端到端 + 事件顺序 + async generator 不泄漏
- `tests/test_smoke_events.py`（4）—— 事件时序不变量 + metadata JSON 可序列化 + error 路径
- `tests/test_smoke_single_tool.py`（4）—— 单工具 / 异常 / terminate / 未知工具四路径
- `tests/test_smoke_agent_harness.py`（8）—— Agent 状态机 + Harness 生命周期 + close 幂等
- `tests/test_smoke_snapshot_session.py`（10）—— Snapshot 不可变 + Session 序列化往返 + path traversal
- `tests/test_smoke_bug_audit.py`（11）—— 暴露并验证修复 5 类 bug

### 修复的真 bug

| # | Bug | 根因 | 修复 |
|---|-----|------|------|
| 1 | `__init__.py` 的 `__all__` 中 `SummaryMessage` / `SummaryType` 重复 | messages 段已导出，compaction 段又重复列了一次 | 删 compaction 段重复 |
| 2 | `run_event_loop` 是无界 `while True`，模型持续返回 `tool_call` 时死循环 | 缺 max_turns 安全网 | 加 `max_turns: int = 50` 参数；超限收敛为 `stop_reason="error"` assistant；Agent 透传 |
| 3 | 多轮 tool 调用上下文丢失（Anthropic 协议失败） | `convert_to_llm` 把 `AssistantMessage.content` 里的 `ToolCall` 全部过滤；`LLMAssistantMessage` 没字段保留；`to_anthropic_messages` 因此缺 `tool_use` block，导致后续 `tool_result` 找不到配对 | `LLMAssistantMessage` 加 `tool_calls: list[ToolCall]`；`convert_to_llm` 保留；`to_anthropic_messages` 渲染为 `tool_use` block |
| 4 | `AnthropicCompatAdapter` / `GLMClient` 没暴露 close 方法 | 持有 `AsyncAnthropic`（包 httpx.AsyncClient）但无释放路径 | `AnthropicCompatAdapter.aclose()` 幂等关闭；`ModelClient.close()` 转发；`AgentHarness.close()` 关 ModelClient |
| 5 | 真实 GLM smoke 测试 `await client.close()` 缺失 | 测试侧问题，配合 #4 | `try/finally: await client.close()` |

### 测试结果

| 命令 | 结果 |
|------|------|
| `pytest tests/test_smoke_import.py -v` | 24 passed |
| `pytest tests/ -v -m "not slow and not docker"` | **563 passed, 22 deselected**（481 baseline + 80 smoke + 2 fixed） |
| `pytest -W error::RuntimeWarning -W error::ResourceWarning tests/` | 563 passed（0 ResourceWarning） |
| `ruff check src tests` | All checks passed |

---

## ✅ v0.0.21.2 — Integration Tests + Examples（2026-07-05）

**真实外部链路 smoke 测试（GLM / MCP stdio / Web）+ examples；不改 runtime。**

### 新增 integration 测试（5 个文件 / 22 个 slow 用例）

- `tests/test_integration_glm_text.py`（2）—— 真实 GLM 文本（`run_min_loop` + stream）
- `tests/test_integration_glm_single_tool.py`（1）—— 真实 GLM 单工具 echo（验证 Step 21 修复在真实 Anthropic 协议下成立）
- `tests/test_integration_glm_multi_tool_use.py`（3，2 deterministic + 1 real）—— 多轮 tool_use 配对
- `tests/test_integration_mcp_stdio.py`（5）—— MCP stdio 全链路（initialize / list_tools / call_tool / unknown_tool / agent_tool register + execute / transport close kills subprocess）
- `tests/test_integration_web_server.py`（11 active + 1 skip）—— Web TestClient + uvicorn subprocess
- `tests/fixtures/fake_mcp_stdio_server.py` —— 最小 JSON-RPC stdio MCP server fixture

### 新增 examples（4 个）

- `examples/glm_text_smoke.py` —— 真实 GLM 文本调用
- `examples/glm_tool_smoke.py` —— 真实 GLM 单工具调用
- `examples/mcp_stdio_smoke.py` —— 真实 stdio MCP server
- `examples/web_server_smoke.md` —— uvicorn 启动 + endpoint 列表 + spec 缺失说明

### 真实链路验证结果

| 链路 | 结果 |
|------|------|
| GLM 文本 | ✅ 2/2 PASSED |
| GLM 单工具 | ✅ 1/1 PASSED |
| GLM 多轮 tool_use（deterministic + real） | ✅ 3/3 PASSED |
| MCP stdio | ✅ 5/5 PASSED |
| Web server | ✅ 10/10 PASSED + 1 SKIPPED（SSE 无限流无法 TestClient 自动化） |

无凭证环境下 GLM 测试用 `pytest.mark.skipif` 自动跳过，输出 `_GLM_SKIP_REASON`。

### 测试结果

| 命令 | 结果 |
|------|------|
| `pytest tests/ -m "slow and not docker" -k "glm"` | 6 passed |
| `pytest tests/ -m "slow and not docker" -k "mcp"` | 6 passed |
| `pytest tests/ -m "slow and not docker" -k "web"` | 10 passed, 1 skipped |
| `pytest tests/ -v -m "not slow and not docker"` | 563 passed, 22 deselected |
| `ruff check src tests` | All checks passed |

---

## ✅ v0.0.22 — Web backend stable baseline（2026-07-05）

**对 Web app 层做加固 + spec 对齐。范围只限 `src/pi_agent_core_py/web/` + `harness.py` 的 `remove_on_event_hook` 最小补丁；不动核心 runtime。**

详见 [`docs/RELEASE_NOTES_v0.0.22_WEB.md`](docs/RELEASE_NOTES_v0.0.22_WEB.md)。

### 新增 endpoint（spec 对齐）

| Endpoint | 用途 |
|----------|------|
| `GET /api/sessions` | 兼容 spec 复数路径，返回 `{count, sessions:[当前 session]}` |
| `GET /api/mcp/tools` | 兼容 spec，仅返回 MCP tools 列表 + count |
| `WS /ws/events` | 推荐的 WebSocket 实时事件通道（每连接 `asyncio.Queue(maxsize=100)`） |
| `GET /api/stream?limit=N` | SSE 测试模式：发完 N 个 event 后正常关闭（`limit=0` 立即关） |

### 旧 endpoint 兼容

- `GET /api/session`（单数）—— ✅ 保留
- `GET /api/mcp`（无 `/tools` 后缀）—— ✅ 保留
- `GET /api/stream`（无限流）—— ✅ 默认行为不变

### 安全 / 健壮性修复

- `event_buffer` 默认 `maxlen=1000`（之前 500），可 `create_app(event_buffer_max_size=N)` 覆盖
- `include_prompt=true` 默认 **403**；需 `create_app(allow_prompt_preview=True)` + localhost
- `POST /api/prompt` 遇到 `RuntimeError("already running")` / `phase != idle` 返回 **409**（不再 500）
- `create_app(harness)` 多次调用不再累积 hook —— `lifespan` shutdown + `dispose_app(app)` 精确 `remove_on_event_hook`
- WebSocket 慢客户端 queue 满丢弃，不阻塞全局
- `_ensure_idle` 多查 `harness.context.phase`，覆盖外部直接调 `harness.run_prompt(...)` 场景

### 改动文件

| 文件 | 改动 |
|------|------|
| `src/pi_agent_core_py/harness.py` | 加 `remove_on_event_hook(hook) -> bool` 最小方法 |
| `src/pi_agent_core_py/web/app.py` | 新增 4 endpoint；`create_app` 加 `event_buffer_max_size` / `allow_prompt_preview`；`_ensure_idle` 加 phase 检查；`POST /api/prompt` RuntimeError → 409；`lifespan` + `dispose_app` |
| `src/pi_agent_core_py/web/state.py` | `TraceEventBuffer` 默认 maxlen 500 → 1000 |
| `tests/test_integration_web_server.py` | 25 个用例覆盖 12 项验收 |
| `tests/test_step_20_web_app.py` | `client_with_skill` fixture 加 `allow_prompt_preview=True` 保持原断言 |

### 测试结果

| 命令 | 结果 |
|------|------|
| `pytest tests/test_integration_web_server.py -v` | **25 passed** |
| `pytest tests/ -v -m "not slow and not docker"` | **563 passed, 36 deselected** |
| `ruff check src tests` | All checks passed |

---

## ✅ v0.0.22.1 — 文档更新（2026-07-06）

**记录 v0.0.22 baseline；不改 runtime / 测试逻辑。**

### 新增 / 修改文档

| 文件 | 类型 | 摘要 |
|------|------|------|
| `PLAN.md` | 修改 | 测试基线 481→563；新增 "Web backend stable baseline（v0.0.22）" 小节 |
| `README.md` | 修改 | 重写 `## Web UI` 段：localhost 警告 / `create_app` 新参数 / 完整 REST endpoint 表 / SSE vs WebSocket / prompt preview 默认禁用 |
| `docs/WEB_API.md` | 新增 | 完整 Web API reference：所有 endpoint（含旧 / 新 / spec）的请求参数、返回结构、前端用途、错误码 |
| `docs/WEB_TESTING.md` | 新增 | Web 测试分层图、运行命令、`dispose_app` 使用场景、`event_buffer_max_size` 配置、SSE limit 测试技巧、uvicorn subprocess 端口 retry |
| `docs/RELEASE_NOTES_v0.0.22_WEB.md` | 新增 | v0.0.22 release notes：新 endpoint / 安全加固 / 默认值 / 测试 / 修改文件 / 剩余风险 / 升级指南 |

### 测试结果（验证零回归）

| 命令 | 结果 |
|------|------|
| `pytest tests/test_integration_web_server.py -v` | 25 passed |
| `pytest tests/ -v -m "not slow and not docker"` | **563 passed, 36 deselected** |
| `ruff check src tests` | All checks passed |

---

## 🔄 下一步待办：Web Claude 改造（P0）

**目标**：在 v0.0.22 baseline 之上把项目从「coding agent runtime 调试器」改造为「claude.ai 风格的 Web 端对话助手」。

详见 [`WEB_CLAUDE_PLAN.md`](WEB_CLAUDE_PLAN.md) 与 [`web-claude-tasks/P0-1-session-sqlite.md`](web-claude-tasks/P0-1-session-sqlite.md)。

> **设计变更（2026-07-06）**：原 P0-4「三栏布局 + 右侧 Drawer」改为「左侧 Session Sidebar + 中间 ChatMain + 淡化 turn 信息卡片 + Skills/MCP Modal」。**不做右栏调试 Drawer**；当前 turn 信息以 inline 卡片（ToolCallCard / ToolResultCard / SkillUsedCard / MCPToolCard / ErrorCard）插入到中间对话流。

### 5 个 P0 子任务

| # | 任务 | 后端 / 前端 | 工作量 | 依赖 | 状态 |
|---|---|---|---|---|---|
| **P0-5** | 默认对话向 system prompt + skills 注入 | 后端 | 小 | — | ✅ 已完成 |
| **P0-1** | Session 线性化（sqlite，保留旧 session.py） | 后端 | 中 | — | ✅ 已完成 |
| **P0-2** | VirtualFileStore + 上传 / 列表 / 下载 API | 后端 | 中 | P0-1 | ✅ 已完成 |
| **P0-3** | `view_file` / `list_files` 工具 + 附件注入 UserMessage content block | 后端 | 小 | P0-2 | ✅ 已完成 |
| **P0-4** | 前端：左侧 Sidebar + 中间 ChatMain + 淡化卡片 + Skills/MCP Modal | 前端 | 大 | P0-1, P0-2, P0-3 | ✅ Step 1–8 完成 |

### 5 个里程碑

| 里程碑 | 含义 | 对应 P0 | 状态 |
|---|---|---|---|
| **M5** 默认 prompt 生效 | 不传 system_prompt 也像对话助手 | P0-5 | ✅ |
| **M1** 后端会话线性化 | sqlite 跑通，多会话管理 | P0-1 | ✅ |
| **M2** 文件上传闭环 | 上传 → FileRef → 持久 | P0-2 | ✅ |
| **M3** LLM 能看附件 | view_file / list_files 闭环（**图片明确不支持**） | P0-3 | ✅ |
| **M4** 前端可点（新结构） | Sidebar + ChatMain + 淡化卡片 + Modal | P0-4 | ✅ |

**最小可用 Web Claude** = M1 + M2 + M3 + M4 + M5 全部 ✅（P0 MVP 完成 2026-07-07）。

### 已确认的技术栈（2026-07-06）

1. Session 存储：**sqlite + aiosqlite** ✅
2. 附件进入 LLM 方式：**所有附件统一注入 FileBlock → view_file 读取**（P0-3 已落地，**图片明确不支持**——不做 ImageBlock / 不做 GLM-4V 直读 / 不做 OCR）
3. 图片支持 fallback：本轮不做；图片以 `format=image_unsupported` 注入，由 view_file 返回明确"不支持图片"
4. 前端状态管理：**Pinia**
5. UI 组件库：**不引入**（用 CSS + 自定义 Vue 组件）
6. 代码高亮：**highlight.js**
7. PDF 渲染：P0 阶段只做 FileChip + 下载；P1 再考虑 iframe / pdf.js
8. 执行顺序：**P0-5 → P0-1 → P0-2 → P0-3 → P0-4**

### 显式不做（P3）

- 跨 session 记忆 / 用户长期记忆 / 向量检索 / RAG
- 本地文件操作工具（bash / read / write / edit / grep / find / ls）
- Project trust / CLAUDE.md 自动加载
- 多 provider 路由 / fallback / load balancing
- 多用户 / 认证 / 公网部署
- TUI / RPC mode

---

## ✅ P0-5 默认对话向 system prompt（2026-07-05）

**让模型默认像 claude.ai 一样回答（Agent.system_prompt 为空时自动启用）。**

新增 / 改动：
- `src/pi_agent_core_py/system_prompt.py`：`build_default_system_prompt(*, skills, mcp_tools, file_tools_enabled)` 纯函数；8 条行为准则 + skills 段（按 priority 排序）+ MCP tools 段（按 server 分组）
- `src/pi_agent_core_py/harness.py::_prepare_skill_prompt`：empty / whitespace prompt 触发 default；metadata 写 `system_prompt_source` / `enabled_skill_names` / `enabled_mcp_tool_names`
- `tests/test_system_prompt.py`（15 用例）

测试结果：
- `pytest tests/test_system_prompt.py` → **15 passed**
- 离线 baseline：563 → **578 passed**

零回归。

---

## ✅ P0-1 Session 线性化 sqlite（2026-07-06）

**新增独立的 `SQLiteSessionStore`，Web 层多会话能力；不动核心 runtime / 不删旧 session.py。**

新增 / 改动：
- `pyproject.toml`：加 `aiosqlite>=0.20`
- `src/pi_agent_core_py/session_sqlite.py`（~430 行）：`SQLiteSessionStore` + `SQLiteSession` / `SQLiteStoredMessage` / `SQLiteStoredSnapshot` + 3 异常（`SQLiteSessionError` / `SessionNotFoundError` / `SessionSerializationError`）
- 表：`sessions` / `messages` / `snapshots`，WAL + 外键级联；messages `UNIQUE(session_id, idx)`
- 并发：单 SQL 子查询原子计算 idx（`RETURNING idx`），UNIQUE 约束兜底；aiosqlite 单连接串行
- 序列化：`{type, data}` JSON → 强类型 Pydantic 对象（覆盖 UserMessage / AssistantMessage / ToolResultMessage / SummaryMessage / CustomMessage）；未知 type 抛 `SessionSerializationError`
- `src/pi_agent_core_py/web/state.py`：`WebAppState` 加 `session_store` / `current_session_id`
- `src/pi_agent_core_py/web/app.py`：`create_app(db_path=...)`；lifespan startup 初始化 + ensure_default_session；shutdown 关 store；新增 5 个 endpoint（POST/PATCH/DELETE `/api/sessions`）；改 `GET /api/messages?session_id=`；改 `POST /api/prompt` 支持 body `session_id`（先 list_messages 加载 → run_prompt → replace_messages 覆盖 + append_snapshot）；修了 v0.0.22 隐含 `list | list` bug

测试：
- `tests/test_session_sqlite.py`（30 用例）：CRUD / append / list 强类型 / replace / snapshot / 错误 / 并发 20 append idx 不重复 / 持久化跨重启
- `tests/test_web_sessions_sqlite.py`（13 用例）：sessions 列表 / 创建 / messages?session_id / prompt 持久化 / 默认 session / 不串消息 / DELETE 级联 / 兼容旧 / 409 / PATCH

测试结果：
- `pytest tests/test_session_sqlite.py` → **30 passed**
- `pytest tests/test_web_sessions_sqlite.py -m "slow"` → **13 passed**
- `pytest tests/test_integration_web_server.py -m "slow"` → **25 passed**（v0.0.22 零回归）
- 离线 baseline：578 → **608 passed, 49 deselected**
- ruff：All checks passed

旧 `session.py` / `session_sync.py` / `compaction/branch_summary.py` 全部保留不动。

---

## ✅ P0-2 VirtualFileStore + 文件上传 / 列表 / 下载 API（2026-07-06）

**会话级文件上传；不动核心 runtime / 不动前端。**

新增 / 改动：
- `pyproject.toml`：加 `python-multipart>=0.0.9`
- `src/pi_agent_core_py/web/files.py`（~430 行）：
  - `VirtualFileStore`：`init` / `save` / `get` / `get_for_session` / `list_session` / `delete` / `delete_for_session` / `delete_session_files` / `session_total_size`
  - `FileRef` Pydantic：`id` / `session_id` / `name` / `size` / `mime` / `sha256` / `path` / `created_at`
  - 6 异常：`FileStoreError` / `VirtualFileNotFoundError` / `FileAccessDeniedError` / `FileTooLargeError` / `SessionStorageLimitError` / `UnsafeFilenameError`
  - `sanitize_filename()` 工具函数 + `Path.resolve().is_relative_to(root)` 双重防路径穿越
  - 流式 chunk 64KB 读 UploadFile + 边写边算 sha256 + 边写边检大小
- 存储结构：`uploads/{safe_session_id}/{file_id}/{safe_filename}` + `metadata.json`（每文件独立，避免 manifest 并发写）
- `src/pi_agent_core_py/web/state.py`：`WebAppState` 加 `file_store` / `uploads_dir`
- `src/pi_agent_core_py/web/app.py`：`create_app(uploads_dir=, max_file_size=25MB, max_session_upload_size=100MB)`；lifespan startup init VirtualFileStore；新增 6 个文件 endpoint（POST/GET `/api/sessions/{sid}/files` + GET/DELETE `/api/sessions/{sid}/files/{fid}` + GET/DELETE `/api/files/{fid}?session_id=`）；改 `DELETE /api/sessions/{sid}` 先删 uploads 再删 sqlite（避免孤儿），返回 `deleted_files` 计数

测试：
- `tests/test_virtual_file_store.py`（33 用例）：sanitize 6 + init 2 + save/get/list 10 + delete 4 + 大小限制 3 + 路径穿越 3 + mime 3 + 常量 2
- `tests/test_web_files.py`（22 用例）：上传单/多 + 列出 + 隔离 + 下载（session / compat）+ 删除（compat / session）+ 缺 session_id 400 + 404 session / file + 403 cross-session download / delete / compat + 413 单文件 / session 总量 + sanitize 路径穿越 + 全分隔符 fallback + 删 session 级联 uploads + sha256 / mime 正确

测试结果：
- `pip install python-multipart>=0.0.9` 成功
- `pytest tests/test_virtual_file_store.py` → **33 passed**
- `pytest tests/test_web_files.py -m "slow"` → **22 passed**
- `pytest tests/test_session_sqlite.py + test_web_sessions_sqlite.py + test_integration_web_server.py -m "slow"` → **38 passed**（P0-1 + v0.0.22 零回归）
- 离线 baseline：608 → **641 passed, 71 deselected**
- ruff：All checks passed

**当前 P0 进度：4/5 完成（P0-5 + P0-1 + P0-2 + P0-3），下一步 P0-4（前端）。**

---

## ✅ P0-3 view_file / list_files 工具 + 附件注入 UserMessage FileBlock（2026-07-06）

**让 agent 能通过工具读取上传文件；附件统一注入 FileBlock，图片明确不支持理解。**

新增 / 改动：
- `pyproject.toml`：加 `pyarrow>=15`（parquet 读取依赖）
- `src/pi_agent_core_py/messages.py`：新增 `FileBlock`（type / file_id / name / mime / size / sha256 / format / summary）+ `FileFormat` Literal（markdown / html / csv / parquet / text / pdf / image_unsupported / binary / unsupported）+ `UserContent = Union[TextContent, FileBlock]`；扩展 `UserMessage.content`
- `src/pi_agent_core_py/context.py`：`convert_to_llm` 把 FileBlock 转 provider-agnostic 文本说明（列元信息 + 引导调用 `view_file`；图片格式明确写"不支持图片"；不暴露 path / 不发 image block）
- `src/pi_agent_core_py/tools/list_files.py`：`ListFilesTool` + `create_list_files_tool(file_store, session_id_getter)` factory
- `src/pi_agent_core_py/tools/view_file.py`（~800 行）：`ViewFileTool` + `create_view_file_tool` factory + `_classify_format` + stdlib `HTMLParser` 抽 HTML 文本 + `csv.Sniffer` 推断 delimiter + `pyarrow.parquet.ParquetFile` 读 schema/row_count/前 N 行
- `src/pi_agent_core_py/tools/__init__.py`：导出 list_files / view_file factory（`# ruff: noqa: E402, I001` 防 import 重排）
- `src/pi_agent_core_py/web/app.py`：POST /api/prompt 支持 body `file_ids: list[str]`（404 / 403 / 400 校验）；附件统一注入 UserMessage.content 为 FileBlock；图片以 `format=image_unsupported` 注入（**不新增 ImageBlock**）；响应增加 `attachments` metadata（attached_file_ids / names / counts / supported / unsupported）；lifespan startup 时若 `file_store` 可用，自动 register `list_files` / `view_file` 到 `agent.tools`
- `src/pi_agent_core_py/system_prompt.py`：默认 prompt 改为明确"支持 markdown / html / csv / parquet" + "不支持图片内容分析（不做 OCR / 不做视觉理解）"；删除原"图片你可以直接理解"描述

设计要点：
- **FileBlock 只放元信息**：不发全文 / 不发 path / 不发 base64
- **跨 session 隔离**：`session_id_getter` 取 `state.current_session_id`，工具内调 `VirtualFileStore.get_for_session(sid, fid)` 强校验
- **provider 无关**：convert_to_llm 把 FileBlock 转成纯文本说明（GLM / Anthropic / OpenAI / Fake 都用同一段文本）
- **避免 import 循环**：list_files / view_file 通过 `TYPE_CHECKING` + deferred import 引用 `web/files.py`（agent.py → tools → web → app → harness → agent 形成循环，必须延迟）
- **POST /api/prompt + file_ids 路径**：构造 `UserMessage(content=[TextContent(text), *FileBlocks])` 后用 `harness.run_continue()`（让 text + FileBlock 同条 UserMessage，避免两条 user msg 连续）；无附件路径仍走 `run_prompt`
- **pyarrow 缺失防御**：`view_file._view_parquet` 内部 `try: import pyarrow.parquet`，失败转 `ToolResult(is_error=True, error_type="PyArrowNotAvailable")`

测试：
- `tests/test_file_tools.py`（27 用例）：list_files 5（含图片 unsupported / 跨 session 隔离 / no session error）+ view_file md/text 4（含大文本截断 / max_bytes 参数）+ html 1（script/style 忽略）+ csv 4（columns/rows / max_rows / 空 CSV / TSV Sniffer）+ parquet 3（schema/rows / max_rows / 缺 pyarrow mock）+ pdf 1 + image 1 + binary 1 + errors 4（missing / cross-session / invalid args / no session）+ JSON serializable 2 + classify_format 1
- `tests/test_prompt_file_injection.py`（15 用例）：md/html/csv 注入 + 图片 image_unsupported + 多文件顺序 + text+files 同条 UserMessage + missing 404 + cross-session 403 + 无附件不回归 + attachment metadata + sqlite restore 强类型 + SQLiteSessionStore round trip + convert_to_llm + 不暴露 path + 工具自动注册（uploads_dir 启停）
- 旧测试扩充：`test_smoke_messages.py`（+4 FileBlock round trip）、`test_smoke_context.py`（+4 FileBlock → 文本）、`test_system_prompt.py`（+2 md/html/csv/parquet 与图片不支持断言）

测试结果：
- `pip install pyarrow>=15`（24.0.0）成功
- `pytest tests/test_file_tools.py` → **27 passed**
- `pytest tests/test_prompt_file_injection.py -m "slow"` → **15 passed**
- `pytest tests/test_virtual_file_store.py` → **33 passed**
- `pytest tests/test_web_files.py -m "slow"` → **22 passed**
- `pytest tests/test_session_sqlite.py` → **22 passed**
- `pytest tests/test_web_sessions_sqlite.py -m "slow"` → **13 passed**
- `pytest tests/test_integration_web_server.py -m "slow"` → **25 passed**（v0.0.22 零回归）
- `pytest tests/test_system_prompt.py` → **17 passed**
- 离线 baseline：641 → **678 passed**
- 全套（offline + slow）：**764 passed, 0 skipped**
- ruff：All checks passed

零回归（v0.0.22 / P0-5 / P0-1 / P0-2 baseline 全部保持）。

**当前 P0 进度：4/5 完成（P0-5 + P0-1 + P0-2 + P0-3），下一步 P0-4（前端）。**

---

## ✅ P0-4 前端 Claude-like 改造（2026-07-07）

**把 v0.0.22 Trace Viewer 改成 claude.ai 风格两栏聊天助手。** 共 8 个 step，**Step 1–7 完成，Step 8（docs 收尾）待办**。

### Step 进度

| # | Step | 状态 |
|---|---|---|
| 1 | Skills API：`POST /api/skills/upload`、enable/disable、`GET /api/skills/{name}`、`POST /api/prompt` 顶层 `skill_names`；unknown skill 由 web 层预校验返回 400 | ✅ |
| 2 | MCP API：servers CRUD、test connection（不污染 harness）、enable/disable、tools enable/disable 真实生效；env values 严格不回显（response 只有 `env_keys`） | ✅ |
| 3 | 前端骨架：Pinia + `types/` + `api/` + `stores/` 五个 store；barrel `index.ts` 保证旧 .vue import 不破坏 | ✅ |
| 4 | UI 骨架：`AppShell` + `SessionSidebar` + `ChatPanel` 两栏；`DeveloperDrawer` 从主路径下线但文件保留 | ✅ |
| 5 | Inline Turn Cards：`chatStore.handleEvent` 完整 WS event mapper；7 个 card 组件；streaming assistant draft | ✅ |
| 6 | 文件上传 UI：`utils/files.ts` + `FileChip` + `AttachmentBar`；`ChatInput` 附件按钮 + drag-drop；`sendPrompt` 带 `file_ids` + `files` | ✅ |
| 7 | Skills/MCP Manager Modal：通用 `Modal.vue` + skills 三件套 + mcp 四件套；SessionSidebar footer 按钮挂载 | ✅ |
| 8 | docs 收尾 + 最终验证：`WEB_CLAUDE_PLAN.md` / `docs/WEB_API.md` / `README.md` / `docs/WEB_TESTING.md` / `docs/RELEASE_NOTES_WEB_CLAUDE_P0.md`（新增）/ `TODO.md` | ✅ |

### Step 7 关键设计（本轮）

- **通用 Modal**：`components/common/Modal.vue`——Teleport 到 body、ESC 关闭、点遮罩关闭、点内容不关闭；body 最大高度 80vh + overflow auto；**居中弹窗形态，非右栏 Drawer**。
- **Skills 三件套**（`components/skills/`）：
  - `SkillManagerModal.vue`：open 时 `loadSkills()`；含说明 + upload form + list + error banner
  - `SkillUploadForm.vue`：`<input type=file multiple accept=".md,text/markdown">` + Upload 按钮；上传中禁用、成功清空 input
  - `SkillList.vue`：每个 skill 一张卡（name / status badge / priority / description / tags / tool_names）；**enabled toggle** 与 **Use this turn checkbox** 分离——selected 必须是 enabled 的子集；store 在 disable 时自动从 `selectedSkillNames` 移除
- **MCP 四件套**（`components/mcp/`）：
  - `MCPManagerModal.vue`：open 时 `loadServers()` + `loadTools()`
  - `MCPServerForm.vue`：name / command / argsJson(textarea) / env rows(key + password) / enabled checkbox（默认 false）；前端解析 argsJson 失败直接报错不发请求；提交后 `resetForm()` 清空全部字段
  - `MCPServerList.vue`：每行 name + status badge + command + args + **env keys (values hidden)** + last_error + last test result；操作 Test / Enable / Disable / Delete（confirm）
  - `MCPToolList.vue`：每行 tool name (mono) + server + mcp_tool + description + enabled badge；操作 Enable / Disable
- **mcpStore 改动**：加 `lastTestResultByServer: Record<string, { toolCount?, error? }>` map——多 server 连续 test 不互相覆盖；`createServer/enableServer/disableServer/testServer/deleteServer` 失败时调 `reloadServersSilent()` 同步后端真实状态（502 attach 失败时后端写到 cfg 的 `last_error` 立即反映到 UI）；`deleteServer` 成功路径清掉对应 test result 防孤儿。

### env value 防回显（Step 7 核心安全约束）

四道防线协同保证 MCP env values 只能提交、不能从 UI 或 response 回显：

1. **后端类型**：`MCPServerSummary` 只有 `env_keys: string[]`，无 `env` 字段——API 层根本拿不到 value
2. **MCPServerForm 永远空白初始化**：`resetForm()` 把 `envRows = [{key:"", value:""}]`，提交后立即清空；`v-model` 不会从响应"自动同步"
3. **env input 用 `type="password" + autocomplete="new-password"`**：浏览器不主动回填历史值；shoulder-surfing 也防
4. **MCPServerList 只渲染 `s.env_keys.join(', ')`** + `(values hidden)` 提示，没有任何地方读 env value

### selected skill → sendPrompt 链路

Step 5 已存在的链路，Step 7 只是补了用户选择入口：

```
SkillList checkbox
  → skillStore.toggleSelectedSkill(name)
  → skillStore.selectedSkillNames
ChatPanel.onSubmit
  → sendPrompt({ ..., skillNames: [...skillStore.selectedSkillNames] })
  → POST /api/prompt body.skill_names
  → 后端合并 skill_selection.names + skill_names 去重
  → registry.select (web 层预校验 unknown → 400)
```

`ChatInput` 不加 skill selector——按设计要求本轮 skill 选择只在 Modal 里完成。

### Step 7 测试结果

| 命令 | 结果 |
|------|------|
| `npm run build`（含 `vue-tsc --noEmit`） | ✅ 123 modules / 127.85 KB JS（gzip 45.08 KB） |
| `pytest tests/test_web_skills_api.py tests/test_web_mcp_api.py -m "slow and not docker"` | ✅ **59 passed** |
| `pytest tests/ -v -m "not slow and not docker"` | ✅ **678 passed**（与基线一致，零回归） |
| `ruff check src tests` | ✅ All checks passed |

**零后端改动**：`web/app.py` 一行未改；core runtime / providers / harness 全部未碰。

### 当前剩余风险

1. **MCP server 编辑流程未提供**——用户想改 args/env 必须先 Delete 再 Add（按设计如此，是 env 防回显的根本保障）
2. ✅ ~~enabled=true + attach 失败时 UI 不一致~~（已修复：catch error → `reloadServersSilent()` → throw）
3. **lastTestResultByServer 不在 modal close 时清空**——保留最近 test result（关闭再打开还能看到，对用户友好）；delete server 时清掉对应 entry 防孤儿
4. **Modal 高度自适应**——`max-height: 88vh` + body `max-height: 80vh + overflow auto`；超长 server list 内部滚动

### 显式不做（Step 7 范围）

- 右栏 / Drawer / `DeveloperDrawer` 引用
- Skills marketplace / 在线编辑 / prompt preview
- MCP marketplace / MCP resources / OAuth / RBAC / 多用户
- MCP 配置持久化（重启即丢）
- `ChatInput` skill selector（skill 选择只在 Modal 里）

---

## ✅ P0-4 Step 8 — Docs 收尾 + 最终验证（2026-07-07）

**把所有文档从「待改造计划」翻新为「Web Claude P0 MVP 已完成」状态；零源码改动。**

### 修改 / 新增文档

| 文件 | 类型 | 摘要 |
|---|---|---|
| `WEB_CLAUDE_PLAN.md` | 改 | 顶部 baseline 翻新（678/59/npm pass）；新增「P0 MVP 完成状态」段（8-step 进度表全部 ✅）+ 已完成能力列表 + 显式不做 + P1 建议 |
| `docs/WEB_API.md` | 改 | 补齐 P0-1~P0-4 全部 endpoints（Sessions CRUD / Files 6 / Skills upload+enable+disable / MCP servers CRUD+test+enable+disable+delete / MCP tools enable+disable / POST /api/prompt 新参数）；强调 `POST /api/prompt` 同步阻塞；错误码速查加 413/422/502；明确 env 不回显、test 不污染 harness、tool enable/disable 真实生效 |
| `README.md` | 改 | 顶部进度：Step 20 → Web Claude P0 MVP；新增「Web Claude MVP」段（功能 / 启动 / 界面布局 / 使用流程 / 安全说明 / 当前限制）；项目结构修正（layout/chat/skills/mcp/common 子目录） |
| `docs/WEB_TESTING.md` | 改 | 测试分层加 P0-1~P0-5；最终验证命令全套；手动 smoke checklist（25 项）；预期结果表更新 |
| `docs/RELEASE_NOTES_WEB_CLAUDE_P0.md` | **新增** | P0 MVP release notes：完成日期 / 范围 / 新增功能（Backend + Frontend）/ 关键安全约束 / Test baseline / Known limitations / Explicit non-goals / Backend changes / Frontend changes / 升级指南 / 下一步 |
| `TODO.md` | 改 | P0-4 / M4 状态 → ✅；最小可用 5/5；P0-4 块标题 🔶 → ✅；追加 Step 8 完成块 |

### 验证结果

| 命令 | 结果 |
|---|---|
| `npm run build`（含 `vue-tsc --noEmit`） | ✅ **123 modules / 127.85 KB JS**（gzip 45.08 KB） |
| `pytest tests/ -v -m "not slow and not docker"` | ✅ **678 passed**（与 Step 7 baseline 一致，零回归） |
| `ruff check src tests` | ✅ **All checks passed** |
| `pytest tests/test_web_skills_api.py tests/test_web_mcp_api.py -m "slow and not docker"` | 沿用 Step 7 已验证结果 **59 passed**（本轮未重跑） |
| `pytest tests/test_web_files.py tests/test_prompt_file_injection.py -m "slow and not docker"` | 沿用 P0-2/P0-3 baseline **37 passed**（本轮未重跑） |
| `pytest tests/test_integration_web_server.py -m "slow and not docker"` | 沿用 v0.0.22 baseline **25 passed**（本轮未重跑） |
| 浏览器手动 smoke | **本轮未执行**——Step 8 仅做文档收尾 + 自动化验证；Browser manual smoke checklist is documented but not yet executed in this Step 8 run；25 项 checklist 见 `docs/WEB_TESTING.md` |
| Coverage gate | `pytest --cov` 报 `FAIL Required test coverage of 75.0% not reached. Total coverage: 73.75%`——**但 exit code = 0**（`--cov-fail-under` 未硬绑）。**Coverage is below the configured 75% threshold and is treated as a known baseline issue, not a Step 8 regression** |

### Step 8 关键声明

- **Web Claude P0 MVP is complete for local development**
- **Localhost-first, no authentication, not suitable for public exposure**
- **不支持图片理解**（不做 OCR / 不做视觉理解）
- **PDF 正文不解析**
- **MCP env value 严格不回显**（response 只有 `env_keys`）
- **MCP/Skill 配置 P0 不持久化**（重启即丢）
- **`POST /api/prompt` 同步阻塞**，当前没有 `/api/prompt/async`

---

**P0 MVP 全部完成（P0-5 + P0-1 + P0-2 + P0-3 + P0-4 Step 1–8）。下一步可进入 P1 polish 或 freeze baseline。**

### P1 推荐优先级（高 → 低）

1. 真实浏览器 smoke test（25 项 checklist）
2. Playwright e2e 最小用例
3. 真实 GLM 端到端冒烟
4. MCP / Skill 配置持久化
5. `/api/prompt/async`
6. WebSocket event_id 去重 + reconnect 补播
7. Regenerate / Export markdown
8. PDF 文本提取


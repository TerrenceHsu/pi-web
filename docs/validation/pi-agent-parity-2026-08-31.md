# pi `agent` 模块对齐报告（2026-08-31）

## 范围

本轮只处理用户确认的第 2 项 `agent`：Agent loop、工具执行、控制队列、上下文边界与公开状态机。
对照源为 `D:\LLMTutorial\pi\pi-main\packages\agent`；本项目主要实现位于
`src/pi_agent_core_py/{agent,loop,hooks,tools,context,events}`，并同步修正 Harness、Snapshot、
Session 与 Web 组合层的消息类型边界。`coding-agent`、SQLite backend 和 telemetry 留到后续阶段。

## 差异与处理结果

| 契约 | 对照前问题 | 本轮结果 |
|---|---|---|
| Turn 控制顺序 | `prepare_next_turn` 在 `should_stop_after_turn` 之前执行，且自然结束时也会无意义 preparation | 先判断 stop；只有确定存在下一轮时才 preparation，并拾取 preparation 期间到达的 steering |
| 终态生命周期 | transform/应用 callback 等意外异常会直接跳到 request error，留下未闭合 turn；length 截断的 tool call 没有 execution start/end | 所有异常路径闭合 message→turn→agent→request；截断调用生成成对工具生命周期与安全错误结果 |
| Prompt / Context | `prompt()` 只有字符串；无图片、单条/批量 AgentMessage、自定义 convert；transform 不接 abort signal | 支持文本+图片、单条/批量 AgentMessage；新增可同步/异步 `convert_to_llm_fn`；transform 兼容 signal-aware 与旧一参数实现 |
| CustomMessage | AgentMessage 类型存在，但 Agent/Harness/Snapshot/Session 链路仍按标准 Message 收窄 | CustomMessage 可进入 prompt/context，由默认转换过滤或应用自定义转换，并可在快照与 Session 中无损往返 |
| 工具参数与 hook | 工具无拥有者级参数兼容层；before 阻断不能请求终止；after 只能整份 ToolResult 替换 | schema 校验前调用 `prepare_arguments`；before 支持 `terminate`；after 支持 content/details/error/usage/terminate 逐字段 patch，并保护 call identity 与 deferred-tool metadata |
| Tool update 生命周期 | 工具 promise 已完成后仍可能通过被保存的 callback 发出迟到 update | 每次 execute 的 update callback 在 promise settle 后关闭；并行批次中迟到 update 被忽略 |
| Queue continue | assistant 尾消息一律拒绝 continue，即使已有 steering/follow-up | assistant 尾优先消费 steering，再消费 follow-up；保持 one-at-a-time 语义且避免第一次轮询重复消费 |
| 动态下一轮 | turn callback 看不到明确的本轮 delta，且 preparation 只能替换 messages，不能切换 system/client/tools/thinking | Turn control 同时提供完整 context 与本轮 `new_messages`；新增 `AgentLoopTurnUpdate`，只在真实下一轮前原子应用 context/system/client/tools/thinking replacement |
| Thinking | `AgentState.thinking_level` 只是展示值，没有进入模型请求 | 非 `off` level 随每次 loop 调用进入 `ModelClient` / `ProviderRequest`；`off` 按上游语义在 Agent 边界省略，具体 wire 参数仍由 provider 能力映射或显式 metadata 决定 |
| 公开状态与结束事件 | system prompt、工具集合不在公开状态；`agent_end.messages` 无法同时表达既有完整 transcript 与上游 per-run delta | 状态同步公开 system prompt 与 active tool names；`AgentEndEvent.messages` 保持完整 transcript 兼容，同时新增 `new_messages` 表达本轮 delta |

## 保留的本项目扩展

- 保留 permission policy、交互审批、审计日志、`max_turns`、request 事件、Snapshot 和现有 Harness；这些是已投入产品使用的增强，不因上游最小核心而删除。
- `AgentEndEvent.messages` 不改成上游的仅增量语义，以免破坏 Web/Session 既有消费者；新增 `new_messages` 消除歧义。
- 上游当前新增的 durable AgentHarness scaffold 仍含未实现入口，本轮不复制占位实现；本项目已有可运行 Harness/Session/恢复链路。
- Provider 数量、模型目录与精细 thinking wire mapping 属于 `ai`/产品能力边界；Agent 层保证 level 不再丢失，但不会向不声明能力的端点猜测私有参数。

## 完整门禁伴随修复

第一次全量测试在 Windows 上暴露 Wiki Parser 状态文件的读写竞争：消费者线程短暂持有
`status.json` 时，Worker 的 `os.replace` 会以 `WinError 5` 失败。原子写入现在只在 Windows
对 `PermissionError` 做有界重试；永久权限错误仍按原协议失败，并有模拟瞬时读锁的回归测试。
该修复不改变 Agent 语义，但确保完整门禁与真实 Windows 队列运行稳定。

## 验证

| 门禁 | 结果 |
|---|---|
| Agent/P0 lifecycle/control/public state 专项 | 36 passed |
| Agent + Session/Compaction/ToolResult/Coding Sandbox 邻接 | 127 passed |
| Backend 全量 | 2131 passed、7 skipped、9 deselected；coverage 77.24% ≥ 75%；370.94s |
| Ruff | `ruff check src tests scripts` PASS |
| strict Mypy | 187 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |

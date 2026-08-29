# Coding Agent 三类意图路由

状态：Implemented（2026-08-29）

## 目标

产品需要在调用 Provider 之前，把每个请求确定为且仅确定为以下一种执行边界：

| 路由 | 语义 | 可用能力 |
|---|---|---|
| `read_only` | 查看、解释、审核、方案与不确定请求 | Workspace/网络/MCP 的保守只读子集 |
| `coding` | 修改代码、运行验证、形成待审批制品 | 现有受管 Sandbox 的 `coding_*` 工具 |
| `knowledge` | 在某个 Wiki Space 中检索或提出 Change Set | Knowledge 固定 Prompt、Skill 和工具白名单 |

路由器位于产品组合包 `coding_agent_app`，不依赖具体 LLM、Provider、FastAPI、Workspace 或
Sandbox 实现。它不是一次额外的 LLM 调用，因此不增加延迟、费用或不可复现的分类结果。

## 决策优先级

1. Session 已绑定 active Knowledge Conversation 时强制 `knowledge`。普通 Session 不得仅凭文本猜测 Wiki Space。
2. `intent_mode=read_only|coding` 是显式覆盖；旧 `coding_mode=true` 等价于显式 Coding。
3. “不要修改/运行”“只检查/分析”等否定约束优先判为 `read_only`。
4. 问题、解释、审核与“先制定方案”判为 `read_only`。
5. 实现、修复、修改、创建、提交、继续执行等动作判为 `coding`。
6. 无可靠信号时安全回退到 `read_only`。

`intent_mode=knowledge` 仅用于确认已有 durable binding，不能在普通 Session 中临时选择知识空间。
Knowledge Conversation 与 Coding/只读显式覆盖冲突时在 Provider 调用前返回 400。

## 执行边界

### Read-only

- 临时构造 ToolRegistry，不修改全局注册表。
- 允许 `list_files`、`view_file`、`web_search` 等本地读取工具。
- MCP 工具先拒绝 mutation/execute 关键词，再只接受明确的 read/list/get/search 等前缀。
- System Prompt 明确禁止声称写入、执行或完成实现；需要 mutation 时要求用户显式授权实现。
- 请求完成或异常后恢复原 ToolRegistry。

### Coding

- 复用既有 `CodingSandboxAutomation`，自动 prepare Session Sandbox。
- 本轮只暴露已注册的 `coding_*` 工具并使用既有 Sandbox 权限边界。
- 模型结束后由 Backend 独立验证，通过 Validation→Freeze TOCTOU 屏障后停在
  `awaiting_approval`，不自动发布到 Workspace。
- 自动路由后的 resolved Coding 状态写入私有 request payload，使 Agent idle 的
  preflight/finalization 阶段仍可正确 Stop。

### Knowledge

- durable Conversation binding 是唯一选择依据。
- 复用 Knowledge 模式固定 Prompt/Skill/工具白名单。
- 修改仍只能形成 Change Set，必须经用户批准。

## API 与审计

`POST /api/prompt`、`POST /api/prompt/async`、异步 request `result_summary` 和 Context Budget
返回相同的 secret-free 决策投影：

```json
{
  "route": "read_only",
  "confidence": 0.98,
  "source": "rule",
  "reason_code": "read_only_constraint",
  "explicit": false
}
```

`/api/state.intent_routing` 公开开关与固定路由集合。前端 Turn 卡在 202 返回后立即展示 route。
低层 `create_app` 默认关闭路由以保持 embedder 兼容；产品启动器显式启用。

## 测试边界

快速开发阶段只保留两个 API 行为测试：一个覆盖三条路线及工具边界，一个覆盖否定约束和显式
覆盖。Sandbox 状态机、Knowledge 工具白名单和 Workspace 权限继续复用其既有测试，不重复建矩阵。

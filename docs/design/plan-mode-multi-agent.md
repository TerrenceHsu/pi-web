# Plan Mode：Planner–Executor–Verifier

状态：Implemented（2026-08-29）

## 产品边界

Plan Mode 不是新的意图路由。请求先得到 `read_only | coding | knowledge`，再选择
`execution_mode=direct|plan`；MVP 只允许 Coding 使用 Plan。前端显式按钮是首版唯一入口，避免
简单请求自动承担多次模型调用。

## 角色与权限

| 角色 | 输入 | 工具 | 终态输出 |
|---|---|---|---|
| Planner | 用户目标、Workspace 上下文、只读文件视图 | 保守只读工具、`plan_submit` | 结构化 PlanSpec |
| Executor | 当前 Task、已通过任务、Verifier 反馈、当前 Sandbox | `coding_*`、任务完成/阻塞工具 | TaskExecutionReport |
| Verifier | Task 验收标准、Executor 报告、累计 Sandbox diff | `coding_list/read/search/diff`、`plan_verdict` | VerificationReport |

三个角色使用独立 Agent/Harness 和独立消息上下文，首版同进程严格串行。角色不能直接互发自然
语言消息，也不能读取彼此隐藏思考。

## 通信机制

SQLite `plan_events` 是唯一事实源；asyncio Event 仅唤醒等待批准的编排器，WebSocket 仅投影 UI。
所有通信由 Orchestrator 中介并带 `run_id/plan_version/task_id/attempt/causation_id`。大内容只传
Workspace/Sandbox reference 与 SHA，不内联日志或文件。角色通过严格 terminal tools 提交结果，
schema 错误不会推进状态。

## 状态机

PlanRun：

```text
planning → awaiting_plan_approval → executing → verifying
         → awaiting_artifact_approval → completed
                         ↘ blocked | failed | cancelled | interrupted
```

Task：

```text
pending → executing → awaiting_verification → passed
                    ↘ failed → executing（有界重试）
                    ↘ blocked
```

Verifier 失败分类固定为 `retry_executor | replan_required | user_input_required`。MVP 对
`retry_executor` 最多执行两次；其它分类停止并向用户展示原因和建议。全部任务 passed 后才调用现有
服务器固定 Validation 和 Freeze；用户仍是发布到 Workspace 的唯一批准者。

## 持久化与 Workspace

Plan 是 SQLite 中的 canonical control-plane 数据，不在执行期间改写 `tasks/current.md` 或
`Memory.md`，避免提升 Workspace revision 破坏 Sandbox baseline。Session 历史只保存用户目标和最终
汇总；内部角色消息进入有界 Plan event/audit。启动时未完成的角色调用标为 interrupted，不自动重放
模型或工具副作用。

## 前端

- Chat 输入区显示独立 Plan 开关；启用时隐含 Coding。
- Planner 提交后展示一个任务列表卡和“批准并执行”。
- 当前任务显示 Planner/Executor/Verifier 阶段及 attempt。
- Verifier passed 显示勾；failed 显示原因、建议和失败分类。
- 刷新通过 latest PlanRun API 恢复任务卡；WebSocket 更新同一 `run_id` 卡片。

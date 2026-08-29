# Coding Agent Workspace 与 Session 连续性设计

> 状态：阶段 1–3 已完成；阶段 4–5 待实施。校准日期：2026-08-29。

## 1. 产品目标

Workspace 是 Coding Agent 的持久执行上下文，不只是上传文件目录。验收目标是：清空
Harness 消息并重启进程后，只依赖同一 Session Workspace，Agent 仍能确定当前目标、已完成
工作、实际代码结构、验证结果、待批准变更和下一步操作。

## 2. 模块边界

最终依赖方向为：

```text
pi_agent_core_py    agent_workspace    coding_sandbox
         \               |               /
          \------ coding_agent_app ------/
```

- `pi_agent_core_py`：可复用 Agent loop、消息、Provider 和通用工具契约，不拥有 Workspace。
- `agent_workspace`：Store、路径/权限、事务、恢复、固定文档转换和连续性领域逻辑；不得 import
  `pi_agent_core_py`、FastAPI、具体 Provider 或 `coding_sandbox`。
- `coding_sandbox`：隔离执行和不可变 Artifact，不知道 Session Workspace 的物理实现。
- `coding_agent_app`：产品组合适配层，连接 Core、Workspace 和 Sandbox。

阶段 1 已把规范实现迁移到顶层 `src/agent_workspace/`，Sandbox/Workspace adapter 迁移到
`src/coding_agent_app/`。旧 `pi_agent_core_py.web.files`、`workspace_documents` 和
`web.coding_sandbox.workspace` 暂时只保留兼容 re-export；Web Checkpointer 只保留 Core 消息和
ModelClient stream adapter。`agent_workspace` 可单独 import，且不会加载 Core 或 FastAPI。

## 3. 目标 Workspace 分类

```text
AGENT.md
Memory.md
HANDOFF.md
tasks/current.md
tasks/archive/**
docs/architecture.md
docs/code-flow.md
docs/decisions.md
docs/validation.md
docs/notes/**
scripts/**
inputs/**
artifacts/**
documents/**
.pi-agent/continuity/**
```

- `AGENT.md`：用户拥有的 Session 指令，自动流程永不修改。
- `Memory.md`：稳定事实、偏好、长期约束和仍有效的经验，不保存高频瞬时进度。
- `HANDOFF.md`：每轮更新的最小续作入口。
- `tasks/current.md`：当前目标、验收标准、计划状态和下一步。
- `docs/code-flow.md`：只根据已批准并发布的代码 revision 更新入口、模块、调用链和数据流。
- `docs/validation.md`：只记录真实工具证据，不接受 Assistant 自述的“测试通过”。
- `scripts/**`：代码唯一逻辑根；修改仍走 Sandbox、验证、冻结、用户审批和事务发布。
- `.pi-agent/**`：系统 operation、来源哈希和恢复证据，不进入 Sandbox 发布白名单。

目录无内容时保持逻辑惰性，不用 `.keep` 伪造用户文件。

阶段 3 已将以上路径固化为 `WorkspacePathPolicy`。文件 API、Agent 文件工具和前端右栏返回同一组
`category`、`owner`、`content_editable`、`movable`、`deletable`、`agent_writable`、
`sandbox_publishable`、`immutable` 字段，不允许各层自行猜测权限。

| 路径 | 所有者 | 创建/更新规则 | Sandbox 发布 |
|---|---|---|---|
| `AGENT.md` | user | 初始化创建；用户可编辑，不可移动/删除 | 禁止 |
| `Memory.md` | continuity | 自动流程更新；用户可纠正，不可移动/删除 | 禁止 |
| `HANDOFF.md` | continuity | 后续 continuity renderer 惰性创建/更新 | 禁止 |
| `tasks/**` | continuity | 后续 task renderer 惰性创建/更新 | 禁止 |
| 固定 `docs/*.md` | continuity | 仅已批准代码事件的固定 renderer 更新 | 禁止 |
| `docs/notes/**` | shared | 用户和 Agent 可创建 Markdown | 允许 UTF-8 Markdown |
| `scripts/**` | shared | 用户上传；Coding 模式通过 Sandbox 修改 | 允许 |
| `inputs/**` | user | 普通上传自动进入；正文不可原地改写，可删除重传 | 禁止 |
| `artifacts/**` | agent | `write_file` 非代码产物默认进入；Markdown 可人工修订 | 允许 |
| `documents/**` | document converter | 原件和固定转换产物均不可变 | 禁止 |

为兼容既有 Workspace，历史普通 Markdown 路径仍可读写并保持既有 Sandbox 发布语义，但所有新入口
默认使用上述命名空间。上传代码仍自动进入 `scripts/**`，其余普通上传进入 `inputs/**`；Agent 的
非代码文本产物进入 `artifacts/**`。`HANDOFF.md`、`tasks/**` 和固定工程摘要在阶段 4–5 的可信
renderer 首次产出内容前不创建空文件。

## 4. 每轮自动连续性更新

成功持久化的普通 Session Assistant turn（含 Regenerate 后的新 active answer）产生有界、可校验的
`pi-agent-turn-evidence/v1` evidence，然后由单 Session 串行 continuity lane 自动提炼：

```text
turn_persisted -> evidence_committed -> extracting -> applying -> current
                                      \-> retry_wait / degraded
```

阶段 2 只更新 `Memory.md`：LLM 直接返回固定章节的 managed Markdown，服务器负责长度限制、secret
redaction、source hash/audit marker、逐文件 SHA 乐观锁与 immutable generation 发布；该调用直接使用
当前 Session Provider，但不进入 Agent loop，也不启用 Tools、Skills 或 MCP。阶段 3 先固定了
`ContinuityDelta` 将使用的目标命名空间和写入权限；阶段 4–5 接入字段 renderer 时，模型仍不获得
任意文件写权限。

Assistant 正文先作为主结果持久化，自动记忆不得反向把成功回答改成失败。随后创建 append-only
`auto_memory` intent/effect/finish records；若进程恰好在消息提交后、intent 落库前退出，下一轮会从
canonical 最新完整 turn 与最近已覆盖 turn hash 重建 intent。成功响应公开 `continuity.status=updated`；
Provider/文件失败公开 `pending_retry` 或 `unavailable` 并保留可恢复 evidence。下一轮 preflight 必须先
归约前序 operation；Provider 持续不可用时，直接注入未提炼 evidence，不能跳过上一轮事实。

Coding turn 在 Sandbox 冻结到 `awaiting_approval` 后暂不修改 `Memory.md`，因为任何 Workspace 写入都会
提升 revision 并使已签名 Artifact 的发布基线过期。operation 记录 Sandbox blocker；发布、拒绝、取消、
失败或中断等终态事件解除 blocker 后自动续跑。后续普通 turn 也继承 blocker，直到待审批 Artifact 收敛。
Knowledge Conversation 使用独立连续性语义，阶段 2 明确跳过自动 Session Memory。

产品入口 `scripts/dev_web_app.py` 默认启用；通用嵌入者必须显式传
`create_app(..., enable_auto_memory=True)`，且同时配置 Session DB 与 `uploads_dir`。

## 5. 代码连续性

代码总结只由 `WorkspacePublished` 或用户代码上传事件触发。Worker 读取实际 revision 的文件清单、
diff、入口和验证记录，更新 `architecture.md`、`code-flow.md` 与 `validation.md`。未批准 Sandbox
变更只能进入 `HANDOFF.md` 的 `pending_approval`，不得写成已完成代码事实。所有生成文档记录
`workspace_revision` 和 source hash；代码已经变化而总结尚未更新时明确标记 `stale`。

## 6. 无上下文启动

`ContextAssembler` 按有界优先级加载 `AGENT.md`、`HANDOFF.md`、`tasks/current.md`、`Memory.md`、
代码流程、最近验证、Workspace 树摘要和未提炼 evidence。它不默认加载全部源码；Agent 通过只读
Workspace 工具按需查看文件。

## 7. 实施顺序

1. **模块分离**：行为和持久格式不变；建立独立包、兼容层和单向依赖。
2. **每轮 Memory**：Turn evidence、durable operation、结构化自动提炼、失败恢复。
3. **内容分类**（完成）：固定 `HANDOFF.md`、`tasks/**`、`docs/**`、`inputs/**`、
   `artifacts/**` 的所有权、默认路由、API metadata 与写入/发布策略；空目录继续惰性。
4. **代码连续性**：批准发布事件驱动 code-flow/architecture/validation 更新和 stale 检测。
5. **无上下文验收**：统一 ContextAssembler、重启续作、Pending Memory 与待批准 Artifact 场景。

测试继续遵守快速开发预算：阶段 1 不新增测试，只运行现有回归；阶段 2 新功能最多增加一个成功
路径和一个恢复失败路径测试；后续阶段优先复用现有 Workspace/Sandbox/Browser 测试。

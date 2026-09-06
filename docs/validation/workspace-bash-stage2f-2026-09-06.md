# Workspace Bash 阶段 2F：Coding/Plan 内任务复用

日期：2026-09-06。对应请求“提交，然后补齐 Coding/Plan 内 Bash 复用”。
先提交阶段 2E 为 `a4a628b`（`feat(bash): add standalone Web approval execution and private history`），
确认工作区干净后开始本阶段。以下改动随本报告共同归档，没有推送，也没有修改真实部署配置。

## 完成的行为

| 模式 | 授权与副本 | 当前文件处理 |
|---|---|---|
| 独立 Bash | 每条完整脚本单独批准，每次新副本 | 只保留输出/变更摘要，副本文件丢弃 |
| Coding | 一次请求范围确认；Bash/argv/编辑/固定验证共享许可、预算与副本 | 固定验证后签名冻结，等待 Changes 审阅 |
| Plan Executor | 精确计划版本与执行许可一次合并确认；连续任务及修复共享副本 | Verifier 独立审阅，最终固定验证/冻结仍必需 |

所有上述本地 Bash 路径都要求 Workspace 选择已配置的 Local Docker 并勾选 Bash。
选择不等于授权；打开页面、切换模式或预览上下文不创建容器。E2B 不装配 `run_bash`，也不启动
另一个本地后端作为回退。Planner/Verifier/read-only/Knowledge/checkpointer 不获得此工具。

本地 Docker 发布仍关闭；冻结制品可以审阅/丢弃，但不能直接写回真实 Workspace。
本轮没有新增文件发布权限、联网安装、后台服务、宿主 shell 或 Python Analysis 授权。

## 实现边界

- `WebExecutionRuntime.coding_bash_tool()` 仅构造请求级 `RunBashTool`；不自己准备快照、审批、
  创建容器、写独立 Bash 历史或管理生命周期。普通聊天保留全局 WebBashTool，Coding/Plan
  composition 将它替换为薄适配，不修改全局工具事实源。
- 适配器捕获服务端 Session/request ID；调用时要求对应任务仍存在，任务种类为 Coding/Plan、
  后端为 Local Docker，且当前 ContextVar 的完整身份、scope SHA 和 executor 角色匹配。
  预览没有 request ID，不能执行；旧适配器不能借用下一请求的上下文或副本。
- 真正的执行仍经过当前 SandboxOperation→GrantedOperationAccess→ExecutionService/Store。
  Bash、argv、编辑和固定验证共用 command CAS、累计命令数/时间预算、撤销与清理；
  已知非零退出保留任务以便修复，不重置预算。Stop/选择撤销会终止现有执行，不另起容器。
- Plan 的外层 composition 提供工具，Orchestrator 只交给 Executor；Planner 和 Verifier 使用
  固定只读集合。两轮 Executor 或多次修复仍指向相同已批准 operation。
- 审批卡新增 `task_bash_enabled` 展示，明确任务内 `run_bash` 与 `coding_run` 共享副本/预算、
  不再逐脚本确认；实际批准仍绑定原快照和资源选择。Coding/Executor 提示词说明了复用语义，
  没有放宽原有写文件、验证、冻结及独立发布要求。
- `RunBashTool` 保留匹配 command ID 的已知中断结果；signal 驱动的取消返回安全终态，
  外部 task 取消仍传播。没有修改固定验证或制品的证据类型。
- Workspace 说明区分 script/task approval，历史面板明确为 **Standalone Bash runs**。
  任务内调用没有第二份独立运行记录；Coding/Plan 全文私有运行日志仍是后续事项。

## 验证

| 检查 | 本轮结果 |
|---|---|
| Bash/执行相关后端 | **174 passed / 5 skipped**，253.53 秒；Web Task Bash、独立 Bash、Bash 门禁、Grant/Runtime、Web Execution |
| 产品组合/生命周期/预算邻接 | **63 passed / 3 skipped**，60.00 秒；Task Bash、CodingAgent Core/Architecture、Sandbox Lifecycle、Web/核心 Context Budget |
| 前端全量 | **226 passed / 33 files**，17.51 秒；含 Coding/Plan 两种共享 Bash 范围提示与单次确认 |
| Chromium | **26 passed**，2.2 分钟；完整审批/Workspace/会话/Knowledge/分析/Telemetry 浏览器回归 |
| 静态检查 | Ruff 通过；strict Mypy **287 source files** 通过；前端 ESLint、Vue 类型检查通过 |
| 构建 | 浏览器回归结束后自动恢复普通生产构建，无 E2E hooks 的生产产物已生成 |
| Evals | candidate gate PASS，6 suites / 10 配对案例；`.eval/workspace-bash-stage2f-2026-09-06/` |
| 差异检查 | `git diff --check` 通过 |

以上后端运行有重叠，不累加为唯一用例总数。跳过项是显式 opt-in 的真实 Docker 用例；
真实 Docker 另行执行。本轮未重跑全仓后端/覆盖率，不借用上一阶段覆盖率声称完整发布门禁通过。
没有新增依赖、安装/拉取/构建镜像或执行真实模型/E2B 网络请求。

### 真实 Docker

使用现有固定镜像：
`sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`。
测试使用 FakeClient 决定工具调用，但 Web/API/审批/SQLite/Workspace/Docker/验证/冻结/销毁均真实运行。
仅操作测试临时 Workspace，没有向用户真实 Workspace 发布文件。

1. Coding：编辑工具创建 Python；第一次 Bash 新建报告后退出 7；`coding_run` 用 Python 读取报告并
   断言内容正确；编辑工具更新 Python，第二次 Bash 同时检查新代码与旧报告并返回成功。
2. Plan：两个有依赖的任务分别由新的 Executor 执行上述前后半段，两个 Verifier 只读检查文件。
   两个任务、两次 Bash、argv 与编辑仍只有一个任务许可、一次确认和一个容器。
3. 验证负例：最后一条 Bash 返回 0，但源代码含语法错误；服务端固定检查失败，保留失败证据，
   无冻结 artifact，发布拒绝；请求结束后许可/容器关闭，真实 Workspace 不出现报告。

正向链路同时断言：ExecutionStore 的两个 Bash、两个 edit、一个 argv 和固定验证属于同一 task，
argv 退出 0，累计记录数等于命令预算计数；冻结证据绑定 `.pi-agent/sandbox.toml`、同 operation
和最终 workspace revision，全部固定检查 passed。容器只创建/销毁一次，独立 Bash 历史为空，
Docker 发布返回 409，保留的 diff 包含 Bash 与编辑工具共同生成的文件。

首轮两条基础链路 **2 passed**，162.77 秒。加强为“两个 Plan 任务 + 固定检查账目 + 无效代码负例”后，
正向两条通过；负例首次仅终态断言不符：请求收尾会将 validation_failed operation 关闭为 cancelled，
而非保持可运行的 validation_failed。代码确实已被拦截、失败证据保留且没有制品；修正测试以检查
既有关闭契约，不改生产验证/冻结逻辑。
加强后该轮为 **2 passed / 1 failed**，200.75 秒；修正断言后的负例单独复核 **1 passed / 13 deselected**，
51.66 秒。三种场景均获得通过证据，但不将分次结果描述成一次全绿的三项运行。
最终只读容器清单仅有 `pi-wiki-parser-worker-1`，状态 healthy；测试容器已回收，业务容器未重启。

### 离线边界与测试纠正

- 新增 Task Bash 专项覆盖标准任务卡、无逐脚本卡、未审批不创建、旧请求/预览不能执行、
  工具未选/E2B/disabled 不提供工具、Stop/工具选择撤销/后端撤销回收与无独立历史。
- 原公共门禁测试补充 Planner/Verifier/read-only 调用 Bash 的负例，仍在后端效果前拒绝。
- 首次离线回归有两项断言把 HTTP 请求 completed 等同于 Plan 执行成功；Plan 实际返回 blocked，
  无 artifact 且执行已回收。现在检查持久 Plan 状态、清理与制品，不改主流程去掩盖真实阻塞结果。
- 前端新增两项参数化用例确认 Coding/Plan 显示共享范围，不显示独立脚本卡，也不自动批准。

## 下一步

阶段 2 的任务内/独立 Bash 授权与执行装配已完成。下一步进入阶段 3：独立 Bash 文件证据与冻结、
Docker 制品确认发布；不得将 Bash 输出冒充 Coding 固定验证证据。
后台孤儿/TTL/缓存回收、全局配额、任务全文历史、管理员执行面板及执行 Telemetry 仍未完成。
本轮没有恢复执行或部署启用行为，也没有把冻结审阅当作已经写回 Workspace。

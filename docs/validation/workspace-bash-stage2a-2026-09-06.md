# Workspace Bash 阶段 2A：内部授权基础

日期：2026-09-06。本次是阶段 2 的内部基础，不是 Web 可用功能；阶段 2 整体验收尚未通过。

## 已实现

- `coding_agent_app/execution/models.py`：不可变 scope/context/command/grant；账号、Session、request、
  Workspace、task、operation 全绑定；固定后端/镜像、配置/策略、输入/基线、请求和 Plan 版本摘要。
- 独立 Bash scope 要求本地后端、精确脚本 SHA/cwd、单次执行，不能借用 Coding/Plan 许可。
- `store.py`：共用 Session SQLite 的连接协调器与 BEGIN IMMEDIATE；批准、启动意图、命令 preparing→running
  均在副作用前持久化。唯一索引约束 task/operation、Workspace 租约、单任务执行中命令。
- 执行前按完整 timeout 预扣预算，明确完成后退还未用时间；失败重试消耗新的命令额度。
  当前保守地将编辑也计入操作/时间额度，尚未接入固定验证预算预留策略。
- 同账号最多一个执行任务、本机最多两个；不确定清理形成持久 cleanup_pending，阻止新增执行。
- `service.py`：服务端角色/范围复验、命令 CAS、在途撤销/超时/取消、失败清理；未知结果不重试，
  对外仅稳定错误码。已知非零退出不谎报成功，许可内可以再次修复执行。
- 重启将 pending/approved/active 任务作废，命令标记 interrupted，返回需对账清理的任务身份。
- Plan 只有在提供**同一连接/同一事务**的可信审批适配时才能批准；适配缺失拒绝执行，抛错整体回滚。
  实际 PlanStore 适配尚未实现，不能把测试替身当作 Plan 产品已接通。

## 本次验证

| 门禁 | 结果 |
|---|---|
| `tests/test_execution_grants.py` | 41 passed |
| 后端全量 `python -m pytest -q` | **2463 passed, 7 skipped, 9 deselected**；1133.32 秒；coverage **77.18%** ≥ 75% |
| 最终：上述 + Local Docker / Workspace Backend / E2B / operation / admin | 154 passed，7.41 秒 |
| 最终专项：Local Docker + ExecutionGrant（含新补单文件限额用例） | 85 passed，5.78 秒 |
| `ruff check src tests evals scripts/check_bash_docker.py` | PASS |
| `mypy src evals scripts/check_bash_docker.py` | PASS，293 source files |
| 本地 `python -m evals --gate` | PASS；6 suites，10 paired cases；不是实际模型评测 |
| 真实 Docker | 14 项通过；见 [阶段 1 记录](workspace-bash-stage1-2026-09-06.md) |

授权测试覆盖跨连接批准/claim 竞态、身份各字段伪造、Planner/Verifier/只读角色、范围/能力伪造、
快照/配置过时、命令 payload 替换、旧副本 revision、重复批准/开始/终结、逐次 Bash、预算、过期/排队超时、
账号/全局/Workspace 并发、清理债务、重启、Plan 同事务回滚，以及运行中撤销/取消/超时与未知执行结果。

初次静态检查发现一处长行和两处 strict typing 问题（aiosqlite Iterable / create_task Awaitable），已修复并重跑通过。
Evals 输出：`.eval/workspace-bash-2026-09-06`，没有脚本或用户正文。
全量运行期间补充了单文件限额补丁，该补丁另行通过最终 154 项相关回归；不把最终定向测试数字累加冒充全量。
全量 coverage 排除真实 Docker worker 内部执行，真机检查单独保留为阶段 1 证据。
本轮没有修改前端，也没有重跑 Frontend/Chromium E2E；它们仍是后续产品接入后的门禁。

## 未接入的强制出口

1. 实际可信脚本文件投递（16 KiB/UTF-8/NUL/cwd 校验，任务用户不可修改的固定路径）。
2. SandboxOperation 的共享执行/编辑/固定验证门禁；覆盖 coding_run、自动修复、E2B，而非只保护新工具。
3. Coding 任务范围确认、PlanStore 精确版本 CAS 与批准事件、Web 不可变活动 request context。
4. 独立 Bash 编排及任务终结、服务端固定验证预算预留、角色化 `run_bash` 装配。
5. 阶段 3–4：输出证据/冻结发布、管理员配置与前端、日志/Telemetry、TTL/孤儿清理与启动对账。
6. 最终 Web/Frontend/E2E/全套交付门禁；不能用本轮内部授权测试替代产品端到端验证。

本轮未修改 Web/Coding/Plan/E2B 的既有执行行为，未打开任何功能配置，未提交 Git。

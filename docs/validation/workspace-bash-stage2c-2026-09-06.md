# Workspace Bash 阶段 2C：内部两段式任务生命周期

日期：2026-09-06。范围为本机 Web Agent 项目的内部实现，**Web Bash 尚未开放**。

按用户“先提交，然后进行下一步”的要求，先将前置上传、分析、记忆、压缩及 Bash 阶段 1–2B
整理为本地提交 `9518499`（149 files），提交后工作区干净；没有推送远端。
本记录对应随后完成的阶段 2C 改动，尚未再次提交。

## 已实现

- 新增服务端 `ExecutionTaskRuntime`，复用 Workspace BaselineProvider、Snapshot、SandboxOperation、
  ExecutionStore 和 PlanStore，不创建第二套 Workspace 文件事实源。
- `prepare` 先验证请求权限与显式启用配置，再生成精确输入快照并固定许可范围；
  `approve` 只批准精确范围，两步均不调用执行后端，不创建容器。
- `start` 在激活前复核请求、配置/资源选择、计划版本、原 Workspace revision/tree SHA 和快照归档；
  先持久激活许可，再创建并初始化运行环境。只有原快照播种完成且再次复核成功，才标记 runtime ready。
  任务激活后 Workspace 的新变更不会刷新或注入已批准副本。
- Coding/Plan 工具与薄 RunBashTool 通过服务端任务/角色上下文使用同一 Operation。
  已知执行失败后可在同一副本修复；Verifier 等只读角色不能编辑或执行。
  独立 Bash 固定脚本、cwd、单命令与批准超时预算，不能扩大授权。
- 实现一次启动、启动超时、取消、任务结束关闭许可与运行资源清理；取消覆盖快照播种，
  以及 SQLite 已提交准备记录但调用尚未返回的窗口。
- 重启恢复使旧许可失效，不恢复执行、不重放创建。无法确认创建结果或丢失进程内 handle 时，
  保留 `cleanup_pending`，除非显式提供的后端清理回调确认运行资源已不存在。
  不把未知创建失败当作“没有容器”，也不销毁与当前任务身份不匹配的返回 handle。
- WorkspaceStore 新增不重新物化副本的 revision/SHA 检查，仍逐文件验证普通文件、范围与内容。
  PlanStore 新增真实计划语义版本检查；准备/批准复用现有同事务绑定与批准接口。

## 最终验证

| 检查 | 结果 |
|---|---|
| 新增 `tests/test_execution_runtime.py` | **26 passed**，13.66 秒 |
| 最终合并相关 pytest 回归 | **398 passed / 2 skipped**，53.94 秒；未启用 coverage |
| Ruff `src tests evals scripts/check_bash_docker.py` | PASS |
| strict Mypy `src scripts/check_bash_docker.py` | PASS，283 source files |
| Evals candidate gate | PASS，6 suites / 10 配对案例 |
| 真实 Docker smoke | **25 项通过**，包含原 20 项及新增 5 项 |

合并回归覆盖 ExecutionRuntime、Bash、ExecutionGrant、LocalDocker、Workspace Backend/E2B/Snapshot、
包结构与架构、Sandbox Operation/Validation/Tools/Lifecycle/API/Admin/Artifact/Publisher、
E2B 配置脚本和 WorkspaceStore。权限、原始文件被外部改写、快照损坏、批准过期、配置/选择变更、
Plan 版本变更、并发启动、取消、未知创建结果与模拟重启均有拒绝路径测试。

2 个跳过是当前 Windows 账号创建宿主符号链接的能力限制；容器内 symlink 负例由真实 Docker 检查覆盖。
离线后端用于验证状态与调用顺序，不作为内核隔离证据。

首次真实生命周期烟测因 Windows 临时目录的 `ADMINI~1` 短路径与快照规范路径不一致而被
`baseline_required` 拒绝。修复为检查现有目录组件后规范化 staging 根路径，随后完整 25 项烟测通过。
首次单元测试中的导入、普通目录检查及快照哈希结果使用问题已修复，最终相关回归与静态检查通过。

Evals 产物：`.eval/workspace-bash-stage2c-2026-09-06/`；这些配对案例不衡量真实模型的 Bash 使用能力。
本轮没有重跑全量后端/coverage、Frontend/Vitest/构建或浏览器 E2E。
新生命周期未做真实 E2B 启动验收；既有 E2B 回归通过不代表该产品接入已完成。

## 真实 Docker 证据

使用与 [阶段 2B](workspace-bash-stage2b-2026-09-06.md) 相同的不可变运行镜像，
本轮没有安装依赖、下载或构建镜像。

- 基础镜像：`python@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134`。
- 运行镜像：`sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`，tag 为 `pi-bash-runtime:stage2b`。
- Docker Desktop Linux 引擎，数据仍位于 `D:\DockerData\DockerDesktopWSL`。
- 最终只读容器清单仅有 `pi-wiki-parser-worker-1`，状态 healthy；测试容器已回收，未停止或重启业务容器。

新增 5 项经过真实 WorkspaceStore → ExecutionTaskRuntime → ExecutionStore → Operation → Docker：

1. 准备与批准阶段按 operation 标签检查，确认没有创建容器。
2. 启动后固定快照可读，编辑与默认 RunBashTool 使用同一副本和任务上下文。
3. 嵌套 Verifier 绑定不能执行命令。
4. 任务正常结束关闭许可并确认容器不存在；原 Workspace revision/SHA 不变，没有自动发布。
5. 批准后、启动前修改原 Workspace，拒绝启动且不创建容器。

测试只使用临时 Workspace、SQLite 和测试容器，明确批准由受信任烟测代码提供，不冒充真实用户审批。
原 20 项隔离、执行许可、脚本保护、固定辅助程序、资源限制、冻结与清理检查一并重跑通过。

复现真实烟测（PowerShell，需显式 Docker 访问权限）：

```powershell
$env:PYTHONPATH = 'src'
D:\miniconda\envs\pipy\python.exe scripts/check_bash_docker.py `
  --docker-executable 'C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe' `
  --image-id sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6 `
  --verify
```

## 明确未完成与下一步

1. 阶段 2D：将可信账户/请求、管理员开关、Workspace 资源选择和版本化配置端口连接到实际 Web；
   实现 Coding 任务范围确认、Plan 合并审批、独立 Bash 完整脚本确认，再接入调度和角色上下文。
   当前默认拒绝/显式端口的内部组合不等于多账户 Web 产品已完成。
2. 新生命周期尚未接入旧 ManagedSandboxLifecycle。Coding/Plan 必须先完成固定验证与制品冻结交接，
   再调用销毁运行环境的 `finish`；不能直接替换现有生命周期而丢失待审批成果。
   Docker 发布与独立 Bash 证据仍属阶段 3，不新增直接写回旁路。
3. 未实现后台 Docker 孤儿/TTL 扫描器；恢复回调只是可信扩展端口，没有回调或无法确认时继续保留清理债务。
   关闭资源的内存记录与准备快照缓存仍需生产级生命周期/过期回收。
   共享 SQLite 的许可配额沿用现有 ExecutionStore，实际 Web 多账户组合与并发验收待完成。
4. 私有审批/运行历史、累计输出预算、管理员前端、Telemetry 和完整门禁仍待后续阶段。
5. `run_bash` 未注册，Bash 默认关闭；现有 Web E2B 未装配新许可，本机 Python 分析的独立环境与逐次确认不变。

状态、待办、变更日志和实现方案已同步。本轮除前置提交 `9518499` 外没有新提交或推送，未删除用户备份。

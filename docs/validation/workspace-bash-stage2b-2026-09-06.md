# Workspace Bash 阶段 2B：受保护脚本与公共执行边界

日期：2026-09-06。仅本机 Web Agent 项目的阶段性交付，**Web Bash 尚不可用**。

## 已实现

- `BashRequest` 严格字段、精确 UTF-8 字节哈希、16 KiB/NUL/路径/超时限制；拒绝模型传入授权、角色、环境、后端和挂载字段。
- `LocalDockerSandboxBackend.execute_bash` 在同一 operation 锁内先投递再执行固定脚本。
  `/run/pi-command` 为 1 MiB 控制 tmpfs，目录 UID 10000/0755、脚本 UID 10000/0444；
  任务 UID 10001 不能覆盖或删除脚本。脚本路径与 Bash argv 固定，不经宿主 shell 解释。
- 公共 `SandboxOperation` 通过中性 `OperationExecutionGuard` 接口和产品 `GrantedOperationAccess`
  保护 argv、Bash、写入/删除/patch 与固定验证。服务端 ContextVar 绑定请求/角色；
  只读角色不能编辑或执行。缺少 guard 的本地 Operation 默认拒绝；已绑定 guard 的其他后端同样受限。
- 编辑与验证共享任务许可、CAS 和预算；已知非零退出后可使用新 command 在同一副本修复。
  校验运行时启动完成、过期与撤销；清理不能确认时保持持久 cleanup_pending。
- 固定 Workspace 检查和制品导出 Python 改为 `python3 -I -S -B -c ...`，
  不加载任务目录中的 `json.py`、`sitecustomize.py` 或 site-packages 启动钩子。
- 实际 PlanStore 与 ExecutionStore 同连接/同事务绑定精确计划版本、批准计划、写审计事件并批准许可；
  任一失败全部回滚。已绑定计划拒绝旧 `approve` 接口和 operation 替换。
- 定义薄 `RunBashTool`，返回有界结果和稳定错误码，**未注册到产品**。

## 最终验证

| 检查 | 结果 |
|---|---|
| 新增 `test_bash_execution.py` | 57 passed |
| 合并相关 pytest 回归 | **314 passed / 2 skipped，31.78 秒**；未启用 coverage |
| Ruff `src tests evals scripts/check_bash_docker.py` | PASS |
| strict Mypy `src scripts/check_bash_docker.py` | PASS，282 source files |
| Evals candidate gate | PASS，6 suites / 10 配对案例；不衡量真实模型 Bash 能力 |
| 真实 Docker | **20 项检查通过**，包含旧 14 项及新增 6 项 |
| Git diff 空白检查 | PASS；仅已有 LF/CRLF 提示 |

合并 pytest 覆盖 Bash/ExecutionGrant/LocalDocker、Workspace Backend/E2B/Snapshot、包结构与架构，
以及 Sandbox Operation/Validation/Tools/Lifecycle/API/Admin/Artifact/Publisher 和 E2B 配置脚本。
新增测试证明拒绝路径不触达后端；Plan 事务回滚使用真实 SQLite/PlanStore，不是仅模拟 callback。
协议 Fake 测试不证明内核隔离；真实 Docker 结果单独列出。
2 个跳过均因当前 Windows 账号不能创建宿主符号链接；容器内 symlink 负例另有真实 Docker 验证。

20 项真实烟测完整通过两次。最后补充“投递期间取消 → 启动 task exec 前重新检查”的拒绝边界、
投递超时上限和启动前 TTL 检查；此补丁通过新增取消专项与最终 314 项回归及静态检查，未再复跑真实烟测。

初次专项运行出现测试构造问题（未设置符合本地限额的输出上限、误用 evidence.succeeded），
已修正为显式输出上限及 evidence.passed，最终合并回归通过。

Evals 产物：`.eval/workspace-bash-stage2b-2026-09-06/`。
本次未重跑全量后端、Frontend/Vitest/构建或 Chromium E2E；
此前 2463 passed / coverage 77.18% 属于阶段 1/2A 历史证据，不作为本次全量验收。

## 真实 Docker 证据

- Docker Desktop 的 Linux 引擎，数据仍在 `D:\DockerData\DockerDesktopWSL`。
- 基础镜像：`python@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134`。
- 测试 tag：`pi-bash-runtime:stage2b`。
- 不可变运行镜像 ID：`sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`。
- 增加 `io.pi-agent.bash-script=1` 能力标签；旧阶段 1 镜像不会被新脚本执行后端接受。
- 显式构建，无依赖安装；BuildKit 查询过 registry 元数据/auth，不能称为完全离线构建。
  仅 worker 白名单进入构建上下文；运行容器无网络、无宿主 Workspace 挂载。
- 容器原有 `/workspace` 256 MiB、`/tmp` 64 MiB 不变；控制 tmpfs 额外 1 MiB 同样计入总内存限额。

原 14 项：用户/环境/网络隔离，tmpfs 传输与任务复用，单向冻结，后台进程拒绝，
内核资源限制与 ENOSPC，已知失败后复用，合并输出限额，timeout/cancel/detached/OOM/symlink/hardlink/FIFO 拒绝与清理。

新增 6 项（通过实际 ExecutionStore → Service → GrantedOperationAccess → Operation → Docker）：

1. 缺少服务端执行上下文拒绝运行。
2. 编辑、包含中文/引号/换行/管道的 Bash 与 argv 读取同一个任务副本。
3. 任务不能覆盖/删除控制脚本，也不能伪装控制用户投递；新 shell 不继承旧 BASH_ENV。
4. 放入恶意同名 Python 模块后，固定只读辅助程序仍正常工作。
5. 固定验证与编辑共用同一许可，实际 10 次受保护操作消耗 10 个命令/副本序号。
6. Verifier 可读取但不能运行；撤销后拒绝执行并确认容器销毁。

测试仅使用临时目录和测试容器；没有启用 Web 功能或发布到真实 Workspace。
测试容器均已回收；原 `pi-wiki-parser-worker-1` 保持 healthy。

复现真实烟测（PowerShell，需显式 Docker 访问权限）：

```powershell
$env:PYTHONPATH = 'src'
D:\miniconda\envs\pipy\python.exe scripts/check_bash_docker.py `
  --docker-executable 'C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe' `
  --image-id sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6 `
  --verify
```

## 明确未完成与下一步

1. Web/Coding 的“准备精确快照与许可 → 用户批准 → 激活并创建副本”生命周期尚未装配。
   当前旧 E2B Web 路线仍沿用旧行为；不能宣称已经取得或被统一 ExecutionGrant 保护。
2. Plan Web 合并审批、Coding 任务范围确认、独立 Bash 完整脚本确认尚未实现；
   需要请求结束失效、执行角色绑定、禁止跨新请求复用 Session 旧副本，再注册工具。
3. Docker 产物的证据/冻结/单独确认发布、私有运行记录、累计输出预算、管理 UI/Telemetry 和后台 TTL/孤儿清理仍待后续阶段。
   暂停容器不依赖自身 PID 1 完成空闲 TTL，不能将当前 smoke 的清理等同于后台回收已实现。
4. 本机 Python 分析仍是独立环境与逐次确认，不因 Bash 实施改变权限。

本轮没有 Git 提交，没有删除旧镜像或其他用户备份，没有停止/重启业务容器。

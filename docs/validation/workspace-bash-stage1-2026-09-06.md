# Workspace Bash 阶段 1 验证记录

日期：2026-09-06。当前状态：阶段 1 后端 14 项真实 Docker 验收通过；阶段 2A 授权基础已实现，Web 产品入口仍未接入。

## 环境恢复后的真实验收（最新）

- Docker Desktop 4.87.0 / Linux Engine 29.7.2，WSL2、cgroup v2、runc；原解析容器健康。
- 基础镜像：`python@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134`，使用已存在的层。
- 已验收运行镜像：`sha256:9f78ba21d30f078e429e74c4a89ce4d93fcca93b102cea8d02a9ea148f39e69f`；
  `pi-bash-runtime:stage1` 仅便于管理员定位，运行配置必须使用完整固定 ID。
- 构建没有安装依赖或下载新基础镜像层，但初次 BuildKit 访问了仓库元数据/auth，**不是严格离线构建**。
  `--network=none` 只限制构建 RUN；构建上下文白名单实际仅传输了约 6 KiB 的 worker 源码。
  依据：[Docker RUN networking](https://docs.docker.com/reference/dockerfile/#run---network)。
- 使用项目 Python、明确 `--docker-executable`、上述 `--image-id` 和 `--verify` 运行固定检查脚本。
- 最终输出 `execution_verified=true`、`reuse_verified=true`、`feature_enabled=false`，14 项均通过：

| 检查组 | 真实结果 |
|---|---|
| 用户/网络/环境 | UID 10001，无 Docker socket，不继承宿主环境，外连失败 |
| 文件副本 | 上传/执行/下载 SHA 校验，跨命令复用，暂停恢复保留 tmpfs |
| 冻结 | tmpfs 导出，冻结后不再接受执行或上传 |
| 后台/脱离进程 | 普通后台及新 session 的残留进程均拒绝复用，整个测试容器销毁 |
| 内核限额 | CPU/memory/swap/PID cgroup 值、无 capability、no-new-privileges/seccomp、不可写根、不可 signal PID 1 |
| 临时盘 | 64 MiB tmpfs 实际 ENOSPC，256 MiB Workspace tmpfs 容量确认 |
| 失败修复 | 已知非零退出保留任务，下一条命令可成功 |
| 输出 | stdout/stderr 合计超过限额后持续排空丢弃；回调和结果均有界 |
| 超时/取消 | 终态正确，CLI 与整个容器均回收 |
| OOM | 128 MiB 测试容器内触发内存耗尽，禁止继续复用并销毁 |
| 不安全输出 | symlink/hardlink/FIFO 的冻结失败，不导入宿主，不遗留容器 |

失败路径先检查容器确实不存在，再进入 finally 清理，避免由最终清理掩盖产品缺陷。
最终 `docker container ls --all` 仅剩 `pi-wiki-parser-worker-1`，健康；未 prune 或触碰业务容器。

验收发现并修复：

1. Python `TimeoutError` 是 `OSError` 子类，原 transport 错误归类为 provider_unavailable；现在保留 timeout 语义。
2. Docker `State.OOMKilled` 不足以覆盖 PID 1 存活时 exec 子进程 OOM；增加只读镜像 health reader，
   在无存活用户进程时检查 private cgroup v2 的 `memory.events`，OOM 后整任务作废。
3. 固定 `--ipc=none` / `--cgroupns=private` / `--runtime=runc`，不额外开放 `/dev/shm`；严格重验 tmpfs 选项和嵌套元数据。

Backend 初次扩展离线测试 43 passed，与既有 Backend/E2B/operation/admin 回归共 112 passed；
加上阶段 2A 授权测试 41 项后，相关回归 **153 passed**。随后补齐单文件上传限额（与总上传限额取小值），
最新 Backend 44 / 授权 41 专项共 **85 passed**，全部相关回归重跑 **154 passed**。
Ruff / strict Mypy 293 files / 本地 Evals gate 通过。
全量后端结果见 [阶段 2A 验证记录](workspace-bash-stage2a-2026-09-06.md)。

限制：本记录证明本机后端可行性，不证明敌对多租户零逃逸。暂停容器的 PID 1 不能自行调度 TTL 退出；
独立 TTL/孤儿清理器、服务启动对账仍是产品启用前的强制阶段 4 工作，不能仅依赖 worker sleep。
没有启用 Web 配置、注册工具或改变 E2B/Python；没有提交 Git。

以下保留初版实施与环境阻塞历史；其中“未执行/未构建”描述的是环境修复前的事实，不代表当前状态。

## 本轮实现

- `src/coding_sandbox/docker_transport.py`：固定本地 endpoint、独立空 Docker config、受限宿主环境、
  argv 子进程、有界 stdin/stdout/stderr、超时/取消时清理 CLI；不使用 shell、隐式 context 或镜像拉取。
- `src/coding_sandbox/local_docker.py`：固定安全规格、实际容器配置重验、精确所属容器清理、命令去重、
  不同 UID 的固定 PID 1/用户执行、暂停与无残留进程检查、临时传输、单向冻结和 cleanup_pending。
- `src/coding_sandbox/local_worker.py`、`docker/bash-runtime/Dockerfile`：只读镜像中的 stdlib 辅助程序，
  无 Web/Provider/MCP 依赖；不使用 `docker cp` 读取 tmpfs，不向真实 Workspace 发布任何文件。
- `src/coding_sandbox/admin/local_store.py`：独立、版本化、CAS 更新的本地配置，不修改 E2B 配置。
- `scripts/check_bash_docker.py`：默认只读探针；`--verify` 才显式运行一次性 backend smoke；不安装或启用功能。

## 实际执行的检查

| 检查 | 结果 |
|---|---|
| `tests/test_local_docker_backend.py --no-cov -q` | 36 passed |
| 上述测试 + `test_coding_workspace_backend.py` / `test_coding_workspace_e2b.py` / `test_coding_sandbox_operation.py` / `test_coding_sandbox_admin.py` | 105 passed，1.01 秒 |
| `ruff check src tests evals` | 通过 |
| `mypy src evals` | 通过，288 source files |
| `ruff check src/coding_sandbox tests/test_local_docker_backend.py scripts/check_bash_docker.py` | 通过 |
| `mypy src/coding_sandbox scripts/check_bash_docker.py` | 通过，27 source files |
| `scripts/check_bash_docker.py`（初查 PATH） | `available=false, error_code=docker_unavailable`；不是成功 smoke |
| 用户提供的安装目录 / `docker.exe version` | CLI 29.7.2、windows/amd64；Linux 引擎连接失败 |
| 显式启动 Docker Desktop 后检查两个本地 named pipe | `docker_engine`、`dockerDesktopLinuxEngine` 均不存在；Desktop 已退出 |

Python：`D:\miniconda\envs\pipy\python.exe`；运行设置 `PYTHONPATH=src`。
使用 `--no-cov` 的相关回归不构成全量 coverage 门禁。没有运行本轮完整后端、Frontend、E2E 或真实模型 Eval。

初次静态检查发现 timeout 参数 lint、长行和 Linux 常量的 Windows mypy 类型问题，已修复后重跑。
首次新增测试 36 passed；随后加强 PID 1 的 UID 隔离并跑 105 个相关回归。上述最终静态检查通过。

## 离线覆盖及限制

覆盖默认关闭、不变镜像、无创建副作用探针、运行配置漂移（网络、挂载、能力、PID、内存等）、
失败命令后的修复、重复 command ID、错误 cwd、残留子进程、超时/取消、daemon 失联清理标记、
跨所属容器的伪造 handle、输入/输出 hash、冻结后拒绝执行/上传、重启不恢复，以及 SQLite CAS/E2B 不变。

Fake transport 不启动容器或执行脚本，不能证明 Docker Desktop 的资源限制、进程清理或 tmpfs 导出已可靠工作。
只读探针即使发现 daemon/镜像，也返回 `execution_verified=false` / `reuse_verified=false`；不自动开启工具。

## 环境阻塞历史及恢复

初次 PowerShell 无法解析 `docker`，常见安装位置未找到 CLI。用户随后提供 Desktop 路径，
本轮通过获批的目录读取定位到：

`C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`

获批后尝试启动现有 Desktop（隐藏窗口方式，未重新安装），但 Linux 引擎未就绪。
本机 2026-09-06 01:05 启动日志记录：`initializing Ingest server` 时无法移除/访问
`C:/Users/Administrator/AppData/Local/Docker/run/sailor-ingest.sock`，错误为
`The file cannot be accessed by the system`。随后 Desktop 退出。只读文件检查显示该项为
0 字节、`Archive, ReparsePoint`；尚未确定底层原因，不能直接认定为普通文件或安全删除目标。

日志另记录 GUI `Reset to factory defaults` 动作及应用重置；本任务未发出重置、删除或更新命令。
由于 daemon 不可连接，无法核实既有镜像/容器状态。未运行测试容器，未拉取/构建镜像，
未修改 Docker 配置或移除上述运行文件。项目外的环境修复需用户另行确认。

`docker cp` 对 tmpfs 的限制来自 [官方文档](https://docs.docker.com/reference/cli/docker/container/cp/)。
相应实现采用无存活用户进程时的固定读取器，而非解冻用户执行；该调整仍需真实 Docker 验收。

## 后续产品接入

1. 用户随后批准修复与迁移，Docker Linux 引擎已恢复，原镜像/容器标识和解析容器健康状态已核对；
   数据现位于 `D:\DockerData\DockerDesktopWSL`，详见 [迁移记录](docker-data-migration-2026-09-06.md)。
   固定运行镜像与真实后端验收已完成，详见文首最新记录。
2. 阶段 2A 内部授权基础已实现；下一步继续公共执行门禁、审批与 `run_bash` 产品接入，再完成制品/Web/Telemetry/完整门禁。
3. 当前没有 `run_bash` 注册、任务授权 UI 或本地发布入口，不对用户宣称功能已可用。
4. 本轮未改现有本地 Python 分析行为，未替换 E2B 默认后端，未提交 Git。

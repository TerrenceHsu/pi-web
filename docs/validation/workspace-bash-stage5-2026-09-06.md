# Workspace Bash 阶段 5：完整门禁与交付

日期：2026-09-06。先按用户要求将阶段 4 提交为 `7908132`；记录当时阶段 5 尚未提交。
交付更新：阶段 5 改动随后续工程化收敛整理本地提交，未推送；下列数字仍保留当时批次，不冒充最新结果。
状态：完整门禁与交付验收已完成；最后一轮 Chromium 关闭重试、26 项全部通过，生产构建已恢复。

## 本轮变更

- 为 12 项真实 Docker 用例补齐 `docker` marker，保留固定镜像环境变量显式选择。
  默认离线门禁即使继承测试镜像变量，也不会启动这些容器。
- 复用已有真实清理用例，扩展为同一账号两个 Workspace 计算副本同时存活：
  各自文件不可互读/覆盖、仅 loopback 网络；撤销一个不删除另一个，最后继续验收孤儿回收。
- 真实复跑发现请求收尾与维护线程同时销毁同一任务的竞态。新增一个确定性回归（先红后绿），
  Runtime 按 task ID 串行清理并在确认成功后设置 closed；失败/未知回收仍保留重试与债务。
- 将既有 Uvicorn 测试的进程就绪等待由 5 秒改为 20 秒、使用 monotonic 时钟并保留超时诊断；
  没有修改产品命令、审批、HTTP 超时，也没有跳过该用例。
- 修正压缩测试 Fake 流的 entered/finally 位置，使 SQLite 初始化等待也被生命周期观察覆盖；
  保持原 20 ms 超时测试不变，不修改实际压缩策略或生产 Provider。
- 更新测试指南、API 部署边界与交付记录；不更换镜像、不增加依赖、不降低覆盖率或审批门槛。

## 执行记录

| 检查 | 当前结果 |
|---|---|
| Backend 全量、覆盖率 ≥ 75%（含分支统计） | `.test-tmp/c5d`：2650 passed / 7 skipped / 21 deselected，1796.88s；coverage **78.41%**，PASS |
| Ruff `src tests scripts evals` | PASS |
| strict Mypy `src evals` | PASS，306 source files |
| Wiki Parser Worker Ruff / strict Mypy | PASS，16 source files |
| 依赖锁一致性 | `uv lock --check --offline` PASS，115 packages；未安装/更换依赖 |
| Evals | PASS，6 suites / 10 pairs；修复后重跑 `.eval/workspace-bash-stage5-final-2026-09-06` |
| Frontend lint / typecheck / production build | PASS |
| Frontend Vitest | 232 passed / 34 files |
| 完整 Chromium E2E | 最终端口 8101：26 passed，2.3m，显式 `--retries=0`；随后 production build PASS |
| 真实 Docker 底层 smoke | 修复前、修复后均为 25 checks PASS；`scripts/check_bash_docker.py --verify` |
| 真实 Docker Web 与维护 | 最终 `.test-tmp/c5r4`：12 passed / 62 deselected，651.15s；初跑/修复过程见下文 |
| 授权、恢复、维护、发布定向回归 | `.test-tmp/c5fix`：114 passed / 1 deselected，50.96s |
| 压缩测试夹具回归 | `.test-tmp/c5ctx`：23 passed，10.55s |

最终后端使用 `python -m pytest tests --tb=short -q --maxfail=1 --basetemp=.test-tmp/c5d`，
`PYTHONPATH=src`，解释器为 `D:\miniconda\envs\pipy\python.exe`。覆盖率来自该单次完整运行；
`.coverage.xml` 于 22:22:01 更新，39169 条语句与 11102 个分支的合并覆盖率为 78.41%。
7 项跳过不计为通过；21 项 deselected 包含需显式选择的 12 项 Docker 用例及 9 项其他非默认用例。

Ruff 初次命令附带一个不存在的根目录 `dev_web.py`，只产生 E902；改为实际存在的
`src tests scripts evals`（包含 `scripts/dev_web_app.py`）后通过。没有为此修改或排除源码。
真实 Docker 首轮 `.test-tmp/c5r` 为 11 passed / 1 failed / 62 deselected，621.07s；
失败用例已经观察到只剩另一个 Workspace 的精确 operation/image，但测试误用 list 与实际 tuple 比较。
修正断言类型后双副本补测通过，未修改产品清理规则。不同批次不合并成一次门禁数字。
增加并发回归前，带镜像环境变量的默认收集为 2656 / 2677（21 deselected，含 12 项 Docker）；
显式 `-m docker` 则只选中 12 项，确认默认离线执行边界不依赖操作人员清空环境变量。

### 失败定位与复跑

- 完整后端首轮 `.test-tmp/c5a`：2648 passed / 1 failed / 19 skipped / 9 deselected，
  1396.99s。唯一失败为 Uvicorn 子进程连续三次未在 5 秒内监听端口，旧测试未保存超时诊断。
  同组定向 31 passed / 18.77s；修正测试就绪等待后单项 1 passed / 9.43s。
- 后端 `.test-tmp/c5b` 在真实 Docker 又发现清理竞态后废弃，不作为验收或覆盖率证据。
  停止前校验 PID 30772 的完整命令与无子进程；PowerShell 停止方法异常后以 .NET 结束同一测试进程。
  后续 `.test-tmp/c5c` 出现既有压缩测试超时分支的夹具竞态，也已停止（验证 PID 29592 且无子进程）。
  该 Fake 流将 entered 信号和 finally 放在 SQLite await 之后，20 ms 超时可能在两者之前打断；
  修正观察边界后整组 23 项通过。最终 `.test-tmp/c5d` 从新源码重新收集运行，
  不使用 coverage append 或跨批次覆盖率，废弃的部分运行不提供全量结果。
- 真实 Docker `.test-tmp/c5r2`：双副本修正断言后 1 passed / 14 deselected，16.02s。
  后续完整 `.test-tmp/c5r3` 为 9 passed / 3 failed / 62 deselected，632.20s：
  非零退出、软链接输出、Coding 固定验证失败三个场景均只创建一个容器，却并发调用了两次销毁。
  原 Workspace/发布屏障未被绕过，但存在重叠的容器存在性检查/删除，必须修复。
- `test_request_finish_and_maintenance_share_one_cleanup` 用事件屏障稳定复现两次 destroy；
  修复前失败，修复后纳入 114 项相关回归通过。保持真实测试原有单次回收断言，未通过放宽测试掩盖竞态。
- Chromium 端口 8100 在批准已完成后的只读 approvals GET 遇到一次 `ECONNRESET`，场景重试后通过；
  25 passed / 1 flaky，2.8m，生产构建已恢复。不把该轮描述为零重试；未为此重试写请求或修改产品审批。
  随后以端口 8101、`--retries=0` 独立执行完整套件，26 passed / 2.3m，包括该审批场景首次通过。
  无测试代码或重试配置变更，最终生产构建再次通过。

本轮无新增测试文件：新增 1 项并发清理回归、扩展 1 项真实双副本用例，另修正既有
Uvicorn/压缩 Fake 辅助代码；12 项真实 Docker 只补环境 marker，未扩大参数矩阵。

### 证据分层

- Backend / Evals：真实 Python Agent、SQLite、Workspace 与状态机，外部 Provider/云 Sandbox 使用确定性 Fake。
  不证明真实 LLM 模型选择、E2B 云端或 MinerU GPU 运行质量。
- Chromium：真实浏览器、临时本机 Web 服务与生产组件；Provider/部分服务响应为 Fake。
  证明审批、刷新恢复、角色隔离和界面交互，不冒充容器执行证明。
- 真实 Docker：使用现有本机镜像
  `sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`；
  Windows Docker Desktop Linux Engine，全部 Workspace、账本和待发布文件为临时测试数据。
  Web 层仍使用 Fake 模型发出确定性工具调用，但容器执行、固定验证、签名与 Workspace 发布都是真实链路。

## 验收场景对应

以下编号对应 [实施方案](../design/workspace-bash-tool.md) 第 11 节，复用既有测试，不复制新的大矩阵。

| 场景 | 主要证据 |
|---|---|
| 1–6：默认关闭、明确范围批准、精确输入、角色限制和重放拒绝 | `test_execution_grants.py`、`test_execution_runtime.py`、`test_web_execution.py`、`test_web_bash.py`、`test_web_task_bash.py` |
| 7–8：撤销、到期、预算、并发/账号/Session 隔离 | 上述授权测试与 `test_execution_maintenance.py`、Auth/Web 既有套件 |
| 9–10：副本内 upload、同任务多工具、精确脚本字节 | 真实独立 Bash 与 Coding/Plan Web 测试；25 项底层 smoke 中的 Bash/argv/Unicode/脚本保护 |
| 11–12：隔离、资源限制和子进程回收 | 真实 Docker 双副本测试；底层 smoke 的非 root、网络/socket/环境、seccomp/cgroup、ENOSPC/OOM/日志、超时/取消/脱离进程组检查 |
| 13–16：安全归档、精确发布、冲突、幂等和恢复 | `test_bash_publication.py`、Sandbox/Workspace 既有测试、真实安全输出负例与 Coding 固定验证失败用例 |
| 17：E2B、Python、MCP、Skills、上传、压缩不回归 | 全量离线 Backend、6 组 Evals、232 项 Vitest 与完整 26 项 Chromium |

底层 smoke 还核对：请求准备/批准不创建容器、批准后使用原快照、失效快照零创建、
Verifier 无执行权、任务内共享副本与预算、任务关闭不自动发布。25 checks 是一个脚本的检查数，
不是 25 项 pytest；不与 Web 测试或重复补测累加成“总通过数”。

## 交付边界

本轮未开启 `PI_LOCAL_DOCKER_ENABLED`、未重启业务 Web/解析容器、未拉取或重建镜像，
未发布到业务 Workspace。Docker 数据仍在 `D:\DockerData\DockerDesktopWSL`。
真实验收结束后只读核对容器：仅原业务 `pi-wiki-parser-worker-1` 运行且 healthy，测试容器全部回收。
未调用真实远端 Provider/E2B/DDGS/MinerU GPU；HTTP MCP、Skills 与独立 Python 产品设计保持不变。
后台维护依赖本机 Web 进程，未知结果保留 `cleanup_pending`；不安装系统清理服务、不重放执行。
联网包安装、交互式终端、长期服务器、跨请求复用、部分发布及自动冲突合并仍不在本轮范围内。

重跑命令与双重 opt-in 见 [测试指南](../guides/web-testing.md)。

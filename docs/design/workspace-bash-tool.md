# Workspace Bash 工具实现方案

> 状态：阶段 3 已提交 `5ec6622`；阶段 4 随本次提交归档；阶段 5 完整发布门禁待完成。
> 日期：2026-09-05；实施进度更新：2026-09-06。
> 范围：本机 Web Agent；Coding/Plan 统一任务执行授权，本地 Docker Workspace 副本中的 Bash 工具。
> 本次修订：Coding/Plan 每任务授权并复用任务副本；独立 Bash 逐次确认；发布仍需单独批准。

当前事实：新增本地 Backend、固定镜像源码、独立 revisioned 配置、只读探针和显式 smoke 脚本。
阶段 2D 已将 Web Coding/Plan 纳入每请求执行许可；阶段 2E 注册独立 `run_bash`，Python 授权行为不变。
阶段 2F 同时向选中本地 Bash 的 Coding/Plan Executor 提供请求绑定适配器，与 `coding_run`
共用当前任务许可、预算和文件副本；不逐脚本弹窗，不启动另一容器，也不借用独立 Bash 执行路径。
阶段 2E 曾只保留 stdout/stderr 与摘要；阶段 3 已接入独立 Bash 的专用文件证据和签名冻结，
同时启用 Docker 制品的单独确认发布。Coding/Plan 保持固定验证，完整性证据不能替代测试证据。
初查 PATH 未找到 Docker；随后依据用户提供的非标准安装路径找到 CLI 29.7.2。
Desktop 曾因旧 socket 无法访问而退出；获准保留并重建运行目录后已恢复，WSL 数据通过官方设置
迁至 `D:\DockerData\DockerDesktopWSL`。见 [迁移记录](../validation/docker-data-migration-2026-09-06.md)。
固定 Bash 镜像已构建；阶段 2C 的 25 项后端 smoke 为历史证据。本轮新增 Coding/Plan 两条真实 Docker
Web 链路，验证批准后启动、固定验证/冻结、回收计算资源后仍可审阅 diff。E2B 原发布流程保留，
上述历史阶段的 Docker 发布尚未开放；阶段 3 已补齐。阶段 2E 另通过真实 Web→脚本审批→Docker→回收链路；
本轮未自动启用真实部署配置。见 [阶段 2D 记录](../validation/workspace-bash-stage2d-2026-09-06.md)
和 [阶段 2E 记录](../validation/workspace-bash-stage2e-2026-09-06.md)。阶段 2F 的任务内混合执行验证见
[阶段 2F 记录](../validation/workspace-bash-stage2f-2026-09-06.md)。

## 1. 结论与首版边界

新增可选工具 `run_bash`，与 Coding/Plan 共用任务执行许可 `ExecutionGrant`，
采用“确认执行范围 → 隔离任务内执行 → 冻结变更 → 单独确认发布”。
普通文件查看、搜索和编辑继续使用现有 Workspace 工具，不必经过容器。

- 管理员配置本地执行能力，用户在自己的 Workspace 中选择；两级默认均关闭。
- Coding 进入执行前授权一次；Plan 的“批准计划”与“授权本次隔离执行”合并为一次明确确认。
- 普通聊天独立调用 Bash 仍逐次审阅脚本；它是仅含一次命令调用的隔离任务。
- 只有实际需要执行且获得授权后才创建任务容器；打开页面、切换模式、选择工具、调用 Python 分析均不启动容器。
- Bash 仅在本地 Docker 中执行，不使用宿主机 Git Bash、PowerShell、cmd 或 WSL 命令作为回退。
- 本地 Docker 首版固定无网络，不提供联网开关、动态安装依赖、交互式终端、常驻服务或后台任务。
- Coding/Plan 在同一任务、同一 operation 内复用容器和文件副本；每条命令仍是新进程，不继承 shell 变量或 `cd`。
- 独立 Bash 每次调用使用新容器与快照；不同任务、不同 Workspace 和后端重启之间都不继承副本或许可。
- 保留 E2B 后端、配置与 Coding 验证/发布流程，为 Coding/Plan 增加统一任务授权门禁；不把旧 E2B 配置静默迁移到 Docker。
- 本地 `run_python_analysis`、固定 `analyze_data`、HTTP MCP 和 Skills 的现有授权行为不变。
- Bash 与 Python 分别启用、分别审批。Python 工具不会因为需要分析而自动取得 Bash 能力。
- 面向受信本机用户，不将普通 Docker 容器宣传为可承载敌对多租户的绝对安全边界。

“一次授权”指一次任务内的执行，不包括最终发布，也不包括后续聊天、后端切换或永久模式授权。

## 2. 源码现状与差距

下表记录方案制定时（2026-09-05）的源码基线；实施后的状态以文首和阶段出口为准。
旧设计中“全部代码仅在 E2B 执行”的描述不涵盖后来加入的本地 Python 工具。

| 位置 | 已有能力 | 本方案需要补足 |
|---|---|---|
| `src/coding_sandbox/backend.py`、`models.py`、`config.py` | 异步 Backend 契约、argv 执行、取消/输出/资源 DTO；已预留 `local_docker` 标识且不要求 Provider 密钥 | 实现真正的本地 Docker Backend；标识存在不代表后端已实现 |
| `src/coding_sandbox/admin/models.py`、`service.py`、`runtime.py` | 现有管理配置和默认工厂仍绑定 E2B，启动组合依赖 Credential 子系统 | 增加独立的本地执行配置和运行时，不要求 E2B API Key，不覆盖旧配置 |
| `src/pi_agent_core_py/tools/coding_sandbox.py` | `coding_run` 接受 argv，可在所选后端执行程序 | `run_bash`、`coding_run` 及验证执行统一检查任务许可；argv 接口本身不能阻止调用解释器 |
| `src/coding_agent_app/sandbox/automation.py` | Code 模式自动准备、验证、冻结，要求先写代码且产生变更 | 普通 Bash 不能套用该强制写文件编排；无变更的检查命令应正常完成 |
| `src/coding_agent_app/planning/orchestrator.py` | Planner/Verifier 只读，用户批准计划后 Executor 使用隔离 Coding 工具，可有界修复重试 | 将计划批准与执行范围批准原子绑定；仅 Executor 获得任务执行能力，计划范围变化重新确认 |
| `src/coding_agent_app/sandbox/workspace.py`、`src/agent_workspace/store.py` | Session 快照、逻辑路径物化、签名制品验证、revision/SHA 冲突与事务发布 | 为 Bash 接入同一事实源和 Publisher；禁止直接复制覆盖物理存储 |
| `src/coding_sandbox/artifact.py`、`validation.py`、`lifecycle.py` | 制品绑定固定 Coding validation evidence，具有持久 CAS 与冻结屏障 | 区分 Bash 输出完整性检查与 Coding 测试验证，不伪造验证通过或取消旧验证要求 |
| `src/pi_agent_core_py/web/approvals.py`、`web/app.py` | 精确 ToolCall 的 Web 确认；Python 已有服务内强制确认；Coding 使用收窄工具集和 AllowAll | 增加任务范围确认类型、持久许可和逐调用检查；AllowAll 不能替代执行授权 |
| `src/coding_agent_app/core/resources.py`、`toolsets.py`、`web/extension_store.py` | Workspace 可选工具和模式装配 | 区分独立 Bash、Coding、Plan Executor；read-only、Planner、Verifier、checkpointer 不获得通用执行能力 |
| 前端 `WorkspaceExtensions.vue`、`ApprovalCard.vue`、Sandbox/Changes 组件 | 工具选择、确认卡、输出及变更审阅 | 增加本地可用性、脚本确认、运行结果与发布状态，复用组件但隔离不同任务状态 |

当前 Workspace 底层是 file-ID、metadata 和不可变内容 generation，不是可以直接挂载的用户逻辑目录。
容器内 `/workspace/upload/…`、`scripts/…`、`artifacts/…` 来自快照物化，不创建第二份持久事实源。

## 3. 用户体验和权限模型

### 3.1 管理员配置

在管理页面增加“本地命令执行”配置，展示 Docker/镜像状态、固定运行环境、限额及风险说明。

- 配置模型建议为独立的 `LocalDockerExecutionConfig`，不把原 E2B singleton 静默切换为 Docker。
- Coding/Plan 的任务配置显式选择已配置的执行后端，创建许可后固定不变。Docker 工具选择不自动开启 E2B，
  E2B 授权也不启动另一个 Docker operation。`run_bash` 首版仅在 local_docker 任务中提供。
- 普通账号只能查看自己可用的能力和 Workspace 选择，不能更换 Docker endpoint、镜像、挂载或安全参数。
- Docker endpoint 由本机部署者固定为本地 Unix socket/Windows named pipe；不读取模型参数，
  不接受任意远程 `DOCKER_HOST`、Docker context 或 TCP endpoint。
- 镜像由管理员预先构建/导入，使用不可变 image ID/digest。审批后再次确认解析到同一镜像。
- 基础镜像只包含 Bash、常用文件工具、Python 及固定辅助程序；不装入 Web 应用、Provider/MCP 凭证。
  不承诺包含 Node、ffmpeg 或完整数据分析库；后续按明确需求增加经过构建验收的环境。
- 安装 Docker、拉取/构建镜像是部署动作，须单独执行和确认；聊天请求不能自动触发。
- 页面状态探针只检查 daemon、镜像与配置，不运行用户代码；管理员显式“测试环境”才启动短时探针容器。

### 3.2 Workspace 和聊天

Workspace → Extensions → Tools 增加 `Bash · Docker · 按任务授权`，默认不选中。
说明文字明确“Coding/Plan 任务内免重复确认，独立调用逐次确认”。选中或切换模式不等于批准执行。
同一任务中该工具只有在管理员能力、Workspace 选择、角色工具集与任务许可同时允许时才能调用。

| 场景 | 用户确认 | 可执行范围 |
|---|---|---|
| Plan 规划 / Verifier 审阅 | 无执行授权，不弹 Bash 卡 | 现有只读工具，不提供通用 Bash 或 `coding_run` |
| Plan 执行 | “批准此版本计划并允许本次隔离执行” | Executor 在该任务副本中编辑、运行、测试和有界修复 |
| Coding 直接执行 | “允许本次 Coding 任务在隔离副本执行” | 当前任务内的受限 Coding/Bash 工具，命令可由 Agent 动态提出 |
| 普通聊天独立 Bash | “批准本次脚本” | 精确脚本、cwd 和快照，一次调用可含多条 shell 命令 |
| 写回真实 Workspace | 独立审阅完整 diff 并确认发布 | 精确冻结制品及目标 revision，不授予新的执行权 |

任务确认卡展示：用户目标、Plan 版本/内容（如适用）、输入快照 revision/hash 与文件范围、执行后端/
镜像、工具能力、网络策略、可发布路径、时间/命令数/资源预算，以及“未来命令由 Agent 决定，
仅修改临时副本，未授权写回”。不能用仅写“批准计划”的模糊按钮代替该明确说明。
独立 Bash 确认卡另展示完整脚本、精确 cwd 与脚本 SHA。均不提供永久允许。

权限范围不是任务目标的语义保证。服务端能强制隔离、工具、输入、预算和发布边界，但不能仅凭
“符合计划”自动证明任意 Bash 命令的目的安全；若用户需要审阅每条脚本，应使用独立 Bash 路径。

运行卡展示 stdout/stderr、退出码、耗时、取消/超时原因和变更数量。
存在可发布变更时，右侧 Changes 展示全部变更，用户可确认发布或丢弃。
首版只做整批发布，不做局部勾选、自动合并或自动覆盖冲突。

### 3.3 不可绕过的统一任务许可

新增服务端持久 `ExecutionGrant`，类型为 `task` 或 `single_command`；浏览器确认一次产生许可，
不是给每条命令生成自动批准点击。许可 ID 由服务端上下文传递，模型不能指定或复用其他任务的许可。

共同绑定字段：账号、Session、活跃 request、execution task/operation、基线 revision/tree SHA、
后端及镜像/runtime、策略与配置版本、输入集合、工具能力、发布路径策略、预算和到期时间。
`task` 另绑定 Coding 用户请求 hash 或 Plan ID/规范版本 hash；`single_command` 另绑定精确脚本 SHA 和 cwd。

- 激活前重新检查真实 Workspace 基线及全部授权条件；排队期间发生变化使旧确认失效。
- 激活后任务副本中的授权编辑不使许可失效，否则无法完成“编辑 → 测试 → 修复”。初始基线仍不可更换。
- 运行中真实 Workspace 的外部变化不自动注入副本；任务可继续使用旧快照，但最终发布会冲突。
  要改用新快照，需要结束旧任务并重新确认，不能通过自动同步扩展旧许可。
- 逐调用重验账号/request/operation/角色、能力开关、策略、有效期与剩余预算；没有有效许可直接拒绝。
- 同一许可不得切换账号、Workspace、后端、网络或输入范围；Plan 的语义内容/范围/验收标准改变需新版本和新确认。
  任务进度、执行日志及原验收范围内的有界修复不视为计划改版。
- 关闭能力、撤销选择、取消、过期、预算耗尽、任务终结或后端重启均使许可不可再用；
  活跃任务必须终止/回收，不仅从下一次工具列表中隐藏工具。
- 不支持的联网、宿主执行和挂载变更直接拒绝；新确认也不能启用尚未实现的能力。

批准采用 CAS 幂等激活，同一任务仅一个有效许可。Plan 版本确认与许可激活采用同一事务或可恢复的
联动提交，不能出现“计划批准成功但执行许可未核验”仍启动 Sandbox。旧记录没有许可时不得视为默认允许。
每条命令另有唯一 command ID、精确脚本/argv hash、执行前副本版本及状态 CAS，预留并扣减任务预算；
相同 ID 不重复执行。已知失败后 Agent 可在预算内提出新命令，未知执行结果不自动重试。

共享执行服务同时拦截 `run_bash`、`coding_run`、`coding_validate` 和自动流程触发的固定验证命令，
而不只按工具名在 Web 层放行。项目测试可能执行用户代码，固定检查也受任务许可与资源预算限制。
任务内 Sandbox 编辑工具也核验同一 operation 及许可；不能同时把直接写真实 Workspace 的工具交给 Executor。
探针、固定初始化、暂停、导出和清理属于服务端控制动作，使用独立的最小内部权限，不接收模型提供的命令，
不能成为通用执行豁免入口。通用 AllowAll、MCP/Skill 文本或前端模式值都不能替代许可。

### 3.4 与 Python 和 E2B 的关系

授权 Bash/`coding_run` 后，脚本可以在获准容器内运行其中已有的 Python；这属于隔离任务的能力范围，
不是授予宿主机 `run_python_analysis` 权限。后者仍在独立本地环境中执行并逐次确认，不共享 Docker 副本或许可。

E2B Coding/Plan 同样采用统一任务门禁，但仍使用其原后端配置、固定验证和发布流程。
界面展示真实的远端数据传输和网络策略，不能把 E2B 任务标成“本地无网络”。两种后端的许可互不通用，
缺少对应后端配置不能自动回退；本轮不向 E2B 注册新的 `run_bash`，已有 `coding_run` 纳入统一检查。

## 4. Agent 工具契约

工具名：`run_bash`。建议最小参数如下：

```json
{
  "script": "find upload -maxdepth 1 -type f -print",
  "cwd": ".",
  "timeout_seconds": 60,
  "reason": "查看当前 Workspace 的上传文件清单"
}
```

- `script` 必填，UTF-8 不超过 16 KiB，拒绝 NUL；不得截断后执行。
- `cwd` 默认 `.`，仅接受已有、规范的 Workspace 相对目录；拒绝绝对路径、`..`、反斜杠与链接逃逸。
- `timeout_seconds` 默认 60 秒、首版最大 300 秒；超出上限明确拒绝。
- `reason` 为简短用途说明，只是给用户的解释，不构成权限或安全依据。
- 严格拒绝额外参数，尤其是宿主路径、账号/Session 指定、解释器、镜像、网络、挂载、env、Docker 参数和审批值。
- 独立调用核对脚本级许可；Coding/Plan 调用核对任务级许可后，为该 command 固定精确脚本字节和 hash。
- 服务端将该 command 的脚本物化到容器内固定控制路径，设置为执行用户不可改写，
  然后通过 argv 启动 `/bin/bash --noprofile --norc -e -o pipefail <固定脚本路径>`。
  控制文件不进入 Workspace，不沿用用户的 shell 启动文件，不继承 `BASH_ENV` 等注入项。
- 后端使用固定 Docker 客户端/API 或明确 argv，绝不把脚本拼入宿主机 `shell=True` 命令。
- `-e`/`pipefail` 只是失败处理默认值，不作为安全边界；执行服务不静默修补或重放脚本。
  独立 Bash 修改脚本须重新确认；任务内 Agent 可以在原授权范围和预算内提出新 command 进行修复。
- 模型/API 不接收 `grant_id`、`approved` 或指定 task/operation 的参数；绑定来自不可变请求上下文。

返回结构包含 `run_id`、`execution_task_id`、`command_id`、执行状态、`exit_code`、`termination_reason`、
有限 stdout/stderr、`output_truncated`、`duration_ms`、基线/副本 revision、变更摘要及任务剩余预算。
只有任务完成冻结后才出现 `artifact_id` 和 `publish_required`；单条命令完成不能假称整个任务已结束。
非零退出是正常命令结果，不能包装成“成功”。执行成功也不代表已发布或分析结果正确。

## 5. 隔离环境与资源限制

### 5.1 数据路径

真实 Workspace、会话 SQLite、用户目录、项目根、Docker socket 和宿主密钥均不挂载到任务容器。
服务端通过受限传输接口注入物化快照。容器的可写区域仅为有界的执行副本和临时目录。

Docker 的可写 bind mount 会直接修改宿主机文件，并不是副本；因此首版不使用真实 Workspace
的可写绑定挂载。[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

快照复用现有隐藏文件/敏感路径规则，排除 `.git`、`.env`、系统 SQLite、凭据和内部状态；
无法保证识别正文中人为写入的所有秘密，所以输入文件本身仍需用户信任。
上传原件、转换产物和固定记忆/状态文件即使在副本中被修改，也不允许发布回真实 Workspace。

### 5.2 容器固定策略

- Linux 容器、非 root 用户、只读根文件系统、删除全部 Linux capabilities、禁止提权。
- 保留 seccomp，适用平台保留 AppArmor 等限制；不启用 privileged、host network/PID/IPC、设备映射或端口发布。
- 网络固定 `none`，不注入 Provider/MCP/API 密钥、代理配置、SSH agent 和宿主环境。
- 控制目录由可信初始化流程准备，不可被任务用户改写；辅助程序在只读镜像中。
- 首版执行副本优先采用限额 tmpfs；不直接使用无限额 Docker volume 冒充磁盘配额。
- 所有 daemon 请求由后端从固定配置构造；任务不能访问 Docker 控制接口。

Docker daemon 控制权本身具有很高权限，Web 配置必须严格限制参数和调用者；容器隔离仍依赖
内核、daemon 与镜像，不能保证零逃逸风险。[Docker Engine security](https://docs.docker.com/engine/security/)

### 5.3 首版建议限额

| 项目 | 建议值 |
|---|---|
| CPU / 内存 | 1 CPU / 1 GiB；不额外放开 swap |
| 进程数 | 64，允许 Bash 必需的子进程但防止无限派生 |
| 可写空间 | 任务可写 `/workspace` 256 MiB、`/tmp` 64 MiB；另有控制用户专用 `/run/pi-command` 1 MiB，任务只读；全部计入总内存预算 |
| 输入 / 输出制品 | 输入总量 100 MiB，输出变化总量 50 MiB，单文件 25 MiB，文件数 5,000 |
| 单命令 | 默认 60 秒、最高 300 秒，且不得超过任务剩余预算 |
| 独立 Bash 任务 | 1 次脚本调用，容器生命周期最多 10 分钟 |
| Coding/Plan 任务 | 最多 50 次执行（含项目验证/修复），命令累计执行最多 10 分钟，容器总生命周期最多 30 分钟 |
| 确认等待 / 排队 | 确认最多 10 分钟，批准后的排队最多 60 秒 |
| 并发 | 同 Workspace 1 个；每账号 1 个执行中任务；本机全局最多 2 个 |
| 输出 | 每命令聊天返回合计 16 KiB；私有日志每命令最多 1 MiB、每任务最多 5 MiB，超出持续丢弃并计数 |
| 保留 | 待发布制品默认 24 小时；日志与历史按账号配额/保留策略清理 |

这些是本方案的保守产品默认值，不是 Docker 默认值。CPU、内存等限制必须实际传给运行时并通过
探针确认，不能只检查工具参数。[Docker resource constraints](https://docs.docker.com/engine/containers/run/)

表内资源规格用于本地 Docker；E2B 继续使用其已配置限额，同时取任务许可预算与后端上限的较小值。
预算不会因命令重试、Plan 子任务切换或模型新一轮思考而重置；固定验证要提前预留执行额度。
50 次指有记录的执行入口调用，不声称能精确统计 shell 内所有子命令；其内部行为由总时间、CPU、PID 等限制约束。

tmpfs 内容在容器停止后会消失，且可能受宿主交换空间策略影响。因此不能“先停止容器，再从 tmpfs
取结果”；导出顺序必须按下一节实现并做真实 Docker 验证。[Docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/)

## 6. 生命周期与并发

### 6.1 创建任务和授权

1. 解析模式与角色。Plan 先只读制定计划；Coding 形成当前请求的执行范围；独立 Bash 固定脚本。
2. 校验账号、能力和后端选择，读取不可变输入引用及 Workspace 基线，预分配 task/operation ID；
   此时不创建任务容器、不执行用户代码。
3. 展示对应确认卡并建立许可；Plan 使用“批准计划并授权执行”一个动作，不再追加逐条 Bash 弹窗。
   等待期间只占活跃任务配额，不占计算槽或长时间 Workspace 文件锁。
4. 取得账号/全局执行槽后重验初始基线、Plan 版本/用户请求、配置、选择与许可，再持久标记启动意图。
5. 使用唯一 task/operation ID 创建专属容器并注入快照；在同一任务内将文件副本交给统一工具服务。
   E2B 使用其对应 Backend，不会因为某个工具需要 Bash 而启动第二份 Docker 副本。

### 6.2 任务内命令循环与安全复用

- Coding/Plan 的编辑、命令执行和验证串行作用于同一 operation；`run_bash` 与 `coding_run`
  共享 `/workspace`，不能各自建立副本。下一条命令可以读取上一条命令的文件修改。
- 每个调用都检查活动许可、角色和剩余预算，持久分配 command ID，固定精确 payload 后启动新进程。
  `cwd` 每次显式确定，shell 变量、内存与 `cd` 不跨调用继承；文件变化可以跨调用保留。
- 命令退出后由可信控制流程暂停本地容器，确认该命令所有派生进程已退出，才能重开下一次执行。
  必须核对进程身份而非可伪造的命令名；仅主进程返回、PID 进程组清理或“没有输出”不能证明安全。
  检测到逃离进程组的后台进程，或无法可靠确认清理时，销毁整个 operation，不带残留进程继续复用。
- 间隔期可以暂停容器；下一条命令只在许可仍有效、无残留执行进程且未开始冻结时恢复。
  冻结阶段的暂停是单向屏障，不能与命令间的可恢复暂停混用。
- 已明确得到的非零退出保留真实结果，Coding/Plan 可在预算内编辑并发起新的修复/测试命令，
  不因一次测试失败立即销毁副本，也不谎报失败命令成功。独立 Bash 失败后结束，不复用旧批准。
- 独立 Bash 完成一次脚本调用即进入终结阶段；Coding/Plan 则在请求/计划执行完成后统一终结。
  任务边界不是一条命令或一个 Plan 子步骤，也不是无限延长的整段聊天会话。
- 取消、超时、OOM、隔离/输出安全违规、预算耗尽或无法确定执行结果为任务级失败：撤销许可并销毁，
  不继续执行或发布不确定结果。需要扩大范围、补充输入或用户协调而阻塞时也结束当前执行许可。

可信派生进程清理、暂停/恢复和 tmpfs 复用是阶段 1 的强制验证项。若目标平台只能安全支持一次性调用，
可以报告独立 Bash 可用、任务复用不可用，但不能宣称 Coding/Plan 已支持，也不能回退宿主执行。

### 6.3 任务结束、冻结和并发

1. Coding/Plan 完成任务后，在许可预算内运行服务端固定完整验证；成功的最终验证才允许冻结。
   原有代码修改/无变更判定继续由 Coding 编排负责，不强加给独立 Bash。
2. 进入 closing 状态，拒绝新的 Agent 执行和编辑；完成派生进程核验并暂停全部任务进程。
3. 确认只剩固定、不同 UID 的 inert PID 1 后，仅允许镜像内固定读取辅助程序临时运行并流式导出 tmpfs，
   随即恢复暂停；冻结标记始终禁止用户执行/上传。由宿主可信收集器验证完整差异与内容 SHA；
   Coding 的实际验证证据必须与导出的最终树一致，不能用 Bash 的输出完整性证据代替。
4. 冻结、持久化签名制品后销毁容器、释放槽位，关闭执行许可；随后单独等待发布确认。
5. 独立 Bash 成功且无变更时正常结束，没有发布按钮；失败、取消或收集不可靠则无可发布制品。

实施时核对到 Docker 官方明确列出 `docker cp` 对 tmpfs 的限制，因此不依赖“暂停后直接 cp”。
改用“全用户进程已退出 → 仅运行固定读取器 → 再次暂停”的传输方式，仍不允许冻结后恢复用户代码执行。
读取器使用镜像只读源码及 `python3 -I -S -B`，不能接收用户 argv 或 import Workspace 文件；
inert PID 1 与用户代码采用不同非 root UID，避免把可由任务进程接管的 supervisor 当成可信存活进程。
该实现必须经过真实 Docker 验证；不支持时停止启用，不能提前停止丢失 tmpfs 或回退可写宿主挂载。
依据：[Docker cp corner cases](https://docs.docker.com/reference/cli/docker/container/cp/)。

同一 Workspace 只有一个活动/待发布 operation：其所属任务内的多次工具调用复用它，不构成冲突；
其他 Coding、Plan 或独立 Bash 请求则返回 `workspace_busy`。不同 Workspace 在账号/全局限额内并行。
普通文件编辑不锁到任务结束，可以继续进行，但会使旧基线的发布冲突；不自动把修改同步到活动副本。

### 6.4 状态与恢复

新增 `ExecutionGrant` 持久记录授权类型、绑定、状态和预算；命令记录以 task/operation + command ID
区分 preparing/running/succeeded/failed/cancelled/interrupted，精确记录每次执行。
任务与冻结/发布状态仍由同一个 Sandbox operation 生命周期扩展管理，独立 Bash 也包装为一个任务。
Plan 子任务、工具运行卡仅持有引用，不另复制一套容器或发布事实源。

- 账号 + Session + request + task + operation 全链路绑定，不能依赖可变的“当前 Workspace”。
- 刷新只恢复显示与仍活跃的任务，不重放工具或重复激活许可；一次确认不延长许可有效期。
- Stop、注销/权限撤销、关闭能力、工具选择撤销或计划范围改版会取消确认/排队或撤销活动许可并终止任务。
- 后端重启后未完成执行标记 interrupted，活动许可作废；对账清理容器，不恢复原任务命令执行。
  已冻结的制品可继续按原发布规则审阅，但不因此恢复执行许可。
- 创建/启动响应不确定时，通过同一 ID 对账，不创建第二个容器或重新执行同一命令。
- 独立于聊天协程的清理器处理任务到期、空闲异常和孤儿容器；启动时也做一次对账。
- daemon 不可达时标记 `cleanup_pending` 并在管理页告警，恢复后仅清理，不宣称任务已被终止。
  若无法确认资源已释放，本地执行能力暂停接收新任务，避免累积失控任务。
- Bash、Coding、Plan 与 E2B 使用同一 Workspace operation 租约。分析工具使用自身临时目录与审批，
  不共享该容器，保存冲突仍由 Workspace revision 处理。

## 7. 冻结、验证与发布

### 7.1 不信任容器输出

归档通过服务端固定路径和限额下载；逐成员验证后写入任务专属 staging，不对原始归档直接 `extractall`。
拒绝绝对路径、路径穿越、反斜杠、Windows 盘符/保留名/ADS、大小写冲突、重复条目、symlink、
hardlink、设备、FIFO、稀疏文件及超限展开。校验文件数、大小、哈希与最终成员集合，服务端重新计算 diff。
不得相信脚本输出的 manifest、测试结论或“可发布”声明，也不得在宿主机执行生成代码完成验证。

日志及产物内容都是不可信数据。前端不执行终端控制序列/raw HTML；危险内容仅以文本显示或作为下载附件。
输出可能经聊天进入所选模型：本地执行不等于数据绝不离开本机。Telemetry 不保存命令或数据正文。

### 7.2 复用与必须修改的契约

保留 Coding/Plan 现有固定检查、Validation→Freeze 屏障与 v1 制品语义，不论中间是否使用了 `run_bash`。
只有独立 Bash 任务引入 `bash-output-integrity/v1` 证据类型，绑定任务/命令许可、执行结果、冻结树及服务端发布策略版本，
表示“命令成功退出且输出可安全导入”，不表示功能测试通过或内容可信。

证据类型由已授权的任务种类决定，不能由最后调用的工具名决定。Coding/Plan 即使最后执行 Bash，
也不能降级为独立脚本的输出检查；任务中的失败测试和后续修复保留记录，最终固定验证必须真实通过。

需要对制品验证入口、发布生命周期和前端响应做显式版本/类型扩展；旧 Coding 制品继续走原验证器。
如果复用当前 `SandboxOutputArtifact` 需要新增版本，就新增受判别的 schema，不把缺失 validation
默认为成功。将经过类型验证的冻结结果接入现有 `LocalTransactionalPublisher` 和
`WorkspaceSandboxArtifactPublisher` 的共同事务提交部分，不另写文件覆盖逻辑。

本轮源码的统一 `workspace_path_policy` 已支持 `scripts/**`、`artifacts/**` 和符合规则的普通 Markdown。
首版沿用并在目标文件 purpose 层复验；固定保护 `AGENT.md`、`Memory.md`、`HANDOFF.md`、tasks、
upload/inputs、documents 转换结果、固定状态文档与隐藏元数据。路径合法不等于 purpose 可覆盖。
禁止写出新权限命名空间；改动受保护路径使本次输出不可发布，不静默忽略后宣称成功。

### 7.3 发布授权

- 只有当前账号/Session 的用户能批准自己的精确 artifact ID/hash、完整变更集合和 baseline revision。
- 执行许可不能代替发布许可；Agent 没有自动发布权限。
- 进入待发布阶段后执行许可已关闭；批准发布不再运行命令。冲突后的重新执行或新计划需新的任务授权。
- Publisher 在目标 Workspace 锁内重验 revision、tree SHA、逐文件 before SHA、签名、路径/purpose 与当前策略。
- 使用已有 journal、不可变 generation、原子 metadata 更新与恢复机制；成功只提升一次 revision 并发送 `workspace_changed`。
- 任一文件冲突整批零写入。首版不提供强制覆盖、自动 rebase 或带旧批准的新制品发布。
- 发布按钮重复点击应幂等；页面显示已提交的 Workspace revision，不能重复创建文件。
- 单独发布前不必保持容器运行；待审阅的是本机内容寻址的冻结制品。

## 8. 模块和 Web 接口规划

以下是拟新增/修改的位置，不表示这些文件或接口已经实现。

| 模块 | 计划 |
|---|---|
| `src/coding_sandbox/local_docker.py` | Backend 实现：固定容器规格、共享任务文件树、逐命令进程清理/暂停恢复、有界传输、最终冻结、销毁与对账 |
| `src/coding_sandbox/admin/` | 独立本地配置/能力探针；保留 E2B 凭据与原配置读取行为 |
| `src/coding_agent_app/execution/` | `models.py`、`service.py`、`store.py`；共用 ExecutionGrant、任务确认、一次性脚本确认、租约、预算、command CAS 与撤销 |
| `src/coding_agent_app/bash/` | 独立 Bash 的薄编排适配；复用统一任务/许可，不再拥有独立授权或容器事实源 |
| `src/pi_agent_core_py/tools/bash.py` | 薄工具适配层；不直接访问 Docker，不自行解析用户身份 |
| `src/coding_sandbox/operation.py`、`src/pi_agent_core_py/tools/coding_sandbox.py` | 在公共执行边界接入受信许可检查，覆盖 argv/Bash/固定验证；Backend 原始入口不向 Agent/Web 暴露 |
| `src/coding_sandbox/artifact.py`、`lifecycle.py`、`publisher.py` | 明确支持 Bash 证据类型；复用签名、CAS、冻结审阅和事务发布 |
| `src/coding_agent_app/planning/`、`sandbox/automation.py` | Plan 批准与许可原子关联，Coding 执行前确认，任务内复用；Plan 改版/结束/阻塞撤销许可 |
| `src/coding_agent_app/sandbox/workspace.py` | 复用快照/Publisher 适配；校验真实 Workspace 的当前路径和 purpose |
| `src/coding_agent_app/core/resources.py`、`toolsets.py` 与 `web/app.py` | 按任务后端与角色装配：Coding/Plan Executor 可含 Bash；Planner/Verifier/read-only/checkpointer 无通用执行工具；任务外独立调用逐次确认 |
| `src/pi_agent_core_py/web/bash.py` | 账号范围内的历史、日志、取消及发布引用；无直接任意代码执行 REST 入口 |
| 前端 Workspace / Chat / Plan / Sandbox 组件与 stores | 区分任务范围和精确脚本确认卡，合并 Plan 批准，展示预算/撤销/命令历史；输出和 Changes 共用 operation ID |
| `docker/bash-runtime/Dockerfile`、环境检查脚本 | 固定镜像和管理员手动构建/探针入口；不在聊天中拉取或构建 |
| tests / evals | Fake 后端确定性测试、真实 Docker 安全契约测试、前端与 E2E |

建议路由：管理员配置/探针为 `/api/admin/local-execution/...`；任务/命令历史及撤销使用
`/api/sessions/{sid}/execution-tasks/...`，Bash 查询可为该任务的过滤视图而非第二份状态。
Coding 任务和独立 Bash 的确认复用 `/api/requests/{request_id}/approvals/{approval_id}`，增加受判别的确认类型；
Plan 复用 `/api/plan-runs/{run_id}/approve`，同时提交当前计划版本和服务端待确认范围 hash，原子激活许可。
浏览器的版本/hash 仅用于匹配服务端已保存记录，不允许客户端定义范围。发布仍复用 Sandbox operation API。

所有入口（含 SSE、下载、审批和发布）都必须校验账号与 Session 所有权，并复用 TrustedHost、Origin、
UI header 等本机 Web 防护；localhost、不可猜 ID 或前端隐藏按钮不能代替权限检查。
现有路由是否由统一账号隔离层覆盖，实施时逐条验证，不仅复用 localhost 路由装饰器。

本地功能启用只要求本机安全配置、持久存储和已验证运行时，不因没有 E2B Credential runtime 而不可用。
MCP/Skill 配置不变，也不向容器注入这些服务的认证环境。

## 9. Telemetry 与日志

复用当前 Admin Telemetry 入口，增加统一 `execution` 分类，区分 task/command 和 bash/coding/plan，
不创建另一套监控产品。记录任务授权/拒绝/过期/撤销、预算用量、命令耗时、退出码/稳定错误码、
取消/超时/OOM、字节计数、文件变更数、发布/冲突、镜像/策略版本和清理结果。
跨模块关联 task/operation/command；不能把一个任务许可下的多条命令统计为多次人工批准。

完整脚本、输入文件名、stdout/stderr、diff 和正文仅在账号隔离的私有运行历史中保留，
不进入通用 Telemetry、错误堆栈、控制台日志或指标标签。日志不写进可被任务篡改的输入副本。
禁用 Docker 无限增长的容器日志持久化，应用输出限额同时覆盖实时传输、事件持久化和最终结果。

建议固定错误码覆盖：`bash_disabled`、`docker_unavailable`、`image_unavailable`、
`isolation_unavailable`、`approval_denied`、`approval_expired`、`approval_stale`、
`grant_required`、`grant_revoked`、`grant_scope_mismatch`、`task_budget_exhausted`、`plan_version_stale`、
`workspace_busy`、`resource_limit`、`command_timeout`、`execution_interrupted`、
`unsafe_output`、`publish_conflict` 和 `cleanup_pending`。

## 10. 按顺序实施与阶段出口

### 阶段 1：本地 Backend 与安全可行性

实现固定 Docker 后端、镜像、管理配置/探针和 Fake 契约测试，暂不向 Agent 注册工具。
真实验证非 root、无网络、无宿主挂载、限制生效、逐命令派生进程清理、暂停/恢复共享副本、
冻结后导出 tmpfs、全容器终止和有界日志；分别报告独立执行与任务复用能力。
Docker 未安装/未授权时保留不可用状态，不安装、不回退；真实运行证据缺失时本阶段不得标记完全验收。

2026-09-06：已实现 Backend/CLI transport/镜像 worker、独立 SQLite 配置及检查脚本；
初版 36 个离线用例及 105 个相关回归通过后，Docker 已恢复并构建固定运行镜像。
后续修复超时分类并增加 cgroup OOM 检查、private cgroup/runc/IPC 限制；当前 Backend 44 个离线用例、
14 项本机真实安全验证通过。本阶段后端可行性验收完成，未注册或启用 Agent 工具。

### 阶段 2：统一任务授权与执行闭环

先实现共用 ExecutionGrant、执行入口门禁、command CAS、身份/角色绑定、预算和撤销，再注册 `run_bash`。
集成 Coding 一次任务确认、Plan 批准/授权合并、独立 Bash 精确确认；不能只给新工具加门禁。
验收同一任务“编辑 → 失败测试 → 修复 → 再测试”不重复弹窗且共用文件树，以及独立调用逐次确认。
该阶段先不开放新增 Docker 发布；原 E2B 发布链保持，但新发起的任务必须取得对应许可。
无变更的独立检查可以正常完成，不能因此把通用 Bash 标为 read-only 工具。

2026-09-06 阶段 2A：已新增 `coding_agent_app/execution/{models,store,service}.py`，
持久绑定、命令 CAS、预算与撤销/清理债务、重启失效基础通过 41 个离线测试。
当前预算保守地将每个受保护操作（含编辑）均计入命令/超时额度，不存在免计费的通用执行旁路；
接入固定验证前需保证验证预留额度。Plan 同事务审批端口缺失时拒绝批准，不能单独激活 Plan。
尚未接入实际 PlanStore/Web、SandboxOperation/E2B、可信脚本路径或注册工具，阶段 2 整体未完成。

2026-09-06 阶段 2B：已完成严格 BashRequest、固定控制脚本投递与薄 RunBashTool（未注册）；
在公共 SandboxOperation 增加可装配执行门禁，本地 Operation 无适配器默认拒绝。
argv/Bash/编辑/固定验证共享许可与预算，角色和任务上下文只从服务端 ContextVar 取得；
固定检查/导出 Python 使用 `-I -S -B`，不加载 Workspace 同名模块。
真实 PlanStore 通过同一个 SQLite 事务绑定计划、CAS 批准精确版本、追加事件并批准许可；
已绑定计划不能走旧批准接口或切换 operation。20 项真实 Docker 检查通过，见阶段 2B 记录。
下一步是 Web/Coding/Plan 的“准备快照 → 明确批准 → 激活并启动”生命周期和三类审批 UI/请求上下文；
现有 Web E2B 仍是旧装配，不能宣称已受新许可保护。尚未开放 Docker 发布或工具注册，阶段 2 仍未完整交付。

2026-09-06 阶段 2C：新增内部 `ExecutionTaskRuntime`，复用现有 Workspace
BaselineProvider、Snapshot、Operation、ExecutionStore 与 PlanStore；不另建 Workspace 事实源。
准备和批准均不启动容器，启动前复核请求/配置/选择/计划与 Workspace revision/SHA，固定快照播种后开放工具。
提供任务与角色绑定、关闭/取消、启动超时、并发一次启动和不重放的保守恢复；未确认运行资源消失时保留清理债务。
新增 26 项专项、398 项相关回归及 25 项真实 Docker 检查通过。下一步 2D 是将这些端口连接到现有 Web
审批和 Coding/Plan 调度，处理冻结制品向旧 Managed 生命周期的交接；本轮未启用工具，也不新增发布旁路。

2026-09-06 阶段 2D：Coding 每请求执行范围确认与 Plan 原子合并审批已接入 Web；可信账户/请求、
后端/Workspace 选择、角色上下文与固定快照共同约束调度。旧直接启动、独立计划批准与手动执行验证入口
不再构成旁路。固定验证/冻结完成后销毁运行环境，保留制品供原有审阅/独立发布使用。
Workspace 提供后端选择，部署者可显式配置本地 Docker，不静默迁移 E2B。独立 Bash 确认/注册尚未实现；
Docker 发布仍关闭。完整交付边界和验证见阶段 2D 记录。

2026-09-06 阶段 2E：`web/bash.py` 以可信异步请求身份连接共用 ExecutionTaskRuntime，
注册 Workspace 可选工具，并增加独立 Bash 意图路由。默认策略不重复通用确认，显式拒绝仍优先；
精确脚本/cwd/SHA/输入范围逐次审批，原范围再次校验后才启动。每次一份副本，结束关闭许可并销毁。
只保留有界 stdout/stderr、退出/中断结果、任务/命令/快照身份、预算与变更摘要，**副本文件丢弃**。
私有 SQLite 与 Session 绑定，上限每账号 200 条、终态 30 天（新运行触发裁剪）；
`GET /api/workspaces/{sid}/bash-runs[/{run_id}]` 和前端只读，刷新/重启不重放。
重启标记旧未终结历史 interrupted，但不将其当作资源已清理；清理债务仍由执行服务管理。
仍未在 Coding/Plan 内注册 `run_bash`，也未创建独立 Bash 文件证据/冻结/发布功能。
详见 [阶段 2E 验证](../validation/workspace-bash-stage2e-2026-09-06.md)。

2026-09-06 阶段 2F：复用现有 `tools/bash.py` 薄适配，通过 Web 服务端请求 ID、任务身份、
当前 Executor ContextVar 与 Workspace 选择装配；预览使用相同工具定义但不能执行。
`run_bash`、argv、编辑和固定验证进入同一 Operation/Grant；失败修复、Plan 连续任务不换副本，
关闭请求后旧适配器不能借用新请求。没有独立脚本审批/第二个容器/独立 Bash 历史条目。
审批卡明确任务内 Bash 复用，Planner/Verifier 不获得 Bash，未选择或 E2B/disabled 不提供此工具。
固定验证、冻结证据与发布门禁不变；详见 [阶段 2F 验证](../validation/workspace-bash-stage2f-2026-09-06.md)。

### 阶段 3：安全制品与确认发布

**已实现**。独立 Bash 使用独立 schema `pi-agent-bash-artifact/v1` 和 `bash-output-integrity/v1`；
Coding/Plan 保留 `pi-agent-coding-artifact/v1`。两类共享 metadata 成员布局与签名/导出/事务实现，
按 manifest schema 严格选择证据类型，拒绝混用。内部胶囊签名绑定 Session、用途、执行范围、发布策略、
原始 baseline 和 artifact；Web 发布提交 artifact ID / archive SHA / review SHA，拒绝空或过期确认。
签名制品已生成但计算资源未确认回收时，发布仍拒绝；重启时只恢复回收已确认的审阅制品，不恢复执行。
Workspace 持久提交回执覆盖“提交成功但生命周期状态未落盘”窗口，重试不重放命令、也不再次提升 revision。
保护路径/purpose、严格完整基线冲突、链接/特殊文件/稀疏 tar/跨平台危险路径都 fail closed。
实现与测试见 [阶段 3 验证](../validation/workspace-bash-stage3-2026-09-06.md)。

实现独立 Bash 的专用证据类型，以及 Docker Coding/Plan 的固定验证和原有证据链；
复用受限归档导入、冻结签名、完整 diff、发布确认和原子提交。
验证任务终结使执行许可失效，保护路径/purpose、并发冲突、重复发布及中断恢复；不得降低 Coding 验证要求。

### 阶段 4：Web 完整体验、恢复与观测

补齐管理员环境页、Workspace 能力状态、任务范围/脚本双类型审批、计划版本冲突、预算/撤销、
运行恢复、Changes 刷新、跨 Workspace 并发、孤儿清理、TTL/配额和 Admin Telemetry。
容器在任务终结后回收，而非每条命令后回收；等待制品审阅不占计算资源。

2026-09-06 实现：网关共享 `execution-control.sqlite`，创建前原子申请槽位；默认账号 2 / 全局 4，
CPU 8、内存 8192 MiB。每账号准备/待批任务也有上限；不同 Workspace 可并行启动，同 Workspace 仍互斥。
后台每 5 秒处理许可过期、60 秒失联心跳、清理债务和有 namespace/operation/image 三重证据的孤儿容器。
实际 namespace 增加控制账本路径的安装级摘要；不同项目/测试实例不会共用默认清理范围，
同一网关的账号则共享该范围与原子配额。旧任务仍使用落盘 namespace 恢复，不猜测新路径下的缺席等于旧资源消失。
探针仅检查 daemon/image，不自动启用部署、不拉镜像，不将其描述为隔离认证。
每分钟回收 30 天以上且不被活跃任务/签名待审制品引用的内部缓存；每账号 2 GiB，准备前预留
两份输入与两份输出的最大归档空间。超 50,000 个缓存文件或非法路径时 fail closed。
清理运行于 Web 进程生命周期内；退出期间依赖容器既有 lifetime 防线，下一次启动继续对账，不安装系统服务。
管理员在 Telemetry 页面管理；完整执行内容只在所属账号的 Workspace 私有历史中读取。
未知 E2B 资源仍保留清理债务，不宣称本轮已实现远端失联自动回收；旧 Docker profile 变化且没有
namespace 回执时也不猜测已清理。验证见 [阶段 4](../validation/workspace-bash-stage4-2026-09-06.md)。

### 阶段 5：完整门禁和交付

跑 Backend 全量测试、Ruff、strict Mypy、eval gate、Frontend lint/typecheck/Vitest/build、
完整 Chromium E2E，并补充本机真实 Docker smoke 与安全负例。依赖发生变化时检查 lock 一致性。
更新 API、STATUS、TODO、CHANGELOG 和独立验证记录，清楚区分 Fake 与真实 Docker 证据。
提交整理需遵循当时的用户授权，不把本轮方案批准等同于允许覆盖其他在途变更。

## 11. 必须通过的验收场景

1. 本地能力与 Bash 默认关闭；只选择 Python、切换 Coding/Plan 或打开页面都不启动 Docker。
2. Plan 规划/Verifier 只有只读工具；批准明确版本的计划并授权执行后，才为 Executor 创建任务副本。
3. Coding 一次授权后可多次编辑、执行、失败修复和固定验证；`run_bash` 与 `coding_run` 读到同一副本，
   任务内不重复弹窗，Plan 子步骤/模型轮次不重置预算。
4. 独立 Bash 每次调用单独确认；脚本或 cwd 改变必须重新批准，不能借用已完成 Coding/Plan 的许可。
5. 未批准、过期、权限不匹配和批准重放均不创建任务容器；AllowAll、直接 `coding_run`、验证入口或伪造 ID 不能绕过。
6. 初始快照、Plan 版本、镜像/后端/策略在激活前变化时零执行；激活后副本内合法编辑不使许可失效。
7. 活动任务关闭能力/撤销选择/停止/过期/超额/计划范围改变时立即失效并清理；被冻结许可不能恢复执行。
8. 两个账号不能互读任务/命令/日志/制品或互相审批；许可不能跨 Session、request、operation 或后端使用。
9. 独立 Bash 列出 `upload` 文件可成功且无发布；副本中新建 `scripts` 或 `artifacts` 文件在发布确认前不改变原 Workspace。
10. 管道、引号、换行与 Unicode 按命令记录的精确字节执行；宿主机不解释 `$()`、反引号或分号。
11. 本地容器不能访问外网、宿主服务、Docker socket、宿主密钥或其他 Workspace；E2B 不被误标为本地执行。
12. fork/脱离进程组的后台子进程、CPU/内存/磁盘/日志耗尽受限；不在有残留用户进程时复用容器；
    无法证实安全清理时整个任务失败，不继续后续命令。
13. 恶意归档、路径/链接逃逸、大小写冲突、超限输出及受保护文件变更不能进入发布；Coding 不能借 Bash 降级验证。
14. 原 Workspace 在运行或审阅期间被编辑后不自动同步，发布冲突整批零写入；扩大输入或换快照必须新任务确认。
15. 已知失败可以在任务预算内用新 command 修复，未知执行结果不重试；刷新/重启/重复批准/发布不重复执行或提交。
16. 发布仍单独确认；任务结束后容器销毁、许可关闭、制品可继续审阅；daemon 失联不虚报清理完成。
17. E2B 后端及其验证/发布回归通过并受统一任务门禁；本地 Python/固定分析、HTTP MCP、Skills、上传、压缩行为不变。

## 12. 暂不纳入本轮实现

本地 Docker 联网白名单/代理、动态 pip/npm/apt 安装、交互式终端、长期开发服务器、跨任务/跨请求复用容器、
Windows 容器、GPU、将现有 Python 分析迁移到 Docker、部分发布与自动冲突合并，均单独规划。

同一 Coding/Plan 任务内跨工具调用、跨模型轮次、跨 Plan 子步骤的副本复用属于本轮范围；
交互 shell 状态、后台进程和新一轮用户请求不继承该任务权限。

后续若接入 Python 的 Docker 执行模式，可以复用本地 Backend，但其固定数据入口、分析结果格式和
执行确认仍独立；不能通过“启用 Bash”静默改变当前本地 Python 产品行为。

用户已确认按计划实施；当前进度与环境阻塞见文首和阶段 1 记录。
实施授权不表示任何配置已开启，也不构成 Docker 安装、联网或代码提交授权。

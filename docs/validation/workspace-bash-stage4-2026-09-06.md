# Workspace Bash 阶段 4 验证

日期：2026-09-06。先提交阶段 3：`5ec6622`。以下阶段 4 改动随本次提交归档，未推送。

## 实现范围

- 共享控制面：Auth gateway 向全部账号应用注入同一 SQLite 控制账本；独立测试/单账号组合使用私有账本。
  `BEGIN IMMEDIATE` 原子控制每账号 2 / 全局 4 个资源槽位、CPU 8、内存 8192 MiB；同账号同 Workspace 唯一租约。
  账号内准备/待批任务也有限额。申请发生在创建容器之前，释放只能来自可信回收结果，不能由浏览器声称成功。
- 恢复与清理：每 5 秒后台检查、60 秒心跳失联、任务 lifetime；用户/管理员撤销只置不可逆停止标志。
  失联或未知创建结果只核验并销毁原 operation，绝不创建替代容器或重放命令。
  本机容器必须同时匹配 namespace、operation、image、确定性名称；只检查该部署标签范围。
  Web namespace 额外绑定共享控制账本路径摘要，避免并行项目/测试实例清理彼此的默认 namespace。
  重启使用持久 namespace；daemon 不可达/无法确认时保留债务并阻止新执行。
- 缓存：每分钟独立扫描，避免慢 IO 阻塞许可心跳。只清理 staging 下预定 snapshots/files/operation-files 目录，
  30 天 TTL；活动快照和待审签名制品受引用保护。拒绝 link/reparse、越界、非普通文件；不触及真实 Workspace、
  上传、签名密钥或 Publisher 事务回执。账号缓存预算 2 GiB，准备前额外预留四份最大归档空间；最多扫描 50,000 文件。
- 管理员：Telemetry 页显示执行状态、预算、历史资源记录和清理债务；支持只读 daemon/image 探针、任务撤销、
  异步清理重试。没有命令执行接口、部署自动启用、镜像安装或任意路径删除接口。
- 私有历史：Coding/Plan/Bash 共用账号 Session SQLite；完整 Bash 输入、截断且标注的输出、命令状态/预算。
  单条 stdout/stderr 分别最多保留 32 KiB，其他输入有界；无结果明确为未知，不重放。
  终态全文最多 200 个任务/30 天；轻量授权元数据保留。原独立 Bash 历史也增加后台 TTL 清理。
- Telemetry：`execution.task` / `execution.command` / `execution.cleanup` / `execution.publication`。
  只传明确列举的结构化字段，不传脚本、cwd、文件名、argv、输出或 diff；任务阶段由私有 SQLite outbox 投影。
  Telemetry 是被动、可丢失/重投的观测，不替代授权账本或精确一次执行依据。
- 安全：所有新 API 强制 UI header 与精确 Origin；管理 API 另要求 admin；普通用户只能查看/撤销自己 Session 的任务。
  私有日志持久化失败不能阻止撤销/回收。未降低 Coding 固定验证或阶段 3 精确发布审批。

## 验证记录

- 初轮邻接：`c4a` 146 passed / 11 skipped，1 个旧默认并发断言失败；底层保留 1/2 默认，Web 显式装配 2/4，随后通过。
- `c4d`：11 个后端文件，**275 passed / 11 skipped**，162.75s。包含真实组合的鉴权、额度、恢复、Bash、发布、Telemetry 邻接。
- `c4e`：10 个文件，**225 passed / 12 skipped**，275.76s；覆盖收尾恢复/缓存/日志修改前后的邻接批次，不与其他批次累加。
- `c4f`：维护与 Bash 授权，**70 passed / 1 skipped**，22.27s；补测 namespace 变更、日志失败仍清理、同账号两个 Workspace 同时启动。
- Frontend：**232 passed / 34 files**；lint、typecheck、生产 build 通过。
- Chromium：第一次 24 passed / 1 flaky / 1 failed；清理按钮 403 定位到测试网关与账号应用 Origin 配置不一致，
  补充账号应用精确端口 Origin；未修改安全依赖。第二次端口 8098 **26 passed，无重试**，2.5m；自动恢复生产构建。
- 真实 Docker `c4r`：**4 passed / 53 deselected**，263.80s；真实保留槽位/孤儿清理，Coding/Plan 副本复用、
  非零后续执行、固定验证成功/失败与独立发布。固定镜像沿用阶段 3；没有构建/拉取镜像。
- Ruff 全 `src/tests/evals` 与开发入口通过；strict Mypy **290 source files** 通过。
- Evals：**6 suites / 10 pairs，Candidate gate PASS**；`.eval/workspace-bash-stage4-2026-09-06`。
- `c4g`：维护与实际 Web 组合 **61 passed / 12 skipped**，197.35s；缓存扫描与心跳分离后的回归。
- 真实 Docker `c4r2`：**2 passed / 45 deselected**，59.97s；最终安装级 namespace 隔离后的后台清理，
  以及独立 Bash 读取上传、冻结、精确确认后发布。没有恢复旧执行许可。
- `c4h`：安装级 namespace 隔离后的 Web 回归 **62 passed / 12 skipped**，215.05s。
- `c4i`：最终维护、授权与 Bash 回归 **114 passed / 1 skipped**，28.40s；私有结果写入只允许首次落盘，
  拒绝重放的调用不能覆盖前次日志，日志故障仍进入撤销/清理。
- 最终只读 Docker 列表仅有 `pi-wiki-parser-worker-1`（Up 8 hours，healthy）；临时测试容器均已回收。
  `git diff --check` 通过；最终 Ruff 与 strict Mypy 290 文件通过。阶段 4 随本次提交归档。

## 范围与限制

本轮不是阶段 5 全量后端覆盖率验收。没有部署开关变更、业务 Workspace 发布、业务容器重启或外部远端调用。
后台任务跟随本机 Web 进程，不是新系统服务；待审制品保留期间可能占满缓存预算，此时拒绝准备新任务，不偷偷删除待审内容。
未知 E2B 资源仍 fail closed；无 namespace 回执且旧 Docker profile 已改变时不猜测恢复。
quota 当前由可信服务装配决定，管理页只读；单账号缓存配额不是全机器文件系统或其他 Docker 产品的磁盘限额。

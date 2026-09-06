# Workspace Bash 阶段 2E：独立脚本 Web 最小闭环

日期：2026-09-06。对应用户请求“提交，然后进行下一步”。
首先将前置阶段 2C/2D 提交为 `297a81a`：
`feat(execution): wire Web approvals and request-scoped Coding Plan runtimes`。
随后完成本页所述阶段 2E；新改动尚未提交，没有推送。

## 本轮交付与边界

- Workspace Extensions 增加默认未选中的 `run_bash`，只支持显式配置并选中的 Local Docker。
  普通聊天的明确 Bash 请求走第四类 `bash` 意图路由，不将执行工具加入 read-only。
- 服务端从活跃异步请求获取账号/Session/Workspace 身份，不接受模型提供的身份、approved、
  role、宿主路径、环境变量、可执行文件或网络设置；同步/失效请求不能启动执行。
- 每条脚本显示完整 UTF-8 文本、cwd、SHA、输入快照、固定镜像、离线范围与预算。
  默认权限策略只交给一次精确执行审批，不再先弹通用未知工具卡；显式 deny 仍优先，
  自定义策略不被改写，AllowAll 也不能跳过服务端审批。批准不意味着永久允许。
- 复用 ExecutionTaskRuntime 的准备→批准→复核→启动，Bash cwd 必须能由获准快照物化。
  每次调用一份新副本，最多一次脚本调用；同一请求的第二次调用也必须重新确认和重新创建。
- 返回退出码、终止原因、有界 stdout/stderr、任务/命令/快照身份、已用预算与变更摘要。
  变更路径最多 50 项、JSON 字节预算 8 KiB，有截断标志；输出沿用公共 Bash 16 KiB 捕获上限。
  **所有副本文件丢弃，不冻结独立 Bash 制品，不自动写回，也没有发布按钮。**
- Stop、拒绝、到期、Workspace/后端/资源选择变更均终止或拒绝执行；异常不重试未知结果。
  正常非零退出记录 failed，超时/中断不能当成功；未知资源仍保留 cleanup_pending。
- 新增每账号 Session SQLite 私有历史：完整脚本/输入、输出和运行摘要，不送入 Telemetry。
  每账号最多 200 条记录，终态保留 30 天，新运行时裁剪；不淘汰进行中的记录，容量满则拒绝新任务。
  Session 外键删除级联，启动将旧 pending/approved/running 标为 interrupted，**不恢复执行**。
  历史状态不是清理成功证明，容器清理债务仍由 ExecutionStore 管理。
- 只读列表/详情 API：`GET /api/workspaces/{sid}/bash-runs[/{run_id}]`；
  列表展示当前 Session 最近 100 条，不含脚本正文，详情按所属 Session 查询。
  前端新增运行历史面板；刷新/重新打开只读取，不重放脚本，旧 Workspace 的迟到响应不会覆盖当前面板。

Coding/Plan 继续使用 `coding_run` 及已实现的任务许可；本轮不在其 Executor 内装配 `run_bash`。
Planner/Verifier/Knowledge/checkpointer 不获得 Bash。Python Analysis 保留独立本机 Python 环境及原有
逐次审批，不因启用 Bash 而变化；HTTP MCP、Skills 和 E2B 的产品边界不扩大。

## 本机使用

部署者仍需显式设置 [阶段 2D](workspace-bash-stage2d-2026-09-06.md) 中的
`PI_LOCAL_DOCKER_ENABLED`、固定 `PI_LOCAL_DOCKER_IMAGE_ID` 和绝对 `PI_LOCAL_DOCKER_EXE`。
本轮没有设置真实部署变量或重启产品服务，也没有拉取/重建镜像。

1. 在 Workspace → extensions → Execution 选择 Local Docker。
2. 在同页 Tools 勾选 Bash 并保存选择。配置可用不等于 daemon 已健康探测。
3. 关闭 Code/Plan，在普通聊天明确请求，例如“用 run_bash 执行 `ls upload`”。
4. 审阅完整脚本和输入范围后批准一次；结束后在 Bash runs 手动刷新结果。

只设置配置、选择工具、打开页面或调用 Python 分析均不创建容器。
没有 Docker 时拒绝，不回退到 PowerShell/cmd/WSL/宿主 Python 或 E2B。
当前只适合一次性检查和 stdout 型轻量分析；需要保存生成文件时必须等待文件制品/发布闭环。

## 验证结果

| 检查 | 结果与范围 |
|---|---|
| 相关后端回归 | **179 passed / 3 skipped**，173.86 秒；Bash Web/History、Web Execution、Bash/ExecutionRuntime/Grant 和 Wiki API |
| 默认策略收尾专项 | **73 passed / 1 skipped**，96.01 秒；Web Bash、BashHistory、ExecutionGrant，包含默认策略单卡与显式拒绝 |
| 最终真实 Docker Web 链路 | **1 passed / 26 deselected**，30.89 秒；使用默认权限策略，仅一次精确审批 |
| 前端全量 | **224 passed / 33 files**，12.01 秒；新增完整脚本安全展示及历史只读/跨 Workspace 迟到响应测试 |
| Chromium 全套 | **26 项，最终状态 passed，failedTests=[]**；新增 2 项 UI fixture 用例，不宣称零重试 |
| 静态检查与构建 | Ruff 通过，strict Mypy **287 source files** 通过；ESLint、Vue 类型检查、生产构建通过 |
| Evals | candidate gate PASS，6 suites / 10 配对案例；`.eval/workspace-bash-stage2e-final-2026-09-06/` |
| 差异检查 | `git diff --check` 通过 |

相关回归与收尾专项有重叠，不能累加为唯一用例总数。本轮未重跑完整后端/覆盖率门禁，
不沿用旧覆盖率宣称本阶段通过完整发布验收。3 个相关跳过为真实 Docker 显式开关或宿主链接条件；
真实 Bash 链路已单独运行。阶段 2C/2D 的 Docker 数字只是历史证据。

真实 Docker 使用已有不可变镜像：
`sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6`。
确定性 FakeClient 驱动真实 Web/API/SQLite/Workspace/审批/Docker：读取测试 upload 文件，
在副本新增 `artifacts/result.txt`，检查输出和变更摘要，然后确认真实 Workspace 没有该文件、
许可 closed、cleanup_pending=false。只处理临时测试数据，没有真实模型调用或真实 Workspace 发布。
Browser 新用例采用注入事件/模拟历史响应验证 UI；不能替代这条真实后端与 Docker 链路。
最终只读容器清单仅有 `pi-wiki-parser-worker-1`，状态 healthy；测试容器已回收，业务容器未停止或重启。

### 验证中发现并修复

- 协作 Stop 若直接向 Agent 抛 CancelledError，会遗漏请求终态；Web 工具在 signal 取消时
  返回安全取消结果，外部 task 取消仍传播。清理与历史写入作为同一受保护收尾任务等待完成。
- 默认策略此前会先要求未知工具通用确认，再要求精确脚本确认；现在只为该请求复制标准策略，
  保留显式拒绝和原策略不变，实际执行仍强制逐次完整审批。
- 普通 direct 请求的上下文预览也过滤 Bash，与实际装配一致；非匹配 command ID 的输出不会
  被归属给当前任务，匹配 ID 的已知中断保留真实终止原因。
- 首轮 Chromium 为 25 passed / 1 failed：新审批 fixture 在 Session 加载结束前注入事件，
  随后被历史加载覆盖。改为等待一次正常假模型请求结束后再注入，全套复跑最终通过。
- 对历史补测重放拒绝、Session FK、重启中断、容量和过期裁剪、超限 JSON 原子失败；
  对已批准运行补测拒绝/Stop/配置变化/超时、每次新副本、非零退出和跨 Session 详情隔离。

## 下一阶段仍未实现

- Coding/Plan Executor 内 `run_bash` 薄适配与同任务副本复用验收；已有 `coding_run` 不变。
- 独立 Bash 专用文件证据、冻结审阅及 Docker 确认发布；Coding 原固定验证证据链不得降级。
- 后台孤儿/TTL/快照缓存回收、跨账号全局并发配额、Coding/Plan 全文运行历史。
- 管理员执行面板与执行 Telemetry、完整后端/覆盖率及最终全链路发布门禁。

重启不重放也不代表已回收所有未知资源；目前仍需受信后续清理器处理持久 cleanup_pending。
历史 30 天保留策略在新运行触发，不是已实现后台定时 GC。

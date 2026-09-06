# Workspace Bash 阶段 2D：Web Coding/Plan 审批与调度

日期：2026-09-06。对应用户请求“接入 Web 审批和 Coding/Plan 调度”。
本轮完成这两条产品路径，不代表独立 `run_bash` 或整个 Bash 方案已交付。
代码仍位于前置本地提交 `9518499` 之后的工作区，包含未提交的阶段 2C 和本轮 2D；没有新增提交或推送。

## 产品闭环

1. Web 从受信账号、Session、当前请求构造任务身份。Workspace 通过带 revision CAS 的
   `GET/PUT /api/workspaces/{session_id}/execution` 选择 disabled/E2B/local Docker；选择本身不授权。
2. Coding 先准备快照再显示范围确认；Plan 先由 Planner 生成结构化计划，再以同一张卡
   原子批准精确 Plan 版本和 ExecutionGrant。准备和批准均不创建容器。
3. 卡片完整展示目标、输入路径与快照 SHA、后端/数据位置、网络策略、能力、预算和完整 Plan。
   精确审批参数超出 256 KiB 时拒绝，不截断后继续批准；页面按文本呈现，不执行计划中的 HTML。
4. 启动前重新验证原请求、配置/资源选择、计划版本和原快照，使用获准副本启动一次。
   Coding 工具及 Plan Executor 共享该任务上下文；Planner 不持有执行上下文，Verifier 和 UI diff 只读。
5. 验证/冻结由请求调度器执行。旧直接启动、单独 Plan 批准和手动验证/冻结 API 不得绕过任务审批，
   Web 移除相应直接操作按钮。拒绝、Stop、过期、选择/能力变化会终止或使许可失效。
6. 任务结束关闭许可并销毁计算资源，签名冻结制品与 diff 保留给现有 Changes 审阅；
   E2B 仍须独立批准发布，Docker 制品目前只支持审阅/丢弃，发布明确拒绝。下一请求重新审批，不复用旧许可。

审批结果只有在服务端原子批准成功后才显示 approved；过期点击返回安全冲突并取消卡片，
不会出现浏览器认为获准而执行许可实际未成立的状态。

## 本机部署入口

`scripts/dev_web_app.py` 读取以下显式配置；本轮没有设置真实部署环境变量或重启产品服务：

```text
PI_LOCAL_DOCKER_ENABLED=1
PI_LOCAL_DOCKER_IMAGE_ID=sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6
PI_LOCAL_DOCKER_EXE=C:\Users\Administrator\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe
```

配置后还需用户在 Workspace 选择 Local Docker，再逐请求批准。启动服务、浏览设置和选择后端
均不拉取镜像或创建容器；Docker 后端在获准启动时校验固定镜像。配置可用不等于 daemon 已通过健康检查。
保留原 E2B 后端选择，不静默迁移；原隐式启动授权不再保留。Python Analysis 仍采用独立逐次批准，
MCP/Skill 的授权边界不扩大。

## 验证

后端初次全量：**2551 passed / 7 failed / 9 skipped / 9 deselected**，1283.07 秒。
其中 6 项 Wiki API 在过深的测试临时目录下遇到 Windows 路径长度限制，改用短 `--basetemp`
后通过，未修改 Wiki 文件逻辑；另 1 项意图路由断言仍预期旧 `coding_sandbox_disabled`，
已对齐无活跃异步请求时的新 `execution_request_denied` 拒绝契约，仍验证 409 且不启动后端。
没有将初次全量描述成一次全绿运行。

相关增量回归 **191 passed / 3 skipped**，98.77 秒；最终制品交接调整后，Web/生命周期/API
**30 passed / 2 skipped**，17.30 秒，其中新增 4 项发布交接竞态用例。

最终短路径合并复核（`.test-tmp/d2g`）：**209 passed / 3 skipped**，90.50 秒，覆盖全部上述
Wiki API 失败项及 Web Execution、ExecutionRuntime/Grant/Bash、Sandbox API/Lifecycle/Artifact/Publisher、
工具审批和异步请求。3 个跳过是 2 项真实 Docker 显式开关与 1 项宿主符号链接能力条件。
真实 Docker 已由下述单独实机运行验证。

`--cov-append` 汇总本轮全量采集及收尾增量后报告 **78.09%**，超过配置的 75% 阈值，
写出 `.coverage.xml`。这是多轮、包含收尾改动的累计诊断值，不作为最终同一代码快照的单次全量发布覆盖率证据；
收尾后没有重新执行另一轮完整全仓测试。

- 新增 Web 专项：11 项离线用例，另有 2 项需显式 opt-in 的真实 Docker 用例。
- 前端最终全量：32 files / **221 passed**，12.09 秒；ESLint、Vue 类型检查和生产构建通过。
- Chromium 全套最终运行状态 `passed`、`failedTests=[]`；24 个浏览器用例。该记录不宣称零重试。
- Ruff 通过；strict Mypy **286 source files** 通过；`git diff --check` 通过。
- Evals candidate gate 通过，6 suites / 10 配对案例，产物位于
  `.eval/workspace-bash-stage2d-2026-09-06/`。不作为真实模型能力评测。
- 真实 Docker Web Coding/Plan：**2 passed**，99.86 秒。使用确定性 FakeClient、真实 Web/API/SQLite/
  Workspace/审批/容器/代码工具/验证/签名冻结/销毁流程，不调用真实 LLM 或 E2B。

真实 Docker 复用了上述不可变镜像，无安装、拉取或构建；只处理测试临时目录，没有发布到真实 Workspace。
两条链路均验证结束后无运行许可/容器、仍可读取冻结 diff、Docker 发布被拒绝及可丢弃制品。
最终只读容器清单只有 `pi-wiki-parser-worker-1`，状态 healthy；业务容器没有停止或重启，
Docker 数据仍在 `D:\DockerData\DockerDesktopWSL`。

[阶段 2C](workspace-bash-stage2c-2026-09-06.md) 的 25 项后端 Docker 检查是历史证据，
不与本轮 2 条 Web 链路合并计数。

### 验证中修复的问题

- 活跃许可先持久化后播种的启动窗口：watcher 不将 `cleanup_pending` 启动屏障误判为失效；
  此窗口由 start 自身验证/取消，Stop 另有初始化阻塞时的回收测试。
- Windows 深层测试目录触及路径长度限制导致制品下载失败：缩短共享 operation staging 层级及
  冗余的制品下载文件名前缀，仍使用唯一临时名/内容寻址，随后完整真实冻结链路通过。
- 用户提前批准发布与请求结束并发：释放计算资源时保留 publishing/publish_conflict 的签名制品，
  幂等释放并继续提供缓存 diff，不取消已批准的发布或回读被销毁的容器。
- 首次浏览器运行因沙箱禁止子进程而 EPERM，按权限流程重新运行；两项旧 Sandbox E2E
  仍点击直接启动按钮，已改为审阅调度器产生的冻结制品并断言刷新不重放/不直接启动。
- 初次真实 Docker 用例的等待窗口不足，改为仅 opt-in 实机使用有界 120 秒等待，离线仍为 10 秒。

## 明确未完成

- 独立 `run_bash` 完整脚本审批及工具注册；当前仅 Coding/Plan 任务范围授权。
- Docker 制品发布（阶段 3）；不能把本轮执行成功理解为文件已回写 Workspace。
- 后台 orphan/TTL reconciler、快照/制品缓存保留期回收；重启不恢复执行，无法确认的资源保留清理债务。
- 跨账号共享的全局并发配额；当前持久许可和限制按账号数据库隔离。
- 私有持久运行全文记录；审批卡当前仍为进程内有界记录，Grant/Plan 持久化不意味着审批卡或执行可重启恢复。
- 本机执行专用 Admin/Telemetry 页面；本轮未做真实云端 E2B 执行到发布的端到端复验。
- Wiki 过深数据根目录的 Windows 长路径兼容仍未修复，已记入 TODO；短测试根只是本轮复核条件。

下一步先补独立 Bash 的逐次完整脚本确认和拒绝/取消测试，再进入 Docker 发布及生命周期清理。

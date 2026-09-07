# 当前测试策略

本项目处于功能快速收敛、尚未发布稳定 1.0 的阶段。测试目标是尽快发现当前产品的
语义、安全和关键用户旅程回归，不再为已退役实现或每个历史阶段维持重复验收矩阵。
准确的最近一次结果见 [`STATUS.md`](../../STATUS.md)。

当前统一执行入口为 `scripts/run_local_gates.py`；覆盖率、Worker、Chromium 和显式 Docker
分组及日志边界见 [本地门禁指南](local-gates.md)。默认不运行真实 MinerU/LLM，不用 Fake 代替实机验收。

## 基本原则

1. 优先运行和复用现有测试，不为已有覆盖重复编写测试。
2. 只测试本次修改产生的外部可观察行为，不测试内部实现细节。
3. 不为了“更加完整”主动扩展测试范围。
4. 不编写大规模参数化测试、重复边界测试、快照测试或大量 Mock。
5. 不为简单 getter、数据类型声明、样式、文案、配置文件或机械性重构新增测试。
6. 不修改或删除与当前任务无关的现有测试。
7. 不以提升测试覆盖率为任务目标。

## 测试预算

预算是上限，不是必须达到的数量；现有测试已覆盖时不新增测试。

| 修改类型 | 默认上限 |
|---|---|
| 新功能 | 最多 2 个：1 个主要成功路径，以及 1 个最高风险失败路径或边界条件 |
| Bug 修复 | 最多 1 个能够复现该 Bug 的回归测试 |
| 行为不变的重构 | 默认不新增测试，只运行现有相关测试 |
| UI 样式、文案、文档、开发配置 | 默认不新增测试 |
| 测试文件 | 每次任务默认最多新增或修改 1 个测试文件 |

只有身份认证或权限控制、支付或计费、数据迁移或数据丢失风险、并发/事务/幂等性、
公共 API 兼容性变更，以及用户明确要求更全面测试时，才考虑突破预算。突破前必须先向
用户说明额外测试防止的具体风险、现有测试无法覆盖的原因和准备增加的测试数量，并取得确认。

## 执行流程

修改代码前先检查相关现有测试，判断覆盖是否已经足够，并简短说明计划新增的测试及数量。
修改后先运行与修改直接相关的最小测试集合；必要时可以运行更大的现有测试集，但不能因此
自动新增更多测试。失败时先判断是否由本次修改引起，不顺手修复无关问题。

最终汇报只记录测试新增或修改数量、每个测试验证的关键行为、实际运行的测试及结果。

## 保留标准

测试只有满足下列至少一项时才进入当前套件：

- 锁定 Agent loop、消息/事件流、Provider 适配、Session 与持久化的核心语义；
- 锁定凭证、路径、上传、审批、Sandbox、Wiki artifact 等安全边界；
- 覆盖当前产品 API、Store 或前端组件的主要成功路径和关键失败路径；
- 复现一个真实缺陷，且更低层测试不能可靠捕获；
- 以一条浏览器旅程证明多个层次可以正确组合。

以下内容不进入默认套件：

- 已退出产品主链路的实现细节；
- 与单元/组件测试完全重复的浏览器断言；
- 只证明某个历史阶段“曾经完成”的冻结用例；
- 真实网络、真实 LLM、Docker 或云 Sandbox 测试；
- 依赖 sleep、外部账号或不稳定模型选择的日常回归。

历史阶段证据保留在 `docs/validation/`，但不作为当前 HEAD 的测试清单。

## 测试金字塔

| 层级 | 用途 | 默认 CI |
|---|---|---|
| Python 单元/组件 | 核心语义、Store/API、安全与故障边界 | 是 |
| Frontend Vitest | 少量关键 Store 行为、组件交互与渲染契约 | 是 |
| Playwright | 少量跨层关键用户旅程 | 发布前/本地 |
| Live/OCI/E2B smoke | 验证外部服务、凭证和真实运行环境 | 显式执行 |

旧 Chunk-RAG 代码只作为显式兼容实现保留，并从当前产品覆盖率分母排除。当前套件只保留
一个退役守卫，确认即使配置了旧目录也不会创建旧数据库、启动 Worker、注册
`search_knowledge` 或挂载旧 REST 路由。

## 日常命令

Backend 默认离线门禁：

```powershell
Set-Location D:\LLMTutorial\test
$env:PYTHONPATH = "src"
D:\miniconda\envs\pipy\python.exe -m pytest tests --tb=short -q --basetemp=.test-tmp/full
```

默认配置排除 `slow`、`integration` 和 `docker`，并使用工作区内可写的 pytest cache
和临时目录。Windows 使用短 basetemp 避免深层安全路径测试撞到系统路径长度限制。
完整门禁保留 branch coverage 和 75% 门槛；只跑某几个文件时显式加 `--no-cov`，
不能把定向测试的覆盖率或多批次合并值当作一次完整门禁。

Frontend 快速门禁：

```powershell
npm --prefix src/pi_agent_core_py/web/frontend run lint
npm --prefix src/pi_agent_core_py/web/frontend run typecheck
npm --prefix src/pi_agent_core_py/web/frontend test
npm --prefix src/pi_agent_core_py/web/frontend run build
```

## Playwright 关键旅程

当前保留以下 12 个规格、26 项浏览器测试：

| 规格 | 关键风险 |
|---|---|
| `web-claude-smoke.spec.ts` | 登录后布局与基本聊天 |
| `session-routing-refresh.spec.ts` | URL/Session 隔离、刷新恢复、登出边界 |
| `mcp-tool-lifecycle.spec.ts` | MCP 配置与 Tool 生命周期 |
| `human-approval.spec.ts` | Tool 批准/拒绝及刷新恢复 |
| `context-budget-compaction.spec.ts` | Context budget 阻塞与 compaction 恢复 |
| `workspace-results.spec.ts` | Agent 成果进入 Workspace 与右栏展示 |
| `coding-sandbox.spec.ts` | Sandbox 验证、审批发布与失败阻断 |
| `knowledge-wiki.spec.ts` | Source → Change Set → Page/FTS/Graph/Conversation |
| `data-analysis.spec.ts` | 可选固定分析、成果保存及 Workspace 恢复 |
| `python-data-analysis.spec.ts` | 独立 Python 执行批准/拒绝、刷新后不重放 |
| `bash-approval.spec.ts` | 完整脚本确认、只读私有历史、不重放 |
| `telemetry-admin.spec.ts` | Admin 脱敏观测与执行管理鉴权 |

运行：

```powershell
$env:CI = "1"
$env:E2E_PORT = "8031"
$env:E2E_PYTHON = "D:/miniconda/envs/pipy/python.exe"
npm --prefix tests/e2e run test:e2e
```

Playwright 使用单 worker、FakeClient 和独立端口，不调用真实 Provider。失败时查看
`tests/e2e/test-results/` 中的 trace、截图和视频。
成功后 npm 的 posttest 自动恢复 production build；测试失败时手动执行前端 `npm run build`，
不要将 E2E 构建留作业务静态页面。此层以 Fake 后端/网络响应验证 UI，不冒充真实 Docker 执行证据。

## 显式环境 smoke

Local Docker 分为底层安全检查和真实 Web 链路。必须使用已验收的本机固定镜像 SHA；
这些命令不安装、不拉取镜像、不启用部署，也不修改业务 Workspace。

```powershell
$env:PYTHONPATH = "src"
$env:PI_TEST_DOCKER_IMAGE = "sha256:9b3899021dbd4afaa0225ab5a5b4bb1764d1743bf8e5e103f3a56795df85a3f6"
$env:PI_TEST_DOCKER_EXE = "C:/Users/Administrator/AppData/Local/Programs/DockerDesktop/resources/bin/docker.exe"
D:\miniconda\envs\pipy\python.exe scripts/check_bash_docker.py --docker-executable $env:PI_TEST_DOCKER_EXE --image-id $env:PI_TEST_DOCKER_IMAGE --verify
D:\miniconda\envs\pipy\python.exe -m pytest tests/test_execution_maintenance.py tests/test_web_execution.py tests/test_web_bash.py tests/test_web_task_bash.py -m docker --no-cov --basetemp=.test-tmp/real --tb=short -q
```

真实测试同时要求显式 `-m docker` 与镜像环境变量；默认离线套件即使继承镜像变量也不会创建容器。
上例的真实 Web 传输使用 Windows Docker Desktop Linux Engine；其他主机需适配测试 endpoint，
不能将 Windows 本机验收外推为已通过 Linux/macOS 验收。完成后只读检查测试 namespace 已无残留，
不得用全局 prune 或删除业务容器来“清理测试环境”。未知结果必须保留清理债务，不能重放命令。

DDGS + GLM：

```powershell
D:\miniconda\envs\pipy\python.exe scripts/run_live_integration_tests.py
```

该脚本固定执行 1 个 DDGS 与 2 个 GLM smoke，并要求 `PI_RUN_INTEGRATION=1` 门禁。
E2B、Parser OCI 和 Windows Keyring 继续使用各自的专用 smoke 脚本；它们不属于默认
pytest/CI，只有变更对应 Provider、凭证或运行环境边界时才执行。

## 新增测试规则

修复缺陷时优先在最低可验证层增加一个回归测试。只有当风险来自跨层组合时才新增
Playwright；如果已有低层测试能稳定覆盖，就不再复制浏览器用例。删除功能时同步删除
其行为测试，并为“功能不会被误启用”保留至多一个产品边界测试。

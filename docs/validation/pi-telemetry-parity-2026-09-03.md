# pi `telemetry` 对齐与 Admin 前端报告（2026-09-03）

## 范围

本轮比较本项目与本机 `D:\LLMTutorial\pi\pi-main\packages\telemetry` 以及
`packages/coding-agent/src/core/telemetry.ts`，完成逐模块计划的第 5 项。上游的关键价值不是某个
监控平台，而是一个与 Backend 无关、callback-shaped、失败不影响业务的 `TelemetryContext` /
`TelemetrySpan` 契约。本项目在对齐这层契约后，增加适合本地多账号产品的 SQLite Recorder、
Admin REST API 和 Vue 仪表盘。

## 已落地结构

```text
pi_agent_core_py/
├── telemetry/
│   ├── types.py                 # Context、Span、Reader 与 record DTO
│   ├── noop.py                  # 禁用/故障时的透明 fallback
│   ├── memory.py                # 测试与 embedder 的 reference recorder
│   ├── schema.py                # 可声明 span/event/attribute vocabulary
│   ├── _attributes.py           # 有界复制与持久化字段脱敏
│   └── sqlite.py                # WAL、retention、query 与聚合
└── web/
    ├── telemetry/
    │   ├── instrumentation.py   # AgentEvent → 安全运行元数据
    │   └── api.py               # Admin-only summary/list/detail
    └── frontend/src/
        ├── stores/telemetryStore.ts
        └── components/telemetry/TelemetryDashboard.vue
```

`CodingAgentServices` 持有统一 `TelemetryContext`；`CodingAgentSession` 产生
`coding_agent.request → agent.run` 嵌套 span。Web 的同步/异步 Prompt 共用 `web.request` 根 span，
由现有 Agent event hook 累加模型、token、延迟、工具和 outcome 数据。开发启动器让所有账号连接
同一个 `.pi-agent-data/telemetry.sqlite`，因此 Admin 可在一个页面查看跨账号运行状态。

## 差距与收敛结果

| 能力 | 本轮前 | 本轮结果 |
|---|---|---|
| Backend-neutral 契约 | 无统一 span/context 接口 | 对齐 callback-shaped Context/Span、嵌套 trace/parent、event、attribute、status 与 noop/memory 实现 |
| 被观测业务的可靠性 | 观测代码需自行处理失败 | Recorder 初始化、写入、终化失败均为 passive；callback 返回、异常与取消语义不变 |
| 产品接线 | 无统一 Agent 请求指标 | Web Prompt 与 Coding Agent Session 记录 outcome、Provider/model、token/cost、turn/tool 数及耗时 |
| 持久化 | 无 | 独立 SQLite schema v1、WAL、并发账号连接、running 可见、事务终化、30 天/50,000 span 双限保留 |
| 查询与聚合 | 无 | 24h/7d/30d摘要、错误率、平均/P95、token/cost、Top tools、Provider、账号和小时趋势；支持状态/账号/Session 筛选及详情 |
| Admin 授权 | 账号只有 id/name | Auth schema v2 增加持久 `is_admin`；新 bootstrap admin 和 v1 的既有 admin 迁移为管理员，API 服务端强制 403 |
| 前端 | 无 | Admin 侧栏入口与 `/telemetry` 页面；概览卡、趋势、工具/Provider 排名、请求表、筛选和事件详情；15 秒自动刷新 |
| 隐私 | 无统一约束 | Prompt、消息正文、thinking、Tool arguments/output、凭证、异常正文和 abort reason 均不持久化；字段有数量/长度上限并按敏感 key 二次脱敏 |
| Package 边界 | 无 | `telemetry` 不依赖 Agent、Web、Coding Agent 或 Session Backend；AST 结构门禁固定依赖方向 |

## 保留的项目差异

- 上游提供轻量 TypeScript contract/schema，安装具体 Backend 由宿主决定；本项目除同等核心契约外，
  自带 SQLite Reader/Recorder 和本地 Admin 产品面。这是功能扩展，不让 Core 反向依赖 Web。
- Python 的 callback 可能同步或 awaitable，`start_span` 本身是 async；上游以 TypeScript Promise
  统一表达。两者都保证 callback 结束时 span 终化。
- 当前没有 OTLP/OpenTelemetry exporter、分布式进程 trace 合并、告警或指标推送；SQLite 面板定位为
  localhost 单机运行诊断，不是外部 APM。
- 当前产品主指标以普通 Web Prompt/Coding Agent request 为根；Regenerate、Checkpointer、Wiki Agent
  等独立操作尚未建立专用 schema，后续应按需要显式扩展，而不是把内容塞入通用属性。
- Admin 是单一持久布尔角色，不是完整 RBAC；产品仍保持 localhost-only，当前没有用户管理/授权 UI。

## 安全与生命周期

- Admin 前端隐藏只是体验层；三个 `/api/admin/telemetry/*` endpoint 都从网关委派的真实
  `auth_user.is_admin` 再鉴权，并继续执行 trusted UI header 与 Origin 检查。
- 每个账号 workspace app 使用独立 SQLite connection，但数据库路径共享；meta 初始化幂等，
  `busy_timeout` + WAL 处理正常并发，connection 内以 lock 包围事务、rollback 和 close。
- `web.request` callback 执行期间已写入 `running` 行；成功、业务错误或取消时原子更新 span 并写 events。
  Recorder 不可用时自动切到 noop，主回答不因此失败。
- `.pi-agent-data/telemetry.sqlite` 与账号 workspace 数据分离；默认保留 30 天且最多 50,000 个 span。

## 验证结果

| 门禁 | 结果 |
|---|---|
| Telemetry/Auth/API/结构专项 | 21 passed |
| Backend 全量 | 2187 passed、7 skipped、9 deselected；coverage 76.43%；422.12s |
| 主项目静态检查 | Ruff PASS；strict Mypy 275 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 16 source files / 0 issues |
| Frontend | Vitest 187/187（28 files）；typecheck、ESLint、production build PASS |
| Browser E2E | Playwright 20/20；含 Admin 请求→Telemetry 表格→详情与正文不显示；最终 HEAD 1.1m |
| Wheel | 离线构建 PASS；444 entries；Telemetry Core/Web 模块与前端资源在包内；隔离安装导入 PASS |

Browser 首次在受限命令沙箱中因 Chromium `spawn EPERM` 无法启动；按权限流程在本机测试环境重跑后
20/20 通过。默认门禁使用 FakeClient，不访问真实 Provider、外网、Docker、E2B 或 Parser OCI。

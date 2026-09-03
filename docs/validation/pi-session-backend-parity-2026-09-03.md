# pi `session-backends/sqlite-node` 对齐报告（2026-09-03）

## 范围

本轮比较本项目与本机
`D:\LLMTutorial\pi\pi-main\packages\session-backends\sqlite-node` 快照，实施已确认的第 4 项：
把既有 SQLite 消息/会话树 Store 收敛为 Agent Harness 可依赖的 Session Repository、每会话
Storage handle 与独立 Search backend，并把它接入 `CodingAgentRuntime`。目标是对齐持久化职责、
并发和恢复语义，同时保留现有 Python Web 数据库与公开 API 的向后兼容。

## 已落地结构

```text
pi_agent_core_py/
├── agent/harness/session/
│   └── types.py                 # Repository/Storage/Search 协议与 DTO
└── session_backends/sqlite/
    ├── repo.py                  # Repository、Storage handle 与兼容 Store
    ├── search_backend.py        # 独立、惰性 FTS5 搜索
    ├── database.py              # 共享 connection 的任务可重入串行化
    ├── migrations.py            # 有序 migration ledger 与事务应用
    ├── migrations/
    │   ├── 001_baseline.sql
    │   └── 003_repository.sql
    ├── branch_cache.py
    ├── sql.py
    ├── types.py
    └── storage/
        ├── sessions.py
        ├── entries.py
        ├── records.py
        ├── lanes.py
        ├── facts.py
        ├── session_sequences.py
        ├── session_stats.py
        ├── writer_leases.py
        ├── branch_entries.py
        └── branch_tips.py
```

旧 `pi_agent_core_py.session_sqlite` 与 `session_backends.sqlite.store` 仍是模块级兼容别名；私有
测试 hook 的 monkeypatch 行为和公开对象身份不变。结构门禁同时禁止 SQLite backend 反向依赖
Web 或 `coding_agent_app`。

## 差距与收敛结果

| 上游能力 | 本轮前 | 本轮结果 |
|---|---|---|
| Harness 级持久化协议 | Web 与 Store 具体类直接耦合 | 新增 provider-neutral `SessionRepository`、`SessionStorage`、`SessionSearch` 协议；错误带稳定 `code` |
| Repository 生命周期 | 只有单体 Store CRUD | 支持 create/open/list/delete/fork/close；打开 handle 持有可释放 writer ownership |
| 全局 append-only 日志 | entry、lane、fact、operation 分散计数 | entry/record/lane/fact 共用 Session sequence，支持 `get_log(after_seq, limit)` |
| typed entry/record 查询 | 主要面向消息树和产品 operation | 支持 type/custom type、order/cursor/limit、branch bounds、lane/run/operation kind 与 open-operation recovery 查询 |
| 统计 | 无 Repository 级统一统计 | 按上游口径维护 message、cache read、input + cache write、total token 与 cost；旧库 migration 回填 |
| 多 writer 防护 | 进程内写锁，没有跨 Repository fence | writer lease 使用 owner/fence/TTL/heartbeat；第二 writer 拒绝，过期接管后旧 writer 永久失效 |
| branch read cache | 运行时遍历 parent 链 | 持久 `branch_entries`/`branch_tips`，append 增量更新，损坏时显式 repair，不静默返回不完整分支 |
| schema 演进 | 单次建表与旧树迁移 | migration ledger、有序事务、未知未来 migration 拒绝、失败整项回滚；SQL 资源随 wheel 发布 |
| Session 搜索 | 无跨 Session entry 搜索协议 | 独立惰性 FTS5 trigram backend；external-content trigger、类型过滤、limit、取消检查与删除同步 |
| 共享 SQLite 并发 | Session/Extension/Plan 可在同 connection 上交错事务 | connection 级任务可重入锁覆盖完整公开操作；异常和取消先 rollback 再解锁；关闭边界清理残留事务 |
| Coding Agent 接线 | Runtime Session 只有内存 Harness 状态 | lifespan 启动后每个 Web Session ID 打开独立 `SessionStorage` handle；Runtime 删除/关闭释放 handle，Repository 再于 connection owner 前关闭 |

## 保留的项目差异

- 本项目使用异步 `aiosqlite`，上游 Node backend 使用同步 SQLite adapter；因此这里采用共享
  connection 的 async operation queue，而不是复制 Bun/Node 的同步事务实现。
- 现有 `sessions/messages/session_entries` schema 和 Web API 需要无损升级，migration 采用
  `001_baseline → 002_legacy_tree → 003_repository → 004_repository_backfill`，没有强制重建为上游
  单表命名。旧线性消息会被幂等投影到 `main` lane。
- 旧 schema 的 entry ID 是全库主键；fork 因而复制内容并生成新 entry/message ID，而上游复合键
  schema 可在新 Session 保留 entry ID。父子关系、lane、label、消息数与来源 Session 仍被保留。
- `SessionMetadata` 继续携带 Web 所需的 `title/updated_at/metadata`，没有照搬上游 Node
  filesystem env 的 `cwd/path`；文件所有权继续由独立 `WorkspaceStore` 管理。
- entry/record DTO 采用 Python 的 `type + payload` 开放模型，以兼容现有消息、compaction 与产品
  operation；上游 TypeScript discriminated union 的全部编译期窄化不会在运行时模型中逐项复制。
- 未进入 lifespan 的低层 TestClient/Embedder 组合保留无持久化 Harness 投影；正常启动的产品路径
  必须安装 Repository，并由回归测试断言每个 Runtime Session 都有独立 Storage handle。

## 验证结果

| 门禁 | 结果 |
|---|---|
| SQLite Repository/旧 Store/Web 映射专项 | 94 passed |
| 共享 SQLite/Coding Agent 邻接回归 | 152 passed |
| 修复后兼容回归 | 75 passed |
| Backend 全量 | 2175 passed、7 skipped、9 deselected；coverage 76.27%；407.31s |
| 主项目静态检查 | Ruff PASS；strict Mypy 265 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 16 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |
| Browser E2E | Playwright 19/19；0 failure；production build 已恢复 |
| Wheel | 离线构建 PASS；428 entries；migration SQL 与 SQLite/Coding Agent 模块均在包内；`python -I` 隔离导入及 SQLite migration 初始化 PASS |

真实 LLM、外网、Docker、E2B 与 Parser OCI 未在本轮重跑；默认 Backend 和 Browser 门禁均使用
确定性离线 Provider。逐模块计划的下一项是第 5 项 `telemetry`。

# pi `agent` package 结构对齐报告（2026-09-01）

## 目标与边界

本轮在已完成 `ai` 与 `agent` 功能契约对齐的基础上，把 Python 实现的物理所有权调整为与
pi-agent 相同的分层方向，同时保留本项目既有公开导入、持久格式和 Web 行为。这里对齐的是
职责与依赖方向，不机械复制 TypeScript/Bun/Node 专属文件。

## Canonical 结构

```text
pi_agent_core_py/
├── ai/                         # 消息、模型客户端、流事件、Provider adapters
├── agent/                      # runtime、loop、events、hooks、context、tool contracts
│   └── harness/
│       ├── compaction/         # budget + compaction/branch summary
│       ├── session/            # memory/store contracts + sync
│       └── tools/              # list/view/write/web search
└── session_backends/
    └── sqlite/                 # append-only Session tree backend
```

依赖关系固定为 `agent core → ai`、`harness → agent core / ai`，以及
`session backend → harness / agent core / ai`。AI 层不得依赖 Agent、Harness、Web 或 SQLite；
Agent Core 不得依赖 Harness、Web 或 SQLite。产品专属 Coding Sandbox 工具仍留在产品组合边界，
不强行放入通用 Harness。

## 兼容策略

- `pi_agent_core_py.messages/model_client/loop/harness/session/...` 继续可导入，但只重导出 canonical
  实现；旧路径与新路径取得同一 class/function 对象。
- `pi_agent_core_py.agent` 从单文件变为 package，仍直接导出 `Agent`，新增明确的
  `pi_agent_core_py.agent.runtime.Agent`。
- `session_sqlite` 使用真实模块别名而非仅复制符号，既保持类身份，也保留历史测试/扩展对
  `_gen_id`、`aiosqlite` 等模块属性的 monkeypatch 行为。
- Provider 与通用工具旧子模块保留 thin facade；canonical 所有权分别位于 `ai.providers` 和
  `agent.harness.tools`。

## 验证

| 门禁 | 结果 |
|---|---|
| 结构契约 | 7 passed；覆盖旧/新对象身份、AI/Agent AST 依赖方向和 facade 轻量性 |
| Backend 全量 | 2138 passed、7 skipped、9 deselected；coverage 77.33%；365.58s |
| Ruff | `ruff check src tests scripts` PASS |
| strict Mypy | `mypy src`：232 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 16 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |
| Wheel | `0.0.29` wheel 构建 PASS；393 entries；canonical Agent/Harness/SQLite 隔离安装导入 PASS |

完整门禁第一次运行的 2 个失败只涉及旧 `session_sqlite` 模块 monkeypatch 语义；升级为模块别名后，
原失败专项 12 passed，第二次全量 2138 项通过。

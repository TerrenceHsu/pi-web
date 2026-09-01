# pi `coding-agent` 首轮对齐报告（2026-09-01）

## 范围

本轮比较本项目与本机 `D:\LLMTutorial\pi\pi-main\packages\coding-agent` 快照，实施已确认的
三个阶段：建立可复用的产品组合核心，把 Sandbox automation/workspace 的规范所有权迁入
`coding_agent_app`，再让 Web Session ID 成为 `CodingAgentRuntime` Session 的真实键。目标是对齐职责
和依赖方向，不复制 TypeScript/Bun/CLI/TUI 的平台实现。

## 已落地结构

```text
coding_agent_app/
├── core/
│   ├── application.py       # 产品顶层组合对象
│   ├── runtime.py           # 多 Session 所有权、创建/替换/关闭
│   ├── session.py           # Harness 的请求级工具/策略/client 绑定
│   ├── harness_template.py  # 从产品模板克隆独立 Agent/Harness
│   ├── services.py          # 产品服务集合
│   ├── settings.py          # secret-free 设置快照
│   ├── resources.py         # Skill/MCP/context 资源快照与诊断
│   ├── toolsets.py          # 按模式解析完整工具集
│   ├── prompts.py           # 确定性 Prompt contribution 合并
│   └── sdk.py               # 稳定公开入口
└── sandbox/
    ├── automation.py        # 自动 Coding 编排与 bootstrap client
    └── workspace.py         # Workspace baseline/publisher 产品适配
```

`pi_agent_core_py.web.coding_sandbox.automation/workspace` 和
`coding_agent_app.sandbox_workspace` 只保留 thin compatibility facade；新旧导入得到同一对象。
`coding_agent_app` 的 AST 门禁禁止反向导入 `pi_agent_core_py.web`。

## 行为收敛

- `CodingAgentRuntime` 按 Session ID 拥有 Session；同 ID 并发创建只调用一次 factory，Runtime
  关闭期间完成的迟到创建会立即关闭，不会泄漏 Harness。
- `CodingAgentSession.compose_request()` 是请求资源的唯一入口。它一次冻结 Provider selection、
  Skill 快照、MCP/普通工具集、Workspace/路由 Prompt contribution，并在成功、异常或取消后统一恢复
  临时 client、工具注册表、权限策略与 Skill registry；同 Session 重叠请求 fail fast。
- `ToolsetResolver` 固定 `direct/read_only/coding/plan/knowledge/checkpointer` 六种模式。
  Coding/Plan 缺任一必需工具即拒绝；Knowledge 必须显式提供工具；显式 override 不能绕过
  read-only/checkpointer 等受限模式。
- 每个持久 Web Session ID 现在直接映射到一个 `CodingAgentRuntime` Session。默认 Session 复用入参
  Harness 作为兼容投影，其他 Session 从模板克隆独立 Agent/Harness 状态机；消息、Snapshot、事件、
  Abort、Regenerate、Checkpointer、Compaction 与 Session tree 投影均按 ID 定位实例。不同 Session
  可并行，同一 Session 仍保持单 active request。克隆保留宿主自定义 `ModelClient` 子类行为，但只
  借用且不接管应用级 Provider transport 的关闭所有权。
- 入参 Harness 只继续拥有应用级 Skill/MCP 配置和 MCP transport；每次请求由
  `HarnessCodingAgentResourceLoader` 生成不可变资源快照。MCP Tool 对象可共享，但工具注册表与请求状态
  不共享，Workspace/Sandbox 选择继续由 task-local Session binding 隔离。
- 默认 pytest coverage 由仅 Core 扩为同时统计 `pi_agent_core_py`、`agent_workspace`、
  `coding_agent_app` 和 `coding_sandbox`。

## 与上游仍有差距

1. Provider、Skill、MCP、Workspace 和 Prompt 已统一进入请求 composition pipeline，但持久 Store、
   Credential/Provider 配置 API、MCP transport 启停和 Workspace assembler 的具体生命周期仍由 Web
   application adapter 持有；后续可继续缩小 `web.app` 的组合体积。
2. 上游 `modes/extensions/cli/tui` 的产品化能力尚未对齐。本项目已有意图路由、Plan、Web UI
   和扩展 Store，但它们仍是分散实现，未形成统一 extension/mode 生命周期。
3. 图片附件到 provider-neutral `ImageContent` 的产品接线仍待完成；SQLite backend 与 telemetry
   分别属于原五项对齐计划的第 4、5 项，不在本轮范围内。

## 验证结果

| 门禁 | 结果 |
|---|---|
| Coding Agent Core 专项 | 17 passed |
| Web Session 映射专项 | 57 passed |
| Backend 全量 | 2160 passed、7 skipped、9 deselected；coverage 76.14%；372.67s |
| 主项目静态检查 | Ruff PASS；strict Mypy 246 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 16 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |
| Browser E2E | Playwright 19/19；0 retry、0 failure；53.9s；production build 已恢复 |
| Wheel | 离线构建 PASS；407 entries；新增 Core 模块在包内；`python -I` 隔离导入 PASS |

真实 LLM、外网、Docker、E2B 和 Parser OCI 未在本轮重跑；默认 Backend 明确排除这些 marker，
相关真实环境证据继续沿用 `STATUS.md` 中最近一次已归档结果。

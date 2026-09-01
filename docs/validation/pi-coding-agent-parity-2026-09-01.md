# pi `coding-agent` 首轮对齐报告（2026-09-01）

## 范围

本轮比较本项目与本机 `D:\LLMTutorial\pi\pi-main\packages\coding-agent` 快照，实施已确认的
前两阶段：先建立可复用的产品组合核心，再把 Sandbox automation/workspace 的规范所有权迁入
`coding_agent_app`。目标是对齐职责和依赖方向，不复制 TypeScript/Bun/CLI/TUI 的平台实现。

## 已落地结构

```text
coding_agent_app/
├── core/
│   ├── application.py       # 产品顶层组合对象
│   ├── runtime.py           # 多 Session 所有权、创建/替换/关闭
│   ├── session.py           # Harness 的请求级工具/策略/client 绑定
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
- `CodingAgentSession` 是请求级可变状态的唯一入口。工具注册表、权限策略和临时 client 在成功、
  异常或取消后统一恢复；重叠请求 fail fast。
- `ToolsetResolver` 固定 `direct/read_only/coding/plan/knowledge/checkpointer` 六种模式。
  Coding/Plan 缺任一必需工具即拒绝；Knowledge 必须显式提供工具；显式 override 不能绕过
  read-only/checkpointer 等受限模式。
- Web 仍兼容既有共享 Harness，但 transport 不再直接赋值 `harness.agent.tools/client`；只通过
  `CodingAgentSession.bind_request()` 与 `bind_client()` 进入产品边界。
- 默认 pytest coverage 由仅 Core 扩为同时统计 `pi_agent_core_py`、`agent_workspace`、
  `coding_agent_app` 和 `coding_sandbox`。

## 与上游仍有差距

1. 上游 coding-agent Session 是产品主对象；本项目 Web 目前仍围绕共享 Harness 运行，只用
   `CodingAgentSession` 作兼容 facade。下一阶段应让 Web Session ID 直接映射 Runtime Session，
   再开放同账号多 Session 并发执行。
2. Settings/Resources/Services 已有稳定契约，但现有 Provider、Skill、MCP、Workspace、Sandbox
   store 尚未全部通过 adapters 注入；Web 仍承担较多组合代码。
3. Prompt contribution helper 已建立，但 Workspace、路由、Skill、Provider notice 尚未统一经过
   单一 composition pipeline。
4. 上游 `modes/extensions/cli/tui` 的产品化能力尚未对齐。本项目已有意图路由、Plan、Web UI
   和扩展 Store，但它们仍是分散实现，未形成统一 extension/mode 生命周期。
5. 图片附件到 provider-neutral `ImageContent` 的产品接线仍待完成；SQLite backend 与 telemetry
   分别属于原五项对齐计划的第 4、5 项，不在本轮范围内。

## 验证结果

| 门禁 | 结果 |
|---|---|
| 首轮核心/结构专项 | 18 passed |
| Sandbox/Web 邻接回归 | 146 passed、1 skipped |
| Backend 全量 | 2156 passed、7 skipped、9 deselected；coverage 76.03%；879.96s |
| 主项目静态检查 | Ruff PASS；strict Mypy 245 source files / 0 issues |
| Wiki Parser Worker | Ruff PASS；strict Mypy 16 source files / 0 issues |
| Frontend | Vitest 183/183；typecheck、ESLint、production build PASS |
| Browser E2E | Playwright 19/19；0 retry、0 failure；production build 已恢复 |
| Wheel | 离线构建 PASS；16 个相关 canonical/compatibility 条目；隔离导入对象身份 PASS |

真实 LLM、外网、Docker、E2B 和 Parser OCI 未在本轮重跑；默认 Backend 明确排除这些 marker，
相关真实环境证据继续沿用 `STATUS.md` 中最近一次已归档结果。

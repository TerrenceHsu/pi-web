# pi evals 本机对齐记录

## 结论

本项目已建立独立的本机离线 evals 模块。实现对齐了上游最有价值的评测边界：
真实 CodingAgentApplication Session、隔离运行、prompt/reload 步骤、确定性 Judge、
baseline/candidate 重复配对、正确率与 token/延迟/成本汇总、缺失观测诊断及 JSONL
产物。

本项目没有照搬上游的真实远程模型默认值。内置套件只使用 FakeProviderAdapter，
不加载 .env、不调用 HTTP、不启动外部 MCP，也不经过 Web 或 Protocol。

## 复用的产品事实

- CodingAgentApplication 和 CodingAgentRuntime 负责 Session ID 与实例生命周期。
- CodingAgentSession 统一装配 Provider、Skill、MCP、Workspace 和 Prompt。
- SQLiteSessionRepository 在 reload 前保存消息和 RequestSnapshot，并在重建时恢复。
- RequestSnapshot 提供工具调用、结果与终态事实。
- InMemoryTelemetryContext 提供本地嵌套 span，同时保持正文不采集。

## 新增能力

| 能力 | 本地实现 |
|---|---|
| Eval 输入 | PromptStep 与 ReloadStep |
| 隔离 | 每个 Observation 独立临时 Workspace 和 SQLite |
| Harness | 真实 Application → Runtime → Session → AgentHarness |
| Judge | 文本、JSON Schema、停止原因、工具序列、工具错误、Workspace、资源装配、reload、Telemetry 隐私 |
| 对照 | baseline、多个 candidate、repetition、稳定 group key |
| 汇总 | pass-rate lift、wins/ties、token、latency、cost、diagnostics |
| 产物 | manifest、runs JSONL、JSON/Markdown summary |
| 门禁 | candidate threshold 与基础设施错误；弱 baseline 不阻断 |

## 隐私

默认 runs.jsonl 不保存 Prompt、Response、消息正文、工具参数/结果、Workspace 路径/
正文、Telemetry 属性、异常正文或 Judge rationale。只有显式 include-content 才保存
这些内容；.eval 已被 Git 忽略。

## 能力边界

Fake Provider 的输出由脚本决定，因此当前分数代表编排、持久化和安全契约，不代表
开放式模型智能，也不能证明某个自然语言 Prompt 的真实回答质量更高。若后续需要
语义评测，应新增明确的本地模型适配器，不能把 Fake 分数解释为模型质量。

## 验证结果

- Evals 单测：18 passed。
- 内置门禁：5 suites、10 baseline/candidate observations，candidate gate PASS。
- 资源装配对照：candidate 相对弱 baseline 为 +100.0 percentage points。
- 默认 runs JSONL 泄漏扫描：测试 marker、response、tool arguments、Workspace paths、
  Telemetry attributes 和 Judge rationale 均未出现。
- 全仓 Ruff：PASS。
- strict Mypy：290 source files / 0 issues。
- Backend：2205 passed、7 skipped、9 deselected，coverage 76.42%。
- Frontend：187/187，typecheck、ESLint、production build PASS。
- Wiki Parser Worker：Ruff PASS，strict Mypy 16 source files / 0 issues。

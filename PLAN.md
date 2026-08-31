# Project Plan

The original Step 1–21 implementation plan has been completed and archived:

- [Archived Step 1–21 Plan](docs/archive/legacy-plans/PLAN_STEP_1_21.md)

The original Web Claude P0 plan is also archived:

- [Archived Web Claude Plan](docs/archive/legacy-plans/WEB_CLAUDE_PLAN_ORIGINAL.md)

> Both archived documents are retained for historical reference and are no longer the source of truth.

## Current project documents

- [Current Status](STATUS.md) — HEAD, latest tag, test baseline, frozen phases, blockers
- [Roadmap](ROADMAP.md) — 已完成主线、当前 P2 候选与明确延期项
- [Changelog](CHANGELOG.md) — released versions
- [Current TODO](TODO.md) — 当前 P0/P1 完成清单、P2 候选与延期边界
- [Architecture docs](docs/architecture/) — runtime, web request lifecycle, persistence, regenerate model
- [API reference](docs/api/web-api.md)
- [Testing guide](docs/guides/web-testing.md)

> 当前代码位于 `0.0.29` 发布基线之后：三类意图路由与 Planner–Executor–Verifier Plan Mode 已完成；与上游 pi 的逐模块对齐已完成第 1 项 `ai` 和第 2 项 `agent`。Session Workspace、自动 Memory、Managed Coding Sandbox、LLM Wiki、Approval、Context Budget/Compaction 等既有主线保持有效。
>
> 当前后续顺序是继续逐项审查 `coding-agent`、`session-backends/sqlite-node`、`telemetry`。实施事实和最新门禁数字见 [STATUS.md](STATUS.md)，对齐证据见 [pi-ai parity report](docs/validation/pi-ai-parity-2026-08-31.md) 与 [pi-agent parity report](docs/validation/pi-agent-parity-2026-08-31.md)，后续优先级与明确延期项见 [ROADMAP.md](ROADMAP.md)。

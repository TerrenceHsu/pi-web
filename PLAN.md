# Project Plan

The original Step 1–21 implementation plan has been completed and archived:

- [Archived Step 1–21 Plan](docs/archive/legacy-plans/PLAN_STEP_1_21.md)

The original Web Claude P0 plan is also archived:

- [Archived Web Claude Plan](docs/archive/legacy-plans/WEB_CLAUDE_PLAN_ORIGINAL.md)

> Both archived documents are retained for historical reference and are no longer the source of truth.

## Current project documents

- [Current Status](STATUS.md) — HEAD, latest tag, test baseline, frozen phases, blockers
- [Roadmap](ROADMAP.md) — **P1-E Multi-Provider Switching（M1 / M2 / M3 milestone）**，P1-F，P2 candidates
- [Changelog](CHANGELOG.md) — released versions
- [Current TODO](TODO.md) — current phase execution checklist（M1-0~M1-7 + M2 + M3）
- [Architecture docs](docs/architecture/) — runtime, web request lifecycle, persistence, regenerate model
- [API reference](docs/api/web-api.md)
- [Testing guide](docs/guides/web-testing.md)

> 当前本地基线主线：**P0-AGENT-RUNTIME Upstream Contract Alignment** 已实施；底层 P2-CHECKPOINTER、P2-AUTH、P2-SESSION-WORKSPACE 与 P1-E Provider Runtime 保持有效。
> 当前首个命令 `/checkpointer` 把本 Session 对话累计总结到 `Memory.md`，文件提交后清空消息，失败保留并回滚；后续请求自动加载该记忆。
> 实施状态与验证基线见 [STATUS.md](STATUS.md)，后续优先级见 [ROADMAP.md](ROADMAP.md)。

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

> 当前主线：**P1-E Multi-Provider Switching**——M1 Runtime / M2 Frontend / M3 Unified Freeze。
> Backend Foundation（P1-E1 Credentials + P1-E2 Profile/Binding）已 ✅ FROZEN @ `cad7ca7`，不单独 merge / tag。
> 详见 [ROADMAP.md](ROADMAP.md) § P1-E 与 [docs/design/p1-e2-provider-profiles.md](docs/design/p1-e2-provider-profiles.md) §19 Pivot 附录。

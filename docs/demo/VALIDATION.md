# 发布与演示准备记录 / Publication and demo preparation

日期 / Date: 2026-09-07. Application baseline before documentation changes: `d913234`.
Target: `TerrenceHsu/pi-web`, **private**. Publishing source does not deploy the application.

## 本次变更 / Changes

- GitHub-facing Chinese README and English README, with portable setup commands.
- Chinese/English walkthroughs, sample prompts, expected values and recording scripts.
- Synthetic CSV, Markdown and single-file HTML fixtures; no user/customer inputs.
- Real chart exported from the application's fixed analysis operation.
- Corrected obsolete root guidance and the Bash runtime's stage-1-only description.
- Retained existing copyright, main license, Worker notices and historical evidence.

## 已执行 / Executed

| Check / 检查 | Result / 结果 |
| --- | --- |
| Demo fixture unit tests | 3 passed; Ruff passed |
| Local document links and whitespace | Checked for the changed documentation |
| One-key gate entry | `--dry-run` succeeded; the full suite was not rerun for this documentation change |
| Separate Web demo | New data root, localhost port 8135, memory-only credentials, Docker disabled |
| Login | Bootstrap test admin; no real account/password changed |
| File upload | Synthetic CSV/Markdown appeared under logical `upload/**` and were read-only; the CSV SHA-256 still matched the source fixture after analysis/publication |
| Streaming | Observed `Hello from delayed fake backend`; explicitly simulated, no real LLM |
| Fixed analysis | Real local computation: 6 input rows, North 2600, South 3400 |
| Save and refresh | `chart.png`, `manifest.json`, `report.md`, `result.csv` persisted under `artifacts/analysis/<run-id>/`; messages and original uploads survived refresh |

Changing the demo port also requires setting its `EXTRA_UI_ORIGINS`. Initial verification
correctly rejected an unlisted origin; both walkthroughs were corrected, and the
isolated demo was restarted with the matching origin. No Origin checks were relaxed.

The included chart has SHA-256:
`9746b9bd05f303c112fc419e22099e3119700392ef6b168cb04f48dc1efbd4ec`.
Only that synthetic PNG was copied into documentation, not its session database or metadata.
The dedicated demo server was stopped after validation; its ignored test data was retained.

## 发布检查 / Pre-publication checks

- A read-only heuristic scan covered 2,815 reachable Git blob versions, 66,119,407
  logical bytes. No blob exceeded the 90 MiB review threshold.
- 46 candidate matches were explicit synthetic test markers/placeholder credentials;
  no unclassified match remained. The scan did not print credential values.
- Tracked/historical path checks found no `.env` secrets, business SQLite or private
  key files; the tracked `.env.e2e` contains only the frontend test-hook switch.
- `.env`, business data, uploads, login state, portable publishing tools and the demo
  data root remain ignored. They must not be force-added to Git.
- Git history and author metadata are preserved, not rewritten. The repository is private.

这些是有边界的启发式检查，不是“绝对无敏感信息”的保证。未来改为公开前需要再次审查
提交历史、作者信息、历史日志与第三方许可；本次没有自动公开仓库。

These are bounded heuristic checks, not a guarantee of absence of sensitive data.
Review history, author metadata, historical logs and third-party terms again before
any future public release. This publication does not make the repository public.

## 未执行 / Not executed in this publication pass

- No real Provider calls, real Wiki page proposal/publishing, Docker tasks or MinerU parsing.
- No full narrated video was recorded. The bilingual storyboard is a reproducible script.
- No dependency reinstall on a clean machine, full local gate, or independent macOS acceptance.
- Existing engineering results remain the dated baseline in [STATUS](../../STATUS.md);
  the three new fixture tests are not added to that historical full-suite count.
- A configured GitHub Actions workflow is not itself evidence of a successful remote run.

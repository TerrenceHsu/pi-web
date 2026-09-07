# 发布与演示准备记录 / Publication and demo preparation

日期 / Date: 2026-09-07. Application baseline before documentation changes: `d913234`.
Target: `TerrenceHsu/pi-web`, initially **private**, subsequently **public** at the user's explicit request.
Publishing source does not deploy the application. See the public-visibility update below.

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
- Git history and author metadata were preserved, not rewritten. The initial upload was private.

这些是有边界的启发式检查，不是“绝对无敏感信息”的保证。首次上传没有自动公开；
后续明确授权公开时的复核记录见下节。

These are bounded heuristic checks, not a guarantee of absence of sensitive data.
The initial upload did not make the repository public. The subsequent authorized
visibility change and recheck are recorded below.

## 公开状态更新 / Public visibility update

- On 2026-09-07, the user explicitly requested public publication. GitHub confirmed
  `isPrivate=false` for `TerrenceHsu/pi-web`; the repository name and history were retained.
- The repeated heuristic scan covered 2,827 reachable blob versions, 66,210,967
  logical bytes: 46 explicit synthetic test matches, no unclassified matches,
  and no blobs at or above the 90 MiB review threshold.
- Existing commits, author metadata and Actions logs are now within the public scope.
  Business data, credentials, local environments and temporary files remain excluded.
- Main/Worker licenses and third-party notices are retained. No Worker image, model
  weights, new release tag, hosted service or public deployment was published.
- The first remote [CI run](https://github.com/TerrenceHsu/pi-web/actions/runs/34095561462)
  on `7ad03ea` failed: frontend passed, Mypy and Linux/Windows platform prechecks
  failed, and downstream backend/browser stages were skipped. Public visibility
  does not imply a stable release or a successful remote gate. This initial failure is retained as history;
  the subsequent repair and successful acceptance are recorded below.

## CI 后续修复 / Subsequent CI repair

- The complete [repair run 34108827664](https://github.com/TerrenceHsu/pi-web/actions/runs/34108827664)
  on `32b3d53` passed all four jobs: backend 2,684 passed (78.50% coverage), Worker 11,
  frontend 241, Linux/Windows Chromium 27 each with zero retries and production builds restored,
  plus both-platform strict Mypy and the offline Evals gate (6 suites / 10 pairs).
- These are subsequent repair results, not additional tests in the original publication pass.
  The [repair report](../validation/github-ci-repair-2026-09-07.md) preserves the intermediate failures,
  fixes, local test limits and final same-run evidence. No live Provider, MinerU or Docker acceptance
  is implied by this offline CI result.

## 未执行 / Not executed in this publication pass

- No real Provider calls, real Wiki page proposal/publishing, Docker tasks or MinerU parsing.
- No full narrated video was recorded. The bilingual storyboard is a reproducible script.
- No dependency reinstall on a clean machine, full local gate, or independent macOS acceptance.
- Existing engineering results remain the dated baseline in [STATUS](../../STATUS.md);
  the three new fixture tests are not added to that historical full-suite count.
- A configured GitHub Actions workflow is not itself evidence of a successful remote run.

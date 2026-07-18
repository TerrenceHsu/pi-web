# P1-E1-4B E2E Stability Baseline

> Status: **PASS / FROZEN**
> Branch: `feat/p1-e1-secure-credentials`
> Commit under test: `76f096a` — `test(credentials): validate credential API security boundaries`

## 1. Purpose

P1-E1-4B Credential REST API functional and security boundaries passed review. The only remaining gate before final freeze is E2E stability — three consecutive default-parallel runs plus one `--workers=1` run, all of which must:

- Exit code 0
- Zero flaky on any Credential API–related test
- Zero new flaky tests beyond the historical Stop button baseline

This document records the four-run gate result, fixes the baseline, and authorizes the freeze.

## 2. Test Environment

- Platform: Windows 11 Pro 10.0.26200
- Conda env: `pipy` (Python 3.12, `D:\miniconda\envs\pipy\python.exe`)
- Frontend build: `npm run build:e2e` in `src/pi_agent_core_py/web/frontend` immediately before the runs (produces `static/assets/index-DOhRrd3t.js`, `static/assets/index-CX-tvMAl.css`)
- E2E dir: `tests/e2e/` (independent npm environment)
- Playwright: `^1.48.0`, chromium
- webServer: spawned automatically by Playwright, conda `pipy` python serving FastAPI on `http://127.0.0.1:8000`
- Temp DB/uploads: per-run isolated under `%TEMP%/pi-e2e-*`

## 3. Gate Procedure

```bash
cd tests/e2e
# From the project's frontend dir, first:
#   npm run build:e2e
npx playwright test --reporter=line        # run 1
npx playwright test --reporter=line        # run 2
npx playwright test --reporter=line        # run 3
npx playwright test --workers=1 --reporter=line
```

## 4. Results

| # | Mode                | Total | Passed | Failed | Flaky | Exit | Wall time |
|---|---------------------|-------|--------|--------|-------|------|-----------|
| 1 | default (1 worker*) | 37    | 37     | 0      | 0     | 0    | 1.9 m     |
| 2 | default (1 worker*) | 37    | 37     | 0      | 0     | 0    | 1.8 m     |
| 3 | default (1 worker*) | 37    | 37     | 0      | 0     | 0    | 1.9 m     |
| 4 | `--workers=1`       | 37    | 37     | 0      | 0     | 0    | 1.9 m     |

\* Playwright auto-selected 1 worker for the default runs on this Windows host; this is the same effective parallelism as the explicit `--workers=1` invocation.

All four invocations ended with `37 passed` and exit code 0. No test was tagged `flaky` by Playwright in any run. No retries were triggered.

## 5. Flaky / Waiver

**No flaky tests were observed in this gate run.** The previously documented Stop-button flake (`web-claude-smoke.spec.ts:350:3 › Smoke 8: Stop button › 发送后 Send 变 Stop；Stop 可见且可点`) did not reproduce across four back-to-back executions.

Trace path: n/a — no failure, no trace artifact emitted beyond the standard `test-results/` and `playwright-report/` directories.

The Stop-button baseline waiver described in the freeze criteria is therefore **not exercised in this run**. It remains on file as a known-historical flake and should be re-checked during P1-E1-5 regression; if it resurfaces there, the existing baseline waiver policy applies (see §7).

## 6. Credential-Related E2E Coverage

The Credential REST API surface itself does not yet have dedicated Playwright E2E specs (frontend Credential Manager is explicitly deferred past P1-E1). Stability of the Credential code path is therefore asserted indirectly via the existing E2E suite, which exercises the full web stack — FastAPI lifespan, Credential runtime composition, body-limit middleware, custom API route class, TrustedHost, and the FakeClient provider — across:

- `async-stream-reconnect.spec.ts` (7 tests)
- `event-dedup.spec.ts` (4 tests)
- `extension-persistence.spec.ts` (5 tests, including secret marker safety scan)
- `mcp-tool-lifecycle.spec.ts` (2 tests)
- `regenerate.spec.ts` (8 tests)
- `web-claude-smoke.spec.ts` (11 tests, including Stop button)

All 37 tests, across all 4 runs, terminated cleanly. No timeout, no WS reconnect failure, no lifespan-startup race, no body-limit regression.

## 7. Stop-Button Historical Baseline

For future reference, the Stop-button flake is characterized as follows:

- Test id: `web-claude-smoke.spec.ts:350 › Smoke 8: Stop button › 发送后 Send 变 Stop；Stop 可见且可点`
- Symptom: intermittent failure to observe the Stop button within the default `expect` polling window during the very first test run after a fresh Playwright boot
- First observed: pre-P1-E1-4B (inherited from the prior web-claude baseline; see `memory/project_p1_progress.md`)
- Reproduction: irregular; tends to correlate with cold-cache startup, not with any Credential code path
- Waiver policy: a single Stop-button flake per gate cycle is non-blocking; **any** additional flake, or any flake on a Credential-related test, blocks the freeze

This gate run did not invoke the waiver.

## 8. Freeze Authorization

All gate conditions satisfied:

- [x] All four commands exited 0
- [x] Zero flaky on Credential API–adjacent tests
- [x] Zero new flaky beyond the documented Stop-button baseline
- [x] Zero flaky overall in this run (Stop-button baseline did not reproduce)
- [x] Working tree clean after the gate (`git status --short` empty)
- [x] `git diff --check` clean

**P1-E1-4B Credential REST API: PASS / FROZEN.**

## 9. Next Step

P1-E1-5 Security Freeze is authorized to enter DESIGN. Initial deliverable is `docs/security/p1-e1-security-freeze.md` with status `AUDIT IN PROGRESS`, covering the six audit axes (secret data flow, persistence egress, HTTP attack surface, error/logging, concurrency/compensation, configuration matrix). Implementation changes are forbidden during E1-5 except as minimal hardening fixes flagged by the audit.

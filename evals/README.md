# Local Coding-Agent Evals

This private package compares coding-agent product configurations on the same
deterministic cases. It is separate from the runtime wheel and from pytest:

- pytest verifies implementation contracts.
- Telemetry observes real product runs.
- evals compares baseline and candidate behavior, resource use and failures.

## Safety boundary

The agent-run suites use FakeProviderAdapter exclusively; context safety also
exercises pure projections and local SQLite directly. They do not load the
repository .env file, create HTTP clients, start MCP subprocesses, or call Web
routes. Each observation receives its own temporary Workspace and SQLite
database.

Default artifacts omit prompts, responses, message bodies, tool arguments and
results, Workspace paths and text, Telemetry attributes, exception messages,
and Judge rationale. Full content is written only with --include-content.

## Run

From the repository root with the pipy environment:

    $env:PYTHONPATH = "src"
    D:\miniconda\envs\pipy\python.exe -m evals --gate

List or select suites:

    D:\miniconda\envs\pipy\python.exe -m evals --list
    D:\miniconda\envs\pipy\python.exe -m evals --suite session-reload --gate

Every run writes manifest.json, runs.jsonl, summary.json and summary.md below a
new .eval run directory. Use --artifacts-dir to select an explicit directory.

## Built-in suites

- local-smoke: complete product Session returns a terminal assistant response.
- tool-lifecycle: tool trace, usage and temporary Workspace artifact.
- resource-composition: Provider, Skill, MCP, Workspace and Prompt assembly.
- session-reload: durable messages survive Runtime Session reconstruction.
- telemetry-safety: required spans exist without prompt or suffix content.
- context-compaction: source/Memory preservation, branch invalidation, failed
  attempts retained, invalid-summary rejection, and current-turn tool readback.
  This deterministic suite does not measure model summary quality or Web retry
  scheduling; those remain separate quality and orchestration checks. Its
  input/total token metric is the shared context estimator, not Provider usage
  or a claim of measured billing savings.

The resource-composition baseline intentionally lacks product resources, so
its lower score demonstrates paired pass-rate lift. The gate applies to
candidates and infrastructure integrity, not to intentionally weaker
baselines.

## Capability limit

Scripted providers can evaluate orchestration, isolation, persistence and
safety, but they cannot measure open-ended model intelligence or determine
whether one natural-language prompt is semantically better. A future local
model adapter can implement that separately without weakening the offline
default.

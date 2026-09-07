# pi-web

[中文](README.md) | [English](README.en.md) · [Demo](docs/demo/README.en.md) · [Status](STATUS.md)

A local Web Agent workbench combining streaming chat, independent workspaces,
reviewable code execution, data analysis, and a page-based knowledge wiki.

The repository is named `pi-web`; the Python distribution and import remain
`pi-agent-core-py` and `pi_agent_core_py`. It started as a Python port of
`@earendil-works/pi-agent-core` and adds the Web application layer.

> **Localhost only.** This is a local development tool, not an internet-facing
> SaaS deployment. Keep it bound to `127.0.0.1`. Current facts and limitations
> live in [STATUS.md](STATUS.md); pending work lives in [TODO.md](TODO.md).

## What you can do

| Area | Implemented workflow |
| --- | --- |
| Chat | Streaming text/tool events, Stop, latest-response Regenerate, Markdown export, refresh recovery |
| Workspaces | Independent runtime per Web Session, uploads, previews, downloads, revision-aware edits |
| Providers | GLM/Anthropic-compatible and OpenAI-compatible adapters; GLM, Qwen and Kimi profiles in the UI |
| MCP and Skills | Account-level catalogs with per-Workspace selection; stdio and Streamable HTTP; built-in DDGS |
| Memory | SQLite history search, source-linked structured memory, session `Memory.md`, context compaction |
| Coding / Plan | Planner–Executor–Verifier, task-scoped execution approval, fixed validation, artifact review and publication |
| Bash | Optional local Docker execution; exact-script approval or approved Coding/Plan Executor reuse |
| Data analysis | Optional fixed operations, charts and explicit saving; separately approved Agent-written Python |
| Knowledge | PDF/HTML sources → proposals → approved pages; FTS5/BM25 search, relations and multiple conversations |
| Observability | Admin-only content-free Telemetry and execution maintenance; offline paired Evals |

Sessions have separate runtimes and resources. Different workspaces can run
concurrently; operations within a session/workspace are serialized or guarded
by execution locks. This does not imply unrestricted parallel shell access.

## Quick start

Requirements: Git, Python 3.11+ (3.12 is the validated local baseline), and Node.js
20+ with npm. Windows is the fully exercised local platform. Linux/macOS commands
are provided, but Keyring, Docker and GPU behavior require platform-specific checks.
Dependencies need internet access to install. Real model calls may incur Provider fees.

### 1. Install and build

The repository is public; cloning the source does not require a GitHub login.

Windows PowerShell:

```powershell
git clone https://github.com/TerrenceHsu/pi-web.git
Set-Location pi-web
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install uv==0.11.26
python -m uv sync --frozen --extra dev --extra web --extra data-analysis
npm --prefix src/pi_agent_core_py/web/frontend ci
npm --prefix src/pi_agent_core_py/web/frontend run build
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` instead of
`python` below; no system execution-policy change is necessary.

Linux/macOS:

```bash
git clone https://github.com/TerrenceHsu/pi-web.git
cd pi-web
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install uv==0.11.26
python -m uv sync --frozen --extra dev --extra web --extra data-analysis
npm --prefix src/pi_agent_core_py/web/frontend ci
npm --prefix src/pi_agent_core_py/web/frontend run build
```

The lock file pins dependency resolution. The `data-analysis` extra installs fixed
analysis dependencies but does not enable the tool or create its separate Python runtime.

### 2. Start the Web application

Run from the repository root, in the environment prepared above:

```powershell
$env:PYTHONPATH = "src"
python scripts/dev_web_app.py
```

On Linux/macOS, use `PYTHONPATH=src python scripts/dev_web_app.py`.
Open `http://127.0.0.1:8000`. The backend serves the built frontend. Application
data defaults to `.pi-agent-data/`; `PI_AGENT_DATA_DIR` selects a different root.

The launcher checks persistent OS Keyring access before listening. On Windows,
start it as the interactive desktop user so Credential Manager is available.
If you explicitly accept that API keys will not survive a restart, select
memory-only storage before starting:

```powershell
$env:PI_AGENT_SECRET_BACKEND = "memory"
python scripts/dev_web_app.py
```

Linux/macOS: `PI_AGENT_SECRET_BACKEND=memory PYTHONPATH=src python scripts/dev_web_app.py`.
There is no silent plaintext-SQLite fallback. See the
[Keyring troubleshooting guide](docs/guides/windows-keyring-preflight-smoke.md) (Chinese).

### 3. Sign in and configure a model

An empty authentication database creates the local bootstrap account:

```text
Username: admin
Password: 123456
```

1. Open **Password**, replace the initial password with 12–128 characters, and sign in again.
2. Open **Providers**, create a GLM/Qwen/Kimi profile, and enter its model ID and API key.
3. Select that profile as the current Session binding.
4. Create or select a chat and send a message.

Without a configured profile, the launcher uses a simulated streaming client that
replies `Hello from delayed fake backend`. This is a UI demonstration, **not** a
real model response. No Users-management UI, OAuth or forced first-login password
change is provided. Default credentials must never be used for public hosting.

### 4. Optional frontend development

Keep the backend running and open another terminal at the repository root:

```powershell
npm --prefix src/pi_agent_core_py/web/frontend run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` and `/ws/events` to port 8000.
For normal use, the production build at port 8000 is sufficient.

## Everyday use

### Files, sessions and memory

- **+ New chat** creates a session; its URL is `/chat/{session_id}`.
- Drop files into chat or Workspace. Originals go to logical `upload/**`, created
  on first upload, with duplicate-name protection. They are read-only to the Agent.
- Working code belongs under `scripts/**`; non-code deliverables use `artifacts/**`.
  Uploaded files are not automatically granted execution permission.
- Edit `AGENT.md` and `Memory.md` through the revision-aware editor. They are scoped
  to this Session and do not override platform safety rules.
- Automatic context management warns at 70%, summarizes at 80%, targets 60%, and
  checks a hard input budget before each call. It preserves the original history.
- `/checkpointer` is different: it summarizes into `Memory.md` and clears the
  current lane after successful publication. Do not use it merely to preview a summary.
- Refresh can recover an active request only while the backend process still owns it.
  Restart retains stored history but does not replay pending execution or approval.

### Account catalogs and Workspace extensions

Configure MCP servers and upload/enable Skills at account level, then select the
ones needed by each Workspace under **extensions**. Each request freezes its own
Provider/Skill/MCP/Workspace/Prompt snapshot.

MCP supports stdio and Streamable HTTP. HTTP authentication stores environment-variable
references, not secret header values. The built-in `ddgs` option cannot be deleted;
external search still requires network access. Only enable trusted Skills and servers:
instructions and tool output are not authority to bypass approvals.

### Fixed analysis and Agent-written Python

Enable **Data Analysis** under Workspace → extensions → Tools. Analyze CSV, TSV,
XLSX or Parquet in the **analysis** tab, review tables/charts, and click Save to
publish results to `artifacts/analysis/<run-id>/`. Originals are unchanged.
Direct analysis-tab operations do not need an LLM. Chat-based analysis can send
table previews and summaries to the configured model Provider.

For Agent-written Python, explicitly prepare the separate environment:

```powershell
python scripts/setup_analysis_python.py
python scripts/setup_analysis_python.py --check
```

Restart the backend and enable **Python Data Analysis**. Each execution presents
the complete code and inputs for approval. **Dependency isolation is not a security
sandbox: approved Python runs with the local user's permissions.** It does not
automatically obtain Bash access. Only approve code you trust.

### Coding, Plan and Bash

Bash is off by default. An operator must prepare and verify an immutable local
Docker image, configure the absolute CLI path and image ID, restart the backend,
and opt in within the intended Workspace. See the
[Bash runtime guide](docker/bash-runtime/README.md).

Execution uses a restricted, offline copy, not a writable host Workspace mount.
Task/script approval permits execution; it is **not publication approval**. Review
the frozen artifact, changed files and validation before separately approving write-back.
Planner, Verifier, read-only and Knowledge roles do not receive Bash tools.
Managed E2B is a separate optional backend and is not required for the local demo.

### Knowledge wiki

Open **Knowledge**, create a Space, upload a PDF or single-file HTML source, and
review parsing results. HTML parsing is local and makes no network requests.
PDF requires the independent [MinerU Worker](workers/wiki_parser_worker/README.md).
The Web choices are `pipeline`, `gpu-medium`, and `gpu-high`; the GPU profiles require CUDA.

With a real Provider, ask for a summary or page changes. Proposals become a Change Set
and are published only after review. Search uses current, approved pages with SQLite
FTS5/BM25 and relations, not a chunk/embedding/vector-store pipeline.

**Quality caveat:** the new Worker completed three-profile synthetic parsing and
cancel/restart checks, but CPU table extraction still has errors. Representative
Chinese/complex-document quality is not accepted; `runtime_ready=false` remains.
An uploaded image is not Workspace OCR, and video-link parsing is not implemented.

### Admin and maintenance

Admin's **Telemetry** view shows requests, latency, tokens, providers, tools, error
codes and execution/cleanup status without collecting prompt, response or tool bodies.
It is not a complete privacy boundary: chat data still goes to its selected Provider.

Stop the Web server and any Worker using the data root before offline backup/restore:

```powershell
python scripts/maintain_data.py backup --source /ABSOLUTE/DATA --destination /NEW/BACKUP --offline
python scripts/maintain_data.py verify --source /NEW/BACKUP
python scripts/maintain_data.py restore --source /NEW/BACKUP --destination /NEW/RESTORE --offline
```

Replace placeholders with explicit paths (drive-qualified on Windows). Destinations
must be new. Backups exclude OS Keyring, Docker images, Python environments and
externally configured data/model directories. They contain private data: do not commit
them. Wiki rebuilds retain a legacy archive but are not lossless in-place migrations.
See [maintenance details](docs/guides/data-maintenance.md) (Chinese).

## Demo and verification

Follow the [five-minute walkthrough](docs/demo/README.en.md), using only the
[synthetic demo inputs](examples/demo/). It includes expected totals, model-enabled
prompts, approval boundaries, and a recording storyboard.

Prepare the remaining gate dependencies explicitly. The E2B SDK is needed for
offline configuration safety tests; installing it does not enable cloud calls:

```powershell
python -m uv sync --frozen --extra dev --extra web --extra data-analysis --extra sandbox-e2b
python scripts/setup_analysis_python.py
npm --prefix tests/e2e ci
npm --prefix tests/e2e exec -- playwright install chromium
python scripts/run_local_gates.py --dry-run
python scripts/run_local_gates.py
```

The gate covers locked dependencies, lint, types, backend branch coverage, Worker
tests, offline Evals, frontend tests/build and Chromium with zero retries. It installs
nothing itself. Docker and real MinerU acceptance are separate opt-ins. Do not run
multiple gates against the same test state. More: [gate guide](docs/guides/local-gates.md).

The **2026-09-07 local baseline**, not a remote CI claim: 2674 backend tests passed,
78.36% coverage including branch statistics, 11 Worker tests, 236 frontend tests,
27 Chromium tests, and 6 Evals suites / 10 pairs. See
[the dated report](docs/validation/engineering-closure-2026-09-07.md).
Fake-provider Evals demonstrate orchestration/isolation, not real-model intelligence.

## Architecture and persistence

```text
src/pi_agent_core_py/   ai, agent, SQLite sessions, MCP, Telemetry, FastAPI and Vue
src/coding_agent_app/  product assembly, tasks, analysis and continuity
src/agent_workspace/   Workspace storage and permissions
src/coding_sandbox/    execution, artifact freezing and approved publication
src/wiki_parser/       external parser contracts and client
workers/              independent MinerU runtime, notices and source materials
evals/                offline paired application-level evaluations
examples/demo/        synthetic inputs for the walkthrough
```

The default `.pi-agent-data/` root contains `auth.sqlite`, `telemetry.sqlite`, and
per-user `workspace.sqlite`, uploads, and `knowledge/wiki.db` plus source/page files.
Workspace paths are logical views; underlying bytes are stored with file-ID isolation.
Never commit this data root, API keys, login cookies, real uploads or runtime backups.

See [architecture](docs/architecture/), [API reference](docs/api/web-api.md),
[roadmap](ROADMAP.md), and [changelog](CHANGELOG.md). Most detailed internal design
and historical validation documents are currently in Chinese.

## License and attribution

The main project is [MIT licensed](LICENSE). Upstream reference:
[pi](https://github.com/earendil-works/pi). Existing notices and copyright statements
are retained; this project does not claim to be the official upstream Web product.

The isolated MinerU Worker has its own MIT code and includes third-party notices,
dependency locks, source metadata and an SPDX SBOM. MinerU, model weights and other
dependencies have their own terms; the main project's MIT license does not replace
them. Inspect **About & Source** and the [Worker license](workers/wiki_parser_worker/MINERU_LICENSE.md)
before redistributing a Worker image or deploying a third-party service.

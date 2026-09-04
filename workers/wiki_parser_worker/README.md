# pi Wiki MinerU Parser Worker

This isolated persistent Worker is the only component that imports the MinerU runtime. The Web
application exchanges immutable PDF jobs and Contract v2 artifacts through a bounded file queue;
the Worker runs with no network during a job.

## Product profiles

| Web value | MinerU backend | effort | Runtime |
| --- | --- | --- | --- |
| `pipeline` | `pipeline` | n/a | CPU or GPU |
| `gpu-medium` | `hybrid-engine` | `medium` | CUDA required |
| `gpu-high` | `hybrid-engine` | `high` | CUDA required; image/chart analysis enabled |

These are fixed profiles. API callers cannot pass arbitrary MinerU command-line options, model
paths, URLs, or thresholds.

## Run locally with Docker

Set `WIKI_PARSER_EXCHANGE_DIR` to the same absolute directory configured through
`PI_AGENT_WIKI_PARSER_EXCHANGE_DIR` for the Web backend.

CPU/pipeline Worker:

```powershell
docker compose -f workers/wiki_parser_worker/compose.yaml up --build
```

GPU-capable Worker:

```powershell
docker compose -f workers/wiki_parser_worker/compose.yaml `
  -f workers/wiki_parser_worker/compose.gpu.yaml up --build
```

The image build installs MinerU 3.4.5 and downloads both pipeline and VLM model sets. Runtime uses
`MINERU_MODEL_SOURCE=local`, Hugging Face offline flags, `network_mode: none`, a read-only root
filesystem, and a non-root identity. The GPU overlay exposes the host NVIDIA runtime; without it,
GPU presets fail closed while `pipeline` remains usable.

MinerU uses `LicenseRef-MinerU-Open-Source-License`. See `MINERU_LICENSE.md` and the upstream
license before distribution or third-party service deployment. The product UI visibly attributes
all three parser profiles to MinerU.

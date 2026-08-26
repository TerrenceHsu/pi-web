# pi Wiki Parser Worker

This directory is the independent Corresponding Source package for the LLM Wiki PDF parser
Worker. It is licensed separately from the MIT main application under
**GNU AGPL-3.0-only**. See [`LICENSE`](LICENSE), [`NOTICE.md`](NOTICE.md), and
[`SOURCE_OFFER.md`](SOURCE_OFFER.md).

Current gate status: **runtime verified** on 2026-08-26 with Docker 29.7.2 on Linux/amd64.
The image source bundle, notices, isolation settings, representative PDFs, quality fallback and
running-cancel recovery all passed the documented gate.

The package now contains dependency-lazy PyMuPDF preflight, deterministic routing,
PyMuPDF4LLM fast/no-OCR, startup-initialized Docling standard/OCR adapters, embedded-image
extraction and a versioned quality evaluator. `runtime-manifest.json` pins the critical
Linux/amd64 wheels, upstream source identities, layout/table/OCR model files and licenses;
`config/routing-quality-v1.json` carries every product threshold behind a canonical SHA-256.

The source wheel does **not** vendor or import the concrete parser packages at module import time.
The production image uses a hash-locked Linux/amd64 CPython 3.12 dependency set, materializes the
three selected Artifex AGPL source archives by pinned SHA, downloads hash/size-pinned models only
during image build, and runs one persistent parser child behind a durable file-queue supervisor.
`runtime_ready=true` records that the source bundle/image build, network-isolation inspection and
representative real-PDF smoke were verified on an actual OCI runtime. A later release image must
repeat the same gate; this flag does not turn an arbitrary deployment into a trusted runtime.

## Isolation boundary

- The main MIT application does not package or import `wiki_parser_worker`.
- The Worker will consume only bounded, hash-pinned job input through the provider protocol.
- The Worker must not receive application credentials, `wiki.db`, Wiki pages, Session Workspace,
  or another job's source files.
- Runtime network access is disabled; dependency/model acquisition is a separate administrator
  build operation.
- The original PDF remains the only parser input. Docling never receives PyMuPDF4LLM Markdown.

## Build and run the OCI Worker

Image build requires network access to the locked package indexes and pinned model revisions.
Runtime does not: `compose.yaml` sets `network_mode: none`, a read-only root filesystem,
non-root UID/GID 65532, no capabilities, no-new-privileges, bounded PIDs/CPU/memory and one
explicit writable exchange mount.

`scripts/fetch_runtime_sources.py` writes the verified AGPL dependency archives and a canonical
evidence manifest to `/usr/share/pi-wiki-parser-worker/upstream-sources` in the image. A release
operator must expose that generated directory together with this Worker source offer.

From this directory in PowerShell, keep the mutable exchange outside the Worker source tree so the
fail-closed Corresponding Source inventory remains exact:

```powershell
New-Item -ItemType Directory -Path ../../.test-tmp/wiki-parser-exchange -Force | Out-Null
$env:WIKI_PARSER_EXCHANGE_DIR = (Resolve-Path ../../.test-tmp/wiki-parser-exchange).Path
docker compose build
docker compose up -d
docker compose ps
```

Never set `WIKI_PARSER_EXCHANGE_DIR` to `.runtime`, `exchange`, or any other descendant of this
Worker directory. The Source Offer intentionally rejects every undeclared runtime file, and the
smoke CLI fails before queue creation if either its exchange or output is inside this tree.

Do not put credentials, the Wiki database, Workspace, or user-facing Wiki pages in the exchange
directory. The application side must configure `PersistentOciParserProvider` with that same
absolute directory and the routing revision/SHA in `runtime-manifest.json`.

From the repository root, run representative smoke cases without printing document content:

```powershell
$env:PYTHONPATH = "src"
python scripts/smoke_wiki_parser_oci.py `
  --exchange-root .test-tmp/wiki-parser-exchange `
  --output-root .test-tmp/wiki-parser-smoke `
  --routing-config workers/wiki_parser_worker/config/routing-quality-v1.json `
  --case digital-fast fast D:/fixtures/digital.pdf `
  --case paper-accurate accurate D:/fixtures/paper.pdf `
  --case simple-auto auto D:/fixtures/simple.pdf `
  --case fallback-auto auto D:/fixtures/fast-quality-fails.pdf `
  --expect-fallback fallback-auto
```

The verified gate uses real digital, paper, complex-layout and scanned PDFs. Its deliberately
constructed fallback fixture validates the orchestration invariant—fast quality rejection is
followed by exactly one Docling attempt against the original PDF—rather than claiming corpus-wide
quality calibration. A successful run leaves only Contract v2 tar artifacts under the output
directory and destroys every queued Worker job.

## Build the source wheel

From this directory, a developer may run `python -m build`. The result contains the Worker-owned
adapter source and compliance assets, but not a runnable PDF service. The root application exposes a deterministic
source archive assembled from `component-manifest.json`; generated caches, wheels and build
output are never included in that offer.

After building, run `python scripts/verify_wheel.py dist/<wheel-name>.whl`. The verifier fails if
the wheel omits a compliance asset, modifies the official AGPL text, disagrees with the completed
OCI readiness gates, or vendors a concrete parser package.

The optional `runtime` dependency group records the audited direct pins. `uv.lock` and
`requirements-linux-x86_64.lock` record the complete resolution; production installation uses
`pip --require-hashes` from the latter inside the digest-pinned base image, not an unconstrained
optional-dependency install.

## License and warranty

Copyright (C) 2026 Pi Python Port

This Worker is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License, version 3 only. It is provided without warranty; see the full
license text for details. The root pi-agent application remains a separate MIT-licensed work.

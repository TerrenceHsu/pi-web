# Worker source bundle

Every file declared by `component-manifest.json` is included in the deterministic Web download
`wiki-parser-worker-0.0.29-source.tar.gz`. The same files are available through **About & Source**
and the `/api/about/wiki-parser-worker/*` endpoints.

The bundle contains the MinerU adapter, file-queue protocol, persistent supervisor, tests, OCI
build, fixed profile configuration, notices, and SBOM. MinerU itself is an external pinned runtime
dependency and is governed by `LicenseRef-MinerU-Open-Source-License`; see `MINERU_LICENSE.md` and
the authoritative upstream repository.

`runtime-manifest.json` intentionally keeps `runtime_ready=false` until the rebuilt image,
pre-downloaded models, GPU profiles, offline execution, cancellation, and representative PDF
corpus have all been verified together. Source-level tests are not a substitute for that release
gate.

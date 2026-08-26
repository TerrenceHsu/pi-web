# Corresponding Source offer

The preferred form for modifying the current `pi-wiki-parser-worker` component is every file
declared by `component-manifest.json`, including its Python source, build metadata, notices,
license, SBOM and this offer.

Users of the pi-agent web application can open **About & Source** and download the deterministic
archive named `wiki-parser-worker-0.0.28-source.tar.gz` at no charge. The equivalent API is:

```text
GET /api/about/wiki-parser-worker/source-offer
GET /api/about/wiki-parser-worker/source
GET /api/about/wiki-parser-worker/license
GET /api/about/wiki-parser-worker/notices
GET /api/about/wiki-parser-worker/sbom
```

The manifest endpoint returns SHA-256 and size evidence for every offered file. The archive is
created with stable ordering and normalized metadata from the same validated byte snapshot. It
requires no password, account-specific decryption key, or proprietary extraction tool.

This repository copy is the complete preferred source for the current Worker-owned service. It
includes the adapters, file-queue protocol, persistent supervisor/child, tests, digest-pinned OCI
build/Compose definitions, full transitive locks, versioned routing/quality configuration, audited
dependency/model identities, and the fail-closed source materializer. Every image build places the
three hash-verified Artifex AGPL archives under the image's `upstream-sources/` directory. The
2026-08-26 OCI gate exported and independently verified those archives, notices and the runtime
manifest from the built image before representative offline PDF smokes were accepted.

Before a parser image is distributed or offered as a network service, its generated upstream source
directory and container-level third-party notices must be mounted into or linked from the same
no-charge source offer, and the exact built image must pass the documented offline smoke.
`runtime-manifest.json` records the transitive-lock, materialization, image and representative-smoke
gates as true. A later distributed image must repeat this verification and publish its generated
upstream source directory; source-level readiness is not a substitute for release evidence.

This engineering gate is not legal advice. Distribution and deployment operators remain
responsible for reviewing their actual delivery and network-service obligations.

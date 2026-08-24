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

This repository copy is the complete Corresponding Source for the current compliance scaffold.
It does not claim to be the source for a future parser image containing dependencies or models.
Before such an image is distributed or offered as a network service, its exact build definition,
lockfiles, adapters, configuration assets, modifications, dependency source references and
installation information must enter the same source offer.

This engineering gate is not legal advice. Distribution and deployment operators remain
responsible for reviewing their actual delivery and network-service obligations.

# pi Wiki Parser Worker

This directory is the independent Corresponding Source package for the LLM Wiki PDF parser
Worker. It is licensed separately from the MIT main application under
**GNU AGPL-3.0-only**. See [`LICENSE`](LICENSE), [`NOTICE.md`](NOTICE.md), and
[`SOURCE_OFFER.md`](SOURCE_OFFER.md).

Current gate status: **compliance scaffold only; runtime not ready**.

The package intentionally contains no PyMuPDF, PyMuPDF4LLM, Docling, Torch, OCR engine, model,
container SDK, main-application import, or parser adapter yet. Exact dependencies, wheels,
models, hashes, offline OCI build, adapters, routing configuration and quality evaluation belong
to the subsequent audited gates. Until those gates complete, this package must not advertise a
ready parser service.

## Isolation boundary

- The main MIT application does not package or import `wiki_parser_worker`.
- The Worker will consume only bounded, hash-pinned job input through the provider protocol.
- The Worker must not receive application credentials, `wiki.db`, Wiki pages, Session Workspace,
  or another job's source files.
- Runtime network access is disabled; dependency/model acquisition is a separate administrator
  build operation.
- The original PDF remains the only parser input. Docling never receives PyMuPDF4LLM Markdown.

## Build the current source package

From this directory, a developer may run `python -m build`. The result is only the compliance
package and does not contain a runnable PDF service. The root application exposes a deterministic
source archive assembled from `component-manifest.json`; generated caches, wheels and build
output are never included in that offer.

After building, run `python scripts/verify_wheel.py dist/<wheel-name>.whl`. The verifier fails if
the wheel omits a compliance asset, modifies the official AGPL text, claims runtime readiness or
contains a concrete parser runtime.

## License and warranty

Copyright (C) 2026 Pi Python Port

This Worker is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License, version 3 only. It is provided without warranty; see the full
license text for details. The root pi-agent application remains a separate MIT-licensed work.

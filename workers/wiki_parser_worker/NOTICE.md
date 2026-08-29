# Notices — pi Wiki Parser Worker

Component: `pi-wiki-parser-worker`
Version: `0.0.29`
License expression: `AGPL-3.0-only`
Copyright: Copyright (C) 2026 Pi Python Port

The complete GNU Affero General Public License version 3 text is provided in `LICENSE`. This
component is distributed without warranty, including without any implied warranty of
merchantability or fitness for a particular purpose.

The root pi-agent application is a separate MIT-licensed work. Bundling this Worker and the main
application in one distribution does not replace, relicense, or hide the Worker license and
Corresponding Source obligations.

## Third-party notices

No third-party runtime package or model is vendored in this source wheel. The following audited
components are selected for the verified offline OCI image; exact artifact/source/model hashes are
machine-readable in `runtime-manifest.json` and package relationships are in `sbom.spdx.json`:

- PyMuPDF4LLM, PyMuPDF and PyMuPDF Layout 1.28.2: dual Artifex commercial or GNU AGPL v3. This
  Worker explicitly selects the AGPL path.
- Docling Slim 2.119.0, Docling Core 2.92.0, Docling Parse 7.15.0 and Docling IBM Models 3.13.2:
  MIT-licensed code.
- Heron layout weights at commit `8f39ad3c...`: Apache-2.0.
- TableFormer accurate weights at tag `v2.3.0` / commit `fc0f2d45...`:
  CDLA-Permissive-2.0 and Apache-2.0 notices apply as declared upstream.
- RapidOCR 3.9.2 and its packaged PP-OCRv6 ONNX assets: Apache-2.0.
- PyTorch/TorchVision CPU-only wheels: BSD-3-Clause; Transformers: Apache-2.0; ONNX Runtime: MIT;
  pypdfium2/PDFium carries its upstream BSD/Apache dependency notices.

The full transitive dependency lock and hash-pinned AGPL source materializer are included. On
2026-08-26 the generated source bundle, container-level notice set, isolation controls and offline
image were verified together with representative real-PDF smokes. These remain mandatory evidence
for every later release image; the current audited manifest therefore records `runtime_ready=true`.

The GNU AGPL license document is published by the Free Software Foundation. Verbatim copies may
be copied and distributed, but the license document itself must not be changed.

# Notices — pi Wiki Parser Worker

Component: `pi-wiki-parser-worker`
Version: `0.0.29`
Worker license: `MIT`

This Worker uses MinerU 3.4.5 to parse PDFs. MinerU declares
`LicenseRef-MinerU-Open-Source-License`: Apache License 2.0 with additional commercial-threshold,
online-service attribution, and termination terms. A bundled notice is in
`MINERU_LICENSE.md`; deployment operators must review the authoritative upstream license. The Web UI labels every
selectable parser profile as MinerU, satisfying the product-attribution requirement for this
application.

MinerU runtime dependencies and model files are installed during the OCI image build. They are not
vendored into the main Web application or this Worker source wheel. `runtime-manifest.json` records
the pinned top-level MinerU version and the product-profile mapping. The complete pinned transitive
dependency inventory is recorded in `uv.lock` and `requirements-linux-x86_64.lock`; regenerate the
SPDX SBOM from those locks before distributing a production image.

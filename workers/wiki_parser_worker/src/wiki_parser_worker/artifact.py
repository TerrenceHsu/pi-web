"""Deterministic Contract v2 artifact construction for the isolated Worker.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkerRuntimeError
from .models import WorkerParsedDocument
from .protocol import (
    ARTIFACT_NAME,
    RECEIPT_NAME,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    path: str
    kind: str
    mime_type: str
    page_number: int | None
    content: bytes
    sha256: str

    def manifest_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "mime_type": self.mime_type,
            "page_number": self.page_number,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    files: tuple[ArtifactContent, ...]
    markdown: ArtifactContent
    pages: tuple[ArtifactContent, ...]
    assets: tuple[ArtifactContent, ...]
    output_size_bytes: int
    output_sha256: str


def _page_boundary(page_number: int, markdown: str) -> bytes:
    if "<!-- llm-wiki-pdf-page:" in markdown:
        raise WorkerRuntimeError("parser_failed")
    normalized = markdown if not markdown or markdown.endswith("\n") else markdown + "\n"
    return f"<!-- llm-wiki-pdf-page:{page_number} -->\n{normalized}".encode()


def prepare_artifact_payload(
    document: WorkerParsedDocument,
    *,
    max_files: int,
    max_image_count: int,
    max_image_bytes: int,
) -> ArtifactPayload:
    page_files: list[ArtifactContent] = []
    document_sections: list[bytes] = []
    for expected_page, page in enumerate(document.pages, start=1):
        if page.page_number != expected_page:
            raise WorkerRuntimeError("parser_failed")
        try:
            content = page.markdown.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WorkerRuntimeError("parser_failed") from error
        page_files.append(
            ArtifactContent(
                path=f"pages/{expected_page:06d}.md",
                kind="page_markdown",
                mime_type="text/markdown",
                page_number=expected_page,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
        document_sections.append(_page_boundary(expected_page, page.markdown))
    combined = b"\n".join(document_sections)
    if not combined:
        raise WorkerRuntimeError("quality_rejected")
    markdown = ArtifactContent(
        path="parsed.md",
        kind="document_markdown",
        mime_type="text/markdown",
        page_number=None,
        content=combined,
        sha256=hashlib.sha256(combined).hexdigest(),
    )

    if len(document.assets) > max_image_count:
        raise WorkerRuntimeError("artifact_limit_exceeded")
    image_bytes = 0
    asset_files: list[ArtifactContent] = []
    seen_paths: set[str] = set()
    for asset in document.assets:
        folded = asset.path.casefold()
        if folded in seen_paths or asset.size_bytes != len(asset.content):
            raise WorkerRuntimeError("parser_failed")
        if hashlib.sha256(asset.content).hexdigest() != asset.sha256:
            raise WorkerRuntimeError("parser_failed")
        seen_paths.add(folded)
        image_bytes += len(asset.content)
        if image_bytes > max_image_bytes:
            raise WorkerRuntimeError("artifact_limit_exceeded")
        asset_files.append(
            ArtifactContent(
                path=asset.path,
                kind="embedded_image",
                mime_type=asset.mime_type,
                page_number=asset.page_number,
                content=asset.content,
                sha256=asset.sha256,
            )
        )

    files = (markdown, *page_files, *sorted(asset_files, key=lambda item: item.path))
    if len(files) + 1 > max_files:
        raise WorkerRuntimeError("artifact_limit_exceeded")
    evidence = [
        {"path": item.path, "sha256": item.sha256, "size_bytes": len(item.content)}
        for item in files
    ]
    output_size = sum(len(item.content) for item in files)
    if output_size <= 0:
        raise WorkerRuntimeError("quality_rejected")
    return ArtifactPayload(
        files=files,
        markdown=markdown,
        pages=tuple(page_files),
        assets=tuple(sorted(asset_files, key=lambda item: item.path)),
        output_size_bytes=output_size,
        output_sha256=hashlib.sha256(canonical_json_bytes(evidence)).hexdigest(),
    )


def write_artifact(
    job_dir: Path,
    *,
    job_id: str,
    source_id: str,
    manifest: dict[str, object],
    payload: ArtifactPayload,
    max_artifact_bytes: int,
) -> dict[str, object]:
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    members = (("manifest.json", manifest_bytes),) + tuple(
        (item.path, item.content) for item in payload.files
    )
    stream = io.BytesIO()
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path, content in members:
                member = tarfile.TarInfo(path)
                member.size = len(content)
                member.mode = 0o444
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                archive.addfile(member, io.BytesIO(content))
    except (OSError, tarfile.TarError) as error:
        raise WorkerRuntimeError("parser_failed") from error
    archive_bytes = stream.getvalue()
    if not archive_bytes or len(archive_bytes) > max_artifact_bytes:
        raise WorkerRuntimeError("artifact_limit_exceeded")
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    receipt: dict[str, object] = {
        "archive_format": "tar",
        "contract_version": 2,
        "file_count": len(members),
        "job_id": job_id,
        "manifest_sha256": manifest_sha256,
        "schema_id": "llm-wiki-parser-artifact/v2",
        "sha256": archive_sha256,
        "size_bytes": len(archive_bytes),
        "source_id": source_id,
    }
    atomic_write_bytes(job_dir / ARTIFACT_NAME, archive_bytes, mode=0o444)
    atomic_write_json(job_dir / RECEIPT_NAME, receipt)
    return receipt


__all__ = [
    "ArtifactContent",
    "ArtifactPayload",
    "prepare_artifact_payload",
    "write_artifact",
]

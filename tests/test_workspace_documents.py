from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from pi_agent_core_py.web.files import WorkspacePublishPolicyError, WorkspaceStore
from pi_agent_core_py.web.workspace_documents import (
    DocumentConversionPayload,
    GeneratedDocumentFile,
    InvalidWorkspaceDocumentError,
    WorkspaceDocumentConverterRegistry,
    WorkspaceDocumentService,
)


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=Headers({"content-type": "application/pdf"}),
    )


@dataclass(frozen=True)
class _Converter:
    fails: bool = False
    source_extension: str = ".pdf"
    converter_name: str = "test-fixed-pdf"
    converter_version: str = "1.0"

    def convert(self, source_path: Path, *, source_name: str) -> DocumentConversionPayload:
        assert source_path.read_bytes() == b"immutable-pdf"
        assert source_name == "Quarterly Report.pdf"
        if self.fails:
            raise InvalidWorkspaceDocumentError("malformed fixture")
        return DocumentConversionPayload(
            status="succeeded",
            files=(
                GeneratedDocumentFile(
                    relative_path="content.md",
                    content=b"# Converted\n",
                    mime="text/markdown",
                ),
                GeneratedDocumentFile(
                    relative_path="tables/sheet.csv",
                    content=b"value\n42\n",
                    mime="text/csv",
                ),
            ),
        )


async def _setup(tmp_path: Path, *, fails: bool = False):
    store = WorkspaceStore(tmp_path / "uploads")
    await store.init()
    await store.ensure_session_workspace("session-1")
    source = await store.save(
        "session-1",
        _upload("Quarterly Report.pdf", b"immutable-pdf"),
    )
    service = WorkspaceDocumentService(
        store,
        staging_root=tmp_path / "document-staging",
        registry=WorkspaceDocumentConverterRegistry({".pdf": _Converter(fails=fails)}),
    )
    return store, source, service


@pytest.mark.asyncio
async def test_document_conversion_publishes_one_readonly_bundle_and_reuses_manifest(tmp_path):
    store, source, service = await _setup(tmp_path)
    source_revision = (await store.get_workspace_state("session-1")).revision

    converted = await service.convert(source.id, "session-1")

    assert source.purpose == "document_original"
    assert source.logical_path.startswith("documents/Quarterly Report-")
    assert source.logical_path.endswith("/original.pdf")
    assert converted.status == "succeeded"
    assert converted.workspace_revision == source_revision + 1
    assert {ref.logical_path.rsplit("/", 1)[-1] for ref in converted.files} == {
        "content.md",
        "manifest.json",
        "sheet.csv",
    }
    assert all(ref.purpose == "document_conversion" for ref in converted.files)
    manifest_ref = next(ref for ref in converted.files if ref.name == "manifest.json")
    manifest_raw = await asyncio.to_thread(
        Path(manifest_ref.path).read_text,
        encoding="utf-8",
    )
    manifest = json.loads(manifest_raw)
    assert manifest["source_sha256"] == source.sha256
    assert {entry["logical_path"].rsplit("/", 1)[-1] for entry in manifest["generated_files"]} == {
        "content.md",
        "sheet.csv",
    }

    reused = await service.convert(source.id, "session-1")
    assert reused.reused is True
    assert reused.workspace_revision == converted.workspace_revision


@pytest.mark.asyncio
async def test_failed_document_conversion_keeps_original_and_publishes_no_partial_content(tmp_path):
    store, source, service = await _setup(tmp_path, fails=True)
    original_bytes = await asyncio.to_thread(Path(source.path).read_bytes)

    result = await service.convert(source.id, "session-1")

    assert result.status == "failed"
    assert result.error_code == "invalid_document"
    assert [ref.name for ref in result.files] == ["manifest.json"]
    assert await asyncio.to_thread(Path(source.path).read_bytes) == original_bytes
    assert (
        await store.get_by_logical_path(
            "session-1",
            f"{Path(source.logical_path).parent.as_posix()}/content.md",
        )
        is None
    )
    with pytest.raises(WorkspacePublishPolicyError):
        await store.update_text("session-1", source.id, "replacement")
    with pytest.raises(WorkspacePublishPolicyError):
        await store.delete_for_session("session-1", source.id)

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from agent_workspace import workspace_document_output_root
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
    assert source.logical_path == "upload/Quarterly Report.pdf"
    assert converted.status == "succeeded"
    assert converted.workspace_revision == source_revision + 1
    assert {ref.logical_path.rsplit("/", 1)[-1] for ref in converted.files} == {
        "content.md",
        "manifest.json",
        "sheet.csv",
    }
    assert all(ref.purpose == "document_conversion" for ref in converted.files)
    assert all(
        ref.logical_path.startswith("documents/Quarterly Report-") for ref in converted.files
    )
    manifest_ref = next(ref for ref in converted.files if ref.name == "manifest.json")
    manifest_raw = await asyncio.to_thread(
        Path(manifest_ref.path).read_text,
        encoding="utf-8",
    )
    manifest = json.loads(manifest_raw)
    assert manifest["source_sha256"] == source.sha256
    assert manifest["source_logical_path"] == "upload/Quarterly Report.pdf"
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
            f"{workspace_document_output_root(source)}/content.md",
        )
        is None
    )
    with pytest.raises(WorkspacePublishPolicyError):
        await store.update_text("session-1", source.id, "replacement")
    with pytest.raises(WorkspacePublishPolicyError):
        await store.delete_for_session("session-1", source.id)


@pytest.mark.asyncio
async def test_duplicate_document_outputs_are_isolated_and_legacy_originals_still_convert(tmp_path):
    store, first, service = await _setup(tmp_path)
    second = await store.save("session-1", _upload("Quarterly Report.pdf", b"immutable-pdf"))
    first_result = await service.convert(first.id, "session-1")
    second_result = await service.convert(second.id, "session-1")
    assert second.logical_path == "upload/Quarterly Report (2).pdf"
    assert first_result.document_id != second_result.document_id
    assert first_result.primary_file_id != second_result.primary_file_id
    assert (await service.convert(first.id, "session-1")).reused
    assert (await service.convert(second.id, "session-1")).reused

    # Emulate persisted metadata from the previous release; no production migration.
    legacy = await store.save("session-1", _upload("Quarterly Report.pdf", b"immutable-pdf"))
    legacy_root = "documents/old-report"
    metadata = Path(legacy.path).parent / "metadata.json"
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["logical_path"] = f"{legacy_root}/original.pdf"
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    result = await service.convert(legacy.id, "session-1")
    assert result.status == "succeeded"
    assert all(ref.logical_path.startswith(f"{legacy_root}/") for ref in result.files)
    assert (await service.convert(legacy.id, "session-1")).reused


@pytest.mark.asyncio
async def test_converter_cannot_publish_over_an_upload_original(tmp_path):
    from agent_workspace import WorkspacePublishChange

    store, source, _ = await _setup(tmp_path)
    snapshot = await store.materialize_workspace_revision("session-1", tmp_path / "snapshot")
    with pytest.raises(WorkspacePublishPolicyError):
        await store.publish_document_conversion(
            "session-1",
            source_file_id=source.id,
            transaction_id="publish-" + "a" * 32,
            expected_workspace_revision=snapshot.revision,
            expected_workspace_sha256=snapshot.tree_sha256,
            changes=(WorkspacePublishChange(
                logical_path=source.logical_path,
                source_path=Path(source.path),
                size=source.size,
                sha256=source.sha256,
            ),),
            deleted_paths=(),
        )
    assert await asyncio.to_thread(Path(source.path).read_bytes) == b"immutable-pdf"

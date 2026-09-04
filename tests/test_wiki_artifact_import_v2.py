"""Fail-closed attacks against the Contract v2 artifact importer."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import WikiJob, WikiSource, WikiStore, WikiStoreError
from pi_agent_core_py.web.wiki.artifact_import import import_parser_artifact_v2
from wiki_parser import (
    FakeMineruParserProvider,
    FakeParserDocumentV2,
    FakeParserScenarioV2,
    ParserArtifactManifestV2,
    ParserArtifactReceiptV2,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserLimits,
    ParserSourceSpec,
)

_PDF = b"%PDF-1.7\nuntrusted artifact source\n%%EOF\n"


async def _prepared_bundle(
    tmp_path: Path,
) -> tuple[
    WikiStore,
    WikiSource,
    WikiJob,
    FakeMineruParserProvider,
    ParserJobHandleV2,
    ParserJobStatusV2,
    ParserArtifactReceiptV2,
    Path,
]:
    store = await WikiStore.open(
        tmp_path / "wiki",
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    space = await store.create_space(name="Artifact v2")
    source = await store.upload_source(
        space.id,
        display_name="paper.pdf",
        mime_type="application/pdf",
        content=_PDF,
    )
    source, job = await store.begin_parse_job(source.id, requested_mode="pipeline")
    job = await store.set_job_status(job.id, "running")
    provider = FakeMineruParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                document=FakeParserDocumentV2(
                    pages=("# Page one\n", ""),
                ),
            ),
        )
    )
    probe = await provider.probe()
    spec = ParserJobSpecV2(
        job_id=job.id,
        source=ParserSourceSpec(
            source_id=source.id,
            display_name=source.display_name,
            size_bytes=source.size_bytes,
            sha256=source.source_sha256,
        ),
        requested_mode="pipeline",
        routing_config=probe.routing_config,
        limits=ParserLimits(),
    )
    source_path = store.file_store.resolve_owned_regular_file(
        source.space_id,
        source.source_relpath,
    )
    handle = await provider.create_job(spec, source_path=source_path)
    status = await provider.wait(handle)
    assert status.state == "succeeded"
    archive_path = (tmp_path / "artifact-v2.tar").resolve()
    receipt = await provider.download_artifact(
        handle,
        local_path=archive_path,
        expected_sha256=status.artifact_sha256,
    )
    return store, source, job, provider, handle, status, receipt, archive_path


def _tar_files(archive_path: Path) -> dict[str, bytes]:
    with tarfile.open(archive_path, mode="r:") as archive:
        files: dict[str, bytes] = {}
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read()
    return files


def _build_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mtime = 0
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def _retarget_evidence(
    status: ParserJobStatusV2,
    receipt: ParserArtifactReceiptV2,
    archive: bytes,
    manifest: bytes,
    *,
    file_count: int,
) -> tuple[ParserJobStatusV2, ParserArtifactReceiptV2]:
    archive_sha = hashlib.sha256(archive).hexdigest()
    status_payload = status.model_dump(mode="python")
    status_payload.update(
        artifact_size_bytes=len(archive),
        artifact_sha256=archive_sha,
    )
    changed_status = ParserJobStatusV2.model_validate(status_payload)
    changed_receipt = ParserArtifactReceiptV2(
        job_id=receipt.job_id,
        source_id=receipt.source_id,
        size_bytes=len(archive),
        sha256=archive_sha,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        file_count=file_count,
    )
    return changed_status, changed_receipt


@pytest.mark.asyncio
async def test_v2_import_accepts_exact_page_bundle(tmp_path: Path) -> None:
    store, source, job, provider, handle, status, receipt, archive_path = (
        await _prepared_bundle(tmp_path)
    )
    try:
        probe = await provider.probe()
        revision_id = store.new_parse_revision_id()
        imported = import_parser_artifact_v2(
            store,
            source,
            job_id=job.id,
            requested_mode="pipeline",
            archive_path=archive_path,
            receipt=receipt,
            status=status,
            limits=ParserLimits(),
            expected_provider="fake_mineru",
            expected_routing_config=probe.routing_config,
            parse_revision_id=revision_id,
        )

        assert imported.page_count == 2
        assert [
            artifact.source_locator_json
            for artifact in imported.artifacts
            if artifact.kind == "page_markdown"
        ] == ['{"page_number":1}', '{"page_number":2}']
        second_page = next(
            artifact
            for artifact in imported.artifacts
            if artifact.relpath.endswith("pages/000002.md")
        )
        assert second_page.size_bytes == 0
        assert {artifact.kind for artifact in imported.artifacts} == {
            "parsed_markdown",
            "page_markdown",
            "manifest",
        }
    finally:
        await provider.destroy(handle)
        await store.close()


@pytest.mark.asyncio
async def test_v2_import_rejects_rehashed_noncanonical_document_markdown(
    tmp_path: Path,
) -> None:
    store, source, job, provider, handle, status, receipt, archive_path = (
        await _prepared_bundle(tmp_path)
    )
    try:
        files = _tar_files(archive_path)
        manifest_payload = json.loads(files["manifest.json"])
        forged_markdown = b"# Rehashed but not canonical\n"
        files["parsed.md"] = forged_markdown
        manifest_payload["markdown"]["size_bytes"] = len(forged_markdown)
        manifest_payload["markdown"]["sha256"] = hashlib.sha256(forged_markdown).hexdigest()
        manifest_bytes = (
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        ParserArtifactManifestV2.model_validate_json(manifest_bytes)
        files["manifest.json"] = manifest_bytes
        archive = _build_tar(files)
        archive_path.write_bytes(archive)
        changed_status, changed_receipt = _retarget_evidence(
            status,
            receipt,
            archive,
            manifest_bytes,
            file_count=len(files),
        )
        probe = await provider.probe()

        with pytest.raises(WikiStoreError) as raised:
            import_parser_artifact_v2(
                store,
                source,
                job_id=job.id,
                requested_mode="pipeline",
                archive_path=archive_path,
                receipt=changed_receipt,
                status=changed_status,
                limits=ParserLimits(),
                expected_provider="fake_mineru",
                expected_routing_config=probe.routing_config,
                parse_revision_id="parse_revision_000000000000000000000001",
            )
        assert raised.value.code == "invalid_artifact"
        assert await store.list_artifacts(source.id) == ()
    finally:
        await provider.destroy(handle)
        await store.close()


@pytest.mark.asyncio
async def test_v2_import_rejects_extra_path_traversal_before_writing(
    tmp_path: Path,
) -> None:
    store, source, job, provider, handle, status, receipt, archive_path = (
        await _prepared_bundle(tmp_path)
    )
    try:
        files = _tar_files(archive_path)
        files["../escape.md"] = b"escape"
        archive = _build_tar(files)
        archive_path.write_bytes(archive)
        changed_status, changed_receipt = _retarget_evidence(
            status,
            receipt,
            archive,
            files["manifest.json"],
            file_count=len(files),
        )
        probe = await provider.probe()

        with pytest.raises(WikiStoreError) as raised:
            import_parser_artifact_v2(
                store,
                source,
                job_id=job.id,
                requested_mode="pipeline",
                archive_path=archive_path,
                receipt=changed_receipt,
                status=changed_status,
                limits=ParserLimits(),
                expected_provider="fake_mineru",
                expected_routing_config=probe.routing_config,
                parse_revision_id="parse_revision_000000000000000000000001",
            )
        assert raised.value.code == "invalid_artifact"
        assert not (tmp_path / "escape.md").exists()
    finally:
        await provider.destroy(handle)
        await store.close()

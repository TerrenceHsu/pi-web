"""Untrusted ParserProvider artifact import boundary."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import WikiJob, WikiSource, WikiStore, WikiStoreError
from pi_agent_core_py.web.wiki.artifact_import import import_parser_artifact
from wiki_parser import (
    FakeParserImage,
    FakeParserOutput,
    FakeParserProvider,
    ParserArtifactReceipt,
    ParserJobHandle,
    ParserJobSpec,
    ParserLimits,
    ParserSourceSpec,
)


async def _source_and_job(
    tmp_path: Path,
    *,
    output: FakeParserOutput,
) -> tuple[
    WikiStore,
    WikiSource,
    WikiJob,
    ParserJobSpec,
    FakeParserProvider,
    ParserJobHandle,
]:
    store = await WikiStore.open(
        tmp_path / "wiki",
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    space = await store.create_space(name="Import")
    source = await store.upload_source(
        space.id,
        display_name="source.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.7\nsource\n",
    )
    source, wiki_job = await store.begin_parse_job(source.id, requested_mode="fast")
    wiki_job = await store.set_job_status(wiki_job.id, "running")
    spec = ParserJobSpec(
        job_id=wiki_job.id,
        source=ParserSourceSpec(
            source_id=source.id,
            display_name=source.display_name,
            size_bytes=source.size_bytes,
            sha256=source.source_sha256,
        ),
        limits=ParserLimits(max_image_bytes=1024 * 1024),
    )
    provider = FakeParserProvider(outputs=(output,))
    source_path = store.file_store.resolve_owned_regular_file(
        source.space_id,
        source.source_relpath,
    )
    handle = await provider.create_job(spec, source_path=source_path)
    status = await provider.wait(handle)
    assert status.state == "succeeded"
    return store, source, wiki_job, spec, provider, handle


async def test_import_valid_bundle_and_commit_source(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"
    store, source, wiki_job, spec, provider, handle = await _source_and_job(
        tmp_path,
        output=FakeParserOutput(
            markdown="# Parsed\n",
            images=(FakeParserImage(mime_type="image/png", content=png, page_number=2),),
            page_count=2,
        ),
    )
    try:
        archive = (tmp_path / "artifact.tar").resolve()
        receipt = await provider.download_artifact(handle, local_path=archive)
        revision_id = store.new_parse_revision_id()
        imported = import_parser_artifact(
            store,
            source,
            job_id=spec.job_id,
            archive_path=archive,
            receipt=receipt,
            limits=spec.limits,
            expected_provider="fake",
            expected_provider_version="fake-1",
            parse_revision_id=revision_id,
        )
        markdown = next(item for item in imported.artifacts if item.kind == "parsed_markdown")
        manifest = next(item for item in imported.artifacts if item.kind == "manifest")
        attempt = store.new_parse_attempt(
            source,
            wiki_job,
            provider_attempt_id=handle.provider_job_id,
            ordinal=1,
            parser=imported.manifest.provider,
            parser_version=imported.manifest.provider_version,
            preset=imported.manifest.mode,
            route_reasons=("legacy_contract_v1",),
            state="succeeded",
            output_size_bytes=sum(item.size_bytes for item in imported.artifacts),
            output_sha256="1" * 64,
            started_at_ms=wiki_job.started_at_ms,
            finished_at_ms=wiki_job.started_at_ms,
        )
        revision = store.new_parse_revision(
            source,
            wiki_job,
            attempt,
            revision_id=revision_id,
            contract_version=1,
            artifact_schema=imported.manifest.schema_id,
            parsed_markdown_relpath=markdown.relpath,
            parsed_markdown_sha256=markdown.sha256,
            manifest_relpath=manifest.relpath,
            manifest_sha256=manifest.sha256,
            page_count=imported.page_count,
        )
        completed = await store.complete_source_parse(
            source.id,
            job_id=wiki_job.id,
            attempts=(attempt,),
            revision=revision,
            artifacts=imported.artifacts,
        )

        assert completed.status == "parsed"
        assert {item.kind for item in imported.artifacts} == {
            "parsed_markdown",
            "page_markdown",
            "embedded_image",
            "manifest",
        }
        assert any(
            item.source_locator_json == '{"page_number":2}'
            for item in imported.artifacts
        )
    finally:
        await store.close()


async def test_import_rejects_image_mime_magic_mismatch(tmp_path: Path) -> None:
    store, source, _job, spec, provider, handle = await _source_and_job(
        tmp_path,
        output=FakeParserOutput(
            images=(FakeParserImage(mime_type="image/png", content=b"not-a-png"),),
        ),
    )
    try:
        archive = (tmp_path / "bad-image.tar").resolve()
        receipt = await provider.download_artifact(handle, local_path=archive)
        revision_id = store.new_parse_revision_id()
        with pytest.raises(WikiStoreError) as exc_info:
            import_parser_artifact(
                store,
                source,
                job_id=spec.job_id,
                archive_path=archive,
                receipt=receipt,
                limits=spec.limits,
                    expected_provider="fake",
                    expected_provider_version="fake-1",
                    parse_revision_id=revision_id,
            )
        assert exc_info.value.code == "invalid_artifact"
    finally:
        await store.close()


async def test_import_rejects_tar_path_traversal_before_writing(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for name in ("../escape", "manifest.json"):
            payload = b"x"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
    archive_bytes = buffer.getvalue()
    archive = (tmp_path / "traversal.tar").resolve()
    archive.write_bytes(archive_bytes)
    receipt = ParserArtifactReceipt(
        job_id="job_000000000000000000000001",
        source_id="source_000000000000000000000001",
        size_bytes=len(archive_bytes),
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        manifest_sha256=hashlib.sha256(b"x").hexdigest(),
        file_count=2,
    )
    limits = ParserLimits(max_image_bytes=1024)

    store = await WikiStore.open(
        tmp_path / "wiki",
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: receipt.source_id,
    )
    try:
        space = await store.create_space(name="Traversal")
        source = await store.upload_source(
            space.id,
            display_name="source.pdf",
            mime_type="application/pdf",
            content=b"%PDF-1.7\nsource\n",
        )
        with pytest.raises(WikiStoreError) as exc_info:
            import_parser_artifact(
                store,
                source,
                job_id=receipt.job_id,
                archive_path=archive,
                receipt=receipt,
                limits=limits,
                expected_provider="fake",
                expected_provider_version="fake-1",
                parse_revision_id="parse_revision_000000000000000000000001",
            )
        assert exc_info.value.code == "invalid_artifact"
        assert not (tmp_path / "escape").exists()
    finally:
        await store.close()

"""Source, artifact and durable parse-job semantics for WikiStore."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path, PurePosixPath

import pytest

from pi_agent_core_py.web.wiki import WikiStore, WikiStoreError


@pytest.fixture
async def source_store(tmp_path: Path) -> AsyncIterator[WikiStore]:
    source_ids: Iterator[str] = iter(
        (
            "source_000000000000000000000001",
            "source_000000000000000000000002",
        )
    )
    artifact_ids: Iterator[str] = iter(
        (
            "artifact_000000000000000000000001",
            "artifact_000000000000000000000002",
            "artifact_000000000000000000000003",
        )
    )
    store = await WikiStore.open(
        tmp_path,
        clock_ms=lambda: 100,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: next(source_ids),
        artifact_id_factory=lambda: next(artifact_ids),
        job_id_factory=lambda: "job_000000000000000000000001",
        parse_attempt_id_factory=lambda: "parse_attempt_000000000000000000000001",
        parse_revision_id_factory=lambda: "parse_revision_000000000000000000000001",
    )
    try:
        yield store
    finally:
        await store.close()


async def test_upload_source_is_no_clobber_and_content_addressed(
    source_store: WikiStore,
    tmp_path: Path,
) -> None:
    space = await source_store.create_space(name="Sources")
    payload = b"%PDF-1.7\nminimal\n"
    source = await source_store.upload_source(
        space.id,
        display_name="Quarterly report.pdf",
        mime_type="application/pdf",
        content=payload,
    )

    assert source.source_relpath == (
        "raw/quarterly-report--source_000000000000000000000001/source.pdf"
    )
    assert source.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "spaces" / space.id / source.source_relpath).read_bytes() == payload
    assert await source_store.list_sources(space.id) == (source,)

    with pytest.raises(WikiStoreError) as exc_info:
        await source_store.upload_source(
            space.id,
            display_name="same-content.pdf",
            mime_type="application/pdf",
            content=payload,
        )
    assert exc_info.value.code == "source_conflict"
    assert not list((tmp_path / "spaces" / space.id / "raw").glob("same-content--*"))


async def test_parse_job_and_source_state_machines(source_store: WikiStore) -> None:
    space = await source_store.create_space(name="Jobs")
    source = await source_store.upload_source(
        space.id,
        display_name="source.html",
        mime_type="text/html",
        content=b"<!doctype html><title>safe</title>",
    )
    job = await source_store.create_job(space.id, kind="parse", source_id=source.id)
    running_source = await source_store.set_source_status(source.id, "parsing")
    running_job = await source_store.set_job_status(job.id, "running")
    assert running_source.status == "parsing"
    assert running_job.status == "running"
    succeeded = await source_store.set_job_status(job.id, "succeeded")
    assert succeeded.status == "succeeded"
    assert succeeded.finished_at_ms is not None

    with pytest.raises(WikiStoreError) as exc_info:
        await source_store.set_job_status(job.id, "running")
    assert exc_info.value.code == "invalid_status_transition"


async def test_complete_parse_verifies_files_before_committing_artifacts(
    source_store: WikiStore,
) -> None:
    space = await source_store.create_space(name="Artifacts")
    source = await source_store.upload_source(
        space.id,
        display_name="文档.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.7\nsource\n",
    )
    source, job = await source_store.begin_parse_job(source.id, requested_mode="fast")
    job = await source_store.set_job_status(job.id, "running")
    revision_id = source_store.new_parse_revision_id()
    bundle = PurePosixPath(
        source_store.file_store.parse_revision_relative_path(source, revision_id)
    )
    parsed_path = f"{bundle}/parsed.md"
    page_path = f"{bundle}/pages/000001.md"
    manifest_path = f"{bundle}/manifest.json"
    parsed = b"# Parsed\n"
    manifest = b'{"schema":"llm-wiki-parser-artifact/v1"}\n'
    for relative_path, payload in (
        (parsed_path, parsed),
        (page_path, parsed),
        (manifest_path, manifest),
    ):
        source_store.file_store.write_owned_file_atomic(
            space.id,
            relative_path,
            payload,
            overwrite=False,
            max_bytes=1024,
        )
    artifacts = (
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="parsed_markdown",
            relpath=parsed_path,
            mime_type="text/markdown",
            size_bytes=len(parsed),
            sha256=hashlib.sha256(parsed).hexdigest(),
        ),
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="page_markdown",
            relpath=page_path,
            mime_type="text/markdown",
            size_bytes=len(parsed),
            sha256=hashlib.sha256(parsed).hexdigest(),
            source_locator_json='{"page_number":1}',
        ),
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="manifest",
            relpath=manifest_path,
            mime_type="application/json",
            size_bytes=len(manifest),
            sha256=hashlib.sha256(manifest).hexdigest(),
        ),
    )
    attempt = source_store.new_parse_attempt(
        source,
        job,
        provider_attempt_id="fake_attempt_1",
        ordinal=1,
        parser="fake",
        parser_version="fake-1",
        preset="fast_no_ocr",
        route_reasons=("legacy_contract_v1",),
        state="succeeded",
        output_size_bytes=sum(item.size_bytes for item in artifacts),
        output_sha256="1" * 64,
        started_at_ms=job.started_at_ms,
        finished_at_ms=job.started_at_ms,
    )
    revision = source_store.new_parse_revision(
        source,
        job,
        attempt,
        revision_id=revision_id,
        contract_version=1,
        artifact_schema="llm-wiki-parser-artifact/v1",
        parsed_markdown_relpath=parsed_path,
        parsed_markdown_sha256=hashlib.sha256(parsed).hexdigest(),
        manifest_relpath=manifest_path,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        page_count=1,
    )
    completed = await source_store.complete_source_parse(
        source.id,
        job_id=job.id,
        attempts=(attempt,),
        revision=revision,
        artifacts=artifacts,
    )

    assert completed.status == "parsed"
    assert completed.selected_parse_revision_id == revision.id
    assert completed.selection_version == 1
    assert await source_store.get_selected_parse_revision(source.id) == revision
    assert await source_store.list_artifacts(source.id) == tuple(
        sorted(artifacts, key=lambda item: (item.relpath, item.id))
    )


async def test_complete_parse_rejects_tampered_artifact(source_store: WikiStore) -> None:
    space = await source_store.create_space(name="Tamper")
    source = await source_store.upload_source(
        space.id,
        display_name="x.html",
        mime_type="text/html",
        content=b"<h1>x</h1>",
    )
    source, job = await source_store.begin_parse_job(source.id, requested_mode="builtin")
    job = await source_store.set_job_status(job.id, "running")
    revision_id = source_store.new_parse_revision_id()
    bundle = PurePosixPath(
        source_store.file_store.parse_revision_relative_path(source, revision_id)
    )
    path = f"{bundle}/parsed.md"
    page_path = f"{bundle}/pages/000001.md"
    manifest_path = f"{bundle}/manifest.json"
    manifest = b'{"schema":"llm-wiki-html-artifact/v1"}\n'
    for relative_path, payload in (
        (path, b"tampered"),
        (page_path, b"safe"),
        (manifest_path, manifest),
    ):
        source_store.file_store.write_owned_file_atomic(
            space.id,
            relative_path,
            payload,
            overwrite=False,
            max_bytes=1024,
        )
    artifacts = (
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="parsed_markdown",
            relpath=path,
            mime_type="text/markdown",
            size_bytes=8,
            sha256="0" * 64,
        ),
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="page_markdown",
            relpath=page_path,
            mime_type="text/markdown",
            size_bytes=4,
            sha256=hashlib.sha256(b"safe").hexdigest(),
            source_locator_json='{"page_number":1}',
        ),
        source_store.new_artifact(
            source.id,
            parse_revision_id=revision_id,
            kind="manifest",
            relpath=manifest_path,
            mime_type="application/json",
            size_bytes=len(manifest),
            sha256=hashlib.sha256(manifest).hexdigest(),
        ),
    )
    attempt = source_store.new_parse_attempt(
        source,
        job,
        provider_attempt_id="html_attempt_1",
        ordinal=1,
        parser="html_builtin",
        parser_version="1.0.0",
        preset="html_single_file",
        route_reasons=("builtin_html",),
        state="succeeded",
        output_size_bytes=sum(item.size_bytes for item in artifacts),
        output_sha256="1" * 64,
        started_at_ms=job.started_at_ms,
        finished_at_ms=job.started_at_ms,
    )
    revision = source_store.new_parse_revision(
        source,
        job,
        attempt,
        revision_id=revision_id,
        contract_version=1,
        artifact_schema="llm-wiki-html-artifact/v1",
        parsed_markdown_relpath=path,
        parsed_markdown_sha256="0" * 64,
        manifest_relpath=manifest_path,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        page_count=1,
    )
    with pytest.raises(WikiStoreError) as exc_info:
        await source_store.complete_source_parse(
            source.id,
            job_id=job.id,
            attempts=(attempt,),
            revision=revision,
            artifacts=artifacts,
        )
    assert exc_info.value.code == "invalid_artifact"
    assert (await source_store.get_source(source.id)).status == "parsing"
    assert await source_store.list_artifacts(source.id) == ()

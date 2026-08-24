"""Immutable Raw parse revisions, selected pointer repair and CAS semantics."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import aiosqlite
import pytest

from pi_agent_core_py.web.wiki import WikiIngestionService, WikiStore, WikiStoreError
from wiki_parser import FakeParserOutput, FakeParserProvider


async def _store(
    root: Path,
    *,
    outputs: tuple[FakeParserOutput, ...],
) -> tuple[WikiStore, WikiIngestionService, str]:
    job_ids: Iterator[str] = iter(
        (
            "job_000000000000000000000001",
            "job_000000000000000000000002",
            "job_000000000000000000000003",
        )
    )
    attempt_ids: Iterator[str] = iter(
        (
            "parse_attempt_000000000000000000000001",
            "parse_attempt_000000000000000000000002",
            "parse_attempt_000000000000000000000003",
        )
    )
    revision_ids: Iterator[str] = iter(
        (
            "parse_revision_000000000000000000000001",
            "parse_revision_000000000000000000000002",
            "parse_revision_000000000000000000000003",
        )
    )
    store = await WikiStore.open(
        root,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: next(job_ids),
        parse_attempt_id_factory=lambda: next(attempt_ids),
        parse_revision_id_factory=lambda: next(revision_ids),
    )
    space = await store.create_space(name="Revisions")
    provider = FakeParserProvider(outputs=outputs)
    return store, WikiIngestionService(store, pdf_provider=provider), space.id


async def _upload_pdf(service: WikiIngestionService, space_id: str) -> str:
    source = await service.upload_source(
        space_id,
        display_name="paper.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.7\nimmutable source\n",
    )
    return source.id


async def test_reparse_publishes_new_revision_and_preserves_history(tmp_path: Path) -> None:
    store, service, space_id = await _store(
        tmp_path,
        outputs=(
            FakeParserOutput(markdown="# First\n"),
            FakeParserOutput(markdown="# Second\n"),
        ),
    )
    try:
        source_id = await _upload_pdf(service, space_id)
        first_outcome = await service.parse_source(source_id)
        first_revision = await store.get_selected_parse_revision(source_id)
        assert first_revision is not None
        first_markdown = await store.read_artifact_content(
            first_outcome.source,
            next(
                item
                for item in await store.list_artifacts(source_id)
                if item.kind == "parsed_markdown"
            ),
        )

        second_outcome = await service.parse_source(source_id)
        second_revision = await store.get_selected_parse_revision(source_id)
        assert second_revision is not None
        assert second_revision.id != first_revision.id
        assert second_outcome.source.selection_version == 2
        assert second_outcome.source.selected_parse_revision_id == second_revision.id

        revisions = await store.list_parse_revisions(source_id)
        assert revisions == (first_revision, second_revision)
        first_artifacts = await store.list_artifacts(
            source_id,
            parse_revision_id=first_revision.id,
        )
        second_artifacts = await store.list_artifacts(source_id)
        assert {item.parse_revision_id for item in first_artifacts} == {first_revision.id}
        assert {item.parse_revision_id for item in second_artifacts} == {second_revision.id}
        assert first_markdown == b"# First\n"
        assert store.file_store.read_owned_file(
            space_id,
            first_revision.parsed_markdown_relpath,
            max_bytes=1024,
        ) == first_markdown
        assert store.file_store.read_owned_file(
            space_id,
            second_revision.parsed_markdown_relpath,
            max_bytes=1024,
        ) == b"# Second\n"

        source = await store.get_source(source_id)
        bundle = PurePosixPath(source.source_relpath).parent
        assert not store.file_store.owned_file_exists(space_id, f"{bundle}/parsed.md")
        assert not store.file_store.owned_file_exists(space_id, f"{bundle}/manifest.json")
        selected = json.loads(
            store.file_store.read_owned_file(
                space_id,
                f"{bundle}/selected.json",
                max_bytes=128 * 1024,
            )
        )
        assert selected["parse_revision_id"] == second_revision.id
        assert selected["selection_version"] == 2
    finally:
        await store.close()


async def test_selection_version_prevents_cross_connection_aba(tmp_path: Path) -> None:
    store, service, space_id = await _store(
        tmp_path,
        outputs=(
            FakeParserOutput(markdown="# First\n"),
            FakeParserOutput(markdown="# Second\n"),
        ),
    )
    source_id = await _upload_pdf(service, space_id)
    await service.parse_source(source_id)
    await service.parse_source(source_id)
    first_revision, second_revision = await store.list_parse_revisions(source_id)
    second_connection = await WikiStore.open(tmp_path)
    try:
        selected_first = await store.select_parse_revision(
            source_id,
            first_revision.id,
            expected_selection_version=2,
        )
        assert selected_first.selection_version == 3
        with pytest.raises(WikiStoreError) as exc_info:
            await second_connection.select_parse_revision(
                source_id,
                second_revision.id,
                expected_selection_version=2,
            )
        assert exc_info.value.code == "source_conflict"
        assert (await second_connection.get_source(source_id)).selected_parse_revision_id == (
            first_revision.id
        )
    finally:
        await second_connection.close()
        await store.close()


async def test_failed_reparse_keeps_previous_selected_revision_and_records_attempt(
    tmp_path: Path,
) -> None:
    store, service, space_id = await _store(
        tmp_path,
        outputs=(
            FakeParserOutput(markdown="# Stable\n"),
            FakeParserOutput(failure_code="parsing_failed"),
        ),
    )
    try:
        source_id = await _upload_pdf(service, space_id)
        first = await service.parse_source(source_id)
        selected_id = first.source.selected_parse_revision_id
        selected_path = store.file_store.selected_parse_relative_path(first.source)
        selected_before_failure = store.file_store.read_owned_file(
            space_id,
            selected_path,
            max_bytes=128 * 1024,
        )
        failed = await service.parse_source(source_id)

        assert failed.source.status == "failed"
        assert failed.source.safe_error_code == "parsing_failed"
        assert failed.source.selected_parse_revision_id == selected_id
        assert failed.source.selection_version == 1
        assert len(await store.list_parse_revisions(source_id)) == 1
        attempts = await store.list_parse_attempts(failed.job.id)
        assert len(attempts) == 1
        assert attempts[0].state == "failed"
        assert attempts[0].safe_error_code == "parsing_failed"
        assert attempts[0].source_sha256 == failed.source.source_sha256
        assert await store.repair_selected_parse_pointers() == ()
        assert store.file_store.read_owned_file(
            space_id,
            selected_path,
            max_bytes=128 * 1024,
        ) == selected_before_failure
    finally:
        await store.close()


async def test_startup_repairs_selected_pointer_from_database(tmp_path: Path) -> None:
    store, service, space_id = await _store(
        tmp_path,
        outputs=(FakeParserOutput(markdown="# Durable\n"),),
    )
    source_id = await _upload_pdf(service, space_id)
    outcome = await service.parse_source(source_id)
    source = outcome.source
    selected_path = store.file_store.selected_parse_relative_path(source)
    store.file_store.write_owned_file_atomic(
        space_id,
        selected_path,
        b"corrupt",
        overwrite=True,
        max_bytes=128 * 1024,
    )
    await store.close()

    reopened = await WikiStore.open(tmp_path)
    try:
        repaired = json.loads(
            reopened.file_store.read_owned_file(
                space_id,
                selected_path,
                max_bytes=128 * 1024,
            )
        )
        assert repaired["source_id"] == source_id
        assert repaired["parse_revision_id"] == source.selected_parse_revision_id
        assert repaired["selection_version"] == source.selection_version
    finally:
        await reopened.close()


async def test_database_rejects_cross_source_selected_revision(tmp_path: Path) -> None:
    source_ids = iter(
        (
            "source_000000000000000000000001",
            "source_000000000000000000000002",
        )
    )
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: next(source_ids),
    )
    space = await store.create_space(name="Isolation")
    service = WikiIngestionService(store)
    try:
        first = await service.upload_source(
            space.id,
            display_name="first.html",
            mime_type="text/html",
            content=b"<h1>First</h1>",
        )
        await service.parse_source(first.id)
        revision = await store.get_selected_parse_revision(first.id)
        assert revision is not None
        second = await service.upload_source(
            space.id,
            display_name="second.html",
            mime_type="text/html",
            content=b"<h1>Second</h1>",
        )
        db = store._require_db()
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                """
                UPDATE wiki_sources
                SET selected_parse_revision_id = ?, selection_version = 1,
                    selected_at_ms = 1, status = 'parsed'
                WHERE id = ?
                """,
                (revision.id, second.id),
            )
    finally:
        await store.close()

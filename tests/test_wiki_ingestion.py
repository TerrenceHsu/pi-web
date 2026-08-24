"""End-to-end Raw Ingestion with built-in HTML and provider-neutral PDF."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import (
    WikiIngestionService,
    WikiSourceMimeType,
    WikiSpace,
    WikiStore,
    WikiStoreError,
)
from wiki_parser import FakeParserImage, FakeParserOutput, FakeParserProvider


async def _store(tmp_path: Path) -> tuple[WikiStore, WikiSpace]:
    job_ids: Iterator[str] = iter(
        (
            "job_000000000000000000000001",
            "job_000000000000000000000002",
        )
    )
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: next(job_ids),
    )
    return store, await store.create_space(name="Ingestion")


async def test_html_upload_parse_and_manifest_are_complete(tmp_path: Path) -> None:
    store, space = await _store(tmp_path)
    service = WikiIngestionService(store)
    try:
        source = await service.upload_source(
            space.id,
            display_name="guide.html",
            mime_type="text/html",
            content=(
                b"<h1>Guide</h1><script>bad()</script>"
                b'<p>Hello</p><img src="https://example.invalid/x.png">'
            ),
        )
        outcome = await service.parse_source(source.id)

        assert outcome.source.status == "parsed"
        assert outcome.job.status == "succeeded"
        revision = await store.get_selected_parse_revision(source.id)
        assert revision is not None
        assert revision.parser == "html_builtin"
        assert revision.requested_mode == "builtin"
        artifacts = await store.list_artifacts(source.id)
        assert {item.kind for item in artifacts} == {
            "parsed_markdown",
            "page_markdown",
            "manifest",
        }
        markdown = store.file_store.read_owned_file(
            source.space_id,
            revision.parsed_markdown_relpath,
            max_bytes=1024,
        ).decode()
        assert "# Guide" in markdown
        assert "bad" not in markdown
        manifest = json.loads(
            store.file_store.read_owned_file(
                source.space_id,
                revision.manifest_relpath,
                max_bytes=1024 * 1024,
            )
        )
        assert manifest["schema"] == "llm-wiki-html-artifact/v1"
        assert manifest["parser"]["provider"] == "html_builtin"
        assert manifest["warnings"] == [
            "external_image_removed",
            "unsafe_html_removed",
        ]
        selected_path = (
            tmp_path
            / "spaces"
            / space.id
            / "raw"
            / f"guide--{source.id}"
            / "selected.json"
        )
        selected = json.loads(selected_path.read_text(encoding="utf-8"))
        assert selected["parse_revision_id"] == revision.id
        assert selected["selection_version"] == 1
    finally:
        await store.close()


async def test_pdf_fake_provider_parse_import_and_destroy(tmp_path: Path) -> None:
    store, space = await _store(tmp_path)
    png = b"\x89PNG\r\n\x1a\nimage"
    provider = FakeParserProvider(
        outputs=(
            FakeParserOutput(
                markdown="# PDF\n",
                images=(FakeParserImage(mime_type="image/png", content=png),),
            ),
        )
    )
    service = WikiIngestionService(store, pdf_provider=provider)
    try:
        source = await service.upload_source(
            space.id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=b"%PDF-1.7\npaper\n",
        )
        outcome = await service.parse_source(source.id)

        assert outcome.source.status == "parsed"
        revision = await store.get_selected_parse_revision(source.id)
        assert revision is not None
        assert revision.parser == "fake"
        assert revision.requested_mode == "fast"
        assert outcome.job.status == "succeeded"
        assert len(provider.destroyed_provider_job_ids) == 1
        assert {item.kind for item in await store.list_artifacts(source.id)} == {
            "parsed_markdown",
            "page_markdown",
            "embedded_image",
            "manifest",
        }
    finally:
        await store.close()


async def test_pdf_without_provider_persists_safe_failure(tmp_path: Path) -> None:
    store, space = await _store(tmp_path)
    service = WikiIngestionService(store)
    try:
        source = await service.upload_source(
            space.id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=b"%PDF-1.7\npaper\n",
        )
        outcome = await service.parse_source(source.id)
        assert outcome.source.status == "failed"
        assert outcome.source.safe_error_code == "invalid_configuration"
        assert outcome.job.status == "failed"
        assert outcome.job.safe_error_code == "invalid_configuration"
    finally:
        await store.close()


async def test_cancel_before_parse_persists_cancelled_state(tmp_path: Path) -> None:
    store, space = await _store(tmp_path)
    service = WikiIngestionService(store)
    signal = asyncio.Event()
    signal.set()
    try:
        source = await service.upload_source(
            space.id,
            display_name="guide.html",
            mime_type="text/html",
            content=b"<h1>Guide</h1>",
        )
        with pytest.raises(asyncio.CancelledError):
            await service.parse_source(source.id, signal=signal)
        persisted_source = await store.get_source(source.id)
        jobs_db = store._require_db()
        async with jobs_db.execute("SELECT id FROM wiki_jobs") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        persisted_job = await store.get_job(row["id"])
        assert persisted_source.status == "failed"
        assert persisted_source.safe_error_code == "cancelled"
        assert persisted_job.status == "cancelled"
    finally:
        await store.close()


async def test_upload_rejects_extension_magic_and_non_utf8(tmp_path: Path) -> None:
    store, space = await _store(tmp_path)
    service = WikiIngestionService(store)
    try:
        invalid: tuple[tuple[str, WikiSourceMimeType, bytes], ...] = (
            ("wrong.html", "application/pdf", b"%PDF-1.7\n"),
            ("wrong.pdf", "application/pdf", b"not pdf"),
            ("wrong.htm", "text/html", b"<p>x</p>"),
            ("wrong.html", "text/html", b"\xff"),
        )
        for display_name, mime_type, content in invalid:
            with pytest.raises(WikiStoreError) as exc_info:
                await service.upload_source(
                    space.id,
                    display_name=display_name,
                    mime_type=mime_type,
                    content=content,
                )
            assert exc_info.value.code == "invalid_source"
    finally:
        await store.close()

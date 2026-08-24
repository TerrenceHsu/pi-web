"""Durable Wiki ingestion recovery and concurrent claim behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pi_agent_core_py.web.wiki import WikiIngestionService, WikiStore, WikiStoreError
from pi_agent_core_py.web.wiki.worker import WikiIngestionWorkerManager
from wiki_parser import (
    FakeDualPdfParserProvider,
    FakeParserProvider,
    FakeParserScenarioV2,
)


async def test_concurrent_parse_claim_creates_exactly_one_job(tmp_path: Path) -> None:
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    try:
        space = await store.create_space(name="Claim")
        source = await store.upload_source(
            space.id,
            display_name="guide.html",
            mime_type="text/html",
            content=b"<h1>Guide</h1>",
        )
        results = await asyncio.gather(
            store.begin_parse_job(source.id),
            store.begin_parse_job(source.id),
            return_exceptions=True,
        )
        successes = [result for result in results if not isinstance(result, BaseException)]
        failures = [result for result in results if isinstance(result, WikiStoreError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].code == "source_conflict"
        db = store._require_db()
        async with db.execute("SELECT COUNT(*) FROM wiki_jobs") as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 1
    finally:
        await store.close()


async def test_worker_recovers_interrupted_parse_with_new_attempt(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    first = await WikiStore.open(
        root,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    space = await first.create_space(name="Recovery")
    source = await first.upload_source(
        space.id,
        display_name="guide.html",
        mime_type="text/html",
        content=b"<h1>Recovered</h1>",
    )
    _parsing, interrupted_job = await first.begin_parse_job(source.id)
    await first.set_job_status(interrupted_job.id, "running")
    await first.close()

    second = await WikiStore.open(
        root,
        job_id_factory=lambda: "job_000000000000000000000002",
    )
    worker = WikiIngestionWorkerManager(
        store=second,
        service=WikiIngestionService(second),
    )
    try:
        await worker.start()
        for _ in range(200):
            recovered = await second.get_source(source.id)
            if recovered.status == "parsed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("recovered parse did not finish")

        old_job = await second.get_job(interrupted_job.id)
        new_job = await second.get_job("job_000000000000000000000002")
        assert old_job.status == "failed"
        assert old_job.safe_error_code == "provider_unavailable"
        assert old_job.attempt == 1
        assert new_job.status == "succeeded"
        assert new_job.attempt == 2
    finally:
        await worker.stop()
        await second.close()


async def test_worker_recovery_preserves_v2_accurate_mode(tmp_path: Path) -> None:
    root = tmp_path / "wiki-v2"
    first = await WikiStore.open(
        root,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    space = await first.create_space(name="Recovery v2")
    source = await first.upload_source(
        space.id,
        display_name="paper.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.7\nrecovery\n%%EOF\n",
    )
    _parsing, interrupted_job = await first.begin_parse_job(
        source.id,
        requested_mode="accurate",
    )
    await first.set_job_status(interrupted_job.id, "running")
    await first.close()

    second = await WikiStore.open(
        root,
        job_id_factory=lambda: "job_000000000000000000000002",
    )
    provider = FakeDualPdfParserProvider(
        scenarios=(FakeParserScenarioV2(preflight_profile="scanned"),)
    )
    worker = WikiIngestionWorkerManager(
        store=second,
        service=WikiIngestionService(second, pdf_provider_v2=provider),
    )
    try:
        await worker.start()
        for _ in range(200):
            recovered = await second.get_source(source.id)
            if recovered.status == "parsed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("v2 recovered parse did not finish")

        assert len(provider.created_specs) == 1
        assert provider.created_specs[0].requested_mode == "accurate"
        old_job = await second.get_job(interrupted_job.id)
        new_job = await second.get_job("job_000000000000000000000002")
        assert old_job.status == "failed"
        assert new_job.status == "succeeded"
        assert new_job.requested_mode == "accurate"
    finally:
        await worker.stop()
        await second.close()


async def test_worker_recovery_skips_mode_unsupported_by_reconfigured_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "provider-switch"
    first = await WikiStore.open(
        root,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: "job_000000000000000000000001",
    )
    space = await first.create_space(name="Provider switch")
    source = await first.upload_source(
        space.id,
        display_name="paper.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.7\nprovider switch\n%%EOF\n",
    )
    _parsing, old_job = await first.begin_parse_job(
        source.id,
        requested_mode="accurate",
    )
    await first.set_job_status(old_job.id, "running")
    await first.close()

    second = await WikiStore.open(root)
    worker = WikiIngestionWorkerManager(
        store=second,
        service=WikiIngestionService(second, pdf_provider=FakeParserProvider()),
    )
    try:
        await worker.start()
        assert worker.running
        recovered = await second.get_source(source.id)
        assert recovered.status == "failed"
        assert recovered.safe_error_code == "provider_unavailable"
        jobs = await second.list_parse_jobs(source.id)
        assert len(jobs) == 1
        assert jobs[0].status == "failed"
        assert jobs[0].requested_mode == "accurate"
    finally:
        await worker.stop()
        await second.close()

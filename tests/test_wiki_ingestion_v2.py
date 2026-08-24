"""Contract v2 ingestion, evidence persistence and revision publication."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import WikiIngestionService, WikiStore, WikiStoreError
from wiki_parser import (
    FakeDualPdfParserProvider,
    FakeParserAssetV2,
    FakeParserDocumentV2,
    FakeParserOutput,
    FakeParserProvider,
    FakeParserScenarioV2,
    ParserArtifactManifestV2,
)

_PDF = b"%PDF-1.7\ncontract-v2 source\n%%EOF\n"
_PNG = b"\x89PNG\r\n\x1a\n" + b"valid-fake-image"


async def _store(tmp_path: Path) -> tuple[WikiStore, str]:
    job_ids: Iterator[str] = iter(
        (
            "job_000000000000000000000001",
            "job_000000000000000000000002",
            "job_000000000000000000000003",
        )
    )
    store = await WikiStore.open(
        tmp_path,
        id_factory=lambda: "space_000000000000000000000001",
        source_id_factory=lambda: "source_000000000000000000000001",
        job_id_factory=lambda: next(job_ids),
    )
    space = await store.create_space(name="Contract v2")
    return store, space.id


@pytest.mark.asyncio
async def test_v2_auto_fallback_atomically_publishes_full_evidence(tmp_path: Path) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                fast_outcome="quality_rejected",
                accurate_document=FakeParserDocumentV2(
                    pages=("# Docling page 1\n", "Docling page 2\n"),
                    assets=(
                        FakeParserAssetV2(
                            kind="table_image",
                            content=_PNG,
                            page_number=2,
                        ),
                    ),
                    warnings=("table_normalized",),
                ),
                fast_document=FakeParserDocumentV2(
                    pages=("Low quality\n", "Low quality again\n"),
                ),
            ),
        )
    )
    service = WikiIngestionService(store, pdf_provider_v2=provider)
    try:
        source = await service.upload_source(
            space_id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=_PDF,
        )
        outcome = await service.parse_source(source.id, requested_mode="auto")

        assert outcome.source.status == "parsed"
        assert outcome.source.selection_version == 1
        assert outcome.job.status == "succeeded"
        assert outcome.job.requested_mode == "auto"
        revision = await store.get_selected_parse_revision(source.id)
        assert revision is not None
        assert revision.contract_version == 2
        assert revision.artifact_schema == "llm-wiki-parser-artifact/v2"
        assert revision.parser == "docling"
        assert revision.preset == "docling_standard"
        assert revision.routing_config_revision == "fake_routing_v1"
        assert revision.page_count == 2

        attempts = await store.list_parse_attempts(outcome.job.id)
        assert [(item.parser, item.state) for item in attempts] == [
            ("pymupdf4llm", "quality_rejected"),
            ("docling", "succeeded"),
        ]
        assert attempts[0].id.startswith("parse_attempt_")
        assert attempts[0].provider_attempt_id.startswith("attempt-1-")
        assert attempts[1].fallback_from_attempt_id == attempts[0].id
        assert attempts[1].provider_attempt_id.startswith("attempt-2-")
        assert json.loads(attempts[0].quality_report_json)["passed"] is False
        assert json.loads(attempts[1].quality_report_json)["passed"] is True
        assert json.loads(attempts[1].route_reasons_json) == ["fast_quality_fallback"]

        artifacts = await store.list_artifacts(source.id)
        assert {item.kind for item in artifacts} == {
            "parsed_markdown",
            "page_markdown",
            "table_image",
            "manifest",
        }
        assert len([item for item in artifacts if item.kind == "page_markdown"]) == 2
        manifest_artifact = next(item for item in artifacts if item.kind == "manifest")
        manifest = ParserArtifactManifestV2.model_validate_json(
            await store.read_artifact_content(outcome.source, manifest_artifact)
        )
        assert manifest.attempts[0].attempt_id == attempts[0].provider_attempt_id
        assert manifest.attempts[1].attempt_id == attempts[1].provider_attempt_id
        assert manifest.route_decision.fallback_parser == "docling"
        assert provider.destroyed_provider_job_ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v2_explicit_fast_rejection_persists_attempt_without_revision(
    tmp_path: Path,
) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeDualPdfParserProvider(
        scenarios=(FakeParserScenarioV2(fast_outcome="quality_rejected"),)
    )
    service = WikiIngestionService(store, pdf_provider_v2=provider)
    try:
        source = await service.upload_source(
            space_id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=_PDF,
        )
        outcome = await service.parse_source(source.id, requested_mode="fast")

        assert outcome.source.status == "failed"
        assert outcome.source.safe_error_code == "quality_rejected"
        assert outcome.job.status == "failed"
        assert outcome.job.requested_mode == "fast"
        attempts = await store.list_parse_attempts(outcome.job.id)
        assert len(attempts) == 1
        assert attempts[0].state == "quality_rejected"
        assert json.loads(attempts[0].quality_report_json)["passed"] is False
        assert await store.get_selected_parse_revision(source.id) is None
        assert await store.list_parse_revisions(source.id) == ()
        assert await store.list_artifacts(source.id) == ()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v2_invalid_image_rejects_revision_but_keeps_successful_attempt_evidence(
    tmp_path: Path,
) -> None:
    store, space_id = await _store(tmp_path)
    document = FakeParserDocumentV2(
        assets=(FakeParserAssetV2(mime_type="image/png", content=b"not-a-png"),),
    )
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                fast_document=document,
                accurate_document=document,
            ),
        )
    )
    service = WikiIngestionService(store, pdf_provider_v2=provider)
    try:
        source = await service.upload_source(
            space_id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=_PDF,
        )
        outcome = await service.parse_source(source.id, requested_mode="auto")

        assert outcome.source.status == "failed"
        assert outcome.source.safe_error_code == "artifact_invalid"
        attempts = await store.list_parse_attempts(outcome.job.id)
        assert len(attempts) == 1
        assert attempts[0].state == "succeeded"
        assert attempts[0].output_sha256
        assert await store.list_parse_revisions(source.id) == ()
        assert await store.list_artifacts(source.id) == ()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v2_reparse_honors_accurate_mode_and_preserves_previous_revision(
    tmp_path: Path,
) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(),
            FakeParserScenarioV2(preflight_profile="scanned"),
        )
    )
    service = WikiIngestionService(store, pdf_provider_v2=provider)
    try:
        source = await service.upload_source(
            space_id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=_PDF,
        )
        first = await service.parse_source(source.id, requested_mode="auto")
        second = await service.parse_source(source.id, requested_mode="accurate")

        assert first.source.selection_version == 1
        assert second.source.selection_version == 2
        assert second.job.requested_mode == "accurate"
        revisions = await store.list_parse_revisions(source.id)
        assert len(revisions) == 2
        assert revisions[0].parser == "pymupdf4llm"
        assert revisions[1].parser == "docling"
        assert second.source.selected_parse_revision_id == revisions[1].id
        assert len(provider.created_specs) == 2
        assert provider.created_specs[1].requested_mode == "accurate"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_contract_modes_are_explicitly_isolated(tmp_path: Path) -> None:
    store, space_id = await _store(tmp_path)
    source = await store.upload_source(
        space_id,
        display_name="paper.pdf",
        mime_type="application/pdf",
        content=_PDF,
    )
    v1 = WikiIngestionService(
        store,
        pdf_provider=FakeParserProvider(outputs=(FakeParserOutput(),)),
    )
    v2 = WikiIngestionService(store, pdf_provider_v2=FakeDualPdfParserProvider())
    try:
        with pytest.raises(WikiStoreError) as v1_error:
            v1.resolve_parse_mode(source, "accurate")
        assert v1_error.value.code == "unsupported_parse_mode"
        with pytest.raises(WikiStoreError) as v2_error:
            v2.resolve_parse_mode(source, "builtin")
        assert v2_error.value.code == "unsupported_parse_mode"
        with pytest.raises(ValueError, match="exactly one"):
            WikiIngestionService(
                store,
                pdf_provider=FakeParserProvider(),
                pdf_provider_v2=FakeDualPdfParserProvider(),
            )
    finally:
        await store.close()

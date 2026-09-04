"""Contract v2 ingestion, evidence persistence and revision publication."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from pi_agent_core_py.web.wiki import WikiIngestionService, WikiStore, WikiStoreError
from wiki_parser import (
    FakeMineruParserProvider,
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
async def test_v2_mineru_profile_atomically_publishes_full_evidence(tmp_path: Path) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeMineruParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                document=FakeParserDocumentV2(
                    pages=("# MinerU page 1\n", "MinerU page 2\n"),
                    assets=(
                        FakeParserAssetV2(
                            kind="table_image",
                            content=_PNG,
                            page_number=2,
                        ),
                    ),
                    warnings=("table_normalized",),
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
        outcome = await service.parse_source(source.id, requested_mode="gpu-high")

        assert outcome.source.status == "parsed"
        assert outcome.source.selection_version == 1
        assert outcome.job.status == "succeeded"
        assert outcome.job.requested_mode == "gpu-high"
        revision = await store.get_selected_parse_revision(source.id)
        assert revision is not None
        assert revision.contract_version == 2
        assert revision.artifact_schema == "llm-wiki-parser-artifact/v2"
        assert revision.parser == "mineru"
        assert revision.preset == "mineru_gpu_high"
        assert revision.routing_config_revision == "fake_routing_v1"
        assert revision.page_count == 2

        attempts = await store.list_parse_attempts(outcome.job.id)
        assert [(item.parser, item.state) for item in attempts] == [
            ("mineru", "succeeded"),
        ]
        assert attempts[0].id.startswith("parse_attempt_")
        assert attempts[0].provider_attempt_id.startswith("attempt-1-")
        assert attempts[0].fallback_from_attempt_id is None
        assert json.loads(attempts[0].quality_report_json)["passed"] is True
        assert json.loads(attempts[0].route_reasons_json) == ["explicit_gpu_high"]

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
        assert manifest.route_decision.fallback_parser is None
        assert provider.destroyed_provider_job_ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v2_quality_rejection_persists_attempt_without_revision(
    tmp_path: Path,
) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeMineruParserProvider(
        scenarios=(FakeParserScenarioV2(outcome="quality_rejected"),)
    )
    service = WikiIngestionService(store, pdf_provider_v2=provider)
    try:
        source = await service.upload_source(
            space_id,
            display_name="paper.pdf",
            mime_type="application/pdf",
            content=_PDF,
        )
        outcome = await service.parse_source(source.id, requested_mode="pipeline")

        assert outcome.source.status == "failed"
        assert outcome.source.safe_error_code == "quality_rejected"
        assert outcome.job.status == "failed"
        assert outcome.job.requested_mode == "pipeline"
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
    provider = FakeMineruParserProvider(
        scenarios=(
            FakeParserScenarioV2(document=document),
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
        outcome = await service.parse_source(source.id, requested_mode="pipeline")

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
async def test_v2_reparse_honors_gpu_profile_and_preserves_previous_revision(
    tmp_path: Path,
) -> None:
    store, space_id = await _store(tmp_path)
    provider = FakeMineruParserProvider(
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
        first = await service.parse_source(source.id, requested_mode="pipeline")
        second = await service.parse_source(source.id, requested_mode="gpu-medium")

        assert first.source.selection_version == 1
        assert second.source.selection_version == 2
        assert second.job.requested_mode == "gpu-medium"
        revisions = await store.list_parse_revisions(source.id)
        assert len(revisions) == 2
        assert revisions[0].preset == "mineru_pipeline"
        assert revisions[1].preset == "mineru_gpu_medium"
        assert second.source.selected_parse_revision_id == revisions[1].id
        assert len(provider.created_specs) == 2
        assert provider.created_specs[1].requested_mode == "gpu-medium"
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
    v2 = WikiIngestionService(store, pdf_provider_v2=FakeMineruParserProvider())
    try:
        with pytest.raises(WikiStoreError) as v1_error:
            v1.resolve_parse_mode(source, "gpu-high")
        assert v1_error.value.code == "unsupported_parse_mode"
        with pytest.raises(WikiStoreError) as v2_error:
            v2.resolve_parse_mode(source, "builtin")
        assert v2_error.value.code == "unsupported_parse_mode"
        with pytest.raises(ValueError, match="exactly one"):
            WikiIngestionService(
                store,
                pdf_provider=FakeParserProvider(),
                pdf_provider_v2=FakeMineruParserProvider(),
            )
    finally:
        await store.close()

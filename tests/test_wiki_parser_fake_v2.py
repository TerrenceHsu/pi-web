"""Offline lifecycle and routing tests for the dual-PDF Contract v2 fake."""

from __future__ import annotations

import asyncio
import hashlib
import tarfile
from pathlib import Path

import pytest

from wiki_parser import (
    FakeDualPdfParserProvider,
    FakeParserAssetV2,
    FakeParserDocumentV2,
    FakeParserScenarioV2,
    ParserArtifactManifestV2,
    ParserError,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserProviderV2,
    ParserRoutingConfigIdentity,
    ParserSourceSpec,
)

_PDF_BYTES = b"%PDF-1.7\n% completely offline fake source\n%%EOF\n"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"offline-fake-image"


async def _job(
    tmp_path: Path,
    provider: FakeDualPdfParserProvider,
    *,
    mode: str = "auto",
    source_bytes: bytes = _PDF_BYTES,
) -> tuple[Path, ParserJobHandleV2, ParserJobSpecV2]:
    source_path = (tmp_path / "source.pdf").resolve()
    source_path.write_bytes(source_bytes)
    probe = await provider.probe()
    spec = ParserJobSpecV2(
        job_id="wiki-job-1",
        source=ParserSourceSpec(
            source_id="wiki-source-1",
            display_name="source.pdf",
            size_bytes=len(source_bytes),
            sha256=hashlib.sha256(source_bytes).hexdigest(),
        ),
        requested_mode=mode,
        routing_config=probe.routing_config,
    )
    handle = await provider.create_job(spec, source_path=source_path)
    return source_path, handle, spec


def _manifest(archive_path: Path) -> tuple[ParserArtifactManifestV2, set[str]]:
    with tarfile.open(archive_path, mode="r:") as archive:
        members = {member.name for member in archive.getmembers()}
        stream = archive.extractfile("manifest.json")
        assert stream is not None
        manifest = ParserArtifactManifestV2.model_validate_json(stream.read())
    return manifest, members


@pytest.mark.asyncio
async def test_fake_v2_probe_is_offline_and_satisfies_provider_protocol(
    tmp_path: Path,
) -> None:
    provider = FakeDualPdfParserProvider(clock_ms=lambda: 100)

    assert isinstance(provider, ParserProviderV2)
    probe = await provider.probe()
    assert probe.provider == "fake_dual_pdf"
    assert probe.license_mode == "not_required"
    assert probe.capabilities.network_during_job is False
    assert probe.routing_config.revision == "fake_routing_v1"

    source_path = (tmp_path / "source.pdf").resolve()
    source_path.write_bytes(_PDF_BYTES)
    wrong_config = ParserRoutingConfigIdentity(
        revision="wrong_config",
        sha256="0" * 64,
    )
    spec = ParserJobSpecV2(
        job_id="wrong-config-job",
        source=ParserSourceSpec(
            source_id="wiki-source-1",
            display_name="source.pdf",
            size_bytes=len(_PDF_BYTES),
            sha256=hashlib.sha256(_PDF_BYTES).hexdigest(),
        ),
        routing_config=wrong_config,
    )
    with pytest.raises(ParserError) as raised:
        await provider.create_job(spec, source_path=source_path)
    assert raised.value.code == "invalid_configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "profile", "expected_preset", "expected_reason"),
    [
        ("accurate", "scanned", "docling_ocr", "explicit_accurate"),
        ("auto", "scanned", "docling_ocr", "scan_text_layer_missing"),
        ("auto", "complex_table", "docling_standard", "complex_table_dense"),
    ],
)
async def test_accurate_and_complex_routes_use_one_direct_docling_attempt(
    tmp_path: Path,
    mode: str,
    profile: str,
    expected_preset: str,
    expected_reason: str,
) -> None:
    scenario = FakeParserScenarioV2(
        preflight_profile=profile,
        accurate_document=FakeParserDocumentV2(
            pages=("# Docling page one\n", "Docling page two\n"),
        ),
        fast_document=FakeParserDocumentV2(
            pages=("# Fast page one\n", "Fast page two\n"),
        ),
    )
    provider = FakeDualPdfParserProvider(scenarios=(scenario,))
    _source, handle, _spec = await _job(tmp_path, provider, mode=mode)

    status = await provider.wait(handle)

    assert status.state == "succeeded"
    assert len(status.attempts) == 1
    assert status.attempts[0].parser == "docling"
    assert status.attempts[0].preset == expected_preset
    assert status.route_decision is not None
    assert status.route_decision.reasons == (expected_reason,)
    assert status.route_decision.fallback_parser is None


@pytest.mark.asyncio
async def test_auto_simple_fast_success_does_not_run_declared_fallback(
    tmp_path: Path,
) -> None:
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                fast_document=FakeParserDocumentV2(
                    pages=("# Fast selected\n", "Second fast page\n"),
                    assets=(
                        FakeParserAssetV2(
                            content=_PNG_BYTES,
                            page_number=1,
                        ),
                    ),
                ),
                accurate_document=FakeParserDocumentV2(
                    pages=("# Must not run\n", "Unused\n"),
                ),
            ),
        )
    )
    _source, handle, _spec = await _job(tmp_path, provider)

    status = await provider.wait(handle)
    assert status.state == "succeeded"
    assert status.route_decision is not None
    assert status.route_decision.fallback_parser == "docling"
    assert [(item.parser, item.state) for item in status.attempts] == [
        ("pymupdf4llm", "succeeded")
    ]

    archive_path = tmp_path / "fast-artifact.tar"
    receipt = await provider.download_artifact(
        handle,
        local_path=archive_path,
        expected_sha256=status.artifact_sha256,
    )
    manifest, members = _manifest(archive_path)
    assert receipt.file_count == 5
    assert manifest.parser == "pymupdf4llm"
    assert manifest.selected_attempt_id == status.attempts[0].attempt_id
    assert [page.path for page in manifest.pages] == [
        "pages/000001.md",
        "pages/000002.md",
    ]
    assert {"manifest.json", "parsed.md", "pages/000001.md", "pages/000002.md"} < members
    assert len(manifest.assets) == 1


@pytest.mark.asyncio
async def test_auto_quality_rejection_falls_back_once_using_original_source_snapshot(
    tmp_path: Path,
) -> None:
    original_sha = hashlib.sha256(_PDF_BYTES).hexdigest()
    provider = FakeDualPdfParserProvider(
        id_factory=lambda: "fallback-provider-job",
        scenarios=(
            FakeParserScenarioV2(
                fast_outcome="quality_rejected",
                accurate_outcome="succeeded",
                fast_document=FakeParserDocumentV2(pages=("Low quality fast\n",)),
                accurate_document=FakeParserDocumentV2(pages=("# Selected Docling\n",)),
            ),
        ),
    )
    source_path, handle, _spec = await _job(tmp_path, provider)
    source_path.write_bytes(b"%PDF-1.7\nchanged after upload\n%%EOF\n")

    status = await provider.wait(handle)

    assert status.state == "succeeded"
    assert [(item.parser, item.state) for item in status.attempts] == [
        ("pymupdf4llm", "quality_rejected"),
        ("docling", "succeeded"),
    ]
    first, second = status.attempts
    assert first.source_sha256 == second.source_sha256 == original_sha
    assert first.output_sha256 is None
    assert second.fallback_from_attempt_id == first.attempt_id
    assert second.route_reasons == ("fast_quality_fallback",)

    archive_path = tmp_path / "fallback-artifact.tar"
    await provider.download_artifact(
        handle,
        local_path=archive_path,
        expected_sha256=status.artifact_sha256,
    )
    manifest, _members = _manifest(archive_path)
    assert manifest.source_sha256 == original_sha
    assert manifest.parser == "docling"
    assert manifest.attempts == status.attempts
    with tarfile.open(archive_path, mode="r:") as archive:
        parsed = archive.extractfile("parsed.md")
        assert parsed is not None
        selected_markdown = parsed.read().decode("utf-8")
    assert "Selected Docling" in selected_markdown
    assert "Low quality fast" not in selected_markdown


@pytest.mark.asyncio
async def test_explicit_fast_quality_rejection_is_terminal_without_fallback(
    tmp_path: Path,
) -> None:
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                fast_outcome="quality_rejected",
                accurate_outcome="succeeded",
            ),
        )
    )
    _source, handle, _spec = await _job(tmp_path, provider, mode="fast")

    status = await provider.wait(handle)

    assert status.state == "failed"
    assert status.safe_error_code == "quality_rejected"
    assert len(status.attempts) == 1
    assert status.attempts[0].parser == "pymupdf4llm"
    assert status.attempts[0].state == "quality_rejected"
    assert status.artifact_sha256 is None
    with pytest.raises(ParserError) as raised:
        await provider.download_artifact(handle, local_path=tmp_path / "absent.tar")
    assert raised.value.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_docling_failure_after_fallback_terminates_and_never_loops(
    tmp_path: Path,
) -> None:
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                fast_outcome="quality_rejected",
                accurate_outcome="failed",
            ),
        )
    )
    _source, handle, _spec = await _job(tmp_path, provider)

    first_status = await provider.wait(handle)
    second_status = await provider.wait(handle)

    assert second_status.observed_at_ms >= first_status.observed_at_ms
    assert second_status.state == first_status.state
    assert second_status.phase == first_status.phase
    assert second_status.attempts == first_status.attempts
    assert second_status.safe_error_code == first_status.safe_error_code
    assert first_status.state == "failed"
    assert first_status.safe_error_code == "parsing_failed"
    assert [(item.parser, item.state) for item in first_status.attempts] == [
        ("pymupdf4llm", "quality_rejected"),
        ("docling", "failed"),
    ]
    assert first_status.attempts[1].fallback_from_attempt_id == (
        first_status.attempts[0].attempt_id
    )


@pytest.mark.asyncio
async def test_wait_cancellation_and_destroy_are_idempotent(tmp_path: Path) -> None:
    gate = asyncio.Event()
    signal = asyncio.Event()
    provider = FakeDualPdfParserProvider(completion_gate=gate)
    _source, handle, _spec = await _job(tmp_path, provider)

    wait_task = asyncio.create_task(provider.wait(handle, signal=signal))
    await asyncio.sleep(0)
    signal.set()
    status = await wait_task

    assert status.state == "cancelled"
    assert status.safe_error_code == "cancelled"
    assert status.attempts == ()
    assert await provider.cancel(handle) == status
    await provider.destroy(handle)
    await provider.destroy(handle)
    destroyed = await provider.status(handle)
    assert destroyed.state == "destroyed"
    assert provider.destroyed_provider_job_ids == [handle.provider_job_id]


def test_fake_v2_repr_hides_document_and_image_content() -> None:
    document = FakeParserDocumentV2(
        pages=("PRIVATE-DOCUMENT-TEXT",),
        assets=(FakeParserAssetV2(content=b"PRIVATE-IMAGE-BYTES"),),
    )
    scenario = FakeParserScenarioV2(
        fast_document=document,
        accurate_document=document,
    )

    assert "PRIVATE-DOCUMENT-TEXT" not in repr(document)
    assert "PRIVATE-IMAGE-BYTES" not in repr(document)
    assert "PRIVATE-DOCUMENT-TEXT" not in repr(scenario)
    assert "PRIVATE-IMAGE-BYTES" not in repr(scenario)

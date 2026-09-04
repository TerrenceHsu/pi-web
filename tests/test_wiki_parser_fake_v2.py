"""Offline lifecycle tests for the MinerU Contract v2 fake."""

from __future__ import annotations

import asyncio
import hashlib
import tarfile
from pathlib import Path

import pytest

from wiki_parser import (
    FakeMineruParserProvider,
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

_PDF = b"%PDF-1.7\n% offline MinerU fake\n%%EOF\n"
_PNG = b"\x89PNG\r\n\x1a\n" + b"offline-image"


async def _job(
    tmp_path: Path,
    provider: FakeMineruParserProvider,
    *,
    mode: str = "pipeline",
) -> tuple[Path, ParserJobHandleV2]:
    path = (tmp_path / "source.pdf").resolve()
    path.write_bytes(_PDF)
    probe = await provider.probe()
    spec = ParserJobSpecV2.model_validate(
        {
            "job_id": "wiki-job-1",
            "source": {
                "source_id": "wiki-source-1",
                "display_name": "source.pdf",
                "size_bytes": len(_PDF),
                "sha256": hashlib.sha256(_PDF).hexdigest(),
            },
            "requested_mode": mode,
            "routing_config": probe.routing_config.model_dump(mode="json"),
        }
    )
    return path, await provider.create_job(spec, source_path=path)


@pytest.mark.asyncio
async def test_fake_probe_is_offline_and_rejects_wrong_config(tmp_path: Path) -> None:
    provider = FakeMineruParserProvider(clock_ms=lambda: 100)
    assert isinstance(provider, ParserProviderV2)
    probe = await provider.probe()
    assert probe.provider == "fake_mineru"
    assert probe.capabilities.network_during_job is False

    path = tmp_path / "source.pdf"
    path.write_bytes(_PDF)
    wrong = ParserJobSpecV2(
        job_id="wrong-job",
        source=ParserSourceSpec(
            source_id="wiki-source-1",
            display_name="source.pdf",
            size_bytes=len(_PDF),
            sha256=hashlib.sha256(_PDF).hexdigest(),
        ),
        routing_config=ParserRoutingConfigIdentity(
            revision="wrong_config",
            sha256="0" * 64,
        ),
    )
    with pytest.raises(ParserError) as raised:
        await provider.create_job(wrong, source_path=path)
    assert raised.value.code == "invalid_configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "preset", "reason"),
    [
        ("pipeline", "mineru_pipeline", "explicit_pipeline"),
        ("gpu-medium", "mineru_gpu_medium", "explicit_gpu_medium"),
        ("gpu-high", "mineru_gpu_high", "explicit_gpu_high"),
    ],
)
async def test_profiles_produce_one_mineru_attempt_and_artifact(
    tmp_path: Path,
    mode: str,
    preset: str,
    reason: str,
) -> None:
    provider = FakeMineruParserProvider(
        scenarios=(
            FakeParserScenarioV2(
                document=FakeParserDocumentV2(
                    pages=("# Page one\n", "Page two\n"),
                    assets=(FakeParserAssetV2(content=_PNG, page_number=1),),
                )
            ),
        )
    )
    _source, handle = await _job(tmp_path, provider, mode=mode)
    status = await provider.wait(handle)

    assert status.state == "succeeded"
    assert status.route_decision is not None
    assert status.route_decision.initial_preset == preset
    assert status.route_decision.reasons == (reason,)
    assert status.route_decision.fallback_parser is None
    assert [(item.parser, item.preset, item.state) for item in status.attempts] == [
        ("mineru", preset, "succeeded")
    ]

    archive_path = tmp_path / f"{mode}.tar"
    receipt = await provider.download_artifact(
        handle,
        local_path=archive_path,
        expected_sha256=status.artifact_sha256,
    )
    with tarfile.open(archive_path, mode="r:") as archive:
        stream = archive.extractfile("manifest.json")
        assert stream is not None
        manifest = ParserArtifactManifestV2.model_validate_json(stream.read())
    assert receipt.file_count == 5
    assert manifest.parser == "mineru"
    assert manifest.requested_mode == mode
    assert len(manifest.attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "error", "attempt_state"),
    [
        ("quality_rejected", "quality_rejected", "quality_rejected"),
        ("failed", "parsing_failed", "failed"),
    ],
)
async def test_failure_is_terminal_without_fallback(
    tmp_path: Path,
    outcome: str,
    error: str,
    attempt_state: str,
) -> None:
    provider = FakeMineruParserProvider(
        scenarios=(FakeParserScenarioV2.model_validate({"outcome": outcome}),)
    )
    _source, handle = await _job(tmp_path, provider)

    first = await provider.wait(handle)
    second = await provider.wait(handle)

    assert first.state == second.state == "failed"
    assert first.safe_error_code == second.safe_error_code == error
    assert len(first.attempts) == 1
    assert first.attempts[0].state == attempt_state
    assert first.attempts[0].fallback_from_attempt_id is None
    with pytest.raises(ParserError) as raised:
        await provider.download_artifact(handle, local_path=tmp_path / "absent.tar")
    assert raised.value.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_cancel_and_destroy_are_idempotent(tmp_path: Path) -> None:
    gate = asyncio.Event()
    signal = asyncio.Event()
    provider = FakeMineruParserProvider(completion_gate=gate)
    _source, handle = await _job(tmp_path, provider)
    waiting = asyncio.create_task(provider.wait(handle, signal=signal))
    await asyncio.sleep(0)
    signal.set()

    assert (await waiting).state == "cancelled"
    assert (await provider.cancel(handle)).state == "cancelled"
    await provider.destroy(handle)
    await provider.destroy(handle)
    assert (await provider.status(handle)).state == "destroyed"


def test_fake_repr_hides_document_and_image_content() -> None:
    document = FakeParserDocumentV2(
        pages=("PRIVATE-DOCUMENT-TEXT",),
        assets=(FakeParserAssetV2(content=b"PRIVATE-IMAGE-BYTES"),),
    )
    scenario = FakeParserScenarioV2(document=document)

    assert "PRIVATE-DOCUMENT-TEXT" not in repr(document)
    assert "PRIVATE-IMAGE-BYTES" not in repr(document)
    assert "PRIVATE-DOCUMENT-TEXT" not in repr(scenario)

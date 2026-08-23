"""Offline contract tests for the isolated LLM Wiki parser package."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from wiki_parser import (
    PARSER_ARTIFACT_SCHEMA,
    FakeParserImage,
    FakeParserOutput,
    FakeParserProvider,
    ParserArtifactFile,
    ParserArtifactManifest,
    ParserError,
    ParserJobHandle,
    ParserJobSpec,
    ParserJobStatus,
    ParserLimits,
    ParserProbe,
    ParserProvider,
    ParserSourceSpec,
    validate_artifact_path,
)


def _source_and_spec(
    tmp_path: Path,
    *,
    data: bytes = b"%PDF-1.7\ncontract-test\n%%EOF\n",
    job_id: str = "parse_job_1",
    source_id: str = "source_1",
    limits: ParserLimits | None = None,
) -> tuple[Path, ParserJobSpec]:
    source_path = tmp_path / f"{source_id}.pdf"
    source_path.write_bytes(data)
    source = ParserSourceSpec(
        source_id=source_id,
        display_name="document.pdf",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return source_path, ParserJobSpec(
        job_id=job_id,
        source=source,
        limits=limits or ParserLimits(),
    )


def test_contract_is_fixed_to_pdf_markdown_images_and_fast_no_ocr() -> None:
    source = ParserSourceSpec(
        source_id="source_1",
        display_name="report.pdf",
        size_bytes=10,
        sha256="a" * 64,
    )
    spec = ParserJobSpec(job_id="job_1", source=source)

    assert spec.mode == "fast_no_ocr"
    assert spec.output_schema == PARSER_ARTIFACT_SCHEMA
    assert spec.extract_embedded_images is True
    with pytest.raises(ValidationError):
        ParserJobSpec.model_validate(
            {**spec.model_dump(), "mode": "balanced"}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ParserJobSpec.model_validate(
            {**spec.model_dump(), "use_llm": True}
        )
    with pytest.raises(ValidationError):
        ParserSourceSpec(
            source_id="source_2",
            display_name="page.html",
            mime_type="text/html",
            size_bytes=10,
            sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "value",
    (
        "",
        "/parsed.md",
        "../parsed.md",
        "images/../../secret",
        r"images\image.png",
        "images//image.png",
        "images/./image.png",
        "images/evil\x00.png",
    ),
)
def test_artifact_paths_reject_escape_and_unnormalized_input(value: str) -> None:
    with pytest.raises(ValueError):
        validate_artifact_path(value)


def test_artifact_manifest_enforces_fixed_files_and_unique_paths() -> None:
    markdown = ParserArtifactFile(
        path="parsed.md",
        kind="markdown",
        mime_type="text/markdown",
        size_bytes=4,
        sha256="a" * 64,
    )
    image = ParserArtifactFile(
        path="images/img_0000_aaaaaaaaaaaaaaaa.png",
        kind="embedded_image",
        mime_type="image/png",
        size_bytes=4,
        sha256="b" * 64,
        page_number=1,
    )
    manifest = ParserArtifactManifest(
        job_id="job_1",
        source_id="source_1",
        source_sha256="c" * 64,
        provider="fake",
        provider_version="fake-1",
        page_count=1,
        markdown=markdown,
        images=(image,),
    )

    assert manifest.schema_id == PARSER_ARTIFACT_SCHEMA
    assert manifest.model_dump(by_alias=True)["schema"] == PARSER_ARTIFACT_SCHEMA
    with pytest.raises(ValidationError, match="suffix"):
        ParserArtifactFile(
            path="images/image.jpg",
            kind="embedded_image",
            mime_type="image/png",
            size_bytes=4,
            sha256="d" * 64,
        )

    case_variant = image.model_copy(update={"path": "images/IMG_0000_AAAAAAAAAAAAAAAA.png"})
    with pytest.raises(ValidationError, match="unique"):
        ParserArtifactManifest(
            job_id="job_1",
            source_id="source_1",
            source_sha256="c" * 64,
            provider="fake",
            provider_version="fake-1",
            page_count=1,
            markdown=markdown,
            images=(image, case_variant),
        )


def test_probe_requires_explicit_marker_license_mode() -> None:
    with pytest.raises(ValidationError, match="explicit license mode"):
        ParserProbe(
            provider="marker_sidecar",
            available=True,
            provider_version="2.0.0",
            license_mode="not_required",
            observed_at_ms=1,
        )
    with pytest.raises(ValidationError, match="requires an error code"):
        ParserProbe(
            provider="marker_sidecar",
            available=False,
            provider_version="2.0.0",
            license_mode="unconfigured",
            observed_at_ms=1,
        )


def test_parser_error_has_fixed_text_and_retry_classification() -> None:
    license_error = ParserError("license_not_configured", provider="marker_sidecar")
    unavailable = ParserError("provider_unavailable", provider="marker_sidecar")

    assert str(license_error) == "Parser provider license eligibility is not configured."
    assert license_error.retryable is False
    assert unavailable.retryable is True
    assert "pdf" not in repr(license_error).lower()


@pytest.mark.asyncio
async def test_fake_provider_conforms_and_probe_is_offline_ready() -> None:
    provider: ParserProvider = FakeParserProvider(clock_ms=lambda: 123)

    assert isinstance(provider, ParserProvider)
    assert provider.provider_name() == "fake"
    probe = await provider.probe()
    assert probe.available is True
    assert probe.license_mode == "not_required"
    assert probe.observed_at_ms == 123
    assert probe.capabilities.network_during_job is False


@pytest.mark.asyncio
async def test_fake_provider_rejects_source_digest_mismatch(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    source_path.write_bytes(b"%PDF-1.7\ntampered\n%%EOF\n")
    provider = FakeParserProvider(id_factory=lambda: "fake-digest")

    with pytest.raises(ParserError) as exc_info:
        await provider.create_job(spec, source_path=source_path)
    assert exc_info.value.code == "invalid_source"


@pytest.mark.asyncio
async def test_fake_provider_rejects_actual_source_over_limit(tmp_path: Path) -> None:
    data = b"%PDF-1.7\n1234"
    source_path, spec = _source_and_spec(
        tmp_path,
        data=data,
        limits=ParserLimits(
            max_source_bytes=len(data),
            max_artifact_files=2,
            max_image_count=0,
        ),
    )
    source_path.write_bytes(data + b"x")
    provider = FakeParserProvider(id_factory=lambda: "fake-limit")

    with pytest.raises(ParserError) as exc_info:
        await provider.create_job(spec, source_path=source_path)
    assert exc_info.value.code == "source_too_large"


@pytest.mark.asyncio
async def test_fake_provider_full_lifecycle_builds_deterministic_valid_tar(
    tmp_path: Path,
) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    output = FakeParserOutput(
        markdown="# 标题\n\n正文。\n",
        images=(
            FakeParserImage(
                mime_type="image/png",
                content=b"fake-png-payload",
                page_number=2,
            ),
        ),
        page_count=3,
        warnings=("low_text_density",),
    )
    provider = FakeParserProvider(
        clock_ms=lambda: 1_000,
        id_factory=lambda: "fake-success",
        outputs=(output,),
    )

    handle = await provider.create_job(spec, source_path=source_path)
    assert (await provider.status(handle)).state == "queued"
    completed = await provider.wait(handle)
    assert completed.state == "succeeded"
    assert completed.artifact_sha256 is not None

    destination = tmp_path / "artifacts" / "parser-output.tar"
    receipt = await provider.download_artifact(
        handle,
        local_path=destination,
        expected_sha256=completed.artifact_sha256,
    )
    assert receipt.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert receipt.file_count == 3

    with tarfile.open(fileobj=io.BytesIO(destination.read_bytes()), mode="r:") as archive:
        names = archive.getnames()
        assert names[0:2] == ["manifest.json", "parsed.md"]
        assert names[2].startswith("images/img_0000_")
        assert archive.extractfile("parsed.md").read().decode() == output.markdown  # type: ignore[union-attr]
        manifest_file = archive.extractfile("manifest.json")
        assert manifest_file is not None
        manifest_bytes = manifest_file.read()
        manifest = json.loads(manifest_bytes)
    assert manifest["schema"] == PARSER_ARTIFACT_SCHEMA
    assert manifest["source_sha256"] == spec.source.sha256
    assert manifest["images"][0]["page_number"] == 2
    assert receipt.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()


@pytest.mark.asyncio
async def test_fake_artifact_is_deterministic_across_provider_job_ids(
    tmp_path: Path,
) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    output = FakeParserOutput(markdown="# Stable\n", page_count=1)
    first = FakeParserProvider(id_factory=lambda: "provider-job-a", outputs=(output,))
    second = FakeParserProvider(id_factory=lambda: "provider-job-b", outputs=(output,))

    first_handle = await first.create_job(spec, source_path=source_path)
    second_handle = await second.create_job(spec, source_path=source_path)
    await first.wait(first_handle)
    await second.wait(second_handle)
    first_path = tmp_path / "first.tar"
    second_path = tmp_path / "second.tar"
    await first.download_artifact(first_handle, local_path=first_path)
    await second.download_artifact(second_handle, local_path=second_path)

    assert first_path.read_bytes() == second_path.read_bytes()


@pytest.mark.asyncio
async def test_fake_provider_honors_pre_cancelled_signal(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    provider = FakeParserProvider(id_factory=lambda: "fake-pre-cancel")
    handle = await provider.create_job(spec, source_path=source_path)
    signal = asyncio.Event()
    signal.set()

    status = await provider.wait(handle, signal=signal)

    assert status.state == "cancelled"
    assert status.safe_error_code == "cancelled"
    assert status.artifact_sha256 is None


@pytest.mark.asyncio
async def test_fake_provider_honors_inflight_cancel_and_cancel_is_idempotent(
    tmp_path: Path,
) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    gate = asyncio.Event()
    provider = FakeParserProvider(
        id_factory=lambda: "fake-live-cancel",
        completion_gate=gate,
    )
    handle = await provider.create_job(spec, source_path=source_path)
    signal = asyncio.Event()
    wait_task = asyncio.create_task(provider.wait(handle, signal=signal))
    await asyncio.sleep(0)
    signal.set()

    status = await wait_task
    again = await provider.cancel(handle)
    assert status.state == "cancelled"
    assert again.state == "cancelled"


@pytest.mark.asyncio
async def test_fake_provider_wait_enforces_timeout(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    provider = FakeParserProvider(
        id_factory=lambda: "fake-timeout",
        completion_gate=asyncio.Event(),
        wait_timeout_seconds=0.001,
    )
    handle = await provider.create_job(spec, source_path=source_path)

    status = await provider.wait(handle)

    assert status.state == "failed"
    assert status.safe_error_code == "request_timeout"
    assert status.finished_at_ms is not None


@pytest.mark.asyncio
async def test_fake_provider_fails_closed_on_artifact_limit(tmp_path: Path) -> None:
    limits = ParserLimits(
        max_artifact_bytes=20_000,
        max_artifact_files=3,
        max_image_count=1,
        max_image_bytes=4,
    )
    source_path, spec = _source_and_spec(tmp_path, limits=limits)
    provider = FakeParserProvider(
        id_factory=lambda: "fake-artifact-limit",
        outputs=(
            FakeParserOutput(
                images=(FakeParserImage(mime_type="image/png", content=b"12345"),),
            ),
        ),
    )
    handle = await provider.create_job(spec, source_path=source_path)

    status = await provider.wait(handle)

    assert status.state == "failed"
    assert status.safe_error_code == "resource_limit"
    with pytest.raises(ParserError) as exc_info:
        await provider.download_artifact(handle, local_path=tmp_path / "must-not-exist.tar")
    assert exc_info.value.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_download_digest_mismatch_never_creates_destination(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    provider = FakeParserProvider(id_factory=lambda: "fake-download-digest")
    handle = await provider.create_job(spec, source_path=source_path)
    await provider.wait(handle)
    destination = tmp_path / "must-not-exist.tar"

    with pytest.raises(ParserError) as exc_info:
        await provider.download_artifact(
            handle,
            local_path=destination,
            expected_sha256="0" * 64,
        )
    assert exc_info.value.code == "artifact_invalid"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_fake_provider_rejects_tampered_handle(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    provider = FakeParserProvider(id_factory=lambda: "fake-handle")
    handle = await provider.create_job(spec, source_path=source_path)
    tampered = ParserJobHandle(
        provider=handle.provider,
        provider_job_id=handle.provider_job_id,
        job_id="different_job",
        source_id=handle.source_id,
        created_at_ms=handle.created_at_ms,
    )

    with pytest.raises(ParserError) as exc_info:
        await provider.status(tampered)
    assert exc_info.value.code == "job_not_found"


@pytest.mark.asyncio
async def test_destroy_is_idempotent_and_removes_artifact(tmp_path: Path) -> None:
    source_path, spec = _source_and_spec(tmp_path)
    provider = FakeParserProvider(id_factory=lambda: "fake-destroy")
    handle = await provider.create_job(spec, source_path=source_path)
    await provider.wait(handle)

    await provider.destroy(handle)
    await provider.destroy(handle)

    assert provider.destroyed_provider_job_ids == ["fake-destroy"]
    assert (await provider.status(handle)).state == "destroyed"
    with pytest.raises(ParserError) as exc_info:
        await provider.download_artifact(handle, local_path=tmp_path / "gone.tar")
    assert exc_info.value.code == "job_not_found"


def test_job_status_rejects_inconsistent_terminal_evidence() -> None:
    handle = ParserJobHandle(
        provider="fake",
        provider_job_id="provider-job",
        job_id="job_1",
        source_id="source_1",
        created_at_ms=1,
    )
    with pytest.raises(ValidationError, match="artifact evidence"):
        ParserJobStatus(handle=handle, state="succeeded", observed_at_ms=2)
    with pytest.raises(ValidationError, match="safe error code"):
        ParserJobStatus(handle=handle, state="failed", observed_at_ms=2)

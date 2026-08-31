"""Persistent OCI queue provider and isolated Worker orchestration tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import wiki_parser.oci as oci_module
from wiki_parser import (
    ParserArtifactManifestV2,
    ParserError,
    ParserJobHandleV2,
    ParserJobSpecV2,
    ParserJobStatusV2,
    ParserLimits,
    ParserRoutingConfigIdentity,
    ParserSourceSpec,
    PersistentOciParserProvider,
)

_ROOT = Path(__file__).resolve().parents[1]
_WORKER_ROOT = _ROOT / "workers" / "wiki_parser_worker"
_WORKER_SOURCE = _WORKER_ROOT / "src"
if str(_WORKER_SOURCE) not in sys.path:
    sys.path.insert(0, str(_WORKER_SOURCE))

import wiki_parser_worker.protocol as worker_protocol  # noqa: E402
import wiki_parser_worker.service as worker_service_module  # noqa: E402
from wiki_parser_worker.config import load_routing_config  # noqa: E402
from wiki_parser_worker.engine import DualPdfJobEngine  # noqa: E402
from wiki_parser_worker.errors import WorkerRuntimeError  # noqa: E402
from wiki_parser_worker.models import (  # noqa: E402
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
)
from wiki_parser_worker.preflight import route_pdf  # noqa: E402
from wiki_parser_worker.protocol import (  # noqa: E402
    ARTIFACT_NAME,
    CANCEL_NAME,
    RECEIPT_NAME,
    REQUEST_NAME,
    SOURCE_NAME,
    STATUS_NAME,
)
from wiki_parser_worker.protocol import (  # noqa: E402
    read_json_object as worker_read_json_object,
)
from wiki_parser_worker.quality import QualityEvaluator  # noqa: E402
from wiki_parser_worker.service import QueueWorkerService  # noqa: E402

from scripts.smoke_wiki_parser_oci import (  # noqa: E402
    _parse_cases,
    _reject_worker_source_runtime_path,
)

_PDF = b"%PDF-1.7\n% isolated queue fixture\n%%EOF\n"


def test_worker_atomic_exchange_write_never_calls_bind_mount_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "status.json"

    def forbidden_fsync(_descriptor: int) -> None:
        raise AssertionError("Worker exchange writes must not fsync a Windows bind mount")

    monkeypatch.setattr(os, "fsync", forbidden_fsync)
    worker_protocol.atomic_write_bytes(target, b'{"state":"queued"}')

    assert target.read_bytes() == b'{"state":"queued"}'
    assert list(tmp_path.glob(".status.json.*.tmp")) == []


def test_worker_atomic_exchange_retries_transient_windows_reader_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "status.json"
    target.write_bytes(b'{"state":"queued"}')
    real_replace = os.replace
    attempts = 0

    def transiently_locked(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated reader lock")
        real_replace(source, destination)

    monkeypatch.setattr(worker_protocol, "_ATOMIC_REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(worker_protocol, "_ATOMIC_REPLACE_RETRY_SECONDS", 0)
    monkeypatch.setattr(os, "replace", transiently_locked)

    worker_protocol.atomic_write_bytes(target, b'{"state":"running"}')

    assert attempts == 3
    assert target.read_bytes() == b'{"state":"running"}'
    assert list(tmp_path.glob(".status.json.*.tmp")) == []


def test_smoke_case_names_accept_documented_ascii_separators(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(_PDF)

    cases = _parse_cases(
        [
            ["digital-fast", "fast", str(source)],
            ["table_case", "accurate", str(source)],
        ]
    )

    assert [(case.name, case.mode) for case in cases] == [
        ("digital-fast", "fast"),
        ("table_case", "accurate"),
    ]
    for invalid in ("-leading", "trailing-", "has space", "非ascii"):
        with pytest.raises(ValueError, match="invalid case name"):
            _parse_cases([[invalid, "fast", str(source)]])


def test_smoke_cli_rejects_mutable_paths_inside_corresponding_source() -> None:
    for purpose, candidate in (
        ("--exchange-root", _WORKER_ROOT / ".runtime" / "exchange"),
        ("--output-root", _WORKER_ROOT / "smoke-output"),
    ):
        with pytest.raises(ValueError, match="outside the Worker source tree"):
            _reject_worker_source_runtime_path(candidate, purpose=purpose)

    safe = _ROOT / ".test-tmp" / "wiki-parser-exchange"
    assert _reject_worker_source_runtime_path(safe, purpose="--exchange-root") == (
        safe.resolve()
    )


class _Parser:
    def __init__(self, document: WorkerParsedDocument) -> None:
        self.document = document
        self.calls: list[tuple[str, str]] = []

    def parse(self, path: Path, **kwargs: object) -> WorkerParsedDocument:
        preset = kwargs.get("preset", self.document.preset)
        self.calls.append((path.name, cast(str, preset)))
        return self.document


class _InlineController:
    def __init__(self, engine: DualPdfJobEngine, queue_root: Path) -> None:
        self.engine = engine
        self.queue_root = queue_root
        self.running = False
        self.restart_count = 0

    def start(self) -> dict[str, object]:
        self.running = True
        self.restart_count += 1
        return {
            "config_revision": self.engine.config.revision,
            "config_sha256": self.engine.config.sha256,
            "type": "ready",
        }

    def run(self, provider_job_id: str) -> None:
        self.engine.execute(self.queue_root / "jobs" / provider_job_id)

    def alive(self) -> bool:
        return self.running

    def terminate(self) -> None:
        self.running = False


class _HangingController:
    def __init__(self, config_revision: str, config_sha256: str) -> None:
        self.config_revision = config_revision
        self.config_sha256 = config_sha256
        self.running = False
        self.restart_count = 0

    def start(self) -> dict[str, object]:
        self.running = True
        self.restart_count += 1
        return {
            "config_revision": self.config_revision,
            "config_sha256": self.config_sha256,
            "type": "ready",
        }

    def run(self, provider_job_id: str) -> None:
        del provider_job_id

    def alive(self) -> bool:
        return self.running

    def terminate(self) -> None:
        self.running = False


def _document(
    parser: Literal["pymupdf4llm", "docling"],
    *,
    healthy: bool = True,
    preset: str | None = None,
) -> WorkerParsedDocument:
    text = "A complete representative page with enough stable text for quality checks.\n"
    if not healthy:
        text = ""
    selected_preset = preset or (
        "pymupdf4llm_fast_no_ocr" if parser == "pymupdf4llm" else "docling_standard"
    )
    return WorkerParsedDocument(
        parser=parser,
        parser_version="1.28.2" if parser == "pymupdf4llm" else "2.119.0",
        preset=cast(Any, selected_preset),
        pages=(WorkerParsedPage(page_number=1, markdown=text, plain_text=text),),
        assets=(),
    )


def _preflight(
    source_id: str,
    source_sha256: str,
    *,
    scanned: bool = False,
) -> WorkerPreflightReport:
    return WorkerPreflightReport(
        source_id=source_id,
        source_sha256=source_sha256,
        page_count=1,
        text_page_count=0 if scanned else 1,
        image_dominant_page_count=1 if scanned else 0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        native_text_character_count=0 if scanned else 68,
        text_page_ratio=0.0 if scanned else 1.0,
        image_dominant_page_ratio=1.0 if scanned else 0.0,
        complexity_score=0.5 if scanned else 0.0,
        encrypted=False,
        has_javascript=False,
        has_embedded_files=False,
        duration_ms=1,
        observed_at_ms=10,
    )


def _spec(
    routing: ParserRoutingConfigIdentity,
    *,
    job_id: str,
    source_id: str,
    mode: Literal["auto", "fast", "accurate"],
    max_artifact_bytes: int = 512 * 1024 * 1024,
    timeout_seconds: int = 30,
) -> ParserJobSpecV2:
    return ParserJobSpecV2(
        job_id=job_id,
        source=ParserSourceSpec(
            source_id=source_id,
            display_name="fixture.pdf",
            size_bytes=len(_PDF),
            sha256=hashlib.sha256(_PDF).hexdigest(),
        ),
        requested_mode=mode,
        routing_config=routing,
        limits=ParserLimits(
            timeout_seconds=timeout_seconds,
            max_source_bytes=1024,
            max_artifact_bytes=max_artifact_bytes,
            max_artifact_files=32,
            max_image_count=0,
            max_image_bytes=min(max_artifact_bytes, 1024),
        ),
    )


def _harness(
    tmp_path: Path,
    *,
    fast_healthy: bool = True,
    scanned: bool = False,
) -> tuple[
    PersistentOciParserProvider,
    QueueWorkerService,
    _Parser,
    _Parser,
    ParserRoutingConfigIdentity,
]:
    config = load_routing_config(_WORKER_ROOT / "config" / "routing-quality-v1.json")
    routing = ParserRoutingConfigIdentity(revision=config.revision, sha256=config.sha256)
    fast = _Parser(_document("pymupdf4llm", healthy=fast_healthy))
    accurate = _Parser(
        _document("docling", preset="docling_ocr" if scanned else "docling_standard")
    )

    def inspect(
        path: Path,
        *,
        source_id: str,
        expected_sha256: str,
        config: object,
    ) -> WorkerPreflightReport:
        del path, config
        return _preflight(source_id, expected_sha256, scanned=scanned)

    engine = DualPdfJobEngine(
        config=config,
        fast_parser=cast(Any, fast),
        accurate_parser=cast(Any, accurate),
        quality_evaluator=QualityEvaluator(config, clock_ms=lambda: 20),
        preflight_inspector=inspect,
        router=route_pdf,
        clock_ms=iter(range(100, 10_000)).__next__,
    )
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    controller = _InlineController(engine, exchange)
    service = QueueWorkerService(
        queue_root=exchange,
        config=config,
        controller=controller,
        clock_ms=iter(range(20_000, 30_000)).__next__,
    )
    service.start()
    provider = PersistentOciParserProvider(
        exchange_root=exchange,
        routing_config=routing,
        id_factory=lambda: "oci-test-job",
        poll_interval_seconds=0.001,
        management_timeout_seconds=1.0,
    )
    return provider, service, fast, accurate, routing


async def _run_job(
    tmp_path: Path,
    provider: PersistentOciParserProvider,
    service: QueueWorkerService,
    spec: ParserJobSpecV2,
) -> tuple[ParserJobHandleV2, ParserJobStatusV2]:
    source = tmp_path / f"{spec.job_id}.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(spec, source_path=source)
    job_dir = tmp_path / "exchange" / "jobs" / handle.provider_job_id
    directory_mode = stat.S_IMODE(job_dir.stat().st_mode)
    assert directory_mode & stat.S_IROTH
    assert directory_mode & stat.S_IWOTH
    assert directory_mode & stat.S_IXOTH
    for shared_name in (SOURCE_NAME, REQUEST_NAME, STATUS_NAME):
        assert stat.S_IMODE((job_dir / shared_name).stat().st_mode) & stat.S_IROTH
    assert service.run_once()
    assert service.run_once()
    return handle, await provider.wait(handle)


@pytest.mark.asyncio
async def test_status_read_retries_atomic_replace_identity_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _service, _fast, _accurate, routing = _harness(tmp_path)
    source = tmp_path / "status-race.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(
        _spec(
            routing,
            job_id="job-status-race",
            source_id="source-status-race",
            mode="fast",
        ),
        source_path=source,
    )
    original_read = oci_module._read_stable
    attempts = 0

    def race_once(path: Path, *, maximum_bytes: int, error_code: str) -> bytes:
        nonlocal attempts
        if path.name == STATUS_NAME and attempts < 2:
            attempts += 1
            raise ParserError("protocol_error", provider="dual_pdf")
        return original_read(
            path,
            maximum_bytes=maximum_bytes,
            error_code=error_code,
        )

    monkeypatch.setattr(oci_module, "_read_stable", race_once)

    assert (await provider.status(handle)).state == "queued"
    assert attempts == 2


@pytest.mark.asyncio
async def test_worker_status_read_retries_its_atomic_replace_identity_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, service, _fast, _accurate, routing = _harness(tmp_path)
    source = tmp_path / "worker-status-race.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(
        _spec(
            routing,
            job_id="job-worker-status-race",
            source_id="source-worker-status-race",
            mode="fast",
        ),
        source_path=source,
    )
    assert service.run_once()
    original_read = worker_read_json_object
    attempts = 0

    def race_twice(path: Path, *, maximum_bytes: int = 1_048_576) -> dict[str, object]:
        nonlocal attempts
        if path.name == STATUS_NAME and attempts < 2:
            attempts += 1
            raise WorkerRuntimeError("source_changed")
        return original_read(path, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(worker_service_module, "read_json_object", race_twice)

    assert service.run_once()
    assert (await provider.status(handle)).state == "succeeded"
    assert attempts == 2


@pytest.mark.asyncio
async def test_worker_status_read_exhaustion_never_leaves_running_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, service, _fast, _accurate, routing = _harness(tmp_path)
    source = tmp_path / "worker-status-failure.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(
        _spec(
            routing,
            job_id="job-worker-status-failure",
            source_id="source-worker-status-failure",
            mode="fast",
        ),
        source_path=source,
    )
    assert service.run_once()

    def always_race(
        _path: Path, *, maximum_bytes: int = 1_048_576
    ) -> dict[str, object]:
        del maximum_bytes
        raise WorkerRuntimeError("source_changed")

    monkeypatch.setattr(worker_service_module, "read_json_object", always_race)

    assert service.run_once()
    status = await provider.status(handle)
    assert status.state == "failed"
    assert status.safe_error_code == "protocol_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "scanned", "expected_parser"),
    [
        ("fast", False, "pymupdf4llm"),
        ("accurate", False, "docling"),
        ("auto", True, "docling"),
    ],
)
async def test_oci_queue_modes_emit_contract_valid_artifact(
    tmp_path: Path,
    mode: Literal["auto", "fast", "accurate"],
    scanned: bool,
    expected_parser: str,
) -> None:
    provider, service, fast, accurate, routing = _harness(tmp_path, scanned=scanned)
    spec = _spec(routing, job_id=f"job-{mode}", source_id=f"source-{mode}", mode=mode)
    handle, status = await _run_job(tmp_path, provider, service, spec)
    assert status.state == "succeeded"
    assert status.attempts[-1].parser == expected_parser
    artifact = tmp_path / f"{mode}.tar"
    receipt = await provider.download_artifact(
        handle,
        local_path=artifact,
        expected_sha256=status.artifact_sha256,
    )
    with tarfile.open(artifact, mode="r:") as archive:
        manifest_stream = archive.extractfile("manifest.json")
        assert manifest_stream is not None
        manifest = ParserArtifactManifestV2.model_validate_json(manifest_stream.read())
    assert manifest.parser == expected_parser
    assert receipt.file_count == 3
    assert bool(fast.calls) == (expected_parser == "pymupdf4llm")
    assert bool(accurate.calls) == (expected_parser == "docling")
    job_dir = tmp_path / "exchange" / "jobs" / handle.provider_job_id
    for shared_name in (STATUS_NAME, RECEIPT_NAME, ARTIFACT_NAME):
        assert stat.S_IMODE((job_dir / shared_name).stat().st_mode) & stat.S_IROTH


@pytest.mark.asyncio
async def test_auto_quality_rejection_falls_back_once_to_original_pdf(tmp_path: Path) -> None:
    provider, service, fast, accurate, routing = _harness(tmp_path, fast_healthy=False)
    spec = _spec(routing, job_id="job-fallback", source_id="source-fallback", mode="auto")
    handle, status = await _run_job(tmp_path, provider, service, spec)
    assert status.state == "succeeded"
    assert [attempt.state for attempt in status.attempts] == ["quality_rejected", "succeeded"]
    assert status.attempts[1].fallback_from_attempt_id == status.attempts[0].attempt_id
    assert fast.calls == [("source.pdf", "pymupdf4llm_fast_no_ocr")]
    assert accurate.calls == [("source.pdf", "docling_standard")]
    await provider.download_artifact(handle, local_path=tmp_path / "fallback.tar")


@pytest.mark.asyncio
async def test_explicit_fast_never_falls_back_and_quota_is_fail_closed(tmp_path: Path) -> None:
    provider, service, _fast, accurate, routing = _harness(tmp_path, fast_healthy=False)
    rejected = _spec(routing, job_id="job-rejected", source_id="source-rejected", mode="fast")
    _handle, status = await _run_job(tmp_path, provider, service, rejected)
    assert status.state == "failed"
    assert status.safe_error_code == "quality_rejected"
    assert not accurate.calls

    other_root = tmp_path / "quota"
    other_root.mkdir()
    provider, service, _fast, _accurate, routing = _harness(other_root)
    quota = _spec(
        routing,
        job_id="job-quota",
        source_id="source-quota",
        mode="fast",
        max_artifact_bytes=1024,
    )
    _handle, status = await _run_job(other_root, provider, service, quota)
    assert status.state == "failed"
    assert status.safe_error_code == "resource_limit"

    tamper_root = tmp_path / "tampered-quota"
    tamper_root.mkdir()
    provider, service, _fast, _accurate, routing = _harness(tamper_root)
    source = tamper_root / "source.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(
        _spec(
            routing,
            job_id="job-tampered-quota",
            source_id="source-tampered-quota",
            mode="fast",
        ),
        source_path=source,
    )
    request_path = (
        tamper_root
        / "exchange"
        / "jobs"
        / handle.provider_job_id
        / REQUEST_NAME
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["spec"]["limits"]["max_artifact_bytes"] = 536_870_913
    request_path.write_text(json.dumps(request), encoding="utf-8")
    service.run_once()
    service.run_once()
    tampered_status = await provider.status(handle)
    assert tampered_status.state == "failed"
    assert tampered_status.safe_error_code == "resource_limit"


@pytest.mark.asyncio
async def test_artifact_sha_tamper_is_detected_before_download(tmp_path: Path) -> None:
    provider, service, _fast, _accurate, routing = _harness(tmp_path)
    spec = _spec(routing, job_id="job-tamper", source_id="source-tamper", mode="fast")
    handle, status = await _run_job(tmp_path, provider, service, spec)
    artifact = tmp_path / "exchange" / "jobs" / handle.provider_job_id / "artifact.tar"
    os.chmod(artifact, 0o600)
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(ParserError) as raised:
        await provider.download_artifact(
            handle,
            local_path=tmp_path / "must-not-exist.tar",
            expected_sha256=status.artifact_sha256,
        )
    assert raised.value.code == "artifact_invalid"
    assert not (tmp_path / "must-not-exist.tar").exists()


@pytest.mark.asyncio
async def test_cancel_timeout_crash_and_restart_recovery_are_terminal(tmp_path: Path) -> None:
    config = load_routing_config(_WORKER_ROOT / "config" / "routing-quality-v1.json")
    routing = ParserRoutingConfigIdentity(revision=config.revision, sha256=config.sha256)
    now = [1_000]
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    controller = _HangingController(config.revision, config.sha256)
    service = QueueWorkerService(
        queue_root=exchange,
        config=config,
        controller=controller,
        clock_ms=lambda: now[0],
    )
    service.start()
    provider = PersistentOciParserProvider(
        exchange_root=exchange,
        routing_config=routing,
        id_factory=iter(("oci-cancel", "oci-timeout", "oci-crash", "oci-recover")).__next__,
        poll_interval_seconds=0.001,
        management_timeout_seconds=1.0,
    )

    controller.running = False
    assert service.run_once()
    assert controller.restart_count == 2
    assert (await provider.probe()).available is True

    async def create(
        name: str, timeout_seconds: int = 30
    ) -> ParserJobHandleV2:
        source = tmp_path / f"{name}.pdf"
        source.write_bytes(_PDF)
        return await provider.create_job(
            _spec(
                routing,
                job_id=f"job-{name}",
                source_id=f"source-{name}",
                mode="fast",
                timeout_seconds=timeout_seconds,
            ),
            source_path=source,
        )

    cancel_handle = await create("cancel")
    service.run_once()
    cancel_task = asyncio.create_task(provider.cancel(cancel_handle))
    for _ in range(100):
        await asyncio.sleep(0.001)
        service.run_once()
        if cancel_task.done():
            break
    assert (await cancel_task).state == "cancelled"

    timeout_handle = await create("timeout")
    service.run_once()
    now[0] += 30_000
    service.run_once()
    timeout_status = await provider.status(timeout_handle)
    assert (timeout_status.state, timeout_status.safe_error_code) == (
        "failed",
        "request_timeout",
    )

    crash_handle = await create("crash")
    service.run_once()
    controller.running = False
    service.run_once()
    crash_status = await provider.status(crash_handle)
    assert (crash_status.state, crash_status.safe_error_code) == (
        "failed",
        "provider_unavailable",
    )

    recover_handle = await create("recover")
    service.run_once()
    service.close()
    recovered = QueueWorkerService(
        queue_root=exchange,
        config=config,
        controller=controller,
        clock_ms=lambda: now[0] + 1,
    )
    recovered.start()
    recover_status = await provider.status(recover_handle)
    assert (recover_status.state, recover_status.safe_error_code) == (
        "failed",
        "provider_unavailable",
    )


@pytest.mark.asyncio
async def test_source_tamper_and_destroy_cleanup(tmp_path: Path) -> None:
    provider, service, _fast, _accurate, routing = _harness(tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(_PDF)
    handle = await provider.create_job(
        _spec(routing, job_id="job-source", source_id="source-source", mode="fast"),
        source_path=source,
    )
    frozen = tmp_path / "exchange" / "jobs" / handle.provider_job_id / "source.pdf"
    frozen.write_bytes(_PDF + b"changed")
    service.run_once()
    service.run_once()
    assert (await provider.status(handle)).safe_error_code == "invalid_source"

    destroy_task = asyncio.create_task(provider.destroy(handle))
    for _ in range(100):
        await asyncio.sleep(0.001)
        service.run_once()
        if destroy_task.done():
            break
    await destroy_task
    job_dir = frozen.parent
    assert (await provider.status(handle)).state == "destroyed"
    assert sorted(path.name for path in job_dir.iterdir()) == [STATUS_NAME]
    assert not (job_dir / CANCEL_NAME).exists()

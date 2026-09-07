"""Persistent queue bridge and isolated MinerU Worker engine tests."""

from __future__ import annotations

import hashlib
import os
import sys
import tarfile
from pathlib import Path

import pytest

from wiki_parser import (
    ParserArtifactManifestV2,
    ParserJobSpecV2,
    ParserProbeV2,
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
from wiki_parser_worker.config import load_routing_config  # noqa: E402
from wiki_parser_worker.engine import MineruJobEngine  # noqa: E402
from wiki_parser_worker.errors import WorkerRuntimeError  # noqa: E402
from wiki_parser_worker.models import (  # noqa: E402
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
    WorkerRouteDecision,
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
        raise AssertionError("Worker exchange writes must not fsync a bind mount")

    monkeypatch.setattr(os, "fsync", forbidden_fsync)
    replace = os.replace
    attempts = 0

    def busy_reader_once(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("host reader denies delete sharing")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", busy_reader_once)
    worker_protocol.atomic_write_bytes(target, b'{"state":"queued"}')
    assert target.read_bytes() == b'{"state":"queued"}'
    assert attempts == 3


def test_smoke_case_names_accept_all_mineru_profiles(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(_PDF)
    cases = _parse_cases(
        [
            ["cpu-pipeline", "pipeline", str(source)],
            ["gpu_medium", "gpu-medium", str(source)],
            ["gpu-high", "gpu-high", str(source)],
        ]
    )

    assert [case.mode for case in cases] == ["pipeline", "gpu-medium", "gpu-high"]
    with pytest.raises(ValueError, match="invalid mode"):
        _parse_cases([["invalid", "auto", str(source)]])


def test_smoke_cli_rejects_mutable_paths_inside_worker_source() -> None:
    with pytest.raises(ValueError, match="outside the Worker source tree"):
        _reject_worker_source_runtime_path(
            _WORKER_ROOT / ".runtime" / "exchange",
            purpose="--exchange-root",
        )


class _Parser:
    def __init__(self) -> None:
        self.routes: list[WorkerRouteDecision] = []

    def parse(
        self,
        _path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
        route: WorkerRouteDecision,
    ) -> WorkerParsedDocument:
        assert expected_sha256 == preflight.source_sha256
        self.routes.append(route)
        text = "A complete MinerU page with enough stable text for quality checks. " * 2
        return WorkerParsedDocument(
            parser="mineru",
            parser_version="3.4.5",
            preset=route.preset,
            pages=(WorkerParsedPage(page_number=1, markdown=text, plain_text=text),),
            assets=(),
        )


def _preflight(
    _path: Path,
    *,
    source_id: str,
    expected_sha256: str,
    config: object,
) -> WorkerPreflightReport:
    del config
    return WorkerPreflightReport(
        source_id=source_id,
        source_sha256=expected_sha256,
        page_count=1,
        text_page_count=1,
        image_dominant_page_count=0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        native_text_character_count=80,
        text_page_ratio=1.0,
        image_dominant_page_ratio=0.0,
        complexity_score=0.0,
        encrypted=False,
        has_javascript=False,
        has_embedded_files=False,
        duration_ms=1,
        observed_at_ms=10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "preset", "backend", "effort"),
    [
        ("pipeline", "mineru_pipeline", "pipeline", None),
        ("gpu-medium", "mineru_gpu_medium", "hybrid-engine", "medium"),
        ("gpu-high", "mineru_gpu_high", "hybrid-engine", "high"),
    ],
)
async def test_queue_provider_and_worker_run_one_selected_mineru_profile(
    tmp_path: Path,
    mode: str,
    preset: str,
    backend: str,
    effort: str | None,
) -> None:
    exchange = (tmp_path / "exchange").resolve()
    config = load_routing_config(_WORKER_ROOT / "config" / "routing-quality-v1.json")
    routing = ParserRoutingConfigIdentity(
        revision=config.revision,
        sha256=config.sha256,
    )
    provider = PersistentOciParserProvider(
        exchange_root=exchange,
        routing_config=routing,
        id_factory=lambda: f"provider-{mode}",
        clock_ms=lambda: 100,
    )
    source = (tmp_path / f"{mode}.pdf").resolve()
    source.write_bytes(_PDF)
    spec = ParserJobSpecV2.model_validate(
        {
            "job_id": f"job-{mode}",
            "source": ParserSourceSpec(
                source_id=f"source-{mode}",
                display_name="source.pdf",
                size_bytes=len(_PDF),
                sha256=hashlib.sha256(_PDF).hexdigest(),
            ).model_dump(mode="json"),
            "requested_mode": mode,
            "routing_config": routing.model_dump(mode="json"),
        }
    )
    handle = await provider.create_job(spec, source_path=source)
    parser = _Parser()
    engine = MineruJobEngine(
        config=config,
        parser=parser,
        quality_evaluator=QualityEvaluator(config, clock_ms=lambda: 20),
        preflight_inspector=_preflight,
        clock_ms=iter(range(100, 200)).__next__,
    )

    engine.execute(exchange / "jobs" / handle.provider_job_id)
    status = await provider.status(handle)

    assert status.state == "succeeded"
    assert len(status.attempts) == 1
    assert status.attempts[0].parser == "mineru"
    assert status.attempts[0].preset == preset
    assert parser.routes[0].backend == backend
    assert parser.routes[0].effort == effort

    artifact_path = tmp_path / f"{mode}.tar"
    receipt = await provider.download_artifact(
        handle,
        local_path=artifact_path,
        expected_sha256=status.artifact_sha256,
    )
    with tarfile.open(artifact_path, mode="r:") as archive:
        stream = archive.extractfile("manifest.json")
        assert stream is not None
        manifest = ParserArtifactManifestV2.model_validate_json(stream.read())
    assert receipt.sha256 == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert manifest.requested_mode == mode
    assert manifest.parser == "mineru"
    assert len(manifest.attempts) == 1


@pytest.mark.asyncio
async def test_provider_probe_is_fail_closed_without_worker(tmp_path: Path) -> None:
    routing = ParserRoutingConfigIdentity(revision="mineru_profiles", sha256="a" * 64)
    provider = PersistentOciParserProvider(
        exchange_root=(tmp_path / "exchange").resolve(),
        routing_config=routing,
    )

    probe = await provider.probe()

    assert probe.provider == "mineru"
    assert probe.available is False
    assert probe.error_code == "provider_unavailable"
    assert probe.license_mode == "mineru_open_source"


@pytest.mark.asyncio
@pytest.mark.parametrize("observed,available", [(100_000, True), (69_999, False), (100_001, False)])
async def test_probe_rejects_expired_or_future_heartbeat(
    tmp_path: Path, observed: int, available: bool,
) -> None:
    routing = ParserRoutingConfigIdentity(revision="mineru_profiles", sha256="a" * 64)
    root = (tmp_path / "exchange").resolve()
    provider = PersistentOciParserProvider(
        exchange_root=root, routing_config=routing, clock_ms=lambda: 100_000,
    )
    probe = ParserProbeV2(
        provider="mineru", available=True, worker_version="0.0.29",
        license_mode="mineru_open_source", routing_config=routing, observed_at_ms=observed,
    )
    (root / "probe.json").write_text(probe.model_dump_json(), encoding="utf-8")
    assert (await provider.probe()).available is available


@pytest.mark.asyncio
async def test_worker_refreshes_heartbeat_when_idle_and_marks_close_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_routing_config(_WORKER_ROOT / "config" / "routing-quality-v1.json")
    now = [100_000]

    class Controller:
        def start(self) -> dict[str, object]:
            return {"config_revision": config.revision, "config_sha256": config.sha256}

        def alive(self) -> bool:
            return True

        def terminate(self) -> None:
            pass

        def run(self, job_id: str) -> None:
            raise AssertionError("no job should run")

    root = (tmp_path / "exchange").resolve()
    provider = PersistentOciParserProvider(
        exchange_root=root, clock_ms=lambda: now[0],
        routing_config=ParserRoutingConfigIdentity(revision=config.revision, sha256=config.sha256),
    )
    service = QueueWorkerService(
        queue_root=root, config=config, controller=Controller(), clock_ms=lambda: now[0],
    )
    service.start()
    assert (await provider.probe()).available
    job_dir = root / "jobs" / "oci-cleanup"
    job_dir.mkdir()
    worker_protocol.atomic_write_json(job_dir / "status.json", {"state": "succeeded"})
    (job_dir / "destroy.request").touch()
    original_write = worker_protocol.atomic_write_json

    def blocked_status(path: Path, value: object) -> None:
        if path.name == "status.json":
            raise WorkerRuntimeError("invalid_source")
        original_write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr("wiki_parser_worker.service.atomic_write_json", blocked_status)
        with pytest.raises(WorkerRuntimeError):
            service.run_once()
        assert (job_dir / "destroy.request").exists()
    service.close()
    service.start()
    assert service.run_once()
    assert worker_protocol.read_json_object(job_dir / "status.json")["state"] == "destroyed"
    assert not (job_dir / "destroy.request").exists()
    now[0] += 40_000
    assert not (await provider.probe()).available
    assert not service.run_once()
    assert (await provider.probe()).observed_at_ms == now[0]
    assert (await provider.probe()).available
    service.close()
    assert not (await provider.probe()).available

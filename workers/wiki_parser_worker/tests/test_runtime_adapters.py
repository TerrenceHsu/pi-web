"""Offline tests for MinerU preflight, profile routing, adapter and quality."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wiki_parser_worker.config import load_routing_config  # noqa: E402
from wiki_parser_worker.errors import WorkerRuntimeError  # noqa: E402
from wiki_parser_worker.models import (  # noqa: E402
    RequestedMode,
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
)
from wiki_parser_worker.parsers import MineruParser  # noqa: E402
from wiki_parser_worker.preflight import inspect_pdf, route_pdf  # noqa: E402
from wiki_parser_worker.quality import QualityEvaluator  # noqa: E402

_CONFIG = load_routing_config(_ROOT / "config" / "routing-quality-v1.json")
_PDF = b"%PDF-1.7\nMinerU test\n%%EOF\n"
_PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


def _source(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source.pdf"
    path.write_bytes(_PDF)
    return path, hashlib.sha256(_PDF).hexdigest()


def _preflight(digest: str, *, pages: int = 2) -> WorkerPreflightReport:
    return WorkerPreflightReport(
        source_id="source-1",
        source_sha256=digest,
        page_count=pages,
        text_page_count=pages,
        image_dominant_page_count=0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        native_text_character_count=80 * pages,
        text_page_ratio=1.0,
        image_dominant_page_ratio=0.0,
        complexity_score=0.0,
        encrypted=False,
        has_javascript=False,
        has_embedded_files=False,
        duration_ms=2,
        observed_at_ms=10,
    )


@pytest.mark.parametrize(
    ("mode", "preset", "backend", "effort", "reason"),
    [
        ("pipeline", "mineru_pipeline", "pipeline", None, "explicit_pipeline"),
        (
            "gpu-medium",
            "mineru_gpu_medium",
            "hybrid-engine",
            "medium",
            "explicit_gpu_medium",
        ),
        (
            "gpu-high",
            "mineru_gpu_high",
            "hybrid-engine",
            "high",
            "explicit_gpu_high",
        ),
    ],
)
def test_product_profiles_map_to_fixed_mineru_arguments(
    mode: str,
    preset: str,
    backend: str,
    effort: str | None,
    reason: str,
) -> None:
    route = route_pdf(cast(RequestedMode, mode), _preflight("a" * 64), _CONFIG)

    assert route.parser == "mineru"
    assert route.preset == preset
    assert route.backend == backend
    assert route.effort == effort
    assert route.reasons == (reason,)


class _Page:
    def __init__(self, text: str, *, images: tuple[object, ...] = ()) -> None:
        self._text = text
        self.images = images

    def extract_text(self) -> str:
        return self._text


class _Reader:
    def __init__(self, _path: str, *, strict: bool) -> None:
        assert strict is True
        self.is_encrypted = False
        self.pages = [_Page("A" * 40), _Page("", images=(object(),))]
        self.trailer: dict[str, object] = {"/Root": {}}


def test_preflight_uses_lightweight_pdf_metadata(tmp_path: Path) -> None:
    path, digest = _source(tmp_path)
    clock = iter((100, 105))
    report = inspect_pdf(
        path,
        source_id="source-1",
        expected_sha256=digest,
        config=_CONFIG,
        pypdf_module=SimpleNamespace(PdfReader=_Reader),
        clock_ms=lambda: next(clock),
    )

    assert report.page_count == 2
    assert report.text_page_count == 1
    assert report.image_dominant_page_count == 1
    assert report.complexity_score == 0.5
    assert report.duration_ms == 5


class _Runner:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def __call__(
        self,
        output_dir: str,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        **kwargs: object,
    ) -> None:
        assert pdf_file_names == ["source"]
        assert pdf_bytes_list == [_PDF]
        assert p_lang_list == ["ch"]
        self.kwargs = kwargs
        result = Path(output_dir) / "source" / "result"
        images = result / "images"
        images.mkdir(parents=True)
        (images / "one.png").write_bytes(_PNG)
        (images / "duplicate.png").write_bytes(_PNG)
        (result / "source_middle.json").write_text(
            json.dumps({"pdf_info": [{}, {}]}),
            encoding="utf-8",
        )
        (result / "source_content_list.json").write_text(
            json.dumps(
                [
                    {
                        "page_idx": 0,
                        "type": "title",
                        "text": "First page",
                        "text_level": 1,
                        "img_path": "images/one.png",
                    },
                    {"page_idx": 1, "type": "text", "text": "Second page"},
                    {
                        "page_idx": 1,
                        "type": "image",
                        "img_path": "images/duplicate.png",
                    },
                ]
            ),
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    ("mode", "backend", "effort", "image_analysis"),
    [
        ("pipeline", "pipeline", "medium", False),
        ("gpu-medium", "hybrid-engine", "medium", False),
        ("gpu-high", "hybrid-engine", "high", True),
    ],
)
def test_adapter_invokes_mineru_and_normalizes_pages_and_assets(
    tmp_path: Path,
    mode: str,
    backend: str,
    effort: str,
    image_analysis: bool,
) -> None:
    path, digest = _source(tmp_path)
    runner = _Runner()
    route = route_pdf(cast(RequestedMode, mode), _preflight(digest), _CONFIG)
    one_unique_image = replace(
        _CONFIG,
        limits=replace(_CONFIG.limits, max_embedded_images=1),
    )
    parser = MineruParser(config=one_unique_image, runner=runner, version="3.4.5")

    result = parser.parse(
        path,
        expected_sha256=digest,
        preflight=_preflight(digest),
        route=route,
    )

    assert result.parser == "mineru"
    assert result.preset == route.preset
    assert [page.page_number for page in result.pages] == [1, 2]
    assert "# First page" in result.pages[0].markdown
    assert "Second page" in result.pages[1].plain_text
    assert len(result.assets) == 1
    assert runner.kwargs["backend"] == backend
    assert runner.kwargs["effort"] == effort
    assert runner.kwargs["image_analysis"] is image_analysis


def test_adapter_rejects_unpinned_version() -> None:
    with pytest.raises(WorkerRuntimeError) as raised:
        MineruParser(config=_CONFIG, runner=_Runner(), version="3.4.4")
    assert raised.value.code == "dependency_version_mismatch"


def test_quality_gate_accepts_complete_output_and_rejects_empty_output() -> None:
    preflight = _preflight("a" * 64)
    healthy = WorkerParsedDocument(
        parser="mineru",
        parser_version="3.4.5",
        preset="mineru_pipeline",
        pages=(
            WorkerParsedPage(1, "A" * 80, "A" * 80),
            WorkerParsedPage(2, "B" * 80, "B" * 80),
        ),
        assets=(),
    )
    empty = WorkerParsedDocument(
        parser="mineru",
        parser_version="3.4.5",
        preset="mineru_pipeline",
        pages=(WorkerParsedPage(1, "", ""), WorkerParsedPage(2, "", "")),
        assets=(),
    )
    evaluator = QualityEvaluator(_CONFIG, clock_ms=lambda: 20)

    assert evaluator.evaluate(healthy, preflight).passed is True
    rejected = evaluator.evaluate(empty, preflight)
    assert rejected.passed is False
    assert "empty_content" in rejected.critical_failures

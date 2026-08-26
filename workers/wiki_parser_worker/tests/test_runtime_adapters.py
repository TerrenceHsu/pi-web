"""Offline contract tests for preflight, routing, adapters, and quality.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wiki_parser_worker.config import load_routing_config  # noqa: E402
from wiki_parser_worker.errors import WorkerRuntimeError  # noqa: E402
from wiki_parser_worker.models import (  # noqa: E402
    WorkerParsedAsset,
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
)
from wiki_parser_worker.parsers import (  # noqa: E402
    DoclingAccurateParser,
    PyMuPdf4LlmFastParser,
    _configure_docling_layout_options,
)
from wiki_parser_worker.preflight import inspect_pdf, route_pdf  # noqa: E402
from wiki_parser_worker.quality import QualityEvaluator  # noqa: E402
from wiki_parser_worker.supply_chain import (  # noqa: E402
    verify_docling_model_artifacts,
)

_CONFIG_PATH = _ROOT / "config" / "routing-quality-v1.json"
_PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"


class _Rect:
    def __init__(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1


class _Page:
    rect = _Rect(0, 0, 100, 100)

    def __init__(
        self,
        text: str,
        *,
        blocks: list[tuple[float, float, float, float, str]] | None = None,
        image_rects: list[_Rect] | None = None,
        drawings: int = 0,
    ) -> None:
        self._text = text
        self._blocks = blocks or []
        self._image_rects = image_rects or []
        self._drawings = drawings

    def get_text(self, mode: str) -> object:
        return self._text if mode == "text" else self._blocks

    def get_images(self, *, full: bool) -> list[tuple[int]]:
        assert full is True
        return [(7,)] if self._image_rects else []

    def get_image_rects(self, xref: int) -> list[_Rect]:
        assert xref == 7
        return self._image_rects

    def get_drawings(self) -> list[object]:
        return [object()] * self._drawings


class _Document:
    needs_pass = False
    is_encrypted = False

    def __init__(self, pages: list[_Page], *, javascript: bool = False) -> None:
        self._pages = pages
        self._javascript = javascript
        self.closed = False

    @property
    def page_count(self) -> int:
        return len(self._pages)

    def load_page(self, index: int) -> _Page:
        return self._pages[index]

    def xref_length(self) -> int:
        return 2

    def xref_object(self, xref: int, *, compressed: bool) -> str:
        assert compressed is False
        return "<< /JavaScript >>" if self._javascript else "<< >>"

    def embfile_count(self) -> int:
        return 0

    def extract_image(self, xref: int) -> dict[str, object]:
        assert xref == 7
        return {"image": _PNG, "ext": "png"}

    def close(self) -> None:
        self.closed = True


class _PyMuPdf:
    __version__ = "1.28.2"

    def __init__(self, pages: list[_Page], *, javascript: bool = False) -> None:
        self.pages = pages
        self.javascript = javascript
        self.documents: list[_Document] = []

    def open(self, path: str) -> _Document:
        assert Path(path).is_file()
        document = _Document(self.pages, javascript=self.javascript)
        self.documents.append(document)
        return document


class _FastModule:
    __version__ = "1.28.2"

    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.kwargs: dict[str, object] = {}

    def to_markdown(self, path: str, **kwargs: object) -> list[dict[str, str]]:
        assert Path(path).is_file()
        self.kwargs = kwargs
        return [{"text": page} for page in self.pages]


class _DoclingDocument:
    def __init__(self, pages: int) -> None:
        self.pages = {index: object() for index in range(1, pages + 1)}

    def export_to_markdown(self, *, page_no: int, traverse_pictures: bool) -> str:
        assert traverse_pictures is True
        return f"# Accurate page {page_no}"

    def export_to_text(self, *, page_no: int, traverse_pictures: bool) -> str:
        assert traverse_pictures is True
        return f"Accurate page {page_no}"


class _Converter:
    def __init__(self, pages: int) -> None:
        self.pages = pages
        self.calls = 0

    def convert(self, path: Path, **kwargs: object) -> object:
        assert path.is_file()
        assert kwargs["raises_on_error"] is True
        self.calls += 1
        return SimpleNamespace(
            status=SimpleNamespace(value="success"),
            document=_DoclingDocument(self.pages),
        )


class _CopyModel:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)

    def model_copy(self, *, update: dict[str, object]) -> _CopyModel:
        values = {**self.__dict__, **update}
        return _CopyModel(**values)


class _LayoutObjectDetectionOptions:
    @staticmethod
    def from_preset(name: str) -> _CopyModel:
        assert name == "layout_heron_default"
        return _CopyModel(
            engine_options=_CopyModel(compile_model=True),
            model_spec=_CopyModel(revision="main"),
        )


def _preflight(page_count: int = 2, native_chars: int = 40) -> WorkerPreflightReport:
    return WorkerPreflightReport(
        source_id="source-1",
        source_sha256="0" * 64,
        page_count=page_count,
        text_page_count=page_count,
        image_dominant_page_count=0,
        multicolumn_page_count=0,
        table_candidate_page_count=0,
        native_text_character_count=native_chars,
        text_page_ratio=1.0,
        image_dominant_page_ratio=0.0,
        complexity_score=0.0,
        encrypted=False,
        has_javascript=False,
        has_embedded_files=False,
        duration_ms=1,
        observed_at_ms=1,
    )


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_routing_config(_CONFIG_PATH)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nworker fixture")
        self.sha256 = hashlib.sha256(self.pdf.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_has_stable_identity_and_rejects_unknown_fields(self) -> None:
        repeated = load_routing_config(_CONFIG_PATH)
        self.assertEqual(self.config.sha256, repeated.sha256)
        self.assertEqual(len(self.config.sha256), 64)
        invalid = self.root / "invalid.json"
        invalid.write_text('{"unexpected": true}', encoding="utf-8")
        with self.assertRaisesRegex(WorkerRuntimeError, "invalid_configuration"):
            load_routing_config(invalid)

    def test_preflight_routes_simple_scan_and_complex_documents(self) -> None:
        digital_pages = [
            _Page("Digital text is deliberately long enough for deterministic routing")
            for _ in range(2)
        ]
        report = inspect_pdf(
            self.pdf,
            source_id="source-1",
            expected_sha256=self.sha256,
            config=self.config,
            pymupdf_module=_PyMuPdf(digital_pages),
            clock_ms=iter((10, 15)).__next__,
        )
        route = route_pdf("auto", report, self.config)
        self.assertEqual(route.parser, "pymupdf4llm")
        self.assertEqual(route.fallback_parser, "docling")
        self.assertEqual(route.reasons, ("simple_digital",))
        self.assertEqual(report.duration_ms, 5)

        scan = inspect_pdf(
            self.pdf,
            source_id="source-1",
            expected_sha256=self.sha256,
            config=self.config,
            pymupdf_module=_PyMuPdf([_Page("", image_rects=[_Rect(0, 0, 100, 100)])]),
        )
        self.assertEqual(route_pdf("auto", scan, self.config).preset, "docling_ocr")

        blocks = [
            (2.0, 2.0, 35.0, 20.0, "left column text"),
            (2.0, 30.0, 35.0, 45.0, "left second block"),
            (65.0, 2.0, 98.0, 20.0, "right column text"),
            (65.0, 30.0, 98.0, 45.0, "right second block"),
        ]
        complex_report = inspect_pdf(
            self.pdf,
            source_id="source-1",
            expected_sha256=self.sha256,
            config=self.config,
            pymupdf_module=_PyMuPdf(
                [
                    _Page(
                        "Complex digital text deliberately long enough for routing",
                        blocks=blocks,
                        drawings=8,
                    )
                ]
            ),
        )
        complex_route = route_pdf("auto", complex_report, self.config)
        self.assertEqual(complex_route.parser, "docling")
        self.assertEqual(complex_route.preset, "docling_standard")
        self.assertIn("complex_mixed_layout", complex_route.reasons)

    def test_preflight_rejects_active_pdf_and_wrong_dependency_version(self) -> None:
        with self.assertRaisesRegex(WorkerRuntimeError, "unsafe_source"):
            inspect_pdf(
                self.pdf,
                source_id="source-1",
                expected_sha256=self.sha256,
                config=self.config,
                pymupdf_module=_PyMuPdf([_Page("text")], javascript=True),
            )
        wrong = _PyMuPdf([_Page("text")])
        wrong.__version__ = "1.27.0"
        with self.assertRaisesRegex(WorkerRuntimeError, "dependency_version_mismatch"):
            inspect_pdf(
                self.pdf,
                source_id="source-1",
                expected_sha256=self.sha256,
                config=self.config,
                pymupdf_module=wrong,
            )

    def test_fast_parser_forces_page_chunks_and_no_ocr(self) -> None:
        pymupdf = _PyMuPdf(
            [
                _Page("First page has enough source text"),
                _Page(
                    "Second page has enough source text",
                    image_rects=[_Rect(0, 0, 10, 10)],
                ),
            ]
        )
        fast = _FastModule(["# First", "# Second"])
        parser = PyMuPdf4LlmFastParser(
            config=self.config,
            pymupdf_module=pymupdf,
            pymupdf4llm_module=fast,
        )
        document = parser.parse(
            self.pdf,
            expected_sha256=self.sha256,
            preflight=_preflight(),
        )
        self.assertEqual([page.page_number for page in document.pages], [1, 2])
        self.assertIs(fast.kwargs["page_chunks"], True)
        self.assertIs(fast.kwargs["use_ocr"], False)
        self.assertIs(fast.kwargs["force_ocr"], False)
        self.assertEqual(len(document.assets), 1)
        self.assertEqual(document.assets[0].content, _PNG)
        self.assertTrue(all(item.closed for item in pymupdf.documents))

    def test_docling_converters_are_created_once_and_selected_by_preset(self) -> None:
        standard = _Converter(2)
        ocr = _Converter(2)
        factory_calls: list[Path] = []

        def factory(root: Path) -> tuple[object, object]:
            factory_calls.append(root)
            return standard, ocr

        parser = DoclingAccurateParser(
            config=self.config,
            model_root=self.root.resolve(),
            pymupdf_module=_PyMuPdf([_Page("one"), _Page("two")]),
            converter_factory=factory,
        )
        first = parser.parse(
            self.pdf,
            expected_sha256=self.sha256,
            preflight=_preflight(),
            preset="docling_standard",
        )
        second = parser.parse(
            self.pdf,
            expected_sha256=self.sha256,
            preflight=_preflight(),
            preset="docling_ocr",
        )
        self.assertEqual(factory_calls, [self.root.resolve()])
        self.assertEqual((standard.calls, ocr.calls), (1, 1))
        self.assertEqual(first.pages[0].markdown, "# Accurate page 1\n")
        self.assertEqual(second.preset, "docling_ocr")

    def test_docling_layout_disables_runtime_compilation_and_pins_revision(self) -> None:
        options = SimpleNamespace(
            LayoutObjectDetectionOptions=_LayoutObjectDetectionOptions
        )
        configured = _configure_docling_layout_options(options)

        self.assertIs(configured.engine_options.compile_model, False)
        self.assertEqual(
            configured.model_spec.revision,
            "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
        )

    def test_quality_evaluator_accepts_healthy_and_rejects_corrupt_output(self) -> None:
        healthy = WorkerParsedDocument(
            parser="pymupdf4llm",
            parser_version="1.28.2",
            preset="pymupdf4llm_fast_no_ocr",
            pages=(
                WorkerParsedPage(
                    1,
                    "# One\n",
                    "This is the first complete page with enough deterministic text.",
                ),
                WorkerParsedPage(
                    2,
                    "# Two\n",
                    "This is the second complete page with enough deterministic text.",
                ),
            ),
            assets=(),
        )
        evaluator = QualityEvaluator(self.config, clock_ms=lambda: 99)
        accepted = evaluator.evaluate(healthy, _preflight(native_chars=104))
        self.assertTrue(accepted.passed)
        self.assertEqual(accepted.evaluated_at_ms, 99)

        missing = "images/" + "0" * 64 + ".png"
        corrupt = WorkerParsedDocument(
            parser="pymupdf4llm",
            parser_version="1.28.2",
            preset="pymupdf4llm_fast_no_ocr",
            pages=(WorkerParsedPage(1, f"Broken � ![]({missing})", ""),),
            assets=(
                WorkerParsedAsset(
                    path="images/" + "1" * 64 + ".png",
                    mime_type="image/png",
                    sha256=hashlib.sha256(_PNG).hexdigest(),
                    size_bytes=len(_PNG),
                    page_number=1,
                    content=_PNG,
                ),
            ),
        )
        rejected = evaluator.evaluate(corrupt, _preflight())
        self.assertFalse(rejected.passed)
        self.assertIn("replacement_characters", rejected.critical_failures)
        self.assertIn("asset_reference_invalid", rejected.critical_failures)

    def test_docling_model_gate_rehashes_every_selected_file(self) -> None:
        artifacts = {
            "docling-layout-heron": (
                "docling-project--docling-layout-heron/model.safetensors",
                b"layout",
            ),
            "tableformer-accurate": (
                "docling-project--docling-models/model_artifacts/tableformer/accurate/model.safetensors",
                b"table",
            ),
            "rapidocr-ppocrv6-multilingual-onnx": (
                "RapidOcr/ocr.onnx",
                b"ocr",
            ),
        }
        models: list[dict[str, object]] = []
        for name, (relative, content) in artifacts.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            manifest_path = (
                "rapidocr/models/ocr.onnx"
                if name == "rapidocr-ppocrv6-multilingual-onnx"
                else relative.split("/", maxsplit=1)[1]
            )
            models.append(
                {
                    "name": name,
                    "files": [
                        {
                            "path": manifest_path,
                            "size_bytes": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ],
                }
            )
        manifest = {"models": models}
        with patch(
            "wiki_parser_worker.supply_chain.load_runtime_manifest",
            return_value=manifest,
        ):
            verify_docling_model_artifacts(self.root)
            (self.root / artifacts["tableformer-accurate"][0]).write_bytes(b"tampered")
            with self.assertRaisesRegex(
                WorkerRuntimeError, "dependency_(unavailable|version_mismatch)"
            ):
                verify_docling_model_artifacts(self.root)


if __name__ == "__main__":
    unittest.main()

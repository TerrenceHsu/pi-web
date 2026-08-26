"""Lazy real parser adapters; concrete runtimes never enter the main app.

Copyright (C) 2026 Pi Python Port
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from .config import WorkerRoutingConfig
from .errors import WorkerRuntimeError
from .models import (
    WorkerParsedAsset,
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
)
from .preflight import verify_source_identity
from .supply_chain import (
    verify_distribution_versions,
    verify_docling_model_artifacts,
    verify_routing_config_identity,
)

_EXPECTED_PYMUPDF_VERSION = "1.28.2"
_EXPECTED_PYMUPDF4LLM_VERSION = "1.28.2"
_EXPECTED_DOCLING_SLIM_VERSION = "2.119.0"
_LAYOUT_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"

_MIME_BY_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_EXTENSION_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
ImageMimeType = Literal["image/png", "image/jpeg", "image/webp"]
LiteralDoclingPreset = Literal["docling_standard", "docling_ocr"]


def _validate_image(content: bytes, mime_type: str) -> None:
    valid = (
        mime_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n")
    ) or (
        mime_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff")
    ) or (
        mime_type == "image/webp"
        and len(content) >= 12
        and content.startswith(b"RIFF")
        and content[8:12] == b"WEBP"
    )
    if not valid:
        raise WorkerRuntimeError("parser_failed")


def _load_module(name: str, distribution: str, expected_version: str) -> Any:
    try:
        version = importlib.metadata.version(distribution)
        module = importlib.import_module(name)
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    if version != expected_version:
        raise WorkerRuntimeError("dependency_version_mismatch")
    return module


def _load_pymupdf(injected: object | None) -> Any:
    if injected is not None:
        if getattr(injected, "__version__", None) != _EXPECTED_PYMUPDF_VERSION:
            raise WorkerRuntimeError("dependency_version_mismatch")
        return injected
    return _load_module("pymupdf", "PyMuPDF", _EXPECTED_PYMUPDF_VERSION)


def extract_embedded_images(
    path: Path,
    *,
    expected_sha256: str,
    config: WorkerRoutingConfig,
    pymupdf_module: object | None = None,
) -> tuple[WorkerParsedAsset, ...]:
    """Extract only PDF embedded raster images, never rendered page screenshots."""

    identity = verify_source_identity(path, expected_sha256)
    module = _load_pymupdf(pymupdf_module)
    try:
        document = module.open(str(path))
    except Exception as error:
        raise WorkerRuntimeError("parser_failed") from error

    assets: dict[str, WorkerParsedAsset] = {}
    total_bytes = 0
    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            images = page.get_images(full=True)
            if not isinstance(images, list | tuple):
                continue
            for item in images:
                if not isinstance(item, list | tuple) or not item or not isinstance(item[0], int):
                    continue
                xref = item[0]
                if xref <= 0:
                    continue
                extracted = document.extract_image(xref)
                if not isinstance(extracted, dict):
                    raise WorkerRuntimeError("parser_failed")
                content = extracted.get("image")
                extension = extracted.get("ext")
                if not isinstance(content, bytes) or not isinstance(extension, str):
                    raise WorkerRuntimeError("parser_failed")
                mime_type = _MIME_BY_EXTENSION.get(extension.casefold())
                if mime_type is None:
                    continue
                _validate_image(content, mime_type)
                if len(content) > config.limits.max_single_image_bytes:
                    raise WorkerRuntimeError("artifact_limit_exceeded")
                digest = hashlib.sha256(content).hexdigest()
                if digest in assets:
                    continue
                total_bytes += len(content)
                if (
                    len(assets) >= config.limits.max_embedded_images
                    or total_bytes > config.limits.max_total_image_bytes
                ):
                    raise WorkerRuntimeError("artifact_limit_exceeded")
                suffix = _EXTENSION_BY_MIME[mime_type]
                assets[digest] = WorkerParsedAsset(
                    path=f"images/{digest}.{suffix}",
                    mime_type=cast(ImageMimeType, mime_type),
                    sha256=digest,
                    size_bytes=len(content),
                    page_number=page_index + 1,
                    content=content,
                )
    except WorkerRuntimeError:
        raise
    except Exception as error:
        raise WorkerRuntimeError("parser_failed") from error
    finally:
        try:
            document.close()
        except Exception:
            pass
    if verify_source_identity(path, expected_sha256) != identity:
        raise WorkerRuntimeError("source_changed")
    return tuple(assets[key] for key in sorted(assets))


def _normalize_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if not normalized or normalized.endswith("\n") else normalized + "\n"


class PyMuPdf4LlmFastParser:
    """Pinned ``page_chunks=True`` fast parser with OCR forcibly disabled."""

    def __init__(
        self,
        *,
        config: WorkerRoutingConfig,
        pymupdf_module: object | None = None,
        pymupdf4llm_module: object | None = None,
    ) -> None:
        verify_routing_config_identity(config)
        self._config = config
        self._pymupdf = _load_pymupdf(pymupdf_module)
        if pymupdf4llm_module is None:
            verify_distribution_versions(("pymupdf", "pymupdf-layout", "pymupdf4llm"))
            self._pymupdf4llm = _load_module(
                "pymupdf4llm", "pymupdf4llm", _EXPECTED_PYMUPDF4LLM_VERSION
            )
            if getattr(self._pymupdf4llm, "_use_layout", None) is not True:
                raise WorkerRuntimeError("dependency_unavailable")
        else:
            if getattr(pymupdf4llm_module, "__version__", None) != _EXPECTED_PYMUPDF4LLM_VERSION:
                raise WorkerRuntimeError("dependency_version_mismatch")
            self._pymupdf4llm = pymupdf4llm_module

    def parse(
        self,
        path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
    ) -> WorkerParsedDocument:
        identity = verify_source_identity(path, expected_sha256)
        try:
            chunks = self._pymupdf4llm.to_markdown(
                str(path),
                page_chunks=True,
                use_ocr=False,
                force_ocr=False,
                write_images=False,
                embed_images=False,
                show_progress=False,
            )
        except Exception as error:
            raise WorkerRuntimeError("parser_failed") from error
        if not isinstance(chunks, list) or len(chunks) != preflight.page_count:
            raise WorkerRuntimeError("parser_failed")

        document: Any | None = None
        try:
            document = self._pymupdf.open(str(path))
            pages: list[WorkerParsedPage] = []
            for page_index, chunk in enumerate(chunks):
                if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
                    raise WorkerRuntimeError("parser_failed")
                raw_plain = document.load_page(page_index).get_text("text")
                plain = raw_plain if isinstance(raw_plain, str) else ""
                pages.append(
                    WorkerParsedPage(
                        page_number=page_index + 1,
                        markdown=_normalize_text(chunk["text"]),
                        plain_text=_normalize_text(plain),
                    )
                )
        except WorkerRuntimeError:
            raise
        except Exception as error:
            raise WorkerRuntimeError("parser_failed") from error
        finally:
            try:
                if document is not None:
                    document.close()
            except Exception:
                pass

        assets = extract_embedded_images(
            path,
            expected_sha256=expected_sha256,
            config=self._config,
            pymupdf_module=self._pymupdf,
        )
        if verify_source_identity(path, expected_sha256) != identity:
            raise WorkerRuntimeError("source_changed")
        return WorkerParsedDocument(
            parser="pymupdf4llm",
            parser_version=_EXPECTED_PYMUPDF4LLM_VERSION,
            preset="pymupdf4llm_fast_no_ocr",
            pages=tuple(pages),
            assets=assets,
        )


ConverterFactory = Callable[[Path], tuple[object, object]]


def _configure_docling_layout_options(options: Any) -> Any:
    """Pin Heron and keep inference independent of a runtime C++ toolchain."""

    layout = options.LayoutObjectDetectionOptions.from_preset("layout_heron_default")
    engine_options = layout.engine_options.model_copy(update={"compile_model": False})
    configured = layout.model_copy(
        update={
            "engine_options": engine_options,
            "model_spec": layout.model_spec.model_copy(
                update={"revision": _LAYOUT_REVISION}
            ),
        }
    )
    if configured.engine_options.compile_model is not False:
        raise WorkerRuntimeError("invalid_configuration")
    return configured


def _create_docling_converters(model_root: Path) -> tuple[object, object]:
    verify_distribution_versions(
        (
            "docling-core",
            "docling-ibm-models",
            "docling-parse",
            "docling-slim",
            "onnxruntime",
            "pypdfium2",
            "rapidocr",
            "torch",
            "torchvision",
            "transformers",
        )
    )
    verify_docling_model_artifacts(model_root)
    try:
        if importlib.metadata.version("docling-slim") != _EXPECTED_DOCLING_SLIM_VERSION:
            raise WorkerRuntimeError("dependency_version_mismatch")
        base_models = importlib.import_module("docling.datamodel.base_models")
        options = importlib.import_module("docling.datamodel.pipeline_options")
        converter_module = importlib.import_module("docling.document_converter")
    except importlib.metadata.PackageNotFoundError as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    except ImportError as error:
        raise WorkerRuntimeError("dependency_unavailable") from error

    input_format = base_models.InputFormat.PDF
    layout = _configure_docling_layout_options(options)
    table = options.TableStructureOptions(
        mode=options.TableFormerMode.ACCURATE,
        do_cell_matching=True,
    )

    def build(*, ocr: bool) -> object:
        pipeline = options.PdfPipelineOptions(
            artifacts_path=model_root,
            enable_remote_services=False,
            allow_external_plugins=False,
            do_table_structure=True,
            table_structure_options=table,
            do_ocr=ocr,
            ocr_options=options.RapidOcrOptions(
                backend="onnxruntime",
                lang=["ch"],
            ),
            layout_options=layout,
            do_code_enrichment=False,
            do_formula_enrichment=False,
            do_picture_classification=False,
            do_picture_description=False,
        )
        converter = converter_module.DocumentConverter(
            allowed_formats=[input_format],
            format_options={
                input_format: converter_module.PdfFormatOption(
                    pipeline_options=pipeline
                )
            },
        )
        converter.initialize_pipeline(input_format)
        return converter

    return build(ocr=False), build(ocr=True)


class DoclingAccurateParser:
    """Two startup-initialized Docling converters sharing pinned offline artifacts."""

    def __init__(
        self,
        *,
        config: WorkerRoutingConfig,
        model_root: Path,
        pymupdf_module: object | None = None,
        converter_factory: ConverterFactory | None = None,
    ) -> None:
        verify_routing_config_identity(config)
        if not model_root.is_absolute() or not model_root.is_dir() or model_root.is_symlink():
            raise WorkerRuntimeError("invalid_configuration")
        self._config = config
        self._model_root = model_root
        self._pymupdf = _load_pymupdf(pymupdf_module)
        factory = converter_factory or _create_docling_converters
        try:
            self._standard_converter, self._ocr_converter = factory(model_root)
        except WorkerRuntimeError:
            raise
        except Exception as error:
            raise WorkerRuntimeError("dependency_unavailable") from error

    def parse(
        self,
        path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
        preset: LiteralDoclingPreset,
    ) -> WorkerParsedDocument:
        if preset not in {"docling_standard", "docling_ocr"}:
            raise WorkerRuntimeError("invalid_configuration")
        identity = verify_source_identity(path, expected_sha256)
        converter = (
            self._standard_converter
            if preset == "docling_standard"
            else self._ocr_converter
        )
        try:
            result = cast(Any, converter).convert(
                path,
                raises_on_error=True,
                max_num_pages=self._config.limits.max_pages,
                max_file_size=path.stat().st_size,
            )
            status = getattr(result.status, "value", result.status)
            if status != "success":
                raise WorkerRuntimeError("parser_failed")
            document = result.document
            if len(document.pages) != preflight.page_count:
                raise WorkerRuntimeError("parser_failed")
            pages = tuple(
                WorkerParsedPage(
                    page_number=page_number,
                    markdown=_normalize_text(
                        document.export_to_markdown(
                            page_no=page_number,
                            traverse_pictures=True,
                        )
                    ),
                    plain_text=_normalize_text(
                        document.export_to_text(
                            page_no=page_number,
                            traverse_pictures=True,
                        )
                    ),
                )
                for page_number in range(1, preflight.page_count + 1)
            )
        except WorkerRuntimeError:
            raise
        except Exception as error:
            raise WorkerRuntimeError("parser_failed") from error

        assets = extract_embedded_images(
            path,
            expected_sha256=expected_sha256,
            config=self._config,
            pymupdf_module=self._pymupdf,
        )
        if verify_source_identity(path, expected_sha256) != identity:
            raise WorkerRuntimeError("source_changed")
        return WorkerParsedDocument(
            parser="docling",
            parser_version=_EXPECTED_DOCLING_SLIM_VERSION,
            preset=preset,
            pages=pages,
            assets=assets,
        )


__all__ = [
    "ConverterFactory",
    "DoclingAccurateParser",
    "PyMuPdf4LlmFastParser",
    "extract_embedded_images",
]

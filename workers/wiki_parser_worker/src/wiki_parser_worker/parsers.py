"""Pinned MinerU adapter; the concrete runtime stays outside the Web process."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from .config import WorkerRoutingConfig
from .errors import WorkerRuntimeError
from .models import (
    WorkerParsedAsset,
    WorkerParsedDocument,
    WorkerParsedPage,
    WorkerPreflightReport,
    WorkerRouteDecision,
)
from .preflight import verify_source_identity
from .supply_chain import verify_distribution_versions, verify_routing_config_identity

_EXPECTED_MINERU_VERSION = "3.4.5"
_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class MineruRunner(Protocol):
    def __call__(
        self,
        output_dir: str,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
        **kwargs: object,
    ) -> None: ...


def _load_runner() -> MineruRunner:
    verify_distribution_versions(("mineru",))
    try:
        # PyTorch 2.14's optional Triton overrides JIT-compile on first GPU use.
        # This immutable, compiler-free Worker uses the built-in CUDA kernels;
        # do not make the model tree writable or remove /tmp's noexec boundary.
        native = importlib.import_module("torch.backends.python_native")
        native.triton.disable()
        module = importlib.import_module("mineru.cli.common")
        runner = module.do_parse
    except (AttributeError, ImportError) as error:
        raise WorkerRuntimeError("dependency_unavailable") from error
    return cast(MineruRunner, runner)


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


def _normalize_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{normalized}\n" if normalized else ""


def _safe_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise WorkerRuntimeError("parser_failed")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerRuntimeError("parser_failed") from error


def _find_one(root: Path, name: str) -> Path:
    matches = [path for path in root.rglob(name) if path.is_file() and not path.is_symlink()]
    if len(matches) != 1:
        raise WorkerRuntimeError("parser_failed")
    return matches[0]


def _content_items(payload: object) -> Iterable[tuple[int, dict[str, object]]]:
    if not isinstance(payload, list):
        raise WorkerRuntimeError("parser_failed")
    for outer_index, raw in enumerate(payload):
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise WorkerRuntimeError("parser_failed")
            item = cast(dict[str, object], candidate)
            raw_page = item.get("page_idx", outer_index)
            if not isinstance(raw_page, int) or isinstance(raw_page, bool) or raw_page < 0:
                raise WorkerRuntimeError("parser_failed")
            yield raw_page, item


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _item_text(item: dict[str, object]) -> str:
    parts: list[str] = []
    for key in (
        "text",
        "content",
        "table_body",
        "code_body",
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
        "chart_caption",
        "chart_footnote",
        "list_items",
    ):
        parts.extend(_strings(item.get(key)))
    return "\n".join(part.strip() for part in parts if part.strip())


def _image_name(item: dict[str, object]) -> str | None:
    raw = item.get("img_path") or item.get("image_path")
    if not isinstance(raw, str) or not raw:
        return None
    pure = PurePosixPath(raw.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise WorkerRuntimeError("parser_failed")
    return pure.name


def _render_item(item: dict[str, object], image_paths: dict[str, str]) -> str:
    kind = item.get("type")
    kind = kind if isinstance(kind, str) else "text"
    text = _item_text(item)
    image_name = _image_name(item)
    image = image_paths.get(image_name) if image_name is not None else None
    rendered: list[str] = []
    if image is not None:
        rendered.append(f"![MinerU extracted image]({image})")
    if kind in {"title", "text"}:
        level = item.get("text_level")
        if kind == "title" or isinstance(level, int) and level > 0:
            heading = min(max(level if isinstance(level, int) else 1, 1), 6)
            rendered.append(f"{'#' * heading} {text}" if text else "")
        else:
            rendered.append(text)
    elif kind == "equation":
        rendered.append(f"$$\n{text}\n$$" if text else "")
    elif kind == "code":
        rendered.append(f"```\n{text}\n```" if text else "")
    elif kind == "list":
        rendered.extend(f"- {line}" for line in text.splitlines() if line)
    else:
        rendered.append(text)
    return "\n\n".join(part for part in rendered if part)


class MineruParser:
    """Run one fixed MinerU profile and normalize its structured output."""

    def __init__(
        self,
        *,
        config: WorkerRoutingConfig,
        runner: MineruRunner | None = None,
        version: str | None = None,
    ) -> None:
        verify_routing_config_identity(config)
        self._config = config
        self._runtime_loaded = runner is None
        self._runner = runner or _load_runner()
        if version is None:
            try:
                version = importlib.metadata.version("mineru")
            except importlib.metadata.PackageNotFoundError as error:
                raise WorkerRuntimeError("dependency_unavailable") from error
        if version != _EXPECTED_MINERU_VERSION:
            raise WorkerRuntimeError("dependency_version_mismatch")
        self._version = version

    def parse(
        self,
        path: Path,
        *,
        expected_sha256: str,
        preflight: WorkerPreflightReport,
        route: WorkerRouteDecision,
    ) -> WorkerParsedDocument:
        if self._runtime_loaded and route.backend == "hybrid-engine":
            try:
                torch = importlib.import_module("torch")
            except ImportError as error:
                raise WorkerRuntimeError("dependency_unavailable") from error
            if not bool(torch.cuda.is_available()):
                raise WorkerRuntimeError("dependency_unavailable")
        identity = verify_source_identity(path, expected_sha256)
        try:
            source_bytes = path.read_bytes()
        except OSError as error:
            raise WorkerRuntimeError("source_changed") from error

        with tempfile.TemporaryDirectory(prefix="mineru-worker-") as temporary:
            output_root = Path(temporary)
            try:
                self._runner(
                    str(output_root),
                    ["source"],
                    [source_bytes],
                    ["ch"],
                    backend=route.backend,
                    parse_method="auto",
                    formula_enable=True,
                    table_enable=True,
                    f_draw_layout_bbox=False,
                    f_draw_span_bbox=False,
                    f_dump_md=True,
                    f_dump_middle_json=True,
                    f_dump_model_output=False,
                    f_dump_orig_pdf=False,
                    f_dump_content_list=True,
                    image_analysis=route.effort == "high",
                    effort=route.effort or "medium",
                )
            except WorkerRuntimeError:
                raise
            except Exception as error:
                raise WorkerRuntimeError("parser_failed") from error
            document = self._read_output(output_root, preflight=preflight, route=route)

        if verify_source_identity(path, expected_sha256) != identity:
            raise WorkerRuntimeError("source_changed")
        return document

    def _read_output(
        self,
        root: Path,
        *,
        preflight: WorkerPreflightReport,
        route: WorkerRouteDecision,
    ) -> WorkerParsedDocument:
        content_path = _find_one(root, "source_content_list.json")
        middle_path = _find_one(root, "source_middle.json")
        middle = _safe_json(middle_path)
        if not isinstance(middle, dict) or not isinstance(middle.get("pdf_info"), list):
            raise WorkerRuntimeError("parser_failed")
        if len(middle["pdf_info"]) != preflight.page_count:
            raise WorkerRuntimeError("parser_failed")
        items = tuple(_content_items(_safe_json(content_path)))
        if any(page_index >= preflight.page_count for page_index, _ in items):
            raise WorkerRuntimeError("parser_failed")

        image_pages: dict[str, int] = {}
        for page_index, item in items:
            name = _image_name(item)
            if name is not None:
                image_pages.setdefault(name, page_index + 1)
        assets, image_paths = self._read_assets(content_path.parent, image_pages)
        page_parts: list[list[str]] = [[] for _ in range(preflight.page_count)]
        for page_index, item in items:
            rendered = _render_item(item, image_paths)
            if rendered:
                page_parts[page_index].append(rendered)
        pages = tuple(
            WorkerParsedPage(
                page_number=index + 1,
                markdown=_normalize_text("\n\n".join(parts)),
                plain_text=_normalize_text(
                    "\n".join(
                        _item_text(item)
                        for page_index, item in items
                        if page_index == index and _item_text(item)
                    )
                ),
            )
            for index, parts in enumerate(page_parts)
        )
        return WorkerParsedDocument(
            parser="mineru",
            parser_version=self._version,
            preset=route.preset,
            pages=pages,
            assets=assets,
        )

    def _read_assets(
        self,
        output_dir: Path,
        image_pages: dict[str, int],
    ) -> tuple[tuple[WorkerParsedAsset, ...], dict[str, str]]:
        images_dir = output_dir / "images"
        if not images_dir.exists():
            return (), {}
        if images_dir.is_symlink() or not images_dir.is_dir():
            raise WorkerRuntimeError("parser_failed")
        assets: list[WorkerParsedAsset] = []
        assets_by_digest: dict[str, str] = {}
        image_paths: dict[str, str] = {}
        total = 0
        for path in sorted(images_dir.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                raise WorkerRuntimeError("parser_failed")
            mime = _MIME_BY_SUFFIX.get(path.suffix.casefold())
            if mime is None:
                continue
            try:
                content = path.read_bytes()
            except OSError as error:
                raise WorkerRuntimeError("parser_failed") from error
            _validate_image(content, mime)
            digest = hashlib.sha256(content).hexdigest()
            suffix = ".jpg" if mime == "image/jpeg" else path.suffix.casefold()
            normalized_path = f"images/{digest}{suffix}"
            existing_path = assets_by_digest.get(digest)
            if existing_path is not None:
                image_paths[path.name] = existing_path
                continue
            total += len(content)
            if (
                len(content) > self._config.limits.max_single_image_bytes
                or len(assets) >= self._config.limits.max_embedded_images
                or total > self._config.limits.max_total_image_bytes
            ):
                raise WorkerRuntimeError("artifact_limit_exceeded")
            page_number = image_pages.get(path.name, 1)
            assets.append(
                WorkerParsedAsset(
                    path=normalized_path,
                    mime_type=cast(Any, mime),
                    sha256=digest,
                    size_bytes=len(content),
                    page_number=page_number,
                    content=content,
                )
            )
            assets_by_digest[digest] = normalized_path
            image_paths[path.name] = normalized_path
        return tuple(assets), image_paths


__all__ = ["MineruParser", "MineruRunner"]

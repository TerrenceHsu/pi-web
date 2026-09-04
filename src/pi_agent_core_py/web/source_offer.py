"""Safe, deterministic Corresponding Source offer for the isolated parser Worker."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from .. import __version__

_COMPONENT_ID: Final = "wiki-parser-worker"
_MANIFEST_NAME: Final = "component-manifest.json"
_LICENSE_NAME: Final = "LICENSE"
_NOTICE_NAME: Final = "NOTICE.md"
_SBOM_NAME: Final = "sbom.spdx.json"
_MAX_FILE_COUNT: Final = 512
_MAX_FILE_BYTES: Final = 16 * 1024 * 1024
_MAX_TOTAL_BYTES: Final = 64 * 1024 * 1024
_ALLOWED_ROLES: Final = frozenset(
    {
        "build",
        "configuration",
        "documentation",
        "license",
        "manifest",
        "notice",
        "runtime_manifest",
        "sbom",
        "source",
        "source_offer",
        "third_party_license",
    }
)
_IGNORED_DIRECTORY_NAMES: Final = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".pytest-tmp",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
)


class SourceOfferError(RuntimeError):
    """The configured source tree is incomplete, unsafe, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class SourceOfferFile:
    path: str
    role: str
    size: int
    sha256: str
    content: bytes

    def public_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "role": self.role,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SourceOfferSnapshot:
    component_id: str
    name: str
    version: str
    license_expression: str
    runtime_ready: bool
    archive_name: str
    files: tuple[SourceOfferFile, ...]
    source_tree_sha256: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "component_id": self.component_id,
            "name": self.name,
            "version": self.version,
            "license_expression": self.license_expression,
            "runtime_ready": self.runtime_ready,
            "source_tree_sha256": self.source_tree_sha256,
            "file_count": len(self.files),
            "total_size": sum(item.size for item in self.files),
            "files": [item.public_dict() for item in self.files],
            "archive_url": f"/api/about/{self.component_id}/source",
            "license_url": f"/api/about/{self.component_id}/license",
            "notices_url": f"/api/about/{self.component_id}/notices",
            "sbom_url": f"/api/about/{self.component_id}/sbom",
        }


def _has_reparse_attribute(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _stable_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise SourceOfferError("source manifest contains an invalid file path")
    if "\\" in value or any(ord(character) < 32 for character in value):
        raise SourceOfferError("source manifest contains an unsafe file path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or value != candidate.as_posix():
        raise SourceOfferError("source manifest contains a non-canonical file path")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise SourceOfferError("source manifest contains a traversal file path")
    return value


def _read_stable_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SourceOfferError("source offer file is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or _has_reparse_attribute(before):
        raise SourceOfferError("source offer rejects links and reparse points")
    if not stat.S_ISREG(before.st_mode):
        raise SourceOfferError("source offer contains a non-regular file")
    if before.st_size > _MAX_FILE_BYTES:
        raise SourceOfferError("source offer file exceeds the size limit")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SourceOfferError("source offer file changed type while reading")
            if _stable_identity(opened) != _stable_identity(before):
                raise SourceOfferError("source offer file changed before reading")
            content = stream.read(_MAX_FILE_BYTES + 1)
        after = path.lstat()
    except SourceOfferError:
        raise
    except OSError as exc:
        raise SourceOfferError("source offer file could not be read") from exc
    if len(content) > _MAX_FILE_BYTES:
        raise SourceOfferError("source offer file exceeds the size limit")
    if _stable_identity(after) != _stable_identity(before):
        raise SourceOfferError("source offer file changed while reading")
    return content


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_generated_part(part: str) -> bool:
    return (
        part in _IGNORED_DIRECTORY_NAMES
        or part == ".coverage"
        or part.startswith(".coverage.")
        or part.endswith(".egg-info")
    )


def _listed_regular_files(root: Path) -> set[str]:
    discovered: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SourceOfferError("source offer tree could not be enumerated") from exc
        for entry in entries:
            relative = entry.relative_to(root)
            if any(_is_generated_part(part) for part in relative.parts):
                continue
            try:
                entry_stat = entry.lstat()
            except OSError as exc:
                raise SourceOfferError("source offer entry is unavailable") from exc
            if stat.S_ISLNK(entry_stat.st_mode) or _has_reparse_attribute(entry_stat):
                raise SourceOfferError("source offer rejects links and reparse points")
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(entry)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise SourceOfferError("source offer tree contains a special file")
            if entry.suffix == ".pyc":
                continue
            discovered.add(relative.as_posix())
    return discovered


class SourceOfferService:
    """Validate and publish one exact, deterministic Worker source tree."""

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root)
        try:
            root_stat = candidate.lstat()
        except OSError as exc:
            raise SourceOfferError("source offer root is unavailable") from exc
        if stat.S_ISLNK(root_stat.st_mode) or _has_reparse_attribute(root_stat):
            raise SourceOfferError("source offer root cannot be a link or reparse point")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise SourceOfferError("source offer root is not a directory")
        self._root = candidate.resolve(strict=True)
        self.snapshot()

    def snapshot(self) -> SourceOfferSnapshot:
        manifest_bytes = _read_stable_regular_file(self._root / _MANIFEST_NAME)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceOfferError("source manifest is not canonical UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise SourceOfferError("source manifest must be an object")
        expected_keys = {
            "component_id",
            "files",
            "license_expression",
            "name",
            "runtime_ready",
            "schema_version",
            "source_archive_name",
            "version",
        }
        if set(manifest) != expected_keys:
            raise SourceOfferError("source manifest has unsupported fields")
        if manifest.get("schema_version") != 1:
            raise SourceOfferError("source manifest schema version is unsupported")
        if manifest.get("component_id") != _COMPONENT_ID:
            raise SourceOfferError("source manifest component identity is invalid")
        if manifest.get("license_expression") != "MIT":
            raise SourceOfferError("source manifest license expression is invalid")
        if not isinstance(manifest.get("name"), str) or not manifest["name"]:
            raise SourceOfferError("source manifest name is invalid")
        if not isinstance(manifest.get("version"), str) or not manifest["version"]:
            raise SourceOfferError("source manifest version is invalid")
        if not isinstance(manifest.get("runtime_ready"), bool):
            raise SourceOfferError("source manifest runtime flag is invalid")
        expected_archive_name = f"wiki-parser-worker-{manifest['version']}-source.tar.gz"
        if manifest.get("source_archive_name") != expected_archive_name:
            raise SourceOfferError("source manifest archive name is invalid")
        declared_files = manifest.get("files")
        if not isinstance(declared_files, list) or not declared_files:
            raise SourceOfferError("source manifest file list is invalid")
        if len(declared_files) > _MAX_FILE_COUNT:
            raise SourceOfferError("source manifest contains too many files")

        declared: list[tuple[str, str]] = []
        exact_paths: set[str] = set()
        folded_paths: set[str] = set()
        for item in declared_files:
            if not isinstance(item, dict) or set(item) != {"path", "role"}:
                raise SourceOfferError("source manifest file entry is invalid")
            relative_path = _validate_relative_path(item.get("path"))
            role = item.get("role")
            if not isinstance(role, str) or role not in _ALLOWED_ROLES:
                raise SourceOfferError("source manifest file role is invalid")
            folded = relative_path.casefold()
            if relative_path in exact_paths or folded in folded_paths:
                raise SourceOfferError("source manifest contains duplicate file paths")
            exact_paths.add(relative_path)
            folded_paths.add(folded)
            declared.append((relative_path, role))
        if {_MANIFEST_NAME, _LICENSE_NAME, _NOTICE_NAME, _SBOM_NAME} - exact_paths:
            raise SourceOfferError("source manifest omits a required compliance asset")
        if _listed_regular_files(self._root) != exact_paths:
            raise SourceOfferError("source manifest does not exactly describe the source tree")

        files: list[SourceOfferFile] = []
        total_size = 0
        for relative_path, role in sorted(declared, key=lambda item: item[0]):
            content = _read_stable_regular_file(
                self._root.joinpath(*PurePosixPath(relative_path).parts)
            )
            total_size += len(content)
            if total_size > _MAX_TOTAL_BYTES:
                raise SourceOfferError("source offer exceeds the total size limit")
            content_sha256 = hashlib.sha256(content).hexdigest()
            if relative_path == _LICENSE_NAME and not content.startswith(b"MIT License"):
                raise SourceOfferError("the Worker MIT license text was modified")
            files.append(
                SourceOfferFile(
                    path=relative_path,
                    role=role,
                    size=len(content),
                    sha256=content_sha256,
                    content=content,
                )
            )
        evidence = [item.public_dict() for item in files]
        source_tree_sha256 = hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
        return SourceOfferSnapshot(
            component_id=_COMPONENT_ID,
            name=manifest["name"],
            version=manifest["version"],
            license_expression="MIT",
            runtime_ready=manifest["runtime_ready"],
            archive_name=manifest["source_archive_name"],
            files=tuple(files),
            source_tree_sha256=source_tree_sha256,
        )

    def read_asset(self, relative_path: str) -> bytes:
        snapshot = self.snapshot()
        for item in snapshot.files:
            if item.path == relative_path:
                return item.content
        raise SourceOfferError("source offer asset is not declared")

    def build_archive(self) -> tuple[SourceOfferSnapshot, bytes, str]:
        snapshot = self.snapshot()
        raw_tar = io.BytesIO()
        archive_root = f"wiki-parser-worker-{snapshot.version}"
        with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for item in snapshot.files:
                member = tarfile.TarInfo(name=f"{archive_root}/{item.path}")
                member.size = item.size
                member.mode = 0o644
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                archive.addfile(member, io.BytesIO(item.content))
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as stream:
            stream.write(raw_tar.getvalue())
        archive_bytes = compressed.getvalue()
        return snapshot, archive_bytes, hashlib.sha256(archive_bytes).hexdigest()


def default_worker_source_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[3] / "workers" / "wiki_parser_worker"
    return candidate if candidate.is_dir() else None


def build_source_offer_service(root: str | Path | None) -> SourceOfferService | None:
    selected = Path(root) if root is not None else default_worker_source_root()
    return SourceOfferService(selected) if selected is not None else None


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "source_offer_unavailable",
            "message": "The Worker Corresponding Source is not configured for this deployment.",
        },
    )


def build_about_router(service: SourceOfferService | None) -> APIRouter:
    router = APIRouter(tags=["about"])

    @router.get("/api/about/licenses")
    async def licenses() -> dict[str, Any]:
        component: dict[str, Any] = {
            "component_id": _COMPONENT_ID,
            "name": "pi Wiki Parser Worker",
            "version": __version__,
            "license_expression": "MIT",
            "runtime_ready": False,
            "source_offer_available": service is not None,
            "source_offer_url": f"/api/about/{_COMPONENT_ID}/source-offer",
            "source_archive_url": f"/api/about/{_COMPONENT_ID}/source",
            "license_url": f"/api/about/{_COMPONENT_ID}/license",
            "notices_url": f"/api/about/{_COMPONENT_ID}/notices",
            "sbom_url": f"/api/about/{_COMPONENT_ID}/sbom",
        }
        if service is not None:
            snapshot = service.snapshot()
            component.update(
                version=snapshot.version,
                runtime_ready=snapshot.runtime_ready,
                source_tree_sha256=snapshot.source_tree_sha256,
            )
        return {
            "application": {
                "name": "pi-agent-core-py",
                "version": __version__,
                "license_expression": "MIT",
            },
            "components": [component],
            "legal_notice": (
                "The MinerU Worker is separately packaged under MIT and uses the separately "
                "licensed MinerU runtime; both are provided without warranty."
            ),
        }

    def require_service() -> SourceOfferService:
        if service is None:
            raise _unavailable()
        return service

    @router.get(f"/api/about/{_COMPONENT_ID}/source-offer")
    async def source_offer() -> dict[str, Any]:
        return require_service().snapshot().public_dict()

    @router.get(f"/api/about/{_COMPONENT_ID}/source")
    async def source_archive() -> Response:
        snapshot, content, archive_sha256 = require_service().build_archive()
        return Response(
            content=content,
            media_type="application/gzip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{snapshot.archive_name}"',
                "X-Archive-SHA256": archive_sha256,
                "X-Source-Tree-SHA256": snapshot.source_tree_sha256,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get(f"/api/about/{_COMPONENT_ID}/license")
    async def worker_license() -> Response:
        return Response(
            content=require_service().read_asset(_LICENSE_NAME),
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(f"/api/about/{_COMPONENT_ID}/notices")
    async def worker_notices() -> Response:
        return Response(
            content=require_service().read_asset(_NOTICE_NAME),
            media_type="text/markdown; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(f"/api/about/{_COMPONENT_ID}/sbom")
    async def worker_sbom() -> Response:
        return Response(
            content=require_service().read_asset(_SBOM_NAME),
            media_type="application/spdx+json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'inline; filename="wiki-parser-worker.spdx.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


__all__ = [
    "SourceOfferError",
    "SourceOfferFile",
    "SourceOfferService",
    "SourceOfferSnapshot",
    "build_about_router",
    "build_source_offer_service",
    "default_worker_source_root",
]

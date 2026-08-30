"""Deterministic project snapshots with conservative filesystem boundaries."""

from __future__ import annotations

import asyncio
import fnmatch
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import SandboxLimits
from .workspace_models import validate_workspace_relative_path

SNAPSHOT_SCHEMA_VERSION: Literal["pi-agent-coding-snapshot/v1"] = "pi-agent-coding-snapshot/v1"
ARCHIVE_WORKSPACE_PREFIX = "workspace/"
ARCHIVE_MANIFEST_PATH = "metadata/base-manifest.json"
_HASH_CHUNK_SIZE = 1024 * 1024

SnapshotErrorCode = Literal[
    "invalid_root",
    "unsafe_path",
    "unsupported_file",
    "file_too_large",
    "too_many_files",
    "snapshot_too_large",
    "file_changed",
    "archive_invalid",
    "archive_write_failed",
]

_SNAPSHOT_ERROR_MESSAGES: dict[SnapshotErrorCode, str] = {
    "invalid_root": "Project root is invalid.",
    "unsafe_path": "Project contains an unsafe path.",
    "unsupported_file": "Project contains an unsupported file type.",
    "file_too_large": "Project contains a file that exceeds the size limit.",
    "too_many_files": "Project exceeds the file count limit.",
    "snapshot_too_large": "Project snapshot exceeds the total size limit.",
    "file_changed": "Project file changed while the snapshot was being created.",
    "archive_invalid": "Project snapshot archive is invalid.",
    "archive_write_failed": "Project snapshot archive could not be written.",
}

_DEFAULT_EXCLUDED_DIRECTORY_NAMES = (
    ".git",
    ".hg",
    ".svn",
    ".conda",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".pi-agent-data",
    ".run-logs",
    ".test-tmp",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "venv",
)

_DEFAULT_EXCLUDED_FILE_NAMES = (
    ".coverage",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
)

_DEFAULT_EXCLUDED_GLOBS = (
    ".env",
    ".env.*",
    "*.key",
    "*.kdbx",
    "*.p12",
    "*.pfx",
    "*.pem",
    "*.pyc",
    "*.pyo",
)


class SnapshotError(Exception):
    """Fixed-code snapshot failure with an optional safe relative path."""

    def __init__(
        self,
        code: SnapshotErrorCode,
        *,
        relative_path: str | None = None,
    ) -> None:
        super().__init__(_SNAPSHOT_ERROR_MESSAGES[code])
        self.code = code
        self.relative_path = relative_path

    def __repr__(self) -> str:
        return f"SnapshotError(code={self.code!r}, relative_path={self.relative_path!r})"


class SnapshotPolicy(BaseModel):
    """Immutable local selection and quota policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_file_count: int = Field(default=20_000, ge=1, le=1_000_000)
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    max_total_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    excluded_directory_names: tuple[str, ...] = _DEFAULT_EXCLUDED_DIRECTORY_NAMES
    excluded_file_names: tuple[str, ...] = _DEFAULT_EXCLUDED_FILE_NAMES
    excluded_globs: tuple[str, ...] = _DEFAULT_EXCLUDED_GLOBS

    @field_validator(
        "excluded_directory_names",
        "excluded_file_names",
        "excluded_globs",
    )
    @classmethod
    def _validate_exclusions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or "\x00" in value for value in normalized):
            raise ValueError("snapshot exclusions must be non-empty and contain no NUL")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("snapshot exclusions must be case-insensitively unique")
        return normalized

    @classmethod
    def from_limits(cls, limits: SandboxLimits) -> SnapshotPolicy:
        return cls(
            max_file_count=limits.max_file_count,
            max_file_bytes=limits.max_file_bytes,
            max_total_bytes=limits.max_upload_bytes,
        )


class SnapshotManifestEntry(BaseModel):
    """One regular file in the exact source snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: int = Field(ge=0, le=0o777)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or "\x00" in value
            or str(path) != value
        ):
            raise ValueError("manifest entry path must be normalized and relative")
        return value


class SnapshotManifest(BaseModel):
    """Canonical, self-verifying manifest embedded in the archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["pi-agent-coding-snapshot/v1"] = SNAPSHOT_SCHEMA_VERSION
    entries: tuple[SnapshotManifestEntry, ...]
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_manifest(self) -> SnapshotManifest:
        paths = tuple(entry.path for entry in self.entries)
        if paths != tuple(sorted(paths)):
            raise ValueError("manifest entries must be sorted by path")
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("manifest paths must be case-insensitively unique")
        if self.file_count != len(self.entries):
            raise ValueError("manifest file_count does not match entries")
        if self.total_bytes != sum(entry.size for entry in self.entries):
            raise ValueError("manifest total_bytes does not match entries")
        expected = _manifest_digest(self.entries, self.file_count, self.total_bytes)
        if self.manifest_sha256 != expected:
            raise ValueError("manifest_sha256 does not match canonical content")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class ProjectSnapshot(BaseModel):
    """Local archive plus the evidence required before provider upload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_path: Path
    archive_size: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: SnapshotManifest
    policy: SnapshotPolicy = Field(default_factory=SnapshotPolicy)


async def build_project_snapshot(
    root: Path,
    archive_path: Path,
    *,
    policy: SnapshotPolicy | None = None,
) -> ProjectSnapshot:
    """Build a deterministic gzip tar without blocking the event loop."""
    selected_policy = policy or SnapshotPolicy()
    return await asyncio.to_thread(
        _build_project_snapshot_sync,
        root,
        archive_path,
        selected_policy,
    )


def scan_project_manifest(
    root: Path,
    *,
    policy: SnapshotPolicy | None = None,
) -> SnapshotManifest:
    """Synchronously scan a project for lock-held conflict verification."""
    selected_policy = policy or SnapshotPolicy()
    resolved_root = _validate_root(root)
    return _make_manifest(_collect_entries(resolved_root, selected_policy))


def read_snapshot_file(snapshot: ProjectSnapshot, relative_path: str) -> bytes | None:
    """Read one exact baseline member without extracting the archive to disk."""
    try:
        validate_workspace_relative_path(relative_path)
    except Exception as exc:
        raise SnapshotError("unsafe_path", relative_path=relative_path) from exc
    expected = next(
        (entry for entry in snapshot.manifest.entries if entry.path == relative_path),
        None,
    )
    if expected is None:
        return None
    try:
        with tarfile.open(snapshot.archive_path, mode="r:gz") as archive:
            member = archive.getmember(ARCHIVE_WORKSPACE_PREFIX + relative_path)
            extracted = archive.extractfile(member)
            if extracted is None or not member.isfile():
                raise SnapshotError("archive_invalid")
            content = extracted.read(expected.size + 1)
    except SnapshotError:
        raise
    except (KeyError, OSError, tarfile.TarError) as exc:
        raise SnapshotError("archive_invalid") from exc
    if len(content) != expected.size or hashlib.sha256(content).hexdigest() != expected.sha256:
        raise SnapshotError("archive_invalid")
    return content


def validate_snapshot_archive(
    archive_path: Path,
    *,
    policy: SnapshotPolicy | None = None,
) -> SnapshotManifest:
    """Validate structure, quotas and embedded manifest without extracting."""
    selected_policy = policy or SnapshotPolicy()
    try:
        archive_size = archive_path.stat().st_size
    except OSError as exc:
        raise SnapshotError("archive_invalid") from exc
    if archive_size > selected_policy.max_total_bytes:
        raise SnapshotError("snapshot_too_large")

    entries: list[SnapshotManifestEntry] = []
    manifest_bytes: bytes | None = None
    total_bytes = 0
    seen_names: set[str] = set()
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                _validate_archive_member(member)
                folded_name = member.name.casefold()
                if folded_name in seen_names:
                    raise SnapshotError("archive_invalid")
                seen_names.add(folded_name)
                if member.isdir():
                    continue
                if member.name == ARCHIVE_MANIFEST_PATH:
                    if manifest_bytes is not None:
                        raise SnapshotError("archive_invalid")
                    extracted = archive.extractfile(member)
                    if extracted is None or member.size > 4 * 1024 * 1024:
                        raise SnapshotError("archive_invalid")
                    manifest_bytes = extracted.read()
                    if len(manifest_bytes) != member.size:
                        raise SnapshotError("archive_invalid")
                    continue
                relative = member.name.removeprefix(ARCHIVE_WORKSPACE_PREFIX)
                if member.size > selected_policy.max_file_bytes:
                    raise SnapshotError("file_too_large", relative_path=relative)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise SnapshotError("archive_invalid")
                actual_size, digest = _hash_archive_member(
                    extracted,
                    declared_size=member.size,
                    max_bytes=selected_policy.max_file_bytes,
                    relative_path=relative,
                )
                total_bytes += actual_size
                if total_bytes > selected_policy.max_total_bytes:
                    raise SnapshotError("snapshot_too_large")
                entries.append(
                    SnapshotManifestEntry(
                        path=relative,
                        size=actual_size,
                        sha256=digest,
                        mode=member.mode & 0o777,
                    )
                )
                if len(entries) > selected_policy.max_file_count:
                    raise SnapshotError("too_many_files")
    except SnapshotError:
        raise
    except (OSError, tarfile.TarError, UnicodeError, ValidationError) as exc:
        raise SnapshotError("archive_invalid") from exc

    if manifest_bytes is None:
        raise SnapshotError("archive_invalid")
    try:
        payload = json.loads(manifest_bytes)
        manifest = SnapshotManifest.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise SnapshotError("archive_invalid") from exc

    archive_shape = tuple((entry.path, entry.size, entry.sha256, entry.mode) for entry in entries)
    manifest_shape = tuple(
        (entry.path, entry.size, entry.sha256, entry.mode) for entry in manifest.entries
    )
    if archive_shape != manifest_shape:
        raise SnapshotError("archive_invalid")
    return manifest


def _build_project_snapshot_sync(
    root: Path,
    archive_path: Path,
    policy: SnapshotPolicy,
) -> ProjectSnapshot:
    resolved_root = _validate_root(root)
    _validate_archive_destination(resolved_root, archive_path)
    entries = _collect_entries(resolved_root, policy)
    manifest = _make_manifest(entries)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = archive_path.with_name(f".{archive_path.name}.{uuid4().hex}.tmp")
    try:
        _write_archive(resolved_root, temp_path, manifest)
        validated_manifest = validate_snapshot_archive(temp_path, policy=policy)
        if validated_manifest.manifest_sha256 != manifest.manifest_sha256:
            raise SnapshotError("archive_invalid")
        temp_path.replace(archive_path)
    except SnapshotError:
        temp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise SnapshotError("archive_write_failed") from exc

    archive_size, archive_sha256 = _hash_file_unbounded(archive_path)
    return ProjectSnapshot(
        archive_path=archive_path.resolve(strict=True),
        archive_size=archive_size,
        archive_sha256=archive_sha256,
        manifest=manifest,
        policy=policy,
    )


def _validate_root(root: Path) -> Path:
    try:
        if not root.exists() or not root.is_dir():
            raise SnapshotError("invalid_root")
        if _is_link_or_reparse(root):
            raise SnapshotError("invalid_root")
        return root.resolve(strict=True)
    except SnapshotError as exc:
        raise SnapshotError("invalid_root") from exc
    except OSError as exc:
        raise SnapshotError("invalid_root") from exc


def _validate_archive_destination(root: Path, archive_path: Path) -> None:
    try:
        target = archive_path.resolve(strict=False)
        target.relative_to(root)
    except ValueError:
        return
    except OSError as exc:
        raise SnapshotError("archive_write_failed") from exc
    raise SnapshotError("unsafe_path")


def _collect_entries(root: Path, policy: SnapshotPolicy) -> tuple[SnapshotManifestEntry, ...]:
    entries: list[SnapshotManifestEntry] = []
    total_bytes = 0
    casefold_paths: set[str] = set()

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        nonlocal total_bytes
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError as exc:
            raise SnapshotError("unsafe_path", relative_path=str(relative_directory)) from exc
        for child in children:
            relative = relative_directory / child.name
            relative_text = relative.as_posix()
            if _is_link_or_reparse(child):
                raise SnapshotError("unsafe_path", relative_path=relative_text)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotError("unsafe_path", relative_path=relative_text) from exc
            if stat.S_ISDIR(child_stat.st_mode):
                if _excluded_directory(child.name, relative_text, policy):
                    continue
                visit(child, relative)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise SnapshotError("unsupported_file", relative_path=relative_text)
            if _excluded_file(child.name, relative_text, policy):
                continue
            folded = relative_text.casefold()
            if folded in casefold_paths:
                raise SnapshotError("unsafe_path", relative_path=relative_text)
            casefold_paths.add(folded)
            if child_stat.st_size > policy.max_file_bytes:
                raise SnapshotError("file_too_large", relative_path=relative_text)
            if len(entries) >= policy.max_file_count:
                raise SnapshotError("too_many_files")
            size, digest, final_stat = _hash_stable_file(
                child,
                child_stat,
                policy.max_file_bytes,
                relative_text,
            )
            total_bytes += size
            if total_bytes > policy.max_total_bytes:
                raise SnapshotError("snapshot_too_large")
            entries.append(
                SnapshotManifestEntry(
                    path=relative_text,
                    size=size,
                    sha256=digest,
                    mode=stat.S_IMODE(final_stat.st_mode),
                )
            )

    visit(root, PurePosixPath())
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _excluded_directory(name: str, relative_path: str, policy: SnapshotPolicy) -> bool:
    if name.casefold() in {value.casefold() for value in policy.excluded_directory_names}:
        return True
    return _matches_glob(relative_path, policy.excluded_globs)


def _excluded_file(name: str, relative_path: str, policy: SnapshotPolicy) -> bool:
    if name.casefold() in {value.casefold() for value in policy.excluded_file_names}:
        return True
    return _matches_glob(relative_path, policy.excluded_globs) or _matches_glob(
        name,
        policy.excluded_globs,
    )


def _matches_glob(value: str, patterns: Iterable[str]) -> bool:
    folded = value.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def is_snapshot_path_excluded(relative_path: str, policy: SnapshotPolicy) -> bool:
    """Return whether one canonical file path is outside ``policy``'s snapshot."""
    validate_workspace_relative_path(relative_path)
    path = PurePosixPath(relative_path)
    for index, part in enumerate(path.parts[:-1], start=1):
        directory_path = PurePosixPath(*path.parts[:index]).as_posix()
        if _excluded_directory(part, directory_path, policy):
            return True
    return _excluded_file(path.name, path.as_posix(), policy)


def _hash_stable_file(
    path: Path,
    initial_stat: os.stat_result,
    max_bytes: int,
    relative_path: str,
) -> tuple[int, str, os.stat_result]:
    digest = hashlib.sha256()
    size = 0
    try:
        if not stat.S_ISREG(initial_stat.st_mode) or _is_link_or_reparse(path):
            raise SnapshotError("file_changed", relative_path=relative_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_stat = os.fstat(stream.fileno())
            if _file_identity(initial_stat) != _file_identity(opened_stat):
                raise SnapshotError("file_changed", relative_path=relative_path)
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise SnapshotError("file_too_large", relative_path=relative_path)
                digest.update(chunk)
            final_stat = os.fstat(stream.fileno())
    except SnapshotError:
        raise
    except OSError as exc:
        raise SnapshotError("file_changed", relative_path=relative_path) from exc
    if _file_identity(initial_stat) != _file_identity(final_stat) or size != final_stat.st_size:
        raise SnapshotError("file_changed", relative_path=relative_path)
    return size, digest.hexdigest(), final_stat


def _make_manifest(entries: tuple[SnapshotManifestEntry, ...]) -> SnapshotManifest:
    file_count = len(entries)
    total_bytes = sum(entry.size for entry in entries)
    return SnapshotManifest(
        entries=entries,
        file_count=file_count,
        total_bytes=total_bytes,
        manifest_sha256=_manifest_digest(entries, file_count, total_bytes),
    )


def _manifest_digest(
    entries: tuple[SnapshotManifestEntry, ...],
    file_count: int,
    total_bytes: int,
) -> str:
    core = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    return hashlib.sha256(_canonical_json_bytes(core)).hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_archive(root: Path, target: Path, manifest: SnapshotManifest) -> None:
    with target.open("wb") as raw_stream:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=6,
            fileobj=raw_stream,
            mtime=0,
        ) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for entry in manifest.entries:
                    source = root.joinpath(*PurePosixPath(entry.path).parts)
                    current_size, current_digest, verified_stat = _hash_stable_file(
                        source,
                        source.stat(follow_symlinks=False),
                        entry.size,
                        entry.path,
                    )
                    if current_size != entry.size or current_digest != entry.sha256:
                        raise SnapshotError("file_changed", relative_path=entry.path)
                    info = tarfile.TarInfo(f"{ARCHIVE_WORKSPACE_PREFIX}{entry.path}")
                    info.size = entry.size
                    info.mode = entry.mode
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(source, flags)
                    with os.fdopen(descriptor, "rb") as source_stream:
                        opened_stat = os.fstat(source_stream.fileno())
                        if not stat.S_ISREG(opened_stat.st_mode) or _file_identity(
                            opened_stat
                        ) != _file_identity(verified_stat):
                            raise SnapshotError(
                                "file_changed",
                                relative_path=entry.path,
                            )
                        archive.addfile(info, source_stream)
                manifest_bytes = manifest.canonical_bytes()
                manifest_info = tarfile.TarInfo(ARCHIVE_MANIFEST_PATH)
                manifest_info.size = len(manifest_bytes)
                manifest_info.mode = 0o600
                manifest_info.mtime = 0
                manifest_info.uid = 0
                manifest_info.gid = 0
                archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        raw_stream.flush()
        os.fsync(raw_stream.fileno())


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in member.name
        or "\x00" in member.name
        or str(path) != member.name
    ):
        raise SnapshotError("archive_invalid")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise SnapshotError("archive_invalid")
    if not member.isfile() and not member.isdir():
        raise SnapshotError("archive_invalid")
    if member.isdir() and not (
        member.name == "workspace"
        or member.name.startswith(ARCHIVE_WORKSPACE_PREFIX)
        or member.name == "metadata"
    ):
        raise SnapshotError("archive_invalid")
    if member.isfile() and (
        member.name != ARCHIVE_MANIFEST_PATH
        and not member.name.startswith(ARCHIVE_WORKSPACE_PREFIX)
    ):
        raise SnapshotError("archive_invalid")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        result = path.lstat()
    except OSError as exc:
        raise SnapshotError("unsafe_path") from exc
    attributes = getattr(result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _hash_file_unbounded(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_SIZE):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _hash_archive_member(
    stream: IO[bytes],
    *,
    declared_size: int,
    max_bytes: int,
    relative_path: str,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(_HASH_CHUNK_SIZE):
        size += len(chunk)
        if size > max_bytes:
            raise SnapshotError("file_too_large", relative_path=relative_path)
        digest.update(chunk)
    if size != declared_size:
        raise SnapshotError("archive_invalid")
    return size, digest.hexdigest()


__all__ = [
    "ARCHIVE_MANIFEST_PATH",
    "ARCHIVE_WORKSPACE_PREFIX",
    "ProjectSnapshot",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotError",
    "SnapshotErrorCode",
    "SnapshotManifest",
    "SnapshotManifestEntry",
    "SnapshotPolicy",
    "build_project_snapshot",
    "scan_project_manifest",
    "read_snapshot_file",
    "validate_snapshot_archive",
]

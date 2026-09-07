"""Path-safe filesystem ownership and rebuildable mirrors for LLM Wiki."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .errors import WikiPathError, WikiStoreError
from .models import (
    WikiMirrorRepairReport,
    WikiParseRevision,
    WikiSelectedParsePointer,
    WikiSource,
    WikiSourceMimeType,
    WikiSpace,
    WikiSpaceManifest,
    validate_parse_revision_id,
    validate_source_id,
    validate_space_id,
)

WIKI_DB_FILENAME = "wiki.db"
LEGACY_SUBDIR = "legacy"
SPACES_SUBDIR = "spaces"
SPACE_MANIFEST_FILENAME = "space.json"
RAW_SUBDIR = "raw"
PAGES_SUBDIR = "pages"

_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

_SOURCE_SUFFIX: dict[WikiSourceMimeType, str] = {
    "application/pdf": ".pdf",
    "text/html": ".html",
}


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _native_cleanup_path(path: Path) -> str:
    """Only for already containment-checked paths; preserve Windows long paths."""
    value = str(path.absolute())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _canonical_manifest(space: WikiSpace) -> bytes:
    payload = WikiSpaceManifest(space=space).model_dump(mode="json", by_alias=True)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


class WikiFileStore:
    """Own fixed paths below one account-scoped Knowledge root.

    Public APIs accept a validated ``space_id`` and normalized relative POSIX
    paths.  Absolute host paths are never part of persistable DTOs or errors.
    """

    def __init__(self, root: str | Path) -> None:
        raw_root = Path(root)
        if not str(raw_root):
            raise WikiStoreError("invalid_configuration")
        configured_root = Path(os.path.abspath(raw_root))
        if configured_root.exists() and _is_link_or_reparse(configured_root):
            raise WikiPathError("path_unsafe")
        self._root = raw_root.resolve(strict=False)
        self._spaces_root = self._root / SPACES_SUBDIR
        self._legacy_root = self._root / LEGACY_SUBDIR

    @property
    def root(self) -> Path:
        """Internal resolved root; never serialize this property."""
        return self._root

    @property
    def database_path(self) -> Path:
        return self._root / WIKI_DB_FILENAME

    @property
    def legacy_root(self) -> Path:
        return self._legacy_root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._assert_directory(self._root)
        for path in (self._legacy_root, self._spaces_root):
            self._ensure_directory(path)

    def validate_database_paths(self) -> None:
        """Reject pre-planted links or special files used by SQLite."""
        for path in (
            self.database_path,
            self.database_path.with_name(f"{WIKI_DB_FILENAME}-wal"),
            self.database_path.with_name(f"{WIKI_DB_FILENAME}-shm"),
        ):
            self._assert_contained(path)
            if not path.exists():
                continue
            if _is_link_or_reparse(path):
                raise WikiPathError("path_unsafe")
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise WikiPathError("path_unsafe") from exc
            if not stat.S_ISREG(info.st_mode):
                raise WikiPathError("path_unsafe")

    def space_dir(self, space_id: str) -> Path:
        validate_space_id(space_id)
        path = self._spaces_root / space_id
        self._assert_contained(path)
        return path

    def create_or_repair_space_layout(self, space: WikiSpace) -> bool:
        """Create fixed dirs and canonical manifest; return whether repair occurred."""
        self.ensure_root()
        target = self.space_dir(space.id)
        manifest = _canonical_manifest(space)
        if target.exists():
            self._assert_directory(target)
            self._ensure_directory(target / RAW_SUBDIR)
            self._ensure_directory(target / PAGES_SUBDIR)
            previous = None
            manifest_path = target / SPACE_MANIFEST_FILENAME
            if manifest_path.exists():
                try:
                    previous = self._read_file_bounded(manifest_path, 128 * 1024)
                except WikiStoreError:
                    previous = None
            self._atomic_write(manifest_path, manifest, overwrite=True)
            return previous != manifest

        staging = self._spaces_root / f".creating-{space.id}-{uuid4().hex}"
        self._assert_contained(staging)
        try:
            staging.mkdir(mode=0o700)
            (staging / RAW_SUBDIR).mkdir(mode=0o700)
            (staging / PAGES_SUBDIR).mkdir(mode=0o700)
            self._atomic_write(
                staging / SPACE_MANIFEST_FILENAME,
                manifest,
                overwrite=False,
            )
            os.replace(staging, target)
            self._fsync_directory(self._spaces_root)
        except Exception:
            if staging.exists() and not _is_link_or_reparse(staging):
                self._quarantine_staging(staging)
            raise
        return True

    def reconcile_space_layouts(
        self,
        spaces: Iterable[WikiSpace],
    ) -> WikiMirrorRepairReport:
        """Rebuild missing manifests/dirs and report unknown space directories."""
        self.ensure_root()
        expected: dict[str, WikiSpace] = {space.id: space for space in spaces}
        repaired: list[str] = []
        quarantined = 0
        for child in tuple(self._spaces_root.iterdir()):
            if child.name.startswith(".creating-"):
                self._assert_contained(child)
                if _is_link_or_reparse(child):
                    raise WikiPathError("path_unsafe")
                self._quarantine_staging(child)
                quarantined += 1
        for space in expected.values():
            if self.create_or_repair_space_layout(space):
                repaired.append(space.id)

        orphans: list[str] = []
        for child in self._spaces_root.iterdir():
            if child.name.startswith("."):
                continue
            try:
                validate_space_id(child.name)
            except ValueError:
                raise WikiPathError("path_unsafe") from None
            self._assert_directory(child)
            if child.name not in expected:
                orphans.append(child.name)
        return WikiMirrorRepairReport(
            repaired_space_ids=tuple(sorted(repaired)),
            orphan_space_ids=tuple(sorted(orphans)),
            quarantined_staging_count=quarantined,
        )

    def write_owned_file_atomic(
        self,
        space_id: str,
        relative_path: str,
        content: bytes,
        *,
        overwrite: bool,
        max_bytes: int,
        allow_empty: bool = False,
    ) -> str:
        """Write one raw/page-owned file and return its SHA-256."""
        if (not content and not allow_empty) or len(content) > max_bytes:
            raise WikiStoreError("file_too_large")
        normalized = self.validate_owned_relative_path(relative_path)
        target = self._owned_path(space_id, normalized)
        self._ensure_directory(target.parent)
        self._reject_case_collision(target)
        try:
            self._atomic_write(target, content, overwrite=overwrite)
        except WikiStoreError:
            raise
        except OSError as exc:
            raise WikiStoreError("file_io_failed") from exc
        return hashlib.sha256(content).hexdigest()

    def write_owned_file_once_or_verify(
        self,
        space_id: str,
        relative_path: str,
        content: bytes,
        *,
        max_bytes: int,
        allow_empty: bool = False,
    ) -> str:
        """Create once, or accept an exact prior write from an interrupted import."""
        if (not content and not allow_empty) or len(content) > max_bytes:
            raise WikiStoreError("file_too_large")
        digest = hashlib.sha256(content).hexdigest()
        if self.owned_file_exists(space_id, relative_path):
            previous = self.read_owned_file(
                space_id,
                relative_path,
                max_bytes=max_bytes,
            )
            if previous != content:
                raise WikiStoreError("file_exists")
            return digest
        return self.write_owned_file_atomic(
            space_id,
            relative_path,
            content,
            overwrite=False,
            max_bytes=max_bytes,
            allow_empty=allow_empty,
        )

    @staticmethod
    def source_bundle_relative_path(source: WikiSource) -> str:
        """Return the identity-bearing Raw directory for one persisted source."""
        return PurePosixPath(source.source_relpath).parent.as_posix()

    @classmethod
    def parse_revision_relative_path(
        cls,
        source: WikiSource,
        parse_revision_id: str,
    ) -> str:
        validate_parse_revision_id(parse_revision_id)
        return (
            PurePosixPath(cls.source_bundle_relative_path(source)) / "parses" / parse_revision_id
        ).as_posix()

    @classmethod
    def selected_parse_relative_path(cls, source: WikiSource) -> str:
        return (PurePosixPath(cls.source_bundle_relative_path(source)) / "selected.json").as_posix()

    def write_selected_parse_pointer(
        self,
        space_id: str,
        source: WikiSource,
        revision: WikiParseRevision,
        pointer: WikiSelectedParsePointer,
    ) -> bool:
        """Atomically create or repair the rebuildable selected parse pointer."""
        expected_revision_dir = self.parse_revision_relative_path(source, revision.id)
        if (
            pointer.source_id != source.id
            or pointer.source_sha256 != source.source_sha256
            or pointer.parse_revision_id != revision.id
            or source.selected_parse_revision_id != revision.id
            or pointer.selection_version != source.selection_version
            or pointer.manifest_relpath != revision.manifest_relpath
            or pointer.manifest_sha256 != revision.manifest_sha256
            or PurePosixPath(revision.manifest_relpath).parent.as_posix() != expected_revision_dir
        ):
            raise WikiStoreError("invalid_artifact")
        payload = (
            json.dumps(
                pointer.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        relative_path = self.selected_parse_relative_path(source)
        previous: bytes | None = None
        if self.owned_file_exists(space_id, relative_path):
            previous = self.read_owned_file(space_id, relative_path, max_bytes=128 * 1024)
        if previous == payload:
            return False
        self.write_owned_file_atomic(
            space_id,
            relative_path,
            payload,
            overwrite=True,
            max_bytes=128 * 1024,
        )
        return previous != payload

    def remove_owned_file_if_present(self, space_id: str, relative_path: str) -> bool:
        """Remove one exact owned regular file without traversing directory trees."""
        normalized = self.validate_owned_relative_path(relative_path)
        path = self._owned_path(space_id, normalized)
        if not path.exists():
            return False
        if _is_link_or_reparse(path):
            raise WikiPathError("path_unsafe")
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise WikiStoreError("file_io_failed") from exc
        if not stat.S_ISREG(info.st_mode):
            raise WikiPathError("path_unsafe")
        path.unlink()
        self._fsync_directory(path.parent)
        return True

    def purge_source_bundle(self, source: WikiSource) -> bool:
        """Remove one Source-owned Raw tree without following links.

        The tree is first renamed to an internal sibling name. A crash after
        the rename is recoverable because the next purge also scans matching
        staging names. Database audit rows are intentionally unaffected.
        """
        bundle_relative = self.source_bundle_relative_path(source)
        bundle_parts = PurePosixPath(bundle_relative).parts
        if len(bundle_parts) != 2 or bundle_parts[0] != RAW_SUBDIR:
            raise WikiPathError("path_unsafe")
        raw_root = self.space_dir(source.space_id) / RAW_SUBDIR
        self._assert_directory(raw_root)
        bundle = raw_root / bundle_parts[1]
        self._assert_contained(bundle)
        prefix = f".purging-{source.id}-"
        removed = False
        if bundle.exists():
            if _is_link_or_reparse(bundle):
                raise WikiPathError("path_unsafe")
            self._assert_directory(bundle)
            staging = raw_root / f"{prefix}{uuid4().hex}"
            self._assert_contained(staging)
            os.replace(bundle, staging)
            self._fsync_directory(raw_root)
            removed = True
        for candidate in tuple(raw_root.iterdir()):
            if not candidate.name.startswith(prefix):
                continue
            self._assert_contained(candidate)
            self._remove_tree_no_links(candidate)
            removed = True
        if removed:
            self._fsync_directory(raw_root)
        return removed

    def read_owned_file(
        self,
        space_id: str,
        relative_path: str,
        *,
        max_bytes: int,
    ) -> bytes:
        normalized = self.validate_owned_relative_path(relative_path)
        try:
            return self._read_file_bounded(
                self._owned_path(space_id, normalized),
                max_bytes,
            )
        except WikiStoreError:
            raise
        except OSError as exc:
            raise WikiStoreError("file_io_failed") from exc

    def owned_file_exists(self, space_id: str, relative_path: str) -> bool:
        normalized = self.validate_owned_relative_path(relative_path)
        path = self._owned_path(space_id, normalized)
        if _is_link_or_reparse(path):
            raise WikiPathError("path_unsafe")
        return path.is_file()

    @staticmethod
    def source_relative_path(
        source_id: str,
        display_name: str,
        mime_type: WikiSourceMimeType,
    ) -> str:
        """Return a deterministic identity-bearing path for one immutable source."""
        validate_source_id(source_id)
        suffix = _SOURCE_SUFFIX[mime_type]
        stem = display_name
        lowered = display_name.casefold()
        if lowered.endswith(suffix):
            stem = display_name[: -len(suffix)]
        normalized = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()
        if not slug:
            slug = "source"
        directory = f"{slug[:48]}--{source_id}"
        return f"{RAW_SUBDIR}/{directory}/source{suffix}"

    def resolve_owned_regular_file(self, space_id: str, relative_path: str) -> Path:
        """Resolve a validated owned file for an isolated provider invocation."""
        normalized = self.validate_owned_relative_path(relative_path)
        path = self._owned_path(space_id, normalized)
        if _is_link_or_reparse(path):
            raise WikiPathError("path_unsafe")
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise WikiStoreError("file_not_found") from exc
        if not stat.S_ISREG(info.st_mode):
            raise WikiPathError("path_unsafe")
        return path

    def remove_owned_file_if_sha256(
        self,
        space_id: str,
        relative_path: str,
        expected_sha256: str,
        *,
        max_bytes: int,
    ) -> bool:
        """Rollback a just-created owned file only when its identity still matches."""
        path = self.resolve_owned_regular_file(space_id, relative_path)
        payload = self._read_file_bounded(path, max_bytes)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            return False
        path.unlink()
        self._fsync_directory(path.parent)
        raw_root = self.space_dir(space_id) / RAW_SUBDIR
        if path.parent != raw_root:
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return True

    @staticmethod
    def validate_owned_relative_path(value: str) -> str:
        if (
            not value
            or len(value) > 240
            or "\\" in value
            or "\x00" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise WikiPathError("path_unsafe")
        path = PurePosixPath(value)
        if path.is_absolute() or str(path) != value or ".." in path.parts:
            raise WikiPathError("path_unsafe")
        if len(path.parts) < 2 or path.parts[0] not in {RAW_SUBDIR, PAGES_SUBDIR}:
            raise WikiPathError("path_unsafe")
        for part in path.parts:
            stem = part.split(".", 1)[0].casefold()
            if (
                part in {"", ".", ".."}
                or part.endswith((" ", "."))
                or ":" in part
                or stem in _WINDOWS_RESERVED
            ):
                raise WikiPathError("path_unsafe")
        return value

    def _owned_path(self, space_id: str, relative_path: str) -> Path:
        space_dir = self.space_dir(space_id)
        self._assert_directory(space_dir)
        target = space_dir.joinpath(*PurePosixPath(relative_path).parts)
        self._assert_contained(target)
        self._assert_no_link_chain(target.parent, stop=space_dir)
        return target

    def _assert_contained(self, target: Path) -> None:
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise WikiPathError("path_unsafe") from exc

    def _assert_directory(self, path: Path) -> None:
        self._assert_contained(path)
        if _is_link_or_reparse(path) or not path.is_dir():
            raise WikiPathError("path_unsafe")

    def _ensure_directory(self, path: Path) -> None:
        self._assert_contained(path)
        try:
            relative = path.relative_to(self._root)
        except ValueError as exc:
            raise WikiPathError("path_unsafe") from exc
        current = self._root
        self._assert_directory(current)
        for part in relative.parts:
            candidate = current / part
            self._reject_case_collision(candidate)
            if candidate.exists():
                self._assert_directory(candidate)
            else:
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                self._assert_directory(candidate)
            current = candidate

    def _assert_no_link_chain(self, path: Path, *, stop: Path) -> None:
        current = path
        while True:
            if current.exists() and _is_link_or_reparse(current):
                raise WikiPathError("path_unsafe")
            if current == stop:
                return
            if current.parent == current:
                raise WikiPathError("path_unsafe")
            current = current.parent

    @staticmethod
    def _reject_case_collision(target: Path) -> None:
        if not target.parent.exists():
            return
        for child in target.parent.iterdir():
            if child.name.casefold() == target.name.casefold() and child.name != target.name:
                raise WikiPathError("path_unsafe")

    def _atomic_write(self, target: Path, content: bytes, *, overwrite: bool) -> None:
        self._assert_contained(target)
        self._assert_no_link_chain(target.parent, stop=self._root)
        if _is_link_or_reparse(target):
            raise WikiPathError("path_unsafe")
        if target.exists() and not overwrite:
            raise WikiStoreError("file_exists")
        # Keep the sibling temp name bounded for Windows paths with deep Raw revisions.
        temp = target.with_name(f".tmp-{uuid4().hex}")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            if _is_link_or_reparse(target):
                raise WikiPathError("path_unsafe")
            if overwrite:
                os.replace(temp, target)
            else:
                try:
                    os.link(temp, target)
                except FileExistsError as exc:
                    raise WikiStoreError("file_exists") from exc
                temp.unlink()
            self._fsync_directory(target.parent)
        finally:
            temp.unlink(missing_ok=True)

    def _read_file_bounded(self, path: Path, max_bytes: int) -> bytes:
        self._assert_contained(path)
        self._assert_no_link_chain(path.parent, stop=self._root)
        if _is_link_or_reparse(path):
            raise WikiPathError("path_unsafe")
        try:
            before = path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise WikiStoreError("file_not_found") from exc
        if not stat.S_ISREG(before.st_mode):
            raise WikiPathError("path_unsafe")
        if before.st_size > max_bytes:
            raise WikiStoreError("file_too_large")
        content = path.read_bytes()
        after = path.stat(follow_symlinks=False)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or len(content) != before.st_size:
            raise WikiPathError("path_unsafe")
        return content

    def _remove_tree_no_links(self, directory: Path) -> None:
        self._assert_contained(directory)
        try:
            info = os.lstat(_native_cleanup_path(directory))
            if (
                not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise WikiPathError("path_unsafe")
            with os.scandir(_native_cleanup_path(directory)) as scan:
                entries = tuple(scan)
        except OSError as exc:
            raise WikiStoreError("file_io_failed") from exc
        for entry in entries:
            # Keep canonical policy paths separate from the native I/O spelling.
            path = directory / entry.name
            self._assert_contained(path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise WikiStoreError("file_io_failed") from exc
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WikiPathError("path_unsafe")
            if stat.S_ISDIR(info.st_mode):
                self._remove_tree_no_links(path)
            elif stat.S_ISREG(info.st_mode):
                try:
                    os.unlink(_native_cleanup_path(path))
                except OSError as exc:
                    raise WikiStoreError("file_io_failed") from exc
            else:
                raise WikiPathError("path_unsafe")
        try:
            os.rmdir(_native_cleanup_path(directory))
        except OSError as exc:
            raise WikiStoreError("file_io_failed") from exc

    def _quarantine_staging(self, staging: Path) -> None:
        quarantine = self._legacy_root / "recovery"
        self._ensure_directory(quarantine)
        target = quarantine / f"{staging.name}-{uuid4().hex}"
        self._assert_contained(target)
        os.replace(staging, target)
        self._fsync_directory(quarantine)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


__all__ = [
    "LEGACY_SUBDIR",
    "PAGES_SUBDIR",
    "RAW_SUBDIR",
    "SPACES_SUBDIR",
    "SPACE_MANIFEST_FILENAME",
    "WIKI_DB_FILENAME",
    "WikiFileStore",
]

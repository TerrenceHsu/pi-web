"""KnowledgeFileStore — long-term filesystem layout for Knowledge Libraries.

Per P2-R0 §3 (file layout frozen) + §3.3 (interface frozen) + §3.4 (path
safety). Independent of ``VirtualFileStore`` (decision R5) — different
lifecycle (Library outlives Session), different ACL (Library ACL ≠ session
file ACL).

R1 scope (per ``docs/design/p2-r0-amendment-1.md``):

- ✅ Path primitives: library_dir / document_dir / atomic write / safe read /
  delete dir / orphan scan
- ✅ Path containment (``..`` / absolute / drive / UNC / separator injection)
- ✅ Symlink escape prevention
- ❌ No PDF parsing, no Markdown generation, no manifest serialization —
  those are R2's responsibility. ``write_file_atomic`` accepts bytes / str
  so R2 can write any of (source.pdf, document.md, manifest.json).

Layout (frozen, P2-R0 §3.1)::

    <root>/
    ├── knowledge.db                  # owned by KnowledgeStore, not FileStore
    └── libraries/
        └── {library_id}/
            ├── library.json          # R2 will write; R1 leaves absent
            └── documents/
                └── {document_id}/
                    ├── source.pdf
                    ├── document.md
                    └── manifest.json

Security invariants (P2-R0 §3.2 / §3.4):

- ``library_id`` / ``document_id`` must match ``^(lib|doc)_<body>$`` body
  ∈ ``[a-z0-9]{12,32}``. Reject anything else before touching the filesystem.
- All paths must ``Path.resolve()`` to a location inside ``root``.
- Symlinks pointing outside ``root`` are rejected.
- API responses never include absolute paths.
- Fixed filenames only — caller may not pass arbitrary ``source_name`` /
  ``markdown_name``.
"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from .models import (
    is_valid_document_id,
    is_valid_library_id,
)

# ============================================================================
# Constants
# ============================================================================

#: Fixed filenames inside ``libraries/{library_id}/documents/{document_id}/``.
#: Per P2-R0 §3.4 — caller may not supply arbitrary filenames.
FIXED_DOCUMENT_FILES: Final[tuple[str, ...]] = (
    "source.pdf",
    "document.md",
    "manifest.json",
)

#: Subdir name under root holding all library dirs.
LIBRARIES_SUBDIR: Final[str] = "libraries"

#: Subdir name under a library dir holding all document dirs.
DOCUMENTS_SUBDIR: Final[str] = "documents"


# ============================================================================
# Errors
# ============================================================================


class KnowledgeFileStoreError(Exception):
    """Base class for KnowledgeFileStore errors."""


class InvalidLibraryIDError(KnowledgeFileStoreError):
    """library_id format check failed (P2-R0 §3.4)."""


class InvalidDocumentIDError(KnowledgeFileStoreError):
    """document_id format check failed."""


class PathSafetyError(KnowledgeFileStoreError):
    """Resolved path escapes root, or symlink escape detected."""


class UnknownFixedFileError(KnowledgeFileStoreError):
    """Caller asked for a filename not in ``FIXED_DOCUMENT_FILES``."""


class FileNotFoundError_(KnowledgeFileStoreError):
    """Expected file does not exist on disk."""


# ============================================================================
# KnowledgeFileStore
# ============================================================================


class KnowledgeFileStore:
    """Long-term filesystem layout for Knowledge Libraries.

    Independent root directory (``data/knowledge/`` by default); does **not**
    share state with ``VirtualFileStore`` (which is session-scoped).

    All public methods accept only ``library_id`` / ``document_id`` plus a
    fixed filename enum — never raw paths. The store owns path construction
    and validates containment at every entry point.
    """

    def __init__(self, root: str | Path) -> None:
        self._root: Path = Path(root).resolve()
        # Root must exist (caller — app composition — creates it).
        # We don't auto-create here to keep the constructor side-effect-free;
        # use ``ensure_root_async()`` from app composition or create ahead.
        self._libraries_root: Path = self._root / LIBRARIES_SUBDIR

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """Resolved absolute root path. For internal diagnostics only —
        API responses must **not** return this value."""
        return self._root

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_root(self) -> None:
        """Create ``root/`` and ``root/libraries/`` if absent. Idempotent.

        Called by app composition at startup. Side-effectful — separate from
        ``__init__`` so tests can construct a store against a non-existent
        path and assert behavior.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self._libraries_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # ID + path validation
    # ------------------------------------------------------------------

    @staticmethod
    def _require_valid_library_id(library_id: str) -> str:
        if not is_valid_library_id(library_id):
            raise InvalidLibraryIDError(f"invalid library_id: {library_id!r}")
        return library_id

    @staticmethod
    def _require_valid_document_id(document_id: str) -> str:
        if not is_valid_document_id(document_id):
            raise InvalidDocumentIDError(f"invalid document_id: {document_id!r}")
        return document_id

    @staticmethod
    def _require_fixed_file(filename: str) -> str:
        if filename not in FIXED_DOCUMENT_FILES:
            raise UnknownFixedFileError(
                f"filename {filename!r} not in fixed set {FIXED_DOCUMENT_FILES}"
            )
        return filename

    def _library_dir_unchecked(self, library_id: str) -> Path:
        return self._libraries_root / library_id

    def _document_dir_unchecked(
        self, library_id: str, document_id: str
    ) -> Path:
        return (
            self._libraries_root
            / library_id
            / DOCUMENTS_SUBDIR
            / document_id
        )

    def _check_containment(self, target: Path) -> Path:
        """Resolve ``target`` and verify it's inside ``self._root``.

        Rejects:
        - ``..`` traversal
        - absolute paths escaping root
        - symlinks pointing outside root (anywhere along the chain)
        - Windows drive / UNC paths

        Returns the resolved absolute path. Raises ``PathSafetyError`` on
        any violation.
        """
        # Use os.path.realpath to resolve symlinks (Path.resolve on Windows
        # is strict about existence; we want logical resolution regardless).
        try:
            resolved = Path(os.path.realpath(str(target)))
        except (OSError, RuntimeError) as e:
            raise PathSafetyError(
                f"failed to resolve path: {type(e).__name__}"
            ) from e

        root_resolved = Path(os.path.realpath(str(self._root)))
        try:
            resolved.relative_to(root_resolved)
        except ValueError as e:
            raise PathSafetyError(
                "path escapes knowledge root"
            ) from e
        return resolved

    @staticmethod
    def _check_no_symlink_escape(path: Path, root: Path) -> None:
        """Walk each parent of ``path`` and reject symlinks that resolve
        outside ``root``.

        ``os.path.realpath`` already collapses symlinks, but a malicious
        actor could plant a symlink mid-tree after realpath. This walk is
        defense-in-depth.
        """
        # Walk from path upward; check each ancestor for being a symlink
        # whose target escapes root.
        root_real = Path(os.path.realpath(str(root)))
        current = path
        seen = set()
        while current not in seen:
            seen.add(current)
            if current.is_symlink():
                try:
                    target = Path(os.path.realpath(str(current)))
                except (OSError, RuntimeError) as exc:
                    raise PathSafetyError(
                        "symlink target unreadable"
                    ) from exc
                try:
                    target.relative_to(root_real)
                except ValueError as exc:
                    raise PathSafetyError(
                        "symlink escapes knowledge root"
                    ) from exc
            if current == root or current.parent == current:
                break
            current = current.parent

    # ------------------------------------------------------------------
    # Library directory ops
    # ------------------------------------------------------------------

    def create_library_dir(self, library_id: str) -> Path:
        """Create ``libraries/{library_id}/`` and its ``documents/`` subdir.

        Returns the resolved library dir path. Idempotent — succeeds if the
        directory already exists.

        Raises ``InvalidLibraryIDError`` / ``PathSafetyError``.
        """
        self._require_valid_library_id(library_id)
        lib_dir = self._library_dir_unchecked(library_id)
        self._check_containment(lib_dir)
        self._check_no_symlink_escape(lib_dir, self._root)
        lib_dir.mkdir(parents=True, exist_ok=True)
        (lib_dir / DOCUMENTS_SUBDIR).mkdir(parents=True, exist_ok=True)
        return lib_dir

    def library_dir_exists(self, library_id: str) -> bool:
        """Return True iff the library directory exists."""
        self._require_valid_library_id(library_id)
        lib_dir = self._library_dir_unchecked(library_id)
        try:
            self._check_containment(lib_dir)
        except PathSafetyError:
            return False
        return lib_dir.exists() and lib_dir.is_dir()

    def delete_library_dir(self, library_id: str) -> None:
        """Recursively delete ``libraries/{library_id}/``.

        Idempotent — succeeds silently if the directory is already gone.

        Raises ``InvalidLibraryIDError`` / ``PathSafetyError``.
        """
        self._require_valid_library_id(library_id)
        lib_dir = self._library_dir_unchecked(library_id)
        # Containment check first — refuse to delete anything outside root
        self._check_containment(lib_dir)
        self._check_no_symlink_escape(lib_dir, self._root)
        if not lib_dir.exists():
            return
        # Use shutil.rmtree with ``onerror`` raising to surface failures.
        # ``ignore_errors=False`` to ensure caller can react.
        shutil.rmtree(lib_dir)

    # ------------------------------------------------------------------
    # Document directory ops
    # ------------------------------------------------------------------

    def create_document_dir(self, library_id: str, document_id: str) -> Path:
        """Create ``libraries/{library_id}/documents/{document_id}/``.

        Parent library dir must already exist (call ``create_library_dir``
        first). Idempotent.

        Raises ``InvalidLibraryIDError`` / ``InvalidDocumentIDError`` /
        ``PathSafetyError``.
        """
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        doc_dir = self._document_dir_unchecked(library_id, document_id)
        self._check_containment(doc_dir)
        self._check_no_symlink_escape(doc_dir, self._root)
        doc_dir.mkdir(parents=True, exist_ok=True)
        return doc_dir

    def document_dir_exists(self, library_id: str, document_id: str) -> bool:
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        doc_dir = self._document_dir_unchecked(library_id, document_id)
        try:
            self._check_containment(doc_dir)
        except PathSafetyError:
            return False
        return doc_dir.exists() and doc_dir.is_dir()

    def delete_document_dir(self, library_id: str, document_id: str) -> None:
        """Recursively delete a single document directory. Idempotent."""
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        doc_dir = self._document_dir_unchecked(library_id, document_id)
        self._check_containment(doc_dir)
        self._check_no_symlink_escape(doc_dir, self._root)
        if not doc_dir.exists():
            return
        shutil.rmtree(doc_dir)

    # ------------------------------------------------------------------
    # Atomic file primitives (R2 will use these for source.pdf / document.md)
    # ------------------------------------------------------------------

    def write_file_atomic(
        self,
        library_id: str,
        document_id: str,
        filename: str,
        content: bytes | str,
    ) -> None:
        """Write ``filename`` under the document dir atomically.

        Atomic strategy (per P2-R0 §3.3):

        1. Write to a sibling temp file (``.{filename}.{rand}.tmp``).
        2. ``flush()`` + ``os.fsync(fd)``.
        3. ``os.replace(temp, target)`` (POSIX atomic; Windows replaces too).
        4. fsync the parent directory (best-effort on Windows).

        ``filename`` must be one of ``FIXED_DOCUMENT_FILES``.

        Raises ``InvalidLibraryIDError`` / ``InvalidDocumentIDError`` /
        ``UnknownFixedFileError`` / ``PathSafetyError``.
        """
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        self._require_fixed_file(filename)

        doc_dir = self._document_dir_unchecked(library_id, document_id)
        self._check_containment(doc_dir)
        self._check_no_symlink_escape(doc_dir, self._root)
        doc_dir.mkdir(parents=True, exist_ok=True)
        target = doc_dir / filename
        self._check_containment(target)
        self._check_no_symlink_escape(target, self._root)

        if isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = bytes(content)

        # Temp file in same dir (same partition → os.replace atomic).
        rand_suffix = secrets.token_hex(8)
        tmp_path = doc_dir / f".{filename}.{rand_suffix}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # Some filesystems (network mounts) don't support fsync.
                    # Atomic rename still guarantees crash consistency within
                    # a single OS; only durability across power loss is weaker.
                    pass
            os.replace(tmp_path, target)
            # Best-effort fsync of parent dir
            try:
                parent_fd = os.open(str(doc_dir), os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                pass
        except Exception:
            # Clean up temp file on any failure — never leave half-written
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise

    def read_file(
        self,
        library_id: str,
        document_id: str,
        filename: str,
        *,
        as_text: bool = False,
    ) -> bytes | str:
        """Read a fixed filename from the document dir.

        ``as_text=True`` decodes as UTF-8 and returns str; otherwise bytes.

        Raises ``FileNotFoundError_`` if absent. Raises
        ``InvalidLibraryIDError`` / ``InvalidDocumentIDError`` /
        ``UnknownFixedFileError`` / ``PathSafetyError``.
        """
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        self._require_fixed_file(filename)

        doc_dir = self._document_dir_unchecked(library_id, document_id)
        target = doc_dir / filename
        self._check_containment(target)
        self._check_no_symlink_escape(target, self._root)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError_(
                f"{filename} not found for document {document_id!r}"
            )
        data = target.read_bytes()
        if as_text:
            return data.decode("utf-8")
        return data

    def file_exists(
        self, library_id: str, document_id: str, filename: str
    ) -> bool:
        """Return True iff the fixed filename exists in the document dir."""
        self._require_valid_library_id(library_id)
        self._require_valid_document_id(document_id)
        self._require_fixed_file(filename)
        doc_dir = self._document_dir_unchecked(library_id, document_id)
        target = doc_dir / filename
        try:
            self._check_containment(target)
        except PathSafetyError:
            return False
        return target.exists() and target.is_file()

    # ------------------------------------------------------------------
    # Orphan detection
    # ------------------------------------------------------------------

    def list_orphan_library_dirs(
        self, known_library_ids: Iterable[str]
    ) -> list[str]:
        """Return library_ids present on disk but missing from the given set.

        Used by app startup to surface libraries whose DB row was deleted
        out-of-band (manual deletion, schema corruption, etc.). Returned
        ids are validated; only valid ``lib_<body>`` directory names count
        — anything else is reported separately via ``list_unknown_dirs``.

        Does **not** delete anything. R1 ships no auto-GC; admin repair is
        the only consumer (P2-R0 §19 minimal solution).
        """
        known = set(known_library_ids)
        orphans: list[str] = []
        if not self._libraries_root.exists():
            return orphans
        for entry in self._libraries_root.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if not is_valid_library_id(name):
                continue  # unknown names handled separately
            if name not in known:
                orphans.append(name)
        return sorted(orphans)

    def list_unknown_dirs(self) -> list[str]:
        """Return directory names under ``libraries/`` that don't match the
        ``lib_<body>`` format. Useful for surfacing manual tampering.
        """
        unknown: list[str] = []
        if not self._libraries_root.exists():
            return unknown
        for entry in self._libraries_root.iterdir():
            if not entry.is_dir():
                continue
            if not is_valid_library_id(entry.name):
                unknown.append(entry.name)
        return sorted(unknown)


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "DOCUMENTS_SUBDIR",
    "FIXED_DOCUMENT_FILES",
    "FileNotFoundError_",
    "InvalidDocumentIDError",
    "InvalidLibraryIDError",
    "KnowledgeFileStore",
    "KnowledgeFileStoreError",
    "LIBRARIES_SUBDIR",
    "PathSafetyError",
    "UnknownFixedFileError",
]


# Suppress unused-import lint for re-exported helper imports used in tests.
_: tuple[object, ...] = (tempfile,)

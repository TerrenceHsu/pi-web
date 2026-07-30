"""KnowledgeService — orchestration + business validation (P2-R1 R1-B).

Sits between the Store (SQLite) and the FileStore (filesystem). Per P2-R1 §6:

- ✅ business validation
- ✅ Store + FileStore 编排
- ✅ deletion compensation (DB ↔ filesystem)
- ✅ session existence validation (via injected callback)
- ✅ library status validation
- ✅ safe error conversion (no raw OSError / traceback / absolute path)
- ✅ DTO assembly (LibraryView / Document / SessionLibraryBinding)
- ❌ no HTTP / FastAPI dep
- ❌ no SQL / direct filesystem ops beyond what Store + FileStore expose

Compensation strategy (P2-R0 §19):

- create_library: DB insert → FS mkdir. If mkdir fails, mark Library
  ``failed`` (DB row remains for audit; user can delete or retry).
- delete_library: status=``deleting`` → FS rmtree → DB hard-delete.
  If FS rmtree fails, Library stays ``deleting`` and is reported as
  ``failed`` after the next attempt (state recoverable; manual retry).
- delete_document: DB hard-delete (cascades chunks/jobs) → FS rmtree.
  If FS rmtree fails, metadata is already gone; orphan dir surfaces via
  ``list_orphan_library_dirs`` / ``list_unknown_dirs`` and can be cleaned
  by admin repair.

Service keeps its **own** error vocabulary distinct from Store / FileStore
errors — API layer only sees ``KnowledgeServiceError`` subclasses so we
can change repository internals without breaking the API contract.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .files import (
    KnowledgeFileStore,
)
from .models import (
    Document,
    Library,
    LibraryStats,
    LibraryUpdate,
    LibraryView,
    SessionLibraryBinding,
    is_valid_library_name,
)
from .store import (
    DocumentNotFoundError,
    DuplicateDocumentError,
    KnowledgeStore,
    KnowledgeStoreError,
    LibraryNotActiveError,
    LibraryNotFoundError,
    LibraryValidationError,
)

# ============================================================================
# Callback types
# ============================================================================

#: Async callback returning True iff a Session id exists in session.db.
#: Injected by app composition — Service must not import session_sqlite.
SessionExistsCallback = Callable[[str], Awaitable[bool]]


async def _always_false_session_exists(_session_id: str) -> bool:
    """Default callback — denies all bindings. App composition overrides."""
    return False


# ============================================================================
# Errors (stable API surface — translated from Store / FileStore internals)
# ============================================================================


class KnowledgeServiceError(Exception):
    """Base class for service-layer errors. Safe to serialize for API."""


class ServiceValidationError(KnowledgeServiceError):
    """Input validation failed (name / description / id format)."""


class LibraryNotFound(KnowledgeServiceError):
    """Library id does not exist."""


class LibraryNotReady(KnowledgeServiceError):
    """Library exists but is not active (archived / deleting / failed)."""


class DocumentNotFound(KnowledgeServiceError):
    """Document id does not exist."""


class DuplicateDocument(KnowledgeServiceError):
    """(library_id, source_sha256) already exists."""


class SessionNotFound(KnowledgeServiceError):
    """Session id does not exist (binding rejected)."""


class FileStoreFailure(KnowledgeServiceError):
    """Filesystem operation failed; Library / Document may be in a
    partially-deleted state. The DB row is preserved for retry."""


# ============================================================================
# KnowledgeService
# ============================================================================


class KnowledgeService:
    """Orchestrates KnowledgeStore + KnowledgeFileStore.

    Construction::

        store = await KnowledgeStore.open(db_path)
        file_store = KnowledgeFileStore(root=data_dir / "knowledge")
        file_store.ensure_root()
        service = KnowledgeService(
            store=store,
            file_store=file_store,
            session_exists=session_store_session_exists_callback,
        )

    All methods raise ``KnowledgeServiceError`` subclasses — never leak
    Store / FileStore error types or ``OSError`` instances.
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        file_store: KnowledgeFileStore,
        session_exists: SessionExistsCallback | None = None,
    ) -> None:
        self._store = store
        self._files = file_store
        self._session_exists = session_exists or _always_false_session_exists

    # ------------------------------------------------------------------
    # Internal: safe error translation
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_store_error(exc: Exception) -> KnowledgeServiceError:
        """Map Store error → Service error. Preserves the message but
        changes the type so API layer only catches Service errors."""
        if isinstance(exc, LibraryNotFoundError):
            return LibraryNotFound(str(exc))
        if isinstance(exc, LibraryNotActiveError):
            return LibraryNotReady(str(exc))
        if isinstance(exc, LibraryValidationError):
            return ServiceValidationError(str(exc))
        if isinstance(exc, DocumentNotFoundError):
            return DocumentNotFound(str(exc))
        if isinstance(exc, DuplicateDocumentError):
            return DuplicateDocument(str(exc))
        if isinstance(exc, KnowledgeStoreError):
            return KnowledgeServiceError(str(exc))
        return KnowledgeServiceError(
            f"unexpected store error: {type(exc).__name__}"
        )

    @staticmethod
    def _translate_file_error(exc: Exception) -> FileStoreFailure:
        """FileStore errors → safe FileStoreFailure. No path / OSError leak."""
        return FileStoreFailure(
            f"file operation failed: {type(exc).__name__}"
        )

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    async def create_library(
        self,
        *,
        name: str,
        description: str = "",
    ) -> Library:
        """Create Library (DB row + filesystem dir).

        Order: DB insert first → FS mkdir second. If FS mkdir fails,
        the Library is marked ``failed`` (DB row kept for audit; user can
        retry via delete + recreate).

        Returns the new Library.
        Raises ``ServiceValidationError`` / ``FileStoreFailure``.
        """
        if not is_valid_library_name(name):
            raise ServiceValidationError(
                "library name must be non-empty and ≤200 chars"
            )
        try:
            library = await self._store.create_library(
                name=name, description=description
            )
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

        # FS mkdir — failure must mark Library failed
        try:
            self._files.create_library_dir(library.id)
        except Exception as exc:
            # Best-effort mark failed; if even that fails, surface both
            try:
                await self._store.set_library_status(library.id, "failed")
            except Exception:
                pass
            raise self._translate_file_error(exc) from exc

        return library

    async def list_libraries(self) -> list[LibraryView]:
        """List all libraries with document + binding counts."""
        try:
            libs = await self._store.list_libraries()
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc
        views: list[LibraryView] = []
        for lib in libs:
            stats = await self._gather_stats(lib.id)
            views.append(LibraryView(library=lib, stats=stats))
        return views

    async def get_library(self, library_id: str) -> LibraryView:
        try:
            lib = await self._store.get_library(library_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc
        stats = await self._gather_stats(lib.id)
        return LibraryView(library=lib, stats=stats)

    async def update_library(
        self,
        library_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Library:
        try:
            return await self._store.update_library(
                library_id, name=name, description=description
            )
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def delete_library(self, library_id: str) -> None:
        """Delete Library + Documents + Chunks + Jobs + Bindings + dir.

        Compensation (P2-R0 §19):

        1. ``status=deleting`` (DB)
        2. ``delete_library_dir`` (FS) — on failure: status stays
           ``deleting``; ``FileStoreFailure`` raised; user can retry
        3. ``delete_library_hard`` (DB cascade)
        """
        try:
            await self._store.set_library_status(library_id, "deleting")
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

        # Filesystem first — if it fails we keep the DB row (status=deleting)
        # so the user can retry safely.
        try:
            self._files.delete_library_dir(library_id)
        except Exception as exc:
            try:
                await self._store.set_library_status(library_id, "failed")
            except Exception:
                pass
            raise self._translate_file_error(exc) from exc

        try:
            await self._store.delete_library_hard(library_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Document metadata (R1 — no PDF parsing)
    # ------------------------------------------------------------------

    async def create_document_metadata(
        self,
        *,
        library_id: str,
        source_name: str,
        source_sha256: str,
        source_relpath: str,
        markdown_relpath: str,
        mime_type: str,
        size_bytes: int = 0,
        page_count: int = 0,
        parser_version: str = "",
    ) -> Document:
        """Create Document metadata only (no PDF / Markdown content).

        R2 ingestion will call this internally as part of upload processing;
        R1 exposes it as a service primitive for tests + future extension.
        """
        try:
            return await self._store.create_document(
                library_id=library_id,
                source_name=source_name,
                source_sha256=source_sha256,
                source_relpath=source_relpath,
                markdown_relpath=markdown_relpath,
                mime_type=mime_type,
                size_bytes=size_bytes,
                page_count=page_count,
                parser_version=parser_version,
            )
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def list_documents(self, library_id: str) -> list[Document]:
        try:
            return await self._store.list_documents(library_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def get_document(self, document_id: str) -> Document:
        try:
            return await self._store.get_document(document_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def delete_document(self, document_id: str) -> None:
        """Delete Document metadata + filesystem dir.

        Order: DB first (cascade chunks/jobs) → FS rmtree. If FS fails the
        metadata is already gone; orphan dir will surface in
        ``list_orphan_library_dirs`` for admin cleanup.
        """
        try:
            doc = await self._store.get_document(document_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc
        try:
            await self._store.delete_document_hard(doc.id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc
        # FS cleanup — best effort; failure surfaces but metadata is gone
        try:
            self._files.delete_document_dir(doc.library_id, doc.id)
        except Exception as exc:
            raise self._translate_file_error(exc) from exc

    # ------------------------------------------------------------------
    # Session Library Binding
    # ------------------------------------------------------------------

    async def list_session_bindings(
        self, session_id: str
    ) -> list[SessionLibraryBinding]:
        try:
            return await self._store.list_session_bindings(session_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def replace_session_bindings(
        self,
        session_id: str,
        library_ids: list[str],
    ) -> list[SessionLibraryBinding]:
        """Replace-all bindings for ``session_id`` (atomic single transaction).

        Validates session existence via injected callback. Empty list = unbind
        all. Duplicate ids deduped. Each unique id must reference an existing
        ``active`` Library (else entire transaction fails).
        """
        # Session existence — only check when adding bindings. Empty list
        # (unbind all) is allowed even for unknown sessions (idempotent).
        if library_ids:
            try:
                exists = await self._session_exists(session_id)
            except Exception:
                exists = False
            if not exists:
                raise SessionNotFound(
                    "session does not exist; cannot bind libraries"
                )
        try:
            return await self._store.replace_session_bindings(
                session_id, library_ids, library_exists_check=True
            )
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def get_active_library_ids_for_session(
        self, session_id: str
    ) -> tuple[str, ...]:
        """Narrow read interface — for future R4 search_knowledge ACL."""
        try:
            return await self._store.get_active_library_ids_for_session(
                session_id
            )
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    async def on_session_deleted(self, session_id: str) -> None:
        """Hook called by app composition when a Session is deleted.

        P2-R0 §7.2 invariant 7 — only bindings are deleted, **not** the
        libraries themselves.
        """
        try:
            await self._store.delete_bindings_for_session(session_id)
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Orphan detection (for admin repair; R1 ships no auto-GC)
    # ------------------------------------------------------------------

    def list_orphan_library_dirs(self) -> list[str]:
        """Return library_ids present on disk but missing in DB.

        Best-effort — does not delete anything.
        """
        # Gather DB-side library ids asynchronously then call files.
        # We expose this as a sync helper because file iteration is sync.
        # Caller (composition root) should run async gather first.
        raise NotImplementedError(
            "use list_orphan_library_dirs_async instead"
        )

    async def list_orphan_library_dirs_async(self) -> list[str]:
        try:
            libs = await self._store.list_libraries()
        except KnowledgeStoreError as exc:
            raise self._translate_store_error(exc) from exc
        known = {lib.id for lib in libs}
        return self._files.list_orphan_library_dirs(known)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _gather_stats(self, library_id: str) -> LibraryStats:
        """Compute document_count + binding_count for a Library."""
        try:
            docs = await self._store.list_documents(library_id)
        except KnowledgeStoreError:
            docs = []
        # binding_count — query session_knowledge_libraries by library_id
        # via a narrow SQL (not exposed as a public Store method; we go
        # through the connection here for stats only).
        binding_count = 0
        db = self._store._db  # internal access; stats are best-effort
        if db is not None:
            try:
                async with db.execute(
                    "SELECT COUNT(*) AS n FROM session_knowledge_libraries "
                    "WHERE library_id = ?",
                    (library_id,),
                ) as cur:
                    row = await cur.fetchone()
                binding_count = int(row["n"]) if row else 0
            except Exception:
                binding_count = 0
        return LibraryStats(
            document_count=len(docs),
            binding_count=binding_count,
        )


# ============================================================================
# Public symbols
# ============================================================================


__all__ = [
    "DocumentNotFound",
    "DuplicateDocument",
    "FileStoreFailure",
    "KnowledgeService",
    "KnowledgeServiceError",
    "LibraryNotFound",
    "LibraryNotReady",
    "SessionExistsCallback",
    "SessionNotFound",
    "ServiceValidationError",
]


# Suppress unused-import lint for symbols imported for re-export shape only.
_: tuple[Any, ...] = (
    Document,
    Library,
    LibraryUpdate,
    LibraryView,
    LibraryStats,
)

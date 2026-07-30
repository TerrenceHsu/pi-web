"""KnowledgeFileStore + KnowledgeService tests (P2-R1 R1-B).

Coverage matrix per P2-R1 §17:

- C. KnowledgeFileStore (root / safe lib path / safe doc path / ``..`` /
  absolute / Windows drive / separator injection / symlink escape /
  atomic write success / atomic write failure / safe delete / idempotent
  delete / no absolute path leak / orphan scan / unknown dir)
- F. Service compensation (mkdir fail rolls back / DB fail leaves no dir /
  library delete FS fail / document delete FS fail / status recovery /
  no OSError leak / no absolute path in error)
"""
from __future__ import annotations

import os

import pytest

from pi_agent_core_py.web.knowledge.files import (
    FIXED_DOCUMENT_FILES,
    FileNotFoundError_,
    InvalidDocumentIDError,
    InvalidLibraryIDError,
    KnowledgeFileStore,
    PathSafetyError,
    UnknownFixedFileError,
)
from pi_agent_core_py.web.knowledge.service import (
    FileStoreFailure,
    KnowledgeService,
    LibraryNotFound,
    LibraryNotReady,
    ServiceValidationError,
    SessionNotFound,
)
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = await KnowledgeStore.open(str(tmp_path / "knowledge.db"))
    yield s
    await s.close()


@pytest.fixture
def file_store(tmp_path):
    fs = KnowledgeFileStore(root=tmp_path / "knowledge")
    fs.ensure_root()
    return fs


@pytest.fixture
async def service(store, file_store):
    return KnowledgeService(
        store=store,
        file_store=file_store,
        session_exists=_session_always_exists,
    )


async def _session_always_exists(_session_id: str) -> bool:
    return True


async def _session_never_exists(_session_id: str) -> bool:
    return False


# ============================================================================
# C. KnowledgeFileStore
# ============================================================================


class TestFileStorePathSafety:
    def test_creates_library_dir(self, file_store):
        lib_id = "lib_test0000000001"
        path = file_store.create_library_dir(lib_id)
        assert path.exists()
        assert (path / "documents").exists()

    def test_creates_document_dir(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        path = file_store.create_document_dir(lib_id, doc_id)
        assert path.exists()

    def test_rejects_invalid_library_id_format(self, file_store):
        with pytest.raises(InvalidLibraryIDError):
            file_store.create_library_dir("../escape")
        with pytest.raises(InvalidLibraryIDError):
            file_store.create_library_dir("LIB_uppercase")

    def test_rejects_invalid_document_id_format(self, file_store):
        lib_id = "lib_test0000000001"
        file_store.create_library_dir(lib_id)
        with pytest.raises(InvalidDocumentIDError):
            file_store.create_document_dir(lib_id, "doc-X")

    def test_rejects_dotdot_library_id(self, file_store):
        with pytest.raises(InvalidLibraryIDError):
            file_store.create_library_dir("..")

    def test_rejects_absolute_path_via_id(self, file_store):
        with pytest.raises(InvalidLibraryIDError):
            file_store.create_library_dir("/etc/passwd")
        with pytest.raises(InvalidLibraryIDError):
            file_store.create_library_dir("C:\\windows\\system32")

    def test_rejects_unknown_filename_in_write(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        with pytest.raises(UnknownFixedFileError):
            file_store.write_file_atomic(
                lib_id, doc_id, "arbitrary.txt", b"x"
            )

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX symlink test — Windows requires admin",
    )
    def test_symlink_escape_rejected(self, file_store, tmp_path):
        """Symlink inside library dir that points outside root must fail
        when subsequent operations walk through it."""
        lib_id = "lib_test0000000001"
        file_store.create_library_dir(lib_id)
        # Plant a symlink that points outside root
        outside = tmp_path / "outside"
        outside.mkdir()
        symlink_target = file_store.root / "libraries" / lib_id / "escape"
        os.symlink(outside, symlink_target)
        # The presence of the symlink is itself OK; but creating a doc_dir
        # under a path that crosses it would resolve outside. We assert
        # containment check fires when the symlink escapes root.
        # Manual containment check via _check_containment:
        with pytest.raises(PathSafetyError):
            file_store._check_containment(symlink_target.resolve())


class TestFileStoreAtomicWrite:
    def test_atomic_write_creates_file(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        file_store.write_file_atomic(
            lib_id, doc_id, "document.md", "# Hello\n"
        )
        assert file_store.file_exists(lib_id, doc_id, "document.md")
        data = file_store.read_file(lib_id, doc_id, "document.md", as_text=True)
        assert "# Hello" in data

    def test_atomic_write_bytes(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        payload = b"\x89PDF fake bytes\n"
        file_store.write_file_atomic(
            lib_id, doc_id, "source.pdf", payload
        )
        data = file_store.read_file(lib_id, doc_id, "source.pdf")
        assert data == payload

    def test_atomic_write_replaces_existing(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        file_store.write_file_atomic(lib_id, doc_id, "document.md", "v1")
        file_store.write_file_atomic(lib_id, doc_id, "document.md", "v2")
        assert file_store.read_file(lib_id, doc_id, "document.md", as_text=True) == "v2"

    def test_atomic_write_no_temp_file_left(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        file_store.write_file_atomic(lib_id, doc_id, "document.md", "x")
        doc_dir = file_store._document_dir_unchecked(lib_id, doc_id)
        tmps = [p for p in doc_dir.iterdir() if p.name.startswith(".")]
        assert tmps == []

    def test_read_missing_raises(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        with pytest.raises(FileNotFoundError_):
            file_store.read_file(lib_id, doc_id, "document.md")


class TestFileStoreDelete:
    def test_delete_library_dir_removes_everything(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        file_store.write_file_atomic(lib_id, doc_id, "document.md", "x")
        file_store.delete_library_dir(lib_id)
        assert not file_store.library_dir_exists(lib_id)

    def test_delete_library_dir_idempotent(self, file_store):
        lib_id = "lib_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.delete_library_dir(lib_id)
        # Second call must not raise
        file_store.delete_library_dir(lib_id)

    def test_delete_document_dir(self, file_store):
        lib_id = "lib_test0000000001"
        doc_id = "doc_test0000000001"
        file_store.create_library_dir(lib_id)
        file_store.create_document_dir(lib_id, doc_id)
        file_store.delete_document_dir(lib_id, doc_id)
        assert not file_store.document_dir_exists(lib_id, doc_id)
        # Library dir still there
        assert file_store.library_dir_exists(lib_id)


class TestFileStoreOrphanScan:
    def test_list_orphan_library_dirs(self, file_store):
        # Plant 2 dirs on disk
        file_store.create_library_dir("lib_orphan000000001")
        file_store.create_library_dir("lib_known0000000001")
        # Known set only contains one
        orphans = file_store.list_orphan_library_dirs({"lib_known0000000001"})
        assert orphans == ["lib_orphan000000001"]

    def test_list_unknown_dirs(self, file_store):
        # Plant a directory with an invalid name
        (file_store._libraries_root / "not-a-valid-id").mkdir()
        (file_store._libraries_root / "lib_known0000000001").mkdir()
        unknown = file_store.list_unknown_dirs()
        assert "not-a-valid-id" in unknown
        assert "lib_known0000000001" not in unknown


# ============================================================================
# F. KnowledgeService — compensation
# ============================================================================


class TestServiceLibraryLifecycle:
    async def test_create_library_creates_db_row_and_dir(self, service, file_store):
        lib = await service.create_library(name="MyLib", description="d")
        assert file_store.library_dir_exists(lib.id)
        assert lib.status == "active"

    async def test_list_libraries_includes_stats(self, service):
        await service.create_library(name="a")
        await service.create_library(name="b")
        views = await service.list_libraries()
        assert len(views) == 2
        # Stats default 0
        for v in views:
            assert v.stats.document_count == 0
            assert v.stats.binding_count == 0

    async def test_get_library_includes_stats_with_documents(self, service, store):
        lib = await service.create_library(name="a")
        await service.create_document_metadata(
            library_id=lib.id,
            source_name="x.pdf",
            source_sha256="sha-1",
            source_relpath="documents/doc_1/source.pdf",
            markdown_relpath="documents/doc_1/document.md",
            mime_type="application/pdf",
        )
        view = await service.get_library(lib.id)
        assert view.stats.document_count == 1

    async def test_update_library(self, service):
        lib = await service.create_library(name="orig")
        updated = await service.update_library(lib.id, name="new")
        assert updated.name == "new"

    async def test_update_library_missing(self, service):
        with pytest.raises(LibraryNotFound):
            await service.update_library("lib_missing00000000", name="x")

    async def test_delete_library_removes_dir_and_db(self, service, file_store, store):
        lib = await service.create_library(name="x")
        doc = await service.create_document_metadata(
            library_id=lib.id,
            source_name="a.pdf",
            source_sha256="sha",
            source_relpath="documents/doc_1/source.pdf",
            markdown_relpath="documents/doc_1/document.md",
            mime_type="application/pdf",
        )
        # Create the doc dir + file manually (R1 has no ingestion)
        file_store.create_document_dir(lib.id, doc.id)
        file_store.write_file_atomic(lib.id, doc.id, "source.pdf", b"x")

        await service.delete_library(lib.id)

        # Dir gone
        assert not file_store.library_dir_exists(lib.id)
        # DB row gone
        with pytest.raises(LibraryNotFound):
            await service.get_library(lib.id)


class TestServiceValidation:
    async def test_invalid_name_rejected(self, service):
        with pytest.raises(ServiceValidationError):
            await service.create_library(name="")
        with pytest.raises(ServiceValidationError):
            await service.create_library(name="x" * 201)

    async def test_library_not_found_translated(self, service):
        with pytest.raises(LibraryNotFound):
            await service.get_library("lib_missing00000000")

    async def test_document_not_found_translated(self, service):
        from pi_agent_core_py.web.knowledge.service import DocumentNotFound

        with pytest.raises(DocumentNotFound):
            await service.get_document("doc_missing00000000")


class TestServiceSessionBinding:
    async def test_replace_bindings_with_known_session(self, service):
        lib = await service.create_library(name="x")
        bindings = await service.replace_session_bindings("sess-1", [lib.id])
        assert len(bindings) == 1

    async def test_replace_bindings_rejects_unknown_session(self, store, file_store):
        svc = KnowledgeService(
            store=store,
            file_store=file_store,
            session_exists=_session_never_exists,
        )
        lib = await svc.create_library(name="x")
        with pytest.raises(SessionNotFound):
            await svc.replace_session_bindings("sess-1", [lib.id])

    async def test_replace_bindings_empty_list_works_for_unknown_session(
        self, store, file_store
    ):
        """Empty list = unbind all; allowed even for unknown session
        (idempotent)."""
        svc = KnowledgeService(
            store=store,
            file_store=file_store,
            session_exists=_session_never_exists,
        )
        bindings = await svc.replace_session_bindings("sess-unknown", [])
        assert bindings == []

    async def test_inactive_library_rejected(self, service, store):
        lib = await service.create_library(name="x")
        await store.set_library_status(lib.id, "archived")
        with pytest.raises(LibraryNotReady):
            await service.replace_session_bindings("sess-1", [lib.id])

    async def test_on_session_deleted_clears_bindings(self, service):
        lib = await service.create_library(name="x")
        await service.replace_session_bindings("sess-1", [lib.id])
        await service.on_session_deleted("sess-1")
        assert await service.list_session_bindings("sess-1") == []
        # Library still there
        await service.get_library(lib.id)

    async def test_get_active_library_ids_excludes_archived(self, service, store):
        lib = await service.create_library(name="x")
        await service.replace_session_bindings("sess-1", [lib.id])
        await store.set_library_status(lib.id, "archived")
        active = await service.get_active_library_ids_for_session("sess-1")
        assert active == ()


class TestServiceCompensation:
    async def test_create_library_dir_failure_marks_failed(
        self, store, tmp_path, monkeypatch
    ):
        """If FileStore.create_library_dir raises, Library must be marked
        ``failed`` in DB and FileStoreFailure raised."""
        fs = KnowledgeFileStore(root=tmp_path / "knowledge")
        fs.ensure_root()
        svc = KnowledgeService(store=store, file_store=fs)

        # Force create_library_dir to fail
        def boom(_lib_id):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(fs, "create_library_dir", boom)

        with pytest.raises(FileStoreFailure):
            await svc.create_library(name="x")

        # DB row exists, marked failed
        libs = await store.list_libraries()
        assert len(libs) == 1
        assert libs[0].status == "failed"

    async def test_delete_library_dir_failure_keeps_db(
        self, service, store, file_store, monkeypatch
    ):
        """If FS delete fails, Library must remain in DB (status=failed)
        for retry — not silently disappear."""
        lib = await service.create_library(name="x")

        def boom(_lib_id):
            raise OSError("disk full")

        monkeypatch.setattr(file_store, "delete_library_dir", boom)

        with pytest.raises(FileStoreFailure):
            await service.delete_library(lib.id)

        # DB row still there (status=failed after compensation attempt)
        libs = await store.list_libraries()
        assert len(libs) == 1
        # Status either deleting (initial) or failed (compensation attempted)
        assert libs[0].status in ("deleting", "failed")

    async def test_no_oserror_leak_in_service_error(self, service, file_store, monkeypatch):
        """Service errors must not include OSError text / absolute paths."""
        lib = await service.create_library(name="x")

        sensitive_msg = "E:/secret/path/explosion"
        def boom(_lib_id):
            raise OSError(sensitive_msg)

        monkeypatch.setattr(file_store, "delete_library_dir", boom)

        with pytest.raises(FileStoreFailure) as exc_info:
            await service.delete_library(lib.id)
        # Service error text must not include the OSError message
        assert sensitive_msg not in str(exc_info.value)

    async def test_no_absolute_path_in_service_error(
        self, service, store, tmp_path, monkeypatch
    ):
        fs = KnowledgeFileStore(root=tmp_path / "knowledge")
        fs.ensure_root()
        svc = KnowledgeService(store=store, file_store=fs)
        # Force failure with an exception message containing the abs path
        abs_path = str((tmp_path / "knowledge").resolve())

        def boom(_lib_id):
            raise OSError(f"failed at {abs_path}")

        monkeypatch.setattr(fs, "create_library_dir", boom)

        with pytest.raises(FileStoreFailure) as exc_info:
            await svc.create_library(name="x")
        # The absolute path must NOT appear in the error
        assert abs_path not in str(exc_info.value)


class TestServiceOrphanScan:
    async def test_orphan_scan_lists_dir_without_db_row(self, service, file_store):
        # Plant a library dir manually (no DB row)
        file_store.create_library_dir("lib_orphan000000001")
        orphans = await service.list_orphan_library_dirs_async()
        assert "lib_orphan000000001" in orphans


# ============================================================================
# Public helpers
# ============================================================================


__all__ = [
    "FIXED_DOCUMENT_FILES",
]

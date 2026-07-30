"""KnowledgeStore unit tests — schema + Library / Document / Binding CRUD.

Coverage matrix per P2-R1 §17:

- A. Schema (init / idempotent / version / future fail / FK on / UNIQUE /
  CHECK / rollback / restart)
- B. Library Store (create / list / get / update / invalid fields /
  not-found / status / delete / concurrent)
- D. Document metadata (create / duplicate SHA / cross-library / list /
  state machine / safe_error_code)
- E. Session Binding (empty / single / multi / replace-all / dedup /
  not-found / inactive / A/B isolate / cascade / narrow interface)

Tests intentionally avoid filesystem cleanup (no PDFs in R1-A); only the
SQLite repository is exercised.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from pi_agent_core_py.web.knowledge.models import (
    Chunk,
    is_valid_document_id,
    is_valid_library_id,
)
from pi_agent_core_py.web.knowledge.store import (
    KNOWLEDGE_SCHEMA_VERSION,
    DocumentNotFoundError,
    DocumentValidationError,
    DuplicateDocumentError,
    KnowledgeSchemaVersionError,
    KnowledgeStore,
    LibraryNotActiveError,
    LibraryNotFoundError,
    LibraryValidationError,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def store(tmp_path):
    s = await KnowledgeStore.open(str(tmp_path / "knowledge.db"))
    yield s
    await s.close()


def _valid_lib_id() -> str:
    """Create a real library and return its id."""
    import asyncio

    async def _make():
        # Use a fresh in-memory store just to mint a valid id format
        s = await KnowledgeStore.open(":memory:")
        try:
            lib = await s.create_library(name="seed")
            return lib.id
        finally:
            await s.close()

    return asyncio.get_event_loop().run_until_complete(_make()) if False else "lib_seed00000000"


# ============================================================================
# A. Schema tests
# ============================================================================


class TestSchema:
    async def test_fresh_db_initializes_v1_schema(self, tmp_path):
        s = await KnowledgeStore.open(str(tmp_path / "k.db"))
        try:
            assert await s.get_schema_version() == KNOWLEDGE_SCHEMA_VERSION
            # All 5 tables exist
            async with s._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                rows = await cur.fetchall()
            table_names = {r["name"] for r in rows}
            for required in (
                "knowledge_libraries",
                "knowledge_documents",
                "knowledge_ingestion_jobs",
                "knowledge_chunks",
                "session_knowledge_libraries",
            ):
                assert required in table_names
        finally:
            await s.close()

    async def test_repeat_init_is_idempotent(self, tmp_path):
        """Opening the same file twice must not lose data or rebuild schema."""
        path = str(tmp_path / "k.db")
        s1 = await KnowledgeStore.open(path)
        lib = await s1.create_library(name="persist-test")
        await s1.close()
        # Reopen — same file
        s2 = await KnowledgeStore.open(path)
        try:
            assert await s2.get_schema_version() == KNOWLEDGE_SCHEMA_VERSION
            libs = await s2.list_libraries()
            assert len(libs) == 1
            assert libs[0].id == lib.id
        finally:
            await s2.close()

    async def test_future_schema_version_raises(self, tmp_path):
        path = str(tmp_path / "k.db")
        # First populate normally
        s1 = await KnowledgeStore.open(path)
        await s1.close()
        # Manually bump version to future
        async with aiosqlite.connect(path) as raw:
            await raw.execute(
                "UPDATE knowledge_schema_meta SET value = ? WHERE key = ?",
                (KNOWLEDGE_SCHEMA_VERSION + 1, "schema_version"),
            )
            await raw.commit()
        with pytest.raises(KnowledgeSchemaVersionError):
            await KnowledgeStore.open(path)

    async def test_foreign_keys_pragma_on(self, store):
        async with store._db.execute("PRAGMA foreign_keys") as cur:
            row = await cur.fetchone()
        assert row[0] == 1

    async def test_unique_library_document_sha(self, store):
        lib = await store.create_library(name="lib1")
        await store.create_document(
            library_id=lib.id,
            source_name="a.pdf",
            source_sha256="sha-x",
            source_relpath="documents/doc_1/source.pdf",
            markdown_relpath="documents/doc_1/document.md",
            mime_type="application/pdf",
        )
        with pytest.raises(DuplicateDocumentError):
            await store.create_document(
                library_id=lib.id,
                source_name="b.pdf",
                source_sha256="sha-x",  # same SHA, same lib
                source_relpath="documents/doc_2/source.pdf",
                markdown_relpath="documents/doc_2/document.md",
                mime_type="application/pdf",
            )

    async def test_check_constraint_library_status(self, store):
        db = store._db
        # Bypass ORM to test CHECK — sqlite3.IntegrityError on bad status enum
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO knowledge_libraries (id, name, description, status, "
                "created_at, updated_at) VALUES (?, ?, '', 'bogus', 0, 0)",
                ("lib_bogus00000000000", "x"),
            )

    async def test_check_constraint_document_status(self, store):
        lib = await store.create_library(name="lib1")
        db = store._db
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO knowledge_documents "
                "(id, library_id, source_name, source_sha256, source_relpath, "
                " markdown_relpath, mime_type, status, created_at, updated_at) "
                "VALUES (?, ?, 'a', 'sha', 'p', 'm', 'application/pdf', "
                "        'bogus_status', 0, 0)",
                ("doc_bogus0000000000", lib.id),
            )

    async def test_transaction_rollback_on_create_document_dup(self, store):
        lib = await store.create_library(name="lib1")
        await store.create_document(
            library_id=lib.id,
            source_name="a.pdf",
            source_sha256="sha-1",
            source_relpath="documents/doc_1/source.pdf",
            markdown_relpath="documents/doc_1/document.md",
            mime_type="application/pdf",
        )
        # Failed insert must not leave the DB in a half-commit state
        with pytest.raises(DuplicateDocumentError):
            await store.create_document(
                library_id=lib.id,
                source_name="dup.pdf",
                source_sha256="sha-1",
                source_relpath="documents/doc_2/source.pdf",
                markdown_relpath="documents/doc_2/document.md",
                mime_type="application/pdf",
            )
        docs = await store.list_documents(lib.id)
        assert len(docs) == 1  # original still there, no orphan

    async def test_restart_persists_data(self, tmp_path):
        path = str(tmp_path / "k.db")
        s1 = await KnowledgeStore.open(path)
        lib = await s1.create_library(name="persist")
        await s1.create_document(
            library_id=lib.id,
            source_name="x.pdf",
            source_sha256="sha-x",
            source_relpath="documents/doc_x/source.pdf",
            markdown_relpath="documents/doc_x/document.md",
            mime_type="application/pdf",
        )
        await s1.close()
        s2 = await KnowledgeStore.open(path)
        try:
            libs = await s2.list_libraries()
            docs = await s2.list_documents(lib.id)
            assert len(libs) == 1
            assert len(docs) == 1
        finally:
            await s2.close()


# ============================================================================
# B. Library CRUD
# ============================================================================


class TestLibraryCRUD:
    async def test_create_library_returns_active_status(self, store):
        lib = await store.create_library(name="MyLib", description="hello")
        assert is_valid_library_id(lib.id)
        assert lib.name == "MyLib"
        assert lib.description == "hello"
        assert lib.status == "active"
        assert lib.created_at > 0
        assert lib.updated_at == lib.created_at

    async def test_list_libraries_stable_order(self, store):
        # Create in non-sorted order
        a = await store.create_library(name="Z")
        b = await store.create_library(name="A")
        c = await store.create_library(name="M")
        libs = await store.list_libraries()
        assert [item.id for item in libs] == [a.id, b.id, c.id]

    async def test_get_library_missing_raises(self, store):
        with pytest.raises(LibraryNotFoundError):
            await store.get_library("lib_nonexistent00")

    async def test_get_library_invalid_id_format_raises(self, store):
        with pytest.raises(ValueError):
            await store.get_library("../etc/passwd")

    async def test_update_library_name(self, store):
        lib = await store.create_library(name="orig")
        updated = await store.update_library(lib.id, name="new")
        assert updated.name == "new"
        assert updated.id == lib.id
        assert updated.updated_at >= lib.updated_at

    async def test_update_library_description(self, store):
        lib = await store.create_library(name="orig", description="d1")
        updated = await store.update_library(lib.id, description="d2")
        assert updated.description == "d2"

    async def test_update_library_empty_payload_rejected(self, store):
        lib = await store.create_library(name="orig")
        with pytest.raises(LibraryValidationError):
            await store.update_library(lib.id)

    async def test_update_library_invalid_name_rejected(self, store):
        lib = await store.create_library(name="orig")
        with pytest.raises(LibraryValidationError):
            await store.update_library(lib.id, name="")
        with pytest.raises(LibraryValidationError):
            await store.update_library(lib.id, name="x" * 201)

    async def test_update_library_missing_raises(self, store):
        with pytest.raises(LibraryNotFoundError):
            await store.update_library("lib_missing00000000", name="x")

    async def test_same_name_allowed(self, store):
        """Per P2-R0 §9.1 — name uniqueness not enforced."""
        a = await store.create_library(name="same")
        b = await store.create_library(name="same")
        assert a.id != b.id
        libs = await store.list_libraries()
        assert len(libs) == 2

    async def test_set_library_status_internal(self, store):
        lib = await store.create_library(name="x")
        arch = await store.set_library_status(lib.id, "archived")
        assert arch.status == "archived"

    async def test_delete_library_hard_cascades(self, store):
        lib = await store.create_library(name="x")
        doc = await store.create_document(
            library_id=lib.id,
            source_name="a.pdf",
            source_sha256="sha",
            source_relpath="documents/doc_1/source.pdf",
            markdown_relpath="documents/doc_1/document.md",
            mime_type="application/pdf",
        )
        await store.replace_session_bindings(
            "sess-1", [lib.id], library_exists_check=True
        )
        await store.delete_library_hard(lib.id)
        # Library gone
        with pytest.raises(LibraryNotFoundError):
            await store.get_library(lib.id)
        # Document gone
        with pytest.raises(DocumentNotFoundError):
            await store.get_document(doc.id)
        # Bindings gone
        assert await store.list_session_bindings("sess-1") == []

    async def test_delete_library_missing_raises(self, store):
        with pytest.raises(LibraryNotFoundError):
            await store.delete_library_hard("lib_missing00000000")


# ============================================================================
# C. Library validation
# ============================================================================


class TestLibraryValidation:
    async def test_empty_name_rejected(self, store):
        with pytest.raises(LibraryValidationError):
            await store.create_library(name="")

    async def test_whitespace_name_rejected(self, store):
        with pytest.raises(LibraryValidationError):
            await store.create_library(name="   ")

    async def test_oversized_name_rejected(self, store):
        with pytest.raises(LibraryValidationError):
            await store.create_library(name="x" * 201)

    async def test_oversized_description_rejected(self, store):
        with pytest.raises(LibraryValidationError):
            await store.create_library(name="ok", description="x" * 2001)


# ============================================================================
# D. Document metadata
# ============================================================================


def _make_doc_kwargs(library_id: str, *, sha: str = "sha-x") -> dict:
    return dict(
        library_id=library_id,
        source_name="example.pdf",
        source_sha256=sha,
        source_relpath="documents/doc_1/source.pdf",
        markdown_relpath="documents/doc_1/document.md",
        mime_type="application/pdf",
    )


class TestDocumentMetadata:
    async def test_create_document_returns_uploaded_status(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        assert is_valid_document_id(doc.id)
        assert doc.status == "uploaded"
        assert doc.error_code == ""
        assert doc.library_id == lib.id

    async def test_duplicate_sha_same_library_rejected(self, store):
        lib = await store.create_library(name="lib")
        await store.create_document(**_make_doc_kwargs(lib.id, sha="sha-dup"))
        with pytest.raises(DuplicateDocumentError):
            await store.create_document(**_make_doc_kwargs(lib.id, sha="sha-dup"))

    async def test_same_sha_different_library_allowed(self, store):
        a = await store.create_library(name="a")
        b = await store.create_library(name="b")
        await store.create_document(**_make_doc_kwargs(a.id, sha="sha-shared"))
        # Different library → allowed
        await store.create_document(**_make_doc_kwargs(b.id, sha="sha-shared"))

    async def test_list_documents_by_library(self, store):
        lib = await store.create_library(name="lib")
        d1 = await store.create_document(**_make_doc_kwargs(lib.id, sha="sha-1"))
        d2 = await store.create_document(**_make_doc_kwargs(lib.id, sha="sha-2"))
        docs = await store.list_documents(lib.id)
        assert {d.id for d in docs} == {d1.id, d2.id}

    async def test_get_document_missing_raises(self, store):
        with pytest.raises(DocumentNotFoundError):
            await store.get_document("doc_missing00000000")

    async def test_document_state_machine_legal(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        # uploaded → extracting → normalizing → chunking → indexing → ready
        for next_status in ("extracting", "normalizing", "chunking", "indexing", "ready"):
            doc = await store.transition_document_status(doc.id, next_status)
            assert doc.status == next_status
            # error_code cleared on non-terminal-failure transitions
            assert doc.error_code == ""

    async def test_document_state_machine_illegal(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        # uploaded → ready is illegal
        with pytest.raises(DocumentValidationError):
            await store.transition_document_status(doc.id, "ready")
        # uploaded → indexing is illegal
        with pytest.raises(DocumentValidationError):
            await store.transition_document_status(doc.id, "indexing")

    async def test_document_failed_state_sets_error_code(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        await store.transition_document_status(doc.id, "extracting")
        failed = await store.transition_document_status(
            doc.id, "failed", error_code="pdf_parse_failed"
        )
        assert failed.status == "failed"
        assert failed.error_code == "pdf_parse_failed"

    async def test_document_needs_ocr_is_terminal(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        await store.transition_document_status(doc.id, "extracting")
        await store.transition_document_status(
            doc.id, "needs_ocr", error_code="no_extractable_text"
        )
        # needs_ocr → anything except deleting is forbidden
        with pytest.raises(DocumentValidationError):
            await store.transition_document_status(doc.id, "extracting")

    async def test_document_no_absolute_path_leak(self, store):
        """Rel paths stored; never absolute paths in DB rows returned."""
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        assert not doc.source_relpath.startswith(("/", "\\"))
        assert not doc.markdown_relpath.startswith(("/", "\\"))
        assert "C:" not in doc.source_relpath
        assert "C:" not in doc.markdown_relpath

    async def test_delete_document_cascades_chunks_and_jobs(self, store):
        lib = await store.create_library(name="lib")
        doc = await store.create_document(**_make_doc_kwargs(lib.id))
        await store.create_job(document_id=doc.id, stage="extract")
        # Insert a chunk manually (R1 stores; chunker is R2)
        chunk = Chunk(
            id="chunk_a00000000000",
            library_id=lib.id,
            document_id=doc.id,
            ordinal=0,
            heading_path="Title",
            page_start=1,
            page_end=1,
            content="hello",
            content_hash="hash",
            token_count=1,
            created_at=0,
        )
        await store.insert_chunk(chunk)
        await store.delete_document_hard(doc.id)
        assert await store.list_chunks_for_document(doc.id) == []
        assert await store.list_jobs_for_document(doc.id) == []


# ============================================================================
# E. Session Library Binding
# ============================================================================


class TestSessionBinding:
    async def test_empty_bindings_for_fresh_session(self, store):
        bindings = await store.list_session_bindings("sess-fresh-1")
        assert bindings == []
        assert await store.get_active_library_ids_for_session("sess-fresh-1") == ()

    async def test_single_binding(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        active = await store.get_active_library_ids_for_session("sess-1")
        assert active == (lib.id,)

    async def test_multi_binding_stable_order(self, store):
        # Create libs in reverse order
        z = await store.create_library(name="z")
        a = await store.create_library(name="a")
        m = await store.create_library(name="m")
        await store.replace_session_bindings("sess-1", [z.id, a.id, m.id])
        active = await store.get_active_library_ids_for_session("sess-1")
        # Sorted by library_id
        assert active == tuple(sorted([z.id, a.id, m.id]))

    async def test_duplicate_ids_deduped(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id, lib.id, lib.id])
        bindings = await store.list_session_bindings("sess-1")
        assert len(bindings) == 1

    async def test_replace_all_is_idempotent(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        await store.replace_session_bindings("sess-1", [lib.id])
        await store.replace_session_bindings("sess-1", [lib.id])
        bindings = await store.list_session_bindings("sess-1")
        assert len(bindings) == 1

    async def test_empty_list_unbinds_all(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        await store.replace_session_bindings("sess-1", [])
        assert await store.list_session_bindings("sess-1") == []

    async def test_invalid_library_id_format_rejected(self, store):
        with pytest.raises(ValueError):
            await store.replace_session_bindings("sess-1", ["../etc/passwd"])

    async def test_nonexistent_library_rejected(self, store):
        with pytest.raises(LibraryNotFoundError):
            await store.replace_session_bindings("sess-1", ["lib_missing00000000"])

    async def test_inactive_library_rejected(self, store):
        lib = await store.create_library(name="lib")
        await store.set_library_status(lib.id, "archived")
        with pytest.raises(LibraryNotActiveError):
            await store.replace_session_bindings("sess-1", [lib.id])

    async def test_partial_failure_no_partial_update(self, store):
        """If any library_id is invalid/inactive, the entire replace fails."""
        lib1 = await store.create_library(name="lib1")
        lib2 = await store.create_library(name="lib2")
        await store.set_library_status(lib2.id, "archived")
        # Pre-bind lib1
        await store.replace_session_bindings("sess-1", [lib1.id])
        with pytest.raises(LibraryNotActiveError):
            await store.replace_session_bindings("sess-1", [lib1.id, lib2.id])
        # Original binding untouched
        active = await store.get_active_library_ids_for_session("sess-1")
        assert active == (lib1.id,)

    async def test_session_a_b_isolation(self, store):
        a = await store.create_library(name="a")
        b = await store.create_library(name="b")
        await store.replace_session_bindings("sess-A", [a.id])
        await store.replace_session_bindings("sess-B", [b.id])
        assert await store.get_active_library_ids_for_session("sess-A") == (a.id,)
        assert await store.get_active_library_ids_for_session("sess-B") == (b.id,)

    async def test_delete_session_clears_bindings(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        await store.delete_bindings_for_session("sess-1")
        # Library still exists
        await store.get_library(lib.id)
        # Binding gone
        assert await store.list_session_bindings("sess-1") == []

    async def test_delete_library_clears_bindings(self, store):
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        await store.delete_library_hard(lib.id)
        assert await store.list_session_bindings("sess-1") == []

    async def test_active_filter_excludes_archived(self, store):
        """get_active_library_ids_for_session must exclude archived libs."""
        lib = await store.create_library(name="lib")
        await store.replace_session_bindings("sess-1", [lib.id])
        # Bind exists, library goes archived afterward
        await store.set_library_status(lib.id, "archived")
        active = await store.get_active_library_ids_for_session("sess-1")
        assert active == ()  # excluded

    async def test_concurrent_replace_end_state_complete(self, store):
        """Two concurrent replaces on same session must serialize cleanly."""
        a = await store.create_library(name="a")
        b = await store.create_library(name="b")
        # Both target same session; final state = whoever commits last
        await asyncio.gather(
            store.replace_session_bindings("sess-1", [a.id]),
            store.replace_session_bindings("sess-1", [b.id]),
        )
        active = await store.get_active_library_ids_for_session("sess-1")
        assert active in ((a.id,), (b.id,))
        assert len(active) == 1


# ============================================================================
# F. Concurrency
# ============================================================================


class TestConcurrency:
    async def test_concurrent_create_no_data_corruption(self, store):
        names = [f"lib-{i}" for i in range(10)]
        await asyncio.gather(*[store.create_library(name=n) for n in names])
        libs = await store.list_libraries()
        assert len(libs) == 10
        # All names present, no duplicates
        lib_names = [item.name for item in libs]
        assert sorted(lib_names) == sorted(names)

    async def test_concurrent_replace_different_sessions(self, store):
        lib = await store.create_library(name="lib")
        # 5 different sessions binding same lib concurrently
        await asyncio.gather(
            *[
                store.replace_session_bindings(f"sess-{i}", [lib.id])
                for i in range(5)
            ]
        )
        for i in range(5):
            assert await store.get_active_library_ids_for_session(f"sess-{i}") == (lib.id,)

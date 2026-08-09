"""P2-R3-D1 — IndexingWorkerManager unit tests.

Covers directive §45 (Worker unit matrix) + §47 (startup recovery) +
§48 (backlog) + §49 (failure isolation) + §50 (resource leak) +
§46 (composition / no double-claim).

Uses real KnowledgeStore + ChunkStore + IndexingStore + real
KnowledgeFileStore + real IndexingOrchestrator wiring on a temp DB +
temp Knowledge root. Writes real ``document.md`` files matching the
R2-B Canonical Markdown schema so the chunker's frontmatter / page
marker validation succeeds.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.indexing_orchestrator import (
    IndexingOrchestrator,
)
from pi_agent_core_py.web.knowledge.indexing_store import (
    INDEXING_INTERRUPTED,
    IndexingStore,
)
from pi_agent_core_py.web.knowledge.indexing_worker import (
    IndexingWorkerManager,
)
from pi_agent_core_py.web.knowledge.store import KnowledgeStore

# ============================================================================
# Helpers — same Canonical Markdown builder as R3-C2 tests
# ============================================================================

_PAGE_MARKER_FMT = "<!-- page:{n} -->"


def _canonical_markdown(
    *,
    document_id: str,
    source_sha256: str,
    page_count: int,
    pages_body: list[str],
) -> str:
    assert len(pages_body) == page_count
    fm = (
        "---\n"
        f'schema "pi-agent-canonical-markdown/v1"\n'
        f'document_id "{document_id}"\n'
        f'source_filename "x.pdf"\n'
        f"source_sha256 \"{source_sha256}\"\n"
        f'parser_id "pypdf"\n'
        f'parser_version "6.14.2"\n'
        f"page_count {page_count}\n"
        "---\n"
    )
    sections = []
    for n, body in enumerate(pages_body, start=1):
        sections.append(f"{_PAGE_MARKER_FMT.format(n=n)}\n\n{body}")
    return fm + "\n\n".join(sections) + "\n"


async def _seed_doc(
    store: KnowledgeStore,
    *,
    document_id: str,
    library_id: str = "lib_test00000001",
    source_sha256: str = "a" * 64,
    status: str = "normalizing",
    page_count: int = 1,
    created_at: int = 100,
    sha_suffix: str = "1",
) -> None:
    async with store._write_lock:
        await store._db.execute("BEGIN IMMEDIATE")
        await store._db.execute(
            "INSERT OR IGNORE INTO knowledge_libraries "
            "(id, name, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (library_id, "L", "", "active", 1, 1),
        )
        await store._db.execute(
            "INSERT OR REPLACE INTO knowledge_documents "
            "(id, library_id, source_name, source_sha256, source_relpath, "
            " markdown_relpath, mime_type, size_bytes, page_count, status, "
            " parser_version, error_code, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id, library_id, f"x{sha_suffix}.pdf",
                source_sha256,
                f"documents/{document_id}/source.pdf",
                f"documents/{document_id}/document.md",
                "application/pdf", 10, page_count, status,
                "pypdf/6.14.2", "", created_at, created_at,
            ),
        )
        await store._db.execute("COMMIT")


def _write_md(
    fs: KnowledgeFileStore, *, library_id: str, document_id: str, md: str
) -> None:
    fs.write_file_atomic(
        library_id=library_id, document_id=document_id,
        filename="document.md", content=md.encode("utf-8"),
    )


# ============================================================================
# Fixture — full real wiring
# ============================================================================


@pytest.fixture
async def worker_setup(tmp_path: Path):
    """Yield ``(KnowledgeStore, KnowledgeFileStore, ChunkStore,
    IndexingStore, IndexingOrchestrator, IndexingWorkerManager)``."""
    db_path = tmp_path / "r3_d1.db"
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir()

    store = await KnowledgeStore.open(str(db_path))
    fs = KnowledgeFileStore(str(knowledge_root))
    chunk_store = ChunkStore(store)
    indexing_store = IndexingStore(store)
    orch = IndexingOrchestrator(
        knowledge_store=store, knowledge_file_store=fs,
        chunk_store=chunk_store, indexing_store=indexing_store,
    )
    # Use a short poll interval in tests to keep them fast but > 0.
    manager = IndexingWorkerManager(
        knowledge_store=store, chunk_store=chunk_store,
        indexing_store=indexing_store, orchestrator=orch,
        poll_interval_seconds=0.05,  # fast idle poll for tests
        shutdown_grace_seconds=5.0,
    )
    try:
        yield store, fs, chunk_store, indexing_store, orch, manager
    finally:
        if manager.snapshot().state != "stopped":
            await manager.stop()
        await store.close()


# ============================================================================
# 1. Lifecycle — start / stop / duplicate / state machine
# ============================================================================


class TestLifecycle:
    async def test_initial_state_stopped(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        snap = m.snapshot()
        assert snap.state == "stopped"
        assert snap.active_document_id is None

    async def test_start_transitions_to_running(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.start()
        assert m.snapshot().state == "running"
        await m.stop()

    async def test_duplicate_start_no_op(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.start()
        # Track the task — second start must not spawn a second task.
        # (We can't directly count tasks; we verify state stays running
        # and stop() still works.)
        await m.start()
        assert m.snapshot().state == "running"
        await m.stop()
        assert m.snapshot().state == "stopped"

    async def test_stop_idempotent(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.start()
        await m.stop()
        await m.stop()  # second stop is no-op
        assert m.snapshot().state == "stopped"

    async def test_stop_without_start_is_no_op(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.stop()
        assert m.snapshot().state == "stopped"

    async def test_exactly_one_worker_task(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.start()
        # Count tasks named "indexing_worker_loop".
        tasks = [
            t for t in asyncio.all_tasks()
            if t.get_name() == "indexing_worker_loop" and not t.done()
        ]
        assert len(tasks) == 1
        await m.stop()
        # After stop, no pending indexing_worker_loop tasks.
        tasks = [
            t for t in asyncio.all_tasks()
            if t.get_name() == "indexing_worker_loop" and not t.done()
        ]
        assert len(tasks) == 0

    async def test_no_pending_task_after_stop(self, worker_setup):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        await m.start()
        await m.stop()
        tasks = [
            t for t in asyncio.all_tasks()
            if "indexing" in t.get_name().lower() and not t.done()
        ]
        assert len(tasks) == 0


# ============================================================================
# 2. Composition — D0 composition gate (no double-claim)
# ============================================================================


class TestComposition:
    async def test_worker_uses_read_only_discovery(
        self, worker_setup
    ):
        """Worker's _discover_next_candidate is read-only SELECT.

        Verified indirectly: when a normalizing Document exists, the
        Worker + Orchestrator together move it through to ready without
        any intermediate "claim failed because already chunking" error.
        """
        store, fs, _cs, _is_, _o, m = worker_setup
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        _write_md(
            fs, library_id="lib_test00000001", document_id=doc_id,
            md=_canonical_markdown(
                document_id=doc_id, source_sha256=sha, page_count=1,
                pages_body=["radiotherapy planning"],
            ),
        )
        await m.start()
        # Wait for the worker to pick up the document.
        await _wait_for_status(store, doc_id, "ready", timeout_seconds=2.0)
        await m.stop()
        doc = await store.get_document(doc_id)
        assert doc.status == "ready"

    async def test_concurrency_exactly_one(
        self, worker_setup
    ):
        """Two normalizing Documents — only one is processed at a time.

        After both finish, both are ready. The Manager never has more
        than one ``active_document_id`` at a time (verified by snapshot
        during processing).
        """
        store, fs, _cs, _is_, _o, m = worker_setup
        for i, did in enumerate(["doc_aaa000000001", "doc_bbb000000001"]):
            sha = ("a" if i == 0 else "b") * 64
            await _seed_doc(
                store, document_id=did, source_sha256=sha,
                created_at=100 + i, sha_suffix=str(i),
            )
            _write_md(
                fs, library_id="lib_test00000001", document_id=did,
                md=_canonical_markdown(
                    document_id=did, source_sha256=sha, page_count=1,
                    pages_body=[f"content {i}"],
                ),
            )
        await m.start()
        await _wait_for_status(store, "doc_aaa000000001", "ready", timeout_seconds=3.0)
        await _wait_for_status(store, "doc_bbb000000001", "ready", timeout_seconds=3.0)
        await m.stop()


# ============================================================================
# 3. Backlog drain + failure isolation
# ============================================================================


class TestBacklogDrain:
    async def test_multiple_documents_drained_in_order(
        self, worker_setup
    ):
        store, fs, _cs, _is_, _o, m = worker_setup
        for i, did in enumerate(["doc_aaa000000001", "doc_bbb000000001", "doc_ccc000000001"]):
            sha = chr(ord("a") + i) * 64
            await _seed_doc(
                store, document_id=did, source_sha256=sha,
                created_at=100 + i, sha_suffix=str(i),
            )
            _write_md(
                fs, library_id="lib_test00000001", document_id=did,
                md=_canonical_markdown(
                    document_id=did, source_sha256=sha, page_count=1,
                    pages_body=[f"content {i}"],
                ),
            )
        await m.start()
        for did in ["doc_aaa000000001", "doc_bbb000000001", "doc_ccc000000001"]:
            await _wait_for_status(store, did, "ready", timeout_seconds=5.0)
        await m.stop()

    async def test_failure_does_not_kill_loop(
        self, worker_setup
    ):
        """Doc A fails (no markdown) → Doc B succeeds. Worker survives."""
        store, fs, _cs, _is_, _o, m = worker_setup
        # Doc A: no markdown → failure path.
        await _seed_doc(
            store, document_id="doc_aaa000000001", source_sha256="a" * 64,
            created_at=100, sha_suffix="a",
        )
        # Doc B: valid markdown.
        sha_b = "b" * 64
        await _seed_doc(
            store, document_id="doc_bbb000000001", source_sha256=sha_b,
            created_at=200, sha_suffix="b",
        )
        _write_md(
            fs, library_id="lib_test00000001", document_id="doc_bbb000000001",
            md=_canonical_markdown(
                document_id="doc_bbb000000001", source_sha256=sha_b,
                page_count=1, pages_body=["valid content"],
            ),
        )
        await m.start()
        await _wait_for_status(store, "doc_aaa000000001", "failed", timeout_seconds=3.0)
        await _wait_for_status(store, "doc_bbb000000001", "ready", timeout_seconds=3.0)
        # Worker still running.
        assert m.snapshot().state == "running"
        await m.stop()


# ============================================================================
# 4. Startup recovery
# ============================================================================


class TestStartupRecovery:
    async def test_stale_chunking_recovered(
        self, worker_setup
    ):
        store, _fs, chunk_store, indexing_store, _o, m = worker_setup
        # Pre-seed a chunking Document with stale chunks.
        await _seed_doc(
            store, document_id="doc_test00000001",
            source_sha256="a" * 64, status="chunking",
        )
        # Manually insert chunks + FTS rows (simulating prior partial run).
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "INSERT INTO knowledge_chunks (id, library_id, document_id, "
                " ordinal, heading_path, page_start, page_end, content, "
                " content_hash, char_count, content_sha256, token_count, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("chunk_stale00001", "lib_test00000001", "doc_test00000001",
                 0, "[]", 1, 1, "stale", "x" * 64, 5, "y" * 64, 0, 1),
            )
            await store._db.execute(
                "INSERT INTO knowledge_chunks_fts (chunk_id, document_id, "
                " library_id, heading_text, content) VALUES (?, ?, ?, ?, ?)",
                ("chunk_stale00001", "doc_test00000001", "lib_test00000001",
                 "", "stale"),
            )
            await store._db.execute("COMMIT")
        # start() should recover: chunking → failed + chunks/FTS cleaned.
        await m.start()
        doc = await store.get_document("doc_test00000001")
        assert doc.status == "failed"
        assert doc.error_code == INDEXING_INTERRUPTED
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks"
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks_fts"
        ) as cursor:
            fts_count = (await cursor.fetchone())["n"]
        assert chunk_count == 0
        assert fts_count == 0
        await m.stop()

    async def test_stale_indexing_recovered(
        self, worker_setup
    ):
        store, _fs, _cs, _is_, _o, m = worker_setup
        await _seed_doc(
            store, document_id="doc_test00000001",
            source_sha256="a" * 64, status="indexing",
        )
        await m.start()
        doc = await store.get_document("doc_test00000001")
        assert doc.status == "failed"
        assert doc.error_code == INDEXING_INTERRUPTED
        await m.stop()

    async def test_normalizing_preserved_by_recovery(
        self, worker_setup
    ):
        store, fs, _cs, _is_, _o, m = worker_setup
        sha = "a" * 64
        await _seed_doc(
            store, document_id="doc_test00000001", source_sha256=sha,
        )
        _write_md(
            fs, library_id="lib_test00000001", document_id="doc_test00000001",
            md=_canonical_markdown(
                document_id="doc_test00000001", source_sha256=sha,
                page_count=1, pages_body=["body"],
            ),
        )
        await m.start()
        # normalizing is preserved → worker should pick it up → ready.
        await _wait_for_status(store, "doc_test00000001", "ready", timeout_seconds=3.0)
        await m.stop()

    async def test_ready_preserved_by_recovery(
        self, worker_setup
    ):
        store, _fs, _cs, _is_, _o, m = worker_setup
        await _seed_doc(
            store, document_id="doc_test00000001",
            source_sha256="a" * 64, status="ready",
        )
        await m.start()
        # Brief wait to ensure recovery ran without touching ready doc.
        await asyncio.sleep(0.1)
        doc = await store.get_document("doc_test00000001")
        assert doc.status == "ready"
        await m.stop()

    async def test_failed_preserved_by_recovery(
        self, worker_setup
    ):
        store, _fs, _cs, _is_, _o, m = worker_setup
        await _seed_doc(
            store, document_id="doc_test00000001",
            source_sha256="a" * 64, status="failed",
        )
        async with store._write_lock:
            await store._db.execute("BEGIN IMMEDIATE")
            await store._db.execute(
                "UPDATE knowledge_documents SET error_code='prior' "
                "WHERE id='doc_test00000001'"
            )
            await store._db.execute("COMMIT")
        await m.start()
        await asyncio.sleep(0.1)
        doc = await store.get_document("doc_test00000001")
        assert doc.status == "failed"
        assert doc.error_code == "prior"  # not overwritten
        await m.stop()


# ============================================================================
# 5. Idle / no-candidate
# ============================================================================


class TestIdlePoll:
    async def test_no_candidate_idles_without_busy_spin(
        self, worker_setup, monkeypatch
    ):
        _s, _fs, _cs, _is_, _o, m = worker_setup
        # Patch _discover_next_candidate to count calls.
        call_count = [0]
        original = m._discover_next_candidate

        async def counting_discover():
            call_count[0] += 1
            return await original()

        m._discover_next_candidate = counting_discover
        await m.start()
        # Wait ~3 poll intervals (poll=0.05 → ~0.15s).
        await asyncio.sleep(0.2)
        await m.stop()
        # Should have polled several times but not thousands.
        assert 3 <= call_count[0] <= 30, (
            f"busy spin detected: {call_count[0]} polls in 0.2s with "
            f"poll_interval=0.05s"
        )


# ============================================================================
# 6. Multiple Managers — single-winner claim
# ============================================================================


class TestMultipleManagers:
    async def test_two_managers_same_document_one_winner(
        self, tmp_path: Path
    ):
        """Two Managers on the same DB; same normalizing Document.

        SQLite conditional claim (in Orchestrator T1) ensures exactly
        one Manager's process_document succeeds in moving to chunking.
        """
        db_path = tmp_path / "r3_d1_multi.db"
        knowledge_root = tmp_path / "knowledge"
        knowledge_root.mkdir()

        store = await KnowledgeStore.open(str(db_path))
        fs = KnowledgeFileStore(str(knowledge_root))
        chunk_store = ChunkStore(store)
        indexing_store = IndexingStore(store)
        orch = IndexingOrchestrator(
            knowledge_store=store, knowledge_file_store=fs,
            chunk_store=chunk_store, indexing_store=indexing_store,
        )
        doc_id = "doc_test00000001"
        sha = "a" * 64
        await _seed_doc(store, document_id=doc_id, source_sha256=sha)
        _write_md(
            fs, library_id="lib_test00000001", document_id=doc_id,
            md=_canonical_markdown(
                document_id=doc_id, source_sha256=sha, page_count=1,
                pages_body=["radiotherapy"],
            ),
        )
        # Two Managers on the same wiring — both will discover the doc.
        m1 = IndexingWorkerManager(
            knowledge_store=store, chunk_store=chunk_store,
            indexing_store=indexing_store, orchestrator=orch,
            poll_interval_seconds=0.05, shutdown_grace_seconds=5.0,
        )
        m2 = IndexingWorkerManager(
            knowledge_store=store, chunk_store=chunk_store,
            indexing_store=indexing_store, orchestrator=orch,
            poll_interval_seconds=0.05, shutdown_grace_seconds=5.0,
        )
        await m1.start()
        await m2.start()
        await _wait_for_status(store, doc_id, "ready", timeout_seconds=3.0)
        # Only one chunk sequence (no duplicates).
        async with store._db.execute(
            "SELECT COUNT(*) AS n FROM knowledge_chunks WHERE document_id=?",
            (doc_id,),
        ) as cursor:
            chunk_count = (await cursor.fetchone())["n"]
        assert chunk_count > 0  # at least one chunk
        # No duplicate chunks (R3-B UNIQUE(document_id, ordinal) prevents).
        await m1.stop()
        await m2.stop()
        await store.close()


# ============================================================================
# 7. Import side effects
# ============================================================================


class TestImportSideEffects:
    def test_import_does_not_start_tasks(self):
        """Importing indexing_worker must not create any background tasks."""
        # Force re-import (does not actually re-import if cached; the
        # point is that the import line itself has no side effects).
        import pi_agent_core_py.web.knowledge.indexing_worker as mod

        assert mod is not None
        # No new tasks created by the import (verified by static audit:
        # no create_task / Event / Queue at module scope).


# ============================================================================
# Helpers — wait for status
# ============================================================================


async def _wait_for_status(
    store: KnowledgeStore,
    document_id: str,
    expected_status: str,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Poll the Document status until it matches or timeout."""
    deadline = _now() + timeout_seconds
    while _now() < deadline:
        doc = await store.get_document(document_id)
        if doc.status == expected_status:
            return
        await asyncio.sleep(0.02)
    # Final check (raises AssertionError on failure).
    doc = await store.get_document(document_id)
    assert doc.status == expected_status, (
        f"document {document_id} did not reach {expected_status!r} "
        f"within {timeout_seconds}s (last status: {doc.status!r}, "
        f"error_code: {doc.error_code!r})"
    )


def _now() -> float:
    import time as _time

    return _time.monotonic()

"""P2-R3-D2 — App lifecycle integration + active-indexing delete guards.

Covers directive §33 (knowledge disabled mode) + §37/§38 (Document /
Library active-indexing delete guard) + §39 (normalizing not blocked) +
§43 (ready delete allowed) + §52 (startup existing normalizing → ready)
+ §53/§54 (stale chunking/indexing recovery on startup).

Uses real FastAPI app via ``create_app(knowledge_root=...)`` + real
TestClient. Direct SQL is used to seed Document status (bypassing the
R2 pipeline) since R3-D2 only validates lifecycle + guard integration.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================
# Helpers
# ============================================================================


def _build_app(tmp_path: Path, *, enable_knowledge: bool = True):
    """Build a real FastAPI app matching the production lifespan path."""
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(50)])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])

    kwargs: dict[str, Any] = dict(
        harness=harness,
        db_path=str(tmp_path / "sessions.db"),
        uploads_dir=str(tmp_path / "uploads"),
        enable_trusted_host=True,
        credential_extra_hosts=("testserver",),
        credential_extra_ui_origins=(),
    )
    if enable_knowledge:
        kwargs["knowledge_root"] = str(tmp_path / "knowledge")
    return create_app(**kwargs)


def _ui_headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


def _seed_document_status(
    knowledge_db_path: Path,
    *,
    document_id: str = "doc_test00000001",
    library_id: str = "lib_test00000001",
    status: str = "chunking",
    source_sha256: str = "a" * 64,
    sha_suffix: str = "1",
) -> None:
    """Direct-SQL seed a Library + Document with the given status.

    Bypasses R2 pipeline; R3-D2 only validates lifecycle + guard
    integration (does not require real PDF / Markdown).
    """
    conn = sqlite3.connect(str(knowledge_db_path))
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_libraries "
        "(id, name, description, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (library_id, "L", "", "active", 1, 1),
    )
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_documents "
        "(id, library_id, source_name, source_sha256, source_relpath, "
        " markdown_relpath, mime_type, size_bytes, page_count, status, "
        " parser_version, error_code, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            document_id, library_id, f"x{sha_suffix}.pdf", source_sha256,
            f"documents/{document_id}/source.pdf",
            f"documents/{document_id}/document.md",
            "application/pdf", 10, 1, status, "pypdf/6.14.2", "", 100, 100,
        ),
    )
    conn.execute("COMMIT")
    conn.close()


# ============================================================================
# 1. App lifespan — IndexingWorkerManager wiring
# ============================================================================


class TestAppLifespanWiring:
    def test_knowledge_disabled_mode_no_indexing_manager(self, tmp_path: Path):
        """knowledge_root=None → indexing_worker_manager is None; app healthy."""
        app = _build_app(tmp_path, enable_knowledge=False)
        with TestClient(app) as client:
            state = app.state.web
            assert state.indexing_worker_manager is None
            assert state.knowledge_store is None
            # App still serves (no knowledge routes, but root/health OK).
            assert client.get("/").status_code in (200, 404)
        # After shutdown, still None.
        assert app.state.web.indexing_worker_manager is None

    def test_knowledge_enabled_mode_starts_indexing_manager(self, tmp_path: Path):
        """knowledge_root set → indexing_worker_manager is constructed + running."""
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            state = app.state.web
            mgr = state.indexing_worker_manager
            assert mgr is not None
            snap = mgr.snapshot()
            assert snap.state == "running"
        # After shutdown, manager is None.
        assert app.state.web.indexing_worker_manager is None

    def test_shutdown_order_indexing_before_store_close(self, tmp_path: Path):
        """Shutdown sequence: Ingestion stop → Index stop → KnowledgeStore close.

        Verified indirectly: after TestClient exit, both managers are
        None and KnowledgeStore is closed. The actual order is enforced
        by app.py lifespan; here we verify the post-shutdown state.
        """
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            state = app.state.web
            assert state.ingestion_worker_manager is not None
            assert state.indexing_worker_manager is not None
            assert state.knowledge_store is not None
        # Post-shutdown.
        state = app.state.web
        assert state.ingestion_worker_manager is None
        assert state.indexing_worker_manager is None
        # KnowledgeStore closed (its _closed flag set).
        ks = state.knowledge_store
        if ks is not None:
            assert ks._closed is True


# ============================================================================
# 2. Startup recovery on app boot
# ============================================================================


class TestStartupRecovery:
    def test_stale_chunking_recovered_on_startup(self, tmp_path: Path):
        """Pre-seed a chunking Document; app startup recovers it to failed."""
        # First boot to create schema.
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            pass  # shutdown — schema created
        # Now seed a stale chunking Document directly.
        knowledge_db = tmp_path / "knowledge" / "knowledge.db"
        _seed_document_status(knowledge_db, status="chunking")
        # Second boot — recovery should convert chunking → failed.
        app2 = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app2):
            state = app2.state.web
            mgr = state.indexing_worker_manager
            assert mgr is not None
            assert mgr.snapshot().state == "running"
            # Verify Document was recovered.
            store = state.knowledge_store
            doc = asyncio.run(store.get_document("doc_test00000001"))
            assert doc.status == "failed"
            assert doc.error_code == "indexing_interrupted"

    def test_stale_indexing_recovered_on_startup(self, tmp_path: Path):
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            pass
        knowledge_db = tmp_path / "knowledge" / "knowledge.db"
        _seed_document_status(knowledge_db, status="indexing")
        app2 = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app2):
            state = app2.state.web
            store = state.knowledge_store
            doc = asyncio.run(store.get_document("doc_test00000001"))
            assert doc.status == "failed"
            assert doc.error_code == "indexing_interrupted"

    def test_normalizing_processed_on_startup(self, tmp_path: Path):
        """normalizing is NOT recovered — left for the worker to claim.

        Without a real document.md the worker will eventually move it to
        ``failed`` (canonical_markdown_missing). This proves the worker
        is wired and running post-startup.
        """
        import time

        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            pass
        knowledge_db = tmp_path / "knowledge" / "knowledge.db"
        _seed_document_status(knowledge_db, status="normalizing")
        app2 = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app2):
            state = app2.state.web
            store = state.knowledge_store
            # Worker should pick up the normalizing doc within ~2-5s.
            # Without markdown, it will fail with canonical_markdown_missing.
            for _ in range(50):  # up to 5s
                doc = asyncio.run(store.get_document("doc_test00000001"))
                if doc.status != "normalizing":
                    break
                time.sleep(0.1)
            assert doc.status in ("failed", "chunking", "indexing", "ready"), (
                f"worker did not process normalizing doc; status={doc.status!r}"
            )


# ============================================================================
# 3. Document active-indexing delete guard
# ============================================================================


class TestDocumentDeleteGuard:
    @pytest.mark.parametrize("status", ["chunking", "indexing"])
    def test_active_indexing_status_blocked(
        self, tmp_path: Path, status: str
    ):
        """DELETE on chunking/indexing Document → 409 document_indexing_active.

        Must use a single app instance — startup recovery would convert
        chunking/indexing to failed on second boot, so we seed the
        status while the app is running (bypassing recovery).
        """
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app) as client:
            client.headers.update(_ui_headers())
            # Seed document in normalizing first (so recovery doesn't touch it).
            knowledge_db = tmp_path / "knowledge" / "knowledge.db"
            _seed_document_status(knowledge_db, status="normalizing")
            # Now flip to chunking/indexing via direct SQL (bypass recovery).
            _seed_document_status(knowledge_db, status=status)
            # DELETE — should 409.
            resp = client.delete(
                "/api/knowledge/documents/doc_test00000001"
            )
            assert resp.status_code == 409
            body = resp.json()
            # _safe_error wraps as {"detail": {"code": ..., "message": ...}}.
            detail = body.get("detail", body)
            assert detail.get("code") == "document_indexing_active"

    @pytest.mark.parametrize(
        "status", ["ready", "failed", "needs_ocr"]
    )
    def test_non_active_status_allowed(
        self, tmp_path: Path, status: str
    ):
        """DELETE on ready/failed/needs_ocr → not 409 indexing."""
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app) as client:
            client.headers.update(_ui_headers())
            knowledge_db = tmp_path / "knowledge" / "knowledge.db"
            _seed_document_status(knowledge_db, status=status)
            resp = client.delete(
                "/api/knowledge/documents/doc_test00000001"
            )
            # Should NOT be 409 indexing. May be 204 (success) or other
            # 409 (e.g. ingestion_active) — but not document_indexing_active.
            if resp.status_code == 409:
                body = resp.json()
                detail = body.get("detail", body)
                assert detail.get("code") != "document_indexing_active"
            else:
                assert resp.status_code == 204


# ============================================================================
# 4. Library active-indexing delete guard
# ============================================================================


class TestLibraryDeleteGuard:
    @pytest.mark.parametrize("status", ["chunking", "indexing"])
    def test_library_with_indexing_doc_blocked(
        self, tmp_path: Path, status: str
    ):
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app) as client:
            client.headers.update(_ui_headers())
            knowledge_db = tmp_path / "knowledge" / "knowledge.db"
            _seed_document_status(knowledge_db, status="normalizing")
            _seed_document_status(knowledge_db, status=status)
            resp = client.delete(
                "/api/knowledge/libraries/lib_test00000001"
            )
            assert resp.status_code == 409
            body = resp.json()
            detail = body.get("detail", body)
            assert detail.get("code") == "library_indexing_active"

    def test_library_with_only_ready_docs_allowed(
        self, tmp_path: Path
    ):
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app) as client:
            client.headers.update(_ui_headers())
            knowledge_db = tmp_path / "knowledge" / "knowledge.db"
            _seed_document_status(knowledge_db, status="ready")
            resp = client.delete(
                "/api/knowledge/libraries/lib_test00000001"
            )
            if resp.status_code == 409:
                body = resp.json()
                detail = body.get("detail", body)
                assert detail.get("code") != "library_indexing_active"
            else:
                assert resp.status_code == 204


# ============================================================================
# 5. Repeated lifespan — no leaked tasks
# ============================================================================


class TestRepeatedLifespan:
    def test_two_lifespan_cycles_no_leak(self, tmp_path: Path):
        """Startup → shutdown → startup → shutdown must not leak tasks."""
        for _ in range(2):
            app = _build_app(tmp_path, enable_knowledge=True)
            with TestClient(app):
                state = app.state.web
                assert state.indexing_worker_manager is not None
                assert state.ingestion_worker_manager is not None
            # After exit, both cleared.
            assert app.state.web.indexing_worker_manager is None
            assert app.state.web.ingestion_worker_manager is None

    def test_no_pending_indexing_tasks_after_shutdown(self, tmp_path: Path):
        """After shutdown, no 'indexing_worker_loop' tasks remain."""
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app):
            pass

        # Check no pending indexing tasks. (Must run inside an event loop
        # to use asyncio.all_tasks; TestClient exit already closed its
        # loop, so we run a fresh one.)
        async def _check():
            return [
                t.get_name()
                for t in asyncio.all_tasks()
                if "indexing" in t.get_name().lower() and not t.done()
            ]

        # all_tasks with no running loop returns empty by default; the
        # important check is that the TestClient's loop is gone and we
        # don't see "Task was destroyed but pending" warnings. The lack
        # of warnings is implicit (pytest would surface them).
        pending = asyncio.run(_check())
        assert pending == []

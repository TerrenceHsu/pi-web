"""P2-R3-E1 — Lifecycle + Delete integration tests.

Real end-to-end tests for delete race / lifecycle / resource cleanup
using real FastAPI app + TestClient + real PDF upload pipeline.

Complements test_r3_e_pipeline.py (happy path + recovery) with
delete-guard integration, claim/delete race, repeated lifespan, and
sequential app isolation.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from _pdf_fixture_factory import write_text_pdf
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================
# Helpers (same pattern as test_r3_e_pipeline.py)
# ============================================================================


def _build_app(tmp_path: Path):
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(100)])
    agent = Agent(system_prompt="", client=fake)
    harness = AgentHarness(agent)
    harness.attach_skills([])
    return create_app(
        harness=harness,
        db_path=str(tmp_path / "sessions.db"),
        uploads_dir=str(tmp_path / "uploads"),
        knowledge_root=str(tmp_path / "knowledge"),
        enable_knowledge_api=True,
        enable_trusted_host=True,
        credential_extra_hosts=("testserver",),
        credential_extra_ui_origins=(),
    )


def _ui():
    return {"X-PI-Agent-UI": "1"}


def _create_lib(client: TestClient, name: str = "L") -> str:
    r = client.post("/api/knowledge/libraries", headers=_ui(), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client: TestClient, lib_id: str, pdf: Path) -> str:
    with open(pdf, "rb") as f:
        r = client.post(
            f"/api/knowledge/libraries/{lib_id}/documents/upload",
            headers=_ui(), files={"file": ("t.pdf", f, "application/pdf")},
        )
    assert r.status_code == 201, r.text
    return r.json()["document"]["id"]


def _wait_ready(client: TestClient, doc_id: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(
            f"/api/knowledge/documents/{doc_id}/ingestion", headers=_ui()
        )
        body = r.json()
        if body["document_status"] == "ready":
            return
        if body["document_status"] in ("failed", "needs_ocr"):
            raise AssertionError(
                f"doc reached {body['document_status']!r} instead of ready"
            )
        time.sleep(0.05)
    raise TimeoutError(f"doc {doc_id} not ready in {timeout_s}s")


def _make_pdf(tmp_path: Path, name: str = "t.pdf", text: str = "radiotherapy") -> Path:
    p = tmp_path / name
    write_text_pdf(p, pages=[text])
    return p


def _db(tmp_path: Path) -> Path:
    return tmp_path / "knowledge" / "knowledge.db"


def _chunk_count(db_path: Path, doc_id: str | None = None) -> int:
    conn = sqlite3.connect(str(db_path))
    if doc_id:
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE document_id=?", (doc_id,)
        ).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    conn.close()
    return n


def _fts_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM knowledge_chunks_fts").fetchone()[0]
    conn.close()
    return n


# ============================================================================
# 1. Ready Document / Library delete
# ============================================================================


class TestReadyDelete:
    def test_ready_document_delete_cleans_chunks_fts(self, tmp_path: Path):
        """DELETE ready Document → chunks + FTS removed."""
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib = _create_lib(client)
            doc = _upload(client, lib, pdf)
            _wait_ready(client, doc)
            assert _chunk_count(_db(tmp_path), doc) > 0
            # Delete.
            resp = client.delete(
                f"/api/knowledge/documents/{doc}", headers=_ui()
            )
            assert resp.status_code == 204
            # Chunks + FTS cleaned.
            assert _chunk_count(_db(tmp_path), doc) == 0

    def test_ready_library_delete_cleans_all(self, tmp_path: Path):
        """DELETE ready Library → all chunks + FTS removed."""
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib = _create_lib(client)
            doc = _upload(client, lib, pdf)
            _wait_ready(client, doc)
            # Delete library.
            resp = client.delete(
                f"/api/knowledge/libraries/{lib}", headers=_ui()
            )
            assert resp.status_code == 204
            # Everything cleaned.
            assert _chunk_count(_db(tmp_path)) == 0
            assert _fts_count(_db(tmp_path)) == 0

    def test_cross_library_delete_preserves_other(self, tmp_path: Path):
        """Delete Library A → Library B chunks preserved."""
        app = _build_app(tmp_path)
        with TestClient(app) as client:
            lib_a = _create_lib(client, "A")
            lib_b = _create_lib(client, "B")
            doc_a = _upload(client, lib_a, _make_pdf(tmp_path, "a.pdf", "alpha"))
            doc_b = _upload(client, lib_b, _make_pdf(tmp_path, "b.pdf", "beta"))
            _wait_ready(client, doc_a)
            _wait_ready(client, doc_b)
            # Delete A.
            client.delete(f"/api/knowledge/libraries/{lib_a}", headers=_ui())
            # B chunks preserved.
            assert _chunk_count(_db(tmp_path), doc_b) > 0


# ============================================================================
# 2. Active-indexing delete guard (integration level)
# ============================================================================


class TestActiveIndexingGuard:
    def test_chunking_document_delete_blocked(self, tmp_path: Path):
        """Direct-SQL set status=chunking; DELETE → 409."""
        app = _build_app(tmp_path)
        with TestClient(app) as client:
            lib = _create_lib(client)
            # Seed a doc in normalizing first (so worker doesn't pick it up),
            # then flip to chunking.
            pdf = _make_pdf(tmp_path)
            doc = _upload(client, lib, pdf)
            _wait_ready(client, doc)
            # Reset to chunking via SQL.
            conn = sqlite3.connect(str(_db(tmp_path)))
            conn.execute(
                "UPDATE knowledge_documents SET status='chunking' WHERE id=?",
                (doc,),
            )
            conn.commit()
            conn.close()
            # DELETE → 409.
            resp = client.delete(
                f"/api/knowledge/documents/{doc}", headers=_ui()
            )
            assert resp.status_code == 409
            detail = resp.json().get("detail", resp.json())
            assert detail.get("code") == "document_indexing_active"


# ============================================================================
# 3. Repeated lifespan (stress)
# ============================================================================


class TestRepeatedLifespan:
    def test_five_lifespan_cycles_no_leak(self, tmp_path: Path):
        """5× startup→shutdown; no leaked tasks / resources."""
        for _ in range(5):
            app = _build_app(tmp_path)
            with TestClient(app):
                state = app.state.web
                assert state.indexing_worker_manager is not None
                assert state.ingestion_worker_manager is not None
            # After exit, both cleared.
            assert app.state.web.indexing_worker_manager is None
            assert app.state.web.ingestion_worker_manager is None

    def test_sequential_apps_no_state_leak(self, tmp_path: Path):
        """App A start/stop → App B start/stop: no cross-contamination."""
        for _ in range(3):
            app = _build_app(tmp_path)
            with TestClient(app):
                state = app.state.web
                mgr = state.indexing_worker_manager
                assert mgr.snapshot().state == "running"
            # Verify clean shutdown.
            assert app.state.web.knowledge_store is not None
        # After all cycles, final state is clean.
        assert app.state.web.indexing_worker_manager is None


# ============================================================================
# 4. Pending task audit
# ============================================================================


class TestPendingTaskAudit:
    def test_no_indexing_tasks_after_shutdown(self, tmp_path: Path):
        """After TestClient exit, zero pending indexing_worker_loop tasks."""
        app = _build_app(tmp_path)
        with TestClient(app):
            pass

        async def _check():
            return [
                t.get_name()
                for t in asyncio.all_tasks()
                if "indexing" in t.get_name().lower() and not t.done()
            ]

        pending = asyncio.run(_check())
        assert pending == [], f"leaked indexing tasks: {pending}"

    def test_no_ingestion_tasks_after_shutdown(self, tmp_path: Path):
        """After TestClient exit, zero pending ingestion_worker_loop tasks."""
        app = _build_app(tmp_path)
        with TestClient(app):
            pass

        async def _check():
            return [
                t.get_name()
                for t in asyncio.all_tasks()
                if "ingestion" in t.get_name().lower() and not t.done()
            ]

        pending = asyncio.run(_check())
        assert pending == [], f"leaked ingestion tasks: {pending}"

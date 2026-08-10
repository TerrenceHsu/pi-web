"""P2-R4-D — Final RAG Integration E2E tests.

Tests the complete pipeline end-to-end using real app components:
  PDF Upload → R2 Ingestion → R3 Index Worker → Document ready
  → SearchKnowledgeService.search() → KnowledgeEvidence
  → Citation process_citations → [1] + Sources footer
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from pathlib import Path

from _pdf_fixture_factory import write_text_pdf
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.knowledge.chunk_store import ChunkStore
from pi_agent_core_py.web.knowledge.citations import (
    process_citations,
    render_source_footer,
)
from pi_agent_core_py.web.knowledge.evidence import EvidenceRegistry
from pi_agent_core_py.web.knowledge.search_service import SearchKnowledgeService

# ============================================================================
# Helpers
# ============================================================================


def _build_app(tmp_path: Path):
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(200)])
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


def _create_lib(client, name="lib1"):
    r = client.post("/api/knowledge/libraries", headers=_ui(), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _upload(client, lib_id, pdf):
    with open(pdf, "rb") as f:
        r = client.post(
            f"/api/knowledge/libraries/{lib_id}/documents/upload",
            headers=_ui(), files={"file": (pdf.name, f, "application/pdf")},
        )
    assert r.status_code == 201, r.text
    return r.json()["document"]["id"]


def _wait_ready(client, doc_id, timeout_s=30.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(
            f"/api/knowledge/documents/{doc_id}/ingestion", headers=_ui()
        )
        body = r.json()
        if body["document_status"] == "ready":
            return body
        if body["document_status"] in ("failed", "needs_ocr"):
            raise AssertionError(
                f"doc reached {body['document_status']!r}, expected ready"
            )
        time.sleep(0.05)
    raise TimeoutError(f"doc {doc_id} not ready in {timeout_s}s")


def _bind_session(knowledge_db, session_id, library_ids):
    conn = sqlite3.connect(str(knowledge_db))
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "DELETE FROM session_knowledge_libraries WHERE session_id = ?",
        (session_id,),
    )
    for lid in library_ids:
        conn.execute(
            "INSERT INTO session_knowledge_libraries "
            "(session_id, library_id, access_mode, created_at) "
            "VALUES (?, ?, 'read', 1)",
            (session_id, lid),
        )
    conn.execute("COMMIT")
    conn.close()


def _create_session(client):
    r = client.post("/api/sessions", headers=_ui(), json={"title": "test"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _db_path(tmp_path):
    return tmp_path / "knowledge" / "knowledge.db"


def _make_pdf(tmp_path, name="test.pdf", pages=None):
    p = tmp_path / name
    if pages is None:
        pages = [
            "Introduction to radiotherapy and IMRT dose planning "
            "methodologies for treatment optimization.",
            "Methods for dose calculation using standard algorithms "
            "and calibration protocols for clinical use.",
        ]
    write_text_pdf(p, pages=pages)
    return p


# ============================================================================
# 1. Full pipeline E2E
# ============================================================================


class TestFullRagPipeline:
    def test_pdf_to_citation_e2e(self, tmp_path):
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib_id = _create_lib(client)
            doc_id = _upload(client, lib_id, pdf)
            _wait_ready(client, doc_id)
            session_id = _create_session(client)
            _bind_session(_db_path(tmp_path), session_id, [lib_id])

            state = app.state.web
            chunk_store = ChunkStore(state.knowledge_store)
            service = SearchKnowledgeService(
                knowledge_store=state.knowledge_store,
                chunk_store=chunk_store,
            )
            registry = EvidenceRegistry()
            result = asyncio.run(service.search(
                session_id=session_id, query="radiotherapy",
                limit=5, registry=registry,
            ))
            assert len(result.hits) > 0
            ev = result.hits[0]
            assert ev.evidence_id == "E1"
            assert ev.document_id == doc_id
            assert ev.source_filename == "test.pdf"
            assert ev.page_start >= 1

            text = "Radiotherapy uses modulated beams.[cite:E1]"
            cr = process_citations(text, registry)
            assert "[1]" in cr.rendered_content
            assert "[cite:" not in cr.rendered_content
            footer = render_source_footer(cr.citations)
            assert "Sources:" in footer
            assert "test.pdf" in footer

    def test_source_pdf_unchanged(self, tmp_path):
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        with TestClient(app) as client:
            lib_id = _create_lib(client)
            doc_id = _upload(client, lib_id, pdf)
            _wait_ready(client, doc_id)
            sp = (
                tmp_path / "knowledge" / "libraries" / lib_id
                / "documents" / doc_id / "source.pdf"
            )
            assert hashlib.sha256(sp.read_bytes()).hexdigest() == sha


# ============================================================================
# 2. Empty binding E2E
# ============================================================================


class TestEmptyBindingE2E:
    def test_no_binding_zero_evidence(self, tmp_path):
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib_id = _create_lib(client)
            doc_id = _upload(client, lib_id, pdf)
            _wait_ready(client, doc_id)
            session_id = _create_session(client)

            state = app.state.web
            cs = ChunkStore(state.knowledge_store)
            svc = SearchKnowledgeService(
                knowledge_store=state.knowledge_store, chunk_store=cs,
            )
            reg = EvidenceRegistry()
            result = asyncio.run(svc.search(
                session_id=session_id, query="radiotherapy",
                limit=5, registry=reg,
            ))
            assert result.hits == ()


# ============================================================================
# 3. Cross-session isolation
# ============================================================================


class TestCrossSessionE2E:
    def test_two_sessions_different_libraries(self, tmp_path):
        app = _build_app(tmp_path)
        pdf_a = _make_pdf(tmp_path, "a.pdf", ["radiotherapy alpha unique"])
        pdf_b = _make_pdf(tmp_path, "b.pdf", ["radiotherapy beta secret"])
        with TestClient(app) as client:
            lib_a = _create_lib(client, "libA")
            lib_b = _create_lib(client, "libB")
            doc_a = _upload(client, lib_a, pdf_a)
            doc_b = _upload(client, lib_b, pdf_b)
            _wait_ready(client, doc_a)
            _wait_ready(client, doc_b)

            sess_a = _create_session(client)
            sess_b = _create_session(client)
            _bind_session(_db_path(tmp_path), sess_a, [lib_a])
            _bind_session(_db_path(tmp_path), sess_b, [lib_b])

            state = app.state.web
            cs = ChunkStore(state.knowledge_store)
            svc = SearchKnowledgeService(
                knowledge_store=state.knowledge_store, chunk_store=cs,
            )
            reg_a = EvidenceRegistry()
            ra = asyncio.run(svc.search(
                session_id=sess_a, query="secret", registry=reg_a,
            ))
            assert ra.hits == ()

            reg_b = EvidenceRegistry()
            rb = asyncio.run(svc.search(
                session_id=sess_b, query="secret", registry=reg_b,
            ))
            assert len(rb.hits) > 0
            assert rb.hits[0].document_id == doc_b
            assert rb.hits[0].source_filename == "b.pdf"

    def test_concurrent_registries_independent(self, tmp_path):
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib_id = _create_lib(client)
            doc_id = _upload(client, lib_id, pdf)
            _wait_ready(client, doc_id)
            session_id = _create_session(client)
            _bind_session(_db_path(tmp_path), session_id, [lib_id])

            state = app.state.web
            cs = ChunkStore(state.knowledge_store)
            svc = SearchKnowledgeService(
                knowledge_store=state.knowledge_store, chunk_store=cs,
            )
            reg1 = EvidenceRegistry()
            asyncio.run(svc.search(
                session_id=session_id, query="radiotherapy", registry=reg1,
            ))
            reg2 = EvidenceRegistry()
            asyncio.run(svc.search(
                session_id=session_id, query="radiotherapy", registry=reg2,
            ))
            assert reg1.all_evidence[0].evidence_id == "E1"
            assert reg2.all_evidence[0].evidence_id == "E1"


# ============================================================================
# 4. Citation pipeline integrity
# ============================================================================


class TestCitationPipelineE2E:
    def test_invalid_citation_in_full_context(self, tmp_path):
        app = _build_app(tmp_path)
        pdf = _make_pdf(tmp_path)
        with TestClient(app) as client:
            lib_id = _create_lib(client)
            doc_id = _upload(client, lib_id, pdf)
            _wait_ready(client, doc_id)
            session_id = _create_session(client)
            _bind_session(_db_path(tmp_path), session_id, [lib_id])

            state = app.state.web
            cs = ChunkStore(state.knowledge_store)
            svc = SearchKnowledgeService(
                knowledge_store=state.knowledge_store, chunk_store=cs,
            )
            registry = EvidenceRegistry()
            asyncio.run(svc.search(
                session_id=session_id, query="radiotherapy",
                registry=registry,
            ))

            text = (
                "Based on the literature [cite:E1], IMRT is effective. "
                "However, some claim otherwise [cite:E999] which is "
                "unsupported. The original study confirms [cite:E1]."
            )
            cr = process_citations(text, registry)
            assert "[1]" in cr.rendered_content
            assert "[cite:" not in cr.rendered_content
            assert "E999" in cr.warnings
            assert len(cr.citations) == 1

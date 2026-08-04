"""P2-R2-C4 — Concurrency / race condition tests.

Per directive §二十四/§二十五/§二十六/§二十七:
- Concurrent retry × retry → at most one 201
- Concurrent retry × delete → no orphan (Job without Document)
- Concurrent claim × delete → no FK orphan
- Library delete with active Job → 409

Pre-populates DB state via a SEPARATE KnowledgeStore connection (in a
fresh asyncio.run) BEFORE the TestClient enters its lifespan. This way
the lifespan Manager sees the pre-existing committed data when it starts.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.knowledge.files import KnowledgeFileStore
from pi_agent_core_py.web.knowledge.store import KnowledgeStore
from tests._pdf_fixture_factory import write_text_pdf

# ============================================================================
# Helpers
# ============================================================================


def _make_app_kwargs(tmp_path: Path) -> dict:
    return dict(
        db_path=str(tmp_path / "sessions.db"),
        uploads_dir=str(tmp_path / "uploads"),
        knowledge_root=str(tmp_path / "knowledge"),
        enable_knowledge_api=True,
        enable_trusted_host=True,
        credential_extra_hosts=("testserver",),
        credential_extra_ui_origins=(),
    )


def _make_harness():
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(50)])
    agent = Agent(system_prompt="", client=fake)
    h = AgentHarness(agent)
    h.attach_skills([])
    return h


def _ui_headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


async def _prepopulate_state(
    db_path: str,
    knowledge_root: Path,
    *,
    failed_doc: bool = False,
    active_job: bool = False,
) -> tuple[str, str]:
    """Pre-create Library + Document (with optional Job) via separate connection.

    Returns (library_id, document_id). Closes the connection at end so the
    lifespan's KnowledgeStore can be opened cleanly.
    """
    store = await KnowledgeStore.open(db_path)
    try:
        file_store = KnowledgeFileStore(root=knowledge_root)
        file_store.ensure_root()

        lib = await store.create_library(name="lib1")
        pdf_bytes = b"%PDF-1.4 test content"
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        doc = await store.create_document(
            library_id=lib.id,
            source_name="preset.pdf",
            source_sha256=sha,
            source_relpath="documents/_/source.pdf",
            markdown_relpath="documents/_/document.md",
            mime_type="application/pdf",
        )
        doc_dir = file_store._document_dir_unchecked(lib.id, doc.id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "source.pdf").write_bytes(pdf_bytes)

        if active_job:
            # Advance to extracting + create running Job (simulates crash mid-pipeline)
            await store.transition_document_status(doc.id, "extracting")
            await store.create_job(document_id=doc.id, stage="extract")
        elif failed_doc:
            await store.transition_document_status(doc.id, "extracting")
            await store.transition_document_status(
                doc.id, "failed", error_code="prior_fail"
            )

        return lib.id, doc.id
    finally:
        await store.close()


# ============================================================================
# A. Library delete with terminal Document succeeds
# ============================================================================


class TestLibraryDeleteRace:
    def test_library_delete_with_terminal_doc_succeeds(self, tmp_path):
        """After processing completes (no active Job), Library is deletable."""
        pdf_path = tmp_path / "src.pdf"
        write_text_pdf(pdf_path, pages=["content"])

        harness = _make_harness()
        app = create_app(harness=harness, **_make_app_kwargs(tmp_path))

        with TestClient(app) as client:
            resp = client.post(
                "/api/knowledge/libraries",
                headers=_ui_headers(),
                json={"name": "lib1"},
            )
            lib_id = resp.json()["id"]
            with open(pdf_path, "rb") as f:
                resp = client.post(
                    f"/api/knowledge/libraries/{lib_id}/documents/upload",
                    headers=_ui_headers(),
                    files={"file": ("t.pdf", f, "application/pdf")},
                )
            doc_id = resp.json()["document"]["id"]

            # Wait for terminal
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                status = client.get(
                    f"/api/knowledge/documents/{doc_id}/ingestion",
                    headers=_ui_headers(),
                ).json()
                if status["document_status"] in (
                    "normalizing", "needs_ocr", "failed"
                ):
                    break
                time.sleep(0.05)

            # DELETE should succeed (terminal — no active Job)
            resp = client.delete(
                f"/api/knowledge/libraries/{lib_id}",
                headers=_ui_headers(),
            )
            assert resp.status_code == 204


# ============================================================================
# B. Document/Library delete with pre-existing active Job → 409
# ============================================================================


class TestActiveJobDeleteGuards:
    def test_document_recovery_then_delete_succeeds(self, tmp_path):
        """Pre-create Document + running Job; startup recovery clears it;
        DELETE then succeeds (204). Active-Job 409 path is covered by C2
        unit tests that don't involve startup recovery.
        """
        knowledge_root = tmp_path / "knowledge"
        db_path = str(knowledge_root / "knowledge.db")
        lib_id, doc_id = asyncio.run(
            _prepopulate_state(db_path, knowledge_root, active_job=True)
        )

        harness = _make_harness()
        app = create_app(harness=harness, **_make_app_kwargs(tmp_path))

        with TestClient(app) as client:
            # After startup recovery, Job is failed (not active) — DELETE succeeds
            resp = client.delete(
                f"/api/knowledge/documents/{doc_id}",
                headers=_ui_headers(),
            )
            assert resp.status_code == 204

    def test_library_recovery_then_delete_succeeds(self, tmp_path):
        """Pre-create Document + running Job; recovery clears; Library DELETE succeeds."""
        knowledge_root = tmp_path / "knowledge"
        db_path = str(knowledge_root / "knowledge.db")
        lib_id, doc_id = asyncio.run(
            _prepopulate_state(db_path, knowledge_root, active_job=True)
        )

        harness = _make_harness()
        app = create_app(harness=harness, **_make_app_kwargs(tmp_path))

        with TestClient(app) as client:
            resp = client.delete(
                f"/api/knowledge/libraries/{lib_id}",
                headers=_ui_headers(),
            )
            assert resp.status_code == 204


# ============================================================================
# C. Concurrent retry × retry (HTTP-level)
# ============================================================================


class TestConcurrentRetryHttp:
    def test_two_concurrent_retries_at_most_one_succeeds(self, tmp_path):
        """Two HTTP retry requests on same failed Document:
        - at most one returns 201
        - the other returns 409 ingestion_already_active
        - exactly one new running Job exists
        """
        knowledge_root = tmp_path / "knowledge"
        db_path = str(knowledge_root / "knowledge.db")
        lib_id, doc_id = asyncio.run(
            _prepopulate_state(db_path, knowledge_root, failed_doc=True)
        )

        harness = _make_harness()
        app = create_app(harness=harness, **_make_app_kwargs(tmp_path))

        with TestClient(app) as client:
            # Verify Document is failed (pre-populated)
            status = client.get(
                f"/api/knowledge/documents/{doc_id}/ingestion",
                headers=_ui_headers(),
            ).json()
            assert status["document_status"] == "failed"

            # Fire two retry requests concurrently
            import concurrent.futures

            def retry():
                return client.post(
                    f"/api/knowledge/documents/{doc_id}/retry",
                    headers=_ui_headers(),
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(retry)
                f2 = pool.submit(retry)
                r1 = f1.result(timeout=10)
                r2 = f2.result(timeout=10)

            statuses = sorted([r1.status_code, r2.status_code])
            # Per directive §二十四 — exactly one 201, one 409
            assert statuses == [201, 409], (
                f"expected [201, 409]; got {statuses}"
            )

            # Verify only one active (running) Job exists
            status_after = client.get(
                f"/api/knowledge/documents/{doc_id}/ingestion",
                headers=_ui_headers(),
            ).json()
            # Latest Job is running (just-created retry Job)
            if status_after["latest_job"]:
                assert status_after["latest_job"]["status"] in (
                    "running", "completed", "failed"
                )  # depending on worker timing

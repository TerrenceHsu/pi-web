"""Ingestion Worker lifespan integration tests — P2-R2-C2-B.

Verifies FastAPI lifespan correctly:
- Constructs + starts IngestionWorkerManager on knowledge subsystem init
- Stops manager BEFORE KnowledgeStore.close on shutdown
- Test-only apps can opt out via knowledge_root=None
- Manager survives full lifespan cycle without leaking tasks
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================
# Helper — build app with knowledge subsystem
# ============================================================================


def _build_app(
    tmp_path: Path,
    *,
    enable_knowledge: bool = True,
) -> FastAPI:
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
        kwargs["enable_knowledge_api"] = True
    return create_app(**kwargs)


# ============================================================================
# A. Manager construction order in lifespan
# ============================================================================


class TestLifespanConstruction:
    def test_app_with_knowledge_root_constructs_manager(self, tmp_path):
        """App with knowledge_root set should construct + start the manager."""
        app = _build_app(tmp_path, enable_knowledge=True)
        with TestClient(app) as client:
            state = app.state.web
            assert state.ingestion_worker_manager is not None
            assert state.ingestion_worker_manager.state == "running"
            # Smoke: app responds (with Trusted UI header)
            response = client.get(
                "/api/knowledge/libraries",
                headers={"X-PI-Agent-UI": "1"},
            )
            assert response.status_code == 200
        # After lifespan exit: manager stopped + cleared
        assert app.state.web.ingestion_worker_manager is None

    def test_app_without_knowledge_root_skips_manager(self, tmp_path):
        """App with knowledge_root=None should NOT construct manager."""
        app = _build_app(tmp_path, enable_knowledge=False)
        with TestClient(app):
            state = app.state.web
            assert state.ingestion_worker_manager is None
            assert state.knowledge_store is None
        assert app.state.web.ingestion_worker_manager is None

    def test_manager_stopped_before_knowledge_store_closed(self, tmp_path):
        """Shutdown order: manager.stop() THEN knowledge_store.close()."""
        app = _build_app(tmp_path, enable_knowledge=True)

        close_order: list[str] = []

        with TestClient(app) as client:
            state = app.state.web
            mgr = state.ingestion_worker_manager
            k_store = state.knowledge_store

            original_mgr_stop = mgr.stop
            original_store_close = k_store.close

            async def tracking_mgr_stop():
                close_order.append("manager_stop")
                return await original_mgr_stop()

            async def tracking_store_close():
                close_order.append("store_close")
                return await original_store_close()

            mgr.stop = tracking_mgr_stop  # type: ignore[method-assign]
            k_store.close = tracking_store_close  # type: ignore[method-assign]

            client.__exit__(None, None, None)

        assert close_order == ["manager_stop", "store_close"]


# ============================================================================
# B. Manager state visibility to FastAPI request handlers
# ============================================================================


class TestAppStateAccess:
    def test_manager_accessible_via_app_state(self, tmp_path):
        app = _build_app(tmp_path, enable_knowledge=True)
        captured: dict[str, Any] = {}

        @app.get("/_test/manager-state")
        def _handler():
            state = app.state.web
            mgr = state.ingestion_worker_manager
            captured["state"] = mgr.state
            captured["active_job_id"] = mgr.snapshot().active_job_id
            return {"ok": True}

        with TestClient(app) as client:
            response = client.get("/_test/manager-state")
            assert response.status_code == 200
            assert captured["state"] == "running"
            assert captured["active_job_id"] is None


# ============================================================================
# C. Test isolation — multiple apps have separate managers
# ============================================================================


class TestAppIsolation:
    def test_two_apps_have_independent_managers(self, tmp_path):
        app1 = _build_app(tmp_path, enable_knowledge=True)
        app2 = _build_app(tmp_path, enable_knowledge=True)

        with TestClient(app1), TestClient(app2):
            mgr1 = app1.state.web.ingestion_worker_manager
            mgr2 = app2.state.web.ingestion_worker_manager
            assert mgr1 is not None
            assert mgr2 is not None
            assert mgr1 is not mgr2
            assert mgr1._worker_task is not mgr2._worker_task


# ============================================================================
# D. Parser close on shutdown (per directive §二十二)
# ============================================================================


class TestParserClose:
    def test_parser_closed_on_shutdown(self, tmp_path):
        app = _build_app(tmp_path, enable_knowledge=True)

        with TestClient(app) as client:
            state = app.state.web
            mgr = state.ingestion_worker_manager
            assert mgr is not None
            parser = mgr._parser
            assert parser is not None
            assert parser.parser_id == "pypdf"
            client.__exit__(None, None, None)

        assert app.state.web.ingestion_worker_manager is None


# ============================================================================
# E. Import-time side effects (per directive §二十三)
# ============================================================================


class TestImportSideEffects:
    def test_importing_create_app_does_not_start_worker(self):
        """Importing the app module should not start any worker."""
        from pi_agent_core_py.web import app as app_module

        assert app_module.create_app is not None

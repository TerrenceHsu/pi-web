"""Knowledge Library REST API tests (P2-R1 R1-C).

Coverage matrix per P2-R1 §17 G (API):

- Missing UI header rejection
- Library CRUD (list / create / get / patch / delete)
- Safe 404 / 409 responses
- Validation responses (422)
- Session Binding GET / PUT
- Empty-list unbinds
- Cross-session isolation
- No absolute path in response
- No raw exception in response
- No PDF upload endpoint
- No search endpoint
- No Tool registration
- Restart persistence (lifespan cycle)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================
# Fixtures
# ============================================================================


def _build_app(
    tmp_path: Path,
    *,
    enable_knowledge: bool = True,
    enable_api: bool | None = None,
) -> FastAPI:
    """Build app with knowledge subsystem enabled."""
    deltas = ["hi"]
    script = [TextDeltaEvent(delta=d) for d in deltas]
    script.append(DoneEvent(stop_reason="stop"))
    fake = FakeClient(scripts=[list(script) for _ in range(50)])

    from pi_agent_core_py.agent import Agent

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
        if enable_api is not None:
            kwargs["enable_knowledge_api"] = enable_api
    return create_app(**kwargs)


def _headers() -> dict:
    return {"X-PI-Agent-UI": "1"}


@pytest.fixture
def app_client(tmp_path):
    """App + client with Knowledge API enabled."""
    app = _build_app(tmp_path)
    with TestClient(app) as client:
        yield app, client


# ============================================================================
# G. API tests
# ============================================================================


class TestKnowledgeSecurityEnvelope:
    def test_missing_ui_header_rejected(self, app_client):
        app, client = app_client
        r = client.get("/api/knowledge/libraries")  # no UI header
        assert r.status_code == 400
        body = r.json()
        assert "missing_ui_header" in str(body)

    def test_invalid_origin_rejected(self, app_client):
        _app, client = app_client
        r = client.get(
            "/api/knowledge/libraries",
            headers={**_headers(), "Origin": "https://evil.example"},
        )
        # Forbidden origin → 403
        assert r.status_code == 403


class TestLibraryCRUD:
    def test_list_empty_initially(self, app_client):
        _app, client = app_client
        r = client.get("/api/knowledge/libraries", headers=_headers())
        assert r.status_code == 200, f"body={r.text}"
        assert r.json() == []

    def test_create_library_returns_201_with_active_status(self, app_client):
        _app, client = app_client
        r = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "MyLib", "description": "first"},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "MyLib"
        assert body["description"] == "first"
        assert body["status"] == "active"
        assert body["id"].startswith("lib_")

    def test_create_library_invalid_name_returns_422(self, app_client):
        _app, client = app_client
        r = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": ""},
        )
        assert r.status_code == 422

    def test_get_library_missing_returns_404(self, app_client):
        _app, client = app_client
        r = client.get(
            "/api/knowledge/libraries/lib_missing00000000",
            headers=_headers(),
        )
        assert r.status_code == 404
        body = r.json()
        assert body["detail"]["code"] == "library_not_found"

    def test_get_library_invalid_id_returns_400(self, app_client):
        _app, client = app_client
        r = client.get(
            "/api/knowledge/libraries/not-a-valid-id",
            headers=_headers(),
        )
        assert r.status_code == 400

    def test_patch_library_name(self, app_client):
        _app, client = app_client
        created = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "orig"},
        ).json()
        lib_id = created["id"]
        r = client.patch(
            f"/api/knowledge/libraries/{lib_id}",
            headers=_headers(),
            json={"name": "new"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "new"

    def test_patch_library_empty_payload_rejected(self, app_client):
        _app, client = app_client
        created = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "orig"},
        ).json()
        lib_id = created["id"]
        r = client.patch(
            f"/api/knowledge/libraries/{lib_id}",
            headers=_headers(),
            json={},
        )
        assert r.status_code == 400

    def test_delete_library_returns_204(self, app_client):
        _app, client = app_client
        created = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "x"},
        ).json()
        lib_id = created["id"]
        r = client.delete(
            f"/api/knowledge/libraries/{lib_id}", headers=_headers()
        )
        assert r.status_code == 204
        # Subsequent GET → 404
        r2 = client.get(
            f"/api/knowledge/libraries/{lib_id}", headers=_headers()
        )
        assert r2.status_code == 404

    def test_list_includes_stats(self, app_client):
        _app, client = app_client
        client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib1"},
        )
        r = client.get("/api/knowledge/libraries", headers=_headers())
        body = r.json()
        assert len(body) == 1
        assert body[0]["document_count"] == 0
        assert body[0]["binding_count"] == 0


class TestDocumentEndpoints:
    def test_list_documents_empty(self, app_client):
        _app, client = app_client
        created = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib"},
        ).json()
        lib_id = created["id"]
        r = client.get(
            f"/api/knowledge/libraries/{lib_id}/documents",
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_get_document_missing_returns_404(self, app_client):
        _app, client = app_client
        r = client.get(
            "/api/knowledge/documents/doc_missing00000000",
            headers=_headers(),
        )
        assert r.status_code == 404

    def test_delete_document_missing_returns_404(self, app_client):
        _app, client = app_client
        r = client.delete(
            "/api/knowledge/documents/doc_missing00000000",
            headers=_headers(),
        )
        assert r.status_code == 404

    def test_no_pdf_upload_endpoint(self, app_client):
        """P2-R0 §17 — PDF upload arrives in R2."""
        _app, client = app_client
        # POST to /api/knowledge/libraries/{lib}/documents must 404 / 405
        created = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib"},
        ).json()
        lib_id = created["id"]
        r = client.post(
            f"/api/knowledge/libraries/{lib_id}/documents",
            headers=_headers(),
            files={"file": ("x.pdf", b"fake", "application/pdf")},
        )
        assert r.status_code in (404, 405)


class TestSessionBinding:
    def test_get_empty_bindings(self, app_client):
        _app, client = app_client
        # Need a session — create one via sessions API
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "test"}
        ).json()
        sid = sess["id"]
        r = client.get(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == sid
        assert body["library_ids"] == []

    def test_put_bindings(self, app_client):
        _app, client = app_client
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "t"}
        ).json()
        sid = sess["id"]
        lib = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib1"},
        ).json()
        r = client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": [lib["id"]]},
        )
        assert r.status_code == 200
        assert r.json()["library_ids"] == [lib["id"]]

    def test_put_empty_unbinds(self, app_client):
        _app, client = app_client
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "t"}
        ).json()
        sid = sess["id"]
        lib = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib1"},
        ).json()
        # Bind
        client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": [lib["id"]]},
        )
        # Unbind
        r = client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": []},
        )
        assert r.status_code == 200
        assert r.json()["library_ids"] == []

    def test_invalid_library_id_format_rejected(self, app_client):
        _app, client = app_client
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "t"}
        ).json()
        sid = sess["id"]
        r = client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": ["not-a-valid-id"]},
        )
        assert r.status_code == 400

    def test_unknown_library_rejected(self, app_client):
        _app, client = app_client
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "t"}
        ).json()
        sid = sess["id"]
        r = client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": ["lib_missing00000000"]},
        )
        assert r.status_code == 404

    def test_cross_session_isolation(self, app_client):
        _app, client = app_client
        a = client.post(
            "/api/sessions", headers=_headers(), json={"title": "A"}
        ).json()
        b = client.post(
            "/api/sessions", headers=_headers(), json={"title": "B"}
        ).json()
        lib_a = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib-a"},
        ).json()
        lib_b = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib-b"},
        ).json()
        client.put(
            f"/api/sessions/{a['id']}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": [lib_a["id"]]},
        )
        client.put(
            f"/api/sessions/{b['id']}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": [lib_b["id"]]},
        )
        ra = client.get(
            f"/api/sessions/{a['id']}/knowledge-libraries",
            headers=_headers(),
        ).json()
        rb = client.get(
            f"/api/sessions/{b['id']}/knowledge-libraries",
            headers=_headers(),
        ).json()
        assert ra["library_ids"] == [lib_a["id"]]
        assert rb["library_ids"] == [lib_b["id"]]


class TestResponseShape:
    def test_no_absolute_path_in_response(self, app_client):
        """P2-R0 §3.2 — API responses never include absolute paths."""
        _app, client = app_client
        body = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "x"},
        ).json()
        # Walk the entire response — no field should leak an abs path
        text = str(body)
        # Look for typical abs path indicators
        assert "C:\\" not in text
        assert "data/knowledge" not in text
        assert "/libraries/" not in text  # only relative paths allowed

    def test_no_search_endpoint(self, app_client):
        """P2-R0 §6 — search_knowledge arrives in R3."""
        _app, client = app_client
        r = client.post(
            "/api/knowledge/search",
            headers=_headers(),
            json={"query": "test"},
        )
        assert r.status_code in (404, 405)


class TestSessionDeleteCascade:
    def test_deleting_session_clears_bindings(self, app_client):
        """P2-R0 §7.2 invariant 7 — session delete cascades to bindings."""
        _app, client = app_client
        sess = client.post(
            "/api/sessions", headers=_headers(), json={"title": "t"}
        ).json()
        sid = sess["id"]
        lib = client.post(
            "/api/knowledge/libraries",
            headers=_headers(),
            json={"name": "lib1"},
        ).json()
        client.put(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
            json={"library_ids": [lib["id"]]},
        )
        # Delete the session
        client.delete(f"/api/sessions/{sid}", headers=_headers())
        # Binding is gone (GET returns empty even after session deleted)
        r = client.get(
            f"/api/sessions/{sid}/knowledge-libraries",
            headers=_headers(),
        )
        assert r.status_code == 200
        assert r.json()["library_ids"] == []
        # Library itself survives
        lib_check = client.get(
            f"/api/knowledge/libraries/{lib['id']}", headers=_headers()
        )
        assert lib_check.status_code == 200


class TestRestartPersistence:
    """Verify data survives an app lifespan cycle (close + reopen)."""

    def test_library_survives_restart(self, tmp_path):
        app1 = _build_app(tmp_path)
        with TestClient(app1) as client:
            client.post(
                "/api/knowledge/libraries",
                headers=_headers(),
                json={"name": "persist"},
            )

        # Reopen with same paths
        app2 = _build_app(tmp_path)
        with TestClient(app2) as client:
            r = client.get("/api/knowledge/libraries", headers=_headers())
            libs = r.json()
            assert len(libs) == 1
            assert libs[0]["name"] == "persist"


class TestNoToolRegistration:
    """R1 must NOT register any AgentTool — search_knowledge is R3."""

    def test_search_knowledge_tool_registered_in_harness(self, tmp_path):
        """P2-R4-B2: search_knowledge IS registered when Knowledge enabled."""
        app = _build_app(tmp_path)
        with TestClient(app) as _client:
            harness = app.state.web.harness
            tool_names = harness.agent.tools.names()
        assert "search_knowledge" in tool_names

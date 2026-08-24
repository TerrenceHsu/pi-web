"""Wiki lifespan, security envelope and Source/Raw artifact API."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from wiki_parser import (
    FakeDualPdfParserProvider,
    FakeParserOutput,
    FakeParserProvider,
    FakeParserScenarioV2,
)

_HEADERS = {"X-PI-Agent-UI": "1"}


def _app(
    tmp_path: Path,
    *,
    provider: FakeParserProvider | None = None,
    provider_v2: FakeDualPdfParserProvider | None = None,
) -> FastAPI:
    fake = FakeClient(scripts=[[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop")]])
    harness = AgentHarness(Agent(system_prompt="", client=fake))
    harness.attach_skills([])
    return create_app(
        harness,
        db_path=str(tmp_path / "sessions.db"),
        uploads_dir=str(tmp_path / "uploads"),
        enable_trusted_host=True,
        credential_extra_hosts=("testserver",),
        wiki_root=str(tmp_path / "knowledge"),
        enable_wiki_api=True,
        wiki_pdf_provider=provider,
        wiki_pdf_provider_v2=provider_v2,
    )


def _wait_source(client: TestClient, source_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/wiki/sources/{source_id}", headers=_HEADERS)
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        if body["status"] == status:
            return body
        time.sleep(0.01)
    raise AssertionError(f"source did not reach {status}")


def test_wiki_lifespan_starts_and_closes_independent_store(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app):
        assert app.state.web.wiki_store is not None
        assert app.state.web.wiki_ingestion_service is not None
        assert app.state.web.wiki_ingestion_worker.running
        assert app.state.web.knowledge_store is None
    assert app.state.web.wiki_store is None
    assert app.state.web.wiki_ingestion_worker is None


def test_wiki_api_security_space_html_upload_and_raw_browse(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/wiki/spaces").status_code == 400
        created = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Docs", "description": "safe"},
        )
        assert created.status_code == 201
        space_id = created.json()["id"]
        upload = client.post(
            f"/api/wiki/spaces/{space_id}/sources",
            headers=_HEADERS,
            files={
                "file": (
                    "guide.html",
                    b"<h1>Guide</h1><script>bad()</script><p>Hello</p>",
                    "text/html",
                )
            },
        )
        assert upload.status_code == 201, upload.text
        body = upload.json()
        assert body["parse_queued"] is True
        source_id = body["source"]["id"]
        persisted = _wait_source(client, source_id, "parsed")
        assert str(tmp_path) not in str(persisted)

        artifacts = client.get(
            f"/api/wiki/sources/{source_id}/artifacts",
            headers=_HEADERS,
        )
        assert artifacts.status_code == 200
        artifact_rows = artifacts.json()
        markdown = next(row for row in artifact_rows if row["kind"] == "parsed_markdown")
        content = client.get(
            f"/api/wiki/artifacts/{markdown['id']}/content",
            headers=_HEADERS,
        )
        assert content.status_code == 200
        assert "# Guide" in content.text
        assert "bad" not in content.text
        assert content.headers["x-content-type-options"] == "nosniff"


def test_pdf_stays_uploaded_until_provider_is_configured(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "PDF"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            files={"file": ("paper.pdf", b"%PDF-1.7\npaper\n", "application/pdf")},
        )
        assert upload.status_code == 201
        assert upload.json()["parse_queued"] is False
        source_id = upload.json()["source"]["id"]
        retry = client.post(
            f"/api/wiki/sources/{source_id}/parse",
            headers=_HEADERS,
        )
        assert retry.status_code == 503
        assert retry.json()["detail"]["code"] == "invalid_configuration"
        assert client.get(
            f"/api/wiki/sources/{source_id}", headers=_HEADERS
        ).json()["status"] == "uploaded"


def test_pdf_fake_provider_runs_through_same_api(tmp_path: Path) -> None:
    provider = FakeParserProvider(outputs=(FakeParserOutput(markdown="# PDF\n"),))
    app = _app(tmp_path, provider=provider)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "PDF"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            files={"file": ("paper.pdf", b"%PDF-1.7\npaper\n", "application/pdf")},
        )
        assert upload.status_code == 201
        assert upload.json()["parse_queued"] is True
        source_id = upload.json()["source"]["id"]
        source = _wait_source(client, source_id, "parsed")
        assert source["selected_parse_revision_id"].startswith("parse_revision_")
        assert source["selection_version"] == 1
        deadline = time.monotonic() + 2
        while not provider.destroyed_provider_job_ids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.destroyed_provider_job_ids

        unsupported = client.post(
            f"/api/wiki/sources/{source_id}/parse",
            headers=_HEADERS,
            json={"parse_mode": "accurate"},
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["detail"]["code"] == "unsupported_parse_mode"


def test_pdf_v2_api_defaults_auto_and_accepts_explicit_accurate_reparse(
    tmp_path: Path,
) -> None:
    provider = FakeDualPdfParserProvider(
        scenarios=(
            FakeParserScenarioV2(),
            FakeParserScenarioV2(preflight_profile="scanned"),
        )
    )
    app = _app(tmp_path, provider_v2=provider)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "PDF v2"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            files={"file": ("paper.pdf", b"%PDF-1.7\npaper\n", "application/pdf")},
        )
        assert upload.status_code == 201
        source_id = upload.json()["source"]["id"]
        first = _wait_source(client, source_id, "parsed")
        assert first["selection_version"] == 1
        assert provider.created_specs[0].requested_mode == "auto"
        first_jobs = client.get(
            f"/api/wiki/sources/{source_id}/jobs",
            headers=_HEADERS,
        )
        assert first_jobs.status_code == 200
        assert first_jobs.json()[0]["requested_mode"] == "auto"
        first_job_id = first_jobs.json()[0]["id"]
        first_attempts = client.get(
            f"/api/wiki/jobs/{first_job_id}/attempts",
            headers=_HEADERS,
        )
        assert first_attempts.status_code == 200
        assert first_attempts.json()[0]["parser"] == "pymupdf4llm"
        first_revisions = client.get(
            f"/api/wiki/sources/{source_id}/parse-revisions",
            headers=_HEADERS,
        )
        assert first_revisions.status_code == 200
        first_revision_id = first_revisions.json()[0]["id"]

        reparse = client.post(
            f"/api/wiki/sources/{source_id}/parse",
            headers=_HEADERS,
            json={"parse_mode": "accurate"},
        )
        assert reparse.status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(
                f"/api/wiki/sources/{source_id}",
                headers=_HEADERS,
            ).json()
            if current["status"] == "parsed" and current["selection_version"] == 2:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("accurate reparse did not finish")
        assert len(provider.created_specs) == 2
        assert provider.created_specs[1].requested_mode == "accurate"
        revisions = client.get(
            f"/api/wiki/sources/{source_id}/parse-revisions",
            headers=_HEADERS,
        ).json()
        assert len(revisions) == 2
        historical = client.get(
            f"/api/wiki/sources/{source_id}/artifacts",
            headers=_HEADERS,
            params={"parse_revision_id": first_revision_id},
        )
        assert historical.status_code == 200
        assert historical.json()
        assert {
            row["parse_revision_id"] for row in historical.json()
        } == {first_revision_id}


def test_pdf_v2_upload_accepts_multipart_parse_mode(tmp_path: Path) -> None:
    provider = FakeDualPdfParserProvider(
        scenarios=(FakeParserScenarioV2(preflight_profile="scanned"),)
    )
    app = _app(tmp_path, provider_v2=provider)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Accurate upload"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            data={"parse_mode": "accurate"},
            files={"file": ("paper.pdf", b"%PDF-1.7\npaper\n", "application/pdf")},
        )
        assert upload.status_code == 201
        source_id = upload.json()["source"]["id"]
        _wait_source(client, source_id, "parsed")
        assert provider.created_specs[0].requested_mode == "accurate"


def test_html_rejects_pdf_parse_mode_before_persisting_source(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "HTML mode"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            data={"parse_mode": "auto"},
            files={"file": ("guide.html", b"<h1>Guide</h1>", "text/html")},
        )
        assert upload.status_code == 400
        assert upload.json()["detail"]["code"] == "unsupported_parse_mode"
        sources = client.get(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
        )
        assert sources.status_code == 200
        assert sources.json() == []

"""Wiki lifespan, security envelope and Source/Raw artifact API."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.messages import ToolCall
from pi_agent_core_py.model_client import (
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    ToolCallEvent,
)
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.wiki.knowledge_agent import KNOWLEDGE_AGENT_TOOL_NAMES
from wiki_parser import (
    FakeMineruParserProvider,
    FakeParserOutput,
    FakeParserProvider,
    FakeParserScenarioV2,
)

_HEADERS = {"X-PI-Agent-UI": "1"}


def _app(
    tmp_path: Path,
    *,
    provider: FakeParserProvider | None = None,
    provider_v2: FakeMineruParserProvider | None = None,
    model_text: str = "ok",
    model_scripts: list[list[Any]] | None = None,
    source_retention_seconds: float = 7 * 24 * 60 * 60,
    enable_intent_routing: bool = False,
) -> FastAPI:
    fake = FakeClient(
        scripts=model_scripts or [[TextDeltaEvent(delta=model_text), DoneEvent(stop_reason="stop")]]
    )
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
        wiki_source_retention_seconds=source_retention_seconds,
        enable_intent_routing=enable_intent_routing,
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
    assert app.state.web.wiki_store is None
    assert app.state.web.wiki_ingestion_worker is None


def test_wiki_space_archive_restore_and_safe_delete(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Lifecycle"},
        ).json()
        space_id = created["id"]
        archived = client.patch(
            f"/api/wiki/spaces/{space_id}/status",
            headers=_HEADERS,
            json={"status": "archived"},
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        blocked_upload = client.post(
            f"/api/wiki/spaces/{space_id}/sources",
            headers=_HEADERS,
            files={"file": ("blocked.html", b"<h1>Blocked</h1>", "text/html")},
        )
        assert blocked_upload.status_code == 409
        assert blocked_upload.json()["detail"]["code"] == "space_read_only"
        restored = client.patch(
            f"/api/wiki/spaces/{space_id}/status",
            headers=_HEADERS,
            json={"status": "active"},
        )
        assert restored.status_code == 200
        assert restored.json()["status"] == "active"
        deleted = client.delete(
            f"/api/wiki/spaces/{space_id}",
            headers=_HEADERS,
        )
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleting"


def test_legacy_chunk_knowledge_stays_retired(tmp_path: Path) -> None:
    """The retired Chunk-RAG product has no runtime switch, package or route."""
    parameters = inspect.signature(create_app).parameters
    assert "knowledge_root" not in parameters
    assert "enable_knowledge_api" not in parameters
    knowledge_package = Path(create_app.__code__.co_filename).parent / "knowledge"
    assert not any(knowledge_package.glob("*.py"))

    app = _app(tmp_path)
    with TestClient(app) as client:
        assert "search_knowledge" not in app.state.web.harness.agent.tools.names()
        response = client.post(
            "/api/knowledge/libraries/legacy/search",
            headers=_HEADERS,
            json={"query": "x"},
        )
        assert response.status_code == 404


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


def test_wiki_source_delete_disables_raw_and_purges_due_files(tmp_path: Path) -> None:
    app = _app(tmp_path, source_retention_seconds=0)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Disposable"},
        ).json()
        uploaded = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            files={"file": ("draft.html", b"<h1>Draft</h1>", "text/html")},
        ).json()
        source = _wait_source(client, uploaded["source"]["id"], "parsed")
        source_path = (
            tmp_path / "knowledge" / "spaces" / space["id"] / source["source_relpath"]
        )
        assert source_path.is_file()

        deleted = client.delete(
            f"/api/wiki/sources/{source['id']}",
            headers=_HEADERS,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleting"
        assert not source_path.parent.exists()
        unavailable = client.get(
            f"/api/wiki/sources/{source['id']}/content",
            headers=_HEADERS,
        )
        assert unavailable.status_code == 410
        assert unavailable.json()["detail"]["code"] == "source_not_available"


def test_wiki_api_generates_and_reads_agent_summary_draft(tmp_path: Path) -> None:
    summary_text = json.dumps(
        {
            "suggested_title": "Guide",
            "overview": "A short guide.",
            "key_points": [{"text": "It says hello.", "page_numbers": [1]}],
            "topics": [],
            "caveats": [],
        }
    )
    app = _app(tmp_path, model_text=summary_text)
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Summary"},
        ).json()
        upload = client.post(
            f"/api/wiki/spaces/{space['id']}/sources",
            headers=_HEADERS,
            files={"file": ("guide.html", b"<h1>Guide</h1><p>Hello</p>", "text/html")},
        )
        source_id = upload.json()["source"]["id"]
        source = _wait_source(client, source_id, "parsed")

        generated = client.post(
            f"/api/wiki/sources/{source_id}/summaries",
            headers=_HEADERS,
        )
        assert generated.status_code == 201, generated.text
        summary = generated.json()
        assert summary["content"]["suggested_title"] == "Guide"
        assert summary["parse_revision_id"] == source["selected_parse_revision_id"]

        listed = client.get(
            f"/api/wiki/sources/{source_id}/summaries",
            headers=_HEADERS,
        )
        assert listed.status_code == 200
        assert listed.json() == [summary]
        fetched = client.get(
            f"/api/wiki/summaries/{summary['id']}",
            headers=_HEADERS,
        )
        assert fetched.status_code == 200
        assert fetched.json() == summary

        proposed = client.post(
            f"/api/wiki/summaries/{summary['id']}/entry-page-proposal",
            headers=_HEADERS,
        )
        assert proposed.status_code == 201, proposed.text
        proposal = proposed.json()
        assert proposal["kind"] == "entry"
        assert proposal["summary_id"] == summary["id"]
        assert "## 关键内容" in proposal["markdown"]
        topics = client.post(
            f"/api/wiki/summaries/{summary['id']}/topic-page-proposals",
            headers=_HEADERS,
        )
        assert topics.status_code == 201, topics.text
        assert topics.json() == []
        frozen = client.post(
            f"/api/wiki/summaries/{summary['id']}/change-set",
            headers=_HEADERS,
        )
        assert frozen.status_code == 201, frozen.text
        bundle = frozen.json()
        assert bundle["change_set"]["status"] == "awaiting_approval"
        assert len(bundle["items"]) == 1
        change_set_id = bundle["change_set"]["id"]
        assert (
            client.get(
                f"/api/wiki/change-sets/{change_set_id}",
                headers=_HEADERS,
            ).json()
            == bundle["change_set"]
        )
        assert (
            client.get(
                f"/api/wiki/change-sets/{change_set_id}/items",
                headers=_HEADERS,
            ).json()
            == bundle["items"]
        )
        assert client.get(
            f"/api/wiki/spaces/{space['id']}/change-sets",
            headers=_HEADERS,
        ).json() == [bundle["change_set"]]
        decision = client.post(
            f"/api/wiki/change-sets/{change_set_id}/decision",
            headers=_HEADERS,
            json={"decision": "approve"},
        )
        assert decision.status_code == 200, decision.text
        published = decision.json()
        assert published["change_set"]["status"] == "approved"
        assert len(published["pages"]) == 1
        page = published["pages"][0]
        assert client.get(
            f"/api/wiki/spaces/{space['id']}/pages",
            headers=_HEADERS,
        ).json() == [page]
        assert (
            client.get(
                f"/api/wiki/pages/{page['id']}",
                headers=_HEADERS,
            ).json()
            == page
        )
        revisions = client.get(
            f"/api/wiki/pages/{page['id']}/revisions",
            headers=_HEADERS,
        ).json()
        assert len(revisions) == 1
        assert (
            client.get(
                f"/api/wiki/page-revisions/{revisions[0]['id']}",
                headers=_HEADERS,
            ).json()
            == revisions[0]
        )
        search = client.get(
            f"/api/wiki/spaces/{space['id']}/search",
            headers=_HEADERS,
            params={"q": "Guide"},
        )
        assert search.status_code == 200, search.text
        assert search.json()[0]["page_id"] == page["id"]
        graph = client.get(
            f"/api/wiki/spaces/{space['id']}/graph",
            headers=_HEADERS,
        )
        assert graph.status_code == 200, graph.text
        graph_body = graph.json()
        assert graph_body["graph_revision"] == 1
        assert [(node["kind"], node["id"]) for node in graph_body["nodes"]] == [
            ("page", page["id"]),
            ("source", source_id),
        ]
        assert len(graph_body["edges"]) == 1
        assert graph_body["edges"][0]["relation_type"] == "derived_from"
        assert graph_body["edges"][0]["system_managed"] is True
        source_delete = client.delete(
            f"/api/wiki/sources/{source_id}",
            headers=_HEADERS,
        )
        assert source_delete.status_code == 409
        assert source_delete.json()["detail"]["code"] == "source_in_use"
        space_delete = client.delete(
            f"/api/wiki/spaces/{space['id']}",
            headers=_HEADERS,
        )
        assert space_delete.status_code == 409
        assert space_delete.json()["detail"]["code"] == "space_in_use"
        neighbors = client.get(
            (f"/api/wiki/spaces/{space['id']}/graph/pages/{page['id']}/neighbors"),
            headers=_HEADERS,
        )
        assert neighbors.status_code == 200, neighbors.text
        assert neighbors.json() == graph_body
        assert (
            client.get(
                f"/api/wiki/page-proposals/{proposal['id']}",
                headers=_HEADERS,
            ).json()
            == proposal
        )
        assert client.get(
            f"/api/wiki/sources/{source_id}/page-proposals",
            headers=_HEADERS,
        ).json() == [proposal]


def test_knowledge_conversation_reuses_agent_with_fixed_wiki_mode(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        model_scripts=[
            [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="wiki-call-1",
                        name="wiki_list_pages",
                        arguments={},
                    )
                ),
                DoneEvent(stop_reason="tool_use"),
            ],
            [
                TextDeltaEvent(delta="Knowledge answer"),
                DoneEvent(stop_reason="stop"),
            ],
        ],
    )
    with TestClient(app) as client:
        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Knowledge chat"},
        ).json()
        created = client.post(
            f"/api/wiki/spaces/{space['id']}/conversations",
            headers=_HEADERS,
            json={"title": "First chat"},
        )
        assert created.status_code == 201, created.text
        conversation = created.json()
        assert conversation["space_id"] == space["id"]
        assert conversation["status"] == "active"
        assert client.get(
            f"/api/wiki/spaces/{space['id']}/conversations",
            headers=_HEADERS,
        ).json() == [conversation]

        wrong_binding = client.post(
            "/api/prompt",
            json={
                "session_id": conversation["session_id"],
                "knowledge_conversation_id": "conversation_ffffffffffffffffffffffff",
                "text": "hello",
            },
        )
        assert wrong_binding.status_code == 403
        attached = client.post(
            "/api/prompt",
            json={
                "session_id": conversation["session_id"],
                "file_ids": ["file_not_allowed"],
                "text": "hello",
            },
        )
        assert attached.status_code == 400
        budget = client.get(f"/api/sessions/{conversation['session_id']}/context-budget")
        assert budget.status_code == 200, budget.text
        assert budget.json()["session_id"] == conversation["session_id"]
        assert budget.json()["estimate"]["tool_definition_tokens"] > 0

        response = client.post(
            "/api/prompt",
            json={
                "session_id": conversation["session_id"],
                "knowledge_conversation_id": conversation["id"],
                "text": "What is in this Wiki?",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == conversation["session_id"]
        fake = app.state.web.harness.agent.client
        assert fake.last_tools is not None
        assert tuple(tool.name for tool in fake.last_tools) == (KNOWLEDGE_AGENT_TOOL_NAMES)
        assert "# Knowledge mode (knowledge-agent/v1)" in fake.last_system_prompt
        assert conversation["id"] in fake.last_system_prompt
        assert len(fake.all_tools_calls) == 2
        assert any(message.get("role") == "toolResult" for message in response.json()["messages"])
        assert "list_files" in app.state.web.harness.agent.tools.names()
        assert not any(
            name.startswith("wiki_") for name in app.state.web.harness.agent.tools.names()
        )

        archived = client.patch(
            f"/api/wiki/conversations/{conversation['id']}/status",
            headers=_HEADERS,
            json={"status": "archived"},
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        inactive = client.post(
            "/api/prompt",
            json={
                "session_id": conversation["session_id"],
                "text": "hello again",
            },
        )
        assert inactive.status_code == 409

        second = client.post(
            f"/api/wiki/spaces/{space['id']}/conversations",
            headers=_HEADERS,
            json={"title": "Second chat"},
        ).json()
        assert second["session_id"] != conversation["session_id"]
        first_tree = client.get(
            f"/api/sessions/{conversation['session_id']}/tree"
        ).json()
        second_tree = client.get(f"/api/sessions/{second['session_id']}/tree").json()
        assert len(first_tree["entries"]) >= 3
        assert second_tree["entries"] == []
        deleted = client.delete(f"/api/sessions/{second['session_id']}")
        assert deleted.status_code == 200, deleted.text
        fetched = client.get(
            f"/api/wiki/conversations/{second['id']}",
            headers=_HEADERS,
        )
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "archived"


def test_intent_router_exposes_and_enforces_all_three_routes(tmp_path: Path) -> None:
    app = _app(
        tmp_path,
        enable_intent_routing=True,
        model_scripts=[
            [TextDeltaEvent(delta="Read-only answer"), DoneEvent(stop_reason="stop")],
            [TextDeltaEvent(delta="Knowledge answer"), DoneEvent(stop_reason="stop")],
        ],
    )
    with TestClient(app) as client:
        session_id = client.post(
            "/api/sessions",
            json={"title": "Intent routes"},
        ).json()["id"]
        read_response = client.post(
            "/api/prompt",
            json={
                "session_id": session_id,
                "text": "只检查当前实现是否完整，不要修改或运行代码",
            },
        )
        assert read_response.status_code == 200, read_response.text
        assert read_response.json()["intent"]["route"] == "read_only"
        fake = app.state.web.harness.agent.client
        assert fake.last_tools is not None
        read_tool_names = {tool.name for tool in fake.last_tools}
        assert {"list_files", "view_file"} <= read_tool_names
        assert "write_file" not in read_tool_names

        coding_response = client.post(
            "/api/prompt",
            json={"session_id": session_id, "text": "帮我写一个 PPO 源码"},
        )
        assert coding_response.status_code == 409
        # A synchronous prompt has no live async request to own an execution
        # approval. It must fail closed before any backend is resolved.
        assert coding_response.json()["error_type"] == "execution_request_denied"

        space = client.post(
            "/api/wiki/spaces",
            headers=_HEADERS,
            json={"name": "Routed knowledge"},
        ).json()
        conversation = client.post(
            f"/api/wiki/spaces/{space['id']}/conversations",
            headers=_HEADERS,
            json={"title": "Bound route"},
        ).json()
        knowledge_response = client.post(
            "/api/prompt",
            json={
                "session_id": conversation["session_id"],
                "knowledge_conversation_id": conversation["id"],
                "text": "总结这个 Wiki",
            },
        )
        assert knowledge_response.status_code == 200, knowledge_response.text
        assert knowledge_response.json()["intent"]["route"] == "knowledge"
        assert tuple(tool.name for tool in fake.last_tools or ()) == KNOWLEDGE_AGENT_TOOL_NAMES


def test_intent_router_honors_negation_and_explicit_overrides(tmp_path: Path) -> None:
    app = _app(
        tmp_path,
        enable_intent_routing=True,
        model_scripts=[
            [TextDeltaEvent(delta="No changes"), DoneEvent(stop_reason="stop")],
            [TextDeltaEvent(delta="Still no changes"), DoneEvent(stop_reason="stop")],
        ],
    )
    with TestClient(app) as client:
        session_id = client.post(
            "/api/sessions",
            json={"title": "Intent overrides"},
        ).json()["id"]
        negated = client.post(
            "/api/prompt",
            json={
                "session_id": session_id,
                "text": "修复建议可以说明，但不要修改或运行代码",
            },
        )
        assert negated.status_code == 200, negated.text
        assert negated.json()["intent"]["reason_code"] == "read_only_constraint"

        explicit_read = client.post(
            "/api/prompt",
            json={
                "session_id": session_id,
                "text": "实现这个功能",
                "intent_mode": "read_only",
            },
        )
        assert explicit_read.status_code == 200, explicit_read.text
        assert explicit_read.json()["intent"] == {
            "route": "read_only",
            "confidence": 1.0,
            "source": "explicit",
            "reason_code": "explicit_read_only_mode",
            "explicit": True,
        }

        explicit_code = client.post(
            f"/api/sessions/{session_id}/context-budget/estimate",
            json={"text": "只解释方案", "intent_mode": "coding"},
        )
        assert explicit_code.status_code == 200, explicit_code.text
        assert explicit_code.json()["intent"]["route"] == "coding"


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
        assert (
            client.get(f"/api/wiki/sources/{source_id}", headers=_HEADERS).json()["status"]
            == "uploaded"
        )


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
            json={"parse_mode": "gpu-high"},
        )
        assert unsupported.status_code == 400
        assert unsupported.json()["detail"]["code"] == "unsupported_parse_mode"


def test_pdf_v2_api_defaults_pipeline_and_accepts_explicit_gpu_reparse(
    tmp_path: Path,
) -> None:
    provider = FakeMineruParserProvider(
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
        assert provider.created_specs[0].requested_mode == "pipeline"
        first_jobs = client.get(
            f"/api/wiki/sources/{source_id}/jobs",
            headers=_HEADERS,
        )
        assert first_jobs.status_code == 200
        assert first_jobs.json()[0]["requested_mode"] == "pipeline"
        first_job_id = first_jobs.json()[0]["id"]
        first_attempts = client.get(
            f"/api/wiki/jobs/{first_job_id}/attempts",
            headers=_HEADERS,
        )
        assert first_attempts.status_code == 200
        assert first_attempts.json()[0]["parser"] == "mineru"
        first_revisions = client.get(
            f"/api/wiki/sources/{source_id}/parse-revisions",
            headers=_HEADERS,
        )
        assert first_revisions.status_code == 200
        first_revision_id = first_revisions.json()[0]["id"]

        reparse = client.post(
            f"/api/wiki/sources/{source_id}/parse",
            headers=_HEADERS,
            json={"parse_mode": "gpu-high"},
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
            raise AssertionError("GPU reparse did not finish")
        assert len(provider.created_specs) == 2
        assert provider.created_specs[1].requested_mode == "gpu-high"
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
        assert {row["parse_revision_id"] for row in historical.json()} == {first_revision_id}


def test_pdf_v2_upload_accepts_multipart_parse_mode(tmp_path: Path) -> None:
    provider = FakeMineruParserProvider(
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
            data={"parse_mode": "gpu-medium"},
            files={"file": ("paper.pdf", b"%PDF-1.7\npaper\n", "application/pdf")},
        )
        assert upload.status_code == 201
        source_id = upload.json()["source"]["id"]
        _wait_source(client, source_id, "parsed")
        assert provider.created_specs[0].requested_mode == "gpu-medium"


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
            data={"parse_mode": "gpu-high"},
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

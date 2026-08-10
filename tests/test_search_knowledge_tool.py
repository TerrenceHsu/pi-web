"""P2-R4-B2 — search_knowledge Tool + Agent wiring tests.

Tests the Tool adapter, schema security, Agent integration via real
app + TestClient, and Knowledge-disabled behavior.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app

# ============================================================================


# ============================================================================
# Helpers
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


# ============================================================================
# 1. Tool schema security
# ============================================================================


class TestToolSchema:
    def test_tool_registered_when_knowledge_enabled(self, tmp_path: Path):
        app = _build_app(tmp_path)
        with TestClient(app):
            harness = app.state.web.harness
            assert harness.agent.tools.has("search_knowledge")

    def test_tool_not_registered_when_knowledge_disabled(self, tmp_path: Path):
        deltas = ["hi"]
        script = [TextDeltaEvent(delta=d) for d in deltas]
        script.append(DoneEvent(stop_reason="stop"))
        fake = FakeClient(scripts=[list(script) for _ in range(50)])
        agent = Agent(system_prompt="", client=fake)
        harness = AgentHarness(agent)
        harness.attach_skills([])
        app = create_app(
            harness=harness,
            db_path=str(tmp_path / "sessions.db"),
            uploads_dir=str(tmp_path / "uploads"),
            knowledge_root=None,
            enable_knowledge_api=False,
            enable_trusted_host=True,
            credential_extra_hosts=("testserver",),
            credential_extra_ui_origins=(),
        )
        with TestClient(app):
            assert not harness.agent.tools.has("search_knowledge")

    def test_schema_has_query_and_limit_only(self, tmp_path: Path):
        app = _build_app(tmp_path)
        with TestClient(app):
            tool = app.state.web.harness.agent.tools.get("search_knowledge")
            params = tool.parameters
            assert set(params.get("properties", {}).keys()) == {"query", "limit"}
            assert params.get("additionalProperties") is False

    def test_schema_forbidden_args_absent(self, tmp_path: Path):
        app = _build_app(tmp_path)
        with TestClient(app):
            tool = app.state.web.harness.agent.tools.get("search_knowledge")
            props = set(tool.parameters.get("properties", {}).keys())
            for forbidden in (
                "session_id", "library_id", "library_ids",
                "document_id", "raw_fts_query",
            ):
                assert forbidden not in props, (
                    f"forbidden arg {forbidden!r} in schema"
                )

    def test_default_limit_is_5(self, tmp_path: Path):
        app = _build_app(tmp_path)
        with TestClient(app):
            tool = app.state.web.harness.agent.tools.get("search_knowledge")
            limit_prop = tool.parameters["properties"]["limit"]
            assert limit_prop["default"] == 5
            assert limit_prop["minimum"] == 1
            assert limit_prop["maximum"] == 10


# ============================================================================
# 2. Tool execution (via direct call)
# ============================================================================


class TestToolExecution:
    def test_missing_session_returns_error(self, tmp_path: Path):
        """Tool execute with no session → controlled error."""
        from pi_agent_core_py.web.knowledge.evidence import EvidenceRegistry
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        def _no_session() -> str | None:
            return None

        def _registry() -> EvidenceRegistry:
            return EvidenceRegistry()

        tool = SearchKnowledgeTool.__new__(SearchKnowledgeTool)
        tool.name = "search_knowledge"
        tool._session_id_getter = _no_session
        tool._evidence_registry_getter = _registry
        tool._service = None

        import asyncio

        result = asyncio.run(tool.execute("tc1", {"query": "test"}))
        assert result.is_error is True
        assert "no active session" in result.content[0].text.lower()


# ============================================================================
# 3. Tool result serialization
# ============================================================================


class TestResultSerialization:
    def test_no_results_message(self):
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        text = SearchKnowledgeTool._serialize_result("test", ())
        assert "No relevant knowledge" in text

    def test_evidence_format(self):
        from pi_agent_core_py.web.knowledge.search_models import (
            KnowledgeEvidence,
        )
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        ev = KnowledgeEvidence(
            evidence_id="E1",
            document_id="doc_x",
            chunk_id="chunk_x",
            source_filename="report.pdf",
            heading_path=("Methods", "Analysis"),
            page_start=12,
            page_end=13,
            content="IMRT uses modulated beams.",
            rank=-1.0,
        )
        text = SearchKnowledgeTool._serialize_result("IMRT", (ev,))
        assert "[E1]" in text
        assert "report.pdf" in text
        assert "Pages: 12-13" in text
        assert "Methods > Analysis" in text
        assert "IMRT uses modulated beams." in text

    def test_single_page_format(self):
        from pi_agent_core_py.web.knowledge.search_models import (
            KnowledgeEvidence,
        )
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        ev = KnowledgeEvidence(
            evidence_id="E1", document_id="d", chunk_id="c",
            source_filename="a.pdf", heading_path=(),
            page_start=5, page_end=5, content="x", rank=-1.0,
        )
        text = SearchKnowledgeTool._serialize_result("q", (ev,))
        assert "Page: 5" in text
        assert "Pages:" not in text

    def test_no_acl_leakage(self):
        from pi_agent_core_py.web.knowledge.search_models import (
            KnowledgeEvidence,
        )
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        ev = KnowledgeEvidence(
            evidence_id="E1", document_id="d", chunk_id="c",
            source_filename="a.pdf", heading_path=(),
            page_start=1, page_end=1, content="x", rank=-1.0,
        )
        text = SearchKnowledgeTool._serialize_result("q", (ev,))
        assert "session" not in text.lower()
        assert "library_id" not in text.lower()

    def test_no_path_leakage(self):
        from pi_agent_core_py.web.knowledge.search_models import (
            KnowledgeEvidence,
        )
        from pi_agent_core_py.web.knowledge.search_tool import (
            SearchKnowledgeTool,
        )

        ev = KnowledgeEvidence(
            evidence_id="E1", document_id="d", chunk_id="c",
            source_filename="a.pdf", heading_path=(),
            page_start=1, page_end=1, content="x", rank=-1.0,
        )
        text = SearchKnowledgeTool._serialize_result("q", (ev,))
        assert "D:\\" not in text
        assert "/home/" not in text
        assert "/tmp/" not in text
        assert "documents/" not in text


# ============================================================================
# 4. Repeated lifecycle — no duplicate registration
# ============================================================================


class TestRepeatedLifecycle:
    def test_two_app_cycles_no_duplicate_registration(
        self, tmp_path: Path
    ):
        for _ in range(2):
            app = _build_app(tmp_path)
            with TestClient(app):
                harness = app.state.web.harness
                assert harness.agent.tools.has("search_knowledge")
                tool = harness.agent.tools.get("search_knowledge")
                assert tool is not None
            assert app.state.web.indexing_worker_manager is None

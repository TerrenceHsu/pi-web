"""Optional tool selection and validation also run without compute dependencies."""

import sqlite3
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent_workspace.analysis import AnalysisError, AnalysisRequest
from coding_agent_app.core.resources import (
    CodingAgentResourceSelection,
    HarnessCodingAgentResourceLoader,
)
from coding_agent_app.data_analysis import service as analysis_service
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import FakeClient
from pi_agent_core_py.tools.data_analysis import DataAnalysisTool
from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore


@pytest.mark.parametrize(
    "changes",
    [
        {"file_id": "../secret"},
        {"python": "print(1)"},
        {"action": "sql"},
        {"columns": ["x", "x"]},
        {"limit": 5001},
        {"filters": [{"column": "x", "operation": "eq", "value": float("nan")}]},
        {"action": "chart", "x": "x", "y": "x"},
    ],
)
def test_request_rejects_code_paths_and_ambiguous_operations(changes):
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate({"file_id": "file-a", "action": "inspect", **changes})


async def test_optional_tool_is_only_visible_in_selected_session_and_errors_are_safe():
    service = AsyncMock()
    tool = DataAnalysisTool(service, lambda: "selected")
    harness = AgentHarness(Agent(system_prompt="", client=FakeClient([])))
    harness.agent.tools.register(tool)

    async def selection(session_id):
        return CodingAgentResourceSelection(
            tool_names=frozenset({"analyze_data"}) if session_id == "selected" else frozenset(),
        )

    loader = HarnessCodingAgentResourceLoader(
        harness,
        selection_loader=selection,
        optional_tool_names=frozenset({"analyze_data"}),
    )
    assert tool in (await loader.load("selected")).tools
    assert tool not in (await loader.load("other")).tools
    assert tool not in (await loader.load(None)).tools
    args = {"file_id": "file-a", "action": "inspect"}
    service.analyze.return_value = {"run_id": "test-run", "rows": [[1]]}
    update = AsyncMock()
    result = await tool.execute("call-1", args, on_update=update)
    assert result.details["analysis"]["rows"] == [[1]]
    assert update.await_count == 1
    assert service.analyze.await_args.args[0] == "selected"
    service.analyze.side_effect = AnalysisError("analysis_disabled")
    assert (await tool.execute("call-2", args)).is_error
    no_session = DataAnalysisTool(service, lambda: None)
    assert (await no_session.execute("call-3", args)).details == {"error_code": "no_active_session"}


async def test_tool_model_preview_is_bounded_without_losing_web_result():
    service = AsyncMock()
    service.analyze.return_value = {
        "run_id": "run", "columns": ["text"], "rows": [["a" * 2_000] for _ in range(50)],
    }
    result = await DataAnalysisTool(service, lambda: "one").execute(
        "call-1", {"file_id": "file-a", "action": "inspect"},
    )
    assert len(result.content[0].text.encode()) < 32_768
    assert '"model_preview_limited": true' in result.content[0].text
    assert len(result.details["analysis"]["rows"]) == 50


def test_missing_optional_dependency_is_explained_not_installed(monkeypatch):
    monkeypatch.setattr(analysis_service.importlib.util, "find_spec", lambda _name: None)
    capability = analysis_service.analysis_capability()
    assert capability["available"] is False
    assert "pandas" in capability["reason"]


@pytest.mark.parametrize("fail", [False, True])
async def test_v3_tool_selection_migration_preserves_choices_and_rolls_back(tmp_path, fail):
    path = tmp_path / "extensions.sqlite"
    initial = ExtensionSQLiteStore(path)
    await initial.init()
    await initial.close()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO sessions VALUES ('one')")
        db.execute("DROP TABLE web_workspace_tool_selection")
        db.execute("UPDATE web_extension_schema_meta SET version=3")
        db.execute("INSERT INTO web_workspace_extension_selection VALUES ('one','now')")
        db.execute("INSERT INTO web_workspace_mcp_selection VALUES ('one','ddgs','now')")
        db.execute("INSERT INTO web_workspace_skill_selection VALUES ('one','review','now')")
        if fail:
            db.execute("""CREATE TRIGGER reject_upgrade BEFORE UPDATE ON web_extension_schema_meta
                WHEN NEW.version=4 BEGIN SELECT RAISE(ABORT,'test interruption'); END""")
    upgraded = ExtensionSQLiteStore(path)
    if fail:
        with pytest.raises(sqlite3.IntegrityError, match="test interruption"):
            await upgraded.init()
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version FROM web_extension_schema_meta").fetchone()[0] == 3
            assert not db.execute(
                "SELECT name FROM sqlite_master WHERE name=?", ("web_workspace_tool_selection",)
            ).fetchall()
    else:
        await upgraded.init()
        try:
            selected = await upgraded.get_workspace_extension_selection("one")
            assert selected.mcp_server_names == ("ddgs",)
            assert selected.skill_names == ("review",)
            assert selected.tool_names == ()
        finally:
            await upgraded.close()

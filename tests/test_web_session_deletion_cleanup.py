"""Session deletion must remove owned state, not only hide the sidebar item."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_workspace import WorkspaceStore
from agent_workspace.analysis import AnalysisError
from coding_agent_app.data_analysis.service import DataAnalysisService
from coding_sandbox.lifecycle import ManagedSandboxLifecycle, SandboxLifecycleError
from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from pi_agent_core_py.web.state import TraceEventBuffer, WebRunRequest


@pytest.fixture
def app_client(tmp_path: Path):
    harness = AgentHarness(Agent(system_prompt="fixture", client=FakeClient([
        [TextDeltaEvent(delta="fixture answer"), DoneEvent(stop_reason="stop")],
    ])))
    app = create_app(harness, uploads_dir=str(tmp_path / "uploads"), db_path=None)
    with TestClient(app) as client:
        yield app, client


def test_delete_cleans_workspace_selections_and_replay_but_preserves_other_session(app_client):
    app, client = app_client
    sid = client.post("/api/sessions", json={"title": "remove"}).json()["id"]
    keep = client.post("/api/sessions", json={"title": "keep"}).json()["id"]
    prompted = client.post("/api/prompt", json={"text": "fixture", "session_id": sid})
    assert prompted.status_code == 200
    state = app.state.web

    async def seed():
        for session_id in (sid, keep):
            await state.extension_store.replace_workspace_extension_selection(
                session_id, mcp_server_names=[], skill_names=["shared-skill"], tool_names=[],
            )
        return await state.file_store.ensure_session_folder(sid)

    directory = client.portal.call(seed)
    state.request_history.append(WebRunRequest(id="gone", session_id=sid, status="completed"))
    state.request_history.append(WebRunRequest(id="kept", session_id=keep, status="completed"))
    state.event_buffer.append({"session_id": sid, "sequence": 100, "payload": "private"})
    state.event_buffer.append({"session_id": keep, "sequence": 101, "payload": "keep"})

    deleted = client.delete(f"/api/sessions/{sid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True
    assert not directory.exists()
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.get(f"/api/messages?session_id={sid}").status_code == 404
    assert client.get("/api/requests/gone").status_code == 404
    assert client.get("/api/requests/kept").status_code == 200
    assert all(event.get("session_id") != sid for event in state.event_buffer.list())
    assert any(event.get("session_id") == keep for event in state.event_buffer.list())
    assert app.state.coding_agent_runtime.get(sid) is None

    async def check():
        removed = await state.extension_store.get_workspace_extension_selection(sid)
        retained = await state.extension_store.get_workspace_extension_selection(keep)
        assert removed.skill_names == ()
        assert retained.skill_names == ("shared-skill",)

    client.portal.call(check)


def test_file_failure_keeps_session_and_binding_retryable(app_client, monkeypatch):
    app, client = app_client
    sid = client.post("/api/sessions", json={"title": "retry"}).json()["id"]
    original = app.state.web.file_store.delete_session_files

    async def fail(_sid):
        raise OSError("private-path-or-secret-marker")

    monkeypatch.setattr(app.state.web.file_store, "delete_session_files", fail)
    response = client.delete(f"/api/sessions/{sid}")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "session_cleanup_failed"
    assert "private-path-or-secret-marker" not in response.text
    assert client.get(f"/api/sessions/{sid}").status_code == 200
    monkeypatch.setattr(app.state.web.file_store, "delete_session_files", original)
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_execution_cleanup_debt_blocks_destructive_deletion(app_client, monkeypatch):
    app, client = app_client
    sid = client.post("/api/sessions", json={"title": "keep-until-clean"}).json()["id"]

    async def fail(_sid):
        raise RuntimeError("private-container-details")

    runtime = SimpleNamespace(prepare_session_deletion=fail)
    with monkeypatch.context() as patch:
        patch.setattr(app.state, "execution_runtime", runtime)
        result = client.delete(f"/api/sessions/{sid}")
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "session_cleanup_pending"
    assert "private-container-details" not in result.text
    assert client.get(f"/api/sessions/{sid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True


def test_replay_filter_keeps_sequence_watermarks():
    buffer = TraceEventBuffer()
    for session, seq in (("gone", 1), ("kept", 2), ("gone", 3), ("kept", 4)):
        buffer.append({"session_id": session, "sequence": seq})
    buffer.remove_session("gone")
    assert [event["sequence"] for event in buffer.list()] == [2, 4]
    assert (buffer.first_sequence, buffer.last_sequence) == (2, 4)
    buffer.remove_session("kept")
    assert buffer.first_sequence is None and buffer.last_sequence is None


@pytest.mark.asyncio
async def test_analysis_delete_is_not_limited_to_ui_page(tmp_path):
    workspace = WorkspaceStore(tmp_path / "uploads")
    await workspace.init()

    async def enabled(_sid):
        return True

    service = DataAnalysisService(tmp_path / "analysis", workspace, enabled)
    await service.init()
    try:
        db = service._database()
        directories = []
        for index in range(26):
            run_id = f"analysis-{index:032x}"
            sid = "remove" if index < 25 else "keep"
            await db.execute(
                "INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?)",
                (run_id, sid, "succeeded", '{"action":"inspect"}', "{}", None, 1, 1),
            )
            directory = service._directory(run_id)
            directory.mkdir()
            (directory / "result.csv").write_text("fixture")
            directories.append(directory)
        await db.commit()
        assert len(await service.list_runs("remove")) == 20
        await service.delete_session("remove")
        assert await service.list_runs("remove") == []
        assert all(not path.exists() for path in directories[:25])
        assert directories[25].is_dir()
        assert len(await service.list_runs("keep")) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_analysis_file_failure_preserves_cleanup_record(tmp_path, monkeypatch):
    workspace = WorkspaceStore(tmp_path / "uploads")
    await workspace.init()

    async def enabled(_sid):
        return True

    service = DataAnalysisService(tmp_path / "analysis", workspace, enabled)
    await service.init()
    try:
        run_id = "analysis-" + "a" * 32
        db = service._database()
        await db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?)", (
            run_id, "remove", "succeeded", '{"action":"inspect"}', "{}", None, 1, 1,
        ))
        await db.commit()
        service._directory(run_id).mkdir()

        def fail(_path):
            raise OSError("fixture disk failure")

        with monkeypatch.context() as patch:
            patch.setattr("coding_agent_app.data_analysis.service.shutil.rmtree", fail)
            with pytest.raises(OSError):
                await service.delete_session("remove")
        assert len(await service.list_runs("remove")) == 1
        await service.delete_session("remove")
        assert await service.list_runs("remove") == []
        assert not service._directory(run_id).exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_analysis_reparse_directory_is_not_deleted(tmp_path, monkeypatch):
    import stat

    workspace = WorkspaceStore(tmp_path / "uploads")
    await workspace.init()

    async def enabled(_sid):
        return True

    service = DataAnalysisService(tmp_path / "analysis", workspace, enabled)
    await service.init()
    try:
        run_id = "analysis-" + "b" * 32
        db = service._database()
        await db.execute("INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,?)", (
            run_id, "remove", "succeeded", '{"action":"inspect"}', "{}", None, 1, 1,
        ))
        await db.commit()
        directory = service._directory(run_id)
        directory.mkdir()
        (directory / "fixture.txt").write_text("must remain")
        original = Path.lstat

        def reparse(path):
            info = original(path)
            if path == directory:
                return SimpleNamespace(
                    st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return info

        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", reparse)
            with pytest.raises(AnalysisError, match="analysis_cleanup_unsafe_path"):
                await service.delete_session("remove")
        assert (directory / "fixture.txt").read_text() == "must remain"
        assert len(await service.list_runs("remove")) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_sandbox_action_cannot_start_for_deleting_session():
    async def unavailable(_sid):
        return False

    lifecycle = SimpleNamespace(_session_exists=unavailable)
    record = SimpleNamespace(session_id="deleted", operation_id="sandbox-" + "a" * 32)

    async def forbidden_action(_record):
        raise AssertionError("publication must not run")

    with pytest.raises(SandboxLifecycleError):
        await ManagedSandboxLifecycle._launch_job(
            lifecycle, record, status="publishing", event_type="fixture", action=forbidden_action,
        )

"""Mandatory confirmation and real lightweight Python execution (no model/network)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_workspace import WorkspaceStore
from agent_workspace.analysis import AnalysisError, PythonAnalysisRequest
from coding_agent_app.data_analysis.python_runtime import PythonRuntime
from coding_agent_app.data_analysis.service import DataAnalysisService
from pi_agent_core_py import (
    Agent,
    AgentHarness,
    AllowAllToolPermissionPolicy,
    DefaultToolPermissionPolicy,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
)
from pi_agent_core_py.telemetry import InMemoryTelemetryContext
from pi_agent_core_py.web.app import create_app, dispose_app


def _runtime() -> PythonRuntime:
    runtime = PythonRuntime()
    if not runtime.executable.is_file():
        if os.environ.get("CI"):
            pytest.fail("CI must install the dedicated Python analysis environment")
        pytest.skip("Run scripts/setup_analysis_python.py to exercise the real dedicated runtime")
    return runtime


async def _service(tmp_path, *, approval=None, runtime=None, **kwargs):
    store = WorkspaceStore(tmp_path / "files")
    await store.ensure_session_workspace("one")
    ref = await store.save(
        "one",
        UploadFile(
            filename="sales.csv",
            file=io.BytesIO(
                b"id,region,sales\n001,East,10\n002,West,20\n003,East,30\n",
            ),
        ),
    )
    instance = DataAnalysisService(
        tmp_path / "analysis",
        store,
        AsyncMock(return_value=False),
        python_enabled=AsyncMock(return_value=True),
        python_approval=approval,
        python_runtime=runtime or _runtime(),
        **kwargs,
    )
    await instance.init()
    return instance, store, ref


@pytest.mark.parametrize(
    "extra",
    [
        {"code": ""},
        {"code": "a" * 16001},
        {"approved": True},
        {"executable": sys.executable},
        {"file_id": "../other"},
        {"limit": 5001},
    ],
)
def test_python_request_cannot_choose_interpreter_or_self_approve(extra):
    with pytest.raises(ValidationError):
        PythonAnalysisRequest.model_validate({"file_id": "file-a", "code": "print(1)", **extra})


async def test_runtime_missing_or_web_interpreter_never_falls_back(tmp_path):
    assert not (await PythonRuntime(tmp_path / "missing.exe").capability())["available"]
    assert not (await PythonRuntime(Path(sys.executable)).capability())["available"]


async def test_real_python_dataframe_stdout_chart_export_and_audit(tmp_path):
    approval = AsyncMock(return_value=True)
    service, store, source = await _service(tmp_path, approval=approval)
    service.telemetry = telemetry = InMemoryTelemetryContext()
    code = (
        "df['sales'] = pd.to_numeric(df['sales'], errors='raise')\n"
        "result = df.groupby('region', as_index=False)['sales'].sum()\n"
        "print('总计', int(df['sales'].sum()))\n"
        "plt.bar(result['region'], result['sales'])\n"
    )
    try:
        result = await service.analyze("one", PythonAnalysisRequest(file_id=source.id, code=code))
        assert result["rows"] == [["East", 40], ["West", 20]]
        assert result["stdout"] == "总计 60\n"
        assert result["has_chart"] and result["code"] == code
        args = approval.await_args.args
        assert args[0] == "one" and args[3]["source_sha256"] == source.sha256
        assert args[3]["code_sha256"] == result["code_sha256"]
        chart = await service.output("one", result["run_id"], "chart.png")
        assert chart.read_bytes().startswith(b"\x89PNG")
        manifest = json.loads(
            (
                await service.output(
                    "one",
                    result["run_id"],
                    "manifest.json",
                )
            ).read_text(encoding="utf-8")
        )
        assert manifest["engine"] == "approved-python/v1"
        assert manifest["request"]["code"] == code
        saved = await service.save("one", result["run_id"])
        assert len(saved["files"]) == 4
        assert (await store.get_for_session("one", source.id)).sha256 == source.sha256
        assert approval.await_count == 1  # Saving does not execute code again.
        spans = repr(telemetry.get_spans())
        assert "run_python_analysis" in spans and "df.groupby" not in spans
    finally:
        await service.close()


@pytest.mark.parametrize("decision", ["deny", "abort", "timeout", "missing"])
async def test_no_execution_without_live_confirmation(tmp_path, decision):
    runtime = PythonRuntime(tmp_path / "never-executed.exe")
    runtime.capability = AsyncMock(return_value={"available": True})
    waiting = asyncio.Event()

    async def approve(*_args):
        waiting.set()
        if decision in {"abort", "timeout"}:
            await asyncio.Future()
        return False

    instance, _, source = await _service(
        tmp_path,
        approval=None if decision == "missing" else approve,
        runtime=runtime,
        approval_timeout_seconds=0.03 if decision == "timeout" else 600,
    )
    request = PythonAnalysisRequest(file_id=source.id, code="raise RuntimeError('never')")
    try:
        if decision == "missing":
            with pytest.raises(AnalysisError, match="python_approval_unavailable"):
                await instance.start("one", request)
            assert await instance.list_runs("one") == []
        elif decision == "abort":
            rid = await instance.start("one", request)
            await asyncio.wait_for(waiting.wait(), 2)
            assert (await instance.get("one", rid))["status"] == "awaiting_approval"
            assert not instance._directory(rid).exists()
            assert (await instance.cancel("one", rid))["status"] == "cancelled"
        else:
            expected = (
                "python_execution_denied" if decision == "deny" else "python_approval_timeout"
            )
            with pytest.raises(AnalysisError, match=expected):
                await instance.analyze("one", request)
    finally:
        await instance.close()


async def test_source_and_selection_rechecked_after_approval(tmp_path):
    runtime = PythonRuntime(tmp_path / "never-executed.exe")
    runtime.capability = AsyncMock(return_value={"available": True})
    instance, _, source = await _service(
        tmp_path, approval=AsyncMock(return_value=True), runtime=runtime
    )

    async def tamper(*_args):
        await asyncio.to_thread(Path(source.path).write_bytes, b"changed")
        return True

    instance._python_approval = tamper
    try:
        request = PythonAnalysisRequest(file_id=source.id, code="print('never')")
        with pytest.raises(AnalysisError, match="source_changed"):
            await instance.analyze("one", request)
        instance._python_approval = AsyncMock(return_value=True)
        instance._python_enabled = AsyncMock(side_effect=[True, False])
        with pytest.raises(AnalysisError, match="python_analysis_disabled"):
            await instance.analyze("one", request)
    finally:
        await instance.close()


async def test_python_errors_are_actionable_and_retry_requires_new_approval(tmp_path):
    approval = AsyncMock(return_value=True)
    instance, _, source = await _service(tmp_path, approval=approval)
    try:
        with pytest.raises(AnalysisError, match="python_execution_failed") as error:
            await instance.analyze(
                "one",
                PythonAnalysisRequest(
                    file_id=source.id,
                    code="print('starting')\nresult = df['missing']",
                ),
            )
        assert "KeyError" in error.value.details["python_error"]
        assert "analysis line 2" in error.value.details["python_error"]
        assert error.value.details["stdout"] == "starting\n"
        rid = error.value.details["run_id"]
        with pytest.raises(AnalysisError, match="analysis_not_succeeded"):
            await instance.save("one", rid)
        result = await instance.analyze(
            "one",
            PythonAnalysisRequest(
                file_id=source.id,
                code="result = df[['id']]\nprint('x' * 100000)",
                limit=2,
            ),
        )
        assert result["rows"] == [["001"], ["002"]] and result["limited"]
        assert len(result["stdout"]) < 16_500 and "truncated" in result["stdout"]
        assert approval.await_count == 2
    finally:
        await instance.close()


async def test_python_loop_timeout_and_restart_never_replays(tmp_path):
    instance, store, source = await _service(
        tmp_path,
        approval=AsyncMock(return_value=True),
        timeout_seconds=2,
    )
    try:
        with pytest.raises(AnalysisError, match="analysis_timeout"):
            await instance.analyze(
                "one",
                PythonAnalysisRequest(
                    file_id=source.id,
                    code="while True: pass",
                ),
            )
        rid = (await instance.list_runs("one"))[0]["id"]
        assert not instance._directory(rid).exists()
        await instance._update(rid, "awaiting_approval")
    finally:
        await instance.close()
    reopened = DataAnalysisService(tmp_path / "analysis", store, AsyncMock(return_value=False))
    await reopened.init()
    try:
        assert (await reopened.get("one", rid))["status"] == "interrupted"
        assert not reopened._tasks
    finally:
        await reopened.close()


def _wait(client, url, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.is_success, response.text
        data = response.json()
        if predicate(data):
            return data
        time.sleep(0.04)
    pytest.fail(f"Timed out waiting for {url}")


@pytest.mark.parametrize(
    "policy", [None, DefaultToolPermissionPolicy(), AllowAllToolPermissionPolicy()]
)
def test_real_web_agent_approval_binds_full_code_and_cannot_be_bypassed(tmp_path, policy):
    _runtime()
    code = "# " + "review all of this " * 80 + "\nresult = df[['id']]\nprint('approved once')"
    call = ToolCall(
        id="python-call", name="run_python_analysis", arguments={"file_id": "pending", "code": code}
    )
    fake = FakeClient(
        [
            [ToolCallEvent(tool_call=call), DoneEvent(stop_reason="tool_use")],
            [TextDeltaEvent(delta="Analysis ready"), DoneEvent(stop_reason="stop")],
        ]
    )
    app = create_app(
        AgentHarness(Agent(system_prompt="", client=fake), permission_policy=policy),
        db_path=tmp_path / "session.sqlite",
        uploads_dir=tmp_path / "uploads",
    )
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={"title": "Python"}).json()["id"]
        other = client.post("/api/sessions", json={"title": "Other"}).json()["id"]
        source = client.post(
            f"/api/sessions/{sid}/files",
            files={
                "files": ("table.csv", b"id,value\n001,2\n", "text/csv"),
            },
        ).json()["files"][0]
        call.arguments["file_id"] = source["id"]
        base = f"/api/workspaces/{sid}"
        enabled = client.put(base + "/extensions", json={"tool_names": ["run_python_analysis"]})
        assert enabled.is_success, enabled.text
        # The fixed calculation API cannot smuggle code into execution.
        assert (
            client.post(base + "/analysis", json={**call.arguments, "action": "python"}).status_code
            == 422
        )
        started = client.post(
            "/api/prompt/async",
            json={
                "session_id": sid,
                "text": "请使用 run_python_analysis 分析上传的数据",
            },
        ).json()
        req = started["request_id"]
        url = f"/api/requests/{req}/approvals"
        approval = _wait(client, url, lambda v: v["count"] > 0)["approvals"][0]
        assert approval["arguments"]["code"] == code  # Not 512-char generic truncation.
        assert approval["arguments"]["source_sha256"] == source["sha256"]
        assert approval["policy_name"] == "python_execution"
        assert "NOT a security sandbox" in approval["reason"]
        assert len(client.get(url).json()["approvals"]) == 1
        runs = client.get(base + "/analysis").json()["runs"]
        assert runs[0]["status"] == "awaiting_approval"
        run_url = base + "/analysis/" + runs[0]["id"]
        assert client.get(run_url).json()["result"] is None
        assert client.get(f"/api/workspaces/{other}/analysis/{runs[0]['id']}").status_code == 404
        assert client.get(f"/api/workspaces/{other}/extensions").json()["selected_tool_names"] == []
        decision = url + "/" + approval["approval_id"]
        response = client.post(decision, json={"decision": "approve", "code": "raise Exception()"})
        assert response.is_success
        assert client.post(decision, json={"decision": "approve"}).json()["idempotent"]
        result = _wait(client, run_url, lambda v: v["status"] in {"succeeded", "failed"})
        assert result["status"] == "succeeded", result
        assert result["result"]["code"] == code and result["result"]["rows"] == [["001"]]
        assert client.post(run_url + "/save").is_success
        _wait(client, f"/api/requests/{req}", lambda v: v["status"] in {"completed", "error"})
        assert any(tool.name == "run_python_analysis" for tool in fake.last_tools)
    dispose_app(app)

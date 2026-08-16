"""P2-B Human Approval Web integration tests."""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pi_agent_core_py import (
    Agent,
    AgentHarness,
    DefaultToolPermissionPolicy,
    DoneEvent,
    FakeClient,
    TextContent,
    TextDeltaEvent,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from pi_agent_core_py.tools import AgentTool, ToolResult
from pi_agent_core_py.web.app import create_app


class _DangerousTool(AgentTool):
    name = "shell"
    label = "Run shell command"
    description = "approval test tool"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, tool_call_id: str, args: dict[str, Any]) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(
            tool_call_id=tool_call_id,
            name=self.name,
            content=[TextContent(text="executed")],
        )


def _build_client(tmp_path):
    tool = _DangerousTool()
    scripts = [
        [
            ToolCallEvent(tool_call=ToolCall(
                id="call_shell",
                name="shell",
                arguments={
                    "command": "echo approved",
                    "api_key": "must-never-reach-browser",
                },
            )),
            DoneEvent(stop_reason="tool_use", usage=Usage()),
        ],
        [TextDeltaEvent(delta="finished"), DoneEvent(stop_reason="stop", usage=Usage())],
    ]
    agent = Agent(system_prompt="", client=FakeClient(scripts), tools=[tool])
    harness = AgentHarness(agent, permission_policy=DefaultToolPermissionPolicy())
    harness.attach_skills([])
    app = create_app(
        harness,
        db_path=str(tmp_path / "workspace.sqlite"),
        uploads_dir=str(tmp_path / "uploads"),
    )
    return TestClient(app), tool


def _wait_for_pending(client: TestClient, request_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/requests/{request_id}/approvals?status=pending"
        )
        if response.status_code == 200 and response.json()["count"]:
            return response.json()["approvals"][0]
        time.sleep(0.02)
    pytest.fail("approval did not become pending")


def _wait_for_terminal(client: TestClient, request_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/requests/{request_id}").json()
        if body.get("status") in {"completed", "error", "aborted"}:
            return body
        time.sleep(0.02)
    pytest.fail("request did not become terminal")


def test_approve_once_executes_and_emits_redacted_request_scoped_events(tmp_path) -> None:
    test_client, tool = _build_client(tmp_path)
    with test_client as client:
        started = client.post("/api/prompt/async", json={"text": "run it"})
        assert started.status_code == 202
        request_id = started.json()["request_id"]
        session_id = started.json()["session_id"]
        approval = _wait_for_pending(client, request_id)

        assert approval["tool_name"] == "shell"
        assert approval["tool_label"] == "Run shell command"
        assert approval["arguments"]["command"] == "echo approved"
        assert approval["arguments"]["api_key"] == "[REDACTED]"
        request = client.get(f"/api/requests/{request_id}").json()
        assert request["awaiting_approval"] is True
        assert request["pending_approval_count"] == 1

        resolved = client.post(
            f"/api/requests/{request_id}/approvals/{approval['approval_id']}",
            json={"decision": "approve"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["approval"]["status"] == "approved"
        assert resolved.json()["idempotent"] is False
        assert _wait_for_terminal(client, request_id)["status"] == "completed"
        assert tool.calls == [{
            "command": "echo approved",
            "api_key": "must-never-reach-browser",
        }]

        replay = client.get(
            "/api/events",
            params={"session_id": session_id, "request_id": request_id, "limit": 200},
        ).json()
        event_types = [event["type"] for event in replay["events"]]
        assert "tool_approval_requested" in event_types
        assert "tool_approval_resolved" in event_types

        repeated = client.post(
            f"/api/requests/{request_id}/approvals/{approval['approval_id']}",
            json={"decision": "approve"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["idempotent"] is True
        conflicting = client.post(
            f"/api/requests/{request_id}/approvals/{approval['approval_id']}",
            json={"decision": "deny"},
        )
        assert conflicting.status_code == 409


def test_deny_skips_tool_and_invalid_or_forged_resolution_is_safe(tmp_path) -> None:
    test_client, tool = _build_client(tmp_path)
    with test_client as client:
        started = client.post("/api/prompt/async", json={"text": "do not run"}).json()
        request_id = started["request_id"]
        approval = _wait_for_pending(client, request_id)

        invalid = client.post(
            f"/api/requests/{request_id}/approvals/{approval['approval_id']}",
            json={"decision": "always"},
        )
        assert invalid.status_code == 400
        forged = client.post(
            f"/api/requests/req_forged/approvals/{approval['approval_id']}",
            json={"decision": "approve"},
        )
        assert forged.status_code == 404

        denied = client.post(
            f"/api/requests/{request_id}/approvals/{approval['approval_id']}",
            json={"decision": "deny"},
        )
        assert denied.status_code == 200
        assert denied.json()["approval"]["status"] == "denied"
        assert _wait_for_terminal(client, request_id)["status"] == "completed"
        assert tool.calls == []


def test_abort_cancels_pending_approval_and_never_executes(tmp_path) -> None:
    test_client, tool = _build_client(tmp_path)
    with test_client as client:
        started = client.post("/api/prompt/async", json={"text": "abort"}).json()
        request_id = started["request_id"]
        approval = _wait_for_pending(client, request_id)

        aborted = client.post(
            f"/api/requests/{request_id}/abort",
            json={"reason": "test abort"},
        )
        assert aborted.status_code == 200
        assert _wait_for_terminal(client, request_id)["status"] == "aborted"
        all_approvals = client.get(
            f"/api/requests/{request_id}/approvals"
        ).json()["approvals"]
        assert all_approvals[0]["approval_id"] == approval["approval_id"]
        assert all_approvals[0]["status"] == "cancelled"
        assert tool.calls == []

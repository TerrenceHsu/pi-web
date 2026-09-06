"""Real Web approval/request/SQLite composition; Docker is explicit opt-in only."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coding_sandbox import FakeSandboxBackend, SandboxFileEntry
from coding_sandbox.admin import SandboxAdminConfig
from coding_sandbox.local_docker import LocalDockerExecutionConfig, LocalDockerSandboxBackend
from pi_agent_core_py import Agent, AgentHarness, DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.web.app import create_app
from tests.test_coding_sandbox_lifecycle import _fingerprint, _helper, _plan_role_call

HEADERS = {"X-PI-Agent-UI": "1"}


class E2BDouble(FakeSandboxBackend):
    """Same deterministic transport, but exercise the real E2B profile boundary."""

    def backend_name(self):
        return "e2b"

    async def create(self, spec):
        handle = await super().create(spec)
        return handle.model_copy(update={"provider": "e2b"})

    def _live_state(self, handle):
        return super()._live_state(handle.model_copy(update={"provider": "fake"}))

    async def destroy(self, handle):
        await super().destroy(handle.model_copy(update={"provider": "fake"}))

    stall_seed = False

    async def execute(self, handle, command, **kwargs):
        if self.stall_seed:
            await asyncio.sleep(100)
        return await super().execute(handle, command, **kwargs)


def client_for(tmp_path: Path, scripts=None, *, local_factory=None, local_config=None):
    backend = E2BDouble()
    model = FakeClient(scripts or [[TextDeltaEvent(delta="done"), DoneEvent(stop_reason="stop")]])
    app = create_app(
        AgentHarness(Agent(system_prompt="test", client=model)),
        db_path=tmp_path / "workspace.sqlite",
        uploads_dir=tmp_path / "uploads",
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
        enable_plan_mode=True,
        coding_sandbox_backend_factory=lambda _config, _secrets: backend,
        local_docker_config=local_config,
        local_docker_backend_factory=local_factory,
    )
    return TestClient(app, base_url="http://testserver", headers=HEADERS), backend


def configure(client: TestClient) -> str:
    sid = client.post("/api/sessions", json={"title": "Execution test"}).json()["id"]
    credential = client.post(
        "/api/credentials",
        json={
            "label": "Test",
            "storage_mode": "session_only",
            "secret_value": "test-not-real",
        },
    ).json()["credential"]["credential_id"]
    response = client.put(
        "/api/coding-sandbox/config",
        json={
            "expected_revision": 0,
            "config": SandboxAdminConfig(enabled=True, credential_id=credential).model_dump(
                mode="json"
            ),
        },
    )
    assert response.status_code == 200, response.text
    return sid


def start(client: TestClient, sid: str, *, plan=False) -> str:
    response = client.post(
        "/api/prompt/async",
        json={
            "text": "build",
            "session_id": sid,
            "coding_mode": True,
            "execution_mode": "plan" if plan else "direct",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def pending(client: TestClient, rid: str) -> dict[str, Any]:
    for _ in range(250):
        records = client.get(f"/api/requests/{rid}/approvals?status=pending").json()
        if records["count"]:
            return records["approvals"][0]
        status = client.get(f"/api/requests/{rid}").json()
        assert status["status"] not in {"completed", "error", "aborted"}, status
        time.sleep(0.02)
    pytest.fail("execution approval did not appear")


def terminal(client: TestClient, rid: str) -> dict[str, Any]:
    deadline = time.monotonic() + (120 if os.environ.get("PI_TEST_DOCKER_IMAGE") else 10)
    while time.monotonic() < deadline:
        record = client.get(f"/api/requests/{rid}").json()
        if record["status"] in {"completed", "error", "aborted"}:
            return record
        time.sleep(0.02)
    pytest.fail("execution request did not end")


def resolve(client, rid, card, decision="approve"):
    return client.post(
        f"/api/requests/{rid}/approvals/{card['approval_id']}", json={"decision": decision}
    )


@pytest.mark.parametrize(
    "decision", ["deny", "stop", "workspace", "config", "selection", "extensions", "expiry"]
)
def test_request_approval_rejects_changes_without_creating_runtime(tmp_path, decision):
    test_client, backend = client_for(tmp_path)
    with test_client as client:
        sid = configure(client)
        rid = start(client, sid)
        card = pending(client, rid)
        assert card["policy_name"] == "execution_task"
        args = card["arguments"]
        assert args["goal"] == "build" and args["data_location"] == "remote E2B"
        assert (
            "AGENT.md" in args["input_paths"] and args["limits"]["command_timeout_seconds"] == 300
        )
        assert not backend.created_specs
        for action in ("validate", "prepare-publish"):
            assert client.post(
                f"/api/coding-sandbox/operations/sandbox-{'a' * 32}/{action}"
            ).status_code == 409
        assert (
            client.post("/api/coding-sandbox/operations", json={"session_id": sid}).status_code
            == 409
        )
        # A guessed ID under a different request cannot resolve this approval.
        assert resolve(client, "other-request", card).status_code == 404
        if decision == "stop":
            client.post(f"/api/requests/{rid}/abort", json={})
        elif decision == "workspace":
            assert client.post(
                f"/api/sessions/{sid}/files",
                files={
                    "files": ("new.csv", b"x\n1\n", "text/csv"),
                },
            ).status_code in {200, 201}
        elif decision == "config":
            config = client.get("/api/coding-sandbox/config").json()
            config["config"]["enabled"] = False
            assert (
                client.put(
                    "/api/coding-sandbox/config",
                    json={
                        "expected_revision": config["revision"],
                        "config": config["config"],
                    },
                ).status_code
                == 200
            )
        elif decision == "selection":
            assert (
                client.put(
                    f"/api/workspaces/{sid}/execution",
                    json={
                        "backend": "disabled",
                        "expected_revision": 0,
                    },
                ).status_code
                == 200
            )
        elif decision == "extensions":
            assert (
                client.put(
                    f"/api/workspaces/{sid}/extensions",
                    json={
                        "mcp_server_names": [],
                        "skill_names": [],
                        "tool_names": [],
                    },
                ).status_code
                == 200
            )
        if decision == "expiry":
            grant_store = client.app.state.execution_runtime.runtime.store
            original_clock = grant_store._clock
            grant_store._clock = lambda: original_clock() + 601_000
        response = resolve(client, rid, card, "deny" if decision == "deny" else "approve")
        assert response.status_code == (200 if decision == "deny" else 409), response.text
        assert terminal(client, rid)["status"] in {"error", "aborted"}
        assert not backend.created_specs
        assert client.get(f"/api/requests/{rid}/approvals?status=pending").json()["count"] == 0


def test_stop_during_seed_cancels_start_and_cleans_known_runtime(tmp_path):
    test_client, backend = client_for(tmp_path)
    backend.stall_seed = True
    with test_client as client:
        sid = configure(client)
        rid = start(client, sid)
        card = pending(client, rid)
        assert resolve(client, rid, card).status_code == 200
        for _ in range(100):
            if backend.created_specs:
                break
            time.sleep(0.02)
        assert backend.created_specs
        began = time.monotonic()
        assert client.post(f"/api/requests/{rid}/abort", json={}).status_code == 200
        assert terminal(client, rid)["status"] == "aborted"
        assert time.monotonic() - began < 5
        assert backend.destroyed_sandbox_ids


def test_execution_selection_persists_and_cannot_enable_unconfigured_local(tmp_path):
    test_client, _ = client_for(tmp_path)
    with test_client as client:
        sid = configure(client)
        path = f"/api/workspaces/{sid}/execution"
        assert client.put(path, json={
            "backend": "local_docker", "expected_revision": 0,
        }).status_code == 409
        assert client.put(path, json={
            "backend": "disabled", "expected_revision": 0,
        }).status_code == 200
        assert client.put(path, json={
            "backend": "e2b", "expected_revision": 0,
        }).status_code == 409
        assert client.put(path, json={
            "backend": "e2b", "expected_revision": 1, "approved": True,
        }).status_code == 422
    next_client, _ = client_for(tmp_path)
    with next_client as client:
        value = client.get(path).json()
        assert value["backend"] == "disabled" and value["revision"] == 1


def seed_fake(client: TestClient, rid: str, backend: E2BDouble) -> tuple[SandboxFileEntry, ...]:
    task = client.app.state.execution_runtime._tasks[rid]
    snapshot = task.prepared.baseline.snapshot
    entries = tuple(
        SandboxFileEntry(path=e.path, size=e.size, sha256=e.sha256)
        for e in snapshot.manifest.entries
    )
    for value in [
        _fingerprint(()),
        *({"path": e.path, "size": e.size, "sha256": e.sha256, "created": True} for e in entries),
        _fingerprint(entries),
    ]:
        backend.queue_command_result(_helper(value))
    return entries


def test_coding_approved_task_binds_tools_and_destroys_on_no_change(tmp_path):
    scripts = [_plan_role_call("list", "coding_list_files", {}), [DoneEvent(stop_reason="stop")]]
    test_client, backend = client_for(tmp_path, scripts)
    with test_client as client:
        sid = configure(client)
        rid = start(client, sid)
        card = pending(client, rid)
        entries = seed_fake(client, rid, backend)
        listing = {
            "root": ".",
            "files": [e.model_dump(mode="json") for e in entries],
            "truncated": False,
        }
        for _ in range(5):
            backend.queue_command_result(_helper(listing))
        assert resolve(client, rid, card).status_code == 200
        result = terminal(client, rid)
        assert result["status"] == "error", result  # No mutation intentionally fails Coding.
        assert len(backend.created_specs) == 1
        assert backend.destroyed_sandbox_ids
        assert len(backend.executed_commands) > len(entries) + 2
        assert not client.app.state.execution_runtime._tasks
        rid2 = start(client, sid)
        card2 = pending(client, rid2)
        assert card2["arguments"]["task_id"] != card["arguments"]["task_id"]
        assert resolve(client, rid2, card2, "deny").status_code == 200
        terminal(client, rid2)


def plan_scripts():
    return [
        _plan_role_call(
            "plan",
            "plan_submit",
            {
                "goal": "build",
                "summary": "Build a small script",
                "tasks": [
                    {
                        "id": "implement",
                        "title": "Implement",
                        "objective": "Create main.py",
                        "dependencies": [],
                        "acceptance_criteria": ["Valid Python"],
                        "allowed_paths": ["scripts/main.py"],
                    }
                ],
            },
        ),
        _plan_role_call(
            "blocked",
            "plan_task_blocked",
            {
                "reason": "Missing requirement",
                "suggestions": ["Clarify the requirement"],
            },
        ),
    ]


def test_plan_combined_approval_binds_exact_version_and_no_legacy_bypass(tmp_path):
    test_client, backend = client_for(tmp_path, plan_scripts())
    with test_client as client:
        sid = configure(client)
        rid = start(client, sid, plan=True)
        card = pending(client, rid)
        args = card["arguments"]
        assert args["kind"] == "plan" and args["plan_version"] == 1
        assert args["plan"]["tasks"][0]["allowed_paths"] == ["scripts/main.py"]
        assert client.post(f"/api/plan-runs/{args['plan_id']}/approve").status_code == 409
        assert not backend.created_specs
        seed_fake(client, rid, backend)
        assert resolve(client, rid, card).status_code == 200
        terminal(client, rid)
        plan = client.get(f"/api/plan-runs/{args['plan_id']}").json()["plan"]
        assert plan["status"] == "blocked", plan
        assert len(backend.created_specs) == 1 and backend.destroyed_sandbox_ids


@pytest.mark.skipif(
    not os.environ.get("PI_TEST_DOCKER_IMAGE"), reason="explicit real Docker opt-in"
)
@pytest.mark.parametrize("plan", [False, True])
def test_real_docker_web_execution_retains_frozen_diff_after_cleanup(tmp_path, plan):
    """No model/network: deterministic Agent calls through the actual Web entry point."""
    from coding_sandbox.docker_transport import DockerCLITransport

    image = os.environ["PI_TEST_DOCKER_IMAGE"]
    config = LocalDockerExecutionConfig(enabled=True, image_id=image)
    transport_root = tmp_path / "docker-client"
    transport_root.mkdir()

    def local_factory(value):
        return LocalDockerSandboxBackend(
            config=value,
            transport=DockerCLITransport(
                executable=Path(os.environ["PI_TEST_DOCKER_EXE"]),
                config_directory=transport_root,
                endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
            ),
        )

    scripts = [
        _plan_role_call("list", "coding_list_files", {}),
        _plan_role_call(
            "write", "coding_write_file", {"path": "scripts/main.py", "content": "print('ok')\n"}
        ),
        [TextDeltaEvent(delta="Code written in the copy."), DoneEvent(stop_reason="stop")],
    ]
    if plan:
        scripts = [
            plan_scripts()[0],
            *scripts[:2],
            _plan_role_call(
                "done",
                "plan_task_complete",
                {
                    "summary": "Created script",
                    "changed_paths": ["scripts/main.py"],
                    "validation_summary": "Server will validate",
                },
            ),
            _plan_role_call(
                "verdict",
                "plan_verdict",
                {
                    "passed": True,
                    "reason": "Valid script",
                    "suggestions": [],
                    "classification": None,
                },
            ),
        ]
    test_client, backend = client_for(
        tmp_path, scripts, local_factory=local_factory, local_config=config
    )
    with test_client as client:
        sid = configure(client)
        assert (
            client.put(
                f"/api/workspaces/{sid}/execution",
                json={
                    "backend": "local_docker",
                    "expected_revision": 0,
                },
            ).status_code
            == 200
        )
        rid = start(client, sid, plan=plan)
        card = pending(client, rid)
        assert card["arguments"]["data_location"] == "local Docker"
        task = client.app.state.execution_runtime._tasks[rid]
        identity = task.prepared.scope.identity
        assert resolve(client, rid, card).status_code == 200
        result = terminal(client, rid)
        assert result["status"] == "completed", result
        operation = client.get(f"/api/coding-sandbox/sessions/{sid}/operation").json()["operation"]
        assert operation["status"] == "awaiting_approval", operation
        assert operation["changed_paths"] == ["scripts/main.py"]
        assert operation["publish_available"] is False
        assert not backend.created_specs  # Never fell back to E2B.
        grant = client.portal.call(client.app.state.execution_runtime.runtime.store.get, identity)
        assert grant.state == "closed" and not grant.cleanup_pending
        diff = client.get(f"/api/coding-sandbox/operations/{identity.operation_id}/diff")
        assert diff.status_code == 200, diff.text
        assert "scripts/main.py" in diff.text
        assert (
            client.post(
                f"/api/coding-sandbox/operations/{identity.operation_id}/publish"
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/coding-sandbox/operations/{identity.operation_id}/discard"
            ).status_code
            == 200
        )

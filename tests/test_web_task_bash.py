"""Coding/Plan Bash uses the existing grant and copy, never standalone approval."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coding_agent_app.execution.context import current_execution_context
from coding_sandbox.docker_transport import DockerCLITransport
from coding_sandbox.local_docker import LocalDockerSandboxBackend
from pi_agent_core_py import DoneEvent, TextDeltaEvent
from pi_agent_core_py.tools.bash import RunBashTool
from pi_agent_core_py.web.bash import WebBashTool
from tests.test_coding_sandbox_lifecycle import _plan_role_call as call
from tests.test_web_bash import history, make_client, select_bash
from tests.test_web_execution import pending, plan_scripts, resolve, seed_fake, start, terminal


def role_scripts(plan: bool):
    return ([plan_scripts()[0]] if plan else []) + [
        call("task-bash", "run_bash", {"script": "sleep 99"}),
        plan_scripts()[1] if plan else [DoneEvent(stop_reason="stop")],
    ]


@pytest.mark.parametrize("plan", [False, True])
def test_task_bash_approval_and_captured_adapter_cannot_borrow_request(tmp_path, plan):
    test_client, backend = make_client(tmp_path, scripts=role_scripts(plan))
    with test_client as client:
        sid = select_bash(client)
        rid = start(client, sid, plan=plan)
        card = pending(client, rid)
        assert card["arguments"]["kind"] == ("plan" if plan else "coding")
        assert card["arguments"]["task_bash_enabled"] is True
        assert "script" not in card["arguments"]
        execution = client.app.state.execution_runtime
        adapter = client.portal.call(execution.coding_bash_tool, sid, rid)
        assert isinstance(adapter, RunBashTool)
        assert isinstance(client.app.state.web.harness.agent.tools.get("run_bash"), WebBashTool)
        result = client.portal.call(adapter.execute, "outside", {"script": "true"})
        assert result.is_error and result.details["error_code"] == "grant_required"
        preview = client.portal.call(execution.coding_bash_tool, sid, None)
        result = client.portal.call(preview.execute, "preview", {"script": "true"})
        assert result.is_error and result.details["error_code"] == "execution_request_denied"
        assert resolve(client, rid, card, "deny").status_code == 200
        terminal(client, rid)
        result = client.portal.call(adapter.execute, "old", {"script": "true"})
        assert result.is_error and result.details["error_code"] == "execution_request_denied"
        assert not backend.created_specs and not backend.scripts
        assert history(client, sid) == []


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("action", ["stop", "tools", "backend"])
def test_task_bash_stop_and_revocation_close_shared_runtime(tmp_path, plan, action):
    test_client, backend = make_client(tmp_path, scripts=role_scripts(plan))
    backend.stall = True
    with test_client as client:
        sid = select_bash(client)
        rid = start(client, sid, plan=plan)
        card = pending(client, rid)
        seed_fake(client, rid, backend)
        prepared = client.app.state.execution_runtime._tasks[rid].prepared
        assert resolve(client, rid, card).status_code == 200
        for _ in range(300):
            if backend.scripts:
                break
            time.sleep(0.01)
        assert backend.scripts, client.get(f"/api/requests/{rid}").text
        approvals = client.get(f"/api/requests/{rid}/approvals").json()["approvals"]
        assert len(approvals) == 1 and approvals[0]["policy_name"] == "execution_task"
        if action == "stop":
            client.post(f"/api/requests/{rid}/abort", json={})
        elif action == "tools":
            client.put(f"/api/workspaces/{sid}/extensions", json={
                "mcp_server_names": [], "skill_names": [], "tool_names": [],
            })
        else:
            client.put(f"/api/workspaces/{sid}/execution", json={
                "backend": "disabled", "expected_revision": 1,
            })
        response = terminal(client, rid)
        if plan and action != "stop":
            # A completed chat turn can truthfully report a blocked Plan.
            run = client.get(f"/api/plan-runs/{card['arguments']['plan_id']}").json()["plan"]
            assert run["status"] == "blocked" and run["artifact_id"] is None
        else:
            assert response["status"] != "completed"
        grant = client.portal.call(
            client.app.state.execution_runtime.runtime.store.get, prepared.scope.identity,
        )
        assert grant.state != "active" and not grant.cleanup_pending
        assert len(backend.created_specs) == len(backend.destroyed_sandbox_ids) == 1
        assert len(backend.scripts) == 1
        assert not client.app.state.execution_runtime.bash_tasks
        assert history(client, sid) == []  # task logs must not pretend to be standalone runs
        model = client.app.state.web.harness.agent.client
        for prompt, tools in zip(model.all_system_prompt_calls, model.all_tools_calls, strict=True):
            names = {t.name for t in tools or []}
            if "You are the Planner" in prompt:
                assert "run_bash" not in names
            else:
                assert "run_bash" in names


@pytest.mark.parametrize("choice", ["unselected", "e2b", "disabled"])
def test_task_bash_selection_is_local_and_explicit(tmp_path, choice):
    # No approval or backend invocation is needed to inspect a task's tool offer.
    test_client, backend = make_client(tmp_path)
    with test_client as client:
        sid = select_bash(client)
        execution = client.app.state.execution_runtime
        if choice == "unselected":
            client.put(f"/api/workspaces/{sid}/extensions", json={
                "mcp_server_names": [], "skill_names": [], "tool_names": [],
            })
        else:
            # Trusted fixture seeds an unavailable backend choice; no E2B credentials/network.
            async def set_choice():
                await execution._db.execute(
                    "UPDATE web_execution_selection SET backend=?,revision=2 WHERE session_id=?",
                    (choice, sid),
                )
                await execution._db.commit()
            client.portal.call(set_choice)
        assert client.portal.call(execution.coding_bash_tool, sid, "request") is None
        assert not backend.created_specs and not backend.scripts


class TrackingDocker(LocalDockerSandboxBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.created = []
        self.destroyed = []
        self.bash_calls = []

    async def create(self, spec):
        handle = await super().create(spec)
        self.created.append(handle.sandbox_id)
        return handle

    async def execute_bash(self, handle, command, **kwargs):
        context = current_execution_context()
        result = await super().execute_bash(handle, command, **kwargs)
        self.bash_calls.append((handle.sandbox_id, context, result))
        return result

    async def destroy(self, handle):
        await super().destroy(handle)
        self.destroyed.append(handle.sandbox_id)


def mixed_scripts(plan: bool, valid: bool = True):
    steps = [
        call("list", "coding_list_files", {}),
        call("write-before", "coding_write_file", {
            "path": "scripts/main.py", "content": "print('before')\n",
        }),
        call("bash-fail", "run_bash", {
            "script": "test -f scripts/main.py; mkdir -p artifacts; "
                      "printf 'bash-created\\n' > artifacts/shared.txt; exit 7",
        }),
        call("argv-read", "coding_run", {"argv": ["python3", "-c",
            "from pathlib import Path; "
            "assert Path('artifacts/shared.txt').read_text() == 'bash-created\\n'; "
            "print('argv saw Bash file')"]}),
        call("write-after", "coding_write_file", {
            "path": "scripts/main.py", "content": "print('after')\n" + ("" if valid else "bad(\n"),
        }),
        call("bash-success", "run_bash", {
            "script": "grep -q after scripts/main.py && grep -q bash-created artifacts/shared.txt "
                      "&& printf 'same-copy-ok\\n'",
        }),
    ]
    if not plan:
        return [*steps, [TextDeltaEvent(delta="Task copy changed."), DoneEvent(stop_reason="stop")]]
    spec = call("plan", "plan_submit", {
        "goal": "build", "summary": "Implement and inspect shared task files", "tasks": [
            {"id": "implement", "title": "Implement", "objective": "Create script and report",
             "dependencies": [], "acceptance_criteria": ["Valid Python and shared report"],
             "allowed_paths": ["scripts/main.py", "artifacts/shared.txt"]},
            {"id": "refine", "title": "Refine", "objective": "Update script using the same copy",
             "dependencies": ["implement"],
             "acceptance_criteria": ["Updated script and old report"],
             "allowed_paths": ["scripts/main.py"]},
        ],
    })
    first_done = call("done-first", "plan_task_complete", {
        "summary": "Implemented script and report", "changed_paths": [
            "scripts/main.py", "artifacts/shared.txt"], "validation_summary": "Fixed checks follow",
    })
    first_verdict = call("verdict-first", "plan_verdict", {
        "passed": True, "reason": "First task files reviewed", "suggestions": [],
        "classification": None,
    })
    return [spec, *steps[:4], first_done,
        call("review-first", "coding_read_file", {"path": "artifacts/shared.txt"}), first_verdict,
        *steps[4:], call("done-second", "plan_task_complete", {
            "summary": "Updated existing script", "changed_paths": ["scripts/main.py"],
            "validation_summary": "Existing report remains readable; fixed checks follow",
        }), call("review", "coding_read_file", {"path": "scripts/main.py"}),
        call("verdict", "plan_verdict", {"passed": True, "reason": "Reviewed actual file",
            "suggestions": [], "classification": None})]


@pytest.mark.skipif(not os.environ.get("PI_TEST_DOCKER_IMAGE"), reason="explicit Docker opt-in")
@pytest.mark.parametrize("plan,valid", [(False, True), (True, True), (False, False)])
def test_real_task_bash_reuses_copy_after_failure_and_cannot_downgrade_evidence(
    tmp_path, plan, valid,
):
    client_root = tmp_path / "docker-client"
    client_root.mkdir()
    backends = []

    def factory(config):
        backend = TrackingDocker(config=config, transport=DockerCLITransport(
            executable=Path(os.environ["PI_TEST_DOCKER_EXE"]), config_directory=client_root,
            endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
        ))
        backends.append(backend)
        return backend

    test_client, unused = make_client(
        tmp_path, factory=factory, image=os.environ["PI_TEST_DOCKER_IMAGE"],
        scripts=mixed_scripts(plan, valid),
    )
    with test_client as client:
        sid = select_bash(client)
        rid = start(client, sid, plan=plan)
        card = pending(client, rid)
        assert card["arguments"]["task_bash_enabled"] is True
        task = client.app.state.execution_runtime._tasks[rid]
        identity = task.prepared.scope.identity
        assert not backends  # even the backend is resolved only after approval
        assert resolve(client, rid, card).status_code == 200
        outcome = terminal(client, rid)
        assert outcome["status"] == ("completed" if valid else "error"), outcome
        assert len(backends) == 1 and not unused.created_specs
        backend = backends[0]
        assert len(backend.created) == len(backend.destroyed) == 1
        assert len(backend.bash_calls) == 2
        assert [item[2].exit_code for item in backend.bash_calls] == [7, 0]
        assert backend.bash_calls[-1][2].stdout == "same-copy-ok\n"
        assert all(item[0] == backend.created[0] and item[1].identity == identity
                   and item[1].role == "executor" for item in backend.bash_calls)
        grant = client.portal.call(client.app.state.execution_runtime.runtime.store.get, identity)
        assert grant.state == "closed" and not grant.cleanup_pending and grant.commands_used >= 5
        async def commands():
            async with client.app.state.execution_runtime.runtime.store._db.execute(
                "SELECT command_json FROM execution_commands WHERE task_id=?", (identity.task_id,),
            ) as cursor:
                return [json.loads(row[0]) for row in await cursor.fetchall()]
        recorded = client.portal.call(commands)
        assert len(recorded) == grant.commands_used
        kinds = [row["intent"]["kind"] for row in recorded]
        assert kinds.count("bash") == 2 and kinds.count("edit") == 2 and kinds.count("argv") == 1
        argv = next(row for row in recorded if row["intent"]["kind"] == "argv")
        assert argv["exit_code"] == 0  # coding_run really read the file written by Bash
        approvals = client.get(f"/api/requests/{rid}/approvals").json()["approvals"]
        assert len(approvals) == 1 and approvals[0]["status"] == "approved"
        assert history(client, sid) == [] and not client.app.state.execution_runtime.bash_tasks
        record = client.get(f"/api/coding-sandbox/sessions/{sid}/operation").json()["operation"]
        evidence = record["validation"]
        assert evidence["passed"] is valid and evidence["source_path"] == ".pi-agent/sandbox.toml"
        assert kinds.count("validation") == sum(
            c["status"] != "not_run" for c in evidence["checks"]
        )
        assert client.post(
            f"/api/coding-sandbox/operations/{identity.operation_id}/publish",
        ).status_code == 409
        if not valid:
            # Request cleanup closes a failed validation operation; the failed
            # evidence remains, but there is no live repair task or artifact.
            assert record["status"] == "cancelled" and record["artifact_id"] is None
            assert any(c["status"] == "failed" for c in evidence["checks"])
            assert "shared.txt" not in client.get(f"/api/sessions/{sid}/files").text
            return
        assert record["status"] == "awaiting_approval" and not record["publish_available"]
        assert set(record["changed_paths"]) == {"scripts/main.py", "artifacts/shared.txt"}
        assert record["artifact_id"]
        assert evidence["operation_id"] == identity.operation_id
        assert evidence["workspace_revision"] == record["workspace_revision"]
        assert evidence["checks"] and all(c["status"] == "passed" for c in evidence["checks"])
        diff = client.get(f"/api/coding-sandbox/operations/{identity.operation_id}/diff")
        assert diff.status_code == 200 and "shared.txt" in diff.text and "after" in diff.text
        assert "shared.txt" not in client.get(f"/api/sessions/{sid}/files").text
        model = client.app.state.web.harness.agent.client
        for prompt, tools in zip(model.all_system_prompt_calls, model.all_tools_calls, strict=True):
            names = {t.name for t in tools or []}
            if "You are the Planner" in prompt or "independent Verifier" in prompt:
                assert "run_bash" not in names
            else:
                assert {"run_bash", "coding_run"} <= names
        assert client.post(
            f"/api/coding-sandbox/operations/{identity.operation_id}/discard",
        ).status_code == 200

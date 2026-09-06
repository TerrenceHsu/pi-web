"""Standalone Bash through actual Web composition; fake transport, no host shell."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent_app.intent_router import route_intent
from coding_sandbox import FakeSandboxBackend, SandboxCommandResult, SandboxFileEntry
from coding_sandbox.local_docker import LocalDockerExecutionConfig
from pi_agent_core_py import Agent, AgentHarness, DoneEvent, FakeClient, TextDeltaEvent
from pi_agent_core_py.policy import AllowAllToolPermissionPolicy, DefaultToolPermissionPolicy
from pi_agent_core_py.web.app import create_app
from tests.test_coding_sandbox_lifecycle import _fingerprint, _helper, _plan_role_call
from tests.test_web_execution import HEADERS, pending, resolve, terminal


class LocalDouble(FakeSandboxBackend):
    exit_code = 0
    stall = False

    def __init__(self):
        super().__init__()
        self.scripts = []

    def backend_name(self):
        return "local_docker"

    async def create(self, spec):
        value = await super().create(spec)
        return value.model_copy(update={"provider": "local_docker"})

    def _live_state(self, handle):
        return super()._live_state(handle.model_copy(update={"provider": "fake"}))

    async def destroy(self, handle):
        await super().destroy(handle.model_copy(update={"provider": "fake"}))

    async def execute_bash(self, handle, command, *, script, signal=None, on_output=None):
        self.scripts.append((script, command.cwd))
        if self.stall:
            await asyncio.sleep(100)
        return SandboxCommandResult(
            command_id=command.command_id,
            exit_code=self.exit_code,
            stdout="中文 output\n",
            stderr="error\n" if self.exit_code else "",
            started_at_ms=1,
            finished_at_ms=2,
        )


SCRIPT = "# " + "审阅" * 800 + "\nprintf 'hello\\n'\n"


def make_client(
    tmp_path: Path,
    *,
    script=SCRIPT,
    factory=None,
    image=None,
    scripts=None,
    policy=None,
):
    backend = LocalDouble()
    model = FakeClient(
        scripts
        or [
            _plan_role_call(
                "bash-call",
                "run_bash",
                {
                    "script": script,
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "reason": "Inspect copy",
                },
            ),
            [TextDeltaEvent(delta="Finished."), DoneEvent(stop_reason="stop")],
        ]
    )
    harness = AgentHarness(Agent(system_prompt="test", client=model))
    # Even a permissive host policy cannot replace the exact service approval.
    harness.set_permission_policy(policy or AllowAllToolPermissionPolicy())
    app = create_app(
        harness,
        db_path=tmp_path / "db.sqlite",
        uploads_dir=tmp_path / "uploads",
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver",),
        enable_trusted_host=True,
        enable_intent_routing=True,
        enable_plan_mode=True,
        local_docker_config=LocalDockerExecutionConfig(
            enabled=True,
            image_id=image or "sha256:" + "f" * 64,
        ),
        local_docker_backend_factory=factory or (lambda _: backend),
    )
    return TestClient(app, base_url="http://testserver", headers=HEADERS), backend


def select_bash(client):
    sid = client.post("/api/sessions", json={"title": "Bash"}).json()["id"]
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
    assert (
        client.put(
            f"/api/workspaces/{sid}/extensions",
            json={
                "mcp_server_names": [],
                "skill_names": [],
                "tool_names": ["run_bash"],
            },
        ).status_code
        == 200
    )
    return sid


def start_bash(client, sid, **fields):
    value = client.post(
        "/api/prompt/async",
        json={
            "text": "run_bash 检查副本",
            "session_id": sid,
            **fields,
        },
    )
    assert value.status_code == 202, value.text
    return value.json()["request_id"]


def seed(client, rid, backend):
    prepared = client.app.state.execution_runtime.bash_tasks[rid]
    entries = tuple(
        SandboxFileEntry(path=e.path, size=e.size, sha256=e.sha256)
        for e in prepared.baseline.snapshot.manifest.entries
    )
    for payload in [
        _fingerprint(()),
        *({"path": e.path, "size": e.size, "sha256": e.sha256, "created": True} for e in entries),
        _fingerprint(entries),
        {"root": ".", "files": [e.model_dump() for e in entries], "truncated": False},
    ]:
        backend.queue_command_result(_helper(payload))
    return prepared


def history(client, sid):
    rows = client.get(f"/api/workspaces/{sid}/bash-runs").json()["runs"]
    return [client.get(f"/api/workspaces/{sid}/bash-runs/{r['run_id']}").json() for r in rows]


@pytest.mark.parametrize("decision", ["deny", "stop", "workspace", "selection", "tools", "expiry"])
def test_exact_approval_denial_and_stale_never_create(tmp_path, decision):
    test_client, backend = make_client(tmp_path)
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid)
        card = pending(client, rid)
        args = card["arguments"]
        assert args["kind"] == "bash" and args["script"] == SCRIPT
        assert args["cwd"] == "." and args["max_commands"] == 1
        assert args["timeout_seconds"] == 60 and args["network"]["mode"] == "none"
        assert not backend.created_specs and not backend.scripts
        assert resolve(client, "wrong-request", card).status_code == 404
        if decision == "stop":
            aborted = client.post(f"/api/requests/{rid}/abort", json={})
            assert aborted.status_code == 200, aborted.text
            assert aborted.json()["ok"], aborted.text
        elif decision == "selection":
            client.put(
                f"/api/workspaces/{sid}/execution",
                json={
                    "backend": "disabled",
                    "expected_revision": 1,
                },
            )
        elif decision == "tools":
            client.put(
                f"/api/workspaces/{sid}/extensions",
                json={
                    "mcp_server_names": [],
                    "skill_names": [],
                    "tool_names": [],
                },
            )
        elif decision == "workspace":
            client.post(f"/api/sessions/{sid}/files", files={"files": ("new.txt", b"new")})
            assert resolve(client, rid, card).status_code == 409
        elif decision == "expiry":
            runtime = client.app.state.execution_runtime.runtime
            old_clock = runtime.store._clock
            runtime.store._clock = lambda: old_clock() + 700_000
            assert resolve(client, rid, card).status_code == 409
        else:
            assert resolve(client, rid, card, "deny").status_code == 200
        terminal(client, rid)
        assert not backend.created_specs and not backend.scripts
        assert history(client, sid)[0]["status"] in {"denied", "cancelled", "interrupted"}


@pytest.mark.parametrize("exit_code", [0, 7])
def test_single_script_output_history_isolation_and_new_copy(tmp_path, exit_code):
    scripts = [
        _plan_role_call("bash-one", "run_bash", {"script": "true"}),
        _plan_role_call("bash-two", "run_bash", {"script": "false"}),
        [DoneEvent(stop_reason="stop")],
    ]
    test_client, backend = make_client(tmp_path, scripts=scripts)
    backend.exit_code = exit_code
    with test_client as client:
        sid = select_bash(client)
        other = client.post("/api/sessions", json={"title": "Other"}).json()["id"]
        rid = start_bash(client, sid)
        task_ids = []
        for index in range(2):
            card = pending(client, rid)
            task_ids.append(card["arguments"]["task_id"])
            prepared = seed(client, rid, backend)
            assert resolve(client, rid, card).status_code == 200
            # Wait for this script to close before the next approval/terminal.
            for _ in range(250):
                rows = history(client, sid)
                if sum(r["status"] in {"succeeded", "failed"} for r in rows) > index:
                    break
                time.sleep(0.01)
            grant = client.portal.call(
                client.app.state.execution_runtime.runtime.store.get,
                prepared.scope.identity,
            )
            assert grant.state == "closed" and not grant.cleanup_pending
        assert task_ids[0] != task_ids[1]
        terminal(client, rid)
        rows = history(client, sid)
        assert len(rows) == len(backend.created_specs) == len(backend.destroyed_sandbox_ids) == 2
        for row in rows:
            assert row["status"] == ("succeeded" if exit_code == 0 else "failed"), row
            assert row["result"]["stdout"] == "中文 output\n"
            assert row["result"]["exit_code"] == exit_code
            assert row["result"]["changed_count"] == 0
            assert row["result"]["file_changes_saved"] is False
            assert (
                client.get(
                    f"/api/workspaces/{other}/bash-runs/{row['run_id']}",
                ).status_code
                == 404
            )
        assert client.get(f"/api/workspaces/{other}/bash-runs").json()["runs"] == []
    # Restart is a read of private history, never replay or recreation.
    reopened, unused = make_client(tmp_path)
    with reopened as client:
        assert len(history(client, sid)) == 2
        assert not unused.created_specs


def test_stop_running_script_releases_grant(tmp_path):
    test_client, backend = make_client(tmp_path)
    backend.stall = True
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid)
        card = pending(client, rid)
        prepared = seed(client, rid, backend)
        resolve(client, rid, card)
        for _ in range(250):
            if backend.scripts:
                break
            time.sleep(0.01)
        assert backend.scripts
        client.post(f"/api/requests/{rid}/abort", json={})
        assert terminal(client, rid)["status"] == "aborted"
        grant = client.portal.call(
            client.app.state.execution_runtime.runtime.store.get,
            prepared.scope.identity,
        )
        assert grant.state != "active" and not grant.cleanup_pending
        assert backend.destroyed_sandbox_ids


def test_strict_read_only_has_no_bash_even_when_selected(tmp_path):
    test_client, backend = make_client(tmp_path, scripts=[[DoneEvent(stop_reason="stop")]])
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid, intent_mode="read_only")
        terminal(client, rid)
        tools = client.app.state.web.harness.agent.client.last_tools
        assert "run_bash" not in {t.name for t in tools}
        assert not backend.created_specs


@pytest.mark.parametrize("denied", [False, True])
def test_default_policy_uses_one_exact_approval_and_preserves_explicit_deny(tmp_path, denied):
    policy = DefaultToolPermissionPolicy(denied_tools={"run_bash"} if denied else set())
    test_client, backend = make_client(tmp_path, policy=policy)
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid)
        if not denied:
            card = pending(client, rid)
            assert card["policy_name"] == "execution_task"
            assert card["arguments"]["script"] == SCRIPT
            resolve(client, rid, card, "deny")
        terminal(client, rid)
        assert "run_bash" not in policy.allowed_tools
        assert not backend.created_specs and not backend.scripts
        if denied:
            assert history(client, sid) == []


@pytest.mark.parametrize(
    "text,mode,route",
    [
        ("run_bash 检查副本", "auto", "bash"),
        ("用 bash 运行命令", "auto", "bash"),
        ("如何使用 bash", "auto", "read_only"),
        ("不要执行 bash", "auto", "read_only"),
        ("run_bash true", "read_only", "read_only"),
        ("execute script", "bash", "bash"),
    ],
)
def test_bash_route_is_explicit_and_never_read_only(text, mode, route):
    assert route_intent(text, intent_mode=mode).route == route
    assert route_intent(text, intent_mode=mode, knowledge_bound=True).route == "knowledge"


@pytest.mark.parametrize(
    "args",
    [
        {"script": "true", "approved": True},
        {"script": "true", "cwd": "../"},
        {"script": "x" * 16385},
        {"script": "bad\0script"},
        {"script": "true", "timeout_seconds": 301},
        {"script": "true", "cwd": "absent"},
    ],
)
def test_invalid_or_nonexistent_scope_cannot_create_runtime(tmp_path, args):
    test_client, backend = make_client(
        tmp_path,
        scripts=[
            _plan_role_call("bad", "run_bash", args),
            [DoneEvent(stop_reason="stop")],
        ],
    )
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid)
        terminal(client, rid)
        assert not backend.created_specs and not backend.scripts
        assert not client.get(f"/api/requests/{rid}/approvals?status=pending").json()["count"]


def test_missing_workspace_selection_never_exposes_bash(tmp_path):
    test_client, backend = make_client(tmp_path, scripts=[[DoneEvent(stop_reason="stop")]])
    with test_client as client:
        sid = client.post("/api/sessions", json={"title": "Disabled"}).json()["id"]
        rid = start_bash(client, sid)
        terminal(client, rid)
        tools = client.app.state.web.harness.agent.client.last_tools
        assert "run_bash" not in {t.name for t in tools}
        assert not backend.created_specs


def test_timeout_is_not_success_and_closes_execution(tmp_path):
    test_client, backend = make_client(
        tmp_path,
        scripts=[
            _plan_role_call("timeout", "run_bash", {"script": "sleep 99", "timeout_seconds": 1}),
            [DoneEvent(stop_reason="stop")],
        ],
    )
    backend.stall = True
    with test_client as client:
        sid = select_bash(client)
        rid = start_bash(client, sid)
        card = pending(client, rid)
        prepared = seed(client, rid, backend)
        resolve(client, rid, card)
        terminal(client, rid)
        record = history(client, sid)[0]
        assert record["status"] == "interrupted", record
        assert record["result"]["termination_reason"] == "timed_out"
        assert record["result"]["exit_code"] is None
        grant = client.portal.call(
            client.app.state.execution_runtime.runtime.store.get,
            prepared.scope.identity,
        )
        assert grant.state != "active" and not grant.cleanup_pending
        assert backend.destroyed_sandbox_ids


@pytest.mark.skipif(not os.environ.get("PI_TEST_DOCKER_IMAGE"), reason="explicit Docker opt-in")
def test_real_standalone_bash_reads_upload_freezes_then_publishes_exact_artifact(tmp_path):
    from coding_sandbox.docker_transport import DockerCLITransport
    from coding_sandbox.local_docker import LocalDockerSandboxBackend

    config_root = tmp_path / "docker-config"
    config_root.mkdir()

    def factory(config):
        return LocalDockerSandboxBackend(
            config=config,
            transport=DockerCLITransport(
                executable=Path(os.environ["PI_TEST_DOCKER_EXE"]),
                config_directory=config_root,
                endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
            ),
        )

    test_client, unused = make_client(
        tmp_path,
        factory=factory,
        image=os.environ["PI_TEST_DOCKER_IMAGE"],
        script="cat upload/data.txt; mkdir -p artifacts; printf 'copy only' > artifacts/result.txt",
        policy=DefaultToolPermissionPolicy(),
    )
    with test_client as client:
        sid = select_bash(client)
        client.post(f"/api/sessions/{sid}/files", files={"files": ("data.txt", b"real Docker\n")})
        rid = start_bash(client, sid)
        card = pending(client, rid)
        prepared = client.app.state.execution_runtime.bash_tasks[rid]
        assert resolve(client, rid, card).status_code == 200
        assert terminal(client, rid)["status"] == "completed"
        row = history(client, sid)[0]
        assert row["status"] == "succeeded", row
        assert row["result"]["stdout"] == "real Docker\n"
        assert row["result"]["changed_paths"] == ["artifacts/result.txt"]
        assert row["result"]["file_changes_saved"] is True
        assert "result.txt" not in client.get(f"/api/sessions/{sid}/files").text
        grant = client.portal.call(
            client.app.state.execution_runtime.runtime.store.get,
            prepared.scope.identity,
        )
        assert grant.state == "closed" and not grant.cleanup_pending
        assert not unused.created_specs
        operation = client.get(f"/api/coding-sandbox/sessions/{sid}/operation").json()["operation"]
        assert operation["validation"] is None
        assert operation["bash_evidence"]["schema_version"] == "bash-output-integrity/v1"
        assert operation["bash_evidence"]["command_id"] == row["result"]["command_id"]
        op = operation["operation_id"]
        endpoint = f"/api/coding-sandbox/operations/{op}/publish"
        assert client.post(endpoint).status_code == 409
        receipt = {k: operation[k] for k in ("artifact_id", "artifact_sha256", "review_sha256")}
        assert client.post(endpoint, json={**receipt, "review_sha256": "f" * 64}).status_code == 409
        assert client.post(endpoint, json=receipt).status_code == 202
        final = client.portal.call(client.app.state.execution_runtime._lifecycle.join_action, op)
        assert final.status == "published", final
        assert "result.txt" in client.get(f"/api/sessions/{sid}/files").text
        assert (
            client.post(endpoint, json=receipt).json()["publish_transaction_id"]
            == final.publish_transaction_id
        )


@pytest.mark.skipif(not os.environ.get("PI_TEST_DOCKER_IMAGE"), reason="explicit Docker opt-in")
@pytest.mark.parametrize(
    "script,expected",
    [
        ("true", "succeeded"),
        ("mkdir -p artifacts; echo unsafe > artifacts/result.txt; exit 7", "failed"),
        ("echo changed > AGENT.md; mkdir -p artifacts; echo ok > artifacts/result.txt", "failed"),
        (
            "mkdir -p artifacts; echo ok > artifacts/result.txt; ln -s result.txt artifacts/link",
            "failed",
        ),
        (
            "mkdir -p artifacts; echo ok > artifacts/result.txt; "
            "ln artifacts/result.txt artifacts/link",
            "failed",
        ),
    ],
)
def test_real_bash_unsafe_failed_or_unchanged_output_has_no_publication(tmp_path, script, expected):
    from coding_sandbox.docker_transport import DockerCLITransport
    from tests.test_web_task_bash import TrackingDocker

    config_root = tmp_path / "docker"
    config_root.mkdir()
    backends = []

    def factory(config):
        backend = TrackingDocker(
            config=config,
            transport=DockerCLITransport(
                executable=Path(os.environ["PI_TEST_DOCKER_EXE"]),
                config_directory=config_root,
                endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
            ),
        )
        backends.append(backend)
        return backend

    test_client, unused = make_client(
        tmp_path,
        factory=factory,
        image=os.environ["PI_TEST_DOCKER_IMAGE"],
        script=script,
    )
    with test_client as client:
        sid = select_bash(client)
        before = client.portal.call(client.app.state.web.file_store.inspect_workspace_revision, sid)
        rid = start_bash(client, sid)
        card = pending(client, rid)
        prepared = client.app.state.execution_runtime.bash_tasks[rid]
        resolve(client, rid, card)
        terminal(client, rid)
        row = history(client, sid)[0]
        assert row["status"] == expected, row
        assert not row["result"]["file_changes_saved"] and not row["result"]["publish_required"]
        grant = client.portal.call(
            client.app.state.execution_runtime.runtime.store.get, prepared.scope.identity
        )
        assert grant.state == "closed" and not grant.cleanup_pending
        executed = [b for b in backends if b.created]
        assert len(executed) == 1 and len(executed[0].created) == len(executed[0].destroyed) == 1
        assert not unused.created_specs
        assert (
            client.portal.call(client.app.state.web.file_store.inspect_workspace_revision, sid)
            == before
        )
        assert client.post(
            f"/api/coding-sandbox/operations/{prepared.scope.identity.operation_id}/publish",
        ).status_code in {404, 409}

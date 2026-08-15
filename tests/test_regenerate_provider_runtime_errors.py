"""Regenerate × Provider Runtime error mapping tests for M1-6（spec §三 39-51 + §五）.

覆盖：
- Profile missing → provider_profile_unavailable
- Profile disabled → provider_profile_disabled
- Credential missing / Secret unavailable → provider_credential_unavailable
- ProviderDefinition missing / Factory failure → provider_initialization_failed
- revision 标记为 error
- 错误时 Agent 不执行（harness.run_continue 调用次数 = 0）
- 错误时不 fallback 到 legacy client
- 错误信息固定安全（不泄漏 profile/model/credential/secret）
- 失败后可再次 Regenerate
- running revision 被标记为 error，不留 running 状态
- harness state 在失败后被 reset

Regenerate 是 async（POST 返回 202，错误在 background task 中映射）：
- request.status == "error"
- revision.status == "error"，error_summary = "PromptRuntimeError: <fixed msg>"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pi_agent_core_py import (  # noqa: E402
    Agent,
    AgentHarness,
    DoneEvent,
    FakeClient,
    TextDeltaEvent,
    Usage,
)
from pi_agent_core_py.providers.base import ProviderAdapter, ProviderRequest  # noqa: E402
from pi_agent_core_py.providers.errors import ProviderConfigError  # noqa: E402
from pi_agent_core_py.providers.registry import (  # noqa: E402
    _DEFAULT_REGISTRY,
    ProviderDefinition,
)
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.runtime import (  # noqa: E402
    RequestProviderRuntime,
)

# asyncio_mode=auto in pyproject.

_UI = {"X-PI-Agent-UI": "1"}
SECRET_MARKER = "sk-M1-6-ERROR-PATH-SECRET-MARKER"


# ============================================================================
# Setup
# ============================================================================


def _harness() -> AgentHarness:
    one = [
        TextDeltaEvent(delta="legacy"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]
    scripts = [list(one) for _ in range(30)]
    client = FakeClient(scripts)
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


def _make_factory(
    *,
    fail: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        calls.append(
            {"provider_id": provider_definition.id, "api_key": api_key, "model_id": model_id}
        )
        if fail:
            raise ProviderConfigError("simulated factory failure")

        class _A(ProviderAdapter):
            def __init__(self_) -> None:
                self_.provider_id = provider_definition.id
                self_.model = model_id

            async def stream(self_, request: ProviderRequest) -> Any:  # pragma: no cover
                yield  # type: ignore[unreachable]

        return _A()

    return factory, calls


def _install_runtime(app: object, *, factory: Any) -> RequestProviderRuntime:
    runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=factory,
    )
    app.state.request_provider_runtime = runtime
    return runtime


@pytest.fixture
async def env(tmp_path: Path) -> tuple[TestClient, object]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as client:
        yield client, app  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": "L", "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient,
    *,
    provider_id: str,
    credential_id: str,
    default_model: str = "m",
    name: str = "P",
) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": name,
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": default_model,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["profile"]["id"]


def _make_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"title": "T"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _send_prompt(c: TestClient, sid: str, text: str = "q") -> None:
    r = c.post("/api/prompt", json={"text": text, "session_id": sid})
    assert r.status_code == 200, r.text


def _bind(c: TestClient, *, session_id: str, profile_id: str, model_id: str = "m") -> None:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": model_id},
    )
    assert r.status_code == 200, r.text


async def _latest_assistant_id(c: TestClient, sid: str) -> str:
    state = c.app.state.web
    db = state.session_store.connection
    cur = await db.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant' "
        "ORDER BY idx DESC LIMIT 1",
        (sid,),
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    return row["id"]


def _regenerate(c: TestClient, *, sid: str, aid: str) -> str:
    r = c.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert r.status_code == 202, r.text
    return r.json()["request_id"]


def _wait(c: TestClient, request_id: str) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        r = c.get(f"/api/requests/{request_id}")
        if r.status_code == 200:
            body = r.json()
            if body["status"] in ("completed", "error", "aborted"):
                return body
        time.sleep(0.02)
    pytest.fail(f"request {request_id} never reached terminal")


async def _fetch_revisions_raw(c: TestClient, sid: str, aid: str) -> list[dict[str, Any]]:
    state = c.app.state.web
    db = state.extension_store._require_db()
    cur = await db.execute(
        "SELECT id, revision_number, status, content_json, error_summary, request_id "
        "FROM web_message_revisions "
        "WHERE session_id = ? AND assistant_message_id = ? "
        "ORDER BY revision_number ASC",
        (sid, aid),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def _latest_revision(c: TestClient, sid: str, aid: str) -> dict[str, Any]:
    revs = await _fetch_revisions_raw(c, sid, aid)
    return revs[-1]


async def _seed_for_regenerate(
    client: TestClient,
    *,
    provider_id: str = "qwen",
    credential_id: str | None = None,
    secret: str = SECRET_MARKER,
    model_id: str = "m",
) -> tuple[str, str, str]:
    """Seed session + prompt (legacy) + bind. Returns (sid, aid, profile_id)."""
    if credential_id is None:
        credential_id = _seed_credential(client, secret)
    prof = _seed_profile(client, provider_id=provider_id, credential_id=credential_id)
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id=model_id)
    return sid, aid, prof


# ============================================================================
# Profile missing → provider_profile_unavailable
# ============================================================================


async def test_profile_missing_marks_revision_error(env: tuple) -> None:
    """Binding points to deleted Profile → regenerate fails, revision marked error."""
    client, app = env
    factory, _ = _make_factory()
    _install_runtime(app, factory=factory)

    sid, aid, prof = await _seed_for_regenerate(client)

    # Simulate profile missing——patch service to raise NotFound
    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    from pi_agent_core_py.web.providers.config_store import (
        ProviderProfileNotFoundError,
    )

    async def _raise_not_found(profile_id: str) -> Any:
        raise ProviderProfileNotFoundError("simulated: profile gone")

    runtime._provider_config_service.get_profile = _raise_not_found  # type: ignore[assignment]

    req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait(client, req_id)

    assert final["status"] == "error"
    assert "Selected provider profile is unavailable." in final.get("error", "")

    rev = await _latest_revision(client, sid, aid)
    assert rev["status"] == "error"
    assert "Selected provider profile is unavailable." in (rev["error_summary"] or "")


# ============================================================================
# Profile disabled → provider_profile_disabled
# ============================================================================


async def test_profile_disabled_marks_revision_error(env: tuple) -> None:
    client, app = env
    factory, _ = _make_factory()
    _install_runtime(app, factory=factory)

    sid, aid, prof = await _seed_for_regenerate(client)

    svc = app.state.provider_config_runtime.service
    await svc.update_profile(prof, enabled=False)

    req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait(client, req_id)

    assert final["status"] == "error"
    assert "Selected provider profile is disabled." in final.get("error", "")

    rev = await _latest_revision(client, sid, aid)
    assert rev["status"] == "error"
    assert "Selected provider profile is disabled." in (rev["error_summary"] or "")


# ============================================================================
# Credential missing → provider_credential_unavailable
# ============================================================================


async def test_credential_missing_marks_revision_error(env: tuple) -> None:
    client, app = env
    factory, _ = _make_factory()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    sid, aid, _ = await _seed_for_regenerate(client, credential_id=cred)

    # Delete credential——profile still references it
    client.delete(f"/api/credentials/{cred}", headers=_UI)

    req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait(client, req_id)

    assert final["status"] == "error"
    assert "Selected provider credential is unavailable." in final.get("error", "")

    rev = await _latest_revision(client, sid, aid)
    assert rev["status"] == "error"
    assert "Selected provider credential is unavailable." in (rev["error_summary"] or "")


# ============================================================================
# Factory failure → provider_initialization_failed
# ============================================================================


async def test_factory_failure_marks_revision_error(env: tuple) -> None:
    client, app = env
    factory, calls = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait(client, req_id)

    assert final["status"] == "error"
    assert "Selected provider could not be initialized." in final.get("error", "")

    rev = await _latest_revision(client, sid, aid)
    assert rev["status"] == "error"
    assert "Selected provider could not be initialized." in (rev["error_summary"] or "")

    # Factory was actually invoked
    assert len(calls) == 1


# ============================================================================
# ProviderDefinition missing → provider_initialization_failed
# ============================================================================


async def test_provider_definition_missing_marks_revision_error(env: tuple) -> None:
    """Binding points to provider_id not in Registry → init failed."""
    client, app = env
    factory, _ = _make_factory()
    # Use a registry missing 'qwen'
    from pi_agent_core_py.providers.registry import ProviderRegistry

    class _EmptyRegistry(ProviderRegistry):
        def __init__(self_) -> None:
            pass

        def get(self_, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
            return None

        def list(self_) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
            return ()

        def has(self_, provider_id: str) -> bool:  # type: ignore[override]
            return False

    runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_EmptyRegistry(),
        provider_factory=factory,
    )
    app.state.request_provider_runtime = runtime

    # Seed profile + binding (validation endpoint allows any provider_id;
    # resolve_selection will fail at registry lookup).
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id="qwen", credential_id=cred)
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof)

    req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait(client, req_id)

    assert final["status"] == "error"
    assert "Selected provider could not be initialized." in final.get("error", "")


# ============================================================================
# Agent NOT executed on failure
# ============================================================================


async def test_agent_not_executed_on_resolve_failure(env: tuple) -> None:
    """When resolve_selection fails, harness.run_continue must NOT be called."""
    client, app = env
    factory, _ = _make_factory()
    _install_runtime(app, factory=factory)

    sid, aid, prof = await _seed_for_regenerate(client)
    svc = app.state.provider_config_runtime.service
    await svc.update_profile(prof, enabled=False)

    harness = app.state.web.harness
    run_continue_calls: list[Any] = []
    orig_run_continue = harness.run_continue

    async def _track(*args: Any, **kwargs: Any) -> Any:
        run_continue_calls.append(args)
        return await orig_run_continue(*args, **kwargs)

    harness.run_continue = _track  # type: ignore[assignment]

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    assert run_continue_calls == []


async def test_agent_not_executed_on_factory_failure(env: tuple) -> None:
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    harness = app.state.web.harness
    run_continue_calls: list[Any] = []
    orig = harness.run_continue

    async def _track(*args: Any, **kwargs: Any) -> Any:
        run_continue_calls.append(args)
        return await orig(*args, **kwargs)

    harness.run_continue = _track  # type: ignore[assignment]

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    assert run_continue_calls == []


# ============================================================================
# No fallback to legacy on failure
# ============================================================================


async def test_no_fallback_to_legacy_on_binding_failure(env: tuple) -> None:
    """Factory failure must NOT silently fall back to legacy FakeClient.

    Verify by checking that the persisted AssistantMessage's content does not
    contain the legacy marker text after a failed regenerate.
    """
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    # Capture pre-regenerate content
    msgs_before = client.get(f"/api/messages?session_id={sid}").json()["messages"]
    assistant_before = [
        m for m in msgs_before if m.get("role") == "assistant"
    ][-1]
    text_before = "".join(
        c.get("text", "")
        for c in assistant_before["message"].get("content", [])
    )

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # After failed regenerate, content must be UNCHANGED (legacy not invoked)
    msgs_after = client.get(f"/api/messages?session_id={sid}").json()["messages"]
    assistant_after = [
        m for m in msgs_after if m.get("role") == "assistant"
    ][-1]
    text_after = "".join(
        c.get("text", "")
        for c in assistant_after["message"].get("content", [])
    )
    assert text_after == text_before


# ============================================================================
# Original client restored after error
# ============================================================================


async def test_original_client_restored_after_regenerate_error(env: tuple) -> None:
    """After factory failure during regenerate, harness.agent.client must be
    restored to the original (legacy) client."""
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    harness = app.state.web.harness
    original = harness.agent.client

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    assert harness.agent.client is original


# ============================================================================
# Failed regenerate leaves no 'running' revision
# ============================================================================


async def test_failed_regenerate_no_running_revision_left(env: tuple) -> None:
    """Spec §五：失败后 revision.status == error，不留 running."""
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    revs = await _fetch_revisions_raw(client, sid, aid)
    statuses = [r["status"] for r in revs]
    assert "running" not in statuses
    assert "error" in statuses


# ============================================================================
# Harness messages reset after error
# ============================================================================


async def test_harness_messages_reset_after_regenerate_error(env: tuple) -> None:
    """Spec item 50: 失败后 Harness messages 正常 reset. _run_regeneration_core
    restores harness.agent.state.messages via _reset_harness_to_session to
    the CANONICAL state (from SQLite).
    """
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    sid, aid, _ = await _seed_for_regenerate(client)

    # Read canonical messages BEFORE regenerate (these are the source of truth)
    store = client.app.state.web.session_store
    canonical_before = await store.list_messages(sid)

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # Harness state after failed regenerate must reflect canonical (unchanged
    # because regenerate failed → canonical not modified)
    harness = client.app.state.web.harness
    msgs_after = list(harness.agent.state.messages)
    canonical_after = await store.list_messages(sid)

    # Canonical didn't change (regenerate failed)
    assert len(canonical_before) == len(canonical_after)
    # Harness matches canonical
    assert len(msgs_after) == len(canonical_after)


# ============================================================================
# Error messages contain no sensitive markers
# ============================================================================


async def test_error_messages_no_sensitive_markers(env: tuple) -> None:
    """Error_summary + HTTP response must not contain profile_id / model_id /
    credential_id / secret / credential label."""
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    PROFILE_NAME = "profile-LEAK-CHECK-M1-6"
    MODEL_ID = "model-LEAK-CHECK-M1-6"
    CRED_LABEL = "cred-LEAK-CHECK-M1-6"

    cred_r = client.post(
        "/api/credentials",
        headers=_UI,
        json={"label": CRED_LABEL, "storage_mode": "session_only", "secret_value": SECRET_MARKER},
    )
    cred_id = cred_r.json()["credential"]["credential_id"]

    prof_r = client.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": PROFILE_NAME,
            "provider_id": "qwen",
            "credential_id": cred_id,
            "default_model": MODEL_ID,
        },
    )
    prof_id = prof_r.json()["profile"]["id"]

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof_id, model_id=MODEL_ID)

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # Check revision error_summary
    rev = await _latest_revision(client, sid, aid)
    summary = rev.get("error_summary") or ""
    assert SECRET_MARKER not in summary
    assert PROFILE_NAME not in summary
    assert MODEL_ID not in summary
    assert CRED_LABEL not in summary
    assert prof_id not in summary
    assert cred_id not in summary

    # Also check request error field (in request_history after terminal status)
    state = client.app.state.web
    for r in state.request_history:
        if r.operation == "regenerate" and r.target_message_id == aid:
            err = r.error or ""
            assert SECRET_MARKER not in err
            assert PROFILE_NAME not in err
            assert MODEL_ID not in err
            assert prof_id not in err
            assert cred_id not in err
            return
    pytest.fail("no regenerate request in history")


# ============================================================================
# Failed regenerate can be retried
# ============================================================================


async def test_failed_regenerate_can_be_retried(env: tuple) -> None:
    """Spec item 51: 失败后可以再次 Regenerate. After error, a re-enabled
    profile allows a successful regenerate."""
    client, app = env
    factory, factory_calls = _make_factory()
    _install_runtime(app, factory=factory)

    sid, aid, prof = await _seed_for_regenerate(client)

    # Disable → first regenerate fails
    svc = app.state.provider_config_runtime.service
    await svc.update_profile(prof, enabled=False)
    f1 = _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert f1["status"] == "error"

    # Re-enable → second regenerate succeeds
    await svc.update_profile(prof, enabled=True)
    f2 = _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert f2["status"] == "completed"
    assert len(factory_calls) == 1  # factory called once on the successful retry


# ============================================================================
# Error code consistency with M1-5 (spec item 47)
# ============================================================================


async def test_regenerate_error_messages_match_m1_5_fixed_set(env: tuple) -> None:
    """All 4 fixed error messages must be surfaceable via regenerate path.
    Confirms error code consistency with M1-5 prompt path.
    """
    client, app = env
    factory, _ = _make_factory(fail=True)
    _install_runtime(app, factory=factory)

    # Test disabled error
    sid, aid, prof = await _seed_for_regenerate(client)
    svc = app.state.provider_config_runtime.service
    await svc.update_profile(prof, enabled=False)
    f = _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert "Selected provider profile is disabled." in f.get("error", "")

    # Re-enable, then simulate credential unavailable
    await svc.update_profile(prof, enabled=True)
    cred = client.get("/api/credentials", headers=_UI).json()["credentials"][0]["credential_id"]
    client.delete(f"/api/credentials/{cred}", headers=_UI)
    f2 = _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert "Selected provider credential is unavailable." in f2.get("error", "")

    # Re-seed credential and bind to a fresh profile to test factory init failure
    cred2 = _seed_credential(client)
    prof2 = _seed_profile(
        client, provider_id="qwen", credential_id=cred2, name="P2"
    )
    _bind(client, session_id=sid, profile_id=prof2, model_id="m2")
    f3 = _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert "Selected provider could not be initialized." in f3.get("error", "")

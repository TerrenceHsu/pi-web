"""Regenerate × Provider Runtime integration tests for M1-6.

验证（spec §一/§三 1-23）：
- Regenerate 实际使用**当前** Session Binding（不是原回答的 Provider/Model）
- 切换 Binding 后再次 Regenerate 使用新 Provider
- AssistantMessage.provider/model 反映**本次** Regenerate 的 Provider
- D2 不变量保持：assistant_message_id 不变 / revision_number 递增 / 旧 revision 保留
- usage 继续保存
- 实际 Provider/Model 来自生成后的 AssistantMessage JSON（不读 Binding 字段）

不修改生产代码——本文件仅验证 M1-5 单一接入点已覆盖 Regenerate 路径。
"""
from __future__ import annotations

import json
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
from pi_agent_core_py.providers.registry import (  # noqa: E402
    _DEFAULT_REGISTRY,
    ProviderDefinition,
)
from pi_agent_core_py.stream_events import StreamEvent  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.runtime import (  # noqa: E402
    RequestProviderRuntime,
)

# asyncio_mode=auto in pyproject——async tests run automatically.

_UI = {"X-PI-Agent-UI": "1"}
SECRET_MARKER = "sk-M1-6-INTEGRATION-SECRET-MARKER"


# ============================================================================
# Stub Adapter——controllable provider_id / model + event script
# ============================================================================


class _ScriptedAdapter(ProviderAdapter):
    """ProviderAdapter that plays back a fixed event script + tracks stream calls."""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        events: list[StreamEvent],
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self.events = list(events)
        self.stream_calls = 0
        self.closed = False

    async def stream(self, request: ProviderRequest) -> Any:
        self.stream_calls += 1
        for ev in self.events:
            yield ev

    async def aclose(self) -> None:
        self.closed = True


def _scripted_factory_call_tracker() -> tuple[
    Any,
    list[dict[str, Any]],
    list[_ScriptedAdapter],
]:
    """Factory that creates a fresh _ScriptedAdapter per call and records inputs."""
    calls: list[dict[str, Any]] = []
    adapters: list[_ScriptedAdapter] = []

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
        events: list[StreamEvent] | None = None,
    ) -> _ScriptedAdapter:
        default_events: list[StreamEvent] = [
            TextDeltaEvent(delta="hello"),
            DoneEvent(stop_reason="stop", usage=Usage()),
        ]
        adapter = _ScriptedAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events=events or default_events,
        )
        adapters.append(adapter)
        calls.append(
            {
                "provider_definition": provider_definition,
                "api_key": api_key,
                "model_id": model_id,
            }
        )
        return adapter

    return factory, calls, adapters


def _install_runtime(
    app: object,
    *,
    factory: Any,
) -> RequestProviderRuntime:
    runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=factory,
    )
    app.state.request_provider_runtime = runtime
    return runtime


# ============================================================================
# App + harness setup
# ============================================================================


def _harness() -> AgentHarness:
    """Legacy FakeClient——used for the initial prompt before Binding is set.

    Provides enough scripts for prompt + a few regenerates (legacy path)."""
    one = [
        TextDeltaEvent(delta="legacy"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]
    scripts = [list(one) for _ in range(30)]
    client = FakeClient(scripts)
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


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


# ============================================================================
# Helpers
# ============================================================================


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={
            "label": "L",
            "storage_mode": "session_only",
            "secret_value": secret,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient,
    *,
    provider_id: str,
    credential_id: str,
    default_model: str,
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


def _bind(
    c: TestClient,
    *,
    session_id: str,
    profile_id: str,
    model_id: str,
) -> dict[str, Any]:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": model_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["binding"]


def _make_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"title": "T"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _send_prompt(c: TestClient, sid: str, text: str = "q") -> None:
    r = c.post("/api/prompt", json={"text": text, "session_id": sid})
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
    assert row is not None, "no assistant message in session"
    return row["id"]


def _last_assistant(c: TestClient, session_id: str) -> dict[str, Any]:
    """Return the inner AssistantMessage dict (full AgentMessage with
    provider/model/usage)."""
    r = c.get(f"/api/messages?session_id={session_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    for msg in reversed(body["messages"]):
        if msg.get("role") == "assistant":
            return msg["message"]
    raise AssertionError(f"no assistant message in session {session_id}")


def _regenerate(
    c: TestClient,
    *,
    sid: str,
    aid: str,
) -> tuple[int, str, str]:
    """POST regenerate; return (status_code, regeneration_id, request_id)."""
    r = c.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    body = r.json()
    return r.status_code, body.get("regeneration_id"), body.get("request_id")


def _wait_for_request(
    c: TestClient,
    request_id: str,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = c.get(f"/api/requests/{request_id}")
        if r.status_code == 200:
            last = r.json()
            if last["status"] in ("completed", "error", "aborted"):
                return last
        time.sleep(0.02)
    pytest.fail(f"request {request_id} never reached terminal: last={last}")


async def _fetch_revisions_raw(c: TestClient, sid: str, aid: str) -> list[dict[str, Any]]:
    """Direct DB read——full row including content_json / error_summary."""
    state = c.app.state.web
    db = state.extension_store._require_db()
    cur = await db.execute(
        "SELECT id, revision_number, status, content_json, error_summary, "
        "request_id, base_content_sha256 "
        "FROM web_message_revisions "
        "WHERE session_id = ? AND assistant_message_id = ? "
        "ORDER BY revision_number ASC",
        (sid, aid),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


# ============================================================================
# §三 Items 9 / 7: legacy path (no Binding) still works for Regenerate
# ============================================================================


async def test_regenerate_legacy_session_without_binding_uses_original_client(
    env: tuple,
) -> None:
    """No Binding set → Regenerate uses legacy FakeClient; Factory not called."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    code, _regen_id, req_id = _regenerate(client, sid=sid, aid=aid)
    assert code == 202
    final = _wait_for_request(client, req_id)
    assert final["status"] == "completed"

    # Legacy path: factory NOT called during regenerate
    assert calls == []


async def test_regenerate_legacy_path_no_secret_read_no_factory_call(env: tuple) -> None:
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    runtime = _install_runtime(app, factory=factory)

    cred_svc = runtime._credential_service
    orig_resolve = cred_svc.resolve_secret_for_request
    resolve_calls: list[str] = []

    async def _track_resolve(credential_id: str) -> str:
        resolve_calls.append(credential_id)
        return await orig_resolve(credential_id)

    cred_svc.resolve_secret_for_request = _track_resolve  # type: ignore[assignment]

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req_id)

    assert resolve_calls == []
    assert calls == []


# ============================================================================
# §三 Items 1-3: Regenerate uses **current** Binding, not original Provider
# ============================================================================


async def test_regenerate_uses_current_binding_not_original_glm(env: tuple) -> None:
    """First answer via legacy (GLM wasn't bound). Bind to Qwen → Regenerate
    actually uses Qwen, not the legacy client.
    """
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    # 1. Initial prompt: no binding → legacy FakeClient
    sid = _make_session(client)
    _send_prompt(client, sid, "first")
    aid = await _latest_assistant_id(client, sid)
    assert calls == []  # sanity: legacy prompt didn't touch factory

    # 2. Bind to Qwen
    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-model-b")

    # 3. Regenerate
    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    final = _wait_for_request(client, req_id)
    assert final["status"] == "completed"

    # Regenerate triggered exactly one factory call, with current binding
    assert len(calls) == 1
    assert calls[0]["provider_definition"].id == "qwen"
    assert calls[0]["model_id"] == "qwen-model-b"
    assert calls[0]["api_key"] == SECRET_MARKER

    # AssistantMessage now reflects the new provider/model
    last = _last_assistant(client, sid)
    assert last.get("provider") == "qwen"
    assert last.get("model") == "qwen-model-b"


async def test_regenerate_switching_qwen_to_kimi(env: tuple) -> None:
    """First regenerate uses Qwen, second uses Kimi (after switching binding)."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred_q = _seed_credential(client, secret="key-Q")
    cred_k = _seed_credential(client, secret="key-K")
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default",
        name="Pqwen",
    )
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default",
        name="Pkimi",
    )

    # Seed initial assistant via legacy path (no binding)——keeps calls[]
    # clean so calls[0] = first regenerate.
    sid = _make_session(client)
    _send_prompt(client, sid, "first")
    aid = await _latest_assistant_id(client, sid)
    assert calls == []  # sanity: legacy seed didn't touch factory

    # Bind to Qwen → first regenerate uses Qwen
    _bind(client, session_id=sid, profile_id=prof_q, model_id="qwen-model-a")
    _, _, req1 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req1)
    assert len(calls) == 1
    assert calls[0]["provider_definition"].id == "qwen"
    assert calls[0]["api_key"] == "key-Q"
    assert calls[0]["model_id"] == "qwen-model-a"

    # Switch binding to Kimi
    _bind(client, session_id=sid, profile_id=prof_k, model_id="kimi-model-b")

    # Second regenerate: Kimi
    _, _, req2 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req2)
    assert len(calls) == 2
    assert calls[1]["provider_definition"].id == "kimi"
    assert calls[1]["api_key"] == "key-K"
    assert calls[1]["model_id"] == "kimi-model-b"


async def test_regenerate_switching_kimi_to_glm(env: tuple) -> None:
    """Switch Kimi → GLM; regenerate uses GLM."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred_k = _seed_credential(client, secret="key-K")
    cred_g = _seed_credential(client, secret="key-G")
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default",
        name="Pkimi",
    )
    prof_g = _seed_profile(
        client,
        provider_id="glm",
        credential_id=cred_g,
        default_model="glm-default",
        name="Pglm",
    )

    sid = _make_session(client)
    _send_prompt(client, sid, "q")  # legacy seed
    aid = await _latest_assistant_id(client, sid)
    assert calls == []

    _bind(client, session_id=sid, profile_id=prof_k, model_id="kimi-first")
    _, _, req1 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req1)
    assert calls[-1]["provider_definition"].id == "kimi"

    _bind(client, session_id=sid, profile_id=prof_g, model_id="glm-after")
    _, _, req2 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req2)
    assert calls[-1]["provider_definition"].id == "glm"
    assert calls[-1]["model_id"] == "glm-after"


# ============================================================================
# §三 Items 4-6: model comes from Binding, not Profile.default_model nor old message
# ============================================================================


async def test_regenerate_model_id_comes_from_binding_not_profile_default(
    env: tuple,
) -> None:
    """Profile.default_model='qwen-default' but Binding.model_id='qwen-override'."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-profile-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-binding-override")
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req_id)

    assert calls[0]["model_id"] == "qwen-binding-override"
    last = _last_assistant(client, sid)
    assert last.get("model") == "qwen-binding-override"
    assert last.get("model") != "qwen-profile-default"


async def test_regenerate_does_not_use_original_assistant_model(env: tuple) -> None:
    """First answer stored model='qwen-first-model'. Switch binding to
    model='qwen-second-model' → regenerate uses second, not the stored one."""
    client, app = env
    factory, calls, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    # Seed via legacy path so calls[0] = regenerate (not prompt)
    sid = _make_session(client)
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)
    assert calls == []

    # First bind + regenerate → qwen-first-model persisted on assistant
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-first-model")
    _, _, req1 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req1)
    original = _last_assistant(client, sid)
    assert original.get("model") == "qwen-first-model"

    # Change binding model_id → regenerate uses new model, not stored one
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-second-model")
    _, _, req2 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req2)

    assert len(calls) == 2
    assert calls[1]["model_id"] == "qwen-second-model"
    last = _last_assistant(client, sid)
    assert last.get("model") == "qwen-second-model"


# ============================================================================
# §三 Items 14-15: Revision invariants — assistant_message_id unchanged +
# revision_number increments + provider/model in new revision content_json
# ============================================================================


async def test_regenerate_preserves_assistant_message_id(env: tuple) -> None:
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-a")
    _send_prompt(client, sid, "q")
    aid_before = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid_before)
    _wait_for_request(client, req_id)

    aid_after = await _latest_assistant_id(client, sid)
    assert aid_after == aid_before  # D2: canonical assistant 位置不变


async def test_regenerate_revision_number_increments(env: tuple) -> None:
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-a")
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)

    _, _, r1 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, r1)
    _, _, r2 = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, r2)

    revs = await _fetch_revisions_raw(client, sid, aid)
    assert len(revs) >= 2
    nums = [r["revision_number"] for r in revs]
    # revision_number 严格递增（>= 0）
    assert nums == sorted(nums)
    assert len(set(nums)) == len(nums)  # unique
    assert nums[-1] > nums[0]


async def test_regenerate_old_revisions_retained(env: tuple) -> None:
    """After multiple regenerates, all prior revisions remain in DB."""
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-a")
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)

    for _ in range(3):
        _, _, req_id = _regenerate(client, sid=sid, aid=aid)
        _wait_for_request(client, req_id)

    revs = await _fetch_revisions_raw(client, sid, aid)
    # Each regenerate creates revisions for both the "baseline" content and
    # the new result; the count grows monotonically. Spec only requires that
    # prior revisions are RETAINED, not an exact count.
    assert len(revs) >= 3
    statuses = [r["status"] for r in revs]
    # Only the last should be 'completed'; earlier ones 'superseded'
    assert statuses[-1] == "completed"
    for st in statuses[:-1]:
        assert st in ("superseded", "completed")


async def test_regenerate_new_revision_has_provider_and_model(env: tuple) -> None:
    """Latest revision content_json should embed provider/model from current binding."""
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred,
        default_model="kimi-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="kimi-target")
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req_id)

    revs = await _fetch_revisions_raw(client, sid, aid)
    latest = revs[-1]
    assert latest["status"] == "completed"
    assert latest["content_json"] is not None
    payload = json.loads(latest["content_json"])
    # content_json is {"type": "AssistantMessage", "data": {...}}
    data = payload.get("data", payload)
    assert data.get("provider") == "kimi"
    assert data.get("model") == "kimi-target"


# ============================================================================
# §三 Item 14: usage still persisted
# ============================================================================


async def test_regenerate_usage_persisted_on_new_revision(env: tuple) -> None:
    client, app = env

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> _ScriptedAdapter:
        return _ScriptedAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events=[
                TextDeltaEvent(delta="hello"),
                DoneEvent(
                    stop_reason="stop",
                    usage=Usage(input=42, output=13, total_tokens=55),
                ),
            ],
        )

    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-plus")
    _send_prompt(client, sid, "q")
    aid = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req_id)

    last = _last_assistant(client, sid)
    usage = last.get("usage", {})
    assert usage.get("input") == 42
    assert usage.get("output") == 13
    assert usage.get("total_tokens") == 55


# ============================================================================
# §三 Item 17: Regenerate only allowed on latest AssistantMessage (D2 invariant)
# ============================================================================


async def test_regenerate_non_latest_assistant_returns_409(env: tuple) -> None:
    """D2 invariant preserved: non-latest assistant → 409 regenerate_target_not_latest.
    Provider Runtime wiring must NOT bypass this check."""
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-a")
    _send_prompt(client, sid, "first")
    aid_old = await _latest_assistant_id(client, sid)

    # New prompt → aid_old is no longer latest
    _send_prompt(client, sid, "second")

    r = client.post(f"/api/sessions/{sid}/messages/{aid_old}/regenerate")
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "regenerate_target_not_latest"


# ============================================================================
# §三 Item 16: history truncation unchanged — regenerate replaces in place
# ============================================================================


async def test_regenerate_replaces_assistant_in_place(env: tuple) -> None:
    """D2 invariant: after regenerate, there is still exactly ONE assistant
    position for that turn (not two). Provider Runtime binding must not
    accidentally fork the message.
    """
    client, app = env
    factory, _, _ = _scripted_factory_call_tracker()
    _install_runtime(app, factory=factory)

    cred = _seed_credential(client)
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-plus")
    _send_prompt(client, sid, "first")
    aid = await _latest_assistant_id(client, sid)

    _, _, req_id = _regenerate(client, sid=sid, aid=aid)
    _wait_for_request(client, req_id)

    msgs = client.get(f"/api/messages?session_id={sid}").json()["messages"]
    assistants = [m for m in msgs if m.get("role") == "assistant"]
    # D2 invariant: still exactly one assistant for this turn
    assert len(assistants) == 1
    assert assistants[0]["message_id"] == aid

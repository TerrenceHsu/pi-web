"""Multi-Provider Runtime freeze tests for M1-7（spec §三/§四/§五/§六/§九）.

**Validation-only**——M1-7 不修改生产代码。本文件覆盖跨模块组合后才可能
发现的问题：

- 3-provider E2E 矩阵（GLM / Qwen / Kimi）× Prompt / Regenerate / Tool call
- 跨 Session 隔离（single harness，顺序执行不污染）
- 默认 Profile 全链路（创建时快照 + 修改默认不影响旧 Session）
- 显式切换全链路（PUT Binding → 下一 Prompt / Regenerate 使用新 Provider）
- Tool loop 不变量（resolve / secret / factory / adapter 各一次）

所有测试使用 mock transport / fake stream——真实网络调用 = 0。
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
    ToolCall,
    Usage,
)
from pi_agent_core_py.providers.base import ProviderAdapter, ProviderRequest  # noqa: E402
from pi_agent_core_py.providers.factory import create_provider  # noqa: E402
from pi_agent_core_py.providers.glm import GLMProviderAdapter  # noqa: E402
from pi_agent_core_py.providers.openai_compat import OpenAICompatibleProvider  # noqa: E402
from pi_agent_core_py.providers.registry import (  # noqa: E402
    _DEFAULT_REGISTRY,
    ProviderDefinition,
)
from pi_agent_core_py.stream_events import StreamEvent, ToolCallEvent  # noqa: E402
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.provider_runtime import (  # noqa: E402
    RequestProviderRuntime,
)

# asyncio_mode=auto in pyproject.

_UI = {"X-PI-Agent-UI": "1"}


# ============================================================================
# Stub Adapter + Tracking Factory
# ============================================================================


class _FreezeAdapter(ProviderAdapter):
    """Records stream invocations; plays back a fixed event script.

    For tool-loop tests, accepts a callable that returns events based on
    the call index——enables simulating tool_call → tool_result → text flow.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        events_factory: Any = None,
        events: list[StreamEvent] | None = None,
        close_log: list[str] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._events_factory = events_factory
        self._events = list(events or [])
        self._close_log = close_log
        self.stream_calls = 0
        self.calls_log: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> Any:
        self.stream_calls += 1
        self.calls_log.append(request)
        if self._events_factory is not None:
            events = self._events_factory(self.stream_calls - 1, request)
        else:
            events = self._events
        for ev in events:
            yield ev

    async def aclose(self) -> None:
        if self._close_log is not None:
            self._close_log.append(f"{self.provider_id}/{self.model}")


def _events_text(text: str = "hi", usage: Usage | None = None) -> list[StreamEvent]:
    return [
        TextDeltaEvent(delta=text),
        DoneEvent(stop_reason="stop", usage=usage or Usage()),
    ]


def _events_tool_then_text(
    tool_name: str = "calc",
    tool_args: dict[str, Any] | None = None,
    final_text: str = "done",
) -> Any:
    """Returns a factory: call 0 emits ToolCallEvent, call 1 emits text."""

    def _factory(call_idx: int, _request: ProviderRequest) -> list[StreamEvent]:
        if call_idx == 0:
            return [
                ToolCallEvent(
                    tool_call=ToolCall(
                        id="call_freeze_1",
                        name=tool_name,
                        arguments=tool_args or {"x": 1},
                    ),
                ),
                DoneEvent(stop_reason="tool_use", usage=Usage()),
            ]
        return _events_text(final_text)

    return _factory


def _make_tracking_factory(
    log: list[dict[str, Any]],
    close_log: list[str] | None = None,
    *,
    events_factory: Any = None,
) -> Any:
    """Each factory call appends to `log` and returns a fresh _FreezeAdapter."""

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        adapter = _FreezeAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events_factory=events_factory,
            events=_events_text(),
            close_log=close_log,
        )
        log.append(
            {
                "provider_id": provider_definition.id,
                "model_id": model_id,
                "api_key": api_key,
                "adapter": adapter,
            }
        )
        return adapter

    return factory


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


def _harness(tools: list | None = None) -> AgentHarness:
    """Legacy FakeClient for seeding (no binding → legacy path)."""
    one = [
        TextDeltaEvent(delta="legacy"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]
    scripts = [list(one) for _ in range(50)]
    client = FakeClient(scripts)
    agent = Agent(system_prompt="base", client=client, tools=tools)
    return AgentHarness(agent)


@pytest.fixture
async def env(
    tmp_path: Path,
) -> tuple[TestClient, object, list[dict[str, Any]], list[str]]:
    log: list[dict[str, Any]] = []
    close_log: list[str] = []
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as client:
        _install_runtime(app, factory=_make_tracking_factory(log, close_log))
        yield client, app, log, close_log  # type: ignore[misc]


# ============================================================================
# Helpers (shared across test groups)
# ============================================================================


def _seed_credential(c: TestClient, secret: str, label: str = "L") -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": label, "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient,
    *,
    provider_id: str,
    credential_id: str,
    default_model: str,
    name: str | None = None,
) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": name or f"P-{provider_id}",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": default_model,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["profile"]["id"]


def _make_session(c: TestClient, title: str = "T") -> str:
    r = c.post("/api/sessions", json={"title": title})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _send_prompt(c: TestClient, sid: str, text: str = "q") -> None:
    r = c.post("/api/prompt", json={"text": text, "session_id": sid})
    assert r.status_code == 200, r.text


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


def _get_binding(c: TestClient, session_id: str) -> dict[str, Any] | None:
    r = c.get(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
    )
    assert r.status_code == 200, r.text
    return r.json()["binding"]  # None or dict


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


def _last_assistant(c: TestClient, session_id: str) -> dict[str, Any]:
    r = c.get(f"/api/messages?session_id={session_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    for msg in reversed(body["messages"]):
        if msg.get("role") == "assistant":
            return msg["message"]
    raise AssertionError(f"no assistant message in session {session_id}")


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


async def _seed_bound_session(
    client: TestClient,
    *,
    provider_id: str,
    model_id: str,
    secret: str,
    profile_name: str | None = None,
) -> tuple[str, str, str]:
    """Seed session via legacy, then bind. Returns (sid, aid, profile_id)."""
    cred = _seed_credential(client, secret, label=f"L-{provider_id}")
    prof = _seed_profile(
        client,
        provider_id=provider_id,
        credential_id=cred,
        default_model=f"{provider_id}-default",
        name=profile_name,
    )
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id=model_id)
    return sid, aid, prof


# ============================================================================
# §三 Provider E2E matrix (GLM / Qwen / Kimi × Prompt / Regenerate)
# ============================================================================


async def test_glm_end_to_end_prompt_and_regenerate(env: tuple) -> None:
    client, app, log, _close = env
    sid, aid, _ = await _seed_bound_session(
        client,
        provider_id="glm",
        model_id="glm-4.5-flash",
        secret="sk-glm-freeze",
    )

    # First regenerate uses GLM
    _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert log[-1]["provider_id"] == "glm"
    assert log[-1]["model_id"] == "glm-4.5-flash"
    assert log[-1]["api_key"] == "sk-glm-freeze"

    # AssistantMessage reflects GLM
    last = _last_assistant(client, sid)
    assert last.get("provider") == "glm"
    assert last.get("model") == "glm-4.5-flash"


async def test_qwen_end_to_end_prompt_and_regenerate(env: tuple) -> None:
    client, app, log, _close = env
    sid, aid, _ = await _seed_bound_session(
        client,
        provider_id="qwen",
        model_id="qwen-turbo",
        secret="sk-qwen-freeze",
    )

    _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert log[-1]["provider_id"] == "qwen"
    assert log[-1]["model_id"] == "qwen-turbo"
    assert log[-1]["api_key"] == "sk-qwen-freeze"

    last = _last_assistant(client, sid)
    assert last.get("provider") == "qwen"
    assert last.get("model") == "qwen-turbo"


async def test_kimi_end_to_end_prompt_and_regenerate(env: tuple) -> None:
    client, app, log, _close = env
    sid, aid, _ = await _seed_bound_session(
        client,
        provider_id="kimi",
        model_id="moonshot-v1-8k",
        secret="sk-kimi-freeze",
    )

    _wait(client, _regenerate(client, sid=sid, aid=aid))
    assert log[-1]["provider_id"] == "kimi"
    assert log[-1]["model_id"] == "moonshot-v1-8k"
    assert log[-1]["api_key"] == "sk-kimi-freeze"

    last = _last_assistant(client, sid)
    assert last.get("provider") == "kimi"
    assert last.get("model") == "moonshot-v1-8k"


# ============================================================================
# §三 Verify real Factory routes provider_id → correct Adapter class
# ============================================================================


def test_factory_routes_glm_to_glm_adapter() -> None:
    defn = _DEFAULT_REGISTRY.get("glm")
    assert defn is not None
    adapter = create_provider(
        provider_definition=defn,
        api_key="sk-test",
        model_id="glm-4.5-flash",
    )
    assert isinstance(adapter, GLMProviderAdapter)
    assert adapter.provider_id == "glm"
    assert adapter.model == "glm-4.5-flash"


def test_factory_routes_qwen_to_openai_compat() -> None:
    defn = _DEFAULT_REGISTRY.get("qwen")
    assert defn is not None
    adapter = create_provider(
        provider_definition=defn,
        api_key="sk-test",
        model_id="qwen-turbo",
    )
    assert isinstance(adapter, OpenAICompatibleProvider)
    assert adapter.provider_id == "qwen"
    assert adapter.model == "qwen-turbo"


def test_factory_routes_kimi_to_openai_compat() -> None:
    defn = _DEFAULT_REGISTRY.get("kimi")
    assert defn is not None
    adapter = create_provider(
        provider_definition=defn,
        api_key="sk-test",
        model_id="moonshot-v1-8k",
    )
    assert isinstance(adapter, OpenAICompatibleProvider)
    assert adapter.provider_id == "kimi"
    assert adapter.model == "moonshot-v1-8k"


# ============================================================================
# §四 Cross-Session Isolation (single harness, sequential prompts)
# ============================================================================


async def test_cross_session_isolation_qwen_kimi(env: tuple) -> None:
    """Session A → Qwen, Session B → Kimi. Sequential prompts don't pollute."""
    client, app, log, _close = env

    # Seed both credentials + profiles up front
    cred_q = _seed_credential(client, "sk-A-qwen", label="LQ")
    cred_k = _seed_credential(client, "sk-B-kimi", label="LK")
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

    sid_a = _make_session(client, title="A")
    sid_b = _make_session(client, title="B")
    _bind(client, session_id=sid_a, profile_id=prof_q, model_id="qwen-A")
    _bind(client, session_id=sid_b, profile_id=prof_k, model_id="kimi-B")

    # Prompt A → Qwen
    _send_prompt(client, sid_a, "first-A")
    assert log[-1]["provider_id"] == "qwen"
    assert log[-1]["api_key"] == "sk-A-qwen"

    # Prompt B → Kimi
    _send_prompt(client, sid_b, "first-B")
    assert log[-1]["provider_id"] == "kimi"
    assert log[-1]["api_key"] == "sk-B-kimi"

    # Re-prompt A → still Qwen
    _send_prompt(client, sid_a, "second-A")
    assert log[-1]["provider_id"] == "qwen"
    assert log[-1]["api_key"] == "sk-A-qwen"

    # Re-prompt B → still Kimi
    _send_prompt(client, sid_b, "second-B")
    assert log[-1]["provider_id"] == "kimi"
    assert log[-1]["api_key"] == "sk-B-kimi"


async def test_cross_session_harness_client_restored_between_requests(
    env: tuple,
) -> None:
    """Each request restores original (legacy) client——single harness is safe."""
    client, app, log, _close = env

    cred_q = _seed_credential(client, "sk-q", label="LQ")
    cred_k = _seed_credential(client, "sk-k", label="LK")
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default",
    )
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default",
    )

    sid_a = _make_session(client)
    sid_b = _make_session(client)
    _bind(client, session_id=sid_a, profile_id=prof_q, model_id="qwen-A")
    _bind(client, session_id=sid_b, profile_id=prof_k, model_id="kimi-B")

    harness = app.state.web.harness
    original = harness.agent.client

    _send_prompt(client, sid_a, "A1")
    assert harness.agent.client is original
    _send_prompt(client, sid_b, "B1")
    assert harness.agent.client is original
    _send_prompt(client, sid_a, "A2")
    assert harness.agent.client is original


async def test_cross_session_failure_does_not_pollute(env: tuple) -> None:
    """Session A failure (e.g. disabled profile) doesn't affect Session B."""
    client, app, log, _close = env

    cred_q = _seed_credential(client, "sk-q", label="LQ")
    cred_k = _seed_credential(client, "sk-k", label="LK")
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default",
        name="Pq",
    )
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default",
        name="Pk",
    )

    sid_a = _make_session(client)
    sid_b = _make_session(client)
    _bind(client, session_id=sid_a, profile_id=prof_q, model_id="qwen-A")
    _bind(client, session_id=sid_b, profile_id=prof_k, model_id="kimi-B")

    # Disable Qwen profile → A fails
    svc = app.state.provider_config_runtime.service
    await svc.update_profile(prof_q, enabled=False)

    r_a = client.post("/api/prompt", json={"text": "A-fail", "session_id": sid_a})
    assert r_a.json().get("ok") is False or r_a.status_code >= 400

    # Session B should still work
    factory_calls_before_b = len(log)
    _send_prompt(client, sid_b, "B-ok")
    assert len(log) == factory_calls_before_b + 1
    assert log[-1]["provider_id"] == "kimi"
    assert log[-1]["api_key"] == "sk-k"


# ============================================================================
# §五 Default Profile full chain
# ============================================================================


async def test_default_profile_applied_on_session_creation(env: tuple) -> None:
    """Set Qwen as global default → new session auto-binds Qwen."""
    client, app, _log, _close = env

    cred_q = _seed_credential(client, "sk-q", label="LQ")
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default-model",
        name="PqwenDefault",
    )

    # Set global default via PATCH is_default=true
    r = client.patch(
        f"/api/provider-profiles/{prof_q}",
        headers=_UI,
        json={"is_default": True},
    )
    assert r.status_code in (200, 204), r.text

    # New session auto-binds Qwen
    sid = _make_session(client)
    binding = _get_binding(client, sid)
    assert binding is not None
    assert binding["profile_id"] == prof_q
    assert binding["model_id"] == "qwen-default-model"
    assert binding["source"] == "default"


async def test_changing_global_default_does_not_affect_existing_sessions(
    env: tuple,
) -> None:
    """Old session bound to Qwen; switch global default to Kimi; old session
    still uses Qwen."""
    client, app, log, _close = env

    cred_q = _seed_credential(client, "sk-q", label="LQ")
    cred_k = _seed_credential(client, "sk-k", label="LK")
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default-model",
        name="Pqwen",
    )
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default-model",
        name="Pkimi",
    )

    # Set Qwen as default
    client.patch(
        f"/api/provider-profiles/{prof_q}",
        headers=_UI,
        json={"is_default": True},
    )

    # Create session A → auto-bind Qwen
    sid_a = _make_session(client)

    # Switch default to Kimi
    client.patch(
        f"/api/provider-profiles/{prof_k}",
        headers=_UI,
        json={"is_default": True},
    )

    # Session A still bound to Qwen
    binding = _get_binding(client, sid_a)
    assert binding is not None
    assert binding["profile_id"] == prof_q
    assert binding["model_id"] == "qwen-default-model"


async def test_changing_profile_default_model_does_not_affect_existing_binding(
    env: tuple,
) -> None:
    """Profile.default_model update doesn't change SessionModelBinding.model_id."""
    client, app, log, _close = env

    cred = _seed_credential(client, "sk-q", label="LQ")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-original",
    )

    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-bound-model")

    # Update profile's default_model via PATCH
    r = client.patch(
        f"/api/provider-profiles/{prof}",
        headers=_UI,
        json={"default_model": "qwen-changed"},
    )
    assert r.status_code in (200, 204), r.text

    # Session binding unchanged
    binding = _get_binding(client, sid)
    assert binding is not None
    assert binding["model_id"] == "qwen-bound-model"


# ============================================================================
# §六 Explicit switching full chain
# ============================================================================


async def test_explicit_switching_glm_to_qwen_to_kimi(env: tuple) -> None:
    """PUT binding GLM → prompt → PUT Qwen → prompt → PUT Kimi → regenerate."""
    client, app, log, _close = env

    cred_g = _seed_credential(client, "sk-g", label="LG")
    cred_q = _seed_credential(client, "sk-q", label="LQ")
    cred_k = _seed_credential(client, "sk-k", label="LK")
    prof_g = _seed_profile(
        client,
        provider_id="glm",
        credential_id=cred_g,
        default_model="glm-default",
        name="Pg",
    )
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default",
        name="Pq",
    )
    prof_k = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_k,
        default_model="kimi-default",
        name="Pk",
    )

    sid = _make_session(client)
    # Seed via legacy so first binding-initiated call is a clean prompt
    _send_prompt(client, sid, "seed")

    # GLM
    _bind(client, session_id=sid, profile_id=prof_g, model_id="glm-1")
    _send_prompt(client, sid, "p1")
    assert log[-1]["provider_id"] == "glm"

    # Qwen
    _bind(client, session_id=sid, profile_id=prof_q, model_id="qwen-2")
    _send_prompt(client, sid, "p2")
    assert log[-1]["provider_id"] == "qwen"

    # Kimi (via regenerate——regenerate the latest assistant)
    _bind(client, session_id=sid, profile_id=prof_k, model_id="kimi-3")
    aid_latest = await _latest_assistant_id(client, sid)
    _wait(client, _regenerate(client, sid=sid, aid=aid_latest))
    assert log[-1]["provider_id"] == "kimi"
    assert log[-1]["model_id"] == "kimi-3"

    # API binding reflects latest
    binding = _get_binding(client, sid)
    assert binding is not None
    assert binding["profile_id"] == prof_k
    assert binding["model_id"] == "kimi-3"


async def test_explicit_switching_does_not_re_read_profile_default(
    env: tuple,
) -> None:
    """After PUT binding with model_id override, profile.default_model must
    NOT be re-read."""
    client, app, log, _close = env

    cred = _seed_credential(client, "sk-q", label="LQ")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-profile-default",
    )

    sid = _make_session(client)
    _send_prompt(client, sid, "seed")
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-binding-override")

    log.clear()
    _send_prompt(client, sid, "p1")
    assert len(log) == 1
    assert log[0]["model_id"] == "qwen-binding-override"
    assert log[0]["model_id"] != "qwen-profile-default"


# ============================================================================
# §九 Tool Loop invariants (resolve / secret / factory / adapter each once)
# ============================================================================


async def test_tool_loop_qwen_single_factory_call(env: tuple) -> None:
    """Qwen: tool_call → tool_result → text. Single factory call across loop."""
    client, app, log, close_log = env

    # Re-install factory with tool-loop event script
    log.clear()
    close_log.clear()

    def _tool_factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        adapter = _FreezeAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events_factory=_events_tool_then_text(
                tool_name="calc",
                tool_args={"x": 1},
                final_text="result=2",
            ),
            close_log=close_log,
        )
        log.append(
            {
                "provider_id": provider_definition.id,
                "model_id": model_id,
                "api_key": api_key,
                "adapter": adapter,
            }
        )
        return adapter

    _install_runtime(app, factory=_tool_factory)

    cred = _seed_credential(client, "sk-tool", label="LT")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-tool")

    # Prompt triggers tool loop
    _send_prompt(client, sid, "calc 1+1")

    # Exactly one factory call——the same adapter handled both turns
    assert len(log) == 1
    assert log[0]["provider_id"] == "qwen"
    # The adapter's stream was called at least twice (tool_call + final text)
    adapter_obj = log[0]["adapter"]
    assert adapter_obj.stream_calls >= 2


async def test_tool_loop_kimi_single_factory_call(env: tuple) -> None:
    """Kimi tool loop: same invariant."""
    client, app, log, close_log = env

    log.clear()
    close_log.clear()

    def _tool_factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        adapter = _FreezeAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events_factory=_events_tool_then_text(
                tool_name="search",
                tool_args={"q": "hello"},
                final_text="found",
            ),
            close_log=close_log,
        )
        log.append(
            {
                "provider_id": provider_definition.id,
                "model_id": model_id,
                "api_key": api_key,
            }
        )
        return adapter

    _install_runtime(app, factory=_tool_factory)

    cred = _seed_credential(client, "sk-tool-k", label="LTK")
    prof = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred,
        default_model="kimi-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="kimi-tool")

    _send_prompt(client, sid, "search hello")

    assert len(log) == 1
    assert log[0]["provider_id"] == "kimi"


async def test_tool_loop_secret_resolved_once(env: tuple) -> None:
    """Within a tool loop (multi-turn), resolve_secret_for_request fires once."""
    client, app, log, close_log = env

    log.clear()
    close_log.clear()

    def _tool_factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        adapter = _FreezeAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            events_factory=_events_tool_then_text(),
            close_log=close_log,
        )
        log.append({"provider_id": provider_definition.id, "model_id": model_id})
        return adapter

    _install_runtime(app, factory=_tool_factory)

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    cred_svc = runtime._credential_service
    orig = cred_svc.resolve_secret_for_request
    secret_calls: list[str] = []

    async def _track(credential_id: str) -> str:
        secret_calls.append(credential_id)
        return await orig(credential_id)

    cred_svc.resolve_secret_for_request = _track  # type: ignore[assignment]

    cred = _seed_credential(client, "sk-once", label="LO")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )
    sid = _make_session(client)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-tool-once")

    secret_calls.clear()
    _send_prompt(client, sid, "calc")

    assert len(secret_calls) == 1
    assert secret_calls[0] == cred


# ============================================================================
# §三 Provider request client closed + legacy restored (per provider)
# ============================================================================


async def test_request_client_closed_after_glm_prompt(env: tuple) -> None:
    client, app, _log, close_log = env
    close_log.clear()

    sid, _aid, _ = await _seed_bound_session(
        client,
        provider_id="glm",
        model_id="glm-flash",
        secret="sk-glm-close",
    )
    _send_prompt(client, sid, "hi")

    assert any("glm" in c for c in close_log)


async def test_request_client_closed_after_qwen_prompt(env: tuple) -> None:
    client, app, _log, close_log = env
    close_log.clear()

    sid, _aid, _ = await _seed_bound_session(
        client,
        provider_id="qwen",
        model_id="qwen-flash",
        secret="sk-qwen-close",
    )
    _send_prompt(client, sid, "hi")

    assert any("qwen" in c for c in close_log)


async def test_request_client_closed_after_kimi_prompt(env: tuple) -> None:
    client, app, _log, close_log = env
    close_log.clear()

    sid, _aid, _ = await _seed_bound_session(
        client,
        provider_id="kimi",
        model_id="kimi-flash",
        secret="sk-kimi-close",
    )
    _send_prompt(client, sid, "hi")

    assert any("kimi" in c for c in close_log)

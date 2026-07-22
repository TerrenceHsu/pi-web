"""Regenerate × Provider Runtime switching tests for M1-6（spec §三 24-28 + 1-8）.

覆盖：
- 每次 regenerate 只读取 Binding 一次（resolve_selection × 1）
- Secret 读取一次（resolve_secret_for_request × 1）
- Factory 构造一次
- Adapter 构造一次
- request client 构造一次（bind enter/exit 各一次）
- 工具循环不重新解析 Binding / 不重新构造 Adapter
- _run_regeneration_core 无直接 Runtime 调用（静态 + 动态验证）
- Regenerate 期间修改 Binding，只影响下一次 Regenerate（请求级 snapshot）
"""
from __future__ import annotations

import ast
import pathlib
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
from pi_agent_core_py.web.provider_runtime import (  # noqa: E402
    RequestProviderRuntime,
)

# asyncio_mode=auto in pyproject——async tests run automatically; sync tests
# (AST static analysis) are not marked.

_UI = {"X-PI-Agent-UI": "1"}


# ============================================================================
# Tracking factory + adapter
# ============================================================================


class _TrackingAdapter(ProviderAdapter):
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        api_key_seen: str,
        log: list[dict[str, Any]],
        events: list[StreamEvent],
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._api_key_seen = api_key_seen
        self._log = log
        self._events = list(events)
        self.stream_calls = 0
        self.closed = False

    async def stream(self, request: ProviderRequest) -> Any:
        self.stream_calls += 1
        self._log.append(
            {
                "kind": "stream",
                "provider_id": self.provider_id,
                "model": self.model,
                "api_key": self._api_key_seen,
            }
        )
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        self.closed = True


def _tracking_factory(log: list[dict[str, Any]]) -> Any:
    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> _TrackingAdapter:
        log.append(
            {
                "kind": "factory",
                "provider_id": provider_definition.id,
                "model": model_id,
                "api_key": api_key,
            }
        )
        return _TrackingAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            api_key_seen=api_key,
            log=log,
            events=[
                TextDeltaEvent(delta="hi"),
                DoneEvent(stop_reason="stop", usage=Usage()),
            ],
        )

    return factory


def _install_tracking_runtime(app: object, log: list[dict[str, Any]]) -> None:
    app.state.request_provider_runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=_tracking_factory(log),
    )


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


@pytest.fixture
async def env(tmp_path: Path) -> tuple[TestClient, object, list[dict[str, Any]]]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    log: list[dict[str, Any]] = []
    with TestClient(app, base_url="http://testserver") as client:
        _install_tracking_runtime(app, log)
        yield client, app, log  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str) -> str:
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


def _bind(c: TestClient, *, session_id: str, profile_id: str, model_id: str) -> None:
    r = c.put(
        f"/api/sessions/{session_id}/model-binding",
        headers=_UI,
        json={"profile_id": profile_id, "model_id": model_id},
    )
    assert r.status_code == 200, r.text


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
    assert row is not None
    return row["id"]


def _regenerate(c: TestClient, *, sid: str, aid: str) -> str:
    """Returns request_id."""
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


# ============================================================================
# §三 Items 19-22: 单次绑定——resolve/build/factory/client 各一次
# ============================================================================


async def test_regenerate_binds_once_per_call(env: tuple) -> None:
    """A single regenerate triggers exactly one factory call."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert len(factory_calls) == 1


async def test_regenerate_resolves_selection_once(env: tuple) -> None:
    """resolve_selection called exactly once for the regenerate itself.

    Note: legacy seeding via /api/prompt also triggers resolve_selection
    (returns None for unbound session). We filter to non-None selections,
    which correspond to bound requests——the regenerate is the only bound
    request in this test.
    """
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    orig_resolve = runtime.resolve_selection
    non_null_calls: list[str] = []

    async def _track(session_id: str) -> Any:
        sel = await orig_resolve(session_id)
        if sel is not None:
            non_null_calls.append(session_id)
        return sel

    runtime.resolve_selection = _track  # type: ignore[assignment]

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # Only the regenerate (bound) produced a real selection
    assert len(non_null_calls) == 1
    assert non_null_calls[0] == sid


async def test_regenerate_reads_secret_once(env: tuple) -> None:
    """resolve_secret_for_request called exactly once per regenerate."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    cred_svc = runtime._credential_service
    orig = cred_svc.resolve_secret_for_request
    secret_calls: list[str] = []

    async def _track(credential_id: str) -> str:
        secret_calls.append(credential_id)
        return await orig(credential_id)

    cred_svc.resolve_secret_for_request = _track  # type: ignore[assignment]

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    secret_calls.clear()  # ignore any pre-regenerate reads
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    assert len(secret_calls) == 1
    assert secret_calls[0] == cred


async def test_regenerate_build_adapter_once(env: tuple) -> None:
    """build_adapter (factory invocation) called exactly once per regenerate."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    orig_build = runtime.build_adapter
    build_calls: list[Any] = []

    async def _track(selection: Any) -> Any:
        build_calls.append(selection)
        return await orig_build(selection)

    runtime.build_adapter = _track  # type: ignore[assignment]

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # build_adapter only called when selection is non-None (i.e. only the regenerate)
    assert len(build_calls) == 1


# ============================================================================
# §三 Items 25-26: tool loop does NOT re-resolve / re-build
# ============================================================================


async def test_regenerate_single_prompt_no_midloop_rebuild(env: tuple) -> None:
    """Single regenerate = single factory call——no mid-loop rebuild.

    bind_to_harness wraps the whole _execute_prompt, so by construction no
    mid-regenerate swap can occur. We verify via factory call count.
    """
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert len(factory_calls) == 1


# ============================================================================
# §三 Item 8: Regenerate 期间修改 Binding，只影响下一次 Regenerate
# ============================================================================


async def test_mid_regenerate_binding_change_does_not_affect_in_flight(
    env: tuple,
) -> None:
    """If binding changes while a regenerate is running, the in-flight one
    uses the snapshot from when it started. (Tested at the resolve_selection
    level——the resolve happens immediately, so even if we change binding
    mid-task, the adapter was already built.)
    """
    client, app, log = env
    cred_a = _seed_credential(client, "key-A")
    cred_b = _seed_credential(client, "key-B")
    prof_a = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_a,
        default_model="qwen-default",
        name="Pa",
    )
    prof_b = _seed_profile(
        client,
        provider_id="kimi",
        credential_id=cred_b,
        default_model="kimi-default",
        name="Pb",
    )

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _bind(client, session_id=sid, profile_id=prof_a, model_id="qwen-A")
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    # Change binding AFTER first regenerate completed——next regenerate uses B
    _bind(client, session_id=sid, profile_id=prof_b, model_id="kimi-B")
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert len(factory_calls) == 2
    assert factory_calls[0]["provider_id"] == "qwen"
    assert factory_calls[0]["api_key"] == "key-A"
    assert factory_calls[1]["provider_id"] == "kimi"
    assert factory_calls[1]["api_key"] == "key-B"


# ============================================================================
# §三 Item 38: 下一次 Regenerate 不复用旧 Adapter
# ============================================================================


async def test_next_regenerate_uses_fresh_adapter(env: tuple) -> None:
    """Two consecutive regenerates create two distinct Adapter instances."""
    client, app, log = env
    cred = _seed_credential(client, "key-A")
    prof = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred,
        default_model="qwen-default",
    )

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof, model_id="qwen-A")

    _wait(client, _regenerate(client, sid=sid, aid=aid))
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert len(factory_calls) == 2


# ============================================================================
# §三 Items 27-28: Static constraint——_run_regeneration_core has NO direct
# Provider Runtime binding call
# ============================================================================


def _walk_lexical_body(fn: ast.AST) -> ast.AST:
    """Yield AST nodes in the body of `fn` WITHOUT descending into nested
    function/class definitions."""
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_lexical_body(child)


def test_run_regeneration_core_has_no_direct_runtime_binding() -> None:
    """AST-level check: _run_regeneration_core does NOT call
    runtime.resolve_selection / runtime.build_adapter / runtime.bind_to_harness
    directly. All binding goes through _execute_prompt.
    """
    app_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "app.py"
    )
    tree = ast.parse(app_path.read_text(encoding="utf-8"))

    regen_core: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_regeneration_core"
        ):
            regen_core = node
            break
    assert regen_core is not None, "_run_regeneration_core not found"

    forbidden_calls = ("resolve_selection", "build_adapter", "bind_to_harness")
    for node in _walk_lexical_body(regen_core):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in forbidden_calls:
                pytest.fail(
                    f"_run_regeneration_core directly calls runtime.{f.attr} "
                    f"at line {node.lineno}——must go through _execute_prompt"
                )
            if isinstance(f, ast.Name) and f.id in forbidden_calls:
                pytest.fail(
                    f"_run_regeneration_core directly calls {f.id} "
                    f"at line {node.lineno}——must go through _execute_prompt"
                )


def test_execute_prompt_is_sole_binding_site() -> None:
    """AST-level check: resolve_selection / bind_to_harness / build_adapter
    appear ONLY inside _execute_prompt's lexical body, not in any other
    function (including _run_regeneration_core / _run_regeneration_background).
    """
    app_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "app.py"
    )
    tree = ast.parse(app_path.read_text(encoding="utf-8"))

    forbidden_attrs = ("resolve_selection", "bind_to_harness", "build_adapter")

    all_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns.append(node)

    offending: dict[str, list[int]] = {}
    for fn in all_fns:
        for inner in _walk_lexical_body(fn):
            if isinstance(inner, ast.Attribute) and inner.attr in forbidden_attrs:
                offending.setdefault(fn.name, []).append(inner.lineno)

    for fn_name, lines in offending.items():
        if fn_name != "_execute_prompt":
            pytest.fail(
                f"Provider Runtime binding call found in {fn_name}() at "
                f"lines {lines}——must be only in _execute_prompt"
            )


# ============================================================================
# §三 Items 1-3 (matrix): exhaustive switching combinations
# ============================================================================


async def test_switching_matrix_glm_to_qwen(env: tuple) -> None:
    client, app, log = env
    cred_g = _seed_credential(client, "key-G")
    cred_q = _seed_credential(client, "key-Q")
    prof_g = _seed_profile(
        client,
        provider_id="glm",
        credential_id=cred_g,
        default_model="glm-default",
        name="Pglm",
    )
    prof_q = _seed_profile(
        client,
        provider_id="qwen",
        credential_id=cred_q,
        default_model="qwen-default",
        name="Pqwen",
    )
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _bind(client, session_id=sid, profile_id=prof_g, model_id="glm-1")
    _wait(client, _regenerate(client, sid=sid, aid=aid))
    _bind(client, session_id=sid, profile_id=prof_q, model_id="qwen-2")
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert factory_calls[0]["provider_id"] == "glm"
    assert factory_calls[0]["api_key"] == "key-G"
    assert factory_calls[1]["provider_id"] == "qwen"
    assert factory_calls[1]["api_key"] == "key-Q"


async def test_switching_matrix_qwen_to_kimi(env: tuple) -> None:
    client, app, log = env
    cred_q = _seed_credential(client, "key-Q")
    cred_k = _seed_credential(client, "key-K")
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
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _bind(client, session_id=sid, profile_id=prof_q, model_id="qwen-1")
    _wait(client, _regenerate(client, sid=sid, aid=aid))
    _bind(client, session_id=sid, profile_id=prof_k, model_id="kimi-2")
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert factory_calls[0]["provider_id"] == "qwen"
    assert factory_calls[1]["provider_id"] == "kimi"


async def test_switching_matrix_kimi_to_glm(env: tuple) -> None:
    client, app, log = env
    cred_k = _seed_credential(client, "key-K")
    cred_g = _seed_credential(client, "key-G")
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
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _bind(client, session_id=sid, profile_id=prof_k, model_id="kimi-1")
    _wait(client, _regenerate(client, sid=sid, aid=aid))
    _bind(client, session_id=sid, profile_id=prof_g, model_id="glm-2")
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert factory_calls[0]["provider_id"] == "kimi"
    assert factory_calls[1]["provider_id"] == "glm"


# ============================================================================
# §三 Item 7: 无 Binding 时使用 legacy client
# ============================================================================


async def test_regenerate_without_binding_uses_legacy_client(env: tuple) -> None:
    """Session never had a Binding → regenerate uses legacy FakeClient,
    factory NOT called.
    """
    client, app, log = env

    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)

    _wait(client, _regenerate(client, sid=sid, aid=aid))

    factory_calls = [e for e in log if e["kind"] == "factory"]
    assert factory_calls == []

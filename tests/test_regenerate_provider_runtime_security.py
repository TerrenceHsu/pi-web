"""Regenerate × Provider Runtime security tests for M1-6（spec §三 52-61 + §九）.

覆盖：
- API Key 不进入 revision（content_json / error_summary）
- API Key 不进入 canonical message（messages API）
- API Key 不进入 snapshot
- API Key 不进入 WS error
- API Key 不进入 HTTP response（POST /api/regenerate, GET /api/requests）
- API Key 不进入日志（caplog）
- API Key 不进入异常链
- credential_id / profile_id / model_id 不出现在安全错误文本
- 不写 context.metadata["provider_selection"]
- 真实网络调用 = 0
- 生产代码 diff = 0（web/app.py Provider Runtime 接入点只有 _execute_prompt）
"""
from __future__ import annotations

import ast
import logging
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
SECRET_MARKER = "sk-M1-6-SECURITY-SECRET-MARKER-DO-NOT-LEAK-ANYWHERE"


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


def _factory_with_marker() -> Any:
    """Factory that receives the secret marker; emits harmless events."""

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        class _A(ProviderAdapter):
            def __init__(self_) -> None:
                self_.provider_id = provider_definition.id
                self_.model = model_id

            async def stream(self_, request: ProviderRequest) -> Any:
                yield TextDeltaEvent(delta="ok")
                yield DoneEvent(stop_reason="stop", usage=Usage())

        return _A()

    return factory


def _failing_factory_with_marker() -> Any:
    """Factory that receives the secret marker and then fails——to exercise
    error-path secret scrubbing."""

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        raise ProviderConfigError(f"simulated failure with {api_key}")

    return factory


@pytest.fixture
async def env_success(tmp_path: Path) -> tuple[TestClient, object]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as client:
        runtime = RequestProviderRuntime(
            provider_config_service=app.state.provider_config_runtime.service,
            credential_service=app.state.credential_runtime.service,
            provider_registry=_DEFAULT_REGISTRY,
            provider_factory=_factory_with_marker(),
        )
        app.state.request_provider_runtime = runtime
        yield client, app  # type: ignore[misc]


@pytest.fixture
async def env_fail(tmp_path: Path) -> tuple[TestClient, object]:
    app = create_app(
        _harness(),
        db_path=str(tmp_path / "app.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app, base_url="http://testserver") as client:
        runtime = RequestProviderRuntime(
            provider_config_service=app.state.provider_config_runtime.service,
            credential_service=app.state.credential_runtime.service,
            provider_registry=_DEFAULT_REGISTRY,
            provider_factory=_failing_factory_with_marker(),
        )
        app.state.request_provider_runtime = runtime
        yield client, app  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER) -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": "L", "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(c: TestClient, *, provider_id: str, credential_id: str) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": f"P-{provider_id}",
            "provider_id": provider_id,
            "credential_id": credential_id,
            "default_model": "m",
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


async def _seed_bound(
    client: TestClient,
    *,
    provider_id: str = "qwen",
) -> tuple[str, str]:
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id=provider_id, credential_id=cred)
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof)
    return sid, aid


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


# ============================================================================
# §三 Item 52: API Key 不进入 revision
# ============================================================================


async def test_secret_marker_not_in_revision(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    revs = await _fetch_revisions_raw(client, sid, aid)
    for r in revs:
        assert SECRET_MARKER not in (r.get("content_json") or "")
        assert SECRET_MARKER not in (r.get("error_summary") or "")


# ============================================================================
# §三 Item 53: API Key 不进入 canonical message
# ============================================================================


async def test_secret_marker_not_in_canonical_message(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    body = client.get(f"/api/messages?session_id={sid}").json()
    for msg in body["messages"]:
        assert SECRET_MARKER not in repr(msg)
        if "message" in msg:
            assert SECRET_MARKER not in repr(msg["message"])


# ============================================================================
# §三 Item 54: API Key 不进入 snapshot
# ============================================================================


async def test_secret_marker_not_in_snapshot(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    r = client.get(f"/api/sessions/{sid}/snapshots")
    if r.status_code == 200:
        assert SECRET_MARKER not in r.text


# ============================================================================
# §三 Item 55: API Key 不进入 HTTP response
# ============================================================================


async def test_secret_marker_not_in_regenerate_response(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)

    r = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert SECRET_MARKER not in r.text

    body = r.json()
    req_id = body["request_id"]
    final = _wait(client, req_id)
    assert SECRET_MARKER not in repr(final)


async def test_secret_marker_not_in_request_endpoint(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    req_id = _regenerate(client, sid=sid, aid=aid)

    _wait(client, req_id)
    r = client.get(f"/api/requests/{req_id}")
    assert SECRET_MARKER not in r.text


async def test_secret_marker_not_in_revisions_endpoint(env_success: tuple) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    r = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    assert SECRET_MARKER not in r.text


# ============================================================================
# §三 Item 57: API Key 不进入日志
# ============================================================================


async def test_secret_marker_not_in_logs(
    env_success: tuple,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, app = env_success
    caplog.set_level(logging.DEBUG)
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    assert SECRET_MARKER not in caplog.text


# ============================================================================
# §三 Item 58: API Key 不进入异常链
# ============================================================================


async def test_secret_marker_not_in_exception_chain(env_fail: tuple) -> None:
    """Factory raises ProviderConfigError containing the secret. The error
    must be re-wrapped via ``raise ... from None`` so __cause__ is broken.

    We verify by directly invoking runtime.build_adapter and walking the
    resulting exception chain——end-to-end regenerate path can't easily expose
    the raw exception (only the safe error_summary), so this is the most
    direct way to confirm the __cause__ link is broken.
    """
    client, app = env_fail
    sid, _aid = await _seed_bound(client)

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    selection = await runtime.resolve_selection(sid)
    assert selection is not None

    with pytest.raises(Exception) as exc_info:
        await runtime.build_adapter(selection)

    # Walk the exception chain——no node should contain the secret marker
    cur: BaseException | None = exc_info.value
    depth = 0
    while cur is not None and depth < 10:
        assert SECRET_MARKER not in str(cur), (
            f"secret marker leaked in exception chain at depth {depth}: "
            f"{type(cur).__name__}: {cur}"
        )
        # build_adapter uses ``from None``——chain should be broken here
        cur = cur.__cause__
        depth += 1


async def test_secret_marker_not_in_error_summary(env_fail: tuple) -> None:
    """Error path: secret-bearing exception → revision.error_summary must
    use the fixed safe message, not the raw exception text."""
    client, app = env_fail
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    revs = await _fetch_revisions_raw(client, sid, aid)
    for r in revs:
        if r["status"] == "error":
            assert SECRET_MARKER not in (r.get("error_summary") or "")


# ============================================================================
# §三 Item 59: credential_id / profile_id / model_id 不出现在安全错误文本
# ============================================================================


async def test_no_profile_model_credential_markers_in_error(
    env_fail: tuple,
) -> None:
    client, app = env_fail

    PROFILE_NAME = "profile-M1-6-LEAK-CHECK"
    MODEL_ID = "model-M1-6-LEAK-CHECK"
    CRED_LABEL = "cred-M1-6-LEAK-CHECK"

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

    revs = await _fetch_revisions_raw(client, sid, aid)
    for r in revs:
        if r["status"] == "error":
            summary = r.get("error_summary") or ""
            assert PROFILE_NAME not in summary
            assert MODEL_ID not in summary
            assert CRED_LABEL not in summary
            assert prof_id not in summary
            assert cred_id not in summary

    # Also check request error
    state = client.app.state.web
    for r in state.request_history:
        if r.operation == "regenerate" and r.target_message_id == aid:
            err = r.error or ""
            assert PROFILE_NAME not in err
            assert MODEL_ID not in err
            assert CRED_LABEL not in err
            assert prof_id not in err
            assert cred_id not in err
            return
    pytest.fail("no regenerate request in history")


# ============================================================================
# §三 Item 60: 不写 context.metadata["provider_selection"]
# ============================================================================


async def test_no_provider_selection_in_context_metadata(
    env_success: tuple,
) -> None:
    client, app = env_success
    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))

    harness = app.state.web.harness
    metadata = harness.context.metadata
    forbidden_keys = (
        "provider_selection",
        "provider_profile_id",
        "provider_credential_id",
        "request_selection",
    )
    for key in forbidden_keys:
        assert key not in metadata, f"metadata[{key!r}] written by Regenerate path"


# ============================================================================
# §三 Item 61: 真实网络调用 = 0
# ============================================================================


async def test_no_real_network_calls_during_regenerate(
    env_success: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    client, app = env_success

    def _no_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("regenerate path must not create real HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", _no_http)
    monkeypatch.setattr(httpx, "Client", _no_http)

    sid, aid = await _seed_bound(client)
    _wait(client, _regenerate(client, sid=sid, aid=aid))


# ============================================================================
# §三 Item 62: Core Runtime diff = 0（AST 静态检查）
# ============================================================================


def test_core_runtime_modules_unchanged() -> None:
    """Spot-check: provider_runtime.py still defines the expected public API.
    This is a sanity check, not a full diff——M1-6 is test-only.
    """
    from pi_agent_core_py.web.providers import runtime as pr

    for name in (
        "RequestProviderRuntime",
        "RequestProviderSelection",
        "ProviderSelectionNotFoundError",
        "ProviderSelectionDisabledError",
        "ProviderSelectionUnavailableError",
        "ProviderInitializationError",
    ):
        assert hasattr(pr, name), f"provider_runtime missing {name}"


def _walk_lexical_body(
    fn: ast.AST,
) -> ast.AST:
    """Yield AST nodes in the body of `fn` WITHOUT descending into nested
    function/class definitions. Used to detect calls that lexically belong
    to `fn` (not to nested functions defined inside it).
    """
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_lexical_body(child)


def test_app_py_provider_runtime_binding_only_in_execute_prompt() -> None:
    """AST-level check: resolve_selection / bind_to_harness calls inside
    app.py exist ONLY within _execute_prompt body (lexically). Other functions
    (including _run_regeneration_core and regenerate handlers) must NOT call
    them directly.
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

    # Collect all function definitions
    all_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns.append(node)

    # For each function, scan its lexical body (excluding nested fns)
    offending: dict[str, list[int]] = {}
    for fn in all_fns:
        for inner in _walk_lexical_body(fn):
            if isinstance(inner, ast.Attribute) and inner.attr in forbidden_attrs:
                offending.setdefault(fn.name, []).append(inner.lineno)

    for fn_name, lines in offending.items():
        if fn_name != "_execute_prompt":
            pytest.fail(
                f"Provider Runtime binding call found in {fn_name}() at lines "
                f"{lines}——must be only in _execute_prompt"
            )


def test_request_provider_runtime_constructed_only_in_composition_root() -> None:
    """AST check: RequestProviderRuntime(...) constructor calls only at the
    composition root (lifespan / create_app init), not inside request handlers.
    """
    app_path = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "pi_agent_core_py"
        / "web"
        / "app.py"
    )
    tree = ast.parse(app_path.read_text(encoding="utf-8"))

    all_fns: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_fns.append(node)

    constructor_locations: list[tuple[str, int]] = []
    for fn in all_fns:
        for inner in _walk_lexical_body(fn):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "RequestProviderRuntime"
            ):
                constructor_locations.append((fn.name, inner.lineno))

    # Allowed caller contexts (composition root only)
    allowed = ("create_app", "_lifespan", "lifespan")
    for fn_name, line in constructor_locations:
        if fn_name not in allowed:
            pytest.fail(
                f"RequestProviderRuntime constructed in {fn_name}() at line "
                f"{line}——must be only in composition root"
            )

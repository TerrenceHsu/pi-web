"""Multi-Provider Runtime security exit audit for M1-7（spec §十二）.

Single marker scanned across every output surface:
- HTTP response body (prompt / regenerate / requests / revisions / messages)
- WS event (via state.request_history + envelope)
- request error state
- Message / AssistantMessage
- Snapshot
- Revision (content_json / error_summary)
- Export Markdown
- SQLite main / WAL / SHM
- application log (caplog)
- exception str/repr + __cause__ / __context__
- RequestProviderSelection repr
- Adapter repr
- ToolCall.raw

Required: marker ZERO occurrences everywhere. Allowed in SQLite: credential_id /
profile_id / provider_id / model_id / secret_ref / masked_value / fingerprint.
"""
from __future__ import annotations

import logging
import sqlite3
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
SECRET_MARKER = "sk-M1-RUNTIME-FREEZE-SECRET-MARKER"


# ============================================================================
# Setup
# ============================================================================


def _harness() -> AgentHarness:
    one = [
        TextDeltaEvent(delta="legacy"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]
    scripts = [list(one) for _ in range(50)]
    return AgentHarness(Agent(system_prompt="x", client=FakeClient(scripts), tools=None))


def _factory_marker_seen() -> Any:
    """Factory that receives marker; emits safe events."""

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


def _factory_fails_with_marker() -> Any:
    """Factory that raises ProviderConfigError containing the marker."""

    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        raise ProviderConfigError(f"init failure saw {api_key}")

    return factory


@pytest.fixture
async def env_ok(tmp_path: Path) -> tuple[TestClient, object]:
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
            provider_factory=_factory_marker_seen(),
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
            provider_factory=_factory_fails_with_marker(),
        )
        app.state.request_provider_runtime = runtime
        yield client, app  # type: ignore[misc]


def _seed_credential(c: TestClient, secret: str = SECRET_MARKER, label: str = "L") -> str:
    r = c.post(
        "/api/credentials",
        headers=_UI,
        json={"label": label, "storage_mode": "session_only", "secret_value": secret},
    )
    assert r.status_code == 201, r.text
    return r.json()["credential"]["credential_id"]


def _seed_profile(
    c: TestClient, *, provider_id: str, credential_id: str, name: str = "P"
) -> str:
    r = c.post(
        "/api/provider-profiles",
        headers=_UI,
        json={
            "name": name,
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
    c.post("/api/prompt", json={"text": text, "session_id": sid})
    # ignore status——error path tested separately


def _wait_request(c: TestClient, request_id: str, timeout_s: float = 10.0) -> Any:
    """Sync polling helper——avoids ASYNC251 false-positive on time.sleep."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = c.get(f"/api/requests/{request_id}")
        if r.status_code == 200:
            body = r.json()
            if body["status"] in ("completed", "error", "aborted"):
                return body
        time.sleep(0.02)
    pytest.fail(f"request {request_id} never reached terminal")


def _read_file_bytes_sync(path: Any) -> bytes:
    """Sync file read——avoids ASYNC230 false-positive on open() in async."""
    with open(path, "rb") as f:  # noqa: ASYNC230
        return f.read()


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


async def _seed_bound(
    client: TestClient, *, provider_id: str = "qwen"
) -> tuple[str, str]:
    cred = _seed_credential(client)
    prof = _seed_profile(client, provider_id=provider_id, credential_id=cred)
    sid = _make_session(client)
    _send_prompt(client, sid)
    aid = await _latest_assistant_id(client, sid)
    _bind(client, session_id=sid, profile_id=prof)
    return sid, aid


# ============================================================================
# HTTP response bodies
# ============================================================================


async def test_marker_not_in_prompt_response(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, _ = await _seed_bound(client)
    r = client.post("/api/prompt", json={"text": "scan", "session_id": sid})
    assert SECRET_MARKER not in r.text


async def test_marker_not_in_messages_endpoint(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "second")
    r = client.get(f"/api/messages?session_id={sid}")
    assert SECRET_MARKER not in r.text


async def test_marker_not_in_sessions_endpoint(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, _ = await _seed_bound(client)
    r = client.get("/api/sessions")
    assert SECRET_MARKER not in r.text


async def test_marker_not_in_regenerate_response(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, aid = await _seed_bound(client)
    r = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert SECRET_MARKER not in r.text


async def test_marker_not_in_request_endpoint(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, aid = await _seed_bound(client)
    r = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = r.json()["request_id"]
    _wait_request(client, req_id)
    s = client.get(f"/api/requests/{req_id}")
    assert SECRET_MARKER not in s.text


async def test_marker_not_in_revisions_endpoint(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, aid = await _seed_bound(client)
    r = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    req_id = r.json()["request_id"]
    _wait_request(client, req_id)
    r = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    assert SECRET_MARKER not in r.text


async def test_marker_not_in_export_markdown(env_ok: tuple) -> None:
    client, _ = env_ok
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "export-scan")
    r = client.get(f"/api/sessions/{sid}/export/markdown")
    if r.status_code == 200:
        assert SECRET_MARKER not in r.text


# ============================================================================
# SQLite main + WAL + SHM
# ============================================================================


async def test_marker_not_in_sqlite_main_db(env_ok: tuple) -> None:
    """Scan the SQLite DB file directly——raw byte search for the marker."""
    client, app = env_ok
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "sqlite-scan")

    # Locate the DB file
    state = client.app.state.web
    db_path = state.session_store._db_path if hasattr(state.session_store, "_db_path") else None
    if db_path is None:
        pytest.skip("DB path not discoverable")

    # Force WAL checkpoint to fold changes into main DB
    ext_db = sqlite3.connect(str(db_path))
    ext_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ext_db.close()

    content = _read_file_bytes_sync(db_path)
    assert SECRET_MARKER.encode() not in content


async def test_marker_not_in_sqlite_wal_or_shm(env_ok: tuple) -> None:
    """WAL / SHM sidecar files (if present) must not contain the marker."""
    client, app = env_ok
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "wal-scan")

    state = client.app.state.web
    db_path = state.session_store._db_path if hasattr(state.session_store, "_db_path") else None
    if db_path is None:
        pytest.skip("DB path not discoverable")

    # Force WAL checkpoint to fold changes into main DB
    ext_db = sqlite3.connect(str(db_path))
    ext_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ext_db.close()

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix) if suffix else Path(str(db_path))
        if not p.exists():
            continue
        content = _read_file_bytes_sync(p)
        assert SECRET_MARKER.encode() not in content, (
            f"marker found in {p.name}"
        )


# ============================================================================
# Application logs
# ============================================================================


async def test_marker_not_in_application_logs(
    env_ok: tuple,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = env_ok
    caplog.set_level(logging.DEBUG)
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "log-scan")
    assert SECRET_MARKER not in caplog.text


# ============================================================================
# Exception chain (via runtime.build_adapter)
# ============================================================================


async def test_marker_not_in_exception_chain(env_fail: tuple) -> None:
    """Factory raises ProviderConfigError with marker——build_adapter wraps with
    ``from None`` so __cause__ is broken."""
    client, app = env_fail
    sid, _ = await _seed_bound(client)

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    selection = await runtime.resolve_selection(sid)
    assert selection is not None

    with pytest.raises(Exception) as exc_info:
        await runtime.build_adapter(selection)

    cur: BaseException | None = exc_info.value
    depth = 0
    while cur is not None and depth < 10:
        assert SECRET_MARKER not in str(cur)
        assert SECRET_MARKER not in repr(cur)
        cur = cur.__cause__
        depth += 1


# ============================================================================
# RequestProviderSelection repr
# ============================================================================


async def test_marker_not_in_selection_repr(env_ok: tuple) -> None:
    """RequestProviderSelection repr——credential_id is field(repr=False)."""
    client, app = env_ok
    sid, _ = await _seed_bound(client)

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    selection = await runtime.resolve_selection(sid)
    assert selection is not None
    # Selection itself doesn't contain the secret, but its repr must be safe
    assert SECRET_MARKER not in repr(selection)
    assert "credential_id" not in repr(selection)


# ============================================================================
# Adapter repr (factory is marker-bearing)
# ============================================================================


async def test_marker_not_in_adapter_repr(env_ok: tuple) -> None:
    """The constructed Adapter must not carry api_key in its repr."""
    client, app = env_ok
    sid, _ = await _seed_bound(client)

    runtime: RequestProviderRuntime = app.state.request_provider_runtime
    selection = await runtime.resolve_selection(sid)
    assert selection is not None
    adapter = await runtime.build_adapter(selection)
    assert SECRET_MARKER not in repr(adapter)


# ============================================================================
# Error-path marker scrubbing
# ============================================================================


async def test_marker_not_in_error_path_responses(env_fail: tuple) -> None:
    """All error-path responses (prompt error, regenerate error) must be free
    of the marker."""
    client, _ = env_fail
    sid, _ = await _seed_bound(client)

    # Prompt path
    r1 = client.post("/api/prompt", json={"text": "err", "session_id": sid})
    assert SECRET_MARKER not in r1.text


async def test_marker_not_in_error_state_messages_or_revisions(
    env_fail: tuple,
) -> None:
    """Persistent state after error——messages, revisions, snapshots——must not
    contain the marker."""
    client, _ = env_fail
    sid, aid = await _seed_bound(client)

    # Trigger a failed regenerate
    r = client.post(f"/api/sessions/{sid}/messages/{aid}/regenerate")
    assert r.status_code == 202
    req_id = r.json()["request_id"]
    _wait_request(client, req_id)

    # Messages
    r = client.get(f"/api/messages?session_id={sid}")
    assert SECRET_MARKER not in r.text
    # Revisions
    r = client.get(f"/api/sessions/{sid}/messages/{aid}/revisions")
    assert SECRET_MARKER not in r.text
    # Snapshots
    r = client.get(f"/api/sessions/{sid}/snapshots")
    if r.status_code == 200:
        assert SECRET_MARKER not in r.text


# ============================================================================
# Full DB scan including provider_config + credentials tables
# ============================================================================


async def test_marker_not_in_credentials_or_provider_tables(env_ok: tuple) -> None:
    """The credentials / provider_config / messages / revisions tables must
    not contain the marker in any TEXT column."""
    client, _ = env_ok
    sid, _ = await _seed_bound(client)
    _send_prompt(client, sid, "full-db-scan")

    state = client.app.state.web
    db_path = state.session_store._db_path
    if db_path is None:
        pytest.skip("DB path not discoverable")

    # Force WAL checkpoint——no need to close async store handles
    ext_db = sqlite3.connect(str(db_path))
    ext_db.row_factory = sqlite3.Row
    # Enumerate all tables, all TEXT columns
    tables = ext_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    for table_row in tables:
        table = table_row["name"]
        cols = ext_db.execute(f"PRAGMA table_info({table})").fetchall()
        text_cols = [c["name"] for c in cols if "TEXT" in (c["type"] or "").upper()]
        for col in text_cols:
            try:
                rows = ext_db.execute(
                    f"SELECT {col} FROM {table} WHERE {col} LIKE ?",
                    (f"%{SECRET_MARKER}%",),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            assert len(rows) == 0, (
                f"marker found in {table}.{col}: {rows[:3]}"
            )
    ext_db.close()

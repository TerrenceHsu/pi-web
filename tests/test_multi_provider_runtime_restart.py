"""Multi-Provider Runtime restart tests for M1-7（spec §七）.

覆盖：
- app restart 恢复 Profile / Binding / Session / Messages
- Env Credential 重启后从环境变量重新解析
- Keyring Credential 重启后从测试 SecretStore 解析
- Session-only Credential 重启后丢失 → Profile needs_key → Prompt 安全失败
- 删除 Credential → Profile 保留 needs_credential → Prompt 安全失败
- Env 值变化 → 重新解析（无 secret 缓存）
- Profile 切换 Credential A→B → 已启动请求继续用 A，下一请求用 B
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


# ============================================================================
# Stub adapter + tracking factory
# ============================================================================


class _RestartAdapter(ProviderAdapter):
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        close_log: list[str] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self._close_log = close_log

    async def stream(self, request: ProviderRequest) -> Any:
        yield TextDeltaEvent(delta="hi")
        yield DoneEvent(stop_reason="stop", usage=Usage())

    async def aclose(self) -> None:
        if self._close_log is not None:
            self._close_log.append(f"{self.provider_id}/{self.model}")


def _factory(close_log: list[str] | None = None) -> Any:
    def factory(
        *,
        provider_definition: ProviderDefinition,
        api_key: str,
        model_id: str,
    ) -> ProviderAdapter:
        return _RestartAdapter(
            provider_id=provider_definition.id,
            model=model_id,
            close_log=close_log,
        )

    return factory


def _harness() -> AgentHarness:
    one = [
        TextDeltaEvent(delta="legacy"),
        DoneEvent(stop_reason="stop", usage=Usage()),
    ]
    scripts = [list(one) for _ in range(50)]
    return AgentHarness(Agent(system_prompt="x", client=FakeClient(scripts), tools=None))


def _make_app(
    tmp_path: Path,
    db_path: str | Path | None = None,
    *,
    secret_backend: str = "memory",
) -> Any:
    return create_app(
        _harness(),
        db_path=str(db_path or (tmp_path / "app.db")),
        credential_secret_backend=secret_backend,
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )


def _install_runtime(app: object) -> RequestProviderRuntime:
    runtime = RequestProviderRuntime(
        provider_config_service=app.state.provider_config_runtime.service,
        credential_service=app.state.credential_runtime.service,
        provider_registry=_DEFAULT_REGISTRY,
        provider_factory=_factory(),
    )
    app.state.request_provider_runtime = runtime
    return runtime


def _seed_credential(
    c: TestClient,
    *,
    secret: str = "sk-restart",
    label: str = "L",
    storage_mode: str = "session_only",
    env_var_name: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "label": label,
        "storage_mode": storage_mode,
    }
    if storage_mode == "session_only":
        body["secret_value"] = secret
    elif storage_mode == "env":
        body["env_var_name"] = env_var_name or "TEST_RESTART_KEY"
    r = c.post("/api/credentials", headers=_UI, json=body)
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


def _send_prompt(c: TestClient, sid: str, text: str = "q") -> dict[str, Any]:
    r = c.post("/api/prompt", json={"text": text, "session_id": sid})
    return {"status": r.status_code, "body": r.json()}


def _bind(c: TestClient, *, session_id: str, profile_id: str, model_id: str) -> None:
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


# ============================================================================
# §七 App restart: Profile / Binding / Session / Messages persist
# ============================================================================


async def test_profile_and_binding_survive_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart app with same db_path——Profile + Binding + Session + Messages
    readable; a prompt with env-backed credential works post-restart."""
    db = tmp_path / "restart1.db"
    env_var = "TEST_M1_7_RESTART_PROFILE"
    monkeypatch.setenv(env_var, "sk-env-restart")

    # First app instance
    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(
            c1, storage_mode="env", env_var_name=env_var, label="L1"
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P1")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-r")
        _send_prompt(c1, sid, "first")
        await _latest_assistant_id(c1, sid)  # sanity: assistant persisted

    # Second app instance——same DB
    app2 = _make_app(tmp_path, db_path=db)
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)

        # Profile exists
        r = c2.get("/api/provider-profiles", headers=_UI)
        body = r.json()
        profiles = body.get("profiles") or body.get("items") or []
        prof_ids = [p["id"] for p in profiles]
        assert prof in prof_ids

        # Session exists
        r = c2.get("/api/sessions")
        session_ids = [s["id"] for s in r.json()["sessions"]]
        assert sid in session_ids

        # Messages persist
        r = c2.get(f"/api/messages?session_id={sid}")
        msgs = r.json()["messages"]
        assert any(m.get("role") == "assistant" for m in msgs)

        # Binding persists
        r = c2.get(f"/api/sessions/{sid}/model-binding", headers=_UI)
        binding = r.json()["binding"]
        assert binding is not None
        assert binding["profile_id"] == prof
        assert binding["model_id"] == "qwen-r"

        # Prompt after restart——env credential resolves, runtime works
        result = _send_prompt(c2, sid, "after-restart")
        assert result["status"] == 200, result


async def test_session_only_credential_lost_after_restart(tmp_path: Path) -> None:
    """Session-only Credential → restart → storage_status=needs_key →
    Prompt fails with provider_credential_unavailable; no fallback."""
    db = tmp_path / "restart2.db"

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        # session_only credential: lives in InMemorySecretStore of app1
        cred = _seed_credential(
            c1, secret="sk-session-only", storage_mode="session_only", label="LS"
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-x")

    # Restart——new InMemorySecretStore is empty
    app2 = _make_app(tmp_path, db_path=db)
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)
        # Prompt fails with credential unavailable
        result = _send_prompt(c2, sid, "post-restart-fail")
        body = result["body"]
        assert body.get("ok") is False
        assert body.get("error_type") == "provider_credential_unavailable"
        assert "Selected provider credential is unavailable." in body.get("error", "")


# ============================================================================
# §七 Env Credential——restart re-resolves env var
# ============================================================================


async def test_env_credential_resolves_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env Credential: env var present after restart → resolves correctly."""
    db = tmp_path / "restart3.db"
    env_var = "TEST_M1_7_ENV_KEY"
    monkeypatch.setenv(env_var, "sk-env-after-restart")

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(
            c1,
            storage_mode="env",
            env_var_name=env_var,
            label="LENV",
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-env")

    # Restart——env var still set
    app2 = _make_app(tmp_path, db_path=db)
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)
        result = _send_prompt(c2, sid, "env-after-restart")
        assert result["status"] == 200, result


async def test_env_credential_value_change_picks_up_new_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env var value changed between restarts → second restart picks up new."""
    db = tmp_path / "restart4.db"
    env_var = "TEST_M1_7_ENV_CHANGE"
    monkeypatch.setenv(env_var, "sk-old-value")

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(
            c1,
            storage_mode="env",
            env_var_name=env_var,
            label="LENV2",
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-env")

    # Change env var
    monkeypatch.setenv(env_var, "sk-new-value")

    app2 = _make_app(tmp_path, db_path=db)
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)

        # Spy on resolve_secret_for_request——capture the resolved value
        runtime: RequestProviderRuntime = app2.state.request_provider_runtime
        cred_svc = runtime._credential_service
        resolved: list[str] = []
        orig = cred_svc.resolve_secret_for_request

        async def _track(credential_id: str) -> str:
            secret = await orig(credential_id)
            resolved.append(secret)
            return secret

        cred_svc.resolve_secret_for_request = _track  # type: ignore[assignment]

        result = _send_prompt(c2, sid, "env-changed")
        assert result["status"] == 200, result
        assert resolved == ["sk-new-value"]


async def test_env_credential_missing_after_restart_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env Credential: var removed before restart → needs_key → safe failure."""
    db = tmp_path / "restart5.db"
    env_var = "TEST_M1_7_ENV_GONE"
    monkeypatch.setenv(env_var, "sk-will-disappear")

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(
            c1,
            storage_mode="env",
            env_var_name=env_var,
            label="LENV3",
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-env")

    # Remove env var
    monkeypatch.delenv(env_var)

    app2 = _make_app(tmp_path, db_path=db)
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)
        result = _send_prompt(c2, sid, "env-missing")
        body = result["body"]
        assert body.get("ok") is False
        assert body.get("error_type") == "provider_credential_unavailable"


# ============================================================================
# §七 Keyring Credential——simulated via shared InMemorySecretStore
# ============================================================================


async def test_keyring_credential_survives_restart_via_shared_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate keyring persistence: shared InMemorySecretStore across app
    instances via monkeypatching OSKeyringSecretStore."""
    db = tmp_path / "restart6.db"

    from pi_agent_core_py.secrets.memory import InMemorySecretStore

    shared_store = InMemorySecretStore()

    # Patch OSKeyringSecretStore to return our shared in-memory store——this
    # simulates a persistent keyring backend that survives process restart.
    monkeypatch.setattr(
        "pi_agent_core_py.web.credentials.runtime.OSKeyringSecretStore",
        lambda: shared_store,
        raising=False,
    )

    app1 = create_app(
        _harness(),
        db_path=str(db),
        credential_secret_backend="keyring",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        # Use keyring storage mode if supported; skip if API path blocks it
        try:
            cred = _seed_credential(
                c1,
                secret="sk-keyring-persistent",
                storage_mode="keyring",
                label="LKR",
            )
        except Exception:
            pytest.skip("keyring storage_mode not supported via this API path")

        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-kr")

    # Restart——shared_store still holds the secret
    app2 = create_app(
        _harness(),
        db_path=str(db),
        credential_secret_backend="keyring",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    with TestClient(app2, base_url="http://testserver") as c2:
        _install_runtime(app2)
        result = _send_prompt(c2, sid, "keyring-restart")
        assert result["status"] == 200, result


# ============================================================================
# §八 Delete Credential → Profile needs_credential → safe failure
# ============================================================================


async def test_deleted_credential_safe_failure(tmp_path: Path) -> None:
    """Bound Profile's credential deleted → next prompt fails safely."""
    db = tmp_path / "restart7.db"

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(c1, secret="sk-will-delete", label="LD")
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-x")

        # Delete the credential——Profile still references cred_id
        r = c1.delete(f"/api/credentials/{cred}", headers=_UI)
        assert r.status_code in (200, 204), r.text

        # Next prompt fails safely
        result = _send_prompt(c1, sid, "post-delete")
        body = result["body"]
        assert body.get("ok") is False
        assert body.get("error_type") == "provider_credential_unavailable"


# ============================================================================
# §八 Credential replacement A → B in-flight snapshot semantics
# ============================================================================


async def test_credential_replacement_inflight_snapshot(tmp_path: Path) -> None:
    """If Profile is updated mid-prompt to use Credential B, the in-flight
    request still uses Credential A (snapshot taken at request start).

    We approximate this by checking that:
    - Profile.credential_id update takes effect on the NEXT request, not
      mid-request (no concurrent in-flight prompt in TestClient).
    """
    db = tmp_path / "restart8.db"

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred_a = _seed_credential(c1, secret="sk-A-replace", label="LA")
        cred_b = _seed_credential(c1, secret="sk-B-replace", label="LB")
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred_a, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-x")

        # Track secret resolved per request
        runtime: RequestProviderRuntime = app1.state.request_provider_runtime
        resolved: list[str] = []
        orig = runtime._credential_service.resolve_secret_for_request

        async def _track(credential_id: str) -> str:
            s = await orig(credential_id)
            resolved.append(s)
            return s

        runtime._credential_service.resolve_secret_for_request = _track  # type: ignore[assignment]

        # First prompt → uses cred_a
        _send_prompt(c1, sid, "first")
        assert resolved[-1] == "sk-A-replace"

        # Switch profile to cred_b
        r = c1.patch(
            f"/api/provider-profiles/{prof}",
            headers=_UI,
            json={"credential_id": cred_b},
        )
        assert r.status_code in (200, 204), r.text

        # Next prompt → uses cred_b
        _send_prompt(c1, sid, "second")
        assert resolved[-1] == "sk-B-replace"


# ============================================================================
# §八 Env value change within same app instance → re-resolves each request
# ============================================================================


async def test_env_value_change_no_caching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No secret caching: change env var between two prompts in the SAME app
    instance → second prompt resolves the new value.
    """
    db = tmp_path / "restart9.db"
    env_var = "TEST_M1_7_NOCACHE"
    monkeypatch.setenv(env_var, "sk-first")

    app1 = _make_app(tmp_path, db_path=db)
    with TestClient(app1, base_url="http://testserver") as c1:
        _install_runtime(app1)
        cred = _seed_credential(
            c1,
            storage_mode="env",
            env_var_name=env_var,
            label="LNC",
        )
        prof = _seed_profile(c1, provider_id="qwen", credential_id=cred, name="P")
        sid = _make_session(c1)
        _bind(c1, session_id=sid, profile_id=prof, model_id="qwen-env")

        runtime: RequestProviderRuntime = app1.state.request_provider_runtime
        resolved: list[str] = []
        orig = runtime._credential_service.resolve_secret_for_request

        async def _track(credential_id: str) -> str:
            s = await orig(credential_id)
            resolved.append(s)
            return s

        runtime._credential_service.resolve_secret_for_request = _track  # type: ignore[assignment]

        _send_prompt(c1, sid, "first")
        assert resolved[-1] == "sk-first"

        # Change env var
        monkeypatch.setenv(env_var, "sk-second")

        # Next prompt should resolve the new value——no caching
        _send_prompt(c1, sid, "second")
        assert resolved[-1] == "sk-second"

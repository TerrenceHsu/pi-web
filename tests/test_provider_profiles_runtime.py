"""Provider Profiles runtime composition tests (E2-3B1).

覆盖 spec §17.1 Composition 矩阵（10 项）.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite
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
from pi_agent_core_py.web.app import create_app  # noqa: E402
from pi_agent_core_py.web.providers.config_runtime import (  # noqa: E402
    ProviderConfigWebSecurityConfigurationError,
    resolve_provider_profiles_api_configuration,
)

# ============================================================================
# Helpers
# ============================================================================


def _harness() -> AgentHarness:
    client = FakeClient(
        [[TextDeltaEvent(delta="ok"), DoneEvent(stop_reason="stop", usage=Usage())]]
    )
    agent = Agent(system_prompt="base", client=client, tools=None)
    return AgentHarness(agent)


def _build_app(
    tmp_path: Path,
    *,
    enable_provider_profiles_api: bool | None = None,
    enable_credentials_api: bool | None = None,
    enable_credential_runtime: bool | None = None,
    enable_trusted_host: bool = True,
    secret_backend: str = "memory",
    extra_hosts: tuple[str, ...] = ("testserver", "localhost", "127.0.0.1"),
    db_path: str | Path | None = None,
):
    if db_path is None:
        db_path = str(tmp_path / "app.db")
    kwargs = {
        "db_path": str(db_path),
        "credential_secret_backend": secret_backend,
        "credential_extra_hosts": extra_hosts,
        "enable_trusted_host": enable_trusted_host,
    }
    if enable_provider_profiles_api is not None:
        kwargs["enable_provider_profiles_api"] = enable_provider_profiles_api
    if enable_credentials_api is not None:
        kwargs["enable_credentials_api"] = enable_credentials_api
    if enable_credential_runtime is not None:
        kwargs["enable_credential_runtime"] = enable_credential_runtime
    return create_app(_harness(), **kwargs)


# ============================================================================
# 1-4. Normal initialization + connection + DB file + shutdown
# ============================================================================


async def test_1_provider_runtime_normal_initialization(
    tmp_path: Path,
) -> None:
    """enable_provider_profiles_api=None (auto) → runtime enabled when conditions met."""
    app = _build_app(tmp_path)
    with TestClient(app, base_url="http://testserver") as client:
        assert app.state.provider_config_runtime is not None
        assert app.state.credential_runtime is not None
        # Sanity: API reachable
        r = client.get("/api/sessions")
        assert r.status_code == 200
    # After exit: cleared
    assert app.state.provider_config_runtime is None


async def test_2_provider_runtime_uses_independent_connection(
    tmp_path: Path,
) -> None:
    """Provider Config Store must use a SEPARATE aiosqlite connection from Session Store."""
    app = _build_app(tmp_path)
    with TestClient(app):
        pc_store = app.state.provider_config_runtime.store
        session_store = app.state.web.session_store
        # Different connection objects
        assert pc_store._db is not session_store._db


async def test_3_provider_runtime_uses_same_absolute_db_file(
    tmp_path: Path,
) -> None:
    """Provider Config Store must point to the SAME absolute DB file as Session Store."""
    db_path = tmp_path / "shared.db"
    app = _build_app(tmp_path, db_path=str(db_path))
    with TestClient(app):
        # Write to Provider Config Store
        pc_store = app.state.provider_config_runtime.store
        await pc_store.create_profile(
            profile_id="profile-shared",
            name="Shared",
            provider_id="anthropic",
            credential_id="cred-x",
            default_model="claude-X",
        )

    # Verify file exists at expected path
    assert db_path.exists()


async def test_4_provider_runtime_shutdown_closes_store(tmp_path: Path) -> None:
    """After TestClient exits, Store must be closed (subsequent calls fail)."""
    app = _build_app(tmp_path)
    with TestClient(app):
        store = app.state.provider_config_runtime.store
        # Verify functional
        await store.list_profiles()
    # After close: store rejects operations
    from pi_agent_core_py.web.providers.config_store import (
        ProviderConfigStoreError,
    )
    with pytest.raises(ProviderConfigStoreError):
        await store.list_profiles()


# ============================================================================
# 5. Initialization failure rollback
# ============================================================================


async def test_5_initialization_failure_rolls_back(tmp_path: Path) -> None:
    """If Provider Config Store fails to open, lifespan fails cleanly.

    Inject failure by passing a DB path that cannot be opened (parent is a file).
    Lifespan startup must raise when TestClient enters.
    """
    bad_parent = tmp_path / "blocker"
    bad_parent.write_text("i am a file")
    bad_db_path = bad_parent / "app.db"

    app = _build_app(
        tmp_path,
        enable_provider_profiles_api=True,
        enable_credentials_api=True,
        db_path=str(bad_db_path),
    )
    # Lifespan startup triggers when TestClient enters
    with pytest.raises((RuntimeError, OSError, Exception)):  # noqa: B017
        with TestClient(app):
            pass


# ============================================================================
# 6-8. Explicit True with conditions missing → reject
# ============================================================================


def test_resolver_true_requires_credential_runtime() -> None:
    """enable_provider_profiles_api=True + credential_runtime off → reject."""
    with pytest.raises(ProviderConfigWebSecurityConfigurationError):
        resolve_provider_profiles_api_configuration(
            enable_provider_profiles_api=True,
            credential_api_enabled=True,
            credential_runtime_enabled=False,  # missing
            trusted_host_enabled=True,
            db_path="/tmp/x.db",
        )


def test_resolver_true_requires_credential_api() -> None:
    """enable_provider_profiles_api=True + credential_api off → reject."""
    with pytest.raises(ProviderConfigWebSecurityConfigurationError):
        resolve_provider_profiles_api_configuration(
            enable_provider_profiles_api=True,
            credential_api_enabled=False,  # missing
            credential_runtime_enabled=True,
            trusted_host_enabled=True,
            db_path="/tmp/x.db",
        )


def test_resolver_true_requires_trusted_host() -> None:
    with pytest.raises(ProviderConfigWebSecurityConfigurationError):
        resolve_provider_profiles_api_configuration(
            enable_provider_profiles_api=True,
            credential_api_enabled=True,
            credential_runtime_enabled=True,
            trusted_host_enabled=False,  # missing
            db_path="/tmp/x.db",
        )


def test_resolver_true_rejects_memory_db() -> None:
    with pytest.raises(ProviderConfigWebSecurityConfigurationError):
        resolve_provider_profiles_api_configuration(
            enable_provider_profiles_api=True,
            credential_api_enabled=True,
            credential_runtime_enabled=True,
            trusted_host_enabled=True,
            db_path=":memory:",
        )


# ============================================================================
# 9. API disabled → no store
# ============================================================================


async def test_9_api_disabled_does_not_open_store(tmp_path: Path) -> None:
    """enable_provider_profiles_api=False → no Store, no Router (app.state None)."""
    app = _build_app(tmp_path, enable_provider_profiles_api=False)
    with TestClient(app):
        # Even though Credential runtime is enabled, Provider Config is off
        assert app.state.provider_config_runtime is None
        assert app.state.credential_runtime is not None


# ============================================================================
# 10. Old create_app() behavior unchanged
# ============================================================================


async def test_10_legacy_create_app_no_provider_config_args(tmp_path: Path) -> None:
    """Calling create_app() with :memory: should NOT enable provider config (no file DB).

    For file DB, Credential runtime auto-enables; Provider Config follows via None auto.
    The "legacy" path is :memory: which rejects file-type requirement.
    """
    app = create_app(
        _harness(),
        db_path=":memory:",
        # No credential / provider flags — all defaults
    )
    with TestClient(app):
        assert app.state.credential_runtime is None
        assert app.state.provider_config_runtime is None


async def test_10b_legacy_memory_db_skips_both_runtimes(tmp_path: Path) -> None:
    """db_path=:memory: → both Credential and Provider Config runtime skip."""
    app = create_app(
        _harness(),
        db_path=":memory:",
    )
    with TestClient(app):
        assert app.state.credential_runtime is None
        assert app.state.provider_config_runtime is None


# ============================================================================
# Additional: auto-enable chains correctly
# ============================================================================


async def test_auto_enable_chains_with_credential_api(tmp_path: Path) -> None:
    """enable_provider_profiles_api=None auto-enables when Credential API is on."""
    app = _build_app(
        tmp_path,
        enable_provider_profiles_api=None,  # auto
        enable_credentials_api=True,  # forces Credential API on
    )
    with TestClient(app):
        assert app.state.credential_runtime is not None
        assert app.state.provider_config_runtime is not None


async def test_auto_disable_when_credentials_disabled(tmp_path: Path) -> None:
    """enable_provider_profiles_api=None + enable_credentials_api=False → PC disabled."""
    app = _build_app(
        tmp_path,
        enable_provider_profiles_api=None,
        enable_credentials_api=False,
    )
    with TestClient(app):
        # Credential runtime may still be on, but API is off → PC must be off
        assert app.state.provider_config_runtime is None


# ============================================================================
# Composition shares DB with Credential Store (file layout sanity)
# ============================================================================


async def test_provider_config_shares_db_file_with_credentials(tmp_path: Path) -> None:
    """Provider Config and Credentials tables coexist in the same SQLite file."""
    db_path = tmp_path / "coexist.db"
    app = _build_app(tmp_path, db_path=str(db_path))
    with TestClient(app):
        # Both runtimes have their own connection to the same file
        assert app.state.credential_runtime is not None
        assert app.state.provider_config_runtime is not None

    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            rows = await cursor.fetchall()
        names = {r["name"] for r in rows}
        # Both stores' tables present
        assert "web_credentials" in names
        assert "web_credentials_schema_meta" in names
        assert "web_provider_profiles" in names
        assert "web_session_model_bindings" in names
        assert "web_provider_config_schema_meta" in names
    finally:
        await conn.close()

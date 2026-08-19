"""Regression tests for safe, deterministic live-test configuration."""

from __future__ import annotations

import sqlite3

import pytest
from _live_integration import integration_enabled, resolve_glm_live_config

_GLM_ENV_KEYS = (
    "PI_RUN_INTEGRATION",
    "PI_AGENT_TEST_GLM_API_KEY",
    "PI_AGENT_TEST_GLM_MODEL",
    "PI_AGENT_TEST_GLM_BASE_URL",
    "PI_AGENT_TEST_WORKSPACE_DB",
    "GLM_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "GLM_MODEL",
    "ANTHROPIC_MODEL",
    "MODEL",
    "GLM_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "BASE_URL",
)


def _clear_glm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _GLM_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_integration_gate_requires_exact_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_glm_environment(monkeypatch)
    assert integration_enabled() is False
    monkeypatch.setenv("PI_RUN_INTEGRATION", "true")
    assert integration_enabled() is False
    monkeypatch.setenv("PI_RUN_INTEGRATION", "1")
    assert integration_enabled() is True


@pytest.mark.asyncio
async def test_explicit_test_config_wins_and_repr_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_glm_environment(monkeypatch)
    secret = "test-secret-must-not-appear"
    monkeypatch.setenv("PI_AGENT_TEST_GLM_API_KEY", secret)
    monkeypatch.setenv("PI_AGENT_TEST_GLM_MODEL", "glm-test-model")
    monkeypatch.setenv("PI_AGENT_TEST_GLM_BASE_URL", "https://glm.example.test")

    config = await resolve_glm_live_config(project_root=tmp_path)

    assert config is not None
    assert config.source == "test_env"
    assert config.model == "glm-test-model"
    assert config.base_url == "https://glm.example.test"
    assert secret not in repr(config)


@pytest.mark.asyncio
async def test_dotenv_is_never_auto_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_glm_environment(monkeypatch)
    (tmp_path / ".env").write_text("GLM_API_KEY=stale-dotenv-key\n", encoding="utf-8")

    config = await resolve_glm_live_config(project_root=tmp_path)

    assert config is None


@pytest.mark.asyncio
async def test_web_default_profile_resolves_env_backed_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_glm_environment(monkeypatch)
    database = tmp_path / "workspace.sqlite"
    secret_env_name = "TEST_WEB_PROFILE_GLM_KEY"
    monkeypatch.setenv(secret_env_name, "web-profile-secret")
    monkeypatch.setenv("PI_AGENT_TEST_WORKSPACE_DB", str(database))

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE web_credentials (
                id TEXT PRIMARY KEY,
                storage_mode TEXT NOT NULL,
                secret_ref TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE web_provider_profiles (
                id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                default_model TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                is_default INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO web_credentials VALUES (?, ?, ?)",
            ("credential", "env", secret_env_name),
        )
        connection.execute(
            "INSERT INTO web_provider_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("profile", "glm", "credential", "glm-web-model", 1, 1, 42),
        )

    config = await resolve_glm_live_config(project_root=tmp_path)

    assert config is not None
    assert config.source == "web_profile"
    assert config.model == "glm-web-model"
    assert "web-profile-secret" not in repr(config)


@pytest.mark.asyncio
async def test_unreadable_web_profile_never_falls_back_to_stale_process_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_glm_environment(monkeypatch)
    database = tmp_path / "workspace.sqlite"
    monkeypatch.setenv("PI_AGENT_TEST_WORKSPACE_DB", str(database))
    monkeypatch.setenv("GLM_API_KEY", "stale-process-key")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE web_credentials (
                id TEXT PRIMARY KEY,
                storage_mode TEXT NOT NULL,
                secret_ref TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE web_provider_profiles (
                id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                credential_id TEXT NOT NULL,
                default_model TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                is_default INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO web_credentials VALUES (?, ?, ?)",
            ("credential", "env", "MISSING_WEB_PROFILE_KEY"),
        )
        connection.execute(
            "INSERT INTO web_provider_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("profile", "glm", "credential", "glm-web-model", 1, 1, 42),
        )

    config = await resolve_glm_live_config(project_root=tmp_path)

    assert config is None

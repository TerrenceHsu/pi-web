"""Shared configuration helpers for opt-in live integration tests.

This module intentionally never loads ``.env``.  Live GLM tests should use the
same persisted profile as the local Web app, or an explicit test-only override,
so a stale developer ``.env`` cannot silently select a different credential.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pi_agent_core_py.providers import get_provider_definition
from pi_agent_core_py.secrets.keyring_store import OSKeyringSecretStore

_DEFAULT_GLM_MODEL = "glm-4.5-flash"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GLMLiveConfig:
    """Resolved live-test settings; the secret is excluded from ``repr``."""

    api_key: str = field(repr=False)
    model: str
    base_url: str
    source: Literal["test_env", "web_profile", "process_env"]


@dataclass(frozen=True)
class _WebProfileCandidate:
    default_model: str
    updated_at: int
    storage_mode: str
    secret_ref: str = field(repr=False)


def integration_enabled() -> bool:
    """Require a second opt-in beyond selecting the pytest marker."""

    return os.environ.get("PI_RUN_INTEGRATION") == "1"


def _glm_default_base_url() -> str:
    definition = get_provider_definition("glm")
    if definition is None:  # pragma: no cover - registry invariant
        raise RuntimeError("built-in GLM provider definition is missing")
    return definition.default_base_url


def _workspace_databases(project_root: Path) -> list[Path]:
    override = os.environ.get("PI_AGENT_TEST_WORKSPACE_DB")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return [path]

    users_root = project_root / ".pi-agent-data" / "users"
    return sorted(users_root.glob("*/workspace.sqlite"))


def _read_default_glm_profile(database: Path) -> _WebProfileCandidate | None:
    if not database.is_file():
        return None

    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            row = connection.execute(
                """
                SELECT
                    profile.default_model,
                    profile.updated_at,
                    credential.storage_mode,
                    credential.secret_ref
                FROM web_provider_profiles AS profile
                JOIN web_credentials AS credential
                  ON credential.id = profile.credential_id
                WHERE profile.provider_id = 'glm'
                  AND profile.enabled = 1
                  AND profile.is_default = 1
                ORDER BY profile.updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError):
        return None

    if row is None:
        return None
    model, updated_at, storage_mode, secret_ref = row
    if not all(isinstance(value, str) and value for value in (model, storage_mode, secret_ref)):
        return None
    return _WebProfileCandidate(
        default_model=model,
        updated_at=int(updated_at),
        storage_mode=storage_mode,
        secret_ref=secret_ref,
    )


async def _resolve_profile_secret(candidate: _WebProfileCandidate) -> str | None:
    if candidate.storage_mode == "env":
        return os.environ.get(candidate.secret_ref)
    if candidate.storage_mode != "keyring":
        # session_only credentials exist only inside the Web backend process.
        return None

    try:
        return await OSKeyringSecretStore().get(candidate.secret_ref)
    except Exception:
        # Keep test diagnostics secret-safe.  The fixture reports only that no
        # usable live configuration was found.
        return None


def _first_nonempty_environment(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


async def resolve_glm_live_config(
    *,
    project_root: Path | None = None,
) -> GLMLiveConfig | None:
    """Resolve GLM settings without loading a dotenv file.

    Priority:
    1. Explicit ``PI_AGENT_TEST_GLM_*`` overrides.
    2. The newest enabled/default Web GLM profile and its Keyring/env secret.
    3. GLM/Anthropic variables already present in the process environment.
    """

    default_base_url = _glm_default_base_url()
    explicit_key = os.environ.get("PI_AGENT_TEST_GLM_API_KEY")
    if explicit_key:
        return GLMLiveConfig(
            api_key=explicit_key,
            model=os.environ.get("PI_AGENT_TEST_GLM_MODEL", _DEFAULT_GLM_MODEL),
            base_url=os.environ.get("PI_AGENT_TEST_GLM_BASE_URL", default_base_url),
            source="test_env",
        )

    root = (project_root or _PROJECT_ROOT).resolve()
    candidates = [
        candidate
        for database in _workspace_databases(root)
        if (candidate := _read_default_glm_profile(database)) is not None
    ]
    for candidate in sorted(candidates, key=lambda item: item.updated_at, reverse=True):
        secret = await _resolve_profile_secret(candidate)
        if secret:
            return GLMLiveConfig(
                api_key=secret,
                model=candidate.default_model,
                base_url=default_base_url,
                source="web_profile",
            )

    if candidates:
        # A configured Web default is authoritative.  Falling through to a
        # possibly stale process variable when its Keyring cannot be read is
        # exactly how a misleading 401 can reappear.
        return None

    process_key = _first_nonempty_environment(
        ("GLM_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    )
    if process_key:
        return GLMLiveConfig(
            api_key=process_key,
            model=_first_nonempty_environment(("GLM_MODEL", "ANTHROPIC_MODEL", "MODEL"))
            or _DEFAULT_GLM_MODEL,
            base_url=_first_nonempty_environment(("GLM_BASE_URL", "ANTHROPIC_BASE_URL", "BASE_URL"))
            or default_base_url,
            source="process_env",
        )

    return None

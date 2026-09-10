"""Provider bindings follow Web Session deletion, without deleting shared config."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import aiosqlite
import pytest

from pi_agent_core_py.web.credentials.service import CredentialService
from pi_agent_core_py.web.providers.config_runtime import provider_config_runtime_context
from pi_agent_core_py.web.providers.config_store import (
    WEB_PROVIDER_CONFIG_SCHEMA_VERSION,
    ProviderConfigSchemaValidationError,
    ProviderProfileInUseError,
    SessionModelBinding,
    SessionModelBindingSessionNotFoundError,
    SQLiteProviderConfigStore,
)

pytestmark = pytest.mark.asyncio


async def _profile(store: SQLiteProviderConfigStore, profile_id: str = "profile-one") -> None:
    await store.create_profile(
        profile_id=profile_id,
        name=profile_id,
        provider_id="anthropic",
        credential_id="shared-credential",
        default_model="fixture-model",
        is_default=profile_id == "profile-one",
    )


async def _bind(store: SQLiteProviderConfigStore, session_id: str) -> SessionModelBinding:
    return await store.upsert_binding(
        session_id=session_id,
        profile_id="profile-one",
        model_id="fixture-model",
        source="explicit",
    )


async def _sessions(store: SQLiteProviderConfigStore, *session_ids: str) -> None:
    db = store._require_db()
    await db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
    await db.executemany("INSERT INTO sessions(id) VALUES(?)", [(sid,) for sid in session_ids])


async def test_standalone_store_without_sessions_keeps_existing_bindings(tmp_path: Path) -> None:
    store = await SQLiteProviderConfigStore.open(tmp_path / "standalone.db")
    try:
        await _profile(store)
        original = await _bind(store, "standalone-session")
        assert await store.install_session_lifecycle_guards() == 0
        assert await store.get_binding(original.session_id) == original
        async with store._require_db().execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ) as cursor:
            assert await cursor.fetchall() == []
    finally:
        await store.close()


async def test_startup_repairs_only_orphans_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "repair.db"
    store = await SQLiteProviderConfigStore.open(path)
    try:
        await _profile(store)
        await _profile(store, "profile-two")
        await _bind(store, "deleted-session")
        surviving_binding = await _bind(store, "surviving-session")
        profiles = await store.list_profiles()
        await _sessions(store, "surviving-session")
        assert await store.install_session_lifecycle_guards() == 1
        assert await store.get_binding("deleted-session") is None
        assert await store.get_binding("surviving-session") == surviving_binding
        assert await store.list_profiles() == profiles
        assert await store.get_schema_version() == WEB_PROVIDER_CONFIG_SCHEMA_VERSION
        assert await store.install_session_lifecycle_guards() == 0
    finally:
        await store.close()

    reopened = await SQLiteProviderConfigStore.open(path)
    try:
        assert await reopened.install_session_lifecycle_guards() == 0
        assert await reopened.get_binding("surviving-session") == surviving_binding
        assert await reopened.list_profiles() == profiles
    finally:
        await reopened.close()


async def test_cascade_is_atomic_with_session_delete_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "atomic.db"
    store = await SQLiteProviderConfigStore.open(path)
    connection = await aiosqlite.connect(path, isolation_level=None)
    try:
        await _profile(store)
        await _sessions(store, "deleted-session", "surviving-session")
        await store.install_session_lifecycle_guards()
        deleted = await _bind(store, "deleted-session")
        surviving = await _bind(store, "surviving-session")
        # The trigger must work from another connection, including one without
        # foreign_keys enabled, and must not expose an unbound surviving Session.
        await connection.execute("BEGIN IMMEDIATE")
        await connection.execute("DELETE FROM sessions WHERE id='deleted-session'")
        async with connection.execute(
            "SELECT 1 FROM web_session_model_bindings WHERE session_id='deleted-session'"
        ) as cursor:
            assert await cursor.fetchone() is None
        assert await store.get_binding("deleted-session") == deleted
        await connection.execute("ROLLBACK")
        assert await store.get_binding("deleted-session") == deleted

        await connection.execute("DELETE FROM sessions WHERE id='deleted-session'")
        assert await store.get_binding("deleted-session") is None
        assert await store.get_binding("surviving-session") == surviving
        with pytest.raises(ProviderProfileInUseError):
            await store.delete_profile("profile-one")
    finally:
        await connection.close()
        await store.close()


async def test_missing_session_insert_and_update_are_rejected(tmp_path: Path) -> None:
    store = await SQLiteProviderConfigStore.open(tmp_path / "guards.db")
    try:
        await _profile(store)
        await _sessions(store, "surviving-session")
        await store.install_session_lifecycle_guards()
        binding = await _bind(store, "surviving-session")
        with pytest.raises(SessionModelBindingSessionNotFoundError) as caught:
            await _bind(store, "missing-session")
        assert str(caught.value) == "session referenced by binding does not exist"
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
        with pytest.raises(aiosqlite.IntegrityError, match="provider_binding_session_not_found"):
            await store._require_db().execute(
                "UPDATE web_session_model_bindings SET session_id='missing-session' "
                "WHERE session_id='surviving-session'"
            )
        assert await store.get_binding("missing-session") is None
        assert await store.get_binding("surviving-session") == binding
    finally:
        await store.close()


@pytest.mark.parametrize("existing_binding", [False, True])
async def test_concurrent_binding_write_and_session_delete_cannot_leave_orphan(
    tmp_path: Path, existing_binding: bool,
) -> None:
    path = tmp_path / "race.db"
    store = await SQLiteProviderConfigStore.open(path)
    connection = await aiosqlite.connect(path, isolation_level=None)
    try:
        await _profile(store)
        await _sessions(store, "race-session")
        await store.install_session_lifecycle_guards()
        if existing_binding:
            await _bind(store, "race-session")
        result, delete_result = await asyncio.gather(
            _bind(store, "race-session"),
            connection.execute("DELETE FROM sessions WHERE id='race-session'"),
            return_exceptions=True,
        )
        assert isinstance(result, (SessionModelBinding, SessionModelBindingSessionNotFoundError))
        assert isinstance(delete_result, aiosqlite.Cursor)
        await delete_result.close()
        async with connection.execute("SELECT 1 FROM sessions WHERE id='race-session'") as cursor:
            assert await cursor.fetchone() is None
        assert await store.get_binding("race-session") is None
        assert (await store.get_profile("profile-one")).credential_id == "shared-credential"
    finally:
        await connection.close()
        await store.close()


async def test_invalid_guard_rolls_back_installation_and_orphan_repair(tmp_path: Path) -> None:
    store = await SQLiteProviderConfigStore.open(tmp_path / "rollback.db")
    try:
        await _profile(store)
        original = await _bind(store, "old-orphan")
        await _sessions(store)
        await store._require_db().execute(
            "CREATE TRIGGER web_provider_binding_session_insert "
            "BEFORE INSERT ON web_session_model_bindings BEGIN SELECT 1; END"
        )
        with pytest.raises(ProviderConfigSchemaValidationError):
            await store.install_session_lifecycle_guards()
        assert await store.get_binding("old-orphan") == original
        async with store._require_db().execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ) as cursor:
            assert [row["name"] for row in await cursor.fetchall()] == [
                "web_provider_binding_session_insert",
            ]
    finally:
        await store.close()


async def test_runtime_composition_installs_guards_after_sessions_exist(tmp_path: Path) -> None:
    path = tmp_path / "composition.db"
    store = await SQLiteProviderConfigStore.open(path)
    try:
        await _profile(store)
        await _bind(store, "old-orphan")
        await _sessions(store, "surviving-session")
    finally:
        await store.close()

    async def session_exists(session_id: str) -> bool:
        return session_id == "surviving-session"

    # Composition only connects repositories; it must not access any credential.
    async with provider_config_runtime_context(
        database_path=path,
        credential_service=cast(CredentialService, object()),
        session_exists=session_exists,
    ) as runtime:
        assert await runtime.store.get_binding("old-orphan") is None
        await _bind(runtime.store, "surviving-session")
        await runtime.store._require_db().execute(
            "DELETE FROM sessions WHERE id='surviving-session'"
        )
        assert await runtime.store.list_bindings_for_profile("profile-one") == ()


async def test_web_session_delete_releases_profile_but_keeps_shared_credential(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from pi_agent_core_py import Agent, AgentHarness, FakeClient
    from pi_agent_core_py.web.app import create_app

    app = create_app(
        AgentHarness(Agent(system_prompt="fixture", client=FakeClient([]), tools=None)),
        db_path=str(tmp_path / "web.db"),
        credential_secret_backend="memory",
        credential_extra_hosts=("testserver", "localhost", "127.0.0.1"),
        enable_trusted_host=True,
    )
    headers = {"X-PI-Agent-UI": "1"}
    with TestClient(app, base_url="http://testserver") as client:
        credential = client.post("/api/credentials", headers=headers, json={
            "label": "Shared fixture", "storage_mode": "session_only",
            "secret_value": "sk-offline-fixture",
        })
        assert credential.status_code == 201
        credential_id = credential.json()["credential"]["credential_id"]
        profiles = []
        sessions = []
        for name in ("deleted", "surviving"):
            response = client.post("/api/provider-profiles", headers=headers, json={
                "name": name, "provider_id": "anthropic", "credential_id": credential_id,
                "default_model": "fixture-model",
            })
            assert response.status_code == 201
            profile = response.json()["profile"]
            profiles.append(profile)
            response = client.post("/api/sessions", headers=headers, json={"title": name})
            assert response.status_code == 200
            session_id = response.json()["id"]
            sessions.append(session_id)
            assert client.put(f"/api/sessions/{session_id}/model-binding", headers=headers, json={
                "profile_id": profile["id"], "model_id": "fixture-model",
            }).status_code == 200

        response = client.delete(f"/api/sessions/{sessions[0]}", headers=headers)
        assert response.status_code == 200 and response.json()["ok"]
        assert client.get(f"/api/sessions/{sessions[0]}", headers=headers).status_code == 404
        assert client.delete(
            f"/api/provider-profiles/{profiles[0]['id']}", headers=headers,
        ).status_code == 204
        assert client.get(
            f"/api/sessions/{sessions[1]}/model-binding", headers=headers,
        ).json()["binding"]["profile_id"] == profiles[1]["id"]
        assert client.get("/api/provider-profiles", headers=headers).json()["profiles"] == [
            profiles[1],
        ]
        remaining = client.get("/api/credentials", headers=headers).json()["credentials"]
        assert [item["credential_id"] for item in remaining] == [credential_id]

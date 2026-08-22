"""Offline persistence and connection-probe tests for sandbox administration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import aiosqlite
import pytest
from pydantic import ValidationError

from coding_sandbox import (
    FakeSandboxBackend,
    SandboxCommandResult,
    SandboxCreateSpec,
    SandboxError,
    SandboxHandle,
    SandboxNetworkPolicy,
)
from coding_sandbox.admin import (
    SandboxAdminConfig,
    SandboxAdminService,
    SandboxConfigConflictError,
    SandboxConfigCorruptError,
    SQLiteSandboxConfigStore,
)


class _Credentials:
    def __init__(self, secret: str = "e2b_private-placeholder") -> None:
        self.secret = secret
        self.get_calls: list[str] = []
        self.resolve_calls: list[str] = []
        self.error: Exception | None = None

    async def get(self, credential_id: str) -> object:
        self.get_calls.append(credential_id)
        if self.error is not None:
            raise self.error
        return object()

    async def resolve_secret_for_request(self, credential_id: str) -> str:
        self.resolve_calls.append(credential_id)
        if self.error is not None:
            raise self.error
        return self.secret


def _configured(*, enabled: bool = False) -> SandboxAdminConfig:
    return SandboxAdminConfig(
        enabled=enabled,
        runtime_id="base",
        credential_id="cred-e2b-production",
        network=SandboxNetworkPolicy(
            mode="allowlist",
            allowed_domains=("pypi.org",),
        ),
    )


def test_admin_config_requires_credential_only_when_enabled() -> None:
    assert SandboxAdminConfig().enabled is False
    assert SandboxAdminConfig().credential_id is None
    with pytest.raises(ValidationError, match="requires credential_id"):
        SandboxAdminConfig(enabled=True)

    config = _configured(enabled=True)
    service_config = config.to_service_config()
    assert service_config.enabled is True
    assert service_config.backend is not None
    assert service_config.backend.get_secret_ref("api_key") == "cred-e2b-production"
    assert "e2b_private" not in config.model_dump_json()


def test_admin_config_rejects_unimplemented_modal_provider() -> None:
    with pytest.raises(ValidationError):
        SandboxAdminConfig.model_validate(
            {
                "provider": "modal",
                "runtime_id": "python:3.12-slim",
            }
        )


@pytest.mark.asyncio
async def test_store_defaults_revision_updates_and_persists(tmp_path: Path) -> None:
    database = tmp_path / "app.db"
    store = await SQLiteSandboxConfigStore.open(database, now_ms=lambda: 1234)
    try:
        default = await store.get()
        assert default.revision == 0
        assert default.updated_at_ms is None
        assert default.config == SandboxAdminConfig()

        saved = await store.put(_configured(), expected_revision=0)
        assert saved.revision == 1
        assert saved.updated_at_ms == 1234
    finally:
        await store.close()

    reopened = await SQLiteSandboxConfigStore.open(database)
    try:
        loaded = await reopened.get()
        assert loaded.revision == 1
        assert loaded.config == _configured()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_store_accepts_retired_null_modal_fields_for_e2b_compatibility() -> None:
    connection = await aiosqlite.connect(":memory:", isolation_level=None)
    store = await SQLiteSandboxConfigStore.for_testing(connection)
    try:
        await connection.execute(
            "INSERT INTO web_coding_sandbox_config "
            "(singleton_id, config_json, revision, updated_at_ms) "
            "VALUES (1, ?, 1, 1)",
            (
                '{"enabled":false,"provider":"e2b","runtime_id":"base",'
                '"credential_id":null,"modal_token_id_credential_id":null,'
                '"modal_token_secret_credential_id":null}',
            ),
        )
        loaded = await store.get()
        assert loaded.config == SandboxAdminConfig()
        assert "modal" not in loaded.config.model_dump_json()
    finally:
        await store.close()
        await connection.close()


@pytest.mark.asyncio
async def test_store_rejects_stale_revision_without_overwrite(tmp_path: Path) -> None:
    store = await SQLiteSandboxConfigStore.open(tmp_path / "app.db")
    try:
        original = await store.put(_configured(), expected_revision=0)
        with pytest.raises(SandboxConfigConflictError):
            await store.put(
                _configured().model_copy(update={"runtime_id": "other"}),
                expected_revision=0,
            )
        assert await store.get() == original
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_store_rejects_corrupt_record_without_dumping_payload() -> None:
    connection = await aiosqlite.connect(":memory:", isolation_level=None)
    store = await SQLiteSandboxConfigStore.for_testing(connection)
    try:
        await connection.execute(
            "INSERT INTO web_coding_sandbox_config "
            "(singleton_id, config_json, revision, updated_at_ms) "
            "VALUES (1, ?, 1, 1)",
            ('{"raw":"must-not-leak"}',),
        )
        with pytest.raises(SandboxConfigCorruptError) as exc_info:
            await store.get()
        assert "must-not-leak" not in str(exc_info.value)
    finally:
        await store.close()
        await connection.close()


@pytest.mark.asyncio
async def test_update_validates_credential_reference_before_persisting(
    tmp_path: Path,
) -> None:
    store = await SQLiteSandboxConfigStore.open(tmp_path / "app.db")
    credentials = _Credentials()
    service = SandboxAdminService(store=store, credential_service=credentials)
    try:
        saved = await service.update_config(_configured(), expected_revision=0)
        assert saved.revision == 1
        assert credentials.get_calls == ["cred-e2b-production"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connection_probe_resolves_secret_runs_fixed_command_and_cleans_up(
    tmp_path: Path,
) -> None:
    store = await SQLiteSandboxConfigStore.open(tmp_path / "app.db")
    await store.put(_configured(), expected_revision=0)
    credentials = _Credentials()
    backend = FakeSandboxBackend(id_factory=lambda: "probe-sandbox")
    received: list[tuple[SandboxAdminConfig, dict[str, str]]] = []

    def factory(
        config: SandboxAdminConfig,
        secrets: Mapping[str, str],
    ) -> FakeSandboxBackend:
        received.append((config, dict(secrets)))
        return backend

    ticks = iter((10.0, 10.125))
    service = SandboxAdminService(
        store=store,
        credential_service=credentials,
        backend_factory=factory,
        now_ms=lambda: 5_000,
        monotonic=lambda: next(ticks),
    )
    try:
        result = await service.test_connection()
    finally:
        await store.close()

    assert result.ok is True
    assert result.python_available is True
    assert result.duration_ms == 125
    assert result.error_code is None
    assert credentials.resolve_calls == ["cred-e2b-production"]
    assert received[0][1] == {"api_key": "e2b_private-placeholder"}
    assert received[0][0].limits.lifetime_seconds == 60
    assert backend.created_specs[0].network == SandboxNetworkPolicy()
    assert backend.executed_commands[0].argv == ("python3", "--version")
    assert backend.destroyed_sandbox_ids == ["probe-sandbox"]
    assert "e2b_private-placeholder" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_connection_probe_reports_missing_credential_without_backend(
    tmp_path: Path,
) -> None:
    store = await SQLiteSandboxConfigStore.open(tmp_path / "app.db")
    credentials = _Credentials()
    called = False

    def factory(
        _: SandboxAdminConfig,
        __: Mapping[str, str],
    ) -> FakeSandboxBackend:
        nonlocal called
        called = True
        return FakeSandboxBackend()

    service = SandboxAdminService(
        store=store,
        credential_service=credentials,
        backend_factory=factory,
    )
    try:
        result = await service.test_connection()
    finally:
        await store.close()

    assert result.ok is False
    assert result.error_code == "not_configured"
    assert called is False


class _CreateErrorBackend(FakeSandboxBackend):
    async def create(self, spec: SandboxCreateSpec) -> SandboxHandle:
        raise SandboxError("authentication_failed", provider="e2b")


class _CleanupErrorBackend(FakeSandboxBackend):
    async def destroy(self, handle: SandboxHandle) -> None:
        raise ConnectionError("raw cleanup error")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_factory", "expected"),
    [
        (lambda: _CreateErrorBackend(), "authentication_failed"),
        (
            lambda: FakeSandboxBackend(
                command_results=(
                    SandboxCommandResult(
                        command_id="queued",
                        exit_code=1,
                        started_at_ms=1,
                        finished_at_ms=2,
                    ),
                )
            ),
            "command_failed",
        ),
        (lambda: _CleanupErrorBackend(), "cleanup_failed"),
    ],
)
async def test_connection_probe_returns_fixed_failure_and_attempts_cleanup(
    tmp_path: Path,
    backend_factory: Callable[[], FakeSandboxBackend],
    expected: str,
) -> None:
    store = await SQLiteSandboxConfigStore.open(tmp_path / f"{expected}.db")
    await store.put(_configured(), expected_revision=0)
    service = SandboxAdminService(
        store=store,
        credential_service=_Credentials(),
        backend_factory=lambda _config, _secrets: backend_factory(),
    )
    try:
        result = await service.test_connection()
    finally:
        await store.close()

    assert result.ok is False
    assert result.error_code == expected
    assert "raw cleanup" not in result.model_dump_json()

"""CredentialService.resolve_secret_for_request tests（M1-4 §十四.Secret resolution）.

覆盖 12 项：
- InMemory / Env / Keyring Secret 正常解析
- Credential 不存在 / secret_ref 在 store 中缺失 / session-only 重启
- backend 不可用
- 空 Secret 拒绝
- 不触发 Credential validation
- 不访问外部网络
- 不缓存明文 Secret
- 错误 cause 截断（from None）
"""
from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStoreError,
)
from pi_agent_core_py.web.credentials.errors import (
    CredentialRequestSecretBackendError,
    CredentialRequestSecretUnavailableError,
)
from pi_agent_core_py.web.credentials.service import (
    CreateCredentialCommand,
    CredentialService,
)
from pi_agent_core_py.web.credentials.store import (
    CredentialNotFoundError,
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.credentials.secret_store import SecretStoreRouter

pytestmark = pytest.mark.asyncio

SECRET_MARKER = "sk-M1-4-SECRET-MARKER-DO-NOT-LEAK"
ENV_VAR = "PI_TEST_M14_RESOLVE_SECRET"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def repository(tmp_path: Any) -> SQLiteCredentialStore:
    s = await SQLiteCredentialStore.open(str(tmp_path / "creds.db"))
    yield s  # type: ignore[misc]
    await s.close()


@pytest.fixture
def keyring_store() -> InMemorySecretStore:
    """Use InMemory stand-in for OS keyring (avoid real backend flakiness)."""
    return InMemorySecretStore()


@pytest.fixture
def session_only_store() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def env_store() -> EnvSecretStore:
    return EnvSecretStore()


@pytest.fixture
def router(
    keyring_store: InMemorySecretStore,
    session_only_store: InMemorySecretStore,
    env_store: EnvSecretStore,
) -> SecretStoreRouter:
    return SecretStoreRouter(
        stores={
            "keyring": keyring_store,
            "session_only": session_only_store,
            "env": env_store,
        }
    )


@pytest.fixture
def service(
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
) -> CredentialService:
    return CredentialService(repository=repository, router=router)


# ============================================================================
# 1-3: Happy path per storage_mode
# ============================================================================


async def test_1_inmemory_secret_resolves(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
) -> None:
    created = await service.create(
        CreateCredentialCommand(
            label="InMem",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    secret = await service.resolve_secret_for_request(created.record.id)
    assert secret == SECRET_MARKER


async def test_2_env_secret_reads_current_value_each_call(
    service: CredentialService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env backend must read os.environ at call time——no caching."""
    monkeypatch.setenv(ENV_VAR, "first-value")
    created = await service.create(
        CreateCredentialCommand(
            label="Env",
            storage_mode="env",
            secret_value=None,  # env forbids body
            env_var_name=ENV_VAR,
        )
    )

    s1 = await service.resolve_secret_for_request(created.record.id)
    assert s1 == "first-value"

    # Change env, ensure next call sees new value
    monkeypatch.setenv(ENV_VAR, "second-value")
    s2 = await service.resolve_secret_for_request(created.record.id)
    assert s2 == "second-value"


async def test_3_keyring_secret_resolves_via_router(
    service: CredentialService,
    keyring_store: InMemorySecretStore,
) -> None:
    created = await service.create(
        CreateCredentialCommand(
            label="Keyring",
            storage_mode="keyring",
            secret_value=SECRET_MARKER,
        )
    )
    secret = await service.resolve_secret_for_request(created.record.id)
    assert secret == SECRET_MARKER


# ============================================================================
# 4-6: Failure modes
# ============================================================================


async def test_4_credential_not_found_raises_domain_error(
    service: CredentialService,
) -> None:
    """CredentialNotFoundError propagates——runtime can distinguish it from
    'secret unreadable' states."""
    with pytest.raises(CredentialNotFoundError):
        await service.resolve_secret_for_request("cred-does-not-exist")


async def test_5_secret_reference_missing_in_store(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
    repository: SQLiteCredentialStore,
) -> None:
    """Credential exists but the store backend lost the secret_ref
    (e.g. session_only store was cleared)."""
    created = await service.create(
        CreateCredentialCommand(
            label="Lost",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    # Wipe the secret_ref from the store without telling repository
    await session_only_store.delete(created.record.secret_ref)

    with pytest.raises(CredentialRequestSecretUnavailableError) as exc_info:
        await service.resolve_secret_for_request(created.record.id)
    assert str(exc_info.value) == "provider credential is unavailable"


async def test_6_session_only_unavailable_after_restart(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
    tmp_path: Any,
) -> None:
    """Simulate process restart: new repository opens existing DB, new
    session_only store is empty. Previously-created secret is gone."""
    created = await service.create(
        CreateCredentialCommand(
            label="PreRestart",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    credential_id = created.record.id

    # Simulate restart: close + reopen repository; new empty session_only store
    await repository.close()
    reopened = await SQLiteCredentialStore.open(str(tmp_path / "creds.db"))
    try:
        restarted_service = CredentialService(
            repository=reopened,
            router=SecretStoreRouter(
                stores={
                    "keyring": None,  # type: ignore[dict-item]
                    "session_only": InMemorySecretStore(),
                    "env": EnvSecretStore(),
                }
            ),
        )
        with pytest.raises(CredentialRequestSecretUnavailableError):
            await restarted_service.resolve_secret_for_request(credential_id)
    finally:
        await reopened.close()


# ============================================================================
# 7: backend unavailable
# ============================================================================


async def test_7_backend_unavailable_raises_backend_error(
    repository: SQLiteCredentialStore,
) -> None:
    """keyring store registered as None (unavailable on this platform) →
    CredentialRequestSecretBackendError with fixed message."""

    class _UnavailableStore(InMemorySecretStore):
        async def is_available(self) -> bool:
            return False

    service = CredentialService(
        repository=repository,
        router=SecretStoreRouter(
            stores={
                "session_only": _UnavailableStore(),
                "env": EnvSecretStore(),
                "keyring": None,  # type: ignore[dict-item]
            }
        ),
    )
    try:
        # Create a record pointing at the unavailable backend.
        # Service.create refuses when backend is unavailable——so we bypass via
        # the writable session_only store then patch the repository row's
        # storage_mode. Simpler: create via a working service then swap.
        writable_router = SecretStoreRouter(
            stores={
                "session_only": InMemorySecretStore(),
                "env": EnvSecretStore(),
                "keyring": None,  # type: ignore[dict-item]
            }
        )
        writable_service = CredentialService(
            repository=repository, router=writable_router
        )
        created = await writable_service.create(
            CreateCredentialCommand(
                label="BackendDown",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        # Now ask the service whose session_only store reports unavailable
        with pytest.raises(CredentialRequestSecretBackendError) as exc_info:
            await service.resolve_secret_for_request(created.record.id)
        assert str(exc_info.value) == "provider credential backend is unavailable"
    finally:
        await repository.close()


# ============================================================================
# 8: empty Secret rejected
# ============================================================================


async def test_8_empty_secret_rejected(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
    repository: SQLiteCredentialStore,
) -> None:
    """A record whose stored value is whitespace-only → unavailable.

    SecretStore.set rejects empty string but allows whitespace-only——resolve
    must reject both as 'provider credential is unavailable'.
    """
    created = await service.create(
        CreateCredentialCommand(
            label="Empty",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    # Overwrite with whitespace-only value (store allows it)
    await session_only_store.set(created.record.secret_ref, "   \t  ")

    with pytest.raises(CredentialRequestSecretUnavailableError):
        await service.resolve_secret_for_request(created.record.id)


# ============================================================================
# 9: 不触发 Credential validation
# ============================================================================


async def test_9_does_not_trigger_credential_validation(
    service: CredentialService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_secret_for_request must NOT call validation_strategy_registry."""
    call_log: list[str] = []

    original_validate = service.validate

    async def _spy_validate(*args: Any, **kwargs: Any) -> Any:
        call_log.append("validate")
        return await original_validate(*args, **kwargs)

    monkeypatch.setattr(service, "validate", _spy_validate)

    created = await service.create(
        CreateCredentialCommand(
            label="NoVal",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    await service.resolve_secret_for_request(created.record.id)
    assert call_log == [], "resolve_secret_for_request must not trigger validate()"


# ============================================================================
# 10: 不访问外部网络
# ============================================================================


async def test_10_does_not_access_external_network(
    service: CredentialService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve_secret_for_request must not open HTTP connections. Block the
    common SDK HTTP entry points; local backends must still succeed.

    Note: we don't block ``socket.socket`` itself because Windows asyncio
    ProactorEventLoop uses IOCP, not sockets——blocking sockets hangs the loop.
    Blocking httpx is sufficient: any real HTTP call goes through it.
    """
    import httpx

    created = await service.create(
        CreateCredentialCommand(
            label="NoNet",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )

    def _no_http(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "resolve_secret_for_request must not create HTTP clients"
        )

    monkeypatch.setattr(httpx, "AsyncClient", _no_http)
    monkeypatch.setattr(httpx, "Client", _no_http)

    secret = await service.resolve_secret_for_request(created.record.id)
    assert secret == SECRET_MARKER


# ============================================================================
# 11: 不缓存明文 Secret
# ============================================================================


async def test_11_does_not_cache_plaintext_secret(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
    repository: SQLiteCredentialStore,
) -> None:
    """After resolving, change the store value——next resolve must observe new."""
    created = await service.create(
        CreateCredentialCommand(
            label="NoCache",
            storage_mode="session_only",
            secret_value="first",
        )
    )
    s1 = await service.resolve_secret_for_request(created.record.id)
    assert s1 == "first"

    await session_only_store.set(created.record.secret_ref, "second")
    s2 = await service.resolve_secret_for_request(created.record.id)
    assert s2 == "second"


# ============================================================================
# 12: 错误 cause 截断
# ============================================================================


async def test_12_errors_truncate_cause_chain(
    service: CredentialService,
    session_only_store: InMemorySecretStore,
) -> None:
    """All wrap exceptions must use `from None`——__cause__ is None."""
    created = await service.create(
        CreateCredentialCommand(
            label="Cause",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
    )
    await session_only_store.delete(created.record.secret_ref)

    with pytest.raises(CredentialRequestSecretUnavailableError) as exc_info:
        await service.resolve_secret_for_request(created.record.id)

    # __cause__ must be None——no SecretStoreError / backend exc leaked
    assert exc_info.value.__cause__ is None
    # str/repr must not contain secret marker
    assert SECRET_MARKER not in str(exc_info.value)
    assert SECRET_MARKER not in repr(exc_info.value)


# ============================================================================
# Bonus: store raising SecretStoreError gets wrapped safely
# ============================================================================


async def test_store_raising_secret_store_error_wrapped_safely(
    repository: SQLiteCredentialStore,
) -> None:
    """SecretStore.get raising SecretStoreError must be wrapped to fixed message."""

    class _RaisingStore(InMemorySecretStore):
        async def get(self, secret_ref: str) -> str | None:
            raise SecretStoreError("simulated backend failure")

    service = CredentialService(
        repository=repository,
        router=SecretStoreRouter(
            stores={
                "session_only": _RaisingStore(),
                "env": EnvSecretStore(),
                "keyring": None,  # type: ignore[dict-item]
            }
        ),
    )
    try:
        created = await service.create(
            CreateCredentialCommand(
                label="Raising",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        with pytest.raises(CredentialRequestSecretUnavailableError) as exc_info:
            await service.resolve_secret_for_request(created.record.id)
        assert str(exc_info.value) == "provider credential is unavailable"
        # Original backend exception text must not leak
        assert "simulated backend failure" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
    finally:
        await repository.close()

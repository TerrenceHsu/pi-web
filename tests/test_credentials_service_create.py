"""CredentialService.create tests（P1-E1-3A）.

覆盖：
- keyring / session-only / env create 成功
- env create 不读取环境变量值
- env create 不调用 set
- Repository create 失败后清理 Secret
- 补偿删除失败 → CompensationError(cleanup_succeeded=False)
- 非法输入组合拒绝
- 生成的 ID/ref 不包含 secret
- SecretStore set 失败时不写数据库
- masked_value / fingerprint 计算正确
- provider_hint 检测正确
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStoreUnavailableError,
    fingerprint_secret,
    mask_secret,
)
from pi_agent_core_py.web.credentials_errors import (
    CredentialBackendUnavailableError,
    CredentialCompensationError,
    CredentialInputError,
    CredentialSecretWriteError,
)
from pi_agent_core_py.web.credentials_service import (
    CreateCredentialCommand,
    CredentialService,
)
from pi_agent_core_py.web.credentials_store import (
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.secret_store_router import SecretStoreRouter

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
async def repository(tmp_path):
    s = await SQLiteCredentialStore.open(str(tmp_path / "creds.db"))
    yield s
    await s.close()


@pytest.fixture
def keyring_store() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def session_only_store() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def memory_store() -> InMemorySecretStore:
    """Alias for tests that don't care which stored backend."""
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


def _det_id_factory(counter: int = 0) -> Any:
    """Return a callable that yields predictable ids for tests."""
    state = {"n": counter}

    def _factory() -> str:
        state["n"] += 1
        return f"cred-det-{state['n']:04d}"

    return _factory


def _det_ref_factory(counter: int = 0) -> Any:
    state = {"n": counter}

    def _factory() -> str:
        state["n"] += 1
        return f"secret-det-{state['n']:04d}"

    return _factory


# ============================================================================
# 1. Happy path——keyring / session-only / env
# ============================================================================


class TestCreateHappyPath:
    async def test_create_session_only_succeeds(
        self,
        service: CredentialService,
        session_only_store: InMemorySecretStore,
        repository: SQLiteCredentialStore,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="Work Key",
            storage_mode="session_only",
            secret_value="sk-test-1234567890",
        )
        result = await service.create(cmd)

        assert result.record.label == "Work Key"
        assert result.record.storage_mode == "session_only"
        assert result.record.validation_status == "never_validated"
        assert result.warnings == ()

        # Secret 写到了 session_only store
        fetched = await session_only_store.get(result.record.secret_ref)
        assert fetched == "sk-test-1234567890"

        # DB row 写到了 repository
        got = await repository.get(result.record.id)
        assert got.id == result.record.id

    async def test_create_keyring_succeeds(
        self,
        service: CredentialService,
        keyring_store: InMemorySecretStore,
    ) -> None:
        """Router 把 'keyring' 路由到 keyring_store（test fixture）."""
        cmd = CreateCredentialCommand(
            label="Keyring Key",
            storage_mode="keyring",
            secret_value="sk-test-1234567890",
        )
        result = await service.create(cmd)
        assert result.record.storage_mode == "keyring"

        fetched = await keyring_store.get(result.record.secret_ref)
        assert fetched == "sk-test-1234567890"

    async def test_create_env_succeeds(
        self,
        service: CredentialService,
        env_store: EnvSecretStore,
        repository: SQLiteCredentialStore,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="Env Key",
            storage_mode="env",
            env_var_name="GLM_API_KEY",
        )
        result = await service.create(cmd)
        assert result.record.storage_mode == "env"
        assert result.record.secret_ref == "GLM_API_KEY"
        assert result.record.masked_value == "ENV[GLM_API_KEY]"
        assert result.record.fingerprint_sha256 is None
        assert result.record.provider_hint is None
        assert result.record.provider_hint_confidence == "unknown"


# ============================================================================
# 2. Env——不读取环境变量值，不调用 set
# ============================================================================


class TestEnvCreateNoSideEffects:
    async def test_env_create_does_not_read_env_value(
        self,
        service: CredentialService,
        monkeypatch,
    ) -> None:
        """masked_value 必须是 ENV[NAME]，不是从 os.environ 读出来的值的脱敏."""
        monkeypatch.setenv("GLM_API_KEY", SECRET_MARKER)
        cmd = CreateCredentialCommand(
            label="Env",
            storage_mode="env",
            env_var_name="GLM_API_KEY",
        )
        result = await service.create(cmd)
        # masked_value 不应含 marker
        assert SECRET_MARKER not in result.record.masked_value
        assert result.record.masked_value == "ENV[GLM_API_KEY]"
        # fingerprint 必须是 None（未对 env value 计算）
        assert result.record.fingerprint_sha256 is None

    async def test_env_create_does_not_call_set(
        self,
        repository: SQLiteCredentialStore,
        memory_store: InMemorySecretStore,
    ) -> None:
        """EnvSecretStore.set 会抛 SecretStoreReadOnlyError——若 service 调了
        create 应当失败. 这里验证 create 成功（说明没调 set）."""
        env = EnvSecretStore()
        # Spy：包装 set 抛 sentinel
        set_called = asyncio.Event()

        original_set = env.set

        async def _spy_set(ref: str, value: str) -> None:
            set_called.set()
            await original_set(ref, value)

        env.set = _spy_set  # type: ignore[method-assign]

        router = SecretStoreRouter(
            stores={
                "keyring": memory_store,
                "session_only": InMemorySecretStore(),
                "env": env,
            }
        )
        service = CredentialService(repository=repository, router=router)

        cmd = CreateCredentialCommand(
            label="Env",
            storage_mode="env",
            env_var_name="GLM_API_KEY",
        )
        await service.create(cmd)
        # set 不应被调用
        assert not set_called.is_set()


# ============================================================================
# 3. Repository failure → compensation
# ============================================================================


class TestCompensationOnRepoFailure:
    async def test_repo_create_failure_cleans_up_secret(
        self,
        repository: SQLiteCredentialStore,
        session_only_store: InMemorySecretStore,
    ) -> None:
        """Pre-create a row with same id——service.create should fail with
        duplicate id, and the just-written secret should be cleaned up."""
        router = SecretStoreRouter(
            stores={
                "keyring": InMemorySecretStore(),
                "session_only": session_only_store,
                "env": EnvSecretStore(),
            }
        )
        # 第一次 create 成功，占住 cred-dup
        seed_service = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-dup",
            secret_ref_factory=lambda: "secret-seed",
        )
        await seed_service.create(
            CreateCredentialCommand(
                label="First",
                storage_mode="session_only",
                secret_value="sk-test-1234567890",
            )
        )

        # 第二次 create 用同样的 id，但新的 secret_ref——应当失败于 DB duplicate
        fail_service = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-dup",  # dup
            secret_ref_factory=lambda: "secret-fresh",
        )
        with pytest.raises(CredentialCompensationError):
            await fail_service.create(
                CreateCredentialCommand(
                    label="Second",
                    storage_mode="session_only",
                    secret_value="sk-test-0987654321",
                )
            )

        # 补偿：第二次写的 secret-fresh 应被清理
        assert await session_only_store.get("secret-fresh") is None
        # 第一次的 secret-seed 仍在
        assert await session_only_store.get("secret-seed") is not None

    async def test_repo_failure_and_cleanup_failure_raises_compensation_error(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        """DB create failure + cleanup failure → CompensationError cleanup_succeeded=False."""

        class _FailingDeleteStore(InMemorySecretStore):
            async def delete(self, secret_ref: str) -> None:
                raise SecretStoreUnavailableError(
                    "delete always fails",
                    secret_ref=secret_ref,
                )

        store = _FailingDeleteStore()
        router = SecretStoreRouter(
            stores={
                "keyring": store,
                "session_only": InMemorySecretStore(),
                "env": EnvSecretStore(),
            }
        )
        # 第一次 create 让 'cred-x' 占位
        service_seed = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",
            secret_ref_factory=lambda: "secret-seed",
        )
        # 先用普通 memory backend 占位（_FailingDeleteStore 也会失败于 delete
        # 但 create 流程的 set 应该 OK）
        await service_seed.create(
            CreateCredentialCommand(
                label="Seed",
                storage_mode="keyring",
                secret_value="sk-test-1234567890",
            )
        )

        # 第二次 create：用同样的 credential_id 触发 DB duplicate，
        # 但 delete 会失败
        service_fail = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",  # dup
            secret_ref_factory=lambda: "secret-new",
        )
        with pytest.raises(CredentialCompensationError) as exc_info:
            await service_fail.create(
                CreateCredentialCommand(
                    label="Fail",
                    storage_mode="keyring",
                    secret_value="sk-test-0987654321",
                )
            )
        assert exc_info.value.cleanup_succeeded is False


# ============================================================================
# 4. SecretStore.set 失败——不写 DB
# ============================================================================


class TestSecretWriteFailure:
    async def test_secret_write_failure_does_not_touch_db(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        class _FailingSetStore(InMemorySecretStore):
            async def set(self, secret_ref: str, value: str) -> None:
                raise SecretStoreUnavailableError(
                    "set always fails",
                    secret_ref=secret_ref,
                )

        store = _FailingSetStore()
        router = SecretStoreRouter(
            stores={
                "keyring": store,
                "session_only": InMemorySecretStore(),
                "env": EnvSecretStore(),
            }
        )
        service = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-fail",
            secret_ref_factory=lambda: "secret-fail",
        )

        with pytest.raises(CredentialSecretWriteError):
            await service.create(
                CreateCredentialCommand(
                    label="Fail",
                    storage_mode="keyring",
                    secret_value="sk-test-1234567890",
                )
            )

        # DB 应当没有 cred-fail
        from pi_agent_core_py.web.credentials_store import CredentialNotFoundError

        with pytest.raises(CredentialNotFoundError):
            await repository.get("cred-fail")


# ============================================================================
# 5. Input validation
# ============================================================================


class TestInputValidation:
    async def test_empty_label_rejected(self, service: CredentialService) -> None:
        with pytest.raises(CredentialInputError, match="label"):
            await service.create(
                CreateCredentialCommand(
                    label="   ",
                    storage_mode="session_only",
                    secret_value="sk-test-1234567890",
                )
            )

    async def test_keyring_without_secret_value_rejected(
        self,
        service: CredentialService,
    ) -> None:
        with pytest.raises(CredentialInputError, match="secret_value"):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="keyring",
                    secret_value=None,
                )
            )

    async def test_env_with_secret_value_rejected(
        self,
        service: CredentialService,
    ) -> None:
        with pytest.raises(CredentialInputError, match="forbids"):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="env",
                    secret_value="sk-test-1234567890",
                    env_var_name="GLM_API_KEY",
                )
            )

    async def test_env_without_env_var_name_rejected(
        self,
        service: CredentialService,
    ) -> None:
        with pytest.raises(CredentialInputError, match="env_var_name"):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="env",
                )
            )

    async def test_env_invalid_var_name_rejected(
        self,
        service: CredentialService,
    ) -> None:
        with pytest.raises(CredentialInputError, match="env_var_name"):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="env",
                    env_var_name="1INVALID",
                )
            )

    async def test_keyring_with_env_var_name_rejected(
        self,
        service: CredentialService,
    ) -> None:
        with pytest.raises(CredentialInputError, match="forbids"):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="keyring",
                    secret_value="sk-test-1234567890",
                    env_var_name="GLM_API_KEY",
                )
            )


# ============================================================================
# 6. ID / ref generation safety
# ============================================================================


class TestIdGenerationSafety:
    async def test_generated_id_does_not_contain_secret(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
        result = await service.create(cmd)
        assert SECRET_MARKER not in result.record.id
        assert SECRET_MARKER not in result.record.secret_ref
        # ID 用 'cred-' 前缀，secret_ref 用 'secret-' 前缀
        assert result.record.id.startswith("cred-")
        assert result.record.secret_ref.startswith("secret-")
        # ID 和 ref 不能相同
        assert result.record.id != result.record.secret_ref

    async def test_default_factory_produces_unpredictable_ids(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """默认工厂应当产生不重复的 ID / ref."""
        service = CredentialService(repository=repository, router=router)
        ids: set[str] = set()
        refs: set[str] = set()
        for _ in range(20):
            r = await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="session_only",
                    secret_value="sk-test-1234567890",
                )
            )
            ids.add(r.record.id)
            refs.add(r.record.secret_ref)
        assert len(ids) == 20
        assert len(refs) == 20


# ============================================================================
# 7. Masked / fingerprint / provider_hint correctness
# ============================================================================


class TestDerivedFields:
    async def test_mask_and_fingerprint_match_utils(
        self,
        service: CredentialService,
    ) -> None:
        secret = "sk-test-1234567890"
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=secret,
        )
        result = await service.create(cmd)
        assert result.record.masked_value == mask_secret(secret)
        assert result.record.fingerprint_sha256 == fingerprint_secret(secret)

    async def test_provider_hint_anthropic_prefix(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="Anthropic",
            storage_mode="session_only",
            secret_value="sk-ant-test-1234567890123",
        )
        result = await service.create(cmd)
        assert result.record.provider_hint == "anthropic"
        assert result.record.provider_hint_confidence == "high"

    async def test_provider_hint_unknown_for_generic_sk(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="Generic",
            storage_mode="session_only",
            secret_value="sk-test-1234567890",
        )
        result = await service.create(cmd)
        # generic sk- prefix → unknown
        assert result.record.provider_hint is None
        assert result.record.provider_hint_confidence == "unknown"


# ============================================================================
# 8. Backend unavailable
# ============================================================================


class TestBackendUnavailable:
    async def test_create_keyring_with_no_backend_raises(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        """keyring=None 时 create('keyring') 应当 raise backend unavailable."""
        router = SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": InMemorySecretStore(),
                "env": EnvSecretStore(),
            }
        )
        service = CredentialService(repository=repository, router=router)
        with pytest.raises(CredentialBackendUnavailableError):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="keyring",
                    secret_value="sk-test-1234567890",
                )
            )

    async def test_create_session_only_when_store_unavailable_raises(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        """session_only store is_available=False 时也应当 raise."""

        class _UnavailableMemoryStore(InMemorySecretStore):
            async def is_available(self) -> bool:
                return False

        router = SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": _UnavailableMemoryStore(),
                "env": EnvSecretStore(),
            }
        )
        service = CredentialService(repository=repository, router=router)
        with pytest.raises(CredentialBackendUnavailableError):
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="session_only",
                    secret_value="sk-test-1234567890",
                )
            )

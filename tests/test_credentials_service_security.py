"""CredentialService security tests（P1-E1-3A）.

用 SECRET_MARKER 验证：
- Service 异常 str / repr 不含 marker
- CredentialOperationResult 不含 marker
- CredentialView 不暴露 secret_ref / fingerprint / masked value with marker
- 生成的 ID / ref 不含 marker
- 模块 globals 不含 marker
- 补偿失败的异常链 __cause__ 不泄漏 marker
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStoreUnavailableError,
)
from pi_agent_core_py.web.credentials_errors import (
    CredentialCompensationError,
    CredentialInputError,
    CredentialSecretDeleteError,
    CredentialSecretWriteError,
)
from pi_agent_core_py.web.credentials_service import (
    CreateCredentialCommand,
    CredentialService,
)
from pi_agent_core_py.web.credentials_store import SQLiteCredentialStore
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
# Helpers
# ============================================================================


def _assert_no_marker(obj: object) -> None:
    text = repr(obj)
    assert SECRET_MARKER not in text, (
        f"SECRET_MARKER leaked in {type(obj).__name__} repr: {text!r}"
    )
    text_str = str(obj) if hasattr(obj, "__str__") else ""
    assert SECRET_MARKER not in text_str, (
        f"SECRET_MARKER leaked in {type(obj).__name__} str: {text_str!r}"
    )


# ============================================================================
# 1. Generated IDs / refs don't contain marker
# ============================================================================


class TestGeneratedIdsNoMarker:
    async def test_create_session_only_id_and_ref_no_marker(
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
        # 也不要 fingerprint 泄漏
        fp = result.record.fingerprint_sha256
        assert fp is not None
        assert SECRET_MARKER not in fp


# ============================================================================
# 2. Service errors str/repr
# ============================================================================


class TestServiceErrorsNoMarker:
    async def test_input_error_no_marker(
        self,
        service: CredentialService,
    ) -> None:
        try:
            await service.create(
                CreateCredentialCommand(
                    label=SECRET_MARKER,  # label 也当作敏感字段处理
                    storage_mode="session_only",
                    secret_value=SECRET_MARKER,
                )
            )
        except CredentialInputError as e:
            _assert_no_marker(e)

    async def test_secret_write_error_no_marker(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        class _FailingSetStore(InMemorySecretStore):
            async def set(self, secret_ref: str, value: str) -> None:
                raise SecretStoreUnavailableError(
                    "set fails",
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
        service = CredentialService(repository=repository, router=router)
        try:
            await service.create(
                CreateCredentialCommand(
                    label="X",
                    storage_mode="keyring",
                    secret_value=SECRET_MARKER,
                )
            )
        except CredentialSecretWriteError as e:
            _assert_no_marker(e)
            # __cause__ 是 SecretStoreUnavailableError——其 secret_ref 字段也不含 marker
            cause = e.__cause__
            assert cause is not None
            assert SECRET_MARKER not in str(cause)
            assert SECRET_MARKER not in repr(cause)

    async def test_compensation_error_no_marker(
        self,
        repository: SQLiteCredentialStore,
        session_only_store: InMemorySecretStore,
    ) -> None:
        """create 失败后补偿——exception 不含 marker."""
        router = SecretStoreRouter(
            stores={
                "keyring": InMemorySecretStore(),
                "session_only": session_only_store,
                "env": EnvSecretStore(),
            }
        )
        # 第一次 create 占住 cred-x
        seed = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",
            secret_ref_factory=lambda: "secret-seed",
        )
        await seed.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value="sk-test-1234567890",
            )
        )

        # 第二次 create 失败
        fail = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",
            secret_ref_factory=lambda: "secret-fail",
        )
        try:
            await fail.create(
                CreateCredentialCommand(
                    label="Y",
                    storage_mode="session_only",
                    secret_value=SECRET_MARKER,
                )
            )
        except CredentialCompensationError as e:
            _assert_no_marker(e)

    async def test_secret_delete_error_no_marker(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        class _FailingDeleteStore(InMemorySecretStore):
            async def delete(self, secret_ref: str) -> None:
                raise SecretStoreUnavailableError(
                    "delete fails",
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
        service = CredentialService(repository=repository, router=router)
        # 先 seed（set 不抛）
        seed_result = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="keyring",
                secret_value=SECRET_MARKER,
            )
        )
        cred_id = seed_result.record.id

        try:
            await service.delete(cred_id)
        except CredentialSecretDeleteError as e:
            _assert_no_marker(e)
            # cause 也不泄漏
            cause = e.__cause__
            assert cause is not None
            assert SECRET_MARKER not in str(cause)
            assert SECRET_MARKER not in repr(cause)


# ============================================================================
# 3. CredentialView & OperationResult——no marker / no ref / no fingerprint
# ============================================================================


class TestResultAndViewSafety:
    async def test_operation_result_repr_no_marker(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
        result = await service.create(cmd)
        # record 是完整 record——其 repr 可能含 masked_value（不含完整 marker）
        # 但 record 不应包含完整 marker（masked 是 sk****XXXX 形式）
        rec_text = repr(result.record)
        assert SECRET_MARKER not in rec_text
        # result 整体 repr
        _assert_no_marker(result)

    async def test_credential_view_does_not_expose_secret_ref(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value="sk-test-1234567890",
        )
        result = await service.create(cmd)
        view = await service.get(result.record.id)

        # CredentialView 不应有 secret_ref / fingerprint_sha256 属性
        assert not hasattr(view, "secret_ref")
        assert not hasattr(view, "fingerprint_sha256")
        assert not hasattr(view, "fingerprint")

    async def test_credential_view_repr_no_marker(
        self,
        service: CredentialService,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
        result = await service.create(cmd)
        view = await service.get(result.record.id)
        # view 应当只暴露 masked_value（不含完整 marker）
        _assert_no_marker(view)
        # masked_value 应当是 mask_secret(marker)——不含 marker
        from pi_agent_core_py.secrets import mask_secret

        assert view.masked_value == mask_secret(SECRET_MARKER)


# ============================================================================
# 4. Compensation chain——__cause__ doesn't leak
# ============================================================================


class TestCompensationChainSafety:
    async def test_compensation_cause_chain_no_marker(
        self,
        repository: SQLiteCredentialStore,
    ) -> None:
        """create 补偿失败——__cause__ 链中所有异常都不含 marker."""
        # 让 SecretStore.delete 失败
        class _FailingDeleteStore(InMemorySecretStore):
            async def delete(self, secret_ref: str) -> None:
                raise SecretStoreUnavailableError(
                    "delete fails",
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
        seed = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",
            secret_ref_factory=lambda: "secret-seed",
        )
        await seed.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="keyring",
                secret_value="sk-test-1234567890",
            )
        )

        fail = CredentialService(
            repository=repository,
            router=router,
            credential_id_factory=lambda: "cred-x",
            secret_ref_factory=lambda: "secret-fail",
        )
        try:
            await fail.create(
                CreateCredentialCommand(
                    label="Y",
                    storage_mode="keyring",
                    secret_value=SECRET_MARKER,
                )
            )
            raise AssertionError("expected CredentialCompensationError")
        except CredentialCompensationError as e:
            # 遍历 __cause__ 链
            current: BaseException | None = e
            while current is not None:
                assert SECRET_MARKER not in str(current), (
                    f"marker leaked in cause chain: {type(current).__name__}"
                )
                assert SECRET_MARKER not in repr(current), (
                    f"marker leaked in cause chain repr: {type(current).__name__}"
                )
                current = current.__cause__


# ============================================================================
# 5. Module globals——no marker
# ============================================================================


def test_credentials_service_module_globals_no_marker() -> None:
    import pi_agent_core_py.web.credentials_service as mod

    for name, value in vars(mod).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert SECRET_MARKER not in value, (
                f"marker in module global {name}: {value!r}"
            )


def test_credentials_errors_module_globals_no_marker() -> None:
    import pi_agent_core_py.web.credentials_errors as mod

    for name, value in vars(mod).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert SECRET_MARKER not in value, (
                f"marker in module global {name}: {value!r}"
            )


# ============================================================================
# 6. Pickle safety——CredentialView safe to pickle but no secret
# ============================================================================


class TestPickleSafety:
    async def test_credential_view_pickle_does_not_contain_marker(
        self,
        service: CredentialService,
    ) -> None:
        import pickle

        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
        result = await service.create(cmd)
        view = await service.get(result.record.id)

        pickled = pickle.dumps(view)
        assert SECRET_MARKER not in pickled.decode("latin-1", errors="ignore")

        # 反序列化也安全
        unpickled = pickle.loads(pickle.dumps(view))
        assert not hasattr(unpickled, "secret_ref")
        assert not hasattr(unpickled, "fingerprint_sha256")


# ============================================================================
# 7. SQLite DB bytes——no marker
# ============================================================================


class TestSQLiteBytesNoMarker:
    async def test_sqlite_file_does_not_contain_marker(
        self,
        service: CredentialService,
        repository: SQLiteCredentialStore,
        tmp_path,
    ) -> None:
        cmd = CreateCredentialCommand(
            label="X",
            storage_mode="session_only",
            secret_value=SECRET_MARKER,
        )
        await service.create(cmd)
        # Close repo to flush
        await repository.close()

        db_path = tmp_path / "creds.db"
        data = db_path.read_bytes()

        assert SECRET_MARKER not in data.decode("utf-8", errors="ignore"), (
            "SECRET_MARKER leaked into SQLite file bytes"
        )
        assert SECRET_MARKER not in data.decode("latin-1", errors="ignore"), (
            "SECRET_MARKER leaked (latin-1 decode)"
        )


# ============================================================================
# 8. Service module doesn't expose secret value via accidental attrs
# ============================================================================


class TestServiceObjectAttrsNoMarker:
    async def test_service_instance_attrs_no_marker(
        self,
        service: CredentialService,
    ) -> None:
        # Service 实例不应有任何 attr 含 marker（不缓存 secret）
        await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )

        # 遍历 service 实例的非 dunder 属性
        for name in vars(service):
            value = getattr(service, name)
            if isinstance(value, str):
                assert SECRET_MARKER not in value, (
                    f"marker in service attr {name}"
                )

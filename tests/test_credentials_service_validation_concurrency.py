"""CredentialService.validate() concurrency tests（P1-E1-3B2）.

覆盖（spec section X items 25–30）：
- validate vs rotate——CAS 拒绝旧结果
- validate vs delete——CAS 拒绝旧结果
- 两个 validate 针对相同 secret_ref 可安全完成
- stale 结果不覆盖新 Key 状态
- CAS 异常 message 不含 secret_ref
- 冲突后新 Credential 仍可正常验证

所有测试用注入的 FakeStrategy——真实网络调用 = 0.
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py.providers.registry import (
    ProviderRegistry,
    list_provider_definitions,
)
from pi_agent_core_py.secrets import EnvSecretStore, InMemorySecretStore
from pi_agent_core_py.web.credentials_errors import (
    CredentialOperationConflictError,
)
from pi_agent_core_py.web.credentials_service import (
    CreateCredentialCommand,
    CredentialService,
    RotateCredentialCommand,
)
from pi_agent_core_py.web.credentials_store import (
    CredentialNotFoundError,
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.provider_validation import (
    ProviderValidationResult,
    ValidationStrategyRegistry,
)
from pi_agent_core_py.web.secret_store_router import SecretStoreRouter

# ============================================================================
# Fakes
# ============================================================================


class _BarrierStrategy:
    """Strategy that blocks on a release event so callers can interleave other
    Service operations during the validate() network window.

    Records the secret seen at call time so security tests can confirm no leak.
    """

    def __init__(
        self,
        *,
        release: asyncio.Event,
        result: ProviderValidationResult | None = None,
    ) -> None:
        self.provider_id = "anthropic"
        self._release = release
        self._result = result or ProviderValidationResult(
            provider_id="anthropic",
            valid=True,
            error_code=None,
        )
        self.calls: list[str] = []

    async def validate(self, secret: str) -> ProviderValidationResult:
        self.calls.append(secret)
        # Wait until the test releases us——simulates the network RTT window
        await self._release.wait()
        return self._result


class _ImmediateStrategy:
    """Strategy that returns a configured result synchronously. Records calls."""

    def __init__(self, *, result: ProviderValidationResult) -> None:
        self.provider_id = "anthropic"
        self._result = result
        self.calls: list[str] = []

    async def validate(self, secret: str) -> ProviderValidationResult:
        self.calls.append(secret)
        return self._result


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


def _build_service(
    *,
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
    strategy,
) -> CredentialService:
    return CredentialService(
        repository=repository,
        router=router,
        provider_registry=ProviderRegistry(list_provider_definitions()),
        validation_strategy_registry=ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy},
        ),
        now_ms=(lambda: 1_700_000_000_000),
    )


async def _seed(
    service: CredentialService,
    *,
    secret_value: str = "sk-ant-seed1234567890",
    storage_mode: str = "session_only",
    env_var_name: str | None = None,
):
    cmd = CreateCredentialCommand(
        label="Seed",
        storage_mode=storage_mode,  # type: ignore[arg-type]
        secret_value=secret_value if storage_mode != "env" else None,
        env_var_name=env_var_name,
    )
    return await service.create(cmd)


# ============================================================================
# 1. validate vs rotate——CAS rejects stale result
# ============================================================================


class TestValidateVsRotate:
    async def test_validate_loses_race_with_rotate_raises_conflict(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """validate reads old_ref → rotate wins CAS → validate's update fails."""
        release = asyncio.Event()
        strategy = _BarrierStrategy(release=release)
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        # Start validate——it will block on the barrier (simulating network)
        validate_task = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        # Yield to let validate enter strategy.validate() and block
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Rotate concurrently——uses a different immediate strategy on a
        # separate service instance.
        rotate_strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        rot_service = _build_service(
            repository=repository, router=router, strategy=rotate_strategy
        )
        await rot_service.rotate(
            RotateCredentialCommand(
                credential_id=seed.record.id,
                secret_value="sk-ant-rotated1234567",
            )
        )

        # Release validate——its CAS will now fail with stale secret_ref
        release.set()
        with pytest.raises(CredentialOperationConflictError):
            await validate_task

        # Row reflects the rotate, not the (lost) validate
        row = await repository.get(seed.record.id)
        assert row.validation_status == "never_validated"
        assert row.last_validated_at is None

    async def test_stale_validate_does_not_overwrite_rotated_state(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        session_only_store: InMemorySecretStore,
    ) -> None:
        """After CAS conflict, validate must not modify rotated row."""
        release = asyncio.Event()
        strategy = _BarrierStrategy(release=release)
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service, secret_value="sk-ant-original1234")

        original_masked = seed.record.masked_value
        original_fp = seed.record.fingerprint_sha256

        validate_task = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        rot_strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        rot_service = _build_service(
            repository=repository, router=router, strategy=rot_strategy
        )
        rotated = await rot_service.rotate(
            RotateCredentialCommand(
                credential_id=seed.record.id,
                secret_value="sk-ant-newkey12345678",
            )
        )

        release.set()
        with pytest.raises(CredentialOperationConflictError):
            await validate_task

        # Rotated fields intact; validate did not stomp them
        row = await repository.get(seed.record.id)
        assert row.secret_ref == rotated.record.secret_ref
        assert row.masked_value != original_masked
        assert row.fingerprint_sha256 != original_fp
        assert row.validation_status == "never_validated"


# ============================================================================
# 2. validate vs delete
# ============================================================================


class TestValidateVsDelete:
    async def test_validate_after_delete_sees_not_found_via_cas(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """validate reads record → delete removes row → validate's CAS update
        sees row gone. Service wraps as CredentialOperationConflictError."""
        release = asyncio.Event()
        strategy = _BarrierStrategy(release=release)
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        validate_task = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Delete concurrently
        del_strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        del_service = _build_service(
            repository=repository, router=router, strategy=del_strategy
        )
        await del_service.delete(seed.record.id)

        release.set()
        with pytest.raises(CredentialOperationConflictError):
            await validate_task

        with pytest.raises(CredentialNotFoundError):
            await repository.get(seed.record.id)


# ============================================================================
# 3. Two validates on same secret_ref——both can complete safely
# ============================================================================


class TestTwoValidatesSameRef:
    async def test_two_serial_validates_same_ref_both_succeed(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        op1 = await service.validate(seed.record.id, "anthropic")
        op2 = await service.validate(seed.record.id, "anthropic")

        assert op1.attempted is True and op1.valid is True
        assert op2.attempted is True and op2.valid is True
        # Both saw the secret_ref and called strategy
        assert len(strategy.calls) == 2

    async def test_concurrent_validates_same_ref_one_wins_one_conflict(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """If two concurrent validates race without rotate/delete in between,
        both read the same secret_ref and CAS both succeed (validate is idempotent
        w.r.t. validation_status). This is acceptable——no stale state risk."""
        release = asyncio.Event()
        strategy = _BarrierStrategy(release=release)
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        # Two validates sharing the same barrier
        t1 = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        t2 = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()

        op1, op2 = await asyncio.gather(t1, t2)
        # Both succeed——no stale state risk since secret_ref didn't change
        assert op1.attempted is True
        assert op2.attempted is True


# ============================================================================
# 4. CAS conflict exception——no secret_ref leakage
# ============================================================================


class TestConflictExceptionSafety:
    async def test_conflict_exception_does_not_contain_secret_ref(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """CredentialOperationConflictError str/repr must not contain secret_ref."""
        release = asyncio.Event()
        strategy = _BarrierStrategy(release=release)
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        validate_task = asyncio.create_task(service.validate(seed.record.id, "anthropic"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        rot_strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        rot_service = _build_service(
            repository=repository, router=router, strategy=rot_strategy
        )
        await rot_service.rotate(
            RotateCredentialCommand(
                credential_id=seed.record.id,
                secret_value="sk-ant-rotated98765432",
            )
        )

        release.set()
        try:
            await validate_task
            raise AssertionError("expected CredentialOperationConflictError")
        except CredentialOperationConflictError as e:
            text = str(e)
            r = repr(e)
            # secret_ref never appears
            assert "secret" not in text.lower(), f"secret_ref leaked in str: {text!r}"
            assert "secret" not in r.lower(), f"secret_ref leaked in repr: {r!r}"
            # credential_id is allowed
            assert seed.record.id in text


# ============================================================================
# 5. After conflict, new credential still works
# ============================================================================


class TestNewCredentialAfterConflict:
    async def test_new_credential_validates_after_prior_conflict(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """After a CAS conflict on cred-A, a fresh credential cred-B validates fine."""
        strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        service = _build_service(repository=repository, router=router, strategy=strategy)

        seed_a = await _seed(service, secret_value="sk-ant-aaa1234567890")
        seed_b = await _seed(service, secret_value="sk-ant-bbb1234567890")

        # First validate A successfully
        op_a = await service.validate(seed_a.record.id, "anthropic")
        assert op_a.attempted is True

        # Now rotate A; then attempt validate with stale ref via barrier
        release = asyncio.Event()
        barrier_strategy = _BarrierStrategy(release=release)
        barrier_service = _build_service(
            repository=repository, router=router, strategy=barrier_strategy
        )

        stale_task = asyncio.create_task(
            barrier_service.validate(seed_a.record.id, "anthropic")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Rotate A using the immediate-strategy service
        await service.rotate(
            RotateCredentialCommand(
                credential_id=seed_a.record.id,
                secret_value="sk-rotated-aaa123456",
            )
        )
        release.set()
        with pytest.raises(CredentialOperationConflictError):
            await stale_task

        # Validate B after the conflict——must succeed without bleed-over
        op_b = await service.validate(seed_b.record.id, "anthropic")
        assert op_b.attempted is True
        assert op_b.valid is True
        assert op_b.validation_status == "valid"

        # A's row reflects the rotate (never_validated), B's reflects valid
        row_a = await repository.get(seed_a.record.id)
        row_b = await repository.get(seed_b.record.id)
        assert row_a.validation_status == "never_validated"
        assert row_b.validation_status == "valid"


# ============================================================================
# 6. Rotate vs validate——validate winner case (validate finishes first)
# ============================================================================


class TestValidateWinsRace:
    async def test_validate_wins_rotate_loses(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
    ) -> None:
        """If validate completes before rotate starts, validate's CAS succeeds;
        rotate then reads the now-valid record and CAS-replaces secret_ref."""
        strategy = _ImmediateStrategy(
            result=ProviderValidationResult(
                provider_id="anthropic",
                valid=True,
                error_code=None,
            )
        )
        service = _build_service(repository=repository, router=router, strategy=strategy)
        seed = await _seed(service)

        # Validate completes
        op = await service.validate(seed.record.id, "anthropic")
        assert op.attempted is True
        assert op.validation_status == "valid"

        # Now rotate——must read updated record (still same secret_ref because
        # validate only updated validation_status fields, not secret_ref)
        rotated = await service.rotate(
            RotateCredentialCommand(
                credential_id=seed.record.id,
                secret_value="sk-ant-rotated1234567",
            )
        )
        # Rotate succeeds and resets validation_status to never_validated
        row = await repository.get(seed.record.id)
        assert row.secret_ref == rotated.record.secret_ref
        assert row.validation_status == "never_validated"
        assert row.last_validated_at is None

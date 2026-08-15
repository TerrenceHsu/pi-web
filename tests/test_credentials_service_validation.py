"""CredentialService.validate() tests（P1-E1-3B2）.

覆盖：
- 基础接线（items 1–10）：session_only/keyring/env 成功；env 动态读取；
  env 变量变更后用新值；provider_hint 不影响显式 provider；
  unknown provider 不读 Secret；GLM unsupported 不读 Secret；
  Secret 缺失不调 Strategy；Backend 不可用不调 Strategy；Credential row 不存在.
- 状态映射（items 11–24）：valid/401/403/429/timeout/protocol/unknown 映射；
  success 清空 last_error_code；失败写入 last_error_code；
  last_validated_provider_id 正确；last_validated_at 用固定测试时钟；
  不修改 secret_ref / masked_value / fingerprint.

所有测试用注入的 FakeStrategy——真实网络调用 = 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest

from pi_agent_core_py.providers.registry import (
    ProviderRegistry,
)
from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStoreUnavailableError,
)
from pi_agent_core_py.web.credentials.errors import (
    CredentialBackendUnavailableError,
    CredentialServiceError,
)
from pi_agent_core_py.web.credentials.service import (
    CreateCredentialCommand,
    CredentialService,
)
from pi_agent_core_py.web.credentials.store import (
    CredentialNotFoundError,
    SQLiteCredentialStore,
)
from pi_agent_core_py.web.provider_validation import (
    CredentialValidationErrorCode,
    ProviderValidationResult,
    ValidationStrategyRegistry,
)
from pi_agent_core_py.web.credentials.secret_store import SecretStoreRouter

# ============================================================================
# Fakes
# ============================================================================


@dataclass
class _StrategyCall:
    """Record of one strategy.validate() call. secret is intentionally stored
    so security tests can prove Service did NOT leak it elsewhere."""

    secret: str
    provider_id_seen: str


class _FakeStrategy:
    """Minimal fake strategy. Returns configured result; records calls."""

    def __init__(
        self,
        *,
        provider_id: str = "anthropic",
        result: ProviderValidationResult | None = None,
        calls: list[_StrategyCall] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._result = result or ProviderValidationResult(
            provider_id=provider_id,
            valid=True,
            error_code=None,
        )
        self._calls = calls if calls is not None else []
        self._raise_exc = raise_exc

    async def validate(self, secret: str) -> ProviderValidationResult:
        if self._raise_exc is not None:
            raise self._raise_exc
        self._calls.append(
            _StrategyCall(secret=secret, provider_id_seen=self.provider_id)
        )
        return self._result


def _result(
    valid: bool,
    code: CredentialValidationErrorCode | None = None,
    *,
    provider_id: str = "anthropic",
) -> ProviderValidationResult:
    return ProviderValidationResult(
        provider_id=provider_id,
        valid=valid,
        error_code=code,
    )


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
def fake_strategy() -> _FakeStrategy:
    return _FakeStrategy()


@pytest.fixture
def strategy_registry(fake_strategy: _FakeStrategy) -> ValidationStrategyRegistry:
    return ValidationStrategyRegistry(
        strategies={"anthropic_models": fake_strategy},
    )


@pytest.fixture
def provider_registry() -> ProviderRegistry:
    """Use the built-in registry (Anthropic + GLM)."""
    from pi_agent_core_py.providers.registry import list_provider_definitions

    return ProviderRegistry(list_provider_definitions())


@pytest.fixture
def fixed_clock() -> tuple[list[int], callable]:
    """Return ([], now_ms) where now_ms pops deterministic values."""
    times: list[int] = [1_700_000_000_000]

    def _now_ms() -> int:
        return times[0]

    return times, _now_ms


@pytest.fixture
def service(
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
    provider_registry: ProviderRegistry,
    strategy_registry: ValidationStrategyRegistry,
    fixed_clock,
) -> CredentialService:
    _times, now_ms = fixed_clock
    return CredentialService(
        repository=repository,
        router=router,
        provider_registry=provider_registry,
        validation_strategy_registry=strategy_registry,
        now_ms=now_ms,
    )


# ============================================================================
# Helpers
# ============================================================================


async def _seed_session_only(
    service: CredentialService,
    secret_value: str = "sk-ant-test1234567890",
    *,
    label: str = "Seed",
):
    """Seed a session_only credential via the default service create() path."""
    return await service.create(
        CreateCredentialCommand(
            label=label,
            storage_mode="session_only",
            secret_value=secret_value,
        )
    )


# ============================================================================
# 1. Basic wiring
# ============================================================================


class TestBasicWiring:
    async def test_session_only_validate_success(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        result = await service.validate(seed.record.id, "anthropic")

        assert result.attempted is True
        assert result.valid is True
        assert result.error_code is None
        assert result.validation_status == "valid"
        assert result.last_validated_at == 1_700_000_000_000
        # Strategy 被调用一次，收到了真实 secret
        assert len(fake_strategy._calls) == 1
        assert fake_strategy._calls[0].secret == "sk-ant-test1234567890"

    async def test_keyring_validate_success(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
        keyring_store: InMemorySecretStore,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="K",
                storage_mode="keyring",
                secret_value="sk-ant-keyring1234",
            )
        )
        result = await service.validate(seed.record.id, "anthropic")
        assert result.attempted is True
        assert result.valid is True
        # Strategy 拿到的就是写入的 secret
        assert fake_strategy._calls[0].secret == "sk-ant-keyring1234"

    async def test_env_validate_reads_current_env_value(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("MY_APP_KEY", "env-value-v1")
        seed = await service.create(
            CreateCredentialCommand(
                label="E",
                storage_mode="env",
                env_var_name="MY_APP_KEY",
            )
        )
        await service.validate(seed.record.id, "anthropic")
        # Strategy 收到的是当前 env 值
        assert fake_strategy._calls[0].secret == "env-value-v1"

    async def test_env_value_change_is_observed_on_next_validate(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
        monkeypatch,
    ) -> None:
        """EnvSecretStore.get 不缓存——第二次 validate 必须读到新值."""
        monkeypatch.setenv("MY_APP_KEY", "v1")
        seed = await service.create(
            CreateCredentialCommand(
                label="E",
                storage_mode="env",
                env_var_name="MY_APP_KEY",
            )
        )
        await service.validate(seed.record.id, "anthropic")
        assert fake_strategy._calls[-1].secret == "v1"

        monkeypatch.setenv("MY_APP_KEY", "v2-very-new")
        await service.validate(seed.record.id, "anthropic")
        assert fake_strategy._calls[-1].secret == "v2-very-new"

    async def test_provider_hint_does_not_override_explicit_provider(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        """sk-ant- 前缀 → hint=anthropic/high，但调用方传 glm 应当走 GLM 路径."""
        # Strategy 是 GLM=unsupported——fake 占位 anthropic_models；GLM 走 unsupported 分支
        seed = await _seed_session_only(
            service, secret_value="sk-ant-prefix-detected1234"
        )
        result = await service.validate(seed.record.id, "glm")
        assert result.attempted is False
        assert result.error_code == "validation_not_supported"
        # Strategy 没被调用
        assert len(fake_strategy._calls) == 0

    async def test_unknown_provider_does_not_read_secret(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        """Provider 不在 registry → 不读 Secret，不调 Strategy."""
        # Build a registry without 'glm' so we can pick an unknown provider id
        # without fiddling with built-in defs.
        # Use a registry containing only anthropic; 'unknown' will miss.
        from pi_agent_core_py.providers.registry import list_provider_definitions

        anthropic_only = ProviderRegistry(
            tuple(d for d in list_provider_definitions() if d.id == "anthropic")
        )
        service._provider_registry = anthropic_only

        seed = await _seed_session_only(service)
        result = await service.validate(seed.record.id, "unknown_provider")
        assert result.attempted is False
        assert result.error_code == "provider_not_supported"
        assert len(fake_strategy._calls) == 0

    async def test_glm_unsupported_does_not_read_secret(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        result = await service.validate(seed.record.id, "glm")
        assert result.attempted is False
        assert result.error_code == "validation_not_supported"
        assert len(fake_strategy._calls) == 0

    async def test_secret_missing_does_not_call_strategy(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
        session_only_store: InMemorySecretStore,
    ) -> None:
        """Secret 在 store 中找不到（store 返回 None）→ attempted=False / credential_missing."""
        # 直接在 repo 写一条 record，但 store 中没有对应 secret
        from pi_agent_core_py.web.credentials.store import CredentialRecord

        now_ms = service._now_ms()
        orphan = CredentialRecord(
            id="cred-orphan",
            label="Orphan",
            storage_mode="session_only",
            secret_ref="secret-not-in-store",
            masked_value="sk****XXXX",
            fingerprint_sha256="abc",
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=now_ms,
            updated_at=now_ms,
        )
        await service._repository.create(orphan)

        result = await service.validate("cred-orphan", "anthropic")
        assert result.attempted is False
        assert result.error_code == "credential_missing"
        assert len(fake_strategy._calls) == 0

    async def test_backend_unavailable_does_not_call_strategy(
        self,
        repository: SQLiteCredentialStore,
        fake_strategy: _FakeStrategy,
        provider_registry: ProviderRegistry,
        strategy_registry: ValidationStrategyRegistry,
        fixed_clock,
        session_only_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """keyring backend returns is_available=False → validate() raises;
        strategy not called."""

        class _UnavailableStore(InMemorySecretStore):
            async def is_available(self) -> bool:
                return False

        router = SecretStoreRouter(
            stores={
                "keyring": _UnavailableStore(),
                "session_only": session_only_store,
                "env": env_store,
            }
        )
        _times, now_ms = fixed_clock
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=now_ms,
        )
        # Insert record directly (we can't go through create() since
        # create() also resolves the store and would raise first).
        from pi_agent_core_py.web.credentials.store import CredentialRecord

        ts = now_ms()
        record = CredentialRecord(
            id="cred-keyring-down",
            label="K",
            storage_mode="keyring",
            secret_ref="secret-keyring-down",
            masked_value="sk****XXXX",
            fingerprint_sha256="fp",
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=ts,
            updated_at=ts,
        )
        await repository.create(record)

        with pytest.raises(CredentialBackendUnavailableError):
            await service.validate("cred-keyring-down", "anthropic")
        assert len(fake_strategy._calls) == 0

    async def test_credential_row_missing_raises_not_found(
        self,
        service: CredentialService,
    ) -> None:
        """CredentialRecord 不存在 → CredentialNotFoundError（不是 outcome）."""
        with pytest.raises(CredentialNotFoundError):
            await service.validate("cred-does-not-exist", "anthropic")


# ============================================================================
# 2. State mapping
# ============================================================================


class TestStateMapping:
    @pytest.mark.parametrize(
        "error_code,expected_status",
        [
            ("authentication_failed", "invalid"),
            ("permission_denied", "error"),
            ("rate_limited", "error"),
            ("endpoint_unreachable", "error"),
            ("request_timeout", "error"),
            ("tls_error", "error"),
            ("protocol_error", "error"),
            ("unknown_error", "error"),
        ],
    )
    async def test_remote_failure_maps_to_status(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
        error_code: CredentialValidationErrorCode,
        expected_status: Literal["invalid", "error"],
    ) -> None:
        seed = await _seed_session_only(service)
        # Replace fake's result
        fake_strategy._result = _result(False, error_code)

        op = await service.validate(seed.record.id, "anthropic")

        assert op.attempted is True
        assert op.valid is False
        assert op.error_code == error_code
        assert op.validation_status == expected_status

        # Repository 也写入
        row = await service._repository.get(seed.record.id)
        assert row.validation_status == expected_status
        assert row.last_error_code == error_code

    async def test_valid_success_writes_valid_status(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        op = await service.validate(seed.record.id, "anthropic")
        assert op.validation_status == "valid"

        row = await service._repository.get(seed.record.id)
        assert row.validation_status == "valid"

    async def test_success_clears_old_last_error_code(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        """先 401（写 last_error_code）→ 再 valid（清空）."""
        seed = await _seed_session_only(service)

        # First: fail with 401
        fake_strategy._result = _result(False, "authentication_failed")
        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.last_error_code == "authentication_failed"
        assert row.validation_status == "invalid"

        # Then: succeed
        fake_strategy._result = _result(True, None)
        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.last_error_code is None
        assert row.validation_status == "valid"

    async def test_last_validated_provider_id_set(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        await service.validate(seed.record.id, "anthropic")

        row = await service._repository.get(seed.record.id)
        assert row.last_validated_provider_id == "anthropic"

    async def test_last_validated_at_uses_injected_clock(
        self,
        service: CredentialService,
        fixed_clock,
    ) -> None:
        times, _ = fixed_clock
        times[0] = 1_700_000_500_000  # overwrite fixture default

        seed = await _seed_session_only(service)
        op = await service.validate(seed.record.id, "anthropic")
        assert op.last_validated_at == 1_700_000_500_000

        row = await service._repository.get(seed.record.id)
        assert row.last_validated_at == 1_700_000_500_000

    async def test_does_not_modify_secret_ref(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        original_ref = seed.record.secret_ref

        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.secret_ref == original_ref

    async def test_does_not_modify_masked_value(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(
            service, secret_value="sk-ant-test1234567890"
        )
        original_masked = seed.record.masked_value

        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.masked_value == original_masked

    async def test_does_not_modify_fingerprint(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        original_fp = seed.record.fingerprint_sha256

        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.fingerprint_sha256 == original_fp


# ============================================================================
# 3. Strategy invariant violations
# ============================================================================


class TestStrategyInvariantViolations:
    async def test_strategy_returning_wrong_provider_id_raises(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        # Strategy claims to be anthropic but returns result for different provider
        fake_strategy._result = ProviderValidationResult(
            provider_id="other-provider",
            valid=True,
            error_code=None,
        )
        # Update provider_id attr too
        fake_strategy.provider_id = "anthropic"  # validate uses registry key not this

        with pytest.raises(CredentialServiceError):
            await service.validate(seed.record.id, "anthropic")

        # Repo 未修改
        row = await service._repository.get(seed.record.id)
        assert row.validation_status == "never_validated"
        assert row.last_validated_at is None

    async def test_strategy_raising_unexpected_exception_wrapped(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        fake_strategy._raise_exc = RuntimeError("boom")

        with pytest.raises(CredentialServiceError):
            await service.validate(seed.record.id, "anthropic")

        # Repo 未修改
        row = await service._repository.get(seed.record.id)
        assert row.validation_status == "never_validated"
        assert row.last_validated_at is None

    async def test_strategy_returning_valid_false_without_error_code_raises(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        fake_strategy._result = ProviderValidationResult(
            provider_id="anthropic",
            valid=False,
            error_code=None,
        )

        with pytest.raises(CredentialServiceError):
            await service.validate(seed.record.id, "anthropic")

    async def test_strategy_returning_non_attempted_error_code_raises(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        """Strategy 不允许返回 credential-layer 码（如 credential_missing）."""
        seed = await _seed_session_only(service)
        fake_strategy._result = ProviderValidationResult(
            provider_id="anthropic",
            valid=False,
            error_code="credential_missing",
        )

        with pytest.raises(CredentialServiceError):
            await service.validate(seed.record.id, "anthropic")

    async def test_strategy_returning_valid_true_with_error_code_raises(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        seed = await _seed_session_only(service)
        fake_strategy._result = ProviderValidationResult(
            provider_id="anthropic",
            valid=True,
            error_code="authentication_failed",  # 不变量违反
        )

        with pytest.raises(CredentialServiceError):
            await service.validate(seed.record.id, "anthropic")


# ============================================================================
# 4. SecretStore.get failures
# ============================================================================


class TestSecretReadFailures:
    async def test_secret_store_get_raising_raises_service_error(
        self,
        repository: SQLiteCredentialStore,
        fake_strategy: _FakeStrategy,
        provider_registry: ProviderRegistry,
        strategy_registry: ValidationStrategyRegistry,
        fixed_clock,
        session_only_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """SecretStore.get 抛 SecretStoreError → CredentialServiceError."""

        class _FailingGetStore(InMemorySecretStore):
            async def get(self, secret_ref: str):
                raise SecretStoreUnavailableError(
                    "get fails",
                    secret_ref=secret_ref,
                )

        store = _FailingGetStore()
        # ensure is_available is True so we get to .get()
        router = SecretStoreRouter(
            stores={
                "keyring": InMemorySecretStore(),
                "session_only": store,
                "env": env_store,
            }
        )
        _times, now_ms = fixed_clock
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=now_ms,
        )
        # Insert record manually pointing at store that fails
        from pi_agent_core_py.web.credentials.store import CredentialRecord

        now_ms_val = now_ms()
        record = CredentialRecord(
            id="cred-x",
            label="X",
            storage_mode="session_only",
            secret_ref="secret-x",
            masked_value="sk****XXXX",
            fingerprint_sha256="fp",
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=now_ms_val,
            updated_at=now_ms_val,
        )
        await repository.create(record)

        with pytest.raises(CredentialServiceError):
            await service.validate("cred-x", "anthropic")
        assert len(fake_strategy._calls) == 0


# ============================================================================
# 5. Empty-string secret treated as missing
# ============================================================================


class TestEmptySecret:
    async def test_empty_string_secret_treated_as_missing(
        self,
        repository: SQLiteCredentialStore,
        fake_strategy: _FakeStrategy,
        provider_registry: ProviderRegistry,
        strategy_registry: ValidationStrategyRegistry,
        fixed_clock,
        env_store: EnvSecretStore,
    ) -> None:
        """session_only store 返回 '' → 视为 missing，不调 strategy."""

        class _EmptyStore(InMemorySecretStore):
            async def get(self, secret_ref: str) -> str | None:
                # 不论写入什么，读取时永远返回 ''
                return ""

        store = _EmptyStore()
        # Seed something to ensure the ref exists
        await store.set("secret-empty", "anything")

        router = SecretStoreRouter(
            stores={
                "keyring": InMemorySecretStore(),
                "session_only": store,
                "env": env_store,
            }
        )
        _times, now_ms = fixed_clock
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=now_ms,
        )
        from pi_agent_core_py.web.credentials.store import CredentialRecord

        now_ms_val = now_ms()
        record = CredentialRecord(
            id="cred-empty",
            label="E",
            storage_mode="session_only",
            secret_ref="secret-empty",
            masked_value="sk****XXXX",
            fingerprint_sha256="fp",
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=now_ms_val,
            updated_at=now_ms_val,
        )
        await repository.create(record)

        op = await service.validate("cred-empty", "anthropic")
        assert op.attempted is False
        assert op.error_code == "credential_missing"
        assert len(fake_strategy._calls) == 0


# ============================================================================
# 6. Non-attempted result preserves prior validation state
# ============================================================================


class TestNonAttemptedPreservesState:
    async def test_provider_not_supported_does_not_overwrite_prior_state(
        self,
        service: CredentialService,
        fake_strategy: _FakeStrategy,
    ) -> None:
        """A credential validated valid=True earlier; subsequent validate against
        unknown provider should NOT wipe the persisted valid state."""
        seed = await _seed_session_only(service)
        # First: validate successfully
        await service.validate(seed.record.id, "anthropic")
        row = await service._repository.get(seed.record.id)
        assert row.validation_status == "valid"
        first_validated_at = row.last_validated_at

        # Now register a registry that doesn't have 'anthropic'
        empty_registry = ProviderRegistry(())
        service._provider_registry = empty_registry

        op = await service.validate(seed.record.id, "anthropic")
        assert op.attempted is False
        assert op.error_code == "provider_not_supported"
        # Returned DTO still reflects prior state
        assert op.validation_status == "valid"
        assert op.last_validated_at == first_validated_at

        # Repo unchanged
        row = await service._repository.get(seed.record.id)
        assert row.validation_status == "valid"
        assert row.last_validated_at == first_validated_at

"""CredentialService.validate() security tests（P1-E1-3B2）.

用 SECRET_MARKER = PI_E1_SECRET_MARKER_7F3A91D2 验证 secret 在 validate()
所有边界都 0 泄漏：

- CredentialValidationOperationResult repr/str
- Service error str/repr（strategy 异常 / result 不变量违反 / read 失败 / repo 写失败）
- __cause__ 链
- 日志（caplog）
- SQLite 字节 + SELECT *
- Service 实例 attrs（不缓存 secret）
- Strategy spy（即便 spy 自己存了 secret，Service 输出也不含）
- JSON-safe 序列化
- 模块 globals
- HTTP Header（用 AnthropicModelsValidationStrategy + MockTransport 集成测试）
- 并发失败路径（CAS conflict 异常不含 secret / secret_ref）
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
import pytest

from pi_agent_core_py.providers.registry import (
    ProviderRegistry,
    list_provider_definitions,
)
from pi_agent_core_py.secrets import EnvSecretStore, InMemorySecretStore
from pi_agent_core_py.web.credentials.errors import (
    CredentialBackendUnavailableError,
    CredentialOperationConflictError,
    CredentialServiceError,
)
from pi_agent_core_py.web.credentials.service import (
    CreateCredentialCommand,
    CredentialService,
    CredentialValidationOperationResult,
    RotateCredentialCommand,
)
from pi_agent_core_py.web.credentials.store import SQLiteCredentialStore
from pi_agent_core_py.web.provider_validation import (
    AnthropicModelsValidationStrategy,
    HttpClientFactory,
    ProviderValidationResult,
    ValidationStrategyRegistry,
)
from pi_agent_core_py.web.credentials.secret_store import SecretStoreRouter

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Fakes
# ============================================================================


class _RecordingStrategy:
    """Strategy that records the secret it received (so tests can prove the
    Service *did* pass the actual secret to the Strategy), then returns a
    configured result."""

    def __init__(
        self,
        *,
        result: ProviderValidationResult | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.provider_id = "anthropic"
        self._result = result or ProviderValidationResult(
            provider_id="anthropic",
            valid=True,
            error_code=None,
        )
        self._raise_exc = raise_exc
        self.received_secret: str | None = None

    async def validate(self, secret: str) -> ProviderValidationResult:
        # Record the secret——then verify Service outputs don't contain it
        self.received_secret = secret
        if self._raise_exc is not None:
            raise self._raise_exc
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


@pytest.fixture
def recording_strategy() -> _RecordingStrategy:
    return _RecordingStrategy()


@pytest.fixture
def strategy_registry(recording_strategy: _RecordingStrategy) -> ValidationStrategyRegistry:
    return ValidationStrategyRegistry(
        strategies={"anthropic_models": recording_strategy},
    )


@pytest.fixture
def provider_registry() -> ProviderRegistry:
    return ProviderRegistry(list_provider_definitions())


@pytest.fixture
def service(
    repository: SQLiteCredentialStore,
    router: SecretStoreRouter,
    provider_registry: ProviderRegistry,
    strategy_registry: ValidationStrategyRegistry,
) -> CredentialService:
    return CredentialService(
        repository=repository,
        router=router,
        provider_registry=provider_registry,
        validation_strategy_registry=strategy_registry,
        now_ms=(lambda: 1_700_000_000_000),
    )


# ============================================================================
# Helpers
# ============================================================================


def _assert_no_marker(text: object) -> None:
    s = str(text)
    assert SECRET_MARKER not in s, f"SECRET_MARKER leaked: {s!r}"


def _walk_cause_chain(exc: BaseException) -> None:
    """Assert SECRET_MARKER absent in every exception str/repr in the chain."""
    current: BaseException | None = exc
    while current is not None:
        _assert_no_marker(str(current))
        _assert_no_marker(repr(current))
        current = current.__cause__


# ============================================================================
# 1. Operation result safety
# ============================================================================


class TestOperationResultSafety:
    async def test_result_repr_does_not_contain_marker(
        self,
        service: CredentialService,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")
        assert isinstance(op, CredentialValidationOperationResult)
        _assert_no_marker(repr(op))
        _assert_no_marker(str(op))

    async def test_result_json_serialization_safe(
        self,
        service: CredentialService,
    ) -> None:
        """dataclasses.asdict + json.dumps 不应泄漏 marker."""
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")
        text = json.dumps(asdict(op))
        _assert_no_marker(text)


# ============================================================================
# 2. Service errors——str / repr / __cause__ chain
# ============================================================================


class TestServiceErrorSafety:
    async def test_strategy_exception_wrapped_no_marker(
        self,
        service: CredentialService,
        recording_strategy: _RecordingStrategy,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        # Strategy raises with marker in the message——Service must not propagate
        recording_strategy._raise_exc = RuntimeError(f"boom contains {SECRET_MARKER}")

        with pytest.raises(CredentialServiceError) as ei:
            await service.validate(seed.record.id, "anthropic")

        _walk_cause_chain(ei.value)

    async def test_strategy_invalid_result_no_marker(
        self,
        service: CredentialService,
        recording_strategy: _RecordingStrategy,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        # Result with wrong provider_id
        recording_strategy._result = ProviderValidationResult(
            provider_id="other",
            valid=True,
            error_code=None,
        )
        with pytest.raises(CredentialServiceError) as ei:
            await service.validate(seed.record.id, "anthropic")
        _walk_cause_chain(ei.value)

    async def test_backend_unavailable_error_no_marker(
        self,
        repository: SQLiteCredentialStore,
        provider_registry: ProviderRegistry,
        strategy_registry: ValidationStrategyRegistry,
        env_store: EnvSecretStore,
    ) -> None:
        """keyring backend is_available=False → error message has no marker."""

        class _UnavailableStore(InMemorySecretStore):
            async def is_available(self) -> bool:
                return False

        router = SecretStoreRouter(
            stores={
                "keyring": _UnavailableStore(),
                "session_only": InMemorySecretStore(),
                "env": env_store,
            }
        )
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=(lambda: 1),
        )
        # Insert a keyring record pointing at a marker-bearing secret_ref
        # (secret_ref itself is non-sensitive, but we verify marker doesn't
        # leak even if input contains it)
        from pi_agent_core_py.web.credentials.store import CredentialRecord

        record = CredentialRecord(
            id="cred-x",
            label="X",
            storage_mode="keyring",
            secret_ref="secret-x",
            masked_value="sk****XXXX",
            fingerprint_sha256="fp",
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=1,
            updated_at=1,
        )
        await repository.create(record)
        with pytest.raises(CredentialBackendUnavailableError) as ei:
            await service.validate("cred-x", "anthropic")
        _walk_cause_chain(ei.value)

    async def test_conflict_error_no_marker_no_secret_ref(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        provider_registry: ProviderRegistry,
        env_store: EnvSecretStore,
    ) -> None:
        """CAS conflict error message must not contain secret_ref or secret."""
        import asyncio

        release = asyncio.Event()

        class _BarrierStrategy:
            provider_id = "anthropic"

            def __init__(self) -> None:
                self.received_secret: str | None = None

            async def validate(self, secret: str) -> ProviderValidationResult:
                self.received_secret = secret
                await release.wait()
                return ProviderValidationResult(
                    provider_id="anthropic",
                    valid=True,
                    error_code=None,
                )

        barrier = _BarrierStrategy()
        strategy_registry = ValidationStrategyRegistry(
            strategies={"anthropic_models": barrier},
        )
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=(lambda: 1),
        )
        # Seed using SECRET_MARKER as the secret
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        # The actual secret_ref assigned by service.create
        # (not marker; it's a generated ref)
        secret_ref_assigned = seed.record.secret_ref
        assert SECRET_MARKER not in secret_ref_assigned

        # Start validate——blocks on barrier
        import asyncio as _aio

        task = _aio.create_task(service.validate(seed.record.id, "anthropic"))
        await _aio.sleep(0)
        await _aio.sleep(0)

        # Rotate concurrently with a different service (immediate strategy)
        rot_strategy = _RecordingStrategy()
        rot_registry = ValidationStrategyRegistry(
            strategies={"anthropic_models": rot_strategy},
        )
        rot_service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=rot_registry,
            now_ms=(lambda: 2),
        )
        await rot_service.rotate(
            RotateCredentialCommand(
                credential_id=seed.record.id,
                secret_value="sk-ant-newkey123456789",
            )
        )

        release.set()
        with pytest.raises(CredentialOperationConflictError) as ei:
            await task
        _walk_cause_chain(ei.value)
        # Confirm secret_ref didn't sneak into message either
        assert secret_ref_assigned not in str(ei.value)
        assert secret_ref_assigned not in repr(ei.value)


# ============================================================================
# 3. Strategy spy——Service passes secret but outputs don't contain it
# ============================================================================


class TestStrategySpySafety:
    async def test_strategy_spy_receives_marker_but_service_output_clean(
        self,
        service: CredentialService,
        recording_strategy: _RecordingStrategy,
    ) -> None:
        """Even when a spy Strategy intentionally stores the secret, Service's
        own outputs (result / errors / repr) must not contain it."""
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")

        # Spy DID receive the marker (proves Service forwarded the real secret)
        assert recording_strategy.received_secret == SECRET_MARKER
        # But Service's return value doesn't carry it
        _assert_no_marker(repr(op))
        _assert_no_marker(str(op))
        _assert_no_marker(repr(service))


# ============================================================================
# 4. SQLite bytes + SELECT *
# ============================================================================


class TestSQLiteBytesSafety:
    async def test_sqlite_bytes_no_marker_after_validate(
        self,
        service: CredentialService,
        repository: SQLiteCredentialStore,
        tmp_path,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        await service.validate(seed.record.id, "anthropic")
        # Flush
        await repository.close()

        db_path = tmp_path / "creds.db"
        data = db_path.read_bytes()
        _assert_no_marker(data.decode("utf-8", errors="ignore"))
        _assert_no_marker(data.decode("latin-1", errors="ignore"))

    async def test_select_star_no_marker(
        self,
        service: CredentialService,
    ) -> None:
        """Raw SELECT * must not return marker in any column."""
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        await service.validate(seed.record.id, "anthropic")

        # Read raw row
        db = service._repository._db  # type: ignore[attr-defined]
        async with db.execute(
            "SELECT * FROM web_credentials WHERE id = ?",
            (seed.record.id,),
        ) as cursor:
            row = await cursor.fetchone()

        # Stringify every column value
        for col in row.keys():
            val = row[col]
            if val is None:
                continue
            _assert_no_marker(str(val))


# ============================================================================
# 5. Module globals——no marker
# ============================================================================


def test_credentials_service_module_globals_no_marker() -> None:
    import pi_agent_core_py.web.credentials.service as mod

    for name, value in vars(mod).items():
        if isinstance(value, str) and not name.startswith("__"):
            assert SECRET_MARKER not in value, (
                f"marker in module global {name}: {value!r}"
            )


# ============================================================================
# 6. Service instance attrs——no marker cached
# ============================================================================


class TestServiceInstanceAttrsSafety:
    async def test_service_attrs_no_marker_after_validate(
        self,
        service: CredentialService,
    ) -> None:
        await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        # The record just seeded has credential_id matching auto-gen;
        # we want to call validate() against that id.
        rows = await service._repository.list()
        cred_id = rows[0].id
        await service.validate(cred_id, "anthropic")

        # Walk every Service attribute——no marker anywhere
        for name in vars(service):
            value = getattr(service, name)
            if isinstance(value, str):
                assert SECRET_MARKER not in value, (
                    f"marker in service attr {name}: {value!r}"
                )
            # Don't recurse into Repository / Router / registries——those have
            # their own security tests.


# ============================================================================
# 7. Logging——no marker via caplog
# ============================================================================


class TestLoggingSafety:
    async def test_validate_writes_no_marker_to_logs(
        self,
        service: CredentialService,
        caplog,
    ) -> None:
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        caplog.set_level(logging.DEBUG)
        await service.validate(seed.record.id, "anthropic")
        _assert_no_marker(caplog.text)


# ============================================================================
# 8. Integration——AnthropicModelsValidationStrategy + MockTransport
#    Strategy sends marker in x-api-key header; Service output must stay clean.
# ============================================================================


def _mock_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpClientFactory:
    @asynccontextmanager
    async def _factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            yield client

    return _factory


class TestIntegrationAnthropicStrategy:
    async def test_validate_with_real_anthropic_strategy_no_marker_in_output(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        provider_registry: ProviderRegistry,
        tmp_path,
    ) -> None:
        """End-to-end: Service + real AnthropicModelsValidationStrategy +
        MockTransport. Marker is the secret; it appears in the x-api-key header
        (Strategy's responsibility) but must NOT appear in any Service output."""

        def _handler(req: httpx.Request) -> httpx.Response:
            # Verify marker is in header (sanity——Strategy sent it)
            assert req.headers.get("x-api-key") == SECRET_MARKER
            return httpx.Response(
                status_code=200,
                content=b'{"data": [{"id": "claude-sonnet-4-6"}]}',
                headers={"content-type": "application/json"},
            )

        factory = _mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        strategy_registry = ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy},
        )
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=(lambda: 1),
        )
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")
        assert op.attempted is True
        assert op.valid is True

        # All Service surfaces stay clean
        _assert_no_marker(repr(op))
        _assert_no_marker(str(op))
        _assert_no_marker(repr(service))

        # Repository content has no marker
        row = await repository.get(seed.record.id)
        for field in (
            row.id, row.label, row.secret_ref, row.masked_value,
            row.fingerprint_sha256 or "", row.provider_hint or "",
            row.validation_status, row.last_validated_provider_id or "",
            (row.last_error_code or ""),
        ):
            _assert_no_marker(field)

        # SQLite file bytes clean
        await repository.close()
        data = (tmp_path / "creds.db").read_bytes()
        _assert_no_marker(data.decode("utf-8", errors="ignore"))
        _assert_no_marker(data.decode("latin-1", errors="ignore"))

    async def test_validate_failure_path_no_marker_in_output(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        provider_registry: ProviderRegistry,
    ) -> None:
        """Strategy returns 401; Service writes invalid status. Marker must
        not leak anywhere."""

        def _handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=401,
                content=b"",  # body not read on non-2xx
            )

        factory = _mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        strategy_registry = ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy},
        )
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=(lambda: 1),
        )
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")
        assert op.valid is False
        assert op.error_code == "authentication_failed"
        assert op.validation_status == "invalid"

        # Outputs clean
        _assert_no_marker(repr(op))
        _assert_no_marker(str(op))

        # Repository also clean
        row = await repository.get(seed.record.id)
        for field in (
            row.masked_value,
            row.fingerprint_sha256 or "",
            row.last_error_code or "",
        ):
            _assert_no_marker(field)

    async def test_validate_adversarial_response_body_no_marker_in_output(
        self,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        provider_registry: ProviderRegistry,
    ) -> None:
        """If an adversarial 200 response body echoes the secret, Strategy
        already strips body from result; Service outputs stay clean."""

        def _handler(req: httpx.Request) -> httpx.Response:
            # Adversarial body includes the marker
            body = json.dumps({"data": [{"id": SECRET_MARKER}]}).encode()
            return httpx.Response(
                status_code=200,
                content=body,
                headers={"content-type": "application/json"},
            )

        factory = _mock_factory(_handler)
        strategy = AnthropicModelsValidationStrategy(client_factory=factory)
        strategy_registry = ValidationStrategyRegistry(
            strategies={"anthropic_models": strategy},
        )
        service = CredentialService(
            repository=repository,
            router=router,
            provider_registry=provider_registry,
            validation_strategy_registry=strategy_registry,
            now_ms=(lambda: 1),
        )
        seed = await service.create(
            CreateCredentialCommand(
                label="X",
                storage_mode="session_only",
                secret_value=SECRET_MARKER,
            )
        )
        op = await service.validate(seed.record.id, "anthropic")
        # Valid=True; strategy succeeded. Service output has no marker.
        assert op.valid is True
        _assert_no_marker(repr(op))
        _assert_no_marker(str(op))

        # Repo content has no marker either
        row = await repository.get(seed.record.id)
        for field in (
            row.masked_value,
            row.fingerprint_sha256 or "",
        ):
            _assert_no_marker(field)

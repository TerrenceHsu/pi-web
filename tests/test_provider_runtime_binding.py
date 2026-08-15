"""RequestProviderRuntime.build_adapter + bind_to_harness tests（M1-4 §十四.26-50）.

覆盖：
- Secret 读取 / Factory 调用次数 + 参数
- Factory 失败映射 / Secret 读取失败不调 Factory
- 不重新读取 Profile / 不修改 Binding / 不缓存 Adapter
- bind_to_harness: selection=None legacy 路径
- bind_to_harness: 替换 / 恢复 / 关闭 client / 不关 original
- body 抛异常 / 取消 路径
- 失败不污染下一次请求
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import ModelClient
from pi_agent_core_py.providers.base import ProviderAdapter
from pi_agent_core_py.providers.errors import ProviderConfigError
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
)
from pi_agent_core_py.web.credentials.errors import (
    CredentialRequestSecretBackendError,
    CredentialRequestSecretUnavailableError,
)
from pi_agent_core_py.web.credentials.service import (
    CredentialService,
)
from pi_agent_core_py.web.credentials.store import CredentialNotFoundError
from pi_agent_core_py.web.providers.config_service import ProviderConfigService
from pi_agent_core_py.web.providers.runtime import (
    ProviderInitializationError,
    ProviderSelectionUnavailableError,
    RequestProviderRuntime,
    RequestProviderSelection,
)

pytestmark = pytest.mark.asyncio

SECRET_MARKER = "sk-M1-4-BIND-SECRET-MARKER-DO-NOT-LEAK"


# ============================================================================
# Fakes
# ============================================================================


def _make_provider_def(pid: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=pid,
        display_name=pid.upper(),
        api_style="openai_compatible",
        default_base_url=f"https://{pid}.example.com",
        credential_validation_strategy="unsupported",
        credential_validation_endpoint=None,
        supports_model_listing=False,
    )


class FakeRegistry(ProviderRegistry):
    def __init__(self) -> None:
        self._defs = {p: _make_provider_def(p) for p in ("qwen", "glm", "anthropic")}

    def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
        return self._defs.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
        return tuple(self._defs.values())

    def has(self, provider_id: str) -> bool:  # type: ignore[override]
        return provider_id in self._defs


class FakeCredService(CredentialService):
    def __init__(self, *, secret: str = SECRET_MARKER) -> None:
        self._secret = secret
        self.resolve_calls = 0

    async def resolve_secret_for_request(  # type: ignore[override]
        self, credential_id: str
    ) -> str:
        self.resolve_calls += 1
        if self._secret == "RAISE_NOT_FOUND":
            raise CredentialNotFoundError(f"not found: {credential_id}")
        if self._secret == "RAISE_UNAVAILABLE":
            raise CredentialRequestSecretUnavailableError(
                "provider credential is unavailable"
            )
        if self._secret == "RAISE_BACKEND":
            raise CredentialRequestSecretBackendError(
                "provider credential backend is unavailable"
            )
        return self._secret


class _CloseTrackingAdapter(ProviderAdapter):
    """Minimal ProviderAdapter that tracks aclose() calls."""

    def __init__(self, *, provider_id: str, model: str) -> None:
        self.provider_id = provider_id
        self.model = model
        self.close_calls = 0

    async def stream(self, request: Any) -> Any:  # pragma: no cover - never called
        yield  # type: ignore[unreachable]

    async def aclose(self) -> None:
        self.close_calls += 1


class _CloseTrackingClient(ModelClient):
    """ModelClient subclass that tracks close() invocations."""

    def __init__(self, adapter: ProviderAdapter) -> None:
        super().__init__(adapter)
        self.close_calls = 0
        self._closed = False

    async def close(self) -> None:  # type: ignore[override]
        self.close_calls += 1
        self._closed = True
        await super().close()


# ============================================================================
# Common fixture
# ============================================================================


def _make_selection(
    *,
    provider_id: str = "qwen",
    model_id: str = "qwen-plus",
    credential_id: str = "cred-test",
    source: str = "explicit",
) -> RequestProviderSelection:
    return RequestProviderSelection(
        profile_id="prof-test",
        provider_id=provider_id,
        model_id=model_id,
        selection_source=source,  # type: ignore[arg-type]
        credential_id=credential_id,
    )


def _make_runtime(
    *,
    cred: FakeCredService | None = None,
    factory: Any = None,
) -> tuple[RequestProviderRuntime, FakeRegistry, FakeCredService, list[dict[str, Any]]]:
    registry = FakeRegistry()
    cred_service = cred if cred is not None else FakeCredService()
    factory_calls: list[dict[str, Any]] = []

    if factory is None:
        def default_factory(
            *,
            provider_definition: ProviderDefinition,
            api_key: str,
            model_id: str,
        ) -> _CloseTrackingAdapter:
            factory_calls.append(
                {
                    "provider_definition": provider_definition,
                    "api_key": api_key,
                    "model_id": model_id,
                }
            )
            return _CloseTrackingAdapter(
                provider_id=provider_definition.id,
                model=model_id,
            )

        factory = default_factory

    runtime = RequestProviderRuntime(
        provider_config_service=_NoOpConfigService(),
        credential_service=cred_service,
        provider_registry=registry,
        provider_factory=factory,
    )
    return runtime, registry, cred_service, factory_calls


class _NoOpConfigService(ProviderConfigService):
    """Stub——build_adapter / bind_to_harness do not touch ConfigService."""

    def __init__(self) -> None:
        # Deliberately skip super().__init__——we don't need real state.
        pass


# ============================================================================
# 26-31: build_adapter
# ============================================================================


async def test_26_reads_secret_exactly_once() -> None:
    runtime, _, cred, _ = _make_runtime()
    adapter = await runtime.build_adapter(_make_selection())
    assert adapter is not None
    assert cred.resolve_calls == 1


async def test_27_calls_factory_exactly_once() -> None:
    runtime, _, _, factory_calls = _make_runtime()
    await runtime.build_adapter(_make_selection())
    assert len(factory_calls) == 1


async def test_28_passes_correct_provider_definition() -> None:
    runtime, registry, _, factory_calls = _make_runtime()
    await runtime.build_adapter(_make_selection(provider_id="qwen"))
    passed_def = factory_calls[0]["provider_definition"]
    assert passed_def is registry.get("qwen")


async def test_29_passes_correct_model_id() -> None:
    runtime, _, _, factory_calls = _make_runtime()
    await runtime.build_adapter(_make_selection(model_id="qwen-turbo"))
    assert factory_calls[0]["model_id"] == "qwen-turbo"


async def test_30_passes_raw_secret() -> None:
    runtime, _, cred, factory_calls = _make_runtime(cred=FakeCredService(secret="RAW-SECRET-VALUE"))
    await runtime.build_adapter(_make_selection())
    assert factory_calls[0]["api_key"] == "RAW-SECRET-VALUE"


async def test_31_factory_failure_mapped_to_initialization_error() -> None:
    def failing_factory(**_: Any) -> Any:
        raise ProviderConfigError("some factory error")

    runtime, _, _, _ = _make_runtime(factory=failing_factory)
    with pytest.raises(ProviderInitializationError) as exc_info:
        await runtime.build_adapter(_make_selection())
    assert str(exc_info.value) == "session provider initialization failed"
    assert exc_info.value.__cause__ is None


# ============================================================================
# 32: Secret read failure does not invoke Factory
# ============================================================================


async def test_32_secret_failure_skips_factory() -> None:
    factory_calls: list[dict[str, Any]] = []

    def factory(**_: Any) -> Any:
        factory_calls.append({"called": True})
        raise AssertionError("Factory must not be called")

    runtime, _, _, _ = _make_runtime(
        cred=FakeCredService(secret="RAISE_UNAVAILABLE"),
        factory=factory,
    )
    with pytest.raises(ProviderSelectionUnavailableError):
        await runtime.build_adapter(_make_selection())
    assert factory_calls == []


# ============================================================================
# 33-35: No Profile re-read / No Binding mutation / No Adapter cache
# ============================================================================


async def test_33_does_not_re_read_profile() -> None:
    """build_adapter receives ``selection`` only——no Profile access path."""
    profile_reads: list[str] = []

    class _ProfileReadTrackingConfigService(_NoOpConfigService):
        async def get_profile(self, profile_id: str) -> Any:
            profile_reads.append(profile_id)
            raise AssertionError("build_adapter must not call get_profile")

    runtime, _, _, _ = _make_runtime()
    runtime._provider_config_service = _ProfileReadTrackingConfigService()  # type: ignore[attr-defined]
    await runtime.build_adapter(_make_selection())
    assert profile_reads == []


async def test_34_does_not_modify_binding() -> None:
    """Runtime has no Binding mutator access——verify via fake service."""
    binding_calls: list[str] = []

    class _BindingTrackingConfigService(_NoOpConfigService):
        async def set_session_binding(self, **kwargs: Any) -> Any:
            binding_calls.append("set")
            raise AssertionError("build_adapter must not write binding")

        async def upsert_binding(self, **kwargs: Any) -> Any:
            binding_calls.append("upsert")
            raise AssertionError("build_adapter must not write binding")

    runtime, _, _, _ = _make_runtime()
    runtime._provider_config_service = _BindingTrackingConfigService()  # type: ignore[attr-defined]
    await runtime.build_adapter(_make_selection())
    assert binding_calls == []


async def test_35_does_not_cache_adapter() -> None:
    runtime, _, cred, _ = _make_runtime()
    a1 = await runtime.build_adapter(_make_selection())
    a2 = await runtime.build_adapter(_make_selection())
    assert a1 is not a2
    assert cred.resolve_calls == 2


# ============================================================================
# 36-37: bind_to_harness None path
# ============================================================================


def _make_harness(client: ModelClient | None = None) -> tuple[
    AgentHarness, _CloseTrackingClient, _CloseTrackingAdapter
]:
    adapter = _CloseTrackingAdapter(provider_id="legacy", model="legacy-model")
    original_client = _CloseTrackingClient(adapter)
    agent = Agent(system_prompt="test", client=client or original_client)
    return AgentHarness(agent), original_client, adapter


async def test_36_none_selection_does_not_replace_client() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()
    async with runtime.bind_to_harness(harness=harness, selection=None) as yielded:
        assert yielded is None
        assert harness.agent.client is original_client


async def test_37_none_selection_does_not_read_secret() -> None:
    runtime, _, cred, _ = _make_runtime()
    harness, _, _ = _make_harness()
    async with runtime.bind_to_harness(harness=harness, selection=None):
        pass
    assert cred.resolve_calls == 0


# ============================================================================
# 38-42: bind_to_harness non-None path
# ============================================================================


async def test_38_replaces_client_during_context() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ) as yielded:
        assert yielded is not None
        assert harness.agent.client is not original_client
        assert isinstance(harness.agent.client, ModelClient)


async def test_39_yield_body_sees_request_client() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()
    seen: list[ModelClient] = []
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        seen.append(harness.agent.client)
    assert len(seen) == 1
    assert seen[0] is not original_client


async def test_40_restores_original_client_on_exit() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    assert harness.agent.client is original_client


async def test_41_request_client_closed_on_exit() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, _, _ = _make_harness()
    captured_adapters: list[_CloseTrackingAdapter] = []

    def tracking_factory(
        *, provider_definition: ProviderDefinition, api_key: str, model_id: str
    ) -> _CloseTrackingAdapter:
        a = _CloseTrackingAdapter(
            provider_id=provider_definition.id, model=model_id
        )
        captured_adapters.append(a)
        return a

    runtime._provider_factory = tracking_factory  # type: ignore[assignment]
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    assert captured_adapters
    # aclose called at least once on each adapter
    assert all(a.close_calls >= 1 for a in captured_adapters)


async def test_42_original_client_not_closed() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, original_adapter = _make_harness()
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    # Original client + its adapter untouched
    assert original_client.close_calls == 0
    assert original_adapter.close_calls == 0


# ============================================================================
# 43-46: Error / cancellation paths
# ============================================================================


async def test_43_body_exception_still_restores_client() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()

    class _BodyError(Exception):
        pass

    with pytest.raises(_BodyError):
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection()
        ):
            raise _BodyError("simulated body failure")
    assert harness.agent.client is original_client


async def test_44_body_cancellation_still_restores_client() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, original_client, _ = _make_harness()

    async def _cancel_inside() -> None:
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection()
        ):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _cancel_inside()
    assert harness.agent.client is original_client


async def test_45_cancellation_request_client_eventually_closed() -> None:
    runtime, _, _, _ = _make_runtime()
    harness, _, _ = _make_harness()
    captured_adapters: list[_CloseTrackingAdapter] = []

    def tracking_factory(
        *, provider_definition: ProviderDefinition, api_key: str, model_id: str
    ) -> _CloseTrackingAdapter:
        a = _CloseTrackingAdapter(
            provider_id=provider_definition.id, model=model_id
        )
        captured_adapters.append(a)
        return a

    runtime._provider_factory = tracking_factory  # type: ignore[assignment]

    async def _cancel_inside() -> None:
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection()
        ):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _cancel_inside()
    # Cancellation-safe close ensures request adapter was closed
    assert captured_adapters
    assert all(a.close_calls >= 1 for a in captured_adapters)


async def test_46_build_failure_original_client_not_replaced() -> None:
    """If build_adapter raises, original client stays bound—no half-state."""
    runtime, _, _, _ = _make_runtime()

    def failing_factory(**_: Any) -> Any:
        raise ProviderConfigError("build failed")

    runtime._provider_factory = failing_factory  # type: ignore[assignment]
    harness, original_client, _ = _make_harness()

    with pytest.raises(ProviderInitializationError):
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection()
        ):
            pass
    # Original never replaced
    assert harness.agent.client is original_client


# ============================================================================
# 47-50: Failure isolation between requests
# ============================================================================


async def test_47_close_failure_does_not_leak_secret_in_exception() -> None:
    SECRET_IN_TEST = "sk-SUPER-SECRET-NO-LEAK-CLOSE"

    class _CloseRaisingAdapter(ProviderAdapter):
        def __init__(self) -> None:
            self.provider_id = "qwen"
            self.model = "qwen-plus"

        async def stream(self, request: Any) -> Any:  # pragma: no cover
            yield  # type: ignore[unreachable]

        async def aclose(self) -> None:
            raise RuntimeError(f"simulated close failure key={SECRET_IN_TEST}")

    def factory(**_: Any) -> Any:
        return _CloseRaisingAdapter()

    runtime, _, cred, _ = _make_runtime(
        cred=FakeCredService(secret=SECRET_IN_TEST),
        factory=factory,
    )
    harness, original_client, _ = _make_harness()
    # close failure is swallowed by ModelClient.close()——no exception escapes,
    # original client is restored, secret never appears in any exception.
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    assert harness.agent.client is original_client


async def test_48_repeat_runtime_calls_create_new_adapter() -> None:
    runtime, _, _, _ = _make_runtime()
    adapters: list[ProviderAdapter] = []
    harness, original_client, _ = _make_harness()

    def capture_factory(**_: Any) -> Any:
        a = _CloseTrackingAdapter(provider_id="qwen", model="qwen-plus")
        adapters.append(a)
        return a

    runtime._provider_factory = capture_factory  # type: ignore[assignment]

    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        pass
    assert len(adapters) == 2
    assert adapters[0] is not adapters[1]


async def test_49_next_request_creates_new_adapter() -> None:
    """Failed first request must not pollute second request's adapter."""
    runtime, _, _, _ = _make_runtime()
    factory_calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append(kwargs)
        return _CloseTrackingAdapter(
            provider_id=kwargs["provider_definition"].id,
            model=kwargs["model_id"],
        )

    runtime._provider_factory = factory  # type: ignore[assignment]
    harness, original_client, _ = _make_harness()

    # First request fails (body raises)
    with pytest.raises(ValueError, match="first-fail"):
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection(model_id="m1")
        ):
            raise ValueError("first-fail")

    # Second request succeeds
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection(model_id="m2")
    ):
        pass

    assert len(factory_calls) == 2
    assert factory_calls[0]["model_id"] == "m1"
    assert factory_calls[1]["model_id"] == "m2"


async def test_50_failed_request_does_not_pollute_next_request() -> None:
    """Even after a build failure, the next successful request runs cleanly."""
    call_count = {"n": 0}

    def flaky_factory(**kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ProviderConfigError("first build fails")
        return _CloseTrackingAdapter(
            provider_id=kwargs["provider_definition"].id,
            model=kwargs["model_id"],
        )

    runtime, _, _, _ = _make_runtime(factory=flaky_factory)
    harness, original_client, _ = _make_harness()

    # First build fails——original client untouched
    with pytest.raises(ProviderInitializationError):
        async with runtime.bind_to_harness(
            harness=harness, selection=_make_selection()
        ):
            pass
    assert harness.agent.client is original_client

    # Second build succeeds
    async with runtime.bind_to_harness(
        harness=harness, selection=_make_selection()
    ):
        assert harness.agent.client is not original_client
    assert harness.agent.client is original_client

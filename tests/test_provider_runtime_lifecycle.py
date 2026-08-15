"""RequestProviderRuntime lifecycle + concurrency tests (M1-4 §十 + §十四).

Covers the cross-request lifecycle invariants not exhaustively checked in the
per-feature files:

- No second active-request lock is introduced
- Concurrent bind_to_harness against the same harness is the caller's job
  (precondition——documented but not enforced by M1-4)
- Runtime has no shared mutable request state——two parallel resolve_selection
  calls return independent snapshots
- Single-instance Runtime services multiple sessions without carryover
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core_py.agent import Agent
from pi_agent_core_py.harness import AgentHarness
from pi_agent_core_py.model_client import ModelClient
from pi_agent_core_py.providers.base import ProviderAdapter
from pi_agent_core_py.providers.registry import (
    ProviderDefinition,
    ProviderRegistry,
)
from pi_agent_core_py.web.providers.runtime import (
    RequestProviderRuntime,
    RequestProviderSelection,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# Minimal fakes
# ============================================================================


class _StaticRegistry(ProviderRegistry):
    def __init__(self) -> None:
        self._defs = {
            pid: ProviderDefinition(
                id=pid,
                display_name=pid,
                api_style="openai_compatible",
                default_base_url=f"https://{pid}.example.com",
                credential_validation_strategy="unsupported",
                credential_validation_endpoint=None,
                supports_model_listing=False,
            )
            for pid in ("qwen", "kimi", "glm")
        }

    def get(self, provider_id: str) -> ProviderDefinition | None:  # type: ignore[override]
        return self._defs.get(provider_id)

    def list(self) -> tuple[ProviderDefinition, ...]:  # type: ignore[override]
        return tuple(self._defs.values())

    def has(self, provider_id: str) -> bool:  # type: ignore[override]
        return provider_id in self._defs


class _NoOpConfigService:
    """Stub——these tests don't go through resolve_selection."""

    pass


class _StubCredentialService:
    """Stub CredentialService——build_adapter calls resolve_secret_for_request."""

    async def resolve_secret_for_request(self, credential_id: str) -> str:
        return "stub-secret"


class _StubAdapter(ProviderAdapter):
    def __init__(self, pid: str, model: str) -> None:
        self.provider_id = pid
        self.model = model

    async def stream(self, request: Any) -> Any:  # pragma: no cover
        yield  # type: ignore[unreachable]


def _make_runtime() -> RequestProviderRuntime:
    def _factory(**kwargs: Any) -> ProviderAdapter:
        return _StubAdapter(kwargs["provider_definition"].id, kwargs["model_id"])

    return RequestProviderRuntime(
        provider_config_service=_NoOpConfigService(),  # type: ignore[arg-type]
        credential_service=_StubCredentialService(),  # type: ignore[arg-type]
        provider_registry=_StaticRegistry(),
        provider_factory=_factory,
    )


def _selection(model: str = "m1") -> RequestProviderSelection:
    return RequestProviderSelection(
        profile_id="prof",
        provider_id="qwen",
        model_id=model,
        selection_source="explicit",
        credential_id="cred",
    )


# ============================================================================
# 1. Runtime has no shared request-level state——parallel selections independent
# ============================================================================


async def test_runtime_has_no_shared_request_state() -> None:
    """Two concurrent build_adapter calls must not interfere."""
    runtime = _make_runtime()
    a1, a2 = await asyncio.gather(
        runtime.build_adapter(_selection("m1")),
        runtime.build_adapter(_selection("m2")),
    )
    assert a1 is not a2
    assert a1.model == "m1"
    assert a2.model == "m2"


# ============================================================================
# 2. No second lock——M1-4 does not serialize concurrent bind_to_harness calls
# ============================================================================


async def test_runtime_does_not_introduce_second_lock() -> None:
    """Two concurrent bind_to_harness calls run in parallel——no internal
    serialization. Caller must use the existing single-active-request lock."""
    runtime = _make_runtime()

    inside: list[int] = []
    max_concurrent = {"n": 0}
    current = {"n": 0}

    async def _hold_and_track() -> None:
        agent = Agent(
            system_prompt="x",
            client=ModelClient(_StubAdapter("legacy", "legacy")),  # type: ignore[arg-type]
        )
        harness = AgentHarness(agent)
        async with runtime.bind_to_harness(harness=harness, selection=_selection()):
            current["n"] += 1
            max_concurrent["n"] = max(max_concurrent["n"], current["n"])
            inside.append(current["n"])
            await asyncio.sleep(0.05)
            current["n"] -= 1

    await asyncio.gather(_hold_and_track(), _hold_and_track())
    # If M1-4 had a lock, max_concurrent would be 1. We allow 2——caller's job.
    assert max_concurrent["n"] == 2


# ============================================================================
# 3. Single runtime instance services multiple sessions without carryover
# ============================================================================


async def test_single_runtime_services_multiple_sessions() -> None:
    """One Runtime instance can be reused across sessions / harnesses."""
    runtime = _make_runtime()

    for i in range(3):
        agent = Agent(
            system_prompt="x",
            client=ModelClient(_StubAdapter("legacy", "legacy")),  # type: ignore[arg-type]
        )
        harness = AgentHarness(agent)
        original = harness.agent.client
        async with runtime.bind_to_harness(
            harness=harness, selection=_selection(f"m{i}")
        ):
            assert harness.agent.client.model == f"m{i}"
            assert harness.agent.client is not original
        # Restored
        assert harness.agent.client is original


# ============================================================================
# 4. CancelledError from body propagates——not swallowed by Runtime
# ============================================================================


async def test_cancelled_error_propagates_from_body() -> None:
    runtime = _make_runtime()
    agent = Agent(
        system_prompt="x",
        client=ModelClient(_StubAdapter("legacy", "legacy")),  # type: ignore[arg-type]
    )
    harness = AgentHarness(agent)
    original = harness.agent.client

    async def _body() -> None:
        async with runtime.bind_to_harness(harness=harness, selection=_selection()):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _body()
    assert harness.agent.client is original

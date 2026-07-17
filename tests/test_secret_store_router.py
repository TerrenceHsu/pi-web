"""SecretStoreRouter tests（P1-E1-3A）.

覆盖：
- 三种 mode 正确路由
- unknown mode 拒绝
- Keyring backend 缺失（None）
- 不自动 fallback
- Router repr 不泄漏 Store 内容
- 必填 mode 缺失时构造失败
- try_resolve 不抛
- 不可 pickle
"""
from __future__ import annotations

import pickle

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InMemorySecretStore,
    SecretStore,
)
from pi_agent_core_py.web.credentials_errors import (
    CredentialBackendUnavailableError,
    CredentialInputError,
)
from pi_agent_core_py.web.secret_store_router import SecretStoreRouter

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def memory_store() -> InMemorySecretStore:
    return InMemorySecretStore()


@pytest.fixture
def env_store() -> EnvSecretStore:
    return EnvSecretStore()


@pytest.fixture
def full_router(
    memory_store: InMemorySecretStore,
    env_store: EnvSecretStore,
) -> SecretStoreRouter:
    """Router with keyring=None（模拟 keyring 不可用）."""
    return SecretStoreRouter(
        stores={
            "keyring": None,
            "session_only": memory_store,
            "env": env_store,
        }
    )


# ============================================================================
# 1. Three modes route correctly
# ============================================================================


class TestRouting:
    def test_resolve_session_only_returns_memory_store(
        self,
        full_router: SecretStoreRouter,
        memory_store: InMemorySecretStore,
    ) -> None:
        resolved = full_router.resolve("session_only")
        assert resolved is memory_store

    def test_resolve_env_returns_env_store(
        self,
        full_router: SecretStoreRouter,
        env_store: EnvSecretStore,
    ) -> None:
        resolved = full_router.resolve("env")
        assert resolved is env_store

    def test_resolve_keyring_returns_store_when_registered(
        self,
        memory_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """Keyring 注册了 fake store（memory 充当）时正常返回."""
        keyring_store = InMemorySecretStore()
        router = SecretStoreRouter(
            stores={
                "keyring": keyring_store,
                "session_only": memory_store,
                "env": env_store,
            }
        )
        assert router.resolve("keyring") is keyring_store

    def test_resolved_store_satisfies_protocol(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        """resolve 返回的对象必须满足 SecretStore Protocol."""
        store = full_router.resolve("session_only")
        assert isinstance(store, SecretStore)


# ============================================================================
# 2. Unknown mode rejected
# ============================================================================


class TestUnknownMode:
    def test_resolve_unknown_mode_raises_input_error(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        with pytest.raises(CredentialInputError, match="unknown storage_mode"):
            full_router.resolve("vault")

    def test_resolve_empty_string_raises_input_error(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        with pytest.raises(CredentialInputError):
            full_router.resolve("")

    def test_try_resolve_unknown_mode_returns_none(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        assert full_router.try_resolve("vault") is None
        assert full_router.try_resolve("") is None


# ============================================================================
# 3. Keyring backend missing
# ============================================================================


class TestKeyringMissing:
    def test_resolve_keyring_raises_backend_unavailable(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        with pytest.raises(
            CredentialBackendUnavailableError,
            match="backend unavailable",
        ):
            full_router.resolve("keyring")

    def test_try_resolve_keyring_returns_none(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        assert full_router.try_resolve("keyring") is None


# ============================================================================
# 4. No automatic fallback
# ============================================================================


class TestNoFallback:
    def test_keyring_does_not_fall_back_to_memory(
        self,
        memory_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """keyring=None 时 resolve('keyring') 不能返回 InMemoryStore."""
        router = SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": memory_store,
                "env": env_store,
            }
        )
        with pytest.raises(CredentialBackendUnavailableError):
            resolved = router.resolve("keyring")
            assert resolved is not memory_store, "must not fall back to memory"
            return resolved

    def test_no_cross_backend_search(
        self,
        memory_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """resolve('env') 不能返回 InMemoryStore（即便 env store 内部为空）."""
        router = SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": memory_store,
                "env": env_store,
            }
        )
        resolved = router.resolve("env")
        assert resolved is env_store
        assert resolved is not memory_store


# ============================================================================
# 5. Router repr doesn't leak Store internals
# ============================================================================


class TestReprSafety:
    def test_repr_does_not_expose_store_objects(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        text = repr(full_router)
        # 不应包含 store 对象的 repr（含内部 dict / lock 等）
        assert "InMemorySecretStore" not in text
        assert "EnvSecretStore" not in text
        assert "_store" not in text
        # 应该列出可用的 storage_mode 名
        assert "session_only" in text
        assert "env" in text
        assert "keyring" in text

    def test_repr_does_not_change_store_state(
        self,
        full_router: SecretStoreRouter,
        memory_store: InMemorySecretStore,
    ) -> None:
        """repr 不应触发 store 状态变化（如 is_available 探测）."""
        # Set some state
        import asyncio

        async def _seed() -> None:
            await memory_store.set("secret-x", "value-y")

        asyncio.run(_seed())

        # Call repr multiple times
        for _ in range(5):
            repr(full_router)

        # State intact
        assert asyncio.run(memory_store.get("secret-x")) == "value-y"


# ============================================================================
# 6. Constructor validation
# ============================================================================


class TestConstructor:
    def test_missing_required_mode_raises(
        self,
        memory_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        with pytest.raises(
            CredentialInputError, match="missing required storage modes"
        ):
            SecretStoreRouter(
                stores={
                    "session_only": memory_store,
                    "env": env_store,
                    # keyring missing
                }
            )

    def test_constructor_does_not_probe_backend(
        self,
        memory_store: InMemorySecretStore,
        env_store: EnvSecretStore,
    ) -> None:
        """构造 Router 不应当调用任何 store 方法（is_available / get 等）.

        用一个 ProbeCounterStore 验证.
        """

        class ProbeCounterStore:
            def __init__(self, wrapped: SecretStore) -> None:
                self._wrapped = wrapped
                self.call_count = 0

            async def is_available(self) -> bool:
                self.call_count += 1
                return await self._wrapped.is_available()  # type: ignore[union-attr]

            async def set(self, ref: str, value: str) -> None:
                await self._wrapped.set(ref, value)  # type: ignore[union-attr]

            async def get(self, ref: str) -> str | None:
                return await self._wrapped.get(ref)  # type: ignore[union-attr]

            async def delete(self, ref: str) -> None:
                await self._wrapped.delete(ref)  # type: ignore[union-attr]

            def backend_name(self) -> str:
                return "probe"

        probe = ProbeCounterStore(memory_store)
        SecretStoreRouter(
            stores={
                "keyring": None,
                "session_only": probe,  # type: ignore[arg-type]
                "env": env_store,
            }
        )
        assert probe.call_count == 0, "Router constructor must not probe backend"


# ============================================================================
# 7. Not picklable
# ============================================================================


class TestPickleSafety:
    def test_router_not_picklable(
        self,
        full_router: SecretStoreRouter,
    ) -> None:
        with pytest.raises(TypeError, match="not picklable"):
            pickle.dumps(full_router)

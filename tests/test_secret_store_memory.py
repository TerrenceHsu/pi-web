"""InMemorySecretStore 单元测试（P1-E1-1）.

覆盖：
- set/get/delete happy path
- 覆盖既有 secret
- delete missing 幂等
- 实例隔离
- 新实例丢失（进程内语义）
- 并发 set/get
- 空引用 / 空 secret 拒绝
- 异常 str() 不泄漏 secret
"""
from __future__ import annotations

import asyncio

import pytest

from pi_agent_core_py.secrets import (
    InMemorySecretStore,
    InvalidSecretReferenceError,
)

# asyncio_mode = "auto"——不需要 pytestmark


async def test_set_and_get_roundtrip() -> None:
    store = InMemorySecretStore()
    await store.set("cred-1", "sk-test-12345")
    assert await store.get("cred-1") == "sk-test-12345"


async def test_set_overwrites_existing() -> None:
    store = InMemorySecretStore()
    await store.set("cred-1", "sk-old-aaaaaaaa")
    await store.set("cred-1", "sk-new-bbbbbbbb")
    assert await store.get("cred-1") == "sk-new-bbbbbbbb"


async def test_get_missing_returns_none() -> None:
    store = InMemorySecretStore()
    assert await store.get("cred-nonexistent") is None


async def test_delete_removes_secret() -> None:
    store = InMemorySecretStore()
    await store.set("cred-1", "sk-test-12345")
    await store.delete("cred-1")
    assert await store.get("cred-1") is None


async def test_delete_missing_is_idempotent() -> None:
    store = InMemorySecretStore()
    # Should not raise
    await store.delete("never-existed")
    await store.delete("never-existed")


async def test_instances_are_isolated() -> None:
    s1 = InMemorySecretStore()
    s2 = InMemorySecretStore()
    await s1.set("cred-1", "sk-test-aaaaaaaa")
    assert await s2.get("cred-1") is None


async def test_new_instance_loses_state() -> None:
    s1 = InMemorySecretStore()
    await s1.set("cred-1", "sk-test-aaaaaaaa")
    s2 = InMemorySecretStore()
    assert await s2.get("cred-1") is None


async def test_concurrent_set_get_no_race() -> None:
    """并发 set/get —— asyncio.Lock 保证一致性."""
    store = InMemorySecretStore()

    async def writer(idx: int) -> None:
        for i in range(50):
            await store.set(f"cred-{idx}", f"sk-test-{idx}-{i}-padding")
            await asyncio.sleep(0)

    async def reader(idx: int) -> None:
        for _ in range(50):
            await store.get(f"cred-{idx}")
            await asyncio.sleep(0)

    await asyncio.gather(*(writer(i) for i in range(5)), *(reader(i) for i in range(5)))

    # 所有 5 个 writer 的 final state 都应该在
    for i in range(5):
        v = await store.get(f"cred-{i}")
        assert v is not None
        assert v.startswith(f"sk-test-{i}-")


@pytest.mark.parametrize("invalid_ref", ["", "   ", "\t\n"])
async def test_empty_or_whitespace_ref_rejected(invalid_ref: str) -> None:
    store = InMemorySecretStore()
    with pytest.raises(InvalidSecretReferenceError):
        await store.set(invalid_ref, "sk-test-aaaaaaaa")


async def test_empty_secret_rejected() -> None:
    store = InMemorySecretStore()
    with pytest.raises(InvalidSecretReferenceError):
        await store.set("cred-1", "")


async def test_non_string_ref_rejected() -> None:
    store = InMemorySecretStore()
    with pytest.raises(InvalidSecretReferenceError):
        await store.set(123, "sk-test-aaaaaaaa")  # type: ignore[arg-type]


async def test_is_available_always_true() -> None:
    store = InMemorySecretStore()
    assert await store.is_available() is True


def test_backend_name() -> None:
    store = InMemorySecretStore()
    assert store.backend_name() == "memory"


async def test_exception_does_not_leak_secret_value() -> None:
    """InvalidSecretReferenceError str/repr 不得包含 secret value."""
    secret = "PI_E1_SECRET_MARKER_7F3A91D2"
    store = InMemorySecretStore()
    try:
        # 用 invalid ref 触发异常——value 是合法 secret
        await store.set("", secret)
    except InvalidSecretReferenceError as e:
        assert secret not in str(e), f"secret leaked in str(): {e}"
        assert secret not in repr(e), f"secret leaked in repr(): {e}"
    else:
        pytest.fail("expected InvalidSecretReferenceError")

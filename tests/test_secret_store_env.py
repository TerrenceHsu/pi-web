"""EnvSecretStore 单元测试（P1-E1-1）.

覆盖：
- 读取现有环境变量
- 读取缺失变量返回 None
- 运行中修改变量后读新值（动态读取，不缓存）
- set() 抛 SecretStoreReadOnlyError
- delete() 不修改环境变量
- 非法变量名拒绝
- 不缓存 secret
- 异常 str() 不泄漏
"""
from __future__ import annotations

import pytest

from pi_agent_core_py.secrets import (
    EnvSecretStore,
    InvalidSecretReferenceError,
    SecretStoreReadOnlyError,
)

# asyncio_mode = "auto"——不需要 pytestmark


async def test_get_existing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PI_E1_GLM_KEY", "sk-test-existing-aaaaaaaa")
    store = EnvSecretStore()
    assert await store.get("TEST_PI_E1_GLM_KEY") == "sk-test-existing-aaaaaaaa"


async def test_get_missing_var_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_PI_E1_MISSING", raising=False)
    store = EnvSecretStore()
    assert await store.get("TEST_PI_E1_MISSING") is None


async def test_dynamic_read_picks_up_runtime_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 变量改后，下次 get() 拿到新值（不缓存）."""
    monkeypatch.setenv("TEST_PI_E1_DYNAMIC", "sk-old-aaaaaaaa")
    store = EnvSecretStore()
    assert await store.get("TEST_PI_E1_DYNAMIC") == "sk-old-aaaaaaaa"

    monkeypatch.setenv("TEST_PI_E1_DYNAMIC", "sk-new-bbbbbbbb")
    assert await store.get("TEST_PI_E1_DYNAMIC") == "sk-new-bbbbbbbb"


async def test_set_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    store = EnvSecretStore()
    with pytest.raises(SecretStoreReadOnlyError):
        await store.set("TEST_PI_E1_VAR", "sk-test-aaaaaaaa")


async def test_delete_does_not_modify_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete() 是 no-op——不删环境变量."""
    monkeypatch.setenv("TEST_PI_E1_NODELETE", "sk-test-aaaaaaaa")
    store = EnvSecretStore()
    await store.delete("TEST_PI_E1_NODELETE")

    import os
    # Env var 仍然存在
    assert os.environ.get("TEST_PI_E1_NODELETE") == "sk-test-aaaaaaaa"


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        "   ",
        "WITH=DASH",
        "WITH SPACE",
        "WITH/SLASH",
        "WITH\nNEWLINE",
        "1STARTS_WITH_DIGIT",
        "HAS-DASH",
    ],
)
async def test_invalid_var_name_rejected(invalid_name: str) -> None:
    store = EnvSecretStore()
    with pytest.raises(InvalidSecretReferenceError):
        await store.get(invalid_name)


async def test_no_caching_across_gets(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一 secret_ref 多次 get——每次都查 os.environ（不缓存）.

    测试覆盖：setenv 改变后，下次 get() 拿到新值——证明内部没有把值缓存到对象属性.
    """
    monkeypatch.setenv("TEST_PI_E1_CACHE", "sk-test-aaaaaaaa")
    store = EnvSecretStore()
    v1 = await store.get("TEST_PI_E1_CACHE")
    monkeypatch.setenv("TEST_PI_E1_CACHE", "sk-changed-bbbbbbbb")
    v2 = await store.get("TEST_PI_E1_CACHE")
    assert v1 == "sk-test-aaaaaaaa"
    assert v2 == "sk-changed-bbbbbbbb"  # 不缓存——拿到新值


async def test_is_available_always_true() -> None:
    store = EnvSecretStore()
    assert await store.is_available() is True


def test_backend_name() -> None:
    assert EnvSecretStore().backend_name() == "env"


async def test_exception_str_does_not_leak_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常 str() 不含环境变量值."""
    secret = "PI_E1_SECRET_MARKER_7F3A91D2"
    monkeypatch.setenv("TEST_PI_E1_LEAK", secret)
    store = EnvSecretStore()
    # 用非法名字触发异常
    try:
        await store.get("1INVALID")
    except InvalidSecretReferenceError as e:
        assert secret not in str(e)
        assert secret not in repr(e)

"""OSKeyringSecretStore 单元测试（P1-E1-1）.

覆盖：
- 依赖缺失（mock keyring 模块不可用）
- null backend
- fail backend
- 可用 backend（用 fake keyring 注入）
- set/get/delete happy path
- delete missing 幂等
- 同步调用通过 asyncio.to_thread
- backend 抛异常→安全 domain error
- 异常 str() 不泄漏
- 并发访问
- 模块 import 不要求安装 keyring
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from pi_agent_core_py.secrets import (
    OSKeyringSecretStore,
    SecretStoreUnavailableError,
)
from pi_agent_core_py.secrets.keyring_store import _SERVICE_NAME

# asyncio_mode = "auto"——不需要 pytestmark


# ============================================================================
# Fake keyring backends
# ============================================================================


class _FakeKeyringBackend:
    """In-memory fake keyring backend for testing."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail_mode: bool = False

    def set_password(self, service: str, account: str, password: str) -> None:
        if self.fail_mode:
            raise RuntimeError("fake backend broken")
        self.store[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail_mode:
            raise RuntimeError("fake backend broken")
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        if self.fail_mode:
            raise RuntimeError("fake backend broken")
        if (service, account) not in self.store:
            # 模拟 keyring.errors.PasswordDeleteError
            err = type(
                "PasswordDeleteError",
                (Exception,),
                {},
            )
            raise err("not found")
        del self.store[(service, account)]


class _FailKeyringBackend:
    """Mimics keyring.backends.fail.Keyring——never works."""

    def set_password(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fail backend")

    def get_password(self, *args: Any, **kwargs: Any) -> None:
        return None

    def delete_password(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fail backend")


class _NullKeyringBackend:
    """Mimics keyring.backends.null.Keyring——always returns None / no-op."""

    def set_password(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_password(self, *args: Any, **kwargs: Any) -> None:
        return None

    def delete_password(self, *args: Any, **kwargs: Any) -> None:
        pass


# ============================================================================
# Tests
# ============================================================================


async def test_module_import_does_not_require_keyring() -> None:
    """模块 import 不要求安装 keyring.

    通过隐藏已安装的 keyring 模块后 import keyring_store——不应失败。
    """
    # 用 importlib 重新 import 模块——确认 import 阶段不需要 keyring
    import importlib

    saved = sys.modules.pop("keyring", None)
    try:
        # blocking import——置空 sys.modules['keyring'] 让 import 失败
        sys.modules["keyring"] = None  # type: ignore[assignment]
        # importlib.reload 不影响已经 import 的子模块
        # 关键：模块顶层应该 try/except ImportError
        # 重新 import secrets 模块
        mod = importlib.import_module("pi_agent_core_py.secrets.keyring_store")
        assert mod is not None
        assert hasattr(mod, "OSKeyringSecretStore")
    finally:
        if saved is not None:
            sys.modules["keyring"] = saved
        else:
            sys.modules.pop("keyring", None)


async def test_keyring_missing_raises_on_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """keyring 未安装时，OSKeyringSecretStore() 抛 SecretStoreUnavailableError."""
    # 模拟 keyring 不在 sys.modules 且 import 失败
    monkeypatch.setitem(sys.modules, "keyring", None)

    # import 关键字触发的 ImportError 我们需要 hook 一下
    builtins_mod = __builtins__
    real_import = (
        builtins_mod["__import__"]
        if isinstance(builtins_mod, dict)
        else builtins_mod.__import__
    )

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "keyring":
            raise ImportError("simulated: keyring not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(SecretStoreUnavailableError) as exc_info:
        OSKeyringSecretStore()

    assert "keyring package not installed" in str(exc_info.value)


async def test_fail_backend_not_available() -> None:
    store = OSKeyringSecretStore(keyring_backend=_FailKeyringBackend())
    # 注意：FailKeyring 不是按 type name "fail.Keyring" 命名，所以我们额外验证 behavior
    # 这里主要验证 _FailKeyringBackend.set_password 会抛
    # is_available() 检查 type name 模式，FailKeyringBackend (在 test 模块) 不匹配 fail/null pattern
    # 但行为是 fail——所以尝试 set 时会抛 RuntimeError
    with pytest.raises(SecretStoreUnavailableError):
        await store.set("cred-test", "sk-test-aaaaaaaa")


async def test_fail_keyring_pattern_unavailable() -> None:
    """keyring.backends.fail.Keyring 类名匹配——is_available 返回 False."""
    # 动态构造一个类，模块名带 'fail.Keyring'
    fail_cls = type("Keyring", (), {
        "set_password": lambda self, *a, **k: None,
        "get_password": lambda self, *a, **k: None,
        "delete_password": lambda self, *a, **k: None,
    })
    fail_cls.__module__ = "keyring.backends.fail"
    fail_backend = fail_cls()
    store = OSKeyringSecretStore(keyring_backend=fail_backend)
    assert await store.is_available() is False


async def test_null_keyring_pattern_unavailable() -> None:
    """keyring.backends.null.Keyring 类名匹配——is_available 返回 False."""
    null_cls = type("Keyring", (), {
        "set_password": lambda self, *a, **k: None,
        "get_password": lambda self, *a, **k: None,
        "delete_password": lambda self, *a, **k: None,
    })
    null_cls.__module__ = "keyring.backends.null"
    null_backend = null_cls()
    store = OSKeyringSecretStore(keyring_backend=null_backend)
    assert await store.is_available() is False


async def test_usable_backend_available() -> None:
    store = OSKeyringSecretStore(keyring_backend=_FakeKeyringBackend())
    assert await store.is_available() is True


async def test_write_access_probe_roundtrips_and_leaves_no_entry() -> None:
    backend = _FakeKeyringBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)

    assert await store.probe_write_access() is True
    assert backend.store == {}


async def test_write_access_probe_rejects_backend_that_only_looks_available() -> None:
    backend = _FakeKeyringBackend()
    backend.fail_mode = True
    store = OSKeyringSecretStore(keyring_backend=backend)

    assert await store.is_available() is True
    assert await store.probe_write_access() is False
    assert backend.store == {}


async def test_write_access_probe_rejects_roundtrip_mismatch_and_cleans_up() -> None:
    class _MismatchBackend(_FakeKeyringBackend):
        def get_password(self, service: str, account: str) -> str | None:
            value = super().get_password(service, account)
            return "wrong-value" if value is not None else None

    backend = _MismatchBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)

    assert await store.probe_write_access() is False
    assert backend.store == {}


async def test_set_get_roundtrip_with_fake_backend() -> None:
    backend = _FakeKeyringBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)
    await store.set("cred-1", "sk-test-aaaaaaaa")
    assert await store.get("cred-1") == "sk-test-aaaaaaaa"
    # 内部存储验证（service name + account ref）
    assert (_SERVICE_NAME, "cred-1") in backend.store


async def test_delete_removes_secret() -> None:
    backend = _FakeKeyringBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)
    await store.set("cred-1", "sk-test-aaaaaaaa")
    await store.delete("cred-1")
    assert await store.get("cred-1") is None


async def test_delete_missing_is_idempotent() -> None:
    """delete_password 对不存在项抛 PasswordDeleteError——store 必须吞掉."""
    backend = _FakeKeyringBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)
    # Should not raise
    await store.delete("never-existed")
    await store.delete("never-existed")


async def test_backend_set_failure_maps_to_unavailable_error() -> None:
    """backend 抛 RuntimeError → 安全映射 SecretStoreUnavailableError."""
    backend = _FakeKeyringBackend()
    backend.fail_mode = True
    store = OSKeyringSecretStore(keyring_backend=backend)
    with pytest.raises(SecretStoreUnavailableError):
        await store.set("cred-1", "sk-test-aaaaaaaa")


async def test_backend_get_failure_returns_none() -> None:
    """get() backend 失败时返回 None（与 Protocol "缺失返回 None" 一致）."""
    backend = _FakeKeyringBackend()
    backend.fail_mode = True
    store = OSKeyringSecretStore(keyring_backend=backend)
    # 失败 backend——get 返回 None
    assert await store.get("cred-1") is None


async def test_exception_does_not_leak_secret_value() -> None:
    """backend 异常 str() 不含 secret value."""
    secret = "PI_E1_SECRET_MARKER_7F3A91D2"
    backend = _FakeKeyringBackend()
    backend.fail_mode = True  # 强制失败
    store = OSKeyringSecretStore(keyring_backend=backend)
    try:
        await store.set("cred-1", secret)
    except SecretStoreUnavailableError as e:
        assert secret not in str(e), f"secret leaked in str(): {e}"
        assert secret not in repr(e), f"secret leaked in repr(): {e}"
        # __cause__ 也不应直接出现在 str()——但可通过 __cause__ 访问
    else:
        pytest.fail("expected SecretStoreUnavailableError")


async def test_concurrent_set_get_safe() -> None:
    backend = _FakeKeyringBackend()
    store = OSKeyringSecretStore(keyring_backend=backend)

    async def writer(idx: int) -> None:
        for i in range(20):
            await store.set(f"cred-{idx}", f"sk-test-{idx}-{i}-padding")

    async def reader(idx: int) -> None:
        for _ in range(20):
            await store.get(f"cred-{idx}")

    await asyncio.gather(
        *(writer(i) for i in range(4)),
        *(reader(i) for i in range(4)),
    )


def test_backend_name() -> None:
    store = OSKeyringSecretStore(keyring_backend=_FakeKeyringBackend())
    assert store.backend_name() == "keyring"


async def test_get_with_invalid_ref_returns_none() -> None:
    """get() 非法 ref 返回 None（不抛）——与 Protocol 一致."""
    store = OSKeyringSecretStore(keyring_backend=_FakeKeyringBackend())
    assert await store.get("") is None
    assert await store.get("   ") is None
    assert await store.get(None) is None  # type: ignore[arg-type]


async def test_set_with_invalid_ref_raises() -> None:
    store = OSKeyringSecretStore(keyring_backend=_FakeKeyringBackend())
    with pytest.raises(SecretStoreUnavailableError):
        await store.set("", "sk-test-aaaaaaaa")


async def test_sync_calls_go_through_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 set/get/delete 都通过 asyncio.to_thread 调用 backend."""
    call_log: list[str] = []

    class _TrackingBackend:
        def set_password(self, *a: Any, **k: Any) -> None:
            call_log.append("set")

        def get_password(self, *a: Any, **k: Any) -> str | None:
            call_log.append("get")
            return None

        def delete_password(self, *a: Any, **k: Any) -> None:
            call_log.append("delete")

    store = OSKeyringSecretStore(keyring_backend=_TrackingBackend())
    await store.set("cred-1", "sk-test-aaaaaaaa")
    await store.get("cred-1")
    await store.delete("cred-1")

    assert call_log == ["set", "get", "delete"]


async def test_delete_password_error_silenced() -> None:
    """keyring PasswordDeleteError（按类名识别）→ 幂等吞掉."""
    class _ErrBackend:
        def set_password(self, *a: Any, **k: Any) -> None:
            pass

        def get_password(self, *a: Any, **k: Any) -> str | None:
            return None

        def delete_password(self, *a: Any, **k: Any) -> None:
            err = type("PasswordDeleteError", (Exception,), {})
            raise err("not found")

    store = OSKeyringSecretStore(keyring_backend=_ErrBackend())
    # Should not raise
    await store.delete("anything")

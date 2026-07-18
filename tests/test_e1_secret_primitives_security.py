"""P1-E1-1 Secret Primitives 安全测试.

用 SECRET_MARKER 验证所有可能泄密出口的 0 命中（**只覆盖 E1-1 范围内的出口**）：
- 异常 str() / repr()
- Keyring backend 异常
- masked value
- ProviderHintResult
- 对象 repr()
- 模块顶层 globals
- 普通 JSON 序列化结果

E1-1 **不**覆盖（留 E1-5）：
- SQLite 数据库
- REST response
- WebSocket event
- Snapshot
- Export Markdown
- Frontend bundle
"""
from __future__ import annotations

import json

import pytest

from pi_agent_core_py import secrets as secrets_mod
from pi_agent_core_py.providers import registry as registry_mod

SECRET_MARKER = "PI_E1_SECRET_MARKER_7F3A91D2"


# ============================================================================
# Helper——遍历 E1-1 涉及的对象并 assert marker 不在内
# ============================================================================


def _assert_no_marker(s: str, *, context: str) -> None:
    assert SECRET_MARKER not in s, (
        f"SECRET_MARKER leaked in {context}: {s!r}"
    )


# ============================================================================
# 1. SecretStore 异常 str / repr
# ============================================================================


class TestSecretStoreExceptions:
    async def test_invalid_ref_error_no_marker(self) -> None:
        from pi_agent_core_py.secrets import InMemorySecretStore, InvalidSecretReferenceError

        store = InMemorySecretStore()
        try:
            await store.set("", SECRET_MARKER)
        except InvalidSecretReferenceError as e:
            _assert_no_marker(str(e), context="InvalidSecretReferenceError str")
            _assert_no_marker(repr(e), context="InvalidSecretReferenceError repr")
            _assert_no_marker(
                str(e.__cause__) if e.__cause__ else "",
                context="InvalidSecretReferenceError __cause__ str",
            )
        else:
            pytest.fail("expected exception")

    async def test_memory_set_value_error_no_marker(self) -> None:
        from pi_agent_core_py.secrets import InMemorySecretStore, InvalidSecretReferenceError

        store = InMemorySecretStore()
        try:
            await store.set("cred-leak", "")
        except InvalidSecretReferenceError as e:
            # 异常对象不该知道我们 tried-to-set value
            _assert_no_marker(str(e), context="memory value error str")

    async def test_env_set_readonly_error_no_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pi_agent_core_py.secrets import EnvSecretStore, SecretStoreReadOnlyError

        monkeypatch.setenv("TEST_PI_E1_LEAK_CHECK", SECRET_MARKER)
        store = EnvSecretStore()
        try:
            await store.set("TEST_PI_E1_LEAK_CHECK", "ignored")
        except SecretStoreReadOnlyError as e:
            _assert_no_marker(str(e), context="env readonly error str")
            _assert_no_marker(repr(e), context="env readonly error repr")
        else:
            pytest.fail("expected SecretStoreReadOnlyError")


# ============================================================================
# 2. Keyring backend 异常映射——原始异常不进 SecretStoreError str
# ============================================================================


class TestKeyringExceptionMapping:
    async def test_backend_failure_no_marker(self) -> None:
        from pi_agent_core_py.secrets import (
            OSKeyringSecretStore,
            SecretStoreUnavailableError,
        )

        class _RaisingBackend:
            def set_password(self, *a: object, **k: object) -> None:
                # 原始异常里塞 marker——必须被吃掉
                raise RuntimeError(f"backend failed with {SECRET_MARKER}")

        store = OSKeyringSecretStore(keyring_backend=_RaisingBackend())
        try:
            await store.set("cred-1", SECRET_MARKER)
        except SecretStoreUnavailableError as e:
            _assert_no_marker(str(e), context="keyring error str")
            _assert_no_marker(repr(e), context="keyring error repr")
            # __cause__ 是原始异常——但其 message 不会出现在 e 的 str() 里
            # 通过 str(e) 不会拿到 cause 的 message
            combined = f"{e!r}{e!s}"
            _assert_no_marker(combined, context="keyring error combined repr+str")


# ============================================================================
# 3. masked value 不含完整 secret
# ============================================================================


class TestMaskedValueSafety:
    def test_masked_does_not_contain_full_secret(self) -> None:
        from pi_agent_core_py.secrets import mask_secret

        masked = mask_secret(SECRET_MARKER)
        _assert_no_marker(masked, context="masked value full secret")
        # 也不应包含中间部分
        assert SECRET_MARKER[4:-4] not in masked

    def test_masked_does_not_leak_in_exception(self) -> None:
        from pi_agent_core_py.secrets import mask_secret

        try:
            mask_secret("")
        except ValueError as e:
            _assert_no_marker(str(e), context="mask error str")


# ============================================================================
# 4. ProviderHintResult 不含 secret
# ============================================================================


class TestProviderHintResultSafety:
    def test_result_str_does_not_leak_secret(self) -> None:
        r = registry_mod.detect_provider_hint(SECRET_MARKER)
        # detect_provider_hint 不存储 secret——str() 安全
        _assert_no_marker(str(r), context="ProviderHintResult str")
        _assert_no_marker(repr(r), context="ProviderHintResult repr")
        # candidates / reason_code / confidence 都不应包含 secret 片段
        for cand in r.candidates:
            _assert_no_marker(cand, context="ProviderHintResult candidate")
        _assert_no_marker(r.reason_code, context="ProviderHintResult reason_code")


# ============================================================================
# 5. 模块顶层 globals 不含 secret
# ============================================================================


class TestModuleGlobalsSafety:
    def test_secrets_module_globals_no_marker(self) -> None:
        """secrets 模块顶层 globals 不应有 marker."""
        for name, value in vars(secrets_mod).items():
            if isinstance(value, str):
                _assert_no_marker(value, context=f"secrets.{name}")

    def test_registry_module_globals_no_marker(self) -> None:
        for name, value in vars(registry_mod).items():
            if isinstance(value, str):
                _assert_no_marker(value, context=f"registry.{name}")


# ============================================================================
# 6. JSON 序列化结果不含 secret
# ============================================================================


class TestJsonSerializationSafety:
    async def test_exception_dict_serializable_no_marker(self) -> None:
        """exception 对象的 __dict__ 不得含 marker."""
        from pi_agent_core_py.secrets import InMemorySecretStore, InvalidSecretReferenceError

        store = InMemorySecretStore()
        try:
            await store.set("", SECRET_MARKER)
        except InvalidSecretReferenceError as e:
            # dataclass-style or plain __dict__
            try:
                payload = json.dumps(e.__dict__, default=str)
            except TypeError:
                payload = str(e.__dict__)
            _assert_no_marker(payload, context="exception __dict__ JSON")

    def test_hint_result_json_serializable_no_marker(self) -> None:
        from dataclasses import asdict

        r = registry_mod.detect_provider_hint(SECRET_MARKER)
        payload = json.dumps(asdict(r))
        _assert_no_marker(payload, context="ProviderHintResult JSON")


# ============================================================================
# 7. 对象 repr() 全覆盖
# ============================================================================


class TestObjectReprSafety:
    def test_secrets_module_repr(self) -> None:
        # module repr 含 module path——不应有 secret
        _assert_no_marker(repr(secrets_mod), context="secrets module repr")

    def test_registry_module_repr(self) -> None:
        _assert_no_marker(repr(registry_mod), context="registry module repr")

    def test_class_repr_no_marker(self) -> None:
        for cls in (
            secrets_mod.InMemorySecretStore,
            secrets_mod.EnvSecretStore,
            secrets_mod.OSKeyringSecretStore,
        ):
            _assert_no_marker(repr(cls), context=f"{cls.__name__} class repr")

    async def test_instance_repr_after_set_no_marker(self) -> None:
        from pi_agent_core_py.secrets import InMemorySecretStore

        store = InMemorySecretStore()
        await store.set("cred-leak-test", SECRET_MARKER)
        # 实例 repr——不应 dump dict 内容
        r = repr(store)
        _assert_no_marker(r, context="InMemorySecretStore instance repr after set")


# ============================================================================
# 8. pytest-friendly summary——marker 全局检查
# ============================================================================


def test_summary_no_marker_in_module_level_strings() -> None:
    """Module-level 字符串字面量（docstrings / 常量）不含 marker.

    这是 broad-net 检查——任何模块顶层定义的字符串都不应包含 marker.
    """
    import pi_agent_core_py.secrets.env as env_mod
    import pi_agent_core_py.secrets.keyring_store as kr_mod
    import pi_agent_core_py.secrets.memory as mem_mod

    for mod in (mem_mod, env_mod, kr_mod, secrets_mod, registry_mod):
        for name in dir(mod):
            value = getattr(mod, name, None)
            if isinstance(value, str) and not name.startswith("__"):
                # 排除我们自己定义的常量名（如果未来有的话）——目前都没有
                _assert_no_marker(value, context=f"{mod.__name__}.{name}")

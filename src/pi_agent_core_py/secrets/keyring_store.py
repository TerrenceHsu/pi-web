"""OSKeyringSecretStore（P1-E1-1）。

OS keyring backend——Windows Credential Manager / macOS Keychain / Linux Secret Service。

**关键安全约束**：
- 延迟导入 `keyring`——模块可 import 不要求安装
- `keyring` 同步调用必须经 `asyncio.to_thread()`——不阻塞事件循环
- `is_available()` 不抛异常——失败/null/fail backend 都返回 False
- **不**永久写入探针 secret——优先用 backend capability check
- 原始 keyring 异常必须映射为安全 domain error（不进 `str()` / `repr()`）

**模块 import 不要求安装 keyring**——只有 `OSKeyringSecretStore()` 实例化时才检查。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from typing import Any
from uuid import uuid4

from .errors import SecretStoreError, SecretStoreUnavailableError

__all__ = ["OSKeyringSecretStore"]


_LOGGER = logging.getLogger(__name__)

# 固定 service name——keyring 按 (service, account) 寻址
# service 是非敏感字符串
_SERVICE_NAME = "pi-agent-core-py"
_WRITE_PROBE_REF_PREFIX = "__pi_agent_keyring_probe__-"


class OSKeyringSecretStore:
    """OS keyring backend.

    所有同步 keyring 调用经 `asyncio.to_thread()`——避免阻塞事件循环。

    `keyring` 在模块顶层 **延迟导入**——只在本类实例化时尝试 import。
    缺失时不影响其它模块加载。
    """

    def __init__(self, *, keyring_backend: Any | None = None) -> None:
        """Initialize.

        Args:
            keyring_backend: 测试注入用 fake backend（生产路径自动探测）；
                None 表示用 keyring.get_keyring() 当前 backend
        """
        self._explicit_backend = keyring_backend
        self._keyring_mod: Any | None = None

        if keyring_backend is None:
            # 延迟 import——不要求模块顶层安装 keyring
            try:
                import keyring
            except ImportError as e:
                raise SecretStoreUnavailableError(
                    "keyring package not installed——install with `pip install keyring>=25` "
                    "or use InMemorySecretStore",
                ) from e
            self._keyring_mod = keyring
        else:
            # 测试路径——直接拿 backend，不依赖 keyring 模块
            self._keyring_mod = None

    # ========================================================================
    # SecretStore Protocol
    # ========================================================================

    async def is_available(self) -> bool:
        """Check backend availability without side effects.

        识别以下情况都返回 False（不抛异常）：
        - keyring 模块缺失
        - fail backend（如 keyring.backends.fail.Keyring）
        - null backend（keyring.backends.null.Keyring）
        - get_keyring() 抛异常
        """
        try:
            backend = await self._get_backend()
        except SecretStoreUnavailableError:
            return False
        except Exception:
            # 任何探测异常都视为不可用——不抛
            return False

        if backend is None:
            return False

        backend_type = type(backend)
        backend_name = backend_type.__module__ + "." + backend_type.__qualname__

        # 已知 fail/null backend 类型——不可用
        if "fail.Keyring" in backend_name or "null.Keyring" in backend_name:
            return False

        return True

    async def probe_write_access(self) -> bool:
        """Verify that the active backend can persist secrets in this process.

        Backend discovery alone is insufficient on Windows: ``WinVaultKeyring``
        can be installed and report a positive priority while ``CredWrite`` still
        fails for a process without a usable interactive logon session.  This
        probe writes a random non-user value under a reserved, unique reference,
        verifies the round trip, and then removes it.

        The method never raises and returns ``True`` only when write, read, and
        cleanup all succeed.  It must be used for startup/readiness checks, not
        per-credential status reads.
        """
        if not await self.is_available():
            return False

        probe_ref = f"{_WRITE_PROBE_REF_PREFIX}{uuid4().hex}"
        probe_value = secrets.token_urlsafe(32)
        roundtrip_ok = False
        cleanup_ok = False

        try:
            await self._call_sync(
                "set_password",
                self._SERVICE_NAME_FOR_CALL(),
                probe_ref,
                probe_value,
            )
            persisted = await self._call_sync(
                "get_password",
                self._SERVICE_NAME_FOR_CALL(),
                probe_ref,
            )
            roundtrip_ok = isinstance(persisted, str) and hmac.compare_digest(
                persisted,
                probe_value,
            )
        except Exception:
            roundtrip_ok = False
        finally:
            # Always attempt cleanup: a backend may persist the value and still
            # raise while returning from set_password/get_password.
            try:
                await self._call_sync(
                    "delete_password",
                    self._SERVICE_NAME_FOR_CALL(),
                    probe_ref,
                )
                cleanup_ok = True
            except Exception:
                cleanup_ok = False

        return roundtrip_ok and cleanup_ok

    async def set(self, secret_ref: str, value: str) -> None:
        if not isinstance(secret_ref, str) or not secret_ref or not secret_ref.strip():
            raise SecretStoreUnavailableError(
                "secret_ref must be a non-empty string",
                secret_ref=None,  # 不传 invalid ref 防泄漏
            )
        if not isinstance(value, str) or not value:
            raise SecretStoreUnavailableError(
                "value must be a non-empty string",
                secret_ref=secret_ref,
            )

        await self._call_sync(
            "set_password",
            self._SERVICE_NAME_FOR_CALL(),
            secret_ref,
            value,
        )

    async def get(self, secret_ref: str) -> str | None:
        if not isinstance(secret_ref, str) or not secret_ref or not secret_ref.strip():
            return None

        try:
            result = await self._call_sync(
                "get_password",
                self._SERVICE_NAME_FOR_CALL(),
                secret_ref,
            )
        except SecretStoreUnavailableError:
            # backend 不可用——返回 None 而非抛（与 Protocol "缺失返回 None" 一致）
            return None
        return result if isinstance(result, str) else None

    async def delete(self, secret_ref: str) -> None:
        if not isinstance(secret_ref, str) or not secret_ref or not secret_ref.strip():
            return

        try:
            await self._call_sync(
                "delete_password",
                self._SERVICE_NAME_FOR_CALL(),
                secret_ref,
            )
        except SecretStoreError as e:
            # PasswordDeleteError 视为幂等成功
            if _is_password_delete_error(e.__cause__):
                return
            # 其他错误——幂等语义下也吞掉（删除不存在的 secret 不抛）
            _LOGGER.debug(
                "keyring delete returned error for secret_ref (idempotent suppression): %s",
                type(e).__name__,
            )
            return
        except Exception as e:
            # 幂等——任何错误都视为已删除
            _LOGGER.debug(
                "keyring delete raised (idempotent suppression): %s",
                type(e).__name__,
            )

    def backend_name(self) -> str:
        return "keyring"

    # ========================================================================
    # Internal helpers
    # ========================================================================

    def _SERVICE_NAME_FOR_CALL(self) -> str:
        """Helper to access _SERVICE_NAME for testing/mocking."""
        return _SERVICE_NAME

    async def _get_backend(self) -> Any:
        """Return the active keyring backend（explicit or module-default）."""
        if self._explicit_backend is not None:
            return self._explicit_backend

        if self._keyring_mod is None:
            raise SecretStoreUnavailableError(
                "keyring module not available",
            )

        return await asyncio.to_thread(self._keyring_mod.get_keyring)

    async def _call_sync(self, method_name: str, *args: Any) -> Any:
        """Call a keyring method via asyncio.to_thread with safe error mapping.

        原始异常通过 __cause__ 链接——但 SecretStoreError 的 str()/repr() 不包含
        原始异常 message。
        """
        backend = await self._get_backend()
        method = getattr(backend, method_name, None)
        if method is None or not callable(method):
            raise SecretStoreUnavailableError(
                f"keyring backend {type(backend).__name__} does not support {method_name}",
                secret_ref=args[1] if len(args) > 1 else None,
            )

        try:
            return await asyncio.to_thread(method, *args)
        except SecretStoreError:
            raise
        except Exception as e:
            # 映射为安全 domain error——原始异常仅 __cause__ 可访问
            raise SecretStoreUnavailableError(
                f"keyring {method_name} failed (error type: {type(e).__name__})",
                secret_ref=args[1] if len(args) > 1 else None,
            ) from e


def _is_password_delete_error(cause: BaseException | None) -> bool:
    """Check if cause is a keyring PasswordDeleteError——by class name to avoid import."""
    if cause is None:
        return False
    return type(cause).__name__ == "PasswordDeleteError"

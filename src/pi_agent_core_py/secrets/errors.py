"""SecretStore domain errors（P1-E1-1）。

**关键安全约束**：所有错误对象的 `str()` / `repr()` 不得包含：

- secret value
- API Key
- Authorization header
- Keyring 返回的原始敏感异常
- 环境变量值

可以包含：

- backend 类型（如 "keyring"）
- 安全错误码（如 "backend_unavailable"）
- secret_ref（其本身不含 Key 片段）

`__cause__` 链接原始异常用于调试，但 `str()` / `repr()` 输出仍要脱敏。
"""
from __future__ import annotations


class SecretStoreError(Exception):
    """All SecretStore errors' base class.

    `secret_ref` 字段供调试用——其本身不含 Key 片段。
    原始异常（如 keyring.PasswordSetError）只能通过 `__cause__` 访问，
    不能出现在 `str()` / `repr()` 中。
    """

    def __init__(self, message: str, *, secret_ref: str | None = None) -> None:
        super().__init__(message)
        self._secret_ref = secret_ref

    @property
    def secret_ref(self) -> str | None:
        """Non-sensitive reference（不含 Key 片段）."""
        return self._secret_ref


class SecretStoreUnavailableError(SecretStoreError):
    """Backend 不可用——例如 keyring 缺失 / D-Bus 失败 / 权限不足。"""


class SecretStoreReadOnlyError(SecretStoreError):
    """Backend 不支持写入——例如 EnvSecretStore。"""


class InvalidSecretReferenceError(SecretStoreError):
    """secret_ref 为空 / 非字符串 / 含非法字符 / 超长。"""


__all__ = [
    "SecretStoreError",
    "SecretStoreUnavailableError",
    "SecretStoreReadOnlyError",
    "InvalidSecretReferenceError",
]

"""EnvSecretStore（P1-E1-1）。

读 `os.environ`——用户在配置时提供环境变量名，应用只保存变量名（不保存值）。

**语义**：
- `secret_ref` = 环境变量名（必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`）
- `is_available()` 总是 True
- `get()` 动态读取——不缓存
- `set()` 抛 `SecretStoreReadOnlyError`
- `delete()` no-op——不修改用户环境变量
"""
from __future__ import annotations

import os
import re

from .errors import InvalidSecretReferenceError, SecretStoreReadOnlyError

__all__ = ["EnvSecretStore"]


# 环境变量名规则：字母/下划线开头，后续字母/数字/下划线
# 与 POSIX 兼容——避免 =、空格、路径分隔、换行
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvSecretStore:
    """Read-only SecretStore backed by `os.environ`."""

    async def is_available(self) -> bool:
        """环境变量永远可读。"""
        return True

    async def set(self, secret_ref: str, value: str) -> None:
        _validate_env_var_name(secret_ref)
        # 应用不应写入用户环境变量
        raise SecretStoreReadOnlyError(
            "EnvSecretStore does not support set()——configure the environment variable directly",
            secret_ref=secret_ref,
        )

    async def get(self, secret_ref: str) -> str | None:
        _validate_env_var_name(secret_ref)
        # 动态读取——不缓存
        return os.environ.get(secret_ref)

    async def delete(self, secret_ref: str) -> None:
        _validate_env_var_name(secret_ref)
        # 不修改用户环境变量——idempotent no-op

    def backend_name(self) -> str:
        return "env"


def _validate_env_var_name(secret_ref: object) -> None:
    """Validate environment variable name."""
    if not isinstance(secret_ref, str):
        raise InvalidSecretReferenceError(
            f"env var name must be a string, got {type(secret_ref).__name__}",
        )
    if not _ENV_VAR_NAME_PATTERN.match(secret_ref):
        raise InvalidSecretReferenceError(
            "env var name must match ^[A-Za-z_][A-Za-z0-9_]*$ "
            "(reject empty / whitespace / '=' / path / newline)",
            secret_ref=secret_ref,
        )

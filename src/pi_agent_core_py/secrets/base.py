"""SecretStore Protocol（P1-E1-1）。

定义 secret 存储的统一契约。所有实现必须满足：

- 异步 API（`async def`）
- `secret_ref` 是非敏感引用（`cred-{uuid}` 或 env var name）
- secret value 永远不进异常 `str()` / `repr()` / 日志
- `get()` 缺失返回 `None`（不抛异常）
- `delete()` 幂等（删除不存在的 secret 不抛异常）
- backend 不可用时 `is_available()` 返回 `False`，不抛异常

`SecretStore` 是 `typing.Protocol`——实现类不需要显式继承。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretStore(Protocol):
    """Secret 存储抽象。

    所有方法都是协程。`secret_ref` 必须是非敏感引用——绝不包含 Key 片段。
    """

    async def is_available(self) -> bool:
        """Return whether the backend can currently be used.

        Returns `False`（不抛异常）当 backend 缺失、失败、或权限不足时。
        """
        ...

    async def set(self, secret_ref: str, value: str) -> None:
        """Store a secret under a non-secret reference.

        Raises:
            InvalidSecretReferenceError: secret_ref 为空 / 非字符串 / 不合法
            SecretStoreReadOnlyError: backend 不支持写入（如 EnvSecretStore）
            SecretStoreUnavailableError: backend 当前不可用
            SecretStoreError: 其它存储失败
        """
        ...

    async def get(self, secret_ref: str) -> str | None:
        """Return the secret, or None when it is not present.

        Never raises for "not found"——返回 None。
        Other failures raise SecretStoreError subclasses.
        """
        ...

    async def delete(self, secret_ref: str) -> None:
        """Delete the secret when supported. Idempotent.

        Deleting a non-existent secret is a no-op（不抛异常）。

        For backends that cannot delete（EnvSecretStore），this is a no-op.
        """
        ...

    def backend_name(self) -> str:
        """Return a stable, non-sensitive backend identifier.

        Used for logging / debugging / serializer `storage_mode` field.
        Examples: "memory" / "env" / "keyring".
        """
        ...


__all__ = ["SecretStore"]

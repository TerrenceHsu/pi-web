"""SecretStoreRouter（P1-E1-3A）.

按 `storage_mode` 把 Credential 路由到对应的 `SecretStore` backend：

```text
storage_mode    →  SecretStore
─────────────────────────────────────
keyring         →  OSKeyringSecretStore | None (backend 不可用)
session_only    →  InMemorySecretStore
env             →  EnvSecretStore
```

**关键不变量**：

1. Router **不在构造期**读秘密 / 探测网络 / 调用 `is_available()`
2. Keyring 不可用（None）**不影响** Router 构造——只在 `resolve()` 时抛
3. **不自动 fallback**：`storage_mode=keyring` 不会偷偷用 InMemorySecretStore
4. **不跨 backend 搜索**：`resolve("keyring")` 不会去 InMemory / Env 找
5. **不记录** secret_ref / secret value——Router 本身不接触 secret
6. Router 的 `repr()` 不暴露 backend 内部状态（只列 storage_mode 名）

Service 层负责：

- 写入前 `resolve()` + `is_available()` 检查
- 读取时 `try_resolve()` → None 表示 backend 不可用 → `storage_status="backend_unavailable"`
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...secrets import SecretStore
from .errors import (
    CredentialBackendUnavailableError,
    CredentialInputError,
)

__all__ = ["SecretStoreRouter"]


class SecretStoreRouter:
    """按 storage_mode 路由到 SecretStore backend.

    Router 是**纯映射**——不持有 secret，不调用 backend 方法（除 Service 显式调）.
    """

    __slots__ = ("_stores",)

    def __init__(
        self,
        stores: Mapping[str, SecretStore | None],
    ) -> None:
        """Initialize router with storage_mode → SecretStore mapping.

        Args:
            stores: 至少包含 'keyring' / 'session_only' / 'env' 三个 key.
                keyring 可为 None（表示 backend 不可用）.

        Raises:
            CredentialInputError: 必填 storage_mode 缺失.
        """
        # 拷贝到内部 dict——隔离 caller 后续修改
        required_modes = ("keyring", "session_only", "env")
        missing = [m for m in required_modes if m not in stores]
        if missing:
            raise CredentialInputError(
                f"SecretStoreRouter missing required storage modes: {missing}"
            )
        self._stores: dict[str, SecretStore | None] = dict(stores)

    def resolve(self, storage_mode: str) -> SecretStore:
        """Return the SecretStore for storage_mode.

        Raises:
            CredentialInputError: unknown storage_mode.
            CredentialBackendUnavailableError: backend 未注册（keyring=None）.

        Notes:
            **不**自动 fallback——keyring 不可用时不会返回 InMemory / Env.
            **不**检查 `is_available()`——Service 调用方负责写前检查.
        """
        if storage_mode not in self._stores:
            raise CredentialInputError(
                f"unknown storage_mode: {storage_mode!r}"
            )
        store = self._stores[storage_mode]
        if store is None:
            raise CredentialBackendUnavailableError(
                f"backend unavailable for storage_mode={storage_mode!r}"
            )
        return store

    def try_resolve(self, storage_mode: str) -> SecretStore | None:
        """Return store or None（不抛）.

        用于读路径（get / list）——backend 不可用时返回 None，
        Service 把 storage_status 设为 "backend_unavailable".

        Notes:
            unknown storage_mode 也返回 None——Service 不应当收到未知 mode
            （schema CHECK 在 DB 层就拦），但保守起见统一返回 None.
        """
        return self._stores.get(storage_mode)

    def __repr__(self) -> str:
        """Safe repr——不暴露 backend 内部状态.

        只列出已注册的 storage_mode 名（keyring / session_only / env），
        不输出 store 对象（store 的 repr 可能含内部状态）.
        """
        modes = sorted(m for m, s in self._stores.items() if s is not None)
        unavailable = sorted(m for m, s in self._stores.items() if s is None)
        return (
            f"SecretStoreRouter(available_modes={modes!r}, "
            f"unavailable_modes={unavailable!r})"
        )

    def __reduce__(self) -> Any:
        """Disable pickling——prevent accidental secret leakage via persistence."""
        raise TypeError("SecretStoreRouter is not picklable")

"""InMemorySecretStore（P1-E1-1）。

进程内 dict 实现——用于：
- 单元测试
- CI / E2E（通过 `PI_AGENT_SECRET_BACKEND=memory` 强制）
- session_only 模式（用户不想持久化 Key）

**关键特性**：
- 实例级隔离——不同实例不共享
- 重启后丢失（进程结束即清空）
- `get()` 缺失返回 `None`
- `delete()` 幂等
- `asyncio.Lock` 保护写入——并发安全
- 不暴露底层 dict（防误用）
"""
from __future__ import annotations

import asyncio

from .errors import InvalidSecretReferenceError

__all__ = ["InMemorySecretStore"]


class InMemorySecretStore:
    """进程内 SecretStore 实现。"""

    def __init__(self) -> None:
        # 实例级 dict——不同实例隔离；用 leading underscore 防外部访问
        self._store: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def is_available(self) -> bool:
        """Memory backend 永远可用（无外部依赖）。"""
        return True

    async def set(self, secret_ref: str, value: str) -> None:
        _validate_secret_ref(secret_ref)
        if not isinstance(value, str) or not value:
            raise InvalidSecretReferenceError(
                "value must be a non-empty string",
                secret_ref=secret_ref,
            )

        async with self._lock:
            self._store[secret_ref] = value

    async def get(self, secret_ref: str) -> str | None:
        _validate_secret_ref(secret_ref)
        # 读无需持锁——Python dict 读是原子的；写有 lock 保证 happens-before
        return self._store.get(secret_ref)

    async def delete(self, secret_ref: str) -> None:
        _validate_secret_ref(secret_ref)
        async with self._lock:
            # 幂等——pop(key, None) 不抛异常
            self._store.pop(secret_ref, None)

    def backend_name(self) -> str:
        return "memory"


# ============================================================================
# Internal helpers
# ============================================================================


def _validate_secret_ref(secret_ref: object) -> None:
    """Validate non-empty string reference. Reject empty / whitespace."""
    if not isinstance(secret_ref, str):
        raise InvalidSecretReferenceError(
            f"secret_ref must be a string, got {type(secret_ref).__name__}",
        )
    if not secret_ref or not secret_ref.strip():
        raise InvalidSecretReferenceError(
            "secret_ref must be non-empty and non-whitespace",
        )

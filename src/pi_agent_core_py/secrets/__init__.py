"""SecretStore 子包（P1-E1-1）。

提供 API Key 等敏感凭证的安全存储抽象：

```text
SecretStore (Protocol)
├── InMemorySecretStore    # 进程内 dict；测试 / CI / E2E / session_only
├── EnvSecretStore         # 读 os.environ；只读
└── OSKeyringSecretStore   # OS keyring（Windows Credential Manager 等）
```

**安全契约（绝对不可破坏）**：

1. 实现不得把 secret value 放进异常 `str()` / `repr()`
2. 实现不得把 secret value 写进日志
3. 实现不得把 secret value 缓存到对象属性（除非 store 本身就是内存 dict）
4. `secret_ref` 是非敏感引用（`cred-{uuid}` 或 env var name），不含 Key 片段
5. `OSKeyringSecretStore` 永远不 fallback 到 SQLite 明文

P1-E1-1 只实现 primitives——不含 SQLite repository、API、validation。
"""
from __future__ import annotations

from .base import SecretStore
from .env import EnvSecretStore
from .errors import (
    InvalidSecretReferenceError,
    SecretStoreError,
    SecretStoreReadOnlyError,
    SecretStoreUnavailableError,
)
from .keyring_store import OSKeyringSecretStore
from .memory import InMemorySecretStore
from .utils import fingerprint_secret, mask_secret

__all__ = [
    # Protocol
    "SecretStore",
    # Implementations
    "InMemorySecretStore",
    "EnvSecretStore",
    "OSKeyringSecretStore",
    # Errors
    "SecretStoreError",
    "SecretStoreUnavailableError",
    "SecretStoreReadOnlyError",
    "InvalidSecretReferenceError",
    # Utilities
    "mask_secret",
    "fingerprint_secret",
]

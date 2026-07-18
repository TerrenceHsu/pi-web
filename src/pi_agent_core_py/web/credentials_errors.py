"""CredentialService 层错误（P1-E1-3A）.

定义 Service 层向调用方（未来的 Web API）抛出的错误类型。所有错误对象的
`str()` / `repr()` **不得**包含：

- secret value / API Key
- secret_ref（虽然本身不含 Key 片段，但属于内部状态）
- fingerprint_sha256
- masked_value
- 原始 SecretStore 异常 message
- SQLite row / SQL 参数

可以包含：

- credential_id（safe）
- storage_mode（safe 枚举）
- 安全错误码（如 "backend_unavailable"）
- 异常类型名

原始异常通过 `__cause__` 链接——`str()` / `repr()` 仍脱敏.
"""
from __future__ import annotations


class CredentialServiceError(Exception):
    """Base class for CredentialService errors."""


class CredentialInputError(CredentialServiceError):
    """Invalid input——bad storage_mode / 缺 secret_value / 缺 env_var_name /
    非法 env var name / 空白 label / 试图通过 rotate 改 storage_mode 等.
    """


class CredentialBackendUnavailableError(CredentialServiceError):
    """目标 backend 不可用——例如 keyring 没安装 / fail backend / 未注册.

    Service 在 create / rotate / delete 写入前发现 backend 不可用时抛.
    Get / list 不抛——返回 storage_status="backend_unavailable".
    """


class CredentialSecretMissingError(CredentialServiceError):
    """Secret 预期存在但找不到（保留——E1-3A 暂不抛，给 E1-3B validation 用）.
    """


class CredentialOperationConflictError(CredentialServiceError):
    """CAS 冲突——credential 已被并发 rotate / delete，调用方读到的快照已过期.

    Service 在 CAS 失败时把 Repository 的 ConcurrentModificationError
    包装成本错误抛出.
    """


class CredentialSecretWriteError(CredentialServiceError):
    """SecretStore.set 失败——未写数据库，无需补偿.
    """


class CredentialSecretDeleteError(CredentialServiceError):
    """SecretStore.delete 在 delete 流程中失败——保留数据库 row.

    此时 secret 可能仍存在或部分存在；调用方应认为 delete 未完成.
    """


class CredentialCompensationError(CredentialServiceError):
    """补偿事务中部分环节失败.

    Variants:
    - create: Repository.create 失败后 secret cleanup 成功/失败
    - rotate: Repository CAS 失败后 new secret cleanup 成功/失败
    - delete: secret 已删但 Repository.delete 失败（row 保留 → needs_key）

    `cleanup_succeeded` 标识补偿是否完整——用于运维诊断.
    """

    def __init__(
        self,
        message: str,
        *,
        cleanup_succeeded: bool = True,
    ) -> None:
        super().__init__(message)
        self._cleanup_succeeded = cleanup_succeeded

    @property
    def cleanup_succeeded(self) -> bool:
        """补偿清理是否成功完成（仅诊断用）."""
        return self._cleanup_succeeded


__all__ = [
    "CredentialServiceError",
    "CredentialInputError",
    "CredentialBackendUnavailableError",
    "CredentialSecretMissingError",
    "CredentialOperationConflictError",
    "CredentialSecretWriteError",
    "CredentialSecretDeleteError",
    "CredentialCompensationError",
]

"""CredentialService（P1-E1-3A）.

编排 `SQLiteCredentialStore`（Repository）+ `SecretStoreRouter` 的应用层服务.
**不**实现远端 Provider 验证（留 E1-3B）、Web API（留 E1-4）、前端（留后续）.

**职责**：

- `create`     : 写 Secret + DB row；DB 失败 → 补偿删 Secret
- `update_label`: 仅 DB label
- `rotate`     : 写新 Secret → CAS 切换 DB ref；失败 → 清理新 Secret；
                 成功 → best-effort 删旧 Secret
- `delete`     : 删 Secret → CAS 删 DB row；Secret 失败 → 保留 row；
                 DB 失败 → row 留存，state 变为 `needs_key`
- `get`/`list` : 读 DB row + 运行时算 storage_status，返回安全 `CredentialView`

**安全契约**（绝对不可破坏）：

1. 异常 `str()` / `repr()` 不得含 secret / secret_ref / fingerprint / masked_value
2. `CredentialView` 不暴露 `secret_ref` / `fingerprint_sha256`
3. Service 层 `delete` 始终传 `expected_secret_ref`——CAS 必备
4. **不**自动 fallback storage_mode（keyring 不可用不会偷换 session_only）
5. **不**在 rotate 中改变 storage_mode（跨 backend 迁移留到未来）
6. Env 模式：`secret_ref = env_var_name`；create/rotate 不调 set；delete 不修环境变量；
   不读取并固化 env value 的 masked/fingerprint（环境变量可在进程外变化）
7. 旧 Secret 清理失败不回滚数据库（另一请求可能已开始用新引用）
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..providers.registry import detect_provider_hint
from ..secrets import (
    SecretStore,
    SecretStoreError,
    fingerprint_secret,
    mask_secret,
)
from ..secrets.errors import SecretStoreUnavailableError
from .credentials_errors import (
    CredentialBackendUnavailableError,
    CredentialCompensationError,
    CredentialInputError,
    CredentialOperationConflictError,
    CredentialSecretDeleteError,
    CredentialSecretWriteError,
    CredentialServiceError,
)
from .credentials_store import (
    CredentialConcurrentModificationError,
    CredentialNotFoundError,
    CredentialRecord,
    CredentialStorageMode,
    CredentialStorageStatus,
    CredentialStoreError,
    CredentialValidationStatus,
    ProviderHintConfidence,
    SQLiteCredentialStore,
    resolve_storage_status,
)
from .secret_store_router import SecretStoreRouter

__all__ = [
    # Commands
    "CreateCredentialCommand",
    "RotateCredentialCommand",
    # Results
    "CredentialOperationResult",
    "CredentialDeleteResult",
    "CredentialView",
    # Warnings
    "CredentialServiceWarning",
    # Service
    "CredentialService",
]


# ============================================================================
# Warnings
# ============================================================================


CredentialServiceWarning = Literal[
    "old_secret_cleanup_failed",
    "rollback_secret_cleanup_failed",
]


# ============================================================================
# Commands
# ============================================================================


@dataclass(frozen=True)
class CreateCredentialCommand:
    """Service-level create input——不和 REST DTO 耦合.

    Args:
        label: 用户可读 label，trim 后非空.
        storage_mode: 'keyring' / 'session_only' / 'env'.
        secret_value: keyring/session_only 必填；env 禁止.
        env_var_name: env 必填；keyring/session_only 禁止.
    """

    label: str
    storage_mode: CredentialStorageMode
    secret_value: str | None = None
    env_var_name: str | None = None


@dataclass(frozen=True)
class RotateCredentialCommand:
    """Service-level rotate input.

    根据**现有 Credential 的 storage_mode** 解释输入：

    - keyring/session_only → 用 `secret_value`
    - env → 用 `env_var_name`

    本阶段不允许通过 rotate 改变 storage_mode.
    """

    credential_id: str
    secret_value: str | None = None
    env_var_name: str | None = None


# ============================================================================
# Results
# ============================================================================


@dataclass(frozen=True)
class CredentialOperationResult:
    """Create / rotate / update_label 的返回值.

    `record` 是内部完整记录（含 secret_ref / fingerprint）——仅供 Service
    内部用；面向 API 的 DTO 是 `CredentialView`（E1-4 序列化时过滤）.
    """

    record: CredentialRecord
    warnings: tuple[CredentialServiceWarning, ...] = ()


@dataclass(frozen=True)
class CredentialDeleteResult:
    """Delete 返回值——不返回 record（已删）."""

    credential_id: str
    warnings: tuple[CredentialServiceWarning, ...] = ()


@dataclass(frozen=True)
class CredentialView:
    """Safe DTO for credential data exposed to API serializers.

    不含 `secret_ref` / `fingerprint_sha256`——那些是内部状态.
    不含 Secret 本体——所有 storage_mode 都不缓存 secret value.
    """

    id: str
    label: str
    storage_mode: CredentialStorageMode
    masked_value: str
    provider_hint: str | None
    provider_hint_confidence: ProviderHintConfidence | None
    validation_status: CredentialValidationStatus
    last_validated_provider_id: str | None
    last_validated_at: int | None
    last_error_code: str | None
    created_at: int
    updated_at: int
    storage_status: CredentialStorageStatus

    @classmethod
    def from_record(
        cls,
        record: CredentialRecord,
        storage_status: CredentialStorageStatus,
    ) -> CredentialView:
        return cls(
            id=record.id,
            label=record.label,
            storage_mode=record.storage_mode,
            masked_value=record.masked_value,
            provider_hint=record.provider_hint,
            provider_hint_confidence=record.provider_hint_confidence,
            validation_status=record.validation_status,
            last_validated_provider_id=record.last_validated_provider_id,
            last_validated_at=record.last_validated_at,
            last_error_code=record.last_error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
            storage_status=storage_status,
        )


# ============================================================================
# Constants
# ============================================================================


# EnvSecretStore 同款正则——副本，避免修改 secrets/env.py（不在允许范围）
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STORED_MODES: frozenset[str] = frozenset({"keyring", "session_only"})


# ============================================================================
# Default ID generators
# ============================================================================


def _default_credential_id() -> str:
    """Generate `cred-{urlsafe token}`——不可预测，不基于 secret/label/time."""
    return f"cred-{secrets.token_urlsafe(16)}"


def _default_secret_ref() -> str:
    """Generate `secret-{urlsafe token}`——与 credential_id 独立."""
    return f"secret-{secrets.token_urlsafe(24)}"


# ============================================================================
# CredentialService
# ============================================================================


class CredentialService:
    """编排 Repository + Router 的应用层服务.

    Lifetime：通常一个 Web app 一个 Service 实例；Repository / Router
    共享底层 SQLite 文件与 store backend.
    """

    def __init__(
        self,
        *,
        repository: SQLiteCredentialStore,
        router: SecretStoreRouter,
        credential_id_factory: Callable[[], str] | None = None,
        secret_ref_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._router = router
        self._credential_id_factory = credential_id_factory or _default_credential_id
        self._secret_ref_factory = secret_ref_factory or _default_secret_ref

    # ========================================================================
    # create
    # ========================================================================

    async def create(
        self,
        command: CreateCredentialCommand,
    ) -> CredentialOperationResult:
        """Create credential + secret atomically (with compensation).

        Flow:
            1. Validate input combos per storage_mode
            2. Generate credential_id (+ secret_ref if stored mode)
            3. Compute masked/fingerprint/hint (stored mode only)
            4. SecretStore.set (stored mode only)
            5. Repository.create
            6. On DB failure → best-effort SecretStore.delete

        Raises:
            CredentialInputError: 非法 storage_mode / 缺字段 / 空 label / 非法 env_var_name
            CredentialBackendUnavailableError: backend 缺失或 is_available() False
            CredentialSecretWriteError: SecretStore.set 失败（未写 DB，无补偿）
            CredentialCompensationError: DB 创建失败，补偿清理成功或失败
        """
        label = self._validate_label(command.label)
        credential_id = self._credential_id_factory()

        if command.storage_mode == "env":
            if command.secret_value is not None:
                raise CredentialInputError(
                    "storage_mode='env' forbids secret_value"
                )
            record = self._build_env_record(
                credential_id=credential_id,
                label=label,
                env_var_name=command.env_var_name,
            )
            # Env：不调 set——Repository.create 是唯一外部副作用，无需补偿
            try:
                await self._repository.create(record)
            except CredentialStoreError as e:
                raise self._wrap_repo_error_simple(e, record.id) from e
            return CredentialOperationResult(record=record)

        # stored modes（keyring / session_only）
        if command.storage_mode not in _STORED_MODES:
            raise CredentialInputError(
                f"unknown storage_mode: {command.storage_mode!r}"
            )
        if command.secret_value is None or not command.secret_value.strip():
            raise CredentialInputError(
                f"storage_mode={command.storage_mode!r} requires secret_value"
            )
        if command.env_var_name is not None:
            raise CredentialInputError(
                f"storage_mode={command.storage_mode!r} forbids env_var_name"
            )

        secret_ref = self._secret_ref_factory()
        secret_value = command.secret_value

        record = self._build_stored_record(
            credential_id=credential_id,
            label=label,
            storage_mode=command.storage_mode,
            secret_ref=secret_ref,
            secret_value=secret_value,
        )

        # 写 Secret——失败时不影响 DB
        store = await self._resolve_available_store(command.storage_mode)
        try:
            await store.set(secret_ref, secret_value)
        except SecretStoreError as e:
            raise CredentialSecretWriteError(
                f"failed to store secret "
                f"(storage_mode={command.storage_mode!r}, credential_id={credential_id})"
            ) from e

        # 写 DB——失败则补偿删 Secret
        try:
            await self._repository.create(record)
        except CredentialStoreError as e:
            cleanup_succeeded = await self._best_effort_delete(
                store, secret_ref, kind="new"
            )
            if not cleanup_succeeded:
                raise CredentialCompensationError(
                    f"create failed and secret cleanup also failed "
                    f"(credential_id={record.id}, warning=rollback_secret_cleanup_failed)",
                    cleanup_succeeded=False,
                ) from e
            raise CredentialCompensationError(
                f"create failed; secret cleanup succeeded "
                f"(credential_id={record.id})",
                cleanup_succeeded=True,
            ) from e

        return CredentialOperationResult(record=record)

    # ========================================================================
    # update_label
    # ========================================================================

    async def update_label(
        self,
        credential_id: str,
        label: str,
    ) -> CredentialOperationResult:
        """Update label only——不读 Secret，不重置 validation.

        Raises:
            CredentialInputError: label trim 后为空.
            CredentialNotFoundError: credential_id 不存在.
        """
        validated_label = self._validate_label(label)
        record = await self._repository.update_label(credential_id, validated_label)
        return CredentialOperationResult(record=record)

    # ========================================================================
    # rotate
    # ========================================================================

    async def rotate(
        self,
        command: RotateCredentialCommand,
    ) -> CredentialOperationResult:
        """Rotate secret + DB metadata with CAS.

        Flow (stored modes):
            1. Repository.get(id) → old_record
            2. Generate new_secret_ref + compute new masked/fingerprint/hint
            3. SecretStore.set(new_ref, new_secret)
            4. Repository.replace_secret_metadata(expected=old_ref, new=new_ref)
            5. On CAS conflict → 清理 new_secret；返回 conflict
            6. On other DB failure → 清理 new_secret；抛 compensation
            7. On success → best-effort 删 old_secret；warning 若失败

        Flow (env):
            1. Repository.get(id) → old_record (storage_mode='env')
            2. validate new env_var_name
            3. Repository.replace_secret_metadata(expected=old_name, new=new_name)
            4. No SecretStore interaction

        Raises:
            CredentialInputError: 输入与 storage_mode 不匹配 / 非法 env_var_name
            CredentialNotFoundError: id 不存在
            CredentialBackendUnavailableError: backend 写前不可用
            CredentialSecretWriteError: SecretStore.set 失败
            CredentialOperationConflictError: CAS 冲突
            CredentialCompensationError: DB 失败后补偿
        """
        old_record = await self._repository.get(command.credential_id)
        storage_mode = old_record.storage_mode

        if storage_mode == "env":
            return await self._rotate_env(old_record, command)

        if storage_mode not in _STORED_MODES:
            # Schema 不允许——保守拒绝
            raise CredentialInputError(
                f"unsupported storage_mode for rotate: {storage_mode!r}"
            )

        # stored modes
        if command.env_var_name is not None:
            raise CredentialInputError(
                f"storage_mode={storage_mode!r} rotate forbids env_var_name"
            )
        if command.secret_value is None or not command.secret_value.strip():
            raise CredentialInputError(
                f"storage_mode={storage_mode!r} rotate requires secret_value"
            )

        new_secret_ref = self._secret_ref_factory()
        new_secret_value = command.secret_value

        masked_value = mask_secret(new_secret_value)
        fingerprint = fingerprint_secret(new_secret_value)
        # provider_hint / confidence 不在 rotate 中更新——Repository 的
        # replace_secret_metadata 只换 secret_ref + masked + fingerprint + reset validation.
        # Provider hint 保留 rotate 前的值；若新 secret 指向不同 provider，由 E1-3B
        # validation 流程更新.

        store = await self._resolve_available_store(storage_mode)
        try:
            await store.set(new_secret_ref, new_secret_value)
        except SecretStoreError as e:
            raise CredentialSecretWriteError(
                f"failed to store new secret during rotate "
                f"(credential_id={old_record.id})"
            ) from e

        # CAS replace
        try:
            new_record = await self._repository.replace_secret_metadata(
                old_record.id,
                expected_secret_ref=old_record.secret_ref,
                new_secret_ref=new_secret_ref,
                masked_value=masked_value,
                fingerprint_sha256=fingerprint,
            )
        except CredentialConcurrentModificationError as e:
            # CAS conflict——new_secret 是孤儿，必须清理
            await self._best_effort_delete(store, new_secret_ref, kind="new")
            raise CredentialOperationConflictError(
                f"credential concurrently modified (credential_id={old_record.id})"
            ) from e
        except CredentialStoreError as e:
            cleanup_ok = await self._best_effort_delete(
                store, new_secret_ref, kind="new"
            )
            if not cleanup_ok:
                raise CredentialCompensationError(
                    f"rotate failed and new secret cleanup also failed "
                    f"(credential_id={old_record.id}, "
                    f"warning=rollback_secret_cleanup_failed)",
                    cleanup_succeeded=False,
                ) from e
            raise CredentialCompensationError(
                f"rotate failed; new secret cleanup succeeded "
                f"(credential_id={old_record.id})",
                cleanup_succeeded=True,
            ) from e

        # CAS 成功——best-effort 删旧 Secret（失败仅记录 warning，不回滚）
        warnings: list[CredentialServiceWarning] = []
        old_cleanup_ok = await self._best_effort_delete(
            store, old_record.secret_ref, kind="old"
        )
        if not old_cleanup_ok:
            warnings.append("old_secret_cleanup_failed")

        return CredentialOperationResult(
            record=new_record,
            warnings=tuple(warnings),
        )

    async def _rotate_env(
        self,
        old_record: CredentialRecord,
        command: RotateCredentialCommand,
    ) -> CredentialOperationResult:
        """Env rotate：切换 env_var_name，不读/写环境变量."""
        if command.secret_value is not None:
            raise CredentialInputError(
                "storage_mode='env' rotate forbids secret_value"
            )
        if command.env_var_name is None or not command.env_var_name.strip():
            raise CredentialInputError(
                "storage_mode='env' rotate requires env_var_name"
            )

        new_env_var_name = command.env_var_name.strip()
        self._validate_env_var_name(new_env_var_name)

        masked_value = f"ENV[{new_env_var_name}]"

        try:
            new_record = await self._repository.replace_secret_metadata(
                old_record.id,
                expected_secret_ref=old_record.secret_ref,
                new_secret_ref=new_env_var_name,
                masked_value=masked_value,
                fingerprint_sha256=None,
            )
        except CredentialConcurrentModificationError as e:
            raise CredentialOperationConflictError(
                f"credential concurrently modified (credential_id={old_record.id})"
            ) from e
        except CredentialStoreError as e:
            # env 没有新 secret 写入——无需补偿，直接映射
            raise self._wrap_repo_error_simple(e, old_record.id) from e

        return CredentialOperationResult(record=new_record)

    # ========================================================================
    # delete
    # ========================================================================

    async def delete(self, credential_id: str) -> CredentialDeleteResult:
        """Delete credential with CAS——先删 Secret，再 CAS 删 DB row.

        Raises:
            CredentialNotFoundError: id 不存在
            CredentialBackendUnavailableError: backend 写前不可用
            CredentialSecretDeleteError: SecretStore.delete 失败（row 保留）
            CredentialOperationConflictError: CAS 冲突（row 已被 rotate）
            CredentialCompensationError: Secret 已删但 DB 删除失败（row 留存→needs_key）
        """
        record = await self._repository.get(credential_id)
        storage_mode = record.storage_mode

        # Resolve store——env 永远可用；keyring 可能 None
        store = self._router.resolve(storage_mode)
        if not await store.is_available():
            raise CredentialBackendUnavailableError(
                f"backend not available for delete "
                f"(storage_mode={storage_mode!r}, credential_id={record.id})"
            )

        # Step 1: 删 Secret——失败则保留 DB row
        try:
            await store.delete(record.secret_ref)
        except SecretStoreError as e:
            raise CredentialSecretDeleteError(
                f"failed to delete secret "
                f"(storage_mode={storage_mode!r}, credential_id={record.id})"
            ) from e

        # Step 2: CAS 删 DB row
        try:
            await self._repository.delete(
                record.id,
                expected_secret_ref=record.secret_ref,
            )
        except CredentialNotFoundError:
            # 另一个并发 delete 已赢——row 已删，调用方应视为 not_found.
            # 我们之前已经删了 secret——这是符合预期的（多个 delete 并发，
            # 第一个删 row，后续 delete 仍会读 stale record，store.delete 幂等）.
            raise
        except CredentialConcurrentModificationError as e:
            # stale delete——读取时的 secret_ref 已被并发 rotate 替换.
            # 旧 Secret 已经被删（正确——旧 ref 已不再被 row 引用）.
            # 新 Secret / row 必须保留——不删除新 Secret.
            raise CredentialOperationConflictError(
                f"credential concurrently modified during delete "
                f"(credential_id={record.id})"
            ) from e
        except CredentialStoreError as e:
            # Secret 已删 + DB 删失败 → row 留存，storage_status 将变为 needs_key.
            # 这是可恢复状态（用户重新写入 Secret 即可）.
            raise CredentialCompensationError(
                f"delete failed; secret removed but row remains "
                f"(credential_id={record.id}, state=needs_key)",
                cleanup_succeeded=True,
            ) from e

        return CredentialDeleteResult(credential_id=record.id)

    # ========================================================================
    # get / list
    # ========================================================================

    async def get(self, credential_id: str) -> CredentialView:
        """Return safe CredentialView for credential_id.

        Raises:
            CredentialNotFoundError: id 不存在.
        """
        record = await self._repository.get(credential_id)
        status = await self._compute_storage_status(record)
        return CredentialView.from_record(record, status)

    async def list(
        self,
        *,
        limit: int = 100,
        before_updated_at: int | None = None,
    ) -> list[CredentialView]:
        """List credentials as safe CredentialView.

        每条独立解析 storage_status——不做缓存（E1-4 引入 lifespan 缓存）.
        """
        records = await self._repository.list(
            limit=limit,
            before_updated_at=before_updated_at,
        )
        views: list[CredentialView] = []
        for r in records:
            status = await self._compute_storage_status(r)
            views.append(CredentialView.from_record(r, status))
        return views

    # ========================================================================
    # Internal helpers
    # ========================================================================

    @staticmethod
    def _validate_label(label: str) -> str:
        if not isinstance(label, str) or not label.strip():
            raise CredentialInputError("label must be non-empty and non-whitespace")
        return label.strip()

    @staticmethod
    def _validate_env_var_name(name: str) -> None:
        if not isinstance(name, str) or not _ENV_VAR_NAME_PATTERN.match(name):
            raise CredentialInputError(
                "env_var_name must match ^[A-Za-z_][A-Za-z0-9_]*$"
            )

    async def _resolve_available_store(self, storage_mode: str) -> SecretStore:
        """Resolve store and verify availability——raises if not usable for write."""
        store = self._router.resolve(storage_mode)
        if not await store.is_available():
            raise CredentialBackendUnavailableError(
                f"backend not available "
                f"(storage_mode={storage_mode!r})"
            )
        return store

    async def _compute_storage_status(
        self,
        record: CredentialRecord,
    ) -> CredentialStorageStatus:
        store = self._router.try_resolve(record.storage_mode)
        if store is None:
            return "backend_unavailable"
        return await resolve_storage_status(record, store)

    @staticmethod
    def _build_env_record(
        *,
        credential_id: str,
        label: str,
        env_var_name: str | None,
    ) -> CredentialRecord:
        if env_var_name is None or not env_var_name.strip():
            raise CredentialInputError(
                "storage_mode='env' requires env_var_name"
            )
        name = env_var_name.strip()
        CredentialService._validate_env_var_name(name)
        import time

        now_ms = int(time.time() * 1000)
        return CredentialRecord(
            id=credential_id,
            label=label,
            storage_mode="env",
            secret_ref=name,
            masked_value=f"ENV[{name}]",
            fingerprint_sha256=None,
            provider_hint=None,
            provider_hint_confidence="unknown",
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=now_ms,
            updated_at=now_ms,
        )

    @staticmethod
    def _build_stored_record(
        *,
        credential_id: str,
        label: str,
        storage_mode: str,
        secret_ref: str,
        secret_value: str,
    ) -> CredentialRecord:
        import time

        now_ms = int(time.time() * 1000)
        masked = mask_secret(secret_value)
        fingerprint = fingerprint_secret(secret_value)
        hint = detect_provider_hint(secret_value)
        provider_hint = hint.candidates[0] if hint.candidates else None
        provider_hint_confidence: ProviderHintConfidence | None = hint.confidence
        return CredentialRecord(
            id=credential_id,
            label=label,
            storage_mode=storage_mode,  # type: ignore[arg-type]
            secret_ref=secret_ref,
            masked_value=masked,
            fingerprint_sha256=fingerprint,
            provider_hint=provider_hint,
            provider_hint_confidence=provider_hint_confidence,
            validation_status="never_validated",
            last_validated_provider_id=None,
            last_validated_at=None,
            last_error_code=None,
            created_at=now_ms,
            updated_at=now_ms,
        )

    @staticmethod
    def _wrap_repo_error_simple(
        e: CredentialStoreError,
        credential_id: str,
    ) -> CredentialServiceError:
        """Map Repository errors to safe Service errors (no compensation context).

        用于 env 路径——没写 Secret 无需补偿，所以不用 CredentialCompensationError.
        """
        from .credentials_store import (
            CredentialAlreadyExistsError,
            CredentialSecretRefConflictError,
        )

        if isinstance(e, CredentialAlreadyExistsError):
            return CredentialOperationConflictError(
                f"credential id already exists (credential_id={credential_id})"
            )
        if isinstance(e, CredentialSecretRefConflictError):
            return CredentialOperationConflictError(
                f"secret_ref already in use (credential_id={credential_id})"
            )
        return CredentialServiceError(
            f"repository operation failed (credential_id={credential_id})"
        )

    @staticmethod
    async def _best_effort_delete(
        store: SecretStore,
        secret_ref: str,
        *,
        kind: str,
    ) -> bool:
        """Best-effort delete——never raises. Returns True if delete succeeded.

        Args:
            kind: 'new' / 'old' / 'rollback'——用于日志分类，不进 message.
        """
        try:
            await store.delete(secret_ref)
            return True
        except SecretStoreUnavailableError:
            return False
        except SecretStoreError:
            return False
        except Exception:
            # 任何意外异常都视为清理失败——不让它逃出补偿流程
            return False

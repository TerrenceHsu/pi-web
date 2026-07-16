"""SQLite-backed CredentialRecord repository（P1-E1-2）.

持久化 user credentials 的 metadata——**不**含 API Key 本体。

**职责边界（E1-2）**：
- ✅ `web_credentials_schema_meta` 独立 schema 版本管理（v1）
- ✅ `web_credentials` 表 + CHECK 约束 + 索引
- ✅ Repository primitives（CRUD / label / rotate / validation state）
- ✅ Schema 初始化 / 校验 / 版本检查
- ✅ Restart persistence（重开 connection 后 record 仍在）
- ✅ `resolve_storage_status(record, secret_store)` 运行时派生
- ❌ 不调 SecretStore（补偿事务在 E1-3 service 层做）
- ❌ 不调 LLM / network / validation endpoint
- ❌ 不修改 `extension_store` 的 SCHEMA_VERSION（保持 v2 不变量）
- ❌ 不持久化 `storage_status`（运行时派生）

**安全约束**：
- SQLite 行不得含 api_key / secret / authorization / headers
- `fingerprint_sha256` 仅用于内部去重——索引非 UNIQUE（允许两个 profile 显式共享同一 Key）
- Error 消息可含 credential_id / safe field name / schema version，但**不得**含
  secret / fingerprint / masked value / SQLite row dump / SQL 参数
- `CredentialRecordDecodeError` 不得把整个 row 放进异常
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

from ..secrets import SecretStore

# ============================================================================
# Types
# ============================================================================


CredentialStorageMode = Literal["keyring", "session_only", "env"]

CredentialValidationStatus = Literal[
    "never_validated",
    "valid",
    "invalid",
    "error",
]

CredentialStorageStatus = Literal[
    "ready",
    "needs_key",
    "backend_unavailable",
]

ProviderHintConfidence = Literal["high", "medium", "low", "unknown"]


_VALID_STORAGE_MODES: frozenset[str] = frozenset(
    {"keyring", "session_only", "env"}
)
_VALID_VALIDATION_STATUSES: frozenset[str] = frozenset(
    {"never_validated", "valid", "invalid", "error"}
)
_VALID_HINT_CONFIDENCES: frozenset[str] = frozenset(
    {"high", "medium", "low", "unknown"}
)


# ============================================================================
# CredentialRecord
# ============================================================================


@dataclass(frozen=True)
class CredentialRecord:
    """Persisted credential metadata. **Never** contains the API Key itself."""

    id: str
    label: str

    storage_mode: CredentialStorageMode
    secret_ref: str                        # non-sensitive（cred-{uuid} 或 env var name）

    masked_value: str                      # "sk****8A31" / "********"
    fingerprint_sha256: str | None         # 内部去重——serializer 永不输出

    provider_hint: str | None
    provider_hint_confidence: ProviderHintConfidence | None

    validation_status: CredentialValidationStatus
    last_validated_provider_id: str | None
    last_validated_at: int | None          # ms epoch
    last_error_code: str | None

    created_at: int                        # ms epoch
    updated_at: int                        # ms epoch


# ============================================================================
# Errors
# ============================================================================


class CredentialStoreError(Exception):
    """Base class for credential store errors."""


class CredentialNotFoundError(CredentialStoreError):
    """Raised when get/update/delete targets a non-existent credential_id."""


class CredentialAlreadyExistsError(CredentialStoreError):
    """Raised when create() hits duplicate primary key (credential_id)."""


class CredentialSecretRefConflictError(CredentialStoreError):
    """Raised when create() hits duplicate secret_ref UNIQUE constraint."""


class CredentialsSchemaError(CredentialStoreError):
    """Base class for schema-level errors."""


class CredentialsSchemaVersionError(CredentialsSchemaError):
    """Raised when DB version > supported version（防止 downgrade 损坏）."""


class CredentialsSchemaValidationError(CredentialsSchemaError):
    """Raised when version=1 but expected tables/columns/checks missing."""


class CredentialRecordDecodeError(CredentialStoreError):
    """Raised when a row cannot be decoded to CredentialRecord.

    **关键安全约束**：异常 str/repr **不得**包含整行内容（可能含 fingerprint）。
    只能含 credential_id（如果可读）/ 字段名 / 错误类型。
    """


# ============================================================================
# Schema constants
# ============================================================================


WEB_CREDENTIALS_SCHEMA_VERSION = 1


_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS web_credentials_schema_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
)
"""

_CREDENTIALS_DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_credentials (
        id                          TEXT PRIMARY KEY,
        label                       TEXT NOT NULL,

        storage_mode                TEXT NOT NULL,
        secret_ref                  TEXT NOT NULL UNIQUE,

        masked_value                TEXT NOT NULL,
        fingerprint_sha256          TEXT,

        provider_hint               TEXT,
        provider_hint_confidence    TEXT,

        validation_status           TEXT NOT NULL,
        last_validated_provider_id  TEXT,
        last_validated_at           INTEGER,
        last_error_code             TEXT,

        created_at                  INTEGER NOT NULL,
        updated_at                  INTEGER NOT NULL,

        CHECK (length(trim(label)) > 0),
        CHECK (storage_mode IN ('keyring', 'session_only', 'env')),
        CHECK (validation_status IN
            ('never_validated', 'valid', 'invalid', 'error')),
        CHECK (provider_hint_confidence IS NULL
            OR provider_hint_confidence IN ('high', 'medium', 'low', 'unknown'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_web_credentials_updated_at
    ON web_credentials(updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_web_credentials_fingerprint
    ON web_credentials(fingerprint_sha256)
    """,
)


_SCHEMA_META_KEY = "version"


# ============================================================================
# SQLiteCredentialStore
# ============================================================================


class SQLiteCredentialStore:
    """SQLite-backed CredentialRecord repository.

    与 `extension_store` 共享 connection（`:memory:` 必须），但用独立
    `web_credentials_schema_meta`——不污染 extension_store 的 SCHEMA_VERSION。
    """

    def __init__(
        self,
        db_path: str,
        *,
        connection: aiosqlite.Connection | None = None,
    ) -> None:
        self._db_path = db_path
        self._injected_connection = connection
        self._owns_connection = connection is None
        self._db: aiosqlite.Connection | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise CredentialStoreError("store not initialized or already closed")
        return self._db

    async def init(self) -> None:
        """Open or accept connection + initialize schema. Idempotent.

        初始化顺序（镜像 extension_store）：
            1. open / accept connection（含 PRAGMA）
            2. 只确保 schema_meta 表存在
            3. 读 version
            4. 按 version 分支：
               - None → fresh DB，单 transaction 建 v1 schema
               - 1 → 只 validate，不重建
               - >1 → raise（防 downgrade）
               - 其它 → raise
        """
        if self._db is not None:
            return
        if self._injected_connection is not None:
            self._db = self._injected_connection
            # shared connection——约定所有 store 都用 aiosqlite.Row（idempotent set）
            self._db.row_factory = aiosqlite.Row
        else:
            db_path = self._db_path
            if db_path != ":memory:":
                from pathlib import Path

                parent = Path(db_path).parent
                if str(parent) and not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys=ON")
            await self._db.execute("PRAGMA busy_timeout=5000")

        await self._db.executescript(_SCHEMA_META_DDL)
        await self._db.commit()

        version = await self.get_schema_version()

        if version is None:
            await self._initialize_fresh_v1_schema()
        elif version == WEB_CREDENTIALS_SCHEMA_VERSION:
            await self._validate_v1_schema()
        elif version > WEB_CREDENTIALS_SCHEMA_VERSION:
            raise CredentialsSchemaVersionError(
                "Web credentials database schema is newer than this application "
                f"(got v{version}, supported v{WEB_CREDENTIALS_SCHEMA_VERSION})"
            )
        else:
            raise CredentialsSchemaVersionError(
                f"Unsupported web credentials schema version: {version}"
            )

    async def close(self) -> None:
        """Close the store if it owns its connection."""
        if not self._owns_connection or self._db is None:
            self._closed = True
            self._db = None
            return
        await self._db.close()
        self._db = None
        self._closed = True

    async def get_schema_version(self) -> int | None:
        """Read schema version from meta table. None = fresh DB."""
        db = self._require_db()
        async with db.execute(
            "SELECT value FROM web_credentials_schema_meta WHERE key = ?",
            (_SCHEMA_META_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["value"])

    # ------------------------------------------------------------------
    # schema 初始化 / 校验
    # ------------------------------------------------------------------

    async def _initialize_fresh_v1_schema(self) -> None:
        """Fresh DB：单 BEGIN IMMEDIATE transaction 建 credentials 表 + 索引 + meta row."""
        db = self._require_db()
        try:
            await db.execute("BEGIN IMMEDIATE")
            for stmt in _CREDENTIALS_DDL_STATEMENTS:
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO web_credentials_schema_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_META_KEY, WEB_CREDENTIALS_SCHEMA_VERSION),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _validate_v1_schema(self) -> None:
        """version=1：只读校验表 / 关键列 / 索引存在——**不**静默重建."""
        db = self._require_db()

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='web_credentials'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise CredentialsSchemaValidationError(
                "version=1 but table 'web_credentials' is missing"
            )

        async with db.execute("PRAGMA table_info(web_credentials)") as cursor:
            rows = await cursor.fetchall()
        columns = {r["name"] for r in rows}
        required_columns = {
            "id", "label", "storage_mode", "secret_ref", "masked_value",
            "fingerprint_sha256", "provider_hint", "provider_hint_confidence",
            "validation_status", "last_validated_provider_id", "last_validated_at",
            "last_error_code", "created_at", "updated_at",
        }
        missing = required_columns - columns
        if missing:
            raise CredentialsSchemaValidationError(
                f"version=1 but web_credentials missing columns: {sorted(missing)}"
            )

        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_web_credentials_updated_at', 'idx_web_credentials_fingerprint')"
        ) as cursor:
            index_rows = await cursor.fetchall()
        index_names = {r["name"] for r in index_rows}
        if "idx_web_credentials_updated_at" not in index_names:
            raise CredentialsSchemaValidationError(
                "version=1 but index 'idx_web_credentials_updated_at' is missing"
            )
        if "idx_web_credentials_fingerprint" not in index_names:
            raise CredentialsSchemaValidationError(
                "version=1 but index 'idx_web_credentials_fingerprint' is missing"
            )

    # ------------------------------------------------------------------
    # Repository primitives
    # ------------------------------------------------------------------

    async def create(self, record: CredentialRecord) -> None:
        """Insert a new record. Raises on duplicate id or secret_ref."""
        db = self._require_db()
        _validate_record_fields(record)
        try:
            await db.execute(_INSERT_SQL, _encode_record(record))
            await db.commit()
        except aiosqlite.IntegrityError as e:
            await db.rollback()
            msg = str(e).lower()
            # SQLite message: "UNIQUE constraint failed: web_credentials.id"
            # or "UNIQUE constraint failed: web_credentials.secret_ref"
            if "web_credentials.id" in msg or ".id)" in msg:
                raise CredentialAlreadyExistsError(
                    f"credential id already exists: {record.id}"
                ) from e
            if "web_credentials.secret_ref" in msg or "secret_ref" in msg:
                raise CredentialSecretRefConflictError(
                    f"secret_ref already in use (credential_id={record.id})"
                ) from e
            raise CredentialStoreError(
                f"integrity error during create (credential_id={record.id})"
            ) from e
        except Exception:
            await db.rollback()
            raise

    async def get(self, credential_id: str) -> CredentialRecord:
        """Return record or raise CredentialNotFoundError."""
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_credentials WHERE id = ?",
            (credential_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return _decode_row(row)

    async def list(
        self,
        *,
        limit: int = 100,
        before_updated_at: int | None = None,
    ) -> list[CredentialRecord]:
        """List records ordered by updated_at DESC. Pagination via before_updated_at."""
        db = self._require_db()
        # Clamp limit to [1, 500]
        clamped_limit = max(1, min(500, limit))

        if before_updated_at is None:
            sql = (
                "SELECT * FROM web_credentials "
                "ORDER BY updated_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (clamped_limit,)
        else:
            sql = (
                "SELECT * FROM web_credentials "
                "WHERE updated_at < ? "
                "ORDER BY updated_at DESC LIMIT ?"
            )
            params = (before_updated_at, clamped_limit)

        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_decode_row(row) for row in rows]

    async def update_label(
        self,
        credential_id: str,
        label: str,
    ) -> CredentialRecord:
        """Update label only."""
        if not label or not label.strip():
            raise CredentialStoreError("label must be non-empty and non-whitespace")

        db = self._require_db()
        try:
            async with db.execute(
                "UPDATE web_credentials SET label = ?, updated_at = ? WHERE id = ?",
                (label, _now_ms(), credential_id),
            ) as cursor:
                rowcount = cursor.rowcount
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if rowcount == 0:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return await self.get(credential_id)

    async def replace_secret_metadata(
        self,
        credential_id: str,
        *,
        secret_ref: str,
        masked_value: str,
        fingerprint_sha256: str | None,
    ) -> CredentialRecord:
        """Atomically replace secret_ref + masked + fingerprint + reset validation.

        Used by E1-3 rotate flow. Optimistic concurrency via WHERE id = ? AND secret_ref = ?
        is the service layer's responsibility——this method does a simple atomic replace
        on credential_id, plus validation state reset.
        """
        if not secret_ref or not secret_ref.strip():
            raise CredentialStoreError("secret_ref must be non-empty")
        if not masked_value:
            raise CredentialStoreError("masked_value must be non-empty")

        db = self._require_db()
        try:
            async with db.execute(
                """
                UPDATE web_credentials SET
                    secret_ref = ?,
                    masked_value = ?,
                    fingerprint_sha256 = ?,
                    validation_status = 'never_validated',
                    last_validated_provider_id = NULL,
                    last_validated_at = NULL,
                    last_error_code = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (secret_ref, masked_value, fingerprint_sha256, _now_ms(), credential_id),
            ) as cursor:
                rowcount = cursor.rowcount
            await db.commit()
        except aiosqlite.IntegrityError as e:
            await db.rollback()
            raise CredentialSecretRefConflictError(
                f"secret_ref already in use (credential_id={credential_id})"
            ) from e
        except Exception:
            await db.rollback()
            raise

        if rowcount == 0:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return await self.get(credential_id)

    async def update_validation_state(
        self,
        credential_id: str,
        *,
        validation_status: CredentialValidationStatus,
        provider_id: str | None,
        validated_at: int | None,
        error_code: str | None,
    ) -> CredentialRecord:
        """Update validation state."""
        if validation_status not in _VALID_VALIDATION_STATUSES:
            raise CredentialStoreError(
                f"invalid validation_status: {validation_status}"
            )

        db = self._require_db()
        try:
            async with db.execute(
                """
                UPDATE web_credentials SET
                    validation_status = ?,
                    last_validated_provider_id = ?,
                    last_validated_at = ?,
                    last_error_code = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    validation_status,
                    provider_id,
                    validated_at,
                    error_code,
                    _now_ms(),
                    credential_id,
                ),
            ) as cursor:
                rowcount = cursor.rowcount
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if rowcount == 0:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return await self.get(credential_id)

    async def delete(self, credential_id: str) -> CredentialRecord:
        """Delete record and return the pre-deletion snapshot. Raises if not found.

        **Important**: does NOT call SecretStore.delete——that's the service layer's job.
        """
        db = self._require_db()
        # Read pre-deletion snapshot for caller to do best-effort secret cleanup
        pre_delete = await self.get(credential_id)
        try:
            async with db.execute(
                "DELETE FROM web_credentials WHERE id = ?",
                (credential_id,),
            ) as cursor:
                rowcount = cursor.rowcount
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if rowcount == 0:
            # 不应发生（get 刚刚成功）——但为安全起见
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return pre_delete


# ============================================================================
# Storage Status Resolver（运行时派生）
# ============================================================================


async def resolve_storage_status(
    record: CredentialRecord,
    secret_store: SecretStore,
) -> CredentialStorageStatus:
    """Compute storage status based on current SecretStore state.

    **关键安全约束**：
    - 不持久化此结果
    - 不修改 CredentialRecord
    - 不写日志
    - SecretStore 异常安全映射为 backend_unavailable
    - **不**把 secret 返回给调用方（只返回 status 枚举）

    Returns:
        "ready": secret 可读
        "needs_key": secret_ref 在 backend 中不存在
        "backend_unavailable": SecretStore.is_available() == False 或异常
    """
    try:
        if not await secret_store.is_available():
            return "backend_unavailable"
        # 仅检查存在性——get 返回值丢弃，绝不返回给 caller
        secret = await secret_store.get(record.secret_ref)
    except Exception:
        # 任何 SecretStore 异常 → backend_unavailable（保守）
        return "backend_unavailable"

    if secret is None:
        return "needs_key"
    return "ready"


# ============================================================================
# Internal helpers
# ============================================================================


_INSERT_SQL = """
INSERT INTO web_credentials (
    id, label, storage_mode, secret_ref,
    masked_value, fingerprint_sha256,
    provider_hint, provider_hint_confidence,
    validation_status, last_validated_provider_id,
    last_validated_at, last_error_code,
    created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _now_ms() -> int:
    """Return current time as integer milliseconds."""
    return int(time.time() * 1000)


def _validate_record_fields(record: CredentialRecord) -> None:
    """Validate fields before insert——fail fast with safe error."""
    if not record.id or not record.id.strip():
        raise CredentialStoreError("credential id must be non-empty")
    if not record.label or not record.label.strip():
        raise CredentialStoreError("credential label must be non-empty")
    if record.storage_mode not in _VALID_STORAGE_MODES:
        raise CredentialStoreError(
            f"invalid storage_mode: {record.storage_mode}"
        )
    if not record.secret_ref or not record.secret_ref.strip():
        raise CredentialStoreError("secret_ref must be non-empty")
    if not record.masked_value:
        raise CredentialStoreError("masked_value must be non-empty")
    if record.validation_status not in _VALID_VALIDATION_STATUSES:
        raise CredentialStoreError(
            f"invalid validation_status: {record.validation_status}"
        )
    if (
        record.provider_hint_confidence is not None
        and record.provider_hint_confidence not in _VALID_HINT_CONFIDENCES
    ):
        raise CredentialStoreError(
            f"invalid provider_hint_confidence: {record.provider_hint_confidence}"
        )


def _encode_record(record: CredentialRecord) -> tuple[Any, ...]:
    """Convert record to SQL parameter tuple."""
    return (
        record.id,
        record.label,
        record.storage_mode,
        record.secret_ref,
        record.masked_value,
        record.fingerprint_sha256,
        record.provider_hint,
        record.provider_hint_confidence,
        record.validation_status,
        record.last_validated_provider_id,
        record.last_validated_at,
        record.last_error_code,
        record.created_at,
        record.updated_at,
    )


def _decode_row(row: aiosqlite.Row | dict[str, Any]) -> CredentialRecord:
    """Decode SQLite row → CredentialRecord.

    **关键安全约束**：异常 str/repr 不得包含 row 内容（可能含 fingerprint）.
    """
    try:
        return CredentialRecord(
            id=row["id"],
            label=row["label"],
            storage_mode=row["storage_mode"],
            secret_ref=row["secret_ref"],
            masked_value=row["masked_value"],
            fingerprint_sha256=row["fingerprint_sha256"],
            provider_hint=row["provider_hint"],
            provider_hint_confidence=row["provider_hint_confidence"],
            validation_status=row["validation_status"],
            last_validated_provider_id=row["last_validated_provider_id"],
            last_validated_at=row["last_validated_at"],
            last_error_code=row["last_error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        credential_id = None
        try:
            credential_id = row["id"] if hasattr(row, "__getitem__") else None
        except Exception:
            pass
        # 异常只含 safe context——不含 row dump / 不含 fingerprint
        raise CredentialRecordDecodeError(
            "failed to decode credential row"
            + (f" (credential_id={credential_id})" if credential_id else "")
            + f": {type(e).__name__}"
        ) from e


__all__ = [
    # Types
    "CredentialStorageMode",
    "CredentialValidationStatus",
    "CredentialStorageStatus",
    "ProviderHintConfidence",
    # Dataclass
    "CredentialRecord",
    # Errors
    "CredentialStoreError",
    "CredentialNotFoundError",
    "CredentialAlreadyExistsError",
    "CredentialSecretRefConflictError",
    "CredentialsSchemaError",
    "CredentialsSchemaVersionError",
    "CredentialsSchemaValidationError",
    "CredentialRecordDecodeError",
    # Constants
    "WEB_CREDENTIALS_SCHEMA_VERSION",
    # Repository
    "SQLiteCredentialStore",
    # Resolver
    "resolve_storage_status",
]

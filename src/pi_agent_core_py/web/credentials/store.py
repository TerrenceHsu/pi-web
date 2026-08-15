"""SQLite-backed CredentialRecord repository（P1-E1-2 + E1-2.1）.

持久化 user credentials 的 metadata——**不**含 API Key 本体。

**职责边界（E1-2）**：
- ✅ `web_credentials_schema_meta` 独立 schema 版本管理（v1）
- ✅ `web_credentials` 表 + CHECK 约束 + 索引
- ✅ Repository primitives（CRUD / label / rotate / validation state）
- ✅ Schema 初始化 / 校验 / 版本检查（含 DDL 字符串约束校验，E1-2.1）
- ✅ Restart persistence（重开 connection 后 record 仍在）
- ✅ `resolve_storage_status(record, secret_store)` 运行时派生
- ❌ 不调 SecretStore（补偿事务在 E1-3 service 层做）
- ❌ 不调 LLM / network / validation endpoint
- ❌ 不修改 `extension_store` 的 SCHEMA_VERSION（保持 v2 不变量）
- ❌ 不持久化 `storage_status`（运行时派生）

**事务隔离（E1-2.1）**：
- 生产路径拥有**独立 aiosqlite.Connection**（与 session/extension store 共享数据库
  文件但不同 connection 对象）
- connection 用 `isolation_level=None`（autocommit 模式）
- 每个写方法显式 `BEGIN IMMEDIATE` → SQL → `COMMIT` / `ROLLBACK`
- 这样多个并发协程共享 store 时，每个写操作有明确事务边界，不会互相污染
- injected connection（测试）必须也是 `isolation_level=None`，由测试保证不被
  其他 Repository 并发共享

**CAS（E1-2.1）**：
- `replace_secret_metadata` / `update_validation_state` 必传 `expected_secret_ref`
- `delete` 可选传 `expected_secret_ref`
- WHERE 子句加 `secret_ref = ?`——rowcount=0 时区分 not_found vs concurrent_modification

**安全约束**：
- SQLite 行不得含 api_key / secret / authorization / headers
- `fingerprint_sha256` 仅用于内部去重——索引非 UNIQUE
- Error 消息可含 credential_id / safe field name / schema version，但**不得**含
  secret / fingerprint / masked value / SQLite row dump / SQL 参数 / secret_ref
- `CredentialRecordDecodeError` 不得把整个 row 放进异常
- `CredentialConcurrentModificationError` 不得输出 expected/current secret_ref
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

from ...secrets import SecretStore

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
    """Raised when create()/rotate hits duplicate secret_ref UNIQUE constraint."""


class CredentialConcurrentModificationError(CredentialStoreError):
    """CAS 失败——expected_secret_ref 不匹配（被并发 rotate / delete）.

    **关键安全约束**：异常 str/repr **不得**包含 expected/current secret_ref——
    secret_ref 本身不含 Key 片段但属于内部状态，泄漏会让攻击者推断 CAS 时机。
    只能含 credential_id（safe）和错误类别。
    """


class CredentialsSchemaError(CredentialStoreError):
    """Base class for schema-level errors."""


class CredentialsSchemaVersionError(CredentialsSchemaError):
    """Raised when DB version > supported version（防止 downgrade 损坏）."""


class CredentialsSchemaValidationError(CredentialsSchemaError):
    """Raised when version=1 but expected tables/columns/checks/constraints missing."""


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


# DDL 字符串归一化后的必含片段——_validate_v1_schema 用
_DDL_UNIQUE_PATTERN = r"secret_ref\s+TEXT\s+NOT\s+NULL\s+UNIQUE"
_DDL_LABEL_CHECK_PATTERN = r"length\s*\(\s*trim\s*\(\s*label\s*\)\s*\)\s*>\s*0"
_CONFIDENCE_NULL_OR_PATTERN = (
    r"provider_hint_confidence\s+IS\s+NULL\s+OR\s+provider_hint_confidence\s+IN\s*\("
)


def _normalize_ddl(sql: str) -> str:
    """归一化 DDL 字符串——折叠空白（保留 case，pattern 用 IGNORECASE）."""
    return re.sub(r"\s+", " ", sql)


def _extract_check_enum_values(
    ddl: str,
    column_name: str,
) -> frozenset[str] | None:
    """提取 `<column> IN ( ... )` CHECK 子句中的字符串字面量集合.

    E1-2.2：取代前缀 regex——必须精确比对**完整**枚举集合.

    Returns:
        frozenset[str]: IN 列表里的所有单引号字符串字面量.
        None: DDL 中找不到该列的 `IN (...)` CHECK 子句.

    Notes:
        项目专用——只解析本项目可控的简单 SQLite DDL，不是通用 SQL parser.
        假设 IN 列表里只有单引号字符串字面量（schema v1 DDL 满足该假设）.
    """
    pattern = (
        rf"(?<!\w){re.escape(column_name)}\s+IN\s*\("
        rf"((?:[^()]|\([^()]*\))*)"
        rf"\)"
    )
    match = re.search(pattern, ddl, re.IGNORECASE)
    if match is None:
        return None
    inner = match.group(1)
    return frozenset(re.findall(r"'([^']*)'", inner))


# ============================================================================
# SQLiteCredentialStore
# ============================================================================


class SQLiteCredentialStore:
    """SQLite-backed CredentialRecord repository.

    与 `extension_store` 共享数据库文件但**不**共享 aiosqlite.Connection（E1-2.1）——
    生产路径 `open()` 建立独立 connection + `isolation_level=None`；每个写方法
    用显式 `BEGIN IMMEDIATE / COMMIT / ROLLBACK` 包事务。

    用 `web_credentials_schema_meta` 独立 schema 版本——不污染 extension_store
    的 SCHEMA_VERSION。
    """

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        owns_connection: bool,
    ) -> None:
        """Private constructor——用 `open()` 或 `for_testing()` 工厂方法."""
        self._db = connection
        self._owns_connection = owns_connection
        self._closed = False
        # E1-2.1：单 store 内的写操作序列化锁——独立 connection 场景下足够
        # 保证 BEGIN/SQL/COMMIT 不会在多协程并发时交错
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, db_path: str) -> SQLiteCredentialStore:
        """Production factory——独立 connection with isolation_level=None.

        每个 SQLiteCredentialStore 拥有自己的 aiosqlite.Connection——
        不与 session_store / extension_store 共享 connection 对象。
        多个 store 写同一 DB 文件时由 SQLite 跨连接写锁保证一致性。
        """
        if db_path != ":memory:":
            from pathlib import Path

            parent = Path(db_path).parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(db_path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        store = cls(connection=conn, owns_connection=True)
        await store._initialize_schema()
        return store

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        owns_connection: bool = False,
    ) -> SQLiteCredentialStore:
        """Test factory——注入 connection.

        Args:
            connection: 必须是 isolation_level=None 的 aiosqlite.Connection.
            owns_connection: True 时 close() 会关闭 connection；False 时不关闭.

        Raises:
            CredentialStoreError: connection 不满足 isolation_level=None.
        """
        if getattr(connection, "isolation_level", "") is not None:
            raise CredentialStoreError(
                "Injected connection must have isolation_level=None "
                "(autocommit mode)——E1-2.1 transaction isolation requirement"
            )
        connection.row_factory = aiosqlite.Row
        store = cls(connection=connection, owns_connection=owns_connection)
        await store._initialize_schema()
        return store

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None or self._closed:
            raise CredentialStoreError("store not initialized or already closed")
        return self._db

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

    async def _initialize_schema(self) -> None:
        """Initialize or validate schema. Idempotent.

        初始化顺序（镜像 extension_store）：
            1. 只确保 schema_meta 表存在（独立 executescript）
            2. 读 version
            3. 按 version 分支：
               - None → fresh DB，建 v1 schema
               - 1 → 只 validate，不重建
               - >1 → raise（防 downgrade）
               - 其它 → raise
        """
        db = self._db
        assert db is not None  # _initialize_schema 在 factories 内调用

        # schema_meta 用 CREATE IF NOT EXISTS——autocommit 模式立即生效
        await db.execute(_SCHEMA_META_DDL)

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

    async def _initialize_fresh_v1_schema(self) -> None:
        """Fresh DB：单 BEGIN IMMEDIATE transaction 建 credentials 表 + 索引 + meta row."""
        db = self._db
        assert db is not None
        try:
            await db.execute("BEGIN IMMEDIATE")
            for stmt in _CREDENTIALS_DDL_STATEMENTS:
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO web_credentials_schema_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_META_KEY, WEB_CREDENTIALS_SCHEMA_VERSION),
            )
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def _validate_v1_schema(self) -> None:
        """version=1：只读校验表 / 关键列 / 索引 / DDL CHECK 约束——**不**静默重建."""
        db = self._require_db()

        # 1. Table exists
        async with db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name='web_credentials'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise CredentialsSchemaValidationError(
                "version=1 but table 'web_credentials' is missing"
            )

        # 2. Required columns
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

        # 3. Required indexes
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

        # 4. DDL string contains required CHECK + UNIQUE constraints (E1-2.1)
        #    + 完整枚举值集合精确比对 (E1-2.2)
        ddl_sql = row["sql"] or ""
        normalized = _normalize_ddl(ddl_sql)

        if not re.search(_DDL_UNIQUE_PATTERN, normalized, re.IGNORECASE):
            raise CredentialsSchemaValidationError(
                "version=1 but secret_ref UNIQUE constraint missing from DDL"
            )

        if not re.search(_DDL_LABEL_CHECK_PATTERN, normalized, re.IGNORECASE):
            raise CredentialsSchemaValidationError(
                "version=1 but label non-empty CHECK missing from DDL"
            )

        storage_modes = _extract_check_enum_values(normalized, "storage_mode")
        if storage_modes is None:
            raise CredentialsSchemaValidationError(
                "version=1 but storage_mode CHECK missing from DDL"
            )
        if storage_modes != _VALID_STORAGE_MODES:
            raise CredentialsSchemaValidationError(
                "version=1 but storage_mode CHECK has unexpected enum values"
            )

        statuses = _extract_check_enum_values(normalized, "validation_status")
        if statuses is None:
            raise CredentialsSchemaValidationError(
                "version=1 but validation_status CHECK missing from DDL"
            )
        if statuses != _VALID_VALIDATION_STATUSES:
            raise CredentialsSchemaValidationError(
                "version=1 but validation_status CHECK has unexpected enum values"
            )

        # provider_hint_confidence 必须保留 nullable（IS NULL OR IN）+ 完整枚举
        if not re.search(_CONFIDENCE_NULL_OR_PATTERN, normalized, re.IGNORECASE):
            raise CredentialsSchemaValidationError(
                "version=1 but provider_hint_confidence CHECK missing "
                "IS NULL OR semantics"
            )
        confidences = _extract_check_enum_values(
            normalized, "provider_hint_confidence"
        )
        if confidences is None:
            raise CredentialsSchemaValidationError(
                "version=1 but provider_hint_confidence CHECK missing from DDL"
            )
        if confidences != _VALID_HINT_CONFIDENCES:
            raise CredentialsSchemaValidationError(
                "version=1 but provider_hint_confidence CHECK "
                "has unexpected enum values"
            )

    # ------------------------------------------------------------------
    # Repository primitives
    # ------------------------------------------------------------------

    async def create(self, record: CredentialRecord) -> None:
        """Insert a new record. Raises on duplicate id or secret_ref."""
        db = self._require_db()
        _validate_record_fields(record)
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(_INSERT_SQL, _encode_record(record))
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                await db.execute("ROLLBACK")
                msg = str(e).lower()
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
                await db.execute("ROLLBACK")
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
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "UPDATE web_credentials SET label = ?, updated_at = ? WHERE id = ?",
                    (label, _now_ms(), credential_id),
                ) as cursor:
                    rowcount = cursor.rowcount
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

        if rowcount == 0:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return await self.get(credential_id)

    async def replace_secret_metadata(
        self,
        credential_id: str,
        *,
        expected_secret_ref: str,
        new_secret_ref: str,
        masked_value: str,
        fingerprint_sha256: str | None,
    ) -> CredentialRecord:
        """Atomically replace secret_ref + masked + fingerprint + reset validation.

        CAS via `WHERE id = ? AND secret_ref = ?`——rowcount=0 区分 not_found vs
        concurrent_modification.

        Used by E1-3 rotate flow. Caller must pass the secret_ref it read before
        rotate——stale rotate raises ConcurrentModificationError.
        """
        if not expected_secret_ref or not expected_secret_ref.strip():
            raise CredentialStoreError("expected_secret_ref must be non-empty")
        if not new_secret_ref or not new_secret_ref.strip():
            raise CredentialStoreError("new_secret_ref must be non-empty")
        if not masked_value:
            raise CredentialStoreError("masked_value must be non-empty")

        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
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
                    WHERE id = ? AND secret_ref = ?
                    """,
                    (
                        new_secret_ref,
                        masked_value,
                        fingerprint_sha256,
                        _now_ms(),
                        credential_id,
                        expected_secret_ref,
                    ),
                ) as cursor:
                    rowcount = cursor.rowcount
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                await db.execute("ROLLBACK")
                raise CredentialSecretRefConflictError(
                    f"secret_ref already in use (credential_id={credential_id})"
                ) from e
            except Exception:
                await db.execute("ROLLBACK")
                raise

        if rowcount == 0:
            # 区分：id 不存在 vs secret_ref 已变化
            await self._raise_not_found_or_concurrent(
                credential_id, expected_secret_ref
            )
        return await self.get(credential_id)

    async def update_validation_state(
        self,
        credential_id: str,
        *,
        expected_secret_ref: str,
        validation_status: CredentialValidationStatus,
        provider_id: str | None,
        validated_at: int | None,
        error_code: str | None,
    ) -> CredentialRecord:
        """Update validation state with CAS.

        Caller must pass the secret_ref it read before validation——stale validation
        raises ConcurrentModificationError，避免旧 Key 验证结果被错误应用到新 Key.
        """
        if not expected_secret_ref or not expected_secret_ref.strip():
            raise CredentialStoreError("expected_secret_ref must be non-empty")
        if validation_status not in _VALID_VALIDATION_STATUSES:
            raise CredentialStoreError(
                f"invalid validation_status: {validation_status}"
            )

        db = self._require_db()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    """
                    UPDATE web_credentials SET
                        validation_status = ?,
                        last_validated_provider_id = ?,
                        last_validated_at = ?,
                        last_error_code = ?,
                        updated_at = ?
                    WHERE id = ? AND secret_ref = ?
                    """,
                    (
                        validation_status,
                        provider_id,
                        validated_at,
                        error_code,
                        _now_ms(),
                        credential_id,
                        expected_secret_ref,
                    ),
                ) as cursor:
                    rowcount = cursor.rowcount
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

        if rowcount == 0:
            await self._raise_not_found_or_concurrent(
                credential_id, expected_secret_ref
            )
        return await self.get(credential_id)

    async def delete(
        self,
        credential_id: str,
        *,
        expected_secret_ref: str | None = None,
    ) -> CredentialRecord:
        """Delete record and return the pre-deletion snapshot.

        Args:
            expected_secret_ref: Optional CAS guard. None = no CAS check（向后兼容
                简单场景）. E1-3 service 应当传读到的 secret_ref 避免删除已被
                rotate 过的新 row.

        **Important**: does NOT call SecretStore.delete——that's the service layer's job.
        """
        db = self._require_db()
        # Read pre-deletion snapshot for caller to do best-effort secret cleanup
        pre_delete = await self.get(credential_id)

        if expected_secret_ref is not None:
            if pre_delete.secret_ref != expected_secret_ref:
                raise CredentialConcurrentModificationError(
                f"credential concurrently modified (credential_id={credential_id})"
                )
            where_clause = "WHERE id = ? AND secret_ref = ?"
            params: tuple[Any, ...] = (credential_id, expected_secret_ref)
        else:
            where_clause = "WHERE id = ?"
            params = (credential_id,)

        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    f"DELETE FROM web_credentials {where_clause}",
                    params,
                ) as cursor:
                    rowcount = cursor.rowcount
                await db.execute("COMMIT")
            except Exception:
                await db.execute("ROLLBACK")
                raise

        if rowcount == 0:
            # 不应发生（get 刚刚成功）——但为安全起见
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        return pre_delete

    # ------------------------------------------------------------------
    # CAS helper
    # ------------------------------------------------------------------

    async def _raise_not_found_or_concurrent(
        self,
        credential_id: str,
        expected_secret_ref: str,
    ) -> None:
        """区分 not_found vs concurrent_modification.

        异常 str/repr 不得包含 expected/current secret_ref——只含 credential_id.
        """
        db = self._require_db()
        async with db.execute(
            "SELECT 1 FROM web_credentials WHERE id = ?",
            (credential_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise CredentialNotFoundError(f"credential not found: {credential_id}")
        # row 存在但 CAS 失败 → secret_ref 已变化
        raise CredentialConcurrentModificationError(
            f"credential concurrently modified (credential_id={credential_id})"
        )


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
    "CredentialConcurrentModificationError",
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

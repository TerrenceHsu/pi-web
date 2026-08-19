"""SQLite-backed Provider Profile + Session Model Binding store（P1-E2-1）.

持久化用户配置的 Provider Profile 与每 Session 的 Model Binding——**不**含
Secret / API Key / Base URL / Capability 等任何敏感或派生字段。

**职责边界（E2-1）**：
- ✅ `web_provider_config_schema_meta` 独立 schema 版本管理（v1）
- ✅ `web_provider_profiles` 表 + CHECK 约束 + 索引（含 partial unique default）
- ✅ `web_session_model_bindings` 表 + FK ON DELETE RESTRICT + 索引
- ✅ Profile CRUD（`provider_id` 创建后 immutable）
- ✅ Default Profile 单事务原子切换（含 enabled/is_default 交叉约束）
- ✅ Binding upsert / get / delete / list
- ✅ Profile-in-use 显式查询 + FK RESTRICT 双重保险
- ✅ Schema 初始化 / 校验 / 版本检查（DDL 字符串 + PRAGMA 校验）
- ✅ Restart persistence
- ❌ 不调 SecretStore / Credential 服务（E2-2）
- ❌ 不调 ProviderRegistry（E2-2）
- ❌ 不调 LLM / network / 静态 model_options（E2-2）
- ❌ 不修改 `web_credentials_schema_meta` / `extension_store` schema
- ❌ 不持久化 base_url / masked_value / fingerprint / validation_status

**事务隔离**：
- 生产路径拥有**独立 aiosqlite.Connection**（与 session/extension/credentials store
  共享数据库文件但不同 connection 对象）
- connection 用 `isolation_level=None`（autocommit 模式）
- 启动时显式 `PRAGMA foreign_keys=ON` 并验证返回 1
- 每个写方法显式 `BEGIN IMMEDIATE` → SQL → `COMMIT` / `ROLLBACK`
- 单 Store 用 `asyncio.Lock` 序列化写操作；跨 Store 由 SQLite 写锁串行化

**安全约束**：
- SQLite 行 / 列不得含 api_key / secret / secret_value / secret_ref / fingerprint /
  masked_value / authorization / headers / base_url / validation_endpoint
- Error 消息可含 profile_id / session_id（短 ID），但**不得**含完整 row dump /
  SQL 参数 / credential_id / model_id / 原始 sqlite exception / DDL
- `ProviderConfigRecordDecodeError` 不得 dump row
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

# ============================================================================
# Types
# ============================================================================


BindingSource = Literal["default", "explicit"]


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass(frozen=True)
class ProviderProfile:
    """Persisted Provider Profile. **Never** contains Secret / API Key / Base URL."""

    id: str
    name: str
    provider_id: str                # immutable after create
    credential_id: str              # foreign reference to web_credentials.id (no DB FK)
    default_model: str
    enabled: bool
    is_default: bool
    created_at: int                 # ms epoch
    updated_at: int                 # ms epoch


@dataclass(frozen=True)
class SessionModelBinding:
    """Persisted session → profile/model binding. At most one row per session_id."""

    session_id: str
    profile_id: str
    model_id: str
    source: BindingSource
    created_at: int                 # ms epoch
    updated_at: int                 # ms epoch


# ============================================================================
# Errors
# ============================================================================


class ProviderConfigStoreError(Exception):
    """Base class for provider config store errors."""


class ProviderConfigSchemaError(ProviderConfigStoreError):
    """Base class for schema-level errors."""


class ProviderConfigSchemaVersionError(ProviderConfigSchemaError):
    """Raised when DB version > supported version (prevents downgrade corruption)."""


class ProviderConfigSchemaValidationError(ProviderConfigSchemaError):
    """Raised when version=1 but expected tables/columns/checks/constraints missing."""


class ProviderProfileNotFoundError(ProviderConfigStoreError):
    """Raised when get/update/delete targets a non-existent profile_id."""


class ProviderProfileAlreadyExistsError(ProviderConfigStoreError):
    """Raised when create() hits duplicate primary key (profile_id)."""


class ProviderProfileInUseError(ProviderConfigStoreError):
    """Raised when delete_profile() finds an active binding referencing the profile."""


class ProviderProfileStateError(ProviderConfigStoreError):
    """Raised when enabled/is_default combination violates cross-constraint.

    E.g., is_default=true requires enabled=true; explicit request to set
    is_default=true on a disabled profile is rejected.
    """


class SessionModelBindingConflictError(ProviderConfigStoreError):
    """Raised when upsert_binding hits an unexpected integrity conflict."""


class ProviderConfigRecordDecodeError(ProviderConfigStoreError):
    """Raised when a row cannot be decoded.

    **Safety**: exception str/repr must NOT include row contents or SQL params.
    """


# ============================================================================
# Schema constants
# ============================================================================


WEB_PROVIDER_CONFIG_SCHEMA_VERSION = 1

_SCHEMA_META_KEY = "version"

_VALID_BINDING_SOURCES: frozenset[str] = frozenset({"default", "explicit"})


_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS web_provider_config_schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_PROVIDER_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS web_provider_profiles (
    id              TEXT PRIMARY KEY,

    name            TEXT NOT NULL
                        CHECK(length(trim(name)) BETWEEN 1 AND 128),

    provider_id     TEXT NOT NULL
                        CHECK(length(trim(provider_id)) BETWEEN 1 AND 64),

    credential_id   TEXT NOT NULL
                        CHECK(length(trim(credential_id)) BETWEEN 1 AND 256),

    default_model   TEXT NOT NULL
                        CHECK(length(trim(default_model)) BETWEEN 1 AND 256),

    enabled         INTEGER NOT NULL DEFAULT 1
                        CHECK(enabled IN (0, 1)),

    is_default      INTEGER NOT NULL DEFAULT 0
                        CHECK(is_default IN (0, 1)),

    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""

_SESSION_BINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS web_session_model_bindings (
    session_id      TEXT PRIMARY KEY,

    profile_id      TEXT NOT NULL,

    model_id        TEXT NOT NULL
                        CHECK(length(trim(model_id)) BETWEEN 1 AND 256),

    source          TEXT NOT NULL
                        CHECK(source IN ('default', 'explicit')),

    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,

    FOREIGN KEY(profile_id)
        REFERENCES web_provider_profiles(id)
        ON DELETE RESTRICT
)
"""

_PROFILE_INDEX_DDL: tuple[str, ...] = (
    """
    CREATE INDEX IF NOT EXISTS ix_provider_profiles_updated_at
    ON web_provider_profiles(updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_provider_profiles_credential
    ON web_provider_profiles(credential_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_provider_profiles_default
    ON web_provider_profiles(is_default) WHERE is_default = 1
    """,
)

_BINDING_INDEX_DDL: tuple[str, ...] = (
    """
    CREATE INDEX IF NOT EXISTS ix_session_bindings_profile
    ON web_session_model_bindings(profile_id)
    """,
)


# ============================================================================
# Helpers
# ============================================================================


def default_now_ms() -> int:
    """Return current time as integer milliseconds."""
    return int(time.time() * 1000)


def generate_profile_id() -> str:
    """Generate a random unpredictable profile id.

    Format: ``profile-<token_urlsafe(32)>``——token 部分约 43 chars，整体不可预测，
    不包含 name / provider_id / credential_id / model_id 等业务字段。
    """
    return f"profile-{secrets.token_urlsafe(32)}"


def _normalize_ddl(sql: str) -> str:
    """Collapse whitespace in DDL string (preserve case; patterns use IGNORECASE)."""
    return re.sub(r"\s+", " ", sql)


def _extract_check_enum_values(
    ddl: str,
    column_name: str,
) -> frozenset[str] | None:
    """Extract quoted string enum values from ``<col> IN ( ... )`` CHECK.

    Returns frozenset of single-quoted string literals, or None if not found.
    Only handles single-quoted string literals (sufficient for our DDL).
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


def _has_int_in_check(
    ddl: str,
    column_name: str,
    expected: tuple[int, ...],
) -> bool:
    """Check that DDL contains ``<col> IN ( <int>, <int>, ... )`` matching expected."""
    expected_sorted = sorted(expected)
    pattern = (
        rf"(?<!\w){re.escape(column_name)}\s+IN\s*\(\s*"
        rf"((?:\d+\s*,\s*)*\d+)"
        rf"\s*\)"
    )
    match = re.search(pattern, ddl, re.IGNORECASE)
    if match is None:
        return False
    inner = match.group(1)
    found = sorted(int(x) for x in re.findall(r"\d+", inner))
    return found == expected_sorted


def _reject_control_chars(value: str, field_name: str) -> None:
    """Reject NUL / newline / CR / DEL / any control char (< 0x20)."""
    for ch in value:
        code = ord(ch)
        if code == 0 or code == 0x7F or (code < 0x20):
            raise ProviderConfigStoreError(
                f"{field_name} must not contain control characters or newlines"
            )


def _validate_profile_id(profile_id: str) -> None:
    if not profile_id or not profile_id.strip():
        raise ProviderConfigStoreError("profile_id must be non-empty")
    if len(profile_id) > 256:
        raise ProviderConfigStoreError("profile_id must be ≤ 256 chars")


def _validate_session_id(session_id: str) -> None:
    if not session_id or not session_id.strip():
        raise ProviderConfigStoreError("session_id must be non-empty")
    if len(session_id) > 256:
        raise ProviderConfigStoreError("session_id must be ≤ 256 chars")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ProviderConfigStoreError("name must be non-empty")
    if len(name.strip()) > 128:
        raise ProviderConfigStoreError("name must be 1–128 chars after trim")
    _reject_control_chars(name, "name")


def _validate_provider_id_field(provider_id: str) -> None:
    if not provider_id or not provider_id.strip():
        raise ProviderConfigStoreError("provider_id must be non-empty")
    if len(provider_id) > 64:
        raise ProviderConfigStoreError("provider_id must be ≤ 64 chars")
    _reject_control_chars(provider_id, "provider_id")


def _validate_credential_id_field(credential_id: str) -> None:
    if not credential_id or not credential_id.strip():
        raise ProviderConfigStoreError("credential_id must be non-empty")
    if len(credential_id) > 256:
        raise ProviderConfigStoreError("credential_id must be ≤ 256 chars")
    _reject_control_chars(credential_id, "credential_id")


def _validate_model_id_field(model_id: str) -> None:
    if not model_id or not model_id.strip():
        raise ProviderConfigStoreError("model_id must be non-empty")
    if len(model_id) > 256:
        raise ProviderConfigStoreError("model_id must be ≤ 256 chars")
    _reject_control_chars(model_id, "model_id")


def _validate_binding_source(source: str) -> None:
    if source not in _VALID_BINDING_SOURCES:
        raise ProviderConfigStoreError(
            f"invalid binding source: {source!r} "
            f"(expected one of {sorted(_VALID_BINDING_SOURCES)})"
        )


def _validate_database_path(database_path: str | Path) -> str:
    """Resolve and validate DB path.

    Rejects:
        - ``:memory:``——要求文件路径（与 E1 / Session / Extension store 同一文件）
        - SQLite URI（``file:`` / ``sqlite://``）——避免 query string 改变行为
        - 相对路径——避免 cwd 变化导致 DB 文件位置漂移
    """
    path_str = str(database_path)
    if not path_str or not path_str.strip():
        raise ProviderConfigStoreError("database_path must be non-empty")
    if path_str == ":memory:":
        raise ProviderConfigStoreError(
            "SQLiteProviderConfigStore requires a file path—':memory:' rejected"
        )
    if path_str.startswith("file:") or "sqlite://" in path_str:
        raise ProviderConfigStoreError(
            "SQLiteProviderConfigStore does not accept SQLite URIs"
        )
    p = Path(database_path)
    if not p.is_absolute():
        raise ProviderConfigStoreError(
            "SQLiteProviderConfigStore requires an absolute DB path "
            "(got relative path)"
        )
    return path_str


# ============================================================================
# Row encoding / decoding
# ============================================================================


def _encode_profile(p: ProviderProfile) -> tuple[Any, ...]:
    return (
        p.id,
        p.name,
        p.provider_id,
        p.credential_id,
        p.default_model,
        1 if p.enabled else 0,
        1 if p.is_default else 0,
        p.created_at,
        p.updated_at,
    )


def _decode_profile(row: aiosqlite.Row | dict[str, Any]) -> ProviderProfile:
    """Decode SQLite row → ProviderProfile.

    **Safety**: exception str/repr must NOT include row contents (decode failure
    only carries error type + safe id if readable).
    """
    profile_id: str | None = None
    try:
        profile_id = row["id"] if hasattr(row, "__getitem__") else None
        return ProviderProfile(
            id=row["id"],
            name=row["name"],
            provider_id=row["provider_id"],
            credential_id=row["credential_id"],
            default_model=row["default_model"],
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        suffix = (
            f" (profile_id={profile_id})" if profile_id else ""
        )
        raise ProviderConfigRecordDecodeError(
            f"failed to decode provider_profile row{suffix}: {type(e).__name__}"
        ) from e


def _decode_binding(row: aiosqlite.Row | dict[str, Any]) -> SessionModelBinding:
    """Decode SQLite row → SessionModelBinding. Same safety contract as _decode_profile."""
    session_id: str | None = None
    try:
        session_id = row["session_id"] if hasattr(row, "__getitem__") else None
        return SessionModelBinding(
            session_id=row["session_id"],
            profile_id=row["profile_id"],
            model_id=row["model_id"],
            source=row["source"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        suffix = (
            f" (session_id={session_id})" if session_id else ""
        )
        raise ProviderConfigRecordDecodeError(
            f"failed to decode session_model_binding row{suffix}: "
            f"{type(e).__name__}"
        ) from e


# ============================================================================
# SQLiteProviderConfigStore
# ============================================================================


class SQLiteProviderConfigStore:
    """SQLite-backed ProviderProfile + SessionModelBinding repository.

    生产路径用 ``open()`` 工厂建立独立 aiosqlite.Connection——不与
    session/extension/credentials store 共享 connection 对象。多 Store 写同一
    DB 文件时由 SQLite 跨连接写锁保证一致性。

    Schema 版本通过 ``web_provider_config_schema_meta`` 独立管理（v1）——不污染
    E1 ``web_credentials_schema_meta`` 或 ``extension_store.SCHEMA_VERSION``.
    """

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        owns_connection: bool,
        now_ms: Callable[[], int],
    ) -> None:
        """Private constructor——用 ``open()`` 或 ``for_testing()`` 工厂."""
        self._db: aiosqlite.Connection | None = connection
        self._owns_connection = owns_connection
        self._closed = False
        self._now_ms = now_ms
        # 单 Store 内的写操作序列化——同一 connection 不能并发 BEGIN IMMEDIATE
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    async def open(
        cls,
        database_path: str | Path,
        *,
        now_ms: Callable[[], int] = default_now_ms,
    ) -> SQLiteProviderConfigStore:
        """Production factory——独立 connection with isolation_level=None.

        Args:
            database_path: 绝对文件路径；拒绝 :memory: / SQLite URI / 相对路径.
            now_ms: 时间注入（测试用固定时钟）.

        Returns:
            Initialized store with v1 schema ready.
        """
        path_str = _validate_database_path(database_path)

        parent = Path(path_str).parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(path_str, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            await cls._ensure_foreign_keys_on(conn)
            await conn.execute("PRAGMA busy_timeout=5000")
            store = cls(
                connection=conn,
                owns_connection=True,
                now_ms=now_ms,
            )
            await store._initialize_schema()
        except BaseException:
            await conn.close()
            raise
        return store

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        owns_connection: bool = False,
        now_ms: Callable[[], int] = default_now_ms,
    ) -> SQLiteProviderConfigStore:
        """Test factory——注入 connection.

        Args:
            connection: 必须是 isolation_level=None 的 aiosqlite.Connection.
            owns_connection: True 时 close() 会关闭 connection；False 时不关闭.

        Raises:
            ProviderConfigStoreError: connection isolation_level 非 None 或
                foreign_keys 无法 ON.
        """
        if getattr(connection, "isolation_level", "sentinel") is not None:
            raise ProviderConfigStoreError(
                "Injected connection must have isolation_level=None "
                "(autocommit mode)—E2-1 transaction isolation requirement"
            )
        connection.row_factory = aiosqlite.Row
        await cls._ensure_foreign_keys_on(connection)
        store = cls(
            connection=connection,
            owns_connection=owns_connection,
            now_ms=now_ms,
        )
        await store._initialize_schema()
        return store

    @staticmethod
    async def _ensure_foreign_keys_on(conn: aiosqlite.Connection) -> None:
        """Set PRAGMA foreign_keys=ON and verify returns 1.

        Failure here means Store is unsafe to use (FK RESTRICT won't fire on
        concurrent deletes). Treat as init failure.
        """
        await conn.execute("PRAGMA foreign_keys=ON")
        async with conn.execute("PRAGMA foreign_keys") as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ProviderConfigStoreError(
                "PRAGMA foreign_keys=ON returned no row (init failed)"
            )
        try:
            value = int(row[0])
        except (TypeError, ValueError) as e:
            raise ProviderConfigStoreError(
                "PRAGMA foreign_keys returned non-integer value"
            ) from e
        if value != 1:
            raise ProviderConfigStoreError(
                f"PRAGMA foreign_keys=ON did not apply (got {value}, expected 1)"
            )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None or self._closed:
            raise ProviderConfigStoreError(
                "store not initialized or already closed"
            )
        return self._db

    async def close(self) -> None:
        """Close the store if it owns its connection. Idempotent."""
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
            "SELECT value FROM web_provider_config_schema_meta WHERE key = ?",
            (_SCHEMA_META_KEY,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError) as e:
            raise ProviderConfigSchemaError(
                "schema meta version is not an integer"
            ) from e

    # ------------------------------------------------------------------
    # Schema init / validate
    # ------------------------------------------------------------------

    async def _initialize_schema(self) -> None:
        """Initialize or validate schema. Idempotent.

        1. 确保 schema_meta 表存在（CREATE IF NOT EXISTS）
        2. 读 version
        3. 按 version 分支：
           - None → fresh DB，建 v1 schema（单事务原子）
           - 1 → 只 validate，不重建
           - >1 → raise（防 downgrade）
           - 其它 → raise
        """
        db = self._db
        assert db is not None

        await db.execute(_SCHEMA_META_DDL)

        version = await self.get_schema_version()

        if version is None:
            await self._initialize_fresh_v1_schema()
        elif version == WEB_PROVIDER_CONFIG_SCHEMA_VERSION:
            await self._validate_v1_schema()
        elif version > WEB_PROVIDER_CONFIG_SCHEMA_VERSION:
            raise ProviderConfigSchemaVersionError(
                "Provider config database schema is newer than this application "
                f"(got v{version}, supported v{WEB_PROVIDER_CONFIG_SCHEMA_VERSION})"
            )
        else:
            raise ProviderConfigSchemaVersionError(
                f"Unsupported provider config schema version: {version}"
            )

    async def _initialize_fresh_v1_schema(self) -> None:
        """Fresh DB: single BEGIN IMMEDIATE transaction builds all tables + indexes + meta row."""
        db = self._db
        assert db is not None
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(_PROVIDER_PROFILES_DDL)
            for stmt in _PROFILE_INDEX_DDL:
                await db.execute(stmt)
            await db.execute(_SESSION_BINDINGS_DDL)
            for stmt in _BINDING_INDEX_DDL:
                await db.execute(stmt)
            await db.execute(
                "INSERT INTO web_provider_config_schema_meta (key, value) "
                "VALUES (?, ?)",
                (_SCHEMA_META_KEY, str(WEB_PROVIDER_CONFIG_SCHEMA_VERSION)),
            )
            await db.execute("COMMIT")
        except BaseException:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    async def _validate_v1_schema(self) -> None:
        """version=1: read-only validation of tables / columns / indexes / CHECK / FK.

        **Never** auto-repair—拒绝任何 schema 漂移。
        """
        db = self._require_db()

        # 1. Profiles table exists + columns
        async with db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name='web_provider_profiles'"
        ) as cursor:
            profiles_row = await cursor.fetchone()
        if profiles_row is None:
            raise ProviderConfigSchemaValidationError(
                "version=1 but table 'web_provider_profiles' is missing"
            )

        async with db.execute(
            "PRAGMA table_info(web_provider_profiles)"
        ) as cursor:
            rows = await cursor.fetchall()
        columns = {r["name"] for r in rows}
        required_profile_columns = {
            "id", "name", "provider_id", "credential_id",
            "default_model", "enabled", "is_default",
            "created_at", "updated_at",
        }
        missing = required_profile_columns - columns
        if missing:
            raise ProviderConfigSchemaValidationError(
                f"version=1 but web_provider_profiles missing columns: "
                f"{sorted(missing)}"
            )

        # Forbidden columns (must NEVER appear in profiles table)
        forbidden_columns = {
            "api_key", "secret", "secret_value", "secret_ref",
            "fingerprint", "masked_value", "authorization",
            "headers", "base_url", "validation_endpoint",
        }
        present_forbidden = forbidden_columns & columns
        if present_forbidden:
            raise ProviderConfigSchemaValidationError(
                f"version=1 but web_provider_profiles contains forbidden columns: "
                f"{sorted(present_forbidden)}"
            )

        # Column affinity / NOT NULL on required fields
        # SQLite quirk: TEXT PRIMARY KEY columns report notnull=0 in PRAGMA
        # table_info; rely on pk > 0 for those instead.
        col_meta = {r["name"]: r for r in rows}
        for col in required_profile_columns:
            meta = col_meta[col]
            if meta["pk"] > 0:
                continue
            if meta["notnull"] != 1:
                raise ProviderConfigSchemaValidationError(
                    f"version=1 but web_provider_profiles.{col} is nullable"
                )

        # 2. Bindings table exists + columns
        async with db.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name='web_session_model_bindings'"
        ) as cursor:
            bindings_row = await cursor.fetchone()
        if bindings_row is None:
            raise ProviderConfigSchemaValidationError(
                "version=1 but table 'web_session_model_bindings' is missing"
            )

        async with db.execute(
            "PRAGMA table_info(web_session_model_bindings)"
        ) as cursor:
            brows = await cursor.fetchall()
        bcolumns = {r["name"] for r in brows}
        required_binding_columns = {
            "session_id", "profile_id", "model_id",
            "source", "created_at", "updated_at",
        }
        bmissing = required_binding_columns - bcolumns
        if bmissing:
            raise ProviderConfigSchemaValidationError(
                f"version=1 but web_session_model_bindings missing columns: "
                f"{sorted(bmissing)}"
            )
        present_forbidden_b = forbidden_columns & bcolumns
        if present_forbidden_b:
            raise ProviderConfigSchemaValidationError(
                f"version=1 but web_session_model_bindings contains forbidden "
                f"columns: {sorted(present_forbidden_b)}"
            )
        bcol_meta = {r["name"]: r for r in brows}
        for col in required_binding_columns:
            meta = bcol_meta[col]
            if meta["pk"] > 0:
                continue
            if meta["notnull"] != 1:
                raise ProviderConfigSchemaValidationError(
                    f"version=1 but web_session_model_bindings.{col} is nullable"
                )

        # 3. Indexes exist (by name)
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN ("
            "'ix_provider_profiles_updated_at', "
            "'ix_provider_profiles_credential', "
            "'ux_provider_profiles_default', "
            "'ix_session_bindings_profile')"
        ) as cursor:
            index_rows = await cursor.fetchall()
        index_names = {r["name"] for r in index_rows}
        for required_index in (
            "ix_provider_profiles_updated_at",
            "ix_provider_profiles_credential",
            "ux_provider_profiles_default",
            "ix_session_bindings_profile",
        ):
            if required_index not in index_names:
                raise ProviderConfigSchemaValidationError(
                    f"version=1 but index '{required_index}' is missing"
                )

        # 4. Partial unique index must have WHERE clause + UNIQUE keyword
        async with db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='ux_provider_profiles_default'"
        ) as cursor:
            uid_row = await cursor.fetchone()
        uid_sql = (uid_row["sql"] if uid_row and uid_row["sql"] else "") or ""
        normalized_uid = _normalize_ddl(uid_sql)
        if not re.search(r"UNIQUE", normalized_uid, re.IGNORECASE):
            raise ProviderConfigSchemaValidationError(
                "version=1 but ux_provider_profiles_default is missing UNIQUE keyword"
            )
        if not re.search(
            r"WHERE\s+is_default\s*=\s*1",
            normalized_uid,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but ux_provider_profiles_default partial WHERE "
                "is_default = 1 clause is missing"
            )

        # 5. Profile DDL string CHECK constraints
        profiles_ddl = _normalize_ddl(profiles_row["sql"] or "")

        # name length CHECK
        if not re.search(
            r"length\s*\(\s*trim\s*\(\s*name\s*\)\s*\)\s+BETWEEN\s+1\s+AND\s+128",
            profiles_ddl,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but name length(trim(name)) BETWEEN 1 AND 128 "
                "CHECK missing from DDL"
            )
        # provider_id length CHECK
        if not re.search(
            r"length\s*\(\s*trim\s*\(\s*provider_id\s*\)\s*\)\s+BETWEEN\s+1\s+AND\s+64",
            profiles_ddl,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but provider_id length CHECK missing from DDL"
            )
        # credential_id length CHECK
        if not re.search(
            r"length\s*\(\s*trim\s*\(\s*credential_id\s*\)\s*\)\s+BETWEEN\s+1\s+AND\s+256",
            profiles_ddl,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but credential_id length CHECK missing from DDL"
            )
        # default_model length CHECK
        if not re.search(
            r"length\s*\(\s*trim\s*\(\s*default_model\s*\)\s*\)\s+BETWEEN\s+1\s+AND\s+256",
            profiles_ddl,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but default_model length CHECK missing from DDL"
            )
        # enabled IN (0, 1)
        if not _has_int_in_check(profiles_ddl, "enabled", (0, 1)):
            raise ProviderConfigSchemaValidationError(
                "version=1 but enabled IN (0, 1) CHECK missing from DDL"
            )
        # is_default IN (0, 1)
        if not _has_int_in_check(profiles_ddl, "is_default", (0, 1)):
            raise ProviderConfigSchemaValidationError(
                "version=1 but is_default IN (0, 1) CHECK missing from DDL"
            )

        # 6. Binding DDL string CHECK constraints + FK
        bindings_ddl = _normalize_ddl(bindings_row["sql"] or "")

        if not re.search(
            r"length\s*\(\s*trim\s*\(\s*model_id\s*\)\s*\)\s+BETWEEN\s+1\s+AND\s+256",
            bindings_ddl,
            re.IGNORECASE,
        ):
            raise ProviderConfigSchemaValidationError(
                "version=1 but model_id length CHECK missing from DDL"
            )
        binding_sources = _extract_check_enum_values(bindings_ddl, "source")
        if binding_sources is None:
            raise ProviderConfigSchemaValidationError(
                "version=1 but source IN (...) CHECK missing from DDL"
            )
        if binding_sources != _VALID_BINDING_SOURCES:
            raise ProviderConfigSchemaValidationError(
                "version=1 but source CHECK has unexpected enum values"
            )

        # 7. FK profile_id → web_provider_profiles.id ON DELETE RESTRICT
        async with db.execute(
            "PRAGMA foreign_key_list(web_session_model_bindings)"
        ) as cursor:
            fk_rows = await cursor.fetchall()
        # Find FK with from=profile_id
        target_fk = None
        for fk in fk_rows:
            if fk["from"] == "profile_id":
                target_fk = fk
                break
        if target_fk is None:
            raise ProviderConfigSchemaValidationError(
                "version=1 but foreign_key_list has no profile_id reference"
            )
        if target_fk["table"] != "web_provider_profiles":
            raise ProviderConfigSchemaValidationError(
                "version=1 but profile_id FK references wrong table: "
                f"{target_fk['table']}"
            )
        if target_fk["to"] != "id":
            raise ProviderConfigSchemaValidationError(
                f"version=1 but profile_id FK references wrong column: "
                f"{target_fk['to']}"
            )
        if str(target_fk["on_delete"]).upper() != "RESTRICT":
            raise ProviderConfigSchemaValidationError(
                "version=1 but profile_id FK on_delete is not RESTRICT "
                f"(got {target_fk['on_delete']})"
            )

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    async def create_profile(
        self,
        *,
        profile_id: str,
        name: str,
        provider_id: str,
        credential_id: str,
        default_model: str,
        enabled: bool = True,
        is_default: bool = False,
    ) -> ProviderProfile:
        """Insert a new ProviderProfile. Raises on duplicate id or invalid state."""
        _validate_profile_id(profile_id)
        _validate_name(name)
        _validate_provider_id_field(provider_id)
        _validate_credential_id_field(credential_id)
        _validate_model_id_field(default_model)

        if is_default and not enabled:
            raise ProviderProfileStateError(
                f"cannot create default profile with enabled=false "
                f"(profile_id={profile_id})"
            )

        db = self._require_db()
        now = self._now_ms()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                # Atomic default switch: clear other defaults BEFORE insert
                if is_default:
                    await db.execute(
                        "UPDATE web_provider_profiles SET is_default = 0 "
                        "WHERE is_default = 1"
                    )
                await db.execute(
                    """
                    INSERT INTO web_provider_profiles (
                        id, name, provider_id, credential_id, default_model,
                        enabled, is_default, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        name,
                        provider_id,
                        credential_id,
                        default_model,
                        1 if enabled else 0,
                        1 if is_default else 0,
                        now,
                        now,
                    ),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                msg = str(e).lower()
                # Primary key conflict (duplicate profile_id)
                if "web_provider_profiles.id" in msg or "primary key" in msg:
                    raise ProviderProfileAlreadyExistsError(
                        f"profile id already exists: {profile_id}"
                    ) from e
                # Partial unique index conflict (extremely unlikely given lock + UPDATE-first)
                if (
                    "ux_provider_profiles_default" in msg
                    or "is_default" in msg
                ):
                    raise ProviderProfileStateError(
                        "concurrent default profile race—retry"
                    ) from e
                raise ProviderConfigStoreError(
                    "integrity error during create_profile"
                ) from e
            except BaseException:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return await self.get_profile(profile_id)

    async def get_profile(self, profile_id: str) -> ProviderProfile:
        """Return profile or raise ProviderProfileNotFoundError."""
        _validate_profile_id(profile_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_provider_profiles WHERE id = ?",
            (profile_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ProviderProfileNotFoundError(
                f"profile not found: {profile_id}"
            )
        return _decode_profile(row)

    async def list_profiles(self) -> tuple[ProviderProfile, ...]:
        """Return all profiles ordered by updated_at DESC, then id ASC for stability."""
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_provider_profiles "
            "ORDER BY updated_at DESC, id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_decode_profile(r) for r in rows)

    async def get_default_profile(self) -> ProviderProfile | None:
        """Return the current default profile, or None if no default."""
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_provider_profiles WHERE is_default = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _decode_profile(row)

    async def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        credential_id: str | None = None,
        default_model: str | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
    ) -> ProviderProfile:
        """Update mutable fields of a Profile.

        ``provider_id`` is **immutable**——no parameter is exposed.

        enabled / is_default cross-constraint (see design doc §4.2):
            - explicit ``is_default=true`` + ``enabled=false`` → reject
            - ``enabled=false`` on current default → auto-clear ``is_default`` in
              the same transaction
            - ``is_default=true`` on enabled profile → atomically clear other
              defaults in same transaction
        """
        _validate_profile_id(profile_id)
        if name is not None:
            _validate_name(name)
        if credential_id is not None:
            _validate_credential_id_field(credential_id)
        if default_model is not None:
            _validate_model_id_field(default_model)

        # Pre-resolve new state to detect illegal combination early (pre-lock).
        # Final resolution happens inside the transaction with current row.
        if enabled is False and is_default is True:
            raise ProviderProfileStateError(
                f"cannot set is_default=true on disabled profile "
                f"(profile_id={profile_id})"
            )

        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM web_provider_profiles WHERE id = ?",
                    (profile_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute("COMMIT")
                    raise ProviderProfileNotFoundError(
                        f"profile not found: {profile_id}"
                    )
                current = _decode_profile(row)

                new_enabled = (
                    enabled if enabled is not None else current.enabled
                )
                if is_default is not None:
                    # Explicit request
                    if is_default and not new_enabled:
                        # Should have been caught pre-lock, but double-check
                        raise ProviderProfileStateError(
                            f"cannot set is_default=true on disabled profile "
                            f"(profile_id={profile_id})"
                        )
                    new_is_default: bool = is_default
                else:
                    # No explicit is_default request
                    if not new_enabled and current.is_default:
                        # Auto-clear when disabling current default
                        new_is_default = False
                    else:
                        new_is_default = current.is_default

                # If becoming default, clear other defaults in same transaction
                if new_is_default and not current.is_default:
                    await db.execute(
                        "UPDATE web_provider_profiles SET is_default = 0 "
                        "WHERE is_default = 1"
                    )

                new_updated = max(self._now_ms(), current.updated_at + 1)
                await db.execute(
                    """
                    UPDATE web_provider_profiles SET
                        name = ?,
                        credential_id = ?,
                        default_model = ?,
                        enabled = ?,
                        is_default = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name if name is not None else current.name,
                        credential_id
                        if credential_id is not None
                        else current.credential_id,
                        default_model
                        if default_model is not None
                        else current.default_model,
                        1 if new_enabled else 0,
                        1 if new_is_default else 0,
                        new_updated,
                        profile_id,
                    ),
                )
                await db.execute("COMMIT")
            except BaseException:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return await self.get_profile(profile_id)

    async def delete_profile(self, profile_id: str) -> ProviderProfile:
        """Delete profile. Raises ProviderProfileInUseError if any binding references it.

        Uses explicit in-use query + FK ON DELETE RESTRICT as double safety.
        """
        _validate_profile_id(profile_id)
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM web_provider_profiles WHERE id = ?",
                    (profile_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute("COMMIT")
                    raise ProviderProfileNotFoundError(
                        f"profile not found: {profile_id}"
                    )
                pre_delete = _decode_profile(row)

                # In-use check (explicit + FK RESTRICT double safety)
                async with db.execute(
                    "SELECT 1 FROM web_session_model_bindings "
                    "WHERE profile_id = ? LIMIT 1",
                    (profile_id,),
                ) as cursor:
                    in_use_row = await cursor.fetchone()
                if in_use_row is not None:
                    await db.execute("COMMIT")
                    raise ProviderProfileInUseError(
                        f"profile in use by session bindings "
                        f"(profile_id={profile_id})"
                    )

                await db.execute(
                    "DELETE FROM web_provider_profiles WHERE id = ?",
                    (profile_id,),
                )
                await db.execute("COMMIT")
            except ProviderProfileInUseError:
                raise
            except ProviderProfileNotFoundError:
                raise
            except BaseException:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return pre_delete

    # ------------------------------------------------------------------
    # Binding CRUD
    # ------------------------------------------------------------------

    async def get_binding(
        self,
        session_id: str,
    ) -> SessionModelBinding | None:
        """Return binding for session_id, or None."""
        _validate_session_id(session_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_session_model_bindings WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _decode_binding(row)

    async def upsert_binding(
        self,
        *,
        session_id: str,
        profile_id: str,
        model_id: str,
        source: BindingSource,
    ) -> SessionModelBinding:
        """Insert or update binding. Preserves created_at on update.

        Does NOT verify:
            - Session existence (E2-2 Service layer)
            - Profile enabled state (E2-2 Service layer)

        Verifies (structural):
            - source in {default, explicit}
            - model_id non-empty / length / no control chars
            - profile_id existence via FK RESTRICT
        """
        _validate_session_id(session_id)
        _validate_profile_id(profile_id)
        _validate_model_id_field(model_id)
        _validate_binding_source(source)

        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT created_at FROM web_session_model_bindings "
                    "WHERE session_id = ?",
                    (session_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                now = self._now_ms()
                if existing is None:
                    await db.execute(
                        """
                        INSERT INTO web_session_model_bindings (
                            session_id, profile_id, model_id, source,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (session_id, profile_id, model_id, source, now, now),
                    )
                else:
                    old_updated_row = await (
                        await db.execute(
                            "SELECT updated_at FROM web_session_model_bindings "
                            "WHERE session_id = ?",
                            (session_id,),
                        )
                    ).fetchone()
                    if old_updated_row is None:
                        raise ProviderConfigStoreError(
                            "session model binding disappeared during update"
                        )
                    old_updated = int(old_updated_row["updated_at"])
                    new_updated = max(now, old_updated + 1)
                    await db.execute(
                        """
                        UPDATE web_session_model_bindings SET
                            profile_id = ?,
                            model_id = ?,
                            source = ?,
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (profile_id, model_id, source, new_updated, session_id),
                    )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as e:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                msg = str(e).lower()
                if "foreign key" in msg or "profile_id" in msg:
                    # FK RESTRICT—profile doesn't exist
                    raise ProviderProfileNotFoundError(
                        f"profile referenced by binding does not exist "
                        f"(profile_id={profile_id})"
                    ) from e
                raise SessionModelBindingConflictError(
                    "integrity error during upsert_binding"
                ) from e
            except BaseException:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        result = await self.get_binding(session_id)
        assert result is not None
        return result

    async def delete_binding(
        self,
        session_id: str,
    ) -> SessionModelBinding | None:
        """Delete binding. Returns the pre-deletion snapshot, or None if not found."""
        _validate_session_id(session_id)
        db = self._require_db()
        async with self._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT * FROM web_session_model_bindings "
                    "WHERE session_id = ?",
                    (session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    await db.execute("COMMIT")
                    return None
                pre_delete = _decode_binding(row)
                await db.execute(
                    "DELETE FROM web_session_model_bindings "
                    "WHERE session_id = ?",
                    (session_id,),
                )
                await db.execute("COMMIT")
            except BaseException:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        return pre_delete

    async def list_bindings_for_profile(
        self,
        profile_id: str,
    ) -> tuple[SessionModelBinding, ...]:
        """Return all bindings referencing the given profile_id."""
        _validate_profile_id(profile_id)
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM web_session_model_bindings "
            "WHERE profile_id = ? ORDER BY updated_at DESC, session_id ASC",
            (profile_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return tuple(_decode_binding(r) for r in rows)


# ============================================================================
# Re-exports
# ============================================================================


__all__ = [
    # Types
    "BindingSource",
    # Dataclasses
    "ProviderProfile",
    "SessionModelBinding",
    # Errors
    "ProviderConfigStoreError",
    "ProviderConfigSchemaError",
    "ProviderConfigSchemaVersionError",
    "ProviderConfigSchemaValidationError",
    "ProviderProfileNotFoundError",
    "ProviderProfileAlreadyExistsError",
    "ProviderProfileInUseError",
    "ProviderProfileStateError",
    "SessionModelBindingConflictError",
    "ProviderConfigRecordDecodeError",
    # Constants
    "WEB_PROVIDER_CONFIG_SCHEMA_VERSION",
    # Helpers
    "default_now_ms",
    "generate_profile_id",
    # Repository
    "SQLiteProviderConfigStore",
]

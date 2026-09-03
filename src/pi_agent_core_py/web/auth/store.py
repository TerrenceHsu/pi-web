"""SQLite account and login-session repository."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import aiosqlite

from .models import AuthUserRecord

AUTH_SCHEMA_VERSION = 2


class AuthStoreError(Exception):
    """Base authentication repository error."""


class AuthUserNameConflictError(AuthStoreError):
    """A case-insensitive account name already exists."""


class AuthSchemaError(AuthStoreError):
    """The authentication schema is corrupt or unsupported."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_user_id() -> str:
    return f"usr_{uuid4().hex}"


def _row_to_user(row: aiosqlite.Row) -> AuthUserRecord:
    return AuthUserRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        is_admin=bool(row["is_admin"]),
        password_hash=str(row["password_hash"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


class AuthStore:
    """Own an independent autocommit connection for accounts and sessions."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._write_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, database_path: str) -> AuthStore:
        connection = await aiosqlite.connect(database_path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        store = cls(connection)
        try:
            await store.init()
        except Exception:
            await connection.close()
            raise
        return store

    async def init(self) -> None:
        db = self._require_connection()
        async with self._write_lock:
            try:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("PRAGMA busy_timeout = 5000")
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS auth_schema_meta ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "version INTEGER NOT NULL CHECK (version >= 1))"
                )
                async with db.execute(
                    "SELECT version FROM auth_schema_meta WHERE singleton = 1"
                ) as cursor:
                    version_row = await cursor.fetchone()
                if version_row is None:
                    await db.execute(
                        "INSERT INTO auth_schema_meta (singleton, version) VALUES (1, ?)",
                        (AUTH_SCHEMA_VERSION,),
                    )
                elif not 1 <= int(version_row["version"]) <= AUTH_SCHEMA_VERSION:
                    raise AuthSchemaError("authentication schema version is unsupported")
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS auth_users ("
                    "id TEXT PRIMARY KEY, "
                    "name TEXT NOT NULL COLLATE NOCASE UNIQUE, "
                    "is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)), "
                    "password_hash TEXT NOT NULL, "
                    "created_at INTEGER NOT NULL, "
                    "updated_at INTEGER NOT NULL, "
                    "CHECK (length(id) BETWEEN 5 AND 64), "
                    "CHECK (length(name) BETWEEN 1 AND 64), "
                    "CHECK (length(password_hash) BETWEEN 40 AND 512))"
                )
                if version_row is not None and int(version_row["version"]) == 1:
                    async with db.execute("PRAGMA table_info(auth_users)") as cursor:
                        columns = {str(row["name"]) for row in await cursor.fetchall()}
                    if "is_admin" not in columns:
                        await db.execute(
                            "ALTER TABLE auth_users ADD COLUMN is_admin INTEGER "
                            "NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1))"
                        )
                    await db.execute(
                        "UPDATE auth_users SET is_admin = 1 "
                        "WHERE name = 'admin' COLLATE NOCASE"
                    )
                    await db.execute(
                        "UPDATE auth_schema_meta SET version = ? WHERE singleton = 1",
                        (AUTH_SCHEMA_VERSION,),
                    )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS auth_sessions ("
                    "token_hash TEXT PRIMARY KEY, "
                    "user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE, "
                    "created_at INTEGER NOT NULL, "
                    "expires_at INTEGER NOT NULL, "
                    "CHECK (length(token_hash) = 64), "
                    "CHECK (expires_at > created_at))"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user "
                    "ON auth_sessions(user_id, expires_at)"
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def get_user_by_name(self, name: str) -> AuthUserRecord | None:
        db = self._require_connection()
        async with db.execute(
            "SELECT id, name, is_admin, password_hash, created_at, updated_at "
            "FROM auth_users WHERE name = ? COLLATE NOCASE",
            (name,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_user(row) if row is not None else None

    async def get_user(self, user_id: str) -> AuthUserRecord | None:
        db = self._require_connection()
        async with db.execute(
            "SELECT id, name, is_admin, password_hash, created_at, updated_at "
            "FROM auth_users WHERE id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_user(row) if row is not None else None

    async def create_user(
        self,
        name: str,
        password_hash: str,
        *,
        is_admin: bool = False,
    ) -> AuthUserRecord:
        """Create an account for bootstrap/administrative tooling (no public API)."""

        db = self._require_connection()
        user_id = _new_user_id()
        now = _now_ms()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO auth_users "
                    "(id, name, is_admin, password_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, name, int(is_admin), password_hash, now, now),
                )
                await db.execute("COMMIT")
            except aiosqlite.IntegrityError as exc:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise AuthUserNameConflictError("account name already exists") from exc
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        record = await self.get_user(user_id)
        if record is None:
            raise AuthStoreError("created account could not be read")
        return record

    async def create_initial_user_if_empty(
        self,
        name: str,
        password_hash: str,
        *,
        is_admin: bool = False,
    ) -> tuple[AuthUserRecord, bool]:
        db = self._require_connection()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT id, name, is_admin, password_hash, created_at, updated_at "
                    "FROM auth_users ORDER BY created_at ASC, id ASC LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    await db.execute("COMMIT")
                    return _row_to_user(row), False
                user_id = _new_user_id()
                now = _now_ms()
                await db.execute(
                    "INSERT INTO auth_users "
                    "(id, name, is_admin, password_hash, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, name, int(is_admin), password_hash, now, now),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        record = await self.get_user(user_id)
        if record is None:
            raise AuthStoreError("bootstrap account could not be read")
        return record, True

    async def create_session(
        self,
        *,
        token_hash: str,
        user_id: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        db = self._require_connection()
        async with self._write_lock:
            try:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "DELETE FROM auth_sessions WHERE expires_at <= ?",
                    (created_at,),
                )
                await db.execute(
                    "INSERT INTO auth_sessions "
                    "(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (token_hash, user_id, created_at, expires_at),
                )
                await db.execute("COMMIT")
            except Exception:
                try:
                    await db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    async def get_user_for_session(
        self,
        token_hash: str,
        *,
        now_ms: int,
    ) -> AuthUserRecord | None:
        db = self._require_connection()
        async with db.execute(
            "SELECT u.id, u.name, u.is_admin, u.password_hash, u.created_at, u.updated_at "
            "FROM auth_sessions AS s "
            "JOIN auth_users AS u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, now_ms),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_user(row) if row is not None else None

    async def revoke_session(self, token_hash: str) -> None:
        db = self._require_connection()
        async with self._write_lock:
            await db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))

    async def revoke_all_sessions(self) -> None:
        """Invalidate every login session while preserving user accounts."""
        db = self._require_connection()
        async with self._write_lock:
            await db.execute("DELETE FROM auth_sessions")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._connection.close()

    def _require_connection(self) -> aiosqlite.Connection:
        if self._closed:
            raise AuthStoreError("authentication store is closed")
        return self._connection


__all__ = [
    "AUTH_SCHEMA_VERSION",
    "AuthSchemaError",
    "AuthStore",
    "AuthStoreError",
    "AuthUserNameConflictError",
]

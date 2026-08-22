"""Revisioned SQLite singleton store for secret-free sandbox configuration."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import aiosqlite
from pydantic import ValidationError

from .models import SandboxAdminConfig, SandboxConfigRecord

_SCHEMA_VERSION = 1


class SandboxConfigStoreError(Exception):
    """Safe base class for configuration persistence failures."""


class SandboxConfigConflictError(SandboxConfigStoreError):
    """The submitted revision is stale."""


class SandboxConfigCorruptError(SandboxConfigStoreError):
    """Persisted data or schema cannot be safely decoded."""


class SQLiteSandboxConfigStore:
    """One config row with optimistic revision checks and atomic writes."""

    def __init__(
        self,
        connection: aiosqlite.Connection,
        *,
        owns_connection: bool,
        now_ms: Callable[[], int],
    ) -> None:
        self._connection = connection
        self._owns_connection = owns_connection
        self._now_ms = now_ms
        self._write_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        database_path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> SQLiteSandboxConfigStore:
        path = Path(database_path)
        if not path.is_absolute() or str(path) == ":memory:":
            raise SandboxConfigStoreError("sandbox config requires an absolute SQLite path")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path, isolation_level=None)
        try:
            store = cls(
                connection,
                owns_connection=True,
                now_ms=now_ms or (lambda: int(time.time() * 1000)),
            )
            await store._initialize()
            return store
        except BaseException:
            await connection.close()
            raise

    @classmethod
    async def for_testing(
        cls,
        connection: aiosqlite.Connection,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> SQLiteSandboxConfigStore:
        if connection.isolation_level is not None:
            raise SandboxConfigStoreError("sandbox config connection must use autocommit")
        store = cls(
            connection,
            owns_connection=False,
            now_ms=now_ms or (lambda: int(time.time() * 1000)),
        )
        await store._initialize()
        return store

    async def get(self) -> SandboxConfigRecord:
        self._ensure_open()
        cursor = await self._connection.execute(
            "SELECT config_json, revision, updated_at_ms "
            "FROM web_coding_sandbox_config WHERE singleton_id = 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return SandboxConfigRecord(
                config=SandboxAdminConfig(),
                revision=0,
                updated_at_ms=None,
            )
        try:
            payload = json.loads(str(row[0]))
            if not isinstance(payload, dict):
                raise ValueError("sandbox configuration must be an object")
            for retired_field in (
                "modal_token_id_credential_id",
                "modal_token_secret_credential_id",
            ):
                if payload.get(retired_field) is not None:
                    raise ValueError("retired provider credentials are not accepted")
                payload.pop(retired_field, None)
            config = SandboxAdminConfig.model_validate(payload)
            return SandboxConfigRecord(
                config=config,
                revision=int(row[1]),
                updated_at_ms=int(row[2]),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise SandboxConfigCorruptError("sandbox configuration record is invalid") from exc

    async def put(
        self,
        config: SandboxAdminConfig,
        *,
        expected_revision: int,
    ) -> SandboxConfigRecord:
        self._ensure_open()
        if expected_revision < 0:
            raise SandboxConfigConflictError("sandbox config revision is stale")
        payload = config.model_dump_json()
        if len(payload.encode("utf-8")) > 64 * 1024:
            raise SandboxConfigStoreError("sandbox configuration is too large")
        async with self._write_lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(
                    "SELECT revision FROM web_coding_sandbox_config WHERE singleton_id = 1"
                )
                row = await cursor.fetchone()
                await cursor.close()
                current_revision = 0 if row is None else int(row[0])
                if current_revision != expected_revision:
                    raise SandboxConfigConflictError("sandbox config revision is stale")
                revision = current_revision + 1
                updated_at = self._now_ms()
                await self._connection.execute(
                    "INSERT INTO web_coding_sandbox_config "
                    "(singleton_id, config_json, revision, updated_at_ms) "
                    "VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(singleton_id) DO UPDATE SET "
                    "config_json = excluded.config_json, "
                    "revision = excluded.revision, "
                    "updated_at_ms = excluded.updated_at_ms",
                    (payload, revision, updated_at),
                )
                await self._connection.execute("COMMIT")
            except BaseException:
                await self._connection.execute("ROLLBACK")
                raise
        return SandboxConfigRecord(
            config=config,
            revision=revision,
            updated_at_ms=updated_at,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            await self._connection.close()

    async def _initialize(self) -> None:
        await self._connection.execute("PRAGMA busy_timeout=5000")
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS web_coding_sandbox_schema_meta ("
            "key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        cursor = await self._connection.execute(
            "SELECT value FROM web_coding_sandbox_schema_meta WHERE key = 'version'"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None and int(row[0]) != _SCHEMA_VERSION:
            raise SandboxConfigCorruptError("unsupported sandbox config schema version")
        await self._connection.execute(
            "INSERT OR IGNORE INTO web_coding_sandbox_schema_meta (key, value) "
            "VALUES ('version', ?)",
            (_SCHEMA_VERSION,),
        )
        await self._connection.execute(
            "CREATE TABLE IF NOT EXISTS web_coding_sandbox_config ("
            "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), "
            "config_json TEXT NOT NULL CHECK(length(config_json) <= 65536), "
            "revision INTEGER NOT NULL CHECK(revision >= 1), "
            "updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= 0))"
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxConfigStoreError("sandbox config store is closed")


__all__ = [
    "SQLiteSandboxConfigStore",
    "SandboxConfigConflictError",
    "SandboxConfigCorruptError",
    "SandboxConfigStoreError",
]

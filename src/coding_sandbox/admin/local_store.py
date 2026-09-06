"""Independent revisioned local runtime config; never migrates E2B settings."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..local_docker import LocalDockerExecutionConfig
from .store import SandboxConfigConflictError, SandboxConfigCorruptError, SandboxConfigStoreError


class LocalDockerConfigRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    config: LocalDockerExecutionConfig = Field(default_factory=LocalDockerExecutionConfig)
    revision: int = Field(default=0, ge=0)


class SQLiteLocalDockerConfigStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._db = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> SQLiteLocalDockerConfigStore:
        if not path.is_absolute():
            raise SandboxConfigStoreError("local config requires an absolute database path")
        connection = await aiosqlite.connect(path, isolation_level=None)
        try:
            await connection.execute("""
                CREATE TABLE IF NOT EXISTS web_local_docker_config (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    config_json TEXT NOT NULL
                )
            """)
            return cls(connection)
        except BaseException:
            await connection.close()
            raise

    async def get(self) -> LocalDockerConfigRecord:
        async with self._lock:
            return await self._get_unlocked()

    async def _get_unlocked(self) -> LocalDockerConfigRecord:
        async with self._db.execute(
            "SELECT revision, schema_version, config_json FROM web_local_docker_config "
            "WHERE singleton_id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return LocalDockerConfigRecord()
        if row[1] != 1:
            raise SandboxConfigCorruptError("unsupported local runtime schema")
        try:
            return LocalDockerConfigRecord(
                config=LocalDockerExecutionConfig.model_validate_json(row[2]),
                revision=row[0],
            )
        except ValidationError:
            raise SandboxConfigCorruptError("invalid local runtime config") from None

    async def put(
        self,
        config: LocalDockerExecutionConfig,
        *,
        expected_revision: int,
    ) -> LocalDockerConfigRecord:
        async with self._lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                current = await self._get_unlocked()
                if expected_revision != current.revision:
                    raise SandboxConfigConflictError("local runtime config changed")
                result = LocalDockerConfigRecord(config=config, revision=current.revision + 1)
                await self._db.execute(
                    "INSERT INTO web_local_docker_config VALUES (1, ?, 1, ?) "
                    "ON CONFLICT(singleton_id) DO UPDATE SET "
                    "revision = excluded.revision, config_json = excluded.config_json",
                    (result.revision, config.model_dump_json()),
                )
                await self._db.commit()
                return result
            except BaseException:
                await self._db.rollback()
                raise

    async def close(self) -> None:
        await self._db.close()

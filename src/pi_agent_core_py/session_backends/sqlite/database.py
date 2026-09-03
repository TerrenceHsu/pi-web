"""Shared asynchronous SQLite coordination primitives."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast
from weakref import WeakKeyDictionary

import aiosqlite

_P = ParamSpec("_P")
_R = TypeVar("_R")
_T = TypeVar("_T")


class ReentrantAsyncLock:
    """Task-reentrant lock used to protect one SQLite connection.

    ``aiosqlite`` serializes individual statements, not application-level
    transactions.  A task-reentrant lock lets public store operations call
    other protected operations without allowing a second task to interleave
    SQL between ``BEGIN`` and ``COMMIT``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def acquire(self) -> bool:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always owns async methods
            raise RuntimeError("SQLite operation has no current asyncio task")
        if self._owner is task:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return True

    def release(self) -> None:
        task = asyncio.current_task()
        if task is None or self._owner is not task:
            raise RuntimeError("SQLite operation lock released by a non-owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> ReentrantAsyncLock:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.release()


class AsyncSqliteDatabase:
    """Connection handle shared by every service using the same connection."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.connection = connection
        self.operation_lock = ReentrantAsyncLock()

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.operation_lock:
            yield self.connection

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self.operation_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise


_DATABASES: WeakKeyDictionary[aiosqlite.Connection, AsyncSqliteDatabase] = (
    WeakKeyDictionary()
)


def database_for(connection: aiosqlite.Connection) -> AsyncSqliteDatabase:
    """Return the process-wide coordinator for an aiosqlite connection."""

    database = _DATABASES.get(connection)
    if database is None:
        database = AsyncSqliteDatabase(connection)
        _DATABASES[connection] = database
    return database


class _HasOperationLock(Protocol):
    _operation_lock: ReentrantAsyncLock
    _db: aiosqlite.Connection | None


def serialized_operation(
    method: Callable[Concatenate[_T, _P], Awaitable[_R]],
) -> Callable[Concatenate[_T, _P], Awaitable[_R]]:
    """Serialize a complete public store operation on its shared connection."""

    @wraps(method)
    async def wrapped(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        owner = cast(_HasOperationLock, self)
        async with owner._operation_lock:
            try:
                return await method(self, *args, **kwargs)
            except BaseException:
                # A cancellation can land after a DML statement completed in
                # aiosqlite's worker but before the caller commits.  Never let
                # that implicit transaction leak into the next shared service
                # operation, where it could be committed accidentally.
                db = owner._db
                if db is not None:
                    try:
                        if getattr(db, "in_transaction", False):
                            await db.rollback()
                    except Exception:
                        # Preserve the originating error.  Store-specific
                        # cleanup and connection close remain the final guard.
                        pass
                raise

    return cast(Callable[Concatenate[_T, _P], Awaitable[_R]], wrapped)


__all__ = [
    "AsyncSqliteDatabase",
    "ReentrantAsyncLock",
    "database_for",
    "serialized_operation",
]

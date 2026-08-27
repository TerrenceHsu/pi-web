"""B7 regression tests: SQLite initialization failures release owned connections."""
from __future__ import annotations

from collections.abc import Callable

import pytest

import pi_agent_core_py.session_sqlite as session_module
import pi_agent_core_py.web.credentials.store as credential_module
import pi_agent_core_py.web.extension_store as extension_module
from pi_agent_core_py.session_sqlite import SQLiteSessionStore
from pi_agent_core_py.web.credentials.store import SQLiteCredentialStore
from pi_agent_core_py.web.extension_store import ExtensionSQLiteStore

pytestmark = pytest.mark.asyncio


class _TrackingConnection:
    def __init__(
        self,
        *,
        executescript_error: BaseException | None = None,
    ) -> None:
        self.row_factory: object | None = None
        self.executescript_error = executescript_error
        self.close_calls = 0

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def executescript(self, *_args: object, **_kwargs: object) -> None:
        if self.executescript_error is not None:
            raise self.executescript_error

    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1


def _connect_returning(
    connection: _TrackingConnection,
) -> Callable[..., object]:
    async def _connect(*_args: object, **_kwargs: object) -> _TrackingConnection:
        return connection

    return _connect


async def test_credential_store_open_closes_on_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TrackingConnection()
    monkeypatch.setattr(
        credential_module.aiosqlite,
        "connect",
        _connect_returning(connection),
    )

    async def _fail_schema(_self: SQLiteCredentialStore) -> None:
        raise RuntimeError("injected initialization failure")

    monkeypatch.setattr(SQLiteCredentialStore, "_initialize_schema", _fail_schema)

    with pytest.raises(RuntimeError, match="injected initialization failure"):
        await SQLiteCredentialStore.open(":memory:")

    assert connection.close_calls == 1


async def test_session_store_init_closes_detaches_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TrackingConnection()
    monkeypatch.setattr(
        session_module.aiosqlite,
        "connect",
        _connect_returning(connection),
    )
    store = SQLiteSessionStore(":memory:")

    async def _fail_schema() -> None:
        raise RuntimeError("injected initialization failure")

    monkeypatch.setattr(store, "_exec_schema", _fail_schema)

    with pytest.raises(RuntimeError, match="injected initialization failure"):
        await store.init()

    assert connection.close_calls == 1
    assert store.connection is None
    assert store.closed is False

    monkeypatch.undo()
    await store.init()
    assert store.connection is not None
    assert store.closed is False
    await store.close()


async def test_extension_store_init_closes_owned_connection_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TrackingConnection(
        executescript_error=RuntimeError("injected initialization failure"),
    )
    monkeypatch.setattr(
        extension_module.aiosqlite,
        "connect",
        _connect_returning(connection),
    )
    store = ExtensionSQLiteStore(":memory:")

    with pytest.raises(RuntimeError, match="injected initialization failure"):
        await store.init()

    assert connection.close_calls == 1
    assert store._db is None
    assert store.closed is False

    monkeypatch.undo()
    await store.init()
    assert store._db is not None
    assert store.closed is False
    await store.close()


async def test_extension_store_failure_never_closes_injected_connection() -> None:
    connection = _TrackingConnection(
        executescript_error=RuntimeError("injected initialization failure"),
    )
    store = ExtensionSQLiteStore(":memory:", connection=connection)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="injected initialization failure"):
        await store.init()

    assert connection.close_calls == 0
    assert store._db is None
    assert store.closed is False

"""Repository-level conformance tests for the SQLite Session backend."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from pi_agent_core_py.agent.harness.session.types import (
    BranchBounds,
    EntryCursor,
    EntryQuery,
    NewSessionEntry,
    NewSessionRecord,
    RecordQuery,
    SessionRepository,
    SessionSearchOptions,
    SessionStorage,
)
from pi_agent_core_py.messages import (
    AssistantMessage,
    TextContent,
    Usage,
    UsageCost,
    UserMessage,
)
from pi_agent_core_py.session_backends.sqlite import (
    SQLiteSessionRepository,
    SQLiteSessionSearch,
    SQLiteSessionStore,
    WriterLeaseError,
)
from pi_agent_core_py.session_backends.sqlite import migrations as sqlite_migrations
from pi_agent_core_py.session_backends.sqlite.database import (
    database_for,
    serialized_operation,
)
from pi_agent_core_py.session_backends.sqlite.migrations import SQLiteMigrationError
from pi_agent_core_py.session_backends.sqlite.repo import SessionBranchError


async def _collect_search(
    search: SQLiteSessionSearch,
    text: str,
    options: SessionSearchOptions | None = None,
):
    return [hit async for hit in search.search(text, options)]


@pytest.mark.asyncio
async def test_repository_implements_contract_and_shared_log(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "repository.sqlite")
    assert isinstance(repository, SessionRepository)
    storage = await repository.create(
        session_id="session",
        title="Review",
        metadata={"profile": "reviewer"},
    )
    assert isinstance(storage, SessionStorage)

    root = await storage.append_entry(
        NewSessionEntry(id="root", type="custom", payload={"custom_type": "note"}),
        "main",
    )
    child = await storage.append_entry(
        NewSessionEntry(id="child", type="compaction", payload={"summary": "done"}),
        "main",
    )
    await storage.create_lane("review", root.id)
    await storage.move_lane("review", child.id)
    started = await storage.append_record(
        NewSessionRecord(
            id="run-1",
            lane="main",
            type="operation_started",
            operation_kind="run",
        )
    )
    assert (await storage.find_open_operations("main"))[0].id == started.id
    await storage.append_record(
        NewSessionRecord(
            id="run-1-finished",
            lane="main",
            type="operation_finished",
            run_id="run-1",
            payload={"outcome": "completed"},
        )
    )

    metadata = await storage.get_metadata()
    assert metadata.title == "Review"
    assert metadata.metadata == {"profile": "reviewer"}
    assert await storage.get_name() == "Review"
    assert [lane.lane for lane in await storage.get_lanes()] == ["main", "review"]
    assert [entry.id for entry in await storage.find_entries()] == ["child", "root"]
    assert [record.id for record in await storage.find_records()] == [
        "run-1-finished",
        "run-1",
    ]
    log = await storage.get_log()
    assert [item.seq for item in log] == list(range(1, len(log) + 1))
    assert {item.kind for item in log} == {"entry", "fact", "lane", "record"}
    assert (await storage.get_stats()).message_count == 0

    await repository.close()


@pytest.mark.asyncio
async def test_unnamed_session_does_not_synthesize_name_fact(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "unnamed.sqlite")
    storage = await repository.create(session_id="session")

    assert await storage.get_name() is None
    first = await storage.append_entry(
        NewSessionEntry(id="root", type="context", payload={"text": "root"}),
        "main",
    )
    assert first.seq == 1

    await repository.close()


@pytest.mark.asyncio
async def test_entry_and_record_query_semantics(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "queries.sqlite")
    storage = await repository.create(session_id="session", title="Queries")
    for entry in (
        NewSessionEntry(id="root", type="context", payload={"text": "root"}),
        NewSessionEntry(
            id="note-old",
            type="custom",
            payload={"custom_type": "note", "text": "old"},
        ),
        NewSessionEntry(id="tail", type="response", payload={"text": "tail"}),
    ):
        await storage.append_entry(entry, "main")

    oldest = await storage.find_entries(EntryQuery(order="oldest_first"))
    cursor = EntryCursor(after_seq=oldest[0].seq)
    assert [
        entry.id
        for entry in await storage.find_entries(
            EntryQuery(order="oldest_first", cursor=cursor, limit=2)
        )
    ] == ["note-old", "tail"]
    assert [
        entry.id
        for entry in await storage.find_entries(
            EntryQuery(custom_type="note")
        )
    ] == ["note-old"]
    assert [
        entry.id
        for entry in await storage.find_entries_on_branch(
            EntryQuery(type="response"),
            BranchBounds(start="tail", stop_at_id="note-old"),
        )
    ] == ["tail"]

    first = await storage.append_record(
        NewSessionRecord(id="usage-1", lane="main", type="usage")
    )
    await storage.append_record(
        NewSessionRecord(id="usage-2", lane="main", type="usage")
    )
    assert [
        record.id
        for record in await storage.find_records(RecordQuery(after_seq=first.seq))
    ] == ["usage-2"]
    with pytest.raises(ValueError, match="cursor"):
        await storage.find_entries(EntryQuery(cursor=EntryCursor(after_seq=-1)))
    with pytest.raises(ValueError, match="limit"):
        await storage.find_records(RecordQuery(limit=0))
    with pytest.raises(ValueError, match="operation_kind"):
        await storage.find_records(RecordQuery(operation_kind="run"))
    with pytest.raises(ValueError, match="after_seq"):
        await storage.find_records(RecordQuery(after_seq=-1))
    with pytest.raises(ValueError, match="limit"):
        await storage.find_open_operations("main", limit=0)
    with pytest.raises(ValueError, match="after_seq"):
        await storage.get_log(after_seq=-1)

    await repository.close()


@pytest.mark.asyncio
async def test_stats_track_messages_usage_and_cost(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "stats.sqlite")
    storage = await repository.create(session_id="session", title="Stats")
    await repository.store.append_message(
        "session", UserMessage(content=[TextContent(text="hello")])
    )
    await repository.store.append_message(
        "session",
        AssistantMessage(
            content=[TextContent(text="world")],
            api="test",
            provider="test",
            model="test",
            usage=Usage(
                input=11,
                output=7,
                cache_read=3,
                cache_write=2,
                total_tokens=23,
                cost=UsageCost(total=0.25),
            ),
        ),
    )
    stats = await storage.get_stats()
    assert stats.message_count == 2
    assert stats.cached_tokens == 3
    assert stats.uncached_tokens == 13
    assert stats.total_tokens == 23
    assert stats.cost_total == 0.25
    await repository.close()


@pytest.mark.asyncio
async def test_branch_cache_can_be_repaired(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "branch.sqlite")
    storage = await repository.create(session_id="session", title="Branch")
    await storage.append_entry(
        NewSessionEntry(id="root", type="context", payload={"text": "root"}),
        "main",
    )
    tail = await storage.append_entry(
        NewSessionEntry(id="tail", type="response", payload={"text": "tail"}),
        "main",
    )
    connection = repository.store.connection
    assert connection is not None
    await connection.execute("DELETE FROM branch_tips WHERE session_id = 'session'")
    await connection.execute("DELETE FROM branch_entries WHERE session_id = 'session'")
    await connection.commit()

    with pytest.raises(SessionBranchError, match="repair"):
        await storage.find_entries_on_branch(
            EntryQuery(), BranchBounds(start=tail.id)
        )
    await repository.store.repair_branch_cache("session")
    assert [
        entry.id
        for entry in await storage.find_entries_on_branch(
            EntryQuery(order="oldest_first"), BranchBounds(start=tail.id)
        )
    ] == ["root", "tail"]
    await repository.close()


@pytest.mark.asyncio
async def test_repository_forks_branch_and_tree(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "fork.sqlite")
    source = await repository.create(session_id="source", title="Source")
    root = await source.append_entry(
        NewSessionEntry(id="root", type="context", payload={"text": "root"}),
        "main",
    )
    tail = await source.append_entry(
        NewSessionEntry(id="tail", type="response", payload={"text": "tail"}),
        "main",
    )
    await source.create_lane("thread", root.id)
    metadata = await source.get_metadata()

    branch = await repository.fork(
        metadata,
        scope="branch",
        entry_id=tail.id,
        session_id="branch-copy",
    )
    tree = await repository.fork(
        metadata,
        scope="tree",
        session_id="tree-copy",
    )
    assert [entry.payload["text"] for entry in await branch.find_entries()] == [
        "tail",
        "root",
    ]
    assert {lane.lane for lane in await tree.get_lanes()} == {"main", "thread"}
    assert (await branch.get_metadata()).parent_session_id == "source"
    assert (await tree.get_metadata()).parent_session_id == "source"
    await repository.close()


@pytest.mark.asyncio
async def test_writer_leases_reject_second_writer_and_fence_stale_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "leases.sqlite"
    first_repository = SQLiteSessionRepository(
        database,
        writer_lease_ttl_ms=120_000,
        writer_heartbeat_interval_ms=60_000,
    )
    second_repository = SQLiteSessionRepository(
        database,
        writer_lease_ttl_ms=120_000,
        writer_heartbeat_interval_ms=60_000,
    )
    first = await first_repository.create(session_id="session", title="Lease")
    metadata = await first.get_metadata()
    with pytest.raises(WriterLeaseError, match="active writer"):
        await second_repository.open(metadata)

    inspection = sqlite3.connect(database)
    inspection.execute(
        "UPDATE writer_leases SET expires_at_ms = 0 WHERE session_id = 'session'"
    )
    inspection.commit()
    inspection.close()

    second = await second_repository.open(metadata)
    with pytest.raises(WriterLeaseError, match="lease lost|active writer"):
        await first.append_entry(
            NewSessionEntry(id="stale", type="custom"), "main"
        )
    await second.append_entry(NewSessionEntry(id="current", type="custom"), "main")
    await first_repository.close()
    assert [entry.id for entry in await second.find_entries()] == ["current"]
    await second_repository.close()


@pytest.mark.asyncio
async def test_writer_initialized_fts_search_filters_and_tracks_deletes(tmp_path: Path) -> None:
    database = tmp_path / "search.sqlite"
    repository = SQLiteSessionRepository(database)
    storage = await repository.create(session_id="session", title="Search")
    search = SQLiteSessionSearch(database)
    assert await _collect_search(search, "  ") == []

    connection = repository.store.connection
    assert connection is not None
    cursor = await connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'session_search_fts'"
    )
    assert await cursor.fetchone() is not None
    await cursor.close()

    entry = await storage.append_entry(
        NewSessionEntry(
            id="auth-note",
            type="custom",
            payload={"custom_type": "note", "text": 'Find the auth "defect"'},
        ),
        "main",
    )
    hits = await _collect_search(
        search,
        "auth",
        SessionSearchOptions(entry_types=("custom",), limit=1),
    )
    assert [(hit.session_id, hit.entry_id) for hit in hits] == [
        ("session", entry.id)
    ]
    assert await _collect_search(search, 'missing "phrase"') == []

    metadata = await storage.get_metadata()
    await repository.delete(metadata)
    assert await _collect_search(search, "auth") == []
    await repository.close()


@pytest.mark.asyncio
async def test_unknown_future_migration_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE session_migrations ("
        "id TEXT PRIMARY KEY, migration_order INTEGER NOT NULL UNIQUE, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "INSERT INTO session_migrations (id, migration_order) VALUES ('999_future', 999)"
    )
    connection.commit()
    connection.close()

    store = SQLiteSessionStore(database)
    with pytest.raises(SQLiteMigrationError, match="newer application"):
        await store.init()
    assert store.closed is False
    assert store.connection is None


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_schema_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rollback.sqlite"

    def broken_migration(_resource: str) -> str:
        return "CREATE TABLE migration_probe (id INTEGER); BROKEN SQL;"

    monkeypatch.setattr(sqlite_migrations, "_load_sql", broken_migration)
    store = SQLiteSessionStore(database)
    with pytest.raises(sqlite3.OperationalError):
        await store.init()

    inspection = sqlite3.connect(database)
    objects = {
        row[0]
        for row in inspection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    applied = list(inspection.execute("SELECT id FROM session_migrations"))
    inspection.close()
    assert "migration_probe" not in objects
    assert applied == []


@pytest.mark.asyncio
async def test_cancelled_shared_operation_rolls_back_before_unlocking(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "cancel.sqlite")
    await store.init()
    connection = store.connection
    assert connection is not None
    await connection.execute("CREATE TABLE cancellation_probe (value TEXT)")
    await connection.commit()

    class CancelledWriter:
        def __init__(self) -> None:
            self._db = connection
            self._operation_lock = database_for(connection).operation_lock

        @serialized_operation
        async def write(self) -> None:
            await self._db.execute(
                "INSERT INTO cancellation_probe (value) VALUES ('uncommitted')"
            )
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await CancelledWriter().write()
    assert connection.in_transaction is False
    cursor = await connection.execute("SELECT COUNT(*) FROM cancellation_probe")
    assert (await cursor.fetchone())[0] == 0
    await cursor.close()
    await store.close()


@pytest.mark.asyncio
async def test_failed_create_clears_transactional_writer_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteSessionStore(tmp_path / "create-rollback.sqlite")
    await store.init()
    original = store._append_name_fact

    async def fail_name_fact(
        session_id: str, name: str | None, *, timestamp: int
    ) -> int:
        del session_id, name, timestamp
        raise RuntimeError("injected create failure")

    monkeypatch.setattr(store, "_append_name_fact", fail_name_fact)
    with pytest.raises(RuntimeError, match="injected"):
        await store.create_session(title="Failed", session_id="retry")
    assert await store.get_session("retry") is None

    monkeypatch.setattr(store, "_append_name_fact", original)
    created = await store.create_session(title="Retry", session_id="retry")
    assert created.id == "retry"
    await store.close()


def test_invalid_writer_lease_timing_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        SQLiteSessionStore(":memory:", writer_lease_ttl_ms=0)
    with pytest.raises(ValueError, match="less than TTL"):
        SQLiteSessionStore(
            ":memory:",
            writer_lease_ttl_ms=100,
            writer_heartbeat_interval_ms=100,
        )

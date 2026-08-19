"""Append-only SQLite Session tree / lane contracts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_agent_core_py.messages import AssistantMessage, TextContent, UserMessage
from pi_agent_core_py.session_sqlite import (
    SessionBranchError,
    SessionEntryNotFoundError,
    SessionLaneExistsError,
    SQLiteSessionStore,
    _serialize_message,
)


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="test-api",
        provider="test",
        model="test-model",
    )


def _text(message: UserMessage | AssistantMessage) -> str:
    block = message.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture
async def store(tmp_path: Path):
    result = SQLiteSessionStore(tmp_path / "tree.sqlite")
    await result.init()
    yield result
    await result.close()


async def test_new_session_has_empty_main_lane(store: SQLiteSessionStore) -> None:
    session = await store.create_session(title="tree")

    assert session.active_lane == "main"
    assert await store.get_active_leaf(session.id) is None
    lanes = await store.list_lanes(session.id)
    assert [(lane.name, lane.leaf_entry_id, lane.is_active) for lane in lanes] == [
        ("main", None, True)
    ]


async def test_append_builds_parent_chain_and_moves_leaf(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    first = await store.append_entry(session.id, _user("one"))
    second = await store.append_entry(session.id, _assistant("two"))

    assert first.parent_id is None
    assert second.parent_id == first.id
    assert await store.get_active_leaf(session.id) == second.id
    assert [entry.id for entry in await store.list_entries(session.id)] == [
        first.id,
        second.id,
    ]
    assert [_text(message) for message in await store.list_messages(session.id)] == [
        "one",
        "two",
    ]


async def test_fork_lanes_share_prefix_and_diverge_independently(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    root = await store.append_entry(session.id, _user("question"))
    main_answer = await store.append_entry(session.id, _assistant("main"))

    fork = await store.fork(
        session.id, "alternate", at_entry_id=root.id, activate=False
    )
    alternate = await store.append_entry(
        session.id, _assistant("alternate"), lane="alternate"
    )

    assert fork.leaf_entry_id == root.id
    assert [entry.id for entry in await store.list_entries(session.id, "main")] == [
        root.id,
        main_answer.id,
    ]
    assert [
        entry.id for entry in await store.list_entries(session.id, "alternate")
    ] == [root.id, alternate.id]
    assert _text((await store.list_messages(session.id))[-1]) == "main"

    await store.set_active_lane(session.id, "alternate")
    assert _text((await store.list_messages(session.id))[-1]) == "alternate"
    assert (await store.get_session(session.id)).active_lane == "alternate"  # type: ignore[union-attr]


async def test_branch_to_ancestor_then_append_preserves_abandoned_suffix(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    first = await store.append_entry(session.id, _user("q1"))
    abandoned = await store.append_entry(session.id, _assistant("a1"))

    await store.branch(session.id, first.id)
    replacement = await store.append_entry(session.id, _assistant("a2"))

    assert replacement.parent_id == first.id
    assert [entry.id for entry in await store.list_entries(session.id)] == [
        first.id,
        replacement.id,
    ]
    assert [entry.id for entry in await store.list_all_entries(session.id)] == [
        first.id,
        abandoned.id,
        replacement.id,
    ]


async def test_branch_rejects_entry_outside_current_lane_path(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    root = await store.append_entry(session.id, _user("q"))
    await store.fork(session.id, "other", at_entry_id=root.id)
    sibling = await store.append_entry(session.id, _assistant("other"), lane="other")

    with pytest.raises(SessionBranchError):
        await store.branch(session.id, sibling.id, lane="main")


async def test_duplicate_fork_name_is_rejected(store: SQLiteSessionStore) -> None:
    session = await store.create_session()
    await store.fork(session.id, "review", at_entry_id=None)
    with pytest.raises(SessionLaneExistsError):
        await store.fork(session.id, "review", at_entry_id=None)


async def test_labels_are_append_only_latest_facts(store: SQLiteSessionStore) -> None:
    session = await store.create_session()
    entry = await store.append_entry(session.id, _user("q"))

    assert (await store.set_label(session.id, entry.id, "first")).label == "first"
    assert (await store.set_label(session.id, entry.id, "second")).label == "second"
    assert (await store.set_label(session.id, entry.id, None)).label is None

    db = store._require_db()
    cursor = await db.execute(
        "SELECT value_json FROM session_facts WHERE session_id = ? ORDER BY seq",
        (session.id,),
    )
    assert [row["value_json"] for row in await cursor.fetchall()] == [
        '"first"',
        '"second"',
        "null",
    ]
    await cursor.close()


async def test_label_rejects_entry_from_other_session(
    store: SQLiteSessionStore,
) -> None:
    first = await store.create_session()
    second = await store.create_session()
    entry = await store.append_entry(first.id, _user("q"))
    with pytest.raises(SessionEntryNotFoundError):
        await store.set_label(second.id, entry.id, "wrong")


async def test_replace_appends_sibling_for_content_revision_and_keeps_message_id(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    root = await store.append_entry(session.id, _user("q"))
    old_answer = await store.append_entry(session.id, _assistant("old"))

    current = await store.list_messages(session.id)
    current[-1] = _assistant("new")
    await store.replace_messages(session.id, current)

    active = await store.list_entries(session.id)
    all_entries = await store.list_all_entries(session.id)
    new_answer = active[-1]
    assert new_answer.id != old_answer.id
    assert new_answer.parent_id == root.id == old_answer.parent_id
    assert new_answer.message_id == old_answer.message_id
    assert [_text(message) for message in await store.list_messages(session.id)] == [
        "q",
        "new",
    ]
    assert len(all_entries) == 3


async def test_switching_lane_round_trips_stable_message_ids(
    store: SQLiteSessionStore,
) -> None:
    session = await store.create_session()
    root = await store.append_entry(session.id, _user("q"))
    main = await store.append_entry(session.id, _assistant("main"))
    await store.fork(session.id, "alt", at_entry_id=root.id)
    alt = await store.append_entry(session.id, _assistant("alt"), lane="alt")

    await store.set_active_lane(session.id, "alt")
    assert [row.id for row in await store.list_persisted_messages(session.id)] == [
        root.message_id,
        alt.message_id,
    ]
    await store.set_active_lane(session.id, "main")
    assert [row.id for row in await store.list_persisted_messages(session.id)] == [
        root.message_id,
        main.message_id,
    ]


async def test_active_lane_and_leaf_survive_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.sqlite"
    first_store = SQLiteSessionStore(db_path)
    await first_store.init()
    session = await first_store.create_session()
    root = await first_store.append_entry(session.id, _user("q"))
    await first_store.fork(
        session.id, "alt", at_entry_id=root.id, activate=True
    )
    alt = await first_store.append_entry(session.id, _assistant("alt"))
    await first_store.close()

    reopened = SQLiteSessionStore(db_path)
    await reopened.init()
    restored = await reopened.get_session(session.id)
    assert restored is not None
    assert restored.active_lane == "alt"
    assert await reopened.get_active_leaf(session.id) == alt.id
    assert _text((await reopened.list_messages(session.id))[-1]) == "alt"
    await reopened.close()


async def test_legacy_linear_database_is_backfilled_once(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idx INTEGER NOT NULL,
            role TEXT NOT NULL, content_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            UNIQUE (session_id, idx)
        );
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
            content_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        """
    )
    connection.execute(
        "INSERT INTO sessions VALUES ('legacy', 'Legacy', 1, 2, '{}')"
    )
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        ("m1", "legacy", 0, "user", _serialize_message(_user("q")), 1),
    )
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        ("m2", "legacy", 1, "assistant", _serialize_message(_assistant("a")), 2),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteSessionStore(db_path)
    await migrated.init()
    first_ids = [entry.id for entry in await migrated.list_all_entries("legacy")]
    assert len(first_ids) == 2
    assert [entry.message_id for entry in await migrated.list_entries("legacy")] == [
        "m1",
        "m2",
    ]
    assert await migrated.get_active_leaf("legacy") == first_ids[-1]
    await migrated.close()

    reopened = SQLiteSessionStore(db_path)
    await reopened.init()
    assert [entry.id for entry in await reopened.list_all_entries("legacy")] == first_ids
    await reopened.close()
